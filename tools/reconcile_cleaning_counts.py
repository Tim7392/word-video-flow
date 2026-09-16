"""H0 口径核对：旧核心 d_c / 验收器 legacy / 生产 v2 三个朗读文本口径到底差多少行。

背景：M0 预检记录"1654 条中 171 条变化"，A-2 实现后实测 291，两边数字对不上。
真正影响成本的是 **v2 与旧核心 d_c 的差异**——因为历史缓存音频是用 d_c 合成的：
只有"已交付范围"要重做时才需要为这些差异行重新合成；全新范围本来就没有缓存，v2 不额外花钱。

只读；不写 M0 证据目录。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--json')
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(REPO / 'tests' / 'acceptance'))
    from word_video.text_policy import spoken_from_meaning, POLICY_V2
    from accept_range import spoken_of  # 验收器（独立实现）
    from subtitle_factory_api import load_words  # 旧核心（受保护，只读调用）

    rows, _ = load_words({'path': args.wordlist})
    counts = {'rows': len(rows), 'v2_ne_validator_legacy': 0, 'v2_ne_core_dc': 0,
              'validator_legacy_ne_core_dc': 0}
    examples = {'v2_ne_core_dc': [], 'validator_legacy_ne_core_dc': []}
    leftover = []
    for position, row in enumerate(rows, start=1):
        meaning, core_dc = row['d_f'], row['d_c']
        v2 = spoken_from_meaning(meaning, POLICY_V2)
        legacy = spoken_of(meaning, 'legacy')
        if v2 != legacy:
            counts['v2_ne_validator_legacy'] += 1
        if v2 != core_dc:
            counts['v2_ne_core_dc'] += 1
            if len(examples['v2_ne_core_dc']) < 8:
                examples['v2_ne_core_dc'].append({'index': position, 'word': row['w'],
                                                  'core_dc': core_dc, 'v2': v2})
        if legacy != core_dc:
            counts['validator_legacy_ne_core_dc'] += 1
            if len(examples['validator_legacy_ne_core_dc']) < 8:
                examples['validator_legacy_ne_core_dc'].append({'index': position, 'word': row['w'],
                                                                'core_dc': core_dc, 'legacy': legacy})
        if '&' in v2 or '/' in v2:
            leftover.append({'index': position, 'word': row['w'], 'v2': v2})
    report = {'wordlist': args.wordlist, 'counts': counts,
              'leftover_amp_or_slash_rows': len(leftover), 'leftover_examples': leftover[:6],
              'examples': examples,
              'note': 'v2 与旧核心 d_c 的差异行 = 若重做已交付范围需重新合成的条数；'
                      '全新范围无历史缓存，v2 不额外花钱'}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
