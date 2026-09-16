"""文字预览：同一套 placements、按需重绘、静帧缓存与可诊断的失败。

These cases cover the preview Tim asked for on 2026-09-17: the window shows the
**template's text effect** on one still of the background, and no longer decodes video,
builds proxies, encodes segments in the background or opens an audio device.  What is
asserted here is therefore:

* the text comes from the same layout port the export uses - the widget draws what
  ``DisplayPort.placements_at`` returns, and it builds no placements of its own;
* a repaint does **not** construct a new QFont/QColor/QPen (the caches are reused), and
  the timer runs only while playing;
* the still is cached by source identity, a changed source invalidates it, a missing or
  unreadable background is *explained* rather than shown as an empty rectangle;
* the window builds this preview and no timeline, and no preview session at all.
"""
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtCore = pytest.importorskip('PySide6.QtCore')
QtGui = pytest.importorskip('PySide6.QtGui')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')

from desktop.editor_window import EditorWindow                 # noqa: E402
from desktop.text_preview import TextPreview                   # noqa: E402
from preview.still import StillFrames                          # noqa: E402
from test_editor_support import tiny_folder                    # noqa: E402
from test_preview_support import scratch, write_test_video     # noqa: E402

from word_video.domain.plan import PlanItem, RenderPlan        # noqa: E402
from word_video.domain.model import MediaSlice                 # noqa: E402
from word_video.domain.timebase import TICKS_PER_SECOND        # noqa: E402

FPS = 30
CANVAS = (320, 180)


class Line:
    def __init__(self, text, x, y, width, height, baseline):
        self.text, self.x, self.y = text, x, y
        self.width, self.height, self.baseline = width, height, baseline


class Placement:
    def __init__(self, clip_id, start_ticks, end_ticks, text='W', colour='#ffffff'):
        self.clip_id, self.role, self.record_id = clip_id, 'english', ''
        self.text = text
        self.start_ticks, self.end_ticks = start_ticks, end_ticks
        self.font_path, self.font_name, self.size = '', '', 40
        self.color, self.bold, self.measured_exactly = colour, False, True
        self.lines = (Line(text, 0.5, 0.5, 40, 20, 16),)


class FakeDisplay:
    """The smallest thing that satisfies the DisplayPort: placements due at a tick."""

    def __init__(self, placements):
        self.placements = tuple(placements)
        self.calls = 0

    def placements_at(self, ticks):
        self.calls += 1
        return tuple(placed for placed in self.placements
                     if placed.start_ticks <= ticks < placed.end_ticks)

    def overflowing(self):
        return ()


def plan_of(seconds=2.0, background=None):
    video = ()
    if background is not None:
        video = (PlanItem(clip_id='layer.background', role='background', record_id='',
                          start_ticks=0, end_ticks=int(seconds * TICKS_PER_SECOND),
                          source=MediaSlice(asset_id='layer:background', source_start=0,
                                            source_end=int(seconds * 1000),
                                            unit_num=1, unit_den=1000)),)
    return RenderPlan(project_id='p', project_revision=0, fps_num=FPS, fps_den=1,
                      width=1920, height=1080, sample_rate=48000, channels=1,
                      total_ticks=int(seconds * TICKS_PER_SECOND), video=video)


@pytest.fixture(scope='module')
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


def test_the_preview_draws_the_placements_the_layout_reports(app):
    """No second layout: what the display says is due is what is drawn."""
    display = FakeDisplay([Placement('w1.english', 0, TICKS_PER_SECOND),
                           Placement('w1.chinese', TICKS_PER_SECOND,
                                     2 * TICKS_PER_SECOND)])
    preview = TextPreview(display=display, plan=plan_of())
    preview.resize(*CANVAS)
    preview.show()
    app.processEvents()
    try:
        assert [p.clip_id for p in preview.presentation().placements] == ['w1.english']
        preview.set_position(int(1.5 * TICKS_PER_SECOND))
        assert [p.clip_id for p in preview.presentation().placements] == ['w1.chinese']
        # A position with nothing due draws nothing - not a stale word.
        preview.set_position(int(2.0 * TICKS_PER_SECOND))
        assert preview.presentation().placements == ()
        assert display.calls >= 3
    finally:
        preview.stop()
        preview.close()
        app.processEvents()


def test_a_repaint_reuses_the_qt_objects_instead_of_building_them(app):
    """Tim's rule: no new QFont/QColor/QPen per repaint (and no per-frame timer)."""
    display = FakeDisplay([Placement('w1.english', 0, TICKS_PER_SECOND, colour='#ffd166')])
    preview = TextPreview(display=display, plan=plan_of())
    preview.resize(*CANVAS)
    preview.show()
    app.processEvents()
    try:
        preview.repaint()
        app.processEvents()
        first = preview.cache.diagnostics()
        assert first['fonts_cached'] >= 0 and first['colors_cached'] >= 1
        for _ in range(5):
            preview.repaint()
            app.processEvents()
        again = preview.cache.diagnostics()
        assert again['colors_cached'] == first['colors_cached']
        assert again['pens_cached'] == first['pens_cached']
        assert again['fonts_cached'] == first['fonts_cached']
        # Idle: nothing ticks.  Play: one low-frequency timer, and it stops again.
        assert preview._timer.isActive() is False
        preview.play()
        assert preview._timer.isActive() is True
        assert preview._timer.interval() >= 50          # 20 Hz or slower
        preview.pause()
        assert preview._timer.isActive() is False
    finally:
        preview.stop()
        preview.close()
        app.processEvents()


def test_playing_advances_the_position_and_stops_at_the_end(app):
    display = FakeDisplay([Placement('w1.english', 0, 2 * TICKS_PER_SECOND)])
    preview = TextPreview(display=display, plan=plan_of(seconds=1.0))
    preview.show()
    app.processEvents()
    try:
        preview.play()
        time.sleep(0.15)
        moved = preview.position_ticks
        assert 0 < moved <= TICKS_PER_SECOND
        preview.pause()
        preview.seek(0)
        assert preview.position_ticks == 0
        preview.seek(10 ** 9)
        assert preview.position_ticks == TICKS_PER_SECOND     # clamped to the lesson
        preview.play()
        preview.seek(10 ** 9)                                 # clamped while playing too
        assert preview.position_ticks == TICKS_PER_SECOND
        preview.pause()
    finally:
        preview.stop()
        preview.close()
        app.processEvents()


# -- the still --------------------------------------------------------------
def test_the_still_is_cached_by_source_identity_and_a_changed_source_invalidates_it(tmp_path):
    source = write_test_video(tmp_path / 'background.mp4', seconds=1.0, fps=FPS,
                              size='320x180')
    stills = StillFrames(tmp_path / '.preview', 320, 180)
    assert stills.request(source, 0.0) is None            # scheduled, not blocking
    assert stills.wait(timeout=60.0) is True
    found = stills.cached(source, 0.0)
    assert found is not None and found.stat().st_size > 0
    assert '0ms' in found.name and '320x180' in found.name
    # A second request is a lookup, never a second extraction.
    before = len(list(stills.cache_dir.glob('*.png')))
    assert stills.request(source, 0.0) == found
    assert len(list(stills.cache_dir.glob('*.png'))) == before
    # A changed source (different size/mtime) is a different key: no stale frame.
    other = write_test_video(tmp_path / 'background2.mp4', seconds=1.0, fps=FPS,
                             size='320x180')
    assert stills.cached(other, 0.0) is None
    assert stills.request(other, 0.0) is None
    assert stills.wait(timeout=60.0) is True
    assert stills.cached(other, 0.0) is not None


def test_a_missing_or_unreadable_background_is_explained_not_blank(app, tmp_path):
    missing = tmp_path / 'not-there.mp4'
    preview = TextPreview(display=FakeDisplay([]), plan=plan_of(background=True),
                          stills=StillFrames(tmp_path / '.preview', 320, 180))
    preview.background_paths = {'layer:background': str(missing)}
    preview.resize(*CANVAS)
    preview.show()
    app.processEvents()
    try:
        preview.set_position(0)
        assert '背景文件不存在' in preview.presentation().preparing
        assert preview.diagnostics()['still_error']
        # A still that cannot be extracted by ffmpeg is reported with its reason.
        broken = tmp_path / 'broken.mp4'
        broken.write_bytes(b'not a video at all')
        stills = StillFrames(tmp_path / '.preview2', 320, 180)
        assert stills.request(broken, 0.0) is None
        assert stills.wait(timeout=60.0) is True
        assert 'ffmpeg' in stills.error() or '抽帧' in stills.error()
        assert stills.stats()['failed']
    finally:
        preview.stop()
        preview.close()
        app.processEvents()


def test_the_window_builds_a_text_preview_and_no_timeline_or_session(app, tmp_path):
    with scratch('light-preview-') as folder:
        project = tiny_folder(folder / 'project')
        window = EditorWindow()
        window.attach(project)
        window.resize(900, 600)
        window.show()
        app.processEvents()
        try:
            assert window.timeline is None
            assert window.showing.session is None
            assert window.canvas is not None
            facts = window.canvas.diagnostics()
            assert facts['has_display'] is True
            # The preview is the same layout the delivery uses, at the canvas size.
            assert window.canvas.display is not None
            assert window.showing.still_frames is not None
            # Play/pause/seek still work: they move the playhead and repaint text.
            window.toggle_play()
            assert window.canvas.playing is True
            window.seek(0)
            window.toggle_play()
            assert window.canvas.playing is False
            assert window.canvas.presentation().position_ticks <= TICKS_PER_SECOND // 10
            assert window.diagnostics()['preview'] is not None
        finally:
            window.close_preview()
            window.close()
            app.processEvents()
