"""QA gate helpers: stable evidence paths and honest re-use of expensive runs.

The gate is ``python -m pytest -m acceptance_media tests``.  Two rules make it
usable per integration instead of ceremonial:

* evidence is written to a **stable path** (``out/reports/``), never only into
  pytest's temporary directory, which is pruned when the session ends - evidence
  that disappears is not evidence;
* an expensive real-media step is re-run only when something it depends on
  changed.  The fingerprint covers the engine sources, the acceptance tools and
  the word list, so a re-used step means "the same code read the same inputs";
  the next commit invalidates it automatically.  When the preferred evidence
  directory is not writable the run falls back loudly, and the fallback is
  recorded in the evidence itself.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORK_ROOT = Path(r'D:\1\1-AI_workflow\word_video_flow')
PREFERRED_OUT = WORK_ROOT / 'out' / 'reports'
FALLBACK_OUT = WORK_ROOT / 'runtime' / 'tmp' / 'QA' / 'gate'
WORDLIST = WORK_ROOT / 'data' / 'wordlists' / '四级核心1500词_已清理.txt'
GOOD_BATCH = Path(r'D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染'
                  r'\wv-b349c24dbf19ce65bc4e5422\0151-0200')
#: Sources whose bytes decide what a gate run means.
TRACKED = ('word_video/**/*.py', 'word_video_cli.py', 'tests/acceptance/**/*.py',
           'tests/qa2/*.py', 'tests/qa3/*.py', 'subtitle_factory_api.py')


def evidence_dir(preferred=None):
    """(directory, warning) - the stable path, or a loud fallback."""
    preferred = Path(preferred) if preferred else PREFERRED_OUT
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / '.gate-write-probe'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        return preferred, None
    except OSError as error:
        FALLBACK_OUT.mkdir(parents=True, exist_ok=True)
        return FALLBACK_OUT, ('%s 不可写（%s），证据改写到 %s'
                              % (preferred, error, FALLBACK_OUT))


def worktree_of(module_file):
    """The work tree a test module belongs to.

    A module under ``<worktree>/tests/`` may pass its own ``__file__``; a module
    under ``<worktree>/tests/qaN/`` passes that one.  Either way the answer is the
    ancestor that holds ``word_video_cli.py``, which keeps the helper correct no
    matter how deep the test lives.
    """
    start = Path(module_file).resolve()
    for parent in (start, *start.parents):
        if (parent / 'word_video_cli.py').exists():
            return parent
    raise RuntimeError('%s 不在任何一个 worktree 里（找不到 word_video_cli.py）' % start)


def fingerprint(worktree, extra=()):
    """sha256 over the tracked sources, the word list and any extra inputs."""
    digest = hashlib.sha256()
    paths = []
    for pattern in TRACKED:
        paths.extend(sorted(Path(worktree).glob(pattern)))
    for path in paths:
        digest.update(str(path.relative_to(worktree)).replace('\\', '/').encode('utf-8'))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    digest.update(WORDLIST.read_bytes())
    for item in extra:
        digest.update(str(item).encode('utf-8'))
    return digest.hexdigest()


def head_of(worktree):
    result = subprocess.run(['git', '-C', str(worktree), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True)
    return result.stdout.strip()


def stamp(worktree, name, extra=()):
    return {'name': name, 'fingerprint': fingerprint(worktree, extra),
            'head': head_of(worktree),
            'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}


def reusable(evidence, name, worktree, extra=()):
    """(usable, reason) - previous evidence still describes this code and input."""
    stamp_path = Path(evidence) / ('stamp-%s.json' % name)
    if not stamp_path.exists():
        return False, '没有上次的指纹记录'
    try:
        old = json.loads(stamp_path.read_text(encoding='utf-8'))
    except ValueError:
        return False, '指纹记录损坏'
    if old.get('fingerprint') != fingerprint(worktree, extra):
        return False, '源码/输入自 %s 起有变化' % str(old.get('head'))[:12]
    return True, '与 %s 的源码、输入一致' % str(old.get('head'))[:12]


def write_stamp(evidence, name, worktree, extra=(), note=None):
    payload = stamp(worktree, name, extra)
    if note:
        payload['note'] = note
    target = Path(evidence) / ('stamp-%s.json' % name)
    # Paths are convenient in the note and meaningless to JSON: keep them readable
    # instead of failing the run for a formatting detail.
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                                 default=str),
                      encoding='utf-8')
    return payload


def run(command, cwd, env=None):
    """Run a subprocess, inheriting this process's environment by default.

    The captured tails are generous on purpose: the CLI answers with one JSON
    object per call and a 50-word job result is several kilobytes, so a small cap
    silently truncates the answer this helper is supposed to read.
    """
    started = time.monotonic()
    result = subprocess.run([str(part) for part in command], cwd=str(cwd),
                            capture_output=True, env=env)
    return {'exit_code': result.returncode,
            'seconds': round(time.monotonic() - started, 1),
            'stdout_tail': result.stdout.decode('utf-8', 'replace')[-20000:],
            'stderr_tail': result.stderr.decode('utf-8', 'replace')[-2000:]}


def last_json(result):
    """The last complete JSON object a CLI call printed, or None.

    ``json.loads`` on the whole tail is wrong as soon as the tail starts in the
    middle of an object; parsing each line from the end is robust and still fails
    loudly (returns None) when the call printed no JSON at all.
    """
    for line in reversed(result.get('stdout_tail', '').splitlines()):
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def job_id(result):
    payload = last_json(result)
    if not payload or 'result' not in payload or 'id' not in (payload['result'] or {}):
        raise AssertionError('CLI 没有返回作业 id：%r' % (result.get('stdout_tail') or
                                                        result.get('stderr_tail')))
    return payload['result']['id']
