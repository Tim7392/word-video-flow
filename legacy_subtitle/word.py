import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn


def generate_srs_word():
    doc = Document()

    # --- 设置全局中文字体支持 ---
    style = doc.styles['Normal']
    style.font.name = 'Microsoft YaHei'
    style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    style.font.size = Pt(10.5)  # 五号字

    # ================= 辅助函数 =================
    def add_heading(text, level):
        h = doc.add_heading(text, level=level)
        h.style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        if level == 0:  # 标题
            h.alignment = WD_ALIGN_PARAGRAPH.CENTER
            h.style.font.size = Pt(22)
            h.style.font.bold = True
            h.style.font.color.rgb = RGBColor(0, 0, 0)
        elif level == 1:
            h.style.font.size = Pt(16)
            h.style.font.color.rgb = RGBColor(14, 165, 233)  # 类似UI的天空蓝
        elif level == 2:
            h.style.font.size = Pt(14)
            h.style.font.color.rgb = RGBColor(40, 40, 40)

    def add_para(text, bold=False):
        p = doc.add_paragraph()
        run = p.add_run(text)
        if bold:
            run.bold = True
        p.style.font.name = 'Microsoft YaHei'

    def add_bullet(text):
        p = doc.add_paragraph(text, style='List Bullet')
        p.style.font.name = 'Microsoft YaHei'

    def add_list_num(text):
        p = doc.add_paragraph(text, style='List Number')
        p.style.font.name = 'Microsoft YaHei'

    # ================= 文档内容构建 =================

    # --- 封面 ---
    add_heading("软件规格说明书 (SRS)", 0)
    add_heading("项目：单词字幕工厂 (V5.1 Ultimate)", 0)
    doc.add_paragraph("\n" * 2)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run("版本号: V5.1 CN (Final Stable)\n").bold = True
    p.add_run("构建架构: Python 3.9+ / PyQt6 (纯软渲染)\n")
    p.add_run("最后更新: 2023-10-28")
    doc.add_page_break()

    # --- 1. 引言 ---
    add_heading("1. 引言 (Introduction)", 1)

    add_heading("1.1 编写目的", 2)
    add_para(
        "本文档旨在详细定义《单词字幕工厂 V5.1》的功能架构、UI设计规范及核心算法逻辑。本软件是为了解决传统字幕工具操作繁琐、界面老化的问题，提供一套“所见即所得”的工业级生产工具。")

    add_heading("1.2 核心差异化", 2)
    add_bullet("所见即所得 (WYSIWYG)：范围筛选参数修改时，触发毫秒级实时预览更新。")
    add_bullet(
        "绝对稳定 (Stability First)：弃用易崩溃的系统级玻璃特效 API，采用底层 QPainter 纯代码软渲染技术，完美兼容各类集成显卡。")
    add_bullet("液态交互 (Liquid UX)：全界面实现 G2 连续曲率与穿透式光标追踪引擎。")

    # --- 2. 界面与交互规范 ---
    add_heading("2. 界面与交互规范 (UI/UX Specification)", 1)

    add_heading("2.1 视觉主题：Deep Ocean Ether", 2)
    add_para("为了提供沉浸式的创作体验，界面采用深海极光风格：")
    add_bullet("底色：#0F111A 至 #191C2D 的深空线性渐变。")
    add_bullet("材质：模拟高通透物理玻璃，拥有 12% 不透明度的白色填充与物理边缘色散。")
    add_bullet("配色：天空蓝 (#38bdf8) 作为强调色，亮青色作为悬停反馈。")

    add_heading("2.2 形态学标准：G2 Squircle", 2)
    add_para("软件内所有矩形控件（窗口、按钮、输入框）禁止使用传统的 Standard Rounded Rect。")
    add_para("必须使用数学计算的超椭圆 (Squircle)，曲率修正系数 k = Radius * 0.62，以保证平滑的视觉流动感。")

    add_heading("2.3 光效交互技术", 2)
    add_para("系统通过 EventFilter 实现了复杂的鼠标事件穿透机制：")
    add_list_num("当鼠标在界面移动时，无论是否被上层控件（如输入框）遮挡，底层玻璃卡片均能捕获坐标。")
    add_list_num("卡片内部实时渲染一个跟随鼠标的径向渐变光斑 (Spotlight)，模拟流体跟随效果。")

    # --- 3. 功能需求 ---
    add_heading("3. 功能需求 (Functional Requirements)", 1)

    add_heading("3.1 智能输入与清洗", 2)
    add_bullet("文件模式：支持 .txt (自动识别编码) 和 .docx 文档。提供格式动态提示。")
    add_bullet("文本模式：支持剪贴板文本的大容量粘贴与即时解析。")
    add_bullet("核心清洗算法：")
    p = doc.add_paragraph(
        "    - 自动剔除行首数字索引 (如 1. 2.) 和特殊符号。\n    - 自动识别音标边界 ([] 或 //)。\n    - 自动移除释义中的词性标签 (n. v. adj.) 以生成纯净中文。")

    add_heading("3.2 实时筛选与定位", 2)
    add_para("重构后的筛选模块具备实时响应能力：", bold=True)
    add_bullet("序号范围：当输入起始/结束序号时，预览区瞬间刷新，无需点击按钮。")
    add_bullet("双重定位：输入单词点击“查找”，系统遍历内存锁定Index并回填至序号框。")
    add_bullet("错误反馈：若 起始序号 >= 结束序号，预览区显示红色警告信息。")

    add_heading("3.3 参数配置（人机工程优化）", 2)
    add_bullet("时间参数：移除 SpinBox，改用弹出式菜单按钮 (MenuButton)。")
    add_bullet("预设档位：包含 0.2s, 0.4s (Default), 0.5s, 0.6s, 0.8s 等常用阅读速率。")

    add_heading("3.4 批量与输出", 2)
    add_bullet("批量分包：勾选后可设定每包单词数（默认50），文件名自动添加 _PartX 后缀。")
    add_bullet("五轨输出：每次生成操作将同时产出 5 份不同用途的 SRT 文件（重复、单次、音标、中文全、中文简）。")

    # --- 4. 核心算法 ---
    add_heading("4. 核心算法逻辑 (Core Algorithm)", 1)

    add_heading("4.1 双场景动态计费公式", 2)
    add_para("系统根据中文字符量 (N) 动态计算显示时长 (Duration)。")
    add_para("定义变量：T1 = 短句基准费率 (0.4s), T2 = 长句边际费率 (0.2s)。")

    # 模拟数学公式显示
    doc.add_paragraph("计算逻辑：").style = 'List Bullet'
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.5)
    p.add_run("若 N <= 6： Duration = N × T1").italic = True

    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.5)
    p.add_run("若 N > 6：  Duration = (6 × T1) + ((N - 6) × T2)").italic = True

    add_para("底线约束：Duration >= 0.5秒", bold=True)

    add_heading("4.2 时间轴拓扑", 2)
    add_para("单个单词块的时间轴结构如下 (设当前时刻为 T)：")
    table = doc.add_table(rows=4, cols=2)
    table.style = 'Light Grid Accent 1'

    table.cell(0, 0).text = "时间段"
    table.cell(0, 1).text = "内容与行为"

    table.cell(1, 0).text = "T + 0.0s ~ 1.0s"
    table.cell(1, 1).text = "第一遍英文朗读"

    table.cell(2, 0).text = "T + 1.0s ~ 2.0s"
    table.cell(2, 1).text = "第二遍英文朗读 / 停顿缓冲"

    table.cell(3, 0).text = "T + 2.0s ~ 结束"
    table.cell(3, 1).text = "中文释义与音标显示 (持续时长由算法决定)"

    # --- 5. 性能与兼容性 ---
    add_heading("5. 性能与兼容性", 1)
    add_bullet("DPI 适配：开启 AA_EnableHighDpiScaling，在高分屏 (2K/4K) 下文字锐利，无锯齿。")
    add_bullet("图形开销：由于移除 Shader 依赖，软件在无独立显卡的办公本上 CPU 占用率 < 3%。")
    add_bullet("文件兼容：强制使用 UTF-8 with BOM 编码写入文件，确保在 Premiere/Final Cut/PotPlayer 中均不乱码。")

    # --- 6. 交付清单 ---
    add_heading("6. 交付清单", 1)
    add_list_num("源代码文件：main_v5.1_final.py")
    add_list_num("配置文件：config.json (自动生成)")
    add_list_num("应用程序图标：DeepOceanIcon.ico")

    # --- 保存 ---
    file_name = 'SRS_Word_SubtitleFactory_v5.1.docx'
    doc.save(file_name)
    print(f"成功生成文档: {file_name}")
    print(f"请在当前目录下查看文件。")


if __name__ == "__main__":
    generate_srs_word()