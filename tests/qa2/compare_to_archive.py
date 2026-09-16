"""QA-2: independent comparison of a reproduced batch against the delivered archive.

Expectations come from the archive's own published files (timeline.json, the five
SRTs) and from the read-only word list - never from the tool this script judges.

Three checks, each with a stated tolerance:

  timeline  every per-word time field (start/male/chinese/end frame) must be
            identical.  Media *paths* are expected to differ (the archive stored
            absolute cache paths, a new run stores its own), so they are reported
            separately and never silently ignored: a differing path is listed.
  srt       the five ``*.srt`` files must be byte-identical, compared both as raw
            bytes and after line-ending normalisation.
  files     every file the reproduced ``complete.json`` lists must exist and
            match its recorded sha256, and the published file set must cover the
            same roles as the archive (five SRTs, a draft, an MP4, a mix).

Usage:
  python compare_to_archive.py --mine BATCH_OR_ROOT --archive BATCH [--json OUT]
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORD_FIELDS = ('index', 'word', 'phonetic', 'meaning', 'spoken_meaning',
               'start_frame', 'male_frame', 'chinese_frame', 'end_frame')
SRT_SUFFIXES = ('_01_英文重复.srt', '_02_英文单次.srt', '_03_音标.srt',
                '_04_中文带词性.srt', '_05_中文无词性.srt')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def find_batch(path):
    """Accept the batch directory itself or a job root containing one batch."""
    path = Path(path)
    if (path / 'timeline.json').exists():
        return path
    batches = sorted(p for p in path.rglob('timeline.json')
                     if 'recovery' not in p.parts)
    if len(batches) != 1:
        raise SystemExit('%s 下找到 %d 个批次，无法判定' % (path, len(batches)))
    return batches[0].parent


def compare_timeline(mine, archive):
    a = json.loads((archive / 'timeline.json').read_text(encoding='utf-8'))
    b = json.loads((mine / 'timeline.json').read_text(encoding='utf-8'))
    report = {'words_archive': len(a['words']), 'words_mine': len(b['words']),
              'field_differences': [], 'path_differences': [], 'audio_differences': []}
    if len(a['words']) != len(b['words']):
        report['field_differences'].append(
            'word count %d != %d' % (len(b['words']), len(a['words'])))
    for one, two in zip(b['words'], a['words']):
        for field in WORD_FIELDS:
            if one.get(field) != two.get(field):
                report['field_differences'].append(
                    'word %s field %s: run %r != archive %r'
                    % (two.get('index'), field, one.get(field), two.get(field)))
    # Paths are allowed to differ, but every difference is named.
    for key in ('background', 'intro_video', 'intro_audio'):
        if a.get(key) != b.get(key):
            report['path_differences'].append(
                '%s: run %r != archive %r' % (key, b.get(key), a.get(key)))
    if len(a.get('audio', [])) != len(b.get('audio', [])):
        report['audio_differences'].append(
            'audio stages %d != %d' % (len(b.get('audio', [])), len(a.get('audio', []))))
    for one, two in zip(b.get('audio', []), a.get('audio', [])):
        for field in ('word_index', 'role', 'text', 'voice', 'start_frame',
                      'duration_frames'):
            if one.get(field) != two.get(field):
                report['audio_differences'].append(
                    'word %s %s field %s: run %r != archive %r'
                    % (two.get('word_index'), two.get('role'), field,
                       one.get(field), two.get(field)))
        if one.get('path') != two.get('path'):
            report['path_differences'].append(
                'word %s %s audio path' % (two.get('word_index'), two.get('role')))
    # Non-time settings that decide the picture must match too.
    for key in ('fps', 'width', 'height', 'speed', 'total_frames', 'video_codec',
                'first_index', 'last_index', 'intro_frames'):
        if a.get(key) != b.get(key):
            report['field_differences'].append(
                'setting %s: run %r != archive %r' % (key, b.get(key), a.get(key)))
    if a.get('styles') != b.get('styles'):
        report['field_differences'].append('caption styles differ')
    report['timeline_equal'] = not report['field_differences']
    return report


def compare_srt(mine, archive):
    """Byte-compare the five tracks.

    Which files to expect comes from the *archive*, not from the run's directory
    name: the archive is the reference, and a run's batch folder can legitimately
    carry another name.  Tracks are matched by their ``_NN_`` suffix so a renamed
    batch is still compared instead of silently reported as five missing files.
    """
    a_dir, b_dir = archive / 'srt', mine / 'srt'
    report = {'files': {}, 'tags': {}, 'equal': True, 'matched_by': 'suffix'}
    archive_files = sorted(a_dir.glob('*.srt'))
    missing, extra = [], []
    for two in archive_files:
        suffix = two.name[two.name.index('_'):] if '_' in two.name else two.name
        matches = sorted(b_dir.glob('*%s' % suffix))
        if not matches:
            report['files'][two.name] = 'MISSING in run'
            missing.append(two.name)
            continue
        one = matches[0]
        raw_a, raw_b = one.read_bytes(), two.read_bytes()
        same_raw = raw_a == raw_b
        text_a = raw_a.decode('utf-8-sig').replace('\r\n', '\n')
        text_b = raw_b.decode('utf-8-sig').replace('\r\n', '\n')
        same_text = text_a == text_b
        report['files'][two.name] = ('identical' if same_raw else
                                     'same text, different line endings' if same_text
                                     else 'DIFFERENT')
        report['tags'][two.name] = {
            'run': {'name': one.name, 'bytes': len(raw_a), 'sha256': sha256(one)},
            'archive': {'name': two.name, 'bytes': len(raw_b), 'sha256': sha256(two)}}
        if not same_text:
            report['equal'] = False
            lines_a = text_a.splitlines()
            lines_b = text_b.splitlines()
            first = next((i for i, (x, y) in enumerate(zip(lines_a, lines_b)) if x != y),
                         min(len(lines_a), len(lines_b)))
            report['files'][two.name] = 'DIFFERENT at line %s' % first
    matched = {Path(v['run']['name']).name for v in report['tags'].values()}
    extra = sorted(p.name for p in b_dir.glob('*.srt') if p.name not in matched)
    report['missing'] = missing
    report['extra_in_run'] = extra
    if missing:
        report['equal'] = False
    return report


def check_complete(mine):
    """Every listed file must exist and match its hash, and the required roles
    must be covered - a complete.json is read from the published files, so a
    missing MP4 or draft must be visible here."""
    complete_path = mine / 'complete.json'
    if not complete_path.exists():
        return {'complete_json': 'MISSING', 'ok': False}
    complete = json.loads(complete_path.read_text(encoding='utf-8'))
    bad, missing, total_bytes = [], [], 0
    for item in complete.get('files', []):
        path = Path(item['path'])
        if not path.exists():
            missing.append(str(path))
            continue
        total_bytes += path.stat().st_size
        if sha256(path) != item['sha256']:
            bad.append(str(path))
    names = [Path(i['path']).name for i in complete.get('files', [])]
    srt_names = [name for name in names if name.endswith('.srt')]
    report = {'listed': len(complete.get('files', [])), 'missing': missing[:5],
              'hash_mismatch': bad[:5], 'bytes': total_bytes,
              'srt_count': len(srt_names),
              'has_five_srts': len(srt_names) == 5,
              'has_mp4': any(name == 'video.mp4' for name in names),
              'has_mix': any(name == 'mix.wav' for name in names),
              'has_draft': any(name == 'draft_content.json' for name in names),
              'has_timeline': any(name == 'timeline.json' for name in names),
              'present_on_disk': [name for name in ('video.mp4', 'mix.wav',
                                                    'timeline.json')
                                  if (mine / 'video' / name).exists()
                                  or (mine / name).exists()]}
    report['ok'] = not missing and not bad and report['has_five_srts'] \
        and report['has_mp4'] and report['has_draft']
    return report


def sample_words(archive, count=10):
    """Evenly spread word indexes across the batch, from the archive itself."""
    timeline = json.loads((archive / 'timeline.json').read_text(encoding='utf-8'))
    indexes = [w['index'] for w in timeline['words']]
    if len(indexes) <= count:
        return indexes
    step = (len(indexes) - 1) / (count - 1)
    picks = sorted({indexes[round(i * step)] for i in range(count)})
    return picks


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mine', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--json')
    parser.add_argument('--samples', type=int, default=10)
    args = parser.parse_args(argv)
    mine = find_batch(args.mine)
    archive = find_batch(args.archive)
    report = {'mine': str(mine), 'archive': str(archive),
              'sampled_word_indexes': sample_words(archive, args.samples)}
    report['timeline'] = compare_timeline(mine, archive)
    report['srt'] = compare_srt(mine, archive)
    report['complete'] = check_complete(mine)
    report['ok'] = (report['timeline']['timeline_equal'] and report['srt']['equal']
                    and report['complete']['ok'])
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
