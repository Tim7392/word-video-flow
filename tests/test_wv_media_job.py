"""A whole job, offline, on the real cached audio and four intro combinations.

Unit tests can agree with each other and still leave the delivered files wrong:
the mixer, the draft exporter and the job report each used to answer "where does
the intro's sound come from?" on their own, and three answers that disagree are
invisible until someone listens to the MP4 and opens the draft.

So this runs the real pipeline - ``jobs.submit`` then ``jobs.work`` - on the nine
**real** cached voice files the p1 fixtures point at, with a local provider, on a
small canvas, and reads the delivered artifacts back:

* the MP4's audio envelope, so "the intro is audible" is measured and not assumed;
* the draft's track list, so the editable project describes the same scene;
* the job report, so what the member is told matches both.

No service is contacted: the provider is local files, and the suite's offline
guard would fail the test if anything tried.  The canvas is 320x180 at 24 fps
purely to keep the run fast; nothing about the media chain depends on the size.
"""
import json
from pathlib import Path
import tempfile
import unittest
import wave

from word_video.contracts import WordEntry
from word_video.jobs import status, submit, work
from word_video.media import executable, has_audio, run

FIXTURES = Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures')
ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
CLIP = ARCHIVE / '_prepared-1080p' / '4级1500开头_透明通道-1080p.mov'
BACKGROUND = ARCHIVE / '_prepared-1080p' / 'background-from-reference-1080p.mp4'
CANVAS = {'width': 320, 'height': 180}
FPS = 24


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def _entries(items):
    """The three words of the fixture, with their spoken text as the audio says."""
    words = {}
    for item in items:
        words.setdefault(item['index'], {})[item['role']] = item
    entries = []
    for index in sorted(words):
        roles = words[index]
        entries.append(WordEntry(index, roles['female']['text'], '/x/',
                                 'n. 测试', roles['chinese']['text']))
    return entries


def _local_provider(items):
    return {'kind': 'local', 'concurrency': 4,
            'items': [dict(item) for item in items]}


class PipelineTests(unittest.TestCase):
    """Submit and run one job per intro combination, then read the artifacts."""

    @classmethod
    def setUpClass(cls):
        if not (FIXTURES.is_dir() and CLIP.is_file() and BACKGROUND.is_file()):
            raise unittest.SkipTest('p1 fixtures or the 1080p reference media are absent')
        cls._folder = tempfile.TemporaryDirectory(prefix='wv-e2e-')
        cls.root = Path(cls._folder.name)
        cls.background = cls._small_background(cls.root / 'background.mp4')
        cls.silent_clip = cls._silent_clip(cls.root / 'silent-intro.mov')

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    @staticmethod
    def _small_background(target):
        """The real background, decoded once into a small canvas for speed."""
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(BACKGROUND),
             '-t', '12', '-vf', 'scale=320:180', '-r', str(FPS), '-an',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
             '-pix_fmt', 'yuv420p', str(target)], timeout=600)
        return target

    @staticmethod
    def _silent_clip(target):
        """The reference clip's picture with its soundtrack removed.

        Kept in the same container and codec as the reference - qtrle/argb in a
        QuickTime file - so this really is "the same countdown clip, silent" and
        not a different kind of asset.
        """
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(CLIP),
             '-t', '1.0', '-an', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(target)])
        if has_audio(target):
            raise AssertionError('the silent clip still carries audio')
        return target

    def _run_job(self, key, intro=None, intro_audio=''):
        fixture = _fixture('p1-有片头.json')
        items = fixture['provider']['items']
        lesson = dict(CANVAS, background=str(self.background), speed=1.25,
                      batch_size=50, fps=FPS,
                      entries=[dict(vars(entry)) for entry in _entries(items)])
        if intro is not None:
            lesson['intro'] = {'video': str(intro)}
        if intro_audio:
            lesson['intro_audio'] = intro_audio
        request = {'idempotency_key': key,
                   'output': str(self.root / 'out'),
                   'lesson': lesson,
                   'provider': _local_provider(items)}
        db = str(self.root / ('jobs-%s.sqlite3' % key))
        job = submit(db, request)['id']
        state = work(db, job)
        self.assertEqual('generated', state['state'], state.get('error'))
        return state

    def test_clean_audio_and_intro_combination(self):
        """The clip carries its own countdown sound: it is what plays."""
        state = self._run_job('intro-with-sound', intro=CLIP)
        result = state['result']
        self.assertEqual('FROM_CLIP', result['intro_sound'])
        batch = result['batches'][0]
        self.assertTrue(batch['video_check']['structural_ok'])
        self.assertTrue(batch['draft']['structural_ok'])
        published = {Path(item['path']).name for item in result['files']}
        self.assertIn('video.mp4', published)
        self.assertIn('mix.wav', published)
        self.assertIn('captions.ass', published)
        self.assertEqual(5, len([name for name in published if name.endswith('.srt')]))
        manifest = json.loads((Path(batch['draft']['draft']).parent
                               / 'timeline.json').read_text(encoding='utf-8'))
        self.assertEqual(str(CLIP), manifest['intro_video'])
        self._assert_intro_audible(state, expected=True)

    def test_a_silent_clip_plays_silence_and_says_so(self):
        """A clip with no audio track is valid input, and is reported honestly."""
        state = self._run_job('intro-silent', intro=self.silent_clip)
        self.assertEqual('SILENT_CLIP_NO_INTRO_AUDIO', state['result']['intro_sound'])
        self._assert_intro_audible(state, expected=False)

    def test_no_intro_at_all_is_reported_as_not_configured(self):
        state = self._run_job('intro-absent')
        self.assertEqual('NOT_CONFIGURED', state['result']['intro_sound'])
        self._assert_intro_audible(state, expected=False)

    def test_a_configured_file_is_used_when_there_is_no_clip(self):
        """The one case where an external file is the intro, and it is marked
        as not auditioned rather than presented as approved."""
        sound = self.root / 'countdown.wav'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=660:duration=0.5', '-ar', '48000', '-ac', '1',
             str(sound)])
        state = self._run_job('intro-file', intro_audio=str(sound))
        result = state['result']
        self.assertEqual('FROM_INTRO_AUDIO_NOT_AUDITIONED', result['intro_sound'])
        self._assert_intro_audible(state, expected=True)

    def _assert_intro_audible(self, state, expected):
        """Decode the delivered MP4's own audio and look at the intro's seconds."""
        batch = state['result']['batches'][0]
        video = Path(batch['video'])
        manifest = json.loads((Path(batch['draft']['draft']).parent
                               / 'timeline.json').read_text(encoding='utf-8'))
        # One decode target per job: sharing a name across the cases in this class
        # made the second case analyse the first case's audio during development.
        decoded = video.parent / ('decoded-%s.wav' % state['id'])
        decoded.unlink(missing_ok=True)
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(video),
             '-vn', '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', str(decoded)])
        with wave.open(str(decoded), 'rb') as stream:
            block = stream.readframes(stream.getnframes())
        samples = [int.from_bytes(block[i:i + 2], 'little', signed=True)
                   for i in range(0, len(block), 2)]
        window = manifest['intro_frames'] * 48000 // manifest['fps']
        intro_peak = max(abs(value) for value in samples[:window]) if window else 0
        body = samples[window:]
        body_peak = max(abs(value) for value in body)
        if expected:
            self.assertGreater(intro_peak, 500, 'the delivered intro region is silent')
        else:
            self.assertEqual(0, intro_peak, 'a silent intro became audible')
        # The lesson body is audible either way, so "silent intro" cannot be
        # satisfied by an export that lost all of its audio.
        self.assertGreater(body_peak, 500)
        decoded.unlink(missing_ok=True)
        # And the question the member actually asks: does the draft describe the
        # same intro the MP4 plays?
        draft = json.loads((Path(batch['draft']['draft']) / 'draft_content.json')
                           .read_text(encoding='utf-8'))
        tracks = {track['name']: track for track in draft['tracks']}
        segments = tracks['片头音效']['segments']
        from_clip = bool(manifest['intro_video']) and has_audio(manifest['intro_video'])
        used_file = bool(manifest['intro_audio']) and not from_clip
        self.assertEqual(1 if (from_clip or used_file) else 0, len(segments))
        if segments:
            self.assertEqual(0, segments[0]['target_timerange']['start'])


class ArchiveReadOnlyTests(unittest.TestCase):
    """The reference archive is evidence: an acceptance run must not touch it."""

    def test_the_archive_is_untouched_by_the_pipeline(self):
        if not ARCHIVE.is_dir():
            raise unittest.SkipTest('reference archive is absent')
        before = _archive_fingerprint()
        self.assertTrue(before)
        # A one-word job that reads the archive's cached audio and reference clip.
        fixture = _fixture('p1-有片头.json')
        items = fixture['provider']['items'][:3]
        with tempfile.TemporaryDirectory(prefix='wv-archive-') as folder:
            root = Path(folder)
            background = root / 'bg.mp4'
            run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
                 '-i', 'color=c=black:s=160x90:r=%d:d=0.5' % FPS, '-c:v', 'libx264',
                 '-pix_fmt', 'yuv420p', str(background)])
            request = {'idempotency_key': 'archive-readonly',
                       'output': str(root / 'out'),
                       'lesson': {'background': str(background), 'width': 160,
                                  'height': 90, 'fps': FPS, 'speed': 1.25,
                                  'intro': {'video': str(CLIP)},
                                  'entries': [vars(entry) for entry in _entries(items)]},
                       'provider': _local_provider(items)}
            db = str(root / 'jobs.sqlite3')
            job = submit(db, request)['id']
            self.assertEqual('generated', work(db, job)['state'])
        self.assertEqual(before, _archive_fingerprint(),
                         'the pipeline wrote into the read-only reference archive')


def _archive_fingerprint():
    """Name, size and mtime of the reference media the pipeline reads."""
    entries = {}
    for path in sorted(ARCHIVE.glob('_prepared-1080p/*')):
        if path.is_file():
            info = path.stat()
            entries[path.name] = (info.st_size, info.st_mtime_ns)
    for item in _fixture('p1-有片头.json')['provider']['items']:
        path = Path(item['path'])
        if path.is_file():
            info = path.stat()
            entries[str(path)] = (info.st_size, info.st_mtime_ns)
    return entries


if __name__ == '__main__':
    unittest.main()
