"""The five teaching subtitle tracks, exported from a ``CuePlan``.

A cue plan is already exactly this file format: one entry per cue, per track, with
tick boundaries and the text of a semantic role.  So the export is the adapter,
not a second projection - ticks become milliseconds once, here, through
:func:`word_video.domain.ticks_to_milliseconds`, and the file layout (names, BOM,
cue numbering, blank line, CRLF-free) is the one the verified engine produced.
:func:`verify_against_manifest` pins that equality: for an equivalent lesson the
plan-driven files and the legacy files are byte-identical, which is what lets the
old chain stay reproducible while the new one is the source of truth.
"""
from pathlib import Path
import os
import uuid

from ..domain.plan import CUE_TRACKS
from ..domain.timebase import ticks_to_milliseconds

#: Stable suffixes of the five files, in track order.
SRT_SUFFIXES = (
    '_01_英文重复.srt',
    '_02_英文单次.srt',
    '_03_音标.srt',
    '_04_中文带词性.srt',
    '_05_中文无词性.srt',
)


def _format_time(milliseconds):
    hours, rest = divmod(int(milliseconds), 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return '%02d:%02d:%02d,%03d' % (hours, minutes, seconds, millis)


def build_tracks(cues):
    """``{track: body}`` for the five tracks; cue numbers restart in each file."""
    grouped = cues.by_track()
    bodies = {}
    for track in CUE_TRACKS:
        parts = []
        for number, cue in enumerate(grouped[track], start=1):
            parts.append('%d\n%s --> %s\n%s\n\n'
                         % (number, _format_time(ticks_to_milliseconds(cue.start_ticks)),
                            _format_time(ticks_to_milliseconds(cue.end_ticks)), cue.text))
        bodies[track] = ''.join(parts)
    return bodies


def export_srts(cues, output_dir, base_name):
    """Write the five files and return their published paths, in track order.

    Files are staged beside their destinations and published with a hard link, so
    a concurrent writer cannot be overwritten and a failure leaves no partial
    subtitle file behind.
    """
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    bodies = build_tracks(cues)
    final_paths = [target / (base_name + suffix) for suffix in SRT_SUFFIXES]
    for path in final_paths:
        if path.exists():
            raise FileExistsError(path)
    staged = []
    try:
        for path, track in zip(final_paths, CUE_TRACKS):
            temporary = path.with_name('.%s.%s.tmp' % (path.name, uuid.uuid4().hex))
            with open(temporary, 'w', encoding='utf-8-sig', newline='\n') as stream:
                stream.write(bodies[track])
            staged.append((temporary, path))
        published = []
        for temporary, path in staged:
            os.link(temporary, path)
            published.append(str(path))
        return published
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def verify_against_manifest(cues, manifest):
    """The plan-driven files must equal what the verified engine would write.

    Raises ``AssertionError`` with the first differing track, because the only
    acceptable outcome of two exporters agreeing is exact equality: a subtitle
    that differs by one millisecond is a different deliverable.
    """
    from ..srt_export import _build_tracks

    legacy = _build_tracks(manifest)
    ours = build_tracks(cues)
    for suffix, track, body in zip(SRT_SUFFIXES, CUE_TRACKS, legacy):
        if body != ours[track]:
            raise AssertionError(
                'track %s differs between the plan and the manifest\n'
                'plan    : %r\nmanifest: %r' % (track, ours[track][:400], body[:400]))
    return True
