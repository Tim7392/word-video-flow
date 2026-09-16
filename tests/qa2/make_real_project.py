"""QA-7: build a project root whose records really are the word list.

The published-directory check needs a project whose content is the real thing -
the words of the range and the recordings of those words - otherwise the
independent acceptance tool refuses it (correctly: A's own template carries
placeholder records like ``apple``/``/x/``, so its wordlist face fails).

This script builds that project with the documented API:

  * records come from the read-only word list through QA's own parser;
  * each record's three speech assets are the archive's *unprocessed* recordings
    (``original.volcengine_legacy.ogg``), measured here with ffprobe;
  * ``word_video.application.instantiate`` expands the built-in template, so the
    clips, their source windows and the teaching order are the engine's, not a
    hand-written document;
  * ``assets.json`` is written in the ``wv-assets@1`` shape the coordinator reads.

Usage:
  python make_real_project.py --root DIR --wordlist TXT --archive BATCH
                              --range 151-153 [--project p1] [--background FILE]
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance.accept_range import load_expectations  # noqa: E402

WORKTREE = Path(__file__).resolve().parents[2]
UNITS_PER_SECOND = 48000


def probe_seconds(path):
    from word_video.media import executable
    out = subprocess.run([executable('ffprobe'), '-v', 'error', '-show_entries',
                          'format=duration', '-of', 'csv=p=0', str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--range', default='151-153')
    parser.add_argument('--project', default='p1')
    parser.add_argument('--background', default=None)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--width', type=int, default=320)
    parser.add_argument('--height', type=int, default=180)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(WORKTREE))
    from word_video.application import instantiate
    from word_video.domain import MediaInfo, MediaSlice, Project, Record
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.storage import save_project

    first, last = (int(part) for part in args.range.split('-'))
    entries = load_expectations(args.wordlist, first, last, 'legacy')['words']
    timeline = json.loads((Path(args.archive) / 'timeline.json').read_text(
        encoding='utf-8'))
    published = {int(stage['word_index']): stage for stage in timeline['audio']
                 if stage['role'] == 'chinese'}

    records, media, assets, speech = [], {}, [], []
    for entry in entries:
        index = entry['index']
        record = Record(id='w%d' % index, word=entry['word'],
                        phonetic=entry['phonetic'], meaning=entry['meaning'],
                        spoken_meaning=entry['spoken_meaning'], index=index)
        records.append(record)
        for role in ('female', 'male', 'chinese'):
            cache = Path(published[index]['path']).parent
            original = cache / 'original.volcengine_legacy.ogg'
            if not original.exists():
                raise SystemExit('原归档缺少未加工录音：%s' % original)
            units = int(Fraction(probe_seconds(original)) * UNITS_PER_SECOND)
            asset_id = 'w%d:%s' % (index, role)
            media[asset_id] = MediaInfo(asset_id, units, 1, UNITS_PER_SECOND)
            assets.append({'asset_id': asset_id, 'kind': 'audio',
                           'path': str(original), 'sha256': '',
                           'unit_num': 1, 'unit_den': UNITS_PER_SECOND,
                           'units': units, 'voice': ''})
            speech.append((asset_id, original))

    project = instantiate(DEFAULT_LESSON_TEMPLATE, tuple(records), media,
                          Project(project_id=args.project, intro_s=0.5,
                                  fps_num=args.fps, width=args.width,
                                  height=args.height, speed=1.25))

    root = Path(args.root).resolve()
    folder = root / 'projects' / args.project
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'media').mkdir(exist_ok=True)
    document = project.to_dict()
    # The project owns its media: copy every recording under a name derived from
    # its asset id.  The archive stores each recording as
    # ``<cache>/original.volcengine_legacy.ogg``, so copying by file name would
    # collapse three different stages into one file and quietly change what the
    # timing face measures.
    for item in assets:
        target = folder / 'media' / ('%s.ogg' % item['asset_id'].replace(':', '_'))
        if not target.exists():
            target.write_bytes(Path(item['path']).read_bytes())
        item['path'] = str(target)
        for clip in document['clips']:
            source = clip.get('source') or {}
            if source.get('asset_id') == item['asset_id']:
                source['unit_num'] = item['unit_num']
                source['unit_den'] = item['unit_den']
    save_project(Project.from_dict(document), folder)
    (folder / 'assets.json').write_text(
        json.dumps({'schema': 'wv-assets@1', 'project_id': args.project,
                    'assets': assets}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    delivery = {'schema': 'wv-delivery@1', 'video_codec': 'h265',
                'background': str(Path(args.background).resolve()) if args.background
                else '', 'fps': args.fps, 'width': args.width, 'height': args.height}
    (folder / 'delivery.json').write_text(json.dumps(delivery, ensure_ascii=False,
                                                     indent=1), encoding='utf-8')

    report = {'project': str(folder), 'records': len(records),
              'assets': len(assets), 'media_files': len(list((folder / 'media').iterdir())),
              'clips': len(document['clips']), 'range': args.range,
              'words': [entry['word'] for entry in entries],
              'background': delivery['background'],
              'background_exists': Path(delivery['background']).exists()
              if delivery['background'] else None}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
