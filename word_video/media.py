"""Local media IO. No shell strings and no desktop automation."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import wave


def executable(name):
    path = shutil.which(name)
    if not path:
        candidate = Path.home() / '.local' / 'bin' / (name + '.exe')
        if candidate.is_file():
            path = str(candidate)
    if not path:
        raise FileNotFoundError(f'{name} not found')
    return path


def run(args, timeout=300):
    result = subprocess.run([str(x) for x in args], capture_output=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-3000:])
    return result.stdout


def probe(path):
    p = Path(path).resolve(strict=True)
    return json.loads(run([executable('ffprobe'), '-v', 'error', '-show_format',
                           '-show_streams', '-of', 'json', p], timeout=30))


def duration(path):
    value = float(probe(path)['format']['duration'])
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid media duration')
    return value


def has_audio(path):
    """True when the file really carries an audio stream.

    A reference intro clip may legitimately have no sound; asking ffmpeg to
    extract a stream that is not there fails the whole job with a cryptic
    "Output file does not contain any stream", so every consumer asks first.
    """
    return any(stream.get('codec_type') == 'audio'
               for stream in probe(path).get('streams', []))


def wav_duration(path):
    """Duration of a PCM WAV read straight from its header, with no subprocess.

    The tempo pass writes a plain mono 48 kHz WAV, so re-measuring it with
    ffprobe spawned a process per utterance just to re-read a number this
    program had already written - which dominated the per-item cost once the
    service calls were made concurrent.
    """
    with wave.open(str(path), 'rb') as stream:
        frames, rate = stream.getnframes(), stream.getframerate()
    if not rate or frames <= 0:
        raise ValueError('Invalid WAV duration')
    return frames / float(rate)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_audio(source, target, speed, fps=60):
    """At tempo ONCE; pad to a whole frame for identical draft/video durations."""
    if not math.isfinite(speed) or not .5 <= speed <= 2:
        raise ValueError('speed must be between 0.5 and 2')
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = duration(source)
    # Conservative padding covers codec/tempo rounding and is removed only as silence.
    frames = math.ceil(original / speed * fps) + 2
    temp = target.with_suffix('.partial.wav')
    try:
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', source,
             '-vn', '-af', f'atempo={speed},apad,atrim=duration={frames/fps:.9f}',
             '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', temp])
        rendered = wav_duration(temp)
        os.replace(temp, target)
        return original, rendered
    finally:
        temp.unlink(missing_ok=True)
