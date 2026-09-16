"""A refusal has to say which word, which file, and by how much.

The defect these pin down is a message, not a behaviour: feeding an un-tempoed
recording to ``prepared_speech`` failed with "Speech exceeds allocated timeline
stage" - no word, no role, no file, no sizes - and the most likely cause (the audio
never went through the tempo pass) was invisible.  That is the single most
expensive error in this pipeline to diagnose by hand: 150 stages, 9 files per
batch, and the member has to work out which one is which by searching.

So the counter-example is built from the real mistake, and the assertions name the
fields the message must carry.  Both routes that can hit it are covered: the draft
exporter (where H0 hit it) and the mixer (the same shape one stage later).
"""
import json
from pathlib import Path
import tempfile
import unittest
import wave

from word_video.contracts import LessonSpec, SpeechAsset, WordEntry
from word_video.jobs import status, submit, work
from word_video.media import duration, executable, run
from word_video.render.mix import build_mix
from word_video.timing import build_timeline

RATE = 48000


def _wav(path, seconds, rate=RATE):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\x01\x00' * int(round(seconds * rate)))
    return Path(path)


class DraftMessageTests(unittest.TestCase):
    """The route H0 hit: a raw recording given as prepared_speech."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = _wav(self.root / 'raw-原件.wav', 2.0)      # never tempo-adjusted
        self.background = self.root / 'background.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'color=c=black:s=180x90:r=30:d=1.0', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', str(self.background)])

    def _request(self, *, declared=0.8):
        """A lesson whose stage is 0.8 s long but whose file is 2.0 s.

        That is the real shape of the mistake: the file named in ``prepared_speech``
        is not the file the timeline measured (a raw original handed in where a
        tempo-adjusted one belongs), so the stage is far shorter than the audio.
        """
        word = {'index': 151, 'word': 'demonstrate', 'phonetic': '/x/', 'meaning': 'v. 证明',
                'spoken_meaning': '证明'}
        return {
            'idempotency_key': 'diagnostics-raw-audio',
            'output': str(self.root / 'out'),
            'lesson': {'background': str(self.background), 'width': 180, 'height': 90,
                       'fps': 30, 'speed': 1.25, 'intro_s': 0.1, 'entries': [word]},
            # The real mistake: the *original* file handed in where the prepared
            # WAV belongs, so what the timeline measured and what is on disk differ.
            'prepared_speech': [
                {'word_index': 151, 'role': role, 'text': '证明' if role == 'chinese'
                 else 'demonstrate', 'path': str(self.raw), 'duration_s': declared,
                 'rendered_duration_s': declared, 'voice': 'V'} for role in
                ('female', 'male', 'chinese')],
        }

    def _failed_job(self, request, db_name):
        """Run a job that must fail, and read the message the *job record* holds.

        ``work`` re-raises with the state already recorded as failed, and it is that
        recorded message a member or a support log actually sees - so the message is
        asserted where it is stored, not on the traceback of a call.
        """
        db = str(self.root / db_name)
        job = submit(db, request)['id']
        with self.assertRaises(ValueError):
            work(db, job)
        recorded = status(db, job)
        self.assertEqual('failed', recorded['state'])
        return recorded

    def test_the_error_names_the_word_the_role_the_file_and_the_sizes(self):
        recorded = self._failed_job(self._request(), 'jobs.sqlite3')
        message = recorded['error']
        self.assertIn('word 151', message)
        self.assertIn('female', message)
        self.assertIn('raw-原件.wav', message)
        # Both lengths, and how far apart they are.
        self.assertIn('2.000s', message)
        self.assertIn('0.800s', message)
        self.assertIn('too long', message)
        # The route that does the tempo pass is named, and the ratio is printed.
        self.assertIn('provider.kind=local', message)
        self.assertIn('2.50x', message)

    def test_no_output_is_published_when_it_refuses(self):
        """Nothing is committed, so a member cannot mistake the失败批次 for a delivery."""
        self._failed_job(self._request(), 'jobs2.sqlite3')
        self.assertEqual([], list((self.root / 'out').rglob('*.mp4')))
        self.assertEqual([], list((self.root / 'out').rglob('complete.json')),
                         'a failed batch left a completion record behind')

    def test_a_correctly_prepared_file_still_passes(self):
        """The message must not appear when the audio really was processed.

        The prepared file is the real thing: 1.25x of the 2 s original, which is
        exactly the stage the timeline reserves for it.
        """
        prepared = self.root / 'prepared.wav'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(self.raw),
             '-af', 'atempo=1.25', '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le',
             str(prepared)])
        prepared_seconds = duration(prepared)
        request = self._request(declared=prepared_seconds)
        request['idempotency_key'] = 'diagnostics-prepared-audio'
        request['output'] = str(self.root / 'out2')
        for item in request['prepared_speech']:
            item['path'] = str(prepared)
        db = str(self.root / 'jobs3.sqlite3')
        job = submit(db, request)['id']
        state = work(db, job)
        self.assertEqual('generated', state['state'], state.get('error'))


class MixMessageTests(unittest.TestCase):
    """The same shape one stage later, in the mixer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _manifest(self, seconds=2.0, text='apple'):
        """The same mismatch at the mixer: a 2 s file in a 0.8 s stage."""
        word = WordEntry(1, text, '/x/', 'n. 苹果', '苹果')
        lesson = LessonSpec([word], width=180, height=90, fps=30, intro_s=0.1,
                            background=str(self.root / 'unused.mp4'))
        asset = _wav(self.root / ('%s.wav' % text), seconds)
        assets = [SpeechAsset(1, role, '苹果' if role == 'chinese' else text,
                              str(asset), 0.8, 0.8, 'V')
                  for role in ('female', 'male', 'chinese')]
        return build_timeline(lesson, assets)

    def test_the_mixer_names_the_stage_the_file_and_the_shortfall(self):
        manifest = self._manifest()
        with self.assertRaises(ValueError) as caught:
            build_mix(manifest, self.root / 'mix.wav')
        message = str(caught.exception)
        self.assertIn('word 1', message)
        self.assertIn('female', message)
        self.assertIn('apple.wav', message)
        self.assertIn('2.000s', message)
        self.assertIn('0.800s', message)
        self.assertIn('too long', message)
        self.assertIn('provider.kind=local', message)

    def test_the_second_word_is_named_when_it_is_the_one_that_fails(self):
        """A one-word lesson cannot catch a message that always says "word 1"."""
        from word_video.contracts import SpeechAsset as Asset

        words = [WordEntry(1, 'apple', '/a/', 'n. 苹果', '苹果'),
                 WordEntry(2, 'banana', '/b/', 'n. 香蕉', '香蕉')]
        lesson = LessonSpec(words, width=180, height=90, fps=30, intro_s=0.1,
                            background=str(self.root / 'unused.mp4'))
        short = _wav(self.root / 'short.wav', 0.5)
        long = _wav(self.root / 'long.wav', 2.0)
        assets = []
        for entry in words:
            for role in ('female', 'male', 'chinese'):
                path = long if entry.index == 2 else short
                assets.append(Asset(entry.index, role,
                                    entry.spoken_meaning if role == 'chinese' else entry.word,
                                    str(path), 0.6, 0.6, 'V'))
        manifest = build_timeline(lesson, assets)
        with self.assertRaises(ValueError) as caught:
            build_mix(manifest, self.root / 'mix2.wav')
        message = str(caught.exception)
        self.assertIn('word 2', message)
        self.assertIn('long.wav', message)
        self.assertNotIn('word 1', message)


if __name__ == '__main__':
    unittest.main()
