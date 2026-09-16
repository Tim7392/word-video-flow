"""Resolve asset ids to files and measurements: one registry, structured refusals.

A clip names an **asset id**, never a path (architecture §4: the locator lives
beside the document, not inside it).  Without a resolver, an editor can show a clip
but cannot say which file it uses, cannot offer to fix a missing one, and cannot
feed the solver — which is why preview had to be handed an ``assets`` mapping by
its caller.

Two layers, deliberately small:

``AssetRef`` / ``AssetIndex``
    the registry as a project document (``assets.json``, kind ``wv-assets@1``):
    id → path plus whatever was measured (``units`` on the source grid), with
    lookups that raise ``MISSING_ASSET`` / ``DUPLICATE_ASSET`` /
    ``ASSET_FILE_MISSING`` / ``UNKNOWN_DURATION`` and a ``problems()`` report for a
    UI that wants to show every broken reference at once.
``resolve(project, ...)``
    the bridge: every asset id the project names, resolved through the registry if
    there is one and otherwise treating the id as the path it is today.  Measuring
    is *not* re-implemented here — a caller that wants lengths passes ``measure``
    (the media package owns probing) and a caller that has none still gets paths
    and a list of what is missing.

This is not the content-addressed asset library: registration is explicit, and a
stale entry is reported rather than silently re-pointed (W08 owns ingest).
"""
from dataclasses import dataclass, replace
from pathlib import Path

from ..domain.errors import (AssetFileError, DuplicateAssetError, MissingAssetError,
                            SchemaError, UnknownDurationError)
from ..domain.model import MediaInfo, Project
from ..domain.plan import Conflict
from .project_store import read_document, write_document

SCHEMA = 'wv-assets@1'
ASSETS_FILENAME = 'assets.json'
KINDS = ('', 'audio', 'video', 'image')


def assets_path(path):
    """Accept either the project directory or the registry document itself."""
    target = Path(path)
    return target / ASSETS_FILENAME if target.is_dir() else target


def _text(value, name, *, path='', allow_empty=True):
    if not isinstance(value, str):
        raise SchemaError('%s must be a string' % name, path=path)
    if not allow_empty and not value:
        raise SchemaError('%s must not be empty' % name, path=path)
    return value


@dataclass(frozen=True)
class AssetRef:
    """One registered asset: what it is called, where it is, what was measured.

    ``units`` is a length on the source's own grid (samples for audio, frames for
    video) with ``unit_num / unit_den`` seconds per unit, exactly like
    :class:`~word_video.domain.model.MediaInfo`; ``None`` means "not measured yet"
    and is reported as ``UNKNOWN_DURATION`` instead of being guessed.
    """

    asset_id: str
    path: str
    units: int | None = None
    unit_num: int = 1
    unit_den: int = 48000
    kind: str = ''
    sha256: str = ''

    def __post_init__(self):
        path = 'asset:%s' % self.asset_id if isinstance(self.asset_id, str) else 'asset'
        _text(self.asset_id, 'asset id', path=path, allow_empty=False)
        _text(self.path, 'asset path', path=path, allow_empty=False)
        if self.units is not None:
            if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units <= 0:
                raise SchemaError('asset units must be a positive integer', path=path)
            for name in ('unit_num', 'unit_den'):
                value = getattr(self, name)
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise SchemaError('asset %s must be a positive integer' % name,
                                      path=path)
        if self.kind not in KINDS:
            raise SchemaError('unknown asset kind %r' % (self.kind,), path=path,
                              hint='可用种类：%s' % '、'.join(k or '(未标注)' for k in KINDS))
        _text(self.sha256, 'asset sha256', path=path)

    @property
    def measured(self):
        return self.units is not None

    @property
    def media_info(self):
        """The measurement as the solver wants it, or ``None`` when unknown."""
        if self.units is None:
            return None
        return MediaInfo(asset_id=self.asset_id, units=self.units,
                         unit_num=self.unit_num, unit_den=self.unit_den)

    def to_dict(self):
        return {'asset_id': self.asset_id, 'path': self.path, 'units': self.units,
                'unit_num': self.unit_num, 'unit_den': self.unit_den,
                'kind': self.kind, 'sha256': self.sha256}

    @classmethod
    def from_dict(cls, value, path='assets'):
        keys = ('asset_id', 'path', 'units', 'unit_num', 'unit_den', 'kind', 'sha256')
        if not isinstance(value, dict):
            raise SchemaError('asset must be an object', path=path)
        unknown = sorted(set(value) - set(keys))
        missing = sorted(set(keys[:2]) - set(value))
        if unknown:
            raise SchemaError('asset has unknown field(s): %s' % ', '.join(unknown),
                              path=path)
        if missing:
            raise SchemaError('asset is missing field(s): %s' % ', '.join(missing),
                              path=path)
        return cls(asset_id=value['asset_id'], path=value['path'],
                   units=value.get('units'), unit_num=value.get('unit_num', 1),
                   unit_den=value.get('unit_den', 48000), kind=value.get('kind', ''),
                   sha256=value.get('sha256', ''))


@dataclass(frozen=True)
class AssetIndex:
    """A project's asset registry, plus the folder relative paths resolve against.

    ``folder`` is *not* stored in the document: it is where the registry was read
    from, so a project folder can be moved and its relative entries keep working.
    """

    project_id: str = ''
    refs: tuple = ()
    folder: str = ''
    schema: str = SCHEMA

    def __post_init__(self):
        if self.schema != SCHEMA:
            raise SchemaError('unsupported asset document %r' % (self.schema,),
                              path='assets', hint='本版本只读写 %s' % SCHEMA)
        _text(self.project_id, 'project_id', path='assets')
        _text(self.folder, 'asset folder', path='assets')
        object.__setattr__(self, 'refs', tuple(self.refs))
        seen = set()
        for ref in self.refs:
            if not isinstance(ref, AssetRef):
                raise SchemaError('assets must contain AssetRef objects', path='assets')
            if ref.asset_id in seen:
                raise DuplicateAssetError('asset %r is registered twice' % (ref.asset_id,),
                                          path='asset:%s' % ref.asset_id,
                                          hint='同一个 id 只能指向一个文件；先删掉重复登记')
            seen.add(ref.asset_id)

    # -- construction ----------------------------------------------------
    @classmethod
    def empty(cls, project_id='', folder=''):
        return cls(project_id=project_id, folder=str(folder or ''))

    @classmethod
    def of(cls, refs, project_id='', folder=''):
        return cls(project_id=project_id, refs=tuple(refs), folder=str(folder or ''))

    def with_ref(self, ref):
        """Register one asset; an id that already exists is a conflict."""
        if self.has(ref.asset_id):
            raise DuplicateAssetError(
                'asset %r is already registered' % (ref.asset_id,),
                path='asset:%s' % ref.asset_id,
                hint='要改指向就先 without() 掉旧登记，或换一个 id')
        return replace(self, refs=self.refs + (ref,))

    def without(self, asset_id):
        return replace(self, refs=tuple(ref for ref in self.refs
                                        if ref.asset_id != asset_id))

    def with_folder(self, folder):
        return replace(self, folder=str(folder or ''))

    # -- lookup (raising, with the object in the error) -------------------
    def has(self, asset_id):
        return any(ref.asset_id == asset_id for ref in self.refs)

    def ref(self, asset_id):
        for ref in self.refs:
            if ref.asset_id == asset_id:
                return ref
        raise MissingAssetError('no asset %r is registered' % (asset_id,),
                                path='asset:%s' % asset_id,
                                hint='先把素材登记到 assets.json（AssetIndex.with_ref），'
                                     '或修正片段上的 asset_id')

    def path(self, asset_id, *, must_exist=True):
        """Absolute path of one asset; a file that is not there is reported."""
        candidate = self.candidate_path(asset_id)
        if must_exist and not candidate.is_file():
            raise AssetFileError(
                'asset %r points at %s, which is not there' % (asset_id, candidate),
                path='asset:%s' % asset_id,
                hint='文件被移动或删除了：重新登记该 id，或把文件放回去')
        return str(candidate)

    def candidate_path(self, asset_id):
        """Where the file *should* be, without checking that it exists."""
        ref = self.ref(asset_id)
        candidate = Path(ref.path)
        if not candidate.is_absolute() and self.folder:
            candidate = Path(self.folder) / candidate
        return candidate

    def media_info(self, asset_id):
        ref = self.ref(asset_id)
        info = ref.media_info
        if info is None:
            raise UnknownDurationError(
                'asset %r has not been measured' % (asset_id,),
                path='asset:%s' % asset_id,
                hint='登记时给出 units（源网格上的长度），或先用媒体层探测再登记')
        return info

    def media_map(self, asset_ids=None):
        """``{asset_id: MediaInfo}`` for the solver, in registry order.

        Only measured entries appear: an unmeasured asset must fail a solve loudly
        (``UNKNOWN_DURATION``) rather than be replaced by an estimate here.
        """
        wanted = None if asset_ids is None else tuple(asset_ids)
        table = {}
        for ref in self.refs:
            if wanted is not None and ref.asset_id not in wanted:
                continue
            info = ref.media_info
            if info is not None:
                table[ref.asset_id] = info
        return table

    # -- reporting (never raises: a UI wants the whole list) --------------
    def problems(self, asset_ids=None):
        """Every broken reference, as plan-style conflicts, in registry order."""
        wanted = None if asset_ids is None else tuple(asset_ids)
        found = []
        for ref in self.refs:
            if wanted is not None and ref.asset_id not in wanted:
                continue
            if not self.candidate_path(ref.asset_id).is_file():
                found.append(Conflict(
                    code=AssetFileError.code,
                    message='asset %r points at %s, which is not there'
                            % (ref.asset_id, self.candidate_path(ref.asset_id)),
                    path='asset:%s' % ref.asset_id,
                    hint='文件被移动或删除了：重新登记该 id，或把文件放回去'))
            elif ref.media_info is None:
                found.append(Conflict(
                    code=UnknownDurationError.code,
                    message='asset %r has not been measured' % (ref.asset_id,),
                    path='asset:%s' % ref.asset_id,
                    hint='登记时给出 units，或先用媒体层探测再登记'))
        return tuple(found)

    # -- document IO -----------------------------------------------------
    _FIELDS = ('schema', 'project_id', 'assets')

    def to_dict(self):
        return {'schema': self.schema, 'project_id': self.project_id,
                'assets': [ref.to_dict() for ref in self.refs]}

    @classmethod
    def from_dict(cls, value, folder=''):
        if not isinstance(value, dict):
            raise SchemaError('asset registry must be a JSON object', path='assets')
        unknown = sorted(set(value) - set(cls._FIELDS))
        missing = sorted({'schema', 'project_id', 'assets'} - set(value))
        if unknown:
            raise SchemaError('asset registry has unknown field(s): %s'
                              % ', '.join(unknown), path='assets')
        if missing:
            raise SchemaError('asset registry is missing field(s): %s'
                              % ', '.join(missing), path='assets')
        assets = value['assets']
        if not isinstance(assets, list):
            raise SchemaError('asset registry assets must be a list', path='assets')
        return cls(project_id=value['project_id'],
                   refs=tuple(AssetRef.from_dict(item) for item in assets),
                   folder=str(folder or ''), schema=value['schema'])

    @classmethod
    def load(cls, path):
        """Read the registry beside a project; the folder comes from the file."""
        source = assets_path(path)
        return cls.from_dict(read_document(source, 'assets'), folder=str(source.parent))

    def save(self, path):
        """Write the registry atomically and return the document path."""
        target = Path(path)
        if target.is_dir():
            target = target / ASSETS_FILENAME
        return write_document(self.to_dict(), target)


def referenced_ids(project):
    """Every asset id the project names, in project order, without duplicates."""
    ids = []
    for clip in project.clips:
        for asset_id in ((clip.source.asset_id if clip.source else ''),
                         clip.audio_asset):
            if asset_id and asset_id not in ids:
                ids.append(asset_id)
    return tuple(ids)


def resolve(project, registry=None, folder='', measure=None, load_registry=True):
    """Resolve a project's assets through its registry, or through the ids themselves.

    ``registry`` may be an :class:`AssetIndex`; when it is omitted and
    ``load_registry`` is true, ``assets.json`` beside the project folder is read if
    it exists.  An id the registry does not know falls back to the id *as a path*,
    which is what today's projects carry, so an editor works before the asset
    library exists.

    ``measure`` is an optional ``asset_id -> (units, unit_num, unit_den) | MediaInfo
    | None`` callable: probing belongs to the media package, and this layer only
    records what it is told.  Without it, unresolved lengths stay unknown and
    :meth:`AssetIndex.problems` reports them.
    """
    if not isinstance(project, Project):
        raise SchemaError('resolve needs a Project document', path='project')
    folder = str(folder or '')
    index = registry
    if index is None and load_registry and folder:
        candidate = Path(folder) / ASSETS_FILENAME
        index = AssetIndex.load(candidate) if candidate.is_file() else None
    if index is not None and not isinstance(index, AssetIndex):
        raise SchemaError('registry must be an AssetIndex', path='assets')
    refs = []
    for asset_id in referenced_ids(project):
        ref = index.ref(asset_id) if index is not None and index.has(asset_id) else \
            AssetRef(asset_id=asset_id, path=asset_id)
        if not ref.measured and measure is not None:
            ref = _measured(ref, measure(asset_id))
        refs.append(ref)
    project_id = index.project_id if index is not None and index.project_id else \
        project.project_id
    return AssetIndex.of(refs, project_id=project_id, folder=folder)


def _measured(ref, reading):
    """Attach one measurement to a ref, in whichever shape the prober returns it."""
    if reading is None:
        return ref
    if isinstance(reading, MediaInfo):
        return replace(ref, units=reading.units, unit_num=reading.unit_num,
                       unit_den=reading.unit_den)
    try:
        units, unit_num, unit_den = reading
    except (TypeError, ValueError):
        raise SchemaError('measure() must return MediaInfo, a (units, num, den) '
                          'triple or None', path='asset:%s' % ref.asset_id) from None
    return replace(ref, units=units, unit_num=unit_num, unit_den=unit_den)
