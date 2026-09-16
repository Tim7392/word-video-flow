"""Archive-level acceptance: every expected batch must exist and pass.

Origin: M0 `word_video_work/accept_all.py` (2026-09-16), moved into the new
repository by QA (task QA-1).  What changed in the move, and why:

  * the validator next to this file is located relative to ``__file__``, so the
    tool works from any worktree instead of only the M0 directory;
  * ``--json`` writes its parent directory like the per-batch tool does.

Why an empty root must not be green
-----------------------------------
The first (pre-M0) version walked whatever it found, so an empty root printed
``{"batches": [], "verdict": "PASS"}`` with exit code 0 - a missing delivery
looked like a green one.  It also ignored the child's exit status and read the
report file at a fixed path, so a crashed ``accept_range`` silently handed back
the *previous* run's verdict, and every batch wrote its report to the same name.

Now:
  * the expected ranges are stated by the caller (``--expect 1-50,51-100``);
    a range with no batch, an unexpected batch, an unreadable batch and an empty
    root are all failures, never a PASS;
  * each batch gets a fresh report path that carries the group and the batch, the
    file is deleted before the run, and the verdict is only read when the report
    is fresh (mtime newer than the start of the run);
  * the caller decides where reports go, so no run writes into the source tree.

Usage:
  python accept_all.py ROOT [ROOT...] --source WORDLIST --expect 1-50,51-100
                        [--out DIR] [--cleaning legacy|v2] [--pixels all|first|none]
                        [--json OUT]
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent


def parse_expect(text):
    ranges = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        first, last = part.split('-')
        ranges.append((int(first), int(last)))
    return ranges


def batch_dirs(root):
    """Directories that claim to be a published batch, readable or not."""
    found = []
    for group in sorted(p for p in root.iterdir() if p.is_dir()):
        for batch in sorted(p for p in group.iterdir() if p.is_dir()):
            if batch.name in ('audio-cache', 'recovery'):
                continue
            if (batch / 'timeline.json').exists() or (batch / 'complete.json').exists():
                found.append(batch)
    return found


def identify(batch):
    """(first, last) or None when the timeline cannot be read."""
    try:
        manifest = json.loads((batch / 'timeline.json').read_text(encoding='utf-8'))
    except Exception:
        return None
    if 'first_index' not in manifest or 'last_index' not in manifest:
        return None
    return int(manifest['first_index']), int(manifest['last_index'])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('roots', nargs='+')
    parser.add_argument('--source', required=True, help='只读原始词表（TXT）')
    parser.add_argument('--expect', required=True,
                        help='期望覆盖的范围，形如 1-50,51-100；缺一包即失败')
    parser.add_argument('--out', default='accept-reports', help='报告目录（每包一份）')
    parser.add_argument('--cleaning', choices=['legacy', 'v2'], default='legacy')
    parser.add_argument('--pixels', choices=['all', 'first', 'none'], default='first')
    parser.add_argument('--json')
    args = parser.parse_args(argv)

    expected = parse_expect(args.expect)
    if not expected:
        print(json.dumps({'batches': [], 'failures': {
            'expectations': ['--expect 为空：没有期望就没有验收']},
            'verdict': 'REFUSED'}, ensure_ascii=False, indent=1))
        return 2

    reports = Path(args.out)
    reports.mkdir(parents=True, exist_ok=True)
    found, failures, rows = [], {}, []
    for root_text in args.roots:
        root = Path(root_text)
        if not root.is_dir():
            failures.setdefault('roots', []).append('%s is not a directory' % root)
            continue
        found.extend(batch_dirs(root))

    if not found:
        failures['batches'] = ['no batch found under %s' % ', '.join(args.roots)]

    by_range = {}
    for batch in found:
        key = identify(batch)
        if key is None:
            failures.setdefault('unreadable', []).append(
                '%s: timeline.json is missing or unreadable' % batch)
            continue
        by_range.setdefault(key, []).append(batch)

    for first, last in expected:
        matches = by_range.get((first, last), [])
        if not matches:
            failures.setdefault('missing', []).append(
                'no batch covers %d-%d' % (first, last))
            continue
        if len(matches) > 1:
            failures.setdefault('duplicate', []).append(
                '%d-%d appears in %d batches' % (first, last, len(matches)))
            continue
        batch = matches[0]
        group = batch.parent.name
        target = reports / ('acc-%s-%s.json' % (group, batch.name))
        if target.exists():
            target.unlink()          # never let a stale report speak for this run
        started = time.time()
        command = [sys.executable, str(HERE / 'accept_range.py'), str(batch),
                   '--source', args.source, '--range', '%d-%d' % (first, last),
                   '--cleaning', args.cleaning, '--pixels', args.pixels,
                   '--json', str(target)]
        result = subprocess.run(command, capture_output=True)
        key = '%s/%s' % (group, batch.name)
        if not target.exists() or target.stat().st_mtime < started - 1:
            failures.setdefault('validator', []).append(
                '%s: accept_range produced no fresh report (exit %d) %s'
                % (key, result.returncode,
                   result.stderr.decode('utf-8', 'replace')[-200:]))
            continue
        report = json.loads(target.read_text(encoding='utf-8'))
        rows.append({'group': group, 'batch': batch.name,
                     'verdict': report.get('verdict'),
                     'duration_s': report.get('video', {}).get('duration_s'),
                     'size_mb': report.get('video', {}).get('size_mb'),
                     'frames': report.get('video', {}).get('frames'),
                     'mp4_seconds': report.get('audio_e2e', {}).get('mp4_seconds'),
                     'mix_seconds': report.get('audio_e2e', {}).get('mix_seconds'),
                     'audio_r': report.get('audio_e2e', {}).get('envelope_r'),
                     'srt_cues': report.get('srt', {}).get('cues'),
                     'draft_problems': report.get('draft', {}).get('problem_count'),
                     'exit_code': result.returncode,
                     'report': str(target)})
        if result.returncode != 0 or report.get('verdict') != 'PASS':
            failures.setdefault('batch', {})[key] = report.get('failures')

    seen = {key for key in by_range}
    extra = [key for key in seen if key not in set(expected)]
    if extra:
        failures.setdefault('unexpected', []).extend(
            '%d-%d was published but is not in --expect' % key for key in sorted(extra))

    summary = {'batches': rows, 'expected': len(expected), 'found': len(found),
               'failures': failures,
               'verdict': 'PASS' if not failures else 'FAIL'}
    text = json.dumps(summary, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
