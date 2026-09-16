"""The display port: it filters by time and hands B's own placements straight through.

The double used here is a *test* double for the port, not a layout: it holds
placements somebody else already computed and answers "which are on screen now".
No wrapping, sizing or placement happens in this package, in this test, or
anywhere else in ``preview`` - that is the point of the port, and
``preview/verify_layout_binding.py`` is what proves the real adapter against B's
``LayoutSurface``.
"""
from dataclasses import dataclass

import pytest

from preview.layout import (DisplayPort, LayoutSurfaceDisplay, LayoutUnavailable,
                            placements_at_time)
from test_preview_support import scratch, three_tone_assets, three_word_plan
from word_video.domain.timebase import TICKS_PER_SECOND


@dataclass(frozen=True)
class FakePlacement:
    """Stands in for B's ``PlacedText``: the fields the port contract names."""

    clip_id: str
    role: str
    text: str
    start_ticks: int
    end_ticks: int
    anchor_x: float
    anchor_y: float


class FakeDisplay:
    """A port implementation holding ready-made placements."""

    def __init__(self, placements, overflowing=()):
        self._placements = tuple(placements)
        self._overflowing = tuple(overflowing)
        self.asked = []

    def placements_at(self, ticks):
        self.asked.append(ticks)
        return placements_at_time(self._placements, ticks)

    def overflowing(self):
        return self._overflowing


def test_the_port_contract_is_satisfied_by_a_minimal_implementation():
    """A caller can supply its own display; the session only needs these two."""
    display = FakeDisplay([FakePlacement('a', 'english', 'word', 0, 100, 0.5, 0.5)])
    assert isinstance(display, DisplayPort)


def test_only_the_placements_due_at_a_tick_are_returned():
    placements = [FakePlacement('a', 'english', 'one', 0, 100, 0.5, 0.5),
                  FakePlacement('b', 'meaning', 'two', 100, 200, 0.5, 0.6),
                  FakePlacement('c', 'footer', 'three', 0, 200, 0.5, 0.9)]
    display = FakeDisplay(placements)
    assert [p.clip_id for p in display.placements_at(0)] == ['a', 'c']
    assert [p.clip_id for p in display.placements_at(99)] == ['a', 'c']
    # Half-open: the end tick belongs to the next clip, not this one.
    assert [p.clip_id for p in display.placements_at(100)] == ['b', 'c']
    assert [p.clip_id for p in display.placements_at(200)] == []


def test_filtering_preserves_the_plan_order():
    """On-screen order is the plan's order, so a later layer cannot jump in front
    of an earlier one because of how the filter walked the list."""
    placements = [FakePlacement('first', 'background', '', 0, 10, 0.5, 0.5),
                  FakePlacement('second', 'title', '', 0, 10, 0.5, 0.1),
                  FakePlacement('third', 'english', '', 0, 10, 0.5, 0.4)]
    assert [p.clip_id for p in placements_at_time(placements, 5)] == \
        ['first', 'second', 'third']


def test_the_adapter_keeps_b_layout_objects_unwrapped():
    """If it copied them into a preview type there would be a second placement
    model to keep in step, which is what "no two layout implementations" forbids."""

    class FakeReport:
        placements = (FakePlacement('a', 'english', 'word', 0, 100, 0.5, 0.5),)
        overflowing = ()
        exact_metrics = True

    class FakeSurface:
        def layout(self):
            return FakeReport()

    display = LayoutSurfaceDisplay(FakeSurface())
    returned = display.placements_at(10)
    assert len(returned) == 1
    assert returned[0] is FakeReport.placements[0]
    assert display.exact_metrics is True
    assert display.to_dict() == {'placements': 1, 'overflowing': [],
                                 'exact_metrics': True}


def test_overflow_is_data_on_the_port_not_an_exception():
    """The editor shows overflow; only the delivery door refuses."""
    overflowing_placement = FakePlacement('w1.english', 'english', 'long', 0, 100,
                                          0.5, 0.5)

    class FakeReport:
        placements = (overflowing_placement,)
        overflowing = (overflowing_placement,)
        exact_metrics = False

    class FakeSurface:
        def layout(self):
            return FakeReport()

    display = LayoutSurfaceDisplay(FakeSurface())
    assert [p.clip_id for p in display.overflowing()] == ['w1.english']
    # Asking for what is on screen still works; nothing was raised.
    assert display.placements_at(0)[0].clip_id == 'w1.english'


def test_the_adapter_answers_by_tick_across_a_real_plan_layout():
    """The tick window is the plan's, converted through the same time base."""
    placements = [FakePlacement('early', 'title', 'T', 0, TICKS_PER_SECOND, 0.5, 0.06),
                  FakePlacement('late', 'footer', 'F', TICKS_PER_SECOND,
                                2 * TICKS_PER_SECOND, 0.5, 0.94)]

    class FakeReport:
        def __init__(self):
            self.placements = tuple(placements)
            self.overflowing = ()
            self.exact_metrics = True

    class FakeSurface:
        def layout(self):
            return FakeReport()

    display = LayoutSurfaceDisplay(FakeSurface())
    assert [p.clip_id for p in display.placements_at(0)] == ['early']
    assert [p.clip_id for p in display.placements_at(TICKS_PER_SECOND)] == ['late']


def _layout_merged():
    import importlib.util
    return importlib.util.find_spec('word_video.layout') is not None


def test_the_real_layout_is_reachable_from_this_checkout():
    """``LayoutSurface`` is merged, so the adapter must find it without help.

    While W03 was still on its own branch this asserted the opposite - that the
    failure named the missing interface.  Now the useful assertion is that the
    binding is live here, and ``preview/verify_layout_binding.py`` checks the
    geometry it produces.
    """
    assert _layout_merged(), ('word_video.layout is not in this tree; pass '
                              '--layout-tree to preview/verify_layout_binding.py')


@pytest.mark.skipif(not _layout_merged(),
                    reason='word_video.layout is not merged into main yet; the real '
                           'binding is verified by preview/verify_layout_binding.py, '
                           'run against the layout worktree')
def test_a_small_canvas_places_text_where_the_delivery_does():
    """The property a member relies on when they trust a small preview.

    Skipped only while the layout is unmerged: when it is available this is the
    in-tree version of the same check ``preview/verify_layout_binding.py`` makes
    against three canvases.
    """
    from preview.verify_layout_binding import CANVASES, build_plan, font_maps
    plan = build_plan()
    styles, paths, names = font_maps()
    if not paths.get('english'):
        pytest.skip('no usable font resolved on this machine')

    base = LayoutSurfaceDisplay.from_plan(plan, width=1920, height=1080,
                                          styles=styles, fonts=names, font_paths=paths)
    reference = {placed.clip_id: placed for placed in base.report.placements}
    assert reference, 'the layout placed nothing'
    for width, height in CANVASES:
        small = LayoutSurfaceDisplay.from_plan(plan, width=width, height=height,
                                               styles=styles, fonts=names,
                                               font_paths=paths)
        placed_small = {placed.clip_id: placed for placed in small.report.placements}
        assert set(placed_small) == set(reference)
        for clip_id, placed in placed_small.items():
            ref = reference[clip_id]
            # Where it sits is exact; how wide it measures is bounded by half a
            # glyph, because advances round to whole pixels per canvas.
            assert placed.anchor_x == ref.anchor_x
            assert placed.anchor_y == ref.anchor_y
            assert placed.lines[0].y == ref.lines[0].y
            assert abs(placed.size - ref.size * height / 1080.0) <= 0.5 + 1e-9
