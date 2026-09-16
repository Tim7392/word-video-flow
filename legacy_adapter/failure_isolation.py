"""W10's failure isolation, reproduced: each entry keeps working while the other fails.

The task asks for two minimal counterexamples, and "minimal" is the hard part - it is
easy to *assert* that a broken renderer cannot break the old factory; what makes it
evidence is a run that fails for a real reason and a second run that still succeeds
afterwards, on the same machine, in the same minute.

**Direction 1 - the old entry fails, the new one is unaffected.**  The old entry fails
twice over: a word list that is not there (this adapter refuses before spawning
anything, ``INPUT_ERROR``) and a word list the old core cannot read (the old CLI itself
exits non-zero, and its own code and details are passed through unchanged).  Afterwards
the new entry builds and exports a project as if nothing had happened.

**Direction 2 - the new entry fails, the old one is unaffected.**  The project's
``assets.json`` registers one recording at a path that does not exist, which is the
missing-media case a member actually hits; the new export refuses with
``ASSET_FILE_MISSING`` and publishes nothing.  Afterwards the old entry produces five
tracks for a three-word selection, and - the strongest form of the same statement - it
still produces them in a process where the whole new engine is blocked from importing
(``sitecustomize`` on ``PYTHONPATH``), reporting the missing new caliber as a label
rather than as a failure.

Everything runs as a **subprocess of this module**, which itself never imports
``word_video``: the failures and the successes are separate processes, so nothing here
can be an artefact of shared state inside one interpreter.  The evidence is one JSON
document at a stable path (``out/reports/gate-legacy-isolation.json``), and every step
in it carries what was expected, what actually happened, and the exit code that proves
the run really failed or really succeeded.

    python -m legacy_adapter.failure_isolation
    python -m legacy_adapter.failure_isolation --report D:\\...\\out\\reports\\gate-legacy-isolation.json

Exit code 0 means every check held; 1 means at least one did not, and the report names
which.  Nothing here is skipped when a file is missing: a missing input is a failed
check with the path in it.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .environment import default_python, locate_legacy_entry, new_run_dir, work_root
from .errors import INPUT_ERROR, LegacyAdapterError
from .report import parse_track, read_text, sha256
from .runner import creation_flags, legacy_environment, write_json

SCHEMA = 'wv-legacy-isolation@1'

#: Where the isolation evidence goes unless the caller says otherwise.
DEFAULT_REPORT = work_root() / 'out' / 'reports' / 'gate-legacy-isolation.json'

#: The same read-only word list and selection the equivalence gate uses: the point is
#: that the old entry still produces a real, byte-checkable batch after the new side
#: failed - three words keep that honest and cheap.
DEFAULT_WORDLIST = Path(os.environ.get('WORD_VIDEO_ACCEPTANCE_WORDLIST')
                        or (work_root() / 'data' / 'wordlists' / '四级核心1500词_已清理.txt'))
OLD_ENTRY_START, OLD_ENTRY_END, OLD_ENTRY_BATCH = 301, 303, 3
#: Cue counts the old rule produces for three words: track 01 repeats each word once
#: (2 cues per word), tracks 02-05 once each.  Pinned because it is the shape of the
#: artefact, not a number this script derived from the run it is judging.
EXPECTED_CUES = [6, 3, 3, 3, 3]
#: The one asset the missing-media counterexample breaks (three-word fixture, word 151).
BROKEN_ASSET = 'w151:female'

BLOCKER_SOURCE = '''"""Make the new engine unimportable in this process - and only in this one."""
import sys


class _Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "word_video" or fullname.startswith("word_video."):
            raise ImportError("blocked on purpose: the new video software is down")
        return None


sys.meta_path.insert(0, _Block())
'''


def repo_root():
    """The work tree this module was loaded from: the cwd every subprocess runs in."""
    return Path(__file__).resolve().parent.parent


def run_subprocess(argv, *, cwd, env=None, timeout=900):
    """One child process, captured the same way everywhere in this module."""
    started = time.perf_counter()
    completed = subprocess.run([str(part) for part in argv], cwd=str(cwd), env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                               check=False, creationflags=creation_flags())
    return {'argv': [str(part) for part in argv], 'cwd': str(cwd),
            'exit_code': completed.returncode,
            'seconds': round(time.perf_counter() - started, 3),
            'stdout': (completed.stdout or b'').decode('utf-8', 'replace'),
            'stderr': (completed.stderr or b'').decode('utf-8', 'replace')[-4000:]}


def last_json(text):
    """The one JSON object a CLI printed, or ``None`` (a message is not an answer)."""
    stripped = str(text or '').strip()
    if not stripped:
        return None
    for candidate in (stripped, *reversed([line.strip() for line in stripped.splitlines()
                                           if line.strip().startswith('{')])):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def check(name, expected, actual):
    """One expectation, kept next to what actually happened - readable in the report."""
    return {'check': name, 'expected': expected, 'actual': actual, 'ok': expected == actual}


def fold_checks(steps):
    """Every step's checks as one verdict: what failed, in the words of the check."""
    failed = [{'step': row['name'], 'check': item['check'], 'expected': item['expected'],
               'actual': item['actual']}
              for row in steps for item in row['checks'] if not item['ok']]
    return {'failed': failed,
            'checks': sum(len(row['checks']) for row in steps),
            'failed_checks': len(failed),
            'problems': ['%s：%s（期望 %r，实际 %r）' % (row['step'], row['check'],
                                                        row['expected'], row['actual'])
                         for row in failed]}


def published(directory, pattern='*.srt'):
    """Every published track under a directory, in stable order."""
    root = Path(directory)
    return sorted(root.rglob(pattern)) if root.is_dir() else []


def _adapter_generate(area, wordlist, *, tag, env=None, timeline=''):
    argv = [sys.executable, '-m', 'legacy_adapter', 'generate',
            '--wordlist', str(wordlist), '--start', str(OLD_ENTRY_START), '--end', str(OLD_ENTRY_END),
            '--batch-size', str(OLD_ENTRY_BATCH),
            '--output', str(area / ('%s-out' % tag)), '--work-dir', str(area / ('%s-work' % tag))]
    if timeline:
        argv.extend(['--timeline', str(timeline)])
    return run_subprocess(argv, cwd=repo_root(), env=env)


def _tracks_of(report):
    """``[{track, name, bytes, sha256, cues}]`` for the tracks a run report published."""
    rows = []
    for item in report.get('tracks') or ():
        path = Path(item['path'])
        cues = parse_track(read_text(path)) if path.is_file() else []
        rows.append({'track': item.get('track'), 'name': path.name,
                     'bytes': path.stat().st_size if path.is_file() else None,
                     'sha256': sha256(path) if path.is_file() else None, 'cues': len(cues),
                     'recorded_sha256': item.get('recorded_sha256')})
    return rows


# -- direction 1: the old entry fails, the new one does not -------------------

def step_old_entry_missing_wordlist(area):
    """The word list is not there: refused by code, before any child is started."""
    missing = Path(area) / 'not-a-wordlist.txt'
    result = run_subprocess(
        [sys.executable, '-m', 'legacy_adapter', 'generate', '--wordlist', str(missing),
         '--start', '1', '--end', '3', '--batch-size', '3',
         '--output', str(area / 'old-missing-out'), '--work-dir', str(area / 'old-missing-work')],
        cwd=repo_root())
    payload = last_json(result['stdout'])
    error = (payload or {}).get('error') or {}
    files = published(area / 'old-missing-out')
    return {
        'name': 'old_entry_missing_wordlist',
        'why': '旧入口的第一种失败：词表文件不存在，适配器在启动任何子进程之前就按 code 拒绝。',
        'command': result, 'evidence': {'error': error, 'published_tracks': len(files)},
        'checks': [
            check('exit_code', 3, result['exit_code']),
            check('stdout 是一个 JSON 对象', True, payload is not None),
            check('error.code', 'INPUT_ERROR', error.get('code')),
            check('error.details.field', 'wordlist', (error.get('details') or {}).get('field')),
            check('没有产出任何 SRT（拒绝发生在运行之前）', 0, len(files)),
        ]}


def step_old_cli_returns_nonzero(area):
    """The old CLI itself fails: a word list it cannot read, called directly."""
    entry = locate_legacy_entry()
    work = Path(area) / 'old-direct-work'
    work.mkdir(parents=True, exist_ok=True)
    broken = Path(area) / 'broken.txt'
    broken.write_text('!!! not a word list\n', encoding='utf-8')
    request = {'action': 'generate', 'source': {'path': str(broken)},
               'range': {'start': 1, 'end': 3}, 'batch_size': 3,
               'output': str(area / 'old-direct-out'),
               'timing': {'first_six': 0.4, 'extra': 0.2}}
    request_file = write_json(work / 'legacy-request.json', request)
    result = run_subprocess([sys.executable, '-B', str(entry), 'request',
                             '--json-file', str(request_file)],
                            cwd=work, env=legacy_environment(work))
    payload = last_json(result['stdout'])
    error = (payload or {}).get('error') or {}
    return {
        'name': 'old_cli_returns_nonzero',
        'why': '旧入口的第二种失败：词表内容不是词表，旧 CLI 自己以非零码结束并给出它自己的 code。'
               '这一跑不经过适配器，是"旧工具自身失败"的原始事实。',
        'command': result,
        'evidence': {'legacy_entry': str(entry), 'request_file': str(request_file),
                     'response_ok': (payload or {}).get('ok'), 'error': error,
                     'published_tracks': len(published(area / 'old-direct-out'))},
        'checks': [
            check('旧 CLI 退出码非零', True, result['exit_code'] != 0),
            check('旧 CLI 仍然只输出一个 JSON 对象', True, payload is not None),
            check('response.ok', False, (payload or {}).get('ok')),
            check('旧核心自己的 error.code', 'INPUT_ERROR', error.get('code')),
        ]}


def step_old_entry_broken_wordlist_through_adapter(area):
    """The same old failure through the new entry: passed through, not translated."""
    broken = Path(area) / 'broken.txt'
    result = _adapter_generate(area, broken, tag='old-broken')
    payload = last_json(result['stdout'])
    error = (payload or {}).get('error') or {}
    details = error.get('details') or {}
    return {
        'name': 'old_entry_broken_wordlist_through_adapter',
        'why': '同一个坏词表走新入口：旧核心的 code 与它自己给出的行号细节原样透传，'
               '适配器不发明第二种诊断。',
        'command': result, 'evidence': {'error': error, 'published_tracks': len(published(area / 'old-broken-out'))},
        'checks': [
            check('exit_code', 3, result['exit_code']),
            check('error.code', 'INPUT_ERROR', error.get('code')),
            check('旧核心的行号细节被保留', True, bool(details.get('lines'))),
            check('没有产出任何 SRT', 0, len(published(area / 'old-broken-out'))),
        ]}


def step_new_entry_works_after_the_old_failures(area):
    """While the old entry is broken, the new one exports its five tracks."""
    probe_area = Path(area) / 'new-good'
    result = run_subprocess([sys.executable, '-m', 'legacy_adapter.new_entry_probe',
                             '--area', str(probe_area)], cwd=repo_root())
    payload = last_json(result['stdout']) or {}
    tracks = payload.get('tracks') or []
    return {
        'name': 'new_entry_works_after_the_old_failures',
        'why': '方向一的后半：旧入口刚刚连续失败三次，新入口照常建工程、解算、导出五轨 SRT。',
        'command': result,
        'evidence': {'ok': payload.get('ok'), 'project': payload.get('project'),
                     'run_dir': payload.get('run_dir'), 'tracks': tracks,
                     'plan_identity': (payload.get('report') or {}).get('plan_identity'),
                     'published_tracks': len(published(probe_area / 'out'))},
        'checks': [
            check('新入口 exit_code', 0, result['exit_code']),
            check('新入口 ok', True, payload.get('ok')),
            check('五轨 SRT 数量', 5, len(tracks)),
            check('磁盘上的 SRT 数量', 5, len(published(probe_area / 'out'))),
            check('五轨都不为空', True, all(row.get('bytes') for row in tracks)),
        ]}


# -- direction 2: the new entry fails, the old one does not -------------------

def step_new_entry_missing_media(area):
    """A registered recording that is not on disk: refused by code, nothing published."""
    probe_area = Path(area) / 'new-broken'
    result = run_subprocess([sys.executable, '-m', 'legacy_adapter.new_entry_probe',
                             '--area', str(probe_area), '--break-asset', BROKEN_ASSET],
                            cwd=repo_root())
    payload = last_json(result['stdout']) or {}
    error = payload.get('error') or {}
    broken = ((payload.get('project') or {}).get('broken') or [{}])[0]
    return {
        'name': 'new_entry_missing_media',
        'why': '新入口的失败：assets.json 把一个素材登记到不存在的文件上（成员移动/删除配音后的真实情形）。',
        'command': result,
        'evidence': {'ok': payload.get('ok'), 'error': error, 'broken_asset': broken,
                     'published_tracks': len(published(probe_area / 'out'))},
        'checks': [
            check('新入口 exit_code', 2, result['exit_code']),
            check('新入口 ok', False, payload.get('ok')),
            check('error.code', 'ASSET_FILE_MISSING', error.get('code')),
            check('error.path 指到那个素材', 'asset:%s' % BROKEN_ASSET, error.get('path')),
            check('文件确实不存在', False, broken.get('exists')),
            check('失败发生在产出之前：没有半个 SRT', 0, len(published(probe_area / 'out'))),
        ]}


def step_new_entry_cli_raw(area):
    """The same broken project through the engine's own CLI: the raw, unpolished fact.

    ``word_video.exporters`` catches ``ValueError``/``OSError``/``RuntimeError``; a
    ``ProjectError`` (this refusal) is not one of them, so the standalone CLI answers
    with a traceback instead of its documented ``{"ok": false}`` object.  The editor
    path turns the same error into a structured notice, so members are not affected -
    the gap is recorded here rather than hidden, and it is B's file, not this task's.
    """
    probe_area = Path(area) / 'new-broken'
    project_dir = probe_area / 'project'
    result = run_subprocess([sys.executable, '-m', 'word_video.exporters',
                             '--project', str(project_dir),
                             '--out', str(probe_area / 'cli-out'),
                             '--no-video', '--no-draft'], cwd=repo_root())
    payload = last_json(result['stdout'])
    return {
        'name': 'new_entry_cli_raw',
        'why': '同一缺媒体工程走 word_video.exporters 自己的 CLI，记录它未被包装的原始行为。',
        'command': result,
        'evidence': {'structured_json': payload is not None, 'published_tracks':
                     len(published(probe_area / 'cli-out')),
                     'stderr_tail': result['stderr'][-600:]},
        'checks': [
            check('CLI 退出码非零', True, result['exit_code'] != 0),
            check('CLI 没有宣称成功', True, (payload or {}).get('ok') is not True),
            check('没有产出任何 SRT', 0, len(published(probe_area / 'cli-out'))),
        ],
        'note': '已知缺口（B 的 word_video/exporters/__main__.py，本任务不改）：'
                'ProjectError 不在它的 except 元组里，所以 headless CLI 会吐 traceback、'
                '退出码 1、stdout 为空；编辑器入口 desktop/editor_export.py 会把它变成结构化通知。',
    }


def step_old_entry_works_after_the_new_failure(area, wordlist):
    """While the new entry is broken, the old entry still produces its five tracks."""
    result = _adapter_generate(area, wordlist, tag='old-good')
    payload = last_json(result['stdout']) or {}
    tracks = _tracks_of(payload)
    ordered = [row for row in sorted(tracks, key=lambda row: int(row.get('track') or 0))]
    return {
        'name': 'old_entry_works_after_the_new_failure',
        'why': '方向二的后半：新入口刚刚因为缺媒体失败，旧入口照常产出五轨，逐文件哈希与旧 CLI 自报一致。',
        'command': result,
        'evidence': {'ok': payload.get('ok'), 'counts': payload.get('counts'),
                     'directory': payload.get('directory'), 'tracks': ordered,
                     'file_count': len(published(area / 'old-good-out'))},
        'checks': [
            check('旧入口 exit_code', 0, result['exit_code']),
            check('旧入口 ok', True, payload.get('ok')),
            check('五轨数量', 5, len(ordered)),
            check('每轨 cue 数', EXPECTED_CUES, [row['cues'] for row in ordered]),
            check('磁盘上的 SRT 数量', 5, len(published(area / 'old-good-out'))),
            check('逐文件 sha256 与旧核心自报一致', True,
                  len(ordered) == 5 and all(row['sha256'] and
                                            row['sha256'] == row['recorded_sha256']
                                            for row in ordered)),
        ]}


def step_old_entry_survives_a_blocked_engine(area, wordlist, timeline):
    """The strongest form: the whole new engine cannot be imported, old entry runs on.

    A ``sitecustomize`` on ``PYTHONPATH`` makes ``import word_video`` raise in every
    process started with that environment.  The old entry still publishes five tracks -
    and reports the new timing caliber as an *unavailable label with a reason*, which is
    exactly what "the caliber is a label, never a dependency" has to mean in practice.
    ``--timeline`` is passed on purpose: without it the caliber would be skipped for
    lack of a plan, and the lazy import this step is about would never run.
    """
    blocker = Path(area) / 'blocker'
    blocker.mkdir(parents=True, exist_ok=True)
    (blocker / 'sitecustomize.py').write_text(BLOCKER_SOURCE, encoding='utf-8')
    environment = dict(os.environ, PYTHONPATH=str(blocker))
    broken = run_subprocess([sys.executable, '-c', 'import word_video'], cwd=repo_root(),
                            env=environment)
    result = _adapter_generate(area, wordlist, tag='old-blocked', env=environment,
                               timeline=timeline)
    payload = last_json(result['stdout']) or {}
    caliber = payload.get('media_caliber') or {}
    ordered = sorted(_tracks_of(payload), key=lambda row: int(row.get('track') or 0))
    return {
        'name': 'old_entry_survives_a_blocked_engine',
        'why': '新视频模块整体不可导入（子进程内屏蔽 import word_video），旧入口照常出五轨；'
               '新口径降级成"标签不可用 + 原因"，不是失败。',
        'command': result, 'control': broken,
        'evidence': {'blocked_import_exit_code': broken['exit_code'],
                     'ok': payload.get('ok'), 'media_caliber': caliber, 'tracks': ordered},
        'checks': [
            check('屏蔽确实生效（import word_video 失败）', True, broken['exit_code'] != 0),
            check('旧入口 exit_code', 0, result['exit_code']),
            check('旧入口 ok', True, payload.get('ok')),
            check('五轨数量', 5, len(ordered)),
            check('每轨 cue 数', EXPECTED_CUES, [row['cues'] for row in ordered]),
            check('新口径可用性', False, caliber.get('available')),
            check('新口径给出了原因', True, 'word_video' in str(caliber.get('reason'))),
        ]}


def run_isolation(*, area='', wordlist=None, report='', label='isolation'):
    """Both directions, in order, as one document with every check's actual value."""
    wordlist = Path(wordlist or DEFAULT_WORDLIST)
    if not wordlist.is_file():
        raise LegacyAdapterError(INPUT_ERROR, '只读词表不存在：%s' % wordlist,
                                 details={'path': str(wordlist), 'field': 'wordlist'})
    if not Path(default_python()).is_file():
        raise LegacyAdapterError(INPUT_ERROR, '运行旧 CLI 的解释器不存在：%s' % default_python())
    area = Path(area) if str(area or '').strip() else new_run_dir('isolation')
    area.mkdir(parents=True, exist_ok=True)
    report_path = Path(report) if str(report or '').strip() else DEFAULT_REPORT
    started_utc = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    started = time.perf_counter()
    # A plan for the *media* caliber to read: the blocked-engine step needs a real
    # timeline.json, or the lazy import of the new timing core would never be reached.
    timeline = write_json(area / 'caliber-timeline.json',
                          {'fps': 60, 'total_frames': 198,
                           'words': [{'index': OLD_ENTRY_START, 'word': 'evaluate',
                                      'start_frame': 0, 'end_frame': 198}]})

    direction_one = [step_old_entry_missing_wordlist(area),
                     step_old_cli_returns_nonzero(area),
                     step_old_entry_broken_wordlist_through_adapter(area),
                     step_new_entry_works_after_the_old_failures(area)]
    direction_two = [step_new_entry_missing_media(area),
                     step_new_entry_cli_raw(area),
                     step_old_entry_works_after_the_new_failure(area, wordlist),
                     step_old_entry_survives_a_blocked_engine(area, wordlist, timeline)]
    steps = [*direction_one, *direction_two]
    folded = fold_checks(steps)
    document = {
        'schema': SCHEMA, 'label': label, 'ok': not folded['failed'],
        'verdict': 'PASS' if not folded['failed'] else 'FAIL',
        'problems': folded['problems'],
        'started_utc': started_utc, 'seconds': round(time.perf_counter() - started, 3),
        'work_root': str(work_root()), 'area': str(area), 'wordlist': str(wordlist),
        'python': sys.executable, 'legacy_entry': str(locate_legacy_entry()),
        'selection': {'start': OLD_ENTRY_START, 'end': OLD_ENTRY_END, 'batch_size': OLD_ENTRY_BATCH,
                      'expected_cues': EXPECTED_CUES},
        'directions': {
            'old_fails_new_ok': {
                'statement': '旧入口失败（词表不存在 / 旧 CLI 非零退出）→ 新入口照常可用',
                'steps': [row['name'] for row in direction_one]},
            'new_fails_old_ok': {
                'statement': '新入口失败（缺媒体）→ 旧入口照常可用；新引擎整体不可导入时同样成立',
                'steps': [row['name'] for row in direction_two]}},
        'steps': steps,
        'totals': {'steps': len(steps), 'checks': folded['checks'],
                   'failed_checks': folded['failed_checks']},
        'evidence': {'report': str(report_path), 'area': str(area)},
        'finished_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    write_json(report_path, document)
    text = format_summary(document)
    print(text)
    return document


def format_summary(report):
    """The human-readable face: each step, its exit code, and whether its checks held."""
    lines = ['== W10 失败隔离复现（两个方向，全部独立子进程）==',
             '工作目录：%s' % report['area'],
             '%-46s %-6s %-8s %s' % ('步骤', '退出码', '检查', '说明')]
    for row in report['steps']:
        passed = sum(1 for item in row['checks'] if item['ok'])
        lines.append('%-46s %-6s %-8s %s' % (row['name'], row['command']['exit_code'],
                                             '%d/%d' % (passed, len(row['checks'])),
                                             row['why']))
        for item in row['checks']:
            if not item['ok']:
                lines.append('    ! %s：期望 %r，实际 %r' % (item['check'], item['expected'],
                                                             item['actual']))
    lines.append('方向一：%s' % report['directions']['old_fails_new_ok']['statement'])
    lines.append('方向二：%s' % report['directions']['new_fails_old_ok']['statement'])
    totals = report['totals']
    lines.append('合计：%d 步 / %d 项检查 / %d 项不成立' % (totals['steps'], totals['checks'],
                                                          totals['failed_checks']))
    lines.append('判定：%s' % report['verdict'])
    for problem in report['problems']:
        lines.append('  - %s' % problem)
    lines.append('证据：%s' % report['evidence']['report'])
    return '\n'.join(lines) + '\n'


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m legacy_adapter.failure_isolation',
        description='W10 失败隔离复现：旧入口坏而新入口可用、新入口坏而旧入口可用，'
                    '每一步都是独立子进程，证据写 out/reports。')
    parser.add_argument('--area', default='', help='本次运行的新目录（默认 out/legacy/<时间>-isolation-*）')
    parser.add_argument('--wordlist', default=str(DEFAULT_WORDLIST), help='只读全量词表')
    parser.add_argument('--report', default='',
                        help='证据 JSON 路径（默认 %s）' % DEFAULT_REPORT)
    parser.add_argument('--label', default='isolation', help='这份证据的标签')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        document = run_isolation(area=args.area, wordlist=args.wordlist, report=args.report,
                                 label=args.label)
    except LegacyAdapterError as error:
        # Same rule as the gate: a refused invocation does not overwrite the last good
        # evidence unless the caller named that exact path.
        fallback = Path(args.report) if str(args.report or '').strip() else \
            DEFAULT_REPORT.with_name('gate-legacy-isolation-error.json')
        write_json(fallback, {'schema': SCHEMA, 'ok': False, 'verdict': 'FAIL',
                              'label': args.label, 'error': error.to_dict(),
                              'argv': list(argv if argv is not None else sys.argv[1:])})
        print('%s\n（本次没有跑：证据没被覆盖，失败写在 %s）' % (error, fallback))
        return error.exit_code
    return 0 if document['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
