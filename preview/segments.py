"""按需/分段代理：先给一小段画面，其余在后台增量生成。

The measured problem
--------------------
Opening the preview of a 50-word lesson (192.5 s, 1080p60 background) took 66 s
before one frame could be shown, and the number grows linearly with the lesson: the
proxy was one file covering the whole course, encoded in full before the decoder was
allowed to start.  H0 measured it on the member's own route (2026-09-16).

What is done instead
--------------------
The picture source is proxied **in segments** - one small file per few seconds of the
lesson - and:

* the segment the playhead is on is encoded **on demand**, so the first frame costs one
  short segment instead of the whole course;
* the rest are encoded **in the background, in lesson order**, a bounded number of
  segments ahead of the playhead;
* a seek into a segment that is not ready yet is **visible**: the session raises its
  ``preparing`` state (the canvas draws it), the segment is promoted to the front of
  the background queue, and the picture appears as soon as it is encoded.  Never a
  silent black frame.

The three invariants from W04 are kept, not traded away
-------------------------------------------------------
``缓存有界``
    segment files live in the same ``preview-proxies`` folder and are pruned by the
    same byte budget, oldest first (``sources.prune_proxies``).  A whole 50-word
    lesson of segments is ~26 MB, the budget is 2 GB.
``失效条件明确``
    a segment's name carries the source's byte size and modification time, exactly
    like the single-file proxy's name, so a changed background can never be served
    from a stale segment.
``段间无洞``
    the grid is **stable**: segment *k* always covers item frames
    ``[k*S, (k+1)*S)`` of a source pass, independent of how long the lesson currently
    is - which is also what makes an edit (which changes the lesson length) reuse the
    segments already on disk instead of invalidating every one of them.  Each file is
    encoded with a one-frame preroll and a four-frame tail so that a boundary frame
    cannot be missing from both neighbours, and the measured frame count is checked
    before the segment is accepted.

What is deliberately *not* segmented (falling back to the single-file proxy, i.e. the
previous behaviour): a source that does not need a proxy at all, a picture whose speed
is not 1 (the preview has never applied picture speed, and inventing that mapping here
would be a second answer), and a slice that does not start at the source's beginning.
"""
from bisect import bisect_right
from dataclasses import dataclass, field
import os
import subprocess
import threading
import time
from pathlib import Path

from word_video.media.core import executable
from word_video.media.proxy import PROXY_PRESETS
from word_video.media.streams import video_stream

from .sources import preview_proxy_height, proxy_cache_dir

__all__ = ['DEFAULT_SEGMENT_SECONDS', 'FIRST_SEGMENT_SECONDS', 'PREFETCH_AHEAD',
           'PREROLL_FRAMES', 'TAIL_FRAMES', 'Segment', 'SegmentError',
           'SegmentPlan', 'SegmentPreparer', 'build_segment_plan', 'source_frames']

#: Target length of one segment.  Measured trade-off: 15 s of 1080p60 background
#: encodes to a 720p segment in ~5 s, and the playhead crosses a segment boundary about
#: twelve times in a 50-word lesson - each crossing restarts the decoder child (~150 ms
#: of picture, with the sound untouched), so longer segments mean fewer hitches while
#: shorter ones mean a faster answer to a seek into a segment nobody prepared.
DEFAULT_SEGMENT_SECONDS = 15.0
#: The **first** segment is short, because it is what the member waits for: the first
#: frame needs one segment, not the whole lesson.  Measured: 6 s of this background
#: encodes in ~2 s, which is what puts "首次预览" inside the 5 s budget.  It is a fixed
#: first cell of the grid, not a lesson-dependent one, so it stays valid across edits.
FIRST_SEGMENT_SECONDS = 6.0
#: Frames encoded before the segment's first needed frame.  Input seeking is
#: frame-accurate in a transcode, and this covers a one-frame slip if it ever is not:
#: the boundary frame is then present in two segment files rather than in none.
PREROLL_FRAMES = 1
#: Frames encoded past the segment's last needed frame, for the same reason at the
#: other end.
TAIL_FRAMES = 4
#: How many segments ahead of the playhead the background thread keeps warm.  Three
#: segments is ~18 s of lesson (~6 s of encoding): enough that ordinary playback never
#: waits, small enough that an editor left open on one word does not encode a whole
#: course nobody watched.
PREFETCH_AHEAD = 3


class SegmentError(RuntimeError):
    """A segment could not be encoded or did not hold what it promised."""


def source_frames(path, fps_num, fps_den):
    """How many frames a file holds, for the loop period.  ``0`` when unknown."""
    from word_video.media.core import duration
    try:
        seconds = duration(path)
    except (OSError, ValueError, RuntimeError):
        return 0
    return int(round(seconds * fps_num / float(fps_den)))


@dataclass(frozen=True)
class Segment:
    """One file of the grid: which item frames it must be able to answer for."""

    index: int
    pass_index: int
    grid: int
    #: First item frame this segment must cover.
    first_frame: int
    #: How many item frames it must cover (``min(S, coverage - first_frame)``).
    cover: int
    #: Which item frame the file's own frame 0 is.
    file_base_frame: int
    #: How many frames the file should hold (preroll + cover + tail, clipped to the pass).
    encode_frames: int
    #: Frame inside the source pass that ``file_base_frame`` maps to.
    source_frame: int
    path: Path

    @property
    def last_frame(self):
        return self.first_frame + self.cover - 1

    def to_dict(self):
        return {'index': self.index, 'pass': self.pass_index, 'grid': self.grid,
                'first_frame': self.first_frame, 'last_frame': self.last_frame,
                'file_base_frame': self.file_base_frame,
                'encode_frames': self.encode_frames, 'source_frame': self.source_frame,
                'path': str(self.path)}


@dataclass
class SegmentPlan:
    """The grid of segments for one picture source, and how to materialise one."""

    source: Path
    cache_dir: Path
    height: int
    fps_num: int
    fps_den: int
    source_total: int
    coverage: int
    segment_frames: int
    stamp: str
    crf: int
    segments: tuple = ()
    _firsts: tuple = field(default=(), repr=False)
    encoded: int = field(default=0, repr=False)

    def __post_init__(self):
        self._firsts = tuple(segment.first_frame for segment in self.segments)

    # -- lookup ----------------------------------------------------------
    def index_of(self, item_frame):
        """Which segment answers for this item frame (clamped into the grid)."""
        position = bisect_right(self._firsts, int(item_frame)) - 1
        return max(0, min(position, len(self.segments) - 1))

    def segment(self, index):
        return self.segments[max(0, min(int(index), len(self.segments) - 1))]

    def ready(self, index):
        segment = self.segment(index)
        try:
            return segment.path.is_file() and segment.path.stat().st_size > 0
        except OSError:
            return False

    def ready_count(self):
        return sum(1 for index in range(len(self.segments)) if self.ready(index))

    # -- materialising ---------------------------------------------------
    def ensure(self, index, on_child=None):
        """The segment, encoded if it is not on disk yet (raises ``SegmentError``)."""
        segment = self.segment(index)
        if self.ready(segment.index):
            return segment
        self.encode(segment, on_child=on_child)
        return segment

    def encode(self, segment, *, timeout=300.0, on_child=None):
        """Encode one segment and check that it holds the frames it promised.

        The encoder is started here rather than through ``media.core.run`` for one
        reason: this child must be **killable**.  A segment encode is background work
        that a closing session has to be able to stop in bounded time, and a wedged
        ffmpeg must not be joined for ten minutes - the same recoverable boundary the
        decoder child has, for the same reason.
        """
        segment.path.parent.mkdir(parents=True, exist_ok=True)
        # The temporary keeps the real extension: ffmpeg chooses its muxer from the
        # file name, and a name ending in ".part" is one it refuses to open.
        partial = segment.path.with_name('.%s.part%s' % (segment.path.stem,
                                                         segment.path.suffix))
        seconds = segment.source_frame * self.fps_den / float(self.fps_num)
        args = [executable('ffmpeg'), '-v', 'warning', '-nostdin', '-n',
                '-ss', '%.9f' % seconds,
                '-i', str(self.source),
                '-frames:v', str(int(segment.encode_frames)),
                '-vf', 'scale=-2:%d' % int(self.height),
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(self.crf),
                '-pix_fmt', 'yuv420p', '-an',
                '-movflags', '+faststart', str(partial)]
        child = None
        try:
            child = subprocess.Popen([str(part) for part in args],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess,
                                                           'CREATE_NO_WINDOW', 0))
            if on_child is not None:
                on_child(child)
            try:
                _, errors = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                raise SegmentError('段 %d 编码超过 %.0f 秒仍未结束'
                                   % (segment.index, timeout)) from None
            if child.returncode != 0:
                raise SegmentError('段 %d 编码失败：%s'
                                   % (segment.index,
                                      (errors or b'').decode('utf-8', 'replace')[-400:]))
            measured = source_frames(partial, self.fps_num, self.fps_den)
            # A duration-derived frame count is good to about one frame; below that,
            # the segment really is short and the boundary would be a hole.
            if measured and measured + 1 < segment.encode_frames:
                raise SegmentError('段 %d 只编码出 %d 帧，应有 %d 帧'
                                   % (segment.index, measured, segment.encode_frames))
            os.replace(partial, segment.path)
        except SegmentError:
            partial.unlink(missing_ok=True)
            raise
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise SegmentError('段 %d 编码失败：%s' % (segment.index, error)) from None
        finally:
            if on_child is not None:
                on_child(None)
        self.encoded += 1
        return {'index': segment.index, 'bytes': segment.path.stat().st_size,
                'frames': measured, 'seconds': round(segment.encode_frames
                                                     / (self.fps_num / float(self.fps_den)), 3)}

    def stats(self):
        return {'source': str(self.source), 'height': self.height,
                'segment_frames': self.segment_frames,
                'segment_seconds': round(self.segment_frames * self.fps_den
                                         / float(self.fps_num), 3),
                'segments': len(self.segments), 'coverage_frames': self.coverage,
                'ready': self.ready_count(), 'encoded_this_session': self.encoded,
                'cache_dir': str(self.cache_dir)}


def _grid_name(source, height, stamp, pass_index, grid):
    """A segment's file name: source identity + grid cell, so it is reusable.

    The name deliberately does **not** carry the lesson length.  An edit changes how
    many segments are needed, never what a given grid cell contains, so the segments
    already on disk stay valid - which is what keeps "editing does not wait for a
    re-encode" true for the preview cache as well as for the timeline.
    """
    return '%s.%dp.%s.seg%02d-%04d.mp4' % (source.stem, int(height), stamp,
                                           int(pass_index), int(grid))


def build_segment_plan(source, cache_dir, canvas_height, fps_num, fps_den, coverage_frames,
                       source_start=0, speed=1.0, segment_seconds=DEFAULT_SEGMENT_SECONDS,
                       first_segment_seconds=FIRST_SEGMENT_SECONDS):
    """The segment grid for a picture source, or ``None`` when it does not apply.

    ``None`` means "use the single-file proxy" (``sources.resolve_preview_source``),
    which is also what every non-segmented case did before: a source that is not
    taller than the canvas, a picture with a speed other than 1, a slice that does not
    start at the source's beginning, or a picture short enough that one segment would
    be the whole thing.
    """
    source = Path(source)
    if speed != 1.0 or int(source_start or 0) != 0 or int(coverage_frames) <= 0:
        return None
    if int(fps_num) <= 0 or int(fps_den) <= 0:
        return None
    try:
        picture = video_stream(source)
        source_height = int(picture['height'])
    except (OSError, ValueError, KeyError):
        return None
    if source_height <= int(canvas_height):
        return None
    height = preview_proxy_height(canvas_height)
    if source_height <= height:
        return None
    total = source_frames(source, fps_num, fps_den)
    if total <= 0:
        return None
    first_frames = max(1, int(round(first_segment_seconds * fps_num / float(fps_den))))
    segment_frames = max(1, int(round(segment_seconds * fps_num / float(fps_den))))
    coverage = int(coverage_frames)
    if coverage <= first_frames:
        # One short segment would cover it: that is the single-file proxy's job.
        return None
    stat = source.stat()
    stamp = '%d-%d' % (stat.st_size, int(stat.st_mtime))
    segments = []
    index = 0
    pass_index = 0
    start = 0
    while start < coverage:
        pass_start = pass_index * total
        pass_end = pass_start + total
        size = first_frames if start == 0 else segment_frames
        end = min(start + size, coverage, pass_end)
        grid = 0 if start == 0 else 1 + (start - first_frames) // segment_frames
        # A preroll only exists when there is a previous frame *in this pass*: at a
        # pass boundary the previous frame belongs to the previous pass, and a segment
        # that started there would hand the decoder the wrong picture.
        within_pass = start - pass_start
        file_base = start - PREROLL_FRAMES if within_pass >= PREROLL_FRAMES else start
        # The tail never crosses the pass boundary either, for the same reason.
        encode_frames = min(end - file_base + TAIL_FRAMES, pass_end - file_base)
        segments.append(Segment(
            index=index, pass_index=pass_index, grid=grid, first_frame=start,
            cover=end - start, file_base_frame=file_base, encode_frames=encode_frames,
            source_frame=file_base - pass_start,
            path=proxy_cache_dir(cache_dir) / _grid_name(source, height, stamp,
                                                         pass_index, grid)))
        index += 1
        start = end
        if end >= pass_end:
            pass_index += 1
    return SegmentPlan(source=source, cache_dir=Path(cache_dir), height=height,
                       fps_num=int(fps_num), fps_den=int(fps_den), source_total=total,
                       coverage=coverage, segment_frames=segment_frames, stamp=stamp,
                       crf=PROXY_PRESETS[height], segments=tuple(segments))


class SegmentPreparer:
    """One background thread that encodes the segments the playhead will need.

    Its order is deliberate: a **demand** (the segment a seek landed on) first, then
    the segments from the playhead forward, at most ``ahead`` of them.  The thread
    never touches the session's lock and never raises into it: a segment that cannot be
    encoded is recorded and skipped, and the session reports it.
    """

    def __init__(self, plan, ahead=PREFETCH_AHEAD, name='preview-segments'):
        self.plan = plan
        self.ahead = int(ahead)
        self.name = name
        self._lock = threading.Condition()
        self._stop = False
        self._demand = None
        self._focus = 0
        self._done = set()
        self._failed = {}
        self._encoded = []
        self._child = None
        self._thread = None

    # -- commands --------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._work, name=self.name, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=8.0):
        """Stop encoding and make sure nothing of ours is still running.

        The segment being encoded is *killed*, not waited for: closing the editor must
        not wait for an ffmpeg run that has seconds left, and a wedged encoder must not
        be joined at all.  ``False`` means the thread did not end within ``timeout``,
        which is reported rather than hidden.
        """
        with self._lock:
            self._stop = True
            child, self._child = self._child, None
            self._lock.notify_all()
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _register_child(self, child):
        """Called by the encoder with its process, then with ``None`` when it ends."""
        with self._lock:
            self._child = child
            cancelled = self._stop and child is not None
        if cancelled:
            try:
                child.kill()
            except OSError:
                pass

    def demand(self, index):
        """Encode this segment next, ahead of the background order."""
        with self._lock:
            self._demand = int(index)
            self._lock.notify_all()

    def focus(self, index):
        """The segment the playhead is on; the background order starts here."""
        with self._lock:
            self._focus = int(index)

    def ready(self, index):
        with self._lock:
            if int(index) in self._done:
                return True
        return self.plan.ready(index)

    def mark_ready(self, index):
        with self._lock:
            self._done.add(int(index))

    # -- worker ----------------------------------------------------------
    def _take(self):
        with self._lock:
            if self._stop:
                return None
            if self._demand is not None:
                index, self._demand = self._demand, None
                if index not in self._done:
                    return index
            for offset in range(self.ahead + 1):
                index = self._focus + offset
                if index >= len(self.plan.segments):
                    break
                if index not in self._done and index not in self._failed:
                    return index
            return None

    def _work(self):
        while True:
            index = self._take()
            if index is None:
                with self._lock:
                    if self._stop:
                        return
                    self._lock.wait(0.2)
                continue
            started = time.monotonic()
            try:
                segment = self.plan.ensure(index, on_child=self._register_child)
            except SegmentError as error:
                with self._lock:
                    if self._stop:
                        # The encode we were told to stop: not a failure to report.
                        return
                    self._failed[index] = str(error)
                continue
            with self._lock:
                self._done.add(index)
                self._encoded.append({'index': index,
                                      'seconds': round(time.monotonic() - started, 3),
                                      'bytes': segment.path.stat().st_size})

    def stats(self):
        with self._lock:
            return {'ahead': self.ahead, 'focus': self._focus, 'demand': self._demand,
                    'encoded': list(self._encoded),
                    'ready': sorted(self._done), 'failed': dict(self._failed),
                    'running': self._thread is not None,
                    'seconds': round(sum(row['seconds'] for row in self._encoded), 3)}
