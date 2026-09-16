"""The one clock: position comes from played samples, and a seek is a generation."""
import pytest

from preview.clock import (AudioClock, ManualAudioOutput, NullAudioOutput,
                           PlaybackState, frames_for_ticks, ticks_for_frames)
from word_video.domain.timebase import TICKS_PER_SECOND


def test_position_follows_played_frames_exactly():
    clock = AudioClock(48000, TICKS_PER_SECOND * 10)
    clock.begin(0, 0)
    assert clock.position_ticks(0) == 0
    # One second of played frames is exactly one second of ticks.
    assert clock.position_ticks(48000) == TICKS_PER_SECOND
    assert clock.position_ticks(24000) == TICKS_PER_SECOND // 2


def test_clock_never_runs_past_the_lesson():
    total = TICKS_PER_SECOND * 2
    clock = AudioClock(48000, total)
    clock.begin(0, 0)
    assert clock.position_ticks(48000 * 99) == total
    assert clock.at_end(48000 * 99)


def test_reading_the_clock_twice_without_playing_does_not_drift():
    """Position is derived, so a stalled UI thread cannot make the timeline creep."""
    clock = AudioClock(48000, TICKS_PER_SECOND * 10)
    clock.begin(TICKS_PER_SECOND, 0)
    first = clock.position_ticks(1234)
    for _ in range(50):
        assert clock.position_ticks(1234) == first


def test_a_seek_bumps_the_generation_and_re_anchors():
    clock = AudioClock(48000, TICKS_PER_SECOND * 10)
    first = clock.begin(0, 0)
    assert clock.is_current(first)
    second = clock.reanchor(TICKS_PER_SECOND * 3, 0)
    assert second == first + 1
    assert not clock.is_current(first)
    assert clock.is_current(second)
    assert clock.position_ticks(0) == TICKS_PER_SECOND * 3
    # The anchor is in *played* frames, so a device that was not reset cannot
    # shift the new position.
    third = clock.reanchor(TICKS_PER_SECOND * 4, 700)
    assert clock.position_ticks(700) == TICKS_PER_SECOND * 4
    assert clock.is_current(third)


def test_ticks_and_frames_round_trip_exactly_at_the_mix_rate():
    """48 kHz divides the 720000 tick grid (15 ticks a sample), so the preview's
    own frame maths is exact and cannot accumulate error."""
    for frames in (0, 1, 15, 480, 48000, 123456):
        assert frames_for_ticks(ticks_for_frames(frames, 48000), 48000) == frames


def test_a_rate_that_does_not_divide_the_tick_grid_loses_less_than_a_sample():
    """44.1 kHz is 800/49 ticks a sample, so one sample is not a whole number of
    ticks.  The conversion must round *down* - never past the tick - and the loss
    must stay below one sample, which is why the preview mix refuses this rate
    instead of pretending to be sample-exact."""
    for frames in (1, 441, 4410, 44100):
        ticks = ticks_for_frames(frames, 44100)
        back = frames_for_ticks(ticks, 44100)
        assert back <= frames
        assert ticks_for_frames(frames - back, 44100) < ticks_for_frames(1, 44100) + 1


@pytest.mark.parametrize('bad', [0, -1])
def test_clock_refuses_a_useless_rate(bad):
    with pytest.raises(ValueError):
        AudioClock(bad, 100)


def test_manual_output_only_moves_when_the_test_says_so():
    output = ManualAudioOutput(rate=48000, buffer_frames=1000)
    output.start()
    assert output.frames_played() == 0
    output.write(bytes(2 * 400))
    # Accepting audio is not hearing it.
    assert output.frames_played() == 0
    assert output.buffered == 400
    assert output.advance(150) == 150
    assert output.frames_played() == 150
    # It cannot play more than it holds.
    assert output.advance(99999) == 250
    assert output.frames_played() == 400


def test_manual_output_refuses_a_block_larger_than_the_free_space():
    output = ManualAudioOutput(rate=48000, buffer_frames=100)
    output.start()
    assert output.write(bytes(2 * 101)) == 0
    assert output.write(bytes(2 * 100)) == 200
    assert output.free_frames() == 0


def test_flush_throws_away_what_was_not_heard_yet():
    """This is the operation a seek depends on: without it the old position keeps
    playing for up to a whole device buffer."""
    output = ManualAudioOutput(rate=48000, buffer_frames=1000)
    output.start()
    output.write(bytes(2 * 600))
    output.advance(200)
    output.flush()
    assert output.buffered == 0
    assert output.frames_played() == 200
    assert output.flushes == 1


def test_null_output_paces_itself_instead_of_finishing_instantly():
    """A null sink that reported instant completion would turn a preview into a
    batch job.  The value is wall-clock derived, so the assertion is a bound: it
    must be nowhere near the full second it was handed, not exactly zero.
    """
    output = NullAudioOutput(rate=48000, buffer_frames=4800)
    output.start()
    output.write(bytes(2 * 48000))
    assert output.frames_played() < 48000
    assert output.free_frames() == 0
    output.close()


def test_states_are_explicit():
    clock = AudioClock(48000, 1000)
    assert clock.state is PlaybackState.IDLE
    clock.set_state(PlaybackState.PLAYING)
    assert clock.state is PlaybackState.PLAYING
    clock.set_state(PlaybackState.ENDED)
    assert clock.state is PlaybackState.ENDED
