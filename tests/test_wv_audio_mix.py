"""Two audio clips in the same seconds: bounded overlap, gain, teaching policy.

The old audio model could only place stages one after another, so the only way
to put a sound under a word was to make the word's stage longer - which moves
every following word and changes the deliverable.  The general mixer must be
able to *express* an overlap; the teaching policy must be able to *refuse* the
ambiguous ones.  Keeping those two apart is what makes an honest error possible:
a model that cannot represent an overlap cannot report one either.

Everything here is checked by reading the published PCM back, sample by sample,
so "overlap" and "volume" mean arithmetic and not a filter name in a log.
"""
from pathlib import Path
import tempfile
import unittest
import wave

from word_video.media import (AudioClip, CLIP_GAIN_MAX, SOURCE_RATE, audio_format,
                              mix_clips, teaching_overlap_conflicts)

RATE = SOURCE_RATE


def _wav(path, values):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(b''.join(
            int(value).to_bytes(2, 'little', signed=True) for value in values))
    return str(path)


def _read(path):
    with wave.open(str(path), 'rb') as stream:
        block = stream.readframes(stream.getnframes())
    return [int.from_bytes(block[i:i + 2], 'little', signed=True)
            for i in range(0, len(block), 2)]


class MixClipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = _wav(self.root / 'a.wav', [1000] * 4800)     # 0.1 s of +1000
        self.b = _wav(self.root / 'b.wav', [2000] * 4800)     # 0.1 s of +2000

    def test_disjoint_clips_are_placed_exactly(self):
        target = self.root / 'out.wav'
        mix_clips([AudioClip(self.a, 0, 4800, label='female'),
                   AudioClip(self.b, 9600, 14400, label='male')], target, 19200)
        values = _read(target)
        self.assertEqual(19200, len(values))
        self.assertEqual([1000] * 4800, values[:4800])
        self.assertEqual([0] * 4800, values[4800:9600])
        self.assertEqual([2000] * 4800, values[9600:14400])
        self.assertEqual([0] * 4800, values[14400:])

    def test_overlapping_clips_sum_and_the_shift_is_real(self):
        """The overlap the old model could not express: two sources at once."""
        target = self.root / 'overlap.wav'
        mix_clips([AudioClip(self.a, 0, 4800, label='female'),
                   AudioClip(self.b, 2400, 7200, label='male')], target, 7200)
        values = _read(target)
        self.assertEqual([1000] * 2400, values[:2400])          # a alone
        self.assertEqual([3000] * 2400, values[2400:4800])      # a + b
        self.assertEqual([2000] * 2400, values[4800:7200])      # b alone
        # Nothing was moved to make room: the second clip starts where it was
        # asked to, half way through the first.
        self.assertEqual(3000, values[2400])
        self.assertEqual(2000, values[4800])

    def test_gain_scales_only_its_own_clip(self):
        target = self.root / 'gain.wav'
        mix_clips([AudioClip(self.a, 0, 4800, gain=1.0, label='female'),
                   AudioClip(self.b, 0, 4800, gain=0.25, label='effect')], target, 4800)
        self.assertEqual(1500, _read(target)[0])   # 1000 + 2000 * 0.25

    def test_gain_zero_mutes_without_removing(self):
        target = self.root / 'mute.wav'
        mix_clips([AudioClip(self.a, 0, 4800, gain=0.0)], target, 4800)
        self.assertEqual([0] * 4800, _read(target))

    def test_sums_are_clipped_not_wrapped(self):
        """A loud sum must saturate; wrapping would turn it into a loud click."""
        loud = _wav(self.root / 'loud.wav', [30000] * 100)
        target = self.root / 'clip.wav'
        mix_clips([AudioClip(loud, 0, 100), AudioClip(loud, 0, 100)], target, 100)
        self.assertEqual([32767] * 100, _read(target))
        quiet = _wav(self.root / 'quiet.wav', [-30000] * 100)
        target = self.root / 'clip-low.wav'
        mix_clips([AudioClip(quiet, 0, 100), AudioClip(quiet, 0, 100)], target, 100)
        self.assertEqual([-32768] * 100, _read(target))

    def test_a_clip_shorter_than_its_window_is_followed_by_silence(self):
        target = self.root / 'padded.wav'
        mix_clips([AudioClip(self.a, 0, 9600)], target, 9600)
        values = _read(target)
        self.assertEqual([1000] * 4800, values[:4800])
        self.assertEqual([0] * 4800, values[4800:])

    def test_speech_longer_than_its_window_is_refused_not_truncated(self):
        with self.assertRaises(ValueError) as caught:
            mix_clips([AudioClip(self.a, 0, 4800 - 1, label='female')],
                      self.root / 'short.wav', 4800)
        self.assertIn('longer than allocated stage', str(caught.exception))
        self.assertFalse((self.root / 'short.wav').exists())

    def test_out_of_range_and_bad_gain_are_refused(self):
        cases = [AudioClip(self.a, 0, 48000 + 1, label='past the end'),
                 AudioClip(self.a, 100, 100, label='empty window'),
                 AudioClip(self.a, -1, 10, label='negative start'),
                 AudioClip(self.a, 0, 4800, gain=-0.1),
                 AudioClip(self.a, 0, 4800, gain=CLIP_GAIN_MAX + 1),
                 AudioClip(self.a, 0, 4800, gain=float('nan'))]
        for index, clip in enumerate(cases):
            with self.subTest(label=clip.label or clip.gain):
                with self.assertRaises(ValueError):
                    mix_clips([clip], self.root / ('bad-%d.wav' % index), 48000)

    def test_a_source_that_is_not_prepared_pcm_is_refused(self):
        stereo = self.root / 'stereo.wav'
        with wave.open(str(stereo), 'wb') as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(RATE)
            stream.writeframes(b'\x00\x01' * 100)
        with self.assertRaises(ValueError) as caught:
            mix_clips([AudioClip(str(stereo), 0, 100)], self.root / 'stereo-out.wav', 100)
        self.assertIn('mono', str(caught.exception))

    def test_target_is_never_overwritten(self):
        target = self.root / 'existing.wav'
        mix_clips([AudioClip(self.a, 0, 4800)], target, 4800)
        with self.assertRaises(FileExistsError):
            mix_clips([AudioClip(self.b, 0, 4800)], target, 4800)

    def test_output_is_exactly_the_requested_length_and_format(self):
        target = self.root / 'exact.wav'
        mix_clips([AudioClip(self.a, 0, 4800)], target, 12345)
        self.assertEqual((1, 2, RATE, 12345), audio_format(target))

    def test_an_empty_mix_is_all_silence(self):
        target = self.root / 'empty.wav'
        mix_clips([], target, 1000)
        self.assertEqual([0] * 1000, _read(target))


class TeachingPolicyTests(unittest.TestCase):
    """Overlaps that must be blocked, and the ones that must be allowed."""

    def test_two_spoken_roles_at_once_is_a_conflict(self):
        clips = [AudioClip('a.wav', 0, 48000, label='female'),
                 AudioClip('b.wav', 24000, 72000, label='male')]
        self.assertEqual([('female', 'male')], teaching_overlap_conflicts(clips))

    def test_a_bed_under_speech_is_not_reported_as_a_conflict(self):
        clips = [AudioClip('intro.wav', 0, 48000, label='intro'),
                 AudioClip('a.wav', 24000, 72000, label='female')]
        self.assertEqual([], teaching_overlap_conflicts(clips))

    def test_touching_stages_are_not_an_overlap(self):
        """The normal case: stage N ends exactly where stage N+1 begins."""
        clips = [AudioClip('a.wav', 0, 48000, label='female'),
                 AudioClip('b.wav', 48000, 96000, label='male')]
        self.assertEqual([], teaching_overlap_conflicts(clips))

    def test_every_ambiguous_pair_is_reported(self):
        clips = [AudioClip('a.wav', 0, 96000, label='female'),
                 AudioClip('b.wav', 48000, 144000, label='male'),
                 AudioClip('c.wav', 60000, 150000, label='chinese')]
        self.assertEqual([('female', 'male'), ('female', 'chinese'),
                          ('male', 'chinese')],
                         teaching_overlap_conflicts(clips))


if __name__ == '__main__':
    unittest.main()
