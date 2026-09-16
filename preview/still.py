"""背景静帧：一张按画布尺寸抽好的 PNG，按需抽取、按源身份缓存。

Why a still, and why cached
---------------------------
Tim 2026-09-17: 预览只做**模板效果**（文字/单词），不再回放视频。  So the picture
behind the words is one frame of the delivery's background, extracted once per source
segment and reused: no decoder, no proxy, no per-frame work - and the text is drawn
against the same framing the export uses, because the extraction filter is the
renderer's own (``force_original_aspect_ratio=increase`` + ``crop``).

The cache is keyed the way the proxy cache is keyed - source path, canvas size, the
source's byte size and modification time, plus the instant the frame is taken - so a
replaced background can never be shown from a stale still, and no second cache
mechanism is invented for it.  Files live under the project's own ``.preview`` folder,
which is disposable by definition.

Extraction runs on **one background thread**: a frame decode is sub-second, but the
member opened a window to see text, not to watch ffmpeg.  A failure is remembered and
reported (``error``), never silent: the canvas has a line for "why there is no
picture", and the caller puts the reason into the window's diagnostics.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

__all__ = ['STILL_DIRNAME', 'StillFrames', 'still_filter']

#: Where the stills live inside the project's preview cache.
STILL_DIRNAME = 'stills'
#: One frame is a decode, not an encode: a generous bound only catches a wedged child.
DEFAULT_TIMEOUT = 120.0


def still_filter(width, height):
    """The renderer's own picture framing, at the canvas size.

    Same expression as the preview decoder used and as the delivered MP4 uses, so the
    still is the frame the export would show at that instant - a plain ``scale=W:H``
    would stretch a 4:3 source and put the text over a different picture.
    """
    return ('scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1'
            % (int(width), int(height), int(width), int(height)))


class StillFrames:
    """One still at a time, extracted in the background and cached by source identity.

    ``extract`` is injectable for tests (it defaults to ffmpeg); ``on_ready`` is called
    from the worker thread when a still appears, and is expected to schedule a repaint
    rather than to paint anything itself.
    """

    def __init__(self, cache_dir, width, height, *, extract=None, on_ready=None,
                 timeout=DEFAULT_TIMEOUT):
        self.cache_dir = Path(cache_dir) / STILL_DIRNAME
        self.width = int(width)
        self.height = int(height)
        self.timeout = float(timeout)
        self._extract = extract or self._ffmpeg_frame
        self._on_ready = on_ready
        self._lock = threading.Lock()
        self._thread = None
        self._wanted = None
        self._done = {}
        self._errors = {}

    # -- identity --------------------------------------------------------
    def name_for(self, source, seconds):
        """The still's file name: source identity + canvas + the instant it shows."""
        source = Path(source)
        try:
            stat = source.stat()
            stamp = '%d-%d' % (stat.st_size, int(stat.st_mtime))
        except OSError:
            stamp = 'missing'
        return '%s.%dx%d.%s.%dms.png' % (source.stem, self.width, self.height, stamp,
                                         int(round(float(seconds) * 1000)))

    def path_for(self, source, seconds):
        return self.cache_dir / self.name_for(source, seconds)

    # -- queries ---------------------------------------------------------
    def cached(self, source, seconds):
        """The still when it is already on disk, else ``None`` (never extracts)."""
        path = self.path_for(source, seconds)
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    def request(self, source, seconds):
        """The still, asking the background thread for it when it is not there yet."""
        found = self.cached(source, seconds)
        if found is not None:
            return found
        source = Path(source)
        if not source.is_file():
            with self._lock:
                self._errors[self.name_for(source, seconds)] = '背景文件不存在：%s' % source
            return None
        with self._lock:
            key = self.name_for(source, seconds)
            if key in self._errors:
                return None
            self._wanted = (source, float(seconds), key)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name='preview-still',
                                                daemon=True)
                self._thread.start()
        return None

    def wait(self, timeout=60.0):
        """Block until the worker is idle (a test or a measurement entry point)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            time.sleep(0.01)
        return False

    def error(self):
        """Why the picture is missing, in one sentence ('' when there is none)."""
        with self._lock:
            return next(iter(self._errors.values()), '')

    def stats(self):
        with self._lock:
            return {'dir': str(self.cache_dir), 'extracted': sorted(self._done),
                    'failed': dict(self._errors), 'worker_alive': bool(
                        self._thread is not None and self._thread.is_alive())}

    # -- worker ----------------------------------------------------------
    def _work(self):
        while True:
            with self._lock:
                wanted = self._wanted
                self._wanted = None
            if wanted is None:
                return
            source, seconds, key = wanted
            try:
                self._extract(source, seconds, self.path_for(source, seconds))
            except Exception as error:                  # noqa: BLE001 - reported, not raised
                with self._lock:
                    self._errors[key] = '%s: %s' % (type(error).__name__, error)
                continue
            with self._lock:
                self._done[key] = str(self.path_for(source, seconds))
            if callable(self._on_ready):
                try:
                    self._on_ready()
                except Exception:                       # noqa: BLE001 - a repaint hint
                    pass

    def _ffmpeg_frame(self, source, seconds, target):
        """Extract one frame at ``seconds`` into ``target`` (atomically)."""
        from word_video.media.core import executable

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name('.' + target.name + '.part.png')
        args = [executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
                '-ss', '%.6f' % max(0.0, float(seconds)), '-i', str(source),
                '-frames:v', '1', '-vf', still_filter(self.width, self.height),
                str(partial)]
        try:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, timeout=self.timeout,
                                       creationflags=getattr(subprocess,
                                                             'CREATE_NO_WINDOW', 0))
            if completed.returncode != 0 or not partial.is_file() \
                    or partial.stat().st_size == 0:
                raise RuntimeError('ffmpeg 抽帧失败：%s'
                                   % (completed.stderr or b'').decode('utf-8', 'replace')[-300:])
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target
