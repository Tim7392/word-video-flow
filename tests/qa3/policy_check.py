"""QA-3: judge the spoken-policy change from approved rules, not from the code.

Two batches are produced from one input (``source`` + ``range``, so the job
derives the reading text itself) and judged separately:

  legacy batch   ``--cleaning legacy`` must PASS, and the batch must be identical
                 to the delivered archive - that is what proves the legacy rule
                 was not disturbed by the new policy.
  v2 batch       ``--cleaning v2 --spoken-file <table>`` must PASS, where the
                 table is QA's own derivation from the approved rule text.  The
                 acceptance tool compares the batch against that table instead of
                 against its own rule, so A's implementation is judged by an
                 outside expectation, not by itself.

The expected *differences* between the two batches are stated here too: only the
Chinese reading text (and the SRT track that carries it) may change.  Anything
else changing is a finding, and running this script twice gives the same answer.

Usage:
  python policy_check.py --v2 BATCH --legacy BATCH --archive BATCH
                         --wordlist TXT --spoken-file TABLE [--json OUT]
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = Path(__file__).resolve().parent
ACCEPTANCE = HERE.parent / 'acceptance'
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(ACCEPTANCE))


def run(args):
    result = subprocess.run([sys.executable, *[str(a) for a in args]],
                            capture_output=True)
    return result


def accepted(batch, wordlist, cleaning, spoken_file, out, pixels='none'):
    command = [ACCEPTANCE / 'accept_range.py', batch, '--source', wordlist,
               '--range', '151-200', '--cleaning', cleaning, '--pixels', pixels,
               '--json', out]
    if spoken_file:
        command += ['--spoken-file', spoken_file]
    result = run(command)
    report = json.loads(Path(out).read_text(encoding='utf-8')) if Path(out).exists() else None
    return {'exit_code': result.returncode,
            'verdict': (report or {}).get('verdict'),
            'faces': sorted((report or {}).get('failures', {})),
            'spoken_file': ((report or {}).get('expectations') or {}).get('spoken_file'),
            'problems': (report or {}).get('failures')}


def compared(mine, archive, out):
    result = run([HERE.parent / 'qa2' / 'compare_to_archive.py', '--mine', mine,
                  '--archive', archive, '--json', out])
    report = json.loads(Path(out).read_text(encoding='utf-8')) if Path(out).exists() else None
    return result.returncode, report


def diff_words(v2, legacy):
    """Which per-word fields differ between the two batches, and in which tracks."""
    a = json.loads((Path(v2) / 'timeline.json').read_text(encoding='utf-8'))
    b = json.loads((Path(legacy) / 'timeline.json').read_text(encoding='utf-8'))
    one = {w['index']: w for w in a['words']}
    two = {w['index']: w for w in b['words']}
    fields = {}
    for index in sorted(set(one) & set(two)):
        for field in ('word', 'phonetic', 'meaning', 'spoken_meaning', 'start_frame',
                      'male_frame', 'chinese_frame', 'end_frame'):
            if one[index].get(field) != two[index].get(field):
                fields.setdefault(field, []).append(index)
    settings = [key for key in ('fps', 'width', 'height', 'speed', 'total_frames',
                                'intro_frames', 'video_codec', 'first_index', 'last_index')
                if a.get(key) != b.get(key)]
    audio = {}
    stages_one = {(s['word_index'], s['role']): s for s in a['audio']}
    stages_two = {(s['word_index'], s['role']): s for s in b['audio']}
    for key in sorted(set(stages_one) & set(stages_two)):
        for field in ('text', 'voice', 'start_frame', 'duration_frames'):
            if stages_one[key].get(field) != stages_two[key].get(field):
                audio.setdefault(field, []).append(list(key))
    tracks = {}
    for path in sorted((Path(v2) / 'srt').glob('*.srt')):
        other = Path(legacy) / 'srt' / path.name
        if not other.exists():
            continue
        tracks[path.name] = 'identical' if path.read_bytes() == other.read_bytes() \
            else 'differs'
    return {'word_fields': {k: {'count': len(v), 'indexes': v} for k, v in fields.items()},
            'setting_fields': settings, 'audio_fields': {k: {'count': len(v),
                                                             'keys': v}
                                                         for k, v in audio.items()},
            'srt_tracks': tracks}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--v2', required=True)
    parser.add_argument('--legacy', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--spoken-file', dest='spoken_file', required=True)
    parser.add_argument('--out', default=None)
    parser.add_argument('--json')
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else Path(args.json or '.').parent / 'policy-out'
    out.mkdir(parents=True, exist_ok=True)

    report = {'v2_batch': str(args.v2), 'legacy_batch': str(args.legacy),
              'archive': str(args.archive), 'spoken_file': str(args.spoken_file)}
    report['accept_v2'] = accepted(args.v2, args.wordlist, 'v2', args.spoken_file,
                                   out / 'accept-v2.json')
    report['accept_legacy'] = accepted(args.legacy, args.wordlist, 'legacy', None,
                                       out / 'accept-legacy.json')
    code, legacy_cmp = compared(args.legacy, args.archive, out / 'legacy-vs-archive.json')
    report['legacy_vs_archive'] = {
        'exit_code': code,
        'timeline_equal': (legacy_cmp or {}).get('timeline', {}).get('timeline_equal'),
        'field_differences': (legacy_cmp or {}).get('timeline', {}).get(
            'field_differences'),
        'srt': (legacy_cmp or {}).get('srt', {}).get('files'),
        'complete_ok': (legacy_cmp or {}).get('complete', {}).get('ok')}
    code, v2_cmp = compared(args.v2, args.archive, out / 'v2-vs-archive.json')
    report['v2_vs_archive'] = {
        'exit_code': code,
        'timeline_equal': (v2_cmp or {}).get('timeline', {}).get('timeline_equal'),
        'differing_fields': sorted({
            line.split(' field ')[-1].split(':')[0]
            for line in (v2_cmp or {}).get('timeline', {}).get('field_differences', [])
            if ' field ' in line}),
        'srt': (v2_cmp or {}).get('srt', {}).get('files'),
        'complete_ok': (v2_cmp or {}).get('complete', {}).get('ok')}
    report['v2_vs_legacy'] = diff_words(args.v2, args.legacy)

    problems = []
    if report['accept_v2']['verdict'] != 'PASS':
        problems.append('v2 批次未通过 --cleaning v2：%s' % report['accept_v2']['problems'])
    if report['accept_v2'].get('spoken_file') != str(args.spoken_file):
        problems.append('v2 判定没有记录到外部期望表，等于没用自己的期望判')
    if report['accept_legacy']['verdict'] != 'PASS':
        problems.append('legacy 批次未通过 --cleaning legacy：%s'
                        % report['accept_legacy']['problems'])
    if not report['legacy_vs_archive']['timeline_equal']:
        problems.append('legacy 批次与归档时间线不一致：%s'
                        % report['legacy_vs_archive']['field_differences'][:3])
    if set(report['legacy_vs_archive']['srt'].values()) != {'identical'}:
        problems.append('legacy 批次与归档 SRT 不一致：%s'
                        % report['legacy_vs_archive']['srt'])
    if report['v2_vs_archive']['differing_fields'] not in (['spoken_meaning'], []):
        problems.append('v2 与归档的差异不止朗读文本：%s'
                        % report['v2_vs_archive']['differing_fields'])
    tracks = report['v2_vs_legacy']['srt_tracks']
    changed_tracks = sorted(name for name, state in tracks.items() if state == 'differs')
    report['v2_vs_legacy']['changed_tracks'] = changed_tracks
    if any('_05_' not in name for name in changed_tracks):
        problems.append('v2 与 legacy 的差异出现在非 05 轨：%s' % changed_tracks)
    for field in ('word', 'phonetic', 'meaning', 'start_frame', 'male_frame',
                  'chinese_frame', 'end_frame'):
        if field in report['v2_vs_legacy']['word_fields']:
            problems.append('v2 改动了不该动的字段 %s' % field)
    if report['v2_vs_legacy']['setting_fields']:
        problems.append('v2 改动了设置 %s' % report['v2_vs_legacy']['setting_fields'])
    audio_fields = set(report['v2_vs_legacy']['audio_fields'])
    if audio_fields - {'text'}:
        problems.append('v2 改动了音频阶段的时间/音色：%s' % sorted(audio_fields))
    report['problems'] = problems
    report['ok'] = not problems
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
