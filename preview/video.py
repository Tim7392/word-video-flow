"""Decode picture frames in a child process, so a wedged decoder can be killed.

Why a child process and not an in-process decoder
-------------------------------------------------
The role rules require a *recoverable boundary* when native decoding hangs, and
that requirement decides the mechanism.  A decoder running inside this process
can fail in ways no timeout can undo: a demuxer waiting on a path that never
answers, a driver-level stall, a codec that never returns.  The only reliable
answer is a process we can kill.  Killing closes the pipe, which unblocks the
reader thread by itself, so no path here ends in joining a thread that will never
return - the specific failure the rules forbid.

It also keeps the clock honest.  A Qt media player would decode *and* play on its
own clock, and a preview that syncs two independent players is exactly the
"多播放器各播一轨凑同步" the architecture rejects.  Here ffmpeg produces raw
frames and nothing else; when a frame reaches the screen is decided by the PCM
clock in :mod:`preview.clock`.

Frames are raw ``bgr0`` - 4 bytes per pixel in BGRX order - because that is
byte-for-byte what ``QImage.Format_RGB32`` reads on a little-endian machine, so
presenting a frame is a wrap rather than a conversion, and no per-pixel Python
runs.

Framing matches the delivered MP4
---------------------------------
The scale filter is the same ``force_original_aspect_ratio=increase`` + ``crop``
the renderer uses, not a plain ``scale=W:H``: a preview that stretched a 4:3
source to fill a 16:9 canvas would show text sitting somewhere the export does
not put it.

Looping a background
--------------------
When the picture item is longer than its source the source is repeated, and the
repeat is a *restart at source offset zero within the same generation*: frame
numbers keep counting so presentation timestamps stay monotonic, and the old
child is still killed before the new one starts.  That is the whole-file repeat
``render._looped_background`` builds with the concat demuxer, so preview and
delivery loop at the same instant.
"""
import subprocess
import threading
import time
from dataclasses import dataclass

from word_video.domain.timebase import TICKS_PER_SECOND

from .queues import BLOCK, BoundedQueue, QueueClosed

__all__ = ['DecodeSpec', 'DecodeStats', 'FfmpegFrameDecoder', 'PIXEL_BYTES',
           'VideoFrame']

#: bgr0: one byte each of blue, green, red, unused.
PIXEL_BYTES = 4
#: Frames the reader may run ahead of the consumer.  Each is ``w*h*4`` bytes, so
#: this is the picture-side memory bound and it is deliberately small: a preview
#: that buffers twenty 720p frames is holding 74 MB to no benefit.
PIPE_DEPTH = 6
#: How long the reader's blocking queue waits before re-checking the stop flag.
_PUT_SLICE = 0.5


def _read_exact(stream, size):
    """Read exactly ``size`` bytes, or ``None`` at end of stream.

    A pipe read may legally return short, so one ``read`` is not enough, and
    treating a short read as a frame would shear the picture.
    """
    chunks = []
    remaining = size
    while remaining:
        block = stream.read(remaining)
        if not block:
            return None
        chunks.append(block)
        remaining -= len(block)
    return b''.join(chunks)


@dataclass(frozen=True)
class VideoFrame:
    """One decoded picture, with the generation and timeline tick it belongs to."""

    generation: int
    index: int
    pts_ticks: int
    width: int
    height: int
    buffer: bytes

    @property
    def bytes_per_line(self):
        return self.width * PIXEL_BYTES


@dataclass(frozen=True)
class DecodeSpec:
    """Everything the decoder needs to produce the frames of one picture item."""

    path: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    #: Timeline tick of frame 0 of the item.
    start_ticks: int
    #: Timeline tick the item ends at; decoding stops here.
    end_ticks: int
    #: Length of the source file in frames; the loop period.
    source_frames: int
    loop: bool = False
    #: Start this run at this offset into the source, in frames.
    offset_frames: int = 0

    @property
    def frame_ticks(self):
        return self.fps_den * TICKS_PER_SECOND // self.fps_num

    def ticks_of(self, index):
        return self.start_ticks + index * self.frame_ticks

    @property
    def frames_needed(self):
        span = max(0, self.end_ticks - self.start_ticks)
        return (span + self.frame_ticks - 1) // self.frame_ticks

    def to_dict(self):
        return {'path': self.path, 'width': self.width, 'height': self.height,
                'fps': '%d/%d' % (self.fps_num, self.fps_den),
                'start_ticks': self.start_ticks, 'end_ticks': self.end_ticks,
                'source_frames': self.source_frames, 'loop': self.loop,
                'offset_frames': self.offset_frames}


@dataclass
class DecodeStats:
    """What the decoder did, for the UI and for the resource evidence."""

    runs: int = 0
    frames: int = 0
    stalls: int = 0
    recoveries: int = 0
    failures: int = 0
    loops: int = 0
    killed: int = 0
    last_error: str = ''
    failure_reason: str = ''

    def to_dict(self):
        return dict(self.__dict__)


class FfmpegFrameDecoder:
    """A decoder thread that owns at most one child process at a time.

    The public surface is deliberately small: :meth:`start`, :meth:`request`
    (decode this item as generation ``g``), :meth:`stop`.  A request *replaces*
    the current one, and the worker notices at its next frame and restarts.  That
    is what makes rapid seeking cheap: twenty clicks in a second leave one child
    running, not twenty.
    """

    def __init__(self, queue, ffmpeg=None, stall_timeout=5.0, max_recoveries=1,
                 spawn_timeout=15.0):
        self.queue = queue
        self.ffmpeg = list(ffmpeg) if ffmpeg else None
        self.stall_timeout = float(stall_timeout)
        self.max_recoveries = int(max_recoveries)
        self.spawn_timeout = float(spawn_timeout)
        self.stats = DecodeStats()
        #: Newest presentation timestamp this decoder has *handed to the queue*.
        #: "Produced" and "shown" are different questions, and this is the first one:
        #: a caller that is about to change which file is being decoded (the segmented
        #: preview switching source files) has to resume after everything already
        #: queued, or those frames are produced twice and the picture jumps backwards.
        self.newest_pts = None
        self._condition = threading.Condition()
        self._request = None
        self._running = False
        self._thread = None
        self._process = None

    def forget_produced(self):
        """Nothing is 'already produced': the consumer has discarded the queue (a seek)."""
        with self._condition:
            self.newest_pts = None

    # -- command ---------------------------------------------------------
    def command(self, spec, offset_frames):
        """The ffmpeg invocation for one decode run.

        A method rather than an inline list because the fault test runs the *real*
        watchdog against a child that never produces a frame; it substitutes a
        command that blocks instead of stubbing this class, so the kill path under
        test is the one that ships.
        """
        from word_video.media.core import executable
        prefix = self.ffmpeg or [executable('ffmpeg')]
        offset = offset_frames * spec.fps_den / spec.fps_num
        scale = ('scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1'
                 % (spec.width, spec.height, spec.width, spec.height))
        return prefix + [
            '-v', 'error', '-nostdin',
            '-ss', '%.6f' % offset,
            '-i', spec.path,
            '-an', '-sn', '-dn',
            '-vf', scale,
            '-f', 'rawvideo', '-pix_fmt', 'bgr0',
            '-']

    # -- lifecycle -------------------------------------------------------
    def start(self):
        with self._condition:
            if self._running:
                return False
            self._running = True
            self._thread = threading.Thread(target=self._work, name='preview-decode',
                                            daemon=True)
            self._thread.start()
            return True

    def request(self, generation, spec):
        """Make ``spec`` at ``generation`` the current decode target."""
        with self._condition:
            self._request = (generation, spec)
            self._condition.notify_all()

    def stop(self, timeout=5.0):
        """Stop decoding and make sure nothing of ours is still running.

        Returns True when the worker really ended.  ``False`` means the thread did
        not exit within ``timeout`` - reported, never waited on forever.
        """
        with self._condition:
            self._running = False
            self._request = None
            self._condition.notify_all()
        self._kill_child()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True

    @property
    def running(self):
        with self._condition:
            return self._running

    def alive_threads(self):
        thread = self._thread
        return 1 if thread is not None and thread.is_alive() else 0

    def alive_children(self):
        process = self._process
        return 1 if process is not None and process.poll() is None else 0

    # -- worker ----------------------------------------------------------
    def _work(self):
        while True:
            with self._condition:
                while self._running and self._request is None:
                    self._condition.wait(0.2)
                if not self._running:
                    return
                request = self._request
                generation, spec = request
            self._run(generation, spec)
            # Parking matters as much as running: a request that has been served,
            # or whose child died at once, must not be picked up again, or the
            # worker spins re-spawning processes and burning a core for nothing.
            with self._condition:
                if self._request is request:
                    self._request = None

    def _superseded(self, generation, spec=None):
        """Is this run no longer what the caller wants?

        The **spec** is compared, not just the generation.  A segmented picture
        replaces its source file without a new generation (nothing is flushed and the
        clock is untouched - that is what makes crossing a segment boundary a decoder
        restart rather than a seek), and a run that kept going until its own file ended
        would deliver frames from the old segment *after* the new segment had started -
        frames the presentation step then shows out of order, which is a visible jump
        backwards.  Comparing the spec makes "a request replaces the current one" true
        for those replacements too.
        """
        with self._condition:
            if not self._running:
                return True
            if self._request is None:
                return True
            if self._request[0] != generation:
                return True
            return spec is not None and self._request[1] is not spec

    def _run(self, generation, spec):
        """Decode one item at one generation, looping the source when asked.

        Two counters, deliberately: ``item`` numbers frames *within the item* and
        is what presentation timestamps come from, while ``source`` is the offset
        inside the file and is what a loop wraps.  Collapsing them is how a looped
        background ends up replaying from timestamp zero, which the presentation
        step would then reject as late for the rest of the lesson.
        """
        item = spec.offset_frames
        attempts = 0
        self.stats.runs += 1
        while not self._superseded(generation, spec):
            if item >= spec.frames_needed:
                return
            if spec.source_frames and not spec.loop and item >= spec.source_frames:
                return
            if spec.source_frames and item and item % spec.source_frames == 0:
                self.stats.loops += 1
            source = item % spec.source_frames if spec.source_frames else item
            before = self.stats.frames
            item, stalled = self._pump(generation, spec, item, source)
            if stalled:
                if attempts >= self.max_recoveries:
                    self.stats.failures += 1
                    self.stats.failure_reason = (
                        '解码器在 %s 的第 %d 帧后无响应，重试 %d 次仍未恢复'
                        % (spec.path, item, attempts))
                    return
                attempts += 1
                self.stats.recoveries += 1
                continue
            if self.stats.frames == before and item < spec.frames_needed:
                # The child ended without producing anything and we are not at the
                # item's end: looping it again would spin.
                return

    def _pump(self, generation, spec, item, source):
        """Run one child to its end; returns the next item frame index and stall flag."""
        size = spec.width * spec.height * PIXEL_BYTES
        frames = BoundedQueue(maxlen=PIPE_DEPTH, overflow=BLOCK, name='decode-pipe')
        process = None
        reader = None
        try:
            process = subprocess.Popen(
                [str(part) for part in self.command(spec, source)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._process = process
            reader = threading.Thread(target=self._read_frames, args=(process, size, frames),
                                      name='preview-read', daemon=True)
            reader.start()
            while True:
                if self._superseded(generation, spec):
                    return item, False
                block = frames.get(timeout=self.stall_timeout)
                if block is None:
                    if frames.closed:
                        return item, False
                    # Nothing for a whole stall timeout: treat the child as wedged,
                    # kill it, and let the caller decide whether to recover.
                    self.stats.stalls += 1
                    self._kill(process)
                    return item, True
                if spec.ticks_of(item) >= spec.end_ticks:
                    return item, False
                frame = VideoFrame(generation=generation, index=item,
                                   pts_ticks=spec.ticks_of(item), width=spec.width,
                                   height=spec.height, buffer=block)
                if not self._deliver(frame):
                    # The consumer stopped taking frames (a long UI stall, or a
                    # paused session): stop decoding rather than racing ahead and
                    # throwing away the frames the clock has not reached yet.
                    return item, False
                self.stats.frames += 1
                if self.newest_pts is None or frame.pts_ticks > self.newest_pts:
                    self.newest_pts = frame.pts_ticks
                item += 1
                source = item % spec.source_frames if spec.source_frames else item
                if spec.source_frames and not spec.loop and item >= spec.source_frames:
                    return item, False
        except (OSError, ValueError) as error:
            self.stats.last_error = str(error)
            return item, False
        finally:
            frames.close()
            if process is not None:
                self._kill(process)
            if reader is not None and reader.is_alive():
                reader.join(timeout=2.0)
            self._process = None

    def _deliver(self, frame, wait=None):
        """Hand a frame to the consumer, waiting for room instead of overwriting.

        Blocking is the pacing mechanism, not a limitation: the queue depth *is*
        the decode lead, so a full queue stops the child from decoding seconds of
        video that the clock has not reached.  Dropping the oldest frame instead
        would be actively wrong - the frames discarded would be the ones the clock
        is about to need, so the picture would freeze until the clock caught up
        with whatever survived, then jump.

        Returns False when the consumer has stopped consuming, which is the signal
        to wind the decode run down.
        """
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            if self._stopped():
                return False
            try:
                if self.queue.put(frame, timeout=0.25):
                    return True
            except QueueClosed:
                return False
            if deadline is not None and time.monotonic() >= deadline:
                return False

    def _stopped(self):
        with self._condition:
            return not self._running

    def _read_frames(self, process, size, frames):
        """Pump the child's stdout into ``frames``; closing it signals the end."""
        try:
            while True:
                block = _read_exact(process.stdout, size)
                if block is None:
                    return
                while True:
                    try:
                        if frames.put(block, timeout=_PUT_SLICE):
                            break
                    except QueueClosed:
                        return
        except (OSError, ValueError):
            return
        finally:
            # Closing (not a sentinel) is what ends the consumer's wait, because a
            # full blocking queue would silently swallow a sentinel.
            frames.close()

    def _kill(self, process=None):
        target = process or self._process
        if target is None or target.poll() is not None:
            return False
        try:
            target.kill()
            target.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
        self.stats.killed += 1
        return True

    def _kill_child(self):
        return self._kill(self._process)
