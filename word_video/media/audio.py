"""Assemble prepared speech onto one timeline, with bounded overlap and volume.

``render.mix`` answered a narrower question: every spoken stage is disjoint, so
the mix is a concatenation with silence between the stages.  That is still the
teaching default, but it is not the whole of what the audio side can express -
an intro sting under the first word, or a sound effect under a stage, is a
*deliberate* overlap, and encoding it as "longer stage" moves the words and
changes the deliverable.

So the arithmetic lives here, once:

* :func:`mix_clips` is the general primitive.  Clips carry gain and a
  sample-accurate window, may overlap, and sum linearly (the same addition the
  MP4 mixdown and the draft both rely on).  It is bounded: every source is one
  streamed read of at most its own window, so memory does not grow with the
  number of clips or with the length of the lesson.
* :func:`teaching_overlap_conflicts` is the *policy* that sits above it.  Only
  genuine overlaps are reported, and the caller decides whether they are the
  teaching-blocking kind (two voices of the same word at once) or a legitimate
  bed (one intro/effect track under one speech track).

The two are deliberately separate: a policy that cannot express an overlap at
all cannot report a useful error when a project contains one.
"""
from array import array
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tempfile
import uuid
import wave

from .core import run, executable

__all__ = ['AudioClip', 'audio_format', 'mix_clips', 'teaching_overlap_conflicts',
           'SOURCE_RATE', 'CLIP_GAIN_MAX']

SOURCE_RATE = 48000
CLIP_GAIN_MAX = 4.0
# One second of mono s16.  Big enough that the per-chunk overhead disappears,
# small enough that the working set stays flat regardless of clip length.
_READ_CHUNK = SOURCE_RATE


@dataclass(frozen=True)
class AudioClip:
    """One prepared WAV placed on the mix, half-open ``[start_sample, end_sample)``.

    ``gain`` is a linear amplitude factor, 1.0 = unchanged.  It is a *mix*
    parameter, not a re-encode: nothing here writes a scaled copy of a source.
    """

    path: str
    start_sample: int
    end_sample: int
    gain: float = 1.0
    label: str = ''


def audio_format(path):
    """(channels, sample_width, rate, frames) of a PCM WAV, without ffprobe."""
    with wave.open(str(path), 'rb') as stream:
        if stream.getcomptype() != 'NONE':
            raise ValueError('Compressed WAV is not usable as prepared speech: %s' % path)
        return (stream.getnchannels(), stream.getsampwidth(),
                stream.getframerate(), stream.getnframes())


def _scaled(block, gain):
    """One chunk of mono s16 bytes, multiplied by ``gain`` and clipped.

    ``array`` follows the host byte order while WAV is little-endian, so the
    little-endian bytes are what crosses this boundary in both directions.
    """
    samples = array('h')
    samples.frombytes(block)
    if os.sys.byteorder == 'big':
        samples.byteswap()
    if gain != 1.0:
        samples = array('h', [min(32767, max(-32768, int(value * gain)))
                              for value in samples])
    if os.sys.byteorder == 'big':
        samples.byteswap()
    return samples.tobytes()


def mix_clips(clips, target, total_samples, rate=SOURCE_RATE):
    """Sum ``clips`` into one mono s16 WAV of exactly ``total_samples`` samples.

    Every clip must already be mono signed-16 PCM at ``rate`` (what
    :func:`word_video.media.prepare_audio` produces).  A clip shorter than its
    window is followed by silence; a clip longer than its window is an error
    rather than a truncation, because silently shortening speech is exactly the
    defect this pipeline must not have.
    """
    target = Path(target)
    if total_samples <= 0:
        raise ValueError('total_samples must be positive')
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(clips, key=lambda clip: (clip.start_sample, clip.end_sample))
    for clip in ordered:
        if clip.start_sample < 0 or clip.end_sample <= clip.start_sample:
            raise ValueError('Clip window must be a non-empty forward range: %r'
                             % (clip.label or clip.path,))
        if clip.end_sample > total_samples:
            raise ValueError('Clip %r runs past the end of the mix'
                             % (clip.label or clip.path,))
        if not math.isfinite(clip.gain) or clip.gain < 0 or clip.gain > CLIP_GAIN_MAX:
            raise ValueError('Clip gain out of range for %r' % (clip.label or clip.path,))
        channels, width, source_rate, frames = audio_format(clip.path)
        if (channels, width, source_rate) != (1, 2, rate):
            raise ValueError('Prepared speech must be mono %dHz signed16 PCM: %s'
                             % (rate, clip.path))
        capacity = clip.end_sample - clip.start_sample
        if frames > capacity:
            raise ValueError('Speech longer than allocated stage for %r (%d > %d samples)'
                             % (clip.label or clip.path, frames, capacity))
    # A zero-filled s16 buffer; built from an int list so host byte order cannot
    # reinterpret the zeros (it could not here, but the read path must not rely on that).
    buffer = array('h', [0]) * total_samples
    for clip in ordered:
        offset = clip.start_sample
        remaining = clip.end_sample - clip.start_sample
        with wave.open(str(clip.path), 'rb') as stream:
            while remaining:
                block = stream.readframes(min(remaining, _READ_CHUNK))
                if not block:
                    break
                addition = array('h')
                addition.frombytes(_scaled(block, clip.gain))
                if os.sys.byteorder == 'big':
                    addition.byteswap()
                position = offset
                for value in addition:
                    total = buffer[position] + value
                    buffer[position] = 32767 if total > 32767 else (
                        -32768 if total < -32768 else total)
                    position += 1
                offset += len(addition)
                remaining -= len(addition)
    if os.sys.byteorder == 'big':
        buffer.byteswap()
    if os.sys.byteorder == 'big':
        buffer.byteswap()
    partial = target.with_name('.' + uuid.uuid4().hex + target.suffix)
    try:
        with wave.open(str(partial), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(buffer.tobytes())
        os.replace(partial, target)
        return str(target)
    finally:
        partial.unlink(missing_ok=True)


def teaching_overlap_conflicts(clips):
    """Overlaps that make a lesson ambiguous, as pairs of labels.

    Two speech clips overlapping is ambiguous *when they would be heard as one
    voice track*: the same word read twice at once, or two different words
    talking over each other.  An overlap that involves a bed (an intro, an
    effect, a music track) is reported too, but with the pair that the caller
    asked for, so the policy layer can allow it.

    The rule implemented here is the conservative one the delivery rules ask
    for: any overlap between two clips whose ``label`` is a spoken role
    (``female``/``male``/``chinese``) is a conflict.  Everything else is
    returned as an informational pair and the caller decides.
    """
    spoken = ('female', 'male', 'chinese')
    conflicts = []
    ordered = sorted(clips, key=lambda clip: (clip.start_sample, clip.end_sample))
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if right.start_sample >= left.end_sample:
                break
            if left.label in spoken and right.label in spoken:
                conflicts.append((left.label, right.label))
    return conflicts
