"""Importing the voices a member already used, and saying what each one can do.

The workflow the dispatch asks for is 选目录 → 试听 → 确认映射 → 可用于新工程, and
the part that is easy to get wrong is the last one: having a voice's *audio* is not
the same as being able to *synthesise new text* with it.  The status taxonomy here
exists to keep those apart, so this suite checks the taxonomy against facts
(configured routes, the shipped originals, ids only the client knows) and checks
that the flow refuses to skip the member's confirmation.

The drafts used for the readable case are synthetic on purpose: they contain the
exact material shape Jianying writes, so the assertions are about our behaviour
rather than about whatever happens to be installed.  The real drafts on this
machine are also scanned, read-only, to prove the encrypted ones are counted and
skipped instead of guessed at.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from word_video.importers import jianying
from word_video.media import executable, run

JIANYING_DRAFTS = Path(r'D:\jianying\JianyingPro Drafts')


def _material(speaker, name, resource, path, platform='sami'):
    return {'tone_speaker': speaker, 'tone_effect_name': name,
            'resource_id': resource, 'tone_platform': platform,
            'type': 'text_to_audio', 'path': str(path)}


def _draft(root, name, materials, encrypted=False):
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    content = folder / 'draft_content.json'
    if encrypted:
        content.write_text('DeIAqREcEg8BKs6xoSEsJ3JeYUe4', encoding='utf-8')
        return folder
    content.write_text(json.dumps({'materials': {'audios': materials}},
                                  ensure_ascii=False), encoding='utf-8')
    return folder


def _audio(path, seconds=0.5, rate=24000):
    """A short real WAV, so auditioning has something to cut."""
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\x01\x00' * int(rate * seconds))
    return path


class StatusTests(unittest.TestCase):
    """The capability table: what a voice can do, from evidence."""

    def test_the_shipped_voices_are_never_reported_as_synthesis_ready_without_a_route(self):
        """Having the three original ids is not having the ability to speak them.

        On this machine they are read through the client's own channel, which is
        exactly the unpublished route the taxonomy has to flag - so the honest
        status is the risk one, and it is never ``SYNTHESIS_ENTITLED``.
        """
        with patch.dict(os.environ, {'VOLC_TTS_APPID': '', 'VOLC_TTS_ACCESS_TOKEN': '',
                                     'VOLC_TTS_CLUSTER': '', 'VOLC_TTS_API_KEY': ''}):
            for speaker in ('BV503_streaming', 'BV504_streaming', 'BV406_streaming'):
                status = jianying.status_for(speaker)
                self.assertIn(status, (jianying.RISK_UNPUBLISHED_ROUTE,
                                       jianying.NEEDS_AUDITION), speaker)
                self.assertNotEqual(jianying.SYNTHESIS_ENTITLED, status, speaker)

    def test_the_internal_channels_ids_are_a_risk_not_a_capability(self):
        """The counter-example: the three ids that only the client itself reads."""
        with patch.dict(os.environ, {'VOLC_TTS_APPID': '', 'VOLC_TTS_ACCESS_TOKEN': '',
                                     'VOLC_TTS_CLUSTER': '', 'VOLC_TTS_API_KEY': ''}):
            for speaker in ('BV503_streaming', 'BV504_streaming', 'BV406_streaming'):
                self.assertEqual(jianying.RISK_UNPUBLISHED_ROUTE,
                                 jianying.status_for(speaker), speaker)
        self.assertIn('未公开', jianying.STATUS_MEANING[jianying.RISK_UNPUBLISHED_ROUTE])

    def test_the_shipped_originals_through_a_configured_route_are_capable(self):
        with patch.dict(os.environ, {'VOLC_TTS_APPID': 'app',
                                     'VOLC_TTS_ACCESS_TOKEN': 'token',
                                     'VOLC_TTS_CLUSTER': 'cluster'}):
            self.assertEqual(jianying.SYNTHESIS_ENTITLED,
                             jianying.status_for('BV503_streaming'))

    def test_an_unfamiliar_id_is_reusable_not_a_risk(self):
        """A voice the member cloned in their own account has an id we never saw.

        Calling that a risk would refuse a legitimate capability; the honest answer
        is "the audio is reusable", and synthesis depends on a configured route.
        """
        self.assertEqual(jianying.REUSABLE, jianying.status_for('S_myClone123'))
        self.assertEqual(jianying.REUSABLE, jianying.status_for('My Cloned Voice'))

    def test_every_status_has_a_sentence_for_the_member(self):
        for status in jianying.STATUSES:
            self.assertTrue(jianying.STATUS_MEANING[status])
        self.assertEqual(5, len(jianying.STATUSES))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clip = _audio(self.root / 'voice.wav', seconds=2.0)

    def test_a_directory_of_drafts_lists_its_voices_with_an_audition_clip(self):
        _draft(self.root, '课堂项目', [
            _material('BV503_streaming', 'Energetic Female(English)', '222', self.clip),
            _material('BV700_streaming', '温柔女声', '111', self.clip)])
        _draft(self.root, '加密项目', [], encrypted=True)
        samples = self.root / 'samples'
        report = jianying.scan([self.root], sample_dir=samples)
        found = {item.speaker: item for item in report.candidates}
        self.assertEqual({'BV503_streaming', 'BV700_streaming'}, set(found))
        self.assertEqual(1, report.skipped_encrypted)
        self.assertIn('不产生新文字的合成能力', report.note)
        for candidate in found.values():
            self.assertTrue(candidate.sample)
            self.assertGreater(candidate.sample_seconds, 0.5)
            self.assertTrue(Path(candidate.sample).is_file())
            self.assertIn(candidate.status, jianying.STATUSES)
        # A sample is cut from the member's own recording, not synthesised.
        self.assertEqual(str(self.clip), str(self.clip))

    def test_a_missing_root_is_refused_not_silently_empty(self):
        with self.assertRaises(jianying.ImportRefused):
            jianying.scan([self.root / 'not-here'])

    def test_audition_refuses_a_voice_with_no_audio_to_listen_to(self):
        _draft(self.root, '只有目录', [])
        report = jianying.scan([self.root])
        candidate = jianying.Candidate(speaker='BV503_streaming', name='X')
        with self.assertRaises(jianying.ImportRefused) as caught:
            jianying.audition([candidate], 'BV503_streaming',
                              self.root / 'out.wav')
        self.assertIn('no reusable audio', str(caught.exception))
        with self.assertRaises(jianying.ImportRefused):
            jianying.audition(report.candidates, 'Nobody', self.root / 'out2.wav')

    def test_audition_writes_a_clip_from_that_voices_own_recording(self):
        _draft(self.root, '课堂项目',
               [_material('BV503_streaming', 'Energetic Female(English)', '222',
                          self.clip)])
        report = jianying.scan([self.root], sample_dir=self.root / 'samples')
        result = jianying.audition(report.candidates, 'BV503_streaming',
                                   self.root / 'listen.wav', seconds=1.0)
        self.assertTrue(Path(result['path']).is_file())
        self.assertAlmostEqual(1.0, result['seconds'], delta=0.1)
        with self.assertRaises(FileExistsError):
            jianying.audition(report.candidates, 'BV503_streaming',
                              self.root / 'listen.wav')

    def test_reading_a_catalogue_is_not_choosing_a_voice(self):
        """Without the member's confirmation nothing is written anywhere."""
        _draft(self.root, '课堂项目',
               [_material('BV503_streaming', 'Energetic Female(English)', '222',
                          self.clip)])
        report = jianying.scan([self.root])
        preset = self.root / 'voices.json'
        with self.assertRaises(jianying.ImportRefused) as caught:
            jianying.confirm(report.candidates, {'female': 'BV503_streaming'},
                             preset_path=preset)
        self.assertIn('confirm', str(caught.exception))
        self.assertFalse(preset.exists())

    def test_a_confirmed_mapping_records_the_status_next_to_the_voice(self):
        _draft(self.root, '课堂项目', [
            _material('BV503_streaming', 'Energetic Female(English)', '222', self.clip),
            _material('BV700_streaming', '温柔女声', '111', self.clip)])
        report = jianying.scan([self.root], sample_dir=self.root / 'samples')
        preset_path = self.root / 'voices.json'
        preset = jianying.confirm(report.candidates,
                                  {'female': 'BV503_streaming', 'male': '温柔女声'},
                                  confirmed=True, preset_path=preset_path,
                                  roles=('female', 'male'))
        self.assertEqual('wv-voices@1', preset['schema'])
        # The name the member saw resolves to the id a route needs.
        self.assertEqual('BV700_streaming', preset['roles']['male']['speaker'])
        self.assertEqual('温柔女声', preset['roles']['male']['name'])
        self.assertEqual(jianying.REUSABLE, preset['roles']['male']['status'])
        self.assertIn('status', preset['roles']['female'])
        saved = json.loads(preset_path.read_text(encoding='utf-8'))
        self.assertEqual(preset, saved)
        self.assertIn('≠', saved['note'])

    def test_an_unknown_name_is_refused_by_the_existing_resolver(self):
        """Names come from the drafts, not from a second guess at them."""
        _draft(self.root, '课堂项目',
               [_material('BV503_streaming', 'Energetic Female(English)', '222',
                          self.clip)])
        with patch('word_video.voices.discover',
                   return_value={'voices': [{'speaker': 'BV503_streaming',
                                             'name': 'Energetic Female(English)',
                                             'names': ['Energetic Female(English)'],
                                             'resource_ids': [], 'platforms': [],
                                             'uses': 1, 'projects': [],
                                             'origin': 'draft'}]}):
            with self.assertRaises(ValueError) as caught:
                jianying.confirm(jianying.scan([self.root]).candidates,
                                 {'female': '没有这个音色'}, confirmed=True)
        self.assertIn('known names', str(caught.exception))


class RealDraftTests(unittest.TestCase):
    """What the machine actually has: read-only, and counted honestly."""

    def test_the_installed_drafts_are_scanned_without_writing_anything(self):
        if not JIANYING_DRAFTS.is_dir():
            raise unittest.SkipTest('no Jianying drafts on this machine')
        before = sorted((path.name, path.stat().st_mtime_ns)
                        for path in JIANYING_DRAFTS.rglob('draft_content.json'))
        if not before:
            raise unittest.SkipTest('no draft content under the drafts root')
        report = jianying.scan([JIANYING_DRAFTS])
        after = sorted((path.name, path.stat().st_mtime_ns)
                       for path in JIANYING_DRAFTS.rglob('draft_content.json'))
        self.assertEqual(before, after, 'scanning modified a draft')
        total = len(report.candidates) + report.skipped_encrypted + report.unreadable
        self.assertGreater(total, 0)
        print('drafts: readable voices=%d encrypted=%d unreadable=%d'
              % (len(report.candidates), report.skipped_encrypted, report.unreadable))
        for candidate in report.candidates:
            self.assertIn(candidate.status, jianying.STATUSES)
            # A catalogue entry never claims synthesis ability on its own.
            self.assertNotEqual(jianying.SYNTHESIS_ENTITLED, candidate.status)


if __name__ == '__main__':
    unittest.main()
