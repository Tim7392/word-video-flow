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
           'samples_from_stream', 'stream_facts', 'video_stream_seconds',
           'opus_pre_skip']


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


def _duration_samples(stream, rate):
    """Samples from the stream's own duration: the safe answer for any codec."""
    if stream.get('duration') in (None, ''):
        return None
    seconds = float(stream['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return int(round(seconds * rate))


#: Codecs whose ``nb_frames`` really is a *sample* count.
#:
#: For a packetised codec (AAC, MP3, Vorbis, Opus, ...) ffprobe's ``nb_frames`` is
#: the number of **packets**, and one packet holds hundreds or thousands of samples
#: - AAC in an MP4 reports ``nb_frames=88`` for 1.856 s at 48 kHz, which is 89088
#: samples.  Trusting it is off by three orders of magnitude *and* was reported as
#: ``exact``.  Everything not named here is therefore answered from
#: ``duration x sample_rate`` and flagged as derived.
SAMPLE_COUNT_CODECS = ('pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_u8', 'pcm_f32le',
                       'pcm_f64le', 'pcm_s16be', 'pcm_s24be', 'pcm_s32be', 'flac',
                       'alac', 'wavpack', 'tta', 'truehd', 'mlp')


#: Codecs whose *container* length counts samples the decoder throws away.
#:
#: An Ogg/Opus granule position counts the pre-skip (the encoder's 6.5 ms priming)
#: as decoded audio, so ``duration x rate`` over-reports.  Measured over the whole
#: test archive: all 2 744 distinct Ogg/Opus files were over-reported by exactly
#: 312 samples at 48 kHz (the pre-skip the files declare), while the decoder emits
#: 312 fewer.  The number is written in the file's own ``OpusHead``, so it is read
#: rather than assumed - see :func:`opus_pre_skip`.
PRIMING_IN_DURATION = ('opus',)


def samples_from_stream(stream):
    """``{'samples', 'sample_rate', 'exact'}`` for one probed audio stream.

    Split out from :func:`audio_sample_count` so the rule can be exercised against
    the stream shapes real files produce - in particular the AAC one, whose
    ``nb_frames`` is a packet count - without needing a file of every codec.

    This is the *header* answer and stays a pure function of the stream: a
    container that counts priming samples (Opus) needs the file, and that
    correction is applied by :func:`audio_sample_count`.
    """
    rate = _rate(stream)
    codec = str(stream.get('codec_name') or '')
    frames = stream.get('nb_frames')
    if codec in SAMPLE_COUNT_CODECS and frames not in (None, ''):
        count = int(float(frames))
        if count > 0:
            return {'samples': count, 'sample_rate': rate, 'exact': True}
    derived = _duration_samples(stream, rate)
    if derived is not None:
        return {'samples': derived, 'sample_rate': rate, 'exact': False}
    if frames not in (None, ''):
        # No duration published, and the codec's nb_frames is not a sample count:
        # refusing beats inventing a number (packets are not samples).
        raise ValueError(
            'Audio stream %r publishes %s packets but no duration; the sample count '
            'cannot be derived without inventing one' % (codec or '?', frames))
    raise ValueError('Audio stream carries no usable length')


def opus_pre_skip(path):
    """The pre-skip an Ogg/Opus file declares, in 48 kHz samples.

    Opus always decodes at 48 kHz whatever the source rate was, so the pre-skip in
    ``OpusHead`` is already a 48 kHz sample count and needs no conversion.  The
    header is the first packet of the first (BOS) page, so exactly that page is
    read - no scanning for a magic string that a lacing table could also contain.
    Returns 0 for anything that is not an Ogg/Opus file.
    """
    try:
        with open(path, 'rb') as handle:
            header = handle.read(27)
            if header[:4] != b'OggS' or len(header) < 27:
                return 0
            lacing = handle.read(header[26])
            body = handle.read(sum(lacing))
    except OSError:
        return 0
    at = body.find(b'OpusHead')
    if at < 0 or len(body) < at + 12:
        return 0
    return int.from_bytes(body[at + 10:at + 12], 'little')


def _priming_samples(path, stream):
    """Samples the decoder discards at the start but the container counts."""
    if str(stream.get('codec_name') or '') not in PRIMING_IN_DURATION:
        return 0
    return opus_pre_skip(path)


def audio_sample_count(path):
    """How many samples of audio a file really holds.

    Three sources, in order of authority:

    1. a **PCM WAV's header**, which *is* the sample count and needs no subprocess;
    2. ``nb_frames`` for a codec where that field means samples
       (:data:`SAMPLE_COUNT_CODECS`);
    3. ``duration x sample_rate`` for everything else - including the packetised
       codecs, whose ``nb_frames`` counts packets and must never be read as samples.
       A container that counts priming samples as audio (Opus, see
       :data:`PRIMING_IN_DURATION`) has that priming subtracted, from the number the
       file itself declares.

    ``exact`` is true only for (1) and (2), so a caller that needs the stronger
    guarantee can tell a real count from a derived one instead of being handed a
    packet count labelled as exact.  A corrected count also carries
    ``priming_samples``, so the correction is visible rather than silent.
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
    facts = samples_from_stream(stream)
    priming = _priming_samples(path, stream)
    if priming and not facts['exact']:
        return {'samples': max(0, facts['samples'] - priming),
                'sample_rate': facts['sample_rate'], 'exact': False,
                'priming_samples': priming}
    return facts


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
