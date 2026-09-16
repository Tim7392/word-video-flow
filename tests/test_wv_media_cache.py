"""One synthesis, many Tempo passes: the two speech caches must be separate.

What this pins down is a cost defect that no output check can see.  The cache
folder used to be named after a spec that *included speed and fps*, so the same
word spoken again at a different speed missed its own cached audio and called the
paid service a second time for bytes already on disk.  Conversely, nothing tied
the processed WAV to its inputs, so a stale ``prepared.wav`` could be served for
a different speed.

The layout asserted here:

``<cache>/<token>/original.<route>``
    the untouched service audio; the token is the *content* identity, so it does
    not move when the speed does.
``<cache>/<token>/prepared.wav``
    the canonical processed variant the manifest points at, plus ``prepared.json``
    recording the speed/fps it was made for and its digest.
``<cache>/<token>/prepared/<key>.wav``
    the other speed variants, sharing the one original.

Every test counts service calls through a fake route, so "did not synthesise
again" is an observation and not an inference.
"""
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from word_video.contracts import LessonSpec, WordEntry
from word_video.media import sha256
from word_video.tts import build_speech, identity_spec

RATE = 24000


def _lesson(speed=1.25, fps=60, entries=(('apple', '苹果'),)):
    return LessonSpec([WordEntry(index, word, '/x/', 'n.', spoken)
                       for index, (word, spoken) in enumerate(entries, 1)],
                      speed=speed, fps=fps)


def _local(lesson):
    """Nine real-audio stand-ins: one file per word and role.

    Half a second each, so the prepared length (the original divided by the
    speed) is clearly shorter than the original - the two numbers a cache record
    keeps have to be distinguishable for these assertions to mean anything.
    """
    folder = Path(tempfile.mkdtemp(prefix='wv-local-'))
    items = []
    for entry in lesson.entries:
        for role in ('female', 'male', 'chinese'):
            path = folder / ('%d-%s.wav' % (entry.index, role))
            with wave.open(str(path), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(RATE)
                stream.writeframes(b'\x01\x00' * (RATE // 2))
            items.append({'index': entry.index, 'role': role,
                          'text': entry.spoken_meaning if role == 'chinese' else entry.word,
                          'voice': 'V_%s' % role, 'path': str(path)})
    return {'kind': 'local', 'items': items}


class FakeRoute:
    """A synthesise_role stand-in that writes real WAV bytes and counts calls."""

    def __init__(self, frames=RATE // 2):
        self.calls = []
        self.frames = frames

    def __call__(self, route, text, role, target, voice=None, resource=None, attempts=3):
        self.calls.append((route, text, role))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(RATE)
            stream.writeframes(b'\x02\x00' * self.frames)


def _network_provider(**extra):
    provider = {'kind': 'volcengine_original', 'voices_confirmed': True}
    provider.update(extra)
    return provider


def _read_record(cache):
    records = list(Path(cache).glob('*/complete.json'))
    return [json.loads(path.read_text(encoding='utf-8')) for path in records]


class CacheLayoutTests(unittest.TestCase):
    def test_a_speed_change_reuses_the_original_and_only_reprocesses(self):
        """The whole point: 1.25x -> 1.0x costs zero service calls."""
        lesson = _lesson()
        route = FakeRoute()
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=route):
                build_speech(lesson, _network_provider(), cache)
            self.assertEqual(3, len(route.calls))          # one per role
            first = {record['spec']['role']: record for record in _read_record(cache)}
            folders = sorted(path.name for path in cache.iterdir())
            originals = {name: sorted(p.name for p in (cache / name).iterdir())
                         for name in folders}

            changed = _lesson(speed=1.0)
            with patch('word_video.tts.synthesize_role', side_effect=route):
                assets = build_speech(changed, _network_provider(), cache)
            self.assertEqual(3, len(route.calls), 'the service was called again')
            # Same folders and same originals: the content identity did not move.
            self.assertEqual(folders, sorted(path.name for path in cache.iterdir()))
            for name in folders:
                self.assertIn('original.volcengine_legacy',
                              sorted(p.name for p in (cache / name).iterdir()))
            second = {record['spec']['role']: record for record in _read_record(cache)}
            for role, record in second.items():
                self.assertEqual(first[role]['prepared']['speed'], 1.25,
                                 'the previous variant note was lost')
                self.assertEqual(1.0, record['prepared']['speed'])
                # 1.0 s of fake speech at speed 1.0 is longer than at 1.25.
                self.assertGreater(record['rendered'], first[role]['rendered'])
            self.assertEqual(3, len(assets))

    def test_rerunning_the_same_speed_reuses_the_prepared_file(self):
        lesson = _lesson()
        route = FakeRoute()
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=route):
                build_speech(lesson, _network_provider(), cache)
                before = {path.parent.name: path.read_bytes()
                          for path in cache.glob('*/prepared.wav')}
                build_speech(lesson, _network_provider(), cache)
                after = {path.parent.name: path.read_bytes()
                         for path in cache.glob('*/prepared.wav')}
        self.assertEqual(3, len(route.calls))
        self.assertEqual(before, after)

    def test_other_speed_variants_are_kept_beside_the_canonical_file(self):
        route = FakeRoute()
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=route):
                build_speech(_lesson(speed=1.25), _network_provider(), cache)
                build_speech(_lesson(speed=1.6), _network_provider(), cache)
                build_speech(_lesson(speed=1.25), _network_provider(), cache)
            self.assertEqual(3, len(route.calls))
            for name in (path.name for path in cache.iterdir()):
                variants = list((cache / name / 'prepared').glob('*.wav'))
                # 1.25x was built, replaced by 1.6x, and built again: the two
                # distinct speeds are both on disk, and the canonical file is one
                # of them rather than a third copy.
                self.assertEqual(2, len(variants), name)
                note = json.loads((cache / name / 'prepared.json').read_text(
                    encoding='utf-8'))
                # The canonical variant is the one the last run asked for.
                self.assertEqual(1.25, note['speed'])
                self.assertEqual(60, note['fps'])
                self.assertEqual(48000, note['rate'])
                self.assertEqual(sha256(cache / name / 'prepared.wav'),
                                 note['prepared_digest'])

    def test_fps_is_recorded_but_does_not_force_new_audio(self):
        """The frame rate only pads the tail; it must not cost a service call."""
        route = FakeRoute()
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=route):
                build_speech(_lesson(fps=60), _network_provider(), cache)
                prepared = {path.parent.name: path.read_bytes()
                            for path in cache.glob('*/prepared.wav')}
                build_speech(_lesson(fps=30), _network_provider(), cache)
            self.assertEqual(3, len(route.calls))
            for record in _read_record(cache):
                self.assertEqual(30, record['prepared']['fps'])
            # ...and the audio was reused, not rewritten for the new grid.
            self.assertEqual(prepared,
                             {path.parent.name: path.read_bytes()
                              for path in cache.glob('*/prepared.wav')})

    def test_the_cache_record_separates_original_from_processed(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=FakeRoute()):
                assets = build_speech(_lesson(), _network_provider(), cache)
            for record, prepared in _records_with_paths(cache):
                # The original's duration is what the timeline reserves; the
                # processed one is what the mixer consumes.  They differ, and the
                # record says which is which instead of reusing one number twice.
                self.assertAlmostEqual(0.5, record['raw'], places=2)
                self.assertAlmostEqual(0.4, record['rendered'], delta=0.05)
                self.assertLess(record['rendered'], record['raw'])
                self.assertEqual(sha256(prepared), record['audio_digest'])
                self.assertEqual(sha256(prepared),
                                 record['prepared']['prepared_digest'])
                self.assertEqual(1.25, record['prepared']['speed'])
            for asset in assets:
                self.assertAlmostEqual(0.5, asset.duration_s, places=2)
                self.assertLess(asset.rendered_duration_s, asset.duration_s)


def _records_with_paths(cache):
    for path in Path(cache).glob('*/complete.json'):
        yield (json.loads(path.read_text(encoding='utf-8')),
               path.parent / 'prepared.wav')


class LocalItemTests(unittest.TestCase):
    def test_a_local_item_is_never_synthesised_and_survives_a_speed_change(self):
        lesson = _lesson()
        provider = _local(lesson)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'audio-cache'
            with patch('word_video.tts.synthesize_role', side_effect=FakeRoute()) as route:
                first = build_speech(lesson, provider, cache)
                originals = sorted(path.parent.name for path in cache.glob('*/complete.json'))
                second = build_speech(_lesson(speed=1.0), provider, cache)
                route.assert_not_called()
            # Local audio is converted, never fetched; the content identity is the
            # caller's own file, so it does not move with the speed either.
            self.assertEqual(originals,
                             sorted(path.parent.name for path in cache.glob('*/complete.json')))
            self.assertEqual(len(first), len(second))
            self.assertGreater(second[0].rendered_duration_s,
                               first[0].rendered_duration_s)
            self.assertAlmostEqual(first[0].duration_s, second[0].duration_s, places=3)


class IdentitySpecTests(unittest.TestCase):
    def test_speed_and_fps_are_not_part_of_the_original_identity(self):
        spec = identity_spec('volcengine_original', 'female', 'apple', 'BV503_streaming')
        self.assertNotIn('speed', spec)
        self.assertNotIn('fps', spec)
        # What really decides the audio is present.
        self.assertEqual({'kind', 'role', 'text', 'voice', 'resource'}, set(spec))

    def test_local_identity_is_the_source_bytes(self):
        first = identity_spec('local', 'female', 'apple', 'V', source_digest='aaa')
        second = identity_spec('local', 'female', 'apple', 'V', source_digest='bbb')
        self.assertNotEqual(first, second)

    def test_parallel_identity_names_the_routes(self):
        without = identity_spec('parallel', 'female', 'apple', 'BV503_streaming',
                                routes=['volcengine_legacy'], timeline_route='volcengine_legacy')
        with_both = identity_spec('parallel', 'female', 'apple', 'BV503_streaming',
                                  routes=['volcengine_legacy', 'jianying'],
                                  timeline_route='volcengine_legacy')
        self.assertNotEqual(without, with_both)
        self.assertEqual(['volcengine_legacy'], without['routes'])


if __name__ == '__main__':
    unittest.main()
