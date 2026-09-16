"""Presentation-only layout for the existing V5.1.1 subtitle workflow.

The parser, selection, export, timing, validation and callbacks remain in
Words_SRT.py. This module only constructs their widgets and styles them.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QTextEdit, QVBoxLayout, QWidget,
)


from liquid_glass.style import STYLE


def refresh(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text, role=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty('role', role)
    widget.setWordWrap(wrap)
    return widget


class Input(QLineEdit):
    def __init__(self, holder='', align=Qt.AlignmentFlag.AlignLeft):
        super().__init__()
        self.setPlaceholderText(holder)
        self.setAlignment(align)
        self.setMinimumHeight(42)


class Button(QPushButton):
    def __init__(self, text, is_primary=False, has_menu=False):
        super().__init__(text)
        self.setMinimumHeight(42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.has_menu = has_menu
        self.value = 0.0
        self.is_primary = is_primary
        if has_menu:
            self.value = float(text.replace('s', ''))
            self.setProperty('role', 'time')
            self.menu = QMenu(self)
            self.menu.setStyleSheet(STYLE)
            for value in (0.2, 0.25, 0.4, 0.5, 0.6, 0.8):
                action = QAction(f'{value} 秒', self)
                action.triggered.connect(lambda checked, v=value: self.set_val(v))
                self.menu.addAction(action)
            self.setMenu(self.menu)

    @property
    def is_primary(self):
        return self.property('primary') is True

    @is_primary.setter
    def is_primary(self, value):
        self.setProperty('primary', bool(value))
        refresh(self)

    def set_val(self, value):
        self.value = value
        self.setText(f'{value}s')
        if hasattr(self, 'setting_label'):
            self.setAccessibleName(f'{self.setting_label}，当前 {value} 秒')
        self.update()


class StatusLabel(QLabel):
    """Only color an existing message; never rewrite the business message."""
    def __init__(self, text=''):
        super().__init__()
        self.setProperty('role', 'status')
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text):
        super().setText(text)
        tone = 'neutral'
        if any(token in text for token in ('必须', '未完成', '失败', '未找到', '不能')):
            tone = 'error'
        elif any(token in text for token in ('已选', '已加载', '生成完成')):
            tone = 'success'
        self.setProperty('tone', tone)
        refresh(self)


def card(title=None, eyebrow=None):
    frame = QFrame()
    frame.setProperty('role', 'card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    if eyebrow:
        layout.addWidget(label(eyebrow, 'eyebrow'))
    if title:
        layout.addWidget(label(title, 'section'))
    return frame, layout


def bind_button(text, name, callback, primary=False):
    button = Button(text, primary)
    button.setObjectName(name)
    button.clicked.connect(callback)
    return button


def install_ui(namespace):
    main_class = namespace['MainApp']
    original_init = main_class.__init__

    def setup(self):
        self.setObjectName('subtitle_factory')
        self.setStyleSheet(STYLE)
        base = QWidget()
        base.setObjectName('workspace')
        self.setCentralWidget(base)
        outer = QVBoxLayout(base)
        outer.setContentsMargins(28, 22, 28, 22)
        outer.setSpacing(20)

        heading = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(6)
        title = label('单词字幕工厂')
        title.setObjectName('brand')
        title_block.addWidget(title)
        subtitle = label('把词表转为五类 SRT 字幕，用于单词教学视频。')
        subtitle.setObjectName('subtitle')
        title_block.addWidget(subtitle)
        heading.addLayout(title_block)
        heading.addStretch()
        version = label('V5.1.1  /  本地生成')
        version.setObjectName('version')
        heading.addWidget(version, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading)

        scroll = QScrollArea()
        scroll.setObjectName('workflow_scroll')
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        columns = QHBoxLayout(content)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(20)

        workspace, work_layout = card('词表内容', '01  准备词条')
        workspace.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tabs = QFrame()
        tabs.setProperty('role', 'subtle')
        nav = QHBoxLayout(tabs)
        nav.setContentsMargins(5, 5, 5, 5)
        nav.setSpacing(4)
        self.bm1 = Button('文件导入', True)
        self.bm2 = Button('粘贴文本')
        for button, index in ((self.bm1, 0), (self.bm2, 1)):
            button.setProperty('role', 'mode')
            button.clicked.connect(lambda checked, i=index: self.sw(i))
            nav.addWidget(button)
        work_layout.addWidget(tabs)
        self.stack = QStackedWidget()

        file_page = QWidget()
        file_layout = QVBoxLayout(file_page)
        file_layout.setContentsMargins(0, 6, 0, 0)
        file_layout.setSpacing(12)
        file_layout.addWidget(label('词表文件', 'field'))
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        self.inp_f = Input('选择 TXT、DOCX 或词表 SRT')
        self.inp_f.setAccessibleName('词表文件路径')
        source_row.addWidget(self.inp_f, 1)
        source_row.addWidget(bind_button('浏览', 'browse_file_button', self.browse))
        source_row.addWidget(bind_button('加载', 'load_file_button', self.loadf))
        file_layout.addLayout(source_row)
        file_layout.addWidget(label('每条包含英文、音标和释义；导入 SRT 词表后会重新计时。', 'muted', True))
        file_layout.addSpacing(8)
        file_layout.addWidget(label('范围与分包', 'section'))
        file_layout.addWidget(label('按序号选择，或输入单词定位。起止词条都包含在内。', 'muted', True))

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.in_s = Input('1', Qt.AlignmentFlag.AlignCenter)
        self.in_e = Input('末项', Qt.AlignmentFlag.AlignCenter)
        self.in_sw = Input('输入起始单词')
        self.in_ew = Input('输入结束单词')
        for number in (self.in_s, self.in_e):
            number.setFixedWidth(74)
            number.textChanged.connect(self.upd_info)
        for row, field, lookup, text, mode in (
            (0, self.in_s, self.in_sw, '起始', 's'),
            (1, self.in_e, self.in_ew, '结束', 'e'),
        ):
            grid.addWidget(label(text, 'field'), row, 0)
            field.setAccessibleName(f'{text}序号')
            lookup.setAccessibleName(f'{text}单词')
            grid.addWidget(field, row, 1)
            grid.addWidget(lookup, row, 2)
            grid.addWidget(bind_button('定位', 'range_start_button' if mode == 's' else 'range_end_button',
                                      lambda checked, m=mode: self.find(m)), row, 3)
        grid.setColumnStretch(2, 1)
        file_layout.addLayout(grid)
        self.infot = StatusLabel('请先加载数据')
        self.infot.setAccessibleName('范围选择结果')
        self.infot.setMinimumHeight(64)
        file_layout.addWidget(self.infot)
        batch_row = QHBoxLayout()
        self.chk_batch = QCheckBox('自动分包')
        self.inp_batch = Input('50', Qt.AlignmentFlag.AlignCenter)
        self.inp_batch.setFixedWidth(74)
        self.inp_batch.setAccessibleName('每包词数')
        self.inp_batch.setEnabled(False)
        self.chk_batch.toggled.connect(self.inp_batch.setEnabled)
        batch_row.addWidget(self.chk_batch)
        batch_row.addWidget(self.inp_batch)
        batch_row.addWidget(label('词 / 包', 'muted'))
        batch_row.addStretch()
        file_layout.addLayout(batch_row)
        file_layout.addStretch(1)
        self.stack.addWidget(file_page)

        paste_page = QWidget()
        paste_layout = QVBoxLayout(paste_page)
        paste_layout.setContentsMargins(0, 6, 0, 0)
        paste_layout.setSpacing(12)
        paste_heading = QHBoxLayout()
        paste_heading.addWidget(label('词条文本', 'field'))
        paste_heading.addStretch()
        clear = bind_button('清空', 'clear_text_button', lambda: self.tx.clear())
        clear.setMinimumHeight(32)
        paste_heading.addWidget(clear)
        paste_layout.addLayout(paste_heading)
        self.tx = QTextEdit()
        from liquid_glass.fonts import entry_font_family
        self.tx.setStyleSheet(f"font-family: '{entry_font_family()}'; font-size: 11pt;")
        self.tx.setMinimumHeight(240)
        self.tx.setAccessibleName('词条文本')
        paste_layout.addWidget(self.tx, 1)
        paste_layout.addWidget(label('一行一个词条，也支持粘贴词表 SRT。', 'muted'))
        # The existing repair initializer adds its synchronized batch controls here.
        self.stack.addWidget(paste_page)
        work_layout.addWidget(self.stack, 1)
        columns.addWidget(workspace, 1)

        side = QWidget()
        side.setFixedWidth(278)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(16)
        timing, timing_layout = card('释义停留时间', '02  调整节奏')
        timing_layout.setSpacing(9)
        timing_layout.addWidget(label('按中文释义的汉字数计算，每词另有固定 2 秒英文阶段。', 'muted', True))
        timing_layout.addWidget(label('前 6 个汉字 · 每字', 'field'))
        self.btn_t1 = Button('0.4s', has_menu=True)
        self.btn_t1.setting_label = '前六个汉字的每字停留时间'
        self.btn_t1.set_val(self.btn_t1.value)
        timing_layout.addWidget(self.btn_t1)
        timing_layout.addWidget(label('超过 6 字的部分 · 每字', 'field'))
        self.btn_t2 = Button('0.2s', has_menu=True)
        self.btn_t2.setting_label = '超出六字部分的每字停留时间'
        self.btn_t2.set_val(self.btn_t2.value)
        timing_layout.addWidget(self.btn_t2)
        timing_layout.addWidget(label('释义至少停留 0.5 秒。', 'muted'))
        timing.setToolTip('无中文汉字时，沿用原有英文长度估算规则。时长选项和计算方式均保持不变。')
        side_layout.addWidget(timing)

        outputs, output_layout = card('每包输出 5 类字幕')
        tracks = (
            ('01', '英文重复', '两段，每段 1 秒'),
            ('02', '英文单次', '持续整个词条'),
            ('03', '音标', '第 1 秒后显示'),
            ('04', '中文 · 带词性', '第 2 秒后显示'),
            ('05', '中文 · 无词性', '第 2 秒后显示'),
        )
        for number, title, hint in tracks:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(label(number, 'track-number'))
            row.addWidget(label(title))
            row.addStretch()
            output_layout.addLayout(row)
            # Timing details are available without making the list visually dense.
            row.itemAt(1).widget().setToolTip(hint)
        output_layout.addSpacing(3)
        output_layout.addWidget(label('每包独立从 00:00 开始。', 'muted'))
        side_layout.addWidget(outputs)
        side_layout.addStretch(1)
        columns.addWidget(side)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        footer, footer_layout = card()
        footer_layout.setContentsMargins(18, 14, 18, 14)
        footer_layout.setSpacing(10)
        footer_heading = QHBoxLayout()
        footer_heading.addWidget(label('03  保存字幕', 'eyebrow'))
        footer_heading.addStretch()
        footer_heading.addWidget(label('每包 5 份 SRT，同名结果另建文件夹', 'muted'))
        footer_layout.addLayout(footer_heading)
        save_row = QHBoxLayout()
        save_row.setSpacing(10)
        self.io = Input('请选择输出文件夹')
        self.io.setAccessibleName('输出文件夹')
        save_row.addWidget(self.io, 1)
        save_row.addWidget(bind_button('选择文件夹', 'browse_output_button', self.cdir))
        generate = bind_button('立即生成', 'generate_button', self.run, True)
        generate.setMinimumWidth(152)
        save_row.addWidget(generate)
        footer_layout.addLayout(save_row)
        self.logger = StatusLabel('系统就绪')
        self.logger.setObjectName('operation_status')
        self.logger.setAccessibleName('操作状态')
        footer_layout.addWidget(self.logger)
        outer.addWidget(footer)

    def initialize(self):
        original_init(self)
        self.setWindowTitle('单词字幕工厂 V5.1.1')
        self.setMinimumSize(980, 760)
        self.resize(1200, 900)
        self.paste_batch.setText('自动分包')
        self.paste_batch.setStyleSheet('')
        self.paste_size.setStyleSheet('')
        self.paste_size.setFixedWidth(74)
        self.paste_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.paste_size.setAccessibleName('文本模式每包词数')
        self.inp_f.setToolTip('选择文件会自动加载；手动修改路径后可点击加载或直接生成。')
        self.in_sw.setToolTip('沿用原有定位规则：从第一条起查找首个相同单词。')
        self.in_ew.setToolTip('沿用原有定位规则：从第一条起查找首个相同单词。')
        # Explicit field order for the existing file-mode workflow.
        order = (self.bm1, self.bm2, self.inp_f,
                 self.findChild(QPushButton, 'browse_file_button'),
                 self.findChild(QPushButton, 'load_file_button'), self.in_s, self.in_sw,
                 self.findChild(QPushButton, 'range_start_button'), self.in_e, self.in_ew,
                 self.findChild(QPushButton, 'range_end_button'), self.chk_batch, self.inp_batch,
                 self.btn_t1, self.btn_t2, self.io,
                 self.findChild(QPushButton, 'browse_output_button'),
                 self.findChild(QPushButton, 'generate_button'))
        for first, second in zip(order, order[1:]):
            QWidget.setTabOrder(first, second)
        from liquid_glass import attach_glass
        attach_glass(self)
        import os
        if os.environ.get('SUBTITLE_FACTORY_CAPTURE'):
            from liquid_glass.diagnostics import schedule_capture
            schedule_capture(self, os.environ['SUBTITLE_FACTORY_CAPTURE'])

    namespace['GhostInput'] = Input
    main_class.setup = setup
    main_class.__init__ = initialize
