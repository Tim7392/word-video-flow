"""Which file the preview actually decodes.

The preview must not decode a 1080p 347 MB background to show a 720p canvas: the
member is editing, and the cost of decoding is what makes an editor feel slow.
So a picture source taller than the preview canvas is replaced by a **proxy** -
and that word is doing real work here:

* a proxy is a *preview* artifact.  It never replaces an original, it is never an
  input to an export, and it is never named like a deliverable.  The delivered
  MP4 keeps its approved resolution ("成片规格不变").
* the proxy itself is not written here.  :func:`word_video.media.proxy.proxy_video`
  already does exactly this job - same frame rate, same duration, audio stream
  copied so the intro cannot start sounding different - and a second scaler in
  the preview package would be a second answer to "what does the proxy look
  like".
* proxies are **bounded on disk**.  A cache that only grows is how a preview
  feature ends up filling a member's D: drive, so the folder is pruned to a byte
  budget on open, oldest first.  Nothing here ever deletes a source file: the
  cache directory is ours and only files this module named are candidates.

The proxy's identity includes the source's size and modification time, so a
re-generated source produces a new proxy instead of silently reusing the old
one; the old file then ages out through the same budget.
"""
import os
import os
from dataclasses import dataclass
from pathlib import Path

from word_video.media.proxy import PROXY_PRESETS, proxy_video
from word_video.media.streams import video_stream

__all__ = ['PreviewSource', 'SOURCE_WINDOW_PREROLL', 'preview_proxy_height',
           'proxy_cache_dir', 'prune_proxies', 'resolve_preview_source',
           'source_seconds', 'windowed_copy']

#: Default disk budget for preview proxies.  2 GB holds several minutes of 720p
#: proxy at the measured crf and is small enough to be unremarkable on D:.
DEFAULT_PROXY_BUDGET = 2 * 1024 * 1024 * 1024
#: Extra seconds proxied past the end of the picture item, so a small seek
#: forward does not immediately need a second proxy.
SOURCE_WINDOW_PREROLL = 2.0
#: Only window a source when the lesson uses less than this fraction of it.
#: Above it the extra stream copy costs more than it saves.
SOURCE_WINDOW_FRACTION = 0.6


def preview_proxy_height(height):
    """The measured proxy preset a canvas of ``height`` should decode.

    The preview does not invent a crf or a scale of its own: it asks for one of
    the presets the media layer measured.  A canvas between two presets takes the
    larger, because a proxy that is taller than the canvas merely costs a little
    more to decode, while one that is shorter would have to be enlarged - paying
    more for a softer picture.
    """
    presets = sorted(PROXY_PRESETS)
    taller = [preset for preset in presets if preset >= int(height)]
    return min(taller) if taller else max(presets)


def source_seconds(path):
    """The file's duration, or ``0.0`` when it cannot be read."""
    from word_video.media.core import duration
    try:
        return float(duration(path))
    except (OSError, ValueError, RuntimeError):
        return 0.0


def windowed_copy(source, seconds, cache_dir):
    """A stream copy of the first ``seconds`` of ``source``; cached, never overwritten.

    Measured reason this exists: the reference background is 200 s of 1080p60, and
    a three-word lesson uses about twelve seconds of it.  Handing the whole file to
    the proxy would encode 200 s to save decode time on 12 s - a member waiting a
    minute or two on their first preview, for a 40-80 MB file that is 95 % unused.
    Copying the needed window first makes the proxy encode only what the lesson
    plays, and the copy is a stream copy, so it re-encodes nothing itself.
    """
    from word_video.media.core import executable, run
    source = Path(source)
    if seconds <= 0:
        return source
    stat = source.stat()
    target = proxy_cache_dir(cache_dir) / (
        '%s.window-%d-%d.mp4' % (source.stem, int(round(seconds * 1000)),
                                 int(stat.st_mtime)))
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name('.' + target.name)
    try:
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
             '-ss', '0', '-i', str(source), '-t', '%.6f' % seconds,
             '-c', 'copy', '-movflags', '+faststart', str(partial)])
        os.replace(partial, target)
    except (OSError, RuntimeError):
        partial.unlink(missing_ok=True)
        return source
    return target


def resolve_preview_source(path, height, cache_dir=None, budget=DEFAULT_PROXY_BUDGET,
                           proxy=True, needed_seconds=None):
    """Return the file to decode for a ``height``-tall canvas.

    A source no taller than the canvas is used as-is: a proxy of a small file
    would cost an encode and save nothing.  A source that cannot be scaled (no
    video stream, a codec the local ffmpeg refuses) is *also* used as-is rather
    than failing the preview - the original is always playable if the source is,
    and a preview that refuses to open because its optimisation failed would be a
    worse editor.

    ``needed_seconds`` is how much of the picture the lesson actually plays.  When
    it is a small part of a long source, only that part is proxied; see
    :func:`windowed_copy`.
    """
    source = Path(path).resolve(strict=True)
    if not proxy or cache_dir is None:
        # Nothing to decide, so do not spend an ffprobe finding out a height we
        # are not going to compare against anything.
        return PreviewSource(str(source), 0, False, str(source))
    try:
        source_height = int(video_stream(source)['height'])
    except (OSError, ValueError, KeyError):
        return PreviewSource(str(source), 0, False, str(source))
    if source_height <= height:
        return PreviewSource(str(source), source_height, False, str(source))
    target_height = preview_proxy_height(height)
    if source_height <= target_height:
        # The preset that suits this canvas is already as tall as the source, so
        # there is nothing a proxy could save.
        return PreviewSource(str(source), source_height, False, str(source))
    window = None
    if needed_seconds:
        total = source_seconds(source)
        if total and needed_seconds + SOURCE_WINDOW_PREROLL < total * SOURCE_WINDOW_FRACTION:
            window = windowed_copy(source, needed_seconds + SOURCE_WINDOW_PREROLL,
                                   cache_dir)
    encode_from = window or source
    target = proxy_cache_dir(cache_dir) / _proxy_name(encode_from, target_height)
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            proxy_video(encode_from, target, height=target_height)
        except (OSError, RuntimeError, ValueError):
            # An optimisation that failed must not stop the member editing.
            return PreviewSource(str(source), source_height, False, str(source))
    if not target.is_file():
        return PreviewSource(str(source), source_height, False, str(source))
    return PreviewSource(str(target), target_height, True, str(source))


@dataclass(frozen=True)
class PreviewSource:
    """The file to decode, and whether it is a stand-in for a bigger original."""

    path: str
    height: int
    is_proxy: bool
    original: str

    def to_dict(self):
        return {'path': self.path, 'height': self.height, 'is_proxy': self.is_proxy,
                'original': self.original}


def proxy_cache_dir(root):
    """``<root>/preview-proxies``; kept next to whatever the caller calls temp."""
    return Path(root) / 'preview-proxies'


def _proxy_name(source, height):
    stat = source.stat()
    return '%s.%dp.%d-%d.mp4' % (source.stem, height, stat.st_size, int(stat.st_mtime))


def prune_proxies(cache_dir, max_bytes=DEFAULT_PROXY_BUDGET):
    """Delete the oldest proxies until the folder fits ``max_bytes``.

    Returns what was removed, so the caller can report cache behaviour instead of
    guessing.  A file that is currently being decoded by another preview session
    is not a special case: on Windows deleting it fails, and the failure is
    reported rather than retried in a loop.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return {'removed': [], 'bytes_before': 0, 'bytes_after': 0}
    entries = []
    for path in cache_dir.glob('*.mp4'):
        try:
            entries.append((path.stat().st_mtime, path.stat().st_size, path))
        except OSError:
            continue
    total = sum(size for _, size, _ in entries)
    before = total
    removed = []
    for _, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError as error:
            removed.append({'path': str(path), 'error': str(error)})
            continue
        total -= size
        removed.append({'path': str(path), 'bytes': size})
    return {'removed': removed, 'bytes_before': before, 'bytes_after': total}
