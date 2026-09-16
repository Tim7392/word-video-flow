"""The old subtitle factory behind a new entry: one subprocess, one report, one clock.

Why this package exists
-----------------------
Members still produce with the old factory (``Words_SRT.py`` /
``subtitle_factory_cli.py``) while the new software owns the video chain.  Without an
adapter they carry a word list between two programs by hand and then eyeball two sets
of subtitles - which is exactly the "fewer operations" the new software exists to
remove.

What it is, and what it deliberately is not
-------------------------------------------
It is a **caller**: it maps a request onto the old CLI's own JSON request, starts that
CLI in its own process with a working directory and an output directory under the D
work root, reads the result back, and re-verifies the published files from outside.

It is not a second implementation of anything.  The word list, the selection, the
packaging and the two timing numbers are the old core's, invoked as they are; the
word-based selection and the old GUI stay available through the old tool itself.  And
it is not a bridge into the old code: nothing here imports ``subtitle_factory_*``, so
a broken old module cannot reach the new process, and nothing here is imported *by*
the old chain.

Failure isolation is what the shape buys
----------------------------------------
* the new video software is never needed to produce old subtitles: this package
  imports no ``word_video`` module at import time, and the one place that reads the
  *new* timing caliber does it lazily and degrades to ``available: False`` when the
  new software cannot be imported;
* the old factory failing (bad word list, missing entry, timeout, a broken old
  module) surfaces as a structured :class:`LegacyAdapterError` and cannot corrupt the
  editor, its project, or its export.

Two timing calibers, never mixed
--------------------------------
:mod:`legacy_adapter.caliber` declares both and the CLI/dialog print the same sentence
(:data:`~legacy_adapter.caliber.UI_NOTICE`): the old entry times by **text rules**, the
new export times by **the audio it actually has**.  The user must be able to see which
one is in force, so it is a first-class field of every report.

Quick start::

    python -m legacy_adapter doctor
    python -m legacy_adapter calibers --wordlist <词表> --start 301 --end 350 \
        --batch-size 50 --timeline <归档运行目录或 timeline.json>
    python -m legacy_adapter generate --wordlist <词表> --start 1 --end 50 --batch-size 50

The two acceptance steps of W10 are modules of their own, because both are things a
person runs and reads rather than functions a program calls::

    python -m legacy_adapter.gate          # 两个真实区间的五轨逐字节/逐毫秒门禁 + 保护核对
    python -m legacy_adapter.failure_isolation   # 两个方向的最小反例（各自独立子进程）

Both write their evidence to fixed names under ``<work root>/out/reports``
(``gate-legacy-*.json``); :mod:`legacy_adapter.protection` is the shared half that
answers "did anything protected change", and :mod:`legacy_adapter.new_entry_probe` is
the small project the isolation evidence runs the *new* entry against.
"""
from .caliber import (CALIBER_CHOICES, CALIBER_LEGACY, CALIBER_MEDIA, LEGACY_FORMULA,
                      LEGACY_LABEL, MEDIA_FORMULA, MEDIA_LABEL, UI_NOTICE, caliber,
                      calibers, missing_plan_block, timeline_caliber)
from .environment import (PROTECTED_ROOTS, check_location, default_output_dir,
                          default_python, default_work_dir, locate_legacy_entry,
                          new_run_dir, run_root, work_root)
from .errors import EXIT_CODES, LegacyAdapterError
from .report import compare_runs, parse_track, read_run_tracks, track_facts
from .request import (DEFAULT_EXTRA, DEFAULT_FIRST_SIX, DEFAULT_TIMEOUT, LegacyRequest,
                      from_mapping)
from .runner import SCHEMA, VERSION, plan_legacy, run_legacy

__all__ = ['CALIBER_CHOICES', 'CALIBER_LEGACY', 'CALIBER_MEDIA', 'DEFAULT_EXTRA',
           'DEFAULT_FIRST_SIX', 'DEFAULT_TIMEOUT', 'EXIT_CODES', 'LEGACY_FORMULA',
           'LEGACY_LABEL', 'LegacyAdapterError', 'LegacyRequest', 'MEDIA_FORMULA',
           'MEDIA_LABEL', 'PROTECTED_ROOTS', 'SCHEMA', 'UI_NOTICE', 'VERSION', 'caliber',
           'calibers', 'check_location', 'compare_runs', 'default_output_dir',
           'default_python', 'default_work_dir', 'from_mapping', 'locate_legacy_entry',
           'missing_plan_block', 'new_run_dir', 'parse_track', 'plan_legacy',
           'read_run_tracks', 'run_legacy', 'run_root', 'timeline_caliber', 'track_facts',
           'work_root']
