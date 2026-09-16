"""Assemble a background that the plan describes as several segments.

A background layer can be cut in two (`wv-project@3`), and each piece keeps its own
source window.  Two things must stay true while that happens:

* **each segment still loops inside itself.**  The verified engine has always
  looped a background that is shorter than the lesson, and a split must not turn
  "loop the background" into "play it once" - so the loop is decided per *item*,
  over the item's own window and duration, not over the concatenation;
* **there are no holes and no overlaps.**  A gap would show the encoder whatever
  is behind the background (nothing), and an overlap would need a blend rule the
  project never asked for.  Both are refused with the item they are about rather
  than rendered as-is.

The assembled file is written once per export into the run folder, so the renderer
and the draft exporter read one file and the segments stay the plan's, not a second
timeline.
"""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import uuid

from ..domain.model import TICKS_PER_SECOND


class BackgroundError(Exception):
    """The background cannot be assembled without inventing something."""


@dataclass(frozen=True)
class BackgroundSegment:
    """One planned background item: its file, its source window and its stage."""

    clip_id: str
    path: str
    source_start: int
    source_end: int
    unit_num: int
    unit_den: int
    speed: float
    start_ticks: int
    end_ticks: int
    loop_seconds: float     # how long this item's content is, speed applied once
    stage_seconds: float    # how long the timeline gives it

    @property
    def loops(self):
        return self.loop_seconds + 1e-6 < self.stage_seconds

    @property
    def repeats(self):
        """How many times the item's own content has to play to fill its stage."""
        if self.loop_seconds <= 0:
            raise BackgroundError('%s has no usable content length' % self.clip_id)
        return max(1, int(-(-self.stage_seconds // self.loop_seconds)))

    def to_dict(self):
        return {'clip_id': self.clip_id, 'path': self.path,
                'source_start': self.source_start, 'source_end': self.source_end,
                'unit_num': self.unit_num, 'unit_den': self.unit_den,
                'speed': self.speed, 'start_ticks': self.start_ticks,
                'end_ticks': self.end_ticks,
                'loop_seconds': round(self.loop_seconds, 6),
                'stage_seconds': round(self.stage_seconds, 6),
                'repeats': self.repeats}


def _seconds(units, unit_num, unit_den, speed):
    return float(Fraction(units * unit_num, unit_den) / Fraction(str(speed)))


def background_segments(plan, sources):
    """Every ``background`` item of the plan, in planned order.

    ``sources`` maps ``asset_id`` to a :class:`SourceMedia`; the exporter has
    already prepared each item's window, so a segment carries the file to loop.
    """
    items = [item for item in plan.video if item.role == 'background']
    segments = []
    for item in items:
        if item.source is None:
            raise BackgroundError('background item %s has no media' % item.clip_id)
        source = sources.get(item.source.asset_id)
        if source is None:
            raise BackgroundError('no prepared media for background item %s (asset %r)'
                                  % (item.clip_id, item.source.asset_id))
        slice_ = item.source
        segments.append(BackgroundSegment(
            clip_id=item.clip_id, path=str(source.path),
            source_start=slice_.source_start, source_end=slice_.source_end,
            unit_num=slice_.unit_num, unit_den=slice_.unit_den, speed=slice_.speed,
            start_ticks=item.start_ticks, end_ticks=item.end_ticks,
            loop_seconds=_seconds(slice_.source_units, slice_.unit_num,
                                  slice_.unit_den, slice_.speed),
            stage_seconds=float(Fraction(item.duration_ticks, TICKS_PER_SECOND))))
    _require_contiguous(segments)
    return tuple(segments)


def _require_contiguous(segments):
    """No hole and no overlap between consecutive background items."""
    for left, right in zip(segments, segments[1:]):
        if right.start_ticks < left.end_ticks:
            raise BackgroundError(
                'background items %s and %s overlap by %d ticks; the project has to '
                'move or trim one of them'
                % (left.clip_id, right.clip_id, left.end_ticks - right.start_ticks))
        if right.start_ticks > left.end_ticks:
            raise BackgroundError(
                'background items %s and %s leave a %d tick hole; the keep the picture '
                'continuous, extend one of them'
                % (left.clip_id, right.clip_id, right.start_ticks - left.end_ticks))


def build_background_track(segments, target, *, fps, size, timeout=1800):
    """Loop each segment inside its own stage and join them into one file.

    One ffmpeg pass per segment, then one concat: the loop is per item, which is
    the behaviour the verified engine gives a single background, applied piece by
    piece.  ``size`` is ``(width, height)``; every segment is scaled and cropped to
    it so the joined stream has one geometry.
    """
    from ..media import executable, run

    segments = tuple(segments)
    if not segments:
        raise BackgroundError('no background segments to assemble')
    target = Path(target)
    work = target.parent
    work.mkdir(parents=True, exist_ok=True)
    pieces = []
    try:
        for index, segment in enumerate(segments):
            piece = work / ('.bg-%d-%s.mp4' % (index, uuid.uuid4().hex[:8]))
            pieces.append(piece)
            frames = max(1, int(round(segment.stage_seconds * fps)))
            args = [executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
                    '-stream_loop', '-1']
            if segment.source_start:
                args += ['-ss', '%.9f' % (segment.source_start * segment.unit_num
                                          / float(segment.unit_den))]
            args += ['-i', segment.path, '-t', '%.9f' % segment.stage_seconds]
            filters = []
            if segment.speed != 1.0:
                filters.append('setpts=PTS/%.9f' % float(segment.speed))
            filters.append('scale=%d:%d:force_original_aspect_ratio=increase' % size)
            filters.append('crop=%d:%d' % size)
            filters.append('fps=%d' % fps)
            filters.append('setsar=1')
            args += ['-vf', ','.join(filters), '-an', '-frames:v', str(frames),
                     '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                     '-pix_fmt', 'yuv420p', str(piece)]
            run(args, timeout=timeout)
        if len(pieces) == 1:
            pieces[0].replace(target)
            pieces = []
        else:
            listing = work / ('.bg-list-%s.txt' % uuid.uuid4().hex[:8])
            listing.write_text(''.join("file '%s'\n" % piece.name for piece in pieces),
                               encoding='utf-8')
            try:
                run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f',
                     'concat', '-safe', '0', '-i', listing, '-c', 'copy', str(target)],
                    timeout=timeout)
            finally:
                listing.unlink(missing_ok=True)
        return target
    finally:
        for piece in pieces:
            piece.unlink(missing_ok=True)
