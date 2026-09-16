"""Measure a project's intro layer by asking the one intro resolver.

``word_video/media/intro.py::resolve_intro_audio`` owns *which file sounds during
the intro* (the clip's own soundtrack first, then the standalone fallback, silent
otherwise).  That decision is not repeated here: this module measures, translates
the answer into the domain's :class:`~word_video.domain.compile.IntroMeasurement`,
and turns anything unusable into a structured refusal instead of a traceback.

Two things the project model needs and the resolver does not answer:

* **how long the stage is when the clip is silent** — the verified engine measures
  the *video stream*, not the container, so
  ``word_video.media.video_stream_seconds`` is reused rather than re-derived
  (a second implementation of "how long is this clip" is what W02 removed for the
  sound and must not come back for the picture);
* **that the picture covers its own sound** — the same rule the verified engine
  applies, with the resolver's own ``FRAME_EPSILON_S`` tolerance.

All IO lives here: the domain stays pure and the plan is solved from a measurement.
"""
from ..domain.compile import IntroMeasurement
from ..domain.errors import IntroMediaError
from ..domain.model import INTRO_ROLE
from ..media import resolve_intro_audio, video_stream_seconds
from ..media.intro import FRAME_EPSILON_S


def _picture_seconds(video):
    """Length of the clip's picture, measured the way the verified engine measures it.

    ``word_video.media.video_stream_seconds`` is that one implementation now; this
    module used to reach into ``timing`` for it, which is the private cross-module
    call the rename removes.
    """
    return float(video_stream_seconds(video))


def measure_intro(video_asset, *, fallback_audio='', clip_id=None):
    """Measure one intro layer: the sound that plays and the stage it needs.

    ``video_asset`` is the intro clip (a path today: asset ids are resolved by the
    asset repository, which does not exist yet), ``fallback_audio`` the standalone
    audio the project declared for it (``Clip.audio_asset``, the legacy
    ``intro_audio``).  Raises :class:`IntroMediaError` with the clip's object path
    when a file is missing, unreadable, silent with no picture, or shorter than its
    own soundtrack.
    """
    path = 'clip:%s' % (clip_id or INTRO_ROLE)
    video = str(video_asset or '').strip()
    if not video:
        raise IntroMediaError('the intro layer names no video asset', path=path,
                              hint='给片头片段一个素材路径，或删掉这个图层')
    try:
        choice = resolve_intro_audio(video, None, fallback_audio or None)
    except (OSError, ValueError) as error:
        raise IntroMediaError('intro media is unusable: %s' % error, path=path,
                              hint='检查片头素材与备用音轨的路径、格式与可读性') from None
    try:
        picture = _picture_seconds(video)
    except (OSError, ValueError) as error:
        raise IntroMediaError('intro picture is unusable: %s' % error, path=path,
                              hint='片头素材必须有可读的视频轨') from None
    if choice.silent:
        # A silent clip still owns the stage: its picture sets the length, exactly
        # as the verified engine reserved it.
        seconds = picture
        if not seconds > 0:
            raise IntroMediaError('the intro clip has no usable picture length',
                                  path=path, hint='片头素材的画面长度必须为正')
    else:
        seconds = float(choice.duration_s)
        if choice.from_clip and picture + FRAME_EPSILON_S < seconds:
            # The clip's own sound may not outlast the picture it belongs to; a
            # *fallback* file is not part of the clip and may outlast it, because
            # the background is already running behind the intro.
            raise IntroMediaError(
                'intro clip picture (%.3fs) is shorter than its sound (%.3fs)'
                % (picture, seconds), path=path,
                hint='换用更长的画面，或把这条音轨放到片头片段之外')
    return IntroMeasurement(asset_id=video, seconds=seconds,
                            sound_asset=str(choice.path), sound_source=choice.source,
                            from_clip=choice.from_clip, picture_seconds=picture)


def measure_project_intro(project, *, clip_id=None):
    """Measure the project's intro layer, or ``None`` when it has none."""
    clip = project.intro_clip()
    if clip is None:
        return None
    if clip.source is None:
        raise IntroMediaError('the intro layer has no media slice',
                              path='clip:%s' % clip.id,
                              hint='片头片段必须指向一个视频素材')
    return measure_intro(clip.source.asset_id, fallback_audio=clip.audio_asset,
                         clip_id=clip_id or clip.id)
