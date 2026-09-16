"""H0 工具：用旧归档批次的 timeline.json + 缓存音频，构造"离线复现 50 词"请求。

目的：在迁入后的新仓库里用**完全离线**的真实媒体与已缓存真实配音，复现归档批次 0151-0200，
从而拿到两条证据：
  1) 新入口在批量规模上可跑通（不是只跑得动 3 词）；
  2) 同一输入下新产物的 timeline/SRT 与归档一致（迁移等价性）。

只读旧归档；输出写到工作根 out/。不调用任何 TTS。
"""
import argparse
import json
from pathlib import Path


def build(archive_batch: Path, output: Path, key: str, fixture: Path):
    timeline = json.loads((archive_batch / 'timeline.json').read_text(encoding='utf-8'))
    entries = []
    for word in timeline['words']:
        entries.append({
            'index': word['index'],
            'word': word['word'],
            'phonetic': word['phonetic'],
            'meaning': word['meaning'],
            'spoken_meaning': word['spoken_meaning'],
        })
    speech = []
    missing = []
    for asset in timeline['audio']:
        prepared = Path(asset['path'])
        token = prepared.parent
        complete = token / 'complete.json'
        if not complete.exists():
            missing.append('complete.json: %s' % token)
            continue
        meta = json.loads(complete.read_text(encoding='utf-8'))
        # 时间线用的是哪一路原始音频由 complete.json 的 spec.timeline_route 决定；
        # 同名 token 目录里可能同时存在 original.jianying.ogg 与 original.volcengine_legacy.ogg，
        # 取错会让"已加工"的时长与原始时长对不上。
        route = (meta.get('spec') or {}).get('timeline_route') or ''
        candidates = list(token.glob('original.%s.*' % route)) if route else []
        originals = candidates or sorted(token.glob('original.*'))
        if not originals:
            missing.append('original audio: %s' % token)
            continue
        # 走 provider.kind=local：这是引擎"复用已有音频"的正规入口——
        # 由引擎自己探测原件真实时长、自己做变速加工，并把时间线指向加工后的 prepared.wav。
        # （prepared_speech 的语义是"调用方已给加工好的音频"，喂入未变速原件会被正确拒绝。）
        speech.append({
            'index': asset['word_index'],
            'role': asset['role'],
            'text': asset['text'],
            'voice': asset.get('voice') or meta.get('spec', {}).get('voice') or '',
            'path': str(originals[0]),
        })
    request = {
        'idempotency_key': key,
        'output': str(output),
        'lesson': {
            'entries': entries,
            'title': timeline.get('title', ''),
            'footer': timeline.get('footer', ''),
            'batch_size': 50,
            'speed': timeline['speed'],
            'fps': timeline['fps'],
            'width': timeline['width'],
            'height': timeline['height'],
            'first_six': 0.4,
            'extra': 0.2,
            'gap_s': 0.1,
            'background': timeline['background'],
            'video_codec': timeline.get('video_codec', 'h265'),
            'styles': timeline.get('styles', {}),
        },
        'prepared_speech': None,
        'provider': {'kind': 'local', 'items': speech, 'concurrency': 4},
    }
    if timeline.get('intro_video'):
        request['lesson']['intro'] = {'video': timeline['intro_video']}
    if timeline.get('intro_audio'):
        request['lesson']['intro_audio'] = timeline['intro_audio']
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding='utf-8')
    report = {
        'archive_batch': str(archive_batch),
        'fixture': str(fixture),
        'output': str(output),
        'entries': len(entries),
        'speech_from_cache': len(speech),
        'speech_missing': len(missing),
        'missing_examples': missing[:5],
        'archive_total_frames': timeline.get('total_frames'),
        'archive_intro_frames': timeline.get('intro_frames'),
        'fps': timeline['fps'],
        'canvas': [timeline['width'], timeline['height']],
    }
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive-batch', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--key', default='p1-replay-151-200-offline')
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--report')
    args = parser.parse_args()
    report = build(Path(args.archive_batch), Path(args.output), args.key, Path(args.fixture))
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8')
    print(text)
    return 0 if report['speech_missing'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
