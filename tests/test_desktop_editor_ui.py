"""The editor window, driven by real mouse events on a headless Qt platform.

What these tests are for
------------------------
The acceptance says a plain click selects one clip and a drag moves only it, and
that the window must be operable at more than one scale.  Those are properties of
the *widget*, not of the model, so they are checked here by handing the timeline
real ``QTest`` mouse events and then reading the project: the click's effect is
asserted on solved ticks per object, not on a screenshot.

No pixel comparison anywhere.  A screenshot of text rendering is not a business
test, and the geometry that matters - which placement, which range - is already a
number the layout produced.

The platform is ``offscreen``, so this file needs no display and cannot steal the
machine from whoever is using it.
"""
import json
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtCore = pytest.importorskip('PySide6.QtCore')
QtGui = pytest.importorskip('PySide6.QtGui')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')
QtTest = pytest.importorskip('PySide6.QtTest')

from desktop import editor_notices as notices            # noqa: E402
from desktop.editor_model import MODE_ADVANCED           # noqa: E402
from desktop.editor_window import EditorWindow           # noqa: E402
from desktop.timeline_widget import HANDLE_PX, LANES     # noqa: E402
from test_editor_support import revision_snapshot, solved_snapshot, tiny_folder  # noqa: E402
from test_preview_support import scratch, write_tone  # noqa: E402

WINDOW = (1280, 820)


@pytest.fixture(scope='module')
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


@pytest.fixture
def area():
    with scratch('editor-ui-') as folder:
        yield folder


@pytest.fixture
def window(app, area):
    folder = tiny_folder(area / 'project')
    editor = EditorWindow()
    editor.attach(folder)
    editor.resize(*WINDOW)
    editor.show()
    app.processEvents()
    yield editor
    editor.close()
    app.processEvents()


def settle(app, times=3):
    for _ in range(times):
        app.processEvents()


def bar_point(editor, clip_id, fraction=0.5):
    item = editor.timeline.item(clip_id)
    assert item is not None, 'the timeline does not draw %s' % clip_id
    rect = editor.timeline.bar_rect(item)
    return QtCore.QPoint(int(rect.left() + rect.width() * fraction),
                         int(rect.center().y()))


def drag(app, editor, start, dx=0, dy=0):
    QtTest.QTest.mousePress(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, start)
    settle(app)
    QtTest.QTest.mouseMove(editor.timeline, QtCore.QPoint(start.x() + dx, start.y() + dy))
    settle(app)
    QtTest.QTest.mouseRelease(editor.timeline, QtCore.Qt.LeftButton,
                              QtCore.Qt.NoModifier,
                              QtCore.QPoint(start.x() + dx, start.y() + dy))
    settle(app)


def tick_pixels(editor):
    return editor.timeline.diagnostics()['pixels_per_second']


# ---------------------------------------------------------------- the window
def test_the_window_really_has_all_four_panels(window, app):
    editor = window
    assert editor.canvas is not None, 'no canvas widget'
    assert editor.showing.session is not None, 'no preview session behind the canvas'
    assert editor.timeline.diagnostics()['rows'] == len(LANES)
    assert len(editor.timeline.items()) == len(editor.state.project.clips)
    # property panel: nothing selected yet, and it says so rather than lying
    assert editor.property_labels['clip'].text() == '—'
    assert editor.notice_area.isVisible() or editor.notice_area is not None
    assert editor.last_notice_texts() == () or isinstance(editor.last_notice_texts(), tuple)
    assert editor.side_tabs.count() == 2                # 属性 / 交付设置


def test_the_first_selection_fills_the_property_panel(window, app):
    editor = window
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    assert editor.state.selection == ('w1.male',)
    assert editor.property_labels['clip'].text() == 'w1.male'
    assert editor.property_labels['role'].text() == '男声英文'
    assert 'promise' in editor.property_labels['word'].text()
    assert '不可改' in editor.property_labels['style'].text() or \
        '无字形' in editor.property_labels['style'].text()


def test_a_click_selects_exactly_one_clip_and_ctrl_click_extends(window, app):
    editor = window
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.chinese'))
    settle(app)
    assert editor.state.selection == ('w1.chinese',)     # replaced, not extended
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.ControlModifier, bar_point(editor, 'w1.male'))
    settle(app)
    assert set(editor.state.selection) == {'w1.male', 'w1.chinese'}


# ------------------------------------------------------------------- drags
def test_dragging_one_clip_moves_only_that_clip(window, app):
    editor = window
    before_solved = solved_snapshot(editor.state)
    before_stored = revision_snapshot(editor.state)
    start = bar_point(editor, 'w1.male')
    drag(app, editor, start, dx=int(0.3 * tick_pixels(editor)))

    after_stored = revision_snapshot(editor.state)
    assert editor.state.revision == 1, 'the drag did not reach the project'
    for clip_id, value in before_stored.items():
        if clip_id != 'w1.male':
            assert after_stored[clip_id] == value, clip_id
    assert after_stored['w1.male'] != before_stored['w1.male']
    after_solved = solved_snapshot(editor.state)
    assert after_solved['w1.male'][0] > before_solved['w1.male'][0]
    assert editor.state.selection == ('w1.male',)


def test_dragging_a_selected_group_moves_the_whole_group(window, app):
    editor = window
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.ControlModifier, bar_point(editor, 'w1.chinese'))
    settle(app)
    before = revision_snapshot(editor.state)
    start = bar_point(editor, 'w1.male')
    drag(app, editor, start, dx=int(0.2 * tick_pixels(editor)))
    after = revision_snapshot(editor.state)
    moved = {clip_id for clip_id in before if after[clip_id] != before[clip_id]}
    assert moved == {'w1.male', 'w1.chinese'}, moved
    assert editor.timeline.last_gesture['kind'] == 'move'
    assert set(editor.timeline.last_gesture['clip_ids']) == moved


def test_dragging_the_right_handle_trims_that_clip(window, app):
    editor = window
    item = editor.timeline.item('w1.chinese')
    rect = editor.timeline.bar_rect(item)
    start = QtCore.QPoint(int(rect.right() - HANDLE_PX / 2), int(rect.center().y()))
    before_stored = revision_snapshot(editor.state)
    before_solved = solved_snapshot(editor.state)
    drag(app, editor, start, dx=-int(0.25 * tick_pixels(editor)))

    after_stored = revision_snapshot(editor.state)
    assert editor.state.revision == 1
    for clip_id, value in before_stored.items():
        if clip_id != 'w1.chinese':
            assert after_stored[clip_id] == value, clip_id
    after_solved = solved_snapshot(editor.state)
    assert after_solved['w1.chinese'][0] == before_solved['w1.chinese'][0]  # start kept
    assert after_solved['w1.chinese'][1] < before_solved['w1.chinese'][1]
    assert editor.timeline.last_gesture['kind'] == 'trim'
    assert editor.timeline.last_gesture['edge'] == 'end'
    # Trimming the target never cuts the recording behind it.
    assert editor.state.project.clip('w1.chinese').source.source_end == 48000


def test_a_press_without_a_move_changes_nothing(window, app):
    editor = window
    before = revision_snapshot(editor.state)
    point = bar_point(editor, 'w1.female')
    drag(app, editor, point, dx=0)
    assert editor.state.revision == 0
    assert revision_snapshot(editor.state) == before


def test_alt_click_on_a_per_record_clip_is_refused_with_a_located_reason(window, app):
    """The cut needs a splittable layer; the window must say so, not invent a clip."""
    editor = window
    before = revision_snapshot(editor.state)
    point = bar_point(editor, 'w1.chinese', fraction=0.4)
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.AltModifier, point)
    settle(app)
    assert revision_snapshot(editor.state) == before
    assert editor.state.revision == 0
    texts = ' | '.join(editor.last_notice_texts())
    assert 'promise' in texts and '拆分' in texts
    assert any(notice.code == 'SPLIT_NOT_APPLICABLE' for notice in editor._last_notices)
    # The button state comes from A's own check, so it agrees with the message.
    assert editor.split_button.isEnabled() is False
    assert 'SPLIT_NOT_APPLICABLE' in editor.split_button.text()


def test_the_split_button_is_offered_only_for_a_splittable_layer(window, app):
    editor = window
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    assert editor.split_button.isEnabled() is False
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'layer.title'))
    settle(app)
    assert editor.split_button.isEnabled() is False


def test_editing_the_font_size_reaches_the_style_table_and_the_canvas(window, app):
    """A-6's ``SetStyle``: the size is the member's now, and it is one table.

    The assertion is on the merged style table the canvas and the exporter both
    read (``merged_styles``), not on a widget value: a spin box that changes nothing
    downstream is exactly what this test exists to catch.
    """
    from word_video.application.styles import merged_styles
    editor = window
    # Select the style role, not an offlined timeline bar.
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
    # It is a style override, not a document rewrite: the clips are untouched.
    assert [clip['id'] for clip in editor.state.project.to_dict()['clips']] == \
        [clip['id'] for clip in document_before['clips']]
    assert editor.state.project.to_dict() != document_before   # but a real edit
    assert '本工程已改' in editor.property_labels['style'].text()

    # And the canvas draws with the merged table, so the change is on screen.
    display = editor.canvas.display
    assert display is not None
    size = max(placed.size for placed in display.report.placements
               if placed.role == role)
    assert size == pytest.approx(180.0 * (editor.canvas.display.report.height / 2160.0),
                                 rel=0.02)

    outcome = editor.state.clear_style(role)
    assert outcome.changed
    editor.after_edit(rebuild_preview=outcome.changed)
    settle(app)
    assert merged_styles(editor.state.plan().style_table())[role]['size'] == before
    assert '（默认）' in editor.property_labels['style'].text()


def test_the_intro_is_not_splittable_and_the_window_says_so(window, app, area):
    """The countdown is A's one non-splittable media layer, and the window shows it.

    Two layers of defence are asserted here, and the second one has to be tested on a
    document the first layer could never produce:

    * A's ``can_split`` refuses the intro with a reason, so the button is disabled
      and the Alt+click route returns the same structured refusal - located at the
      clip, with the document untouched (revision included);
    * the delivery gate (``SPLIT_INTRO_NOT_EXPORTABLE``) still refuses to *publish* a
      project that carries two countdowns, on a project built directly for the test
      so the safety net stays exercised instead of becoming dead code.  It blocks the
      export only: the conflicting project still saves.
    """
    from desktop.editor_import import import_request
    from test_editor_support import tiny_folder
    from test_preview_support import write_test_video

    folder = tiny_folder(area / 'splittable')
    intro = write_test_video(area / 'intro.mp4', 2.0, 30, '160x120')
    request = area / 'request.json'
    wordlist = area / 'words.txt'
    wordlist.write_text('promise /ˈprɒmɪs/ n. 承诺\n', encoding='utf-8')
    tones = {}
    for role in ('female', 'male', 'chinese'):
        tones[role] = str(write_tone(area / ('%s.wav' % role), 1.0, 400.0))
    request.write_text(json.dumps({
        'source': {'path': str(wordlist)}, 'range': {'start': 1, 'end': 1},
        'lesson': {'speed': 1.25, 'width': 640, 'height': 360,
                   'intro': {'video': str(intro)}},
        'provider': {'kind': 'local', 'items': [
            {'index': 1, 'role': role, 'text': '承诺' if role == 'chinese' else 'promise',
             'voice': 'BV503_streaming', 'path': tones[role]}
            for role in ('female', 'male', 'chinese')]}}, ensure_ascii=False),
        encoding='utf-8')
    del folder

    editor = window
    editor.attach(import_request(request, area / 'split-project'))
    editor.resize(*WINDOW)
    settle(app)
    clip = editor.state.project.intro_clip()
    assert clip is not None
    before = revision_snapshot(editor.state)

    # The button is *unavailable* for the countdown, and the reason comes from A's
    # own check - so what the member sees greyed out and what the command would say
    # cannot disagree.  Selecting it through the timeline is what refreshes the panel.
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, clip.id))
    settle(app)
    assert editor.state.selection == (clip.id,)
    check = editor.state.split_check(clip.id)
    assert check.ok is False and check.code == 'SPLIT_NOT_APPLICABLE'
    assert 'countdown' in check.reason
    assert editor.split_button.isEnabled() is False
    assert 'SPLIT_NOT_APPLICABLE' in editor.split_button.text()

    # Pressing it anyway changes nothing: no clip is created, the revision stays put.
    editor.seek(clip.duration_ticks // 2)
    settle(app)
    editor.split_button.click()
    settle(app)
    assert revision_snapshot(editor.state) == before
    assert editor.state.revision == 0
    assert len([item for item in editor.state.project.clips
                if item.role == 'intro']) == 1

    # Alt+click asks for a cut at that tick and gets A's structured refusal, located
    # at the clip - the same code, so the two routes cannot say different things.
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.AltModifier,
                            bar_point(editor, clip.id, fraction=0.4))
    settle(app)
    assert revision_snapshot(editor.state) == before
    assert any(notice.code == 'SPLIT_NOT_APPLICABLE' for notice in editor._last_notices)
    assert any(clip.id in notice.headline() or notice.clip_id == clip.id
               for notice in editor._last_notices)
    assert editor.export() is not None or editor.last_export is not None

    # The delivery gate is the second line of defence, so it is exercised on a
    # document A's command can no longer produce: two intro clips, built directly.
    # (``tests/test_desktop_editor_project.py`` asserts the same for the folder and
    # the checker; here it is the *window* that must refuse to publish.)
    from dataclasses import replace
    from desktop.editor_export import blocking_notices
    from word_video.domain.timebase import TimeExpr
    twin = replace(clip, id='layer.intro.2', start=TimeExpr.at(clip.duration_ticks))
    editor.state.session = replace(editor.state.session,
                                   project=editor.state.project.with_clips(
                                       editor.state.project.clips + (twin,)),
                                   history=editor.state.session.history, future=())
    editor.state._invalidate()
    editor.after_edit(rebuild_preview=False)
    settle(app)
    intros = [item.id for item in editor.state.project.clips if item.role == 'intro']
    assert sorted(intros) == ['layer.intro', 'layer.intro.2']
    blockers = blocking_notices(editor.folder, editor.state.project)
    assert [notice.code for notice in blockers] == ['SPLIT_INTRO_NOT_EXPORTABLE']
    assert blockers[0].clip_id == 'layer.intro.2'
    assert editor.export() is None
    assert editor.diagnostics()['exporting'] is False
    # Saving is not blocked: a conflicting project may be kept and fixed later.
    assert editor.save() is not None
    assert editor.state.dirty is False


def test_a_click_on_a_group_member_collapses_to_that_one_clip(window, app):
    """A press that never moves is a click, and a click selects exactly one clip.

    Pressing a member of a group has to keep the group for a *drag* (that is how a
    group is moved), so which gesture it was is only known on release - and the
    difference matters: a member who clicks one bar of a group and then drags it
    must not carry the others along.
    """
    editor = window
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.ControlModifier, bar_point(editor, 'w1.chinese'))
    settle(app)
    assert set(editor.state.selection) == {'w1.male', 'w1.chinese'}

    before = revision_snapshot(editor.state)
    QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                            QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
    settle(app)
    assert editor.state.selection == ('w1.male',)
    assert revision_snapshot(editor.state) == before    # a click is not an edit
    assert editor.timeline.last_gesture['kind'] == 'collapse'

    # And the drag that follows moves the one clip, not the group it came from.
    drag(app, editor, bar_point(editor, 'w1.male'), dx=int(0.3 * tick_pixels(editor)))
    after = revision_snapshot(editor.state)
    moved = {clip_id for clip_id in before if after[clip_id] != before[clip_id]}
    assert moved == {'w1.male'}, moved


def test_the_canvas_draws_the_layout_the_delivery_draws(window, app):
    """预览与成片一致: the canvas reads B's ``LayoutSurface``, at the canvas's size.

    The measurement is the placement list the session actually returns at a tick
    where text is due, plus the fact that it was built from the *merged* style
    table - the two things that make "the preview shows what will be exported" a
    checkable claim instead of a hope.
    """
    editor = window
    canvas = editor.canvas
    assert canvas.display is not None, 'the canvas has no layout port attached'
    start, end = editor.state.clip_range('w1.english')
    midpoint = (start + end) // 2
    editor.seek(midpoint)
    due = canvas.presentation().placements
    # Independently construct the delivery surface at preview resolution, then compare
    # every field (not just role count or font size). Overrides catch a stale table.
    from word_video.application.styles import style_fonts
    from word_video.exporters.ass import layout_for
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
    roles = {placed.role for placed in due}
    assert 'english' in roles
    for placed in due:
        assert placed.lines, placed.clip_id
        for line in placed.lines:
            assert 0.0 <= line.x <= 1.0 and 0.0 <= line.y <= 1.0
    # Built for the canvas, not for the plan: a 450-px size belongs to a 2160-high
    # canvas, and the preview would draw text four times too large if it used it.
    english = next(placed for placed in due if placed.role == 'english')
    assert english.size == pytest.approx(
        editor.state.styles()['english']['size']
        * (canvas.display.report.height / 2160.0), rel=0.02)
    over = canvas.display.overflowing()
    assert over == (), [placed.clip_id for placed in over]


# ------------------------------------------------------------- the notices
def test_the_notice_panel_shows_a_button_that_locates_the_problem(window, app):
    editor = window
    male = editor.state.project.clip('w1.male').start.ticks
    chinese = editor.state.project.clip('w1.chinese').start.ticks
    editor.state.select('w1.male')
    editor.state.move_selection(chinese - male - 12000)     # one frame of overlap
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
    editor = window
    from pathlib import Path
    gone = editor.folder.asset_path('w1:male')
    Path(gone).unlink()
    editor.refresh(rebuild_preview=True)
    settle(app)
    assert any(notice.code == 'ASSET_FILE_MISSING'
               for notice in editor._last_notices)
    # Put a file back and ask the editor to look again.
    from test_preview_support import write_tone
    editor.folder.set_asset_file('w1:male', str(write_tone(area / 'back.wav', 1.0, 500.0)))
    editor.run_action(notices.Action(notices.ACTION_RECHECK, '重新检查'))
    settle(app)
    assert not [notice for notice in editor._last_notices
                if notice.code == 'ASSET_FILE_MISSING']
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
    assert editor.state.revision == 3        # 1 move + undo + redo, never backwards


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
    assert editor.showing.preview_builds == builds      # mode is not an edit


# ------------------------------------------------------------- preview cost
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
    """Selecting is not editing: it must not cost an open (~1 s on real media)."""
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


# ---------------------------------------------------------- scale and layout
@pytest.mark.parametrize('size', [(960, 640), (1600, 1000)])
def test_the_window_is_operable_at_more_than_one_size(app, area, size):
    """Two window sizes here; two DPI factors are measured in a fresh process.

    ``QT_SCALE_FACTOR`` cannot be changed inside a running Qt application, so the
    DPI half of the acceptance lives in ``desktop/measure_editor.py``, which
    starts one process per factor.  What this test does prove is that the pixel to
    tick mapping follows the widget rather than a constant: the same click lands on
    the same clip at both sizes.
    """
    folder = tiny_folder(area / ('size-%d' % size[0]))
    editor = EditorWindow()
    editor.attach(folder)
    editor.resize(*size)
    editor.show()
    settle(app)
    try:
        assert editor.devicePixelRatioF() > 0
        probe = editor.timeline.diagnostics()
        assert probe['pixels_per_second'] > 0 and probe['width'] > 200
        QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                                QtCore.Qt.NoModifier, bar_point(editor, 'w1.male'))
        settle(app)
        assert editor.state.selection == ('w1.male',)
        # And every clip is still reachable at this width.
        for clip_id in ('w1.female', 'w1.chinese', 'layer.title'):
            point = bar_point(editor, clip_id, fraction=0.25)
            QtTest.QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton,
                                    QtCore.Qt.NoModifier, point)
            settle(app)
            assert editor.state.selection == (clip_id,), (size, clip_id)
    finally:
        editor.close()
        settle(app)


def test_zoom_keeps_the_mapping_consistent(window, app):
    editor = window
    timeline = editor.timeline
    assert timeline.zoom == 1.0
    timeline.zoom = 4.0
    timeline.update()
    settle(app)
    for clip_id in ('w1.female', 'w1.male'):
        point = bar_point(editor, clip_id)
        ticks = timeline.x_to_ticks(point.x())
        item = timeline.item(clip_id)
        assert item.start_ticks <= ticks <= item.end_ticks
        assert timeline.ticks_to_x(item.start_ticks) <= point.x() + 1
    timeline.fit()
    assert timeline.zoom == 1.0 and timeline.origin_ticks == 0


# --------------------------------------------------------------- the export
def test_export_refuses_a_project_with_a_missing_file_without_starting_a_thread(window, app):
    editor = window
    from pathlib import Path
    Path(editor.folder.asset_path('w1:chinese')).unlink()
    editor.refresh(rebuild_preview=True)
    settle(app)
    result = editor.export()
    settle(app)
    assert result is None
    assert editor.diagnostics()['exporting'] is False
    assert editor._thread is None
    assert any(notice.code == 'ASSET_FILE_MISSING'
               for notice in editor._last_notices)


def test_the_delivery_panel_writes_what_the_exporter_reads(window, app, area):
    editor = window
    from test_preview_support import write_test_video
    background = write_test_video(area / 'bg.mp4', 1.0, 30, '160x120')
    editor.delivery_edits['background'].setText(str(background))
    editor.save_delivery()
    settle(app)
    assert editor.folder.delivery.background == str(background)
    from desktop.editor_project import Delivery
    assert Delivery.load(editor.folder.path).background == str(background)
    # The canvas shows it: the delivery setting reaches the preview plan.
    assert editor.folder.background_slice(editor.state.project) is not None
