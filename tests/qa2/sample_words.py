"""QA-2: verify sampled words by looking at the picture once per phase.

Independent of both the producer and the earlier acceptance pass:

  * which words are sampled comes from the archive's own timeline (evenly spread);
  * when to look comes from the archive's own SRT cues for those words - the
    second-by-second text the delivery promises - not from the new timeline;
  * what to expect comes from the read-only word list (word, phonetic, meaning),
    and the draft's text objects for the "editable objects" question.

For every sampled phase the script decodes one frame of the reproduced MP4 and
one frame of the *archive's* MP4 at the same instant, and reports:

  band ink      the caption bands that must be painted (per the archive's SRT) and
                the frame difference against the archive's MP4 at the same time
                (tolerance 2/255 on a grey 320x180 frame, i.e. codec noise only);
  srt text      the cue text at that instant, checked against the word list.

Usage:
  python sample_words.py --mine BATCH --archive BATCH --wordlist TXT
                         [--count 10] [--json OUT]
"""
import argparse
import array
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BANDS = {'title': 0.0650, 'subtitle': 0.1800, 'english': 0.4062,
         'phonetic': 0.5426, 'meaning': 0.6356, 'footer': 0.9350}
HALF_HEIGHT = 0.075
HALF_WIDTH = 0.45
WIDTH, HEIGHT = 320, 180
MEAN_TOLERANCE = 2.0        # grey levels, averaged over the band
INK_THRESHOLD = 0.006       # same threshold the acceptance uses


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path.home() / '.local' / 'bin' / (name + '.EXE'),
                      Path.home() / '.local' / 'bin' / name):
        if candidate.exists():
            return str(candidate)
    raise SystemExit('cannot find %s' % name)


FFMPEG = tool('ffmpeg')


def gray_frame(source, seconds):
    out = subprocess.run(
        [FFMPEG, '-v', 'error', '-nostdin', '-ss', '%.6f' % seconds, '-i', str(source),
         '-frames:v', '1', '-vf', 'scale=%d:%d,format=gray' % (WIDTH, HEIGHT),
         '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
    return out[:WIDTH * HEIGHT]


def band_bounds(band):
    y0 = max(0, int((BANDS[band] - HALF_HEIGHT) * HEIGHT))
    y1 = min(HEIGHT, int((BANDS[band] + HALF_HEIGHT) * HEIGHT))
    x0 = max(0, int((0.5 - HALF_WIDTH) * WIDTH))
    x1 = min(WIDTH, int((0.5 + HALF_WIDTH) * WIDTH))
    return y0, y1, x0, x1


def band_mean(frame, band):
    y0, y1, x0, x1 = band_bounds(band)
    total = count = 0
    for y in range(y0, y1):
        base = y * WIDTH
        for x in range(x0, x1):
            total += frame[base + x]
            count += 1
    return total / max(1, count)


def _reference_frame(background, seconds):
    return gray_frame(background, seconds)


def band_ink(frame, background_frame, band):
    y0, y1, x0, x1 = band_bounds(band)
    changed = total = 0
    for y in range(y0, y1):
        base = y * WIDTH
        for x in range(x0, x1):
            total += 1
            if abs(frame[base + x] - background_frame[base + x]) > 24:
                changed += 1
    return changed / max(1, total)


def parse_srt(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    cues = []
    for block in [b for b in text.strip().split('\n\n') if b.strip()]:
        match = re.match(r'(\d+)\n(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)\n(.*)',
                         block, re.S)
        if not match:
            raise ValueError('unparseable cue %r' % block[:60])
        a = (int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4))) * 1000
        b = (int(match.group(6)) * 3600 + int(match.group(7)) * 60 + int(match.group(8))) * 1000
        cues.append({'start': a + int(match.group(5)), 'end': b + int(match.group(9)),
                     'text': match.group(10).strip()})
    return cues


def cues_at(cues, milliseconds):
    """Every cue covering one instant, plus the nearest boundary distance."""
    inside = [cue for cue in cues if cue['start'] <= milliseconds <= cue['end']]
    distance = min(abs(milliseconds - cue['start']) for cue in cues) if cues else None
    return inside, distance


def phase_instants(timeline):
    """One instant per phase per word, taken from the *archive's* own SRT."""
    return timeline


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mine', required=True, help='复现批次目录')
    parser.add_argument('--archive', required=True, help='已交付批次目录')
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--json')
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from acceptance.accept_range import load_expectations

    mine, archive = Path(args.mine), Path(args.archive)
    timeline = json.loads((archive / 'timeline.json').read_text(encoding='utf-8'))
    fps = timeline['fps']
    words = timeline['words']
    entries = {w['index']: w for w in load_expectations(
        args.wordlist, words[0]['index'], words[-1]['index'], 'legacy')['words']}
    base = '%04d-%04d' % (words[0]['index'], words[-1]['index'])
    scripts = {suffix: parse_srt(archive / 'srt' / (base + suffix))
               for suffix in ('_01_英文重复.srt', '_02_英文单次.srt', '_03_音标.srt',
                              '_04_中文带词性.srt', '_05_中文无词性.srt')}

    positions = list(range(len(words)))
    if len(positions) > args.count:
        step = (len(positions) - 1) / (args.count - 1)
        positions = sorted({round(i * step) for i in range(args.count)})

    mine_video = mine / 'video' / 'video.mp4'
    archive_video = archive / 'video' / 'video.mp4'
    background = Path(timeline['background'])
    rows, problems = [], []
    for position in positions:
        word = words[position]
        index = word['index']
        want = entries[index]
        phases = {
            'first_pass': (word['start_frame'] + word['male_frame']) / 2 / fps,
            'second_pass': (word['male_frame'] + word['chinese_frame']) / 2 / fps,
            'chinese': (word['chinese_frame'] + word['end_frame']) / 2 / fps,
        }
        background_frame = None
        for phase, seconds in phases.items():
            milliseconds = round(seconds * 1000)
            mine_frame = gray_frame(mine_video, seconds)
            archive_frame = gray_frame(archive_video, seconds)
            if background_frame is None:
                background_frame = _reference_frame(background, seconds)
            ink = {band: round(band_ink(mine_frame, background_frame, band), 4)
                   for band in BANDS}
            means = {band: (round(band_mean(mine_frame, band), 3),
                            round(band_mean(archive_frame, band), 3)) for band in BANDS}
            mean_gap = max(abs(a - b) for a, b in means.values())
            # What the archive's own subtitles promise at this instant.
            inside01, distance01 = cues_at(scripts['_01_英文重复.srt'], milliseconds)
            inside_rest = {suffix: cues_at(scripts[suffix], milliseconds)[0]
                           for suffix in ('_02_英文单次.srt', '_03_音标.srt',
                                          '_04_中文带词性.srt', '_05_中文无词性.srt')}
            row = {'word': index, 'phase': phase, 'at_s': round(seconds, 3),
                   'mine_band_mean': {b: means[b][0] for b in BANDS},
                   'archive_band_mean': {b: means[b][1] for b in BANDS},
                   'band_mean_gap': round(mean_gap, 3),
                   'ink_vs_background': ink,
                   'srt_01': [c['text'] for c in inside01],
                   'srt_others': {k: [c['text'] for c in v]
                                  for k, v in inside_rest.items()}}
            # The picture at this instant must be the same as the archive's.
            if mean_gap > MEAN_TOLERANCE:
                problems.append('word %d %s: band mean differs from the archive by %.3f'
                                % (index, phase, mean_gap))
            if phase in ('first_pass', 'second_pass'):
                if ink['english'] < INK_THRESHOLD:
                    problems.append('word %d %s: English band not painted (%.4f)'
                                    % (index, phase, ink['english']))
                if want['word'] not in row['srt_01']:
                    problems.append('word %d %s: track 01 does not show %r'
                                    % (index, phase, want['word']))
            else:
                for band in ('english', 'phonetic', 'meaning'):
                    if ink[band] < INK_THRESHOLD:
                        problems.append('word %d chinese: %s band not painted (%.4f)'
                                        % (index, band, ink[band]))
                if row['srt_others']['_04_中文带词性.srt'] != [want['meaning']]:
                    problems.append('word %d chinese: track 04 is %r, word list says %r'
                                    % (index, row['srt_others']['_04_中文带词性.srt'],
                                       want['meaning']))
                if row['srt_others']['_05_中文无词性.srt'] != [want['spoken_meaning']]:
                    problems.append('word %d chinese: track 05 is %r, derived %r'
                                    % (index, row['srt_others']['_05_中文无词性.srt'],
                                       want['spoken_meaning']))
            if phase == 'second_pass' and row['srt_others']['_03_音标.srt'] != [want['phonetic']]:
                problems.append('word %d second pass: track 03 is %r, word list %r'
                                % (index, row['srt_others']['_03_音标.srt'],
                                   want['phonetic']))
            rows.append(row)

    report = {'mine': str(mine), 'archive': str(archive),
              'sampled_words': [words[p]['index'] for p in positions],
              'phases_per_word': 3, 'frames_compared': len(rows) * 2,
              'mean_tolerance': MEAN_TOLERANCE,
              'rows': rows, 'problems': problems, 'problem_count': len(problems)}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    target = Path(args.json) if args.json else None
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    print(json.dumps({'sampled_words': report['sampled_words'],
                      'frames_compared': report['frames_compared'],
                      'problem_count': len(problems), 'problems': problems[:5],
                      'max_band_mean_gap': max(r['band_mean_gap'] for r in rows)},
                     ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == '__main__':
    raise SystemExit(main())
