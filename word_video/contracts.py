"""Shared contract v1. Owned by the integrating agent; no UI dependencies."""
from dataclasses import asdict, dataclass, field
from typing import Any

ROLES = ('female', 'male', 'chinese')
DISPLAY_TRACKS = ('english', 'phonetic', 'meaning')


@dataclass(frozen=True)
class WordEntry:
    index: int
    word: str
    phonetic: str
    meaning: str
    spoken_meaning: str


@dataclass(frozen=True)
class SpeechAsset:
    word_index: int
    role: str
    text: str
    path: str  # Already tempo-adjusted PCM WAV, consumed identically by both outputs.
    duration_s: float  # Original (unscaled) speech duration.
    rendered_duration_s: float  # Actual probed duration of the prepared WAV.
    voice: str = ''


@dataclass
class LessonSpec:
    entries: list[WordEntry]
    title: str = '四级1500高频词'
    footer: str = '不积小流 无以成江海'
    batch_size: int = 50
    speed: float = 1.25
    fps: int = 60
    width: int = 3840
    height: int = 2160
    intro_s: float = 2.0
    first_six: float = 0.4
    extra: float = 0.2
    gap_s: float = 0.1  # Before speed conversion.
    background: str = ''
    intro_audio: str = ''
    # Reference intro clips: {"video": path, "audio": path}.  The audio length
    # decides the intro duration, so every word starts when it ends.
    intro: dict[str, Any] = field(default_factory=dict)
    # 'h264' (default, most compatible) or 'h265' (smaller files).
    video_codec: str = 'h264'
    styles: dict[str, Any] = field(default_factory=dict)
    # Resolved face per style role; a substitute is recorded, never hidden.
    fonts: dict[str, Any] = field(default_factory=dict)


@dataclass
class TimelineManifest:
    schema_version: int
    title: str
    subtitle: str
    footer: str
    first_index: int
    last_index: int
    fps: int
    width: int
    height: int
    speed: float
    intro_frames: int
    total_frames: int
    background: str
    intro_audio: str
    intro_video: str
    video_codec: str
    styles: dict[str, Any]
    words: list[dict[str, Any]]
    # Each word: index, word, phonetic, meaning, spoken_meaning,
    # start_frame, male_frame, chinese_frame, end_frame.
    audio: list[dict[str, Any]]
    # Each audio: word_index, role, text, voice, path, start_frame, duration_frames.
    # duration_frames reserves the whole stage. Both exporters pad trailing silence.

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if value.get('schema_version') != 1:
            raise ValueError('Unsupported timeline schema')
        return cls(**value)
