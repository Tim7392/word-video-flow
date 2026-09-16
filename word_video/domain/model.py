"""The editable project document (``wv-project@1``).

One editable truth (top-level architecture §4): the project owns the frozen
input snapshot (``records``), the expanded editable units (``clips``) and the
lesson settings.  Solved time — :class:`~word_video.domain.plan.RenderPlan` and
:class:`~word_video.domain.plan.CuePlan` — is derived from this document by
:mod:`word_video.domain.compile` and is never written back.

Role names come from the shared contract: ``contracts.ROLES`` is the teaching
order (English female voice → English male voice → Chinese meaning) and
``contracts.DISPLAY_TRACKS`` the on-screen text layers.  Clips carry their own
text so the document stays editable without a second content table.
"""
from dataclasses import dataclass
from fractions import Fraction

from ..contracts import DISPLAY_TRACKS, ROLES
from .errors import (DuplicateIdError, InvalidTimeError, SchemaError,
                     SourceRangeError)
from .rhythm import (DEFAULT_EXTRA, DEFAULT_FIRST_SIX, DEFAULT_FOOTER,
                     DEFAULT_FPS_NUM, DEFAULT_GAP_S, DEFAULT_HEIGHT,
                     DEFAULT_INTRO_S, DEFAULT_SPEED, DEFAULT_TITLE,
                     DEFAULT_WIDTH, intro_ticks)
from .timebase import (TICKS_PER_SECOND, TimeExpr, finite_number, frame_seconds,
                       frame_ticks, half_up, positive_int, rational)

SCHEMA = 'wv-project@2'
#: The first document revision.  It has no intro layer and no clip audio asset, so
#: it still loads unchanged; anything that carries an intro is @2 and an older
#: reader refuses it instead of silently dropping the countdown.
SCHEMA_V1 = 'wv-project@1'
SCHEMAS = (SCHEMA_V1, SCHEMA)

#: Speech stages in teaching order; the same tuple the verified engine renders.
TEACHING_STAGES = tuple(ROLES)
#: On-screen text layers of one record.
DISPLAY_LAYERS = tuple(DISPLAY_TRACKS)
#: Whole-project layers.  ``intro`` is the reference countdown clip: it is picture,
#: not a reading stage, so it belongs to the project rather than to a record.
PROJECT_LAYERS = ('title', 'subtitle', 'footer', 'background', 'intro')
CLIP_ROLES = TEACHING_STAGES + DISPLAY_LAYERS + PROJECT_LAYERS
#: Roles whose media is speech and must therefore have a measured length.
AUDIO_ROLES = TEACHING_STAGES
#: Paint order for the video list: background, then the intro over it, text on top.
VIDEO_ORDER = ('background', 'intro', 'title', 'subtitle', 'footer') + DISPLAY_LAYERS
#: Role of the reference intro clip; the only role that may carry ``audio_asset``.
INTRO_ROLE = 'intro'


def _text(value, name, *, path='', allow_empty=True):
    if not isinstance(value, str):
        raise SchemaError('%s must be a string' % name, path=path)
    if not allow_empty and not value:
        raise SchemaError('%s must not be empty' % name, path=path)
    return value


def _strict(value, name, required, path, optional=()):
    """A stored object must carry the declared fields and no others.

    ``optional`` names fields a newer revision added: a document written before
    them still loads, and an unknown field is still refused.
    """
    if not isinstance(value, dict):
        raise SchemaError('%s must be an object' % name, path=path)
    unknown = sorted(set(value) - set(required) - set(optional))
    missing = sorted(set(required) - set(value))
    if unknown:
        raise SchemaError('%s has unknown field(s): %s' % (name, ', '.join(unknown)),
                          path=path)
    if missing:
        raise SchemaError('%s is missing field(s): %s' % (name, ', '.join(missing)),
                          path=path)
    return value


@dataclass(frozen=True)
class MediaInfo:
    """What the media layer measured for one asset (W02 probes this).

    ``units`` is a length on the source's own grid — samples for audio, frames
    for video — with ``unit_num / unit_den`` seconds per unit.  Keeping the grid
    is what makes a 44.1 kHz source exact: 4410 samples are 0.1 s and 72000
    ticks, with no float in the path.
    """

    asset_id: str
    units: int
    unit_num: int = 1
    unit_den: int = 48000

    def __post_init__(self):
        _text(self.asset_id, 'asset_id', path='media', allow_empty=False)
        positive_int(self.units, 'media units', path='media')
        if isinstance(self.unit_num, bool) or not isinstance(self.unit_num, int) or self.unit_num <= 0:
            raise SchemaError('media unit_num must be a positive integer', path='media')
        if isinstance(self.unit_den, bool) or not isinstance(self.unit_den, int) or self.unit_den <= 0:
            raise SchemaError('media unit_den must be a positive integer', path='media')

    @property
    def seconds(self):
        """Exact measured length in seconds."""
        return Fraction(self.units * self.unit_num, self.unit_den)

    def to_dict(self):
        return {'asset_id': self.asset_id, 'units': self.units,
                'unit_num': self.unit_num, 'unit_den': self.unit_den}


@dataclass(frozen=True)
class MediaSlice:
    """A window on one media asset: source in/out plus the processing chain.

    Source positions stay on the source grid so trimming never invents media;
    ``speed`` is declared once here and the mixer applies it once (architecture
    §8.3 forbids applying speed twice).
    """

    asset_id: str
    source_start: int = 0
    source_end: int = 0
    unit_num: int = 1
    unit_den: int = 48000
    speed: float = 1.0
    gain_db: float = 0.0

    def __post_init__(self):
        path = 'media:%s' % self.asset_id if isinstance(self.asset_id, str) else 'media'
        _text(self.asset_id, 'asset_id', path=path, allow_empty=False)
        for name in ('source_start', 'source_end'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaError('media slice %s must be an integer' % name, path=path)
        if self.source_start < 0:
            raise SourceRangeError('media slice source_start must not be negative',
                                   path=path)
        if self.source_end <= self.source_start:
            raise SourceRangeError('media slice must cover at least one unit',
                                   path=path,
                                   hint='source_end 必须大于 source_start')
        for name in ('unit_num', 'unit_den'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SchemaError('media slice %s must be a positive integer' % name,
                                  path=path)
        finite_number(self.speed, 'media slice speed', path=path, positive=True)
        finite_number(self.gain_db, 'media slice gain_db', path=path)

    @property
    def source_units(self):
        return self.source_end - self.source_start

    @property
    def seconds(self):
        """Exact length of the source window, before speed."""
        return Fraction(self.source_units * self.unit_num, self.unit_den)

    @property
    def duration_ticks(self):
        """The window on the project grid, speed applied exactly once."""
        return half_up(self.seconds * TICKS_PER_SECOND / rational(self.speed))

    def to_dict(self):
        return {'asset_id': self.asset_id, 'source_start': self.source_start,
                'source_end': self.source_end, 'unit_num': self.unit_num,
                'unit_den': self.unit_den, 'speed': self.speed,
                'gain_db': self.gain_db}

    @classmethod
    def from_dict(cls, value, path='media'):
        keys = ('asset_id', 'source_start', 'source_end', 'unit_num', 'unit_den',
                'speed', 'gain_db')
        _strict(value, 'media slice', keys, path)
        return cls(**{key: value[key] for key in keys})


@dataclass(frozen=True)
class Record:
    """One word of the input snapshot: the frozen word list entry.

    ``index`` is the position in the source word list when it is known (0 when
    the caller has none); it is what the batch naming and the on-screen
    ``速通（151–200）`` line are read from.
    """

    id: str
    word: str
    phonetic: str = ''
    meaning: str = ''
    spoken_meaning: str = ''
    index: int = 0

    def __post_init__(self):
        path = 'record:%s' % self.id if isinstance(self.id, str) else 'record'
        _text(self.id, 'record id', path=path, allow_empty=False)
        _text(self.word, 'record word', path=path, allow_empty=False)
        for name in ('phonetic', 'meaning', 'spoken_meaning'):
            _text(getattr(self, name), 'record %s' % name, path=path)
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise SchemaError('record index must be a non-negative integer', path=path)

    def field(self, name):
        """Read one bound field by name; used by the template expander."""
        if name not in ('word', 'phonetic', 'meaning', 'spoken_meaning'):
            raise SchemaError('record has no field %r' % (name,),
                              path='record:%s' % self.id)
        return getattr(self, name)

    def to_dict(self):
        return {'id': self.id, 'word': self.word, 'phonetic': self.phonetic,
                'meaning': self.meaning, 'spoken_meaning': self.spoken_meaning,
                'index': self.index}

    @classmethod
    def from_dict(cls, value, path='record'):
        keys = ('id', 'word', 'phonetic', 'meaning', 'spoken_meaning', 'index')
        _strict(value, 'record', keys, path)
        return cls(**{key: value[key] for key in keys})


@dataclass(frozen=True)
class Clip:
    """One editable unit: a speech stage, a text layer or a media layer.

    ``start`` is a :class:`~word_video.domain.timebase.TimeExpr`: absolute for
    everything the template expands (so moving one clip cannot silently drag
    others) or a declared link when the user asked for one.  ``duration_ticks``
    is ``None`` when the length still follows the teaching rhythm of the media
    and explicit once the clip has been trimmed.
    """

    id: str
    role: str
    record_id: str = ''
    start: object = None
    duration_ticks: int | None = None
    text: str = ''
    source: MediaSlice | None = None
    #: A standalone sound that can carry this layer when its own media has none.
    #: Only the intro layer uses it (the legacy ``lesson.intro_audio``); which of
    #: the two actually sounds is decided by ``word_video.media.resolve_intro_audio``
    #: and never here.
    audio_asset: str = ''

    def __post_init__(self):
        path = 'clip:%s' % self.id if isinstance(self.id, str) else 'clip'
        _text(self.id, 'clip id', path=path, allow_empty=False)
        if self.role not in CLIP_ROLES:
            raise SchemaError('unknown clip role %r' % (self.role,), path=path,
                              hint='可用角色：%s' % ', '.join(CLIP_ROLES))
        if self.start is None:
            object.__setattr__(self, 'start', TimeExpr.at(0))
        elif not isinstance(self.start, TimeExpr):
            raise SchemaError('clip start must be a TimeExpr', path=path)
        if self.role in PROJECT_LAYERS:
            if self.record_id:
                raise SchemaError('a %s layer belongs to the project, not a record'
                                  % self.role, path=path)
        elif not self.record_id:
            raise SchemaError('role %s needs a record_id' % self.role, path=path)
        _text(self.record_id, 'clip record_id', path=path)
        if self.duration_ticks is not None:
            if isinstance(self.duration_ticks, bool) or not isinstance(self.duration_ticks, int):
                raise SchemaError('clip duration must be an integer tick count', path=path)
            if self.duration_ticks <= 0:
                raise InvalidTimeError('clip duration must be positive', path=path)
        _text(self.text, 'clip text', path=path)
        if self.source is not None and not isinstance(self.source, MediaSlice):
            raise SchemaError('clip source must be a MediaSlice', path=path)
        if self.source is None and self.role in AUDIO_ROLES:
            raise SchemaError('speech clip %s needs a media source' % self.id,
                              path=path)
        _text(self.audio_asset, 'clip audio_asset', path=path)
        if self.audio_asset and self.role != INTRO_ROLE:
            # A second, standalone sound belongs to the intro layer; a reading
            # stage speaks the media in its own slice.
            raise SchemaError('only the %s layer carries a separate audio asset'
                              % INTRO_ROLE, path=path)
        if self.role == INTRO_ROLE and self.source is None:
            raise SchemaError('the intro layer needs its video source', path=path,
                              hint='没有片头素材时用 project.intro_s 兜底，不要建空的片头片段')

    @property
    def is_speech(self):
        return self.role in AUDIO_ROLES

    @property
    def is_intro(self):
        return self.role == INTRO_ROLE

    def to_dict(self):
        document = {'id': self.id, 'role': self.role, 'record_id': self.record_id,
                    'start': self.start.to_dict(),
                    'duration_ticks': self.duration_ticks, 'text': self.text,
                    'source': self.source.to_dict() if self.source else None}
        if self.audio_asset:
            # Written only when it is used: a document with no intro layer keeps
            # exactly the fields it had before the intro existed.
            document['audio_asset'] = self.audio_asset
        return document

    @classmethod
    def from_dict(cls, value, path='clip'):
        keys = ('id', 'role', 'record_id', 'start', 'duration_ticks', 'text', 'source')
        _strict(value, 'clip', keys, path, optional=('audio_asset',))
        source = value['source']
        return cls(id=value['id'], role=value['role'], record_id=value['record_id'],
                   start=TimeExpr.from_dict(value['start']),
                   duration_ticks=value['duration_ticks'], text=value['text'],
                   source=MediaSlice.from_dict(source, path=path) if source is not None else None,
                   audio_asset=value.get('audio_asset', ''))


@dataclass(frozen=True)
class Project:
    """The project document; every edit produces a new instance and revision."""

    project_id: str = 'project'
    records: tuple = ()
    clips: tuple = ()
    title: str = DEFAULT_TITLE
    footer: str = DEFAULT_FOOTER
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps_num: int = DEFAULT_FPS_NUM
    fps_den: int = 1
    sample_rate: int = 48000
    channels: int = 1
    speed: float = DEFAULT_SPEED
    intro_s: float = DEFAULT_INTRO_S
    gap_s: float = DEFAULT_GAP_S
    first_six: float = DEFAULT_FIRST_SIX
    extra: float = DEFAULT_EXTRA
    revision: int = 0
    schema: str = SCHEMA

    def __post_init__(self):
        if self.schema not in SCHEMAS:
            raise SchemaError('unsupported project schema %r' % (self.schema,),
                              path='project',
                              hint='本版本只读写 %s' % '、'.join(SCHEMAS))
        _text(self.project_id, 'project_id', path='project', allow_empty=False)
        object.__setattr__(self, 'records', tuple(self.records))
        object.__setattr__(self, 'clips', tuple(self.clips))
        for name, value in (('records', self.records), ('clips', self.clips)):
            for item in value:
                expected = Record if name == 'records' else Clip
                if not isinstance(item, expected):
                    raise SchemaError('%s must contain %s objects'
                                      % (name, expected.__name__), path='project')
        if self.schema == SCHEMA_V1:
            for clip in self.clips:
                if clip.is_intro:
                    raise SchemaError(
                        'schema %s cannot carry an intro layer' % SCHEMA_V1,
                        path='clip:%s' % clip.id,
                        hint='把工程升到 %s（Project.with_schema）后再加片头素材' % SCHEMA)
        for name in ('width', 'height', 'sample_rate', 'channels'):
            positive_int(getattr(self, name), name, path='project')
        positive_int(self.fps_num, 'fps numerator', path='project')
        positive_int(self.fps_den, 'fps denominator', path='project')
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise SchemaError('revision must be a non-negative integer', path='project')
        finite_number(self.speed, 'speed', path='project', positive=True)
        for name in ('intro_s', 'gap_s', 'first_six', 'extra'):
            value = finite_number(getattr(self, name), name, path='project')
            if value < 0:
                # The verified engine refuses a negative gap or intro too.
                raise InvalidTimeError('%s must not be negative' % name, path='project')
        # Refuse a frame rate the tick base cannot express, at construction time.
        frame_ticks(self.fps_num, self.fps_den)

    # -- lookups ---------------------------------------------------------
    def clip(self, clip_id):
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def record(self, record_id):
        for record in self.records:
            if record.id == record_id:
                return record
        return None

    def clips_of(self, record_id):
        return tuple(clip for clip in self.clips if clip.record_id == record_id)

    def intro_clip(self):
        """The project's intro layer, or ``None`` when the lesson has none."""
        for clip in self.clips:
            if clip.is_intro:
                return clip
        return None

    # -- derived settings ------------------------------------------------
    @property
    def frame_ticks(self):
        return frame_ticks(self.fps_num, self.fps_den)

    @property
    def frame_seconds(self):
        return frame_seconds(self.fps_num, self.fps_den)

    @property
    def intro_ticks(self):
        """Fallback length of the intro slot, from ``intro_s`` *only*.

        This is what a project without an intro layer reserves in front of the
        lesson body, and it is all the legacy engine ever knew.  When the project
        *has* an intro layer its length comes from that media (see
        :mod:`word_video.domain.compile`), so a template that says 2.0 s and a
        clip that runs 1.867 s can never both be true: the media wins.
        """
        return intro_ticks(self.intro_s, self.fps_num, self.fps_den)

    def with_schema(self, schema, *, revision=None):
        """State the document revision explicitly (never silently on save)."""
        return Project(**self._replaced('schema', schema, revision))

    # -- new revisions ---------------------------------------------------
    def with_clips(self, clips, *, revision=None):
        return Project(**self._replaced('clips', tuple(clips), revision))

    def with_records(self, records, *, revision=None):
        return Project(**self._replaced('records', tuple(records), revision))

    def next_revision(self):
        return Project(**self._replaced('clips', self.clips, self.revision + 1))

    def _replaced(self, name, value, revision):
        values = {field: getattr(self, field) for field in self.to_dict()}
        values[name] = value
        values['revision'] = self.revision if revision is None else revision
        return values

    # -- document IO -----------------------------------------------------
    _FIELDS = ('schema', 'project_id', 'revision', 'title', 'footer', 'width', 'height',
               'fps_num', 'fps_den', 'sample_rate', 'channels', 'speed', 'intro_s',
               'gap_s', 'first_six', 'extra', 'records', 'clips')

    def to_dict(self):
        return {
            'schema': self.schema,
            'project_id': self.project_id,
            'revision': self.revision,
            'title': self.title,
            'footer': self.footer,
            'width': self.width,
            'height': self.height,
            'fps_num': self.fps_num,
            'fps_den': self.fps_den,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'speed': self.speed,
            'intro_s': self.intro_s,
            'gap_s': self.gap_s,
            'first_six': self.first_six,
            'extra': self.extra,
            'records': [record.to_dict() for record in self.records],
            'clips': [clip.to_dict() for clip in self.clips],
        }

    @classmethod
    def from_dict(cls, value):
        _strict(value, 'project', cls._FIELDS, 'project')
        return cls(
            project_id=value['project_id'],
            records=tuple(Record.from_dict(item, path='project') for item in value['records']),
            clips=tuple(Clip.from_dict(item, path='project') for item in value['clips']),
            title=value['title'], footer=value['footer'], width=value['width'],
            height=value['height'], fps_num=value['fps_num'], fps_den=value['fps_den'],
            sample_rate=value['sample_rate'], channels=value['channels'],
            speed=value['speed'], intro_s=value['intro_s'], gap_s=value['gap_s'],
            first_six=value['first_six'], extra=value['extra'],
            revision=value['revision'], schema=value['schema'])


def check_duplicate_ids(records, clips):
    """Refuse duplicate identities before anything is solved."""
    seen = set()
    for record in records:
        if record.id in seen:
            raise DuplicateIdError('duplicate record id %r' % (record.id,),
                                   path='record:%s' % record.id)
        seen.add(record.id)
    seen = set()
    for clip in clips:
        if clip.id in seen:
            raise DuplicateIdError('duplicate clip id %r' % (clip.id,),
                                   path='clip:%s' % clip.id)
        seen.add(clip.id)
    indexes = [(record.index, record.id) for record in records if record.index]
    seen = {}
    for index, record_id in indexes:
        if index in seen:
            raise DuplicateIdError(
                'records %s and %s share index %d' % (seen[index], record_id, index),
                path='record:%s' % record_id)
        seen[index] = record_id
