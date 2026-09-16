"""Continuous preview for the editor: one PCM clock, one decoder, bounded queues.

The package exists to answer one requirement - *editing must not wait for a
re-encode, and a seek must not play the old position* - without turning the
member's thin laptop into a rendering farm.  Five decisions carry that:

``clock``
    One time source, the PCM frames the audio device actually played.  Video and
    text are pure functions of it, so nothing can drift and a starving device
    stalls the picture instead of letting it run ahead.
``queues``
    Every hand-off is bounded and every wait has a timeout.  Frames overflow by
    dropping the oldest (the newest picture is the useful one); PCM never drops,
    because a dropped block is an audible click.
``audio``
    Only the window the device needs next is mixed, from the plan's own audio
    items, using the delivery mixer's contract - so a preview is cheap without
    becoming a second opinion about the mix.
``video``
    Frames are decoded by an ffmpeg **child process**.  That is what gives a
    hung native decoder a hard, testable recovery boundary: it is killed, which
    closes the pipe and unblocks the reader, instead of a thread being joined
    forever while the UI freezes.
``sources``
    A tall source is decoded through B's low-resolution proxy, under a disk
    budget.  A proxy is a preview artifact only; delivered resolution is the
    plan's, unchanged.

Qt is deliberately absent here.  The QAudioSink output and the canvas live in
``desktop/``, so this engine can be tested headlessly and quickly, and the
packaged headless CLI keeps excluding PySide6.
"""
from .audio import AudioSpan, WindowMixer, spans_from_plan
from .clock import (AudioClock, AudioOutput, ManualAudioOutput, NullAudioOutput,
                    PlaybackState, frames_for_ticks, ticks_for_frames)
from .layout import DisplayPort, LayoutSurfaceDisplay, LayoutUnavailable
from .queues import BLOCK, DROP_OLDEST, BoundedQueue, QueueClosed
from .session import (DEFAULT_CANVAS_HEIGHT, Presentation, PreviewSession,
                      canvas_size_for, picture_item)
from .sources import PreviewSource, prune_proxies, resolve_preview_source
from .video import DecodeSpec, FfmpegFrameDecoder, VideoFrame

__all__ = [
    'AudioClock', 'AudioOutput', 'AudioSpan', 'BLOCK', 'BoundedQueue',
    'DEFAULT_CANVAS_HEIGHT', 'DROP_OLDEST', 'DecodeSpec', 'DisplayPort',
    'FfmpegFrameDecoder', 'LayoutSurfaceDisplay', 'LayoutUnavailable',
    'ManualAudioOutput', 'NullAudioOutput', 'PlaybackState', 'Presentation',
    'PreviewSession', 'PreviewSource', 'QueueClosed', 'VideoFrame', 'WindowMixer',
    'canvas_size_for', 'frames_for_ticks', 'picture_item', 'prune_proxies',
    'resolve_preview_source', 'spans_from_plan', 'ticks_for_frames',
]
