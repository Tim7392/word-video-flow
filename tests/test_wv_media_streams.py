"""What a media file really contains: sample counts, PTS, and proxy copies.

Every number here is checked against the file it describes, because three
different "durations" are routinely confused:

* the container's, which is what the old code read everywhere;
* the stream's, which is what a muxer pads or trims to;
* the sample count, which is the only one that can be compared with
  ``round(seconds * sample_rate)`` and the only one that reveals an encoder that
  added or dropped a packet.

A **decode** is the fourth authority and the one that settled the audit in B-6: a
container can count samples the decoder throws away (Ogg/Opus counts its 6.5 ms
pre-skip; every one of the archive's 2 744 Opus files answered 312 samples too
many) or the decoder can pad samples the container does not hold (AAC pads its
last packet to a whole frame).  Both directions are pinned below, so a later
"correction" in either direction has to argue with a test.

A picture is measured in frames only while it really has one frame grid: a
variable frame rate clip is measured in milliseconds instead, because ffprobe's
average rate is not a rate any frame was captured at.

A proxy is checked for the property that makes it usable at all: same length,
same content area, smaller picture - never a replacement for the original.
"""
import json
from pathlib import Path
import tempfile
import unittest
import wave
from fractions import Fraction

import pytest

from word_video.media import (audio_format, audio_pts, audio_sample_count,
                              audio_stream, duration, executable, probe,
                              proxy_video, run, stream_facts, video_stream,
                              video_stream_seconds)
from word_video.media.streams import opus_pre_skip, samples_from_stream

RATE = 48000
ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
REFERENCE_CLIP = ARCHIVE / '_prepared-1080p' / '4级1500开头_透明通道-1080p.mov'


def _decoded_samples(path):
    """How many samples the decoder really emits - the authority, not a header."""
    data = run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(path),
                '-map', '0:a:0', '-vn', '-f', 's16le', '-ac', '1', '-'], timeout=600)
    return len(data) // 2


def _encoder(name):
    """Skip a codec-specific fixture on an ffmpeg that cannot write it."""
    listing = run([executable('ffmpeg'), '-hide_banner', '-encoders']).decode('utf-8',
                                                                             'replace')
    if name not in listing:
        raise unittest.SkipTest('this ffmpeg has no %s encoder' % name)



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

    def test_aac_is_never_answered_from_its_packet_count(self):
        """The defect H0 found: ``nb_frames`` for AAC counts packets, not samples.

        The reference clip reports ``nb_frames=88`` for 1.856 s at 48 kHz, so the
        answer is 89088 samples.  Reading the packet count gave 88 *and* labelled it
        ``exact`` - off by three orders of magnitude.  Both a real and a synthetic
        AAC file are checked, so the rule holds whatever this machine has.
        """
        if REFERENCE_CLIP.is_file():
            stream = next(item for item in probe(REFERENCE_CLIP)['streams']
                          if item['codec_type'] == 'audio')
            self.assertEqual('aac', stream['codec_name'])
            packets = int(stream['nb_frames'])
            expected = int(round(float(stream['duration'])
                                 * float(stream['sample_rate'])))
            facts = audio_sample_count(REFERENCE_CLIP)
            self.assertEqual(89088, expected)
            self.assertEqual(expected, facts['samples'])
            self.assertNotEqual(packets, facts['samples'],
                                'the packet count was returned as a sample count')
            self.assertFalse(facts['exact'],
                             'a derived count must not be labelled exact')

        path = self.root / 'tone.m4a'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1.0:sample_rate=48000',
             '-c:a', 'aac', '-b:a', '128k', str(path)])
        stream = next(item for item in probe(path)['streams']
                      if item['codec_type'] == 'audio')
        facts = audio_sample_count(path)
        self.assertEqual(48000, facts['sample_rate'])
        self.assertFalse(facts['exact'])
        self.assertAlmostEqual(48000, facts['samples'], delta=3000)
        if stream.get('nb_frames'):
            self.assertNotEqual(int(stream['nb_frames']), facts['samples'])

    def test_a_lossless_stream_may_still_answer_from_its_frame_count(self):
        """The exact path stays for a codec whose ``nb_frames`` is samples.

        Real FLAC comes through the duration branch (ffprobe publishes no frame
        count for it), so the whitelist itself is exercised on the stream shapes
        that *do* carry one - the same shapes the real clip and a real MP4 produce.
        """
        path = self.root / 'lossless.flac'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=0.5:sample_rate=48000',
             '-c:a', 'flac', str(path)])
        facts = audio_sample_count(path)
        self.assertEqual(24000, facts['samples'])
        # 24000 samples of a 0.5 s sine: right either way, and honest about which.
        stream = next(item for item in probe(path)['streams']
                      if item['codec_type'] == 'audio')
        self.assertEqual('flac', stream['codec_name'])
        self.assertEqual(stream.get('nb_frames') is not None, facts['exact'])

    def test_the_stream_rule_itself_is_exercised_on_real_shapes(self):
        """A pure check of the rule, on the stream dicts ffprobe really returns."""
        from word_video.media.streams import samples_from_stream

        # The AAC shape from the reference clip: 88 packets, 1.856 s.
        aac = {'codec_name': 'aac', 'sample_rate': '48000', 'nb_frames': '88',
               'duration': '1.856000'}
        self.assertEqual({'samples': 89088, 'sample_rate': 48000, 'exact': False},
                         samples_from_stream(aac))
        # A PCM stream that does publish a frame count: samples, and exact.
        pcm = {'codec_name': 'pcm_s16le', 'sample_rate': '44100', 'nb_frames': '4410',
               'duration': '0.100000'}
        self.assertEqual({'samples': 4410, 'sample_rate': 44100, 'exact': True},
                         samples_from_stream(pcm))
        # Packets without a duration cannot be turned into samples: refuse.
        with self.assertRaises(ValueError) as caught:
            samples_from_stream({'codec_name': 'mp3', 'sample_rate': '44100',
                                 'nb_frames': '40'})
        self.assertIn('packets', str(caught.exception))

    def test_opus_is_never_counted_with_its_pre_skip(self):
        """Ogg/Opus counts its pre-skip as audio; the decoder does not.

        The audit measured the whole archive first: every one of the 2 744 distinct
        Ogg/Opus files answered exactly 312 samples (6.5 ms at 48 kHz) more than the
        decoder emits, and 312 is the pre-skip the files' own OpusHead declares.  The
        engine therefore reads it from the file it is measuring rather than assuming
        a constant - a file that declares a different one gets that one.
        """
        _encoder('libopus')
        path = self.root / 'tone.ogg'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1.0:sample_rate=48000',
             '-c:a', 'libopus', '-b:a', '32k', str(path)])
        stream = audio_stream(path)
        self.assertEqual('opus', stream['codec_name'])
        self.assertEqual(312, opus_pre_skip(path))
        # What the container claims, and what it is worth: 1.0065 s for a 1.0 s tone.
        header_answer = samples_from_stream(stream)['samples']
        facts = audio_sample_count(path)
        self.assertEqual(_decoded_samples(path), facts['samples'],
                         'the engine has to agree with the decoder, sample for sample')
        self.assertEqual(header_answer - 312, facts['samples'])
        self.assertEqual(312, facts['priming_samples'])
        self.assertFalse(facts['exact'], 'a derived count is still derived')

    def test_a_packetised_mp3_is_measured_to_within_one_frame(self):
        """The mp3 case H0 named: no frame count, and packets are not samples.

        A VBR file written without a Xing header publishes no frame count at all, so
        the engine derives the count from the duration and says it is not exact.  The
        derived number is within one MP3 frame (1 152 samples) of the decode; a
        stream that publishes packets but *no* duration is refused by name, which the
        rule test above already pins.
        """
        _encoder('libmp3lame')
        path = self.root / 'vbr.mp3'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1.0:sample_rate=44100',
             '-c:a', 'libmp3lame', '-q:a', '4', '-write_xing', '0', str(path)])
        stream = audio_stream(path)
        self.assertEqual('mp3', stream['codec_name'])
        self.assertFalse(stream.get('nb_frames'), 'a packet count must not be published')
        facts = audio_sample_count(path)
        self.assertFalse(facts['exact'])
        self.assertEqual(44100, facts['sample_rate'])
        self.assertLessEqual(abs(facts['samples'] - _decoded_samples(path)), 1152)

    def test_an_aac_stream_is_not_corrected_the_way_opus_is(self):
        """The other direction: AAC pads, so its duration is the playable length.

        36 of the archive's products disagree with their own decode by 0...992 samples
        because the decoder pads the last packet up to a whole 1 024-sample frame.  If
        the Opus correction were applied to every packetised codec, it would invent a
        shortage here - so this test fixes which way the correction goes.
        """
        path = self.root / 'tone.m4a'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1.0:sample_rate=48000',
             '-c:a', 'aac', '-b:a', '128k', str(path)])
        facts = audio_sample_count(path)
        decoded = _decoded_samples(path)
        self.assertFalse(facts['exact'])
        self.assertNotIn('priming_samples', facts, 'AAC has no pre-skip to subtract')
        self.assertLessEqual(facts['samples'], decoded)
        self.assertLess(decoded - facts['samples'], 1024)

    def test_measuring_one_asset_probes_it_once(self):
        """Two ffprobes per asset was half the import's measurement cost.

        The catalogue asked ``has_audio`` (one probe) and then measured the audio
        (another probe) - 0.32 s an asset over the 150 assets of a real 50-word
        import.  The stream list is taken once and handed to the measurement.
        """
        from unittest.mock import patch

        from word_video.exporters.catalog import Asset, measure
        from word_video.media import core as media_core
        from word_video.media import probe as real_probe
        import word_video.media as media_package

        path = self.root / 'measured.m4a'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=0.5:sample_rate=48000',
             '-c:a', 'aac', '-b:a', '128k', str(path)])
        calls = []

        def counting(value):
            calls.append(str(value))
            return real_probe(value)

        with patch.object(media_package, 'probe', counting), \
                patch.object(media_core, 'probe', counting):
            info = measure(Asset(asset_id='audit', path=str(path)))
        self.assertEqual(1, len(calls), calls)
        self.assertEqual(24000, info.units)
        self.assertEqual(48000, info.unit_den)

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


class PictureGridTests(unittest.TestCase):
    """A picture is measured in frames only while it has one frame grid."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _measure(self, path):
        from word_video.exporters.catalog import Asset, measure

        return measure(Asset('clip', str(path)))

    def test_a_variable_frame_rate_picture_is_measured_in_time(self):
        """ffprobe's average rate is not a grid: 20 frames in 1.0 s measured 0.867 s.

        The clip is built from two rates (30 fps then 10 fps), which is the shape a
        phone or a screen recorder produces.  Dividing its frame count by ffprobe's
        average rate was **13.3% short** - and a background that believes it is
        shorter than its stage gets looped, an intro stage gets planned too short,
        with nothing in any product saying so.  It is measured in milliseconds from
        its own duration instead, exactly as the picture-length rule already does.
        """
        path = self.root / 'vfr.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
             '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=30:duration=0.5',
             '-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=10:duration=0.5',
             '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]',
             '-vsync', 'vfr', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
        stream = video_stream(path)
        self.assertNotEqual(stream['avg_frame_rate'], stream['r_frame_rate'])
        # What the old rule would have said, for the record: 20 frames / 23.08 fps.
        self.assertLess(float(stream['nb_frames'])
                        / (Fraction(stream['avg_frame_rate'])), 0.9)
        info = self._measure(path)
        self.assertEqual((1, 1000), (info.unit_num, info.unit_den),
                         'a clip with no frame grid is measured in milliseconds')
        self.assertAlmostEqual(1.0, float(info.seconds), delta=0.02)
        self.assertAlmostEqual(video_stream_seconds(path), float(info.seconds),
                               delta=0.001)

    def test_a_non_integer_frame_rate_picture_keeps_its_exact_grid(self):
        """30000/1001 is a rate like any other: exact, with no float in the path.

        The variable-rate guard above must not cost a normal NTSC-rate clip its frame
        grid - 30 frames of 1001/30000 s are exactly 1.001 s.
        """
        path = self.root / 'ntsc.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'testsrc=size=160x90:rate=30000/1001:duration=1.0',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
        stream = video_stream(path)
        self.assertEqual(stream['avg_frame_rate'], stream['r_frame_rate'])
        info = self._measure(path)
        self.assertEqual((30, 1001, 30000),
                         (info.units, info.unit_num, info.unit_den))
        self.assertEqual(Fraction(30030, 30000), info.seconds)


@pytest.mark.acceptance_media
class ArchiveOpusTests(unittest.TestCase):
    """The pre-skip rule against the real files, decoded one by one.

    Marked ``acceptance_media`` because it reads the protected archive; the fixtures
    above guard the same rule on a file the suite builds itself.
    """

    def test_a_stride_of_real_opus_files_agrees_with_its_decoder(self):
        if not ARCHIVE.is_dir():
            self.skipTest('the protected archive is absent')
        checked = 0
        for path in sorted(ARCHIVE.rglob('*.ogg'))[::37]:
            try:
                stream = audio_stream(path)
            except ValueError:
                continue
            if stream.get('codec_name') != 'opus':
                continue
            facts = audio_sample_count(path)
            self.assertEqual(312, opus_pre_skip(path), path)
            self.assertEqual(312, facts.get('priming_samples'), path)
            self.assertEqual(_decoded_samples(path), facts['samples'], path)
            checked += 1
            if checked >= 12:
                break
        self.assertGreaterEqual(checked, 12, 'no Ogg/Opus files were found to check')


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
