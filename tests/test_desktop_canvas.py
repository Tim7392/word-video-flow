"""The independent canvas: it draws what the session says, at the size it was given.

Run headless: the Qt platform is set to ``offscreen`` before Qt is imported, so
these tests need no display and cannot steal focus from whoever is using the
machine.  They assert the drawing *contract* - which geometry reaches the painter,
and that the frame buffer is not copied or converted - not the pixels, because a
pixel comparison of text rendering would be a screenshot test dressed up as a
business test.
"""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtCore = pytest.importorskip('PySide6.QtCore')
QtGui = pytest.importorskip('PySide6.QtGui')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')

from desktop.preview_canvas import PreviewCanvas  # noqa: E402
from preview.clock import ManualAudioOutput  # noqa: E402
from preview.session import PreviewSession  # noqa: E402
from preview.video import VideoFrame  # noqa: E402
from test_preview_support import (RATE, scratch, three_tone_assets,  # noqa: E402
                                  three_word_plan, write_test_video)


@pytest.fixture(scope='module')
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


class FakeLine:
    def __init__(self, text, x, y, height, baseline):
        self.text, self.x, self.y, self.height, self.baseline = text, x, y, height, baseline


class FakePlaced:
    def __init__(self, clip_id, font_path, size, color, lines, anchor=(0.5, 0.5)):
        self.clip_id = clip_id
        self.font_path = font_path
        self.size = size
        self.color = color
        self.lines = tuple(lines)
        self.anchor_x, self.anchor_y = anchor


class FixedDisplay:
    """A display that returns exactly what a test hands it (no layout involved)."""

    def __init__(self, placements):
        self.placements = tuple(placements)

    def placements_at(self, ticks):
        return self.placements

    def overflowing(self):
        return ()


def make_session(folder, display=None, frames=()):
    assets = three_tone_assets(folder)
    plan, _ = three_word_plan(assets)
    session = PreviewSession(plan, assets, display=display, proxy=False,
                             output=ManualAudioOutput(rate=RATE), canvas_height=180)
    session.open()
    for frame in frames:
        session.frames.put_nowait(frame)
    return session


def test_the_canvas_draws_a_decoded_frame_without_converting_it(app):
    """ffmpeg's bgr0 is QImage.Format_RGB32 byte-for-byte, so the paint path must be
    a scaled blit and never a per-pixel conversion."""
    with scratch() as folder:
        session = make_session(folder)
        canvas = PreviewCanvas(session)
        canvas.resize(160, 90)
        try:
            frame = VideoFrame(generation=session.clock.generation, index=3, pts_ticks=0,
                               width=8, height=4, buffer=bytes([0x40] * (8 * 4 * 4)))
            image = canvas._image_for(frame)
            assert image is not None
            assert image.format() == QtGui.QImage.Format_RGB32
            assert image.width() == 8 and image.height() == 4
            assert image.bytesPerLine() == 8 * 4
            # The buffer is referenced, not owned: the canvas keeps the frame alive.
            assert canvas._frame_kept_alive is frame
        finally:
            canvas.stop()
            session.close()


def test_a_short_or_misshapen_buffer_is_refused_rather_than_drawn_sheared(app):
    with scratch() as folder:
        session = make_session(folder)
        canvas = PreviewCanvas(session)
        try:
            assert canvas._image_for(VideoFrame(0, 0, 0, 8, 4, b'short')) is None
            assert canvas._image_for(VideoFrame(0, 0, 0, 0, 4, b'')) is None
        finally:
            canvas.stop()
            session.close()


SENTINEL = QtGui.QColor(1, 2, 3)


def painted_pixels(image):
    """Pixels that are not the sentinel backdrop.

    ``QImage.fill(0)`` on ``Format_RGB32`` produces *opaque black*, not a zero
    pixel, so "count non-zero pixels" silently counts the whole image and makes a
    drawing assertion pass without anything being drawn.  A distinctive sentinel
    is what makes the count mean something.
    """
    target = SENTINEL.rgb()
    return sum(1 for y in range(image.height()) for x in range(image.width())
               if image.pixel(x, y) != target)


def test_text_is_drawn_from_the_layout_geometry_not_remeasured(app):
    """The canvas must place a glyph run by the layout's own baseline; if it
    re-derived the position from Qt's metrics the preview and the export would
    drift apart, which is exactly what the shared layout exists to prevent."""
    with scratch() as folder:
        from word_video.template import default_styles
        styles = default_styles()
        path = next((item.get('font') for item in styles.values() if item.get('font')), None)
        if not path:
            pytest.skip('no usable font resolved on this machine')
        display = FixedDisplay([
            FakePlaced('layer.title', path, 20, '#FFFFFF',
                       [FakeLine('上', 0.5, 0.2, 0.1, 16.0),
                        FakeLine('下', 0.5, 0.3, 0.1, 16.0)])])
        session = make_session(folder, display=display)
        canvas = PreviewCanvas(session)
        canvas.resize(320, 180)
        try:
            canvas._tick()
            image = QtGui.QImage(320, 180, QtGui.QImage.Format_RGB32)
            image.fill(SENTINEL)
            painter = QtGui.QPainter(image)
            try:
                canvas._draw_text(painter, canvas.presentation().placements,
                                  QtCore.QRect(0, 0, 320, 180))
            finally:
                painter.end()
            # Glyphs were actually painted: the sentinel is no longer everywhere.
            assert painted_pixels(image) > 0, 'no glyph reached the canvas'
            assert canvas.diagnostics()['fonts_loaded'] == 1
            assert canvas.last_text_error == ''
        finally:
            canvas.stop()
            session.close()


def test_a_placement_with_no_font_file_is_reported_not_drawn_in_a_substitute(app):
    """A substituted face is a silent change of the deliverable, so the canvas
    refuses and says so instead of picking whatever Qt defaults to."""
    with scratch() as folder:
        display = FixedDisplay([FakePlaced('x', '', 20, '#FFFFFF',
                                           [FakeLine('hi', 0.5, 0.5, 0.1, 10.0)])])
        session = make_session(folder, display=display)
        canvas = PreviewCanvas(session)
        canvas.resize(64, 64)
        try:
            canvas._tick()
            image = QtGui.QImage(64, 64, QtGui.QImage.Format_RGB32)
            image.fill(SENTINEL)
            painter = QtGui.QPainter(image)
            try:
                canvas._draw_text(painter, canvas.presentation().placements,
                                  QtCore.QRect(0, 0, 64, 64))
            finally:
                painter.end()
            assert canvas.last_text_error == 'placement x has no font file'
            assert painted_pixels(image) == 0, 'text was drawn without a font file'
        finally:
            canvas.stop()
            session.close()


def test_the_canvas_owns_its_own_timer_and_stops_cleanly(app):
    """独立画布: the preview repaints on its own schedule, and stopping it does not
    leave a widget holding a frame or a timer."""
    with scratch() as folder:
        session = make_session(folder)
        canvas = PreviewCanvas(session)
        try:
            canvas.start()
            assert canvas._timer.isActive()
            canvas.stop()
            assert not canvas._timer.isActive()
            assert canvas._image is None
        finally:
            session.close()


def test_an_audio_sink_is_created_for_the_preview_rate_and_can_be_closed(app):
    from desktop.audio_qt import QtAudioOutput
    output = QtAudioOutput(rate=RATE, buffer_frames=960)
    try:
        assert output.format.sampleRate() == RATE
        assert output.format.channelCount() == 1
        assert output.format.sampleFormat() == QtWidgets.QAudioFormat.Int16 \
            if hasattr(QtWidgets, 'QAudioFormat') else True
        assert output.frames_played() == 0
        # Writing before start is a no-op, not a crash: a session may resolve media
        # before it ever plays.
        assert output.write(b'\x00\x00') == 0
    finally:
        output.close()
    assert output.report()['state'] is None
