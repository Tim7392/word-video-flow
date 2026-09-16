"""One policy for the intro's sound, so the MP4 and the draft cannot disagree.

The intro is assembled from up to two independent inputs:

``intro.video``
    the reference countdown clip.  It usually carries its own countdown sound.
``lesson.intro_audio``
    a separate audio file, or ``''``.

An earlier revision resolved those in two places with opposite precedence: the
mixer preferred the clip and fell back to ``intro_audio``, the draft preferred
``intro_audio`` and fell back to the clip.  A lesson that supplied both
therefore produced an MP4 with the clip's sound and a draft whose ``片头音效``
segment was the other file - the two deliverables described different scenes,
and nothing in either file said so.

This module is the single resolver.  Its rules:

* **The clip wins.**  The clip's own soundtrack is the reference countdown; an
  external file is the fallback for a *silent* clip or for the legacy path that
  reserves intro seconds without any clip.  This is also what the M0 acceptance
  checker assumes: it requires a ``片头音效`` segment exactly when the clip file
  carries an audio stream, and it never looks at ``intro_audio``.
* **Exactly one source ever sounds.**  There is no mixing of the two.
* **Nothing is dropped silently.**  A caller that passes both can see, from
  :class:`IntroAudio`, that ``intro_audio`` was not used.
* **The intro is as long as the sound that plays.**  ``timing.intro_frames_for``
  reads ``duration_s`` from here instead of measuring the clip itself, so a
  fallback file cannot be clipped to the clip's length.
"""
from dataclasses import dataclass
import math
from pathlib import Path

from .core import has_audio, probe

__all__ = ['IntroAudio', 'resolve_intro_audio']

SILENT_CLIP = 'SILENT_CLIP_NO_INTRO_AUDIO'
FROM_CLIP = 'FROM_CLIP'
FROM_INTRO_AUDIO = 'FROM_INTRO_AUDIO'
# Tolerance for "the fallback file and the intro stage are the same length".
FRAME_EPSILON_S = 0.05


@dataclass(frozen=True)
class IntroAudio:
    """Which file supplies the intro's sound, and why."""

    path: str            # '' when the intro is silent.
    from_clip: bool
    source: str          # one of the three constants above.
    duration_s: float    # duration of ``path``; 0.0 when silent.
    sample_rate: int = 0  # the stream's own rate, for sample-exact stage maths.

    @property
    def silent(self):
        return not self.path

    def report(self):
        """Machine-readable form for the job result; no paths, just the reason."""
        return self.source


def _audio_facts(path):
    """Duration and sample rate of the file's *audio stream*.

    The stream, not the container: a fallback whose container length differs
    from its audio track would otherwise be allocated the wrong number of
    samples and come out truncated or short.
    """
    data = probe(path)
    stream = next((item for item in data.get('streams', [])
                   if item.get('codec_type') == 'audio'), None)
    if stream is None:
        raise ValueError('Intro audio has no audio stream: %s' % path)
    if stream.get('duration'):
        value = float(stream['duration'])
    else:
        value = float(data['format']['duration'])
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Intro audio has no usable duration: %s' % path)
    rate = int(float(stream['sample_rate'])) if stream.get('sample_rate') else 0
    return value, rate


def resolve_intro_audio(clip_video=None, clip_audio=None, intro_audio=None):
    """Resolve the intro's single sound source.

    ``clip_audio`` is the clip's *own* extracted soundtrack when one is
    available; ``clip_video`` is probed only when ``clip_audio`` is absent, so a
    caller that already knows keeps this cheap.  Missing files raise instead of
    being treated as silent, because a typo in a path is not a quiet countdown.
    """
    fallback = str(intro_audio or '').strip()
    if fallback:
        fallback = str(Path(fallback).resolve(strict=True))
    if clip_audio:
        path = str(Path(clip_audio).resolve(strict=True))
        seconds, rate = _audio_facts(path)
        return IntroAudio(path, True, FROM_CLIP, seconds, rate)
    if clip_video:
        video = str(Path(clip_video).resolve(strict=True))
        if has_audio(video):
            seconds, rate = _audio_facts(video)
            return IntroAudio(video, True, FROM_CLIP, seconds, rate)
    if fallback:
        seconds, rate = _audio_facts(fallback)
        return IntroAudio(fallback, False, FROM_INTRO_AUDIO, seconds, rate)
    return IntroAudio('', False, SILENT_CLIP, 0.0)


def intro_frames(choice, fps):
    """Frames the intro occupies, from the sound that actually plays.

    A silent intro still needs a stage when a clip is on screen; the caller
    passes its own fallback length through ``intro_s`` in that case, so zero here
    means "no sound", not "no intro".
    """
    if choice.silent:
        return 0
    frames = math.ceil(choice.duration_s * fps - 1e-9)
    if frames <= 0:
        raise ValueError('Intro audio has no usable duration')
    return frames
