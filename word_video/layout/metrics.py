"""Real text extents from the font file, with an honest fallback.

The layout must be the *same* one the preview and the MP4 use, so it cannot
answer "how wide is this text" with a guess when a real answer is available: the
fonts this product ships are in the project's resource folder, and Pillow reads
their metrics in-process (no extra dependency, no Qt, no GUI).

Pillow is optional here on purpose.  The bundled fonts are OpenType/TrueType
collections that Pillow can read today; if a future face cannot be loaded, the
fallback still produces a *conservative* width from the character classes
(CJK/emoji are full-width, everything else averages a little over half an em) so
an overflow check errs towards reporting overflow rather than hiding it.
"""
from dataclasses import dataclass
import unicodedata

#: Average advance of a non-full-width character, in ems.  Measured on the
#: bundled Source Han Sans / Arial faces; deliberately generous.
NARROW_EM = 0.62
#: Full-width advance (CJK, Hangul, full-width forms), in ems.
WIDE_EM = 1.0


@dataclass(frozen=True)
class TextExtent:
    """One measured string: its bounds and the metrics its line sits on."""

    width: float
    height: float
    ascent: float
    descent: float
    exact: bool


def is_full_width(char):
    """True for characters that occupy a full em (CJK, kana, Hangul, full-width)."""
    code = ord(char)
    if 0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7A3:
        return True
    if 0xF900 <= code <= 0xFAFF or 0xFE30 <= code <= 0xFE6F:
        return True
    if 0xFF00 <= code <= 0xFF60 or 0xFFE0 <= code <= 0xFFE6:
        return True
    if code >= 0x20000:          # supplementary ideographic plane
        return True
    return unicodedata.east_asian_width(char) in ('W', 'F')


class FontMetrics:
    """Measured metrics for one font file at the size a canvas needs.

    Instances are cached per ``(path, size)``; the size is an integer pixel size
    because that is what a rasteriser uses, while the layout works in canvas
    fractions and converts once.
    """

    def __init__(self, path, size):
        self.path = str(path) if path else ''
        self.size = max(1, int(round(size)))
        self._font = None
        self._exact = False
        if self.path:
            try:
                from PIL import ImageFont
                self._font = ImageFont.truetype(self.path, self.size)
                self._exact = True
            except (ImportError, OSError, ValueError):
                self._font = None

    @property
    def exact(self):
        return self._exact

    def text_extent(self, text):
        """Width/height/ascent/descent of ``text`` in pixels of this surface."""
        if self._font is not None:
            try:
                left, top, right, bottom = self._font.getbbox(text)
                return TextExtent(width=float(right - left), height=float(bottom - top),
                                  ascent=float(-top), descent=float(bottom), exact=True)
            except (OSError, ValueError):
                pass
        return self._estimate(text)

    def _estimate(self, text):
        em = float(self.size)
        width = 0.0
        for char in text:
            width += (WIDE_EM if is_full_width(char) else NARROW_EM) * em
        ascent, descent = em * 0.88, em * 0.22
        return TextExtent(width=width, height=ascent + descent, ascent=ascent,
                          descent=descent, exact=False)


_METRIC_CACHE = {}


def metrics_for(path, size):
    """Cached :class:`FontMetrics` for one file and pixel size."""
    key = (str(path) if path else '', max(1, int(round(size))))
    found = _METRIC_CACHE.get(key)
    if found is None:
        found = FontMetrics(key[0], key[1])
        _METRIC_CACHE[key] = found
    return found
