"""The intro's sound: one policy, so the MP4 and the draft cannot disagree.

The defect these tests pin down is a *silent* one: the mixer preferred the intro
clip's own soundtrack while the draft exporter preferred ``intro_audio``.  A
lesson that configured both produced an MP4 whose countdown came from the clip
and a draft whose ``片头音效`` segment was a different file, and neither artifact
recorded that they differed.  Nothing crashed, and every existing test passed.

The four combinations below are the whole input space:

===========================  ==========================================
clip with its own audio      the clip sounds, a configured file is not used
clip with no audio track     silent, unless a configured file supplies sound
no clip, only intro seconds  only a configured file can sound
neither                      silent
===========================  ==========================================

Every case asserts on decoded samples, not on "no exception": the sample that
must be audible is read back out of the mix, and the region that must stay
silent is compared with exact zero bytes.
"""
from pathlib import Path
import tempfile
import unittest
import wave

from word_video.contracts import LessonSpec, SpeechAsset, WordEntry
from word_video.media import executable, resolve_intro_audio, run
from word_video.render.mix import build_mix
from word_video.timing import build_timeline

RATE = 48000


def _wav(path, samples, rate=RATE):
    """Write mono s16 PCM from an iterable of sample values."""
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b''.join(
            int(value).to_bytes(2, 'little', signed=True) for value in samples))
    return Path(path)


def _constant(path, value, count, rate=RATE):
    return _wav(path, [value] * count, rate)


def _read(path):
    with wave.open(str(path), 'rb') as stream:
        return stream.getnframes(), stream.readframes(stream.getnframes())


def _samples(path):
    _, block = _read(path)
    return [int.from_bytes(block[i:i + 2], 'little', signed=True)
            for i in range(0, len(block), 2)]


# ``aevalsrc`` takes a *normalised* expression, so a level the tests can compare
# with a number is ``level / 32767`` (written inline: a shared helper would make
# the level invisible at the call site).
def _clip(root, name, seconds, level=None, rate=RATE):
    """A tiny reference-style clip: video, plus an audio track when ``level``.

    The soundtrack is a DC level rather than a tone so a decoded sample can be
    compared with a number: a tone would make "one intro" and "the same intro
    twice" indistinguishable without a spectrum.
    """
    path = Path(root) / name
    args = [executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
            '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=60:d=%.3f' % seconds]
    if level is not None:
        args += ['-f', 'lavfi', '-i',
                 'aevalsrc=%.6f:s=%d:d=%.3f' % (level / 32767.0, rate, seconds),
                 '-shortest']
    else:
        args += ['-an']
    args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']
    if level is not None:
        args += ['-c:a', 'aac', '-b:a', '128k']
    run(args + [str(path)])
    return str(path)


def _extract(root, name, seconds, level):
    """A standalone WAV of ``seconds`` at a constant level, for ``intro_audio``.

    Built through the same encoder as the clips so the two paths cannot differ
    in some way the test does not notice.  ffmpeg is run with ``-n``, so an
    existing file is an error rather than a silent overwrite.
    """
    clip = _clip(root, name + '.mp4', seconds, level=level)
    path = Path(root) / (name + '.wav')
    path.unlink(missing_ok=True)
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', clip,
         '-vn', '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', str(path)])
    return str(path)


class IntroAudioPolicyTests(unittest.TestCase):
    """The resolver decides; every other module asks it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fallback = _constant(self.root / 'fallback.wav', 9000, RATE)

    def test_clip_with_sound_wins_and_the_fallback_is_not_used(self):
        clip = _clip(self.root, 'talking.mp4', 0.2, level=15000)
        choice = resolve_intro_audio(clip, None, str(self.fallback))
        self.assertEqual(clip, choice.path)
        self.assertTrue(choice.from_clip)
        self.assertEqual('FROM_CLIP', choice.source)
        self.assertAlmostEqual(0.2, choice.duration_s, places=2)

    def test_silent_clip_falls_back_to_the_configured_file(self):
        clip = _clip(self.root, 'mute.mp4', 0.2, level=None)
        choice = resolve_intro_audio(clip, None, str(self.fallback))
        self.assertEqual(str(self.fallback), choice.path)
        self.assertFalse(choice.from_clip)
        self.assertEqual('FROM_INTRO_AUDIO', choice.source)
        self.assertAlmostEqual(1.0, choice.duration_s, places=3)

    def test_no_clip_uses_the_configured_file(self):
        choice = resolve_intro_audio(None, None, str(self.fallback))
        self.assertEqual('FROM_INTRO_AUDIO', choice.source)

    def test_nothing_configured_is_silent_not_an_error(self):
        choice = resolve_intro_audio(None, None, '')
        self.assertTrue(choice.silent)
        self.assertEqual(0.0, choice.duration_s)
        self.assertEqual('SILENT_CLIP_NO_INTRO_AUDIO', choice.source)

    def test_a_missing_fallback_file_is_reported_not_treated_as_silence(self):
        with self.assertRaises(FileNotFoundError):
            resolve_intro_audio(None, None, str(self.root / 'typo.wav'))


class IntroMixTests(unittest.TestCase):
    """What actually reaches the mixer, decoded back from the published WAV."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.word = _constant(self.root / 'spoken.wav', 100, RATE // 10)
        self.fallback = _constant(self.root / 'fallback.wav', 9000, RATE // 5)

    def _manifest(self, clip=None, intro_audio='', intro_s=0.2, fps=60):
        entry = WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')
        lesson = LessonSpec([entry], width=160, height=90, fps=fps, intro_s=intro_s,
                            background=self._background(),
                            intro={'video': clip} if clip else {},
                            intro_audio=intro_audio)
        assets = [SpeechAsset(1, role, '苹果' if role == 'chinese' else 'apple',
                              str(self.word), .1, .1, 'test')
                  for role in ('female', 'male', 'chinese')]
        return build_timeline(lesson, assets)

    def _background(self):
        """A plain background clip, rebuilt for the calling test.

        Never cached on ``self``: unittest may hand the same instance to several
        test methods, and a cached path then points at the previous test's
        temporary directory - which is how an unrelated 0.2 s clip turned up as
        this test's background during development.
        """
        path = Path(self.root) / 'bg.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'color=c=blue:s=160x90:r=60:d=0.5', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', str(path)])
        return str(path)

    def _mix(self, name, **kwargs):
        manifest = self._manifest(**kwargs)
        target = self.root / name
        build_mix(manifest, target)
        return manifest, target

    def test_clip_with_sound_is_the_only_intro_source(self):
        """Both configured: the clip sounds, the file does not - exactly once."""
        clip = _clip(self.root, 'talking.mp4', 0.2, level=15000)
        manifest, mix = self._mix('clip-mix.wav', clip=clip, intro_audio=str(self.fallback))
        frames, _ = _read(mix)
        self.assertEqual(manifest.total_frames * 800, frames)
        intro = _samples(mix)[:manifest.intro_frames * 800]
        self.assertTrue(any(value != 0 for value in intro), 'intro is silent')
        # The clip's soundtrack is +15000 and the fallback file is +9000.  Both
        # sounding would read about 24000 (and clip at the s16 ceiling); the
        # clip alone must stay near its own level.
        self.assertLessEqual(max(intro), 18000)
        self.assertGreaterEqual(min(intro), 12000)

    def test_silent_clip_with_fallback_sounds_once_and_stays_in_the_intro(self):
        """A silent clip still shows; the file it falls back to sets the length.

        The picture of a silent clip does not bound the fallback file: the
        background is already running behind the intro, so a countdown sting
        longer than this particular clip is not an error and must not be cut.
        """
        clip = _clip(self.root, 'mute.mp4', 0.2, level=None)
        sound = _extract(self.root, 'fallback', 1.0, level=9000)
        manifest, mix = self._mix('mute-mix.wav', clip=clip, intro_audio=sound)
        window = manifest.intro_frames * 800
        # 1.002667 s at 60 fps covers 61 frames; the file has 48128 samples, so
        # the 60th frame is filled from the file and the 128 leftover samples are
        # frame rounding, not a reason to fail or to cut the sound.
        self.assertEqual(61, manifest.intro_frames)
        values = _samples(mix)
        intro = values[:window]
        self.assertGreater(max(abs(value) for value in intro), 1000)
        self.assertLessEqual(max(intro), 12000)
        # The intro must not bleed past its own stage: just after it the level
        # drops to the first word's, not the fallback's 9000.
        tail = values[window:window + 800]
        self.assertLessEqual(max(abs(value) for value in tail), 1000)

    def test_no_clip_with_fallback_keeps_the_configured_length(self):
        """Without a clip, the sound file owns the intro - not ``intro_s``.

        Reading the intro length from the clip alone means a lesson with no clip
        reserves ``intro_s`` and then has nowhere to put a longer file: the
        countdown would be cut.  The intro must be as long as what plays.
        """
        sound = _extract(self.root, 'longer', 1.0, level=9000)
        manifest, mix = self._mix('file-only-mix.wav', intro_audio=sound,
                                  intro_s=0.05)
        # The file really is 1.0 s and the manifest must have grown to it, not
        # kept the 0.05 s it was told: `intro_s` is only a placeholder length.
        self.assertEqual(61, manifest.intro_frames)
        self.assertGreater(manifest.total_frames, manifest.intro_frames)
        values = _samples(mix)
        self.assertGreater(max(abs(value) for value in values[:manifest.intro_frames * 800]),
                           1000)
        # The word body still follows the longer intro, so nothing was pushed off
        # the timeline: the last word's stage carries its own speech.
        tail_start = manifest.words[-1]['chinese_frame'] * 800
        self.assertGreater(max(abs(value) for value in values[tail_start:]), 0)

    def test_silent_clip_and_no_file_leaves_the_intro_silent(self):
        clip = _clip(self.root, 'mute2.mp4', 0.2, level=None)
        manifest, mix = self._mix('silent-mix.wav', clip=clip)
        values = _samples(mix)
        window = manifest.intro_frames * 800
        self.assertEqual([0] * window, values[:window])
        self.assertTrue(any(value != 0 for value in values[window:]))

    def test_no_clip_and_no_file_is_silent_and_keeps_intro_s(self):
        manifest, mix = self._mix('plain-mix.wav', intro_s=0.1)
        self.assertEqual(6, manifest.intro_frames)
        values = _samples(mix)
        self.assertEqual([0] * (manifest.intro_frames * 800),
                         values[:manifest.intro_frames * 800])

    def test_intro_sound_shorter_than_the_stage_is_refused_not_trimmed(self):
        """A truncated countdown must fail the job, never lose its tail."""
        clip = _clip(self.root, 'short.mp4', 0.2, level=15000)
        manifest = self._manifest(clip=clip)
        # A caller (or a stale manifest) that reserves more than the sound has.
        manifest.intro_frames += 60
        manifest.total_frames += 60
        manifest.words[0]['start_frame'] += 60
        for item in manifest.audio:
            item['start_frame'] += 60
        with self.assertRaises(ValueError) as caught:
            build_mix(manifest, self.root / 'too-long.wav')
        self.assertIn('shorter than the intro stage', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
