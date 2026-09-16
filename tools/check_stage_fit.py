"""H0 诊断：按时间线逐条核对"语音文件实际时长 vs 分配到的舞台帧数"。

用于定位 `draft.py` 的 `Speech exceeds allocated timeline stage`：
它对每个语音片段探测实际文件时长，只允许比计划超出 1 帧。
"""
import argparse
import json
import subprocess
from pathlib import Path


def probe_seconds(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                          '-of', 'default=nw=1:nk=1', str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeline', required=True)
    parser.add_argument('--top', type=int, default=8)
    parser.add_argument('--json')
    args = parser.parse_args()

    data = json.loads(Path(args.timeline).read_text(encoding='utf-8'))
    fps = data['fps']
    rows = []
    for item in data['audio']:
        path = Path(item['path'])
        planned = item['duration_frames'] / fps
        if not path.exists():
            rows.append({'word': item['word_index'], 'role': item['role'], 'status': 'MISSING',
                         'path': str(path)})
            continue
        actual = probe_seconds(path)
        rows.append({'word': item['word_index'], 'role': item['role'],
                     'planned_s': round(planned, 6), 'actual_s': round(actual, 6),
                     'overflow_s': round(actual - planned, 6),
                     'overflow_frames': round((actual - planned) * fps, 3),
                     'exceeds': actual > planned + 1 / fps})
    bad = [row for row in rows if row.get('exceeds')]
    report = {
        'timeline': args.timeline, 'fps': fps, 'items': len(rows),
        'exceeds_count': len(bad),
        'worst': sorted((row for row in rows if 'overflow_s' in row),
                        key=lambda row: row['overflow_s'], reverse=True)[:args.top],
        'bad': bad[:args.top],
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        Path(args.json).write_text(text, encoding='utf-8')
    print(text)
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())
