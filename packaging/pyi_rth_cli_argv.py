"""PyInstaller runtime hook: give the frozen exe the source run's argv contract.

``word_video_cli.py start`` re-enters the CLI as the worker process:

    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--db', ...])

In a PyInstaller ``onedir`` build ``sys.executable`` is ``WordVideo.exe`` and
``__file__`` is ``<_MEIPASS>/word_video_cli.py``, so the worker command line
carries one argument that the source run does not have.  ``argparse`` would
reject it and the packaged worker could never start.

This hook drops exactly that injected argument and nothing else, so the frozen
exe accepts byte-identical argv to ``python word_video_cli.py ...``.  The rule is
narrow on purpose:

  * only ``argv[1]`` is considered, and only when it is an absolute path;
  * only when its basename is the entry script name or the packaged exe name.

A real CLI call can never match: the first positional argument of this CLI is
always a subcommand word (``doctor``, ``submit``, ``start``, ...), never an
absolute path, so ``--db``/``--request``/``--job`` values keep their meaning.
No production code is modified by this.

Measured facts behind the rule: ``packaging/probe_frozen_argv.py``.
"""
import os
import sys

ENTRY_NAME = 'word_video_cli.py'


def normalize_argv(argv, executable=None):
    """Return ``argv`` without the script path a frozen entry injects."""
    executable = executable or sys.executable
    accepted = {ENTRY_NAME, os.path.basename(os.path.normpath(str(executable)))}
    if len(argv) > 1:
        first = str(argv[1])
        if os.path.isabs(first) and os.path.basename(os.path.normpath(first)) in accepted:
            return [argv[0]] + list(argv[2:])
    return list(argv)


if getattr(sys, 'frozen', False):
    sys.argv = normalize_argv(sys.argv)
