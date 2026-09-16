"""Text layout: one implementation, used by the preview and by every export.

``LayoutSurface`` answers the only three questions the picture side has: where
does a block of text sit, how much room does it take, and does it fit.  The MP4
exporter, the editable draft and (W04) the continuous preview all read the same
:class:`~word_video.layout.surface.LayoutReport`, so "预览和成片各写一套排版" cannot
happen by accident.

Font metrics come from the real font file through
:mod:`word_video.layout.metrics`; the fallback there is conservative, and every
placement records whether it was measured exactly.
"""
from .metrics import FontMetrics, TextExtent, is_full_width, metrics_for
from .surface import (DEFAULT_STYLE, MAX_BLOCK_HEIGHT, MIN_OVERHANG, REFERENCE_HEIGHT,
                      SAFE_MARGIN_X, TEXT_ROLES, VERTICAL_OVERHANG, LayoutOverflow,
                      LayoutReport, LayoutSurface, LineBox, PlacedText, TextBox,
                      style_for)

__all__ = ['DEFAULT_STYLE', 'FontMetrics', 'LayoutOverflow', 'LayoutReport',
           'LayoutSurface', 'LineBox', 'MAX_BLOCK_HEIGHT', 'MIN_OVERHANG',
           'PlacedText', 'REFERENCE_HEIGHT', 'SAFE_MARGIN_X', 'TEXT_ROLES',
           'TextBox', 'TextExtent', 'VERTICAL_OVERHANG', 'is_full_width',
           'metrics_for', 'style_for']
