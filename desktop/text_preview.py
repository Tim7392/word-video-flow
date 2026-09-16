"""文字预览：一张背景静帧 + B 的 placements，只推进时间，不解码任何媒体。

Tim 2026-09-17: 预览只做**模板效果**（文字/单词），不做可编辑的时间线、不回放视频。
So this widget is the whole preview now:

* the picture is **one still** of the delivery's background (:mod:`preview.still`),
  extracted once per source segment and cached; nothing decodes per frame;
* the text comes from the **same layout port** the export uses
  (``LayoutSurfaceDisplay.placements_at``), so what the member approves here is what the
  delivered MP4 draws - this widget has no layout of its own;
* the clock is wall time: play advances ``position_ticks`` from ``time.monotonic()``,
  pause and seek set it directly.  There is no audio device and no mixer, so nothing
  can drift and nothing has to be kept in sync;
* repaints are **event-driven**: a new position, a new still, a new style or a resize
  calls ``update()``, and while playing a single low-frequency timer (15 Hz by default)
  does the advancing.  No 20/60 Hz timer, and no QFont/QColor/QPen is constructed
  during a repaint - the caches in :mod:`desktop.text_draw` are shared with the video
  canvas.

Failure is visible, never blank: while the still is being extracted the canvas says so,
and a still that cannot be made shows the reason on the canvas and in
:meth:`diagnostics`.
"""
import time
from dataclasses import dataclass, field

from PySide6 import QtCore, QtGui, QtWidgets

from preview.still import StillFrames
from word_video.domain.timebase import TICKS_PER_SECOND

from .text_draw import TextStyleCache, draw_placements, draw_placeholder

__all__ = ['TextPresentation', 'TextPreview']

#: Repaints per second while playing.  15 Hz is enough for text that changes on word
#: boundaries (seconds apart) and is a twentieth of the video canvas's 60 Hz.
DEFAULT_PLAY_HZ = 15


@dataclass(frozen=True)
class TextPresentation:
    """What the preview is showing; the same shape the video canvas reported.

    The window reads ``position_ticks``/``position_seconds``/``state``/``preparing``
    from it, so the light preview and the video canvas are interchangeable for the
    caller - which is what made swapping one for the other a one-file change.
    """

    position_ticks: int
    placements: tuple = ()
    playing: bool = False
    state: str = 'idle'
    still: str = ''
    preparing: str = ''
    frame: object = None
    generation: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def position_seconds(self):
        return self.position_ticks / float(TICKS_PER_SECOND)

    def to_dict(self):
        return {'position_ticks': self.position_ticks,
                'position_seconds': round(self.position_seconds, 4),
                'placements': len(self.placements), 'playing': self.playing,
                'state': self.state, 'still': self.still or None,
                'preparing': self.preparing, **self.extra}


class TextPreview(QtWidgets.QWidget):
    """The template preview: background still, text layer, a playhead."""

    def __init__(self, display=None, plan=None, *, stills=None, parent=None,
                 play_hz=DEFAULT_PLAY_HZ):
        super().__init__(parent)
        self.display = display
        self.plan = plan
        self.stills = stills
        self.setAutoFillBackground(True)
        self.setMinimumSize(160, 90)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.cache = TextStyleCache()
        self.selection = ()
        self._still = None
        self._still_for = None
        self._still_image = None
        self._still_error = ''
        self._position_ticks = 0
        self._playing = False
        self._started_wall = None
        self._started_ticks = 0
        self._presentation = None
        self.paints = 0
        self.repaints = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.CoarseTimer)
        self._timer.setInterval(max(10, int(1000 / float(play_hz))))
        self._timer.timeout.connect(self._advance_clock)

    # -- wiring ----------------------------------------------------------
    def set_content(self, plan, display, stills=None):
        """Adopt a newly solved plan: new placements, new stills, still frame 0."""
        self.plan = plan
        self.display = display
        if stills is not None:
            self.stills = stills
        self._still = None
        self._still_for = None
        self._still_image = None
        self._still_error = ''
        self.set_position(self._position_ticks)
        return self

    def set_selection(self, clip_ids):
        self.selection = tuple(clip_ids or ())
        self.update()
        return self.selection

    # -- the clock -------------------------------------------------------
    @property
    def position_ticks(self):
        if self._playing and self._started_wall is not None:
            elapsed = time.monotonic() - self._started_wall
            return self._clamp(self._started_ticks + int(elapsed * TICKS_PER_SECOND))
        return self._position_ticks
    def _clamp(self, ticks):
        total = 0 if self.plan is None else int(self.plan.total_ticks)
        return max(0, min(int(ticks), total))

    def set_position(self, ticks):
        """Move the playhead; repaints only if the picture can differ."""
        ticks = self._clamp(ticks)
        if self._playing:
            self._started_ticks = ticks
            self._started_wall = time.monotonic()
        changed = ticks != self._position_ticks
        self._position_ticks = ticks
        # The still is refreshed on every move, not only when the tick changed: the
        # *segment* may have changed (a cut background asks for another frame), and the
        # first call after a rebuild must ask for one at all.
        self._refresh_still()
        if changed or self._still_image is None:
            self.update()
        return self._position_ticks

    def play(self):
        if self.plan is None:
            return self
        if self.position_ticks >= int(self.plan.total_ticks):
            self._position_ticks = 0
        self._started_ticks = self._position_ticks
        self._started_wall = time.monotonic()
        self._playing = True
        self._timer.start()
        self.update()
        return self

    def pause(self):
        self._position_ticks = self.position_ticks
        self._playing = False
        self._started_wall = None
        self._timer.stop()
        self.update()
        return self

    def toggle(self):
        return self.pause() if self._playing else self.play()

    @property
    def playing(self):
        return self._playing

    def seek(self, ticks):
        self.set_position(ticks)
        return self.position_ticks

    def stop(self):
        """Stop the timer and drop the decoded still; safe to call twice."""
        self._timer.stop()
        self._playing = False
        self._still_image = None

    def _advance_clock(self):
        """The only periodic work while playing: move the playhead, repaint if needed."""
        position = self.position_ticks
        if self.plan is not None and position >= int(self.plan.total_ticks):
            self.pause()
            return
        self._position_ticks = position
        self._refresh_still()
        self.update()

    # -- the still -------------------------------------------------------
    def _refresh_still(self):
        """Ask for the still the current position needs, without blocking the UI."""
        if self.stills is None or self.display is None:
            return
        source, seconds = self._still_source()
        if source is None:
            self._still_for = None
            return
        key = (str(source), round(float(seconds), 3))
        if key == self._still_for and self._still is not None:
            return
        self._still_for = key
        found = self.stills.request(source, seconds)
        self._still = found
        self._still_error = '' if found is not None else (self.stills.error() or '')
        if found is not None:
            self._still_image = QtGui.QImage(str(found))
        elif not self._still_error:
            self._still_error = '正在准备背景静帧…'

    def _still_source(self):
        """Which background file, at which instant, this position shows.

        One still per background segment: a project cut with ``SplitClip`` plays its
        segments in order, and each asks for the frame at its own source start.  The
        **intro** is deliberately not a source: Tim's rule is that the intro shows the
        background still, not a replay of the countdown animation.  A plan with no
        background picture legitimately has none (text on the backdrop), which is why
        this is a question and not an error.
        """
        if self.plan is None:
            return None, 0.0
        backgrounds = [item for item in self.plan.video
                       if item.source is not None and item.role == 'background']
        if not backgrounds:
            return None, 0.0
        chosen = backgrounds[0]
        for item in backgrounds:
            if item.start_ticks <= self.position_ticks < item.end_ticks:
                chosen = item
                break
        source = chosen.source
        seconds = source.source_start * source.unit_num / float(source.unit_den)
        return self._published_path(source.asset_id), seconds

    def _published_path(self, asset_id):
        """The delivery's own background path, when the caller gave us one."""
        resolver = getattr(self, 'background_paths', None)
        if isinstance(resolver, dict) and asset_id in resolver:
            return resolver[asset_id]
        return asset_id

    def on_still_ready(self):
        """Called from the still worker: repaint, but never paint from that thread."""
        QtCore.QMetaObject.invokeMethod(self, 'update', QtCore.Qt.QueuedConnection)

    # -- painting --------------------------------------------------------
    def presentation(self):
        """What the canvas is showing, for the window's label and diagnostics."""
        placements = ()
        if self.display is not None:
            placements = tuple(self.display.placements_at(self.position_ticks))
        self._presentation = TextPresentation(
            position_ticks=self.position_ticks, placements=placements,
            playing=self._playing, state='playing' if self._playing else 'idle',
            still='' if self._still is None else str(self._still),
            preparing=self._still_error)
        return self._presentation

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            self.paints += 1
            rect = self.rect()
            painter.fillRect(rect, self.cache.backdrop)
            if self._still_image is not None and not self._still_image.isNull():
                # No smooth transform for the picture: the still is already at canvas
                # size and the scaled blit was the whole cost of a repaint (measured:
                # ~9 ms per frame at 15 Hz).  Text quality is unaffected - glyphs are
                # drawn as paths, not scaled from this image.
                painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)
                painter.drawImage(rect, self._still_image)
            elif self._still_error:
                draw_placeholder(painter, self._still_error, rect, self.cache)
            presentation = self.presentation()
            draw_placements(painter, presentation.placements, rect, self.cache,
                            self.selection)
        finally:
            painter.end()

    def diagnostics(self):
        facts = self.cache.diagnostics()
        facts.update({'paints': self.paints, 'playing': self._playing,
                      'position_ticks': self.position_ticks,
                      'still': None if self._still is None else str(self._still),
                      'still_error': self._still_error,
                      'has_display': self.display is not None,
                      'placements': 0 if self.display is None
                                    else len(self.display.placements_at(self.position_ticks))})
        return facts
