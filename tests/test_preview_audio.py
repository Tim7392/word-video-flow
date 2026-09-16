"""The windowed mixer: overlap, silence, gain, and a bounded number of open files."""
import struct
import wave
from pathlib import Path

import pytest

from preview.audio import (AudioSpan, WindowMixer, db_to_gain, spans_from_plan)
from test_preview_support import (RATE, scratch, three_tone_assets, three_word_plan,
                                  write_tone)


def read_wav(path):
    with wave.open(str(path), 'rb') as handle:
        return handle.readframes(handle.getnframes())


def test_gain_conversion_matches_the_decibel_definition():
    assert db_to_gain(0.0) == pytest.approx(1.0)
    assert db_to_gain(-6.0) == pytest.approx(0.5012, abs=1e-3)
    assert db_to_gain(6.0) == pytest.approx(1.9953, abs=1e-3)
    # Clamped to the range the delivery mixer accepts; anything far below the
    # floor is silence, not a very small number.
    assert db_to_gain(60.0) == 4.0
    assert db_to_gain(-200.0) == pytest.approx(0.0, abs=1e-6)


def test_a_gap_between_words_is_silence_not_a_repeat():
    """The teaching rhythm has gaps; a preview that carried sound across them
    would be describing a different lesson."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets, gap=0.25)
        mixer = WindowMixer(spans_from_plan(plan, assets))
        # The span list must show the gap as an absence, not as a longer clip.
        assert len(mixer.spans) == 3
        gaps = []
        for left, right in zip(mixer.spans, mixer.spans[1:]):
            gaps.append(right.first_frame - left.last_frame)
        assert all(gap == int(0.25 * RATE) for gap in gaps)
        # A window inside the first gap is exactly zero.
        silence = mixer.render_window(int(0.5 * RATE) + 100, 960)
        assert silence == bytes(2 * 960)
        mixer.close()


def test_a_window_no_clip_covers_is_silence():
    with scratch() as folder:
        assets = three_tone_assets(folder)
        beyond = AudioSpan(path=assets['tone1'], first_frame=48000, last_frame=96000)
        mixer = WindowMixer([beyond])
        assert mixer.render_window(0, 240) == bytes(2 * 240)
        mixer.close()


def test_single_clip_window_is_the_source_bytes_unchanged():
    """The common case must be a copy, not a re-synthesis."""
    with scratch() as folder:
        source = write_tone(Path(folder) / 'one.wav', 0.5, 440.0)
        raw = read_wav(source)
        mixer = WindowMixer([AudioSpan(path=str(source), first_frame=0,
                                       last_frame=len(raw) // 2)])
        block = mixer.render_window(0, 4800)
        assert block == raw[:2 * 4800]
        assert block != bytes(2 * 4800)
        mixer.close()


def test_a_window_straddling_a_clip_pads_both_ends_with_silence():
    with scratch() as folder:
        source = write_tone(Path(folder) / 'one.wav', 0.2, 440.0)
        raw = read_wav(source)
        frames = len(raw) // 2
        mixer = WindowMixer([AudioSpan(path=str(source), first_frame=1000,
                                       last_frame=1000 + frames)])
        block = mixer.render_window(0, 1000 + frames + 100)
        values = struct.unpack('<%dh' % (len(block) // 2), block)
        assert set(values[:1000]) == {0}
        assert set(values[1000:1000 + frames]) != {0}
        assert set(values[1000 + frames:]) == {0}
        mixer.close()


def test_two_overlapping_clips_are_summed_and_saturated():
    """Overlap is what an intro bed or an effect needs; the arithmetic is the
    delivery mix's, including the clip at full scale."""
    with scratch() as folder:
        loud = write_tone(Path(folder) / 'loud.wav', 0.2, 440.0, amplitude=20000)
        also = write_tone(Path(folder) / 'also.wav', 0.2, 660.0, amplitude=20000)
        spans = [AudioSpan(path=str(loud), first_frame=0, last_frame=9600, label='female'),
                 AudioSpan(path=str(also), first_frame=0, last_frame=9600, label='intro')]
        mixer = WindowMixer(spans)
        summed = struct.unpack('<%dh' % 960, mixer.render_window(0, 960))
        only_first = struct.unpack('<%dh' % 960,
                                   WindowMixer([spans[0]]).render_window(0, 960))
        # Where the two agree in sign the sum is larger; where they do not it is
        # smaller.  Either way it is not simply the first clip.
        assert summed != only_first
        # Signed 16-bit, so the floor is -32768 and the ceiling 32767.
        assert all(-32768 <= value <= 32767 for value in summed)
        mixed = mixer.render_window(0, 48000)
        values = struct.unpack('<%dh' % (len(mixed) // 2), mixed)
        assert max(values) == 32767 and min(values) == -32768
        mixer.close()


def test_gain_is_applied_before_summing():
    with scratch() as folder:
        source = write_tone(Path(folder) / 'one.wav', 0.1, 440.0, amplitude=8000)
        quiet = WindowMixer([AudioSpan(path=str(source), first_frame=0,
                                       last_frame=4800, gain=0.5)])
        loud = WindowMixer([AudioSpan(path=str(source), first_frame=0,
                                      last_frame=4800, gain=1.0)])
        a = struct.unpack('<%dh' % 480, quiet.render_window(0, 480))
        b = struct.unpack('<%dh' % 480, loud.render_window(0, 480))
        for left, right in zip(a, b):
            assert abs(left - int(right * 0.5)) <= 1
        quiet.close()
        loud.close()


def test_a_source_shorter_than_its_window_is_counted_not_hidden():
    """The plan already calls this SOURCE_TRUNCATED; the preview must not be the
    place where it becomes invisible."""
    with scratch() as folder:
        source = write_tone(Path(folder) / 'short.wav', 0.1, 440.0)
        mixer = WindowMixer([AudioSpan(path=str(source), first_frame=0,
                                       last_frame=9600)])
        block = mixer.render_window(0, 9600)
        assert len(block) == 2 * 9600
        assert mixer.truncated_frames == 9600 - 4800
        mixer.close()


def test_the_number_of_open_files_stays_bounded_on_a_long_lesson():
    """A 50-word lesson has ~150 clips; holding one handle each would show up in
    the handle counts the resource acceptance measures."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        spans = []
        for index in range(40):
            key = 'tone%d' % (index % 3 + 1)
            spans.append(AudioSpan(path=assets[key], first_frame=index * 4800,
                                   last_frame=(index + 1) * 4800))
        mixer = WindowMixer(spans, reader_cache=4)
        for index in range(40):
            mixer.render_window(index * 4800, 480)
            assert mixer.open_readers() <= 4
        mixer.close()
        assert mixer.open_readers() == 0


def test_mixing_refuses_a_rate_the_prepared_chain_does_not_produce():
    with pytest.raises(ValueError):
        WindowMixer([], rate=44100)


def test_mixing_refuses_a_file_that_is_not_prepared_speech():
    with scratch() as folder:
        bad = write_tone(Path(folder) / 'bad.wav', 0.1, 440.0, rate=44100)
        mixer = WindowMixer([AudioSpan(path=str(bad), first_frame=0, last_frame=4800)])
        with pytest.raises(ValueError):
            mixer.render_window(0, 480)
        mixer.close()


def test_the_plan_is_the_only_source_of_what_sounds():
    """spans come from plan.audio; a preview that scanned clips itself would be a
    second opinion about the timeline."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        spans = spans_from_plan(plan, assets, RATE)
        assert len(spans) == len(plan.audio) == 3
        assert [span.label for span in spans] == ['female'] * 3
        assert spans[0].first_frame == 0
        assert spans[0].last_frame == int(0.5 * RATE)
        assert spans[1].first_frame == int(0.5 * RATE)
        assert all(span.gain == 1.0 for span in spans)


def test_an_unresolved_asset_is_an_error_not_silence():
    """Playing nothing because a path was missing would look like a bug in the
    lesson rather than a bug in the wiring."""
    with scratch() as folder:
        assets = three_tone_assets(folder)
        plan, _ = three_word_plan(assets)
        del assets['tone2']
        with pytest.raises(KeyError):
            spans_from_plan(plan, assets, RATE)
