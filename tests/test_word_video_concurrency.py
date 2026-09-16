"""Concurrency in build_speech: parallel, still deterministic, still honest.

The service ceilings are measured elsewhere (``word_video_work/probe*.json``);
what is asserted here is that turning the knob changes *only* the scheduling:
the tokens, the on-disk cache layout and the failure behaviour must be exactly
what a serial run produced.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

from word_video.contracts import LessonSpec, WordEntry
from word_video.tts import build_speech


def make_wav(target, frames=2400, rate=24000):
    with wave.open(str(target), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\x01\x00' * frames)


class Recorder:
    """Fake route: counts in-flight calls so parallelism is observable."""

    def __init__(self, fail_on=None, delay=0.05):
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls = []
        self.fail_on = fail_on
        self.delay = delay

    def __call__(self, route, text, role, target, voice=None, resource=None, attempts=3):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.calls.append((route, text, role))
        try:
            time.sleep(self.delay)
            if self.fail_on and text == self.fail_on:
                raise RuntimeError('synthetic transport failure')
            make_wav(target, 2400 if route == 'volcengine_legacy' else 3600)
        finally:
            with self.lock:
                self.inflight -= 1


def parallel_provider(**extra):
    provider = {'kind': 'parallel', 'routes': ['volcengine_legacy', 'jianying'],
                'timeline_route': 'volcengine_legacy', 'voices_confirmed': True}
    provider.update(extra)
    return provider


def lesson_of(entries):
    return LessonSpec([WordEntry(*entry) for entry in entries])


WORDS = [(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果'),
         (2, 'banana', '/bəˈnɑːnə/', 'n. 香蕉', '香蕉'),
         (3, 'pear', '/peə/', 'n. 梨', '梨')]


class ConcurrencyTests(unittest.TestCase):
    def test_rejects_impossible_limits(self):
        lesson = lesson_of(WORDS[:1])
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            for bad in (0, 101, -1, 2.5, '8', True):
                with self.subTest(concurrency=bad), self.assertRaises(ValueError):
                    build_speech(lesson, parallel_provider(concurrency=bad), cache)
            for bad in ({'not-a-route': 4}, {'volcengine_legacy': 0},
                        {'volcengine_legacy': 101}, 'eight'):
                with self.subTest(routes=bad), self.assertRaises(ValueError):
                    build_speech(lesson, parallel_provider(route_concurrency=bad), cache)
            self.assertFalse(cache.exists(), 'validation must fail before any work')

    def test_parallel_run_keeps_serial_cache_layout_and_tokens(self):
        """Same utterances, same folders, same specs - only the timing differs."""
        lesson = lesson_of(WORDS)
        with tempfile.TemporaryDirectory() as folder:
            serial_cache = Path(folder) / 'serial'
            parallel_cache = Path(folder) / 'parallel'
            serial = Recorder()
            with patch('word_video.tts.synthesize_role', side_effect=serial):
                assets_serial = build_speech(lesson, parallel_provider(concurrency=1),
                                             serial_cache)
            self.assertEqual(1, serial.peak)
            self.assertEqual(18, len(serial.calls))  # 3 words x 3 roles x 2 routes
            concurrent = Recorder()
            with patch('word_video.tts.synthesize_role', side_effect=concurrent):
                assets_parallel = build_speech(lesson, parallel_provider(concurrency=6),
                                               parallel_cache)
            self.assertGreater(concurrent.peak, 1, 'the run was not actually parallel')
            self.assertEqual(18, len(concurrent.calls))
            self.assertEqual(sorted(p.name for p in serial_cache.iterdir()),
                             sorted(p.name for p in parallel_cache.iterdir()))
            for name in (p.name for p in serial_cache.iterdir()):
                left = json.loads((serial_cache / name / 'complete.json').read_text(encoding='utf-8'))
                right = json.loads((parallel_cache / name / 'complete.json').read_text(encoding='utf-8'))
                self.assertEqual(left['spec'], right['spec'])
                self.assertEqual(left['raw'], right['raw'])
            self.assertEqual([(a.word_index, a.role, a.voice) for a in assets_serial],
                             [(a.word_index, a.role, a.voice) for a in assets_parallel])

    def test_identical_utterances_share_one_unit(self):
        """A repeated word must not have two threads writing the same folder."""
        lesson = lesson_of([(1, 'bank', '/bæŋk/', 'n. 银行', '银行'),
                            (2, 'bank', '/bæŋk/', 'n. 银行', '银行')])
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            with patch('word_video.tts.synthesize_role', side_effect=recorder):
                assets = build_speech(lesson, parallel_provider(concurrency=4), cache)
            # Two words, three roles, two routes - but one utterance each.
            self.assertEqual(6, len(recorder.calls))
            self.assertEqual(3, len(list(cache.iterdir())))
            # Every word still gets its own asset; the two words simply point at
            # the same prepared file instead of synthesising it twice.
            self.assertEqual(6, len(assets))
            for role in ('female', 'male', 'chinese'):
                same = [a.path for a in assets if a.role == role]
                self.assertEqual(1, len(set(same)), role)

    def test_one_failure_fails_the_job(self):
        recorder = Recorder(fail_on='banana')
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache'
            with patch('word_video.tts.synthesize_role', side_effect=recorder):
                with self.assertRaises(RuntimeError):
                    build_speech(lesson_of(WORDS), parallel_provider(concurrency=6), cache)
            # Nothing half-written is treated as complete.
            complete = list(cache.glob('*/complete.json'))
            self.assertLess(len(complete), 9)
            for record in complete:
                saved = json.loads(record.read_text(encoding='utf-8'))
                self.assertIn('audio_digest', saved)

    def test_progress_reports_every_unit(self):
        seen = []
        recorder = Recorder(delay=0)
        with tempfile.TemporaryDirectory() as folder:
            with patch('word_video.tts.synthesize_role', side_effect=recorder):
                build_speech(lesson_of(WORDS), parallel_provider(concurrency=4),
                             Path(folder) / 'cache', progress=seen.append)
        self.assertEqual(9, seen[-1]['total'])
        self.assertEqual(9, seen[-1]['done'])
        self.assertIn('route_limits', seen[0])
        self.assertEqual(100, seen[0]['route_limits']['volcengine_legacy'])

    def test_default_is_serial(self):
        """An account with a low ceiling must not be flooded by default."""
        recorder = Recorder()
        with tempfile.TemporaryDirectory() as folder:
            with patch('word_video.tts.synthesize_role', side_effect=recorder):
                build_speech(lesson_of(WORDS), parallel_provider(),
                             Path(folder) / 'cache')
        self.assertEqual(1, recorder.peak)


if __name__ == '__main__':
    unittest.main()
