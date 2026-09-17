"""The editor window: text preview, property panel, notices, delivery, export.

Layout, and why
---------------
* the **preview** is :class:`~desktop.text_preview.TextPreview`: one cached still of the
  delivery's background plus the placements B's ``LayoutSurface`` publishes.  Tim's
  direction (2026-09-17) is that the editor previews the **template's text effect**, not
  the delivery: no video decode, no segment proxies, no audio device, no mixer, and
  repaints only when something can actually differ. The retired video canvas has
  been removed; continuous-session engine tests remain independent of this window;
* the **property panel** edits a style role independently of clip selection through
  :class:`~desktop.editor_model.EditorState`: size, color and normalized position
  use A's existing commands; fonts remain read-only;
* the **notices panel** is where a refusal lands, with a button per repair.  There
  is no dialog that only says 确定;
* the **delivery panel** holds what is not document content: background, intro
  clip, codec.  B's exporter takes exactly these as parameters, so the panel and
  the delivery cannot drift apart.

The editable timeline ("模拟剪映" drag/trim/split/bind) is **offlined**: A's commands
still exist and are still tested, and editing proper happens in 剪映 on the delivered
draft, which is the requirement that matters. The unused timeline widget is removed.

The window owns no edit logic.  Everything it does to the project it does by
calling one method on the state, and every message it shows comes from
:mod:`desktop.editor_notices`.
"""
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from word_video.domain.errors import ProjectError
from word_video.exporters.catalog import CatalogError

from . import editor_notices as notices
from .editor_export import blocking_notices, export_folder
from .editor_model import MODE_ADVANCED, MODE_TEMPLATE, EditorState
from .editor_project import ProjectFolder
from .legacy_dialog import LegacyDialog

__all__ = ['EditorWindow', 'ExportWorker']


class ExportWorker(QtCore.QObject):
    """Runs one export off the UI thread; the project is already saved."""

    finished = QtCore.Signal(object)
    progress = QtCore.Signal(object)

    def __init__(self, folder, state, output=''):
        super().__init__()
        self.folder = folder
        self.state = state
        self.output = output

    @QtCore.Slot()
    def run(self):
        try:
            outcome = export_folder(self.folder, self.state, output=self.output,
                                    progress=self.progress.emit, save=False)
        except Exception as error:                      # noqa: BLE001 - reported to the UI
            import traceback
            from .editor_export import ExportOutcome
            outcome = ExportOutcome(ok=False, notices=(
                notices.Notice(code=type(error).__name__, message='导出线程异常：%s' % error,
                               severity=notices.SEVERITY_BLOCK,
                               detail={'traceback': traceback.format_exc()[-4000:]}),))
        self.finished.emit(outcome)


@dataclass
class WindowState:
    """What the window is currently showing; kept in one place for tests."""

    folder: object = None
    state: object = None
    session: object = None
    #: The background-still cache behind the text preview (``None`` when no preview).
    still_frames: object = None
    position_ticks: int = 0
    preview_error: str = ''
    preview_builds: int = 0


class EditorWindow(QtWidgets.QMainWindow):
    """The member-facing window."""

    def __init__(self, folder=None, state=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('单词教学视频 — 编辑器')
        self.showing = WindowState()
        self._exporting = False
        self._thread = None
        self._worker = None
        self._last_notices = ()
        self.last_export = None
        #: The last edit's outcome (command type, moved ids, notes).  Kept because
        #: "which command did this gesture become" is the question a member's
        #: support call - and the acceptance measurement - actually asks.
        self.last_outcome = None
        #: The last old-factory run (W10) and the word list it used, so the entry can
        #: offer the same list again without asking the member to find it twice.
        self.last_legacy = None
        self._last_wordlist = ''
        #: The last import attempt's own account of itself (ok, reason, fix).  Read by
        #: the driven run's report, so a failed import is never just "ok: false".
        self.last_import = None
        self._build_ui()
        self._build_menus()
        if folder is not None:
            self.attach(folder, state)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        outer.addWidget(self.splitter)

        top = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.addWidget(top)

        picture = QtWidgets.QWidget()
        picture_box = QtWidgets.QVBoxLayout(picture)
        picture_box.setContentsMargins(0, 0, 0, 0)
        self.canvas_holder = QtWidgets.QWidget()
        self.canvas_layout = QtWidgets.QVBoxLayout(self.canvas_holder)
        self.canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = None
        picture_box.addWidget(self.canvas_holder, 1)
        self.transport = QtWidgets.QHBoxLayout()
        self.play_button = QtWidgets.QPushButton('播放')
        self.play_button.clicked.connect(self.toggle_play)
        self.position_label = QtWidgets.QLabel('0.000s')
        self.state_label = QtWidgets.QLabel('未打开工程')
        self.transport.addWidget(self.play_button)
        self.transport.addWidget(self.position_label)
        self.transport.addWidget(self.state_label, 1)
        picture_box.addLayout(self.transport)
        seek_row = QtWidgets.QHBoxLayout()
        self.seek_edit = QtWidgets.QDoubleSpinBox()
        self.seek_edit.setDecimals(3)
        self.seek_edit.setRange(0, 100000)
        self.seek_edit.setSuffix(' s')
        self.seek_button = QtWidgets.QPushButton('定位')
        self.seek_button.clicked.connect(
            lambda: self.seek(self.state.ticks_from_seconds(self.seek_edit.value(), snap=False))
            if self.state is not None else None)
        self.progress_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.progress_slider.setRange(0, 0)
        self.progress_slider.valueChanged.connect(lambda ms: self.seek(ms * 720))
        seek_row.addWidget(self.seek_edit)
        seek_row.addWidget(self.seek_button)
        seek_row.addWidget(self.progress_slider, 1)
        picture_box.addLayout(seek_row)
        top.addWidget(picture)

        side = QtWidgets.QTabWidget()
        side.addTab(self._build_property_panel(), '属性')
        side.addTab(self._build_delivery_panel(), '交付设置')
        side.setMinimumWidth(300)
        self.side_tabs = side
        top.addWidget(side)
        top.setSizes([900, 320])

        bottom = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        # The editable timeline is offlined (Tim 2026-09-17: 不做可编辑的时间线模拟剪映,
        # 只做模板/文字效果预览). The unused widget and gesture tests are removed:
        # drag/trim/split/bind are not
        # offered, and nothing repaints a few hundred bars at 20-60 Hz while a delivery
        # is being encoded.
        self.timeline = None
        bottom.addWidget(self._build_notice_panel())
        bottom.setSizes([200])
        self.splitter.addWidget(bottom)
        self.splitter.setSizes([520, 200])

        self.status = self.statusBar()
        self.status.showMessage('就绪')

    def _build_property_panel(self):
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        self.role_box = QtWidgets.QComboBox()
        self.role_box.currentIndexChanged.connect(self._update_property_panel)
        form.addRow('样式角色', self.role_box)
        label = QtWidgets.QLabel('—')
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.property_labels = {'style': label}
        form.addRow('字体（只读）/当前样式', label)
        self.size_edit = QtWidgets.QDoubleSpinBox()
        self.size_edit.setDecimals(1)
        self.size_edit.setRange(1.0, 2000.0)
        self.size_edit.setSingleStep(10.0)
        self.size_button = QtWidgets.QPushButton('应用字号（该角色的全部片段）')
        self.size_button.clicked.connect(self.on_apply_size)
        self.size_reset = QtWidgets.QPushButton('恢复默认字号/样式')
        self.size_reset.clicked.connect(self.on_clear_style)
        form.addRow('字号（1..2000 px，2160 高设计尺度）', self.size_edit)
        form.addRow(self.size_button)
        self.color_edit = QtWidgets.QLineEdit()
        self.color_edit.setPlaceholderText('#RRGGBB')
        self.color_button = QtWidgets.QPushButton('应用颜色（该角色的全部片段）')
        self.color_button.clicked.connect(self.on_apply_color)
        form.addRow('颜色（#RRGGBB）', self.color_edit)
        form.addRow(self.color_button)
        self.x_edit = QtWidgets.QDoubleSpinBox()
        self.y_edit = QtWidgets.QDoubleSpinBox()
        for edit in (self.x_edit, self.y_edit):
            edit.setDecimals(16)
            edit.setRange(0.0, 1.0)
            edit.setSingleStep(0.01)
        form.addRow('位置 x（0..1，画布宽度比例）', self.x_edit)
        form.addRow('位置 y（0..1，画布高度比例）', self.y_edit)
        hint = QtWidgets.QLabel('文字块中心坐标，与 LayoutSurface 相同：左上 (0,0)，'
                                '右下 (1,1)。点击应用后立即预览。')
        hint.setWordWrap(True)
        form.addRow(hint)
        self.position_button = QtWidgets.QPushButton('应用位置（该角色的全部片段）')
        self.position_button.clicked.connect(self.on_apply_position)
        form.addRow(self.position_button)
        form.addRow(self.size_reset)
        self.mode_box = QtWidgets.QComboBox()
        self.mode_box.addItems(['模板模式', '高级模式'])
        self.mode_box.currentIndexChanged.connect(self.on_mode_changed)
        form.addRow('模式', self.mode_box)
        return panel

    def _build_delivery_panel(self):
        panel = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(panel)
        self.delivery_edits = {}
        for key, title in (('background', '背景视频'), ('intro_video', '片头视频'),
                           ('intro_audio', '片头音轨'), ('video_codec', '编码')):
            row = QtWidgets.QWidget()
            box = QtWidgets.QHBoxLayout(row)
            box.setContentsMargins(0, 0, 0, 0)
            edit = QtWidgets.QLineEdit()
            edit.setReadOnly(key == 'video_codec')
            box.addWidget(edit, 1)
            if key != 'video_codec':
                button = QtWidgets.QPushButton('选择…')
                button.clicked.connect(lambda _=False, name=key: self.choose_delivery_file(name))
                box.addWidget(button)
            self.delivery_edits[key] = edit
            form.addRow(title, row)
        save = QtWidgets.QPushButton('保存交付设置')
        save.clicked.connect(self.save_delivery)
        form.addRow(save)
        hint = QtWidgets.QLabel('背景与片头是交付参数（B 的导出器按参数渲染），'
                                '不是工程内容；改了这里预览与成片一起变。')
        hint.setWordWrap(True)
        form.addRow(hint)
        return panel

    def _build_notice_panel(self):
        panel = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(panel)
        box.setContentsMargins(4, 4, 4, 4)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel('检查与提示'))
        self.recheck_button = QtWidgets.QPushButton('重新检查')
        self.recheck_button.clicked.connect(self.refresh_notices)
        header.addWidget(self.recheck_button)
        box.addLayout(header)
        self.notice_area = QtWidgets.QScrollArea()
        self.notice_area.setWidgetResizable(True)
        self.notice_host = QtWidgets.QWidget()
        self.notice_layout = QtWidgets.QVBoxLayout(self.notice_host)
        self.notice_layout.setAlignment(QtCore.Qt.AlignTop)
        self.notice_area.setWidget(self.notice_host)
        box.addWidget(self.notice_area, 1)
        return panel

    def _build_menus(self):
        file_menu = self.menuBar().addMenu('文件')
        self.action_open = file_menu.addAction('打开工程…', self.choose_open)
        self.action_import = file_menu.addAction('从请求导入工程…', self.choose_import)
        file_menu.addSeparator()
        self.action_save = file_menu.addAction('保存', self.save, 'Ctrl+S')
        self.action_export = file_menu.addAction('导出三产物', self.export, 'Ctrl+E')
        file_menu.addSeparator()
        # The old factory is a different clock, so it is a differently named entry and
        # never a mode of "导出三产物": the dialog it opens says which clock it is on.
        self.action_legacy = file_menu.addAction('旧字幕工厂（文字规则计时）…', self.choose_legacy)
        file_menu.addSeparator()
        file_menu.addAction('退出', self.close)
        edit_menu = self.menuBar().addMenu('编辑')
        self.action_undo = edit_menu.addAction('撤销', self.undo, 'Ctrl+Z')
        self.action_redo = edit_menu.addAction('重做', self.redo, 'Ctrl+Y')
        edit_menu.addSeparator()
        self.action_reload = edit_menu.addAction('放弃改动并重新读取', self.reload)
        view_menu = self.menuBar().addMenu('视图')
        view_menu.addAction('重新构建文字预览', lambda: self.refresh(rebuild_preview=True))
        self._update_actions()

    # ------------------------------------------------------------- binding
    def attach(self, folder, state=None):
        """Adopt a folder and its editor state, and build the first preview."""
        self.close_preview()
        self.showing = WindowState(folder=folder)
        self.showing.state = state if state is not None else EditorState(
            folder.project, media=folder.media_table(), path=folder.project_path)
        self.setWindowTitle('单词教学视频 — %s' % folder.path)
        self._fill_delivery()
        self.mode_box.setCurrentIndex(0 if self.showing.state.mode == MODE_TEMPLATE else 1)
        self.refresh(rebuild_preview=True, keep_position=False)
        return self.showing.state

    @property
    def state(self):
        return self.showing.state

    @property
    def folder(self):
        return self.showing.folder

    def _require_state(self):
        if self.state is None or self.folder is None:
            self.status.showMessage('先打开或导入一个工程')
            return False
        return True

    # -------------------------------------------------------------- refresh
    def refresh(self, *, rebuild_preview=True, keep_position=True, reason=''):
        """Re-solve, rebuild the text preview, re-list the notices."""
        if not self._require_state():
            return
        position = self.showing.position_ticks if keep_position else 0
        self.state.set_media_table(self.folder.media_table())
        self.state.set_intro(self.folder.intro_measurement())
        self._update_property_panel()
        self.refresh_notices()
        if rebuild_preview:
            self._rebuild_preview(position)
        self._update_actions()
        self._update_title()
        if reason:
            self.status.showMessage(reason)

    def _rebuild_preview(self, position):
        """Build the **text** preview: one background still, the layout's placements.

        Tim 2026-09-17 downlined the video preview (and with it the crash that lived on
        "wrap every decoded frame into a QImage and paint it"): what a member needs to
        approve is the template's *text effect*, not a replay of the delivery.  So this
        builds no session, no decoder, no segment proxy, no audio output and no mixer -
        the picture is a cached still and the clock is wall time.

        The placements still come from B's ``LayoutSurface`` through the same
        :meth:`_build_display` the export's geometry comes from: the preview exists to
        show what the delivery draws, so it is not allowed a layout of its own.
        """
        self.close_preview()
        plan = self.state.plan()
        if plan is None:
            self.showing.preview_error = self.state.plan_error and str(self.state.plan_error) or ''
            self.state_label.setText('时间线无法求解：%s' % self.showing.preview_error)
            return
        from preview.session import DEFAULT_CANVAS_HEIGHT, canvas_size_for

        from .editor_project import with_background
        from .text_preview import TextPreview
        from preview.still import StillFrames

        background = self.folder.background_slice(self.state.project)
        preview_plan = with_background(plan, self.state.project, background)
        canvas = canvas_size_for(preview_plan, DEFAULT_CANVAS_HEIGHT)
        display = self._build_display(preview_plan, canvas)
        if display is None:
            self.state_label.setText('文字预览不可用：%s' % self.showing.preview_error)
            return
        stills = StillFrames(self.folder.preview_dir, canvas[0], canvas[1])
        widget = TextPreview(display=display, plan=preview_plan, stills=stills)
        widget.background_paths = ({'layer:background': self.folder.delivery.background}
                                   if background is not None else {})
        widget.on_ready = widget.update
        stills._on_ready = widget.on_still_ready
        self.showing.session = None
        self.showing.still_frames = stills
        self.showing.preview_error = ''
        self.showing.preview_builds += 1
        self.canvas = widget
        self.canvas.set_selection(self.state.selection)
        self.canvas_layout.addWidget(self.canvas)
        self.canvas.changed.connect(self._tick)
        with QtCore.QSignalBlocker(self.progress_slider):
            self.progress_slider.setRange(0, plan.total_ticks // 720)
        self.seek_edit.setMaximum(plan.total_ticks / 720000.0)
        self.canvas.set_position(position or 0)
        self.state_label.setText('文字预览已就绪 · %s · 画布 %dx%d'
                                 % (plan.project_id, canvas[0], canvas[1]))

    def close_preview(self):
        if self.canvas is not None:
            self.canvas.stop()
            self.canvas_layout.removeWidget(self.canvas)
            self.canvas.deleteLater()
            self.canvas = None
        self.showing.session = None

    def _build_display(self, plan, canvas):
        """The layout port the canvas draws through, at the *canvas*'s own size.

        Two decisions are load-bearing here and both were handed to the editor by
        the layout's owner:

        * the styles come from ``merged_styles(plan.style_table())``, **not** from
          the project's bare overrides.  ``LayoutSurface`` has a neutral fallback of
          its own, so handing it only the overrides silently resets every role the
          project did not touch - the trap A measured (english quietly becoming 240);
        * the surface is built at the canvas size, not the plan size, because a
          placement's ``size`` and ``baseline`` are pixels.  The fractions (``x``,
          ``y``) are canvas-independent, so text lands in the same relative place at
          720p and at 4K, which is what makes the preview and the export agree.
        """
        from preview.layout import LayoutSurfaceDisplay, LayoutUnavailable
        styles = self.state.styles()
        try:
            from word_video.application.styles import style_fonts
            font_paths, font_names = style_fonts(styles)
            return LayoutSurfaceDisplay.from_plan(
                plan, width=canvas[0], height=canvas[1], styles=styles,
                fonts=font_names, font_paths=font_paths)
        except LayoutUnavailable as error:
            self.showing.preview_error = str(error)
            return None

    def _tick(self):
        """Update transport on preview events; no second polling timer while idle."""
        if self.canvas is None:
            return
        presentation = self.canvas.presentation()
        if presentation is None:
            return
        self.showing.position_ticks = presentation.position_ticks
        self.play_button.setText('暂停' if presentation.playing else '播放')
        with QtCore.QSignalBlocker(self.progress_slider):
            self.progress_slider.setValue(presentation.position_ticks // 720)
        self.position_label.setText('%.3fs · %s' % (presentation.position_seconds,
                                                    presentation.state))
        # The canvas draws "preparing" itself; the label repeats it because a member
        # who is looking at the status bar must not have to guess either.
        if presentation.preparing and self.state_label.text() != presentation.preparing:
            self.state_label.setText(presentation.preparing)

    def _update_title(self):
        if self.state is None:
            return
        mark = '*' if self.state.dirty else ''
        self.setWindowTitle('单词教学视频 — %s%s（rev %d）'
                            % (self.folder.path, mark, self.state.revision))

    def _update_actions(self):
        ready = self.state is not None
        for action in (getattr(self, 'action_save', None),
                       getattr(self, 'action_export', None)):
            if action is not None:
                action.setEnabled(ready and not self._exporting)
        if getattr(self, 'action_undo', None) is not None:
            self.action_undo.setEnabled(bool(ready and self.state.can_undo)
                                        and not self._exporting)
            self.action_redo.setEnabled(bool(ready and self.state.can_redo)
                                        and not self._exporting)

    # ------------------------------------------------------------ transport
    def toggle_play(self):
        if self.canvas is None:
            self.refresh(rebuild_preview=True)
            return
        self.canvas.toggle()
        self.play_button.setText('播放' if not self.canvas.playing else '暂停')

    def on_playhead_moved(self, ticks):
        if self.canvas is not None:
            self.canvas.seek(int(ticks))
        self.showing.position_ticks = int(ticks)

    def seek(self, ticks):
        self.on_playhead_moved(ticks)

    # --------------------------------------------------------------- edits
    def _apply_style(self, **fields):
        if not self._require_state():
            return
        role = self.role_box.currentData()
        if role is None:
            return
        outcome = self.state.set_style(role, **fields)
        self.last_outcome = outcome
        self.after_edit(rebuild_preview=outcome.changed,
                        reason=self._describe(outcome, '样式'))

    def on_apply_size(self):
        self._apply_style(size=float(self.size_edit.value()))

    def on_apply_color(self):
        import re
        color = self.color_edit.text().strip()
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            self.status.showMessage('颜色须为 #RRGGBB，例如 #FFFFFF')
            return
        self._apply_style(color=color.upper())

    def on_apply_position(self):
        self._apply_style(x=float(self.x_edit.value()), y=float(self.y_edit.value()))

    def on_clear_style(self):
        if not self._require_state():
            return
        role = self.role_box.currentData()
        if role is None:
            return
        outcome = self.state.clear_style(role)
        self.last_outcome = outcome
        self.after_edit(rebuild_preview=outcome.changed,
                        reason=self._describe(outcome, '恢复默认样式'))

    def on_mode_changed(self, index):
        if not self._require_state():
            return
        before = self.state.project.to_dict()
        mode = MODE_TEMPLATE if index == 0 else MODE_ADVANCED
        self.state.set_mode(mode)
        same = self.state.project.to_dict() == before
        self.status.showMessage('模式切换为 %s；业务数据%s'
                                % (mode, '逐字不变' if same else '被改动了（这是缺陷）'))

    def undo(self):
        if not self._require_state():
            return
        outcome = self.state.undo()
        self.after_edit(rebuild_preview=outcome.changed, reason='已撤销')

    def redo(self):
        if not self._require_state():
            return
        outcome = self.state.redo()
        self.after_edit(rebuild_preview=outcome.changed, reason='已重做')

    def reload(self):
        if not self._require_state():
            return
        try:
            self.folder = ProjectFolder.open(self.folder.path)
        except ProjectError as error:
            self.state.last_notices = (notices.notice_from_error(self.state.project, error),)
            self.refresh_notices()
            return
        self.attach(self.folder)

    def after_edit(self, *, rebuild_preview=True, reason=''):
        """Redraw everything after an edit, without rebuilding the preview if unneeded."""
        self.state.set_media_table(self.folder.media_table())
        self.state.set_intro(self.folder.intro_measurement())
        self.canvas_selection()
        self._update_property_panel()
        self.refresh_notices()
        self._update_actions()
        self._update_title()
        if rebuild_preview:
            self._rebuild_preview(self.showing.position_ticks)
        if reason:
            self.status.showMessage(reason)

    def canvas_selection(self):
        if self.canvas is not None:
            self.canvas.set_selection(self.state.selection)

    def _describe(self, outcome, verb):
        if not outcome.changed:
            return '%s没有改动' % verb
        extra = '；'.join(notice.message for notice in outcome.notices)
        return '%s完成%s' % (verb, ('：' + extra) if extra else '')

    def save(self):
        if not self._require_state():
            return None
        path = self.state.save()
        self._update_title()
        self._update_actions()
        self.status.showMessage('已保存 %s（rev %d）' % (path, self.state.revision))
        return path

    # ------------------------------------------------------------- panels
    def _update_property_panel(self):
        if self.state is None:
            return
        styles = self.state.styles()
        roles = list(styles)
        if [self.role_box.itemData(i) for i in range(self.role_box.count())] != roles:
            role = self.role_box.currentData()
            # Populating the selector is not an edit and must not recurse or rebuild.
            with QtCore.QSignalBlocker(self.role_box):
                self.role_box.clear()
                for key in roles:
                    self.role_box.addItem(notices.role_label(key), key)
                self.role_box.setCurrentIndex(roles.index(role) if role in roles else 0)
        role = self.role_box.currentData()
        style = styles.get(role) or {}
        for widget in (self.size_edit, self.size_button, self.color_edit,
                       self.color_button, self.x_edit, self.y_edit,
                       self.position_button, self.size_reset):
            widget.setEnabled(bool(style))
        self.property_labels['style'].setText(self._style_text(role))
        if style:
            self.size_edit.setValue(float(style['size']))
            self.color_edit.setText(style['color'])
            self.x_edit.setValue(float(style['x']))
            self.y_edit.setValue(float(style['y']))

    def _style_text(self, role):
        """The resolved style of a role, and whether it is the default or an override.

        Read from the *merged* table this revision renders with, so the number here
        is the number the canvas and the export use - and it says explicitly when the
        project overrides it, because "why is this one different" is the question a
        style layer raises.
        """
        styles = self.state.styles()
        style = styles.get(role)
        if not style:
            return '该角色无字形（声音）' if role in ('female', 'male', 'chinese') \
                else '无样式'
        overridden = role in (self.state.plan().style_table() if self.state.plan() else {})
        return '%s · 字号 %s（2160 高设计尺度）· %s%s' % (
            style.get('font_name') or '?', style.get('size'),
            'Bold' if style.get('bold') else 'Regular',
            '（本工程已改）' if overridden else '（默认）')

    def _fill_delivery(self):
        delivery = self.folder.delivery
        for key, edit in self.delivery_edits.items():
            edit.setText(str(getattr(delivery, key) or ''))

    def choose_delivery_file(self, name):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择文件', '', '媒体 (*.mp4 *.mov *.mkv *.wav *.mp3 *.ogg)')
        if path:
            self.delivery_edits[name].setText(path)

    def save_delivery(self):
        if not self._require_state():
            return
        values = {key: edit.text().strip() for key, edit in self.delivery_edits.items()}
        self.folder.set_delivery(**values)
        self.status.showMessage('交付设置已保存')
        self.refresh(rebuild_preview=True)

    # ------------------------------------------------------------- notices
    def refresh_notices(self, extra=()):
        """Rebuild the notice list: what the member has to decide, and what to click."""
        while self.notice_layout.count():
            item = self.notice_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if self.state is None and not extra:
            return ()
        found = list(extra)
        if self.state is not None:
            found += list(self.folder.diagnostics(self.state.project))
            found += list(self.state.document_notices())
        seen, unique = set(), []
        for notice in found:
            key = (notice.code, notice.clip_id, notice.message)
            if key not in seen:
                seen.add(key)
                unique.append(notice)
        for notice in unique:
            self.notice_layout.addWidget(self._notice_card(notice))
        if not unique:
            label = QtWidgets.QLabel('没有待处理的问题')
            label.setStyleSheet('color:#8aa')
            self.notice_layout.addWidget(label)
        self._last_notices = tuple(unique)
        return self._last_notices

    def _notice_card(self, notice):
        card = QtWidgets.QFrame()
        card.setFrameShape(QtWidgets.QFrame.StyledPanel)
        colour = {notices.SEVERITY_BLOCK: '#5b1f22',
                  notices.SEVERITY_WARN: '#5b4a1f',
                  notices.SEVERITY_INFO: '#1f3a5b'}.get(notice.severity, '#333')
        card.setStyleSheet('QFrame { background:%s; border-radius:4px; }' % colour)
        box = QtWidgets.QVBoxLayout(card)
        head = QtWidgets.QLabel('%s [%s]' % (notice.headline(), notice.code))
        head.setWordWrap(True)
        box.addWidget(head)
        if notice.hint:
            hint = QtWidgets.QLabel(notice.hint)
            hint.setWordWrap(True)
            hint.setStyleSheet('color:#dfe3ea')
            box.addWidget(hint)
        if notice.actions:
            row = QtWidgets.QHBoxLayout()
            for action in notice.actions:
                button = QtWidgets.QPushButton(action.label)
                button.clicked.connect(lambda _=False, item=action: self.run_action(item))
                row.addWidget(button)
            row.addStretch(1)
            box.addLayout(row)
        return card

    def run_action(self, action):
        """Perform one repair.  Every branch goes through the same edit path."""
        if not self._require_state():
            return
        state = self.state
        if action.kind == notices.ACTION_SELECT_CLIP:
            state.select(action.clip_id)
            self.after_edit(rebuild_preview=False, reason='已定位到 %s' % action.clip_id)
        elif action.kind == notices.ACTION_SELECT_RECORD:
            clips = state.project.clips_of(action.record_id)
            if clips:
                state.select(clips[0].id)
                self.after_edit(rebuild_preview=False)
        elif action.kind == notices.ACTION_UNDO:
            self.undo()
        elif action.kind == notices.ACTION_UNBIND:
            state.unbind_start(action.clip_id)
            self.after_edit(reason='已解绑 %s' % action.clip_id)
        elif action.kind == notices.ACTION_TRIM_TO_SOURCE:
            state.trim_to_source_length(action.clip_id)
            self.after_edit(reason='已恢复到语音长度')
        elif action.kind == notices.ACTION_TRIM_TO_TICKS:
            state.trim(action.clip_id, 'end', action.ticks, snap=False)
            self.after_edit(reason='已修剪到切口')
        elif action.kind == notices.ACTION_DO_SPLIT:
            outcome = state.split_at_ticks(action.clip_id, action.ticks)
            self.after_edit(reason=self._describe(outcome, '拆分'))
        elif action.kind == notices.ACTION_RELOAD:
            self.reload()
        elif action.kind == notices.ACTION_CHOOSE_ASSET:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, '为 %s 选择配音文件' % action.asset_id, '',
                '音频 (*.ogg *.wav *.mp3 *.m4a *.aac);;所有文件 (*)')
            if path:
                self.folder.set_asset_file(action.asset_id, path)
                self.refresh(rebuild_preview=True, reason='已更换 %s 的文件' % action.asset_id)
        elif action.kind == notices.ACTION_CHOOSE_VOICE:
            voice = self._ask_voice(action.asset_id)
            if voice:
                self.folder.set_voice(action.asset_id, voice)
                self.refresh(rebuild_preview=False, reason='已记录音色 %s' % voice)
        elif action.kind == notices.ACTION_SHOW_FONT_FOLDER:
            folder = str(Path(action.path).parent) if action.path else ''
            if folder:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))
        elif action.kind == notices.ACTION_OPEN_DELIVERY:
            self.parent_tabs().setCurrentIndex(1)
        elif action.kind == notices.ACTION_OPEN_FOLDER:
            if action.path:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(action.path))
        elif action.kind == notices.ACTION_RECHECK:
            # "重新检查" means re-measure too: the cheap file check runs on every
            # redraw, but an ffprobe pass is what this button is for.
            self.folder.media_table(refresh=True)
            self.refresh(rebuild_preview=True, reason='已重新检查与测量')

    def parent_tabs(self):
        """The side panel, so a notice can bring the delivery settings forward."""
        return self.side_tabs

    def _ask_voice(self, asset_id):
        """Pick from the voices this machine has actually used (read-only scan)."""
        choices = []
        try:
            from word_video.voices import discover
            choices = [item['speaker'] for item in discover()['voices']]
        except Exception:                               # noqa: BLE001 - a scan may fail
            choices = []
        if not choices:
            return ''
        chosen, accepted = QtWidgets.QInputDialog.getItem(
            self, '选择音色', '%s 的配音是哪个音色？' % asset_id, choices, 0, False)
        return chosen if accepted else ''

    # -------------------------------------------------------------- export
    def export(self, output=''):
        if not self._require_state():
            return None
        blockers = blocking_notices(self.folder, self.state.project)
        if blockers:
            self.refresh_notices(extra=blockers)
            self.status.showMessage('导出被拦下：先处理第一张卡片')
            return None
        if self.state.dirty:
            self.save()
        self._exporting = True
        self._update_actions()
        self.status.showMessage('导出中…（不阻塞编辑，编辑不会等整段重编码）')
        self._thread = QtCore.QThread(self)
        self._worker = ExportWorker(self.folder, self.state, output=output)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_export_progress)
        self._worker.finished.connect(self._on_export_finished)
        self._thread.start()
        return self._thread

    def _on_export_progress(self, payload):
        if isinstance(payload, dict):
            stage = payload.get('stage') or ''
            fraction = payload.get('fraction')
            self.status.showMessage('导出中… %s %s' % (
                stage, '' if fraction is None else '%.0f%%' % (float(fraction) * 100)))

    def _on_export_finished(self, outcome):
        self._exporting = False
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None
        extra = list(outcome.notices)
        if outcome.ok:
            extra.insert(0, notices.Notice(
                code='EXPORT_OK',
                message='三产物已发布：%s（%.1fs）' % (outcome.run_dir, outcome.seconds),
                severity=notices.SEVERITY_INFO,
                hint='MP4 + 五轨 SRT + 可编辑剪映草稿（新口径：按音频实际时长）',
                actions=(notices.Action(notices.ACTION_OPEN_FOLDER, '打开输出目录',
                                        path=outcome.run_dir),),
                detail=outcome.to_dict()))
            self.status.showMessage('导出完成：%s' % outcome.run_dir)
        else:
            self.status.showMessage('导出失败：见下方提示')
        self.refresh_notices(extra=extra)
        self._update_actions()
        self.last_export = outcome
        return outcome

    def last_notice_texts(self):
        return tuple(notice.headline() for notice in getattr(self, '_last_notices', ()))

    # ------------------------------------------- 旧字幕工厂入口（W10，不改旧核心）
    def media_caliber_block(self):
        """The **new** clock's real numbers for what is open, from the engine's plan.

        Read off ``RenderPlan.total_ticks`` and converted with the time core's own
        function: the dialog must be able to show both clocks' numbers without a
        second way of computing either of them.
        """
        from legacy_adapter import caliber, missing_plan_block

        if self.state is None or self.folder is None:
            return missing_plan_block('还没有打开工程')
        plan = self.state.plan()
        if plan is None:
            return missing_plan_block('当前工程的时间线无法求解：%s'
                                      % (self.showing.preview_error
                                         or self.state.plan_error or ''))
        from word_video.domain.timebase import ticks_to_milliseconds

        block = dict(caliber('media-actual'))
        block.update({'available': True, 'reason': '',
                      'timeline': str(self.folder.project_path),
                      'measured': {'total_duration_ms': ticks_to_milliseconds(plan.total_ticks),
                                   'total_ticks': plan.total_ticks, 'items': len(plan.video),
                                   'word_count': len(self.state.project.records),
                                   'source': '本工程当前计划（RenderPlan.total_ticks）'}})
        return block

    def legacy_dialog(self, **values):
        """The old-factory dialog, wired to this window's own numbers and notices."""
        known = {key: values[key] for key in
                 ('wordlist', 'output', 'start', 'end', 'batch_size', 'first_six', 'extra')
                 if key in values}
        known.setdefault('wordlist', self._last_wordlist)
        return LegacyDialog(self, media_caliber=self.media_caliber_block(),
                            report=(self.last_legacy or {}).get('report'),
                            on_outcome=self._on_legacy_finished, **known)

    def choose_legacy(self):
        """Menu action: open the entry; the dialog runs the old core on its button."""
        dialog = self.legacy_dialog()
        dialog.show()
        dialog.exec()
        return dialog.last_outcome

    def run_legacy(self, mapping=None, *, wait=True):
        """The same dialog driven without a member (support call, scripted evidence).

        It is the dialog's own ``run`` that is called, so a scripted run is evidence
        about the member's route rather than about a second implementation of it.
        """
        mapping = dict(mapping or {})
        dialog = self.legacy_dialog(**mapping)
        dialog.show()
        try:
            return dialog.run(wait=wait)
        finally:
            dialog.close()

    def _on_legacy_finished(self, outcome):
        """One notice card: what the old clock produced, or why it refused."""
        if outcome.get('ok'):
            report = outcome['report']
            counts = report['counts']
            if report['request']['wordlist']:
                self._last_wordlist = report['request']['wordlist']
            notice = notices.Notice(
                code='LEGACY_TRACKS_OK',
                message='旧口径（文字规则）已生成 %s 包 / %s 个文件：%s'
                        % (counts['package_count'], counts['file_count'], report['directory']),
                severity=notices.SEVERITY_INFO,
                hint='这五轨按文字规则计时；与“导出三产物”（按音频实际时长）不是同一口径',
                actions=(notices.Action(notices.ACTION_OPEN_FOLDER, '打开输出目录',
                                        path=report['directory']),),
                detail={'caliber': report['caliber'], 'counts': counts,
                        'media_caliber': report['media_caliber']})
            self.status.showMessage('旧入口完成：%s' % report['directory'])
        else:
            error = outcome.get('error') or {}
            notice = notices.Notice(
                code=error.get('code') or 'LEGACY_FAILED',
                message='旧入口失败：%s' % (error.get('message') or ''),
                severity=notices.SEVERITY_BLOCK,
                hint=error.get('hint') or '旧核心本身没有被改动：修好输入再点一次即可',
                detail=error.get('details') or {})
            self.status.showMessage('旧入口失败：%s' % (error.get('code') or ''))
        self.last_legacy = outcome
        self.refresh_notices(extra=(notice,))
        return notice

    # ------------------------------------------------------------- dialogs
    def choose_open(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '打开工程目录')
        if not path:
            return None
        return self.open_folder(path)

    def open_folder(self, path):
        try:
            folder = ProjectFolder.open(path)
        except (ProjectError, ValueError) as error:
            notice = notices.stale_document_notice(str(error), path=str(path))
            self.refresh_notices(extra=(notice,))
            self.status.showMessage('打不开 %s：%s' % (path, error))
            return None
        self.attach(folder)
        self.status.showMessage('已打开 %s（rev %d）' % (folder.path, folder.project.revision))
        return folder

    def choose_import(self):
        request, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择请求 JSON（词表 + 已有配音）', '', '请求 (*.json)')
        if not request:
            return None
        target = QtWidgets.QFileDialog.getExistingDirectory(self, '选择工程目录（将写入其中）')
        if not target:
            return None
        return self.import_request(request, target)

    def import_request(self, request, target):
        from .editor_import import import_request
        self.last_import = None
        try:
            folder = import_request(request, target)
        except (ValueError, CatalogError, ProjectError, OSError) as error:
            # The driven report needs the reason even when there is no project open to
            # hang a notice card on: H0 hit exactly that (``import.ok=false`` and an
            # empty ``messages`` list), and a failure with no reason is not a report.
            self.last_import = {'ok': False, 'type': type(error).__name__,
                                'reason': str(error),
                                'fix': getattr(error, 'fix', '')
                                       or '检查请求 JSON、词表路径与配音文件是否都在本机',
                                'request': str(request), 'into': str(target)}
            notice = notices.Notice(code=type(error).__name__,
                                    message='导入失败：%s' % error,
                                    severity=notices.SEVERITY_BLOCK,
                                    hint=self.last_import['fix'])
            self.refresh_notices(extra=(notice,))
            self.status.showMessage('导入失败：%s' % error)
            return None
        self.last_import = {'ok': True, 'request': str(request), 'into': str(target),
                            'project_dir': str(folder.path),
                            'records': len(folder.project.records),
                            'clips': len(folder.project.clips)}
        self.attach(folder)
        self.status.showMessage('已导入 %d 个词到 %s'
                                % (len(folder.project.records), folder.path))
        return folder

    # ----------------------------------------------------------- lifecycle
    def closeEvent(self, event):
        self.close_preview()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        super().closeEvent(event)

    def diagnostics(self):
        """The window's own state, for a test or a support call - not pixels."""
        preview = None
        if self.canvas is not None and hasattr(self.canvas, 'diagnostics'):
            preview = self.canvas.diagnostics()
        return {'folder': None if self.folder is None else str(self.folder.path),
                'revision': None if self.state is None else self.state.revision,
                'dirty': None if self.state is None else self.state.dirty,
                'selection': [] if self.state is None else list(self.state.selection),
                'mode': None if self.state is None else self.state.mode,
                'preview_builds': self.showing.preview_builds,
                'preview_error': self.showing.preview_error,
                'has_session': False,
                'preview': preview,
                'still_error': None if self.showing.still_frames is None
                               else self.showing.still_frames.error(),
                'timeline': None,
                'notices': [notice.code for notice in getattr(self, '_last_notices', ())],
                'exporting': self._exporting,
                'legacy': self.legacy_diagnostics(),
                'scale': self.devicePixelRatioF()}

    def legacy_diagnostics(self):
        """Which clock the old entry is on, and what the last run did (W10)."""
        from legacy_adapter import CALIBER_LEGACY, CALIBER_MEDIA

        outcome = self.last_legacy or {}
        report = outcome.get('report') or {}
        return {'caliber': CALIBER_LEGACY, 'export_caliber': CALIBER_MEDIA,
                'last_ok': (None if not self.last_legacy else bool(outcome.get('ok'))),
                'last_code': (None if not self.last_legacy
                              else 'LEGACY_TRACKS_OK' if outcome.get('ok')
                              else (outcome.get('error') or {}).get('code')),
                'last_output': (None if not self.last_legacy else outcome.get('output')),
                'packages': (report.get('counts') or {}).get('package_count'),
                'files': (report.get('counts') or {}).get('file_count'),
                'wordlist': self._last_wordlist,
                'media_caliber_available': bool(
                    (self.media_caliber_block() or {}).get('available'))}
