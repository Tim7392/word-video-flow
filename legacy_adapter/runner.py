"""Run the old CLI in its own process, then read back what it published.

**A subprocess, not an import.**  The old factory is five flat modules that import
each other by name and pull in PyQt6 for their window; importing them into the new
software would put that whole surface - and its failures - inside the editor's
process.  So the adapter starts ``subtitle_factory_cli.py`` the way its own contract
intends (a UTF-8 JSON request in, one JSON object out) and never touches its code.

What the child is given, and why:

* ``cwd`` is a **new directory under the D work root**, never the old tree;
* ``TEMP``/``TMP`` point inside that same directory, so the throwaway files the old
  core stages while it measures (``subtitle_factory_api.render_plan`` renders into a
  temporary directory) land on D instead of the system drive;
* ``-B``: no ``__pycache__`` is written next to the protected files, so a run leaves
  the old tree byte-identical - the property the task's fourth acceptance item checks;
* the request goes in as a **file**, not as ``argv`` text, so a word list path with
  spaces or Chinese characters is not re-parsed by a shell - there is no shell;
* ``CREATE_NO_WINDOW`` on Windows: a windowed editor spawning a console program would
  otherwise flash a console on every run, and the pipes already carry the answer.

Failure isolation is the shape of this module: everything the child can do wrong
(missing entry, cannot start, times out, prints nonsense, says ok but publishes
nothing) becomes a :class:`LegacyAdapterError` with a code, and **nothing here
imports the new video software** - so the old entry cannot be broken by a broken
renderer.  What the run produced is summarised in one report document, which is also
written beside the run as ``legacy-run.json`` so a later comparison needs no rerun.
"""
from dataclasses import replace
import json
import os
import subprocess
import time
from pathlib import Path

from .caliber import (CALIBER_LEGACY, CALIBER_MEDIA, caliber, missing_plan_block,
                      timeline_caliber)
from .environment import default_python, locate_legacy_entry
from .errors import (INTERNAL_ERROR, INTERRUPTED, LEGACY_BAD_RESPONSE,
                     LEGACY_ENTRY_MISSING, LEGACY_LAUNCH_FAILED, LEGACY_OUTPUT_MISSING,
                     LEGACY_TIMEOUT, OUTPUT_CHANGED, LegacyAdapterError)
from .report import sha256, summarize_packages, track_facts

SCHEMA = 'wv-legacy-run@1'
VERSION = '0.1.0'
REQUEST_FILENAME = 'legacy-request.json'
JOURNAL_FILENAME = 'legacy-run.json'
_EXCERPT = 2000

#: Codes the old CLI can answer with; anything else is folded into INTERNAL_ERROR so
#: one surprising string cannot create an exit code this adapter does not define.
_KNOWN_CODES = {'INVALID_REQUEST', 'INPUT_ERROR', 'OUTPUT_ERROR', 'IDEMPOTENCY_CONFLICT',
                'INCOMPLETE_RUN', 'OUTPUT_CHANGED', 'INTERNAL_ERROR', 'INTERRUPTED'}


def creation_flags():
    """No console window for the child; a captured pipe is the whole interface."""
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0


def _excerpt(raw, limit=_EXCERPT):
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw or '')
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + '…（已截断）'


def write_json(path, payload):
    """Write a JSON document so a reader never sees half of it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    temporary.replace(target)
    return target


#: The old name, kept because the report/journal code reads better with it.
_write_json = write_json


def legacy_environment(work_dir):
    """The child's environment: inherited, with temp files redirected onto D."""
    environment = os.environ.copy()
    temporary = Path(work_dir) / 'tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    environment.update({'TEMP': str(temporary), 'TMP': str(temporary),
                        'PYTHONIOENCODING': 'utf-8'})
    return environment


def _verify_published(result, files):
    """Every file the old CLI named must be on disk with the hash it reported.

    The old core already checks this inside one keyed run (``saved_run``); this is
    the same check from *outside*, which is what makes the report evidence about the
    files rather than a repetition of what the producer said about itself.
    """
    problems = []
    for item in files:
        path = Path(item.get('path') or '')
        if not path.is_file():
            problems.append({'file': item.get('relative_path'), 'path': str(path),
                             'problem': 'missing'})
        elif item.get('sha256') and sha256(path) != item['sha256']:
            problems.append({'file': item.get('relative_path'), 'path': str(path),
                             'problem': 'sha256', 'expected': item['sha256']})
    if problems:
        raise LegacyAdapterError(
            LEGACY_OUTPUT_MISSING if all(p['problem'] == 'missing' for p in problems)
            else OUTPUT_CHANGED,
            '旧核心报告成功，但产出的文件与报告不一致（%d 项）。' % len(problems),
            details={'problems': problems[:20], 'checked': len(files)})
    return len(files)


def _read_response(stdout, stderr, returncode, entry):
    """The old CLI's one-JSON-object contract, enforced rather than assumed.

    The *whole* stdout is parsed; the excerpts below are only for the failure
    message.  (Truncating first and parsing afterwards is how a 2 kB preview of a
    100 kB answer becomes "the old CLI prints nonsense" - a false accusation that
    costs an hour.)
    """
    text = (stdout or b'').decode('utf-8-sig', 'replace').strip()
    if not text:
        raise LegacyAdapterError(LEGACY_BAD_RESPONSE, '旧 CLI 没有输出任何 JSON（退出码 %s）。'
                                 % returncode,
                                 details={'returncode': returncode, 'stderr': _excerpt(stderr),
                                          'legacy_entry': str(entry)})
    try:
        response = json.loads(text)
    except ValueError as error:
        raise LegacyAdapterError(LEGACY_BAD_RESPONSE, '旧 CLI 的 stdout 不是单个 JSON：%s' % error,
                                 details={'returncode': returncode, 'stdout': _excerpt(stdout),
                                          'stdout_bytes': len(stdout or b''),
                                          'stderr': _excerpt(stderr),
                                          'legacy_entry': str(entry)}) from None
    if not isinstance(response, dict):
        raise LegacyAdapterError(LEGACY_BAD_RESPONSE, '旧 CLI 的 stdout 不是 JSON 对象。',
                                 details={'returncode': returncode, 'stdout': _excerpt(stdout)})
    return response


def run_legacy(request, *, action='generate', report_path=''):
    """Run the old CLI and return one report document; raise on any refusal."""
    entry = locate_legacy_entry(request.legacy_script)
    resolved = replace(request, legacy_script=str(entry)).resolve()
    python = str(request.legacy_python or '').strip() or default_python()
    if not python:
        raise LegacyAdapterError(LEGACY_ENTRY_MISSING, '找不到能运行旧 CLI 的解释器。',
                                 details={'hint': '用 --legacy-python 指定（成员机不装 Python 时'
                                                  '需要包内解释器，归 packaging 决定）',
                                          'legacy_entry': str(entry)})
    work_dir = Path(resolved.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    request_file = _write_json(work_dir / REQUEST_FILENAME, resolved.to_legacy_request(action))
    argv = [python, '-B', str(entry), 'request', '--json-file', str(request_file)]
    environment = legacy_environment(work_dir)
    started = time.perf_counter()
    try:
        completed = subprocess.run(argv, cwd=str(work_dir), env=environment,
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=float(resolved.timeout), check=False,
                                   creationflags=creation_flags())
    except subprocess.TimeoutExpired as error:
        raise LegacyAdapterError(
            LEGACY_TIMEOUT, '旧 CLI 在 %.0f 秒内没有结束。' % resolved.timeout,
            details={'seconds': round(time.perf_counter() - started, 3),
                     'timeout': resolved.timeout, 'stdout': _excerpt(error.stdout),
                     'stderr': _excerpt(error.stderr), 'legacy_entry': str(entry),
                     'work_dir': str(work_dir)}) from None
    except OSError as error:
        raise LegacyAdapterError(LEGACY_LAUNCH_FAILED, '启动旧 CLI 失败：%s' % error,
                                 details={'argv': argv, 'legacy_python': python,
                                          'legacy_entry': str(entry)}) from None
    except KeyboardInterrupt:
        raise LegacyAdapterError(INTERRUPTED, '操作被取消；带 key 的任务可用旧 CLI 的 status 核验。',
                                 details={'work_dir': str(work_dir)}) from None
    seconds = round(time.perf_counter() - started, 3)
    response = _read_response(completed.stdout, completed.stderr, completed.returncode, entry)
    if response.get('ok') is not True:
        failure = response.get('error') or {}
        code = failure.get('code') or INTERNAL_ERROR
        if code not in _KNOWN_CODES:
            code = INTERNAL_ERROR
        raise LegacyAdapterError(
            code, failure.get('message') or '旧 CLI 报告失败（未给原因）。',
            details={**(failure.get('details') or {}), 'legacy_entry': str(entry),
                     'returncode': completed.returncode, 'seconds': seconds,
                     'stderr': _excerpt(completed.stderr), 'work_dir': str(work_dir)})
    result = response.get('result')
    if not isinstance(result, dict):
        raise LegacyAdapterError(LEGACY_BAD_RESPONSE, '旧 CLI 的 result 不是对象。',
                                 details={'returncode': completed.returncode,
                                          'keys': sorted(response)})
    files = list(result.get('files') or ())
    tracks = []
    if action == 'generate':
        _verify_published(result, files)
        for item in files:
            facts = track_facts(item['path'])
            tracks.append({'package': item.get('package'), 'track': item.get('track'),
                           'relative_path': item.get('relative_path'),
                           'recorded_sha256': item.get('sha256'),
                           'recorded_bytes': item.get('bytes'),
                           **{key: value for key, value in facts.items()
                              if key not in ('text', 'parsed')}})
    packages = summarize_packages(result)
    report = {
        'schema': SCHEMA, 'version': VERSION, 'ok': True, 'action': action,
        'adapter': {'legacy_entry': str(entry), 'legacy_python': python,
                    'legacy_api_version': response.get('api_version'),
                    'returncode': completed.returncode, 'seconds': seconds,
                    'argv': argv, 'work_dir': str(work_dir), 'request_file': str(request_file),
                    'temp_dir': str(work_dir / 'tmp')},
        'request': resolved.to_dict(), 'legacy_request': resolved.to_legacy_request(action),
        'source': result.get('source'),
        'counts': {'total_count': result.get('total_count'),
                   'selected_count': result.get('selected_count'),
                   'package_count': result.get('package_count'),
                   'file_count': result.get('file_count'),
                   'published_checked': len(tracks)},
        'caliber': {**caliber(CALIBER_LEGACY),
                    'measured': {'packages': packages,
                                 'total_duration_ms': (packages[-1]['duration_ms']
                                                       if packages else None),
                                 'package_count': result.get('package_count'),
                                 'word_count': result.get('selected_count'),
                                 'timing': {'first_six': resolved.first_six,
                                            'extra': resolved.extra}}},
        'media_caliber': (timeline_caliber(resolved.timeline) if str(resolved.timeline or '').strip()
                          else missing_plan_block('没有指定 timeline.json / 工程目录；'
                                                  '新口径的数值请用 calibers --timeline 查看')),
        'directory': result.get('directory'), 'fingerprint': result.get('fingerprint'),
        'packages': packages, 'tracks': tracks, 'problems': [],
    }
    _write_json(work_dir / JOURNAL_FILENAME, report)
    if report_path:
        _write_json(report_path, report)
    return report


def plan_legacy(request):
    """The old core's own ``plan``: real numbers, nothing published to the output."""
    return run_legacy(request, action='plan')
