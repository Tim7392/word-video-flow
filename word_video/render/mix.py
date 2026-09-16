"""Sample-accurate PCM assembly; prepared speech is never time-scaled again.

The intro is mixed here and nowhere else.  Which file sounds during the intro is
decided by :func:`word_video.media.resolve_intro_audio` - the clip's own
countdown sound, else the configured ``intro_audio``, else silence - and the
renderer passes that decision in, so the MP4 and the editable draft cannot end
up describing two different intros.  Exactly one source is ever mixed in; the
two are alternatives, never a layer.
"""
import os
from pathlib import Path
import uuid
import wave

from ..media import executable, probe, resolve_intro_audio, run


def intro_audio_seconds(path):
    """Duration of the file's audio stream - the number the intro is built from."""
    for stream in probe(path).get('streams', []):
        if stream.get('codec_type') == 'audio':
            if stream.get('duration'):
                value = float(stream['duration'])
            else:
                from ..media import duration
                value = duration(path)
            if value <= 0:
                break
            return value
    raise ValueError('Intro sound has no usable duration: %s' % path)


def build_mix(manifest, target, cancel=None, intro_audio=None):
    target = Path(target).resolve()
    if target.exists():
        raise FileExistsError(target)
    def check():
        if cancel and cancel():
            raise RuntimeError('Audio assembly cancelled')
    check()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name('.' + uuid.uuid4().hex + '.wav')
    intro = target.with_name('.intro-' + uuid.uuid4().hex + '.wav')
    # Frame -> sample by truncation, not rounding: a frame owns the samples that
    # start inside it, so frame N begins at floor(N * rate / fps).  Rounding up
    # asks for a sample the previous frame already owns and makes the last frame
    # of a clip one sample-too-long - which an exact-length countdown then fails.
    sample = lambda frame: frame * 48000 // manifest.fps
    # The resolved source is the only thing that may sound.  A silent clip is
    # valid input - the intro is then simply silent - and must not fail the job
    # the way it did before M0, when ffmpeg was asked for a stream that was not
    # there ("Output file does not contain any stream").
    if intro_audio is None:
        choice = resolve_intro_audio(manifest.intro_video, None, manifest.intro_audio)
        intro_audio = choice.path
    events = []
    if intro_audio:
        if manifest.intro_frames <= 0:
            raise ValueError('Intro sound without intro duration')
        window = sample(manifest.intro_frames)
        available = round(intro_audio_seconds(intro_audio) * 48000)
        if available < window - 48000 // manifest.fps:
            # The intro sound defines the intro's length (timing.py reads it from
            # the same resolver), so a genuinely short file here means the two
            # disagreed and the tail of the countdown would be cut.  A file that
            # is short by less than one frame is just frame rounding: it gets
            # silence padding below, exactly as a speech stage does - never a
            # trim of real audio.
            raise ValueError('Intro sound is shorter than the intro stage')
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', intro_audio,
             '-t', '%.9f' % (window / 48000), '-ar', '48000', '-ac', '1',
             '-c:a', 'pcm_s16le', intro])
        events.append((0, window, intro))
    events.extend((sample(a['start_frame']), sample(a['start_frame'] + a['duration_frames']),
                   Path(a['path'])) for a in manifest.audio)
    events.sort(key=lambda e: e[0])
    end = sample(manifest.total_frames)
    previous = 0
    try:
        with wave.open(str(partial), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(48000)
            def zeros(count):
                while count:
                    check(); chunk = min(count, 48000)
                    output.writeframesraw(b'\0' * (chunk * 2)); count -= chunk
            for start, stop, path in events:
                check()
                if start < previous or stop <= start or stop > end:
                    raise ValueError('Overlapping or out-of-range audio stage')
                zeros(start - previous)
                with wave.open(str(path), 'rb') as source:
                    if (source.getnchannels(), source.getsampwidth(),
                            source.getframerate()) != (1, 2, 48000):
                        raise ValueError('Prepared speech must be mono 48kHz signed16 PCM')
                    available = source.getnframes(); capacity = stop - start
                    if available > capacity + 1:
                        raise ValueError('Speech longer than allocated stage')
                    remaining = min(available, capacity)
                    while remaining:
                        check()
                        frames = min(remaining, 48000); block = source.readframes(frames)
                        if len(block) != frames * 2:
                            raise ValueError('Truncated WAV data')
                        output.writeframesraw(block); remaining -= frames
                    zeros(capacity - min(available, capacity))
                previous = stop
            zeros(end - previous)
        check()
        os.link(partial, target)
        return str(target)
    finally:
        partial.unlink(missing_ok=True); intro.unlink(missing_ok=True)
