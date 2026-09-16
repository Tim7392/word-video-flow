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
from .sources import DEFAULT_PROXY_BUDGET, prune_proxies, resolve_preview_source
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


def source_frames(path, fps_num, fps_den):
    """How many frames a file holds, for the loop period.  ``0`` when unknown."""
    from word_video.media.core import duration
    try:
        seconds = duration(path)
    except (OSError, ValueError, RuntimeError):
        return 0
    return int(round(seconds * fps_num / float(fps_den)))


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

    @property
    def position_seconds(self):
        return self.position_ticks / float(TICKS_PER_SECOND)

    def to_dict(self):
        return {'generation': self.generation, 'position_ticks': self.position_ticks,
                'position_seconds': round(self.position_seconds, 4),
                'frame_index': getattr(self.frame, 'index', None),
                'placements': len(self.placements), 'state': self.state,
                'stale_dropped': self.stale_dropped, 'skipped_late': self.skipped_late}


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
                 max_recoveries=1, ffmpeg=None):
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
        self.sources = self._resolve_sources()
        self.mixer = WindowMixer(spans_from_plan(self.plan, self.assets, self.rate),
                                 rate=self.rate)
        item = picture_item(self.plan)
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

    def _resolve_sources(self):
        resolved = {}
        if self.temp_root is not None:
            self.proxy_pruning = prune_proxies(self.temp_root, self.proxy_budget)
        # How much picture the lesson actually plays, so a long source is proxied
        # over the window that is used instead of in full (see sources.windowed_copy).
        needed = None
        item = picture_item(self.plan)
        if item is not None and item.duration_ticks > 0:
            needed = item.duration_ticks / float(TICKS_PER_SECOND)
        for asset_id, path in self.assets.items():
            resolved[asset_id] = resolve_preview_source(
                path, self.canvas_height,
                cache_dir=self.temp_root if self.proxy else None,
                budget=self.proxy_budget, proxy=self.proxy, needed_seconds=needed)
        return resolved

    def _decode_spec(self, item):
        entry = self.sources.get(item.source.asset_id)
        if entry is None:
            return None
        from word_video.media.streams import video_stream
        try:
            video_stream(entry.path)
        except (OSError, ValueError, KeyError):
            return None
        frames = source_frames(entry.path, self.plan.fps_num, self.plan.fps_den)
        item_frames = 0
        if item.duration_ticks > 0:
            tick = self.plan.fps_den * TICKS_PER_SECOND // self.plan.fps_num
            item_frames = (item.duration_ticks + tick - 1) // tick
        return DecodeSpec(
            path=entry.path, width=self.canvas_width, height=self.canvas_height,
            fps_num=self.plan.fps_num, fps_den=self.plan.fps_den,
            start_ticks=item.start_ticks, end_ticks=item.end_ticks,
            source_frames=frames, loop=bool(frames) and frames < item_frames)

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
        """
        spec = self.decode_spec
        if self.decoder is None or spec is None:
            return
        offset_ticks = max(0, at_ticks - spec.start_ticks)
        offset_frames = offset_ticks // spec.frame_ticks
        if spec.source_frames:
            offset_frames %= spec.source_frames
        self.decoder.request(generation, replace(spec, offset_frames=offset_frames))

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
        self._advance(position, generation)
        placements = ()
        if self.display is not None:
            placements = tuple(self.display.placements_at(position))
        return Presentation(generation=generation, position_ticks=position,
                            frame=self._frame, placements=placements,
                            state=self.clock.state.value,
                            stale_dropped=self.stale_dropped,
                            skipped_late=self.skipped_late)

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
                       'children_left': children, 'threads_left': threads})
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
