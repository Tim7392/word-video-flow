"""Read the old tool's own output back, and say - per cue - whether two runs agree.

The equivalence this task has to prove is exact, so the comparison is exact too: a
track is compared **byte for byte**, then cue by cue on milliseconds and on text.
"Nearly equal" is not a verdict this module can produce; it reports the first
differing cue with its number, its word and both time ranges, and the caller either
accepts the file as identical or has to explain each difference by hand.

Parsing lives here rather than being borrowed from the exporting side on purpose:
this is *verification* of a published artefact, and a parser that shares code with
the writer can share its bug.  It reads the four fields the format defines
(sequence number, start, end, text) and nothing else.
"""
import hashlib
import json
import re
from pathlib import Path

from .errors import INVALID_REQUEST, LEGACY_OUTPUT_MISSING, LegacyAdapterError

#: ``00:00:00,000 --> 00:00:01,500``; the legacy format, to the millisecond.
TIME = r'(?P<start>\d{1,3}:\d{2}:\d{2},\d{3}) --> (?P<end>\d{1,3}:\d{2}:\d{2},\d{3})'
#: One cue: number, time line, text, blank line.  Named groups on purpose: the time
#: line already owns two groups, and a numbered group here is one edit away from
#: silently reading the end timestamp as the cue's text (found exactly that way).
CUE = re.compile(r'^(?P<number>\d+)\r?\n' + TIME + r'\r?\n(?P<text>.*?)\r?\n\r?\n',
                 re.MULTILINE | re.DOTALL)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def to_milliseconds(stamp):
    """``00:01:02,345`` -> 62345.  Hours are not wrapped: the old core never wraps."""
    hours, minutes, rest = stamp.split(':')
    seconds, millis = rest.split(',')
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def parse_track(text):
    """Every cue in one track, in file order: number, start/end ms, text."""
    cues = []
    for match in CUE.finditer(text):
        cues.append({'number': int(match.group('number')),
                     'start_ms': to_milliseconds(match.group('start')),
                     'end_ms': to_milliseconds(match.group('end')),
                     'text': match.group('text')})
    return cues


def track_facts(path):
    """What one published track is: bytes, hash, cue count and its time span."""
    source = Path(path)
    if not source.is_file():
        raise LegacyAdapterError(LEGACY_OUTPUT_MISSING, '轨道文件不存在：%s' % source,
                                 details={'path': str(source)})
    raw = source.read_bytes()
    text = raw.decode('utf-8-sig')
    cues = parse_track(text)
    return {'path': str(source), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'cues': len(cues), 'text': text, 'parsed': cues,
            'first_start_ms': cues[0]['start_ms'] if cues else None,
            'last_end_ms': cues[-1]['end_ms'] if cues else None,
            'texts_sha256': hashlib.sha256(
                '\n'.join(cue['text'] for cue in cues).encode('utf-8')).hexdigest()}


def read_text(path):
    """Read a text artefact the way the old tool reads its own inputs.

    Same encoding ladder as the protected core's ``_read_source`` (UTF-16 when the
    BOM says so, then UTF-8 with BOM, then GB18030), because the file this compares
    may well be one a member saved from a Windows shell - and refusing to compare a
    correct answer would be a worse failure than accepting it.
    """
    raw = Path(path).read_bytes()
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LegacyAdapterError(INVALID_REQUEST, '无法识别文本编码：%s' % path,
                             details={'path': str(path)})


def read_run_tracks(source):
    """``{track index: facts}`` from a run report, a legacy response, or a folder.

    Three shapes are accepted because three readers have to agree: this adapter's
    own report, the old CLI's raw stdout (the "old entry produced this" evidence),
    and a directory that holds a ``legacy-run.json``.
    """
    document = source
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            path = path / 'legacy-run.json'
        if not path.is_file():
            raise LegacyAdapterError(INVALID_REQUEST, '读不到运行报告：%s' % path,
                                     details={'path': str(path)})
        try:
            document = json.loads(read_text(path))
        except ValueError as error:
            raise LegacyAdapterError(INVALID_REQUEST, '运行报告不是 JSON：%s' % error,
                                     details={'path': str(path)}) from None
    if not isinstance(document, dict):
        raise LegacyAdapterError(INVALID_REQUEST, '运行报告必须是 JSON 对象。')
    rows = document.get('tracks')
    if rows is None and isinstance(document.get('result'), dict):
        rows = document['result'].get('files')
    if rows is None:
        rows = document.get('files')
    if not rows:
        raise LegacyAdapterError(INVALID_REQUEST,
                                 '运行报告里没有 tracks/files，无法对账。',
                                 details={'keys': sorted(document)})
    tracks = {}
    for row in rows:
        facts = track_facts(row['path'])
        facts.update({'track': int(row['track']), 'relative_path': row.get('relative_path', ''),
                      'recorded_sha256': row.get('sha256', ''),
                      'recorded_bytes': row.get('bytes')})
        tracks[int(row['track'])] = facts
    return tracks


def compare_runs(left, right, *, limit=20):
    """Cue-exact comparison of two runs of five tracks, keyed by track index.

    The verdict is one of ``identical`` / ``times_differ`` / ``text_differs`` /
    ``bytes_differ`` per track.  ``times_differ`` and ``text_differs`` are produced
    from the parsed cues, so the caller gets the *word* and the two ranges instead of
    a byte offset; ``bytes_differ`` without a cue difference means the files differ
    somewhere this parser does not model (BOM, line endings, trailing newline), and it
    is reported that way rather than being rounded into "the same".
    """
    first, second = read_run_tracks(left), read_run_tracks(right)
    missing = sorted(set(first) - set(second)) + sorted(set(second) - set(first))
    tracks, differences = [], []
    for index in sorted(set(first) & set(second)):
        mine, theirs = first[index], second[index]
        rows = {'track': index, 'left': mine['path'], 'right': theirs['path'],
                'left_bytes': mine['bytes'], 'right_bytes': theirs['bytes'],
                'left_sha256': mine['sha256'], 'right_sha256': theirs['sha256'],
                'left_cues': mine['cues'], 'right_cues': theirs['cues'],
                'bytes_identical': mine['sha256'] == theirs['sha256']}
        cue_diffs = []
        for position in range(max(mine['cues'], theirs['cues'])):
            one = mine['parsed'][position] if position < mine['cues'] else None
            two = theirs['parsed'][position] if position < theirs['cues'] else None
            if one == two:
                continue
            cue_diffs.append({'position': position + 1,
                              'left': None if one is None else _cue(one),
                              'right': None if two is None else _cue(two)})
            if len(cue_diffs) >= limit:
                break
        rows['cue_differences'] = cue_diffs
        rows['times_identical'] = not cue_diffs and mine['cues'] == theirs['cues']
        rows['texts_identical'] = (mine['texts_sha256'] == theirs['texts_sha256'])
        if not rows['bytes_identical']:
            rows['verdict'] = ('text_differs' if not rows['texts_identical']
                               else 'times_differ' if cue_diffs else 'bytes_differ')
        else:
            rows['verdict'] = 'identical'
        tracks.append(rows)
        differences.extend({'track': index, 'word': _word(item), **item} for item in cue_diffs)
    return {'schema': 'wv-legacy-compare@1', 'tracks': tracks,
            'tracks_left': sorted(first), 'tracks_right': sorted(second),
            'tracks_missing': missing,
            'verdict': ('identical' if tracks and not missing
                        and all(row['verdict'] == 'identical' for row in tracks)
                        else 'differs'),
            'difference_count': len(differences), 'differences': differences[:limit]}


def _word(difference):
    """Which word a cue difference is about, so the report can be read by a human."""
    for side in ('left', 'right'):
        cue = difference.get(side)
        if cue and cue.get('word'):
            return cue['word']
    return ''


def _cue(cue):
    """One cue, shrunk to what a human needs to find it in either file."""
    return {'number': cue['number'], 'start_ms': cue['start_ms'], 'end_ms': cue['end_ms'],
            'word': cue['text'].splitlines()[0] if cue['text'] else ''}


def summarize_packages(result):
    """The old core's own per-package numbers, as the run report publishes them."""
    packages = []
    for package in result.get('packages') or ():
        packages.append({'number': package.get('number'), 'word_count': package.get('word_count'),
                         'first_word': package.get('first_word'), 'last_word': package.get('last_word'),
                         'duration_ms': package.get('duration_ms')})
    return packages
