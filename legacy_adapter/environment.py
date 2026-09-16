"""Where the old CLI is, which interpreter runs it, and where nothing may be written.

The task's rule is "调用旧 CLI、工作目录与输出都在 D 盘新目录下" and the project's
rule is "原字幕核心、原入口、旧归档受保护".  Those two sentences are implemented
here as one check with one failure code:

* the **old CLI is located, never imported** - this module hands a path to a
  subprocess; nothing in ``legacy_adapter`` imports ``subtitle_factory_*`` into the
  new process, so a syntax error or a missing PyQt6 in the old tree cannot reach it;
* **nothing is written into the old tree**: the output directory and the working
  directory may not be inside the directory that holds the old CLI, and no path may
  be inside a protected root (the old core's original tree, the delivered archive,
  the Jianying draft folder).  This is a refusal, not a warning: the only acceptable
  cost of a mistake here is a failed run, not a rewritten archive;
* the **defaults are on D:** - ``<work root>/out/legacy/<run id>`` - and the working
  directory is also handed to the child as its ``TEMP``/``TMP``, so even the
  throwaway files the old core stages (``subtitle_factory_api.plan`` renders into a
  temporary directory) land in the D directory this adapter made, not in ``%TEMP%``
  on the system drive.

Why the drive letter is not itself enforced: a member machine may have no ``D:``
at all, and a rule that refuses to run there would trade a real capability for a
letter.  What is enforced is what the rule protects - **never inside the old tree,
never inside an archive** - and every path this adapter picks by default is under
the D work root, which the run report prints so the claim is checkable.
"""
import os
import sys
import time
import uuid
from pathlib import Path

from .errors import (INPUT_ERROR, LEGACY_ENTRY_MISSING, LEGACY_OUTPUT_UNSAFE,
                     LegacyAdapterError)

#: Read-only by rule.  A path inside one of these is refused before anything runs.
PROTECTED_ROOTS = (
    r'C:\Users\Administrator\PycharmProjects\PythonSRT',
    r'D:\单词速记自动化_测试归档_0915',
    r'D:\jianying\JianyingPro Drafts',
)

#: The old CLI's entry, as it is named in the original tree and in this repository.
LEGACY_SCRIPT_NAME = 'subtitle_factory_cli.py'

WORK_ROOT_ENV = 'WORD_VIDEO_WORK_ROOT'
ENTRY_ENV = 'WORD_VIDEO_LEGACY_FACTORY'
PYTHON_ENV = 'WORD_VIDEO_LEGACY_PYTHON'
DEFAULT_WORK_ROOT = r'D:\1\1-AI_workflow\word_video_flow'


def work_root():
    """The D work root, from the environment when H0 (or a member) moved it."""
    return Path(os.environ.get(WORK_ROOT_ENV) or DEFAULT_WORK_ROOT)


def run_root():
    """Where this adapter puts runs: one new directory per invocation."""
    return work_root() / 'out' / 'legacy'


def _run_path(label=''):
    """A fresh, unique path under the run root; the caller decides whether to make it."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = '-'.join(part for part in (stamp, _safe(label), uuid.uuid4().hex[:6]) if part)
    return run_root() / name


def new_run_dir(label=''):
    """A fresh, empty run directory on D; nothing is ever reused or merged."""
    target = _run_path(label)
    target.mkdir(parents=True, exist_ok=False)
    return target


def default_output_dir():
    """Where the five tracks go when the caller did not say.

    Returned, not created: the old core creates the output directory itself
    (``_generate_packages`` does ``mkdir(parents=True, exist_ok=True)``), and a
    directory made "just in case" is a directory left behind by a run that failed.
    """
    return str(_run_path('tracks'))


def default_work_dir():
    """A fresh working directory for the child; the runner creates it."""
    return str(_run_path('work'))


def _safe(text):
    return ''.join(character for character in str(text)
                   if character.isalnum() or character in '-_')[:24]


def _resolved(path):
    try:
        return Path(path).expanduser().resolve()
    except OSError:                                 # a path that cannot exist yet
        return Path(os.path.abspath(str(path)))


def protected_roots():
    return tuple(Path(root) for root in PROTECTED_ROOTS)


def is_within(path, folder):
    try:
        return _resolved(path).is_relative_to(_resolved(folder))
    except ValueError:
        return False


def check_location(path, *, role, legacy_entry=''):
    """Refuse a working/output path that must not be written. Returns the path used.

    ``role`` only names the thing in the message ("输出目录"/"工作目录"), because a
    member reading a refusal needs to know *which* box to fix.  The factory directory
    is found from the entry when the caller named one, and from the usual places when
    it did not - so a request is refused at validation time even before a run starts.
    """
    if not str(path or '').strip():
        raise LegacyAdapterError(INPUT_ERROR, '%s不能为空。' % role,
                                 details={'role': role, 'path': str(path)})
    target = _resolved(path)
    for root in protected_roots():
        if target == root or target.is_relative_to(root):
            raise LegacyAdapterError(
                LEGACY_OUTPUT_UNSAFE, '%s落在受保护目录里：%s' % (role, target),
                details={'role': role, 'path': str(target), 'protected_root': str(root),
                         'candidates': [str(item) for item in protected_roots()]})
    factory = _factory_dir(legacy_entry)
    if factory is not None and (target == factory or target.is_relative_to(factory)):
        raise LegacyAdapterError(
            LEGACY_OUTPUT_UNSAFE, '%s落在旧字幕工厂目录里：%s' % (role, target),
            details={'role': role, 'path': str(target), 'factory_dir': str(factory),
                     'hint': '旧核心与旧入口一字不改：输出与工作目录都在 D 盘新目录下'})
    return target


def _factory_dir(legacy_entry=''):
    """The old CLI's directory, or ``None`` when it cannot be found (no exception)."""
    if str(legacy_entry or '').strip():
        return _resolved(legacy_entry).parent
    try:
        return locate_legacy_entry().parent
    except LegacyAdapterError:
        return None


def default_python():
    """The interpreter that runs the old CLI.

    From source this is the interpreter running the adapter (the same locked venv),
    which is what makes a dev run and a support run identical.  In a frozen window
    there is no interpreter to reuse, so the locked venv beside the work root is the
    documented fallback - and its absence is reported, not guessed at.
    """
    override = os.environ.get(PYTHON_ENV)
    if override:
        return str(Path(override).expanduser())
    if not getattr(sys, 'frozen', False):
        return sys.executable
    candidate = work_root() / 'runtime' / 'venv' / 'Scripts' / 'python.exe'
    return str(candidate) if candidate.is_file() else ''


def _candidate_entries():
    """Every place the old CLI is allowed to be, best first."""
    here = Path(__file__).resolve().parent
    roots = [Path(os.environ[ENTRY_ENV]).expanduser()] if os.environ.get(ENTRY_ENV) else []
    roots += [here.parent,                             # the repository root, from source
              work_root() / 'repo',                    # the main worktree
              Path.cwd(),
              Path(sys.executable).resolve().parent]   # a packaged directory
    seen, candidates = set(), []
    for root in roots:
        candidate = root / LEGACY_SCRIPT_NAME if root.is_dir() else root
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def locate_legacy_entry(explicit=''):
    """The old CLI's path, or ``LEGACY_ENTRY_MISSING`` listing everywhere it looked."""
    if str(explicit or '').strip():
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise LegacyAdapterError(
                LEGACY_ENTRY_MISSING, '指定的旧 CLI 不存在：%s' % path,
                details={'explicit': str(path), 'candidates': []})
        return path.resolve()
    candidates = _candidate_entries()
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise LegacyAdapterError(
        LEGACY_ENTRY_MISSING, '找不到旧字幕工厂入口 %s（旧核心不改，只做外层适配）。'
        % LEGACY_SCRIPT_NAME,
        details={'candidates': [str(item) for item in candidates],
                 'env': ENTRY_ENV, 'explicit_flag': '--legacy-script'})
