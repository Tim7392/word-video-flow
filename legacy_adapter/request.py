"""One request, mapped onto the old CLI's own JSON request - and nothing more.

The whole point of this layer is that it has **no second business**: the word list
is read by the old core (``source.path``), the selection is the old core's one-based
inclusive ``range``, the packaging is the old core's ``batch_size``, and the two
timing numbers are the old core's ``timing`` fields with the same defaults its own
GUI and CLI use (``first_six = 0.4``, ``extra = 0.2``).  This module therefore does
one thing: it validates what a caller typed, fills in the D-drive defaults, and
serialises the request object the old CLI already understands.

Two behaviours are worth naming, because guessing either one would be a second
implementation:

* an omitted ``end`` means "to the end of the list" - that is what the old core does
  with a missing ``range.end`` - so this layer does not count words to fill it in;
* an omitted ``batch_size`` means "one package", again the old core's own rule.
"""
from dataclasses import dataclass, field, replace
import math
from pathlib import Path

from .environment import check_location, default_output_dir, default_work_dir
from .errors import INPUT_ERROR, INVALID_REQUEST, LegacyAdapterError

#: The old tool's own defaults (its CLI and GUI both use these numbers).
DEFAULT_FIRST_SIX = 0.4
DEFAULT_EXTRA = 0.2
DEFAULT_TIMEOUT = 900.0


def _positive_number(value, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise LegacyAdapterError(INVALID_REQUEST, '%s 必须是有限的正数。' % name,
                                 details={'field': name, 'value': repr(value)})
    return float(value)


def _positive_int(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LegacyAdapterError(INVALID_REQUEST, '%s 必须是大于等于 %d 的整数。' % (name, minimum),
                                 details={'field': name, 'value': repr(value)})
    return value


@dataclass(frozen=True)
class LegacyRequest:
    """What the member asked for: a word list, a selection, a package size, an output."""

    wordlist: str = ''
    start: int | None = None
    end: int | None = None
    batch_size: int | None = None
    first_six: float = DEFAULT_FIRST_SIX
    extra: float = DEFAULT_EXTRA
    output: str = ''
    work_dir: str = ''
    timeline: str = ''
    idempotency_key: str = ''
    legacy_script: str = ''
    legacy_python: str = ''
    timeout: float = DEFAULT_TIMEOUT
    #: Filled in by :func:`resolve` so a report can print exactly what was used.
    resolved: dict = field(default_factory=dict, compare=False)

    # -- validation ------------------------------------------------------
    def checked(self):
        """A request that can be run: every field validated, defaults made explicit."""
        if not str(self.wordlist or '').strip():
            raise LegacyAdapterError(INPUT_ERROR, '必须指定词表文件（TXT / DOCX / 词表 SRT）。',
                                     details={'field': 'wordlist'})
        wordlist = Path(self.wordlist).expanduser()
        if not wordlist.is_file():
            raise LegacyAdapterError(INPUT_ERROR, '词表文件不存在：%s' % wordlist,
                                     details={'path': str(wordlist), 'field': 'wordlist'})
        start = None if self.start is None else _positive_int(self.start, 'range.start')
        end = None if self.end is None else _positive_int(self.end, 'range.end')
        if start is not None and end is not None and start > end:
            raise LegacyAdapterError(INVALID_REQUEST, '起止序号写反了：start=%d > end=%d' % (start, end),
                                     details={'start': start, 'end': end})
        batch = None if self.batch_size is None else _positive_int(self.batch_size, 'batch_size')
        first_six = _positive_number(self.first_six, 'timing.first_six')
        extra = _positive_number(self.extra, 'timing.extra')
        timeout = _positive_number(self.timeout, 'timeout')
        return replace(self, wordlist=str(wordlist.resolve()), start=start, end=end,
                       batch_size=batch, first_six=first_six, extra=extra, timeout=timeout)

    def resolve(self):
        """``checked()`` plus the D-drive working and output directories."""
        request = self.checked()
        output = str(request.output or '').strip() or default_output_dir()
        work = str(request.work_dir or '').strip() or default_work_dir()
        output = str(check_location(output, role='输出目录',
                                    legacy_entry=request.legacy_script))
        work = str(check_location(work, role='工作目录',
                                  legacy_entry=request.legacy_script))
        markers = {'output': output, 'work_dir': work,
                   'first_six': request.first_six, 'extra': request.extra,
                   'batch_size': request.batch_size,
                   'selection': {'start': request.start, 'end': request.end},
                   'selection_label': _selection_label(request.start, request.end)}
        return replace(request, output=output, work_dir=work, resolved=markers)

    # -- mapping onto the old CLI ----------------------------------------
    def to_legacy_request(self, action='generate'):
        """The JSON object the old CLI's ``request`` action reads, verbatim.

        Field names and the shape of ``range``/``timing`` are the old tool's; this
        is a mapping, not a dialect.  ``start_word``/``end_word`` are deliberately
        absent: this entry selects by number, and the old core's word-based
        selection stays available through the old CLI itself.
        """
        request = {'action': action, 'source': {'path': str(self.wordlist)},
                   'timing': {'first_six': float(self.first_six), 'extra': float(self.extra)}}
        span = {}
        if self.start is not None:
            span['start'] = int(self.start)
        if self.end is not None:
            span['end'] = int(self.end)
        if span:
            request['range'] = span
        if self.batch_size is not None:
            request['batch_size'] = int(self.batch_size)
        if self.idempotency_key:
            request['idempotency_key'] = str(self.idempotency_key)
        if action == 'generate':
            request['output'] = str(self.output)
        return request

    def to_dict(self):
        return {'wordlist': str(self.wordlist), 'start': self.start, 'end': self.end,
                'batch_size': self.batch_size, 'first_six': self.first_six,
                'extra': self.extra, 'output': str(self.output),
                'work_dir': str(self.work_dir), 'timeline': str(self.timeline),
                'idempotency_key': self.idempotency_key,
                'legacy_script': str(self.legacy_script),
                'legacy_python': str(self.legacy_python), 'timeout': self.timeout,
                # The paths and numbers actually used, after defaults were filled in.
                'resolved': dict(self.resolved)}


def _selection_label(start, end):
    """How the selection reads to a member ('第 1～50 个词' / '从第 301 个词起')."""
    if start is None and end is None:
        return '全部词条'
    if start is None:
        return '第 1～%d 个词' % end
    if end is None:
        return '从第 %d 个词起' % start
    return '第 %d～%d 个词' % (start, end)


def from_mapping(values):
    """Build a request from a JSON-ish mapping (the editor's dialog and the CLI)."""
    known = {'wordlist', 'start', 'end', 'batch_size', 'first_six', 'extra', 'output',
             'work_dir', 'timeline', 'idempotency_key', 'legacy_script', 'legacy_python',
             'timeout'}
    unknown = sorted(set(values) - known)
    if unknown:
        raise LegacyAdapterError(INVALID_REQUEST, '请求含未知字段：%s' % ', '.join(unknown),
                                 details={'unknown': unknown, 'known': sorted(known)})
    payload = {key: value for key, value in values.items() if value not in (None, '')}
    return LegacyRequest(**payload)
