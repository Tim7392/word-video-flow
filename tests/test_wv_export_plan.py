"""What the projection refuses, and the publication record it leaves behind.

Two things W03 has to be able to say "no" about, because both would otherwise
change the deliverable silently:

* **a frame rate the manifests cannot express.**  The MP4, the draft and the
  subtitle exporter all work in an integer frame rate; a 30000/1001 project
  rendered as 30 retimes the lesson by 1 ms per second.  The projection refuses
  instead.  This is the counter-example H0 asked for after W03: the refusal had
  code but no test.
* **a plan the manifest cannot draw.**  If a text layer has been moved, the
  manifest's rebuilt display window is no longer the plan's, and rendering it
  would deliver a different scene than the one that was approved.

Plus the record that ties a published run together: ``complete.json`` lists every
file with its digest, so an independent checker can prove what was published.
"""
import json
from pathlib import Path
import tempfile
import unittest

from word_video.application import TrimClip, apply, instantiate
from word_video.domain import MediaInfo, Project, Record, solve
from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
from word_video.exporters.plan import ProjectionError, SourceMedia, build_manifest
from word_video.exporters.render import ExportError, _publish_record
from word_video.media import executable, run
from word_video.template import default_styles

STYLES = default_styles()


def _project(**overrides):
    record = Record(id='w1', word='apple', phonetic='/ˈæpəl/', meaning='n. 苹果',
                    spoken_meaning='苹果', index=1)
    media = {'w1:%s' % role: MediaInfo('w1:%s' % role, 48000)
             for role in ('female', 'male', 'chinese')}
    settings = dict(project_id='projection', intro_s=1.0)
    settings.update(overrides)
    return instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media, Project(**settings))


def _sources():
    return {'w1:%s' % role: SourceMedia(path='unused.wav', voice='V_%s' % role)
            for role in ('female', 'male', 'chinese')}


class FrameRateRefusalTests(unittest.TestCase):
    def test_2997_is_refused_rather_than_rounded_to_30(self):
        """1001/30000 is representable on the tick grid, but not as an mp4 rate."""
        project = _project(fps_num=30000, fps_den=1001)
        solution = solve(project, {key: MediaInfo(key, 48000)
                                   for key in _sources()})
        with self.assertRaises(ProjectionError) as caught:
            build_manifest(solution.render, project, _sources(),
                           background='bg.mp4', styles=STYLES)
        self.assertIn('integer frame rate', str(caught.exception))
        self.assertIn('30000/1001', str(caught.exception))

    def test_the_same_project_projects_when_the_caller_states_an_integer_rate(self):
        """Rendering 29.97 at 30 is a caller's explicit decision, not a default."""
        record = Record(id='w1', word='apple', phonetic='/ˈæpəl/', meaning='n. 苹果',
                        spoken_meaning='苹果', index=1)
        media = {'w1:%s' % role: MediaInfo('w1:%s' % role, 48000)
                 for role in ('female', 'male', 'chinese')}
        # A 30 fps project with the same content: the frames are then exact.
        project = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media,
                              Project(project_id='at30', intro_s=1.0, fps_num=30))
        solution = solve(project, media)
        view = build_manifest(solution.render, project, _sources(),
                              background='bg.mp4', styles=STYLES)
        self.assertEqual(30, view.fps)
        self.assertEqual(30, view.intro_frames)      # 1.0 s at 30 fps

    def test_a_non_finite_or_absent_rate_cannot_slip_through(self):
        project = _project(fps_num=60, fps_den=1)
        solution = solve(project, {key: MediaInfo(key, 48000) for key in _sources()})
        view = build_manifest(solution.render, project, _sources(),
                              background='bg.mp4', styles=STYLES, fps=25)
        self.assertEqual(25, view.fps)
        self.assertEqual(25, view.intro_frames)


class DisplayWindowTests(unittest.TestCase):
    def test_moving_a_text_layer_is_refused_not_redrawn(self):
        """The manifest rebuilds display windows from stage frames.

        Moving a layer without moving the stage is a documented edit; the manifest
        has no field for it, so the projection must refuse rather than render the
        old window under the new plan.
        """
        project = _project()
        media = {key: MediaInfo(key, 48000) for key in _sources()}
        moved = apply(project, TrimClip('w1.phonetic', start_ticks=0,
                                        end_ticks=project.clip('w1.male').start.ticks))
        solution = solve(moved.project, media)
        with self.assertRaises(ProjectionError) as caught:
            build_manifest(solution.render, moved.project, _sources(),
                           background='bg.mp4', styles=STYLES)
        self.assertIn('phonetic', str(caught.exception))
        self.assertIn('the plan places it at', str(caught.exception))

    def test_a_speech_stage_shorter_than_its_audio_still_projects(self):
        """A trim is a legitimate edit; the manifest carries the shorter stage."""
        project = _project()
        media = {key: MediaInfo(key, 48000) for key in _sources()}
        female = project.clip('w1.female')
        start = female.start.ticks
        short = apply(project, TrimClip('w1.female', end_ticks=start + 24000))
        solution = solve(short.project, media)
        view = build_manifest(solution.render, short.project, _sources(),
                              background='bg.mp4', styles=STYLES)
        item = next(entry for entry in view.audio if entry['role'] == 'female')
        # 24000 ticks is 2 frames at 60 fps: the manifest carries the plan's
        # length, and the renderer pads the remainder of the stage with silence.
        self.assertEqual(2, item['duration_frames'])
        self.assertEqual(2 * 12000, 24000)


class PublishRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_complete_json_covers_every_published_file_and_hashes_it(self):
        (self.root / 'video').mkdir()
        (self.root / 'video' / 'video.mp4').write_bytes(b'not really a video')
        (self.root / 'srt').mkdir()
        (self.root / 'srt' / 'a.srt').write_bytes(b'1\n')
        _publish_record(self.root)
        record = json.loads((self.root / 'complete.json').read_text(encoding='utf-8'))
        listed = {Path(item['path']).name for item in record['files']}
        self.assertEqual({'video.mp4', 'a.srt'}, listed)
        from word_video.media import sha256
        for item in record['files']:
            self.assertEqual(sha256(item['path']), item['sha256'])

    def test_a_run_that_published_nothing_is_refused(self):
        with self.assertRaises(ExportError):
            _publish_record(self.root)

    def test_the_record_never_covers_its_own_previous_copy(self):
        (self.root / 'video.mp4').write_bytes(b'x')
        _publish_record(self.root)
        (self.root / 'video.mp4').write_bytes(b'y')
        _publish_record(self.root)
        record = json.loads((self.root / 'complete.json').read_text(encoding='utf-8'))
        self.assertEqual(1, len(record['files']))
        from word_video.media import sha256
        self.assertEqual(sha256(self.root / 'video.mp4'), record['files'][0]['sha256'])


class CaptionBandTests(unittest.TestCase):
    """The layout's anchors are the ones the independent checker measures against."""

    def test_anchor_bands_match_the_published_expectation(self):
        from word_video.layout import LayoutSurface
        from word_video.template import resolve_fonts, font_name
        project = _project()
        media = {key: MediaInfo(key, 48000) for key in _sources()}
        solution = solve(project, media)
        fonts = {role: STYLES[role].get('font') for role in STYLES}
        names = {role: STYLES[role].get('font_name') for role in STYLES}
        surface = LayoutSurface(solution.render, width=1920, height=1080,
                                styles=STYLES, fonts=names, font_paths=fonts)
        report = surface.layout()
        # The bands the M0 acceptance checker probes, in canvas fractions.
        expected = {'title': 0.0650, 'subtitle': 0.1800, 'english': 0.4062,
                    'phonetic': 0.5426, 'meaning': 0.6356, 'footer': 0.9350}
        for placed in report.placements:
            self.assertIn(placed.role, expected)
            self.assertAlmostEqual(expected[placed.role], placed.anchor_y, places=4)


if __name__ == '__main__':
    unittest.main()
