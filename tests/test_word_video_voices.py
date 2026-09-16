"""Voice discovery and selection: pick the voice the user actually used.

A Jianying draft records ``tone_speaker`` (the id both routes need) next to
``tone_effect_name`` (the name the user recognises).  These tests pin down that
the catalogue is read from real drafts, that encrypted drafts are skipped rather
than guessed at, and that an explicit choice is honoured while a partial or
unconfirmed one is still refused.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from word_video.contracts import LessonSpec, WordEntry
from word_video.original_tts import VOICES
from word_video.tts import build_speech
from word_video.voices import as_role_map, discover


def material(speaker, name, resource):
    return {'tone_speaker': speaker, 'tone_effect_name': name,
            'resource_id': resource, 'tone_platform': 'sami', 'type': 'text_to_audio'}


def draft(speakers):
    return {'materials': {'audios': [material(*item) for item in speakers]}}


class DiscoveryTests(unittest.TestCase):
    def _tree(self, root):
        # A normal draft.
        plain = root / '课堂项目'
        plain.mkdir(parents=True)
        (plain / 'draft_content.json').write_text(
            json.dumps(draft([('BV700_streaming', '温柔女声', '111'),
                              ('BV503_streaming', 'Energetic Female(English)', '222'),
                              ('BV700_streaming', '温柔女声', '111')]), ensure_ascii=False),
            encoding='utf-8')
        # A nested draft, the shape 剪映 uses inside a project.
        nested = root / '模板项目'
        inner = nested / 'subdraft' / 'C90F687F'
        inner.mkdir(parents=True)
        (inner / 'draft_content.json').write_text(
            json.dumps({'materials': {'drafts': [
                {'draft': json.dumps(draft([('BV900_streaming', '男声解说', '333')]))}]}},
                ensure_ascii=False), encoding='utf-8')
        # Encrypted (what 剪映 writes after saving) and corrupt files.
        encrypted = root / '加密项目'
        encrypted.mkdir()
        (encrypted / 'draft_content.json').write_text('DeIAqREcEg8BKs6xoSEsJ3JeYUe4', encoding='utf-8')
        corrupt = root / '坏项目'
        corrupt.mkdir()
        (corrupt / 'draft_content.json').write_text('{"materials": [', encoding='utf-8')

    def test_finds_used_voices_and_skips_the_unreadable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._tree(root)
            report = discover([root])
            found = {item['speaker']: item for item in report['voices']}
            self.assertIn('BV700_streaming', found)
            self.assertEqual('温柔女声', found['BV700_streaming']['name'])
            self.assertEqual(2, found['BV700_streaming']['uses'])
            self.assertEqual(['课堂项目'], found['BV700_streaming']['projects'])
            # Nested drafts count too, and the built-in originals are always listed.
            self.assertIn('BV900_streaming', found)
            for meta in VOICES.values():
                self.assertIn(meta['speaker'], found)
            self.assertEqual(1, report['skipped_encrypted'])
            self.assertGreaterEqual(report['unreadable'], 1)

    def test_missing_root_is_not_an_error(self):
        report = discover([Path('definitely-not-here')])
        self.assertEqual(['definitely-not-here'], report['roots'])
        self.assertEqual(0, report['drafts_read'])
        self.assertTrue(report['voices'])  # built-ins remain listed

    def test_role_map_accepts_the_id_or_the_name_the_user_saw(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._tree(root)
            with patch('word_video.voices.discover', return_value=discover([root])):
                self.assertEqual({'female': 'BV700_streaming'},
                                 as_role_map({'female': 'BV700_streaming'}))
                self.assertEqual({'male': 'BV700_streaming'},
                                 as_role_map({'male': '温柔女声'}))
                # An id that is not in the local catalogue is still passed to the
                # service, which owns entitlement; only a nonsense *name* is a
                # client-side error.
                self.assertEqual({'female': 'BV999_streaming'},
                                 as_role_map({'female': 'BV999_streaming'}))
                with self.assertRaises(ValueError) as caught:
                    as_role_map({'female': '没有这个音色'})
                self.assertIn('known names', str(caught.exception))


class SelectionTests(unittest.TestCase):
    def _lesson(self):
        return LessonSpec([WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')])

    def _provider(self, **extra):
        provider = {'kind': 'parallel', 'routes': ['volcengine_legacy', 'jianying'],
                    'timeline_route': 'volcengine_legacy'}
        provider.update(extra)
        return provider

    def test_a_complete_auditioned_choice_is_used_verbatim(self):
        import wave
        chosen = {'female': 'BV700_streaming', 'male': 'BV701_streaming',
                  'chinese': 'BV900_streaming'}
        calls = []

        def fake(route, text, role, target, voice=None, resource=None, attempts=3):
            calls.append((role, voice))
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                stream.writeframes(b'\x01\x00' * 2400)

        with tempfile.TemporaryDirectory() as folder, \
                patch('word_video.tts.synthesize_role', side_effect=fake):
            assets = build_speech(self._lesson(),
                                  self._provider(voices=chosen, voices_confirmed=True),
                                  Path(folder) / 'cache')
        self.assertEqual({('female', 'BV700_streaming'), ('male', 'BV701_streaming'),
                          ('chinese', 'BV900_streaming')}, set(calls))
        self.assertEqual({'BV700_streaming', 'BV701_streaming', 'BV900_streaming'},
                         {asset.voice for asset in assets})

    def test_partial_or_unconfirmed_choices_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            for provider in (
                    self._provider(voices={'female': 'BV700_streaming'}, voices_confirmed=True),
                    self._provider(voices={'female': 'BV700_streaming', 'male': '', 'chinese': 'x'},
                                   voices_confirmed=True),
                    self._provider(voices={'female': 'a', 'male': 'b', 'chinese': 'c'}),
                    self._provider()):
                with self.subTest(provider=provider), self.assertRaises(ValueError):
                    build_speech(self._lesson(), provider, cache)
            self.assertFalse(cache.exists())

    def test_a_different_voice_gets_a_different_cache_entry(self):
        import wave
        calls = []

        def fake(route, text, role, target, voice=None, resource=None, attempts=3):
            calls.append(voice)
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                stream.writeframes(b'\x01\x00' * 2400)

        with tempfile.TemporaryDirectory() as folder, \
                patch('word_video.tts.synthesize_role', side_effect=fake):
            cache = Path(folder) / 'cache'
            first = build_speech(self._lesson(), self._provider(voices_confirmed=True), cache)
            folders = {p.name for p in cache.iterdir()}
            second = build_speech(self._lesson(),
                                  self._provider(voices={'female': 'BV700_streaming',
                                                         'male': 'BV504_streaming',
                                                         'chinese': 'BV406_streaming'},
                                                 voices_confirmed=True), cache)
            after = {p.name for p in cache.iterdir()}
        # The new voice was really synthesised (not silently served from cache)
        # and only the changed role got new folders.
        self.assertIn('BV700_streaming', calls)
        self.assertTrue(folders < after)
        self.assertNotEqual(first[0].path, second[0].path)


if __name__ == '__main__':
    unittest.main()
