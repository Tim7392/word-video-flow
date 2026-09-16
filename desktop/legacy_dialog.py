"""The old subtitle factory as one entry in the editor - with its clock on the label.

Why this file exists
--------------------
Members still produce with the old factory.  Without an entry they carry a word list
between two programs and then compare two sets of subtitles by eye.  This dialog is
that entry: it collects exactly the parameters the old core takes, calls
:mod:`legacy_adapter` (which calls the old CLI in its own process), and reports what
came back.

Why the banner is the first widget
----------------------------------
The single most expensive mistake available here is not a crash, it is a batch whose
subtitles were timed by one rule while the video was cut by another.  So the dialog
says which clock it is on, in the words of
:data:`legacy_adapter.caliber.UI_NOTICE` - the same sentence the CLI prints - and puts
the **actual numbers** of both clocks underneath: the old rule's total for the run
that just finished, and the new rule's total for the project that is open.  A member
who cannot see the difference between "3.6 s because 评估；估价 is four characters"
and "3.3 s because that is how long the audio is" cannot be expected to keep them
apart.

Failure isolation
-----------------
The run happens in a worker object and *every* failure becomes a structured outcome
this window renders as a notice card: a missing old entry, a refused path, a bad word
list, an old core that prints nonsense.  Nothing in this file edits the project, and
nothing it can do stops the editor from editing or exporting - which is the property
the task's second acceptance item asks for in both directions.
"""
from PySide6 import QtCore, QtWidgets

from legacy_adapter import (LegacyAdapterError, UI_NOTICE, default_output_dir,
                            from_mapping, run_legacy)

#: The one line a member must not miss; the rest of the banner is the numbers.
BANNER_TITLE = '计时口径：旧口径（按文字规则推时长，与音频无关）'


def media_line(media):
    """The new caliber, in one line, or why it cannot be shown."""
    media = media or {}
    measured = media.get('measured') or {}
    if media.get('available'):
        return ('新口径实际数值（本窗口当前计划）：合计 %.3f s，%s 个片段 / %s 条词，'
                '来源＝%s' % (measured.get('total_duration_ms', 0) / 1000.0,
                             measured.get('items', '?'), measured.get('word_count', '?'),
                             measured.get('source', '当前计划')))
    reason = media.get('reason') or '未打开工程'
    return '新口径实际数值：暂不可用（%s）' % reason


def legacy_line(report):
    """The old caliber's numbers from the run that just finished."""
    if not report:
        return '旧口径实际数值：点“生成五轨 SRT”后显示（只读文字，不读任何音频）'
    measured = report['caliber']['measured']
    packages = measured['packages'] or []
    return ('旧口径实际数值（本次）：合计 %.3f s，%d 包 ×（%s 词/包），'
            'first_six=%.2f extra=%.2f，换算规则＝%s'
            % (measured['total_duration_ms'] / 1000.0, len(packages),
               '/'.join(str(row['word_count']) for row in packages),
               measured['timing']['first_six'], measured['timing']['extra'],
               report['caliber']['formula']))


def banner_text(media=None, report=None):
    """The whole banner: which clock, then both clocks' real numbers."""
    return '\n'.join((BANNER_TITLE, UI_NOTICE, media_line(media), legacy_line(report)))


class LegacyWorker(QtCore.QObject):
    """Runs one old-factory run off the UI thread; never raises into the window."""

    finished = QtCore.Signal(object)

    def __init__(self, request):
        super().__init__()
        self.request = request

    @QtCore.Slot()
    def run(self):
        try:
            report = run_legacy(self.request, action='generate')
            outcome = {'ok': True, 'report': report, 'error': None,
                       'output': report['directory'], 'seconds': report['adapter']['seconds']}
        except LegacyAdapterError as error:
            outcome = {'ok': False, 'report': None, 'error': error.to_dict(),
                       'output': str(self.request.output), 'seconds': None}
        except Exception as error:                      # noqa: BLE001 - into the card
            import traceback
            outcome = {'ok': False, 'report': None, 'output': str(self.request.output),
                       'seconds': None,
                       'error': {'ok': False, 'code': type(error).__name__,
                                 'message': '旧入口线程异常：%s' % error, 'hint': '',
                                 'details': {'traceback': traceback.format_exc()[-2000:]},
                                 'exit_code': 1}}
        self.finished.emit(outcome)


class LegacyDialog(QtWidgets.QDialog):
    """Collect the old core's parameters, show the caliber, run, report."""

    def __init__(self, parent=None, *, media_caliber=None, wordlist='', output='',
                 start=None, end=None, batch_size=None, first_six=None, extra=None,
                 report=None, on_outcome=None):
        super().__init__(parent)
        self.setWindowTitle('旧字幕工厂（文字规则计时）')
        self.setMinimumWidth(720)
        self.media_caliber = media_caliber
        #: The last run's report, so reopening the entry still shows its numbers.
        self.last_report = report
        self.last_outcome = None
        self.on_outcome = on_outcome
        self._thread = None
        self._worker = None
        self._build_ui()
        self.wordlist_edit.setText(str(wordlist or ''))
        self.output_edit.setText(str(output or ''))
        self.start_box.setValue(int(start or 0))
        self.end_box.setValue(int(end or 0))
        self.batch_box.setValue(int(batch_size or 0))
        if first_six is not None:
            self.first_six_box.setValue(float(first_six))
        if extra is not None:
            self.extra_box.setValue(float(extra))
        self._refresh_banner()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = QtWidgets.QVBoxLayout(self)
        self.banner = QtWidgets.QLabel(banner_text(self.media_caliber, None))
        self.banner.setWordWrap(True)
        self.banner.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.banner.setStyleSheet(
            'QLabel { background:#3a2f12; color:#f4e2b8; border:1px solid #7a6320;'
            ' border-radius:4px; padding:8px; }')
        outer.addWidget(self.banner)

        form = QtWidgets.QFormLayout()
        row = QtWidgets.QWidget()
        box = QtWidgets.QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        self.wordlist_edit = QtWidgets.QLineEdit()
        browse = QtWidgets.QPushButton('选择…')
        browse.clicked.connect(self.choose_wordlist)
        box.addWidget(self.wordlist_edit, 1)
        box.addWidget(browse)
        form.addRow('词表（TXT/DOCX/词表 SRT）', row)

        span = QtWidgets.QWidget()
        span_box = QtWidgets.QHBoxLayout(span)
        span_box.setContentsMargins(0, 0, 0, 0)
        span_box.addWidget(QtWidgets.QLabel('第'))
        self.start_box = self._optional_int()
        span_box.addWidget(self.start_box)
        span_box.addWidget(QtWidgets.QLabel('个到第'))
        self.end_box = self._optional_int()
        span_box.addWidget(self.end_box)
        span_box.addWidget(QtWidgets.QLabel('个词（0＝不填，旧核心按默认取值）'))
        span_box.addStretch(1)
        form.addRow('选区', span)

        timing = QtWidgets.QWidget()
        timing_box = QtWidgets.QHBoxLayout(timing)
        timing_box.setContentsMargins(0, 0, 0, 0)
        self.batch_box = self._optional_int()
        timing_box.addWidget(QtWidgets.QLabel('每包词数'))
        timing_box.addWidget(self.batch_box)
        timing_box.addWidget(QtWidgets.QLabel('（0＝单包）　前六字每字秒数'))
        self.first_six_box = QtWidgets.QDoubleSpinBox()
        self.first_six_box.setDecimals(2)
        self.first_six_box.setRange(0.05, 10.0)
        self.first_six_box.setSingleStep(0.05)
        self.first_six_box.setValue(0.4)
        timing_box.addWidget(self.first_six_box)
        timing_box.addWidget(QtWidgets.QLabel('超出每字秒数'))
        self.extra_box = QtWidgets.QDoubleSpinBox()
        self.extra_box.setDecimals(2)
        self.extra_box.setRange(0.05, 10.0)
        self.extra_box.setSingleStep(0.05)
        self.extra_box.setValue(0.2)
        timing_box.addWidget(self.extra_box)
        timing_box.addStretch(1)
        form.addRow('分包与计时（旧口径参数）', timing)

        out_row = QtWidgets.QWidget()
        out_box = QtWidgets.QHBoxLayout(out_row)
        out_box.setContentsMargins(0, 0, 0, 0)
        self.output_edit = QtWidgets.QLineEdit()
        default_button = QtWidgets.QPushButton('用默认（D 盘新目录）')
        default_button.clicked.connect(self.use_default_output)
        pick = QtWidgets.QPushButton('选择…')
        pick.clicked.connect(self.choose_output)
        out_box.addWidget(self.output_edit, 1)
        out_box.addWidget(default_button)
        out_box.addWidget(pick)
        form.addRow('输出目录', out_row)
        outer.addLayout(form)

        self.status_label = QtWidgets.QLabel('就绪：旧核心以独立子进程运行，工作目录与输出都在 D 盘新目录。')
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        self.result_label = QtWidgets.QLabel(
            '尚未生成。' if self.last_report is None else
            '上次输出目录：%s' % self.last_report['directory'])
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        outer.addWidget(self.result_label, 1)

        buttons = QtWidgets.QDialogButtonBox()
        self.run_button = buttons.addButton('生成五轨 SRT（旧口径）',
                                            QtWidgets.QDialogButtonBox.ActionRole)
        self.run_button.clicked.connect(self.run)
        close = buttons.addButton('关闭', QtWidgets.QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)
        outer.addWidget(buttons)

    def _optional_int(self):
        box = QtWidgets.QSpinBox()
        box.setRange(0, 1000000)
        box.setSpecialValueText('默认')
        return box

    # ---------------------------------------------------------- the form
    def choose_wordlist(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择词表', '', '词表 (*.txt *.docx *.srt);;所有文件 (*)')
        if path:
            self.wordlist_edit.setText(path)

    def choose_output(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '选择输出目录（D 盘新目录）')
        if path:
            self.output_edit.setText(path)

    def use_default_output(self):
        self.output_edit.setText(default_output_dir())
        self.status_label.setText('输出目录已设为 D 盘工作根下的新目录。')

    def values(self):
        """The form as the adapter's own request mapping (no second parameter set)."""
        return {'wordlist': self.wordlist_edit.text().strip(),
                'start': self.start_box.value() or None,
                'end': self.end_box.value() or None,
                'batch_size': self.batch_box.value() or None,
                'first_six': float(self.first_six_box.value()),
                'extra': float(self.extra_box.value()),
                'output': self.output_edit.text().strip()}

    def request(self):
        return from_mapping(self.values())

    # -------------------------------------------------------- the banner
    def set_media_caliber(self, block):
        """The new caliber, recomputed when the project changes."""
        self.media_caliber = block
        self._refresh_banner()

    def banner_text(self):
        return self.banner.text()

    def _refresh_banner(self):
        self.banner.setText(banner_text(self.media_caliber, self.last_report))

    # ------------------------------------------------------------- run
    def run(self, *, wait=True):
        """Start the old core and (by default) wait for it, pumping the event loop."""
        try:
            request = self.request().resolve()
        except LegacyAdapterError as error:
            return self._finish({'ok': False, 'report': None, 'error': error.to_dict(),
                                 'output': self.output_edit.text().strip(), 'seconds': None})
        self.output_edit.setText(request.output)
        self.status_label.setText('运行中：旧核心子进程……（不改旧核心，不写旧目录）')
        self.run_button.setEnabled(False)
        self.last_outcome = None
        application = QtWidgets.QApplication.instance()
        self._thread = QtCore.QThread(self)
        self._worker = LegacyWorker(request)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._finish)
        self._thread.start()
        if wait:
            while self.last_outcome is None:
                application.processEvents()
                QtCore.QThread.msleep(20)
        return self.last_outcome

    @QtCore.Slot(object)
    def _finish(self, outcome):
        self.last_outcome = outcome
        self.last_report = outcome.get('report')
        self.run_button.setEnabled(True)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None
        self._refresh_banner()
        if outcome.get('ok'):
            report = outcome['report']
            counts = report['counts']
            self.status_label.setText(
                '完成：%s 包 / %s 个文件，%.2fs（口径＝旧口径·文字规则）'
                % (counts['package_count'], counts['file_count'], outcome['seconds']))
            self.result_label.setText('输出目录：%s\n%s'
                                      % (report['directory'],
                                         '五轨 SRT 内容与毫秒时间已由适配器逐文件复核。'))
        else:
            error = outcome.get('error') or {}
            self.status_label.setText('失败：%s' % error.get('code', 'UNKNOWN'))
            self.result_label.setText('%s\n%s' % (error.get('message', ''),
                                                  error.get('hint', '')))
        if callable(self.on_outcome):
            self.on_outcome(outcome)
        return outcome
