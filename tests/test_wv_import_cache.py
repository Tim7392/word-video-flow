"""Importing the recordings that already exist: match, refuse, reuse, zero calls.

This is the **fast** suite, because "随手可跑" is a现场 rule: everything here runs on
a small synthetic cache tree carrying the same shapes as the real archive
(``jianying_original``, ``parallel`` with both route files, a record whose
``timeline_route`` was lost, a record that is not a speech stage, a folder with no
original, a ``local`` folder), so the rules are guarded on every run without
reading 8 GB of archive.

The assertions that genuinely need the whole archive - all 150 recordings of
151-200 match, every choice is the file the fixture points at, and 2310/2310
records agree with the file they name - are marked ``acceptance_media``:

    python -m pytest -m acceptance_media tests/test_wv_import_cache.py

The load-bearing claim is checked in both: the engine is driven through
``build_speech`` with a fake synthesis route that *counts*, so "no TTS call" is
observed rather than assumed, and the suite's network guard is the second lock.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

import pytest

from word_video.contracts import LessonSpec, WordEntry
from word_video.domain import Record
from word_video.importers import cache, store
from word_video.media import sha256
from word_video.tts import build_speech

ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
SLICED = ARCHIVE / '范围151-200-切片渲染' / 'wv-b349c24dbf19ce65bc4e5422' / 'audio-cache'
FIXTURE = Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-有片头.json')

_SCAN = None


def archive_scan():
    """The whole archive, scanned once per process (it is read-only)."""
    global _SCAN
    if _SCAN is None:
        _SCAN = cache.scan_roots([ARCHIVE])
    return _SCAN


class FakeRoute:
    """A synthesis stand-in that records every call and writes real bytes."""

    def __init__(self):
        self.calls = []

    def __call__(self, route, text, role, target, voice=None, resource=None, attempts=3):
        self.calls.append((route, text, role))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b'\x01\x00' * 4800)


def _record(path, seconds=1.0, rate=24000, seed=0):
    """A real (tiny) audio file with content unique to ``seed``.

    Unique content matters: publication is by digest, so two recordings that
    happened to hold the same bytes would legitimately become one asset and the
    counts in these tests would be measuring the fixture rather than the importer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(((seed + index) % 251 + 1) for index in range(2))
                           * int(rate * seconds))
    return path


_SEQUENCE = [0]


def _token(root, name, spec, *, files=('original.mp3',)):
    """One cache folder with its record; ``spec`` ``None`` writes a non-speech one."""
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    for index, file_name in enumerate(files):
        _SEQUENCE[0] += 1
        _record(folder / file_name, 0.5 + index * 0.25, seed=_SEQUENCE[0] * 13)
    if spec is None:
        (folder / 'complete.json').write_text(json.dumps({'batch': {}, 'files': []}),
                                              encoding='utf-8')
    else:
        (folder / 'complete.json').write_text(json.dumps({
            'spec': spec, 'raw': 1.0, 'rendered': 0.8, 'audio_digest': 'x'}),
            encoding='utf-8')
    return folder


class SyntheticTree:
    """A cache tree with every shape the real archive contains."""

    def __init__(self, root):
        self.root = Path(root) / 'audio-cache'
        self.root.mkdir(parents=True, exist_ok=True)
        _token(self.root, 'token-female-apple',
               {'kind': 'jianying_original', 'role': 'female', 'text': 'apple',
                'voice': 'BV503_streaming'})
        _token(self.root, 'token-male-apple',
               {'kind': 'jianying_original', 'role': 'male', 'text': 'apple',
                'voice': 'BV504_streaming'})
        _token(self.root, 'token-female-banana',
               {'kind': 'jianying_original', 'role': 'female', 'text': 'banana',
                'voice': 'BV503_streaming'})
        # The same words from a second voice: a real ambiguity.
        _token(self.root, 'token-female-apple-other',
               {'kind': 'jianying_original', 'role': 'female', 'text': 'apple',
                'voice': 'BV999_streaming'})
        # A parallel folder with both channels: the route decides the file.
        _token(self.root, 'token-male-banana',
               {'kind': 'parallel', 'role': 'male', 'text': 'banana',
                'voice': 'BV504_streaming', 'routes': ['jianying', 'volcengine_legacy'],
                'timeline_route': 'volcengine_legacy'},
               files=('original.jianying.ogg', 'original.volcengine_legacy.ogg'))
        _token(self.root, 'token-chinese-pear',
               {'kind': 'parallel', 'role': 'chinese', 'text': '梨',
                'voice': 'BV406_streaming', 'routes': ['jianying', 'volcengine_legacy'],
                'timeline_route': 'volcengine_legacy'},
               files=('original.jianying.ogg', 'original.volcengine_legacy.ogg'))
        _token(self.root, 'token-chinese-apple',
               {'kind': 'jianying_original', 'role': 'chinese', 'text': '苹果',
                'voice': 'BV406_streaming'})
        # ...and one whose record lost the route: undecidable, reported.  Its text is
        # its own word so the missing route is the only thing this folder tests.
        _token(self.root, 'token-chinese-pear-lost',
               {'kind': 'parallel', 'role': 'chinese', 'text': '无路由词',
                'voice': 'BV406_streaming', 'routes': ['jianying', 'volcengine_legacy']},
               files=('original.jianying.ogg', 'original.volcengine_legacy.ogg'))
        # Two batches recorded the same words in the same voice: still one choice.
        _token(self.root, 'token-female-pear',
               {'kind': 'jianying_original', 'role': 'female', 'text': 'pear',
                'voice': 'BV503_streaming'})
        _token(self.root, 'token-female-pear-again',
               {'kind': 'jianying_original', 'role': 'female', 'text': 'pear',
                'voice': 'BV503_streaming'})
        # A record that is not a speech stage, and a folder with no original.
        _token(self.root, 'token-bookkeeping', None)
        folder = self.root / 'token-no-original'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'complete.json').write_text(json.dumps({
            'spec': {'kind': 'jianying_original', 'role': 'male', 'text': 'pear',
                     'voice': 'BV504_streaming'}}), encoding='utf-8')
        # A local-provider folder: the caller's own audio, nothing to import.  Its
        # text belongs to no record, so it cannot be mistaken for a candidate.
        _token(self.root, 'token-local', {'kind': 'local', 'role': 'chinese',
                                          'text': '仅本地音频',
                                          'voice': 'Energetic Female(English)'})


class ScanTests(unittest.TestCase):
    """Every shape is read, counted and reported - on a tree we control."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory(prefix='wv-cache-')
        cls.root = Path(cls._folder.name)
        SyntheticTree(cls.root)
        cls.scan = cache.scan_roots([cls.root])

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def test_every_folder_is_read_and_accounted_for(self):
        self.assertEqual(13, self.scan.counts['folders'])
        # Two route files are not two recordings: a parallel folder yields exactly
        # one entry, chosen by its record.
        self.assertEqual({'jianying_original', 'parallel', 'local'},
                         {entry.kind for entry in self.scan.entries})
        self.assertEqual(11, len(self.scan.entries) + len(self.scan.ambiguous))
        self.assertEqual(1, self.scan.counts['records_missing'])     # bookkeeping
        self.assertEqual(1, self.scan.counts['originals_missing'])   # no original
        self.assertEqual(1, self.scan.counts['route_undecided'])     # lost route
        self.assertEqual(1, len(self.scan.ambiguous))
        self.assertIn('timeline_route', self.scan.ambiguous[0].reason)

    def test_the_two_route_files_are_told_apart_by_the_record(self):
        entry = next(item for item in self.scan.entries
                     if item.text == 'banana' and item.role == 'male')
        self.assertTrue(entry.path.endswith(cache.ROUTE_SUFFIX['volcengine_legacy']),
                        entry.path)
        # ...and the other channel is a *different* recording.
        other = Path(entry.folder) / 'original.jianying.ogg'
        self.assertNotEqual(sha256(entry.path), sha256(other))

    def test_a_non_speech_record_is_skipped_with_its_reason(self):
        reasons = [item['reason'] for item in self.scan.skipped]
        self.assertIn('record is not a speech stage', reasons)
        self.assertIn('no original.* file for the record', reasons)

    def test_a_missing_root_is_refused(self):
        with self.assertRaises(cache.ImportError_):
            cache.scan_roots([self.root / 'not-here'])


class TextIdentityTests(unittest.TestCase):
    def test_text_is_compared_by_what_is_said(self):
        self.assertEqual(cache.normalize_text('证明；证实'), cache.normalize_text('证明；证实'))
        self.assertEqual(cache.normalize_text(' ａｐｐｌｅ '), 'apple')
        self.assertEqual(cache.normalize_text('line\nbreak'), 'line break')
        self.assertNotEqual(cache.normalize_text('apple'), cache.normalize_text('apples'))

    def test_a_voice_is_one_identity_whether_named_by_id_or_by_name(self):
        self.assertEqual(cache.canonical_voice('BV503_streaming'),
                         cache.canonical_voice('Energetic Female(English)'))
        self.assertEqual(cache.canonical_voice(''), '')
        # An unknown voice keeps its own text: matching it to a known one would be
        # substituting a sound.
        self.assertEqual(cache.canonical_voice('My Custom Voice'), 'My Custom Voice')


class AmbiguityTests(unittest.TestCase):
    def _entry(self, voice, path, role='female', text='apple'):
        return cache.CacheEntry(folder=str(Path(path).parent), role=role, text=text,
                                voice=voice, voice_id=voice, kind='jianying_original',
                                route='jianying', path=str(path))

    def test_two_voices_for_one_word_are_a_question(self):
        left = self._entry('BV503_streaming', r'C:\a\original.mp3')
        right = self._entry('BV999_streaming', r'C:\b\original.mp3')
        chosen, question = cache.resolve_entry([left, right])
        self.assertIsNone(chosen)
        self.assertIsNotNone(question)
        self.assertEqual(('BV503_streaming', 'BV999_streaming'), question.voice_ids)
        self.assertIn('different voices', question.reason)

    def test_a_named_voice_settles_it(self):
        left = self._entry('BV503_streaming', r'C:\a\original.mp3')
        right = self._entry('BV999_streaming', r'C:\b\original.mp3')
        chosen, question = cache.resolve_entry([left, right], 'BV999_streaming')
        self.assertIsNone(question)
        self.assertEqual('BV999_streaming', chosen.voice_id)

    def test_asking_for_a_voice_that_is_not_there_is_refused(self):
        """The cross-account case: an id this machine has no recording for."""
        left = self._entry('BV503_streaming', r'C:\a\original.mp3')
        with self.assertRaises(cache.ImportError_) as caught:
            cache.resolve_entry([left], 'BV777_streaming')
        self.assertIn('BV777_streaming', str(caught.exception))
        self.assertIn('BV503_streaming', str(caught.exception))

    def test_the_same_voice_twice_is_not_an_ambiguity(self):
        left = self._entry('BV503_streaming', r'C:\a\original.mp3')
        right = self._entry('BV503_streaming', r'C:\b\original.mp3')
        chosen, question = cache.resolve_entry([left, right])
        self.assertIsNone(question)
        self.assertEqual(str(Path(r'C:\a\original.mp3')), chosen.path)

    def test_a_word_that_is_not_on_disk_is_missing_not_substituted(self):
        """A wrong word-list entry fails loudly instead of reusing another word."""
        records = [Record(id='w9', word='nonexistentword', phonetic='', meaning='',
                          spoken_meaning='不存在的词', index=9)]
        with tempfile.TemporaryDirectory() as folder:
            SyntheticTree(folder)
            plan = store.plan_import(records, cache.scan_roots([folder]))
        self.assertFalse(plan.ok)
        self.assertEqual(3, len(plan.missing))
        self.assertEqual('nonexistentword', plan.missing[0]['text'])

    def test_an_ambiguous_word_is_reported_and_a_named_voice_settles_it(self):
        records = [Record(id='w1', word='apple', phonetic='', meaning='',
                          spoken_meaning='苹果', index=1)]
        with tempfile.TemporaryDirectory() as folder:
            SyntheticTree(folder)
            scan = cache.scan_roots([folder])
            plan = store.plan_import(records, scan)
            named = store.plan_import(records, scan,
                                      voice_by_role={'female': 'BV999_streaming'})
        self.assertFalse(plan.ok)
        self.assertEqual(1, len(plan.ambiguous))
        self.assertEqual(('BV503_streaming', 'BV999_streaming'),
                         plan.ambiguous[0].voice_ids)
        self.assertTrue(named.ok, named.to_dict())


class MaterializeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory(prefix='wv-synth-')
        cls.root = Path(cls._folder.name)
        SyntheticTree(cls.root)
        cls.scan = cache.scan_roots([cls.root])
        cls.records = [
            Record(id='w1', word='apple', phonetic='', meaning='', spoken_meaning='苹果',
                   index=1),
            Record(id='w2', word='banana', phonetic='', meaning='', spoken_meaning='梨',
                   index=2)]
        # The two ambiguous roles are pinned to a voice here, so these tests are
        # about publishing and re-timing rather than about the question above.
        cls.voices = {'female': 'BV503_streaming', 'male': 'BV504_streaming',
                      'chinese': 'BV406_streaming'}

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def _plan(self):
        plan = store.plan_import(self.records, self.scan, voice_by_role=self.voices)
        self.assertTrue(plan.ok, plan.to_dict())
        return plan

    def test_a_lesson_imports_from_the_tree(self):
        plan = self._plan()
        self.assertEqual(6, len(plan.items))
        self.assertEqual({'female', 'male', 'chinese'},
                         {item['role'] for item in plan.items})
        for item in plan.items:
            self.assertTrue(Path(item['source']).is_file())

    def test_materialize_publishes_by_digest_and_a_second_import_adds_nothing(self):
        plan = self._plan()
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / 'assets'
            first = store.materialize(plan, assets)
            self.assertEqual('local', first.provider['kind'])
            self.assertEqual(6, first.report['published_new'])
            before = {path.name: (path.stat().st_size, sha256(path))
                      for path in assets.iterdir()}
            self.assertEqual(6, len(before))
            for name, (_, digest) in before.items():
                self.assertEqual(digest + Path(name).suffix, name,
                                 'the asset name is not its own digest')

            second = store.materialize(plan, assets)
            after = {path.name: (path.stat().st_size, sha256(path))
                     for path in assets.iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(0, second.report['published_new'])
            self.assertEqual(6, second.report['reused'])
            for item in second.provider['items']:
                self.assertTrue(Path(item['path']).is_file())
                self.assertTrue(Path(item['path']).resolve()
                                .is_relative_to(assets.resolve()))

    def test_the_engine_re_measures_and_never_calls_the_service(self):
        """The load-bearing claim: import -> real speech build with zero calls."""
        plan = self._plan()
        with tempfile.TemporaryDirectory() as folder:
            result = store.materialize(plan, Path(folder) / 'assets')
            lesson = LessonSpec([WordEntry(r.index, r.word, r.phonetic, r.meaning,
                                           r.spoken_meaning) for r in self.records],
                                speed=1.25, fps=60)
            route = FakeRoute()
            with patch('word_video.tts.synthesize_role', side_effect=route):
                speech = build_speech(lesson, result.provider,
                                      Path(folder) / 'audio-cache')
            self.assertEqual([], route.calls)      # the counter is the evidence
            self.assertEqual(6, len(speech))
            for asset in speech:
                self.assertTrue(Path(asset.path).is_file())
                self.assertGreater(asset.duration_s, 0.0)
                self.assertGreater(asset.rendered_duration_s, 0.0)
                self.assertLess(asset.rendered_duration_s, asset.duration_s)

    def test_the_imported_originals_are_the_source_bytes(self):
        """No re-encode on import: the asset is the recording, byte for byte."""
        plan = self._plan()
        with tempfile.TemporaryDirectory() as folder:
            result = store.materialize(plan, Path(folder) / 'assets')
            for item in plan.items:
                asset_id = '%s:%s' % (item['index'], item['role'])
                self.assertEqual(sha256(item['source']),
                                 sha256(result.assets[asset_id]['path']), asset_id)

    def test_the_record_selects_a_file_and_is_never_trusted_for_a_duration(self):
        """Two facts together, because each alone misleads.

        The *chosen* route file's length is what the engine will measure, and the
        two channels of one parallel folder are different recordings - which is why
        the route decides the file and why a recorded length must never be reused as
        a fact about the audio.
        """
        from word_video.media import duration

        entry = next(item for item in self.scan.entries
                     if item.kind == 'parallel' and item.text == 'banana')
        siblings = sorted(Path(entry.folder).glob('original.*'))
        self.assertEqual(2, len(siblings))
        self.assertEqual(2, len({duration(path) for path in siblings}),
                         'the two channels are the same length')
        self.assertEqual(duration(entry.path),
                         duration(Path(entry.folder) / 'original.volcengine_legacy.ogg'))
        plan = self._plan()
        for item in plan.items:
            self.assertNotIn('duration_s', item)
            self.assertNotIn('raw', item)
            self.assertNotIn('rendered', item)


@pytest.mark.acceptance_media
class ArchiveCoverageTests(unittest.TestCase):
    """The whole archive on request: the real 151-200 recordings and their identity.

    Marked ``acceptance_media`` because it reads the read-only archive (32 cache
    roots, 2868 folders) and measures every chosen original; the fast suite above
    guards the same rules on a tree it owns.
    """

    def test_the_real_archive_is_covered_and_every_choice_is_the_fixture_file(self):
        if not SLICED.is_dir():
            self.skipTest('the 151-200 sliced batch is absent')
        scan = archive_scan()
        self.assertEqual(32, len(scan.roots))
        self.assertEqual(2310, len(scan.entries))
        self.assertEqual(558, scan.counts['originals_missing'])
        self.assertEqual(0, scan.counts['route_undecided'])

        timeline = json.loads((SLICED.parent / '0151-0200' / 'timeline.json')
                              .read_text(encoding='utf-8'))
        records = [Record(id='w%d' % word['index'], word=word['word'],
                          phonetic=word['phonetic'], meaning=word['meaning'],
                          spoken_meaning=word['spoken_meaning'], index=word['index'])
                   for word in timeline['words']]
        plan = store.plan_import(records, scan)
        self.assertEqual(150, len(plan.items))
        self.assertEqual(0, len(plan.missing))
        self.assertEqual(0, len(plan.ambiguous))
        fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        wanted = {(item['index'], item['role']): Path(item['path'])
                  for item in fixture['provider']['items']}
        for item in plan.items:
            key = (item['index'], item['role'])
            if key in wanted:
                self.assertEqual(wanted[key], Path(item['source']), item)

    def test_every_record_agrees_with_the_file_it_names(self):
        """2310 of 2310: the old metadata is not the hazard; the file choice is."""
        from word_video.media import duration

        checked = disagreeing = 0
        for entry in archive_scan().entries:
            saved = json.loads((Path(entry.folder) / 'complete.json')
                               .read_text(encoding='utf-8'))
            if 'raw' not in saved:
                continue
            checked += 1
            if abs(float(saved['raw']) - duration(entry.path)) > 0.01:
                disagreeing += 1
        self.assertEqual(2310, checked)
        self.assertEqual(0, disagreeing,
                         '%d records disagree with the file they name' % disagreeing)


if __name__ == '__main__':
    unittest.main()
