"""The editor as a program: the window a member opens, and that window under script control.

Two ways in, one implementation
-------------------------------
* **a member** runs the packaged launcher with no arguments.  This module creates the
  Qt application and shows :class:`~desktop.editor_window.EditorWindow`; importing a
  word list and delivering the three products are then the window's own 文件 menu
  items, exactly as W06 acceptance measured them;
* **a script or a support call** passes the same launcher
  ``--editor --import <request.json> --into <dir> --export --report <out.json>``.
  This module then drives *that same window* - the menu's own
  ``EditorWindow.import_request`` and the 导出 button's own ``EditorWindow.export``,
  with the Qt event loop running - and writes a JSON record of what happened.

There is deliberately no second import or export implementation here: a scripted run
is evidence about the window only because it goes through the window.  Nothing in
this file changes what the editor does; it starts it, and it is the only file in
``desktop/`` that decides the process should end.

Why a script route exists at all
--------------------------------
The member's route needs two folder pickers, so it cannot be re-run unattended on a
machine that is being checked for "does the package work here".  The flags below are
the same steps without the dialogs, which is also what makes a support call ("run
this and send me the JSON") possible without a debugger.

A frozen windowed build has ``sys.stdout`` set to ``None``, so this module never
writes to it: progress and failures go into the report file and, when there is one,
to stderr.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

#: Read as early as the frozen bootstrap allows, so "how long until the member sees a
#: window" is measured from the entry itself rather than from a later step.  The
#: bootloader and the interpreter start before this line, so the number under-counts
#: the real cold start; the verifier measures the launcher round trip as well, and
#: both numbers are reported rather than one being presented as the other.
ENTRY_START = time.perf_counter()

WINDOW_SIZE = (1280, 840)
LIVE_REPORT_DELAY_MS = 1200
DEFAULT_EXPORT_TIMEOUT = 5400.0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='WordVideoEditor',
        description='单词教学视频编辑器（打开窗口；带参数时按脚本驱动并输出 JSON 记录）')
    parser.add_argument('--editor', action='store_true',
                        help='打开编辑器窗口；不带其它参数时这就是默认行为'
                             '（启动器会显式加上这个开关，以便和命令行入口区分）')
    parser.add_argument('--import', dest='request', metavar='REQUEST.JSON',
                        help='从请求 JSON 导入一个工程（需要 --into）')
    parser.add_argument('--into', dest='into', metavar='DIR',
                        help='--import 要写入的工程目录（不存在则创建）')
    parser.add_argument('--open', dest='open_dir', metavar='DIR',
                        help='打开一个已存在的工程目录')
    parser.add_argument('--export', action='store_true',
                        help='导入/打开之后立刻导出三产物并退出（退出码 0/1）')
    parser.add_argument('--output', dest='output', metavar='DIR',
                        help='导出位置（默认 <工程目录>\\out）')
    parser.add_argument('--report', dest='report', metavar='FILE',
                        help='把本次运行的 JSON 记录写到这个文件'
                             '（先写临时文件再改名，读的人不会看到半截）')
    return parser


def parse_args(argv=None):
    """Parse an explicit argv so a test can exercise this without spawning a process."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.request and not args.into:
        parser.error('--import 需要 --into')
    if args.request and args.open_dir:
        parser.error('--import 与 --open 只能用一个')
    if args.export and not (args.request or args.open_dir):
        parser.error('--export 需要 --import 或 --open')
    if args.output and not args.export:
        parser.error('--output 只在 --export 时有用；不加 --export 请去掉它')
    return args


def write_report(path, payload):
    """Write the report so a reader polling the file never sees a partial one."""
    if not path:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temp, target)
    return target


def release_inherited_console():
    """Give back the console handles this process inherited from whoever started it.

    Measured problem, not a precaution.  The launcher starts the window with
    ``start``, and Windows hands a new process *every* inheritable handle of its
    parent - including the write end of the pipe a script used to capture the
    launcher's output.  ``cmd.exe`` exits immediately afterwards, but that stray
    handle keeps the pipe open, so the caller never sees EOF: its ``communicate()``
    waits, and even a timeout cannot save it (``subprocess.run`` drains the pipes
    again after killing the child).  A windowed editor has nothing to say on stdout
    anyway, so dropping the handles here is what makes "just run the launcher" safe
    to script.

    Only called on the interactive path: a driven run *does* write its report to
    stderr when a caller gave it one, and that is worth keeping.
    """
    if os.name != 'nt':
        return []
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        closed = []
        for constant in (-10, -11, -12):                    # stdin, stdout, stderr
            handle = kernel32.GetStdHandle(constant)
            if handle and kernel32.CloseHandle(handle):
                closed.append(constant)
        # Python's wrappers would now be writing to a closed handle; ``None`` is what
        # a windowed build has from the start, and ``print`` treats it as a no-op.
        sys.stdout = None
        sys.stderr = None
        return closed
    except Exception:                                       # noqa: BLE001 - never fatal
        return []


def environment_facts():
    """What the process was handed; the PATH/TEMP rules are acceptance items."""
    return {'frozen': bool(getattr(sys, 'frozen', False)),
            'executable': sys.executable,
            'package_dir': str(Path(sys.executable).parent),
            'cwd': os.getcwd(),
            'python': sys.version.split()[0],
            'PATH': os.environ.get('PATH', ''),
            'TEMP': os.environ.get('TEMP', ''),
            'TMP': os.environ.get('TMP', ''),
            'PYTHONHOME': os.environ.get('PYTHONHOME'),
            'PYTHONPATH': os.environ.get('PYTHONPATH'),
            'tts_credentials_present': sorted(
                name for name in os.environ
                if 'TTS' in name.upper() or 'VOLC' in name.upper())}


def window_facts(window, *, seconds_to_window):
    """The window's own state, read from the window - not a re-derived opinion of it.

    ``seconds_to_window`` is measured immediately after ``show()`` returns and Qt has
    processed its events, so it is *the member's* startup cost and nothing else.  The
    import, the first preview and the delivery are timed separately: averaging them
    into "startup" is how a slow first render gets reported as a slow editor.
    """
    diagnostics = window.diagnostics()
    session = window.showing.session
    return {'entry_to_window_seconds': round(seconds_to_window, 3),
            'window_shown': window.isVisible(),
            'window_title': window.windowTitle(),
            'window_size': [window.width(), window.height()],
            'canvas_present': window.canvas is not None,
            'timeline': {'rows': diagnostics['timeline'].get('rows'),
                         'bars': len(window.timeline.items()),
                         'width': diagnostics['timeline'].get('width')},
            'preview': {'has_session': diagnostics['has_session'],
                        'open_seconds': None if session is None else session.open_seconds,
                        'canvas': None if session is None else '%dx%d' % (session.canvas_width,
                                                                          session.canvas_height),
                        'error': diagnostics['preview_error']},
            'diagnostics': diagnostics}


def run_import(window, args):
    """Import or open through the window's own methods, and report what it said."""
    started = time.perf_counter()
    if not args.request and not args.open_dir:
        return {'skipped': True}
    if args.request:
        folder = window.import_request(str(Path(args.request).resolve()),
                                       str(Path(args.into).resolve()))
    else:
        folder = window.open_folder(str(Path(args.open_dir).resolve()))
    fact = {'request': args.request, 'into': args.into, 'open': args.open_dir,
            'seconds': round(time.perf_counter() - started, 3), 'ok': folder is not None,
            'messages': list(window.last_notice_texts())}
    if folder is not None:
        fact.update({'project_dir': str(folder.path),
                     'records': len(folder.project.records),
                     'clips': len(folder.project.clips),
                     'revision': folder.project.revision})
    return fact


def run_export(window, application, timeout=DEFAULT_EXPORT_TIMEOUT, output=''):
    """Press 导出 and wait for the worker, pumping the event loop exactly as a UI does."""
    started = time.perf_counter()
    returned = window.export(output=output or '')
    deadline = time.monotonic() + timeout
    while window.diagnostics()['exporting'] and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    application.processEvents()
    outcome = window.last_export
    fact = {'started': returned is not None,
            'seconds_wall': round(time.perf_counter() - started, 3),
            'timeout': window.diagnostics()['exporting'],
            'messages': list(window.last_notice_texts())}
    if outcome is None:
        fact['ok'] = False
        fact['reason'] = ('导出在开始前被拦下' if returned is None
                          else '导出没有在 %.0f 秒内结束' % timeout)
        return fact
    fact.update(outcome.to_dict())
    fact['files'] = sorted(str(path.relative_to(outcome.run_dir)).replace('\\', '/')
                           for path in Path(outcome.run_dir).rglob('*')
                           if path.is_file()) if outcome.run_dir else []
    return fact


def main(argv=None):
    args = parse_args(argv)
    report_path = args.report
    payload = {'mode': '导出' if args.export else '交互',
               'started': time.strftime('%Y-%m-%d %H:%M:%S'),
               'argv': list(sys.argv),
               'arguments': {'request': args.request, 'into': args.into,
                             'open': args.open_dir, 'export': args.export,
                             'output': args.output},
               'environment': environment_facts(),
               'ok': False}
    try:
        return _run(args, payload, report_path)
    except BaseException as error:                      # noqa: BLE001 - into the report
        import traceback
        payload['error'] = '%s: %s' % (type(error).__name__, error)
        payload['traceback'] = traceback.format_exc()[-4000:]
        write_report(report_path, payload)
        if sys.stderr is not None:
            traceback.print_exc()
        return 3


def _run(args, payload, report_path):
    from PySide6 import QtCore, QtWidgets

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    application.setApplicationName('单词教学视频编辑器')
    from desktop.editor_window import EditorWindow

    window = EditorWindow()
    window.resize(*WINDOW_SIZE)
    window.show()
    application.processEvents()
    # Taken here, before anything is imported or rendered: this is the number that
    # answers "how long until the member sees a window".
    window_shown_seconds = time.perf_counter() - ENTRY_START
    payload['qt'] = {'platform': application.platformName(),
                     'qt': QtCore.qVersion(), 'pyside': QtCore.__version__,
                     'device_pixel_ratio': window.devicePixelRatioF()}

    payload['import'] = run_import(window, args)
    payload.update(window_facts(window, seconds_to_window=window_shown_seconds))
    if args.request or args.open_dir:
        payload['ok'] = bool(payload['import'].get('ok'))
        if not payload['ok']:
            write_report(report_path, payload)
            if sys.stderr is not None:
                print(json.dumps(payload, ensure_ascii=False, indent=1), file=sys.stderr)
            return 1

    if args.export:
        payload['export'] = run_export(window, application, output=args.output)
        payload['ok'] = bool(payload['export'].get('ok'))
        payload['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
        write_report(report_path, payload)
        if sys.stderr is not None:
            print(json.dumps(payload, ensure_ascii=False, indent=1), file=sys.stderr)
        window.close()
        application.processEvents()
        return 0 if payload['ok'] else 1

    # Interactive: the window is the point.  The report is written once now and once
    # from inside the running event loop, so a caller polling the file can tell "the
    # window is up" apart from "the window is up and Qt is dispatching events".
    payload['ok'] = True
    write_report(report_path, payload)
    payload['console_released'] = release_inherited_console()
    ticks = {'count': 0}
    beat = QtCore.QTimer()
    beat.setInterval(100)
    beat.timeout.connect(lambda: ticks.__setitem__('count', ticks['count'] + 1))
    beat.start()

    def live():
        payload['event_loop'] = {'ticks': ticks['count'],
                                 'seconds': round(time.perf_counter() - ENTRY_START, 3),
                                 'window_visible': window.isVisible()}
        write_report(report_path, payload)

    QtCore.QTimer.singleShot(LIVE_REPORT_DELAY_MS, live)
    return application.exec()


if __name__ == '__main__':
    sys.exit(main())
