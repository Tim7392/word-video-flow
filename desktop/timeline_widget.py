"""The timeline: one row per role, one bar per clip, drag / trim / cut on it.

Rows are **roles**, not words.  A lesson's records follow one another in time, so
within a role every clip has its own stretch of the row and nothing overlaps; the
result is eleven rows whether the lesson has three words or fifty, which is what
makes a long batch navigable without a second scrolling axis.

What the widget does and does not do
------------------------------------
It draws the **solved plan** - the same ranges the canvas and the exporter use - so
a bar's position is never the widget's opinion.  A drag moves a *ghost* and emits a
request when the mouse is released; the command, the new revision and the undo
entry belong to :class:`desktop.editor_model.EditorState`.  The widget never edits
a clip.

The gestures are the product's decision, not a default:

* a plain click selects **exactly one** clip;
* ``Ctrl``+click adds or removes one (the explicit way to ask for a group move);
* dragging a selected bar moves every selected clip, and dragging an unselected one
  moves only it - an unselected neighbour is never carried along;
* the outer 6 px of a bar is a trim handle.  ``Alt``+click inside a bar asks for a
  cut at that tick, which is a request the editor may have to refuse (creating a
  clip is A's command, not the widget's).

Coordinates are Qt's logical pixels throughout, so a high-DPI screen changes the
device pixel ratio and not a single tick: the same 400-px-wide timeline is 400
logical pixels at 100% and 150% and maps to the same times.
"""
from PySide6 import QtCore, QtGui, QtWidgets

__all__ = ['TimelineWidget', 'ROW_HEIGHT', 'GUTTER', 'RULER_HEIGHT', 'HANDLE_PX']

#: Row geometry, in logical pixels.  Rows follow the widget font so a scaled
#: desktop does not produce rows too short for their own labels.
ROW_HEIGHT = 22
GUTTER = 132
RULER_HEIGHT = 20
HANDLE_PX = 6
#: Colour per role family; the point is to tell speech from text from layers at a
#: glance, not to be pretty.
ROLE_COLORS = {
    'female': '#3d7dd8', 'male': '#2f9e8f', 'chinese': '#b07d2b',
    'english': '#6b62c9', 'phonetic': '#8a6bbf', 'meaning': '#a05f9e',
    'title': '#5f6b7a', 'subtitle': '#5f6b7a', 'footer': '#5f6b7a',
    'intro': '#c0603f', 'background': '#4a4a52',
}
#: The rows, in the order they are drawn: the teaching stages first, then the text
#: layers, then the project layers - the same grouping the document uses.
LANES = (('female', '女声英文'), ('male', '男声英文'), ('chinese', '中文释义朗读'),
         ('english', '英文字幕'), ('phonetic', '音标'), ('meaning', '中文释义字幕'),
         ('intro', '片头'), ('title', '标题'), ('subtitle', '批次副标题'),
         ('footer', '页脚'))
LANE_INDEX = {role: index for index, (role, _) in enumerate(LANES)}


class TimelineWidget(QtWidgets.QWidget):
    """Draws the plan; turns mouse gestures into edit *requests*."""

    clipSelected = QtCore.Signal(str, bool)     # clip_id, additive
    selectionCleared = QtCore.Signal()
    clipsMoved = QtCore.Signal(tuple, int)      # clip_ids, delta_ticks
    clipTrimmed = QtCore.Signal(str, str, int)  # clip_id, edge, ticks
    splitRequested = QtCore.Signal(str, int)    # clip_id, at_ticks
    playheadMoved = QtCore.Signal(int)          # ticks

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)
        self.plan = None
        self.selection = ()
        self.total_ticks = 0
        self.frame_ticks = 1
        self.playhead = 0
        self.zoom = 1.0
        self.origin_ticks = 0
        self.last_gesture = {}
        self._drag = None
        self._ghost = None

    # -- data ------------------------------------------------------------
    def set_plan(self, plan, *, selection=(), playhead=None):
        """Adopt one solved plan; the bars are its items and nothing else."""
        self.plan = plan
        self.selection = tuple(selection)
        self.total_ticks = int(getattr(plan, 'total_ticks', 0) or 0)
        if playhead is not None:
            self.playhead = int(playhead)
        self.update()
        return self

    def set_selection(self, selection):
        self.selection = tuple(selection)
        self.update()

    def set_playhead(self, ticks):
        self.playhead = int(ticks)
        self.update()

    def items(self):
        return tuple(self.plan.video + self.plan.audio) if self.plan else ()

    def item(self, clip_id):
        for item in self.items():
            if item.clip_id == clip_id:
                return item
        return None

    # -- geometry --------------------------------------------------------
    def row_top(self, role):
        index = LANE_INDEX.get(role)
        if index is None:
            return None
        return RULER_HEIGHT + index * ROW_HEIGHT

    def content_rect(self):
        return QtCore.QRect(GUTTER, 0, max(1, self.width() - GUTTER),
                            max(1, self.height()))

    def visible_ticks(self):
        return max(1.0, self.total_ticks / max(1.0, float(self.zoom)))

    def scale(self):
        """Pixels per tick, from the widget's own width (logical pixels)."""
        return max(1e-9, self.content_rect().width() / self.visible_ticks())

    def ticks_to_x(self, ticks):
        return GUTTER + (float(ticks) - self.origin_ticks) * self.scale()

    def x_to_ticks(self, x):
        return int(round(self.origin_ticks + (float(x) - GUTTER) / self.scale()))

    def bar_rect(self, item):
        top = self.row_top(item.role)
        if top is None:
            return None
        left = self.ticks_to_x(item.start_ticks)
        right = self.ticks_to_x(item.end_ticks)
        return QtCore.QRectF(left, top + 1, max(2.0, right - left), ROW_HEIGHT - 2)

    def bar_at(self, position):
        """The topmost item whose bar contains ``position`` (logical pixels)."""
        for item in reversed(self.items()):
            rect = self.bar_rect(item)
            if rect is not None and rect.contains(QtCore.QPointF(position)):
                return item
        return None

    def edge_at(self, item, position):
        """``'start'``, ``'end'`` or ``''`` - which trim handle a point is on."""
        rect = self.bar_rect(item)
        if rect is None or not rect.contains(QtCore.QPointF(position)):
            return ''
        if position.x() - rect.left() <= HANDLE_PX:
            return 'start'
        if rect.right() - position.x() <= HANDLE_PX:
            return 'end'
        return ''

    # -- snapping --------------------------------------------------------
    def candidates(self, exclude=()):
        points = [0, self.total_ticks]
        for item in self.items():
            if item.clip_id in exclude:
                continue
            points += [item.start_ticks, item.end_ticks]
        return points

    def snap(self, ticks, exclude=(), tolerance_ticks=None):
        """Nearest bar edge or whole second, then the frame grid."""
        from .editor_model import SNAP_TOLERANCE_TICKS, nearest_candidate, snap_to_grid
        tolerance = SNAP_TOLERANCE_TICKS if tolerance_ticks is None else tolerance_ticks
        seconds = int(self.total_ticks // 720000) + 1
        candidates = list(self.candidates(exclude))
        candidates += [index * 720000 for index in range(seconds + 1)]
        return snap_to_grid(nearest_candidate(int(ticks), candidates, tolerance),
                            self.frame_ticks)

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        try:
            painter.fillRect(self.rect(), QtGui.QColor('#14161a'))
            self._paint_ruler(painter)
            self._paint_rows(painter)
            self._paint_bars(painter)
            self._paint_playhead(painter)
        finally:
            painter.end()

    def _paint_ruler(self, painter):
        painter.setPen(QtGui.QColor('#6a7280'))
        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() - 1.0))
        painter.setFont(font)
        step = self._ruler_step_seconds()
        seconds = 0
        while seconds * 720000 <= self.total_ticks:
            x = self.ticks_to_x(seconds * 720000)
            if x >= GUTTER:
                painter.drawLine(QtCore.QPointF(x, RULER_HEIGHT - 6),
                                 QtCore.QPointF(x, self.height()))
                painter.drawText(QtCore.QRectF(x + 3, 1, 60, RULER_HEIGHT - 4),
                                 QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                                 '%gs' % seconds)
            seconds += step
        painter.setPen(QtGui.QColor('#3a3f47'))
        painter.drawLine(GUTTER, 0, GUTTER, self.height())

    def _ruler_step_seconds(self):
        span = self.visible_ticks() / 720000.0
        for step in (1, 2, 5, 10, 15, 30, 60, 120, 300):
            if span / step <= 12:
                return step
        return 600

    def _paint_rows(self, painter):
        font = painter.font()
        for index, (role, label) in enumerate(LANES):
            top = RULER_HEIGHT + index * ROW_HEIGHT
            if index % 2:
                painter.fillRect(QtCore.QRect(0, top, self.width(), ROW_HEIGHT),
                                 QtGui.QColor('#181b20'))
            painter.setPen(QtGui.QColor('#9aa3ae'))
            painter.drawText(QtCore.QRect(6, top, GUTTER - 12, ROW_HEIGHT),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, label)
        painter.setFont(font)

    def _paint_bars(self, painter):
        for item in self.items():
            rect = self.bar_rect(item)
            if rect is None or rect.right() < GUTTER or rect.left() > self.width():
                continue
            colour = QtGui.QColor(ROLE_COLORS.get(item.role, '#5f6b7a'))
            ghost = self._ghost if self._ghost else None
            if ghost and ghost['clip_id'] == item.clip_id:
                rect = QtCore.QRectF(rect)
                if ghost['kind'] == 'move':
                    rect.translate(ghost['delta_ticks'] * self.scale(), 0)
                elif ghost['edge'] == 'start':
                    left = self.ticks_to_x(ghost['ticks'])
                    rect.setLeft(left)
                else:
                    rect.setRight(self.ticks_to_x(ghost['ticks']))
            if item.clip_id in self.selection:
                painter.setPen(QtGui.QPen(QtGui.QColor('#ffd166'), 2))
            else:
                painter.setPen(QtGui.QPen(colour.darker(140), 1))
            painter.setBrush(colour)
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QtGui.QColor('#f0f2f5'))
            text = (item.text or item.clip_id)
            painter.drawText(rect.adjusted(5, 0, -5, 0),
                             QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 text, QtCore.Qt.ElideRight, int(rect.width()) - 8))

    def _paint_playhead(self, painter):
        x = self.ticks_to_x(self.playhead)
        if x < GUTTER:
            return
        painter.setPen(QtGui.QPen(QtGui.QColor('#ff5d5d'), 1))
        painter.drawLine(QtCore.QPointF(x, 0), QtCore.QPointF(x, self.height()))

    # -- mouse -----------------------------------------------------------
    def mousePressEvent(self, event):
        position = event.position()
        self.last_gesture = {}
        if event.button() == QtCore.Qt.MiddleButton or (
                event.button() == QtCore.Qt.LeftButton
                and event.modifiers() & QtCore.Qt.ShiftModifier
                and self.bar_at(position) is None):
            self._drag = {'kind': 'pan', 'x': position.x(),
                          'origin': self.origin_ticks}
            return
        if position.y() < RULER_HEIGHT:
            ticks = max(0, min(self.x_to_ticks(position.x()), self.total_ticks))
            self.playheadMoved.emit(int(ticks))
            return
        item = self.bar_at(position)
        if item is None:
            self.selectionCleared.emit()
            self._drag = None
            return
        additive = bool(event.modifiers() & QtCore.Qt.ControlModifier)
        if additive:
            self.clipSelected.emit(item.clip_id, True)
        elif item.clip_id not in self.selection:
            self.clipSelected.emit(item.clip_id, False)
        if event.modifiers() & QtCore.Qt.AltModifier:
            self.splitRequested.emit(item.clip_id, self.snap(self.x_to_ticks(position.x()),
                                                             exclude=(item.clip_id,)))
            self._drag = None
            return
        edge = self.edge_at(item, position)
        if edge:
            self._drag = {'kind': 'trim', 'clip_id': item.clip_id, 'edge': edge,
                          'ticks': item.start_ticks if edge == 'start' else item.end_ticks}
        else:
            selected = tuple(self.selection) if item.clip_id in self.selection \
                else (item.clip_id,)
            # Pressing a member of a group keeps the group, so it can be dragged as
            # one - but a press that never moves is a *click*, and the product rule
            # is that a plain click selects exactly one clip.  Which of the two it
            # was is only known on release, so the intent is carried there.
            collapse = bool(not additive and item.clip_id in self.selection
                            and len(self.selection) > 1)
            self._drag = {'kind': 'move', 'clip_ids': selected, 'x': position.x(),
                          'delta_ticks': 0, 'primary': item.clip_id,
                          'start': item.start_ticks, 'collapse': collapse,
                          'anchor': self.snap(item.start_ticks, exclude=selected)}
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            item = self.bar_at(event.position())
            if item is not None and self.edge_at(item, event.position()):
                self.setCursor(QtCore.Qt.SizeHorCursor)
            else:
                self.setCursor(QtCore.Qt.ArrowCursor)
            return
        position = event.position()
        if self._drag['kind'] == 'pan':
            delta_px = position.x() - self._drag['x']
            self.origin_ticks = max(0.0, self._drag['origin'] - delta_px / self.scale())
            self._clamp_origin()
            self.update()
            return
        if self._drag['kind'] == 'move':
            wanted = self.x_to_ticks(position.x()) - (
                self.x_to_ticks(self._drag['x']) - self._drag['anchor'])
            snapped = self.snap(wanted, exclude=self._drag['clip_ids'])
            self._drag['delta_ticks'] = int(snapped - self._drag['start'])
            self._ghost = {'kind': 'move', 'clip_id': self._drag['primary'],
                           'delta_ticks': self._drag['delta_ticks']}
        else:
            ticks = max(0, self.snap(self.x_to_ticks(position.x()),
                                     exclude=(self._drag['clip_id'],)))
            self._drag['ticks'] = int(ticks)
            self._ghost = {'kind': 'trim', 'clip_id': self._drag['clip_id'],
                           'edge': self._drag['edge'], 'ticks': int(ticks)}
        self.update()

    def mouseReleaseEvent(self, event):
        drag, self._drag, self._ghost = self._drag, None, None
        self.update()
        if not drag:
            return
        if drag['kind'] == 'move' and drag['delta_ticks']:
            self.last_gesture = {'kind': 'move', 'clip_ids': tuple(drag['clip_ids']),
                                 'delta_ticks': int(drag['delta_ticks'])}
            self.clipsMoved.emit(tuple(drag['clip_ids']), int(drag['delta_ticks']))
        elif drag['kind'] == 'move' and drag.get('collapse'):
            self.last_gesture = {'kind': 'collapse', 'clip_ids': (drag['primary'],)}
            self.clipSelected.emit(drag['primary'], False)
        elif drag['kind'] == 'trim':
            item = self.item(drag['clip_id'])
            original = None
            if item is not None:
                original = item.start_ticks if drag['edge'] == 'start' else item.end_ticks
            if original is not None and int(drag['ticks']) != int(original):
                self.last_gesture = {'kind': 'trim', 'clip_id': drag['clip_id'],
                                     'edge': drag['edge'], 'ticks': int(drag['ticks'])}
                self.clipTrimmed.emit(drag['clip_id'], drag['edge'], int(drag['ticks']))

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        anchor = self.x_to_ticks(event.position().x())
        self.zoom = min(64.0, max(1.0, self.zoom * (1.25 ** steps)))
        if self.zoom <= 1.0:
            self.zoom, self.origin_ticks = 1.0, 0.0
        else:
            self.origin_ticks = max(0.0, anchor - (event.position().x() - GUTTER)
                                    / self.scale())
        self._clamp_origin()
        self.update()

    def _clamp_origin(self):
        overflow = max(0.0, self.total_ticks - self.visible_ticks())
        self.origin_ticks = min(max(0.0, self.origin_ticks), overflow)
        if self.zoom <= 1.0:
            self.origin_ticks = 0.0

    def fit(self):
        self.zoom, self.origin_ticks = 1.0, 0.0
        self.update()

    def diagnostics(self):
        """What a test (or a support call) needs to know about the view."""
        rect = self.content_rect()
        return {'zoom': round(self.zoom, 4), 'origin_ticks': int(self.origin_ticks),
                'total_ticks': int(self.total_ticks), 'rows': len(LANES),
                'width': rect.width(), 'height': self.height(),
                'device_pixel_ratio': self.devicePixelRatioF(),
                'pixels_per_second': round(self.scale() * 720000, 4),
                'selected': list(self.selection), 'bars': len(self.items())}
