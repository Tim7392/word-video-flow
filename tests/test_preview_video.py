"""Decoding: real frames, a real loop, and a real kill when the child wedges.

The hang test is the important one, and it does not stub the decoder.  It hands
:class:`FfmpegFrameDecoder` a command that blocks forever instead of an ffmpeg
invocation, so the code under test is the shipping watchdog, the shipping kill
and the shipping join-with-timeout.  If the recovery boundary were fake, this test
would hang rather than pass.
"""
import struct
import subprocess
import sys
import threading
import time

import pytest

from preview.queues import DROP_OLDEST, BoundedQueue
from preview.sources import PreviewSource, prune_proxies, resolve_preview_source
from preview.video import (DecodeSpec, FfmpegFrameDecoder, PIXEL_BYTES, VideoFrame,
                           _read_exact)
from test_preview_support import scratch, write_test_video
from word_video.domain.timebase import TICKS_PER_SECOND

FPS = 30


def spec_for(path, seconds=1.0, start_ticks=0, offset_frames=0, loop=False,
             source_frames=FPS, width=64, height=48):
    return DecodeSpec(path=str(path), width=width, height=height, fps_num=FPS,
                      fps_den=1, start_ticks=start_ticks,
                      end_ticks=start_ticks + int(seconds * TICKS_PER_SECOND),
                      source_frames=source_frames, loop=loop,
                      offset_frames=offset_frames)


def collect(decoder, queue, count, timeout=30.0):
    frames = []
    deadline = time.monotonic() + timeout
    while len(frames) < count and time.monotonic() < deadline:
        item = queue.get(timeout=0.2)
        if item is not None:
            frames.append(item)
    return frames


def test_real_frames_arrive_with_the_declared_geometry_and_monotonic_timestamps():
    with scratch() as folder:
        video = write_test_video(folder / 'clip.mp4', seconds=1.0, fps=FPS)
        queue = BoundedQueue(maxlen=32, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(queue, stall_timeout=20.0)
        decoder.start()
        try:
            decoder.request(7, spec_for(video, seconds=0.5, source_frames=FPS))
            frames = collect(decoder, queue, 15)
            assert len(frames) >= 12, 'expected around 15 frames of a 0.5 s clip'
            assert all(frame.generation == 7 for frame in frames)
            assert all(len(frame.buffer) == 64 * 48 * PIXEL_BYTES for frame in frames)
            stamps = [frame.pts_ticks for frame in frames]
            assert stamps == sorted(stamps)
            assert stamps[0] == 0
            step = TICKS_PER_SECOND // FPS
            assert stamps[1] - stamps[0] == step
        finally:
            assert decoder.stop(timeout=10.0) is True
        assert decoder.alive_threads() == 0
        assert decoder.alive_children() == 0


def test_a_short_background_restarts_in_the_same_generation_without_going_backwards():
    """This is the preview's whole-file loop, and its timestamps must stay
    monotonic or the presentation step would reject the second pass as late."""
    with scratch() as folder:
        video = write_test_video(folder / 'clip.mp4', seconds=0.5, fps=FPS)
        queue = BoundedQueue(maxlen=64, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(queue, stall_timeout=20.0)
        decoder.start()
        try:
            # A 1.2 s item over a 0.5 s source: it must repeat, twice over.
            decoder.request(3, spec_for(video, seconds=1.2, loop=True,
                                        source_frames=15))
            frames = collect(decoder, queue, 32)
            assert len(frames) >= 30
            stamps = [frame.pts_ticks for frame in frames]
            assert stamps == sorted(stamps), 'the loop went backwards in time'
            assert decoder.stats.loops >= 1
        finally:
            decoder.stop(timeout=10.0)


def test_a_seek_replaces_the_running_decode_instead_of_queueing_another():
    """Twenty clicks in a second must leave one child running, not twenty."""
    with scratch() as folder:
        video = write_test_video(folder / 'clip.mp4', seconds=1.0, fps=FPS)
        queue = BoundedQueue(maxlen=64, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(queue, stall_timeout=20.0)
        decoder.start()
        try:
            for generation in range(1, 6):
                decoder.request(generation, spec_for(video, seconds=0.5,
                                                     source_frames=FPS))
                time.sleep(0.02)
            time.sleep(0.6)
            assert decoder.alive_children() <= 1
            # Whatever arrived belongs to the newest generation or an older one
            # that the queue will drop; never to a generation that never existed.
            seen = set()
            while True:
                item = queue.get(timeout=0)
                if item is None:
                    break
                seen.add(item.generation)
            assert seen <= {1, 2, 3, 4, 5}
        finally:
            decoder.stop(timeout=10.0)


def test_a_wedged_decoder_is_killed_and_its_thread_ends():
    """The recoverable boundary, exercised for real.

    ``sys.executable -c "time.sleep(600)"`` accepts the ffmpeg arguments, ignores
    them and never writes a byte, which is exactly the failure shape of a native
    decoder that has stopped making progress.  The watchdog must notice, kill it,
    and let the worker finish - not sit in a join while the UI waits.
    """
    with scratch() as folder:
        queue = BoundedQueue(maxlen=8, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(
            queue, ffmpeg=[sys.executable, '-c', 'import time; time.sleep(600)'],
            stall_timeout=0.4, max_recoveries=0)
        decoder.start()
        try:
            decoder.request(1, spec_for(folder / 'never-used.mp4', seconds=1.0))
            deadline = time.monotonic() + 10.0
            while decoder.stats.stalls == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert decoder.stats.stalls == 1, 'the stall was never detected'
            while decoder.stats.failures == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert decoder.stats.failures == 1
            assert 'no response' in decoder.stats.failure_reason \
                or '无响应' in decoder.stats.failure_reason
            assert decoder.stats.killed >= 1, 'the wedged child was not killed'
            assert decoder.stats.frames == 0
            assert queue.get(timeout=0) is None
        finally:
            started = time.monotonic()
            assert decoder.stop(timeout=10.0) is True, 'the worker did not end'
            assert time.monotonic() - started < 10.0
        assert decoder.alive_threads() == 0
        assert decoder.alive_children() == 0


def test_a_wedged_decoder_is_retried_once_before_it_is_given_up_on():
    """The policy the UI relies on: transient trouble recovers, permanent trouble
    is reported instead of retried forever."""
    with scratch() as folder:
        queue = BoundedQueue(maxlen=8, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(
            queue, ffmpeg=[sys.executable, '-c', 'import time; time.sleep(600)'],
            stall_timeout=0.3, max_recoveries=2)
        decoder.start()
        try:
            decoder.request(1, spec_for(folder / 'never-used.mp4', seconds=1.0))
            deadline = time.monotonic() + 15.0
            while decoder.stats.failures == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert decoder.stats.stalls == 3, decoder.stats.to_dict()
            assert decoder.stats.recoveries == 2, decoder.stats.to_dict()
            assert decoder.stats.failures == 1
        finally:
            assert decoder.stop(timeout=10.0) is True


def test_stop_ends_the_worker_even_while_the_child_is_wedged():
    """Teardown must not wait on the decoder to become polite."""
    with scratch() as folder:
        queue = BoundedQueue(maxlen=4, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(
            queue, ffmpeg=[sys.executable, '-c', 'import time; time.sleep(600)'],
            stall_timeout=30.0, max_recoveries=0)
        decoder.start()
        decoder.request(1, spec_for(folder / 'never-used.mp4', seconds=1.0))
        time.sleep(0.4)
        started = time.monotonic()
        assert decoder.stop(timeout=10.0) is True
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, 'teardown waited on the stall timeout (%.2fs)' % elapsed
        assert decoder.alive_children() == 0


def test_a_child_that_dies_immediately_does_not_spin_the_worker():
    """A command that exits at once must end the run, not be restarted in a loop."""
    with scratch() as folder:
        queue = BoundedQueue(maxlen=4, overflow=DROP_OLDEST)
        decoder = FfmpegFrameDecoder(queue,
                                     ffmpeg=[sys.executable, '-c', 'raise SystemExit(0)'],
                                     stall_timeout=2.0)
        decoder.start()
        try:
            decoder.request(1, spec_for(folder / 'never-used.mp4', seconds=5.0))
            time.sleep(1.0)
            assert decoder.stats.runs == 1, decoder.stats.to_dict()
            assert decoder.stats.frames == 0
        finally:
            decoder.stop(timeout=10.0)


def test_read_exact_returns_none_at_end_and_never_a_partial_frame():
    class FakeStream:
        def __init__(self, payload, chunk):
            self.payload = payload
            self.chunk = chunk

        def read(self, size):
            take = min(size, self.chunk, len(self.payload))
            block, self.payload = self.payload[:take], self.payload[take:]
            return block

    # Short reads are legal on a pipe and must be stitched, not treated as a frame.
    assert _read_exact(FakeStream(b'abcdef', 2), 6) == b'abcdef'
    assert _read_exact(FakeStream(b'abc', 2), 6) is None
    assert _read_exact(FakeStream(b'', 2), 6) is None


def test_the_decoder_command_keeps_the_renderers_framing():
    """A preview that stretched a source would show text where the export does not
    put it, so the scale filter must be the cover-and-crop one."""
    decoder = FfmpegFrameDecoder(BoundedQueue(maxlen=2), ffmpeg=['ffmpeg'])
    command = decoder.command(spec_for('x.mp4', width=1280, height=720), 0)
    assert 'force_original_aspect_ratio=increase' in ' '.join(command)
    assert 'crop=1280:720' in ' '.join(command)
    assert command[-1] == '-'
    assert 'bgr0' in command


# -- proxies -------------------------------------------------------------
def test_a_tall_source_is_replaced_by_a_proxy_and_the_proxy_is_reused():
    """The product case: a 1080p background on the 720p preview canvas."""
    with scratch() as folder:
        source = write_test_video(folder / 'big.mp4', seconds=0.5, fps=FPS,
                                  size='1920x1080')
        cache = folder / 'cache'
        first = resolve_preview_source(source, 720, cache_dir=cache)
        assert first.is_proxy is True
        assert first.path != str(source)
        assert first.original == str(source)
        # The proxy is the same clip, smaller - never a substitute deliverable -
        # and it uses a *measured* preset rather than a height the preview invented.
        from word_video.media.streams import video_stream
        assert int(video_stream(first.path)['height']) == 720
        assert first.height == 720
        second = resolve_preview_source(source, 720, cache_dir=cache)
        from preview.sources import proxy_cache_dir
        files = sorted(proxy_cache_dir(cache).glob('*.mp4'))
        assert second.path == first.path
        assert len(files) == 1, 'the proxy was re-encoded instead of reused'
        # A second canvas height gets its own proxy rather than reusing this one.
        other = resolve_preview_source(source, 540, cache_dir=cache)
        assert other.path != first.path
        assert other.height == 540


def test_a_canvas_between_two_presets_takes_the_taller_one():
    """A proxy taller than the canvas costs a little more to decode; one shorter
    would have to be enlarged, which is paying more for a softer picture."""
    from preview.sources import preview_proxy_height
    assert preview_proxy_height(180) == 540
    assert preview_proxy_height(540) == 540
    assert preview_proxy_height(541) == 720
    assert preview_proxy_height(720) == 720
    assert preview_proxy_height(1080) == 1080
    assert preview_proxy_height(2160) == 1080


def test_a_source_the_smallest_preset_cannot_shrink_is_not_proxied():
    """Presets only ever shrink, so asking for a 540 proxy of a 480-tall source
    would produce a copy that costs an encode and saves nothing."""
    with scratch() as folder:
        source = write_test_video(folder / 'small.mp4', seconds=0.4, fps=FPS,
                                  size='640x480')
        resolved = resolve_preview_source(source, 240, cache_dir=folder / 'cache')
        assert resolved.is_proxy is False
        assert resolved.path == str(source)


def test_a_source_no_taller_than_the_canvas_is_used_as_it_is():
    """A proxy of a small file would cost an encode and save nothing."""
    with scratch() as folder:
        source = write_test_video(folder / 'small.mp4', seconds=0.4, fps=FPS,
                                  size='160x120')
        resolved = resolve_preview_source(source, 720, cache_dir=folder / 'cache')
        assert resolved.is_proxy is False
        assert resolved.path == str(source)
        assert resolved.height == 120


def test_a_file_with_no_video_stream_falls_back_instead_of_failing():
    """A preview that refused to open because its optimisation failed would be a
    worse editor than one that plays the original."""
    with scratch() as folder:
        not_video = folder / 'words.txt'
        not_video.write_text('hello\n', encoding='utf-8')
        resolved = resolve_preview_source(not_video, 240, cache_dir=folder / 'cache')
        assert resolved.is_proxy is False
        assert resolved.path == str(not_video)


def test_the_proxy_folder_is_pruned_to_a_budget_oldest_first():
    """A cache that only grows is how a preview feature fills a member's disk."""
    with scratch() as folder:
        cache = folder / 'cache'
        cache.mkdir()
        import os
        paths = []
        for index in range(5):
            path = cache / ('p%d.mp4' % index)
            path.write_bytes(b'x' * 1000)
            os.utime(path, (1_000_000 + index, 1_000_000 + index))
            paths.append(path)
        result = prune_proxies(cache, max_bytes=2500)
        remaining = sorted(path.name for path in cache.glob('*.mp4'))
        assert remaining == ['p3.mp4', 'p4.mp4']
        assert result['bytes_before'] == 5000
        assert result['bytes_after'] == 2000
        assert len(result['removed']) == 3


def test_pruning_a_folder_that_does_not_exist_is_not_an_error():
    with scratch() as folder:
        result = prune_proxies(folder / 'absent', max_bytes=10)
        assert result == {'removed': [], 'bytes_before': 0, 'bytes_after': 0}


def test_a_long_source_is_proxied_only_over_the_window_the_lesson_uses():
    """Measured reason: the reference background is 200 s of 1080p60 and a
    three-word lesson uses about twelve seconds of it.  Proxying all 200 s would
    make the member wait a minute or two on the first preview, to save decode time
    on twelve seconds and leave a 40-80 MB file that is 95 % unused."""
    from preview.sources import SOURCE_WINDOW_PREROLL
    from word_video.media.core import duration
    with scratch() as folder:
        source = write_test_video(folder / 'long.mp4', seconds=6.0, fps=FPS,
                                  size='1920x1080')
        cache = folder / 'cache'
        full = resolve_preview_source(source, 540, cache_dir=cache)
        windowed = resolve_preview_source(source, 540, cache_dir=cache,
                                          needed_seconds=1.0)
        assert full.is_proxy and windowed.is_proxy
        assert full.path != windowed.path
        assert duration(windowed.path) <= 1.0 + SOURCE_WINDOW_PREROLL + 0.3
        assert duration(windowed.path) < duration(full.path)
        # And it is still the same clip, so the timeline can seek into it.
        assert windowed.original == str(source)


def test_a_source_the_lesson_uses_most_of_is_not_windowed():
    """The extra stream copy has to earn itself; below the fraction it does not."""
    with scratch() as folder:
        source = write_test_video(folder / 'short.mp4', seconds=2.0, fps=FPS,
                                  size='1920x1080')
        proxy = resolve_preview_source(source, 540, cache_dir=folder / 'c1')
        windowed = resolve_preview_source(source, 540, cache_dir=folder / 'c2',
                                          needed_seconds=1.9)
        assert proxy.is_proxy and windowed.is_proxy
        from preview.sources import proxy_cache_dir
        names = [path.name for path in proxy_cache_dir(folder / 'c2').glob('*.mp4')]
        assert not any('.window-' in name for name in names)


def test_a_proxy_for_one_canvas_is_not_reused_for_another():
    with scratch() as folder:
        source = write_test_video(folder / 'big.mp4', seconds=0.5, fps=FPS,
                                  size='1920x1080')
        cache = folder / 'cache'
        one = resolve_preview_source(source, 720, cache_dir=cache)
        other = resolve_preview_source(source, 540, cache_dir=cache)
        assert one.path != other.path
        assert (one.height, other.height) == (720, 540)


def test_no_named_pipe_or_deadlock_is_used_to_wait_for_a_child():
    """Guards the invariant the sandbox and the packaged app both depend on: the
    decoder talks to its child through a plain anonymous pipe, never a named one."""
    import preview.video as module
    source = module.__file__
    with open(source, encoding='utf-8') as handle:
        text = handle.read()
    assert '\\\\\\\\.\\\\pipe' not in text
    assert 'namedpipe' not in text.lower()
    assert isinstance(subprocess.PIPE, int)
