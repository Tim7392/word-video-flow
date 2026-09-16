"""Independent acceptance of one published batch.

Origin: M0 `word_video_work/accept_range.py` (2026-09-16), moved into the new
repository by QA (task QA-1).  The eight faces and every threshold are the ones
M0 approved; nothing was relaxed.  What changed in the move, and why:

  * no M0 path is baked in: ffmpeg/ffprobe are resolved from PATH or the user's
    local bin, and everything else (batch, word list, range, report path) is a
    command line argument, so the tool runs self-contained inside any worktree;
  * the "policy <x>" note in a spoken-text failure read a ``_cleaning`` key that
    was never stored; the policy name is passed along instead (the note only,
    the comparison itself is unchanged);
  * a media file that ffprobe cannot read is reported as a problem of that face
    instead of crashing the whole validator;
  * ``--json`` always writes its parent directory, so a caller may keep reports
    outside the tree being judged.

Why the expectations do not come from the timeline
--------------------------------------------------
The first (pre-M0) version re-derived every expectation from the very
``timeline.json`` the pipeline had just produced.  A coordinated mistake - the
same wrong word in the timeline, the SRTs and the draft - therefore passed all
seven faces.  Expectations now come from the **read-only word list**
(``--source`` + ``--range``), parsed here; missing expectations refuse to judge
instead of printing PASS.

Usage:
  python accept_range.py BATCH --source WORDLIST --range 151-200
                           [--cleaning legacy|v2] [--pixels all|first|none]
                           [--json OUT]
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import wave

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SUFFIXES = ('_01_英文重复.srt', '_02_英文单次.srt', '_03_音标.srt',
            '_04_中文带词性.srt', '_05_中文无词性.srt')
# Produced by the job itself rather than by the batch, so they are not part of
# the published file list.
BOOKKEEPING = {'complete.json', 'job-owner.json'}
POS = re.compile(r'\b(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr|pl)\.')
PHONETIC = re.compile(r'/[^/\s]+/')
POS_TOKEN = re.compile(r'^(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr|pl)\.$')
# A part-of-speech tag together with the connectors that only join tags, so that
# "n. & v." goes away as a unit while a real "R&D" in the definition survives.
POS_WORD = r'(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr|pl)\.'
# ``\b`` is useless next to a Chinese character ("根本的n." has no word boundary),
# so the lookbehind rejects only a preceding Latin letter: that catches a tag glued
# to Chinese while still refusing to eat the tail of an English word like "print.".
POS_FLEX = re.compile(r'(?<![A-Za-z])%s' % POS_WORD)
POS_RUN = re.compile(r'(?<![A-Za-z])%s(?:\s*(?:&|/|,|，|、)\s*%s)*'
                     % (POS_WORD, POS_WORD))
# Caption band centres, read from the published ASS positions.  Kept here so a
# style change cannot silently move the expectation.
BANDS = {'title': 0.0650, 'subtitle': 0.1800, 'english': 0.4062,
         'phonetic': 0.5426, 'meaning': 0.6356, 'footer': 0.9350}
BAND_HALF_HEIGHT = 0.075
BAND_HALF_WIDTH = 0.45
BAND_HALF_HEIGHT_TIGHT = 0.045
INK_THRESHOLD = 0.006
# The two directions need two thresholds, and the second one is measured, not
# guessed.  A frame of the published MP4 is compared with a frame of the *source*
# background, so re-encoding leaves a noise floor: over 151 probes of a 50-word
# batch the highest reading in a band that had to stay empty was 0.0061 (0.61%),
# while the smallest genuine caption measured 0.0206 (2.06%).  Anything above
# 1.5% is therefore a caption that appeared too early, and codec noise stays
# below it.  The "must be painted" side keeps the original 0.6%.
EARLY_INK_THRESHOLD = 0.015
# Frame counts are derived from real media, so allow the rounding of one frame
# at each end.  Anything larger is a timeline that disagrees with its own audio.
MEDIA_TOLERANCE_FRAMES = 2
# AAC in an MP4 cannot be compared sample by sample with the PCM mix; the
# envelope correlation and a 150 ms tail allowance are the stated tolerances.
TAIL_TOLERANCE_SECONDS = 0.15


def resolve(tool):
    """Locate ffmpeg/ffprobe without importing the pipeline under test."""
    found = shutil.which(tool)
    if found:
        return found
    for candidate in (Path.home() / '.local' / 'bin' / (tool + '.EXE'),
                      Path.home() / '.local' / 'bin' / tool,
                      Path(r'C:\ffmpeg\bin') / (tool + '.exe')):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError('cannot find %s' % tool)


FFMPEG = resolve('ffmpeg')
FFPROBE = resolve('ffprobe')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run_json(args):
    out = subprocess.run(args, capture_output=True, check=True)
    return json.loads(out.stdout.decode('utf-8', 'replace'))


def probe(path):
    return run_json([FFPROBE, '-v', 'error', '-print_format', 'json',
                     '-show_format', '-show_streams', str(path)])


def media_seconds(path):
    info = probe(path)
    return float(info['format']['duration'])


def has_audio_stream(path):
    info = probe(path)
    return any(s['codec_type'] == 'audio' for s in info['streams'])


def parse_srt(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    cues = []
    for block in [b for b in text.strip().split('\n\n') if b.strip()]:
        match = re.match(r'(\d+)\n(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)\n(.*)',
                         block, re.S)
        if not match:
            raise ValueError('unparseable cue in %s: %r' % (Path(path).name, block[:60]))
        a = (int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4))) * 1000
        b = (int(match.group(6)) * 3600 + int(match.group(7)) * 60 + int(match.group(8))) * 1000
        cues.append({'number': int(match.group(1)), 'start_ms': a + int(match.group(5)),
                     'end_ms': b + int(match.group(9)), 'text': match.group(10)})
    return cues


def frame_to_ms(frame, fps):
    return (frame * 2000 + fps) // (2 * fps)


def cjk(text):
    """Only the Chinese, so punctuation and spacing cannot hide a difference."""
    return re.sub(r'[^\u4e00-\u9fff]+', '', text)


# --- 独立期望：从只读词表解析出这个范围应该长什么样 -----------------------

def split_entry(line):
    """word [phonetic] meaning...  - parsed here, independent of the pipeline.

    The meaning is sliced out of the original line rather than rebuilt from
    tokens, because the word list contains double spaces and tags glued to
    Chinese ("n. 文物adj. 陈旧的"); the published display text keeps both.
    """
    phonetic = PHONETIC.search(line)
    tag = POS_FLEX.search(line)
    boundary = None
    meaning_at = 0
    if phonetic:
        boundary, meaning_at = phonetic.start(), phonetic.end()
    elif tag:
        boundary, meaning_at = tag.start(), tag.start()
    if boundary is None:
        return line.strip(), '', ''
    word = line[:boundary].strip()
    if not word:
        raise ValueError('no word before the phonetic or part of speech: %r' % line)
    return word, (phonetic.group(0) if phonetic else ''), line[meaning_at:].strip()


def spoken_of(meaning, cleaning):
    """The spoken line: display meaning minus what must not be read out.

    legacy  what the delivered batches were built with - parts of speech only, so
            "n. & v. 谋 杀" is read as "& 谋 杀" and an embedded "/kənˈtent/" is read
            out loud.
    v2      the policy the user approved on 2026-09-16 - a part-of-speech tag is
            removed together with the connectors that only join tags, and phonetics
            embedded in the definition go too.  Anything that is part of the real
            definition ("R&D", "A/B", a slash inside a word) is left alone.
    """
    if cleaning == 'legacy':
        # What the delivered batches were built with: each tag is removed and
        # nothing else, so the spaces around it stay ("adj. 故意的 v. 仔细考虑"
        # becomes "故意的  仔细考虑").  A tag glued to Chinese counts as a tag.
        text = POS_FLEX.sub('', meaning)
    else:
        text = POS_RUN.sub(' ', meaning)
        text = PHONETIC.sub(' ', text)
    return text.strip() if cleaning == 'legacy' else ' '.join(text.split())


def load_expectations(wordlist, first, last, cleaning, spoken_file=None):
    """Expectations for one range.

    ``spoken_file`` optionally replaces the *derived* reading text with a table
    computed elsewhere (``{index: spoken_meaning}`` JSON).  The acceptance tool
    then compares the batch against that table instead of its own rule, which is
    how an approved policy change is judged without editing the tool.  The word,
    phonetic and display meaning still come from the read-only word list.
    """
    path = Path(wordlist)
    lines = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()]
    entries = [line for line in lines if line]
    if not (1 <= first <= last <= len(entries)):
        raise SystemExit('range %d-%d is outside the %d entries of %s'
                         % (first, last, len(entries), path))
    supplied = {}
    if spoken_file:
        raw = json.loads(Path(spoken_file).read_text(encoding='utf-8'))
        supplied = {int(key): value for key, value in raw.items()}
    words = []
    for index in range(first, last + 1):
        word, phonetic, meaning = split_entry(entries[index - 1])
        words.append({'index': index, 'word': word, 'phonetic': phonetic,
                      'meaning': meaning,
                      'spoken_meaning': supplied.get(index, spoken_of(meaning, cleaning))})
    return {'source': str(path), 'source_sha256': sha256(path), 'cleaning': cleaning,
            'spoken_file': str(spoken_file) if spoken_file else None,
            'first': first, 'last': last, 'words': words}


# --- 各面检查 -------------------------------------------------------------

def check_integrity(batch, report):
    complete_path = batch / 'complete.json'
    if not complete_path.exists():
        report['integrity'] = {'problem_count': 1, 'problems': ['complete.json missing']}
        return ['complete.json missing: nothing proves what was published']
    complete = json.loads(complete_path.read_text(encoding='utf-8'))
    problems, checked = [], 0
    listed = set()
    for item in complete.get('files', []):
        path = Path(item['path'])
        listed.add(str(path).lower())
        if not path.exists():
            problems.append('missing: %s' % path)
            continue
        actual = sha256(path)
        if actual != item['sha256']:
            problems.append('hash mismatch: %s' % path.name)
        checked += 1
    if not complete.get('files'):
        problems.append('complete.json lists no files')
    extra = [str(p) for p in batch.rglob('*')
             if p.is_file() and str(p).lower() not in listed
             and p.name not in BOOKKEEPING
             and 'audio-cache' not in p.parts and 'recovery' not in p.parts]
    if extra:
        problems.append('files not covered by complete.json: %d' % len(extra))
    report['integrity'] = {'files': checked, 'unlisted': extra[:5], 'problems': problems,
                           'problem_count': len(problems)}
    return problems


def check_video(batch, manifest, report):
    problems = []
    video = batch / 'video/video.mp4'
    if not video.exists():
        report['video'] = {'problems': ['video/video.mp4 missing'], 'problem_count': 1}
        return ['video/video.mp4 missing']
    try:
        info = probe(video)
    except Exception as error:
        report['video'] = {'problems': ['video/video.mp4 unreadable: %r' % error],
                           'problem_count': 1}
        return ['video/video.mp4 unreadable: %r' % error]
    stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    fps = manifest['fps']
    if 'video_codec' not in manifest:
        problems.append('timeline.json has no %r (schema_version=%s)'
                        % ('video_codec', manifest.get('schema_version')))
        codec = None
    else:
        codec = manifest['video_codec']
    duration = float(info['format']['duration'])
    if codec is not None and stream['codec_name'] != {'h265': 'hevc', 'h264': 'h264'}[codec]:
        problems.append('codec %s != %s' % (stream['codec_name'], codec))
    if (stream['width'], stream['height']) != (manifest['width'], manifest['height']):
        problems.append('canvas %sx%s != %sx%s' % (stream['width'], stream['height'],
                                                   manifest['width'], manifest['height']))
    if stream['avg_frame_rate'] != '%d/1' % fps:
        problems.append('fps %s' % stream['avg_frame_rate'])
    if int(stream['nb_frames']) != manifest['total_frames']:
        problems.append('frames %s != %s' % (stream['nb_frames'], manifest['total_frames']))
    if abs(duration - manifest['total_frames'] / fps) > 0.15:
        problems.append('duration %.3f != %.3f' % (duration, manifest['total_frames'] / fps))
    if stream['pix_fmt'] != 'yuv420p':
        problems.append('pix_fmt %s' % stream['pix_fmt'])
    if audio is None:
        problems.append('no audio stream')
    report['video'] = {'codec': stream['codec_name'], 'profile': stream.get('profile'),
                       'canvas': [stream['width'], stream['height']],
                       'fps': stream['avg_frame_rate'], 'frames': int(stream['nb_frames']),
                       'pix_fmt': stream['pix_fmt'], 'duration_s': round(duration, 3),
                       'audio': audio and audio['codec_name'],
                       'audio_rate': audio and audio['sample_rate'],
                       'size_mb': round(int(info['format']['size']) / 1048576, 1),
                       'bitrate_kbps': round(int(info['format']['bit_rate']) / 1000),
                       'problems': problems, 'problem_count': len(problems)}
    return problems


def check_mix(batch, manifest, report):
    problems = []
    mix = batch / 'video/mix.wav'
    if not mix.exists():
        report['mix'] = {'problems': ['video/mix.wav missing'], 'problem_count': 1}
        return ['video/mix.wav missing']
    with wave.open(str(mix), 'rb') as stream:
        shape = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate())
        frames = stream.getnframes()
    want = round(manifest['total_frames'] * 48000 / manifest['fps'])
    if shape != (1, 2, 48000):
        problems.append('mix.wav is %s, not mono 48k s16' % (shape,))
    if frames != want:
        problems.append('mix.wav %d samples != timeline %d' % (frames, want))
    report['mix'] = {'channels': shape[0], 'rate': shape[2], 'samples': frames,
                     'expected_samples': want, 'problems': problems,
                     'problem_count': len(problems)}
    return problems


def check_srt(batch, manifest, report, expectations, cleaning='legacy'):
    problems = []
    words = manifest['words']
    fps = manifest['fps']
    base = '%04d-%04d' % (words[0]['index'], words[-1]['index'])
    tracks, counts = {}, {}
    for suffix in SUFFIXES:
        path = batch / 'srt' / (base + suffix)
        if not path.exists():
            problems.append('missing subtitle track: %s' % path.name)
            tracks[suffix] = []
            counts[suffix] = 0
            continue
        cues = parse_srt(path)
        tracks[suffix] = cues
        counts[suffix] = len(cues)
        if [c['number'] for c in cues] != list(range(1, len(cues) + 1)):
            problems.append('%s numbering' % path.name)
        for i, cue in enumerate(cues):
            if cue['end_ms'] <= cue['start_ms']:
                problems.append('%s cue %d empty' % (path.name, cue['number']))
            if i and cues[i - 1]['end_ms'] > cue['start_ms']:
                problems.append('%s overlap at cue %d' % (path.name, cue['number']))
        # Track 01 repeats both English passes and stops when the Chinese
        # reading begins; the other four run to the end of the word.
        ending = frame_to_ms(words[-1]['chinese_frame'] if suffix == SUFFIXES[0]
                             else manifest['total_frames'], fps)
        if cues and cues[-1]['end_ms'] != ending:
            problems.append('%s ends at %d, expected %d'
                            % (path.name, cues[-1]['end_ms'], ending))
    if counts[SUFFIXES[0]] != 2 * len(words):
        problems.append('track 01 has %d cues for %d words' % (counts[SUFFIXES[0]], len(words)))
    for suffix in SUFFIXES[1:]:
        if counts[suffix] != len(words):
            problems.append('%s has %d cues for %d words' % (suffix, counts[suffix], len(words)))
    # The text of every cue is compared with the read-only word list, and its
    # timing with the timeline.  A coordinated mistake now fails here.
    for position, word in enumerate(words):
        want = expectations[position]
        expect = {
            SUFFIXES[0]: [(word['start_frame'], word['male_frame'], want['word']),
                          (word['male_frame'], word['chinese_frame'], want['word'])],
            SUFFIXES[1]: [(word['start_frame'], word['end_frame'], want['word'])],
            SUFFIXES[2]: [(word['male_frame'], word['end_frame'], want['phonetic'])],
            SUFFIXES[3]: [(word['chinese_frame'], word['end_frame'], want['meaning'])],
            SUFFIXES[4]: [(word['chinese_frame'], word['end_frame'], want['spoken_meaning'])],
        }
        for suffix, wanted in expect.items():
            stride = len(wanted)
            actual = tracks[suffix][position * stride:position * stride + stride]
            got = [(c['start_ms'], c['end_ms'], c['text']) for c in actual]
            want_rows = [(frame_to_ms(a, fps), frame_to_ms(b, fps), text)
                         for a, b, text in wanted]
            if got != want_rows:
                problems.append('%s word %d mismatch: got %r want %r'
                                % (suffix, word['index'], got[:1], want_rows[:1]))
        meaning = tracks[SUFFIXES[3]][position]['text'] if counts[SUFFIXES[3]] > position else ''
        spoken = tracks[SUFFIXES[4]][position]['text'] if counts[SUFFIXES[4]] > position else ''
        if POS.search(spoken):
            problems.append('word %d spoken text still carries a part of speech: %r'
                            % (word['index'], spoken))
        if cjk(spoken) != cjk(meaning):
            problems.append('word %d spoken %r says something else than %r'
                            % (word['index'], spoken, meaning))
        if meaning != want['meaning']:
            problems.append('word %d display meaning %r != word list %r'
                            % (word['index'], meaning, want['meaning']))
        if spoken != want['spoken_meaning']:
            problems.append('word %d spoken %r != derived from word list %r (policy %s)'
                            % (word['index'], spoken, want['spoken_meaning'], cleaning))
    report['srt'] = {'cues': counts, 'words': len(words), 'problems': problems[:20],
                     'problem_count': len(problems)}
    return problems


def materials_of(draft):
    out = {}
    for kind in ('videos', 'audios'):
        for item in draft['materials'].get(kind, []):
            out[item['id']] = item.get('path')
    return out


def text_of(draft, material_id):
    for material in draft['materials'].get('texts', []):
        if material['id'] == material_id:
            try:
                return json.loads(material['content']).get('text', '')
            except Exception:
                return '<unparseable>'
    return '<no material>'


def check_draft(batch, manifest, report, expectations):
    folder = batch / 'editable-draft'
    raw_path = folder / 'draft_content.json'
    problems = []
    if not raw_path.exists():
        report['draft'] = {'problems': ['draft_content.json missing'], 'problem_count': 1}
        return ['draft_content.json missing']
    raw = raw_path.read_bytes()
    try:
        draft = json.loads(raw.decode('utf-8'))
    except Exception as error:  # encrypted or corrupt
        report['draft'] = {'problems': ['draft_content.json unreadable: %s' % error],
                           'problem_count': 1}
        return ['draft_content.json unreadable: %s' % error]
    fps = manifest['fps']
    if list(draft['canvas_config'][k] for k in ('width', 'height')) != \
            [manifest['width'], manifest['height']]:
        problems.append('draft canvas != timeline canvas')
    materials = materials_of(draft)
    inventory, missing_tracks = {}, []
    # Whether the intro carries sound is decided by the media, not assumed.
    intro_video = manifest.get('intro_video') or ''
    intro_has_audio = bool(intro_video) and Path(intro_video).exists() \
        and has_audio_stream(intro_video)
    want = {('video', '背景'): 1,
            ('audio', 'female'): len(manifest['words']),
            ('audio', 'male'): len(manifest['words']),
            ('audio', 'chinese'): len(manifest['words']),
            ('text', 'english'): len(manifest['words']),
            ('text', 'phonetic'): len(manifest['words']),
            ('text', 'meaning'): len(manifest['words']),
            ('text', 'title'): 1, ('text', 'subtitle'): 1, ('text', 'footer'): 1}
    if intro_video:
        want[('video', '片头')] = 1
        want[('audio', '片头音效')] = 1 if intro_has_audio else 0
    by_key = {}
    for track in draft['tracks']:
        key = (track['type'], track.get('name'))
        by_key[key] = track
        inventory['%s/%s' % key] = len(track.get('segments', []))
        segments = sorted(track.get('segments', []), key=lambda s: s['target_timerange']['start'])
        for i, segment in enumerate(segments):
            if i and (segments[i - 1]['target_timerange']['start']
                      + segments[i - 1]['target_timerange']['duration']
                      > segment['target_timerange']['start']):
                problems.append('draft %s/%s overlaps at segment %d' % (*key, i))
            path = materials.get(segment['material_id'])
            if path and not Path(path).exists():
                problems.append('draft %s/%s references a missing file' % key)
        if track['type'] == 'text':
            for segment in segments:
                content = json.dumps(segment.get('content'), ensure_ascii=False)
                if re.search(r'>\s*\d\s*<', content):
                    problems.append('draft draws a bare countdown digit on %s' % key[1])
    for key, count in want.items():
        if key not in by_key:
            missing_tracks.append('%s/%s' % key)
            problems.append('draft is missing the required track %s/%s' % key)
        elif len(by_key[key].get('segments', [])) != count:
            problems.append('draft %s/%s has %d segments, expected %d'
                            % (*key, len(by_key[key].get('segments', [])), count))
    bg = by_key.get(('video', '背景'))
    intro_start = bg_start = None
    if bg:
        spans = sorted((s['target_timerange']['start'], s['target_timerange']['duration'])
                       for s in bg['segments'])
        bg_start = spans[0][0]
        # The background must run from frame 0 to the end without a hole: that is
        # what the published MP4 draws, so the draft has to agree frame for frame.
        if bg_start != 0:
            problems.append('draft background starts at %.3fs, not 0' % (bg_start / 1e6))
        cursor = 0
        for start, length in spans:
            if start > cursor:
                problems.append('draft background has a gap at %.3fs' % (start / 1e6))
            cursor = max(cursor, start + length)
        if abs(cursor - manifest['total_frames'] * 1e6 / fps) > 20000:
            problems.append('draft background stops at %.3fs, timeline ends at %.3fs'
                            % (cursor / 1e6, manifest['total_frames'] / fps))
    intro_track = by_key.get(('video', '片头'))
    if intro_track and intro_track.get('segments'):
        intro_start = intro_track['segments'][0]['target_timerange']['start']
        if intro_start != 0:
            problems.append('draft intro clip does not start at 0')
    if abs(draft.get('duration', 0) - manifest['total_frames'] * 1e6 / fps) > 20000:
        problems.append('draft duration %s != timeline' % draft.get('duration'))
    # Every word, every text track: content against the word list, timing against
    # the timeline.  The first version only looked at word 1's English start.
    # The draft stores integer microseconds; start and end are each rounded, so a
    # duration may differ from a freshly rounded difference by 1 us.  The tolerance
    # is that rounding and nothing more: one frame at 60 fps is 16667 us.
    def us(frame):
        return round(frame * 1e6 / fps)

    US_TOLERANCE = 2
    stages = {(a['word_index'], a['role']): a for a in manifest['audio']}
    checked_words = 0
    for position, word in enumerate(manifest['words']):
        want_word = expectations[position]
        want_rows = {'english': (word['start_frame'], word['end_frame'], want_word['word']),
                     'phonetic': (word['male_frame'], word['end_frame'], want_word['phonetic']),
                     'meaning': (word['chinese_frame'], word['end_frame'], want_word['meaning'])}
        for name, (start, end, text) in want_rows.items():
            track = by_key.get(('text', name))
            if track is None or len(track['segments']) <= position:
                continue
            segments = sorted(track['segments'], key=lambda s: s['target_timerange']['start'])
            segment = segments[position]
            span = segment['target_timerange']
            if abs(span['start'] - us(start)) > US_TOLERANCE:
                problems.append('draft %s word %d starts at %d, timeline %d'
                                % (name, word['index'], span['start'], us(start)))
            if abs(span['duration'] - (us(end) - us(start))) > US_TOLERANCE:
                problems.append('draft %s word %d lasts %d, timeline %d'
                                % (name, word['index'], span['duration'],
                                   us(end) - us(start)))
            actual = text_of(draft, segment['material_id'])
            if actual != text:
                problems.append('draft %s word %d says %r, word list says %r'
                                % (name, word['index'], actual, text))
        for role in ('female', 'male', 'chinese'):
            track = by_key.get(('audio', role))
            stage = stages.get((word['index'], role))
            if track is None or len(track['segments']) <= position or stage is None:
                continue
            segments = sorted(track['segments'], key=lambda s: s['target_timerange']['start'])
            span = segments[position]['target_timerange']
            if abs(span['start'] - us(stage['start_frame'])) > US_TOLERANCE:
                problems.append('draft %s word %d starts at %d, timeline %d'
                                % (role, word['index'], span['start'],
                                   us(stage['start_frame'])))
            if abs(span['duration'] - us(stage['duration_frames'])) > US_TOLERANCE:
                problems.append('draft %s word %d lasts %d, timeline %d'
                                % (role, word['index'], span['duration'],
                                   us(stage['duration_frames'])))
        checked_words += 1
    report['draft'] = {'encrypted': False, 'canvas': draft['canvas_config'],
                       'duration_s': round(draft.get('duration', 0) / 1e6, 3),
                       'tracks': inventory, 'missing_tracks': missing_tracks,
                       'intro_has_audio': intro_has_audio,
                       'intro_start_s': intro_start and intro_start / 1e6,
                       'background_start_s': bg_start and bg_start / 1e6,
                       'materials': len(materials), 'words_checked': checked_words,
                       'problems': problems[:20], 'problem_count': len(problems)}
    return problems


# Pacing constants of the published contract (word_video/timing.py): the English
# floor, the Chinese floor and the first-six/remainder formula, plus the gap added
# to the unscaled recording before the speed change.  Kept here on purpose: the
# validator recomputes the time axis from the read-only word list and the real
# media instead of believing the timeline it is checking.
ENGLISH_FLOOR = 1.0
CHINESE_FLOOR = 0.5
FIRST_SIX = 0.4
EXTRA = 0.2
GAP_S = 0.1
HANZI_START, HANZI_END = '\u4e00', '\u9fff'
SPEED_TOLERANCE_FRAMES = 1


def chinese_floor(spoken, word):
    count = sum(1 for char in spoken if HANZI_START <= char <= HANZI_END)
    if count == 0:
        count = max(len(word) // 4, 1)
    head = min(count, 6)
    return max(CHINESE_FLOOR, head * FIRST_SIX + (count - head) * EXTRA)


def check_timing(manifest, report, expectations):
    """Stage lengths and identities against the read-only word list and the media.

    ``raw`` in the cache record is the *unscaled* recording length, so the whole
    reserved duration can be recomputed: a timeline that claims a stage the real
    recording does not fit into is caught even when every published file agrees
    with the timeline.
    """
    problems, rows, missing = [], [], 0
    fps, speed = manifest['fps'], manifest['speed']
    by_index = {word['index']: word for word in expectations}
    for stage in manifest['audio']:
        path = Path(stage['path'])
        row = {'word_index': stage['word_index'], 'role': stage['role']}
        want = by_index.get(stage['word_index'])
        if want is None:
            problems.append('word %d has audio but is outside the requested range'
                            % stage['word_index'])
            continue
        want_text = want['word'] if stage['role'] in ('female', 'male') \
            else want['spoken_meaning']
        if stage.get('text') != want_text:
            problems.append('word %d %s: the timeline synthesised %r, the word list says %r'
                            % (stage['word_index'], stage['role'], stage.get('text'),
                               want_text))
        if not path.exists():
            problems.append('word %d %s audio file missing: %s'
                            % (stage['word_index'], stage['role'], path))
            continue
        media = media_seconds(path)
        record_path = path.parent / 'complete.json'
        if not record_path.exists():
            missing += 1
            row.update({'cache': 'DATA_MISSING', 'media_seconds': round(media, 4)})
            rows.append(row)
            continue
        record = json.loads(record_path.read_text(encoding='utf-8'))
        spec = record.get('spec', {})
        if spec.get('role') != stage['role']:
            problems.append('word %d: cache role %r != timeline role %r'
                            % (stage['word_index'], spec.get('role'), stage['role']))
        if spec.get('text') != want_text:
            problems.append('word %d %s: the cached speech is %r, the word list says %r'
                            % (stage['word_index'], stage['role'], spec.get('text'),
                               want_text))
        if stage.get('voice') and spec.get('voice') and stage['voice'] != spec['voice']:
            problems.append('word %d %s: cache voice %r != timeline voice %r'
                            % (stage['word_index'], stage['role'], spec['voice'],
                               stage['voice']))
        if spec.get('speed') not in (None, speed):
            problems.append('word %d %s: cached at speed %s, timeline says %s'
                            % (stage['word_index'], stage['role'], spec.get('speed'), speed))
        if spec.get('fps') not in (None, fps):
            problems.append('word %d %s: cached at %s fps, timeline says %s'
                            % (stage['word_index'], stage['role'], spec.get('fps'), fps))
        rendered = record.get('rendered')
        if rendered is not None and abs(rendered - media) > MEDIA_TOLERANCE_FRAMES / fps:
            problems.append('word %d %s: cache says the prepared audio is %.4fs, the file is '
                            '%.4fs' % (stage['word_index'], stage['role'], rendered, media))
        digest = record.get('audio_digest')
        if digest and sha256(path) != digest:
            problems.append('word %d %s: the prepared audio is not the file the cache '
                            'committed' % (stage['word_index'], stage['role']))
        raw = record.get('raw')
        if raw is None:
            missing += 1
            row.update({'cache': 'DATA_MISSING(raw)', 'media_seconds': round(media, 4)})
            rows.append(row)
            continue
        floor = ENGLISH_FLOOR if stage['role'] in ('female', 'male') \
            else chinese_floor(want['spoken_meaning'], want['word'])
        minimum = max(floor, raw + GAP_S)
        expected = max(math.ceil(minimum / speed * fps), math.ceil(media * fps), 1)
        row.update({'raw': raw, 'rendered': rendered,
                    'media_seconds': round(media, 4),
                    'duration_frames': stage['duration_frames'],
                    'expected_frames': expected})
        if abs(expected - stage['duration_frames']) > SPEED_TOLERANCE_FRAMES:
            problems.append('word %d %s: the timeline reserves %d frames (%.3fs) but the '
                            'recording needs %d frames (%.3fs raw, %.3fs prepared)'
                            % (stage['word_index'], stage['role'],
                               stage['duration_frames'],
                               stage['duration_frames'] / fps, expected,
                               raw, media))
        rows.append(row)
    report['timing'] = {'stages': len(rows), 'data_missing': missing,
                        'frame_tolerance': SPEED_TOLERANCE_FRAMES,
                        'pacing_constants': {'english_floor': ENGLISH_FLOOR,
                                             'chinese_floor': CHINESE_FLOOR,
                                             'first_six': FIRST_SIX, 'extra': EXTRA,
                                             'gap_s': GAP_S},
                        'rows': rows[:200], 'problems': problems[:20],
                        'problem_count': len(problems)}
    return problems


def envelope(pcm, rate, window_ms=20):
    """Peak per window of s16le mono bytes."""
    step = rate * window_ms // 1000
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    return [max((abs(x) for x in samples[i:i + step]), default=0)
            for i in range(0, len(samples) - step + 1, step)]


def pcm_of(source, rate=8000, extra=()):
    args = [FFMPEG, '-v', 'error', '-nostdin', *extra, '-i', str(source),
            '-vn', '-ac', '1', '-ar', str(rate), '-f', 's16le', '-']
    return subprocess.run(args, capture_output=True, check=True).stdout


def pearson(a, b):
    n = min(len(a), len(b))
    if not n:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    if not va or not vb:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb)


def check_audio(batch, manifest, report):
    problems = []
    mp4_path = batch / 'video/video.mp4'
    if not mp4_path.exists():
        report['audio_e2e'] = {'problems': ['video/video.mp4 missing'], 'problem_count': 1}
        return ['video/video.mp4 missing']
    mp4 = pcm_of(mp4_path)
    mix_path = batch / 'video/mix.wav'
    if not mix_path.exists():
        report['audio_e2e'] = {'problems': ['video/mix.wav missing'], 'problem_count': 1}
        return ['video/mix.wav missing']
    mix = pcm_of(mix_path)
    env_mp4 = envelope(mp4, 8000)
    env_mix = envelope(mix, 8000)
    correlation = pearson(env_mp4, env_mix)
    if abs(len(mp4) - len(mix)) > 8000:
        problems.append('MP4 audio %d bytes vs mix.wav %d' % (len(mp4), len(mix)))
    if correlation < 0.97:
        problems.append('MP4 audio envelope differs from mix.wav (r=%.3f)' % correlation)
    # AAC tail: the published audio must last as long as the mix, within a stated
    # tolerance, so a truncated tail cannot hide behind a shorter total.
    tail_gap = len(mix) / 2 / 8000 - len(mp4) / 2 / 8000
    if tail_gap > TAIL_TOLERANCE_SECONDS:
        problems.append('MP4 audio ends %.3fs before mix.wav (tolerance %.2fs)'
                        % (tail_gap, TAIL_TOLERANCE_SECONDS))
    window_ms = 20
    per_window = window_ms / 1000.0

    def loud_windows(start_frame, duration_frames):
        """Loud windows inside one stage, ignoring its silent head and tail.

        A synthesised clip starts and ends with a little silence, so sampling
        the first window would report a perfectly audible stage as mute.
        """
        first = int((start_frame / manifest['fps'] + 3 * per_window) / per_window)
        last = int((start_frame + duration_frames) / manifest['fps'] / per_window)
        return sum(1 for value in env_mp4[first:last] if value > 200)

    # A lesson without an intro must not be asked to prove the intro is audible.
    intro_video = manifest.get('intro_video') or ''
    intro_seconds = manifest.get('intro_frames', 0) / manifest['fps']
    intro_audible = None
    if intro_seconds > 0:
        window = env_mp4[:int(intro_seconds * 1000 / window_ms)]
        intro_audible = any(x > 200 for x in window)
        if intro_video and Path(intro_video).exists() and has_audio_stream(intro_video):
            if not intro_audible:
                problems.append('intro region of the MP4 has no audible samples')
        elif not intro_audible:
            # The intro media has no sound: silence here is correct, not a fault.
            intro_audible = False
    stages = {(a['word_index'], a['role']): a for a in manifest['audio']}
    silent, missing_stage = [], []
    for word in manifest['words']:
        stage = stages.get((word['index'], 'chinese'))
        if stage is None:
            missing_stage.append(word['index'])
        elif not loud_windows(stage['start_frame'], stage['duration_frames']):
            silent.append(word['index'])
    if missing_stage:
        problems.append('%d words have no Chinese audio stage in the timeline (e.g. %s)'
                        % (len(missing_stage), missing_stage[:5]))
    if silent:
        problems.append('%d words have no audible Chinese reading (e.g. %s)'
                        % (len(silent), silent[:5]))
    quiet = [w['index'] for w in manifest['words']
             if all((w['index'], role) in stages for role in ('female', 'male'))
             and any(not loud_windows(stages[(w['index'], role)]['start_frame'],
                                      stages[(w['index'], role)]['duration_frames'])
                     for role in ('female', 'male'))]
    if quiet:
        problems.append('%d words have an inaudible English pass (e.g. %s)'
                        % (len(quiet), quiet[:5]))
    early = [a['word_index'] for a in manifest['audio']
             if a['start_frame'] < manifest.get('intro_frames', 0)]
    if early:
        problems.append('word audio starts before the intro ends: %s' % early[:5])
    report['audio_e2e'] = {'mp4_seconds': round(len(mp4) / 2 / 8000, 3),
                           'mix_seconds': round(len(mix) / 2 / 8000, 3),
                           'envelope_r': round(correlation, 4),
                           'tail_gap_s': round(tail_gap, 3),
                           'tail_tolerance_s': TAIL_TOLERANCE_SECONDS,
                           'intro_seconds': round(intro_seconds, 3),
                           'intro_audible': intro_audible,
                           'audible_windows': sum(1 for x in env_mp4 if x > 200),
                           'silent_chinese_words': silent,
                           'quiet_english_words': quiet,
                           'problems': problems, 'problem_count': len(problems)}
    return problems


def gray_frame(source, seconds, width=320, height=180, extra=()):
    args = [FFMPEG, '-v', 'error', '-nostdin', *extra, '-ss', '%.6f' % seconds,
            '-i', str(source), '-frames:v', '1', '-vf',
            'scale=%d:%d,format=gray' % (width, height), '-f', 'rawvideo', '-']
    out = subprocess.run(args, capture_output=True, check=True).stdout
    return out[:width * height]


def band_ink(frames, band, width=320, height=180, half_height=None):
    """Fraction of the band's pixels where the published frame differs from the
    bare background frame.  Non-zero means something was painted there."""
    fg, bg = frames
    if len(fg) != len(bg) or not fg:
        raise RuntimeError('frame decode failed')
    half = BAND_HALF_HEIGHT if half_height is None else half_height
    y0 = max(0, int((BANDS[band] - half) * height))
    y1 = min(height, int((BANDS[band] + half) * height))
    x0 = max(0, int((0.5 - BAND_HALF_WIDTH) * width))
    x1 = min(width, int((0.5 + BAND_HALF_WIDTH) * width))
    changed = total = 0
    for y in range(y0, y1):
        base = y * width
        for x in range(x0, x1):
            total += 1
            if abs(fg[base + x] - bg[base + x]) > 24:
                changed += 1
    return changed / max(1, total)


def check_pixels(batch, manifest, report, expectations, mode):
    video = batch / 'video/video.mp4'
    background = manifest['background']
    problems, rows = [], []
    words = manifest['words']
    fps = manifest['fps']
    background_path = Path(background)
    if not background_path.exists():
        report['pixels'] = {'problems': ['background missing: %s' % background],
                            'problem_count': 1, 'probes': []}
        return ['background missing: %s' % background]

    def frames_at(seconds):
        return (gray_frame(video, seconds), gray_frame(background_path, seconds))

    def phase(index, key_a, key_b):
        word = words[index]
        return (word[key_a] + word[key_b]) / 2 / fps

    probes = []
    if mode != 'none':
        # Global text spans the whole lesson, so it is probed once at the start
        # and once at a late phase.
        probes.append(('intro/title', 0.10,
                       {'title': True, 'subtitle': True, 'footer': True}))
    if mode == 'first':
        picks = [0]
    else:
        picks = list(range(len(words)))
    for position in picks:
        word = words[position]
        want = expectations[position]
        probes.append(('word %d first pass' % word['index'],
                       phase(position, 'start_frame', 'male_frame'),
                       {'english': True, 'phonetic': False, 'meaning': False,
                        'expected_text': want['word']}))
        probes.append(('word %d second pass' % word['index'],
                       phase(position, 'male_frame', 'chinese_frame'),
                       {'english': True, 'phonetic': True, 'meaning': False,
                        'expected_text': want['word'] + ' ' + want['phonetic']}))
        probes.append(('word %d Chinese' % word['index'],
                       phase(position, 'chinese_frame', 'end_frame'),
                       {'english': True, 'phonetic': True, 'meaning': True,
                        'expected_text': want['meaning']}))
    cache = {}
    empty_readings, painted_readings = [], []
    for label, seconds, wants in probes:
        row = {'probe': label, 'at_s': round(seconds, 3), 'ink': {}}
        if 'expected_text' in wants:
            row['expected_text'] = wants['expected_text']
        key = round(seconds, 3)
        if key not in cache:
            cache[key] = frames_at(seconds)
        for band, expected in wants.items():
            if band == 'expected_text':
                continue
            half = None if expected else BAND_HALF_HEIGHT_TIGHT
            fraction = band_ink(cache[key], band, half_height=half)
            row['ink'][band] = round(fraction, 4)
            if expected:
                painted_readings.append(fraction)
                if fraction < INK_THRESHOLD:
                    problems.append('%s: nothing painted in the %s band (%.4f)'
                                    % (label, band, fraction))
            else:
                empty_readings.append(fraction)
                if fraction >= EARLY_INK_THRESHOLD:
                    problems.append('%s: %s band painted too early (%.4f > %.4f)'
                                    % (label, band, fraction, EARLY_INK_THRESHOLD))
        rows.append(row)
    report['pixels'] = {'threshold': INK_THRESHOLD,
                        'early_threshold': EARLY_INK_THRESHOLD,
                        'measured_empty_max': round(max(empty_readings), 4)
                        if empty_readings else None,
                        'measured_painted_min': round(min(painted_readings), 4)
                        if painted_readings else None,
                        'mode': mode,
                        'probes': rows if mode == 'none' else rows[:400],
                        'problem_count': len(problems), 'problems': problems[:20],
                        'method': 'frame minus bare background'}
    return problems


def write_report(report, target):
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if target:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    print(text)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('batch')
    parser.add_argument('--source', help='只读原始词表（TXT）')
    parser.add_argument('--range', dest='range_', help='形如 151-200')
    parser.add_argument('--cleaning', choices=['legacy', 'v2'], default='legacy',
                        help='朗读文本清洗政策；已交付批次用 legacy')
    parser.add_argument('--spoken-file',
                        help='{index: spoken_meaning} JSON，用外部推导的朗读文本'
                             '替换本工具自己的规则（用于已批准政策变更的独立判定）')
    parser.add_argument('--pixels', choices=['all', 'first', 'none'], default='all')
    parser.add_argument('--json')
    args = parser.parse_args(argv)
    batch = Path(args.batch).resolve(strict=True)
    report, failures = {'batch': str(batch)}, {}

    if not (args.source and args.range_):
        report['expectations'] = {'provided': False}
        report['verdict'] = 'REFUSED'
        report['failures'] = {'expectations': [
            '缺少独立期望：必须给 --source <只读词表> --range <起-止>；'
            '只从 timeline 自证的验收不再判 PASS']}
        write_report(report, args.json)
        return 2
    first, last = (int(part) for part in args.range_.split('-'))
    expectations = load_expectations(args.source, first, last, args.cleaning,
                                     args.spoken_file)
    report['expectations'] = {'provided': True, 'source': expectations['source'],
                              'source_sha256': expectations['source_sha256'],
                              'cleaning': args.cleaning, 'first': first, 'last': last,
                              'spoken_file': expectations['spoken_file'],
                              'words': len(expectations['words']),
                              'sample': expectations['words'][:2]}

    manifest_path = batch / 'timeline.json'
    manifest, broken = None, None
    if not manifest_path.exists():
        broken = 'timeline.json missing'
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if not isinstance(manifest, dict) or not isinstance(manifest.get('words'), list):
                broken = 'timeline.json is not a timeline manifest'
        except Exception as error:
            broken = 'timeline.json unusable: %r' % error
    if broken:
        # A batch that cannot even be read must produce a report saying so; a
        # crashed validator with no report is what let an old report speak before.
        report['verdict'] = 'FAIL'
        report['failures'] = {'timeline': [broken]}
        write_report(report, args.json)
        return 1
    words = manifest.get('words') or []
    expected_words = expectations['words']
    if len(words) != len(expected_words) or \
            [w['index'] for w in words] != [w['index'] for w in expected_words]:
        failures['range'] = ['timeline covers %s, the requested range is %d-%d (%d words)'
                             % ([w.get('index') for w in words[:1]] and
                                '%d-%d' % (words[0]['index'], words[-1]['index']) or 'nothing',
                                first, last, len(expected_words))]
    else:
        for word, want in zip(words, expected_words):
            actual = (word.get('word'), word.get('phonetic'), word.get('meaning'),
                      word.get('spoken_meaning'))
            wanted = (want['word'], want['phonetic'], want['meaning'],
                      want['spoken_meaning'])
            if actual != wanted:
                fields = ('word', 'phonetic', 'meaning', 'spoken_meaning')
                broken = ['%s: timeline %r != word list %r' % (name, a, b)
                          for name, a, b in zip(fields, actual, wanted) if a != b]
                failures.setdefault('wordlist', []).append(
                    'word %d differs from the read-only word list - %s'
                    % (word['index'], '; '.join(broken)))
    for name, function in (('integrity', check_integrity), ('video', check_video),
                           ('mix', check_mix), ('srt', check_srt), ('draft', check_draft),
                           ('timing', check_timing), ('audio_e2e', check_audio),
                           ('pixels', check_pixels)):
        if name in failures:
            continue
        try:
            if name == 'integrity':
                problems = function(batch, report)
            elif name == 'srt':
                problems = function(batch, manifest, report, expected_words, args.cleaning)
            elif name in ('draft', 'timing'):
                problems = function(batch, manifest, report, expected_words) \
                    if name != 'timing' else function(manifest, report, expected_words)
            elif name == 'pixels':
                problems = function(batch, manifest, report, expected_words, args.pixels)
            else:
                problems = function(batch, manifest, report)
        except Exception as error:
            problems = ['%s check crashed: %r' % (name, error)]
            report.setdefault(name, {})['crash'] = repr(error)
        if problems:
            failures[name] = problems
    report['failures'] = {k: v[:20] for k, v in failures.items()}
    report['verdict'] = 'PASS' if not failures else 'FAIL'
    write_report(report, args.json)
    return 0 if not failures else 1


if __name__ == '__main__':
    raise SystemExit(main())
