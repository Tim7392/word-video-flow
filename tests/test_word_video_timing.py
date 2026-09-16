"""Tests for word_video.timing (deterministic stages and frame timeline)."""
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from word_video.contracts import LessonSpec, SpeechAsset, WordEntry  # noqa: E402
from word_video.srt_export import SRT_SUFFIXES, export_srts  # noqa: E402
from word_video.timing import build_timeline, chinese_minimum_duration, split_lesson  # noqa: E402


def original_duration(word_text, spoken, first_six=0.4, extra=0.2):
    """Independent transcription of subtitle_factory_core._gen lines 213-218."""
    count = len(re.findall(r'[\u4e00-\u9fa5]', spoken))
    if count == 0:
        count = max(len(word_text) // 4, 1) if word_text else 1
    duration = count * first_six if count <= 6 else 6 * first_six + (count - 6) * extra
    return max(duration, 0.5)


def word(index, text='alpha', spoken='你好世界', phonetic='p', meaning='m'):
    return WordEntry(index=index, word=text, phonetic=phonetic,
                     meaning=meaning, spoken_meaning=spoken)


def asset(index, role, text, duration=0.2, rendered=0.2):
    return SpeechAsset(word_index=index, role=role, text=text,
                       path='fake/%d_%s.wav' % (index, role),
                       duration_s=duration, rendered_duration_s=rendered,
                       voice='voice-%s' % role)


def assets_for(entries, duration=0.2, rendered=0.2):
    out = []
    for entry in entries:
        out.append(asset(entry.index, 'female', entry.word, duration, rendered))
        out.append(asset(entry.index, 'male', entry.word, duration, rendered))
        out.append(asset(entry.index, 'chinese', entry.spoken_meaning, duration, rendered))
    return out


def lesson(entries, **kwargs):
    kwargs.setdefault('intro_s', 0.0)
    return LessonSpec(entries=list(entries), **kwargs)


class SplitLessonTests(unittest.TestCase):
    def test_batches_preserve_original_indexes(self):
        entries = [word(i) for i in range(1, 6)]
        batches = split_lesson(lesson(entries, batch_size=2))
        self.assertEqual([2, 2, 1], [len(b.entries) for b in batches])
        self.assertEqual([[1, 2], [3, 4], [5]],
                         [[e.index for e in b.entries] for b in batches])
        self.assertEqual(2, batches[0].batch_size)

    def test_settings_are_carried_to_batches(self):
        entries = [word(i) for i in range(1, 4)]
        spec = lesson(entries, batch_size=5, speed=1.5, fps=30, styles={'a': 1})
        batch = split_lesson(spec)[0]
        self.assertEqual((1.5, 30, {'a': 1}), (batch.speed, batch.fps, batch.styles))

    def test_rejects_invalid_batch_size(self):
        entries = [word(1)]
        for bad in (0, -3, True, 2.5, '2', None):
            with self.assertRaises(ValueError):
                split_lesson(lesson(entries, batch_size=bad))

    def test_rejects_empty_or_bad_indexes(self):
        with self.assertRaises(ValueError):
            split_lesson(lesson([], batch_size=2))
        with self.assertRaises(ValueError):
            split_lesson(lesson([word(2), word(1)], batch_size=5))
        with self.assertRaises(ValueError):
            split_lesson(lesson([word(1), word(1)], batch_size=5))


class TimelineBasicsTests(unittest.TestCase):
    def test_stage_order_and_contiguous_words(self):
        entries = [word(1), word(2)]
        manifest = build_timeline(lesson(entries), assets_for(entries))
        self.assertEqual(2, len(manifest.words))
        first, second = manifest.words
        self.assertEqual(first['start_frame'], first['start_frame'])
        self.assertLess(first['start_frame'], first['male_frame'])
        self.assertLess(first['male_frame'], first['chinese_frame'])
        self.assertLess(first['chinese_frame'], first['end_frame'])
        self.assertEqual(first['end_frame'], second['start_frame'])
        self.assertEqual(second['end_frame'], manifest.total_frames)
        self.assertEqual(0, manifest.intro_frames)

    def test_audio_never_overlaps_and_matches_stage_starts(self):
        entries = [word(1), word(2)]
        manifest = build_timeline(lesson(entries), assets_for(entries))
        expected = []
        for w in manifest.words:
            expected.extend([w['start_frame'], w['male_frame'], w['chinese_frame']])
        self.assertEqual(expected, [a['start_frame'] for a in manifest.audio])
        for current, following in zip(manifest.audio, manifest.audio[1:]):
            self.assertLessEqual(
                current['start_frame'] + current['duration_frames'],
                following['start_frame'])
        self.assertEqual(['female', 'male', 'chinese'] * 2,
                         [a['role'] for a in manifest.audio])

    def test_manifest_identity_and_passthrough(self):
        entries = [word(1), word(2), word(3)]
        spec = lesson(entries, title='T', footer='F', batch_size=5,
                      styles={'x': 2}, background='bg.png', intro_s=1.0, fps=60)
        manifest = build_timeline(spec, assets_for(entries))
        self.assertEqual(1, manifest.schema_version)
        self.assertEqual(('T', 'F', 1, 3), (manifest.title, manifest.footer,
                                            manifest.first_index, manifest.last_index))
        self.assertEqual('速通（1–3）', manifest.subtitle)
        self.assertEqual({'x': 2}, manifest.styles)
        self.assertEqual('bg.png', manifest.background)
        self.assertEqual(60, manifest.intro_frames)

    def test_intro_is_unaccelerated_and_offsets_first_word(self):
        entries = [word(1)]
        spec = lesson(entries, intro_s=2.0, fps=60, speed=2.0)
        manifest = build_timeline(spec, assets_for(entries))
        self.assertEqual(120, manifest.intro_frames)
        self.assertEqual(120, manifest.words[0]['start_frame'])

    def test_all_frames_are_integers(self):
        entries = [word(1), word(2)]
        manifest = build_timeline(lesson(entries, fps=30, speed=1.3),
                                  assets_for(entries, duration=0.37))
        values = [manifest.intro_frames, manifest.total_frames]
        for w in manifest.words:
            values.extend([w[k] for k in
                           ('start_frame', 'male_frame', 'chinese_frame', 'end_frame')])
        for a in manifest.audio:
            values.extend([a['start_frame'], a['duration_frames']])
        for value in values:
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, bool)
            self.assertGreaterEqual(value, 0)


class TimingRuleTests(unittest.TestCase):
    def test_long_speech_uses_speed_exactly_once(self):
        entry = word(1)
        speech = [
            asset(1, 'female', entry.word, duration=10.0),
            asset(1, 'male', entry.word),
            asset(1, 'chinese', entry.spoken_meaning),
        ]
        manifest = build_timeline(lesson([entry], speed=1.25, fps=60), speech)
        female = manifest.audio[0]
        self.assertEqual(math.ceil(10.1 / 1.25 * 60), female['duration_frames'])
        self.assertNotEqual(math.ceil(10.1 * 1.25 * 60), female['duration_frames'])
        self.assertEqual(485, female['duration_frames'])
        self.assertEqual(48, manifest.audio[1]['duration_frames'])

    def test_gap_is_added_before_speed_conversion(self):
        entry = word(1)
        speech = [
            asset(1, 'female', entry.word, duration=1.0),
            asset(1, 'male', entry.word),
            asset(1, 'chinese', entry.spoken_meaning),
        ]
        manifest = build_timeline(lesson([entry], speed=1.25, fps=60, gap_s=0.5), speech)
        self.assertEqual(math.ceil(1.5 / 1.25 * 60), manifest.audio[0]['duration_frames'])
        self.assertEqual(72, manifest.audio[0]['duration_frames'])

    def test_english_minimum_one_second(self):
        entry = word(1)
        manifest = build_timeline(lesson([entry], speed=1.0, fps=60), assets_for([entry], 0.05))
        self.assertEqual(60, manifest.audio[0]['duration_frames'])
        self.assertEqual(60, manifest.audio[1]['duration_frames'])

    def test_rendered_duration_is_a_floor(self):
        entry = word(1)
        speech = [
            asset(1, 'female', entry.word, duration=0.0, rendered=2.0),
            asset(1, 'male', entry.word),
            asset(1, 'chinese', entry.spoken_meaning),
        ]
        manifest = build_timeline(lesson([entry], fps=60), speech)
        self.assertEqual(math.ceil(2.0 * 60), manifest.audio[0]['duration_frames'])
        self.assertEqual(120, manifest.audio[0]['duration_frames'])

    def test_chinese_character_algorithm(self):
        entry = word(1, text='word', spoken='你好世界')
        self.assertAlmostEqual(1.6, chinese_minimum_duration(entry, 0.4, 0.2))
        long_entry = word(2, text='word', spoken='一' * 10)
        self.assertAlmostEqual(6 * 0.4 + 4 * 0.2,
                               chinese_minimum_duration(long_entry, 0.4, 0.2))
        manifest = build_timeline(lesson([entry], speed=1.0, fps=60),
                                  assets_for([entry], 0.01))
        self.assertEqual(math.ceil(1.6 * 60), manifest.audio[2]['duration_frames'])
        self.assertEqual(96, manifest.audio[2]['duration_frames'])

    def test_chinese_without_hanzi_uses_count_fallback(self):
        # The fallback is a character count fed through first_six/extra, then
        # floored at 0.5s -- never returned directly as seconds.
        cases = [
            ('alpha', ''),        # 5 // 4 = 1 -> 1*0.4 -> 0.5 floor
            ('cat', ''),          # 3 // 4 = 0 -> max(0, 1) -> 0.5 floor
            ('abcdefgh', 'nein'),  # 8 // 4 = 2 -> 2*0.4 = 0.8
            ('a' * 28, ''),       # 28 // 4 = 7 -> 6*0.4 + 1*0.2 = 2.6
            ('ab', '你好'),        # Hanzi present: 2 * 0.4 -> 0.8
        ]
        for index, (text, spoken) in enumerate(cases, 1):
            entry = word(index, text=text, spoken=spoken)
            expected = original_duration(text, spoken)
            self.assertEqual(expected,
                             chinese_minimum_duration(entry, 0.4, 0.2),
                             '%r/%r' % (text, spoken))

    def test_chinese_minimum_matches_original_for_many_cases(self):
        spoken_cases = ['', '你好', '你好世界', '一二三四五六', '一' * 7, '一' * 12]
        for index, spoken in enumerate(spoken_cases, 1):
            entry = word(index, text='abcdefghij', spoken=spoken)
            self.assertEqual(original_duration('abcdefghij', spoken, 0.5, 0.25),
                             chinese_minimum_duration(entry, 0.5, 0.25),
                             repr(spoken))

    def test_first_six_and_extra_are_configurable(self):
        entry = word(1, spoken='一二三四五六七八')
        self.assertAlmostEqual(6 * 0.5 + 2 * 0.25,
                               chinese_minimum_duration(entry, 0.5, 0.25))
        self.assertAlmostEqual(original_duration('word', '一二三四五六七八', 0.5, 0.25),
                               chinese_minimum_duration(entry, 0.5, 0.25))


class ValidationTests(unittest.TestCase):
    def test_rejects_empty_lesson_and_oversized_batch(self):
        with self.assertRaises(ValueError):
            build_timeline(lesson([]), [])
        entries = [word(1), word(2)]
        with self.assertRaises(ValueError):
            build_timeline(lesson(entries, batch_size=1), assets_for(entries))

    def test_rejects_missing_duplicate_and_unexpected_assets(self):
        entry = word(1)
        full = assets_for([entry])
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), full[:2])
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), full + [full[0]])
        extra = asset(99, 'female', 'ghost')
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), full + [extra])

    def test_rejects_invalid_role(self):
        entry = word(1)
        speech = assets_for([entry])
        speech[0] = asset(1, 'robot', entry.word)
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)

    def test_rejects_mismatching_text(self):
        entry = word(1, text='alpha', spoken='你好世界')
        speech = assets_for([entry])
        speech[0] = asset(1, 'female', 'wrong')
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)
        speech = assets_for([entry])
        speech[2] = asset(1, 'chinese', 'alpha')
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)

    def test_rejects_nonfinite_or_negative_values(self):
        entry = word(1)
        for bad in (float('nan'), float('inf'), float('-inf'), -1.0):
            with self.assertRaises(ValueError):
                build_timeline(lesson([entry], speed=bad), assets_for([entry]))
            with self.assertRaises(ValueError):
                build_timeline(lesson([entry], intro_s=bad), assets_for([entry]))
        speech = assets_for([entry])
        speech[0] = asset(1, 'female', entry.word, duration=float('nan'))
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)
        speech = assets_for([entry])
        speech[0] = asset(1, 'female', entry.word, rendered=float('inf'))
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)

    def test_rejects_invalid_settings_types(self):
        entry = word(1)
        for kwargs in ({'fps': True}, {'fps': 0}, {'width': -1}, {'height': 2.5},
                       {'batch_size': False}, {'gap_s': float('nan')}):
            with self.assertRaises(ValueError):
                build_timeline(lesson([entry], **kwargs), assets_for([entry]))

    def test_rejects_negative_first_six_and_extra(self):
        entry = word(1)
        for kwargs in ({'first_six': -0.1}, {'extra': -1.0},
                       {'first_six': float('-inf')}, {'extra': float('nan')}):
            with self.assertRaises(ValueError):
                build_timeline(lesson([entry], **kwargs), assets_for([entry]))
            with self.assertRaises(ValueError):
                split_lesson(lesson([entry], batch_size=5, **kwargs))

    def test_rejects_bad_asset_metadata(self):
        entry = word(1)
        speech = assets_for([entry])
        speech[0] = asset(1, 'female', entry.word)
        speech[0] = SpeechAsset(word_index=0, role='female', text=entry.word,
                                path='f.wav', duration_s=0.2, rendered_duration_s=0.2)
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)
        speech = assets_for([entry])
        speech[0] = SpeechAsset(word_index=1, role='female', text=entry.word,
                                path='', duration_s=0.2, rendered_duration_s=0.2)
        with self.assertRaises(ValueError):
            build_timeline(lesson([entry]), speech)


class SrtExportTests(unittest.TestCase):
    def setUp(self):
        # Output goes directly into this (pre-existing) tests directory: the
        # DSH sandbox denies writes inside directories created during the run,
        # and %TEMP% is outside the workspace.  Every artifact is removed again.
        self.root = os.path.dirname(os.path.abspath(__file__))
        self.created = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for path in self.created:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def sample_manifest(self, first=1):
        entries = [
            word(first, text='apple', spoken='你好世界',
                 phonetic='/ˈæpəl/', meaning='n. 苹果'),
            word(first + 1, text='book', spoken='书',
                 phonetic='/bʊk/', meaning='n. 书'),
        ]
        spec = lesson(entries, intro_s=0.0, fps=60, speed=1.25, gap_s=0.0)
        return build_timeline(spec, assets_for(entries, 0.1, 0.1))

    def _targets(self, manifest):
        base = '%04d-%04d' % (manifest.first_index, manifest.last_index)
        return [os.path.join(self.root, base + suffix) for suffix in SRT_SUFFIXES]

    def _export(self, manifest):
        for path in self._targets(manifest):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        paths = export_srts(manifest, self.root)
        self.created.extend(paths)
        return paths

    def test_creates_five_bom_files_with_range_names(self):
        manifest = self.sample_manifest()
        paths = self._export(manifest)
        self.assertEqual(5, len(paths))
        base = '0001-0002'
        self.assertEqual(
            {base + suffix for suffix in SRT_SUFFIXES},
            {os.path.basename(p) for p in paths})
        for path in paths:
            with open(path, 'rb') as stream:
                self.assertTrue(stream.read().startswith(b'\xef\xbb\xbf'))
            with open(path, 'r', encoding='utf-8-sig') as stream:
                self.assertNotIn('countdown', stream.read())

    def test_exact_content_numbering_and_intro_offset(self):
        entry = word(1, text='apple', spoken='你好世界',
                     phonetic='/ˈæpəl/', meaning='n. 苹果')
        manifest = build_timeline(
            lesson([entry], intro_s=1.0, fps=60, speed=1.0, gap_s=0.0),
            assets_for([entry], 0.1, 0.1))
        paths = self._export(manifest)
        expected = [
            '1\n00:00:01,000 --> 00:00:02,000\napple\n\n'
            '2\n00:00:02,000 --> 00:00:03,000\napple\n\n',
            '1\n00:00:01,000 --> 00:00:04,600\napple\n\n',
            '1\n00:00:02,000 --> 00:00:04,600\n/ˈæpəl/\n\n',
            '1\n00:00:03,000 --> 00:00:04,600\nn. 苹果\n\n',
            '1\n00:00:03,000 --> 00:00:04,600\n你好世界\n\n',
        ]
        for index, path in enumerate(paths):
            with open(path, 'r', encoding='utf-8-sig') as stream:
                self.assertEqual(expected[index], stream.read(),
                                 SRT_SUFFIXES[index])
        self.assertEqual(60, manifest.intro_frames)
        self.assertEqual(276, manifest.words[0]['end_frame'])

    def test_rounds_frames_to_milliseconds_and_numbers_sequentially(self):
        manifest = self.sample_manifest()
        fps = manifest.fps
        paths = self._export(manifest)
        contents = []
        for path in paths:
            with open(path, 'r', encoding='utf-8-sig') as stream:
                contents.append(stream.read())
        # Independent expectation built from manifest frames with round().
        def fmt(ms):
            h, rest = divmod(ms, 3600000)
            m, rest = divmod(rest, 60000)
            s, ms = divmod(rest, 1000)
            return '%02d:%02d:%02d,%03d' % (h, m, s, ms)
        ms = lambda frame: int(round(frame * 1000 / fps))
        tracks = [[] for _ in SRT_SUFFIXES]
        numbers = [0] * 5
        for w in manifest.words:
            starts = {k: ms(w[k]) for k in
                      ('start_frame', 'male_frame', 'chinese_frame', 'end_frame')}
            def cue(num, a, b, text):
                return '%d\n%s --> %s\n%s\n\n' % (
                    num, fmt(starts[a]), fmt(starts[b]), text)
            numbers[0] += 1
            tracks[0].append(cue(numbers[0], 'start_frame', 'male_frame', w['word']))
            numbers[0] += 1
            tracks[0].append(cue(numbers[0], 'male_frame', 'chinese_frame', w['word']))
            numbers[1] += 1
            tracks[1].append(cue(numbers[1], 'start_frame', 'end_frame', w['word']))
            numbers[2] += 1
            tracks[2].append(cue(numbers[2], 'male_frame', 'end_frame', w['phonetic']))
            numbers[3] += 1
            tracks[3].append(cue(numbers[3], 'chinese_frame', 'end_frame', w['meaning']))
            numbers[4] += 1
            tracks[4].append(cue(numbers[4], 'chinese_frame', 'end_frame', w['spoken_meaning']))
        self.assertEqual([''.join(t) for t in tracks], contents)
        # 2 words -> four repeat cues, one cue per word in each other track.
        self.assertEqual([4, 2, 2, 2, 2], [c.count('-->') for c in contents])
        self.assertIn('00:00:04,883', contents[1])

    def test_rejects_existing_destination_without_partial_output(self):
        manifest = self.sample_manifest(first=11)
        targets = self._targets(manifest)
        for path in targets:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        blocker = targets[3]
        with open(blocker, 'w', encoding='utf-8') as stream:
            stream.write('keep')
        self.created.append(blocker)
        with self.assertRaises(FileExistsError):
            export_srts(manifest, self.root)
        with open(blocker, encoding='utf-8') as stream:
            self.assertEqual('keep', stream.read())
        for path in targets:
            if path != blocker:
                self.assertFalse(os.path.exists(path), path)


class IntroClipTests(unittest.TestCase):
    """A supplied intro clip owns the intro duration; words start at its end."""

    def _clip(self, folder, seconds=3.008, video_seconds=None):
        """Build a clip whose audio is ``seconds`` long and video ``video_seconds``."""
        from word_video.media import run, executable
        clip = Path(folder) / 'intro.mov'
        length = video_seconds if video_seconds is not None else seconds
        args = [executable('ffmpeg'), '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
                'color=c=black:s=320x180:r=60:d=%.3f' % length,
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=%.3f' % seconds,
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac']
        if video_seconds is None or video_seconds >= seconds:
            # A normal clip: both streams end together.
            args += ['-shortest']
        else:
            # Deliberately inconsistent: keep the longer audio stream intact so
            # the validator has something real to reject.
            args += ['-map', '0:v:0', '-map', '1:a:0']
        run(args + [str(clip)])
        return str(clip)

    def _assets(self, word_index=1):
        entry = WordEntry(word_index, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')
        raw = Path(tempfile.mkdtemp()) / 'raw.wav'
        with wave.open(str(raw), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
            stream.writeframes(b'\x01\x00' * 4800)
        items = [SpeechAsset(word_index, role, '苹果' if role == 'chinese' else 'apple',
                             str(raw), .1, .1, 'test')
                 for role in ('female', 'male', 'chinese')]
        return entry, items

    def test_clip_length_decides_where_words_start(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = self._clip(folder)
            entry, items = self._assets()
            lesson = LessonSpec([entry], fps=60, background=clip,
                                intro={'video': clip}, intro_s=999.0)
            manifest = build_timeline(lesson, items)
            self.assertEqual(181, manifest.intro_frames)   # ceil(3.008 * 60)
            self.assertEqual(clip, manifest.intro_video)
            self.assertEqual(181, manifest.words[0]['start_frame'])

    def test_video_shorter_than_audio_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = self._clip(folder, seconds=3.0, video_seconds=1.0)
            entry, items = self._assets()
            lesson = LessonSpec([entry], background=clip, intro={'video': clip})
            with self.assertRaises(ValueError) as caught:
                build_timeline(lesson, items)
            self.assertIn('shorter than its audio', str(caught.exception))

    def test_missing_clip_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            entry, items = self._assets()
            lesson = LessonSpec([entry], background=str(Path(folder) / 'bg.mp4'),
                                intro={'video': str(Path(folder) / 'nope.mov')})
            with self.assertRaises(FileNotFoundError):
                build_timeline(lesson, items)

    def test_split_lesson_keeps_the_clip(self):
        """Batches are what build_timeline reads, so they must carry the clip.

        Losing it here silently falls back to ``intro_s`` — a synthetic intro
        of the wrong length instead of the reference countdown.
        """
        with tempfile.TemporaryDirectory() as folder:
            clip = self._clip(folder)
            entry, items = self._assets()
            lesson = LessonSpec([entry], fps=60, background=clip,
                                intro={'video': clip}, batch_size=1)
            batches = split_lesson(lesson)
            self.assertEqual(1, len(batches))
            self.assertEqual({'video': clip}, batches[0].intro)
            manifest = build_timeline(batches[0], items)
            self.assertEqual(181, manifest.intro_frames)
            self.assertEqual(clip, manifest.intro_video)
            self.assertEqual(181, manifest.words[0]['start_frame'])

    def test_no_clip_keeps_intro_s_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            clip = self._clip(folder)
            entry, items = self._assets()
            lesson = LessonSpec([entry], fps=60, background=clip, intro_s=0.5)
            manifest = build_timeline(lesson, items)
            self.assertEqual(30, manifest.intro_frames)
            self.assertEqual('', manifest.intro_video)


class SplitLessonFidelityTests(unittest.TestCase):
    """Batches are what the renderer reads, so no lesson setting may be dropped.

    ``split_lesson`` rebuilds a LessonSpec per batch.  Twice a newly added
    setting was silently forgotten there (the intro clip, then the video codec)
    and the job quietly fell back to a default — a wrong intro length once, and
    h264 output when h265 was requested.  This compares every field so the next
    setting cannot be lost the same way.
    """

    def test_every_setting_survives_the_split(self):
        # Imported here on purpose: other suites reassign same-named helpers at
        # module scope, and those bindings win over this module's imports.
        from word_video.contracts import LessonSpec as Spec
        from word_video.contracts import WordEntry as Entry

        def entry(index):
            return Entry(index, 'alpha', 'p', 'm', '你好世界')
        lesson = Spec(
            [entry(1), entry(2), entry(3)],
            title='T', footer='F', batch_size=2, speed=1.5, fps=30,
            width=1920, height=1080, intro_s=1.25, first_six=.35, extra=.15,
            gap_s=.05, background='bg.mp4', intro_audio='in.wav',
            intro={'video': 'clip.mov', 'audio': 'clip.wav'},
            video_codec='h265', styles={'english': {'size': 1}},
            fonts={'bold': 'f.otf'})
        batches = split_lesson(lesson)
        indexes = [[e.index for e in b.entries] for b in batches]
        if indexes != [[1, 2], [3]]:
            raise AssertionError('split_lesson grouped %r (entries built: %r)'
                                 % (indexes, [e.index for e in lesson.entries]))
        self.assertEqual(2, len(batches))
        wanted = {k: v for k, v in asdict(lesson).items() if k != 'entries'}
        for batch in batches:
            actual = {k: v for k, v in asdict(batch).items() if k != 'entries'}
            # Compare serialized values so containers of dataclasses compare by
            # content rather than by list/dict identity.
            self.assertEqual(json.dumps(wanted, sort_keys=True),
                             json.dumps(actual, sort_keys=True),
                             'split_lesson changed a batch setting')


if __name__ == '__main__':
    unittest.main()
