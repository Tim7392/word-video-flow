"""A background the member cut in two: exported as two pieces, never as one.

W06 offers "split this clip" on the background layer, so the picture has to survive
it: if the projection quietly drew only the first piece, a delivered lesson would be
missing half its background and nothing would say so.  These tests pin the two
allowed outcomes - both pieces present, or a structured refusal - and the red line
for the change: a background that was *not* cut produces exactly what it produced
before this feature existed.

Style overrides are covered here too, because both are "the project says something
the exporter used to decide": the merged table (defaults + overrides) must reach
the captions, and a bare override must not be handed to the layout, whose neutral
fallback would otherwise silently become the size of every role it did not name.
"""
import json
from pathlib import Path
import tempfile
import unittest

from word_video.application import SplitClip, apply, instantiate
from word_video.domain import MediaInfo, MediaSlice, Project, Record, solve
from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
from word_video.domain.model import StyleOverride
from word_video.exporters.background import (BackgroundError, background_segments,
                                             build_background_track)
from word_video.exporters.plan import ProjectionError, SourceMedia, build_manifest
from word_video.layout import LayoutSurface
from word_video.media import executable, run
from word_video.template import default_styles

BACKGROUND = 'bg.mp4'


def _record(index=1, word='apple'):
    return Record(id='w%d' % index, word=word, phonetic='/ˈæpəl/', meaning='n. 苹果',
                  spoken_meaning='苹果', index=index)


def _media(records):
    return {'%s:%s' % (record.id, role): MediaInfo('%s:%s' % (record.id, role), 48000)
            for record in records for role in ('female', 'male', 'chinese')}


def _sources(records, background='bg.mp4'):
    sources = {'%s:%s' % (record.id, role): SourceMedia(path='unused.wav',
                                                        voice='V_%s' % role)
               for record in records for role in ('female', 'male', 'chinese')}
    sources['layer.background'] = SourceMedia(path=background)
    return sources


def expected_frames(solution):
    """Total frames the plan asks for, at 30 fps (the rate these tests use)."""
    from fractions import Fraction

    return int(Fraction(solution.render.total_ticks * 30, 720000))


class SplitBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clip = self.root / 'background.mp4'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'testsrc=size=160x90:rate=30:duration=0.4', '-an',
             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.clip)])

    def _project(self, *, record_count=1, split=None):
        records = [_record(index) for index in range(1, record_count + 1)]
        media = _media(records)
        # A background long enough to cut: the source window covers the whole
        # lesson stage (3.8 s here), which is what `SplitClip` needs to find an
        # inner unit on the source grid.
        background = MediaSlice('layer.background', 0, 200000)   # ~4.17 s at 48 kHz
        project = instantiate(
            DEFAULT_LESSON_TEMPLATE, tuple(records), media,
            Project(project_id='split-bg', intro_s=0.5, fps_num=30, width=320,
                    height=180, speed=1.0),
            background=background)
        if split is not None:
            project = apply(project, SplitClip('layer.background', split)).project
        return project, media

    def _manifest(self, project, media, **kwargs):
        solution = solve(project, media)
        view = build_manifest(solution.render, project,
                              _sources([r for r in project.records],
                                       background=str(self.clip)),
                              background=str(self.clip), styles=default_styles(),
                              **kwargs)
        return solution, view

    def test_an_uncut_background_is_projected_exactly_as_before(self):
        """The red line: no `background_segments`, one file, unchanged document."""
        project, media = self._project()
        solution, view = self._manifest(project, media)
        self.assertEqual([], view.background_segments)
        document = view.to_dict()
        self.assertNotIn('background_segments', document)
        self.assertEqual(str(self.clip), view.background)
        # The document carries exactly the fields the verified chain always wrote.
        self.assertEqual(
            {'schema_version', 'title', 'subtitle', 'footer', 'first_index',
             'last_index', 'fps', 'width', 'height', 'speed', 'intro_frames',
             'total_frames', 'background', 'intro_audio', 'intro_video',
             'video_codec', 'styles', 'words', 'audio'}, set(document))
        self.assertEqual(15, view.intro_frames)                     # 0.5 s at 30 fps
        self.assertEqual(expected_frames(solution), view.total_frames)
        pieces = background_segments(solution.render,
                                     _sources(project.records, str(self.clip)))
        self.assertEqual(1, len(pieces))
        self.assertEqual('layer.background', pieces[0].clip_id)

    def test_a_split_background_leaves_two_contiguous_pieces(self):
        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        project = apply(project, SplitClip('layer.background', total // 2)).project
        solution, view = self._manifest(project, media)
        pieces = background_segments(solution.render,
                                     _sources(project.records, str(self.clip)))
        self.assertEqual(['layer.background', 'layer.background.2'],
                         [piece.clip_id for piece in pieces])
        left, right = pieces
        self.assertEqual(left.end_ticks, right.start_ticks)   # no hole, no overlap
        self.assertEqual(left.source_end, right.source_start)  # contiguous windows
        self.assertLess(left.source_start, left.source_end)
        self.assertLess(right.source_start, right.source_end)
        # Both pieces carry a file and a stage; the file is the one to loop.
        for piece in pieces:
            self.assertTrue(Path(piece.path).is_file())
            self.assertGreater(piece.stage_seconds, 0)

    def test_a_hole_between_the_pieces_is_refused_with_the_item(self):
        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        project = apply(project, SplitClip('layer.background', total // 2)).project
        # Move the second piece later, leaving a hole nothing would fill.
        from dataclasses import replace
        from word_video.domain import TimeExpr

        clips = tuple(replace(clip, start=TimeExpr.at(clip.start.ticks + 24000),
                              duration_ticks=clip.duration_ticks - 24000)
                      if clip.id == 'layer.background.2' else clip
                      for clip in project.clips)
        holed = project.with_clips(clips)
        solution = solve(holed, media)
        with self.assertRaises(BackgroundError) as caught:
            background_segments(solution.render, _sources(project.records, str(self.clip)))
        self.assertIn('hole', str(caught.exception))
        self.assertIn('layer.background', str(caught.exception))
        self.assertIn('layer.background.2', str(caught.exception))

    def test_an_overlap_between_the_pieces_is_refused_with_the_item(self):
        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        project = apply(project, SplitClip('layer.background', total // 2)).project
        from dataclasses import replace
        from word_video.domain import TimeExpr

        clips = tuple(replace(clip, start=TimeExpr.at(clip.start.ticks - 24000),
                              duration_ticks=clip.duration_ticks + 24000)
                      if clip.id == 'layer.background.2' else clip
                      for clip in project.clips)
        overlapped = project.with_clips(clips)
        solution = solve(overlapped, media)
        with self.assertRaises(BackgroundError) as caught:
            background_segments(solution.render, _sources(project.records, str(self.clip)))
        self.assertIn('overlap', str(caught.exception))

    def test_each_piece_loops_inside_its_own_stage(self):
        """A piece shorter than its stage repeats; the cut must not lose the loop.

        This is the normal shape of a real project - a short background under a long
        lesson - and a split must not turn "loop the background" into "play each half
        once", which would leave the second half of the lesson with no picture.
        """
        from word_video.domain import TimeExpr
        from word_video.domain.model import Clip

        records = [_record(index) for index in range(1, 3)]
        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        half = total // 2
        # Each piece gets a 0.2 s window (9600 units at 48 kHz) against a ~1.9 s
        # stage, so both have to repeat.
        clips = tuple(clip for clip in project.clips if clip.role != 'background')
        clips += (
            Clip(id='layer.background', role='background', start=TimeExpr.at(0),
                 duration_ticks=half,
                 source=MediaSlice('layer.background', 0, 9600)),
            Clip(id='layer.background.2', role='background', start=TimeExpr.at(half),
                 duration_ticks=total - half,
                 source=MediaSlice('layer.background', 9600, 19200)))
        project = project.with_clips(clips)
        solution = solve(project, media)
        pieces = background_segments(solution.render,
                                     _sources(project.records, str(self.clip)))
        self.assertEqual(2, len(pieces))
        for piece in pieces:
            self.assertTrue(piece.loops, piece.clip_id)
            self.assertGreater(piece.repeats, 1)
            self.assertAlmostEqual(0.2, piece.loop_seconds, places=2)
            self.assertGreater(piece.stage_seconds, 1.5)

    def test_the_assembled_pieces_cover_the_whole_lesson(self):
        """The picture has no hole: each piece is as long as its own stage.

        A piece that fell short would leave the frame empty for the rest of its
        stage - the silent failure this whole feature is about - so the check is on
        the assembled seconds, not on the plan alone.
        """
        from word_video.media.streams import video_stream_seconds

        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        project = apply(project, SplitClip('layer.background', total // 2)).project
        solution, view = self._manifest(project, media)
        pieces = background_segments(solution.render,
                                     _sources(project.records, str(self.clip)))
        outputs = [build_background_track([piece], self.root / ('piece-%d.mp4' % index),
                                          fps=view.fps, size=(view.width, view.height))
                   for index, piece in enumerate(pieces, start=1)]
        expected = view.total_frames / view.fps
        joined = 0.0
        for index, output in enumerate(outputs, start=1):
            seconds = video_stream_seconds(output)
            # Every piece covers its own stage (a frame of rounding allowed).
            self.assertGreaterEqual(seconds + 0.05, pieces[index - 1].stage_seconds)
            joined += seconds
        self.assertGreaterEqual(joined + 0.1, expected)
        self.assertLessEqual(joined, expected + 1.0)

    def _frames(self, path):
        out = run([executable('ffprobe'), '-v', 'error', '-count_frames',
                   '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames',
                   '-of', 'csv=p=0', path])
        return int(out.decode('utf-8').strip())

    def test_a_stage_that_is_not_a_whole_number_of_frames_still_exports(self):
        """QA's counter-example: 3.55 s of stage is 106.5 frames at 30 fps.

        ``round(106.5)`` is 106 in Python while the plan's half-up conversion asks for
        107, so the piece came out a frame short of the range the draft handed to
        剪映 - which then refused the whole draft ("读取媒体时间范围超出媒体时长")
        before a single picture was drawn.  A half frame is the only rounding where
        the two rules disagree, and the two-record lesson splits into exactly it:
        5112000 ticks, 213 frames, 2556000 per piece.
        """
        from dataclasses import replace

        from word_video.draft import export_draft
        from word_video.exporters.plan import frame_at

        project, media = self._project(record_count=2)
        total = project.clip('layer.background').duration_ticks
        self.assertEqual(2556000, total // 2)      # 106.5 frames at 30 fps
        project = apply(project, SplitClip('layer.background', total // 2)).project
        solution = solve(project, media)
        # Speech the draft can copy and re-time; its content plays no part here.
        speech = self.root / 'speech.wav'
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=0.2', '-ar', '48000', '-ac', '1',
             str(speech)])
        sources = {'%s:%s' % (record.id, role): SourceMedia(path=str(speech),
                                                            voice='V_%s' % role)
                   for record in project.records
                   for role in ('female', 'male', 'chinese')}
        sources['layer.background'] = SourceMedia(path=str(self.clip))
        view = build_manifest(solution.render, project, sources,
                              background=str(self.clip), styles=default_styles())
        self.assertEqual(213, view.total_frames)   # 7.1 s
        pieces = background_segments(solution.render, sources)
        wanted = [frame_at(piece.end_ticks, view.fps)
                  - frame_at(piece.start_ticks, view.fps) for piece in pieces]
        self.assertEqual([107, 106], wanted)
        built = []
        for index, piece in enumerate(pieces, start=1):
            target = self.root / ('piece-%d.mp4' % index)
            build_background_track([piece], target, fps=view.fps,
                                   size=(view.width, view.height))
            # The file carries the frames the plan reserves - 107 for the half that
            # lands on a half frame, not the 106 the nearest-even rounding gave.
            self.assertEqual(wanted[index - 1], self._frames(target), piece.clip_id)
            built.append(dict(piece.to_dict(), path=str(target)))
        # And the product that refused it - the editable draft - now takes it: one
        # segment per piece, each inside its own material, ending on the timeline.
        draft = self.root / 'editable-draft'
        export_draft(replace(view, background_segments=built), draft)
        document = json.loads((draft / 'draft_content.json').read_text(encoding='utf-8'))
        track = next(item for item in document['tracks'] if item['name'] == '背景')
        ranges = [(segment['target_timerange']['start'],
                   segment['target_timerange']['duration'])
                  for segment in track['segments']]
        self.assertEqual([(0, 3566667), (3566667, 3533333)], ranges)
        self.assertEqual(2, len({segment['material_id'] for segment in track['segments']}))
        self.assertEqual(7100000, ranges[-1][0] + ranges[-1][1])   # 213 frames
        durations = {material['id']: material['duration']
                     for material in document['materials']['videos']}
        for segment in track['segments']:
            span = segment['source_timerange']
            self.assertLessEqual(span['start'] + span['duration'],
                                 durations[segment['material_id']],
                                 'the draft asks a piece for more than its file holds')


class SplitIntroTests(unittest.TestCase):
    """A split intro is refused, not half-drawn.

    The state is built by hand rather than with ``SplitClip``: A tightened
    ``SPLITTABLE_ROLES`` to the background only, so the command refuses to create
    two intro items at all - which is better, because a member never reaches an
    unexportable project.  The projection still has to answer for a document that
    carries them (an older build, or a hand edit), and it answers by name.
    """

    def test_two_intro_items_are_refused_by_name(self):
        from dataclasses import replace

        from word_video.domain import IntroMeasurement, TimeExpr

        clip = 'D:/fixtures/intro.mp4'
        measurement = IntroMeasurement(asset_id=clip, seconds=1.0, sound_asset=clip,
                                       sound_source='FROM_CLIP', from_clip=True,
                                       picture_seconds=1.0)
        record = _record()
        project = instantiate(
            replace(DEFAULT_LESSON_TEMPLATE, intro=True), (record,), _media([record]),
            Project(project_id='split-intro', intro_s=2.0, fps_num=30),
            intro=MediaSlice(clip, 0, 48000), intro_measure=measurement)
        layer = project.clip('layer.intro')
        half = layer.duration_ticks // 2
        second = replace(layer, id='layer.intro.2',
                         start=TimeExpr.at(layer.start.ticks + half),
                         duration_ticks=layer.duration_ticks - half)
        project = project.with_clips(tuple(
            replace(item, duration_ticks=half) if item.id == 'layer.intro' else item
            for item in project.clips) + (second,))
        self.assertEqual(2, len([item for item in project.clips if item.role == 'intro']))
        solution = solve(project, _media([record]), measurement)
        with self.assertRaises(ProjectionError) as caught:
            build_manifest(solution.render, project, _sources([record]),
                           background='bg.mp4', styles=default_styles())
        message = str(caught.exception)
        self.assertIn('split', message)
        self.assertIn('layer.intro', message)
        self.assertIn('layer.intro.2', message)


class StyleOverrideTests(unittest.TestCase):
    """The project's style override has to reach the layout the products use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        record = _record()
        project = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), _media([record]),
                              Project(project_id='styles', intro_s=0.5, fps_num=30,
                                      width=320, height=180))
        from word_video.domain.model import StyleOverride as Override

        overridden = project.with_clips(project.clips)  # keep ids stable
        self.project = overridden
        self.media = _media([record])
        self.plan = solve(self.project, self.media).render
        self.record = record

    def _surface(self, plan, styles, fonts, paths):
        return LayoutSurface(plan, width=320, height=180, styles=styles, fonts=fonts,
                             font_paths=paths)

    def test_merged_styles_carry_the_override_and_keep_the_other_roles(self):
        from word_video.application.styles import merged_styles, style_fonts

        overrides = {'english': {'size': 800}}
        merged = merged_styles(overrides)
        fonts, names = style_fonts(merged)
        surface = self._surface(self.plan, merged, names, fonts)
        report = surface.layout()
        english = next(item for item in report.placements if item.role == 'english')
        meaning = next(item for item in report.placements if item.role == 'meaning')
        # english was overridden to 800 (320x180 -> 800 * 180/2160 = 67 px)...
        self.assertEqual(67, english.size)
        # ...and the roles that were not named keep the preset sizes.
        default_meaning = default_styles()['meaning']['size']
        self.assertEqual(round(default_meaning * 180 / 2160), meaning.size)

    def test_a_bare_override_would_change_every_other_role(self):
        """The trap this guards: the layout's own fallback is not the preset.

        Passing raw overrides to ``LayoutSurface`` makes every role the override did
        not name fall back to the surface's neutral default, which is why the
        exporter must merge first.  This is the counter-example that keeps the
        warning honest rather than a comment nobody can check.
        """
        bare = self._surface(self.plan, {'english': {'size': 800}},
                             {'english': 'x'}, {'english': ''})
        report = bare.layout()
        sizes = {item.role: item.size for item in report.placements}
        self.assertNotEqual(round(default_styles()['meaning']['size'] * 180 / 2160),
                            sizes['meaning'])
        from word_video.application.styles import merged_styles, style_fonts

        merged = merged_styles({'english': {'size': 800}})
        fonts, names = style_fonts(merged)
        right = {item.role: item.size for item in
                 self._surface(self.plan, merged, names, fonts).layout().placements}
        self.assertNotEqual(sizes, right)


if __name__ == '__main__':
    unittest.main()
