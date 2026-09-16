"""H0 核对：某个已生产批次的"中文无词性"轨文本到底用的是 v2 还是历史口径。

只读，用来证明清洗策略**真的作用到了产物**（而不是只在单元测试里）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch', required=True)
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--range', required=True)
    parser.add_argument('--json')
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    from word_video.text_policy import spoken_from_meaning, POLICY_V2
    from subtitle_factory_api import load_words

    start, end = (int(x) for x in args.range.split('-'))
    rows, _ = load_words({'path': args.wordlist})
    selected = rows[start - 1:end]
    differing = []
    for offset, row in enumerate(selected, start=start):
        v2 = spoken_from_meaning(row['d_f'], POLICY_V2)
        if v2 != row['d_c']:
            differing.append({'index': offset, 'word': row['w'], 'core_dc': row['d_c'], 'v2': v2})

    srt = sorted(Path(args.batch, 'srt').glob('*_05_*.srt'))
    texts = []
    if srt:
        for line in srt[0].read_text(encoding='utf-8').splitlines():
            if line.strip().isdigit() or '-->' in line or not line.strip():
                continue
            texts.append(line.strip())
    v2_hits = sum(1 for item in differing if item['v2'] in texts)
    core_hits = sum(1 for item in differing if item['core_dc'].strip() in texts)
    report = {
        'batch': args.batch, 'range': args.range, 'srt_05': str(srt[0]) if srt else None,
        'words_in_range': len(selected), 'differing_rows': len(differing),
        'v2_text_present_in_srt': v2_hits, 'core_text_present_in_srt': core_hits,
        'examples': differing[:6],
        'manifest_policy': None,
    }
    timeline = Path(args.batch) / 'timeline.json'
    if timeline.exists():
        report['manifest_policy'] = json.loads(timeline.read_text(encoding='utf-8')).get('spoken_policy')
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
