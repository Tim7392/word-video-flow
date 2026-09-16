"""Shared support for the preview tests: synthetic media, plans, and a D:-side tmp.

Kept as a ``test_preview_*`` module so it lives inside this round's allowed write
scope, and so the preview tests share one definition of "a plan with three
distinguishable words" instead of three drifting copies.

Two things are deliberate:

* **The temp root is on the work root's drive.**  The project keeps development
  data, caches and outputs under the work root, and a preview test writes decoded
  frames and proxies; pytest's default temp directory is on C:.  The location is
  discovered by walking up for ``runtime/`` (the same rule the packaging scripts
  use) and falls back to the system temp for a checkout that has no work root, so
  QA can run this outside the normal layout.
* **Each word is a different tone.**  "After a seek the old audio must not be
  heard" is not something a human ear can turn into a regression test.  A distinct
  frequency per clip is: the test reads back the PCM the device was actually
  given and names the word from its pitch, so a block from the previous position
  shows up as the wrong frequency rather than as a vague complaint.
"""
import contextlib
import math
from array import array
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

from word_video.domain.model import MediaSlice
from word_video.domain.plan import PlanItem, RenderPlan
from word_video.domain.timebase import TICKS_PER_SECOND

RATE = 48000


def work_root():
    """The work root above this checkout, or ``None`` outside the normal layout."""
    for candidate in list(Path(__file__).resolve().parents)[:5]:
        if (candidate / 'runtime').is_dir() and (candidate / 'worktrees').is_dir():
            return candidate
    return None


def tmp_root():
    """Where preview tests may write: ``<work root>/runtime/tmp/<role>``."""
    override = os.environ.get('WORD_VIDEO_PREVIEW_TMP')
    if override:
        return Path(override)
    root = work_root()
    if root is None:
        return Path(tempfile.gettempdir()) / 'wv-preview'
    return root / 'runtime' / 'tmp' / Path(__file__).resolve().parents[1].name / 'pytest'


@contextlib.contextmanager
def scratch(prefix='preview-'):
    """A fresh directory under :func:`tmp_root`, removed on exit."""
    root = tmp_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# -- synthetic media -----------------------------------------------------
def write_tone(path, seconds, frequency=440.0, rate=RATE, amplitude=8000):
    """A mono 48 kHz signed-16 WAV holding one sine tone."""
    frames = int(round(seconds * rate))
    samples = array('h', [int(amplitude * math.sin(2 * math.pi * frequency * i / rate))
                          for i in range(frames)])
    if sys.byteorder == 'big':
        samples.byteswap()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


def dominant_hz(pcm, rate=RATE):
    """The tone a PCM block sounds like, by zero crossings.

    Exact enough to tell three distinct word tones apart, which is all the
    "did the old position's audio survive the seek" assertion needs, and it has
    no dependency the project does not already have.
    """
    count = len(pcm) // 2
    if count < 2:
        return 0.0
    samples = struct.unpack('<%dh' % count, pcm[:2 * count])
    crossings = 0
    previous = samples[0]
    for value in samples[1:]:
        if (previous < 0 <= value) or (previous >= 0 > value):
            crossings += 1
        previous = value
    seconds = count / float(rate)
    return crossings / (2.0 * seconds)


def peak(pcm):
    count = len(pcm) // 2
    if not count:
        return 0
    return max(abs(value) for value in struct.unpack('<%dh' % count, pcm[:2 * count]))


def samples_of(pcm):
    count = len(pcm) // 2
    return struct.unpack('<%dh' % count, pcm[:2 * count])


def write_test_video(path, seconds=1.0, fps=30, size='160x120', rate=RATE):
    """A tiny real MP4 with a video and (optionally silent) audio track."""
    from word_video.media.core import executable, run
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
         '-f', 'lavfi', '-i', 'testsrc=size=%s:rate=%d:duration=%.4f' % (size, fps, seconds),
         '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'ultrafast',
         '-movflags', '+faststart', str(path)])
    return path


# -- plans ---------------------------------------------------------------
def ticks(seconds):
    return int(round(seconds * TICKS_PER_SECOND))


def speech_item(clip_id, role, asset_id, start_s, seconds, source_seconds=None,
                gain_db=0.0, record_id='w1'):
    source_seconds = seconds if source_seconds is None else source_seconds
    units = int(round(source_seconds * RATE))
    return PlanItem(clip_id=clip_id, role=role, record_id=record_id,
                    start_ticks=ticks(start_s), end_ticks=ticks(start_s + seconds),
                    text=clip_id,
                    source=MediaSlice(asset_id=asset_id, source_start=0,
                                      source_end=units, unit_num=1, unit_den=RATE,
                                      gain_db=gain_db))


def background_item(asset_id, total_seconds, start_s=0.0):
    return PlanItem(clip_id='layer.background', role='background', record_id='',
                    start_ticks=ticks(start_s), end_ticks=ticks(total_seconds),
                    source=MediaSlice(asset_id=asset_id, source_start=0,
                                      source_end=int(total_seconds * RATE),
                                      unit_num=1, unit_den=RATE))


def plan_of(audio, video=(), total_seconds=None, fps=30, width=1920, height=1080,
            rate=RATE):
    """A RenderPlan from ready-made items; ``total_seconds`` defaults to the end."""
    entries = tuple(audio) + tuple(video)
    total = ticks(total_seconds) if total_seconds is not None \
        else max(item.end_ticks for item in entries)
    return RenderPlan(project_id='p', project_revision=0, fps_num=fps, fps_den=1,
                      width=width, height=height, sample_rate=rate, channels=1,
                      total_ticks=total, video=tuple(video), audio=tuple(audio))


def three_word_plan(assets, gap=0.0):
    """Three words, three tones, laid end to end - the "no old audio" fixture.

    Word 1 is 300 Hz, word 2 is 900 Hz, word 3 is 1700 Hz: far enough apart that a
    zero-crossing estimate cannot confuse them.
    """
    tones = (300.0, 900.0, 1700.0)
    audio = []
    cursor = 0.0
    for index in range(len(tones)):
        duration = 0.5
        key = 'tone%d' % (index + 1)
        assert key in assets, 'the fixture needs %s' % key
        audio.append(speech_item('w%d.female' % (index + 1), 'female', key,
                                 cursor, duration))
        cursor += duration + gap
    return plan_of(audio, total_seconds=cursor - gap), tones


def three_tone_assets(folder):
    """``{asset_id: path}`` for the three-word fixture, written to ``folder``."""
    assets = {}
    for index, frequency in enumerate((300.0, 900.0, 1700.0)):
        key = 'tone%d' % (index + 1)
        assets[key] = str(write_tone(Path(folder) / ('%s.wav' % key), 0.5, frequency))
    return assets


def process_counters(pid=None):
    """Private bytes / handles / threads of this process, for the resource report."""
    pid = pid or os.getpid()
    counters = {}
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t),
                        ('PrivateUsage', ctypes.c_size_t)]

        counters['private_bytes'] = None
        process = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if process:
            data = Counters()
            data.cb = ctypes.sizeof(Counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(data), data.cb):
                counters['private_bytes'] = data.PrivateUsage
            ctypes.windll.kernel32.CloseHandle(process)
        handle_count = wintypes.DWORD()
        if ctypes.windll.kernel32.GetProcessHandleCount(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(handle_count)):
            counters['handles'] = handle_count.value
    except Exception as error:  # pragma: no cover - diagnostics only
        counters['error'] = str(error)
    try:
        import threading
        counters['threads'] = threading.active_count()
    except Exception:  # pragma: no cover
        pass
    return counters


def child_processes():
    """PIDs of live children of this process (decoder leak evidence)."""
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '(Get-CimInstance Win32_Process -Filter "ParentProcessId=%d").ProcessId'
             % os.getpid()],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]
