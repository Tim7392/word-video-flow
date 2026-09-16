"""W10's hard indicator as one acceptance step: the archive equivalence long run.

``measure_equivalence`` proves the claim for **one** selection; this module is the gate
around it, because one selection can pass by luck and a report in a temporary
directory is not evidence:

* **two ranges, not one** - 301-350 and 501-550 from the read-only archive, each with
  its own slice of the word list, its own delivered ``timeline.json`` and its own
  comparison run.  They behave differently on purpose (on 301-350 the old text rule is
  longer than the audio on *every* word; on 501-550 the first words are shorter), so a
  single lucky range cannot carry the verdict;
* **逐轨逐毫秒对照表** - for every one of the five tracks, every cue of the old
  entry's file beside the same cue of the new entry's file, with both millisecond
  ranges and the delta.  ``mismatched`` counts the cues that are not equal to the
  millisecond; at 0 the table is still written out, because "0 differences" over 300
  cues only means something next to the cues themselves;
* **a negative control** - the *same* comparator is pointed at the archive's own
  delivered track and the old entry's track for the same 50 words.  They must differ
  in time and agree in text, which is what makes "identical" above a measurement
  rather than a comparator that cannot see anything;
* **stable evidence** - ``out/reports/gate-legacy-equivalence.json``, one full report
  per range and a human-readable summary, all under fixed names.  pytest's temporary
  directory is pruned when the session ends, so nothing here is written into one;
* **protection** - the six protected old-core files and every archive file this run
  reads are snapshotted before and after, git is asked whether the branch touched
  them, and H0's own tool is run as a subprocess.  A run that changed the archive
  fails the gate even if all ten tracks matched.

    python -m legacy_adapter.gate                 # both ranges, evidence under D:
    python -m legacy_adapter.gate --ranges 301-350

Exit code 0 means every track is byte-identical, every cue equal to the millisecond
and nothing protected changed; 1 means at least one range or a protection check
failed; the code of the failure (2/3/4) means the data this gate needs - word list,
archive batch, interpreter, old entry - is not there, which is a failure, never a skip.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import protection
from .environment import default_python, locate_legacy_entry, run_root, work_root
from .errors import INPUT_ERROR, INVALID_REQUEST, LEGACY_ENTRY_MISSING, LegacyAdapterError
from .measure_equivalence import main as equivalence_main
from .report import compare_runs, parse_track, read_run_tracks, read_text, sha256
from .runner import write_json

SCHEMA = 'wv-legacy-gate@1'

#: The two archived ranges this gate judges.  ``archive`` is the delivered batch the
#: media caliber is read from and the negative control is taken against.
DEFAULT_RANGES = (
    {'label': '0301-0350', 'start': 301, 'end': 350, 'batch_size': 50,
     'archive': (r'D:\单词速记自动化_测试归档_0915\50词本地配音'
                 r'\wv-423592ff3e80c4748f0fd573\0301-0350')},
    {'label': '0501-0550', 'start': 501, 'end': 550, 'batch_size': 50,
     'archive': (r'D:\单词速记自动化_测试归档_0915\范围501-700-1080p素材'
                 r'\wv-2bdf4a5ba1e77f1d99a2d369\0501-0550')},
)

#: The old core reads the whole list; the selection is what varies per range.
DEFAULT_WORDLIST = Path(os.environ.get('WORD_VIDEO_ACCEPTANCE_WORDLIST')
                        or (work_root() / 'data' / 'wordlists' / '四级核心1500词_已清理.txt'))

#: The track the negative control uses: English single occurrence, whose text is the
#: word itself, so "same text, different time" is readable at a glance.
CONTROL_TRACK = 2


def evidence_dir(preferred=''):
    """Where the gate writes: the D work root's ``out/reports`` unless told otherwise."""
    return Path(preferred) if str(preferred or '').strip() else work_root() / 'out' / 'reports'


def parse_ranges(tokens=()):
    """``['301-350']`` -> the known specs; an unknown range is refused with the list.

    Matching is by numbers, not by label text, so ``301-350`` and ``0301-0350`` name
    the same range; a range whose delivered batch is not known is refused instead of
    being quietly mapped onto the wrong archive.
    """
    known = [dict(spec) for spec in DEFAULT_RANGES]
    if not tokens:
        return known
    chosen = []
    for token in tokens:
        text = str(token).strip()
        try:
            start, _, end = text.replace('～', '-').partition('-')
            wanted = (int(start), int(end))
        except ValueError:
            wanted = None
        match = next((spec for spec in known if (spec['start'], spec['end']) == wanted), None)
        if match is None:
            raise LegacyAdapterError(
                INVALID_REQUEST, '不认识的区间：%s' % text,
                details={'range': text,
                         'known': ['%d-%d' % (spec['start'], spec['end']) for spec in known],
                         'hint': '区间与归档批次是绑定的：要加新区间，先在 DEFAULT_RANGES 里写明批次'})
        if match not in chosen:
            chosen.append(match)
    return chosen


def archive_track_path(archive, track):
    """The delivered track file for one index, or ``None`` when the batch lacks it."""
    matches = sorted(Path(archive).joinpath('srt').glob('*_%02d_*.srt' % int(track)))
    return matches[0] if matches else None


def cue_table(left, right, *, limit=2000):
    """Every cue of both runs, side by side, in milliseconds - the comparison table.

    ``compare_runs`` answers "are they equal" and lists the differences; this answers
    "show me", which is what a delivery review needs.  Truncation is recorded
    (``truncated``) rather than silently shortening the table.
    """
    first, second = read_run_tracks(left), read_run_tracks(right)
    tracks, total, mismatched, worst, truncated = [], 0, 0, 0, False
    for index in sorted(first):
        mine = first[index]
        theirs = second.get(index)
        rows, track_mismatch = [], 0
        for position, cue in enumerate(mine['parsed']):
            other = (theirs['parsed'][position]
                     if theirs is not None and position < theirs['cues'] else None)
            start_delta = None if other is None else cue['start_ms'] - other['start_ms']
            end_delta = None if other is None else cue['end_ms'] - other['end_ms']
            same = cue == other
            if not same:
                track_mismatch += 1
            for delta in (start_delta, end_delta):
                if delta is not None:
                    worst = max(worst, abs(delta))
            rows.append({'cue': position + 1,
                         'word': cue['text'].splitlines()[0] if cue['text'] else '',
                         'left_start_ms': cue['start_ms'], 'left_end_ms': cue['end_ms'],
                         'right_start_ms': None if other is None else other['start_ms'],
                         'right_end_ms': None if other is None else other['end_ms'],
                         'delta_start_ms': start_delta, 'delta_end_ms': end_delta,
                         'same': same})
        truncated = truncated or len(rows) > limit
        total += len(rows)
        mismatched += track_mismatch
        tracks.append({'track': index, 'left': mine['path'],
                       'right': None if theirs is None else theirs['path'],
                       'cues_left': mine['cues'],
                       'cues_right': None if theirs is None else theirs['cues'],
                       'sha256_left': mine['sha256'],
                       'sha256_right': None if theirs is None else theirs['sha256'],
                       'bytes_identical': theirs is not None and mine['sha256'] == theirs['sha256'],
                       'mismatched_cues': track_mismatch, 'rows': rows[:limit]})
    return {'tracks': tracks, 'cues': total, 'mismatched': mismatched, 'truncated': truncated,
            'max_abs_delta_ms': worst, 'missing_right': sorted(set(first) - set(second)),
            'missing_left': sorted(set(second) - set(first))}


def negative_control(new_report_path, archive, track=CONTROL_TRACK):
    """The same comparator, pointed at two files that *must* differ.

    Two runs of the same entry agreeing is only evidence if the comparison can fail,
    so the archive's own delivered track for the same selection is compared with the
    old entry's track: the words are the same words, the times are two different
    clocks.  A pair that came out identical (a batch actually produced by the text
    rule) would make this control vacuous, and that is reported as a failed check
    rather than as a pass.
    """
    journal = json.loads(Path(new_report_path).read_text(encoding='utf-8'))
    legacy_track = next((row['path'] for row in journal.get('tracks') or ()
                         if int(row.get('track') or 0) == int(track)), None)
    archive_file = archive_track_path(archive, track)
    control = {'track': int(track), 'legacy_file': legacy_track,
               'archive_file': None if archive_file is None else str(archive_file),
               'available': bool(legacy_track and archive_file), 'reason': ''}
    if not control['available']:
        control['reason'] = '归档或旧入口缺少第 %d 轨，反面对照不可用' % track
        return control
    pair = compare_runs(
        {'tracks': [{'track': track, 'relative_path': 'legacy', 'path': legacy_track,
                     'sha256': ''}]},
        {'tracks': [{'track': track, 'relative_path': 'archive',
                     'path': str(archive_file), 'sha256': ''}]}, limit=10000)
    row = pair['tracks'][0]
    differing = [item for item in row['cue_differences']
                 if item.get('left') and item.get('right')
                 and (item['left']['start_ms'] != item['right']['start_ms']
                      or item['left']['end_ms'] != item['right']['end_ms'])]
    control.update({'texts_identical': row['texts_identical'],
                    'times_identical': row['times_identical'],
                    'bytes_identical': row['bytes_identical'],
                    'differing_cues': len(differing),
                    'first_difference': differing[0] if differing else None,
                    'left_cues': row['left_cues'], 'right_cues': row['right_cues']})
    return control


def run_range(spec, *, wordlist, area, evidence, python=''):
    """One range: old entry vs new entry, plus the table, the control and both calibers."""
    root = Path(area) / spec['label']
    report_file = Path(evidence) / ('gate-legacy-%s.json' % spec['label'])
    argv = ['--wordlist', str(wordlist), '--start', str(spec['start']), '--end', str(spec['end']),
            '--batch-size', str(spec['batch_size']), '--timeline', str(spec['archive']),
            '--root', str(root), '--report', str(report_file), '--label', spec['label']]
    if python:
        argv = ['--legacy-python', str(python), *argv]
    captured = io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(captured):
        code = equivalence_main(argv)
    seconds = round(time.perf_counter() - started, 3)
    report = json.loads(report_file.read_text(encoding='utf-8'))
    comparison = report['comparison']
    # Both sides are read back from the files the comparison itself used: the old
    # entry's own stdout (kept by ``measure_equivalence``) and the adapter's report.
    new_report_file = root / 'new-entry-report.json'
    table = cue_table(report['old_entry']['stdout_file'], new_report_file)
    control = negative_control(new_report_file, spec['archive'])
    legacy = report['calibers']['legacy']           # the old core's own measured numbers
    media = report['calibers']['media']             # the archive's media caliber block
    measured = media.get('measured') or {}
    delivered = None
    archive_file = archive_track_path(spec['archive'], CONTROL_TRACK)
    if archive_file is not None:
        cues = parse_track(read_text(archive_file))
        delivered = cues[-1]['end_ms'] if cues else None

    problems = []
    if code != 0 or not report.get('ok'):
        problems.append('等价性判定不是 identical：verdict=%s，差异 %d 处'
                        % (comparison['verdict'], comparison['difference_count']))
    if comparison['tracks_missing']:
        problems.append('缺少轨道：%s' % comparison['tracks_missing'])
    if len(comparison['tracks']) != 5:
        problems.append('轨道数不是 5：%d' % len(comparison['tracks']))
    if table['mismatched']:
        problems.append('逐轨逐毫秒对照表里有 %d 处毫秒差异' % table['mismatched'])
    if not control.get('available'):
        problems.append('反面对照不可用：%s' % control.get('reason'))
    elif control.get('texts_identical') is not True:
        problems.append('反面对照的文本不同：同一批词的归档轨道与旧入口轨道本应同文')
    elif not control.get('differing_cues'):
        problems.append('反面对照没有任何一处时间差：本批归档的时间恰好等于旧文字规则，'
                        '这条对照不再能证明比较器看得见差异')
    if delivered is not None and measured.get('total_duration_ms') != delivered:
        problems.append('新口径合计 %s ms 与归档自身第 %d 轨末点 %s ms 不一致'
                        % (measured.get('total_duration_ms'), CONTROL_TRACK, delivered))
    return {
        'label': spec['label'], 'start': spec['start'], 'end': spec['end'],
        'batch_size': spec['batch_size'], 'archive': str(spec['archive']),
        'wordlist': report['inputs']['wordlist'], 'wordlist_sha256': report['inputs']['wordlist_sha256'],
        'ok': not problems, 'problems': problems, 'exit_code': code, 'seconds': seconds,
        'evidence': str(report_file), 'root': str(root),
        'tracks': comparison['tracks'], 'tracks_missing': comparison['tracks_missing'],
        'verdict': comparison['verdict'], 'difference_count': comparison['difference_count'],
        'cue_table': table, 'control': control,
        'calibers': {'legacy_total_ms': legacy['total_duration_ms'],
                     'media_total_ms': measured.get('total_duration_ms'),
                     'media_frames': measured.get('total_frames'), 'media_fps': measured.get('fps'),
                     'media_words': measured.get('word_count'),
                     'media_first_word': measured.get('first_word'),
                     'media_last_word': measured.get('last_word'),
                     'delta_ms': (None if measured.get('total_duration_ms') is None
                                  else legacy['total_duration_ms'] - measured['total_duration_ms']),
                     'delivered_track_end_ms': delivered,
                     'legacy_packages': legacy['packages']},
        'old_entry': report['old_entry'], 'new_entry': report['new_entry'],
        'per_word_caliber_table': report['archive_reference']['summary'],
        'child_stdout': captured.getvalue()[-4000:],
    }


def run_gate(*, wordlist=None, ranges=(), area='', evidence='', summary='', report='',
             python='', label='equivalence'):
    """The whole acceptance step, as one document that names every path it used."""
    specs = parse_ranges(ranges)
    wordlist = Path(wordlist or DEFAULT_WORDLIST)
    if not wordlist.is_file():
        raise LegacyAdapterError(INPUT_ERROR, '只读词表不存在：%s' % wordlist,
                                 details={'path': str(wordlist), 'field': 'wordlist'})
    entry = locate_legacy_entry()                       # fails now, not after ten runs
    interpreter = str(python or '').strip() or default_python()
    if not interpreter:
        raise LegacyAdapterError(LEGACY_ENTRY_MISSING, '找不到能运行旧 CLI 的解释器。')
    directory = evidence_dir(evidence)
    directory.mkdir(parents=True, exist_ok=True)
    area = Path(area) if str(area or '').strip() else \
        run_root() / ('gate-%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), uuid.uuid4().hex[:6]))
    area.mkdir(parents=True, exist_ok=True)
    report_path = Path(report) if str(report or '').strip() else \
        directory / 'gate-legacy-equivalence.json'
    summary_path = Path(summary) if str(summary or '').strip() else \
        directory / 'gate-legacy-summary.txt'
    started_utc = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    started = time.perf_counter()

    # -- before: what must still be true afterwards -----------------------
    repo = Path(__file__).resolve().parent.parent
    batches = [str(spec['archive']) for spec in specs]
    protected_before = protection.snapshot(protection.protected_files(repo))
    watched = [path for batch in batches for path in protection.archive_watch_files(batch)]
    archive_before = protection.snapshot(watched)

    results = []
    for spec in specs:
        if not Path(spec['archive']).is_dir():
            raise LegacyAdapterError(INPUT_ERROR, '归档批次不存在：%s' % spec['archive'],
                                     details={'archive': str(spec['archive']),
                                              'label': spec['label']})
        results.append(run_range(spec, wordlist=wordlist, area=area, evidence=directory,
                                 python=str(interpreter)))

    # -- after: the same files, plus git and H0's own tool ----------------
    protected_after = protection.snapshot(protection.protected_files(repo))
    archive_after = protection.snapshot(watched)
    h0 = protection.run_h0_tool(repo, protection.DEFAULT_ORIGIN, batches,
                                directory / 'gate-legacy-protection.json',
                                python=str(interpreter))
    protection_report = protection.check(protected_before, protected_after, archive_before,
                                         archive_after, protection.git_checks(repo), h0, batches)

    problems = ['%s：%s' % (row['label'], problem) for row in results for problem in row['problems']]
    problems.extend(protection_report['problems'])
    document = {
        'schema': SCHEMA, 'label': label, 'ok': not problems,
        'verdict': 'PASS' if not problems else 'FAIL', 'problems': problems,
        'started_utc': started_utc, 'seconds': round(time.perf_counter() - started, 3),
        'work_root': str(work_root()), 'area': str(area), 'repo': str(repo),
        'git_head': protection_report['git']['head'],
        'wordlist': str(wordlist), 'wordlist_sha256': sha256(wordlist),
        'legacy_entry': str(entry), 'legacy_python': str(interpreter),
        'ranges': results,
        'totals': {'ranges': len(results),
                   'tracks_identical': sum(1 for row in results for track in row['tracks']
                                           if track['verdict'] == 'identical'),
                   'tracks': sum(len(row['tracks']) for row in results),
                   'cues_compared': sum(row['cue_table']['cues'] for row in results),
                   'mismatched_cues': sum(row['cue_table']['mismatched'] for row in results),
                   'max_abs_delta_ms': max([row['cue_table']['max_abs_delta_ms']
                                            for row in results] or [0])},
        'protection': protection_report,
        'evidence': {'report': str(report_path), 'summary': str(summary_path),
                     'per_range': [row['evidence'] for row in results],
                     'h0_protection': h0.get('report_file', ''), 'area': str(area)},
        'finished_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    write_json(report_path, document)
    text = format_summary(document)
    summary_path.write_text(text, encoding='utf-8')
    print(text)
    return document


def format_summary(report):
    """The human-readable face of the gate: one line per range, then protection."""
    lines = ['== W10 归档等价性门禁（旧入口 vs 新入口：五轨逐字节 + 逐轨逐毫秒）==',
             '口径：同一词表、同一选区、同一分包与计时参数；旧入口直接调用旧 CLI，'
             '新入口走 legacy_adapter，两边各自成进程。',
             '%-10s %-8s %-9s %-9s %-8s %-12s %-12s %s'
             % ('区间', '轨一致', '对照 cue', '毫秒差异', '最大差', '旧口径 ms', '新口径 ms',
                '反面对照')]
    for row in report['ranges']:
        calibers, control = row['calibers'], row['control']
        lines.append('%-10s %-8s %-9s %-9s %-8s %-12s %-12s %s'
                     % (row['label'],
                        '%d/%d' % (sum(1 for track in row['tracks']
                                       if track['verdict'] == 'identical'), len(row['tracks'])),
                        row['cue_table']['cues'], row['cue_table']['mismatched'],
                        row['cue_table']['max_abs_delta_ms'], calibers['legacy_total_ms'],
                        calibers['media_total_ms'],
                        ('同文不同时（%d 处差）' % control.get('differing_cues', 0))
                        if control.get('available') else '不可用'))
    totals = report['totals']
    lines.append('合计：%d 个区间 / %d 轨逐字节相同 / %d 条 cue 逐毫秒对照 / %d 处差异'
                 % (totals['ranges'], totals['tracks_identical'], totals['cues_compared'],
                    totals['mismatched_cues']))
    for row in report['ranges']:
        calibers = row['calibers']
        lines.append('  %s 两种口径：旧（文字规则）%s ms vs 归档音频（实际时长）%s ms'
                     '（%s 帧 @ %s fps，%s 词 %s～%s），相差 %s ms；归档第 %d 轨末点 %s ms'
                     % (row['label'], calibers['legacy_total_ms'], calibers['media_total_ms'],
                        calibers['media_frames'], calibers['media_fps'], calibers['media_words'],
                        calibers['media_first_word'], calibers['media_last_word'],
                        calibers['delta_ms'], CONTROL_TRACK, calibers['delivered_track_end_ms']))
    protection_report = report['protection']
    lines.append('== 保护核对 ==')
    lines.append('受保护旧核心 6 文件：%d/%d 未变（sha256 与 mtime 都未变）'
                 % (protection_report['files']['unchanged'], protection_report['files']['checked']))
    lines.append('git：head=%s，worktree/staged/status/自 %s 起的提交 = %s'
                 % (str(report['git_head'])[:12], protection_report['git']['merge_base']['base_ref'],
                    protection_report['git']['changed'] or '无改动'))
    for ledger in protection_report['batches']:
        lines.append('旧归档 %s：complete.json %d 项，ok %d / changed %d / missing %d'
                     % (Path(ledger['batch']).name, ledger['entries'], ledger['ok'],
                        ledger['changed'], ledger['missing']))
    lines.append('本次运行读到的归档文件 %d 个：%s'
                 % (protection_report['archive']['checked'],
                    '全部未变' if protection_report['archive']['ok']
                    else '有改动 %s' % protection_report['archive']['changed']))
    h0 = protection_report['h0_tool']
    lines.append('H0 verify_protection.py：%s（%s）'
                 % (h0.get('verdict'), h0.get('reason') or h0.get('report_file')))
    lines.append('判定：%s' % report['verdict'])
    for problem in report['problems']:
        lines.append('  - %s' % problem)
    lines.append('证据：%s' % report['evidence']['report'])
    for path in report['evidence']['per_range']:
        lines.append('      %s' % path)
    lines.append('      %s' % report['evidence']['summary'])
    if report['evidence'].get('h0_protection'):
        lines.append('      %s' % report['evidence']['h0_protection'])
    return '\n'.join(lines) + '\n'


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m legacy_adapter.gate',
        description='W10 归档等价性长跑门禁：真实区间的五轨逐字节 + 逐轨逐毫秒对照，'
                    '证据写 out/reports，同时完成旧核心与旧归档的保护核对。')
    parser.add_argument('--wordlist', default=str(DEFAULT_WORDLIST),
                        help='只读全量词表（默认 D 盘工作根 data/wordlists）')
    parser.add_argument('--ranges', nargs='*', default=[],
                        help='要跑的区间，如 301-350 501-550；默认全部（%s）'
                             % ' '.join('%d-%d' % (spec['start'], spec['end'])
                                        for spec in DEFAULT_RANGES))
    parser.add_argument('--area', default='', help='本次运行的工作目录（默认 out/legacy/gate-<时间>）')
    parser.add_argument('--evidence', default='', help='证据目录（默认 D 盘工作根 out/reports）')
    parser.add_argument('--report', default='', help='汇总报告路径（默认 gate-legacy-equivalence.json）')
    parser.add_argument('--summary', default='', help='人类可读汇总路径（默认 gate-legacy-summary.txt）')
    parser.add_argument('--legacy-python', default='', help='运行旧 CLI 的解释器')
    parser.add_argument('--label', default='equivalence', help='这份证据的标签')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        document = run_gate(wordlist=args.wordlist, ranges=args.ranges, area=args.area,
                            evidence=args.evidence, summary=args.summary, report=args.report,
                            python=args.legacy_python, label=args.label)
    except LegacyAdapterError as error:
        # A refused invocation must not overwrite the last good evidence: the failure
        # goes to its own file unless the caller named the exact path to write.
        fallback = Path(args.report) if str(args.report or '').strip() else \
            evidence_dir(args.evidence) / 'gate-legacy-error.json'
        write_json(fallback, {'schema': SCHEMA, 'ok': False, 'verdict': 'FAIL',
                              'label': args.label, 'error': error.to_dict(),
                              'argv': list(argv if argv is not None else sys.argv[1:])})
        print('%s\n（本次没有跑：证据没被覆盖，失败写在 %s）' % (error, fallback))
        return error.exit_code
    return 0 if document['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
