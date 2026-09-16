"""Build QA-2's own reproduction request from the delivered archive.

Independence: every expectation this file feeds comes from the *archive itself* -
its published ``timeline.json`` (word text, voice ids, per-stage audio-cache
locations, background and intro media) and the audio files those records point
at.  It deliberately does not read H0's ``p1-50词复现-local.json`` fixture, so the
reproduction is driven by a second, independently derived input; the two can be
diffed afterwards as an extra check.

The words come from the read-only word list through QA's own parser (the same
one ``accept_range.py`` uses), so a wrong word in the timeline would be caught
even before the run.

Usage:
  python build_request.py --archive BATCH --wordlist TXT --out REQUEST.json
                          --output DIR [--range 151-200] [--first 151] [--last 200]
                          [--background PATH] [--intro PATH] [--width 1920]
                          [--height 1080] [--fps 60] [--speed 1.25]
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance.accept_range import load_expectations  # noqa: E402


def stage_items(timeline, first, last, cache_suffix='original.volcengine_legacy.ogg'):
    """One provider item per published audio stage inside [first, last]."""
    items = []
    for stage in timeline['audio']:
        if not (first <= int(stage['word_index']) <= last):
            continue
        prepared = Path(stage['path'])
        cache_dir = prepared.parent
        original = cache_dir / cache_suffix
        if not original.exists():
            raise SystemExit('原归档缺少未加工音频：%s' % original)
        items.append({'index': int(stage['word_index']), 'role': stage['role'],
                      'text': stage['text'], 'voice': stage.get('voice', ''),
                      'path': str(original)})
    return items


def styles_from(timeline):
    """The published styles, minus the fields the request contract does not take."""
    return {role: dict(style) for role, style in timeline.get('styles', {}).items()}


def config(**overrides):
    """The same knobs as the CLI, as a mapping that also answers attribute access.

    Tests and tools use this instead of argparse's Namespace, so there is exactly
    one definition of the defaults.
    """
    settings = {'archive': None, 'wordlist': None, 'out': None, 'output': None,
                'key': 'qa2-replay-from-archive-r1', 'first': 151, 'last': 200,
                'background': None, 'intro': None, 'intro_s': 1.0, 'width': 1920,
                'height': 1080, 'fps': 60, 'speed': 1.25, 'video_codec': None,
                'concurrency': 1, 'source_mode': False, 'policy': None,
                'spoken_file': None}
    settings.update(overrides)

    class Config(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name) from None

    return Config(settings)


def build_request(args):
    timeline = json.loads(Path(args.archive).read_text(encoding='utf-8')
                          if args.archive.endswith('.json')
                          else (Path(args.archive) / 'timeline.json').read_text(
                              encoding='utf-8'))
    entries = load_expectations(args.wordlist, args.first, args.last, 'legacy')['words']
    # The words we ask for come from the read-only word list; the archive is then
    # checked against them, so a mismatch is reported as a finding of this script
    # instead of being copied into the request.
    published = {int(w['index']): w for w in timeline['words']}
    missing = [w['index'] for w in entries if w['index'] not in published]
    if not args.source_mode:
        if missing:
            raise SystemExit('归档 timeline 里没有这些词：%s' % missing)
        for want in entries:
            got = published[want['index']]
            if (got['word'], got['phonetic'], got['meaning'],
                    got.get('spoken_meaning')) != (want['word'], want['phonetic'],
                                                   want['meaning'], want['spoken_meaning']):
                raise SystemExit('归档 timeline 第 %d 个词与只读词表不一致：%r'
                                 % (want['index'], got))
    spoken = {}
    if args.spoken_file:
        raw = json.loads(Path(args.spoken_file).read_text(encoding='utf-8'))
        spoken = {int(key): value for key, value in raw.items()}
    lesson = {
        'title': timeline.get('title', '四级1500高频词'),
        'footer': timeline.get('footer', '不积小流 无以成江海'),
        'batch_size': max(1, len(entries)),
        'speed': float(timeline.get('speed', args.speed)),
        'fps': int(timeline.get('fps', args.fps)),
        'width': int(timeline.get('width', args.width)),
        'height': int(timeline.get('height', args.height)),
        'intro_s': float(args.intro_s),
        'first_six': 0.4, 'extra': 0.2, 'gap_s': 0.1,
        'background': args.background or timeline['background'],
        'video_codec': args.video_codec or timeline.get('video_codec', 'h264'),
        'styles': styles_from(timeline),
    }
    if args.source_mode:
        # The reading text is derived by the job from the read-only word list
        # under this policy, so the request deliberately carries no
        # lesson.entries: an independent table is then used to judge the result.
        if not args.policy:
            raise SystemExit('source 模式需要 --policy，让作业自己推导朗读文本')
        lesson['spoken_policy'] = args.policy
    else:
        lesson['entries'] = [{'index': w['index'], 'word': w['word'],
                              'phonetic': w['phonetic'], 'meaning': w['meaning'],
                              'spoken_meaning': w['spoken_meaning']}
                             for w in entries]
    intro = args.intro if args.intro is not None else (timeline.get('intro_video') or '')
    if intro:
        lesson['intro'] = {'video': intro}
    items = stage_items(timeline, args.first, args.last)
    if spoken:
        # A local item carries the text of the recording it points at.  Only the
        # Chinese stage reads the meaning, so only it follows the spoken policy;
        # the two English stages read the word itself.
        for item in items:
            if item['role'] == 'chinese' and item['index'] in spoken:
                item['text'] = spoken[item['index']]
    request = {
        'idempotency_key': args.key,
        'output': str(Path(args.output).resolve()),
        'lesson': lesson,
        'provider': {'kind': 'local', 'items': items,
                     'concurrency': int(args.concurrency)},
    }
    if args.source_mode:
        request['source'] = {'path': str(Path(args.wordlist).resolve())}
        request['range'] = {'start': args.first, 'end': args.last}
    return request


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True, help='已交付批次目录（只读）')
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--out', required=True, help='请求 JSON 输出路径')
    parser.add_argument('--output', required=True, help='作业输出根目录')
    parser.add_argument('--key', default='qa2-replay-from-archive-r1')
    parser.add_argument('--range', default='151-200')
    parser.add_argument('--first', type=int, default=None)
    parser.add_argument('--last', type=int, default=None)
    parser.add_argument('--background', default=None)
    parser.add_argument('--intro', default=None)
    parser.add_argument('--intro-s', dest='intro_s', type=float, default=1.0)
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--speed', type=float, default=1.25)
    parser.add_argument('--video-codec', dest='video_codec', default=None)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--policy', default=None, choices=['v2', 'legacy'],
                        help='source 模式：作业按该政策从只读词表推导朗读文本')
    parser.add_argument('--source-mode', dest='source_mode', action='store_true',
                        help='发 source+range 请求（不携带 lesson.entries）')
    parser.add_argument('--spoken-file', dest='spoken_file', default=None,
                        help='{index: 朗读文本} JSON，用来给本地 provider 的 chinese 条目正名')
    args = parser.parse_args(argv)
    first, last = args.range.split('-')
    args.first = args.first if args.first is not None else int(first)
    args.last = args.last if args.last is not None else int(last)
    request = build_request(args)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'request': str(target),
                      'words': len(request['lesson'].get('entries', ())) or
                      args.last - args.first + 1,
                      'mode': 'source+range' if args.source_mode else 'entries',
                      'spoken_policy': request['lesson'].get('spoken_policy', ''),
                      'items': len(request['provider']['items']),
                      'output': request['output'],
                      'background': request['lesson']['background'],
                      'intro': request['lesson'].get('intro', {}).get('video', ''),
                      'codec': request['lesson']['video_codec']}, ensure_ascii=False,
                     indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
