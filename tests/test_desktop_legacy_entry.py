"""The old factory as an entry in the editor window, driven offscreen.

What these tests are for
------------------------
The task has two interface requirements that are properties of the *window*, not of
the adapter: the member must be able to reach the old factory from the new software,
and the member must be able to see which of the two timing rules the entry is on -
with both rules' real numbers in front of them.  So the tested object here is the
window and its dialog, driven without a display:

* the menu entry names the clock, and the banner prints the shared sentence plus the
  old run's total and the open project's plan total;
* a run from the window publishes the five tracks, reports one notice, and leaves the
  project revision untouched;
* a *refused* run (a bad word list, a protected output path) is a notice card, and the
  editor keeps working - the failure isolation the task asks for, seen from the side
  the member actually experiences;
* a project whose plan cannot be computed still gets its old-factory entry: the new
  caliber says why it is unavailable and the old one still produces files.
"""
import os
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
QtWidgets = pytest.importorskip('PySide6.QtWidgets')

from desktop.editor_export import blocking_notices          # noqa: E402
from desktop.editor_window import EditorWindow              # noqa: E402
from desktop.legacy_dialog import BANNER_TITLE              # noqa: E402
from legacy_adapter import CALIBER_LEGACY, CALIBER_MEDIA, UI_NOTICE  # noqa: E402
from legacy_adapter import locate_legacy_entry              # noqa: E402
from test_editor_support import tiny_folder                 # noqa: E402
from test_preview_support import scratch                    # noqa: E402

WORDS = '''apple [ˈæpl] n. 苹果
banana [bəˈnɑːnə] n. 香蕉；芭蕉
candidate [ˈkændɪdət] n. 候选人；应试者；申请人；候补者；竞选人；报名者
'''


@pytest.fixture(scope='module')
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


@pytest.fixture
def area():
    with scratch('legacy-entry-') as folder:
        yield folder


@pytest.fixture
def wordlist(area):
    path = area / 'words.txt'
    path.write_text(WORDS, encoding='utf-8')
    return path


@pytest.fixture
def window(app, area):
    folder = tiny_folder(area / 'project')
    editor = EditorWindow()
    editor.attach(folder)
    editor.resize(1100, 700)
    editor.show()
    app.processEvents()
    yield editor
    editor.close()
    app.processEvents()


def plan_milliseconds(window):
    from word_video.domain.timebase import ticks_to_milliseconds
    plan = window.state.plan()
    assert plan is not None, window.state.plan_error
    return ticks_to_milliseconds(plan.total_ticks)


def test_the_menu_entry_names_the_old_clock(window):
    assert window.action_legacy.text() == '旧字幕工厂（文字规则计时）…'
    # And it is a separate entry from the delivery: "导出三产物" stays the new clock.
    assert window.action_export.text() == '导出三产物'


def test_the_banner_shows_both_clocks_with_their_real_numbers(window, app):
    dialog = window.legacy_dialog()
    dialog.show()
    app.processEvents()
    text = dialog.banner_text()
    assert BANNER_TITLE in text
    assert UI_NOTICE in text                       # 与 CLI 同一句话，不是第二套说法
    assert '旧口径' in text and '新口径' in text
    # The old clock has no number yet, and says so instead of showing a guess.
    assert '点“生成五轨 SRT”后显示' in text
    # The new clock's number comes from the open project's own plan.
    expected = '合计 %.3f s' % (plan_milliseconds(window) / 1000.0)
    assert expected in text, text
    assert CALIBER_LEGACY == window.diagnostics()['legacy']['caliber']
    assert CALIBER_MEDIA == window.diagnostics()['legacy']['export_caliber']
    dialog.close()


def test_the_dialog_collects_only_the_old_tools_parameters(window):
    dialog = window.legacy_dialog()
    dialog.start_box.setValue(2)
    dialog.end_box.setValue(3)
    dialog.batch_box.setValue(1)
    request = dialog.request()
    assert (request.start, request.end, request.batch_size) == (2, 3, 1)
    assert (request.first_six, request.extra) == (0.4, 0.2)   # the old tool's defaults
    assert set(dialog.values()) == {'wordlist', 'start', 'end', 'batch_size', 'first_six',
                                    'extra', 'output'}
    dialog.close()


def test_a_window_run_writes_five_tracks_and_touches_no_project_state(window, area,
                                                                     wordlist, app):
    output = area / 'legacy-out'
    revision = window.state.revision
    outcome = window.run_legacy({'wordlist': str(wordlist), 'start': 1, 'end': 3,
                                 'batch_size': 3, 'output': str(output)})
    assert outcome['ok'] is True, outcome
    report = outcome['report']
    assert report['counts']['file_count'] == 5
    assert sorted(path.name for path in output.glob('*.srt')) == [
        'apple_Part1_01_英文重复.srt', 'apple_Part1_02_英文单次.srt',
        'apple_Part1_03_音标.srt', 'apple_Part1_04_中文带词性.srt',
        'apple_Part1_05_中文无词性.srt']
    diagnostics = window.diagnostics()['legacy']
    assert diagnostics['last_ok'] is True and diagnostics['last_code'] == 'LEGACY_TRACKS_OK'
    assert diagnostics['packages'] == 1 and diagnostics['files'] == 5
    assert diagnostics['wordlist'] == str(wordlist)
    assert 'LEGACY_TRACKS_OK' in window.diagnostics()['notices']
    # The project is not a party to this: same revision, nothing dirty, still solvable.
    assert window.state.revision == revision and window.state.dirty is False
    assert blocking_notices(window.folder, window.state.project) == ()
    # The banner now carries the old clock's real total as well.
    assert '旧口径实际数值（本次）' in window.legacy_dialog().banner_text()


def test_a_failed_run_is_a_card_and_leaves_the_editor_working(window, area, wordlist):
    broken = area / 'broken.txt'
    broken.write_text('!!! not a word list\n', encoding='utf-8')
    revision = window.state.revision
    outcome = window.run_legacy({'wordlist': str(broken), 'output': str(area / 'refused')})
    assert outcome['ok'] is False
    assert outcome['error']['code'] == 'INPUT_ERROR'
    assert 'INPUT_ERROR' in window.diagnostics()['notices']
    assert window.diagnostics()['legacy']['last_ok'] is False
    # Nothing about the editor changed, and the next run is not poisoned by the last.
    assert window.state.revision == revision and window.state.dirty is False
    assert window.state.plan() is not None
    assert blocking_notices(window.folder, window.state.project) == ()
    again = window.run_legacy({'wordlist': str(wordlist), 'start': 1, 'end': 1,
                               'output': str(area / 'after-failure')})
    assert again['ok'] is True and again['report']['counts']['file_count'] == 5


def test_a_protected_output_path_is_a_structured_refusal(window, area, wordlist):
    factory = Path(locate_legacy_entry()).parent
    outcome = window.run_legacy({'wordlist': str(wordlist),
                                 'output': str(factory / 'w10-must-not-exist')})
    assert outcome['ok'] is False
    assert outcome['error']['code'] == 'LEGACY_OUTPUT_UNSAFE'
    assert 'LEGACY_OUTPUT_UNSAFE' in window.diagnostics()['notices']
    assert window.state.plan() is not None
    assert not (factory / 'w10-must-not-exist').exists()


def test_the_old_entry_works_while_the_new_caliber_cannot_be_computed(app, area, wordlist):
    """A project whose media is broken still gets the old factory's five tracks.

    This is the failure isolation of the task seen from the member's side: the new
    module's refusal (an asset that is no longer on disk, so no plan) is information
    in the banner, and the old entry keeps working - it never reads media at all.
    """
    folder = tiny_folder(area / 'broken-project')
    Path(folder.asset_path('w1:female')).unlink()
    folder.media_table(refresh=True)          # 重新探测：测不到就清掉测量，不留下旧长度
    editor = EditorWindow()
    editor.attach(folder)
    editor.show()
    app.processEvents()
    try:
        block = editor.media_caliber_block()
        assert block['available'] is False and block['reason']
        dialog = editor.legacy_dialog()
        assert '暂不可用' in dialog.banner_text()
        outcome = editor.run_legacy({'wordlist': str(wordlist), 'start': 1, 'end': 1,
                                     'output': str(area / 'broken-project-legacy')})
        assert outcome['ok'] is True, outcome
        assert outcome['report']['counts']['file_count'] == 5
    finally:
        editor.close()
        app.processEvents()
