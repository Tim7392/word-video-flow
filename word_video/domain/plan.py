"""Derived, read-only plans: ``RenderPlan`` and ``CuePlan``.

A plan is solved time, not editable time.  Both objects are frozen and hold only
immutable values, so a consumer cannot write a plan back into the project and a
project edit cannot mutate a plan that a job already froze.  Plans carry tick
boundaries; milliseconds (SRT) and microseconds (draft) are produced once, at the
adapter boundary, through :func:`word_video.domain.timebase.ticks_to_milliseconds`
and :func:`~word_video.domain.timebase.ticks_to_microseconds`.
"""
from dataclasses import dataclass
import hashlib
import json

from .model import MediaSlice

#: The five teaching subtitle tracks, in file order.
CUE_TRACKS = ('01', '02', '03', '04', '05')
#: Semantic source of each track's text (see docs: SRT projection rule).
CUE_ROLES = ('english', 'english', 'phonetic', 'meaning', 'spoken_meaning')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


@dataclass(frozen=True)
class PlanSound:
    """The one file that sounds with a layer, and the verdict behind that choice.

    ``source`` is echoed verbatim from the single resolver
    (``word_video.media.resolve_intro_audio``); the planner never decides which
    file wins, it records what the resolver answered.  ``asset_id`` is empty when
    nothing sounds, and ``fallback_asset`` is the standalone audio the project
    declared for this layer (empty when it declared none).
    """

    asset_id: str = ''
    fallback_asset: str = ''
    source: str = ''

    @property
    def silent(self):
        return not self.asset_id

    def to_dict(self):
        return {'asset_id': self.asset_id, 'fallback_asset': self.fallback_asset,
                'source': self.source}


@dataclass(frozen=True)
class PlanItem:
    """One solved clip: absolute range plus everything a consumer needs."""

    clip_id: str
    role: str
    record_id: str
    start_ticks: int
    end_ticks: int
    text: str = ''
    source: MediaSlice | None = None
    sound: PlanSound | None = None

    @property
    def duration_ticks(self):
        return self.end_ticks - self.start_ticks

    @property
    def is_speech(self):
        return self.source is not None and self.role in ('female', 'male', 'chinese')

    @property
    def is_intro(self):
        return self.role == 'intro'

    def to_dict(self):
        return {'clip_id': self.clip_id, 'role': self.role, 'record_id': self.record_id,
                'start_ticks': self.start_ticks, 'end_ticks': self.end_ticks,
                'text': self.text,
                'source': self.source.to_dict() if self.source else None,
                'sound': self.sound.to_dict() if self.sound else None}


@dataclass(frozen=True)
class Conflict:
    """A problem the plan can still express, reported instead of dropped."""

    code: str
    message: str
    path: str = ''
    other_path: str = ''
    hint: str = ''

    def to_dict(self):
        return {'code': self.code, 'message': self.message, 'object_path': self.path,
                'other_path': self.other_path, 'hint': self.hint}


@dataclass(frozen=True)
class RenderPlan:
    """Everything the picture, the mix and the preview need, already solved."""

    project_id: str
    project_revision: int
    fps_num: int
    fps_den: int
    width: int
    height: int
    sample_rate: int
    channels: int
    total_ticks: int
    video: tuple = ()
    audio: tuple = ()
    conflicts: tuple = ()
    #: The project's per-role style overrides, carried into the plan so a renderer
    #: and an on-screen canvas read *one* style source instead of each merging the
    #: defaults again (fields not named here still fall back to the defaults).
    styles: tuple = ()
    schema: str = 'wv-render@1'

    def __post_init__(self):
        object.__setattr__(self, 'video', tuple(self.video))
        object.__setattr__(self, 'audio', tuple(self.audio))
        object.__setattr__(self, 'conflicts', tuple(self.conflicts))
        object.__setattr__(self, 'styles', tuple(self.styles))

    def style_table(self):
        """``{role: {field: value}}`` of the overrides this plan was solved with."""
        return {override.role: override.values for override in self.styles}

    def item(self, clip_id):
        for item in self.video + self.audio:
            if item.clip_id == clip_id:
                return item
        return None

    @property
    def intro_item(self):
        """The solved intro stage, or ``None`` when the lesson has no intro layer.

        Everything a consumer needs is on it: the video asset in ``source``, the
        stage length in ``start_ticks``/``end_ticks``, and which file sounds plus
        the resolver's verdict in ``sound``.
        """
        for item in self.video:
            if item.is_intro:
                return item
        return None

    def identity(self):
        """Stable content identity for job freezing (never a cache key per frame)."""
        return hashlib.sha256(_canonical(self.to_dict())).hexdigest()

    def to_dict(self):
        return {'schema': self.schema, 'project_id': self.project_id,
                'project_revision': self.project_revision, 'fps_num': self.fps_num,
                'fps_den': self.fps_den, 'width': self.width, 'height': self.height,
                'sample_rate': self.sample_rate, 'channels': self.channels,
                'total_ticks': self.total_ticks,
                'video': [item.to_dict() for item in self.video],
                'audio': [item.to_dict() for item in self.audio],
                'conflicts': [conflict.to_dict() for conflict in self.conflicts],
                'styles': [override.to_dict() for override in self.styles]}


@dataclass(frozen=True)
class Cue:
    """One subtitle cue: absolute ticks plus the text of its semantic role."""

    track: str
    index: int
    record_id: str
    role: str
    start_ticks: int
    end_ticks: int
    text: str

    def to_dict(self):
        return {'track': self.track, 'index': self.index, 'record_id': self.record_id,
                'role': self.role, 'start_ticks': self.start_ticks,
                'end_ticks': self.end_ticks, 'text': self.text}


@dataclass(frozen=True)
class CuePlan:
    """The five-track teaching projection, only produced when it is unambiguous."""

    project_id: str
    project_revision: int
    fps_num: int
    fps_den: int
    total_ticks: int
    cues: tuple = ()
    schema: str = 'wv-cues@1'

    def __post_init__(self):
        object.__setattr__(self, 'cues', tuple(self.cues))

    def by_track(self):
        """``{track: (Cue, ...)}`` with one entry per track, in file order."""
        grouped = {track: [] for track in CUE_TRACKS}
        for cue in self.cues:
            grouped[cue.track].append(cue)
        return {track: tuple(items) for track, items in grouped.items()}

    def identity(self):
        return hashlib.sha256(_canonical(self.to_dict())).hexdigest()

    def to_dict(self):
        return {'schema': self.schema, 'project_id': self.project_id,
                'project_revision': self.project_revision, 'fps_num': self.fps_num,
                'fps_den': self.fps_den, 'total_ticks': self.total_ticks,
                'cues': [cue.to_dict() for cue in self.cues]}
