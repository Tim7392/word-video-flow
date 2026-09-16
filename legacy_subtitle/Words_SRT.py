from subtitle_factory_core import (
    LogicCore, _read_source, _source_lines, _parse, _format_time, _gen,
    _generate_packages, _suffixes,
)

import sys
import os
import re
import math
from datetime import datetime

# ==========================================
# 0. 基础设置与防崩溃
# ==========================================
try:
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QTextEdit, QMenu,
        QStackedWidget, QFileDialog, QCheckBox, QMessageBox, QSizePolicy, QFrame
    )
    from PyQt6.QtCore import Qt, QSize, QTimer, QPointF, QRectF, QEvent
    from PyQt6.QtGui import (
        QColor, QFont, QPainter, QBrush, QPen,
        QLinearGradient, QRadialGradient, QPainterPath,
        QAction, QCursor
    )
except ImportError as exc:
    print(f"错误: 无法加载 PyQt6：{exc}")
    sys.exit(1)

try:
    import docx
except ImportError:
    docx = None

# 视觉主题 (IOS 26 Dark Mode)
THEME = {
    "bg_start": QColor(15, 17, 26),
    "bg_end": QColor(25, 28, 45),
    "glass_fill": QColor(255, 255, 255, 12),
    "glass_hover": QColor(255, 255, 255, 20),
    "border_dim": QColor(255, 255, 255, 30),
    "border_lit": QColor(255, 255, 255, 90),
    "spotlight": QColor(255, 255, 255, 45),
    "accent": "#38bdf8",
    "accent_dim": QColor(56, 189, 248, 40),
    "text": "#F8FAFC",
    "text_gray": "#94A3B8"
}


# ==========================================
# 1. 核心数学渲染引擎 (G2 Squircle)
# ==========================================
class RenderUtils:
    @staticmethod
    def get_squircle_path(rect, radius):
        """生成 iOS 风格的 G2 连续超椭圆路径 (平滑无锯齿)"""
        path = QPainterPath()
        l, t, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
        k = radius * 0.62

        path.moveTo(l + radius, t)
        path.lineTo(r - radius, t)
        path.cubicTo(QPointF(r - radius + k, t), QPointF(r, t + radius - k), QPointF(r, t + radius))
        path.lineTo(r, b - radius)
        path.cubicTo(QPointF(r, b - radius + k), QPointF(r - radius + k, b), QPointF(r - radius, b))
        path.lineTo(l + radius, b)
        path.cubicTo(QPointF(l + radius - k, b), QPointF(l, b - radius + k), QPointF(l, b - radius))
        path.lineTo(l, t + radius)
        path.cubicTo(QPointF(l, t + radius - k), QPointF(l + radius - k, t), QPointF(l + radius, t))
        path.closeSubpath()
        return path


# ==========================================
# 2. 高级玻璃组件
# ==========================================

class LiquidCard(QFrame):
    """
    [液态容器] 包含光标追踪穿透修复
    """

    def __init__(self, parent=None, radius=20):
        super().__init__(parent)
        self.radius = radius
        self.setMouseTracking(True)
        self.hover_pos = QPointF(-1000, -1000)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseMove:
            child_pos = event.position().toPoint()
            local_pos = self.mapFromGlobal(watched.mapToGlobal(child_pos))
            self.hover_pos = local_pos
            self.update()
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event):
        self.hover_pos = event.position()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_pos = QPointF(-1000, -1000)
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        path = RenderUtils.get_squircle_path(rect, self.radius)

        p.setBrush(THEME['glass_fill'])
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

        p.setClipPath(path)
        spot = QRadialGradient(self.hover_pos, self.width() * 0.6)
        spot.setColorAt(0.0, THEME['spotlight'])
        spot.setColorAt(1.0, Qt.GlobalColor.transparent)
        p.setBrush(spot);
        p.setPen(Qt.PenStyle.NoPen);
        p.drawRect(self.rect())
        p.setClipping(False)

        grad_border = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad_border.setColorAt(0.0, THEME['border_lit'])
        grad_border.setColorAt(1.0, THEME['border_dim'])
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(grad_border), 1.0))
        p.drawPath(path)


class G2Button(QPushButton):
    """带菜单功能的按钮"""

    def __init__(self, text, is_primary=False, has_menu=False):
        super().__init__(text)
        self.is_primary = is_primary
        self.has_menu = has_menu
        self.value = 0.0

        if "s" in text:
            try:
                self.value = float(text.replace("s", ""))
            except:
                pass

        self.setFixedHeight(42)
        self.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.hovered = False

        if has_menu:
            self.menu = QMenu(self)
            self.menu.setStyleSheet("""
                QMenu { background-color: #1e293b; color: white; border: 1px solid #475569; border-radius:8px; padding:5px; }
                QMenu::item { padding: 6px 20px; border-radius:4px; }
                QMenu::item:selected { background-color: #0ea5e9; }
            """)
            for v in [0.2, 0.25, 0.4, 0.5, 0.6, 0.8]:
                a = QAction(f"{v} 秒", self)
                a.triggered.connect(lambda c, val=v: self.set_val(val))
                self.menu.addAction(a)

    def set_val(self, v):
        self.value = v
        self.setText(f"{v}s")
        self.update()

    def mousePressEvent(self, e):
        if self.has_menu and e.button() == Qt.MouseButton.LeftButton:
            self.menu.exec(e.globalPosition().toPoint())
        else:
            super().mousePressEvent(e)

    def enterEvent(self, e):
        self.hovered = True; self.update(); super().enterEvent(e)

    def leaveEvent(self, e):
        self.hovered = False; self.update(); super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        path = RenderUtils.get_squircle_path(r, 12)

        if self.is_primary:
            bg = QColor(THEME['accent'])
            bg.setAlpha(200 if self.hovered else 160)
            fg = QColor("white")
            bd = QColor(255, 255, 255, 80)
        elif self.has_menu:
            bg = QColor(THEME['accent']);
            bg.setAlpha(40 if self.hovered else 20)
            fg = QColor(THEME['accent']);
            bd = QColor(THEME['accent']);
            bd.setAlpha(100)
        else:
            bg = QColor(255, 255, 255, 25 if self.hovered else 10)
            fg = QColor("white")
            bd = QColor(255, 255, 255, 40 if self.hovered else 20)

        p.setBrush(bg);
        p.setPen(Qt.PenStyle.NoPen);
        p.drawPath(path)
        p.setBrush(Qt.BrushStyle.NoBrush);
        p.setPen(QPen(bd, 1.0));
        p.drawPath(path)
        p.setPen(fg);
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())


class GhostInput(QLineEdit):
    """自绘输入框"""

    def __init__(self, holder="", align=Qt.AlignmentFlag.AlignLeft):
        super().__init__()
        self.setPlaceholderText(holder)
        self.setAlignment(align)
        self.setFixedHeight(38)
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet("QLineEdit { background:transparent; border:none; color:white; padding:0 8px; }")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        path = RenderUtils.get_squircle_path(r, 10)

        p.setBrush(QColor(0, 0, 0, 50));
        p.setPen(Qt.PenStyle.NoPen);
        p.drawPath(path)

        pen = QPen(QColor(THEME['accent']) if self.hasFocus() else QColor(255, 255, 255, 40),
                   1.0 if not self.hasFocus() else 1.5)
        p.setBrush(Qt.BrushStyle.NoBrush);
        p.setPen(pen);
        p.drawPath(path)
        super().paintEvent(e)


# ==========================================
# 3. 核心逻辑 (SRS V5.1 Core)
# ==========================================


# ==========================================
# 4. 主程序 (V5.1 Final)
# ==========================================
class MainApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subtitle Factory V5.1 (Stable)")
        self.resize(1050, 800)
        self.core = LogicCore()
        self.mem_data = []
        self.setup()

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, THEME['bg_start'])
        g.setColorAt(1, THEME['bg_end'])
        p.fillRect(self.rect(), g)

    def setup(self):
        base = QWidget()
        self.setCentralWidget(base)
        lay = QHBoxLayout(base)
        lay.setContentsMargins(25, 25, 25, 25);
        lay.setSpacing(25)

        # SIDEBAR
        side = LiquidCard(radius=24)
        side.setFixedWidth(270)
        ls = QVBoxLayout(side);
        ls.setContentsMargins(20, 40, 20, 30);
        ls.setAlignment(Qt.AlignmentFlag.AlignTop)

        l1 = QLabel("单词字幕工厂")
        l1.setStyleSheet(f"font-size:28px; font-weight:900; color:{THEME['accent']}; font-family:'Microsoft YaHei UI'")
        l2 = QLabel("智能生成工具 v5.1")
        l2.setStyleSheet("color:#64748b; font-weight:bold; font-size:12px; margin-bottom:20px")
        ls.addWidget(l1);
        ls.addWidget(l2)

        ls.addWidget(QLabel("⏳ 字幕停留时间配置", styleSheet="color:white; font-weight:bold; margin-bottom:5px"))
        pbox = QFrame();
        pbox.setStyleSheet("background:rgba(255,255,255,0.05); border-radius:8px")
        lpb = QVBoxLayout(pbox)

        lpb.addWidget(QLabel("短单词 (≤6字):", styleSheet="color:#ccc; font-size:11px"))
        self.btn_t1 = G2Button("0.4s", has_menu=True)
        lpb.addWidget(self.btn_t1)
        lpb.addSpacing(10)
        lpb.addWidget(QLabel("长单词 (>6字):", styleSheet="color:#ccc; font-size:11px"))
        self.btn_t2 = G2Button("0.2s", has_menu=True)
        lpb.addWidget(self.btn_t2)
        ls.addWidget(pbox)
        ls.addStretch()

        self.logger = QLabel("系统就绪");
        self.logger.setWordWrap(True);
        self.logger.setStyleSheet("color:#10b981; font-size:11px")
        ls.addWidget(self.logger)
        lay.addWidget(side)

        # MAIN
        main = QWidget();
        lm = QVBoxLayout(main);
        lm.setContentsMargins(0, 0, 0, 0)

        # Tab
        nav = LiquidCard(radius=18)
        nav.setFixedHeight(75);
        nav.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ln = QHBoxLayout(nav)

        self.bm1 = G2Button("📂 文件导入模式", True)
        self.bm2 = G2Button("📝 文本粘贴模式", False)
        self.bm1.clicked.connect(lambda: self.sw(0))
        self.bm2.clicked.connect(lambda: self.sw(1))
        ln.addWidget(self.bm1);
        ln.addWidget(self.bm2)
        lm.addWidget(nav)

        # Stack
        self.stack = QStackedWidget()

        # P1 File
        p1 = QWidget()
        lp1 = QVBoxLayout(p1);
        lp1.setContentsMargins(0, 15, 0, 0);
        lp1.setAlignment(Qt.AlignmentFlag.AlignTop)

        # File Card
        c_file = LiquidCard()
        c_file.setFixedHeight(120)
        lcf = QVBoxLayout(c_file)

        h_in = QHBoxLayout()
        self.inp_f = GhostInput("请选择文件...")
        bb = G2Button("浏览");
        bb.setFixedWidth(80);
        bb.clicked.connect(self.browse)
        bl = G2Button("加载", True);
        bl.setFixedWidth(80);
        bl.clicked.connect(self.loadf)
        h_in.addWidget(self.inp_f);
        h_in.addWidget(bb);
        h_in.addWidget(bl)

        lbl_hint = QLabel("💡 提示：支持 .txt 或 .docx (需每行一条数据: 单词 音标 释义)")
        lbl_hint.setStyleSheet(f"color:rgba(255,255,255,0.4); font-size:11px; margin-left:5px")

        lcf.addLayout(h_in);
        lcf.addWidget(lbl_hint)
        lp1.addWidget(c_file);
        lp1.addSpacing(15)

        # Filter Card
        c_filt = LiquidCard()
        lfl = QVBoxLayout(c_filt);
        lfl.setContentsMargins(20, 20, 20, 20)
        lfl.addWidget(QLabel("范围定位与筛选", styleSheet="color:white; font-weight:bold"))

        r1 = QHBoxLayout()
        self.in_s = GhostInput("1", Qt.AlignmentFlag.AlignCenter);
        self.in_s.setFixedWidth(50)
        # [NEW] 实时刷新监听
        self.in_s.textChanged.connect(self.upd_info)
        self.in_sw = GhostInput("始单词...");
        self.in_sw.setFixedWidth(90)
        bs = G2Button("定始");
        bs.setFixedWidth(50);
        bs.clicked.connect(lambda: self.find('s'))
        r1.addWidget(QLabel("从:", styleSheet="color:#ccc"));
        r1.addWidget(self.in_s);
        r1.addWidget(self.in_sw);
        r1.addWidget(bs)

        r2 = QHBoxLayout()
        self.in_e = GhostInput("End", Qt.AlignmentFlag.AlignCenter);
        self.in_e.setFixedWidth(50)
        # [NEW] 实时刷新监听
        self.in_e.textChanged.connect(self.upd_info)
        self.in_ew = GhostInput("末单词...");
        self.in_ew.setFixedWidth(90)
        be = G2Button("定末");
        be.setFixedWidth(50);
        be.clicked.connect(lambda: self.find('e'))
        r2.addWidget(QLabel("到:", styleSheet="color:#ccc"));
        r2.addWidget(self.in_e);
        r2.addWidget(self.in_ew);
        r2.addWidget(be)

        # [RESTORED] 批量功能回归
        rb = QHBoxLayout()
        self.chk_batch = QCheckBox("启用批量自动分包");
        self.chk_batch.setStyleSheet("color:white")
        self.inp_batch = GhostInput("50", Qt.AlignmentFlag.AlignCenter);
        self.inp_batch.setFixedWidth(50);
        self.inp_batch.setEnabled(False)
        self.chk_batch.toggled.connect(lambda c: self.inp_batch.setEnabled(c))
        rb.addWidget(self.chk_batch);
        rb.addWidget(self.inp_batch);
        rb.addWidget(QLabel("词/包", styleSheet="color:gray"));
        rb.addStretch()

        lfl.addSpacing(10);
        lfl.addLayout(r1);
        lfl.addSpacing(5);
        lfl.addLayout(r2)
        lfl.addSpacing(10);
        lfl.addLayout(rb)

        self.infot = QLabel("请先加载数据")
        self.infot.setStyleSheet(
            "background:rgba(0,0,0,0.3); color:#888; border-radius:6px; padding:8px; margin-top:10px")
        self.infot.setWordWrap(True)
        lfl.addWidget(self.infot)

        lp1.addWidget(c_filt)
        lp1.addStretch()
        self.stack.addWidget(p1)

        # P2 Text
        p2 = QWidget()
        lp2 = QVBoxLayout(p2);
        lp2.setContentsMargins(0, 15, 0, 0);
        lp2.setAlignment(Qt.AlignmentFlag.AlignTop)

        c_t = LiquidCard()
        lct = QVBoxLayout(c_t)
        lct.addWidget(QLabel("文本编辑 (支持手动修改)", styleSheet="color:white; font-weight:bold"))
        self.tx = QTextEdit()
        # EventFilter for mouse
        self.tx.setMouseTracking(True);
        self.tx.installEventFilter(c_t)
        self.tx.setStyleSheet("background:transparent; color:white; border:none; font-family:Consolas;")
        self.tx.setPlaceholderText("粘贴单词...")
        lct.addWidget(self.tx)
        bt = G2Button("清空");
        bt.clicked.connect(self.tx.clear)
        lct.addWidget(bt)
        lp2.addWidget(c_t)
        self.stack.addWidget(p2)

        lm.addWidget(self.stack)

        # Bot
        bot = LiquidCard();
        bot.setFixedHeight(80)
        lb = QHBoxLayout(bot)
        self.io = GhostInput(os.path.join(os.path.expanduser("~"), "Desktop"))
        bo = G2Button("...");
        bo.setFixedWidth(40);
        bo.clicked.connect(self.cdir)
        br = G2Button("🚀 立即生成", True);
        br.setFixedWidth(160);
        br.clicked.connect(self.run)
        lb.addWidget(QLabel("输出:", styleSheet="color:white"));
        lb.addWidget(self.io);
        lb.addWidget(bo);
        lb.addSpacing(15);
        lb.addWidget(br)
        lm.addWidget(bot)
        lay.addWidget(main)

    # --- Funcs ---
    def log(self, s):
        self.logger.setText(s)

    def sw(self, i):
        self.stack.setCurrentIndex(i)
        self.bm1.is_primary = (i == 0);
        self.bm1.update()
        self.bm2.is_primary = (i == 1);
        self.bm2.update()

    def browse(self):
        f, _ = QFileDialog.getOpenFileName(self)
        if f: self.inp_f.setText(f)

    def cdir(self):
        d = QFileDialog.getExistingDirectory(self)
        if d: self.io.setText(d)

    def loadf(self):
        path = self.inp_f.text()
        if not os.path.exists(path): return self.log("文件不存在")
        self.mem_data, e = self.core.parse(path=path)
        self.log(f"已加载 {len(self.mem_data)} 条数据")
        if e: self.log(f"含 {len(e)} 条格式错误")

        if self.mem_data:
            # 此时会触发 textChanged 从而触发 update_info
            self.in_s.setText("1")
            self.in_e.setText(str(len(self.mem_data)))

    def upd_info(self):
        # [Core Fix] 实时响应文字变化
        if not self.mem_data: return
        try:
            s_txt = self.in_s.text()
            e_txt = self.in_e.text()
            if not s_txt or not e_txt: return

            s = int(s_txt) - 1;
            e = int(e_txt)
            if s < 0: s = 0
            if e > len(self.mem_data): e = len(self.mem_data)

            if s >= e:
                self.infot.setStyleSheet(
                    "background:rgba(200,50,50,0.2); color:#ffcccc; border-radius:6px; padding:8px; margin-top:10px")
                self.infot.setText(f"❌ 起始序号 ({s + 1}) 不能大于等于 结束序号 ({e})")
                return

            t = self.mem_data[s:e]
            self.infot.setStyleSheet(
                "background:rgba(16,185,129,0.2); color:white; border-radius:6px; padding:8px; margin-top:10px")
            msg = f"✅ 已选: {len(t)} 个\n"
            msg += f"首: {t[0]['w']} {t[0]['d_c'][:10]}...\n"
            msg += f"末: {t[-1]['w']} {t[-1]['d_c'][:10]}..."
            self.infot.setText(msg)
        except:
            self.infot.setText("...等待正确序号...")

    def find(self, m):
        w = self.in_sw.text().strip().lower() if m == 's' else self.in_ew.text().strip().lower()
        if not self.mem_data or not w: return
        for i, d in enumerate(self.mem_data):
            if d['w'].lower() == w:
                if m == 's':
                    self.in_s.setText(str(i + 1))
                else:
                    self.in_e.setText(str(i + 1))
                return
        self.log(f"未找到: {w}")

    def run(self):
        o = self.io.text()
        t1, t2 = self.btn_t1.value, self.btn_t2.value
        tg = []
        is_batch = False
        bn = 50

        if self.stack.currentIndex() == 0:
            if not self.mem_data: return self.log("请加载数据")
            try:
                s = int(self.in_s.text()) - 1;
                e = int(self.in_e.text())
                tg = self.mem_data[s:e]
                is_batch = self.chk_batch.isChecked()
                if self.inp_batch.text(): bn = int(self.inp_batch.text())
            except:
                return self.log("范围错误")
        else:
            tg, _ = self.core.parse(txt=self.tx.toPlainText())

        if not tg: return self.log("有效数据为空")

        if is_batch and bn > 0:
            import math
            n = math.ceil(len(tg) / bn)
            self.log(f"批量分包: 共 {n} 包")
            for i in range(n):
                sub = tg[i * bn: (i + 1) * bn]
                self.core.gen_srt(sub, o, t1, t2, f"_Part{i + 1}")
        else:
            self.core.gen_srt(tg, o, t1, t2)

        QMessageBox.information(self, "成功", f"生成完成\n{o}")
        os.startfile(o)



# ==========================================
# V5.1.1 修复：从桌面字幕工厂.exe完整恢复的可编辑补丁
# 保留原有界面；下列函数替换对应旧实现。
# ==========================================
"""Repairs installed on the original classes before creating the window."""
import logging
import tempfile
from pathlib import Path
import shutil
import traceback

_repair_logger = logging.getLogger('subtitle_factory')
try:
    _log_dir = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'SubtitleFactory' / 'logs'
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = _log_dir / 'subtitle_factory.log'
    if _log_file.exists() and _log_file.stat().st_size > 1_000_000:
        _log_file.replace(_log_dir / 'subtitle_factory.previous.log')
    _handler = logging.FileHandler(_log_file, encoding='utf-8')
    _handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    _repair_logger.addHandler(_handler)
    _repair_logger.setLevel(logging.INFO)
except OSError:
    _log_file = None
    _repair_logger.addHandler(logging.NullHandler())

def _exception_hook(kind, value, tb):
    _repair_logger.error('Unhandled callback exception', exc_info=(kind, value, tb))
    instance = QApplication.instance()
    if instance:
        QMessageBox.critical(instance.activeWindow(), '操作未完成',
            '本次操作遇到错误，程序仍可继续使用。\n' + str(value) +
            ('\n错误记录：' + str(_log_file) if _log_file else ''))

sys.excepthook = _exception_hook





def _input_path(self):
    return self.inp_f.text().strip().strip('"')

def _fingerprint(path):
    file = Path(path).resolve()
    stat = file.stat()
    return str(file), stat.st_size, stat.st_mtime_ns

def _invalidate(self, *_):
    self.mem_data = []
    self._loaded_source = None
    self.infot.setText('文件已更改，点击加载或直接生成。')

def _loadf(self):
    self.mem_data = []
    self._loaded_source = None
    path = _input_path(self)
    data, errors = self.core.parse(path=path)
    if errors:
        self.log('加载失败，请检查输入文件')
        self.infot.setText('\n'.join(errors[:4]))
        QMessageBox.warning(self, '输入内容需要检查', '\n'.join(errors[:12]))
        return False
    if not data:
        self.log('未找到有效词条')
        self.infot.setText('文件为空，或没有可识别的英语词条。')
        return False
    try:
        self._loaded_source = _fingerprint(path)
    except OSError:
        self.log('文件已变动，请重新加载')
        return False
    self.mem_data = data
    self.in_s.setText('1')
    self.in_e.setText(str(len(data)))
    self.upd_info()
    self.log(f'已加载 {len(data)} 条数据')
    return True

def _browse(self):
    path, _ = QFileDialog.getOpenFileName(self, '选择词表', '', '词表文件 (*.txt *.docx *.srt)')
    if path:
        self.inp_f.setText(path)
        self.loadf()

def _selection(self):
    count = len(self.mem_data)
    try:
        start = int(self.in_s.text().strip() or '1')
        end = int(self.in_e.text().strip() or str(count))
    except ValueError:
        raise ValueError('起始和结束序号必须是整数。') from None
    if not 1 <= start <= end <= count:
        raise ValueError(f'范围必须在 1～{count} 内，起始序号不能大于结束序号。')
    return self.mem_data[start - 1:end]

def _upd_info(self):
    if not self.mem_data:
        self.infot.setText('请先加载数据')
        return
    try:
        selected = _selection(self)
        self.infot.setText(f'已选：{len(selected)} 个\n首：{selected[0]["w"]}\n末：{selected[-1]["w"]}')
    except ValueError as exc:
        self.infot.setText(str(exc))


_original_gen = LogicCore.gen




def _run(self):
    if getattr(self, '_generating', False):
        return
    self._generating = True
    try:
        if self.stack.currentIndex() == 0:
            try:
                fresh = self._loaded_source == _fingerprint(_input_path(self))
            except (OSError, ValueError):
                fresh = False
            if not fresh and not self.loadf():
                return
            selected = _selection(self)
        else:
            selected, errors = self.core.parse(txt=self.tx.toPlainText())
            if errors:
                raise ValueError('以下内容无法识别，请修改后重试：\n' + '\n'.join(errors[:12]))
        batch_size = None
        if self.chk_batch.isChecked():
            try:
                batch_size = int(self.inp_batch.text().strip() or '50')
            except ValueError:
                raise ValueError('每包词数必须是大于 0 的整数。') from None
            if batch_size <= 0:
                raise ValueError('每包词数必须是大于 0 的整数。')
        destination, packages, files = _generate_packages(self.core, selected, self.io.text(),
            self.btn_t1.value, self.btn_t2.value, batch_size)
        self.log(f'生成完成：{len(selected)} 词 / {packages} 包 / {files} 个文件')
        self.last_output_dir = str(destination)
        QMessageBox.information(self, '生成完成', f'已生成 {len(selected)} 个词条，共 {packages} 包、{files} 个字幕文件。\n\n保存位置：{destination}')
        try:
            os.startfile(str(destination))
        except OSError:
            _repair_logger.exception('Export succeeded but folder could not be opened')
            self.log('字幕已生成，请按保存位置手动打开文件夹')
    except Exception as exc:
        _repair_logger.exception('Generation failed')
        self.log('未完成：' + str(exc).splitlines()[0][:80])
        QMessageBox.warning(self, '生成未完成', str(exc))
    finally:
        self._generating = False

_original_init = MainApp.__init__
def _init(self):
    self._loaded_source = None
    self._generating = False
    _original_init(self)
    self.setWindowTitle('单词字幕工厂 V5.1.1 修复版')
    self.inp_f.textChanged.connect(lambda *_: _invalidate(self))
    self.inp_f.setObjectName('source_file')
    self.tx.setObjectName('source_text')
    self.io.setObjectName('output_directory')
    for button in self.findChildren(QPushButton):
        if '立即生成' in button.text():
            button.setObjectName('generate_button')
    for label in self.findChildren(QLabel):
        if label.text() == '智能生成工具 v5.1':
            label.setText('智能生成工具 v5.1.1 修复版')
        if '提示：支持' in label.text():
            label.setText('支持 TXT / DOCX / 单词词表 SRT；每个词条包含单词、音标和释义。')
    # Give the paste page the same visible batch settings as the import page.
    row = QHBoxLayout()
    self.paste_batch = QCheckBox('启用批量自动分包')
    self.paste_batch.setStyleSheet('color:white')
    self.paste_size = GhostInput('50')
    self.paste_size.setFixedWidth(65)
    self.paste_size.setEnabled(False)
    self.paste_batch.toggled.connect(self.chk_batch.setChecked)
    self.chk_batch.toggled.connect(self.paste_batch.setChecked)
    self.chk_batch.toggled.connect(self.paste_size.setEnabled)
    self.paste_size.textChanged.connect(self.inp_batch.setText)
    self.inp_batch.textChanged.connect(self.paste_size.setText)
    row.addWidget(self.paste_batch)
    row.addWidget(self.paste_size)
    row.addWidget(QLabel('词 / 包', styleSheet='color:#94a3b8'))
    row.addStretch()
    self.stack.widget(1).layout().addLayout(row)
    self.tx.setPlaceholderText('每行一个词条，例如：\napple [ˈæpəl] n. 苹果\n也可粘贴包含单词词条的 SRT 内容。')
    self.log('修复版已就绪')

LogicCore.parse = _parse
LogicCore.gen = _gen
LogicCore.gen_srt = _gen
MainApp.__init__ = _init
MainApp.browse = _browse
MainApp.loadf = _loadf
MainApp.upd_info = _upd_info
MainApp.run = _run


from subtitle_factory_ui import install_ui
install_ui(globals())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    w = MainApp()
    w.show()
    sys.exit(app.exec())
