"""把 B 的 placements 画到画布上：一处实现，两个画布共用。

The text layer is what the preview exists for, and it must be the *same* text the
delivered MP4 draws - same wrapping, same baseline, same font file, same colour.  Both
canvases in this editor (the video canvas of W04 and the light template preview Tim
asked for on 2026-09-17) therefore draw through this module and not through a private
copy: two drawing routines would disagree the first time a long meaning wrapped
differently, and the member would approve something the export does not produce.

Two measured rules are kept here verbatim from the canvas that shipped:

* a line is placed by its **baseline** (``line.baseline``, the distance from the line's
  top to its baseline), which is the one number that makes a Qt preview and libass
  agree - Qt's own idea of a centred line is a few pixels away;
* nothing is measured again: the geometry comes from the layout report, and the font is
  the file the layout measured (asking Qt to embolden again would widen every glyph).

What is new here is only *lifetime*: every QFont, QColor and QPen is cached by the value
it was built from, because Tim's direction is that a repaint must not create PySide6
objects (the light preview repaints on a 10-20 Hz timer while the machine is also
exporting, and per-repaint wrapper churn is exactly the resource cost being removed).
"""
from PySide6 import QtCore, QtGui

__all__ = ['TextStyleCache', 'draw_placements', 'draw_placeholder']

#: Painted behind the picture so a canvas with no still is still a canvas.
BACKDROP = '#14161a'


class TextStyleCache:
    """Fonts, colours and pens, built once per distinct value.

    ``font_path`` is the key: the layout resolved weight and family into a file, so a
    file is a complete description of the face.  The application font is registered
    once per file, which is also what keeps repeated repaints from touching Qt's font
    database again.
    """

    def __init__(self, backdrop=BACKDROP):
        self._families = {}
        self._fonts = {}
        self._colors = {}
        self._pens = {}
        self.backdrop = self.color(backdrop)
        self.last_error = ''

    def family_for(self, font_path):
        if font_path in self._families:
            return self._families[font_path]
        identifier = QtGui.QFontDatabase.addApplicationFont(font_path)
        families = QtGui.QFontDatabase.applicationFontFamilies(identifier) \
            if identifier != -1 else []
        if not families:
            self.last_error = 'Qt could not load %s' % font_path
            self._families[font_path] = ''
            return ''
        self._families[font_path] = families[0]
        return self._families[font_path]

    def font_for(self, placed):
        """The font of one placement, at its own pixel size, cached by file+size."""
        key = (placed.font_path, int(placed.size))
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        if not placed.font_path:
            self.last_error = 'placement %s has no font file' % placed.clip_id
            return None
        family = self.family_for(placed.font_path)
        if not family:
            return None
        font = QtGui.QFont(family)
        font.setPixelSize(max(1, int(placed.size)))
        self._fonts[key] = font
        return font

    def color(self, value):
        cached = self._colors.get(value)
        if cached is None:
            cached = QtGui.QColor(value)
            self._colors[value] = cached
        return cached

    def pen(self, value, width=1, style=None):
        key = (value, int(width), style)
        cached = self._pens.get(key)
        if cached is None:
            cached = QtGui.QPen(self.color(value), int(width),
                                style if style is not None else QtCore.Qt.SolidLine)
            self._pens[key] = cached
        return cached

    def diagnostics(self):
        return {'fonts_loaded': len([value for value in self._families.values() if value]),
                'fonts_cached': len(self._fonts), 'colors_cached': len(self._colors),
                'pens_cached': len(self._pens), 'last_text_error': self.last_error}


def draw_placements(painter, placements, rect, cache, selection=()):
    """Draw the due placements, in the order the layout returned them.

    ``rect`` is the widget's own rectangle: a line's ``x``/``y`` are canvas fractions,
    so the same drawing code puts text in the same relative place at any canvas size.
    """
    for placed in placements:
        font = cache.font_for(placed)
        if font is None:
            continue
        painter.setPen(cache.color(placed.color))
        for line in placed.lines:
            if not line.text:
                continue
            path = QtGui.QPainterPath()
            path.addText(0.0, 0.0, font, line.text)
            bounds = path.boundingRect()
            centre_x = line.x * rect.width()
            top = (line.y - line.height / 2.0) * rect.height()
            painter.save()
            painter.translate(centre_x - (bounds.left() + bounds.width() / 2.0),
                              top + line.baseline)
            painter.fillPath(path, cache.color(placed.color))
            painter.restore()
        if placed.clip_id in selection:
            draw_selection_box(painter, placed, rect, cache)


def draw_selection_box(painter, placed, rect, cache):
    """A dashed box around a selected placement, from the layout's own lines."""
    if not placed.lines:
        return
    left = min(line.x * rect.width() - line.width * rect.width() / 2.0
               for line in placed.lines)
    right = max(line.x * rect.width() + line.width * rect.width() / 2.0
                for line in placed.lines)
    top = min((line.y - line.height / 2.0) * rect.height() for line in placed.lines)
    bottom = max((line.y + line.height / 2.0) * rect.height() for line in placed.lines)
    painter.save()
    painter.setPen(cache.pen('#ffd166', 1, QtCore.Qt.DashLine))
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRect(QtCore.QRectF(left - 4, top - 3, (right - left) + 8,
                                   (bottom - top) + 6))
    painter.restore()


def draw_placeholder(painter, text, rect, cache, color='#f4e2b8'):
    """One line of explanation, centred: what a canvas shows instead of nothing.

    Used for "the still is being made" and for "the still cannot be made, and here is
    why" - a preview that shows an empty rectangle without saying anything is the
    silent failure this replaces.
    """
    if not text:
        return
    key = ('placeholder', max(12, int(rect.height() / 28)))
    font = cache._fonts.get(key)
    if font is None:
        font = QtGui.QFont()
        font.setPixelSize(key[1])
        cache._fonts[key] = font
    painter.save()
    painter.setFont(font)
    painter.setPen(cache.color(color))
    painter.drawText(rect.adjusted(16, 0, -16, 0),
                     QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap, text)
    painter.restore()
