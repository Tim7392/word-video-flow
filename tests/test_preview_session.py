"""Continuous preview session: no old audio after a seek, and a finite teardown.

The central test here does not ask "does it sound right".  It reads back the PCM
the device was actually handed, labels each block with the generation the session
was in when it wrote it, and names the word by its pitch.  A block left over from
the previous position therefore fails as *the wrong frequency*, which is a
regression test; "sounds fine to me" is not.
"""
import threading
import time

import pytest

from preview.clock import ManualAudioOutput, PlaybackState
from preview.layout import LayoutUnavailable
from preview.queues import BLOCK
from preview.session import PreviewSession, canvas_size_for, picture_item
from preview.video import VideoFrame
from test_preview_support import (RATE, background_item, dominant_hz, plan_of, scratch,
                                  speech_item, three_tone_assets, three_word_plan,
                                  write_tone)
from word_video.domain.timebase import TICKS_PER_SECOND


class RecordingOutput(ManualAudioOutput):
    """A manual sink that keeps every block, tagged with the live generation."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.blocks = []
        self.session = None

    def write(self, pcm):
        accepted = super().write(pcm)
        if accepted:
            generation = self.session.clock.generation if self.session else -1
            self.blocks.append((generation, pcm))
        return accepted


def wait_for(predicate, timeout=5.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def audio_session(plan, assets, **kwargs):
    output = RecordingOutput(rate=RATE, buffer_frames=4800)
    session = PreviewSession(plan, assets, output=output, proxy=False, **kwargs)
    output.session = session
    return session, output


def test_a_seek_never_plays_the_previous_positions_audio():
    """The acceptance that matters most: after a seek the device must not be
    handed one more block of the old position."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, tones = three_word_plan(assets)
        session, output = audio_session(plan, assets)
        session.play()
        try:
            assert wait_for(lambda: output.written >= 2 * 960), 'mixer never started'
            before = [block for block in output.blocks]
            assert before, 'no PCM was ever written'
            spoken_before = b''.join(pcm for _, pcm in before)
            assert dominant_hz(spoken_before) == pytest.approx(tones[0], rel=0.05)

            generation_before = session.clock.generation
            # Seek into the third word (tones are 0.5 s each, no gap).
            session.seek(plan.audio[2].start_ticks + 24000)
            generation_after = session.clock.generation
            assert generation_after == generation_before + 1

            output.advance(4800)
            assert wait_for(lambda: len(output.blocks) > len(before)), \
                'mixer wrote nothing after the seek'
            after = output.blocks[len(before):]
            # Every block after the seek belongs to the new generation: the mixer
            # re-checks under the same lock the seek takes, so nothing rendered for
            # the old position can slip through.
            assert {generation for generation, _ in after} == {generation_after}
            spoken_after = b''.join(pcm for _, pcm in after)
            assert dominant_hz(spoken_after) == pytest.approx(tones[2], rel=0.05)
            assert session.report()['audio_blocks_dropped'] >= 0
        finally:
            session.close()


def test_a_seek_while_paused_shows_the_new_position_without_sound():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        session, output = audio_session(plan, assets)
        try:
            session.seek(plan.audio[1].start_ticks)
            assert session.clock.state is not PlaybackState.PLAYING
            # No mixer thread is running, so no PCM can be written while paused.
            time.sleep(0.05)
            assert output.blocks == []
            assert session.position_ticks() == plan.audio[1].start_ticks
        finally:
            session.close()


def test_frames_from_a_previous_generation_are_never_presented():
    """Stale-drop is asserted directly, because "it looked fine" cannot tell a
    dropped old frame from a frame that happened to match."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        session, output = audio_session(plan, assets)
        try:
            session.open()
            session.play()
            session.seek(24000)
            current = session.clock.generation
            # One frame from the generation the user just left, and one current.
            session.frames.put_nowait(VideoFrame(generation=current - 1, index=0,
                                                 pts_ticks=0, width=2, height=2,
                                                 buffer=bytes(16)))
            session.frames.put_nowait(VideoFrame(generation=current, index=5,
                                                 pts_ticks=0, width=2, height=2,
                                                 buffer=bytes(16)))
            presentation = session.snapshot()
            assert presentation.frame is not None
            assert presentation.frame.generation == current
            assert presentation.stale_dropped == 1
        finally:
            session.close()


def test_the_frame_queue_is_bounded_and_paces_the_decoder():
    """The queue refuses rather than overwriting.

    Regression for a real defect: with a drop-oldest frame queue the decoder raced
    to the end of the item and the queue kept only the newest frames, so the frames
    the clock was about to need had already been thrown away.  A looping background
    presented 6 of 120 frames and then jumped.  Refusing to accept more than
    ``maxlen`` frames makes the queue depth the decode lead, which is the pacing
    that was missing.
    """
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        session, output = audio_session(plan, assets, frame_queue=3)
        try:
            session.open()
            accepted = [session.frames.put_nowait(
                VideoFrame(generation=0, index=index, pts_ticks=index, width=2,
                           height=2, buffer=bytes(16))) for index in range(10)]
            assert accepted == [True, True, True] + [False] * 7
            stats = session.frames.stats()
            assert stats['depth'] == 3
            assert stats['maxlen'] == 3
            assert stats['overflow'] == BLOCK
            # Nothing was silently discarded: a refused frame stays with the
            # producer, which is what lets it wait and deliver it later.
            assert stats['dropped'] == 0
            assert session.frames.get(timeout=0).index == 0
        finally:
            session.close()


def test_the_decoder_waits_for_room_instead_of_decoding_past_the_clock():
    """The delivery path must block on a full queue, not drop."""
    from preview.queues import BLOCK, BoundedQueue
    from preview.video import FfmpegFrameDecoder

    queue = BoundedQueue(maxlen=2, overflow=BLOCK)
    decoder = FfmpegFrameDecoder(queue, ffmpeg=['unused'])
    frame = VideoFrame(generation=1, index=0, pts_ticks=0, width=2, height=2,
                       buffer=bytes(16))
    # A stopped decoder delivers nothing: refusing is how the producer learns to
    # wind down rather than spin.
    assert decoder._deliver(frame) is False
    decoder.start()
    try:
        assert decoder._deliver(frame) is True
        assert decoder._deliver(frame) is True
        # Full: the third hand-off must not overwrite what is already queued.
        started = time.monotonic()
        assert decoder._deliver(frame, wait=0.2) is False
        assert time.monotonic() - started >= 0.15
        assert len(queue) == 2
        assert queue.dropped == 0
        # Making room releases it, so waiting is not the same as giving up.
        assert queue.get(timeout=0) is not None
        assert decoder._deliver(frame, wait=0.5) is True
    finally:
        decoder.stop(timeout=5.0)
    queue.close()
    # And a closed queue stops the producer instead of letting it spin.
    assert decoder._deliver(frame) is False


def test_closing_twice_is_safe_and_reports_a_clean_teardown():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        session, output = audio_session(plan, assets)
        session.play()
        assert wait_for(lambda: output.written > 0)

        def live_preview_threads():
            return [thread.name for thread in threading.enumerate()
                    if thread.name.startswith('preview-')]

        assert live_preview_threads() == ['preview-mix']
        report = session.close()
        assert report['mixer_stopped'] is True
        assert report['decoder_stopped'] is True
        assert report['children_left'] == 0
        assert report['threads_left'] == 0
        assert output.closed is True
        assert live_preview_threads() == []
        assert session.clock.state is PlaybackState.RELEASED
        # Idempotent: a second close reports rather than raising.
        assert session.close()['state'] == PlaybackState.RELEASED.value
        # And commands after close are refused instead of half-working.
        with pytest.raises(RuntimeError):
            session.seek(0)


def test_repeated_open_play_seek_close_leaves_nothing_behind():
    """The acceptance runs this twenty times; here it runs enough times to show the
    thread count is flat rather than merely small."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        for _ in range(8):
            session, output = audio_session(plan, assets)
            session.play()
            session.seek(plan.audio[1].start_ticks)
            output.advance(960)
            session.snapshot()
            report = session.close()
            assert report['threads_left'] == 0 and report['children_left'] == 0
        assert not [thread for thread in threading.enumerate()
                    if thread.name.startswith('preview-')]


def test_a_plan_without_a_background_opens_with_no_decoder():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        assert picture_item(plan) is None
        session, output = audio_session(plan, assets)
        try:
            session.open()
            assert session.decoder is None
            # A snapshot is still valid: text-only is a legitimate preview.
            assert session.snapshot().frame is None
        finally:
            session.close()


def test_the_picture_item_is_the_one_with_media():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        video = [background_item('tone1', 1.5)]
        with_picture = plan_of(plan.audio, video=video, total_seconds=1.5)
        item = picture_item(with_picture)
        assert item is not None and item.role == 'background'


def test_canvas_keeps_the_plan_aspect_and_never_exceeds_the_ask():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        assert canvas_size_for(plan, 720) == (1280, 720)
        assert canvas_size_for(plan, 1080) == (1920, 1080)
        # A source smaller than the ask is not upscaled.
        assert canvas_size_for(plan, 540) == (960, 540)


class QtLikeAudioOutput(ManualAudioOutput):
    """A sink that behaves like the measured Qt one, where it matters.

    ``QAudioSink.reset()`` discards the buffer, re-zeroes ``processedUSecs()``
    *and stops the device*, and a stopped sink accepts nothing until it is started
    again.  A teardown or a seek that gets the order wrong therefore goes silent
    from the first seek onwards - invisibly, because nothing raises.  This double
    reproduces that, so the ordering is covered by a test instead of by a
    measurement run on real media.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.running = False

    def start(self):
        super().start()
        self.running = True

    def write(self, pcm):
        if not self.running:
            return 0
        return super().write(pcm)

    def free_frames(self):
        if not self.running:
            return 0
        return super().free_frames()

    def flush(self):
        super().flush()
        self.running = False


def test_audio_flows_again_after_a_seek_on_a_sink_that_stops_when_flushed():
    """Regression: starting the device and *then* flushing leaves it stopped, and
    the preview is silent after the first seek while every log looks normal."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        output = QtLikeAudioOutput(rate=RATE, buffer_frames=4800)
        session = PreviewSession(plan, assets, output=output, proxy=False)
        try:
            session.play()
            assert wait_for(lambda: output.written > 0), 'no audio before the seek'
            session.seek(plan.audio[2].start_ticks)
            assert output.running is True, 'the sink was left stopped by the seek'
            output.advance(4800)
            before = output.written
            assert wait_for(lambda: output.written > before), \
                'no audio after the seek: the sink was never restarted'
        finally:
            session.close()


def test_a_seek_asks_the_decoder_for_a_video_frame_offset_not_an_audio_sample():
    """Regression: the decoder's offset is a *video frame* index.  Converting it
    through the 48 kHz sample rate asks for a frame tens of thousands too far in,
    so ffmpeg finds nothing past the end and no picture ever appears."""
    with scratch() as folder:
        from preview.video import DecodeSpec

        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        session = PreviewSession(plan, assets, proxy=False, output=ManualAudioOutput(rate=RATE))
        requests = []

        class FakeDecoder:
            def request(self, generation, spec):
                requests.append((generation, spec.offset_frames))

            def start(self):
                return True

            def stop(self, timeout=5.0):
                return True

            alive_threads = staticmethod(lambda: 0)
            alive_children = staticmethod(lambda: 0)

        try:
            session.open()
            # A 60 fps plan whose picture item spans the whole lesson.
            session.decode_spec = DecodeSpec(
                path='unused.mp4', width=64, height=48, fps_num=60, fps_den=1,
                start_ticks=0, end_ticks=plan.total_ticks, source_frames=12000)
            session.decoder = FakeDecoder()
            target = plan.audio[2].start_ticks
            session.play()
            session.seek(target)
            generation, offset = requests[-1]
            assert generation == session.clock.generation
            expected = target // session.decode_spec.frame_ticks
            assert offset == expected, 'asked for frame %d, expected %d' % (offset, expected)
            # And that is a frame number, not a sample count: 6.7 s of 60 fps
            # material is ~400 frames, not ~320000 samples.
            assert offset < plan.total_ticks // session.decode_spec.frame_ticks + 1
            assert offset != target * RATE // TICKS_PER_SECOND
        finally:
            session._closed = True
            session.frames.close()


def test_the_preview_refuses_a_rate_the_mix_chain_cannot_serve():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        from dataclasses import replace
        with pytest.raises(ValueError):
            PreviewSession(replace(plan, sample_rate=44100), assets, proxy=False)


def test_asking_for_the_real_layout_says_what_is_missing():
    """Until B's LayoutSurface is merged the preview must fail with a sentence an
    engineer can act on, not an ImportError from three frames down."""
    from preview.layout import LayoutSurfaceDisplay
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        with pytest.raises(LayoutUnavailable) as error:
            LayoutSurfaceDisplay.from_plan(plan, width=320, height=180)
        assert 'word_video.layout' in str(error.value)
        assert 'second layout' in str(error.value)
