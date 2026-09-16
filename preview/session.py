"""One continuous preview session: clock, decode, mix, and a seek that is honest.

The shape of a session
----------------------
``open`` resolves the media, starts exactly one decoder thread and (while
playing) exactly one mixer thread; a caller then drives it two ways: commands
(:meth:`play`, :meth:`pause`, :meth:`seek`, :meth:`close`) and *pulling*
(:meth:`snapshot`) from the UI timer.  Nothing paints from a worker thread, and
no worker thread ever touches a widget.

What "continuous" is bought with
--------------------------------
* **Editing does not wait for a re-encode.**  A seek is a new generation: the
  decoder restarts at the new offset and the mixer re-anchors, both in bounded
  time.  Nothing is rendered for the whole lesson up front.
* **No old sound.**  :meth:`seek` flushes the device and bumps the generation
  while holding the same lock the mixer holds when it writes, so PCM rendered for
  the previous position is either flushed away or rejected - there is no window in
  which it can be heard.  A test asserts that by generation, not by ear.
* **Bounded everywhere.**  Frames: a small drop-oldest queue.  PCM: the device's
  own free space is the back-pressure.  Proxies: a byte budget on disk.  Readers:
  a few most-recently-used handles, so a 50-word lesson does not hold 150 files
  open at once.
* **Releasable.**  :meth:`close` kills the child, closes the device, joins both
  threads with a timeout, and reports any that did not end.  It is safe to call
  twice, and a session that was never opened still closes.
"""
import threading
import time
from dataclasses import dataclass, field, replace

from word_video.domain.timebase import TICKS_PER_SECOND
from word_video.media.audio import SOURCE_RATE

from .audio import WindowMixer, spans_from_plan
from .clock import (AudioClock, NullAudioOutput, PlaybackState, frames_for_ticks)
from .queues import BLOCK, BoundedQueue
from .segments import (DEFAULT_SEGMENT_SECONDS, FIRST_SEGMENT_SECONDS, PREFETCH_AHEAD,
                       SegmentError, SegmentPreparer, build_segment_plan, source_frames)
from .sources import (DEFAULT_PROXY_BUDGET, PreviewSource, prune_proxies,
                      resolve_preview_source)
from .video import DecodeSpec, FfmpegFrameDecoder

__all__ = ['Presentation', 'PreviewSession', 'canvas_size_for', 'picture_item',
           'source_frames']

#: Picture frames held ready to present.  Four 720p frames is ~15 MB, and a queue
#: deeper than the pipeline's real jitter would only add latency to a seek.
DEFAULT_FRAME_QUEUE = 4
#: PCM rendered per mixer pass.  20 ms keeps a seek responsive without making the
#: mixer wake thousands of times a second.
DEFAULT_AUDIO_WINDOW_MS = 20
#: Preview canvas height.  720p is the R1 *suggestion* for the preview tier and is
#: not a change to any delivered resolution: the export keeps the plan's own size.
DEFAULT_CANVAS_HEIGHT = 720


def canvas_size_for(plan, height=DEFAULT_CANVAS_HEIGHT):
    """The preview canvas, same aspect as the plan, at most ``height`` tall."""
    if plan.height <= 0 or plan.width <= 0:
        raise ValueError('plan has no canvas')
    height = min(int(height), int(plan.height))
    width = int(round(plan.width * height / float(plan.height)))
    return max(2, width - width % 2), max(2, height - height % 2)


def picture_item(plan):
    """The one video item that carries media (the background), or ``None``.

    Text layers are drawn from the layout, not decoded, so a plan with no
    background legitimately has no picture source: the preview then shows text on
    a plain canvas instead of refusing to open.
    """
    for item in plan.video:
        if item.source is not None:
            return item
    return None


@dataclass
class Presentation:
    """What the canvas should draw right now."""

    generation: int
    position_ticks: int
    frame: object = None
    placements: tuple = ()
    state: str = PlaybackState.IDLE.value
    stale_dropped: int = 0
    skipped_late: int = 0
    #: Non-empty while the picture for the current position is being produced: the
    #: canvas draws it, so a seek into an unprepared segment is a message rather than
    #: a black frame (H0's rule for the segmented proxy).
    preparing: str = ''
    #: Which proxy segment is being decoded, when the picture is segmented.
    segment: int | None = None

    @property
    def position_seconds(self):
        return self.position_ticks / float(TICKS_PER_SECOND)

    def to_dict(self):
        return {'generation': self.generation, 'position_ticks': self.position_ticks,
                'position_seconds': round(self.position_seconds, 4),
                'frame_index': getattr(self.frame, 'index', None),
                'placements': len(self.placements), 'state': self.state,
                'stale_dropped': self.stale_dropped, 'skipped_late': self.skipped_late,
                'preparing': self.preparing, 'segment': self.segment}


class PreviewSession:
    """A single continuous preview over one render plan.

    One session owns one decoder child plus at most one mixer thread.  The module
    docstring of :mod:`preview` explains why that count matters on the target
    machine: at most one preview session may run at a time.
    """

    def __init__(self, plan, assets, *, display=None, output=None, temp_root=None,
                 canvas_height=DEFAULT_CANVAS_HEIGHT, proxy=True,
                 proxy_budget=DEFAULT_PROXY_BUDGET, frame_queue=DEFAULT_FRAME_QUEUE,
                 audio_window_ms=DEFAULT_AUDIO_WINDOW_MS, stall_timeout=5.0,
                 max_recoveries=1, ffmpeg=None,
                 segment_seconds=DEFAULT_SEGMENT_SECONDS,
                 first_segment_seconds=FIRST_SEGMENT_SECONDS,
                 prefetch_ahead=PREFETCH_AHEAD):
        self.plan = plan
        self.assets = dict(assets)
        self.display = display
        self.canvas_width, self.canvas_height = canvas_size_for(plan, canvas_height)
        self.rate = int(plan.sample_rate)
        if self.rate != SOURCE_RATE:
            raise ValueError('preview mixes %d Hz only; the plan asks for %d'
                             % (SOURCE_RATE, self.rate))
        self.temp_root = temp_root
        self.proxy = bool(proxy)
        self.proxy_budget = int(proxy_budget)
        self.stall_timeout = float(stall_timeout)
        self.max_recoveries = int(max_recoveries)
        self.ffmpeg = ffmpeg
        #: Segment grid for the picture proxy; see :mod:`preview.segments`.  Parameters
        #: rather than constants so the acceptance measurement can try a different
        #: trade-off without editing the module the product ships.
        self.segment_seconds = float(segment_seconds)
        self.first_segment_seconds = float(first_segment_seconds)
        self.prefetch_ahead = int(prefetch_ahead)
        self.output = output if output is not None else NullAudioOutput(rate=self.rate)
        self.clock = AudioClock(self.rate, plan.total_ticks)

        self.frames = BoundedQueue(maxlen=int(frame_queue), overflow=BLOCK,
                                   name='preview-frames')
        self.audio_window = max(1, int(self.rate * audio_window_ms / 1000.0))
        self.mixer = None
        self.sources = {}
        self.decode_spec = None
        self.decoder = None

        self.stale_dropped = 0
        self.skipped_late = 0
        self.audio_blocks_dropped = 0
        self.mixer_error = ''
        #: Segmented picture proxy (see :mod:`preview.segments`): ``None`` when the
        #: picture uses one proxy file, which is the case for a source that does not
        #: need a proxy or needs only one segment's worth of it.
        self.segment_plan = None
        self.preparer = None
        self.segment_index = None
        self.preparing_index = None
        self.preparing = ''
        self.segment_error = ''
        #: Timeline tick of the picture item's own frame 0.  Separate from
        #: ``decode_spec.start_ticks`` because a segment's spec is shifted to *its*
        #: file's frame 0, and "which frame of the item is this position on" must not
        #: move with the segment that happens to be loaded.
        self.picture_start_ticks = 0
        self._seek_lock = threading.Lock()
        self._mix_thread = None
        self._mix_stop = threading.Event()
        self._write_frame = 0
        self._frame = None
        self._pending = None
        self._opened = False
        self._closed = False
        self.open_seconds = None
        self.first_frame_seconds = None
        self._started_at = None

    # -- opening ---------------------------------------------------------
    def open(self):
        """Resolve media, prune the proxy cache, start the decoder."""
        if self._opened:
            return self
        started = time.monotonic()
        item = picture_item(self.plan)
        self.sources = self._resolve_sources(item)
        self.mixer = WindowMixer(spans_from_plan(self.plan, self.assets, self.rate),
                                 rate=self.rate)
        if item is not None:
            spec = self._decode_spec(item)
            if spec is not None:
                self.decode_spec = spec
                self.decoder = FfmpegFrameDecoder(
                    self.frames, ffmpeg=self.ffmpeg, stall_timeout=self.stall_timeout,
                    max_recoveries=self.max_recoveries)
                self.decoder.start()
        self._opened = True
        self.open_seconds = time.monotonic() - started
        return self

    def _resolve_sources(self, item=None):
        """The file each asset is read from: a proxy only where a picture is decoded.

        Every asset used to be run through :func:`sources.resolve_preview_source`,
        which asks ffprobe for its height first.  For the 150 speech files of a
        50-word lesson the answer is always the same - not a picture, use it as it is -
        and measured 2026-09-16 that pass cost 24 s of *every* session open, including
        the rebuild after each committed edit.  The decoder only ever opens the picture
        item (:meth:`_decode_spec`), and the mixer reads ``self.assets`` (the caller's
        own paths), so the speech assets are recorded as-is and never probed here.

        ``item`` is the picture item; when there is none, nothing is probed at all.
        """
        resolved = {}
        if self.temp_root is not None:
            self.proxy_pruning = prune_proxies(self.temp_root, self.proxy_budget)
        # How much picture the lesson actually plays, so a long source is proxied
        # over the window that is used instead of in full (see sources.windowed_copy).
        needed = None
        if item is not None and item.duration_ticks > 0:
            needed = item.duration_ticks / float(TICKS_PER_SECOND)
        picture_id = item.source.asset_id if item is not None and item.source else None
        for asset_id, path in self.assets.items():
            if asset_id != picture_id:
                # Speech (or anything else the plan never decodes): the caller's own
                # file, unchanged, and no subprocess spent finding that out.
                resolved[asset_id] = PreviewSource(str(path), 0, False, str(path))
                continue
            entry = self._picture_source(path, item)
            if entry is None:
                entry = resolve_preview_source(
                    path, self.canvas_height,
                    cache_dir=self.temp_root if self.proxy else None,
                    budget=self.proxy_budget, proxy=self.proxy, needed_seconds=needed)
            resolved[asset_id] = entry
        return resolved

    def _picture_source(self, path, item):
        """The picture source: a segmented proxy when one applies, else ``None``.

        ``None`` means "ask :func:`sources.resolve_preview_source` as before".  The
        first segment is materialised **here**, inside ``open``, because it is what the
        first frame needs; everything after it is the background thread's business.  A
        segment that cannot be encoded falls back to the single-file proxy and the
        reason is kept in :attr:`segment_error` rather than swallowed.
        """
        if not self.proxy or self.temp_root is None or item is None or item.source is None:
            return None
        coverage = self._coverage_frames(item)
        try:
            plan = build_segment_plan(path, self.temp_root, self.canvas_height,
                                      self.plan.fps_num, self.plan.fps_den, coverage,
                                      source_start=item.source.source_start,
                                      speed=float(item.source.speed),
                                      segment_seconds=self.segment_seconds,
                                      first_segment_seconds=self.first_segment_seconds)
        except (OSError, ValueError) as error:
            self.segment_error = '%s: %s' % (type(error).__name__, error)
            return None
        if plan is None:
            return None
        try:
            first = plan.ensure(0)
        except SegmentError as error:
            self.segment_error = str(error)
            return None
        self.segment_plan = plan
        self.segment_index = 0
        self.preparer = SegmentPreparer(plan, ahead=self.prefetch_ahead)
        self.preparer.start()
        self.preparer.focus(0)
        return PreviewSource(str(first.path), plan.height, True, str(path))

    def _item_frame_ticks(self):
        return self.plan.fps_den * TICKS_PER_SECOND // self.plan.fps_num

    def _coverage_frames(self, item):
        """How many item frames playback can actually reach (never past the item)."""
        frame_ticks = self._item_frame_ticks()
        end = int(min(self.plan.total_ticks, item.end_ticks))
        span = max(0, end - int(item.start_ticks))
        return max(1, -(-span // frame_ticks))

    def _item_frame_at(self, ticks):
        """Which frame of the picture item a timeline instant lands on."""
        if self.decode_spec is None:
            return 0
        offset = max(0, int(ticks) - int(self.picture_start_ticks))
        return offset // self._item_frame_ticks()

    def _decode_spec(self, item):
        """The decoder's spec for the picture item, segmented when that applies.

        The loop period and the "does the source cover the item" decision are asked of
        the **original** file, not of a proxy segment: a segment is a few seconds long,
        so measuring it would say every picture loops.
        """
        entry = self.sources.get(item.source.asset_id)
        if entry is None:
            return None
        from word_video.media.streams import video_stream
        original = entry.original or entry.path
        try:
            video_stream(original)
        except (OSError, ValueError, KeyError):
            return None
        frames = source_frames(original, self.plan.fps_num, self.plan.fps_den)
        item_frames = 0
        if item.duration_ticks > 0:
            tick = self._item_frame_ticks()
            item_frames = (item.duration_ticks + tick - 1) // tick
        self.picture_start_ticks = int(item.start_ticks)
        spec = DecodeSpec(
            path=entry.path, width=self.canvas_width, height=self.canvas_height,
            fps_num=self.plan.fps_num, fps_den=self.plan.fps_den,
            start_ticks=item.start_ticks, end_ticks=item.end_ticks,
            source_frames=frames, loop=bool(frames) and frames < item_frames)
        if self.segment_plan is not None:
            # A segment is not a loop period and has no earlier frame to wrap to: the
            # run ends when the file does.  ``start_ticks`` moves to the file's own
            # frame 0, because the decoder uses one number both to seek inside the
            # file and to stamp what it reads - the two must describe that file.
            first = self.segment_plan.segment(0)
            spec = replace(spec, path=str(first.path), source_frames=0, loop=False,
                           start_ticks=item.start_ticks
                           + first.file_base_frame * self._item_frame_ticks())
        return spec

    # -- commands --------------------------------------------------------
    def play(self, at_ticks=None):
        """Start (or resume) playing, optionally from ``at_ticks``."""
        self._require_open()
        if at_ticks is not None:
            self._restart(at_ticks, playing=True)
        elif self.clock.generation == 0:
            self._restart(0, playing=True)
        else:
            # Resume == seek to where we stopped.  Re-anchoring is simpler and
            # more predictable than depending on what a stopped device does to
            # its played-frame counter.
            self._restart(self.position_ticks(), playing=True)
        self._started_at = time.monotonic()
        return self

    def seek(self, ticks):
        """Jump to ``ticks``; everything from the old position is discarded.

        While playing, playback continues from the new position; while paused the
        new position is still decoded and shown, which is what "定位后能出画"
        means for an editor and costs one decode run.
        """
        self._require_open()
        playing = self.clock.state == PlaybackState.PLAYING
        self._restart(ticks, playing=playing)
        return self.clock.generation

    def _restart(self, ticks, playing):
        """The one place a generation changes: flush, re-anchor, re-request.

        The order is load-bearing.  A flush *discards* what the device has not
        played yet and re-zeroes its counter, and on Qt it also stops the device -
        so starting first and flushing second leaves a stopped sink that silently
        accepts nothing, and the preview goes quiet from the first seek onwards.
        Flush, then start, then anchor from the freshly zeroed counter.
        """
        ticks = max(0, min(int(ticks), self.plan.total_ticks))
        with self._seek_lock:
            self.output.flush()
            self.output.start()
            generation = self.clock.reanchor(ticks, self.output.frames_played())
            self._write_frame = frames_for_ticks(ticks, self.rate)
            self.frames.clear()
            self._pending = None
            self._frame = None
        self._request_decode(generation, ticks)
        if playing:
            self.clock.set_state(PlaybackState.PLAYING)
            self._start_mixer()
        return generation

    def _request_decode(self, generation, at_ticks):
        """Ask the decoder for the item starting at the tick the user landed on.

        ``offset_frames`` is a **video frame index**, so it comes from the frame
        duration - not from the audio rate.  Converting through the sample rate
        asks for a frame tens of thousands too far in, ffmpeg finds nothing past
        the end of the source, and the picture simply never appears after a seek
        while everything else looks healthy.

        With a segmented picture this picks the segment the tick lands in; if that
        segment is not encoded yet the session says so (:attr:`preparing`) and the
        background thread promotes it, instead of waiting inside the UI thread or
        showing a black frame.
        """
        spec = self.decode_spec
        if self.decoder is None or spec is None:
            return
        if self.segment_plan is not None:
            index = self.segment_plan.index_of(self._item_frame_at(at_ticks))
            if self.segment_plan.ready(index):
                self._use_segment(index, generation, at_ticks)
            else:
                self._wait_for_segment(index)
            return
        offset_ticks = max(0, at_ticks - spec.start_ticks)
        offset_frames = offset_ticks // spec.frame_ticks
        if spec.source_frames:
            offset_frames %= spec.source_frames
        self.decoder.request(generation, replace(spec, offset_frames=offset_frames))

    # -- segmented picture ------------------------------------------------
    def _use_segment(self, index, generation, at_ticks):
        """Point the decoder at one segment, keeping the timeline's own stamps.

        The decoder carries a single frame index and uses it twice: to seek inside the
        file and to compute the presentation timestamp of what it reads.  A segment
        file starts at an item frame of its own (``file_base_frame``), so the spec's
        ``start_ticks`` moves with the segment - which keeps both uses truthful, and
        keeps a frame's timestamp the timeline's own across a boundary.  Frames
        already queued from the previous segment are therefore still correct and are
        kept: crossing a boundary is a decoder restart, not a seek, and the sound
        never notices it.
        """
        segment = self.segment_plan.segment(index)
        frame_ticks = self._item_frame_ticks()
        start_ticks = self.picture_start_ticks + segment.file_base_frame * frame_ticks
        offset = max(0, (int(at_ticks) - start_ticks) // frame_ticks)
        self.decode_spec = replace(self.decode_spec, path=str(segment.path),
                                   source_frames=0, loop=False,
                                   start_ticks=start_ticks, offset_frames=offset)
        self.segment_index = index
        self.preparing_index = None
        self.preparing = ''
        if self.preparer is not None:
            self.preparer.mark_ready(index)
            self.preparer.focus(index)
        self.decoder.request(generation, self.decode_spec)

    def _wait_for_segment(self, index):
        """Make an unprepared segment visible and ask for it at the front of the queue."""
        segment = self.segment_plan.segment(index)
        rate = self.plan.fps_num / float(self.plan.fps_den)
        self.preparing_index = index
        self.preparing = ('正在准备第 %d/%d 段画面（%.1f–%.1f 秒）…'
                          % (index + 1, len(self.segment_plan.segments),
                             segment.first_frame / rate,
                             (segment.last_frame + 1) / rate))
        if self.preparer is not None:
            self.preparer.demand(index)

    def _follow_segments(self, position, generation):
        """Keep the decoder on the segment the playhead is in, and stay visible.

        Called from :meth:`snapshot` (the UI's own tick), so the state a member sees
        and the state the decoder is in cannot drift apart: a segment that becomes
        ready while the playhead waits in it is picked up on the next paint.
        """
        index = self.segment_plan.index_of(self._item_frame_at(position))
        if self.preparing_index is not None:
            if self.preparing_index != index:
                # The member moved on; the old demand is no longer what we wait for.
                self.preparing_index = None
                self.preparing = ''
            elif self.segment_plan.ready(self.preparing_index):
                self._use_segment(self.preparing_index, generation, position)
                return
            else:
                if self.preparer is not None:
                    self.preparer.focus(index)
                return
        if self.segment_index != index:
            if self.segment_plan.ready(index):
                self._use_segment(index, generation, position)
            else:
                self._wait_for_segment(index)
        elif self.preparer is not None:
            self.preparer.focus(index)

    def pause(self):
        if self._closed:
            return self
        self.clock.set_state(PlaybackState.PAUSED)
        with self._seek_lock:
            self.output.stop()
        self._stop_mixer()
        return self

    def position_ticks(self):
        return self.clock.position_ticks(self.output.frames_played())

    # -- mixer thread ----------------------------------------------------
    def _start_mixer(self):
        if self.mixer is None:
            return
        if self._mix_thread is not None and self._mix_thread.is_alive():
            return
        self._mix_stop.clear()
        self._mix_thread = threading.Thread(target=self._mix_loop, name='preview-mix',
                                            daemon=True)
        self._mix_thread.start()

    def _stop_mixer(self, timeout=3.0):
        thread, self._mix_thread = self._mix_thread, None
        if thread is None:
            return True
        self._mix_stop.set()
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _mix_loop(self):
        """Keep the device fed; drop any block the generation has left behind."""
        while not self._mix_stop.is_set():
            if self.clock.state != PlaybackState.PLAYING:
                time.sleep(0.005)
                continue
            if self.position_ticks() >= self.plan.total_ticks:
                self.clock.set_state(PlaybackState.ENDED)
                continue
            if self.output.free_frames() < self.audio_window:
                time.sleep(0.002)
                continue
            generation = self.clock.generation
            start = self._write_frame
            end_frame = frames_for_ticks(self.plan.total_ticks, self.rate)
            if start >= end_frame:
                time.sleep(0.002)
                continue
            frames = min(self.audio_window, end_frame - start)
            if self.mixer.spans:
                try:
                    pcm = self.mixer.render_window(start, frames)
                except (OSError, ValueError) as error:
                    self.mixer_error = str(error)
                    self.audio_blocks_dropped += 1
                    time.sleep(0.05)
                    continue
            else:
                pcm = bytes(2 * frames)
            with self._seek_lock:
                if self.clock.generation != generation \
                        or self.clock.state != PlaybackState.PLAYING:
                    # Rendered for a position the user already left: never written.
                    self.audio_blocks_dropped += 1
                    continue
                accepted = self.output.write(pcm)
                if accepted >= 2 * frames:
                    self._write_frame = start + frames
                else:
                    self.audio_blocks_dropped += 1
                    time.sleep(0.002)

    # -- presentation ----------------------------------------------------
    def snapshot(self):
        """The picture and text due now; call this from the UI timer."""
        if self._closed:
            return Presentation(generation=self.clock.generation, position_ticks=0,
                                state=PlaybackState.RELEASED.value)
        position = self.position_ticks()
        generation = self.clock.generation
        if self.clock.state == PlaybackState.PLAYING \
                and position >= self.plan.total_ticks:
            self.clock.set_state(PlaybackState.ENDED)
        if self.segment_plan is not None and self.decoder is not None:
            self._follow_segments(position, generation)
        self._advance(position, generation)
        placements = ()
        if self.display is not None:
            placements = tuple(self.display.placements_at(position))
        return Presentation(generation=generation, position_ticks=position,
                            frame=self._frame, placements=placements,
                            state=self.clock.state.value,
                            stale_dropped=self.stale_dropped,
                            skipped_late=self.skipped_late,
                            preparing=self.preparing, segment=self.segment_index)

    def _advance(self, position, generation):
        """Show the newest frame whose timestamp has arrived; drop stale ones.

        Frames are taken one at a time in timestamp order, so nothing is skipped
        while the preview keeps up.  When it cannot - the UI stalled, the disk
        hiccuped - the due frames are consumed as fast as they arrive and only the
        last is shown; those are counted in ``skipped_late`` rather than passed
        off as a smooth playthrough.
        """
        while True:
            if self._pending is None:
                self._pending = self.frames.get(timeout=0)
                if self._pending is None:
                    break
            if self._pending.generation != generation:
                self.stale_dropped += 1
                self._pending = None
                continue
            if self._pending.pts_ticks <= position:
                if self._frame is not None:
                    self.skipped_late += 1
                self._frame = self._pending
                self._pending = None
                continue
            break
        if self._frame is not None and self._frame.generation != generation:
            self._frame = None
        if self.first_frame_seconds is None and self._frame is not None \
                and self._started_at is not None:
            self.first_frame_seconds = time.monotonic() - self._started_at

    def wait_for_frame(self, timeout=10.0, interval=0.005):
        """Pump :meth:`snapshot` until a picture is due; returns seconds waited.

        The measurement entry point uses this so "time to first frame" is the
        time the *preview* needed, not the time a test framework needed to set
        itself up.
        """
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            presentation = self.snapshot()
            if presentation.frame is not None:
                return time.monotonic() - started
            time.sleep(interval)
        return None

    # -- lifecycle -------------------------------------------------------
    def close(self, timeout=5.0):
        """Release every resource this session owns, in finite time.

        Returns a report, not just ``None``: whether the decoder child really
        died, whether each thread really ended, and whether the device was closed.
        A caller that only got ``None`` could not tell a clean teardown from a
        leak, and the resource acceptance is exactly about telling.
        """
        if self._closed:
            return self.report()
        self._closed = True
        self.clock.set_state(PlaybackState.RELEASED)
        # The segment thread first: it is the only worker that may be mid-ffmpeg, and
        # a segment half-written must not be mistaken for a ready one later on (it is
        # written to a temporary name and only then renamed, so this is belt and
        # braces - the join is what makes "closed" mean "nothing of ours is running").
        preparer_stopped = True
        if self.preparer is not None:
            preparer_stopped = self.preparer.stop(timeout=timeout)
        mixer_stopped = self._stop_mixer(timeout=timeout)
        decoder_stopped = True
        if self.decoder is not None:
            decoder_stopped = self.decoder.stop(timeout=timeout)
        children = 0 if self.decoder is None else self.decoder.alive_children()
        threads = 0 if self.decoder is None else self.decoder.alive_threads()
        if not mixer_stopped:
            threads += 1
        self.frames.close()
        self.frames.clear()
        if self.mixer is not None:
            self.mixer.close()
        self.output.close()
        with self._seek_lock:
            self._frame = None
            self._pending = None
        report = self.report()
        report.update({'mixer_stopped': mixer_stopped, 'decoder_stopped': decoder_stopped,
                       'children_left': children, 'threads_left': threads,
                       'preparer_stopped': preparer_stopped})
        return report

    def report(self):
        """Diagnostics; safe to call at any time, including after close."""
        return {
            'state': self.clock.state.value,
            'generation': self.clock.generation,
            'position_ticks': self.clock.position_ticks(self.output.frames_played()),
            'canvas': '%dx%d' % (self.canvas_width, self.canvas_height),
            'open_seconds': self.open_seconds,
            'first_frame_seconds': self.first_frame_seconds,
            'preparing': self.preparing,
            'segment': self.segment_index,
            'segment_error': self.segment_error,
            'segments': None if self.segment_plan is None else self.segment_plan.stats(),
            'preparer': None if self.preparer is None else self.preparer.stats(),
            'audio_window_frames': self.audio_window,
            'stale_dropped': self.stale_dropped,
            'skipped_late': self.skipped_late,
            'audio_blocks_dropped': self.audio_blocks_dropped,
            'mixer_error': self.mixer_error,
            'truncated_audio_frames': None if self.mixer is None
                                      else self.mixer.truncated_frames,
            'open_readers': None if self.mixer is None else self.mixer.open_readers(),
            'frame_queue': self.frames.stats(),
            'sources': {key: value.to_dict() for key, value in self.sources.items()},
            'decode': None if self.decoder is None else self.decoder.stats.to_dict(),
            'decode_spec': None if self.decode_spec is None
                           else self.decode_spec.to_dict(),
        }

    def _require_open(self):
        if self._closed:
            raise RuntimeError('session is closed')
        self.open()
