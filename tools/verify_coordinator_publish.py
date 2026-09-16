"""H0 独立核对：协调器的"发布即最终路径"红线（P2 A5/A7 + 发布状态可信）。

背景：曾出现"发布成功但产物里全是 staging 路径"的缺陷（发布把文件搬进 runs/<job>，
而 complete.json/timeline.json/草稿引用仍指向 staging → 所有消费者踩空）。本脚本把它固化成检查。

判定：
  1) 缺背景提交 -> 必须 NEEDS_INPUT 且**不冻结任何收据**；
  2) 给背景后 job run -> succeeded；
  3) 发布目录里三份文档（complete/timeline/draft_content）中**每个路径都存在**、
     **不含 staging 字样**；
  4) 发布目录里不存在 staging/ 残留；
  5) 独立验收器在**发布目录**上 PASS（integrity/draft 两面必须真过）。

用法：
  python tools/verify_coordinator_publish.py --root <临时根> --fixture <request.json> --batch 151-153 \
      --background <背景.mp4> --wordlist <词表> --json <报告>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def cli(root, *args):
    proc = subprocess.run([sys.executable, '-m', 'word_video.cli', '--root', str(root), *args],
                          cwd=REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        return proc.returncode, json.loads(proc.stdout)
    except Exception:
        return proc.returncode, {'_stdout': proc.stdout[:400], '_stderr': proc.stderr[:400]}


def json_paths(blob, out):
    if isinstance(blob, dict):
        for key, value in blob.items():
            if key in ('path', 'file') and isinstance(value, str):
                out.append(value)
            else:
                json_paths(value, out)
    elif isinstance(blob, list):
        for item in blob:
            json_paths(item, out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--batch', default='151-153')
    parser.add_argument('--background', required=True)
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--json')
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO))
    from desktop.editor_import import import_request
    project = root / 'project'
    if not (project / 'project.json').exists():
        import_request(Path(args.fixture), project)

    report = {'root': str(root)}
    code, payload = cli(root, 'batch', 'plan', '--project', str(project), '--batch', args.batch)
    report['plan_without_background'] = {'exit': code, 'ready': (payload.get('result') or {}).get('ready'),
                                         'delivery_fixes': (payload.get('result') or {}).get('delivery_fixes')}
    code, payload = cli(root, 'batch', 'submit', '--project', str(project), '--batch', args.batch, '--key', 'h0-pub-nobg')
    report['submit_without_background'] = {'exit': code, 'code': (payload.get('error') or {}).get('code'),
                                           'fixes': (payload.get('error') or {}).get('fixes')}
    code, receipt = cli(root, 'receipts')
    report['receipts_after_refusal'] = (receipt.get('result') or {}).get('receipts')

    code, sub = cli(root, 'batch', 'submit', '--project', str(project), '--batch', args.batch,
                    '--key', 'h0-pub-withbg', '--codec', 'h265', '--background', args.background)
    job = (sub.get('result') or {}).get('job')
    report['submit_with_background'] = {'exit': code, 'job': job}
    if job:
        code, run = cli(root, 'job', 'run', '--job', job)
        report['run'] = {'exit': code, **(run.get('result') or {})}
        run_dir = root / 'runs' / job
        docs = {'complete.json': run_dir / 'complete.json',
                'timeline.json': run_dir / 'timeline.json',
                'editable-draft/draft_content.json': run_dir / 'editable-draft' / 'draft_content.json'}
        per_doc, missing, staging = {}, 0, 0
        for name, path in docs.items():
            paths = []
            if path.exists():
                json_paths(json.loads(path.read_text(encoding='utf-8')), paths)
            bad = [p for p in paths if not Path(p).exists()]
            stg = [p for p in paths if 'staging' in p.replace('\\', '/').lower()]
            per_doc[name] = {'paths': len(paths), 'missing': len(bad), 'staging': len(stg),
                             'missing_examples': bad[:2], 'staging_examples': stg[:2]}
            missing += len(bad)
            staging += len(stg)
        report['published_documents'] = per_doc
        report['missing_total'] = missing
        report['staging_total'] = staging
        report['staging_dir_present'] = (root / 'staging').exists()
        proc = subprocess.run([sys.executable, str(REPO / 'tests' / 'acceptance' / 'accept_range.py'),
                               str(run_dir), '--source', args.wordlist, '--range', args.batch,
                               '--cleaning', 'v2', '--pixels', 'all',
                               '--json', str(Path(args.json).with_suffix('.accept.json')) if args.json else ''],
                              cwd=REPO, capture_output=True, text=True)
        accept = Path(args.json).with_suffix('.accept.json') if args.json else None
        verdict = None
        if accept and accept.exists():
            parsed = json.loads(accept.read_text(encoding='utf-8'))
            verdict = {'verdict': parsed.get('verdict'), 'failures': sorted((parsed.get('failures') or {}).keys())}
        report['acceptance'] = {'exit': proc.returncode, **(verdict or {})}

    ok = (report.get('plan_without_background', {}).get('ready') is False
          and report.get('submit_without_background', {}).get('code') == 'NEEDS_INPUT'
          and report.get('receipts_after_refusal') == []
          and report.get('missing_total') == 0 and report.get('staging_total') == 0
          and report.get('staging_dir_present') is False
          and (report.get('acceptance') or {}).get('verdict') == 'PASS')
    report['verdict'] = 'PASS' if ok else 'FAIL'
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
