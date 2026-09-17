"""文字预览窗口的行为契约：EditorState 命令、菜单、通知与交付设置。

时间线拖动/裁剪/拆分交互已下线。保留对象层断言，不用截图替代排版与命令结果。
Qt 使用 offscreen；这不是成员机的观感或 DPI 验收。
"""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtWidgets, QtTest

from desktop import editor_notices as notices
from desktop.editor_model import MODE_ADVANCED
from desktop.editor_window import EditorWindow
from test_editor_support import solved_snapshot, tiny_folder
from test_preview_support import scratch

WINDOW = (1280, 820)


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def area():
    with scratch('editor-ui-') as folder:
        yield folder


@pytest.fixture
def window(app, area):
    editor = EditorWindow()
    editor.attach(tiny_folder(area / 'project'))
    editor.resize(*WINDOW)
    editor.show()
    app.processEvents()
    yield editor
    editor.close()
    app.processEvents()


def settle(app, times=3):
    for _ in range(times):
        app.processEvents()


def test_editing_the_font_size_reaches_the_style_table_and_the_canvas(window, app):
    """SetStyle 的真实结果：表、文档、属性显示与画布同时变化。"""
    from word_video.application.styles import merged_styles
    editor = window
    role = 'english'
    editor.role_box.setCurrentIndex(editor.role_box.findData(role))
    settle(app)
    before = merged_styles(editor.state.plan().style_table())[role]['size']
    document_before = editor.state.project.to_dict()
    outcome = editor.state.set_style(role, size=180.0)
    assert outcome.changed
    editor.after_edit(rebuild_preview=outcome.changed)
    settle(app)
    assert editor.state.revision == 1
    after = merged_styles(editor.state.plan().style_table())[role]['size']
    assert after == 180.0
    assert after != before
    assert editor.state.project.to_dict()['clips'] == document_before['clips']
    assert editor.state.project.to_dict() != document_before
    assert '本工程已改' in editor.property_labels['style'].text()
    display = editor.canvas.display
    assert display is not None
    size = max(p.size for p in display.report.placements if p.role == role)
    assert size == pytest.approx(180.0 * (display.report.height / 2160.0), rel=0.02)
    outcome = editor.state.clear_style(role)
    assert outcome.changed
    editor.after_edit(rebuild_preview=outcome.changed)
    settle(app)
    assert merged_styles(editor.state.plan().style_table())[role]['size'] == before
    assert '（默认）' in editor.property_labels['style'].text()


def test_the_canvas_draws_the_layout_the_delivery_draws(window, app):
    """逐字段比较文字画布与导出端 layout_for，默认样式及覆盖样式都必须一致。"""
    from word_video.application.styles import style_fonts
    from word_video.exporters.ass import layout_for
    editor = window
    canvas = editor.canvas
    assert canvas.display is not None, 'the canvas has no layout port attached'
    start, end = editor.state.clip_range('w1.english')
    midpoint = (start + end) // 2
    editor.seek(midpoint)
    for fields in ({}, {'size': 180.0, 'x': 0.42, 'y': 0.61, 'color': '#123ABC'}):
        if fields:
            outcome = editor.state.set_style('english', **fields)
            assert outcome.changed
            editor.after_edit(rebuild_preview=outcome.changed)
        canvas = editor.canvas
        styles = editor.state.styles()
        paths, names = style_fonts(styles)
        report = canvas.display.report
        delivery = layout_for(canvas.plan, styles, names, paths,
                              width=report.width, height=report.height).layout()
        expected = {p.clip_id: p for p in delivery.placements
                    if p.start_ticks <= midpoint < p.end_ticks}
        actual = {p.clip_id: p for p in canvas.presentation().placements}
        assert actual.keys() == expected.keys()
        assert actual
        for clip_id, placed in actual.items():
            exported = expected[clip_id]
            for field in ('role', 'anchor_x', 'anchor_y', 'size', 'font_name',
                          'font_path', 'color', 'bold', 'lines'):
                assert getattr(placed, field) == getattr(exported, field), (clip_id, field)
    due = canvas.presentation().placements
    assert due, 'no placement is due mid-word'
    assert 'english' in {placed.role for placed in due}
    for placed in due:
        assert placed.lines, placed.clip_id
        for line in placed.lines:
            assert 0.0 <= line.x <= 1.0 and 0.0 <= line.y <= 1.0
    english = next(placed for placed in due if placed.role == 'english')
    assert english.size == pytest.approx(editor.state.styles()['english']['size']
                                        * (canvas.display.report.height / 2160.0), rel=0.02)
    over = canvas.display.overflowing()
    assert over == (), [placed.clip_id for placed in over]


def test_the_notice_panel_shows_a_button_that_locates_the_problem(window, app):
    editor = window
    male = editor.state.project.clip('w1.male').start.ticks
    chinese = editor.state.project.clip('w1.chinese').start.ticks
    editor.state.select('w1.male')
    editor.state.move_selection(chinese - male - 12000)
    editor.refresh(rebuild_preview=False)
    settle(app)
    cards = [editor.notice_layout.itemAt(index).widget()
             for index in range(editor.notice_layout.count())]
    buttons = [button for card in cards if card is not None
               for button in card.findChildren(QtWidgets.QPushButton)]
    assert buttons, 'no repair button was rendered'
    assert any(notice.code == 'TEACHING_OVERLAP' for notice in editor._last_notices)
    overlap_card = next(card for card in cards if card is not None
                        and '重叠' in card.findChildren(QtWidgets.QLabel)[0].text()
                        or card is not None
                        and 'overlap' in card.findChildren(QtWidgets.QLabel)[0].text())
    overlap_buttons = overlap_card.findChildren(QtWidgets.QPushButton)
    assert len(overlap_buttons) == 2, 'both clips of the pair must be offered'
    QtTest.QTest.mouseClick(overlap_buttons[0], QtCore.Qt.LeftButton)
    settle(app)
    assert editor.state.selection in (('w1.male',), ('w1.chinese',))
    assert editor.diagnostics()['notices']


def test_a_recheck_button_re_measures_and_the_notice_goes_away(window, app, area):
    from pathlib import Path
    from test_preview_support import write_tone
    editor = window
    Path(editor.folder.asset_path('w1:male')).unlink()
    editor.refresh(rebuild_preview=True)
    settle(app)
    assert any(notice.code == 'ASSET_FILE_MISSING' for notice in editor._last_notices)
    editor.folder.set_asset_file('w1:male', str(write_tone(area / 'back.wav', 1.0, 500.0)))
    editor.run_action(notices.Action(notices.ACTION_RECHECK, '重新检查'))
    settle(app)
    assert not [notice for notice in editor._last_notices if notice.code == 'ASSET_FILE_MISSING']
    assert editor.showing.preview_error == ''


def test_undo_and_redo_from_the_menu_restore_the_solved_ranges(window, app):
    editor = window
    before = solved_snapshot(editor.state)
    editor.state.select('w1.male')
    outcome = editor.state.move_selection(216000, snap=False)
    assert outcome.changed
    editor.after_edit(rebuild_preview=outcome.changed)
    changed = solved_snapshot(editor.state)
    assert changed != before
    editor.action_undo.trigger()
    settle(app)
    assert solved_snapshot(editor.state) == before
    editor.action_redo.trigger()
    settle(app)
    assert solved_snapshot(editor.state) == changed
    assert editor.state.revision == 3


def test_the_mode_box_switches_three_times_without_changing_the_document(window, app):
    editor = window
    editor.state.select('w1.male')
    outcome = editor.state.move_selection(216000, snap=False)
    assert outcome.changed
    editor.after_edit(rebuild_preview=outcome.changed)
    before = editor.state.project.to_dict()
    for index in (1, 0, 1, 0):
        editor.mode_box.setCurrentIndex(index)
        settle(app)
        assert editor.state.project.to_dict() == before
    assert editor.state.mode in (MODE_ADVANCED, 'template')


def test_switching_mode_does_not_rebuild_the_preview_or_lose_the_selection(window, app):
    editor = window
    editor.state.select('w1.male')
    editor.after_edit(rebuild_preview=False)
    settle(app)
    builds = editor.showing.preview_builds
    selection = editor.state.selection
    editor.mode_box.setCurrentIndex(1)
    settle(app)
    assert editor.state.selection == selection
    assert editor.showing.preview_builds == builds


def test_each_committed_edit_rebuilds_exactly_one_preview_and_leaves_none(window, app):
    editor = window
    assert editor.showing.preview_builds == 1
    assert editor.canvas is not None
    assert editor.showing.session is None
    editor.state.select('w1.male')
    retired = []
    for index in range(3):
        retired.append(editor.canvas)
        outcome = editor.state.move_selection(144000, snap=False)
        assert outcome.changed
        editor.after_edit(rebuild_preview=outcome.changed)
        assert editor.showing.preview_builds == index + 2
        assert editor.canvas is not retired[-1]
        assert not retired[-1]._timer.isActive()
        assert editor.canvas_layout.count() == 1
    assert editor.showing.preview_builds == 4
    assert editor.canvas is not None
    editor.close()
    settle(app)
    assert editor.canvas is None, 'closing the window must release the preview'
    assert editor.canvas_layout.count() == 0
    assert editor.showing.session is None


def test_a_selection_click_does_not_rebuild_the_preview(window, app):
    """历史名称保留；选择命令不是文档编辑，不应重建预览。"""
    editor = window
    builds = editor.showing.preview_builds
    original = editor.canvas
    before = editor.state.project.to_dict()
    editor.state.select('w1.male')
    editor.after_edit(rebuild_preview=False)
    settle(app)
    assert editor.state.selection == ('w1.male',)
    assert editor.canvas.selection == ('w1.male',)
    assert editor.state.project.to_dict() == before
    assert editor.canvas is original
    assert editor.showing.preview_builds == builds


def test_export_refuses_a_project_with_a_missing_file_without_starting_a_thread(window, app):
    from pathlib import Path
    editor = window
    Path(editor.folder.asset_path('w1:chinese')).unlink()
    editor.refresh(rebuild_preview=True)
    settle(app)
    result = editor.export()
    settle(app)
    assert result is None
    assert editor.diagnostics()['exporting'] is False
    assert editor._thread is None
    assert any(notice.code == 'ASSET_FILE_MISSING' for notice in editor._last_notices)


def test_the_delivery_panel_writes_what_the_exporter_reads(window, app, area):
    from test_preview_support import write_test_video
    from desktop.editor_project import Delivery
    editor = window
    background = write_test_video(area / 'bg.mp4', 1.0, 30, '160x120')
    editor.delivery_edits['background'].setText(str(background))
    editor.save_delivery()
    settle(app)
    assert editor.folder.delivery.background == str(background)
    assert Delivery.load(editor.folder.path).background == str(background)
    assert editor.folder.background_slice(editor.state.project) is not None
