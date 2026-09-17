"""角色样式面板：真实命令、真实 LayoutSurface placements，无时间线依赖。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6 import QtCore, QtWidgets
import pytest

from desktop import editor_notices as notices
from desktop.editor_window import EditorWindow
from test_editor_support import tiny_folder


@pytest.fixture(scope='module')
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    editor = EditorWindow()
    editor.attach(tiny_folder(tmp_path / 'project'))
    start, end = editor.state.clip_range('w1.english')
    editor.seek((start + end) // 2)
    yield editor
    editor.state.mark_saved()
    editor.close()
    app.processEvents()


def choose(window, role):
    index = window.role_box.findData(role)
    assert index >= 0
    window.role_box.setCurrentIndex(index)


def assert_placement(window, role, style):
    placed = [p for p in window.canvas.presentation().placements if p.role == role]
    assert [p.clip_id for p in placed] == ['w1.' + role]
    p = placed[0]
    assert p.role == role
    assert p.anchor_x == pytest.approx(style['x'])
    assert p.anchor_y == pytest.approx(style['y'])
    assert p.size == round(style['size'] * window.canvas.display.report.height / 2160)
    assert p.color == style['color']


@pytest.mark.parametrize('field', ['size', 'color', 'position'])
def test_style_edit_reaches_table_and_each_placement_field_once(window, app, field):
    choose(window, 'english')
    assert window.state.selection == ()
    before = window.state.styles()
    expected = dict(before['english'])
    clips = window.state.project.to_dict()['clips']
    builds = window.showing.preview_builds
    position = window.canvas.position_ticks
    if field == 'size':
        window.size_edit.setValue(180)
        expected['size'] = 180.0
        window.size_button.click()
    elif field == 'color':
        window.color_edit.setText('#123ABC')
        expected['color'] = '#123ABC'
        window.color_button.click()
    else:
        window.x_edit.setValue(0.42)
        window.y_edit.setValue(0.61)
        expected.update(x=0.42, y=0.61)
        window.position_button.click()
    app.processEvents()
    assert window.state.revision == 1
    assert window.showing.preview_builds == builds + 1
    assert window.canvas.position_ticks == position
    assert window.state.styles()['english'] == expected
    assert window.state.project.to_dict()['clips'] == clips
    assert window.state.plan().style_table()['english'] == {
        key: expected[key] for key in ({'position': ('x', 'y')}.get(field, (field,)))}
    for role in before:
        if role != 'english':
            assert window.state.styles()[role] == before[role]
    assert_placement(window, 'english', expected)
    assert window.size_edit.value() == expected['size']
    assert window.color_edit.text() == expected['color']
    assert window.x_edit.value() == expected['x']
    assert window.y_edit.value() == expected['y']


def test_role_selector_uses_existing_labels_without_rebuild_or_clip_selection(window):
    original = window.canvas
    builds = window.showing.preview_builds
    styles = window.state.styles()
    assert [window.role_box.itemData(i) for i in range(window.role_box.count())] == list(styles)
    window.state.select('w1.male')  # even a stale clip selection cannot set the target
    for role, style in styles.items():
        choose(window, role)
        assert window.role_box.currentText() == notices.role_label(role)
        assert window.size_edit.value() == style['size']
        assert window.color_edit.text() == style['color']
        assert window.x_edit.value() == style['x']
        assert window.y_edit.value() == style['y']
        assert window.canvas is original
        assert window.showing.preview_builds == builds
        assert window.state.revision == 0
    choose(window, 'meaning')
    start, end = window.state.clip_range('w1.meaning')
    window.seek((start + end) // 2)
    window.size_edit.setValue(180)
    window.size_button.click()
    assert window.state.plan().style_table() == {'meaning': {'size': 180.0}}
    assert_placement(window, 'meaning', window.state.styles()['meaning'])


def test_reset_undo_redo_restore_all_style_fields_and_preserve_role(window):
    choose(window, 'english')
    default = dict(window.state.styles()['english'])
    for edit, value, button in ((window.size_edit, 180, window.size_button),
                                (window.x_edit, 0.42, window.position_button)):
        edit.setValue(value)
        button.click()
    window.color_edit.setText('#123ABC')
    window.color_button.click()
    changed = dict(window.state.styles()['english'])
    for action, expected in ((window.size_reset.click, default),
                             (window.action_undo.trigger, changed),
                             (window.action_redo.trigger, default)):
        builds = window.showing.preview_builds
        action()
        assert window.showing.preview_builds == builds + 1
        assert window.role_box.currentData() == 'english'
        assert window.state.styles()['english'] == expected
        assert_placement(window, 'english', expected)
    assert window.state.plan().style_table() == {}
    assert window.size_edit.value() == default['size']
    assert window.color_edit.text() == default['color']
    assert window.x_edit.value() == default['x']
    assert window.y_edit.value() == default['y']


def test_invalid_color_does_not_edit_or_rebuild(window):
    choose(window, 'english')
    before = window.state.project.to_dict()
    builds = window.showing.preview_builds
    window.color_edit.setText('red')
    window.color_button.click()
    assert window.state.project.to_dict() == before
    assert window.showing.preview_builds == builds
    assert '#RRGGBB' in window.status.currentMessage()


def test_no_timeline_controls_and_transport_mode_save_remain(window):
    for name in ('start_edit', 'end_edit', 'apply_button', 'speed_edit', 'speed_button',
                 'bind_button', 'unbind_button', 'restore_button', 'split_button'):
        assert not hasattr(window, name), name
    assert window.timeline is None
    before = window.state.project.to_dict()
    builds = window.showing.preview_builds
    for index in (1, 0):
        window.mode_box.setCurrentIndex(index)
        assert window.state.project.to_dict() == before
        assert window.showing.preview_builds == builds
    window.seek_edit.setValue(1.5)
    window.seek_button.click()
    assert window.canvas.position_ticks == 1080000
    assert window.progress_slider.value() == 1500
    window.progress_slider.setValue(2000)
    assert window.canvas.position_ticks == 1440000
    window.play_button.click()
    assert window.canvas.playing
    window.play_button.click()
    assert not window.canvas.playing
    assert not window.canvas._timer.isActive()
    assert not any(t.isActive() for t in window.findChildren(QtCore.QTimer))
    choose(window, 'english')
    window.size_edit.setValue(180)
    window.size_button.click()
    window.action_save.trigger()
    from desktop.editor_project import ProjectFolder
    reopened = ProjectFolder.open(window.folder.path)
    assert reopened.project.to_dict() == window.state.project.to_dict()
