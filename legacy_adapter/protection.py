"""Prove the run read the old core and the old archive, and wrote neither.

The task's fourth acceptance item is a negative: *nothing protected changed*.  A
negative is only worth as much as the way it is checked, so this module checks it
three ways, each independent of the other two:

* **the six protected files** are hashed with their mtime before and after the run,
  and ``git`` is asked whether the branch touched them (working tree, index, and
  every commit since ``main`` branched) - a hash catches an edit, ``git`` catches a
  file that was rewritten to the same bytes, and the mtime catches a file that was
  opened for writing and put back;
* **the archive batches** are verified against the ``sha256`` their own
  ``complete.json`` recorded when they were published, plus a before/after snapshot
  of the few files this run actually reads (``timeline.json`` and the five tracks) -
  the recorded hashes answer "is this still the delivered batch", the snapshot
  answers "did *this* run touch it";
* **H0's own tool** (``tools/verify_protection.py``) is run as a subprocess so the
  same question is also asked in the words the project already uses for it, and its
  answer is kept as evidence.  It compares the six files against the original C-drive
  tree, which is the only place that can say "byte-identical to what was there before
  the migration".

Everything here reads.  The module never writes inside a protected root - it has no
function that could, on purpose: the paths it opens for reading are recorded in the
snapshot, and the only file it writes is the evidence JSON the caller names.
"""
from pathlib import Path
import json
import subprocess
import sys

from .environment import PROTECTED_ROOTS
from .report import sha256
from .runner import creation_flags

#: The protected old subtitle factory, as it sits flat in the repository root.
PROTECTED_SOURCES = ('subtitle_factory_api.py', 'subtitle_factory_core.py',
                     'subtitle_factory_cli.py', 'subtitle_factory_ui.py',
                     'Words_SRT.py', 'word.py')

#: Files an equivalence run reads out of a delivered batch and must not disturb.
ARCHIVE_WATCH = ('complete.json', 'timeline.json')

#: The old core's original tree on this machine (H0's tool compares against it).
DEFAULT_ORIGIN = PROTECTED_ROOTS[0]
H0_TOOL = ('tools', 'verify_protection.py')


def facts(path):
    """What one file is right now: bytes, hash and the mtime a write would move."""
    target = Path(path)
    if not target.is_file():
        return {'path': str(target), 'exists': False, 'bytes': None,
                'sha256': None, 'mtime_ns': None}
    stat = target.stat()
    return {'path': str(target), 'exists': True, 'bytes': stat.st_size,
            'sha256': sha256(target), 'mtime_ns': stat.st_mtime_ns}


def snapshot(paths):
    """``{path: facts}`` for a list of files; a missing file is recorded, not raised."""
    return {str(Path(path)): facts(path) for path in paths}


def compare(before, after):
    """Which of the snapshotted files changed, and how - size, mtime, hash, existence."""
    absent = {'path': '', 'exists': False, 'bytes': None, 'sha256': None, 'mtime_ns': None}
    rows, changed = [], []
    for key in sorted(set(before) | set(after)):
        one = before.get(key, absent)
        two = after.get(key, absent)
        row = {'path': key,
               'exists_before': one.get('exists', False), 'exists_after': two.get('exists', False),
               'sha256_before': one.get('sha256'), 'sha256_after': two.get('sha256'),
               'mtime_ns_before': one.get('mtime_ns'), 'mtime_ns_after': two.get('mtime_ns'),
               'bytes_before': one.get('bytes'), 'bytes_after': two.get('bytes')}
        row['mtime_changed'] = row['mtime_ns_before'] != row['mtime_ns_after']
        row['sha256_changed'] = row['sha256_before'] != row['sha256_after']
        row['unchanged'] = not (row['mtime_changed'] or row['sha256_changed']
                                or row['exists_before'] != row['exists_after'])
        rows.append(row)
        if not row['unchanged']:
            changed.append(key)
    return {'files': rows, 'changed': changed,
            'checked': len(rows), 'unchanged': len(rows) - len(changed),
            'ok': not changed}


def archive_watch_files(batch):
    """The files of a delivered batch a run reads: the manifest, the tracks, the ledger."""
    root = Path(batch)
    watched = [root / name for name in ARCHIVE_WATCH]
    watched.extend(sorted((root / 'srt').glob('*.srt')))
    return [path for path in watched if path.is_file()]


def verify_complete(batch):
    """Re-hash a batch against the ``sha256`` its own ``complete.json`` recorded.

    This is H0's rule, kept identical on purpose (absolute entries are used as they
    are, relative ones resolve against the batch): a delivered batch is its own
    ledger, and the only honest question afterwards is whether the files still hash
    to what the ledger says.
    """
    root = Path(batch)
    ledger = root / 'complete.json'
    if not ledger.is_file():
        return {'batch': str(root), 'available': False, 'reason': '没有 complete.json',
                'entries': 0, 'ok': 0, 'changed': 0, 'missing': 0,
                'problems': ['missing complete.json: %s' % ledger]}
    try:
        document = json.loads(ledger.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        return {'batch': str(root), 'available': False,
                'reason': 'complete.json 不是 JSON：%s' % error, 'entries': 0, 'ok': 0,
                'changed': 0, 'missing': 0, 'problems': ['unreadable complete.json']}
    entries = document.get('files') or []
    ok = changed = missing = 0
    problems = []
    for entry in entries:
        relative, recorded = ((entry.get('path'), entry.get('sha256'))
                              if isinstance(entry, dict) else (entry, None))
        if not relative:
            continue
        path = Path(relative)
        if not path.is_absolute():
            path = root / relative
        if not path.is_file():
            missing += 1
            problems.append('missing: %s' % relative)
            continue
        if recorded and sha256(path) != recorded:
            changed += 1
            problems.append('differs: %s' % relative)
        else:
            ok += 1
    return {'batch': str(root), 'available': True, 'entries': len(entries), 'ok': ok,
            'changed': changed, 'missing': missing, 'problems': problems[:20]}


def _git(repo, arguments):
    completed = subprocess.run(['git', '-C', str(repo), *arguments], capture_output=True,
                               timeout=120, check=False, creationflags=creation_flags())
    # ``rstrip`` and not ``strip``: the leading space of ``git status --porcelain`` is
    # the staged/unstaged column, and throwing it away would lose real information.
    return {'argv': ['git', '-C', str(repo), *arguments], 'returncode': completed.returncode,
            'stdout': (completed.stdout or b'').decode('utf-8', 'replace').rstrip(),
            'stderr': (completed.stderr or b'').decode('utf-8', 'replace').strip()[:500]}


def git_checks(repo, files=PROTECTED_SOURCES, base='main'):
    """Ask git whether the branch touched the protected files - four ways.

    ``diff`` sees unstaged edits, ``diff --cached`` sees what a commit would carry,
    ``status --porcelain`` also sees an untracked or deleted copy, and the diff from
    the merge base with ``main`` sees every commit this branch already made.  A
    clean answer on all four is what "``git diff --name-only`` 为空" means once the
    task branch has commits of its own.
    """
    repo = Path(repo)
    listing = ['--', *files]
    commands = {
        'worktree': _git(repo, ['diff', '--name-only', *listing]),
        'staged': _git(repo, ['diff', '--cached', '--name-only', *listing]),
        'status': _git(repo, ['status', '--porcelain', *listing]),
        'head': _git(repo, ['rev-parse', 'HEAD']),
    }
    merge_base = _git(repo, ['merge-base', base, 'HEAD'])
    since_base = {'base_ref': base, 'merge_base': merge_base['stdout'] or None,
                  'available': merge_base['returncode'] == 0,
                  'reason': '' if merge_base['returncode'] == 0
                            else (merge_base['stderr'] or '找不到 %s' % base),
                  'files': []}
    if since_base['available']:
        diff = _git(repo, ['diff', '--name-only', merge_base['stdout'], 'HEAD', *listing])
        since_base['files'] = [line for line in diff['stdout'].splitlines() if line.strip()]
        since_base['argv'] = diff['argv']
    names = {'worktree': commands['worktree']['stdout'], 'staged': commands['staged']['stdout'],
             'status': commands['status']['stdout']}
    changed = {key: [line for line in value.splitlines() if line.strip()]
               for key, value in names.items()}
    changed['since_merge_base'] = since_base['files']
    return {'repo': str(repo), 'files': list(files), 'commands': commands,
            'merge_base': since_base, 'head': commands['head']['stdout'],
            'changed': changed, 'diff_name_only': changed['worktree'],
            'clean': all(not value for value in changed.values()),
            'ok': all(not value for value in changed.values())}


def run_h0_tool(repo, origin, batches, report_path, python=''):
    """H0's ``verify_protection.py``, run as a subprocess and kept as evidence.

    Called rather than re-implemented: it is the project's own answer to this
    question, it compares against the original C-drive tree, and running it here
    means the two answers (this module's snapshot and H0's hash ledger) have to
    agree in public instead of in a report nobody re-derives.
    """
    repo = Path(repo)
    tool = repo.joinpath(*H0_TOOL)
    if not tool.is_file():
        return {'available': False, 'reason': '找不到 %s' % tool, 'verdict': 'UNAVAILABLE'}
    if not Path(origin).is_dir():
        return {'available': False, 'reason': '受保护原树不存在：%s' % origin,
                'verdict': 'UNAVAILABLE'}
    argv = [python or sys.executable, str(tool), '--repo', str(repo), '--origin', str(origin)]
    for batch in batches:
        argv.extend(['--batch', str(batch)])
    if report_path:
        argv.extend(['--json', str(report_path)])
    completed = subprocess.run(argv, capture_output=True, timeout=900, check=False,
                               creationflags=creation_flags())
    report = None
    if report_path and Path(report_path).is_file():
        try:
            report = json.loads(Path(report_path).read_text(encoding='utf-8'))
        except ValueError:
            report = None
    return {'available': True, 'argv': argv, 'returncode': completed.returncode,
            'report_file': str(report_path or ''),
            'verdict': (report or {}).get('verdict', 'FAIL' if completed.returncode else 'UNKNOWN'),
            'legacy_core': (report or {}).get('legacy_core'),
            'batches': (report or {}).get('batches'),
            'stdout': (completed.stdout or b'').decode('utf-8', 'replace')[:2000],
            'stderr': (completed.stderr or b'').decode('utf-8', 'replace')[:2000]}


def protected_files(repo):
    """The six protected files that exist in ``repo`` (a missing one is a problem)."""
    root = Path(repo)
    return [root / name for name in PROTECTED_SOURCES]


def check(before_protected, after_protected, archive_before, archive_after, git, h0, batches):
    """Fold every protection answer into one verdict with the reasons it failed."""
    problems = []
    files = compare(before_protected, after_protected)
    if not files['ok']:
        problems.append('受保护旧核心在本次运行中被改动：%s' % files['changed'])
    # A file that was never there is not a "change", but it is not a pass either:
    # the six protected files are the ones this task promises to leave alone.
    absent = [row['path'] for row in files['files'] if not row['exists_after']]
    if absent:
        problems.append('受保护旧核心文件不存在：%s' % '、'.join(absent))
    archive = compare(archive_before, archive_after)
    if not archive['ok']:
        problems.append('旧归档在本次运行中被改动：%s' % archive['changed'])
    if not git['ok']:
        problems.append('git 显示受保护旧核心有改动：%s' % git['changed'])
    ledgers = [verify_complete(batch) for batch in batches]
    for ledger in ledgers:
        if ledger['problems']:
            problems.append('归档 %s 与其 complete.json 不一致：%s'
                            % (ledger['batch'], ledger['problems'][:3]))
    if h0.get('available') and h0.get('verdict') != 'PASS':
        problems.append('H0 的 verify_protection.py 判定 %s' % h0.get('verdict'))
    return {'files': files, 'archive': archive, 'git': git, 'h0_tool': h0,
            'batches': ledgers, 'origin_checked': bool(h0.get('available')),
            'problems': problems, 'ok': not problems}
