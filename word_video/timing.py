"""Deterministic lesson splitting and timeline construction (contract v1).

Timing rules (word_video_work/DS-01.md):
- Each stage (female, male, chinese) lasts ``max(minimum_duration,
  raw_speech_duration + gap_s)`` seconds, divided by ``speed`` exactly once.
- Stage frames are the integer ceil of that value, raised when needed to
  ``ceil(prepared rendered_duration_s * fps)`` (the WAV is already tempo
  adjusted, so it is never divided by speed again).
- Stages never overlap; each next word starts exactly where the previous
  word ended.  ``intro_frames = ceil(intro_s * fps)``, unaccelerated.
"""
import math
from pathlib import Path

from .contracts import ROLES, LessonSpec, TimelineManifest
from .media import duration

SCHEMA_VERSION = 1
_HANZI_START = '\u4e00'
_HANZI_END = '\u9fff'
_CHINESE_MINIMUM = 0.5
_ENGLISH_MINIMUM = 1.0


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value, name, *, positive=False):
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ValueError('%s must be a finite number' % name)
    if positive and float(value) <= 0:
        raise ValueError('%s must be positive' % name)
    return float(value)


def _positive_int(value, name):
    if not _is_int(value) or value <= 0:
        raise ValueError('%s must be a positive integer' % name)
    return value


def _validate_settings(lesson):
    _positive_int(lesson.batch_size, 'batch_size')
    _positive_int(lesson.fps, 'fps')
    _positive_int(lesson.width, 'width')
    _positive_int(lesson.height, 'height')
    _finite(lesson.speed, 'speed', positive=True)
    _finite(lesson.intro_s, 'intro_s')
    _finite(lesson.first_six, 'first_six')
    _finite(lesson.extra, 'extra')
    _finite(lesson.gap_s, 'gap_s')
    if lesson.intro_s < 0:
        raise ValueError('intro_s must not be negative')
    if lesson.gap_s < 0:
        raise ValueError('gap_s must not be negative')
    if lesson.first_six < 0:
        raise ValueError('first_six must not be negative')
    if lesson.extra < 0:
        raise ValueError('extra must not be negative')


def _validate_entries(entries):
    if not entries:
        raise ValueError('lesson must contain at least one word')
    indexes = []
    for entry in entries:
        if not _is_int(entry.index) or entry.index <= 0:
            raise ValueError('word index must be a positive integer')
        if not isinstance(entry.word, str) or not entry.word:
            raise ValueError('word must be a non-empty string')
        for name in ('phonetic', 'meaning', 'spoken_meaning'):
            if not isinstance(getattr(entry, name), str):
                raise ValueError('word %s must be a string' % name)
        indexes.append(entry.index)
    if len(set(indexes)) != len(indexes):
        raise ValueError('word indexes must be unique')
    if indexes != sorted(indexes):
        raise ValueError('word indexes must be ordered')


def split_lesson(lesson):
    """Split a lesson into batches, preserving the original word indexes."""
    _positive_int(lesson.batch_size, 'batch_size')
    _validate_settings(lesson)
    _validate_entries(lesson.entries)
    batches = []
    size = lesson.batch_size
    for start in range(0, len(lesson.entries), size):
        chunk = list(lesson.entries[start:start + size])
        batches.append(LessonSpec(
            entries=chunk,
            title=lesson.title,
            footer=lesson.footer,
            batch_size=lesson.batch_size,
            speed=lesson.speed,
            fps=lesson.fps,
            width=lesson.width,
            height=lesson.height,
            intro_s=lesson.intro_s,
            first_six=lesson.first_six,
            extra=lesson.extra,
            gap_s=lesson.gap_s,
            background=lesson.background,
            intro_audio=lesson.intro_audio,
            intro=dict(lesson.intro),
            video_codec=lesson.video_codec,
            styles=dict(lesson.styles),
            fonts=dict(lesson.fonts),
        ))
    return batches


def chinese_minimum_duration(entry, first_six, extra):
    """Clean-Chinese minimum, matching subtitle_factory_core._gen.

    ``count`` is a character count: the number of Hanzi in the spoken meaning,
    or the English fallback ``max(len(word) // 4, 1)`` when there is none.  The
    count then feeds the first-6/remainder formula, and the result is floored
    at 0.5 seconds.  The fallback count is never treated as seconds.
    """
    count = sum(
        1 for char in entry.spoken_meaning if _HANZI_START <= char <= _HANZI_END
    )
    if count == 0:
        count = max(len(entry.word) // 4, 1)
    head = min(count, 6)
    tail = count - head
    return max(_CHINESE_MINIMUM, head * first_six + tail * extra)


def _stage_frames(minimum, asset, speed, fps):
    stage_seconds = max(minimum, asset.duration_s + 0.0)
    accelerated = stage_seconds / speed
    frames = math.ceil(accelerated * fps)
    rendered = math.ceil(asset.rendered_duration_s * fps)
    return max(frames, rendered, 1)


def _validate_assets(assets, entries, batch_size):
    if len(entries) > batch_size:
        raise ValueError('timeline batch exceeds batch_size')
    by_index = {entry.index: entry for entry in entries}
    seen = set()
    resolved = {}
    for asset in assets:
        if not _is_int(asset.word_index) or asset.word_index <= 0:
            raise ValueError('asset word_index must be a positive integer')
        if asset.word_index not in by_index:
            raise ValueError('unexpected asset word_index %r' % (asset.word_index,))
        if asset.role not in ROLES:
            raise ValueError('invalid asset role %r' % (asset.role,))
        key = (asset.word_index, asset.role)
        if key in seen:
            raise ValueError('duplicate asset for %r' % (key,))
        seen.add(key)
        _finite(asset.duration_s, 'duration_s')
        _finite(asset.rendered_duration_s, 'rendered_duration_s')
        if asset.duration_s < 0 or asset.rendered_duration_s < 0:
            raise ValueError('asset durations must not be negative')
        if not isinstance(asset.path, str) or not asset.path:
            raise ValueError('asset path must be a non-empty string')
        if not isinstance(asset.text, str):
            raise ValueError('asset text must be a string')
        entry = by_index[asset.word_index]
        expected = entry.word if asset.role in ('female', 'male') else entry.spoken_meaning
        if asset.text != expected:
            raise ValueError(
                'asset text does not match spoken text for %r' % (key,)
            )
        resolved[key] = asset
    for index in by_index:
        for role in ROLES:
            if (index, role) not in resolved:
                raise ValueError('missing %s asset for word %r' % (role, index))
    return resolved


def intro_has_alpha(path):
    """True when the intro clip's own alpha channel should composite it.

    A transparent export (``bgra``/``rgba``, e.g. QuickTime animation) is drawn
    with a plain alpha overlay.  An opaque black-backed export has no alpha, so
    the renderer must use a lighten blend instead — the two need different
    treatment and the file itself decides which.
    """
    from .media import probe
    for stream in probe(path).get('streams', []):
        if stream.get('codec_type') == 'video':
            return 'a' in str(stream.get('pix_fmt') or '').lower()
    return False


def _video_stream_seconds(path):
    """Duration of the *video stream*, which may be shorter than the container.

    A clip whose audio outruns its picture still reports the longer container
    length, so reading the container alone would miss a truncated countdown.
    """
    from .media import probe
    data = probe(path)
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            for key in ('duration', 'tags', 'nb_frames'):
                if key == 'duration' and stream.get('duration'):
                    return float(stream['duration'])
            if stream.get('nb_frames'):
                rate = stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1'
                number, _, denominator = str(rate).partition('/')
                if float(denominator or 1) and float(number):
                    return float(stream['nb_frames']) / (float(number) / float(denominator))
    return duration(path)


def intro_frames_for(lesson):
    """Frames occupied by the reference intro clip, from the clip itself.

    The intro's *audio* length decides the duration, so the first word starts
    exactly when the countdown sound effect ends.  The clip's picture must cover
    that length (it may run longer and simply end on black).  An unusable clip
    fails the job rather than silently dropping the intro.
    """
    clip = dict(lesson.intro or {})
    if not clip:
        return 0, ''
    video = clip.get('video')
    if not video:
        raise ValueError('intro video path is required')
    Path(video).resolve(strict=True)
    audio = clip.get('audio')
    if audio:
        Path(audio).resolve(strict=True)
        seconds = duration(audio)
    else:
        seconds = duration(video)
    frames = math.ceil(seconds * lesson.fps)
    if frames <= 0:
        raise ValueError('intro audio has no usable duration')
    picture = _video_stream_seconds(video)
    if picture + 0.05 < seconds:
        raise ValueError('intro video (%.3fs) is shorter than its audio (%.3fs)'
                         % (picture, seconds))
    return frames, str(video)


def build_timeline(lesson, speech_assets):
    """Build one batch timeline manifest from validated speech assets."""
    _validate_settings(lesson)
    _validate_entries(lesson.entries)
    if len(lesson.entries) > lesson.batch_size:
        raise ValueError('timeline batch exceeds batch_size')
    resolved = _validate_assets(list(speech_assets), lesson.entries, lesson.batch_size)

    fps = lesson.fps
    speed = lesson.speed
    clip_frames, clip_video = intro_frames_for(lesson)
    if clip_frames:
        # A supplied clip replaces the synthetic seconds, so the two can never
        # disagree about where the lesson body begins.
        intro_frames = clip_frames
    else:
        intro_frames = math.ceil(lesson.intro_s * fps)
    cursor = intro_frames
    words = []
    audio = []
    for entry in lesson.entries:
        start = cursor
        frames = {}
        for role in ROLES:
            asset = resolved[(entry.index, role)]
            if role == 'female':
                minimum = _ENGLISH_MINIMUM
            elif role == 'male':
                minimum = _ENGLISH_MINIMUM
            else:
                minimum = chinese_minimum_duration(
                    entry, lesson.first_six, lesson.extra
                )
            minimum = max(minimum, asset.duration_s + lesson.gap_s)
            frames[role] = _stage_frames(minimum, asset, speed, fps)

        male_frame = start + frames['female']
        chinese_frame = male_frame + frames['male']
        end_frame = chinese_frame + frames['chinese']

        words.append({
            'index': entry.index,
            'word': entry.word,
            'phonetic': entry.phonetic,
            'meaning': entry.meaning,
            'spoken_meaning': entry.spoken_meaning,
            'start_frame': start,
            'male_frame': male_frame,
            'chinese_frame': chinese_frame,
            'end_frame': end_frame,
        })
        starts = {
            'female': start,
            'male': male_frame,
            'chinese': chinese_frame,
        }
        for role in ROLES:
            asset = resolved[(entry.index, role)]
            audio.append({
                'word_index': entry.index,
                'role': role,
                'text': asset.text,
                'voice': asset.voice,
                'path': asset.path,
                'start_frame': starts[role],
                'duration_frames': frames[role],
            })
        cursor = end_frame

    first_index = lesson.entries[0].index
    last_index = lesson.entries[-1].index
    return TimelineManifest(
        schema_version=SCHEMA_VERSION,
        title=lesson.title,
        subtitle='速通（%d–%d）' % (first_index, last_index),
        footer=lesson.footer,
        first_index=first_index,
        last_index=last_index,
        fps=fps,
        width=lesson.width,
        height=lesson.height,
        speed=speed,
        intro_frames=intro_frames,
        total_frames=cursor,
        background=lesson.background,
        intro_audio=lesson.intro_audio,
        intro_video=clip_video,
        video_codec=lesson.video_codec,
        styles=dict(lesson.styles),
        words=words,
        audio=audio,
    )
