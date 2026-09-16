"""Read what a media file *really* contains, not what its header claims.

Three numbers are routinely confused with each other:

container duration
    what ``ffprobe -show_format`` reports.  For a WAV it is the byte length of
    the data chunk, so a file written with padding reports the padded length;
    for an MP4 it is the longest stream plus the edit list.
stream duration
    what the track itself says, which is what a muxer pads or trims to.
sample count
    ``nb_frames`` / packets - the actual number of audio samples.  This is the
    only one that can be compared with ``round(seconds * sample_rate)`` and the
    only one that catches an encoder that added or dropped a packet.

Consumers that mix by sample index need the third; consumers that place a clip
on a timeline need the presentation timestamp, because a stream that starts at
0.021 s is not misaligned, it is early-encoded, and re-timing it as if it
started at zero is what makes draft and MP4 drift apart.

Facts are returned as plain dicts (JSON-serialisable) so a job result can carry
them without importing a private type.
"""
import math
from pathlib import Path
import wave

from .core import probe

__all__ = ['audio_stream', 'video_stream', 'audio_sample_count', 'audio_pts',
           'stream_facts', 'video_stream_seconds']


def _streams(path):
    return list(probe(path).get('streams', []))


def _audio_streams(path):
    return [item for item in _streams(path) if item.get('codec_type') == 'audio']


def audio_stream(path):
    """The single audio stream of ``path``; refuses to guess between several."""
    found = _audio_streams(path)
    if not found:
        raise ValueError('No audio stream: %s' % path)
    if len(found) > 1:
        raise ValueError('Several audio streams, name one by index: %s' % path)
    return found[0]


def video_stream(path):
    """The single video stream of ``path``; refuses to guess between several."""
    found = [item for item in _streams(path) if item.get('codec_type') == 'video']
    if not found:
        raise ValueError('No video stream: %s' % path)
    if len(found) > 1:
        raise ValueError('Several video streams, name one by index: %s' % path)
    return found[0]


def _rate(stream):
    value = stream.get('sample_rate')
    if value in (None, ''):
        raise ValueError('Audio stream reports no sample rate')
    rate = int(float(value))
    if rate <= 0:
        raise ValueError('Audio stream reports an unusable sample rate')
    return rate


def audio_sample_count(path):
    """Real number of audio samples, preferring the file's own frame count.

    A PCM WAV is read straight from its header, which *is* the sample count and
    needs no subprocess at all.  Otherwise the stream's ``nb_frames`` is used;
    when the container does not publish it the stream duration is used instead,
    which is exact for a fixed-rate codec and off by at most one packet
    otherwise.  ``exact`` says which happened, so a caller that needs the
    stronger guarantee can tell.
    """
    try:
        with wave.open(str(path), 'rb') as stream:
            if stream.getcomptype() != 'NONE':
                raise ValueError('compressed WAV')
            count = stream.getnframes()
            if count > 0:
                return {'samples': count, 'sample_rate': stream.getframerate(),
                        'exact': True}
    except (wave.Error, EOFError, ValueError):
        pass
    stream = audio_stream(path)
    rate = _rate(stream)
    frames = stream.get('nb_frames')
    if frames not in (None, ''):
        count = int(float(frames))
        if count > 0:
            return {'samples': count, 'sample_rate': rate, 'exact': True}
    if stream.get('duration') not in (None, ''):
        seconds = float(stream['duration'])
        if math.isfinite(seconds) and seconds > 0:
            return {'samples': int(round(seconds * rate)), 'sample_rate': rate,
                    'exact': False}
    raise ValueError('Audio stream carries no usable length: %s' % path)


def audio_pts(path):
    """Start time of the audio stream, in seconds, and its time base.

    A non-zero value is information, not an error: it is what a timeline has to
    place *relative to*, and ignoring it shifts every later clip.
    """
    stream = audio_stream(path)
    start = stream.get('start_time')
    base = float(stream['time_base'].split('/')[1]) if stream.get('time_base') else None
    seconds = float(start) if start not in (None, '') else 0.0
    if not math.isfinite(seconds):
        raise ValueError('Audio stream reports an unusable start time')
    return {'start_s': seconds, 'time_base_denominator': base,
            'start_sample': (int(round(seconds * _rate(stream)))
                             if seconds and stream.get('sample_rate') else 0)}


def video_stream_seconds(path):
    """Duration of the *video stream*, which may be shorter than the container.

    A clip whose audio outruns its picture still reports the longer container
    length, so reading the container alone would miss a truncated countdown.  The
    fallback order matters: the stream's own duration first, then its frame count
    over its rate, then the container.

    It lives beside the other stream facts rather than in the timing module because
    two callers need it - the verified engine's intro length and the project's
    ``measure_intro`` - and a second implementation of "how long is this picture"
    is exactly the drift this package exists to prevent.
    """
    from .core import duration

    for stream in _streams(path):
        if stream.get('codec_type') != 'video':
            continue
        if stream.get('duration'):
            return float(stream['duration'])
        if stream.get('nb_frames'):
            rate = stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1'
            number, _, denominator = str(rate).partition('/')
            if float(denominator or 1) and float(number):
                return float(stream['nb_frames']) / (float(number) / float(denominator))
    return duration(path)


def stream_facts(path):
    """Everything the mixer and the timeline need about one file, in one probe."""
    resolved = str(Path(path).resolve(strict=True))
    facts = {'path': resolved, 'has_audio': bool(_audio_streams(resolved))}
    if facts['has_audio']:
        facts.update(audio_sample_count(resolved))
        facts.update(audio_pts(resolved))
    return facts
