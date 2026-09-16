"""``python -m legacy_adapter``: the old factory, one JSON object per invocation.

Three verbs, chosen so that every question this task has to answer can be asked from
a terminal:

``doctor``
    Where is the old CLI, which interpreter runs it, what does *its own* ``doctor``
    say, and where would output and working directories go?  A support call starts
    here, and so does the packaged editor when it cannot find the old entry.
``calibers``
    Both timing calibers, as definitions **and as numbers**.  The old numbers come
    from the old core's own ``plan`` action (it renders into a temporary directory and
    publishes nothing), the new numbers from a delivered ``timeline.json`` - so the
    two figures a member is warned not to mix are printed side by side, each with the
    scope it belongs to.
``generate``
    Produce the five tracks per package.  This is the entry the editor's dialog calls.

Output is one UTF-8 JSON object on stdout, written as bytes (a Windows console with a
legacy code page must not be able to turn a Chinese message into a crash), and the
exit code is the failure class from :mod:`legacy_adapter.errors`.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import (CALIBER_LEGACY, CALIBER_MEDIA, EXIT_CODES, UI_NOTICE, VERSION,
               LegacyAdapterError, LegacyRequest, calibers, compare_runs,
               default_output_dir, default_python, default_work_dir,
               locate_legacy_entry, plan_legacy, run_legacy, timeline_caliber)
from .environment import LEGACY_SCRIPT_NAME, PROTECTED_ROOTS, work_root
from .errors import LEGACY_BAD_RESPONSE
from .request import DEFAULT_EXTRA, DEFAULT_FIRST_SIX, DEFAULT_TIMEOUT
from .runner import creation_flags, legacy_environment, write_json


class Parser(argparse.ArgumentParser):
    """Bad flags are a structured refusal too, not a usage dump on stderr."""

    def error(self, message):
        raise LegacyAdapterError('INVALID_REQUEST', message,
                                 details={'prog': self.prog})


def _common(parser, *, wordlist_required=False):
    parser.add_argument('--wordlist', required=wordlist_required,
                        help='词表文件（TXT / DOCX / 词表 SRT）；由旧核心读取')
    parser.add_argument('--start', type=int, help='起始序号（一基，含）')
    parser.add_argument('--end', type=int, help='结束序号（一基，含）；省略=到表尾')
    parser.add_argument('--batch-size', type=int, help='每包词数；省略=单包（旧核心规则）')
    parser.add_argument('--first-six', type=float, default=DEFAULT_FIRST_SIX,
                        help='旧口径前六个汉字每字秒数（默认 %.1f）' % DEFAULT_FIRST_SIX)
    parser.add_argument('--extra', type=float, default=DEFAULT_EXTRA,
                        help='旧口径超出六个汉字后每字秒数（默认 %.1f）' % DEFAULT_EXTRA)
    parser.add_argument('--timeline', default='',
                        help='新口径的 timeline.json（或它的运行/工程目录），用来打印另一种口径的数值')
    parser.add_argument('--legacy-script', default='', help='旧 subtitle_factory_cli.py 的路径')
    parser.add_argument('--legacy-python', default='', help='运行旧 CLI 的解释器')
    parser.add_argument('--report', default='', help='把本次的 JSON 同时写到这个文件')
    return parser


def build_parser():
    parser = Parser(prog='python -m legacy_adapter',
                    description='旧字幕工厂的新软件入口：独立子进程调用旧 CLI，'
                                '输出与工作目录都在 D 盘新目录，旧核心一字不改。')
    sub = parser.add_subparsers(dest='action', required=True)

    doctor = sub.add_parser('doctor', help='检查旧 CLI、解释器、默认目录与两种口径')
    doctor.add_argument('--legacy-script', default='')
    doctor.add_argument('--legacy-python', default='')
    doctor.add_argument('--report', default='')

    _common(sub.add_parser('calibers', help='两种计时口径：定义 + 实际数值'))

    generate = _common(sub.add_parser('generate', help='用旧核心产出五轨 SRT（每包一组）'),
                       wordlist_required=True)
    generate.add_argument('--output', default='', help='输出目录（默认 D 盘工作根的 out/legacy/<run>）')
    generate.add_argument('--work-dir', default='', help='旧 CLI 的工作目录（默认 D 盘新目录）')
    generate.add_argument('--idempotency-key', default='', help='交给旧核心的键；同键同内容复用')
    generate.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                          help='旧 CLI 的超时秒数（默认 %.0f）' % DEFAULT_TIMEOUT)

    compare = sub.add_parser('compare', help='逐字节/逐毫秒对照两次运行的五轨 SRT')
    compare.add_argument('--left', required=True, help='旧入口产出：运行报告 JSON、旧 CLI stdout，或运行目录')
    compare.add_argument('--right', required=True, help='新入口产出：同上')
    compare.add_argument('--limit', type=int, default=20, help='每条轨道最多列出多少处差异')
    compare.add_argument('--report', default='')
    return parser


def _request(args, *, output='', work_dir=''):
    return LegacyRequest(wordlist=getattr(args, 'wordlist', '') or '',
                         start=getattr(args, 'start', None),
                         end=getattr(args, 'end', None),
                         batch_size=getattr(args, 'batch_size', None),
                         first_six=getattr(args, 'first_six', DEFAULT_FIRST_SIX),
                         extra=getattr(args, 'extra', DEFAULT_EXTRA),
                         output=output, work_dir=work_dir,
                         timeline=getattr(args, 'timeline', '') or '',
                         idempotency_key=getattr(args, 'idempotency_key', '') or '',
                         legacy_script=getattr(args, 'legacy_script', '') or '',
                         legacy_python=getattr(args, 'legacy_python', '') or '',
                         timeout=getattr(args, 'timeout', DEFAULT_TIMEOUT))


def legacy_doctor(entry, python, timeout=120.0):
    """The old CLI's own ``doctor``, run as a subprocess and quoted verbatim."""
    import subprocess
    import time

    work = Path(default_work_dir())
    work.mkdir(parents=True, exist_ok=True)
    argv = [python, '-B', str(entry), 'doctor']
    started = time.perf_counter()
    completed = subprocess.run(argv, cwd=str(work), env=legacy_environment(work),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout, check=False, creationflags=creation_flags())
    seconds = round(time.perf_counter() - started, 3)
    text = (completed.stdout or b'').decode('utf-8', 'replace').strip()
    try:
        answer = json.loads(text)
    except ValueError:
        raise LegacyAdapterError(
            LEGACY_BAD_RESPONSE, '旧 CLI 的 doctor 没有输出可解析的 JSON。',
            details={'returncode': completed.returncode, 'stdout': text[:500],
                     'stderr': (completed.stderr or b'').decode('utf-8', 'replace')[:500],
                     'legacy_entry': str(entry)}) from None
    return {'argv': argv, 'returncode': completed.returncode, 'seconds': seconds,
            'result': answer.get('result') if answer.get('ok') else None,
            'error': None if answer.get('ok') else answer.get('error')}


def command_doctor(args):
    report = {'ok': True, 'exit_code': 0, 'action': 'doctor', 'adapter_version': VERSION,
              'notice': UI_NOTICE,
              'adapter': {'python': sys.executable, 'frozen': bool(getattr(sys, 'frozen', False)),
                          'os': os.name, 'work_root': str(work_root()),
                          'protected_roots': list(PROTECTED_ROOTS)},
              'defaults': {'output': default_output_dir(), 'work_dir': default_work_dir()},
              'calibers': calibers(), 'legacy': {}, 'problems': []}
    entry = None
    try:
        entry = locate_legacy_entry(args.legacy_script)
    except LegacyAdapterError as error:
        report['problems'].append(error.to_dict())
    python = str(args.legacy_python or '').strip() or default_python()
    report['legacy'].update({'entry': None if entry is None else str(entry),
                             'script_name': LEGACY_SCRIPT_NAME,
                             'python': python,
                             'python_exists': bool(python) and Path(python).is_file()})
    if entry is not None and report['legacy']['python_exists']:
        try:
            report['legacy']['doctor'] = legacy_doctor(entry, python)
        except LegacyAdapterError as error:
            report['problems'].append(error.to_dict())
    if report['problems']:
        report['ok'] = False
        report['exit_code'] = report['problems'][0]['exit_code']
    return report


def command_calibers(args):
    report = {'ok': True, 'exit_code': 0, 'action': 'calibers', 'adapter_version': VERSION,
              'notice': UI_NOTICE, 'calibers': calibers(),
              'legacy_numbers': None, 'media_numbers': None, 'comparison': None,
              'problems': []}
    if args.wordlist:
        try:
            planned = plan_legacy(_request(args))
        except LegacyAdapterError as error:
            report['legacy_numbers'] = error.to_dict()
            report['problems'].append(error.to_dict())
            report['ok'] = False
            report['exit_code'] = error.exit_code
        else:
            measured = planned['caliber']['measured']
            report['legacy_numbers'] = {
                'available': True, 'id': CALIBER_LEGACY,
                'label': planned['caliber']['label'],
                'source': planned['caliber']['source'],
                'formula': planned['caliber']['formula'],
                'scope': {'wordlist': planned['request']['wordlist'],
                          'selection': planned['request']['resolved'].get('selection'),
                          'selection_label': planned['request']['resolved'].get('selection_label'),
                          'batch_size': planned['request'].get('batch_size'),
                          'timing': measured['timing'],
                          'total_count': planned['counts']['total_count'],
                          'selected_count': planned['counts']['selected_count'],
                          'package_count': planned['counts']['package_count']},
                'packages': measured['packages'],
                'total_duration_ms': measured['total_duration_ms'],
                'work_dir': planned['adapter']['work_dir'],
                'note': '数值来自旧核心自己的 plan（只在临时目录渲染，未产出文件）'}
    else:
        report['legacy_numbers'] = {'available': False,
                                    'reason': '没有指定 --wordlist，无法用旧核心复算旧口径数值'}
    report['media_numbers'] = timeline_caliber(args.timeline) \
        if str(args.timeline or '').strip() else {
            'available': False, 'reason': '没有指定 --timeline，读不到新口径的实际数值',
            'id': CALIBER_MEDIA, 'timeline': '', 'measured': None}
    old, new = report['legacy_numbers'], report['media_numbers']
    if old and old.get('available') and new and new.get('available'):
        legacy_scope, media_scope = old['scope'], new['measured']
        same = (legacy_scope['selected_count'] == media_scope['word_count'])
        report['comparison'] = {
            'legacy_total_ms': old['total_duration_ms'],
            'media_total_ms': media_scope['total_duration_ms'],
            'delta_ms': old['total_duration_ms'] - media_scope['total_duration_ms'],
            'same_selection': bool(same),
            'scope_note': ('两种口径各说各的：旧口径按 %.1f/%.1f 秒每字的文字规则，'
                           '新口径按已交付时间线的音频实际时长；'
                           '数值只在“同一词表同一选区”时可比。'
                           % (old['scope']['timing']['first_six'], old['scope']['timing']['extra'])),
            'legacy_words': '%s～%s' % (old['packages'][0]['first_word'],
                                        old['packages'][-1]['last_word']) if old['packages'] else '',
            'media_words': '%s～%s' % (media_scope['first_word'], media_scope['last_word'])}
    return report


def command_generate(args):
    request = _request(args, output=args.output, work_dir=args.work_dir)
    report = run_legacy(request, action='generate')
    report.update({'exit_code': 0, 'notice': UI_NOTICE})
    return report


def command_compare(args):
    report = compare_runs(args.left, args.right, limit=args.limit)
    identical = report['verdict'] == 'identical'
    report.update({'ok': identical, 'exit_code': 0 if identical else 4,
                   'action': 'compare', 'adapter_version': VERSION, 'notice': UI_NOTICE,
                   'left': str(args.left), 'right': str(args.right)})
    return report


COMMANDS = {'doctor': command_doctor, 'calibers': command_calibers,
            'generate': command_generate, 'compare': command_compare}


def emit(payload):
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n'
    try:
        sys.stdout.buffer.write(raw.encode('utf-8'))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return 0


def main(argv=None):
    action = None
    try:
        args = build_parser().parse_args(argv)
        action = args.action
        payload = COMMANDS[action](args)
        if getattr(args, 'report', ''):
            write_json(args.report, payload)
        code = int(payload.get('exit_code', 0 if payload.get('ok') else 1))
    except LegacyAdapterError as error:
        payload = {'ok': False, 'action': action, 'adapter_version': VERSION,
                   'error': error.to_dict(), 'notice': UI_NOTICE,
                   'exit_code': error.exit_code}
        code = error.exit_code
    except KeyboardInterrupt:
        payload = {'ok': False, 'action': action, 'adapter_version': VERSION,
                   'error': LegacyAdapterError('INTERRUPTED', '操作被取消。').to_dict()}
        code = EXIT_CODES['INTERRUPTED']
    except Exception as error:                      # noqa: BLE001 - one JSON always
        import traceback
        payload = {'ok': False, 'action': action, 'adapter_version': VERSION,
                   'error': {'ok': False, 'code': 'INTERNAL_ERROR',
                             'message': '%s: %s' % (type(error).__name__, error),
                             'details': {'traceback': traceback.format_exc()[-2000:]},
                             'hint': '', 'exit_code': 1}}
        code = 1
    emit(payload)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
