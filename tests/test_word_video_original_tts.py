import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from word_video.original_tts import VOICES, readiness, synthesize_original, audition
from word_video.tts import build_speech


class OriginalTTSTests(unittest.TestCase):
    env = {'VOLC_TTS_APPID': 'test-app', 'VOLC_TTS_ACCESS_TOKEN': 'private-test-token',
           'VOLC_TTS_CLUSTER': 'volcano_tts'}

    def test_exact_ids(self):
        self.assertEqual({k:v['speaker'] for k,v in VOICES.items()},
                         {'female':'BV503_streaming','male':'BV504_streaming','chinese':'BV406_streaming'})

    def test_presence_only(self):
        with patch.dict('os.environ', self.env):
            data = json.dumps(readiness())
        self.assertNotIn('private-test-token', data)
        self.assertNotIn('test-app', data)

    def test_missing_config_no_network_or_folder(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True), \
                patch('urllib.request.urlopen') as network:
            with self.assertRaises(PermissionError): audition(Path(folder)/'output')
            self.assertFalse((Path(folder)/'output').exists())
            network.assert_not_called()

    def test_exact_request_and_atomic_file(self):
        for role in VOICES:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as folder:
                result = io.BytesIO(json.dumps({'code':3000,'data':base64.b64encode(b'audio').decode()}).encode())
                with patch.dict('os.environ', self.env), patch('urllib.request.urlopen',return_value=result) as network, \
                        patch('word_video.original_tts.duration',return_value=1.0):
                    target=Path(folder)/'new.mp3'
                    synthesize_original('new text',role,target)
                    request=network.call_args.args[0]
                    body=json.loads(request.data)
                    self.assertTrue(request.full_url.endswith('/api/v1/tts'))
                    self.assertEqual(body['audio']['voice_type'],VOICES[role]['speaker'])
                    self.assertEqual(body['audio']['speed_ratio'],1.0)
                    self.assertEqual(body['request']['operation'],'query')
                    self.assertEqual(target.read_bytes(),b'audio')
                    with self.assertRaises(FileExistsError): synthesize_original('new text',role,target)
                    self.assertEqual(network.call_count,1)
                    self.assertEqual(len(list(Path(folder).iterdir())),1)

    def test_denied_no_partial_no_fallback_no_secret(self):
        for reply in ({'code':3001,'message':'private-test-token','data':'YQ=='},
                      {'code':'private-test-token'}, {'code':3000,'data':'invalid'}, {'code':3000,'data':''}):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',self.env), \
                    patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(reply).encode())) as network:
                target=Path(folder)/'new.mp3'
                with self.assertRaises(RuntimeError) as caught: synthesize_original('new','female',target)
                self.assertNotIn('private-test-token',str(caught.exception))
                self.assertFalse(target.exists())
                self.assertEqual(network.call_count,1)

    def test_http_error_redaction(self):
        with patch.dict('os.environ',self.env), patch('urllib.request.urlopen',
                side_effect=HTTPError('https://private-test-token',403,'private-test-token',{},None)):
            with self.assertRaises(RuntimeError) as caught: synthesize_original('new','female','unused.mp3')
            self.assertNotIn('private-test-token',str(caught.exception))

    def test_batch_requires_audition_and_cannot_replace(self):
        with self.assertRaises(ValueError):
            build_speech(None,{'kind':'volcengine_original','voices_confirmed':True,
                               'voices':{'female':'other'}},'unused')
        for kind in ('volcengine_original','jianying_original'):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError): build_speech(None,{'kind':kind},'unused')

    def test_cloned_speaker_is_used_verbatim(self):
        """A voice the user created in their own account must reach the request as-is."""
        from word_video.contracts import LessonSpec, WordEntry
        lesson = LessonSpec([WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')])
        voices = {'female': 'S_cloneF', 'male': 'S_cloneM', 'chinese': 'S_cloneC'}
        sent = []

        def fake_synthesize(text, voice, target, resource='seed-tts-2.0'):
            sent.append({'text': text, 'voice': voice, 'resource': resource})
            Path(target).write_bytes(b'\x00')
            raise RuntimeError('stop after the first call')

        with tempfile.TemporaryDirectory() as folder, \
                patch('word_video.tts.synthesize', side_effect=fake_synthesize):
            with self.assertRaises(RuntimeError):
                build_speech(lesson, {'kind': 'volcengine', 'voices': voices,
                                      'voices_confirmed': True}, Path(folder))
        self.assertEqual(sent[0]['voice'], 'S_cloneF')
        self.assertEqual(sent[0]['resource'], 'seed-tts-2.0')

    def test_incomplete_clone_config_is_rejected(self):
        with self.assertRaises(ValueError):
            build_speech(None, {'kind': 'volcengine', 'voices_confirmed': True,
                                'voices': {'female': 'S_cloneF'}}, 'unused')

    def test_parallel_cache_survives_a_second_run(self):
        """Re-running a parallel job must reuse its cache, not fight it.

        The token is the hash of the spec, so the spec has to be complete before
        the hash is taken; adding route bookkeeping afterwards made the second
        run hash a different dict, land on the same folder and then report
        'Cached speech changed'.
        """
        import wave
        from word_video.contracts import LessonSpec, WordEntry
        from word_video.tts import build_speech
        entry = WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')
        lesson = LessonSpec([entry])
        calls = []

        def fake(route, text, role, target, voice=None, resource=None, attempts=3):
            calls.append(route)
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                stream.writeframes(b'\x01\x00' * 2400)

        provider = {'kind': 'parallel', 'routes': ['volcengine_legacy', 'jianying'],
                    'timeline_route': 'volcengine_legacy', 'voices_confirmed': True}
        with tempfile.TemporaryDirectory() as folder, \
                patch('word_video.tts.synthesize_role', side_effect=fake):
            cache = Path(folder) / 'audio-cache'
            first = build_speech(lesson, provider, cache)
            synthesized = len(calls)
            self.assertEqual(6, synthesized)          # 3 roles x 2 routes
            second = build_speech(lesson, provider, cache)
            self.assertEqual(synthesized, len(calls))  # nothing re-synthesised
            self.assertEqual(len(first), len(second))
            for asset in first + second:
                # voice is the original speaker id, not the route name.
                self.assertEqual('BV503_streaming' if asset.role == 'female'
                                 else 'BV504_streaming' if asset.role == 'male'
                                 else 'BV406_streaming', asset.voice)
            records = list(cache.glob('*/complete.json'))
            self.assertEqual(3, len(records))
            for record in records:
                spec = json.loads(record.read_text(encoding='utf-8'))['spec']
                self.assertEqual(['volcengine_legacy', 'jianying'], spec['routes'])
                self.assertEqual('volcengine_legacy', spec['timeline_route'])

    def test_parallel_timeline_route_is_honoured(self):
        """The named preferred route must be the one the timeline consumes."""
        import wave
        from word_video.contracts import LessonSpec, WordEntry
        from word_video.tts import build_speech
        lesson = LessonSpec([WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')])

        def fake(route, text, role, target, voice=None, resource=None, attempts=3):
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
                # Different length per route so the chosen one is identifiable.
                frames = 2400 if route == 'volcengine_legacy' else 3600
                stream.writeframes(b'\x01\x00' * frames)

        provider = {'kind': 'parallel', 'routes': ['volcengine_legacy', 'jianying'],
                    'timeline_route': 'volcengine_legacy', 'voices_confirmed': True}
        with tempfile.TemporaryDirectory() as folder, \
                patch('word_video.tts.synthesize_role', side_effect=fake):
            assets = build_speech(lesson, provider, Path(folder) / 'cache')
            records = {json.loads(r.read_text(encoding='utf-8'))['spec']['role']:
                       json.loads(r.read_text(encoding='utf-8'))['spec']
                       for r in (Path(folder) / 'cache').glob('*/complete.json')}
        for asset in assets:
            # The preferred route's 0.1 s file is the one on the timeline, not
            # the other route's 0.15 s file.
            self.assertAlmostEqual(0.1, asset.duration_s, places=2)
            self.assertEqual('volcengine_legacy', records[asset.role]['timeline_route'])
        """A parallel run must name the supported routes and be auditioned."""
        cases = [
            {'kind': 'parallel'},
            {'kind': 'parallel', 'voices_confirmed': True},
            {'kind': 'parallel', 'routes': ['jianying', 'volcengine_legacy']},
            {'kind': 'parallel', 'voices_confirmed': True, 'routes': ['jianying']},
            {'kind': 'parallel', 'voices_confirmed': True,
             'routes': ['jianying', 'volcengine_sse']},
            {'kind': 'parallel', 'voices_confirmed': True,
             'routes': ['jianying', 'not-a-route']},
            {'kind': 'parallel', 'voices_confirmed': True,
             'routes': ['jianying', 'volcengine_legacy'], 'voices': {'female': 'other'}},
        ]
        for provider in cases:
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                build_speech(None, provider, 'unused')

    def test_routes_are_named_explicitly(self):
        from word_video.tts import BIGMODEL_RESOURCES, ROUTES, synthesize_role
        self.assertEqual(('jianying', 'volcengine_legacy', 'volcengine_sse'), ROUTES)
        self.assertEqual(('seed-tts-2.0', 'seed-tts-1.0', 'seed-tts-1.0-concurr'),
                         BIGMODEL_RESOURCES)
        with self.assertRaises(ValueError):
            synthesize_role('not-a-route', 'text', 'female', 'unused-target')
        # The SSE route cannot invent a speaker; it must be given one.
        with self.assertRaises(ValueError):
            synthesize_role('volcengine_sse', 'text', 'female', 'unused-target')

    def test_bigmodel_route_requires_key_and_named_resource(self):
        """The new-console route needs VOLC_TTS_API_KEY and a stated resource."""
        import os
        from unittest.mock import patch
        from word_video.contracts import LessonSpec, WordEntry
        from word_video.tts import synthesize_bigmodel
        lesson = LessonSpec([WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')])
        voices = {'female': 'BV503_streaming', 'male': 'BV504_streaming',
                  'chinese': 'BV406_streaming'}
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionError):
                synthesize_bigmodel('text', 'BV503_streaming', 'unused.mp3')
        for bad in ({'kind': 'volcengine_bigmodel'},
                    {'kind': 'volcengine_bigmodel', 'voices': voices,
                     'voices_confirmed': True},
                    {'kind': 'volcengine_bigmodel', 'voices': voices,
                     'voices_confirmed': True, 'resource': 'volc.service_type.10029'},
                    {'kind': 'volcengine_bigmodel', 'voices': {'female': 'x'},
                     'voices_confirmed': True, 'resource': 'seed-tts-1.0'}):
            with self.subTest(provider=bad), self.assertRaises(ValueError):
                build_speech(lesson, bad, 'unused')

    def test_bigmodel_sse_parsing_and_error_codes(self):
        from word_video.tts import read_sse
        good = [b'data: {"code":0,"data":"' + base64.b64encode(b'abc') + b'"}\n',
                b'data: {"code":20000000,"data":"' + base64.b64encode(b'de') + b'"}\n']
        self.assertEqual(b'abcde', read_sse(good))
        with self.assertRaises(RuntimeError) as caught:
            read_sse([b'data: {"code":45000010,"message":"Invalid X-Api-Key"}\n'])
        self.assertIn('45000010', str(caught.exception))
        with self.assertRaises(RuntimeError):
            read_sse([b'data: {"code":0}\n'])


if __name__=='__main__': unittest.main()
