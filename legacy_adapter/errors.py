"""Structured failures, in the code style the rest of the tree already uses.

Every refusal leaves this adapter as ``{code, message, details, hint, exit_code}`` -
the same shape the legacy CLI answers with and the same shape the editor's notice
cards render.  The codes the protected core raises (``INPUT_ERROR`` and friends) are
**passed through unchanged**, so a caller that already knows the old tool keeps its
diagnosis; the ``LEGACY_*`` codes below only describe what this layer added: finding
the old CLI, starting it, and reading back what it published.

Exit codes follow the legacy tool's own table, and each new code borrows the class
its failure belongs to: 2 = the request is wrong, 3 = the input is wrong, 4 = the
output/environment cannot be used, 5 = a previous or partial run is in the way,
1 = the protocol broke.
"""

INVALID_REQUEST = 'INVALID_REQUEST'
INPUT_ERROR = 'INPUT_ERROR'
OUTPUT_ERROR = 'OUTPUT_ERROR'
IDEMPOTENCY_CONFLICT = 'IDEMPOTENCY_CONFLICT'
INCOMPLETE_RUN = 'INCOMPLETE_RUN'
OUTPUT_CHANGED = 'OUTPUT_CHANGED'
INTERNAL_ERROR = 'INTERNAL_ERROR'
INTERRUPTED = 'INTERRUPTED'

#: The old CLI was not found (or no interpreter that could run it).
LEGACY_ENTRY_MISSING = 'LEGACY_ENTRY_MISSING'
#: The old CLI could not be started at all (``OSError`` from the spawn).
LEGACY_LAUNCH_FAILED = 'LEGACY_LAUNCH_FAILED'
#: The requested working directory or output is inside something protected.
LEGACY_OUTPUT_UNSAFE = 'LEGACY_OUTPUT_UNSAFE'
#: The old CLI reported success but a file it named is not on disk.
LEGACY_OUTPUT_MISSING = 'LEGACY_OUTPUT_MISSING'
#: The old CLI did not finish inside the timeout; it may have published partly.
LEGACY_TIMEOUT = 'LEGACY_TIMEOUT'
#: The old CLI's stdout was not the one JSON object its contract promises.
LEGACY_BAD_RESPONSE = 'LEGACY_BAD_RESPONSE'

EXIT_CODES = {
    INVALID_REQUEST: 2,
    INPUT_ERROR: 3,
    OUTPUT_ERROR: 4,
    IDEMPOTENCY_CONFLICT: 5,
    INCOMPLETE_RUN: 5,
    OUTPUT_CHANGED: 5,
    INTERNAL_ERROR: 1,
    INTERRUPTED: 130,
    LEGACY_ENTRY_MISSING: 4,
    LEGACY_LAUNCH_FAILED: 4,
    LEGACY_OUTPUT_UNSAFE: 4,
    LEGACY_OUTPUT_MISSING: 4,
    LEGACY_TIMEOUT: 5,
    LEGACY_BAD_RESPONSE: 1,
}

#: What a member (or a support call) should try next, per code.  Kept next to the
#: codes so a new refusal cannot be added without an answer to "then what".
HINTS = {
    LEGACY_ENTRY_MISSING: '确认仓库根目录（或包目录）里有 subtitle_factory_cli.py，'
                          '或用 --legacy-script 显式指定；旧核心本身不改。',
    LEGACY_LAUNCH_FAILED: '检查 --legacy-python 指向的解释器是否存在、能否运行。',
    LEGACY_OUTPUT_UNSAFE: '把输出与工作目录指到 D 盘的新目录：旧核心、旧归档与旧草稿都不可写。',
    LEGACY_OUTPUT_MISSING: '旧核心报告成功但文件不在磁盘上：先查磁盘空间与杀毒软件拦截，再重跑。',
    LEGACY_TIMEOUT: '加大 --timeout 或用更小的选区重跑；带 key 的任务可用 status 核验。',
    LEGACY_BAD_RESPONSE: '旧 CLI 的 stdout 不是单个 JSON：确认调的是 subtitle_factory_cli.py，'
                         '而不是旧 GUI（Words_SRT.py）。',
}


class LegacyAdapterError(Exception):
    """One refusal: a known code, a human message, and the facts that located it."""

    def __init__(self, code, message, details=None, hint=''):
        super().__init__(message)
        self.code = code
        self.message = str(message)
        self.details = dict(details or {})
        self.hint = hint or HINTS.get(code, '')
        self.exit_code = EXIT_CODES.get(code, 1)

    def to_dict(self):
        return {'ok': False, 'code': self.code, 'message': self.message,
                'details': self.details, 'hint': self.hint,
                'exit_code': self.exit_code}

    def __str__(self):
        return '%s: %s' % (self.code, self.message)
