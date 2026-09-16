"""What a media file really contains: sample counts, PTS, and proxy copies.

Every number here is checked against the file it describes, because three
different "durations" are routinely confused:

* the container's, which is what the old code read everywhere;
* the stream's, which is what a muxer pads or trims to;
* the sample count, which is the only one that can be compared with
  ``round(seconds * sample_rate)`` and the only one that reveals an encoder that
  added or dropped a packet.

A proxy is checked for the property that makes it usable at all: same length,
same content area, smaller picture - never a replacement for the original.
"""
import json
from pathlib import Path
import tempfile
import unittest
import wave

from word_video.media import (audio_format, audio_pts, audio_sample_count, duration,
                              executable, proxy_video, run, stream_facts, video_stream)

RATE = 48000


def _wav(path, seconds, rate=RATE, sample=1000):
    count = int(round(seconds * rate))
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(sample.to_bytes(2, 'little', signed=True) * count)
    return Path(path)


class SampleFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_sample_count_is_the_sample_count(self):
        """48 kHz and 44.1 kHz, whole seconds and not, from the header itself."""
        for seconds, rate in ((1.0, 48000), (0.5, 48000), (1.0, 44100), (0.25, 22050)):
            with self.subTest(seconds=seconds, rate=rate):
                path = _wav(self.root / ('%s-%s.wav' % (seconds, rate)), seconds, rate)
                facts = audio_sample_count(path)
                self.assertEqual(int(round(seconds * rate)), facts['samples'])
                self.assertEqual(rate, facts['sample_rate'])
                self.assertTrue(facts['exact'])
                # The WAV header is the authority, so the two must agree exactly.
                self.assertEqual(audio_format(path)[3], facts['samples'])

    def test_sample_count_from_a_compressed_container_is_reported_as_such(self):
        """A compressed source answers from packets or from duration - and says which."""
        path = self.root / 'tone.m4a'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1.0:sample_rate=48000',
             '-c:a', 'aac', '-b:a', '128k', str(path)])
        facts = audio_sample_count(path)
        self.assertEqual(48000, facts['sample_rate'])
        self.assertIn(facts['exact'], (True, False))
        if not facts['exact']:
            # Derived from the stream duration, so an encoder's priming and
            # padding show up as a small difference; that is exactly why the
            # flag is reported instead of being hidden.
            self.assertAlmostEqual(48000, facts['samples'], delta=3000)
        else:
            self.assertGreater(facts['samples'], 0)

    def test_pts_is_reported_and_a_wav_starts_at_zero(self):
        path = _wav(self.root / 'plain.wav', 0.3)
        facts = audio_pts(path)
        self.assertEqual(0.0, facts['start_s'])
        self.assertEqual(0, facts['start_sample'])

    def test_a_missing_stream_is_named_not_guessed(self):
        path = self.root / 'picture.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'color=c=black:s=64x64:r=30:d=0.2', '-an', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', str(path)])
        with self.assertRaises(ValueError) as caught:
            audio_sample_count(path)
        self.assertIn('No audio stream', str(caught.exception))
        self.assertEqual(64, int(video_stream(path)['width']))
        self.assertFalse(stream_facts(path)['has_audio'])

    def test_stream_facts_are_json_serialisable_for_a_job_result(self):
        path = _wav(self.root / 'facts.wav', 0.1)
        facts = stream_facts(path)
        self.assertEqual(str(path.resolve()), facts['path'])
        self.assertTrue(facts['has_audio'])
        self.assertEqual(4800, facts['samples'])
        json.dumps(facts)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'big.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'testsrc=size=1920x1080:rate=30:duration=1.0',
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.0',
             '-shortest', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k', str(self.source)])

    def test_proxy_keeps_length_and_aspect_and_is_smaller(self):
        target = self.root / 'proxy.mp4'
        proxy_video(self.source, target, height=540)
        self.assertTrue(target.is_file())
        picture = video_stream(target)
        self.assertEqual(540, int(picture['height']))
        self.assertEqual(960, int(picture['width']))     # 16:9 preserved
        # Same seconds as the original, within a frame of the 30 fps grid.
        self.assertAlmostEqual(duration(self.source), duration(target), delta=0.1)
        self.assertFalse(audio_sample_count(target)['samples'] == 0)
        self.assertLess(target.stat().st_size, self.source.stat().st_size)

    def test_proxy_never_upscales_a_preset_and_never_overwrites(self):
        """A preset shrinks or keeps the size - it never enlarges the picture."""
        target = self.root / 'same-size.mp4'
        proxy_video(self.source, target, height='1080p')
        self.assertEqual(1080, int(video_stream(target)['height']))
        with self.assertRaises(FileExistsError):
            proxy_video(self.source, target, height=720)

    def test_unknown_preset_and_bad_crf_are_refused(self):
        with self.assertRaises(ValueError):
            proxy_video(self.source, self.root / 'x.mp4', height=1234)
        with self.assertRaises(ValueError):
            proxy_video(self.source, self.root / 'y.mp4', height=720, crf=90)
        # An explicit crf is allowed for a non-preset height, and is honoured.
        proxy_video(self.source, self.root / 'z.mp4', height=1234, crf=30)
        self.assertEqual(1234, int(video_stream(self.root / 'z.mp4')['height']))

    def test_a_silent_source_produces_a_silent_proxy(self):
        silent = self.root / 'silent.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'color=c=black:s=320x180:r=30:d=0.5', '-an', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', str(silent)])
        target = self.root / 'silent-proxy.mp4'
        # No preset is 180p, so the caller states the quality; the aspect ratio
        # of the tiny source is preserved.
        proxy_video(silent, target, height=180, crf=28)
        self.assertEqual(320, int(video_stream(target)['width']))
        with self.assertRaises(ValueError):
            audio_sample_count(target)


if __name__ == '__main__':
    unittest.main()
