"""H0 迁移/保护核对：只在真实边界算哈希，不做全库台账。

1) 把仓库副本与 M0 冻结清单（word_video_m0/baseline/manifest-after.json）逐文件比对：
   证明"迁入 = 复制"，没有在迁移中改字节。旧测试 4 个文件已按新布局移到 legacy_tests/。
2) 抽查一个已交付归档批次：用其自身 complete.json 记录的 sha256 复核磁盘现状，
   证明开发活动没有写穿受保护归档（M0 曾发生硬链接写穿事故）。

用法：
  python verify_protection.py --repo <repo> --m0 <word_video_m0> --batch <归档批次目录>
"""
import argparse
import hashlib
import json
from pathlib import Path

MOVED = {
    'tests/test_ai_interface.py': 'legacy_tests/test_ai_interface.py',
    'tests/test_glass_assets.py': 'legacy_tests/test_glass_assets.py',
    'tests/test_repair.py': 'legacy_tests/test_repair.py',
    'tests/test_ui_contract.py': 'legacy_tests/test_ui_contract.py',
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_migration(repo, m0, tools=None):
    manifest = json.loads((Path(m0) / 'baseline' / 'manifest-after.json').read_text(encoding='utf-8'))
    same = changed = missing = 0
    problems = []
    for item in manifest['files']:
        rel = MOVED.get(item['path'], item['path'])
        if rel.startswith('word_video_work/'):          # M0 验收器 → tools/m0（仓库外）
            target = Path(tools or (Path(repo).parent / 'tools' / 'm0')) / Path(rel).name
        else:
            target = Path(repo) / rel
        if not target.exists():
            missing += 1
            problems.append('missing: %s' % item['path'])
            continue
        data = target.read_bytes()
        if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
            changed += 1
            problems.append('differs: %s' % item['path'])
        else:
            same += 1
    return {'checked': len(manifest['files']), 'identical': same, 'changed': changed,
            'missing': missing, 'problems': problems[:20]}


def check_batch(batch):
    batch = Path(batch)
    complete = batch / 'complete.json'
    if not complete.exists():
        return {'error': 'no complete.json in %s' % batch}
    data = json.loads(complete.read_text(encoding='utf-8'))
    entries = data.get('files') or data.get('hashes') or []
    ok = bad = missing = 0
    problems = []
    for entry in entries:
        if isinstance(entry, dict):
            rel = entry.get('path') or entry.get('name')
            recorded = entry.get('sha256') or entry.get('hash')
        else:
            rel, recorded = entry, None
        if not rel:
            continue
        path = Path(rel)
        if not path.is_absolute():
            path = batch / rel
        if not path.exists():
            missing += 1
            problems.append('missing: %s' % rel)
            continue
        if recorded and sha256(path) != recorded:
            bad += 1
            problems.append('differs: %s' % rel)
        else:
            ok += 1
    return {'batch': str(batch), 'entries': len(entries), 'ok': ok, 'changed': bad,
            'missing': missing, 'problems': problems[:20]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--m0', required=True)
    parser.add_argument('--tools', default=None)
    parser.add_argument('--batch', action='append', default=[])
    parser.add_argument('--json')
    args = parser.parse_args()
    report = {'migration': check_migration(args.repo, args.m0, args.tools),
              'batches': [check_batch(b) for b in args.batch]}
    report['verdict'] = 'PASS' if (report['migration']['problems'] == []
                                   and all(b.get('problems') == [] for b in report['batches'])) else 'FAIL'
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
