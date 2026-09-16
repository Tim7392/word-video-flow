"""The preview's own canvas: one widget that draws the frame and the text layer.

"独立画布" is a requirement, not a preference: the preview must not repaint the
editor's widget tree, and the editor must not be able to make the preview drop
frames by being busy.  So this is a leaf ``QWidget`` with its own timer that pulls
:meth:`preview.session.PreviewSession.snapshot` and paints; nothing else draws
into it, and it draws nothing else.

Text is drawn from B's ``PlacedText`` geometry, using the numbers the layout
already computed - never by measuring the string again:

* a line's centre is ``(line.x, line.y)`` in canvas fractions;
* ``line.baseline`` is the distance from the line's top to its baseline, in
  pixels, which is the one number that makes a Qt preview and libass agree;
* so the glyph run is placed by its *baseline*, not by Qt's own idea of where a
  centred line sits.  Re-deriving that here is how a preview and an export drift
  apart by a few pixels that nobody notices until text is near a safe edge.

The frame is wrapped, not converted: ffmpeg hands over ``bgr0``, which is
byte-for-byte ``QImage.Format_RGB32`` on a little-endian machine.  The buffer is
kept alive by the frame object for as long as the image is used, because a
``QImage`` built over foreign memory does not copy it - verified, not assumed.
"""
from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ['PreviewCanvas']

#: Paint rate.  60 Hz is smooth for 30 fps material and cheap: the paint path is a
#: scaled blit plus a handful of glyph runs.
DEFAULT_PAINT_HZ = 60
#: Painted behind the picture so a canvas with no background is still a canvas.
BACKDROP = '#101014'


class PreviewCanvas(QtWidgets.QWidget):
    """Draws the current preview frame and text; owns no media of its own."""

    def __init__(self, session, parent=None, paint_hz=DEFAULT_PAINT_HZ):
        super().__init__(parent)
        self.session = session
        self.setAutoFillBackground(True)
        self.setMinimumSize(160, 90)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self._font_families = {}
        self._image = None
        self._frame_kept_alive = None
        self._presentation = None
        self.paints = 0
        self.frames_drawn = 0
        self.last_text_error = ''
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer.setInterval(max(1, int(1000 / float(paint_hz))))
        self._timer.timeout.connect(self._tick)

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self._timer.start()
        return self

    def stop(self):
        self._timer.stop()
        self._image = None
        self._presentation = None

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    # -- painting --------------------------------------------------------
    def _tick(self):
        self._presentation = self.session.snapshot()
        self.update()

    def presentation(self):
        """The last snapshot, for a caller that wants the numbers, not the pixels."""
        return self._presentation

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            self.paints += 1
            rect = self.rect()
            painter.fillRect(rect, QtGui.QColor(BACKDROP))
            frame = None if self._presentation is None else self._presentation.frame
            if frame is not None:
                self._image = self._image_for(frame)
                if self._image is not None:
                    painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
                    painter.drawImage(rect, self._image)
                    self.frames_drawn += 1
            if self._presentation is not None:
                self._draw_text(painter, self._presentation.placements, rect)
        finally:
            painter.end()

    def _image_for(self, frame):
        """Wrap the decoded buffer; keep it alive for as long as the image lives."""
        if frame.width <= 0 or frame.height <= 0:
            return None
        if len(frame.buffer) < frame.bytes_per_line * frame.height:
            return None
        image = QtGui.QImage(frame.buffer, frame.width, frame.height,
                             frame.bytes_per_line, QtGui.QImage.Format_RGB32)
        # A QImage over foreign memory does not own it, so the frame (and through
        # it the bytes) stays referenced by this canvas while the image is shown.
        self._frame_kept_alive = frame
        return image

    def _draw_text(self, painter, placements, rect):
        for placed in placements:
            font = self._font_for(placed)
            if font is None:
                continue
            painter.setPen(QtGui.QColor(placed.color))
            for line in placed.lines:
                if not line.text:
                    continue
                path = QtGui.QPainterPath()
                path.addText(0.0, 0.0, font, line.text)
                bounds = path.boundingRect()
                centre_x = line.x * rect.width()
                top = (line.y - line.height / 2.0) * rect.height()
                painter.save()
                # Path space has the baseline at y=0 and the run starting at x=0;
                # translating by the layout's own baseline is what keeps this
                # identical to the export instead of "close enough".
                painter.translate(centre_x - (bounds.left() + bounds.width() / 2.0),
                                  top + line.baseline)
                painter.fillPath(path, QtGui.QColor(placed.color))
                painter.restore()

    def _font_for(self, placed):
        """The face the layout measured, loaded once and addressed by pixel size."""
        key = placed.font_path
        if not key:
            self.last_text_error = 'placement %s has no font file' % placed.clip_id
            return None
        family = self._font_families.get(key)
        if family is None:
            identifier = QtGui.QFontDatabase.addApplicationFont(key)
            families = QtGui.QFontDatabase.applicationFontFamilies(identifier) \
                if identifier != -1 else []
            if not families:
                self.last_text_error = 'Qt could not load %s' % key
                self._font_families[key] = ''
                return None
            family = families[0]
            self._font_families[key] = family
        if not family:
            return None
        font = QtGui.QFont(family)
        # The layout already resolved the *weight* into the file it measured
        # (a bold role points at the bold face), so asking Qt to embolden again
        # would widen every glyph away from the geometry that was checked.
        font.setPixelSize(max(1, int(placed.size)))
        return font

    def diagnostics(self):
        return {'paints': self.paints, 'frames_drawn': self.frames_drawn,
                'fonts_loaded': len([value for value in self._font_families.values()
                                     if value]),
                'last_text_error': self.last_text_error}
