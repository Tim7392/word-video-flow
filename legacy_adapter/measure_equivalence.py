"""W10's hard indicator as one command: old entry vs new entry, track by track.

The claim this script tests is exact equality, so it is tested the hard way: the
**same request document** is handed to the old CLI directly (its own stdout is kept
as the "old entry" evidence) and to this adapter, and the five published tracks of
both runs are compared byte for byte, then cue by cue on milliseconds and text.

It also prints the two timing calibers side by side for the same selection - the old
rule's real numbers from the old core's own plan, the new rule's real numbers from a
delivered ``timeline.json`` - because "the numbers differ" is the point of labelling
them, not a bug to hide.

    python -m legacy_adapter.measure_equivalence \
        --wordlist D:\\...\\data\\wordlists\\四级核心1500词_已清理.txt \
        --start 301 --end 350 --batch-size 50 \
        --timeline "D:\\单词速记自动化_测试归档_0915\\...\\0301-0350" \
        --report D:\\...\\out\\reports\\legacy-equivalence.json

Exit code 0 means every track is identical and every published file verified; 1 means
at least one difference, which the report lists one by one.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .caliber import timeline_caliber
from .environment import check_location, default_python, locate_legacy_entry, run_root
from .report import compare_runs, parse_track, read_text, sha256
from .request import DEFAULT_EXTRA, DEFAULT_FIRST_SIX, LegacyRequest
from .runner import creation_flags, legacy_environment, run_legacy, write_json

SCHEMA = 'wv-legacy-equivalence@1'


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('必须是正整数')
    return number


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m legacy_adapter.measure_equivalence',
                                     description='旧入口 vs 新入口：五轨 SRT 逐字节/逐毫秒对照')
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--start', type=_positive_int, default=None)
    parser.add_argument('--end', type=_positive_int, default=None)
    parser.add_argument('--batch-size', type=_positive_int, default=None)
    parser.add_argument('--first-six', type=float, default=DEFAULT_FIRST_SIX)
    parser.add_argument('--extra', type=float, default=DEFAULT_EXTRA)
    parser.add_argument('--root', default=str(run_root() / 'equivalence'),
                        help='本次对照的工作根（默认 D 盘 out/legacy/equivalence）')
    parser.add_argument('--timeline', default='', help='新口径数值来源：归档运行目录或 timeline.json')
    parser.add_argument('--legacy-script', default='')
    parser.add_argument('--legacy-python', default='',
                        help='运行旧 CLI 的解释器；两侧（旧入口直调与新入口适配器）都用它')
    parser.add_argument('--report', default='', help='把对照报告写到这个文件')
    parser.add_argument('--label', default='equivalence', help='本次运行的标签，进目录名')
    return parser


def run_old_entry(request, entry, python):
    """The old CLI, called directly: the yardstick the new entry has to match."""
    work = Path(request.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    request_file = write_json(work / 'legacy-request.json', request.to_legacy_request('generate'))
    argv = [python, '-B', str(entry), 'request', '--json-file', str(request_file)]
    started = time.perf_counter()
    completed = subprocess.run(argv, cwd=str(work), env=legacy_environment(work),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=float(request.timeout), check=False,
                               creationflags=creation_flags())
    seconds = round(time.perf_counter() - started, 3)
    stdout_path = work / 'old-entry-stdout.json'
    stdout_path.write_bytes(completed.stdout or b'')
    response = json.loads((completed.stdout or b'').decode('utf-8-sig') or '{}')
    return {'argv': argv, 'returncode': completed.returncode, 'seconds': seconds,
            'stdout_file': str(stdout_path), 'response': response,
            'stderr': (completed.stderr or b'').decode('utf-8', 'replace')[-2000:]}


def archive_words(batch):
    """Per-word times from a delivered ``timeline.json`` (the new caliber, real)."""
    source = Path(batch)
    if source.is_dir():
        source = source / 'timeline.json'
    if not source.is_file():
        return []
    data = json.loads(source.read_text(encoding='utf-8-sig'))
    fps = data.get('fps') or 60
    rows = []
    for word in data.get('words') or ():
        rows.append({'index': word.get('index'), 'word': word.get('word'),
                     'fps': fps,
                     'end_frame': word.get('end_frame'), 'start_frame': word.get('start_frame'),
                     'end_ms': int(round(int(word['end_frame']) * 1000.0 / float(fps)))})
    return rows


def legacy_words(track_path):
    """Per-word cumulative end from the old entry's own track 02 (the old caliber)."""
    if not Path(track_path).is_file():
        return []
    rows = []
    for position, cue in enumerate(parse_track(read_text(track_path)), start=1):
        rows.append({'position': position, 'word': cue['text'].splitlines()[0] if cue['text'] else '',
                     'start_ms': cue['start_ms'], 'end_ms': cue['end_ms']})
    return rows


def per_word_table(new_report, archive_batch):
    """The two calibers, word by word, for one and the same selection."""
    track_two = None
    for row in new_report.get('tracks') or ():
        if int(row.get('track') or 0) == 2:
            track_two = row['path']
    old_rows = legacy_words(track_two) if track_two else []
    media_rows = archive_words(archive_batch) if archive_batch else []
    words = []
    for position, old in enumerate(old_rows):
        media = media_rows[position] if position < len(media_rows) else None
        words.append({'position': position + 1, 'word': old['word'],
                      'legacy_end_ms': old['end_ms'],
                      'media_end_ms': None if media is None else media['end_ms'],
                      'delta_ms': (None if media is None else old['end_ms'] - media['end_ms']),
                      'same_word': (None if media is None else old['word'] == media['word'])})
    deltas = [row['delta_ms'] for row in words if row['delta_ms'] is not None]
    return {'track': track_two, 'archive_batch': str(archive_batch or ''),
            'words': words,
            'summary': {'words': len(words), 'compared': len(deltas),
                        'same_words': sum(1 for row in words if row['same_word']),
                        'legacy_end_ms': words[-1]['legacy_end_ms'] if words else None,
                        'media_end_ms': (words[-1]['media_end_ms'] if words
                                         and words[-1]['media_end_ms'] is not None else None),
                        'delta_ms': (words[-1]['delta_ms'] if words
                                     and words[-1]['delta_ms'] is not None else None),
                        'min_delta_ms': min(deltas) if deltas else None,
                        'max_delta_ms': max(deltas) if deltas else None}}


def main(argv=None):
    args = build_parser().parse_args(argv)
    entry = locate_legacy_entry(args.legacy_script)
    root = Path(args.root)
    old_output, new_output = root / 'old-entry', root / 'new-entry'
    old_work, new_work = root / 'old-work', root / 'new-work'
    common = dict(wordlist=args.wordlist, start=args.start, end=args.end,
                  batch_size=args.batch_size, first_six=args.first_six, extra=args.extra,
                  legacy_script=str(entry), legacy_python=str(args.legacy_python or ''))
    old_request = LegacyRequest(**common, output=str(old_output),
                                work_dir=str(old_work)).resolve()
    for path, role in ((old_output, '输出目录'), (new_output, '输出目录'),
                       (old_work, '工作目录'), (new_work, '工作目录')):
        check_location(path, role=role, legacy_entry=str(entry))
    old = run_old_entry(old_request, entry, old_request.legacy_python or default_python())
    new = run_legacy(LegacyRequest(**common, output=str(new_output),
                                   work_dir=str(new_work), timeline=args.timeline),
                     action='generate', report_path=str(root / 'new-entry-report.json'))
    comparison = compare_runs(old['stdout_file'], root / 'new-entry-report.json')
    table = per_word_table(new, args.timeline)
    media = timeline_caliber(args.timeline) if str(args.timeline or '').strip() else None
    report = {'schema': SCHEMA, 'ok': comparison['verdict'] == 'identical',
              'label': args.label, 'root': str(root),
              'inputs': {'wordlist': str(Path(args.wordlist).resolve()),
                         'start': args.start, 'end': args.end,
                         'batch_size': args.batch_size, 'first_six': args.first_six,
                         'extra': args.extra, 'timeline': str(args.timeline or ''),
                         'wordlist_sha256': sha256(Path(args.wordlist))},
              'old_entry': {'entry': str(entry), 'argv': old['argv'],
                            'returncode': old['returncode'], 'seconds': old['seconds'],
                            'stdout_file': old['stdout_file'],
                            'directory': old['response'].get('result', {}).get('directory')},
              'new_entry': {key: new[key] for key in
                            ('ok', 'adapter', 'request', 'counts', 'directory')},
              'calibers': {'legacy': new['caliber']['measured'],
                           'media': media},
              'archive_reference': table,
              'comparison': comparison}
    if args.report:
        write_json(args.report, report)
        report['report_file'] = str(Path(args.report))
    _print_summary(report)
    return 0 if report['ok'] else 1


def _print_summary(report):
    comparison = report['comparison']
    print('== 旧入口 vs 新入口（同一份请求文档，同一词表/选区/分包/计时参数）==')
    print('%-6s %-10s %-16s %-10s %-16s %-6s %s' % ('轨道', '旧 bytes', '旧 sha256[:16]',
                                                     '新 bytes', '新 sha256[:16]', 'cues', '判定'))
    for row in comparison['tracks']:
        print('%-6s %-10s %-16s %-10s %-16s %-6s %s'
              % (row['track'], row['left_bytes'], row['left_sha256'][:16], row['right_bytes'],
                 row['right_sha256'][:16], row['left_cues'], row['verdict']))
    print('汇总：%d/%d 轨逐字节相同，毫秒差异 %d 处，缺失轨道 %s，verdict=%s'
          % (sum(1 for row in comparison['tracks'] if row['bytes_identical']),
             len(comparison['tracks']), comparison['difference_count'],
             comparison['tracks_missing'] or '无', comparison['verdict'].upper()))
    legacy, media = report['calibers']['legacy'], report['calibers']['media']
    print('\n== 两种计时口径（同一选区的实际数值）==')
    print('旧口径（文字规则）：合计 %s ms，%s 包，每包词数 %s，first_six=%s extra=%s'
          % (legacy['total_duration_ms'], legacy['package_count'],
             [row['word_count'] for row in legacy['packages']], legacy['timing']['first_six'],
             legacy['timing']['extra']))
    if media and media.get('available'):
        measured = media['measured']
        print('新口径（音频实际时长）：合计 %s ms（%s 帧 @ %sfps，%s 词，%s～%s）'
              % (measured['total_duration_ms'], measured['total_frames'], measured['fps'],
                 measured['word_count'], measured['first_word'], measured['last_word']))
        print('两者相差 %s ms（旧口径 %s）'
              % (legacy['total_duration_ms'] - measured['total_duration_ms'],
                 '更长' if legacy['total_duration_ms'] >= measured['total_duration_ms'] else '更短'))
        print('口径来源：%s' % media['source'])
    else:
        print('新口径：读不到（%s）' % (media or {}).get('reason', '未指定 --timeline'))
    table = report['archive_reference']
    if table['summary']['compared']:
        print('\n== 逐词对照（旧口径累计末点 vs 归档时间线累计末点）==')
        print('%-6s %-18s %-14s %-14s %s' % ('序号', '词', '旧口径 ms', '新口径 ms', '差 ms'))
        for row in table['words'][:10]:
            print('%-6s %-18s %-14s %-14s %s' % (row['position'], row['word'],
                                                  row['legacy_end_ms'], row['media_end_ms'],
                                                  row['delta_ms']))
        summary = table['summary']
        print('（共 %d 词，词序相同 %d；末点 旧 %s ms / 新 %s ms，差 %s ms；'
              '逐词差范围 %s～%s ms）'
              % (summary['words'], summary['same_words'], summary['legacy_end_ms'],
                 summary['media_end_ms'], summary['delta_ms'], summary['min_delta_ms'],
                 summary['max_delta_ms']))
    print('\n判定：%s' % ('PASS（内容与毫秒完全等价）' if report['ok'] else 'FAIL（逐条差异见报告）'))
    if report.get('report_file'):
        print('报告：%s' % report['report_file'])


if __name__ == '__main__':
    raise SystemExit(main())
