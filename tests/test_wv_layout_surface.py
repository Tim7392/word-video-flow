"""`LayoutSurface`: one layout, measured from the real font file.

Assertions here are about geometry that a preview and an MP4 must agree on:
where a block sits, how wide it draws, and whether it fits.  The font metrics
come from the actual face the renderer will hand to libass, so a placement that
overflows in this test overflows on screen too (and one that fits, fits).
"""
import math
from pathlib import Path
import unittest

from word_video.application import instantiate
from word_video.domain import MediaInfo, Project, Record, solve
from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
from word_video.layout import (LayoutOverflow, LayoutSurface, REFERENCE_HEIGHT,
                               SAFE_MARGIN_X, metrics_for)
from word_video.template import default_styles, resolve_fonts

RATE = 48000


def _record(index=151, word='demonstrate', phonetic='ˈdemənstreɪt',
            meaning='v. 证明；演示；示范', spoken='证明；证实'):
    return Record(id='w%d' % index, word=word, phonetic=phonetic, meaning=meaning,
                  spoken_meaning=spoken, index=index)


def _media(record):
    return {'%s:%s' % (record.id, role): MediaInfo('%s:%s' % (record.id, role), RATE)
            for role in ('female', 'male', 'chinese')}


def _project(records, **overrides):
    settings = dict(project_id='layout-test', intro_s=1.0)
    settings.update(overrides)
    return instantiate(DEFAULT_LESSON_TEMPLATE, tuple(records),
                       {key: value for record in records
                        for key, value in _media(record).items()},
                       Project(**settings))


def _font_maps(styles=None):
    """The resolved face per *display* role, exactly as the style preset resolved it.

    ``resolve_fonts`` answers for the five font roles (heading/bold/heavy/...);
    a text layer is a *style* role, so its file is the one ``default_styles``
    picked for it.  Reading it from there is what keeps the layout, the ASS
    export and the draft on one font decision.
    """
    resolved = styles if styles is not None else default_styles()
    paths = {role: item.get('font') for role, item in resolved.items()}
    names = {role: item.get('font_name') for role, item in resolved.items()}
    return paths, names


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font_paths, cls.font_names = _font_maps()
        if not all(cls.font_paths.get(role) for role in ('english', 'phonetic',
                                                         'meaning', 'title')):
            raise unittest.SkipTest('no usable font resolved on this machine')
        cls.record = _record()
        cls.project = _project([cls.record])
        cls.solution = solve(cls.project, {key: value for key, value in
                                           _media(cls.record).items()})

    def surface(self, width=1920, height=1080, styles=None):
        return LayoutSurface(self.solution.render, width=width, height=height,
                             styles=styles if styles is not None else default_styles(),
                             fonts=self.font_names, font_paths=self.font_paths)

    # -- what lands where ------------------------------------------------
    def test_every_on_screen_layer_is_placed_once(self):
        report = self.surface().layout()
        roles = [placed.role for placed in report.placements]
        for role in ('title', 'subtitle', 'footer', 'english', 'phonetic', 'meaning'):
            self.assertEqual(1, roles.count(role), role)
        self.assertTrue(report.exact_metrics, 'layout fell back to estimated metrics')

    def test_placement_uses_the_real_font_and_scales_with_the_canvas(self):
        report = self.surface(1920, 1080).layout()
        english = next(item for item in report.placements if item.role == 'english')
        expected = round(default_styles()['english']['size'] * 1080 / REFERENCE_HEIGHT)
        self.assertEqual(expected, english.size)
        self.assertEqual(self.font_paths['english'], english.font_path)
        self.assertGreater(english.lines[0].width, 0.0)
        # The same text on a 4K canvas keeps its relative width, not its pixels.
        big = self.surface(3840, 2160).layout()
        big_english = next(item for item in big.placements if item.role == 'english')
        # Exact to font hinting: a face rasterised at two pixel sizes advances by
        # a fraction of a pixel per glyph, which is under 0.1% of the line here.
        self.assertAlmostEqual(english.lines[0].width, big_english.lines[0].width,
                               delta=0.002)
        self.assertEqual(english.size * 2, big_english.size)

    def test_anchors_match_the_style_preset(self):
        styles = default_styles()
        report = self.surface(styles=styles).layout()
        for placed in report.placements:
            self.assertAlmostEqual(styles[placed.role]['x'], placed.anchor_x, places=9)
            self.assertAlmostEqual(styles[placed.role]['y'], placed.anchor_y, places=9)

    def test_the_box_is_centred_on_the_anchor(self):
        report = self.surface().layout()
        english = next(item for item in report.placements if item.role == 'english')
        self.assertAlmostEqual(english.anchor_x, (english.box.left + english.box.right) / 2,
                               places=9)
        self.assertAlmostEqual(english.anchor_y, (english.box.top + english.box.bottom) / 2,
                               places=9)

    # -- wrapping and overflow -------------------------------------------
    def test_long_meaning_wraps_instead_of_running_off_the_canvas(self):
        record = _record(index=1, word='international',
                         meaning='adj. 国际的；国际性的；世界性的；超越国界的',
                         spoken='国际的国际性的世界性的超越国界的')
        project = _project([record], width=640, height=360)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=640, height=360,
                                styles=default_styles(), fonts=self.font_names,
                                font_paths=self.font_paths)
        report = surface.layout()
        meaning = next(item for item in report.placements if item.role == 'meaning')
        self.assertGreater(len(meaning.lines), 1, 'the meaning did not wrap')
        self.assertTrue(meaning.fits, meaning.box.reason(meaning.clip_id))

    def test_a_word_wider_than_the_safe_area_is_reported_not_hyphenated(self):
        record = _record(index=1, word='antidisestablishmentarianism')
        project = _project([record], width=320, height=180)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=320, height=180,
                                styles=default_styles(), fonts=self.font_names,
                                font_paths=self.font_paths)
        report = surface.layout()
        english = next(item for item in report.placements if item.role == 'english')
        # One line, whole word: no invented hyphen, but it does not fit.
        self.assertEqual(1, len(english.lines))
        self.assertEqual('antidisestablishmentarianism', english.lines[0].text)
        self.assertFalse(english.fits)
        self.assertIn('w1.english', report.to_dict()['overflowing'])

    def test_require_fit_refuses_a_delivery_that_does_not_fit(self):
        record = _record(index=1, word='antidisestablishmentarianism')
        project = _project([record], width=320, height=180)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=320, height=180,
                                styles=default_styles(), fonts=self.font_names,
                                font_paths=self.font_paths)
        with self.assertRaises(LayoutOverflow) as caught:
            surface.require_fit()
        self.assertIn('w1.english', str(caught.exception))
        # A fitting layout passes the same door unchanged.
        self.assertTrue(self.surface().require_fit().placements)

    def test_a_style_driven_overflow_is_caught(self):
        """Enlarging the font in the project reaches the layout, not the encoder."""
        styles = default_styles()
        styles['meaning'] = dict(styles['meaning'], size=6000)
        with self.assertRaises(LayoutOverflow):
            self.surface(320, 180, styles=styles).require_fit()

    def test_a_block_that_grew_too_tall_is_refused(self):
        """An overflowing block wraps into a wall of text; that is caught too."""
        record = _record(index=1, phonetic='\n'.join(['wrapped'] * 40))
        project = _project([record], width=1920, height=1080)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=1920, height=1080,
                                styles=default_styles(), fonts=self.font_names,
                                font_paths=self.font_paths)
        with self.assertRaises(LayoutOverflow) as caught:
            surface.require_fit()
        self.assertIn('占画面高度', str(caught.exception))

    def test_horizontal_safety_is_the_checked_edge(self):
        """Where the preset sits, and what the verdict is against.

        The approved preset is the approved layout: its title block is centred on
        ``y=0.065`` and therefore reaches a few thousandths above the frame edge,
        and its footer reaches below.  The check therefore allows a proportional
        overhang while the *horizontal* margin is strict - that is the direction
        text actually runs out of.
        """
        report = self.surface().layout()
        for placed in report.placements:
            self.assertTrue(placed.fits, placed.box.reason(placed.clip_id))
            self.assertGreaterEqual(placed.box.left, SAFE_MARGIN_X - 1e-9)
            self.assertLessEqual(placed.box.right, 1 - SAFE_MARGIN_X + 1e-9)
            # The drawn geometry, within the overhang the preset itself needs
            # (measured: at most 0.006 of the canvas on this template).
            self.assertGreater(placed.box.top, -0.01)
            self.assertLess(placed.box.bottom, 1.01)

    # -- text handling ---------------------------------------------------
    def test_author_line_breaks_are_kept(self):
        record = _record(index=1, meaning='n. 第一行\n第二行')
        project = _project([record], width=1920, height=1080)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=1920, height=1080,
                               styles=default_styles(), fonts=self.font_names,
                               font_paths=self.font_paths)
        meaning = next(item for item in surface.layout().placements
                       if item.role == 'meaning')
        self.assertEqual(('n. 第一行', '第二行'), tuple(line.text for line in meaning.lines))

    def test_empty_text_gets_an_empty_placement_not_a_crash(self):
        record = _record(index=1, phonetic='')
        project = _project([record], width=1920, height=1080)
        solution = solve(project, _media(record))
        surface = LayoutSurface(solution.render, width=1920, height=1080,
                               styles=default_styles(), fonts=self.font_names,
                               font_paths=self.font_paths)
        phonetic = next(item for item in surface.layout().placements
                        if item.role == 'phonetic')
        self.assertEqual(('',), tuple(line.text for line in phonetic.lines))
        self.assertTrue(phonetic.fits)

    def test_layout_is_deterministic(self):
        first = self.surface().layout().to_dict()
        second = self.surface().layout().to_dict()
        self.assertEqual(first, second)

    def test_an_unusable_canvas_is_refused(self):
        with self.assertRaises(ValueError):
            LayoutSurface(self.solution.render, width=0, height=1080,
                          styles=default_styles(), fonts=self.font_names,
                          font_paths=self.font_paths)
        with self.assertRaises(ValueError):
            LayoutSurface(self.solution.render, width=1920, height=1080, dpi=0,
                          styles=default_styles(), fonts=self.font_names,
                          font_paths=self.font_paths)


class MetricsTests(unittest.TestCase):
    def test_real_metrics_beat_the_fallback_and_are_marked_exactly(self):
        paths, _ = _font_maps()
        real = metrics_for(paths['english'], 100)
        self.assertTrue(real.exact)
        extent = real.text_extent('demonstrate')
        self.assertGreater(extent.width, 300)
        self.assertLess(extent.width, 900)

    def test_the_fallback_is_conservative_and_says_it_is_estimated(self):
        estimated = metrics_for('', 100)
        self.assertFalse(estimated.exact)
        wide = estimated.text_extent('证证证')
        narrow = estimated.text_extent('lll')
        self.assertGreater(wide.width, narrow.width)
        # CJK is full width; the estimate must not under-report it.
        self.assertGreaterEqual(wide.width, 300)

    def test_a_font_collection_loads(self):
        """The bundled faces are .ttc/.otf; a collection must not silently fail."""
        paths, _ = _font_maps()
        for role in ('english', 'meaning', 'title'):
            metrics = metrics_for(paths[role], 200)
            self.assertTrue(metrics.exact, role)
            self.assertGreater(metrics.text_extent('四级1500').width, 0)


if __name__ == '__main__':
    unittest.main()
