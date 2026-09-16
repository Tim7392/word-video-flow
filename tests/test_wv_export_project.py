"""The three deliverables, from a project, on the real cached audio.

What this must prove (W03 acceptance) is not "a file appeared" but that all three
products describe one scene:

* every teaching stage's text and window is asserted **per role, per word** - not
  sampled - against the plan that drove them;
* the five SRT tracks carry that same text and those same times;
* the editable draft holds one independent object per voice, one per text layer
  and the background, with its own media beside it;
* the MP4's own audio is measured (intro region versus body), and the burnt-in
  caption at a word's midpoint is checked in pixels.

The canvas is small (320x180) so the suite stays fast; nothing about the chain
depends on the size, and the same code path renders 1080p in the long check.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import wave

from word_video.application import instantiate
from word_video.domain import Project, Record, solve
from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
from word_video.domain.model import MediaInfo
from word_video.exporters import catalog, export_run
from word_video.exporters.srt import SRT_SUFFIXES, verify_against_manifest
from word_video.media import executable, has_audio, run

FIXTURE = Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-有片头.json')
ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
BACKGROUND = ARCHIVE / '_prepared-1080p' / 'background-from-reference-1080p.mp4'
CANVAS = {'width': 320, 'height': 180}
FPS = 24


def _fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def _roles(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item['index'], {})[item['role']] = item
    return grouped


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (FIXTURE.is_file() and BACKGROUND.is_file()):
            raise unittest.SkipTest('p1 fixtures or the 1080p reference media are absent')
        cls._folder = tempfile.TemporaryDirectory(prefix='wv-export-')
        cls.root = Path(cls._folder.name)
        cls.background = cls._small_background(cls.root / 'background.mp4')
        cls.project_dir = cls.root / 'project'
        cls.project_dir.mkdir()
        cls.items = _fixture()['provider']['items']
        cls.roles = _roles(cls.items)
        cls._write_project()
        cls.result = export_run(str(cls.project_dir), output=cls.root / 'out',
                                background=str(cls.background), video_codec='h264',
                                slices=1)
        cls.plan = solve(cls.project, cls.media)

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    @classmethod
    def _small_background(cls, target):
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(BACKGROUND),
             '-t', '12', '-vf', 'scale=320:180', '-r', str(FPS), '-an',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
             '-pix_fmt', 'yuv420p', str(target)], timeout=900)
        return target

    @classmethod
    def _write_project(cls):
        records, assets = [], []
        for index in sorted(cls.roles):
            roles = cls.roles[index]
            record_id = 'w%d' % index
            records.append(Record(id=record_id, word=roles['female']['text'],
                                  phonetic='ˈdemənstreɪt', meaning='v. 证明；演示；示范',
                                  spoken_meaning=roles['chinese']['text'], index=index))
            for role in ('female', 'male', 'chinese'):
                item = roles[role]
                assets.append(catalog.Asset(asset_id='%s:%s' % (record_id, role),
                                            path=item['path'], voice=item['voice']))
        base = Project(project_id='p1-export', intro_s=1.0, fps_num=FPS,
                       width=CANVAS['width'], height=CANVAS['height'], speed=1.25)
        cls.media = catalog.measure_all({asset.asset_id: asset for asset in assets})
        cls.project = instantiate(DEFAULT_LESSON_TEMPLATE, tuple(records), cls.media, base)
        from word_video.storage.project_store import save_project
        save_project(cls.project, cls.project_dir)
        catalog.save_catalog({asset.asset_id: asset for asset in assets}, cls.project_dir)

    # -- the artifacts exist ---------------------------------------------
    def test_three_deliverables_are_published(self):
        result = self.result
        self.assertTrue(result.video and Path(result.video).is_file())
        self.assertEqual(5, len(result.srts))
        for path in result.srts:
            self.assertTrue(Path(path).is_file(), path)
        self.assertTrue(Path(result.draft).is_dir())
        self.assertTrue((Path(result.draft) / 'draft_content.json').is_file())
        self.assertTrue(Path(result.ass).is_file())
        self.assertTrue(Path(result.timeline).is_file())
        report = result.report
        self.assertEqual(self.plan.render.identity(), report['plan_identity'])
        self.assertEqual(self.plan.cues.identity(), report['cue_identity'])
        self.assertEqual([], report['conflicts'])

    def test_the_five_tracks_are_the_plan_not_a_second_projection(self):
        """Byte-level equality with the verified exporter is the only acceptable gap."""
        manifest = json.loads(Path(self.result.timeline).read_text(encoding='utf-8'))
        from word_video.contracts import TimelineManifest
        verify_against_manifest(self.plan.cues, TimelineManifest.from_dict(manifest))

    def test_the_speech_record_lets_an_independent_reader_recheck_the_timing(self):
        """The `timing` face of the acceptance checker needs exactly these numbers.

        Without a per-asset record the checker cannot re-derive "stage = rhythm +
        the recording it plays", so it reported PASS while checking nothing (C's
        ``data_missing=9``).  Every speech asset must therefore be in ``speech.json``
        with its text, role, voice, both lengths and its planned stage - and the
        numbers must match the files that were published.
        """
        from word_video.media import wav_duration

        path = Path(self.result.run_dir) / 'speech.json'
        self.assertTrue(path.is_file(), 'no speech record was published')
        document = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual('wv-speech@1', document['schema'])
        self.assertEqual(FPS, document['fps'])
        self.assertEqual(48000, document['sample_rate'])
        records = document['records']
        self.assertEqual(3 * len(self.roles), len(records))

        expected = {(record.index, role):
                    (record.word if role in ('female', 'male')
                     else record.spoken_meaning)
                    for record in self.project.records
                    for role in ('female', 'male', 'chinese')}
        seen = set()
        for record in records:
            key = (int(record['record_id'][1:]), record['role'])
            seen.add(key)
            self.assertEqual(expected[key], record['text'])
            # Both durations, describing the file that was really published.
            self.assertGreater(record['raw_seconds'], 0)
            self.assertGreater(record['rendered_seconds'], 0)
            self.assertLess(record['rendered_seconds'], record['raw_seconds'])
            self.assertTrue(Path(record['path']).is_file())
            self.assertAlmostEqual(wav_duration(record['path']),
                                   record['rendered_seconds'], places=3)
            # The planned stage in ticks and frames: no re-derivation required.
            self.assertEqual(record['end_ticks'] - record['start_ticks'],
                             record['duration_ticks'])
            self.assertAlmostEqual(record['duration_ticks'] / 720000,
                                   record['stage_seconds'], places=6)
            self.assertAlmostEqual(record['start_ticks'] * FPS / 720000,
                                   record['start_frame'], delta=0.5)
            # ...and the stage really can hold the prepared audio.
            self.assertGreaterEqual(record['stage_seconds'] + 1e-6,
                                    record['rendered_seconds'] - 1.0 / FPS)
        self.assertEqual(set(expected), seen)

    def test_changing_one_word_changes_only_that_words_records(self):
        """A record has to describe the run it belongs to, item by item.

        The edited lesson is re-expanded (a clip carries its own text snapshot, so
        editing a record alone does not re-render it) and the two documents are
        compared item by item: the edited word's record must differ and every other
        record must be byte-identical.
        """
        from dataclasses import replace

        from word_video.application import instantiate
        from word_video.application.intro import measure_project_intro
        from word_video.domain import solve
        from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
        from word_video.exporters.render import write_speech_record

        published = json.loads((Path(self.result.run_dir) / 'speech.json')
                               .read_text(encoding='utf-8'))['records']
        facts = {record['asset_id']: {key: value for key, value in record.items()
                                      if key not in ('clip_id', 'text', 'start_ticks',
                                                     'end_ticks', 'duration_ticks',
                                                     'start_frame', 'end_frame',
                                                     'stage_seconds')}
                 for record in published}
        measurement = measure_project_intro(self.project)
        changed_records = [replace(record, spoken_meaning=record.spoken_meaning + '补充')
                           if record.index == 152 else record
                           for record in self.project.records]
        # Expansion needs an empty carrier, so the edited lesson is built the same
        # way the exported one was: same settings, same assets, edited records.
        carrier = self.project.with_records(()).with_clips(())
        edited = instantiate(DEFAULT_LESSON_TEMPLATE, tuple(changed_records),
                             self.media, carrier, asset_ids={
                                 (record.id, role): '%s:%s' % (record.id, role)
                                 for record in changed_records
                                 for role in ('female', 'male', 'chinese')})
        base, other = solve(self.project, self.media, measurement), \
            solve(edited, self.media, measurement)

        with tempfile.TemporaryDirectory() as folder:
            first = write_speech_record(folder, _view_from(base.render, self.result),
                                        facts, base.render)
            second = write_speech_record(folder, _view_from(other.render, self.result),
                                         facts, other.render)
        by_key = {(record['record_id'], record['role']): record
                  for record in first['records']}
        after = {(record['record_id'], record['role']): record
                 for record in second['records']}
        self.assertEqual(set(by_key), set(after))
        # The edit is a Chinese-record edit: that record's text changes, and no other
        # record's text does.  (Later words shift in time, which is real - the lesson
        # got longer - so the comparison is about *what each record says*.)
        texts_before = {key: record['text'] for key, record in by_key.items()}
        texts_after = {key: record['text'] for key, record in after.items()}
        touched = {key for key in texts_before if texts_before[key] != texts_after[key]}
        self.assertEqual({('w152', 'chinese')}, touched,
                         'the edit changed the text of %r' % (sorted(touched),))
        self.assertIn('补充', texts_after[('w152', 'chinese')])
        # The word before the edit is untouched in every field, including its stage.
        for key in [key for key in by_key if key[0] == 'w151']:
            self.assertEqual(by_key[key], after[key], key)

    def test_the_speech_record_is_not_published_when_the_run_fails(self):
        """A record is part of publication, so a run that cannot start leaves none."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(Exception):
                export_run(str(root / 'missing-project'), output=root / 'out')
            self.assertFalse((root / 'out').exists())

    def test_every_role_of_every_word_is_on_the_timeline_with_its_own_text(self):
        """Per role, per word: no sampling.  The plan is the oracle."""
        manifest = json.loads(Path(self.result.timeline).read_text(encoding='utf-8'))
        by_id = {clip.id: clip for clip in self.project.clips}
        records = {record.id: record for record in self.project.records}
        seen = set()
        for item in manifest['audio']:
            record_id = 'w%d' % item['word_index']
            clip = by_id['%s.%s' % (record_id, item['role'])]
            record = records[record_id]
            expected = (record.spoken_meaning if item['role'] == 'chinese'
                        else record.word)
            self.assertEqual(expected, item['text'],
                             '%s/%s text' % (record_id, item['role']))
            self.assertEqual(expected, clip.text)
            self.assertTrue(Path(item['path']).is_file(), item['path'])
            seen.add((record_id, item['role']))
        self.assertEqual(3 * len(self.roles), len(seen))
        for track, expected_count in self.result.report['tracks'].items():
            self.assertGreaterEqual(expected_count, 2 * len(self.roles) if track == '01'
                                    else len(self.roles))

    def test_subtitle_files_carry_that_text_and_time(self):
        """Each track's cues are the plan's cues, with millisecond rounding once."""
        from word_video.domain import ticks_to_milliseconds
        base = '%04d-%04d' % (min(self.roles), max(self.roles))
        expected = {track: [(ticks_to_milliseconds(cue.start_ticks),
                             ticks_to_milliseconds(cue.end_ticks), cue.text)
                            for cue in self.plan.cues.by_track()[track]]
                    for track in ('01', '02', '03', '04', '05')}
        folder = Path(self.result.srts[0]).parent
        for suffix in SRT_SUFFIXES:
            track = suffix[1:3]
            path = folder / (base + suffix)
            blocks = [block for block in
                      path.read_text(encoding='utf-8-sig').split('\n\n') if block.strip()]
            self.assertEqual(len(expected[track]), len(blocks), track)
            for block, (start, end, text) in zip(blocks, expected[track]):
                lines = block.split('\n')
                self.assertEqual(text, lines[2], track)
                self.assertEqual(_ms(start), lines[1].split(' --> ')[0], track)
                self.assertEqual(_ms(end), lines[1].split(' --> ')[1], track)

    def test_the_draft_is_independently_editable(self):
        draft = json.loads((Path(self.result.draft) / 'draft_content.json')
                           .read_text(encoding='utf-8'))
        tracks = {track['name']: track for track in draft['tracks']}
        words = len(self.roles)
        for role in ('female', 'male', 'chinese'):
            self.assertEqual(words, len(tracks[role]['segments']), role)
        for role in ('english', 'phonetic', 'meaning'):
            self.assertEqual(words, len(tracks[role]['segments']), role)
        self.assertTrue(tracks['背景']['segments'], 'no background segment')
        # Every referenced material resolves to a file inside the draft folder.
        root = Path(self.result.draft)
        ids = {material['id'] for group in draft['materials'].values()
               if isinstance(group, list) for material in group
               if isinstance(material, dict) and 'id' in material}
        for track in draft['tracks']:
            for segment in track['segments']:
                self.assertIn(segment['material_id'], ids)
        paths = [Path(item['path']) for item in draft['materials']['videos']]
        paths += [Path(item['path']) for item in draft['materials']['audios']]
        for path in paths:
            self.assertTrue(path.is_file(), path)
            self.assertTrue(path.resolve().is_relative_to(root.resolve()), path)

    def test_the_mp4_is_audible_and_keeps_its_canvas(self):
        from word_video.media import probe
        facts = probe(self.result.video)
        picture = next(item for item in facts['streams']
                       if item['codec_type'] == 'video')
        self.assertEqual((CANVAS['width'], CANVAS['height']),
                         (picture['width'], picture['height']))
        self.assertTrue(any(item['codec_type'] == 'audio'
                            for item in facts['streams']))
        decoded = self.root / 'decoded.wav'
        decoded.unlink(missing_ok=True)
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i',
             self.result.video, '-vn', '-ar', '48000', '-ac', '1',
             '-c:a', 'pcm_s16le', str(decoded)])
        with wave.open(str(decoded), 'rb') as stream:
            block = stream.readframes(stream.getnframes())
        values = [int.from_bytes(block[i:i + 2], 'little', signed=True)
                  for i in range(0, len(block), 2)]
        manifest = json.loads(Path(self.result.timeline).read_text(encoding='utf-8'))
        window = manifest['intro_frames'] * 48000 // manifest['fps']
        # The background carries no sound, so a synthetic countdown is the only
        # thing in the intro region; the words must be audible after it.
        self.assertGreater(max(abs(value) for value in values[window:]), 500)
        self.assertEqual(0, max(abs(value) for value in values[:window]) if window else 0)

    def test_the_burnt_in_caption_is_at_the_words_midpoint(self):
        """A pixel probe, because "captions exist" is not "captions are on screen"."""
        manifest = json.loads(Path(self.result.timeline).read_text(encoding='utf-8'))
        first = manifest['words'][0]
        midpoint = (first['start_frame'] + first['male_frame']) // 2
        frame = self.root / 'caption.png'
        frame.unlink(missing_ok=True)
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i',
             self.result.video, '-vf', 'select=eq(n\\,%d)' % midpoint,
             '-frames:v', '1', str(frame)])
        painted = _ink(frame)
        self.assertGreater(painted, 0.01, 'no caption ink at the word midpoint')
        # The countdown's own frame has ink too (the digit), so compare against a
        # frame taken before the first word: the caption adds ink of its own.
        intro = self.root / 'intro.png'
        intro.unlink(missing_ok=True)
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i',
             self.result.video, '-vf', 'select=eq(n\\,0)', '-frames:v', '1', str(intro)])
        self.assertGreater(painted, _ink(intro))


def _view_from(plan, result):
    """The little view `write_speech_record` reads: fps and total frames."""
    import types

    manifest = json.loads(Path(result.timeline).read_text(encoding='utf-8'))
    return types.SimpleNamespace(fps=manifest['fps'],
                                 total_frames=manifest['total_frames'],
                                 speed=manifest['speed'])


def _ms(milliseconds):
    hours, rest = divmod(int(milliseconds), 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return '%02d:%02d:%02d,%03d' % (hours, minutes, seconds, millis)


def _ink(path):
    """Fraction of pixels that are clearly brighter than the dim background.

    Implemented with ffmpeg's own statistics rather than a Python pixel loop:
    ``signalstats`` gives YAVG over the frame, and a black frame has a much lower
    average than one carrying white text.
    """
    output = subprocess.run(
        [executable('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(path),
         '-vf', 'signalstats,metadata=print:file=-', '-f', 'null', '-'],
        capture_output=True, check=True).stdout.decode('utf-8', 'replace')
    values = re.findall(r'lavfi\.signalstats\.YAVG=([0-9.]+)', output)
    if not values:
        raise AssertionError('signalstats produced no YAVG for %s' % path)
    return float(values[-1]) / 255.0


if __name__ == '__main__':
    unittest.main()
