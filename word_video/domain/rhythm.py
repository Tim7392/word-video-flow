"""Lesson rhythm: the M0-verified timing rules, reused read-only.

``word_video/timing.py`` owns the teaching rhythm (default speed 1.25, English
floor 1.0 s, Chinese floor ``max(0.5, min(n,6)*0.4 + max(n-6,0)*0.2)``, gap 0.1 s)
and M0 recomputed that behaviour independently.  This module *applies* those
rules instead of restating them: the minimum comes from
``timing.chinese_minimum_duration``/``timing._ENGLISH_MINIMUM`` and the frame
decision from the same arithmetic ``timing._stage_frames`` performs, so a new
project reserves exactly the frames the verified chain reserved.

Why the doubling is deliberate: that decision is taken in doubles, and at a
frame boundary its rounding can differ by one frame from exact decimal
arithmetic (measured: 4% of boundary cases).  W01's mandate is to reuse the
verified behaviour rather than to improve it, so the *decision* stays the
engine's while everything the project stores — instants, boundaries, source
grids — is exact: the frames are converted to integer ticks once, at the end.

One deliberate difference is documented in :func:`stage_duration_ticks`: the old
engine also floored a stage at ``ceil(rendered_duration_s * fps)`` because it
received an already tempo-adjusted WAV.  A new project declares the playback
speed on the clip and the mixer applies it exactly once, so there is no second
rendered length to floor against.
"""
import math

from .. import timing
from ..contracts import LessonSpec
from .errors import InvalidTimeError
from .timebase import finite_number, frame_ticks

# The single source of those defaults is the verified contract, not a copy here.
_LESSON_DEFAULTS = LessonSpec(entries=[])

DEFAULT_SPEED = _LESSON_DEFAULTS.speed
DEFAULT_GAP_S = _LESSON_DEFAULTS.gap_s
DEFAULT_FIRST_SIX = _LESSON_DEFAULTS.first_six
DEFAULT_EXTRA = _LESSON_DEFAULTS.extra
DEFAULT_FPS_NUM = _LESSON_DEFAULTS.fps
DEFAULT_WIDTH = _LESSON_DEFAULTS.width
DEFAULT_HEIGHT = _LESSON_DEFAULTS.height
DEFAULT_TITLE = _LESSON_DEFAULTS.title
DEFAULT_FOOTER = _LESSON_DEFAULTS.footer
DEFAULT_INTRO_S = _LESSON_DEFAULTS.intro_s

# timing._ENGLISH_MINIMUM is the only definition of the 1.0 s English floor;
# tests/test_wv_project_golden.py guards this reference against silent drift.
ENGLISH_MINIMUM_S = timing._ENGLISH_MINIMUM


def chinese_minimum_seconds(record, first_six=DEFAULT_FIRST_SIX, extra=DEFAULT_EXTRA):
    """Chinese stage floor for one record, exactly as the verified engine computes it.

    ``record`` supplies ``spoken_meaning`` and ``word``; counting falls back to
    ``max(len(word) // 4, 1)`` when the spoken meaning has no Hanzi, and the
    count is never treated as seconds.  The value is the engine's double, noise
    included, because that is the number the frame decision is taken from.
    """
    return timing.chinese_minimum_duration(record, float(first_six), float(extra))


def engine_frames(seconds, speed, fps_num, fps_den=1):
    """Frames one accelerated duration occupies — the verified engine's decision.

    ``timing._stage_frames`` computes ``ceil(max(minimum, duration) / speed * fps)``
    in doubles; this is that expression, with the frame rate as an exact rational
    so 30000/1001 is representable.
    """
    if fps_den <= 0 or fps_num <= 0:
        raise InvalidTimeError('fps must be a positive rational', path='project')
    frame_ticks(fps_num, fps_den)  # refuse a rate the tick base cannot express
    finite_number(speed, 'speed', path='project', positive=True)
    rate = fps_num / fps_den
    return max(int(math.ceil(float(seconds) / float(speed) * rate)), 1)


def stage_duration_ticks(role, record, source_seconds, *, gap_s, speed,
                         first_six=DEFAULT_FIRST_SIX, extra=DEFAULT_EXTRA,
                         fps_num=None, fps_den=1):
    """Frames reserved by one teaching stage, in ticks.

    Mirrors ``timing.build_timeline`` + ``timing._stage_frames``::

        minimum      = max(role floor, source_seconds + gap)
        stage        = max(minimum, source_seconds)
        frames       = ceil(stage / speed * fps)

    English stages floor at 1.0 s, the Chinese stage at the verified
    ``chinese_minimum_duration`` rule; ``max`` is what lets a real recording win
    over the floor.  No frame is shorter than one frame.
    """
    if role in ('female', 'male'):
        minimum = ENGLISH_MINIMUM_S
    elif role == 'chinese':
        minimum = chinese_minimum_seconds(record, first_six, extra)
    else:
        raise InvalidTimeError('role %r has no teaching rhythm' % (role,), path='clip')
    if fps_num is None:
        raise InvalidTimeError('a frame rate is needed for a teaching stage',
                               path='clip')
    finite_number(gap_s, 'gap_s', path='project')
    stage = max(float(minimum), float(source_seconds) + float(gap_s))
    frames = engine_frames(stage, speed, fps_num, fps_den)
    return frames * frame_ticks(fps_num, fps_den)


def intro_ticks(intro_s, fps_num, fps_den=1):
    """Frames reserved in front of the lesson body (unaccelerated, like the engine)."""
    if intro_s < 0:
        raise InvalidTimeError('intro_s must not be negative', path='project')
    return engine_frames(intro_s, 1.0, fps_num, fps_den) * frame_ticks(fps_num, fps_den)


__all__ = ['DEFAULT_EXTRA', 'DEFAULT_FIRST_SIX', 'DEFAULT_FOOTER', 'DEFAULT_FPS_NUM',
           'DEFAULT_GAP_S', 'DEFAULT_HEIGHT', 'DEFAULT_INTRO_S', 'DEFAULT_SPEED',
           'DEFAULT_TITLE', 'DEFAULT_WIDTH', 'ENGLISH_MINIMUM_S',
           'chinese_minimum_seconds', 'engine_frames', 'intro_ticks',
           'stage_duration_ticks']
