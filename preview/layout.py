"""The one door between the preview and text layout.

Why this file is a *port* and not a layout
-----------------------------------------
The preview must draw exactly the text the delivered MP4 draws, so it must not
own a second implementation of wrapping, sizing or placement: two implementations
disagree the first time a long meaning wraps differently, and the member sees an
export that does not match what they approved on screen.  So the preview does not
place text at all.  It asks a :class:`DisplayPort` "what is on screen at tick T"
and draws whatever it is told, in the caller's own geometry.

:class:`LayoutSurfaceDisplay` is the only production implementation, and it is a
thin adapter over B's ``word_video.layout.LayoutSurface`` - it calls
``.layout()`` once per plan and then answers by time.  The objects it returns are
B's own ``PlacedText``/``LineBox`` values, passed through unwrapped, so there is
no second placement type to keep in step either.

Where the interface stood when this was written
-----------------------------------------------
``word_video.layout`` had not been merged into ``main`` yet, so the import is
deliberately lazy and its absence is a clear, actionable error rather than an
ImportError at module load.  Nothing else in :mod:`preview` depends on layout
being present, which is what let the clock, the queues and the proxy work be
finished and measured before the layout interface froze.  The concrete interface
this waits for is::

    LayoutSurface(plan, width=..., height=..., styles=..., fonts=...,
                  font_paths=..., dpi=...)
    surface.layout() -> LayoutReport(placements=(PlacedText, ...),
                                     overflowing=(PlacedText, ...))
    PlacedText: clip_id role record_id text start_ticks end_ticks
                anchor_x anchor_y font_path font_name size size_em color bold
                lines=(LineBox,) box measured_exactly
    LineBox:    text x y width height baseline        # x/y are canvas fractions,
                                                      # y is the line's centre
"""
from typing import Protocol, Sequence, runtime_checkable

__all__ = ['DisplayPort', 'LayoutSurfaceDisplay', 'LayoutUnavailable',
           'placements_at_time']

#: Where the layout package must appear for the real display to be available.
LAYOUT_MODULE = 'word_video.layout'


class LayoutUnavailable(RuntimeError):
    """The real layout surface is not importable in this checkout."""


@runtime_checkable
class DisplayPort(Protocol):
    """What the preview needs from text layout: the placements due at a tick."""

    def placements_at(self, ticks) -> Sequence:
        """Placements whose ``[start_ticks, end_ticks)`` contains ``ticks``."""

    def overflowing(self) -> Sequence:
        """Placements the layout reported as leaving the safe area."""


def placements_at_time(placements, ticks):
    """Filter placements by time, in a stable order.

    Linear in the number of placements of the plan, which is a few hundred even
    for fifty words, and the scan is what keeps the on-screen order equal to the
    plan's order.  A caller that needed something cleverer would be drawing a much
    longer lesson than this product previews.
    """
    return tuple(placed for placed in placements
                 if placed.start_ticks <= ticks < placed.end_ticks)


class LayoutSurfaceDisplay:
    """Query B's ``LayoutSurface`` by tick; the only production display."""

    def __init__(self, surface):
        self.surface = surface
        report = surface.layout()
        self.report = report
        self._placements = tuple(sorted(report.placements,
                                        key=lambda placed: (placed.start_ticks,
                                                            placed.end_ticks,
                                                            placed.clip_id)))
        self._overflowing = tuple(report.overflowing)
        self._drawn = 0

    @classmethod
    def from_plan(cls, plan, *, width=None, height=None, styles=None, fonts=None,
                  font_paths=None, dpi=72):
        """Build the display for a plan, or explain which interface is missing."""
        try:
            from word_video.layout import LayoutSurface
        except ImportError as error:
            raise LayoutUnavailable(
                '%s is not available in this checkout (%s); the preview needs B\'s '
                'LayoutSurface before it can draw text, and it will not grow a '
                'second layout of its own' % (LAYOUT_MODULE, error)) from error
        return cls(LayoutSurface(plan, width=width, height=height, styles=styles,
                                 fonts=fonts, font_paths=font_paths, dpi=dpi))

    # -- DisplayPort -----------------------------------------------------
    def placements_at(self, ticks):
        due = placements_at_time(self._placements, ticks)
        self._drawn += len(due)
        return due

    def overflowing(self):
        return self._overflowing

    @property
    def exact_metrics(self):
        return bool(getattr(self.report, 'exact_metrics', False))

    def to_dict(self):
        return {'placements': len(self._placements),
                'overflowing': [placed.clip_id for placed in self._overflowing],
                'exact_metrics': self.exact_metrics}
