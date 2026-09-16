"""Derived media files for work that does not need the full-resolution original.

Editing and rendering repeatedly seek into a 4K background or a 348 MB reference
export; a 1080p or 720p proxy of the same seconds is several times cheaper to
decode, and the caption stage - not the decoder - is what actually costs the
render.  A proxy is therefore useful for *预览/编辑*, where it must be visually
the same clip, and never a way to lower the delivered resolution: nothing in
this module replaces an original, and every output is published under its own
name via a no-overwrite link.

The frame rate and duration are preserved exactly (the scale filter does not
touch them), so a proxy can be dropped into the same timeline slot as its
source.  Audio is stream-copied when the source has any, so a proxy never
changes what the intro sounds like.
"""
from pathlib import Path
import os
import uuid

from .core import executable, has_audio, run
from .streams import video_stream

__all__ = ['PROXY_PRESETS', 'proxy_video']

#: Measured presets, by picture height.  The quality is the crf chosen for that
#: size, so a caller naming only a height still gets a deliberate setting.
PROXY_PRESETS = {1080: 20, 720: 22, 540: 24}


def _preset(height):
    """The preset key for ``height``, accepting the numbers people write."""
    if isinstance(height, str):
        text = height.strip().lower().rstrip('p')
        if text.isdigit():
            height = int(text)
    if height not in PROXY_PRESETS:
        raise ValueError('Unknown proxy preset %r; name one of %s, or a crf'
                         % (height, ', '.join(str(key) for key in PROXY_PRESETS)))
    return int(height)


def proxy_video(source, target, height=720, crf=None, preset='veryfast'):
    """Write a smaller same-length copy of ``source``; never overwrites.

    Returns the output path.  ``height`` is the *target* picture height, so
    asking for 720 from a 1080p source shrinks it and asking for 1080 from a
    720p source keeps 720 - the scale filter never enlarges, because an
    "upscaled proxy" would only cost more to decode than the original.  A bare
    ``crf`` allows any height; without one, the height must be a known preset.
    """
    source = Path(source).resolve(strict=True)
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    if target.suffix.lower() not in ('.mp4', '.mov', '.mkv'):
        raise ValueError('Proxy target must be mp4/mov/mkv: %s' % target)
    if crf is None:
        height = _preset(height)
        quality = PROXY_PRESETS[height]
    else:
        quality = crf
        height = int(height)
    if not 0 <= quality <= 51:
        raise ValueError('crf must be 0..51')
    picture = video_stream(source)
    source_height = int(picture['height'])
    # An explicitly named height is honoured even upwards - the caller asked for
    # that canvas.  A *preset* only ever shrinks: the presets exist so ordinary
    # preview work gets a smaller file, and quietly enlarging a 720p source to a
    # "1080p proxy" would cost more to decode than the original.
    target_height = height if crf is not None else min(height, source_height)
    args = [executable('ffmpeg'), '-v', 'warning', '-nostdin', '-n', '-i', source]
    if target_height != source_height:
        # scale=-2: writes the width that keeps the aspect ratio and is divisible
        # by two; libx264 with yuv420p requires even dimensions.
        args += ['-vf', 'scale=-2:%d' % target_height]
    args += ['-c:v', 'libx264', '-preset', preset, '-crf', str(quality),
             '-pix_fmt', 'yuv420p']
    args += ['-c:a', 'copy'] if has_audio(source) else ['-an']
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name('.' + uuid.uuid4().hex + target.suffix)
    try:
        run(args + ['-movflags', '+faststart', str(partial)], timeout=1800)
        # Hard-link publication cannot overwrite a concurrently created file.
        os.link(partial, target)
        return str(target)
    finally:
        partial.unlink(missing_ok=True)
