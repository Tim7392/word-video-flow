"""Export the five deterministic SRT tracks for one timeline manifest.

Tracks (stable suffixes, UTF-8 with BOM):
    01 英文重复  female->male and male->chinese, English word repeated
    02 英文单次  female->end, English word once
    03 音标      male->end, phonetic
    04 中文带词性 chinese->end, meaning with part of speech
    05 中文无词性 chinese->end, clean spoken meaning

Cue numbers restart at 1 in every file and stay sequential.  Times are
frame-to-millisecond integer roundings of the manifest frames; the intro
offset is already baked into every ``start_frame`` and no countdown cue is
written here.  Track files are staged as hidden temporary files beside their
destinations and only published after every track is written, and existing
destinations are rejected rather than overwritten.
"""
import os
from pathlib import Path
import uuid

SRT_SUFFIXES = (
    '_01_英文重复.srt',
    '_02_英文单次.srt',
    '_03_音标.srt',
    '_04_中文带词性.srt',
    '_05_中文无词性.srt',
)


def _frame_to_ms(frame, fps):
    """Round frame -> milliseconds half up using integer arithmetic."""
    return (frame * 2000 + fps) // (2 * fps)


def _format_time(milliseconds):
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return '%02d:%02d:%02d,%03d' % (hours, minutes, seconds, millis)


def _cue(number, start_ms, end_ms, text):
    return '%d\n%s --> %s\n%s\n\n' % (
        number, _format_time(start_ms), _format_time(end_ms), text
    )


def _build_tracks(manifest):
    if not isinstance(manifest.fps, int) or isinstance(manifest.fps, bool) or manifest.fps <= 0:
        raise ValueError('manifest.fps must be a positive integer')
    fps = manifest.fps
    tracks = [[] for _ in SRT_SUFFIXES]
    numbers = [0] * len(SRT_SUFFIXES)
    for word in manifest.words:
        start = _frame_to_ms(word['start_frame'], fps)
        male = _frame_to_ms(word['male_frame'], fps)
        chinese = _frame_to_ms(word['chinese_frame'], fps)
        end = _frame_to_ms(word['end_frame'], fps)
        numbers[0] += 1
        tracks[0].append(_cue(numbers[0], start, male, word['word']))
        numbers[0] += 1
        tracks[0].append(_cue(numbers[0], male, chinese, word['word']))
        numbers[1] += 1
        tracks[1].append(_cue(numbers[1], start, end, word['word']))
        numbers[2] += 1
        tracks[2].append(_cue(numbers[2], male, end, word['phonetic']))
        numbers[3] += 1
        tracks[3].append(_cue(numbers[3], chinese, end, word['meaning']))
        numbers[4] += 1
        tracks[4].append(_cue(numbers[4], chinese, end, word['spoken_meaning']))
    return [''.join(track) for track in tracks]


def export_srts(manifest, output_dir):
    """Write five BOM SRT files and return their published paths."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    base = '%04d-%04d' % (manifest.first_index, manifest.last_index)
    final_paths = [target / (base + suffix) for suffix in SRT_SUFFIXES]
    for path in final_paths:
        if path.exists():
            raise FileExistsError(path)
    contents = _build_tracks(manifest)
    staged = []
    try:
        for path, content in zip(final_paths, contents):
            temporary = path.with_name(
                '.%s.%s.tmp' % (path.name, uuid.uuid4().hex))
            with open(temporary, 'w', encoding='utf-8-sig', newline='\n') as stream:
                stream.write(content)
            staged.append((temporary, path))
        # Best-effort recheck just before publishing; never overwrite.
        for _, path in staged:
            if path.exists():
                raise FileExistsError(path)
        published = []
        for temporary, path in staged:
            os.link(temporary, path)  # Fails atomically if a concurrent writer created it.
            published.append(str(path))
        return published
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
