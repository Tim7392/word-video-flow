"""Red-to-green evidence at archive level: run every known fault through the
current validator and record what it actually said.

Origin: `D:\\1\\1-AI_workflow\\word_video_m0\\tools\\run_fault_matrix.py`
(2026-09-16), generalised by QA (task QA-1):

  * no M0 path is baked in - the good batch, the word list, the fault tree, the
    report directory and the validator to use are all arguments;
  * every case runs with the same ``--pixels`` setting as the good control, so a
    fault cannot be "caught" only because the run was cheaper;
  * the expected face of each fault is asserted, not just the verdict: a batch
    that fails for an unrelated reason is not evidence that the intended defect
    was caught;
  * the two stale-report traps write their summary through this tool, which is
    the strongest form of the M0 accident: a pre-positioned ``PASS`` summary at
    the exact output path must be overwritten by this run's real verdict.

Usage:
  python run_fault_matrix.py --good-batch GOOD --faults DIR --wordlist TXT \
      --out DIR [--range 151-200] [--pixels all|first|none] [--json OUT]
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_faults  # noqa: E402  (same directory, no package needed for a CLI)

CASES = make_faults.CASES
ARCHIVE_CASES = make_faults.ARCHIVE_CASES
STALE_REPORT_CASE = make_faults.STALE_REPORT_CASE


def run(command):
    return subprocess.run([sys.executable, *[str(part) for part in command]],
                          capture_output=True)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def run_batch_cases(config, forward):
    """One case = one batch directory judged by accept_range."""
    rows = []
    for name, directory, want, want_faces in CASES:
        batch = config['faults'] / directory / config['batch_name']
        target = config['out'] / ('range-%s.json' % name)
        if target.exists():
            target.unlink()
        command = [HERE / 'accept_range.py', batch, '--source', config['wordlist'],
                   '--range', '%d-%d' % config['range'], '--cleaning', 'legacy',
                   '--pixels', config['pixels'], '--json', target]
        started = time.time()
        result = run(command)
        report = read_json(target)
        fresh = bool(report) and target.stat().st_mtime >= started - 1
        verdict = report.get('verdict') if report else 'NO_REPORT'
        faces = sorted((report or {}).get('failures', {}))
        ok = fresh and verdict == want and all(face in faces for face in want_faces)
        rows.append({'case': name, 'kind': 'batch', 'expected': want,
                     'expected_faces': list(want_faces), 'verdict': verdict,
                     'faces': faces, 'exit_code': result.returncode, 'fresh': fresh,
                     'ok': ok, 'report': str(target),
                     'seconds': round(time.time() - started, 1),
                     'failures': (report or {}).get('failures'),
                     'stderr': result.stderr.decode('utf-8', 'replace')[-400:]})
        if forward:
            forward(rows[-1])
    return rows


def run_archive_cases(config, forward):
    """One case = one faults root judged by accept_all, summary written by us."""
    rows = []
    for name, root_name, expect, want, want_key in ARCHIVE_CASES:
        root = config['faults'] / root_name
        target = config['out'] / ('all-%s.json' % name)
        # The trap: a PASS summary already sits where this run will write.
        target.write_text(json.dumps({'batches': [], 'verdict': 'PASS',
                                      'stale': True}), encoding='utf-8')
        wordlist = config['wordlist']
        if name == STALE_REPORT_CASE:
            wordlist = config['missing_wordlist']
        command = [HERE / 'accept_all.py', root, '--source', wordlist,
                   '--expect', expect, '--out', config['out'] / 'batches',
                   '--cleaning', 'legacy', '--pixels', config['pixels'],
                   '--json', target]
        started = time.time()
        result = run(command)
        summary = read_json(target)
        fresh = bool(summary) and target.stat().st_mtime >= started - 1
        verdict = summary.get('verdict') if summary else 'NO_REPORT'
        failures = (summary or {}).get('failures', {})
        ok = (fresh and verdict == want and want_key in failures
              and not summary.get('stale'))
        rows.append({'case': name, 'kind': 'archive', 'expected': want,
                     'expected_faces': [want_key], 'verdict': verdict,
                     'faces': sorted(failures), 'exit_code': result.returncode,
                     'fresh': fresh, 'ok': ok, 'report': str(target),
                     'seconds': round(time.time() - started, 1),
                     'failures': {k: v for k, v in failures.items()
                                  if k != 'batch'},
                     'stderr': result.stderr.decode('utf-8', 'replace')[-400:]})
        if forward:
            forward(rows[-1])
    return rows


def write_summary(config, rows, validators):
    out = {'validators': validators, 'range': '%d-%d' % config['range'],
           'pixels': config['pixels'], 'good_batch': str(config['good_batch']),
           'wordlist': str(config['wordlist']), 'faults': str(config['faults']),
           'rows': rows, 'all_ok': all(row['ok'] for row in rows)}
    target = Path(config['summary'])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    return out


def print_table(rows):
    print('%-28s %-9s %-9s %-6s %-5s %s'
          % ('case', 'expected', 'verdict', 'exit', 'fresh', 'faces'))
    for row in rows:
        print('%-28s %-9s %-9s %-6s %-5s %s%s'
              % (row['case'], row['expected'], row['verdict'], row['exit_code'],
                 row['fresh'], ','.join(row['faces']),
                 '' if row['ok'] else '   <-- 判错'))
    bad = [row['case'] for row in rows if not row['ok']]
    print('all_ok =', not bad, ('未通过：%s' % ', '.join(bad)) if bad else '')
    return not bad


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--good-batch', required=True)
    parser.add_argument('--faults', required=True)
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--range', default='151-200')
    parser.add_argument('--pixels', choices=['all', 'first', 'none'], default='all')
    parser.add_argument('--json')
    args = parser.parse_args(argv)

    config = {
        'good_batch': Path(args.good_batch).resolve(strict=True),
        'faults': Path(args.faults).resolve(strict=True),
        'wordlist': Path(args.wordlist).resolve(strict=True),
        'out': Path(args.out).resolve(),
        'range': tuple(int(part) for part in args.range.split('-')),
        'pixels': args.pixels,
        # A word list that cannot be read, used to make the child validator die
        # before it writes anything - the accident the stale-report trap needs.
        'missing_wordlist': Path(args.faults).resolve() / 'no-such-wordlist.txt',
        'summary': Path(args.json).resolve() if args.json
        else Path(args.out).resolve() / 'matrix.json',
    }
    config['out'].mkdir(parents=True, exist_ok=True)
    config['batch_name'] = config['good_batch'].name

    def progress(row):
        print('[%s] %s -> %s  exit=%s  faces=%s'
              % (row['kind'], row['case'], row['verdict'], row['exit_code'],
                 ','.join(row['faces'])), flush=True)

    rows = run_batch_cases(config, progress)
    rows += run_archive_cases(config, progress)
    summary = write_summary(config, rows, {
        'accept_range': str(HERE / 'accept_range.py')})
    print_table(rows)
    print('summary:', config['summary'])
    return 0 if summary['all_ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
