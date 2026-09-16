"""Render only the PCM window the device is about to need, from the plan.

Why this is not ``media.audio.mix_clips``
-----------------------------------------
``mix_clips`` is the *delivery* mixer: it allocates the whole lesson, sums every
clip in one pass and writes a WAV.  That is the right shape for an export, and
the wrong shape for an editor, for two reasons that both show up as "the member
is waiting":

* it costs the entire lesson before the first sound, so a preview built on it
  would make every edit wait for a full remix - exactly the "不等整段重编码"
  requirement, one layer down;
* its accumulator is a per-sample Python loop over the whole timeline, so its
  cost grows with lesson length even when the user is looking at one word.

So this module renders ``[start, start+frames)`` and nothing else.  What it does
**not** do is invent a second mixing *policy*: the contract is imported from
:mod:`word_video.media.audio` (mono 48 kHz signed-16 prepared speech, the same
linear gain range, the same saturating addition, the same "short clip is
followed by silence"), and the overlap arithmetic is the windowed form of the
same sum.  A future change to the delivered mix that is not reflected here is a
bug in this file, not a second opinion.

Cost is bounded by the window, not the lesson
---------------------------------------------
The common case is one clip in the window, which is a slice copy.  The
saturating add only runs where clips genuinely overlap - the deliberate
intro-bed/effect case - and it too only ever touches the current window.

A clip longer than its window is *truncated for the preview and counted*, not
refused: an editor has to be able to preview a project that the delivery rules
would reject (``compile`` already reports it as ``SOURCE_TRUNCATED``), and the
count is what lets the UI say so instead of quietly cutting speech.
"""
from array import array
import os
import sys
import threading
import wave
from dataclasses import dataclass

from word_video.domain.timebase import TICKS_PER_SECOND
from word_video.media.audio import CLIP_GAIN_MAX, SOURCE_RATE, audio_format

from .clock import frames_for_ticks

__all__ = ['AudioSpan', 'WindowMixer', 'db_to_gain', 'spans_from_plan']

#: Open readers kept at once.  A 50-word lesson has ~150 speech clips; holding
#: one handle each would show up in the very handle counts the resource
#: acceptance measures, and reopening the file for every 20 ms window would be
#: a syscall storm.  A handful of most-recently-used readers is both bounded and
#: fast, because a window only ever touches the clips that sound in it.
READER_CACHE = 8


def db_to_gain(db):
    """``gain_db`` -> linear factor, clamped to the range the mixer accepts."""
    gain = 10.0 ** (float(db) / 20.0)
    return max(0.0, min(CLIP_GAIN_MAX, gain))


@dataclass(frozen=True)
class AudioSpan:
    """One plan audio clip placed on the preview timeline, in target frames.

    ``first_frame``/``last_frame`` are half-open on the *timeline*.
    ``file_offset_frame`` is where in the resolved file the window starts; it is
    zero for every clip the template expands, and non-zero only when a project
    has been trimmed.  ``file_frames`` is the resolved file's own length, so the
    mixer never has to open a file to find out that a window lies past its end.
    """

    path: str
    first_frame: int
    last_frame: int
    file_offset_frame: int = 0
    file_frames: int = 0
    gain: float = 1.0
    label: str = ''

    @property
    def frames(self):
        return self.last_frame - self.first_frame


def _source_offset_frames(source, rate):
    """Where in the resolved file a trimmed clip's window starts.

    The project declares the window on the *source's own grid*; converting
    through ticks keeps a 44.1 kHz source exact.  The prepared file that W02's
    cache resolves is already at ``MediaSlice.speed``, so no speed is applied
    here - re-applying it would be the double-speed defect the media rules
    forbid.
    """
    if not source.source_start:
        return 0
    ticks = source.source_start * source.unit_num * TICKS_PER_SECOND // source.unit_den
    return frames_for_ticks(ticks, rate)


def spans_from_plan(plan, assets, rate=None):
    """Lay every audio item of ``plan`` on the preview timeline.

    Only ``plan.audio`` is read.  The plan is the single derived truth of what
    sounds when; a preview that also scanned clips for itself would be a second
    opinion about the timeline, and the two would disagree the first time a clip
    moved.
    """
    rate = int(rate or plan.sample_rate)
    spans = []
    for item in plan.audio:
        source = item.source
        if source is None:
            continue
        try:
            path = assets[source.asset_id]
        except KeyError:
            raise KeyError('no media resolved for asset %r (clip %s)'
                           % (source.asset_id, item.clip_id)) from None
        first = frames_for_ticks(item.start_ticks, rate)
        last = frames_for_ticks(item.end_ticks, rate)
        if last <= first:
            continue
        spans.append(AudioSpan(
            path=str(path), first_frame=first, last_frame=last,
            file_offset_frame=_source_offset_frames(source, rate),
            file_frames=0, gain=db_to_gain(source.gain_db), label=item.role))
    spans.sort(key=lambda span: (span.first_frame, span.last_frame, span.label))
    return tuple(spans)


class WindowMixer:
    """Reads the clips that sound in a window and sums them into one buffer."""

    def __init__(self, spans, rate=SOURCE_RATE, reader_cache=READER_CACHE):
        self.rate = int(rate)
        if self.rate != SOURCE_RATE:
            # The prepared chain is 48 kHz mono by contract; resampling belongs
            # to the media layer, not to the preview.
            raise ValueError('preview mixing is %d Hz only, asked for %d'
                             % (SOURCE_RATE, self.rate))
        self.spans = tuple(spans)
        self.reader_cache = int(reader_cache)
        self._readers = {}
        self._order = []
        self._lock = threading.Lock()
        self.truncated_frames = 0
        self.rendered_windows = 0

    # -- readers ---------------------------------------------------------
    def _reader(self, path):
        reader = self._readers.get(path)
        if reader is not None:
            return reader
        channels, width, rate, frames = audio_format(path)
        if (channels, width, rate) != (1, 2, self.rate):
            raise ValueError('prepared speech must be mono %dHz signed16 PCM: %s'
                             % (self.rate, path))
        handle = wave.open(str(path), 'rb')
        self._readers[path] = (handle, frames)
        self._order.append(path)
        while len(self._order) > self.reader_cache:
            stale = self._order.pop(0)
            handle, _ = self._readers.pop(stale)
            handle.close()
        return self._readers[path]

    def close(self):
        """Close every cached reader; the object is reusable afterwards."""
        with self._lock:
            for handle, _ in self._readers.values():
                handle.close()
            self._readers.clear()
            self._order.clear()

    def open_readers(self):
        with self._lock:
            return len(self._readers)

    # -- rendering -------------------------------------------------------
    def render_window(self, start_frame, frames):
        """Mono s16 bytes of exactly ``frames`` frames starting at ``start_frame``.

        A window no clip covers is silence, which is what the gap between two
        teaching stages must sound like.
        """
        if frames <= 0:
            raise ValueError('frames must be positive')
        if start_frame < 0:
            raise ValueError('start_frame must not be negative')
        end_frame = start_frame + frames
        acc = None
        overlaps = 0
        for span in self.spans:
            if span.last_frame <= start_frame or span.first_frame >= end_frame:
                continue
            block = self._read_span(span, start_frame, frames)
            if block is None:
                continue
            if acc is None:
                acc = block
            else:
                overlaps += 1
                acc = _saturating_add(acc, block)
        self.rendered_windows += 1
        if acc is None:
            return bytes(2 * frames)
        return acc

    def _read_span(self, span, start_frame, frames):
        """The span's contribution to the window, padded with silence.

        The span covers timeline ``[first, last)`` and reads the resolved file
        from ``file_offset_frame``.  Whatever the window asks for beyond what the
        file still holds is silence, and is counted in ``truncated_frames``: the
        plan reports the same condition as ``SOURCE_TRUNCATED``, and the preview
        must not be the place where it becomes invisible.
        """
        take_from = max(start_frame, span.first_frame)
        take_to = min(start_frame + frames, span.last_frame)
        if take_to <= take_from:
            return None
        with self._lock:
            handle, file_frames = self._reader(span.path)
            offset_in_file = span.file_offset_frame + (take_from - span.first_frame)
            want = take_to - take_from
            available = max(0, file_frames - offset_in_file)
            if available < want:
                self.truncated_frames += want - available
                want = available
            if want <= 0:
                return None
            handle.setpos(offset_in_file)
            raw = handle.readframes(want)
            if len(raw) < 2 * want:
                # A short read is a truncated file, not a rounding detail.
                self.truncated_frames += want - len(raw) // 2
                want = len(raw) // 2
        if span.gain != 1.0:
            raw = _scaled(raw, span.gain)
        head = take_from - start_frame
        tail = frames - head - want
        if head == 0 and tail == 0:
            return raw
        return bytes(2 * head) + raw + bytes(2 * max(0, tail))


def _scaled(block, gain):
    """Multiply one block by ``gain`` with clipping (same rule as the mix)."""
    samples = array('h')
    samples.frombytes(block)
    if sys.byteorder == 'big':
        samples.byteswap()
    scaled = array('h', [min(32767, max(-32768, int(value * gain)))
                         for value in samples])
    if sys.byteorder == 'big':
        scaled.byteswap()
    return scaled.tobytes()


def _saturating_add(left, right):
    """Sample-wise ``left + right`` clipped to s16 - the overlap arithmetic."""
    first = array('h')
    first.frombytes(left)
    second = array('h')
    second.frombytes(right)
    if sys.byteorder == 'big':
        first.byteswap()
        second.byteswap()
    if len(first) != len(second):
        raise ValueError('overlapping blocks must be the same length')
    total = array('h', [min(32767, max(-32768, a + b))
                        for a, b in zip(first, second)])
    if sys.byteorder == 'big':
        total.byteswap()
    return total.tobytes()
