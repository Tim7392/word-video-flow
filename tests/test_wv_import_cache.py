"""Importing the recordings that already exist: match, refuse, reuse, prove zero calls.

The numbers here are the real archive - 32 cache roots, 2868 token folders, 2310
usable originals - so the suite answers the question W07 asks ("can a member
actually reuse the old audio?") with measurements rather than a fixture that was
built to pass.

The load-bearing assertion is ``route.assert_not_called()``: the engine is driven
through ``build_speech`` with a fake synthesis route that *counts*, so "no TTS
call" is observed rather than assumed.  The suite's own network guard would also
fail a real connection, which is the second lock on the same door.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

from word_video.contracts import LessonSpec, WordEntry
from word_video.domain import Record
from word_video.importers import cache, store
from word_video.media import sha256
from word_video.tts import build_speech

ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
SLICED = (ARCHIVE / '范围151-200-切片渲染' / 'wv-b349c24dbf19ce65bc4e5422' / 'audio-cache')

_SCAN = None
_SCAN_SECONDS = 0.0


def archive_scan():
    """Scan the whole archive once; it is read-only and does not change."""
    global _SCAN, _SCAN_SECONDS
    if _SCAN is None:
        started = time.monotonic()
        _SCAN = cache.scan_roots([ARCHIVE])
        _SCAN_SECONDS = round(time.monotonic() - started, 1)
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


class ScanTests(unittest.TestCase):
    def test_every_cache_root_is_read_and_counted(self):
        scan = archive_scan()
        self.assertEqual(32, len(scan.roots))
        self.assertEqual(2868, scan.counts['folders'])
        self.assertEqual(0, scan.counts['records_missing'])
        self.assertEqual(0, scan.counts['records_unreadable'])
        # The 558 folders with no original are the `local` provider's: the caller
        # supplied that audio, so there is nothing of the service's to re-import.
        self.assertEqual(558, scan.counts['originals_missing'])
        self.assertEqual(2310, len(scan.entries))
        self.assertEqual(558, len(scan.skipped))

    def test_the_two_route_files_are_told_apart_by_the_record(self):
        """A parallel folder holds both channels; the record says which one played."""
        scan = archive_scan()
        routes = {entry.route for entry in scan.entries}
        self.assertEqual({'jianying', 'volcengine_legacy', ''}, routes)
        parallel = [entry for entry in scan.entries if entry.kind == 'parallel']
        self.assertTrue(parallel)
        for entry in parallel:
            expected = cache.ROUTE_SUFFIX[entry.route]
            self.assertTrue(entry.path.endswith(expected), entry.path)
        legacy = [entry for entry in parallel if entry.route == 'volcengine_legacy']
        self.assertEqual(1378, len(legacy))

    def test_a_parallel_folder_without_a_route_is_ambiguous_not_guessed(self):
        """The counter-example: strip the route and the folder must be reported."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'audio-cache'
            token = root / 'deadbeef'
            token.mkdir(parents=True)
            for name in ('original.jianying.ogg', 'original.volcengine_legacy.ogg'):
                (token / name).write_bytes(b'not really audio')
            (token / 'complete.json').write_text(json.dumps({
                'spec': {'kind': 'parallel', 'role': 'female', 'text': 'apple',
                         'voice': 'BV503_streaming',
                         'routes': ['jianying', 'volcengine_legacy']},
                'raw': 1.0, 'rendered': 0.8, 'audio_digest': 'x'}), encoding='utf-8')
            scan = cache.scan_roots([root])
            self.assertEqual((), scan.entries)
            self.assertEqual(1, len(scan.ambiguous))
            self.assertIn('timeline_route', scan.ambiguous[0].reason)
            self.assertEqual(1, scan.counts['route_undecided'])

    def test_a_folder_without_an_original_is_reported_not_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'audio-cache'
            token = root / 'cafe'
            token.mkdir(parents=True)
            (token / 'complete.json').write_text(json.dumps({
                'spec': {'kind': 'jianying_original', 'role': 'male', 'text': 'apple',
                         'voice': 'BV504_streaming'}}), encoding='utf-8')
            scan = cache.scan_roots([root])
            self.assertEqual((), scan.entries)
            self.assertEqual(1, scan.counts['originals_missing'])
            self.assertIn('no original.* file', scan.skipped[0]['reason'])

    def test_a_record_that_is_not_a_speech_stage_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'audio-cache'
            token = root / 'batch'
            token.mkdir(parents=True)
            (token / 'original.mp3').write_bytes(b'x')
            # A job's bookkeeping record lives at the cache root in one old batch;
            # it must be skipped with a reason instead of crashing the scan.
            (token / 'complete.json').write_text(json.dumps(
                {'batch': {'first': 1}, 'files': []}), encoding='utf-8')
            scan = cache.scan_roots([root])
            self.assertEqual((), scan.entries)
            self.assertEqual(1, scan.counts['records_missing'])
            self.assertIn('not a speech stage', scan.skipped[0]['reason'])


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
        records = [Record(id='w9', word='nonexistentword', phonetic='', meaning='',
                          spoken_meaning='不存在的词', index=9)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'audio-cache'
            (root / 'token').mkdir(parents=True)
            entry_path = root / 'token' / 'original.mp3'
            entry_path.write_bytes(b'x')
            (root / 'token' / 'complete.json').write_text(json.dumps({
                'spec': {'kind': 'jianying_original', 'role': 'female', 'text': 'apple',
                         'voice': 'BV503_streaming'}}), encoding='utf-8')
            plan = store.plan_import(records, cache.scan_roots([root]))
        self.assertFalse(plan.ok)
        self.assertEqual(3, len(plan.missing))
        self.assertEqual('nonexistentword', plan.missing[0]['text'])
        with self.assertRaises(cache.ImportError_):
            store.materialize(plan, Path(tempfile.gettempdir()) / 'never-used')


class MaterializeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scan = archive_scan()
        if not SLICED.is_dir():
            raise unittest.SkipTest('the 151-200 sliced batch is absent')
        cls.records = cls._records_from_batch()

    @classmethod
    def _records_from_batch(cls):
        """The three words of the sliced batch, from its own timeline (read-only)."""
        timeline = json.loads((SLICED.parent / '0151-0200' / 'timeline.json')
                              .read_text(encoding='utf-8'))
        records = []
        for word in timeline['words'][:3]:
            records.append(Record(id='w%d' % word['index'], word=word['word'],
                                  phonetic=word['phonetic'], meaning=word['meaning'],
                                  spoken_meaning=word['spoken_meaning'],
                                  index=word['index']))
        return records

    def test_three_roles_import_from_the_real_archive(self):
        plan = store.plan_import(self.records, self.scan)
        self.assertTrue(plan.ok, plan.to_dict())
        self.assertEqual(9, len(plan.items))
        by_role = {}
        for item in plan.items:
            by_role.setdefault(item['role'], []).append(item)
        self.assertEqual({'female', 'male', 'chinese'}, set(by_role))
        for role, items in by_role.items():
            self.assertEqual(3, len(items), role)
        # The chosen files really are the ones the fixture points at.
        fixture = json.loads(Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures'
                                  r'\p1-有片头.json').read_text(encoding='utf-8'))
        wanted = {(item['index'], item['role']): Path(item['path'])
                  for item in fixture['provider']['items']}
        for item in plan.items:
            self.assertEqual(wanted[(item['index'], item['role'])],
                             Path(item['source']), item)

    def test_materialize_publishes_by_digest_and_a_second_import_adds_nothing(self):
        plan = store.plan_import(self.records, self.scan)
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / 'assets'
            first = store.materialize(plan, assets)
            self.assertTrue(first.provider['items'])
            self.assertEqual('local', first.provider['kind'])
            self.assertEqual(9, first.report['published_new'])
            before = {path.name: (path.stat().st_size, sha256(path))
                      for path in assets.iterdir()}
            self.assertEqual(9, len(before))
            for name, (_, digest) in before.items():
                self.assertEqual(digest + Path(name).suffix, name,
                                 'the asset name is not its own digest')
            usage = sum(size for size, _ in before.values())

            second = store.materialize(plan, assets)
            after = {path.name: (path.stat().st_size, sha256(path))
                     for path in assets.iterdir()}
            self.assertEqual(before, after)
            self.assertEqual(0, second.report['published_new'])
            self.assertEqual(9, second.report['reused'])
            self.assertEqual(usage, sum(size for size, _ in after.values()))
            # Every provider item points at a published asset, not at the archive.
            for item in second.provider['items']:
                self.assertTrue(Path(item['path']).is_file())
                self.assertTrue(Path(item['path']).resolve()
                                .is_relative_to(assets.resolve()))

    def test_the_engine_re_measures_and_never_calls_the_service(self):
        """The load-bearing claim: import -> real speech build with zero calls."""
        plan = store.plan_import(self.records, self.scan)
        with tempfile.TemporaryDirectory() as folder:
            result = store.materialize(plan, Path(folder) / 'assets')
            lesson = LessonSpec([WordEntry(r.index, r.word, r.phonetic, r.meaning,
                                           r.spoken_meaning) for r in self.records],
                                speed=1.25, fps=60)
            route = FakeRoute()
            with patch('word_video.tts.synthesize_role', side_effect=route):
                speech = build_speech(lesson, result.provider,
                                      Path(folder) / 'audio-cache')
            # The service route was never reached: the counter is the evidence.
            self.assertEqual([], route.calls)
            self.assertEqual(9, len(speech))
            for asset in speech:
                self.assertTrue(Path(asset.path).is_file())
                self.assertGreater(asset.duration_s, 0.0)
                self.assertGreater(asset.rendered_duration_s, 0.0)
                # The engine's own measurement, not the old record's numbers.
                self.assertLess(asset.rendered_duration_s, asset.duration_s)

    def test_the_imported_originals_are_the_archive_bytes(self):
        """No re-encode on import: the asset is the recording, byte for byte."""
        plan = store.plan_import(self.records, self.scan)
        with tempfile.TemporaryDirectory() as folder:
            result = store.materialize(plan, Path(folder) / 'assets')
            for item in plan.items:
                asset_id = '%s:%s' % (item['index'], item['role'])
                asset = result.assets[asset_id]
                self.assertEqual(sha256(item['source']),
                                 sha256(asset['path']), asset_id)

    def test_the_record_names_a_file_and_is_never_trusted_for_a_duration(self):
        """The chosen file's length is measured; a record only selects the file.

        Two facts are asserted together, because each alone is misleading:

        * every chosen route file's own length *does* agree with the record's
          ``raw`` (2310 of 2310 by measurement) - so the old metadata is not the
          problem people expect;
        * the two channels of one ``parallel`` folder do **not** agree (1.248 s vs
          1.160 s in the first folder examined), which is why choosing the file by
          ``timeline_route`` matters and why nothing downstream may read a length
          out of a record.

        The imported item therefore carries the file and no duration at all.
        """
        from word_video.media import duration

        checked = disagreeing = 0
        for entry in self.scan.entries:
            saved = json.loads((Path(entry.folder) / 'complete.json')
                               .read_text(encoding='utf-8'))
            if 'raw' not in saved:
                continue
            checked += 1
            if abs(float(saved['raw']) - duration(entry.path)) > 0.01:
                disagreeing += 1
        self.assertEqual(2310, checked, 'the archive changed size')
        self.assertEqual(0, disagreeing,
                         '%d records disagree with the file they name' % disagreeing)

        # The two channels of one parallel folder are different recordings.
        pair = None
        for entry in self.scan.entries:
            if entry.kind != 'parallel':
                continue
            siblings = sorted(Path(entry.folder).glob('original.*'))
            if len(siblings) == 2:
                pair = (siblings[0], siblings[1])
                break
        self.assertIsNotNone(pair)
        self.assertNotAlmostEqual(duration(pair[0]), duration(pair[1]), places=3,
                                 msg='the two channels are the same length')

        plan = store.plan_import(self.records, self.scan)
        for item in plan.items:
            self.assertNotIn('duration_s', item)
            self.assertNotIn('raw', item)
            self.assertNotIn('rendered', item)


if __name__ == '__main__':
    unittest.main()
