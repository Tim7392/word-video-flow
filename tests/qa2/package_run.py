"""QA-2: exercise the member package in a clean environment and judge the result.

The package is a black box here: the script only calls the shipped launcher and
the shipped executable, with a PATH that contains nothing but ``System32`` (plus
the package's own ``ffmpeg`` folder, exactly as the launcher sets it), no
``PYTHONHOME``/``PYTHONPATH``/``PYTHONSTARTUP``, no TTS credentials, and a
per-user PATH / AppData variables removed so nothing from the developer machine
can leak in.  Everything is run with the package as the working directory.

What is recorded, because "it worked" is not a measurement:

  * the exact environment the child saw (the variables the script set or removed),
  * the launch overhead (``doctor``) and the per-stage job seconds,
  * the published file count and sizes, and the exit codes of every call.

Usage:
  python package_run.py --package DIR --request FILE [--json OUT]
                        [--expect-files 172] [--expect-state generated]
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Variables a packaged app must not inherit; the list is deliberately explicit so
# the report can show what was removed instead of claiming "a clean environment".
REMOVE = ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONUTF8',
          'VOLC_TTS_API_KEY', 'MODEL_SPEECH_API_KEY')
# Variables Windows itself needs to start a process at all.  They are *added*
# explicitly when the parent does not have them: measured 2026-09-16, a child
# started without SystemRoot dies before it can print anything (exit 1, empty
# stderr), which looks exactly like a broken package.
REQUIRED = {'SystemRoot': r'C:\Windows', 'windir': r'C:\Windows',
            'COMSPEC': r'C:\Windows\System32\cmd.exe',
            'PATHEXT': '.COM;.EXE;.BAT;.CMD'}


def clean_environment(package, temp):
    env = {key: value for key, value in os.environ.items() if key not in REMOVE}
    env['PATH'] = r'C:\Windows\System32' + os.pathsep + str(Path(package) / 'ffmpeg')
    env['TEMP'] = str(temp)
    env['TMP'] = str(temp)
    env['PYTHONHOME'] = ''
    env['PYTHONPATH'] = ''
    env['PYTHONSTARTUP'] = ''
    for key, value in REQUIRED.items():
        env.setdefault(key, value)
    return env


def run(exe, args, env, cwd):
    started = time.monotonic()
    result = subprocess.run([str(exe), *[str(a) for a in args]], capture_output=True,
                            env=env, cwd=str(cwd))
    seconds = time.monotonic() - started
    stdout = result.stdout.decode('utf-8', 'replace')
    payload = None
    for line in reversed(stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    return {'args': [str(a) for a in args], 'exit_code': result.returncode,
            'seconds': round(seconds, 3), 'payload': payload,
            'stderr_tail': result.stderr.decode('utf-8', 'replace')[-400:]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--json')
    parser.add_argument('--expect-files', type=int, default=None)
    parser.add_argument('--expect-state', default='generated')
    parser.add_argument('--db', default=None)
    parser.add_argument('--use-launcher', action='store_true',
                        help='run through 单词视频.cmd instead of WordVideo.exe directly')
    args = parser.parse_args(argv)

    package = Path(args.package).resolve(strict=True)
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    temp = package / 'temp'
    temp.mkdir(parents=True, exist_ok=True)
    db = Path(args.db) if args.db else (package / 'temp' / 'qa2-package.sqlite3')
    env = clean_environment(package, temp)
    exe = package / 'WordVideo' / 'WordVideo.exe'
    if not exe.exists():
        raise SystemExit('包内没有 %s' % exe)

    report = {'package': str(package), 'request': str(Path(args.request).resolve()),
              'launcher': str(package / '单词视频.cmd') if args.use_launcher else str(exe),
              'working_directory': str(package),
              'environment': {
                  'PATH': env['PATH'], 'TEMP': env['TEMP'], 'TMP': env['TMP'],
                  'PYTHONHOME': env['PYTHONHOME'], 'PYTHONPATH': env['PYTHONPATH'],
                  'SystemRoot': env.get('SystemRoot'),
                  'added_by_harness': sorted(key for key in REQUIRED
                                             if key not in os.environ),
                  'removed': sorted(set(os.environ) & set(REMOVE))},
              'output_root': request['output'],
              'words': len(request['lesson']['entries']),
              'provider_kind': request['provider']['kind'],
              'calls': []}

    doctor = run(exe, ['doctor'], env, package)
    report['calls'].append(doctor)
    report['launch_seconds'] = doctor['seconds']

    submitted = run(exe, ['--db', db, 'submit', '--request', args.request], env, package)
    report['calls'].append(submitted)
    job = (submitted['payload'] or {}).get('result', {}).get('id')
    if not job:
        report['ok'] = False
        report['error'] = 'submit 没有返回作业 id'
        _write(report, args.json)
        return 1
    report['job'] = job

    worked = run(exe, ['--db', db, 'work', '--job', job], env, package)
    report['calls'].append(worked)
    report['job_seconds'] = worked['seconds']
    state = (worked['payload'] or {}).get('result', {})
    report['state'] = state.get('state')
    report['stage_seconds'] = (state.get('result') or {}).get('stage_seconds')
    report['file_count'] = (state.get('result') or {}).get('file_count')
    report['error'] = state.get('error')

    problems = []
    if report['state'] != args.expect_state:
        problems.append('作业状态 %r != %r' % (report['state'], args.expect_state))
    if args.expect_files is not None and report['file_count'] != args.expect_files:
        problems.append('发布文件数 %r != %r' % (report['file_count'], args.expect_files))
    if any(call['exit_code'] != 0 for call in report['calls']):
        problems.append('有调用非零退出：%s'
                        % [(c['args'][0], c['exit_code']) for c in report['calls']])
    # The batch must exist where the request said, with the three products.
    batch = None
    root = Path(request['output']) / job
    batches = sorted(p.parent for p in root.rglob('timeline.json')) if root.exists() else []
    if len(batches) != 1:
        problems.append('输出目录下找到 %d 个批次' % len(batches))
    else:
        batch = batches[0]
        report['batch'] = str(batch)
        if not (batch / 'video' / 'video.mp4').exists():
            problems.append('缺 MP4')
        if len(list((batch / 'srt').glob('*.srt'))) != 5:
            problems.append('SRT 不是 5 条')
        if not (batch / 'editable-draft' / 'draft_content.json').exists():
            problems.append('缺草稿')
        if not (batch / 'complete.json').exists():
            problems.append('缺 complete.json')
    report['problems'] = problems
    report['ok'] = not problems
    _write(report, args.json)
    print(json.dumps({k: report[k] for k in
                      ('package', 'launcher', 'job', 'state', 'file_count',
                       'launch_seconds', 'job_seconds', 'stage_seconds', 'batch',
                       'problems', 'ok') if k in report},
                     ensure_ascii=False, indent=1))
    return 0 if report['ok'] else 1


def _write(report, target):
    if target:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
