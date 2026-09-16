"""One command: ``project.json`` + measured media -> the three deliverables.

The three products share one timeline and one layout, and that is enforced here
rather than promised:

* the **plan** is solved once (:func:`word_video.domain.solve`);
* the **layout** is computed once from that plan
  (:class:`word_video.layout.LayoutSurface`) and every caption, every subtitle
  and the draft read the same placements;
* the **MP4** and the **editable draft** are produced by the verified engine
  (:mod:`word_video.render`, :mod:`word_video.draft`) driven by
  :func:`word_video.exporters.plan.build_manifest`, which refuses a plan whose
  picture the engine cannot express exactly - so "the plan drove it" is a checked
  claim, not a hopeful one;
* the **five SRT tracks** come straight from the ``CuePlan``.

Speech audio is prepared once per asset (source window + speed applied exactly
once) into the job folder, and the paths the manifest carries are those prepared
files: nothing downstream applies speed a second time.

Outputs land under ``<output>/<run>/``: ``video/``, ``srt/``, ``editable-draft/``
and ``timeline.json``, i.e. the same shape the verified chain produces, so the M0
acceptance checker and a human both find what they expect.
"""
from dataclasses import dataclass, field
from fractions import Fraction
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import shutil
import time

from ..domain.errors import ProjectError
from ..domain.model import MediaSlice
from ..domain.plan import CUE_TRACKS
from ..domain.timebase import TICKS_PER_SECOND
from ..storage.project_store import load_project
from ..template import default_styles, font_name, resolve_fonts
from . import ass as ass_export
from . import srt as srt_export
from .background import BackgroundError, background_segments, build_background_track
from .catalog import CatalogError, load_catalog, measure_all
from .plan import ProjectionError, SourceMedia, build_manifest, frame_at

RENDER_SCHEMA = 'wv-export@1'
#: The per-asset speech record published with every run (see `write_speech_record`).
SPEECH_SCHEMA = 'wv-speech@1'
SPEECH_FILENAME = 'speech.json'
#: How many speech assets are prepared at once.  Each is one ffmpeg writing a short
#: mono PCM file, so the cost is the process rather than the work.
PREPARE_WORKERS = min(8, os.cpu_count() or 4)


class ExportError(Exception):
    """The export cannot proceed without changing what the user approved."""


class DeliveryError(ProjectError):
    """The delivery has no usable picture: A's own check, worded once.

    The background is a *delivery* setting, so this exporter cannot invent one.
    Rather than let ``Path('')`` - which is the current working directory - reach
    the draft's ``shutil.copy2`` and come back as ``PermissionError`` on a folder,
    the run refuses with the code, message, hint and fixes A's
    ``application.batches.check_delivery`` already produces, so the CLI, the batch
    planner and the coordinator all say the same thing about the same input.
    """

    code = 'BACKGROUND_MISSING'

    def __init__(self, check):
        problem = check.problems[0]
        super().__init__(problem.message, path=problem.path, hint=problem.hint)
        self.code = problem.code
        self.fixes = tuple(check.fixes)


@dataclass
class ExportResult:
    run_dir: str
    video: str
    srts: tuple = ()
    draft: str = ''
    ass: str = ''
    timeline: str = ''
    report: dict = field(default_factory=dict)

    def to_dict(self):
        return {'schema': RENDER_SCHEMA, 'run_dir': self.run_dir, 'video': self.video,
                'srts': list(self.srts), 'draft': self.draft, 'ass': self.ass,
                'timeline': self.timeline, 'report': self.report}


def _styles(project_styles=None):
    """The merged style table plus the fonts it resolved, in one place.

    Overrides arrive as style *names* -> settings; a font override that cannot be
    read is refused by ``default_styles``, so a substituted face never slips in.
    """
    merged = {name: dict(style) for name, style in default_styles().items()}
    for name, override in (project_styles or {}).items():
        merged.setdefault(name, {}).update(override or {})
    paths = {name: style.get('font') for name, style in merged.items()}
    names = {name: style.get('font_name') for name, style in merged.items()}
    missing = sorted(name for name, path in paths.items() if not path)
    if missing:
        raise ExportError('no usable font for style(s): %s; run doctor'
                          % ', '.join(missing))
    return merged, paths, names


def _prepared(asset_id, asset_path, source, target_dir, rate=48000):
    """One asset's speech over its source window, at tempo exactly once.

    Returns ``(path, raw_seconds, rendered_seconds, digest)``.  The two durations
    are the numbers an independent reader needs to re-derive the timeline: the
    original's own length (what a stage must reserve room for) and the prepared
    file's (what the mixer consumes) - they differ by the tempo pass, and a
    published run has to say both rather than leave them to be re-probed.

    A slice at speed 1.0 is only trimmed, and the file is read back before
    publication so a truncated write cannot reach the renderer.
    """
    from ..media import duration as whole_duration
    from ..media import executable, run, wav_duration
    from ..media.core import sha256

    source_path = Path(asset_path)
    if not source_path.is_file():
        raise ExportError('asset %s is missing: %s' % (asset_id, source_path))
    window_seconds = float(Fraction(source.source_units * source.unit_num,
                                    source.unit_den))
    raw_seconds = float(Fraction(source.source_end * source.unit_num, source.unit_den)) \
        if source.source_start == 0 else None
    if raw_seconds is None:
        # A trimmed window: the "original" length is still the whole source's, which
        # only the caller's asset knows - read it once, here, where the file is.
        raw_seconds = float(whole_duration(source_path))
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = '_%.4fs_%.4fx.wav' % (window_seconds, float(source.speed))
    target = target_dir / (asset_id.replace(':', '_') + suffix)
    if target.exists():
        return target, raw_seconds, wav_duration(target), sha256(target)
    partial = target.with_suffix('.partial.wav')
    args = [executable('ffmpeg'), '-v', 'error', '-nostdin', '-n']
    if source.source_start:
        args += ['-ss', '%.9f' % (source.source_start * source.unit_num
                                  / float(source.unit_den))]
    args += ['-i', str(source_path), '-t', '%.9f' % window_seconds]
    speed = float(source.speed)
    if speed != 1.0:
        args += ['-af', 'atempo=%.9f' % speed]
    args += ['-ar', str(rate), '-ac', '1', '-c:a', 'pcm_s16le', str(partial)]
    try:
        run(args, timeout=600)
        if wav_duration(partial) <= 0:
            raise ExportError('prepared audio for %s is empty' % asset_id)
        partial.replace(target)
        rendered = wav_duration(target)   # read it back: a truncated write fails here
        return target, raw_seconds, rendered, sha256(target)
    finally:
        partial.unlink(missing_ok=True)


def _asset_source(base):
    """Where this project's assets come from: the registry, else the catalogue.

    One document is the goal, and it is A's ``assets.json`` (``wv-assets@1``): the
    editor registers an asset once, and the solver, the renderer and this exporter
    all read that one file.  ``media.json`` (W02's catalogue) is still accepted for
    a project written before the registry existed - the manifest is measured from
    the files either way - and which document was read is reported in the run, so a
    reader never has to guess where the assets came from.
    """
    from ..storage.assets import ASSETS_FILENAME, AssetIndex

    registry = Path(base) / ASSETS_FILENAME
    if registry.is_file():
        index = AssetIndex.load(base)
        entries = {}
        media = {}
        for ref in index.refs:
            entries[ref.asset_id] = _CatalogEntry(
                asset_id=ref.asset_id, path=index.path(ref.asset_id),
                voice=str(getattr(ref, 'voice', '') or ''))
            if ref.media_info is not None:
                media[ref.asset_id] = ref.media_info
        return entries, media, True
    assets = load_catalog(base)
    return assets, measure_all(assets), False


class _CatalogEntry:
    """The two fields the exporter needs from an asset: where it is, whose voice."""

    __slots__ = ('asset_id', 'path', 'voice')

    def __init__(self, asset_id, path, voice=''):
        self.asset_id = asset_id
        self.path = path
        self.voice = voice


def export_run(project_path, *, output=None, run_name='', background=None,
               intro_video='', intro_audio='', video_codec=None,
               slices=None, render=True, draft=True, progress=None):
    """Produce all three deliverables for one project revision.

    ``background`` and ``video_codec`` are *delivery* settings, not project content,
    and the project folder owns them in ``delivery.json`` (A's ``storage/delivery.py``,
    which the editor writes).  A parameter given here **overrides** that sidecar - the
    caller knows what this particular delivery is - and an omitted one follows the
    project, so the headless entry produces what the editor would.  Before this, the
    CLI rendered h264 with no background while the editor rendered h265 with the
    project's background: two entries, one project, two different pictures (H0's
    reproduction).

    A run that has to draw a picture (``render`` or ``draft``) refuses **before the
    run folder exists** when the delivery still has no usable background, using A's
    :func:`~word_video.application.batches.check_delivery` wording: an empty path is
    the working directory, and the old failure arrived as a ``PermissionError`` on a
    folder - about the cwd, days after its cause.

    ``intro_video``/``intro_audio`` are the **legacy fallback** for a project that
    has no intro layer of its own (the "seconds without media" case, and the old
    fixture shape).  A project that *does* carry an intro layer is measured from
    that layer and the parameters are ignored - the plan is the source of truth
    for its own intro, exactly as it is for the words.
    """
    project = load_project(project_path)
    base = Path(project_path)
    base = base if base.is_dir() else base.parent
    if output is None:
        output = base / 'out'
    # The delivery sidecar is the project's own answer; an explicit parameter wins
    # over it, and the codec still falls back to h264 when neither says anything.
    from ..storage.delivery import delivery_settings

    settings = delivery_settings(base)
    background = str(background or '').strip() or settings['background']
    video_codec = str(video_codec or '').strip() or settings['video_codec'] or 'h264'
    if render or draft:
        from ..application.batches import ExportProfile, check_delivery

        check = check_delivery(ExportProfile(background=background))
        if not check.ready:
            # Covers "no background at all" (BACKGROUND_MISSING) and a path that is
            # not a readable file (BACKGROUND_FILE_MISSING/UNREADABLE), before the run
            # folder is created.
            raise DeliveryError(check)
    # Stage timing, so "the export took four minutes" can be answered with where they
    # went instead of a guess.  The names are the verified chain's (`jobs.py` writes
    # `stage_seconds` for speech / timeline / draft / srt / render); a stage that is
    # not run is absent rather than zero, which tells a reader whether a number is
    # small or was never measured.
    stage_seconds = {}
    wall_started = time.monotonic()

    def spend(name, started):
        stage_seconds[name] = round(time.monotonic() - started, 3)
        return time.monotonic()

    started = wall_started
    assets, media, from_registry = _asset_source(base)
    from ..application.intro import measure_project_intro
    from ..domain import solve
    # A project with an intro layer is measured from its own media; a project
    # without one keeps the verified engine's reserved-seconds fallback.
    intro_measure = measure_project_intro(project) if project.intro_clip() else None
    solution = solve(project, media, intro_measure)
    # Style overrides travel with the plan, so the renderer and the canvas read the
    # same table (a role the project does not override still follows the default).
    styles, font_paths, font_names = _styles(solution.render.style_table())
    started = spend('plan', started)
    run_dir = Path(output) / (run_name or ('rev%d' % project.revision))
    # A published batch may be checked by an independent reader that treats every
    # file under the run folder as part of the delivery, so an existing folder is
    # refused instead of merged with an older run.
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ExportError('output folder is not empty: %s' % run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    speech_dir = run_dir / '.speech'
    sources = {}
    prepared_facts = {}
    speech_jobs = []
    for clip in project.clips:
        if clip.source is None:
            continue
        if clip.role in ('female', 'male', 'chinese'):
            asset = assets.get(clip.source.asset_id)
            if asset is None:
                raise CatalogError('clip %s needs asset %r, which the catalogue lacks'
                                   % (clip.id, clip.source.asset_id))
            speech_jobs.append((clip, asset))
            continue
        if clip.is_intro:
            # The intro layer's measurement already resolved its media to a real
            # file, and the intro is not looped by this exporter (the renderer does
            # that), so the path is carried through as-is.
            sources.setdefault(clip.source.asset_id,
                               SourceMedia(path=clip.source.asset_id))
            continue
        # A picture layer (the background): carried through unchanged, because the
        # background is looped by whoever renders it and needs no tempo pass.
        sources.setdefault(clip.source.asset_id, SourceMedia(path=clip.source.asset_id))
    # One ffmpeg per speech asset, at tempo, into the run's own `.speech/`.  They are
    # independent passes, and 150 of them in a row measured 27-32 s of the export;
    # run together it is the same commands, in the same order, with the same output.
    if speech_jobs:
        with ThreadPoolExecutor(max_workers=PREPARE_WORKERS) as pool:
            prepared = list(pool.map(
                lambda job: _prepared(job[0].source.asset_id, job[1].path,
                                      job[0].source, speech_dir),
                speech_jobs))
    else:
        prepared = []
    for (clip, asset), facts in zip(speech_jobs, prepared):
        path, raw_seconds, rendered_seconds, digest = facts
        sources[clip.source.asset_id] = SourceMedia(path=str(path), voice=asset.voice)
        prepared_facts[clip.source.asset_id] = {
            'asset_id': clip.source.asset_id, 'role': clip.role,
            'record_id': clip.record_id, 'text': clip.text,
            'voice': asset.voice, 'path': str(path),
            'raw_seconds': round(raw_seconds, 6),
            'rendered_seconds': round(rendered_seconds, 6),
            'source_seconds': round(raw_seconds, 6), 'sha256': digest}
    started = spend('speech', started)
    view = build_manifest(solution.render, project, sources, background=background or '',
                          intro_video=intro_video, intro_audio=intro_audio,
                          video_codec=video_codec, styles=styles)
    # A background the member cut in two: every planned piece gets a file of its own,
    # looped inside itself exactly like a whole background, and the manifest carries
    # the pieces so the draft keeps one editable segment per planned item.  With one
    # piece nothing changes at all (the red line for this feature).
    pieces = background_segments(solution.render, sources)
    if len(pieces) > 1:
        view.background_segments = [
            _background_piece(piece, index, run_dir, view)
            for index, piece in enumerate(pieces, start=1)]
    timeline = run_dir / 'timeline.json'
    _atomic_json(timeline, view.to_dict())
    # The per-asset speech record: what an independent reader needs to re-derive the
    # timeline instead of taking it on trust.  Written here, before the run record,
    # so a failure later leaves no record claiming a complete run.
    speech_record = write_speech_record(run_dir, view, prepared_facts,
                                        solution.render)
    started = spend('timeline', started)

    # One layout for every product: computed here, read by the captions below and
    # (W04) by the preview.
    report = ass_export.layout_for(solution.render, styles, font_names, font_paths).require_fit()
    ass_text = ass_export.build_layout_ass(solution.render, report, styles=styles,
                                           fps=view.fps)
    ass_path = ass_export.write_ass(ass_text, run_dir / 'captions.ass')
    started = spend('layout', started)

    srts = srt_export.export_srts(solution.cues, run_dir / 'srt',
                                  '%04d-%04d' % (view.first_index, view.last_index))
    started = spend('srt', started)
    draft_dir = ''
    if draft:
        from ..draft import export_draft
        draft_target = run_dir / 'editable-draft'
        export_draft(view, draft_target)
        draft_dir = str(draft_target)
        started = spend('draft', started)
    video = ''
    slices_used = 0
    if render:
        from ..render import render_video
        from ..validate import validate_video
        video_path = run_dir / 'video' / 'video.mp4'
        # ``timings`` is filled by the renderer with what happened *inside* the call:
        # the mix pass and the picture pass are separate costs and the old chain only
        # ever reported their sum as "render".
        inner = {}
        render_video(view, video_path.parent, slices=slices,
                     progress=progress, timings=inner)
        validate_video(video_path, view)
        video = str(video_path)
        slices_used = inner.pop('slices', 0)
        stage_seconds.update(inner)
        started = spend('render', started)
    stage_seconds['total'] = round(time.monotonic() - wall_started, 3)
    result = ExportResult(run_dir=str(run_dir), video=video, srts=tuple(srts),
                          draft=draft_dir, ass=str(ass_path),
                          timeline=str(timeline),
                          report={'project_revision': project.revision,
                                  'plan_identity': solution.render.identity(),
                                  'cue_identity': solution.cues.identity(),
                                  'speech_record': str(run_dir / SPEECH_FILENAME),
                                  'speech_assets': len(speech_record['records']),
                                  'asset_source': 'registry' if from_registry
                                  else 'catalogue',
                                  'total_ticks': solution.render.total_ticks,
                                  'total_frames': view.total_frames,
                                  'fps': view.fps, 'width': view.width,
                                  'height': view.height,
                                  'stage_seconds': dict(sorted(
                                      stage_seconds.items(),
                                      key=lambda item: -item[1])),
                                  'seconds_wall': stage_seconds['total'],
                                  'slices': slices_used,
                                  'conflicts': [item.to_dict()
                                                for item in solution.render.conflicts],
                                  'layout': {'exact_metrics': report.exact_metrics,
                                             'placements': len(report.placements),
                                             'overflowing': []},
                                  'tracks': {track: len(solution.cues.by_track()[track])
                                             for track in CUE_TRACKS}})
    _atomic_json(run_dir / 'export.json', result.to_dict())
    _publish_record(run_dir)
    return result


def _publish_record(run_dir):
    """``complete.json``: the digest of everything this run published.

    The same record the verified chain writes, and the one the independent
    acceptance checker reads: ``{files: [{path, sha256}]}`` covering every file in
    the run folder.  It is written last, so a failure earlier leaves no record
    claiming a complete run - which is exactly what a reader must be able to trust.
    """
    from ..media import atomic_json, sha256

    run_dir = Path(run_dir)
    recorded = run_dir / 'complete.json'
    published = sorted((path for path in run_dir.rglob('*')
                        if path.is_file() and path != recorded),
                       key=lambda path: str(path).lower())
    if not published:
        raise ExportError('nothing was published in %s' % run_dir)
    return atomic_json(recorded, {'files': [{'path': str(path), 'sha256': sha256(path)}
                                            for path in published]})


def _background_piece(piece, index, run_dir, view):
    """One planned background item as its own looped file, plus its stage.

    The file is built here rather than by the renderer because the *loop* belongs
    to the item: each piece fills its own stage, so the assembled picture has no
    hole and no repeat across the cut.  The returned dict is what the draft turns
    into one editable segment per item.
    """
    target = Path(run_dir) / ('background-%d.mp4' % index)
    build_background_track([piece], target, fps=view.fps,
                           size=(view.width, view.height))
    return dict(piece.to_dict(), path=str(target))


def write_speech_record(run_dir, view, prepared_facts, plan):
    """One record per speech asset, next to the audio it describes.

    An independent checker has to be able to ask "is this stage as long as the
    rhythm plus the recording it actually plays?" - and answering that needs the
    recording's **own** length, the prepared file's length, the text, the role and
    the voice.  The verified chain kept exactly that in
    ``audio-cache/<token>/complete.json``; the new entry point prepares its speech
    under ``.speech/`` instead, so without this document the ``timing`` face of the
    acceptance checker has nothing to recompute against and reports PASS while
    having checked nothing (the false-green H0 found: ``data_missing=9``).

    The record also carries the **planned stage** for each asset (ticks and frames),
    so a reader can compare item by item instead of re-deriving the plan.

    It is written as part of publication, before ``complete.json``, so a run that
    fails leaves no record claiming a complete one - the same rule as the run
    record itself.
    """
    records = []
    for item in getattr(plan, 'audio', ()):
        facts = prepared_facts.get(item.source.asset_id if item.source else None)
        if facts is None:
            # A plan item with no prepared audio would have failed earlier; being
            # explicit here beats writing a record that silently omits a stage.
            raise ExportError('no prepared speech for %s (asset %r)'
                              % (item.clip_id, item.source.asset_id
                                 if item.source else None))
        records.append(dict(
            facts, clip_id=item.clip_id,
            text=item.text,
            start_ticks=item.start_ticks, end_ticks=item.end_ticks,
            duration_ticks=item.duration_ticks,
            start_frame=frame_at(item.start_ticks, view.fps),
            end_frame=frame_at(item.end_ticks, view.fps),
            stage_seconds=round(float(Fraction(item.duration_ticks, 720000)), 6)))
    document = {'schema': SPEECH_SCHEMA, 'mode': 'prepared',
                'fps': view.fps, 'sample_rate': 48000, 'speed': view.speed,
                'total_frames': view.total_frames, 'records': records}
    target = Path(run_dir) / SPEECH_FILENAME
    _atomic_json(target, document)
    return document


def _atomic_json(path, value):
    from ..media import atomic_json
    atomic_json(path, value)
    return path
