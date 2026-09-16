"""H0 工具：比较两个批次的 timeline 与五轨 SRT（迁移/新旧等价性）。

用途：
  * 迁入等价性 —— 同一请求在旧树与新树各跑一次，产物应一致；
  * W10 新旧字幕等价 —— 旧字幕工厂与新入口在同一词表/选区/计时参数下应毫秒等价。

只比较"业务字段"，忽略路径、job id、输出目录等环境差异；差异逐项列出，不做模糊判定。
"""
import argparse
import json
from pathlib import Path

WORD_FIELDS = ('index', 'word', 'phonetic', 'meaning', 'spoken_meaning',
               'start_frame', 'male_frame', 'chinese_frame', 'end_frame')
AUDIO_FIELDS = ('word_index', 'role', 'text', 'start_frame', 'duration_frames')
SCALAR_FIELDS = ('schema_version', 'fps', 'width', 'height', 'speed', 'intro_frames',
                 'total_frames', 'video_codec', 'title', 'footer')


def load(batch):
    batch = Path(batch)
    timeline = json.loads((batch / 'timeline.json').read_text(encoding='utf-8'))
    return batch, timeline


def compare_timeline(left, right, limit=20):
    problems = []
    for field in SCALAR_FIELDS:
        if left.get(field) != right.get(field):
            problems.append('scalar %s: %r != %r' % (field, left.get(field), right.get(field)))
    lw, rw = left.get('words', []), right.get('words', [])
    if len(lw) != len(rw):
        problems.append('word count: %d != %d' % (len(lw), len(rw)))
    for a, b in zip(lw, rw):
        for field in WORD_FIELDS:
            if a.get(field) != b.get(field):
                problems.append('word %s.%s: %r != %r' % (a.get('index'), field, a.get(field), b.get(field)))
    la, ra = left.get('audio', []), right.get('audio', [])
    if len(la) != len(ra):
        problems.append('audio count: %d != %d' % (len(la), len(ra)))
    for a, b in zip(la, ra):
        for field in AUDIO_FIELDS:
            if a.get(field) != b.get(field):
                problems.append('audio %s/%s.%s: %r != %r'
                                % (a.get('word_index'), a.get('role'), field, a.get(field), b.get(field)))
    return problems


def compare_srt(left, right):
    rows = []
    left_dir, right_dir = Path(left) / 'srt', Path(right) / 'srt'
    names = sorted({p.name for p in left_dir.glob('*.srt')} | {p.name for p in right_dir.glob('*.srt')})
    for name in names:
        a, b = left_dir / name, right_dir / name
        if not a.exists() or not b.exists():
            rows.append({'track': name, 'status': 'MISSING'})
            continue
        same = a.read_bytes() == b.read_bytes()
        rows.append({'track': name, 'status': 'IDENTICAL' if same else 'DIFFERS',
                     'left_bytes': a.stat().st_size, 'right_bytes': b.stat().st_size})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--left', required=True, help='参照批次（例如归档）')
    parser.add_argument('--right', required=True, help='待比较批次（例如新跑）')
    parser.add_argument('--json')
    args = parser.parse_args()

    left_batch, left = load(args.left)
    right_batch, right = load(args.right)
    problems = compare_timeline(left, right)
    srt = compare_srt(left_batch, right_batch)
    srt_bad = [row for row in srt if row['status'] != 'IDENTICAL']
    report = {
        'left': str(left_batch), 'right': str(right_batch),
        'timeline_differences': len(problems), 'timeline_problems': problems[:20],
        'srt': srt, 'srt_not_identical': len(srt_bad),
    }
    report['verdict'] = 'PASS' if not problems and not srt_bad else 'FAIL'
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
