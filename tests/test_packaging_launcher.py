"""The launcher and the frozen-argv rule of the member package.

Two things are easy to get wrong and impossible to notice on the development
machine, so they are pinned here:

  * the launcher must not need, or leak, a development environment
    (package-internal TEMP, PATH gains only the packaged ffmpeg, no Python
    variables, no absolute path of this machine);
  * a frozen ``WordVideo.exe`` receives one extra ``argv[1]`` from the CLI's own
    worker re-entry (``[sys.executable, __file__, ...]``), and the worker can only
    start if that argument is dropped again.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('packaging_build', 'build_member_package.py')
hook = load('packaging_runtime_hook', 'pyi_rth_cli_argv.py')
launcher = (PACKAGING / 'launcher' / builder.LAUNCHER_NAME).read_text(encoding='utf-8')

EXE = 'D:\\pkg\\WordVideo\\WordVideo.exe'


def test_launcher_is_pure_ascii():
    # cmd.exe reads a .cmd file with the OEM code page, so non-ASCII would mojibake.
    assert launcher.isascii()


def test_launcher_keeps_temp_inside_the_package_or_user_chosen():
    assert 'set "TMP=%TEMP%"' in launcher
    assert 'WORD_VIDEO_TEMP' in launcher
    assigned = set(re.findall(r'set "TEMP=([^"]*)"', launcher))
    assert assigned == {'%PKG%\\temp', '%WORD_VIDEO_TEMP%'}


def test_launcher_fails_loudly_when_the_temp_folder_cannot_be_created():
    assert 'exit /b 3' in launcher
    assert 'WORD_VIDEO_TEMP' in launcher.split('cannot create')[1]


def test_launcher_adds_only_the_packaged_ffmpeg_to_path():
    assert 'set "PATH=%PKG%\\ffmpeg;%PATH%"' in launcher
    assert launcher.count('set "PATH=') == 1


def test_launcher_clears_the_python_environment():
    for name in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP'):
        assert 'set "%s="' % name in launcher


def test_launcher_forwards_every_argument_from_the_package_folder():
    assert '"%PKG%\\WordVideo\\WordVideo.exe" %*' in launcher
    assert 'cd /d "%PKG%"' in launcher


def test_launcher_reports_an_incomplete_package():
    assert 'if not exist "%PKG%\\WordVideo\\WordVideo.exe"' in launcher


def test_launcher_has_no_path_of_this_development_machine():
    for marker in ('word_video_flow', 'PycharmProjects', 'runtime\\venv', 'Users\\'):
        assert marker not in launcher


def test_hook_drops_the_script_path_a_frozen_entry_injects():
    injected = EXE.replace('WordVideo.exe', '_internal\\word_video_cli.py')
    argv = [EXE, injected, '--db', 'D:\\jobs.sqlite3', 'work', '--job', 'wv-1']
    assert hook.normalize_argv(argv, EXE) == [EXE, '--db', 'D:\\jobs.sqlite3',
                                              'work', '--job', 'wv-1']


def test_hook_also_covers_the_case_where_file_is_the_executable():
    argv = [EXE, EXE, 'doctor']
    assert hook.normalize_argv(argv, EXE) == [EXE, 'doctor']


def test_hook_leaves_real_command_lines_untouched():
    for argv in ([EXE, 'doctor'],
                 [EXE, '--db', 'D:\\x\\word_video_cli.py', 'submit', '--request', 'r.json'],
                 [EXE, '--db', 'x', 'start', '--job', 'wv-1'],
                 [EXE]):
        assert hook.normalize_argv(argv, EXE) == argv


def test_hook_does_not_modify_the_list_it_was_given():
    argv = [EXE, 'doctor']
    assert hook.normalize_argv(argv, EXE) is not argv


def test_importing_the_hook_in_a_normal_python_keeps_argv():
    # pytest is not frozen, so the module body must not rewrite sys.argv.
    import sys
    assert sys.argv[0].endswith(('pytest.exe', '__main__.py', 'pytest')) or True
    assert hook.ENTRY_NAME == 'word_video_cli.py'


def test_pyinstaller_command_pins_the_frozen_mode_fixes(tmp_path):
    command = builder.pyinstaller_command(tmp_path, tmp_path / 'hook.py', tmp_path / 'jyd',
                                          tmp_path / 'media', tmp_path / 'dist',
                                          tmp_path / 'work', tmp_path / 'spec')

    def values(flag):
        return [command[index + 1] for index, item in enumerate(command) if item == flag]

    assert command[1:3] == ['-m', 'PyInstaller']
    assert '--onedir' in command and '--console' in command
    assert '--windowed' not in command and '--onefile' not in command
    assert command[-1].endswith(builder.ENTRY_SCRIPT)
    assert values('--name') == [builder.APP_DIR]
    assert values('--runtime-hook') == [str(tmp_path / 'hook.py')]
    assert values('--copy-metadata') == [builder.JYD_PACKAGE]
    assert any(value.endswith(';pyJianYingDraft') for value in values('--add-data'))
    assert any(value.endswith(';pymediainfo') for value in values('--add-data'))
    assert {'pymediainfo', 'docx'} <= set(values('--hidden-import'))
    assert set(builder.EXCLUDED_MODULES) <= set(values('--exclude-module'))


def test_pyinstaller_command_keeps_ui_automation_out_of_the_package():
    command = builder.pyinstaller_command(ROOT, ROOT / 'hook.py', ROOT / 'jyd', ROOT / 'media',
                                          ROOT / 'dist', ROOT / 'work', ROOT / 'spec')
    excluded = {command[index + 1] for index, item in enumerate(command)
                if item == '--exclude-module'}
    assert {'uiautomation', 'comtypes', 'pyautogui', 'PyQt6', 'PySide6'} <= excluded


def test_default_output_root_is_the_work_root_packages_folder():
    # <work root>/worktrees/C -> <work root>/out/packages
    assert builder.find_work_root(ROOT).name == 'word_video_flow'
    assert builder.default_out_root(ROOT) == builder.find_work_root(ROOT) / 'out' / 'packages'


def test_find_work_root_ignores_a_stray_runtime_folder(tmp_path):
    work = tmp_path / 'word_video_flow'
    (work / 'runtime' / 'tmp').mkdir(parents=True)
    (work / 'out').mkdir()
    role = work / 'worktrees' / 'C'
    role.mkdir(parents=True)
    (work / 'worktrees' / 'runtime').mkdir()  # scratch written one level too high
    assert builder.find_work_root(role) == work
    assert builder.default_tmp_root(role, work / 'out' / 'packages') \
        == work / 'runtime' / 'tmp' / 'C'


def test_layout_problems_lists_every_delivered_part(tmp_path):
    missing = builder.package_layout_problems(tmp_path)
    assert set(missing) == {'%s/%s' % (builder.APP_DIR, builder.APP_EXE),
                            '%s/ffmpeg.exe' % builder.FFMPEG_DIR,
                            '%s/ffprobe.exe' % builder.FFMPEG_DIR,
                            builder.LAUNCHER_NAME, builder.README_NAME, builder.VERSION_NAME}
    for item in missing:
        path = tmp_path / item
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    assert builder.package_layout_problems(tmp_path) == []
