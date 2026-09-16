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
import json
import math
from pathlib import Path
import shutil

from ..domain.model import MediaSlice
from ..domain.plan import CUE_TRACKS
from ..domain.timebase import TICKS_PER_SECOND
from ..storage.project_store import load_project
from ..template import default_styles, font_name, resolve_fonts
from . import ass as ass_export
from . import srt as srt_export
from .catalog import CatalogError, load_catalog, measure_all
from .plan import ProjectionError, SourceMedia, build_manifest

RENDER_SCHEMA = 'wv-export@1'


class ExportError(Exception):
    """The export cannot proceed without changing what the user approved."""


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

    Returns the published path.  ``source`` is the clip's
    :class:`~word_video.domain.model.MediaSlice` and ``asset_path`` is where the
    catalogue says that asset lives.  A slice at speed 1.0 is trimmed only; the
    file is read back once before publication so a truncated write cannot reach
    the renderer.
    """
    from ..media import executable, run, wav_duration

    source_path = Path(asset_path)
    if not source_path.is_file():
        raise ExportError('asset %s is missing: %s' % (asset_id, source_path))
    window_seconds = float(Fraction(source.source_units * source.unit_num,
                                    source.unit_den))
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = '_%.4fs_%.4fx.wav' % (window_seconds, float(source.speed))
    target = target_dir / (asset_id.replace(':', '_') + suffix)
    if target.exists():
        return target
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
        wav_duration(target)  # read it back once: a truncated write fails here
        return target
    finally:
        partial.unlink(missing_ok=True)


def export_run(project_path, *, output=None, run_name='', background=None,
               intro_video='', intro_audio='', video_codec='h264',
               slices=None, render=True, draft=True, progress=None):
    """Produce all three deliverables for one project revision.

    ``background``, ``intro_video`` and ``intro_audio`` are delivery settings, not
    project content: the document owns the editable lesson (words, stages, text
    layers, rhythm) and the caller owns which background and which countdown clip
    this delivery uses.
    """
    project = load_project(project_path)
    base = Path(project_path)
    base = base if base.is_dir() else base.parent
    if output is None:
        output = base / 'out'
    assets = load_catalog(base)
    media = measure_all(assets)
    from ..domain import solve
    solution = solve(project, media)
    styles, font_paths, font_names = _styles()
    run_dir = Path(output) / (run_name or ('rev%d' % project.revision))
    # A published batch may be checked by an independent reader that treats every
    # file under the run folder as part of the delivery, so an existing folder is
    # refused instead of merged with an older run.
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ExportError('output folder is not empty: %s' % run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    speech_dir = run_dir / '.speech'
    sources = {}
    for clip in project.clips:
        if clip.source is None or clip.role not in ('female', 'male', 'chinese'):
            continue
        asset = assets.get(clip.source.asset_id)
        if asset is None:
            raise CatalogError('clip %s needs asset %r, which the catalogue lacks'
                               % (clip.id, clip.source.asset_id))
        prepared = _prepared(clip.source.asset_id, asset.path, clip.source, speech_dir)
        sources[clip.source.asset_id] = SourceMedia(path=str(prepared), voice=asset.voice)
    view = build_manifest(solution.render, project, sources, background=background or '',
                          intro_video=intro_video, intro_audio=intro_audio,
                          video_codec=video_codec, styles=styles)
    timeline = run_dir / 'timeline.json'
    _atomic_json(timeline, view.to_dict())

    # One layout for every product: computed here, read by the captions below and
    # (W04) by the preview.
    report = ass_export.layout_for(solution.render, styles, font_names, font_paths).require_fit()
    ass_text = ass_export.build_layout_ass(solution.render, report, styles=styles,
                                           fps=view.fps)
    ass_path = ass_export.write_ass(ass_text, run_dir / 'captions.ass')

    srts = srt_export.export_srts(solution.cues, run_dir / 'srt',
                                  '%04d-%04d' % (view.first_index, view.last_index))
    draft_dir = ''
    if draft:
        from ..draft import export_draft
        draft_target = run_dir / 'editable-draft'
        export_draft(view, draft_target)
        draft_dir = str(draft_target)
    video = ''
    if render:
        from ..render import render_video
        from ..validate import validate_video
        video_path = run_dir / 'video' / 'video.mp4'
        render_video(view, video_path.parent, slices=slices,
                     progress=progress)
        validate_video(video_path, view)
        video = str(video_path)
    result = ExportResult(run_dir=str(run_dir), video=video, srts=tuple(srts),
                          draft=draft_dir, ass=str(ass_path),
                          timeline=str(timeline),
                          report={'project_revision': project.revision,
                                  'plan_identity': solution.render.identity(),
                                  'cue_identity': solution.cues.identity(),
                                  'total_ticks': solution.render.total_ticks,
                                  'total_frames': view.total_frames,
                                  'fps': view.fps, 'width': view.width,
                                  'height': view.height,
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


def _atomic_json(path, value):
    from ..media import atomic_json
    atomic_json(path, value)
    return path
