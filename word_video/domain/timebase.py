"""Integer tick time base (720000 tick/s) with exact rational conversions.

One time core for the editor (top-level architecture §6.1): every project instant
is an integer tick, 720000 of them per second.  A 60 fps frame is 12000 ticks and
a 48 kHz sample is 15 ticks, so the common grids are exact.  Media that does not
land on the tick grid — 44.1 kHz audio, 30000/1001 video — keeps its own rational
time base in :class:`~word_video.domain.model.MediaSlice` and is converted once,
half up, at the boundary.  A boundary is never produced by adding up already
rounded segment durations.
"""
from dataclasses import dataclass
from fractions import Fraction
import math

from .errors import InvalidTimeError, UnsupportedRateError

TICKS_PER_SECOND = 720000


def rational(value):
    """Exact ``Fraction`` for a project number.

    Floats go through their decimal text, so ``Fraction(str(1.25))`` is ``5/4``
    and ``Fraction(str(3.275))`` is ``131/40``: a value typed into a request or
    stored in ``project.json`` keeps the value the user wrote instead of the
    nearest binary double.  That is what makes the golden fixtures reproducible.
    """
    if isinstance(value, bool) or value is None:
        raise TypeError('rational() needs a number, got %r' % (value,))
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('rational() needs a finite number, got %r' % (value,))
        return Fraction(str(value))
    raise TypeError('rational() needs a number, got %r' % (value,))


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def finite_number(value, name, *, path='', positive=False, allow_zero=True):
    """Validate a JSON number: finite, and positive when required."""
    if not is_number(value) or not math.isfinite(float(value)):
        raise InvalidTimeError('%s must be a finite number' % name, path=path)
    if positive and float(value) <= 0:
        raise InvalidTimeError('%s must be positive' % name, path=path)
    if not allow_zero and float(value) == 0:
        raise InvalidTimeError('%s must not be zero' % name, path=path)
    return float(value)


def positive_int(value, name, *, path='', allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTimeError('%s must be an integer' % name, path=path)
    if value < 0 or (value == 0 and not allow_zero):
        raise InvalidTimeError(
            '%s must be %s' % (name, 'non-negative' if allow_zero else 'positive'),
            path=path)
    return value


def half_up(value):
    """Half-up rounding of an exact rational to an integer.

    ``Fraction(800, 49)`` -> 16, ``Fraction(2400, 49)`` -> 49, ``Fraction(1, 2)``
    -> 1.  Only non-negative project instants and durations are rounded here.
    """
    value = Fraction(value)
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def rate_pair(fps_num, fps_den=1):
    """Validate a rational frame rate and return ``(num, den)``."""
    if isinstance(fps_num, bool) or not isinstance(fps_num, int):
        # A float rate would hide a rounding decision; callers pass 30000/1001.
        raise InvalidTimeError('fps numerator must be an integer', path='project')
    if isinstance(fps_den, bool) or not isinstance(fps_den, int) or fps_den <= 0 or fps_num <= 0:
        raise InvalidTimeError('fps must be a positive rational', path='project')
    return fps_num, fps_den


def frame_ticks(fps_num, fps_den=1):
    """Ticks per video frame; refuse a rate the tick base cannot express exactly."""
    num, den = rate_pair(fps_num, fps_den)
    ticks = Fraction(TICKS_PER_SECOND * den, num)
    if ticks.denominator != 1:
        raise UnsupportedRateError(
            'frame rate %d/%d is not representable on the %d tick/s grid'
            % (num, den, TICKS_PER_SECOND),
            path='project', hint='使用整数帧率或 30000/1001、24000/1001 等可精确表示的帧率')
    return int(ticks)


def frame_seconds(fps_num, fps_den=1):
    num, den = rate_pair(fps_num, fps_den)
    return Fraction(den, num)


def ticks_to_milliseconds(ticks):
    """Absolute endpoint -> SRT milliseconds, half up, converted once (§6.1)."""
    return (int(ticks) * 1000 + TICKS_PER_SECOND // 2) // TICKS_PER_SECOND


def ticks_to_microseconds(ticks):
    """Absolute endpoint -> draft microseconds, half up, converted once (§6.1)."""
    return (int(ticks) * 1000000 + TICKS_PER_SECOND // 2) // TICKS_PER_SECOND


@dataclass(frozen=True)
class TimeExpr:
    """A project instant: an absolute tick, or a declared link to another clip.

    Absolute form: ``ticks`` on the 720000 tick/s grid; every clip range is
    left-closed ``[start, end)``.  Linked form: ``ref`` names a clip whose
    ``edge`` (``start`` or ``end``) this instant follows, plus a signed
    ``offset_ticks``.  Links are resolved by :mod:`word_video.domain.compile`;
    a cycle or a missing target is a hard error, never a silent fallback.
    """

    ticks: int | None = None
    ref: str = ''
    edge: str = ''
    offset_ticks: int = 0

    def __post_init__(self):
        if self.ticks is not None:
            if isinstance(self.ticks, bool) or not isinstance(self.ticks, int):
                raise InvalidTimeError('time must be an integer tick count', path='time')
            if self.ticks < 0:
                raise InvalidTimeError('time must not be negative', path='time',
                                       hint='工程时间从 0 开始')
            if self.ref or self.edge:
                raise InvalidTimeError('an absolute time cannot also follow a clip',
                                       path='time')
            if self.offset_ticks:
                raise InvalidTimeError('an absolute time cannot carry an offset',
                                       path='time')
            return
        if not isinstance(self.ref, str) or not self.ref:
            raise InvalidTimeError('time needs an absolute tick or a clip reference',
                                   path='time')
        if self.edge not in ('start', 'end'):
            raise InvalidTimeError("time edge must be 'start' or 'end'", path='time')
        if isinstance(self.offset_ticks, bool) or not isinstance(self.offset_ticks, int):
            raise InvalidTimeError('time offset must be an integer', path='time')

    # -- construction ----------------------------------------------------
    @classmethod
    def at(cls, ticks):
        return cls(ticks=int(ticks))

    @classmethod
    def seconds(cls, value):
        """An instant given in seconds (exact decimal), rounded half up once."""
        return cls(ticks=half_up(rational(value) * TICKS_PER_SECOND))

    @classmethod
    def frames(cls, frames, fps_num, fps_den=1):
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise InvalidTimeError('frame count must be an integer', path='time')
        return cls(ticks=frames * frame_ticks(fps_num, fps_den))

    @classmethod
    def from_units(cls, count, unit_num, unit_den):
        """An instant on a media source grid, e.g. sample 4410 of a 44.1 kHz file."""
        return cls(ticks=half_up(Fraction(int(count) * int(unit_num) * TICKS_PER_SECOND,
                                          int(unit_den))))

    @classmethod
    def after(cls, clip_id, offset_ticks=0):
        return cls(ref=clip_id, edge='end', offset_ticks=offset_ticks)

    @classmethod
    def at_start_of(cls, clip_id, offset_ticks=0):
        return cls(ref=clip_id, edge='start', offset_ticks=offset_ticks)

    # -- queries ---------------------------------------------------------
    @property
    def is_absolute(self):
        return self.ticks is not None

    @property
    def is_linked(self):
        return self.ticks is None

    def dependencies(self):
        return () if self.is_absolute else (self.ref,)

    def shift(self, delta_ticks):
        """Move this instant; a linked instant keeps its link and moves its offset."""
        if isinstance(delta_ticks, bool) or not isinstance(delta_ticks, int):
            raise InvalidTimeError('delta must be an integer tick count', path='time')
        if self.is_absolute:
            moved = self.ticks + delta_ticks
            if moved < 0:
                raise InvalidTimeError('time must not be negative', path='time',
                                       hint='不能移到工程 0 之前')
            return TimeExpr(ticks=moved)
        return TimeExpr(ref=self.ref, edge=self.edge,
                        offset_ticks=self.offset_ticks + delta_ticks)

    def to_dict(self):
        if self.is_absolute:
            return {'ticks': self.ticks}
        return {'ref': self.ref, 'edge': self.edge, 'offset_ticks': self.offset_ticks}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise InvalidTimeError('time must be an object', path='time')
        allowed = {'ticks', 'ref', 'edge', 'offset_ticks'}
        unknown = set(value) - allowed
        if unknown:
            raise InvalidTimeError('unknown time field(s): %s' % ', '.join(sorted(unknown)),
                                   path='time')
        if 'ticks' in value:
            if set(value) != {'ticks'}:
                raise InvalidTimeError('an absolute time cannot mix with a link',
                                       path='time')
            return cls.at(value['ticks'])
        missing = {'ref', 'edge'} - set(value)
        if missing:
            raise InvalidTimeError('linked time needs %s' % ', '.join(sorted(missing)),
                                   path='time')
        return cls(ref=value['ref'], edge=value['edge'],
                   offset_ticks=value.get('offset_ticks', 0))
