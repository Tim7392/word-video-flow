"""H0 保护核对：只在真实边界算哈希，不做全库台账。

检查两类"不许变"的东西：

1. **受保护的旧字幕核心**：仓库根目录那 6 个旧文件（`subtitle_factory_*.py`、`Words_SRT.py`、
   `word.py`）必须与 C 盘原树**逐字节相同**——迁入是复制，W10 只做外层适配，不改核心。
   （迁移期的"引擎源码 = M0 冻结版"检查已完成使命：`word_video/**` 现在本来就该改，
   见 docs/STATUS.md 的迁移记录，不再作为门禁。）
2. **受保护的旧归档**：抽查批次用其自身 `complete.json` 记录的 sha256 复核磁盘现状，
   证明开发活动没有写穿/改写历史产物（M0 曾发生硬链接写穿事故）。

用法：
  python tools/verify_protection.py --repo <repo> --origin <C盘原树> --batch <归档批次目录> [--json out.json]
"""
import argparse
import hashlib
import json
from pathlib import Path

LEGACY = ('subtitle_factory_api.py', 'subtitle_factory_core.py', 'subtitle_factory_cli.py',
          'subtitle_factory_ui.py', 'Words_SRT.py', 'word.py')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_legacy(repo, origin):
    rows, problems = [], []
    for name in LEGACY:
        current, original = Path(repo) / name, Path(origin) / name
        if not current.exists() or not original.exists():
            problems.append('missing: %s (repo=%s origin=%s)'
                            % (name, current.exists(), original.exists()))
            continue
        same = current.read_bytes() == original.read_bytes()
        rows.append({'file': name, 'identical_to_origin': same, 'bytes': current.stat().st_size})
        if not same:
            problems.append('changed: %s' % name)
    legacy_tests = Path(repo) / 'legacy_tests'
    tests = sorted(p.name for p in legacy_tests.glob('test_*.py')) if legacy_tests.exists() else []
    return {'checked': len(rows),
            'identical': sum(1 for row in rows if row['identical_to_origin']),
            'rows': rows, 'legacy_tests_present': tests, 'problems': problems}


def check_batch(batch):
    batch = Path(batch)
    complete = batch / 'complete.json'
    if not complete.exists():
        return {'error': 'no complete.json in %s' % batch, 'problems': ['missing complete.json']}
    entries = json.loads(complete.read_text(encoding='utf-8')).get('files') or []
    ok = bad = missing = 0
    problems = []
    for entry in entries:
        rel, recorded = ((entry.get('path'), entry.get('sha256'))
                         if isinstance(entry, dict) else (entry, None))
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
    parser.add_argument('--origin', required=True, help='受保护旧核心的原树目录')
    parser.add_argument('--batch', action='append', default=[])
    parser.add_argument('--json')
    args = parser.parse_args()
    report = {'legacy_core': check_legacy(args.repo, args.origin),
              'batches': [check_batch(b) for b in args.batch]}
    report['verdict'] = 'PASS' if (not report['legacy_core']['problems']
                                   and all(b.get('problems') == [] for b in report['batches'])) else 'FAIL'
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
