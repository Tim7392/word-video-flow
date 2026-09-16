"""The member package with the editor in it: what must stay true about both routes.

The launcher is one file serving two audiences - a member who double-clicks it and
an agent that passes arguments - and it is the only thing standing between "the
package works" and "the member sees a black window they must not close".  These
checks pin the dispatch, the two frozen builds and the rules that keep the two
executables from drifting apart, because none of it is visible on the development
machine (where the editor is started with ``python -m desktop``).

The *behaviour* of the launcher is measured for real by
``packaging/verify_member_package.py`` against a built package: it starts the editor
by double-click equivalent and then drives import -> export through the window.  What
is pinned here is everything that can be decided without building a 400 MB package.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('packaging_build_editor', PACKAGING / 'build_member_package.py')
verify = load('packaging_verify_editor', PACKAGING / 'verify_member_package.py')
entry = load('desktop_main_entry', ROOT / 'desktop' / '__main__.py')
entry_source = (ROOT / 'desktop' / '__main__.py').read_text(encoding='utf-8')
launcher_bytes = (PACKAGING / 'launcher' / builder.LAUNCHER_NAME).read_bytes()
launcher = launcher_bytes.decode('utf-8')
readme = (PACKAGING / builder.README_NAME).read_text(encoding='utf-8')


# --------------------------------------------------------------- the launcher
def test_the_launcher_opens_the_editor_when_it_is_given_nothing():
    assert 'if "%~1"=="" set "WV_MODE=window"' in launcher
    assert 'start "" "%PKG%\\WordVideoEditor\\WordVideoEditor.exe" --editor' in launcher
    assert 'if "%WV_MODE%"=="window" exit /b 0' in launcher


def test_the_cli_is_reached_only_by_an_argument_the_editor_does_not_claim():
    """The order matters: both editor doors must close before the CLI line, or
    ``单词视频.cmd doctor`` would open a window and never return."""
    cli = launcher.index('"%PKG%\\WordVideo\\WordVideo.exe" %*')
    assert launcher.index('if "%WV_MODE%"=="window" exit /b 0') < cli
    assert launcher.index('if "%WV_MODE%"=="driven" exit /b %ERRORLEVEL%') < cli


def test_a_driven_editor_run_stays_in_the_foreground_so_the_caller_gets_the_code():
    assert ('if "%WV_MODE%"=="driven" "%PKG%\\WordVideoEditor\\WordVideoEditor.exe" %*'
            in launcher)
    # ``--editor`` on its own is still just "open the window"; it is the arguments
    # after it that make the run something a caller waits for.
    assert 'if /i "%~1"=="--editor" set "WV_MODE=driven"' in launcher
    assert 'if /i "%~1"=="--editor" if "%~2"=="" set "WV_MODE=window"' in launcher


def test_the_launcher_has_no_goto_labels():
    r"""Measured, not stylistic: this file is stored with LF endings, cmd.exe looks a
    label up on a CR-terminated line, and so *every* GOTO was answered with "The
    system cannot find the batch label specified - editor_window" and the member's
    double-click did nothing at all.  A mode variable with single-line IFs does the
    same dispatching without that trap - and without the other one either, because an
    ``%ERRORLEVEL%`` inside brackets is expanded before the line that sets it runs.
    """
    commands = [line.strip().lower() for line in launcher.splitlines()]
    assert [line for line in commands if line.startswith('goto ')] == []
    assert [line for line in commands if line.startswith(':')] == []


def test_the_launcher_is_crlf_which_is_what_cmd_expects():
    assert launcher_bytes.count(b'\r\n') == launcher_bytes.count(b'\n') > 0
    assert b'\r\n' not in launcher_bytes.replace(b'\r\n', b'')


def test_the_detached_window_does_not_hand_its_console_to_the_caller():
    """Measured: a caller that captured the launcher's output kept its pipe open
    until the editor was closed, because the started process inherits the handles."""
    window_line = next(line for line in launcher.splitlines()
                       if line.startswith('if "%WV_MODE%"=="window" start'))
    assert window_line.endswith('>nul 2>nul')


def test_the_launcher_reports_a_package_that_is_missing_the_editor():
    assert 'if not exist "%PKG%\\WordVideoEditor\\WordVideoEditor.exe"' in launcher
    # Both halves say the same thing and use the same exit code for the same reason.
    assert launcher.count('incomplete package') == 2
    assert 'exit /b 3' in launcher


def test_the_launcher_still_announces_the_parts_it_always_did():
    # ASCII only, so the launcher's own (Chinese) file name cannot appear in it.
    assert launcher_bytes.decode('ascii')
    for part in ('WordVideo\\WordVideo.exe', 'WordVideoEditor\\WordVideoEditor.exe',
                 'ffmpeg', 'temp'):
        assert part in launcher


# --------------------------------------------- the launcher, actually executed
def stub_package(tmp_path):
    """A package whose two executables are ``cmd.exe``.

    Not a stand-in for a build: it is what makes the *dispatch* observable.  The
    launcher forwards its arguments verbatim, so ``cmd.exe`` echoing them back is
    evidence about which door opened - and the doors are handed different argument
    lists, which is exactly what the two tests below rely on.  The window door
    cannot be run this way (it would open a real console window in the test suite);
    it is measured against the built package by ``verify_member_package.py``.
    """
    import shutil
    for relative in ('WordVideo/WordVideo.exe', 'WordVideoEditor/WordVideoEditor.exe'):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe', target)
    shutil.copy2(PACKAGING / 'launcher' / builder.LAUNCHER_NAME,
                 tmp_path / builder.LAUNCHER_NAME)
    return tmp_path


def run_stub_launcher(stub, arguments=(), timeout=120):
    environment, _ = verify.clean_environment(stub)
    return subprocess.run([environment['ComSpec'], '/c', builder.LAUNCHER_NAME]
                          + [str(item) for item in arguments],
                          cwd=str(stub), env=environment, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def test_an_ordinary_argument_reaches_the_cli_door(tmp_path):
    stub = stub_package(tmp_path)
    result = run_stub_launcher(stub, ['/c', 'echo', 'cli-door'])
    assert 'cli-door' in result.stdout
    assert result.returncode == 0
    assert (stub / 'temp').is_dir()          # TEMP was pointed inside the package


def test_the_editor_switch_reaches_the_editor_door_with_its_own_arguments(tmp_path):
    """If ``--editor`` fell through to the CLI, ``cmd.exe --editor /c echo ...``
    would fail on ``--editor`` as a command and print nothing."""
    stub = stub_package(tmp_path)
    result = run_stub_launcher(stub, ['--editor', '/c', 'echo', 'editor-door'])
    assert 'editor-door' in result.stdout
    assert result.returncode == 0


def test_a_doctor_call_is_not_turned_into_a_window(tmp_path):
    """The regression this whole dispatch exists for: an agent's
    ``单词视频.cmd doctor`` must never open a window and sit there."""
    stub = stub_package(tmp_path)
    result = run_stub_launcher(stub, ['/c', 'echo', 'doctor-called'])
    assert 'doctor-called' in result.stdout
    assert 'WV_MODE' not in result.stdout


# ------------------------------------------------------------ the editor entry
def test_the_entry_needs_no_arguments_and_accepts_the_launcher_switch():
    assert entry.parse_args([]).request is None
    assert entry.parse_args(['--editor']).editor is True


@pytest.mark.parametrize('argv', [
    ['--import', 'r.json'],                       # no --into
    ['--import', 'r.json', '--into', 'p', '--open', 'q'],
    ['--export'],                                 # nothing to deliver
    ['--output', 'x'],                            # --output alone is not a run
])
def test_the_entry_refuses_argument_combinations_it_cannot_honour(argv):
    with pytest.raises(SystemExit):
        entry.parse_args(argv)


def test_a_driven_run_is_expressed_completely_on_the_command_line():
    args = entry.parse_args(['--editor', '--import', 'r.json', '--into', 'p',
                             '--export', '--report', 'run.json'])
    assert (args.request, args.into, args.export, args.report) == ('r.json', 'p', True,
                                                                   'run.json')


def test_qt_is_imported_inside_the_run_so_help_works_without_it():
    assert 'from PySide6 import QtCore, QtWidgets' in entry_source
    assert (entry_source.index('def _run(')
            < entry_source.index('from PySide6 import QtCore, QtWidgets'))
    assert 'PySide6' not in entry_source.split('def _run(')[0].split('def main(')[0]


def test_the_entry_writes_its_report_atomically(tmp_path):
    target = tmp_path / 'run.json'
    entry.write_report(target, {'ok': True, 'n': 1})
    assert not (tmp_path / 'run.json.tmp').exists()
    assert '"n": 1' in target.read_text(encoding='utf-8')
    entry.write_report(target, {'ok': False, 'n': 2})
    assert '"n": 2' in target.read_text(encoding='utf-8')
    assert list(tmp_path.glob('*.tmp')) == []


def test_a_report_path_is_optional_and_never_invented(tmp_path):
    assert entry.write_report('', {'ok': True}) is None
    assert entry.write_report(None, {'ok': True}) is None
    assert list(tmp_path.iterdir()) == []


def test_the_entry_reports_what_the_environment_handed_it():
    facts = entry.environment_facts()
    for name in ('frozen', 'PATH', 'TEMP', 'TMP', 'PYTHONHOME', 'PYTHONPATH',
                 'tts_credentials_present'):
        assert name in facts
    assert facts['frozen'] is False      # the test is not a frozen build


def test_the_entry_never_writes_to_stdout_which_a_windowed_build_does_not_have():
    for line in entry_source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith('print(') or 'file=sys.stderr' in stripped
        assert 'sys.stdout.write' not in stripped


def test_the_window_hands_back_the_console_handles_it_inherited(tmp_path):
    """Measured in the package: a caller that captured the launcher's output waited
    for the member to close the editor, because the started process held the pipe's
    write end.  Run in a child process, so the handles being closed are not the
    test's own."""
    marker = tmp_path / 'closed.json'
    script = ('import json, sys\n'
              'sys.path.insert(0, %r)\n'
              'from desktop.__main__ import release_inherited_console\n'
              'closed = release_inherited_console()\n'
              'open(%r, "w", encoding="utf-8").write(json.dumps(closed))\n'
              % (str(ROOT), str(marker)))
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text(encoding='utf-8')) == [-10, -11, -12]


def test_only_the_interactive_path_gives_up_its_console():
    """A driven run writes its report to stderr when the caller gave it one, and the
    verifier reads that; the release must stay on the window path."""
    call = entry_source.index('release_inherited_console()', entry_source.index('def _run('))
    assert call > entry_source.index('if args.export:', entry_source.index('def _run('))


def test_the_startup_number_is_taken_before_the_import():
    """Otherwise "how long until the member sees a window" silently becomes "window
    plus import plus first preview", which is how a slow render gets reported as a
    slow editor."""
    run = entry_source[entry_source.index('def _run('):]
    assert run.index('window_shown_seconds = ') < run.index('run_import(window, args)')
    assert 'seconds_to_window=window_shown_seconds' in run


# -------------------------------------------------------------- the two builds
def test_the_editor_build_is_windowed_and_names_its_own_folder():
    command = builder.editor_pyinstaller_command(ROOT, ROOT / 'jyd', ROOT / 'media',
                                                 ROOT / 'dist', ROOT / 'work', ROOT / 'spec')
    values = lambda flag: [command[index + 1] for index, item in enumerate(command)
                           if item == flag]                              # noqa: E731
    assert '--windowed' in command and '--console' not in command
    assert '--onedir' in command and '--onefile' not in command
    assert values('--name') == [builder.EDITOR_DIR]
    assert command[-1] == str(ROOT / builder.EDITOR_ENTRY)
    assert (ROOT / builder.EDITOR_ENTRY).is_file()


def test_the_editor_build_does_not_need_the_worker_argv_hook():
    """The editor never re-enters itself: B's exporter runs in-process, so no
    ``sys.executable`` worker line exists to normalize."""
    command = builder.editor_pyinstaller_command(ROOT, ROOT / 'jyd', ROOT / 'media',
                                                 ROOT / 'dist', ROOT / 'work', ROOT / 'spec')
    assert '--runtime-hook' not in command
    assert '--runtime-hook' in builder.pyinstaller_command(
        ROOT, ROOT / 'hook.py', ROOT / 'jyd', ROOT / 'media', ROOT / 'dist',
        ROOT / 'work', ROOT / 'spec')


def test_the_editor_build_carries_the_same_runtime_data_as_the_cli():
    command = builder.editor_pyinstaller_command(ROOT, ROOT / 'jyd', ROOT / 'media',
                                                 ROOT / 'dist', ROOT / 'work', ROOT / 'spec')
    values = lambda flag: [command[index + 1] for index, item in enumerate(command)
                           if item == flag]                              # noqa: E731
    assert values('--copy-metadata') == [builder.JYD_PACKAGE]
    assert any(value.endswith(';pyJianYingDraft') for value in values('--add-data'))
    assert any(value.endswith(';pymediainfo') for value in values('--add-data'))
    assert {'pymediainfo', 'docx'} <= set(values('--hidden-import'))


def test_the_only_difference_between_the_two_builds_is_qt():
    """Same module set on purpose: ``word_video/layout/metrics.py`` falls back to an
    estimate when PIL is absent, so a GUI that shipped PIL while the CLI did not
    would lay the same project out differently in the preview and in the delivery."""
    assert set(builder.EXCLUDED_MODULES) - set(builder.EDITOR_EXCLUDED_MODULES) \
        == {'PySide6'}
    assert set(builder.EDITOR_EXCLUDED_MODULES) < set(builder.EXCLUDED_MODULES)


def test_the_editor_half_of_the_layout_rule_is_exact(tmp_path):
    missing = builder.editor_layout_problems(tmp_path)
    assert set(missing) == {'%s/%s' % (builder.EDITOR_DIR, builder.EDITOR_EXE),
                            '%s/_internal/PySide6/QtCore.pyd' % builder.EDITOR_DIR,
                            '%s/_internal/PySide6/plugins/platforms/qoffscreen.dll'
                            % builder.EDITOR_DIR}
    for item in missing:
        path = tmp_path / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    assert builder.editor_layout_problems(tmp_path) == []


def test_the_cli_layout_rule_is_untouched_by_the_editor():
    assert set(builder.package_layout_problems(Path('nowhere'))) == {
        '%s/%s' % (builder.APP_DIR, builder.APP_EXE),
        '%s/%s' % (builder.FFMPEG_DIR, builder.FFMPEG_TOOLS[0]),
        '%s/%s' % (builder.FFMPEG_DIR, builder.FFMPEG_TOOLS[1]),
        builder.LAUNCHER_NAME, builder.README_NAME, builder.VERSION_NAME}


def test_the_version_file_says_which_entry_is_which_and_what_the_launcher_defaults_to(tmp_path):
    facts = {'version': '9.9.9', 'built': 'now', 'commit': 'deadbeef', 'dirty': False,
             'built_with': 'CPython x / PyInstaller y / PySide6 z', 'tools': {},
             'scan': {'result': 'PASS', 'errors': 0, 'warnings': 0}}
    builder.write_version_file(tmp_path, facts)
    text = (tmp_path / builder.VERSION_NAME).read_text(encoding='utf-8')
    assert 'editor=%s' % builder.EDITOR_ENTRY in text
    assert builder.EDITOR_DIR in text
    assert 'launcher_default=%s with no arguments opens the editor' % builder.LAUNCHER_NAME \
        in text
    assert 'PySide6 z' in text


def test_the_scanned_package_still_has_to_carry_the_editor(tmp_path):
    """A build that produced only the CLI must not be publishable."""
    result = builder.scan_package(tmp_path)
    missing = builder.package_layout_problems(tmp_path) + builder.editor_layout_problems(tmp_path)
    assert result['errors'] == 0
    assert any(item.startswith(builder.EDITOR_DIR) for item in missing)


def test_the_library_document_template_is_allowed_in_both_frozen_apps(tmp_path):
    """Found the hard way: the allowance named only the CLI's ``_internal``, so the
    editor's byte-identical copy was reported as "document material" and the entire
    package refused to publish after ten minutes of building."""
    for app in (builder.APP_DIR, builder.EDITOR_DIR):
        allowed = tmp_path / app / '_internal' / 'docx' / 'templates' / 'default.docx'
        allowed.parent.mkdir(parents=True)
        allowed.write_bytes(b'PK\x03\x04' + b'\x00' * 32)
    result = builder.scan_package(tmp_path)
    assert result['findings'] == []
    assert set(result['evidence']['allowances']) == {
        'WordVideo/_internal/docx/templates/default.docx',
        'WordVideoEditor/_internal/docx/templates/default.docx'}


def test_a_member_document_inside_either_frozen_app_is_still_flagged(tmp_path):
    """The allowance is one path shape, not "anything with .docx in _internal"."""
    for app in (builder.APP_DIR, builder.EDITOR_DIR):
        member = tmp_path / app / '_internal' / 'words.docx'
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(b'PK\x03\x04' + b'\x00' * 32)
    findings = builder.scan_package(tmp_path)['findings']
    assert [item['path'] for item in findings] == [
        'WordVideo/_internal/words.docx', 'WordVideoEditor/_internal/words.docx']
    assert {item['rule'] for item in findings} == {'forbidden-extension'}


# ----------------------------------------------------------- the verification
def test_the_verify_script_drives_the_editor_through_the_launcher():
    assert verify.EDITOR_EXE == builder.EDITOR_EXE
    assert verify.LAUNCHER_NAME == builder.LAUNCHER_NAME
    assert verify.CHECKER == 'tests/acceptance/accept_range.py'
    assert verify.checker_path().is_file()


def test_the_verify_script_asks_the_machine_whether_the_editor_is_running(tmp_path):
    """A windowed executable leaves its parent nothing to wait on, so the check has
    to be an observation: ``tasklist``, whose own process is in System32 - the only
    directory the cleaned environment keeps on PATH."""
    environment, _ = verify.clean_environment(tmp_path)
    found = verify.process_ids('python.exe', environment)
    assert found['returncode'] == 0
    assert str(os.getpid()) in found['pids']
    assert verify.process_ids('no-such-image.exe', environment)['pids'] == []


def test_run_launcher_keeps_the_one_json_line_contract_and_run_command_does_not(tmp_path):
    package = tmp_path
    (package / verify.LAUNCHER_NAME).write_text('@echo off\r\necho not json\r\n', encoding='ascii')
    environment, _ = verify.clean_environment(tmp_path)
    raw = verify.run_command(package, [], environment, timeout=60)
    assert raw['returncode'] == 0
    assert raw['stdout'].strip() == 'not json'
    assert 'parse_error' in verify.run_launcher(package, [], environment, timeout=60)


def test_the_gui_smoke_counts_each_of_its_own_checks(tmp_path):
    """No package here: the point is that the run refuses instead of reporting a
    green aggregate over nothing."""
    result = verify.gui_smoke(tmp_path, {'ComSpec': 'x'}, tmp_path, ROOT, 1)
    assert result['ok'] is False
    assert result['editor_exe_present'] is False


# ----------------------------------------------------------------- the readme
def test_the_readme_documents_both_ways_in_and_their_switch():
    assert '双击 `单词视频.cmd`' in readme
    assert '`单词视频.cmd` **带参数**' in readme
    assert '--editor' in readme
    for flag in ('--import', '--into', '--open', '--export', '--report'):
        assert flag in readme
    for marker in ('word_video_flow', 'PycharmProjects', 'runtime\\venv'):
        assert marker not in readme
    assert '还没有图形界面' not in readme


def test_the_readme_names_the_folders_the_package_actually_has():
    for part in (builder.APP_DIR, builder.EDITOR_DIR, builder.FFMPEG_DIR,
                 builder.LAUNCHER_NAME, builder.README_NAME):
        assert part in readme


def test_the_readme_still_tells_the_truth_about_being_unsigned_and_offline():
    assert '不联网' in readme
    assert 'SmartScreen' in readme
    assert re.search(r'不含任何密钥', readme)
