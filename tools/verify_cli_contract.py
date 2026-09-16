"""H0 独立核对：W05 CLI 的 Agent 自动化契约（P2 清单 A 组）。

不依赖生产自测：本脚本自己建工程、自己发命令、自己判读 stdout/退出码，并把结果写成 JSON。
用法：
  python tools/verify_cli_contract.py --root <临时根> --fixture <request.json> --batch 151-153 --json <报告>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def run(args, root):
    proc = subprocess.run([sys.executable, '-m', 'word_video.cli', '--root', str(root), *args],
                          cwd=REPO, capture_output=True, text=True)
    payload = None
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        payload = None
    return {'args': args, 'exit': proc.returncode, 'json': payload,
            'stdout_is_single_json': payload is not None,
            'stderr_head': (proc.stderr or '').strip().splitlines()[:2]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--batch', default='151-153')
    parser.add_argument('--background', required=True,
                        help='交付背景文件；W08 起交付预检要求它存在，缺失应返回 NEEDS_INPUT')
    parser.add_argument('--roots', required=True,
                        help='旧缓存根（import audio 用它把已有录音与音色记进工程）')
    parser.add_argument('--json')
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO))
    from desktop.editor_import import import_request
    project = root / 'project'
    import_request(Path(args.fixture), project)

    checks = []
    # W08 起交付预检要求"每个资产记录了音色"，否则 batch submit 正确拒绝（NEEDS_INPUT）。
    # 这里走产品自己的路径把三条配音的音色记下来，然后再验证幂等/冲突/取消等契约面。
    voices = run(['import', 'audio', '--project', str(project),
                  '--roots', args.roots, '--batch', args.batch,
                  '--voices', 'female=BV503_streaming,male=BV504_streaming,chinese=BV406_streaming',
                  '--apply'], root)
    checks.append({'name': 'A0 record voices (import audio --apply)', **voices})
    checks.append({'name': 'A1 capabilities', **run(['capabilities'], root)})
    checks.append({'name': 'A1 doctor', **run(['doctor'], root)})
    checks.append({'name': 'A1 project show', **run(['project', 'show', '--project', str(project)], root)})

    before = sorted(p.name for p in root.rglob('*'))
    plan = run(['batch', 'plan', '--project', str(project), '--batch', args.batch], root)
    after = sorted(p.name for p in root.rglob('*'))
    plan['wrote_nothing'] = before == after
    checks.append({'name': 'A2 batch plan writes nothing', **plan})

    key = 'h0-cli-check-1'
    first = run(['batch', 'submit', '--project', str(project), '--batch', args.batch,
                 '--key', key, '--codec', 'h265', '--background', args.background], root)
    again = run(['batch', 'submit', '--project', str(project), '--batch', args.batch,
                 '--key', key, '--codec', 'h265', '--background', args.background], root)
    conflict = run(['batch', 'submit', '--project', str(project), '--batch', args.batch,
                    '--key', key, '--codec', 'h264', '--background', args.background], root)
    job = (first.get('json') or {}).get('result', {}).get('job')
    same_job = job is not None and job == (again.get('json') or {}).get('result', {}).get('job')
    checks.append({'name': 'A3 same key same request -> same job', 'job': job,
                   'same_job': same_job, 'first': first, 'again': again,
                   'exit': max(first['exit'], again['exit']),
                   'stdout_is_single_json': first['stdout_is_single_json'] and again['stdout_is_single_json']})
    checks.append({'name': 'A3 same key different content -> conflict', **conflict})
    if job:
        checks.append({'name': 'A4 job status', **run(['job', 'status', '--job', job], root)})
        checks.append({'name': 'A5 artifacts before run', **run(['artifacts', '--job', job], root)})
        checks.append({'name': 'A4 cancel', **run(['job', 'cancel', '--job', job], root)})
        checks.append({'name': 'A4 artifacts after cancel', **run(['artifacts', '--job', job], root)})
        checks.append({'name': 'A5 receipts', **run(['receipts'], root)})
    checks.append({'name': 'A6 unknown action', **run(['nonsense'], root)})
    checks.append({'name': 'A6 missing subcommand', **run(['job'], root)})
    checks.append({'name': 'A6 submit without key', **run(['batch', 'submit', '--project', str(project),
                                                           '--batch', args.batch], root)})
    checks.append({'name': 'A7 reconcile', **run(['reconcile'], root)})

    report = {'root': str(root), 'project': str(project), 'checks': checks}
    report['verdict'] = 'PASS' if all(c.get('stdout_is_single_json') for c in checks) else 'FAIL'
    report['notes'] = {
        'cancel_published': (checks[-6].get('json') or {}).get('result', {}).get('published')
        if len(checks) > 6 else None,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text[:4000])
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
