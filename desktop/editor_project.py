"""The folder a member opens: the document, its assets, the delivery.

Three files and one cache directory, each owned by exactly one layer:

``project.json``
    A's document (``wv-project@2``), written by A's atomic store.  The editor
    reads and writes it through :class:`word_video.application.session.Session`
    and nothing else.
``assets.json``
    A's asset registry (``wv-assets@1``): ``asset_id -> path`` plus what the media
    layer measured.  This is the *one* place a path is looked up, and
    :meth:`ProjectFolder.diagnostics` asks its ``problems()`` for the whole list of
    broken references rather than discovering them one exception at a time.
``media.json``
    B's exporter catalogue.  ``export_run`` reads this file today, and it is the
    only document that also carries each asset's **voice**, which ``AssetRef`` has
    no field for.  It is written from the same ``AssetRef`` list as ``assets.json``
    on every save, in the same call, so the two cannot describe different files -
    ``tests/test_desktop_editor_project.py`` asserts they agree after every write
    and after every repair.  When the exporter moves to ``wv-assets@1`` (reported to
    H0) the bridge and the voices move with it and this file disappears.
``delivery.json``
    the settings that are *not* document content: which background, which codec.
    B's exporter takes the background as a delivery parameter
    (``export_run(background=...)``), so putting it in a sidecar keeps the editor
    and the exporter describing the same delivery instead of the editor keeping a
    private opinion.  The intro is **not** here: since B's W07 the intro is a layer
    of the project, measured from its own media, and the editor creates it that way.
``.preview/``
    prepared speech for the canvas, at the project's speed, cached per asset.
    Disposable: deleting the folder costs one ffmpeg pass per asset and changes
    nothing about the delivery.

Nothing here edits the project.  Corrections to the *folder* (a file moved, a voice
missing) are written here; corrections to the *document* go through
:class:`desktop.editor_model.EditorState` and A's commands.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import json

from word_video.domain.model import MediaInfo
from word_video.domain.plan import Conflict
from word_video.exporters import catalog as catalog_module
from word_video.exporters.catalog import Asset, CatalogError
from word_video.storage.assets import (ASSETS_FILENAME, AssetIndex, AssetRef,
                                       referenced_ids)
from word_video.storage.project_store import PROJECT_FILENAME, load_project, save_project

from . import editor_notices as notices

__all__ = ['Delivery', 'DELIVERY_FILENAME', 'PREVIEW_DIRNAME', 'ProjectFolder',
           'assets_agree']

#: Delivery sidecar schema; the editor's own file, versioned like every other.
DELIVERY_SCHEMA = 'wv-delivery@1'
DELIVERY_FILENAME = 'delivery.json'
#: Prepared preview audio.  One flat folder: an asset id per file, so a rebuild
#: is a lookup rather than a scan.
PREVIEW_DIRNAME = '.preview'
_DELIVERY_FIELDS = ('background', 'intro_video', 'intro_audio', 'video_codec')


@dataclass(frozen=True)
class Delivery:
    """What this delivery renders with, and nothing about the lesson itself.

    ``intro_video``/``intro_audio`` are the exporter's *legacy fallback* for a
    project with no intro layer; the editor creates an intro layer, so they are
    normally empty and the plan is the source of truth for the intro.
    """

    background: str = ''
    intro_video: str = ''
    intro_audio: str = ''
    video_codec: str = 'h264'

    def to_dict(self):
        return {'schema': DELIVERY_SCHEMA, 'background': self.background,
                'intro_video': self.intro_video, 'intro_audio': self.intro_audio,
                'video_codec': self.video_codec}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('delivery settings must be an object')
        unknown = sorted(set(value) - set(_DELIVERY_FIELDS) - {'schema'})
        if unknown:
            raise ValueError('delivery settings have unknown field(s): %s'
                             % ', '.join(unknown))
        return cls(background=str(value.get('background') or ''),
                   intro_video=str(value.get('intro_video') or ''),
                   intro_audio=str(value.get('intro_audio') or ''),
                   video_codec=str(value.get('video_codec') or 'h264'))

    @classmethod
    def load(cls, path):
        source = Path(path)
        if source.is_dir():
            source = source / DELIVERY_FILENAME
        if not source.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(source.read_text(encoding='utf-8')))
        except ValueError as error:
            raise ValueError('%s: %s' % (source, error)) from None


def assets_agree(folder):
    """``(ok, differences)``: does the exporter's catalogue name what the registry does?

    The bridge between A's registry and B's catalogue is only honest while the two
    files agree about the assets the exporter actually uses; this is what lets a
    test say so after every write, instead of hoping two writers stayed in step.
    """
    folder = Path(folder)
    registry = AssetIndex.load(folder / ASSETS_FILENAME)
    catalog = catalog_module.load_catalog(folder)
    problems = []
    for ref in registry.refs:
        entry = catalog.get(ref.asset_id)
        if entry is None:
            if _exported(ref):
                problems.append('media.json is missing %s' % ref.asset_id)
        elif Path(entry.path) != Path(ref.path):
            problems.append('%s: assets.json says %s, media.json says %s'
                            % (ref.asset_id, ref.path, entry.path))
    for asset_id in catalog:
        if not registry.has(asset_id):
            problems.append('assets.json is missing %s' % asset_id)
    return (not problems), problems


def _exported(ref):
    """Whether the exporter prepares from this ref (speech), or measures it itself."""
    return ref.kind in ('', 'audio')


class ProjectFolder:
    """One project directory: the document, its registry, its delivery, its cache."""

    def __init__(self, path, project, refs=None, voices=None, delivery=None):
        self.path = Path(path)
        self.project = project
        self.refs = {ref.asset_id: ref for ref in (refs or ())}
        self.voices = dict(voices or {})
        self.delivery = delivery or Delivery()
        #: Assets the project names that the registry does not know, resolved the
        #: way ``wv-assets@1`` documents: the id *is* the path.  Kept apart from
        #: ``refs`` so that merely opening a project never rewrites its registry -
        #: a repair is what registers an asset, not a read.
        self.extra = {}
        self._measured = False
        self._errors = {}
        self._intro_measure = None
        self._intro_error = None
        self._intro_revision = None

    # -- identity --------------------------------------------------------
    @property
    def project_path(self):
        return self.path / PROJECT_FILENAME

    @property
    def assets_path(self):
        return self.path / ASSETS_FILENAME

    @property
    def catalog_path(self):
        """B's catalogue: the exporter reads this one today."""
        return self.path / catalog_module.CATALOG_FILENAME

    @property
    def delivery_path(self):
        return self.path / DELIVERY_FILENAME

    @property
    def preview_dir(self):
        return self.path / PREVIEW_DIRNAME

    def index(self):
        """The registry as A's :class:`AssetIndex`, folder-relative paths included."""
        return AssetIndex.of(tuple(self.refs[key] for key in sorted(self.refs)),
                             project_id=self.project.project_id, folder=str(self.path))

    def to_dict(self):
        return {'path': str(self.path), 'project_id': self.project.project_id,
                'revision': self.project.revision, 'schema': self.project.schema,
                'clips': len(self.project.clips), 'records': len(self.project.records),
                'assets': len(self.refs), 'delivery': self.delivery.to_dict()}

    # -- opening and creating --------------------------------------------
    @classmethod
    def open(cls, path):
        """Read a folder; a missing registry is tolerated and reported, not fatal."""
        folder = cls(path, load_project(path))
        if folder.assets_path.is_file():
            registry = AssetIndex.load(folder.assets_path)
            folder.refs = {ref.asset_id: ref for ref in registry.refs}
        if folder.catalog_path.is_file():
            try:
                for asset_id, asset in catalog_module.load_catalog(folder.path).items():
                    folder.voices[asset_id] = asset.voice
            except CatalogError:
                folder.voices = {}
        folder.delivery = Delivery.load(folder.path)
        return folder

    @classmethod
    def create(cls, path, project, refs=None, voices=None, delivery=None):
        """Write a new folder: document, registry, catalogue and delivery settings."""
        folder = cls(path, project, refs, voices, delivery)
        Path(path).mkdir(parents=True, exist_ok=True)
        folder.save_project()
        folder.save_assets()
        folder.save_delivery()
        return folder

    # -- writing ---------------------------------------------------------
    def save_project(self, project=None):
        self.project = project if project is not None else self.project
        self._intro_revision = None
        return save_project(self.project, self.project_path)

    def save_assets(self):
        """Write both registries from one list, in one call.

        They are two documents because two layers own two formats, not because the
        editor has two opinions: ``assets.json`` is the registry A's resolver reads
        and carries every asset, and ``media.json`` is written from the same refs
        for the exporter, which prepares speech from it.  Non-speech refs (the
        intro's picture) are left out of the exporter's catalogue on purpose: it
        measures the intro from the plan itself, and its audio-first measurement of
        a packetised track is not a window anybody should cut media on.
        """
        self.index().save(self.assets_path)
        assets = {asset_id: Asset(asset_id=asset_id, path=ref.path,
                                  voice=self.voices.get(asset_id, ''))
                  for asset_id, ref in self.refs.items() if _exported(ref)}
        if assets:
            catalog_module.save_catalog(assets, self.path)
        return self.assets_path

    def save_delivery(self):
        from word_video.media import atomic_json
        return atomic_json(self.delivery_path, self.delivery.to_dict())

    def set_delivery(self, **fields):
        self.delivery = replace(self.delivery, **fields)
        self.save_delivery()
        return self.delivery

    # -- assets ----------------------------------------------------------
    def ref(self, asset_id):
        return self.refs.get(asset_id) or self.extra.get(asset_id)

    def all_refs(self):
        """Registered refs plus the id-as-path fallbacks, in a stable order."""
        merged = dict(self.extra)
        merged.update(self.refs)
        return {key: merged[key] for key in sorted(merged)}

    def unregistered(self):
        """Referenced ids the registry does not know (A's id-as-path fallback)."""
        return tuple(asset_id for asset_id in referenced_ids(self.project)
                     if asset_id not in self.refs)

    def sync_fallbacks(self):
        """Build the in-memory fallback refs for unregistered ids (never written)."""
        from word_video.storage.assets import resolve as resolve_assets
        index = resolve_assets(self.project, registry=self.index(),
                               folder=str(self.path))
        known = {ref.asset_id for ref in index.refs}
        self.extra = {ref.asset_id: ref for ref in index.refs
                      if ref.asset_id not in known or ref.asset_id not in self.refs}
        return self.extra

    def asset_path(self, asset_id):
        """The path the registry (or the id-as-path fallback) gives, or ``''``.

        The file is not checked here: :meth:`problems` reports what is missing, so a
        caller that wants a path can have one and a caller that wants the whole list
        of breakage asks for it once.
        """
        ref = self.ref(asset_id)
        if ref is None:
            return ''
        candidate = Path(ref.path)
        if not candidate.is_absolute():
            candidate = self.path / candidate
        return str(candidate)

    def add_asset(self, asset_id, path, *, voice='', kind='', measure=True):
        """Register (or re-point) one asset and probe it through the media package."""
        ref = AssetRef(asset_id=asset_id, path=str(path), kind=kind)
        if measure:
            ref = self._probe(ref)
        self.refs[asset_id] = ref
        self.extra.pop(asset_id, None)
        if voice:
            self.voices[asset_id] = voice
        self.save_assets()
        self._measured = True
        return ref

    def set_asset_file(self, asset_id, path, voice=None):
        """Repair action: point one asset at another file and re-measure it."""
        if voice is None:
            voice = self.voices.get(asset_id, '')
        return self.add_asset(asset_id, path, voice=voice,
                              kind=(self.ref(asset_id).kind
                                    if self.ref(asset_id) is not None else ''))

    def set_voice(self, asset_id, voice):
        """Repair action: record the voice an existing file was made with."""
        if asset_id not in self.refs:
            raise KeyError(asset_id)
        self.voices[asset_id] = str(voice)
        self.save_assets()
        return self.voices[asset_id]

    def measure(self, refresh=False):
        """Probe every asset the project needs, once, through B's media measurement.

        One unreadable file must not hide the state of the other eleven, so each
        asset is measured on its own and its refusal is remembered for
        :meth:`diagnostics` instead of aborting the folder.  ``Exception`` is
        caught on purpose: this deliberately probes files that may be anything at
        all, and a probe failure is information here, not a crash.
        """
        if self._measured and not refresh:
            return self._table()
        self.sync_fallbacks()
        errors = {}
        for asset_id, ref in sorted(self.all_refs().items()):
            if ref.measured and not refresh:
                continue
            probed = self._probe(ref, errors)
            if asset_id in self.refs:
                self.refs[asset_id] = probed
            else:
                self.extra[asset_id] = probed
        self._errors = errors
        self._measured = True
        if errors or refresh:
            self.save_assets()
        return self._table()

    def _probe(self, ref, errors=None):
        """Measure one asset; a failure *clears* any earlier measurement.

        Keeping the last known length for a file that has since become unreadable
        is how a timeline silently describes media that is no longer there, so a
        failed probe drops the measurement and the solver then refuses the stage
        with ``UNKNOWN_DURATION`` - loudly, and about the right asset.
        """
        try:
            info = catalog_module.measure(Asset(asset_id=ref.asset_id, path=ref.path,
                                                voice=self.voices.get(ref.asset_id, '')))
        except Exception as error:              # noqa: BLE001 - reported, not raised
            if errors is not None:
                errors[ref.asset_id] = '%s: %s' % (type(error).__name__, error)
            return replace(ref, units=None)
        return replace(ref, units=info.units, unit_num=info.unit_num,
                       unit_den=info.unit_den)

    def _table(self):
        """``{asset_id: MediaInfo}`` of everything measured, without probing."""
        table = {}
        for asset_id, ref in self.all_refs().items():
            info = ref.media_info
            if info is not None:
                table[asset_id] = info
        return table

    def media_table(self, refresh=False):
        """``{asset_id: MediaInfo}`` for everything measured (probing if needed)."""
        return self.measure(refresh=refresh)

    def media_errors(self):
        self.measure()
        return dict(self._errors)

    def problems(self):
        """Every broken reference: the registry's report plus the unknown ids.

        ``referenced_ids`` is what the *document* names, so an id that is neither
        registered nor an existing file is reported here rather than discovered as
        an exception three layers down.  An unregistered id that *is* a file is the
        documented id-as-path fallback: usable, and not a problem.
        """
        self.measure()
        found = list(self.index().problems(referenced_ids(self.project)))
        for asset_id in self.unregistered():
            ref = self.extra.get(asset_id)
            if ref is None or ref.media_info is None:
                found.append(Conflict(
                    code='MISSING_ASSET',
                    message='asset %r is not registered and is not a file' % (asset_id,),
                    path='asset:%s' % asset_id,
                    hint='用"选择配音文件"登记它，或把 assets.json 放回工程目录'))
        return tuple(found)

    # -- intro -----------------------------------------------------------
    def intro_measurement(self):
        """The project's intro layer measured, or ``None`` (with the error kept).

        The intro's length is a property of its media (A-4), and the same function
        the exporter calls does the measuring, so the preview, the timeline and the
        delivery cannot disagree about how long the countdown is.
        """
        clip = self.project.intro_clip()
        if clip is None:
            return None
        if self._intro_revision != self.project.revision:
            from word_video.application.intro import measure_project_intro
            try:
                self._intro_measure = measure_project_intro(self.project)
                self._intro_error = None
            except Exception as error:          # noqa: BLE001 - shown as a notice
                self._intro_measure, self._intro_error = None, error
            self._intro_revision = self.project.revision
        return self._intro_measure

    @property
    def intro_error(self):
        self.intro_measurement()
        return self._intro_error

    # -- pre-flight ------------------------------------------------------
    def diagnostics(self, project=None):
        """Everything about this folder that would stop a delivery, with repairs.

        The asset references come from A's own ``problems()`` so the editor reports
        the same codes the storage layer defines, and each one is turned into a
        notice located at the word and the role that names it.  On top of that the
        editor checks the two things the storage layer cannot know about: the fonts
        the styles resolved to, and the delivery's own files.
        """
        project = project if project is not None else self.project
        found = []
        by_asset = {}
        for clip in project.clips:
            if clip.source is not None and clip.source.asset_id:
                by_asset.setdefault(clip.source.asset_id, clip.id)
        errors = self.media_errors()
        for conflict in self.problems():
            asset_id = conflict.path.partition(':')[2]
            located = self.asset_path(asset_id)
            found.append(notices.notice_from_asset_conflict(
                project, conflict, clip_id=by_asset.get(asset_id, ''),
                # Only a file that is genuinely absent is worth naming; an unknown
                # id has no path to show and says so instead.
                path=located if located and not Path(located).is_file() else '',
                reason=errors.get(asset_id, '')))
        for asset_id, ref in sorted(self.refs.items()):
            # Only a reading *stage* needs a voice: it is what the draft labels the
            # audio with.  The intro is picture and its asset has no voice to name.
            if ref.measured and not self.voices.get(asset_id) \
                    and asset_id in speech_assets(project):
                found.append(notices.missing_voice_notice(
                    project, speech_assets(project)[asset_id], asset_id))
        if self.intro_error is not None:
            found.append(notices.notice_from_error(project, self.intro_error))
        found.extend(split_layer_notices(project))
        found.extend(self._font_notices())
        found.extend(self._delivery_notices())
        found.sort(key=lambda notice: (notice.severity != notices.SEVERITY_BLOCK,
                                       notice.severity != notices.SEVERITY_WARN))
        return tuple(found)

    def _font_notices(self):
        """The styles' own font files, checked once (never substituted)."""
        try:
            from word_video.template import default_styles
            styles = default_styles()
        except Exception as error:                     # noqa: BLE001 - reported, not raised
            return [notices.Notice(code=notices.EDITOR_MISSING_FONT,
                                   message='样式表不可用：%s' % error,
                                   severity=notices.SEVERITY_BLOCK,
                                   hint='导出会因为找不到字体而拒绝')]
        found = []
        for name, style in sorted(styles.items()):
            path = style.get('font')
            if not path or not Path(path).is_file():
                found.append(notices.missing_font_notice(name, path or '(未解析)'))
        return found

    def _delivery_notices(self):
        """The delivery's own files: a background that is not there.

        Not document content, so the repair is in the delivery panel - but it is
        still media the member must have, and a render that starts without it
        produces a black lesson instead of a message.
        """
        found = []
        for name, label in (('background', '背景视频'), ('intro_video', '片头视频'),
                            ('intro_audio', '片头音轨')):
            value = str(getattr(self.delivery, name) or '')
            if value and not Path(value).is_file():
                found.append(notices.Notice(
                    code=notices.EDITOR_MISSING_FILE,
                    message='%s不存在：%s' % (label, value),
                    severity=notices.SEVERITY_BLOCK,
                    hint='在"交付设置"里重新选择文件；不会被静默替换',
                    actions=(notices.Action(notices.ACTION_OPEN_DELIVERY, '打开交付设置'),)))
        return found

    # -- preview ---------------------------------------------------------
    def preview_assets(self, project=None, *, speed=None, refresh=False):
        """``{asset_id: prepared wav}`` at the project's speed, cached in ``.preview``.

        The preview plays files at the *project's* timeline length, so it needs the
        same tempo pass the exporter applies (``export_run._prepared``).  It is done
        here, once per asset, into the project's own cache directory - not into the
        media cache B owns, and never as a second mixdown of the whole lesson.
        """
        from word_video.media import prepare_audio
        project = project if project is not None else self.project
        factor = float(project.speed if speed is None else speed)
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        resolved = {}
        for clip in project.clips:
            if clip.source is None or clip.role not in ('female', 'male', 'chinese'):
                continue
            asset_id = clip.source.asset_id
            if asset_id in resolved:
                continue
            path = self.asset_path(asset_id)
            if not path or not Path(path).is_file():
                continue
            target = self.preview_dir / ('%s_%.4fx.wav' % (_safe(asset_id), factor))
            if refresh and target.is_file():
                target.unlink()
            if not target.is_file():
                try:
                    prepare_audio(Path(path), target, factor, project.fps_num)
                except (OSError, ValueError, RuntimeError, FileExistsError):
                    continue
            resolved[asset_id] = str(target)
        return resolved

    def background_slice(self, project=None):
        """The delivery background as a media slice, or ``None`` when unset.

        The exporter draws the background file itself (``export_run(background=)``),
        so the preview reads the *same* setting and measures it the same way -
        one delivery setting, one picture, no second opinion about what plays
        behind the words.
        """
        from word_video.domain.model import MediaSlice
        path = self.delivery.background
        if not path or not Path(path).is_file():
            return None
        try:
            info = catalog_module.measure(Asset(asset_id='layer:background', path=path))
        except (CatalogError, OSError, ValueError, RuntimeError):
            return None
        return MediaSlice(asset_id='layer:background', source_start=0,
                          source_end=int(info.units), unit_num=int(info.unit_num),
                          unit_den=int(info.unit_den))


def _safe(asset_id):
    return ''.join(character if character.isalnum() or character in '-_.' else '_'
                   for character in str(asset_id))


def speech_assets(project):
    """``{asset_id: clip_id}`` of the reading stages; the only assets with a voice."""
    found = {}
    for clip in project.clips:
        if clip.role in ('female', 'male', 'chinese') and clip.source is not None:
            found.setdefault(clip.source.asset_id, clip.id)
    return found


#: Layers a member may split today (A's ``SPLITTABLE_ROLES``) whose pieces the
#: exporter's projection cannot draw yet - it still carries one window per layer.
SPLIT_GATED_ROLES = ('background', 'intro')


def split_layer_notices(project):
    """A split media layer blocks the *delivery*, not the editing.

    Reported here rather than in the timeline because it is a property of the
    delivery: the member may split the intro or the background and keep working,
    but publishing now would draw only one of the halves - a film that silently
    disagrees with the project, which is exactly what this product refuses to do.
    """
    found = []
    for role in SPLIT_GATED_ROLES:
        clips = tuple(clip.id for clip in project.clips if clip.role == role)
        if len(clips) > 1:
            found.append(notices.split_layer_notice(project, role, clips))
    return found


def background_item(project, background, total_ticks):
    """The background as a plan item for the *preview* only.

    It is a projection of a delivery setting, not an editable clip: the exporter
    takes the background file as a parameter and never reads a background clip, so
    offering one in the timeline would be a control that changes nothing in the
    delivery.  The canvas, on the other hand, must show the picture the member
    will get, so the preview plan carries it.

    The end is rounded **up** to a bucket, and the reason is measured rather than
    aesthetic: the preview caches its windowed copy and its proxy by window length,
    so a drag that lengthens the lesson by a second would otherwise re-copy ~26 MB
    of 1080p background and re-encode its 720p proxy on every mouse release.  A
    30 s bucket makes small edits reuse one window; the item still covers the whole
    lesson, so nothing about what is drawn changes.
    """
    from word_video.domain.plan import PlanItem
    end = max(int(total_ticks), _bucket_up(int(total_ticks)))
    return PlanItem(clip_id='layer.background', role='background', record_id='',
                    start_ticks=0, end_ticks=end, source=background)


#: Preview window bucket: see :func:`background_item`.
BACKGROUND_BUCKET_TICKS = 30 * 720000


def _bucket_up(ticks):
    if ticks <= 0:
        return BACKGROUND_BUCKET_TICKS
    steps = -(-int(ticks) // BACKGROUND_BUCKET_TICKS)
    return steps * BACKGROUND_BUCKET_TICKS


def with_background(plan, project, background):
    """``plan`` plus the delivery background, as the preview sees it."""
    if background is None or plan is None:
        return plan
    return replace(plan, video=(background_item(project, background, plan.total_ticks),)
                   + tuple(plan.video))
