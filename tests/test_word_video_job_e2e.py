"""End-to-end smoke test of one job.

The pipeline has twice been broken by edits that every unit test still passed:
``split_lesson`` silently dropped a field, and a stray indentation left ``assets``
unassigned so every real run died one second in.  Both are invisible unless a
whole job actually runs, so this test submits a two-word lesson, runs it, and
checks the published artifacts.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from word_video.jobs import submit, work
from word_video.media import executable, run


class JobEndToEndTests(unittest.TestCase):
    def _background(self, root):
        path = root / 'background.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi', '-i',
             'color=c=blue:s=320x180:r=60:d=0.5', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', path])
        return path

    def test_published_job_with_concurrent_speech(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            background = self._background(root)
            peak = [0]
            inflight = [0]
            lock = threading.Lock()

            def fake(text, voice, target, resource='seed-tts-2.0'):
                with lock:
                    inflight[0] += 1
                    peak[0] = max(peak[0], inflight[0])
                try:
                    time.sleep(0.02)
                    with wave.open(str(target), 'wb') as stream:
                        stream.setnchannels(1)
                        stream.setsampwidth(2)
                        stream.setframerate(24000)
                        stream.writeframes(b'\x01\x00' * 2400)
                finally:
                    with lock:
                        inflight[0] -= 1

            request = {
                'idempotency_key': 'e2e-smoke',
                'source': {'text': 'apple [ˈæpəl] n. 苹果\nbanana [bəˈnɑːnə] n. 香蕉'},
                'range': {'start': 1, 'end': 2},
                'output': str(root / 'output'),
                'lesson': {'background': str(background), 'width': 320, 'height': 180,
                           'intro_s': 0.2},
                'provider': {'kind': 'volcengine', 'voices_confirmed': True,
                             'voices': {'female': 'S_f', 'male': 'S_m', 'chinese': 'S_c'},
                             'concurrency': 4},
            }
            job = submit(str(root / 'queue.sqlite3'), request)['id']
            with patch('word_video.tts.synthesize', side_effect=fake):
                state = work(str(root / 'queue.sqlite3'), job)
            self.assertEqual('generated', state['state'], state.get('error'))
            result = state['result']
            batch = result['batches'][0]
            self.assertEqual([1, 2], [batch['first'], batch['last']])
            self.assertGreater(peak[0], 1, 'the job did not use concurrent speech')
            self.assertTrue(batch['video_check']['structural_ok'])
            self.assertTrue(batch['draft']['structural_ok'])
            published = [Path(item['path']) for item in result['files']]
            names = {path.name for path in published}
            self.assertIn('video.mp4', names)
            self.assertIn('mix.wav', names)
            self.assertIn('captions.ass', names)
            self.assertEqual(5, len([n for n in names if n.endswith('.srt')]))
            self.assertEqual(6, batch['draft']['speech_segments'])  # 2 words x 3 roles
            for path in published:
                self.assertTrue(path.is_file(), path)
            # Two artifacts produced by different code paths must agree on the
            # one thing the whole pipeline is built on: the timeline length.
            self.assertGreater(batch['video_check']['duration_s'], 0.2)
            self.assertAlmostEqual(batch['video_check']['duration_s'],
                                   batch['draft']['duration_s'], delta=0.01)
            committed = json.loads((Path(batch['draft']['draft']).parent
                                    / 'complete.json').read_text(encoding='utf-8'))
            self.assertEqual(len(result['files']), len(committed['files']))


if __name__ == '__main__':
    unittest.main()
