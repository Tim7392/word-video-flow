"""Solve a project into derived plans.

``render_plan`` materialises every clip on the project grid and *reports* what it
had to accept (overlapping sound, a target range shorter than its own audio);
``cue_plan`` is the teaching delivery and *refuses* anything ambiguous — missing
role, wrong teaching order, two readings sounding at once.  Nothing here writes to
the project: solve twice and the document is byte-identical.

A lesson may carry an **intro layer**.  Its length is not a number in the project:
it comes from the intro media, measured once by
:func:`word_video.application.intro.measure_intro` (which asks the single resolver
``word_video.media.resolve_intro_audio`` which file sounds).  The measurement is an
input here, exactly like the speech lengths, so this module stays free of IO and a
project cannot claim 2.0 s of intro while its clip runs 1.867 s.

Order of the checks is deliberate: identity and structure first, then time
resolution (with cycle detection), then media, then the teaching semantics.
"""
from dataclasses import dataclass

from .errors import (AmbiguousRoleError, CycleError, DanglingRefError,
                     DuplicateIdError, IntroMediaError, InvalidTimeError,
                     MissingRoleError, SchemaError, SourceRangeError,
                     TeachingOrderError, TeachingOverlapError,
                     UnknownDurationError)
from .model import (AUDIO_ROLES, DISPLAY_LAYERS, INTRO_ROLE, TEACHING_STAGES,
                    VIDEO_ORDER, MediaInfo, check_duplicate_ids)
from .plan import (CUE_TRACKS, Conflict, Cue, CuePlan, PlanItem, PlanSound,
                   RenderPlan)
from .rhythm import intro_ticks, stage_duration_ticks

AUDIO_ORDER = TEACHING_STAGES


@dataclass(frozen=True)
class Solution:
    """Both derivations of one project revision, solved together."""

    render: RenderPlan
    cues: CuePlan


@dataclass(frozen=True)
class IntroMeasurement:
    """What the intro layer's media really is, measured once.

    Produced by :func:`word_video.application.intro.measure_intro`, which asks
    ``word_video.media.resolve_intro_audio`` which of the two candidate files
    sounds and measures the picture length the same way the verified engine does.
    ``seconds`` is therefore the stage's own length: the sound's when something
    plays, the picture's when the clip is silent.  ``sound_source`` is that
    resolver's verdict tag, carried through so the plan can state it instead of
    guessing it again.
    """

    asset_id: str
    seconds: float
    sound_asset: str = ''
    sound_source: str = ''
    from_clip: bool = False
    picture_seconds: float = 0.0

    @property
    def silent(self):
        return not self.sound_asset

    def to_dict(self):
        return {'asset_id': self.asset_id, 'seconds': self.seconds,
                'sound_asset': self.sound_asset, 'sound_source': self.sound_source,
                'from_clip': self.from_clip, 'picture_seconds': self.picture_seconds}


def media_table(media):
    """Validate an ``{asset_id: MediaInfo}`` mapping (shared with expansion)."""
    if media is None:
        return {}
    if not hasattr(media, 'items'):
        raise SchemaError('media must be a mapping of asset_id to MediaInfo',
                          path='media')
    table = {}
    for key, value in media.items():
        if not isinstance(value, MediaInfo):
            raise SchemaError('media entries must be MediaInfo objects', path='media')
        if value.asset_id != key:
            raise SchemaError('media key %r does not match asset id %r'
                              % (key, value.asset_id), path='media')
        if key in table:
            raise DuplicateIdError('duplicate media entry %r' % (key,), path='media')
        table[key] = value
    return table


def _intro_measurement(project, clip, intro):
    """The measurement this intro clip needs, or a structured refusal."""
    path = 'clip:%s' % clip.id
    if intro is None:
        raise IntroMediaError(
            'the intro layer %s has no measurement' % clip.id, path=path,
            hint='先测量片头素材（word_video.application.intro.measure_intro）：'
                 '片头时长只能来自媒体，不能来自秒数常量')
    if not isinstance(intro, IntroMeasurement):
        raise SchemaError('intro must be an IntroMeasurement', path=path)
    if clip.source is not None and intro.asset_id != clip.source.asset_id:
        raise IntroMediaError(
            'the measurement is for %r but the intro layer uses %r'
            % (intro.asset_id, clip.source.asset_id), path=path,
            hint='素材换过之后必须重新测量，不能用旧结果求解')
    if not (intro.seconds > 0):
        raise IntroMediaError('the intro media has no usable length', path=path,
                              hint='检查片头素材：画面与声音都必须有正的长度')
    return intro


def _resolve_duration(project, clip, intro=None):
    if clip.duration_ticks is not None:
        # A trimmed clip keeps the length the user gave it; the rhythm is only
        # a default, never a silent override.
        return clip.duration_ticks
    if clip.source is None:
        raise InvalidTimeError('clip %s has no length and no media' % clip.id,
                               path='clip:%s' % clip.id,
                               hint='显式给出时长，或为它绑定媒体')
    if clip.is_intro:
        # The intro's length comes from its own media, on the frame grid the
        # verified engine used, so a template's intro_s can never contradict it.
        measurement = _intro_measurement(project, clip, intro)
        return intro_ticks(measurement.seconds, project.fps_num, project.fps_den)
    if clip.role not in AUDIO_ROLES:
        raise InvalidTimeError('only teaching stages derive their length from media',
                               path='clip:%s' % clip.id,
                               hint='为该图层显式给出时长')
    record = project.record(clip.record_id)
    return stage_duration_ticks(
        clip.role, record, clip.source.seconds, gap_s=project.gap_s, speed=project.speed,
        first_six=project.first_six, extra=project.extra,
        fps_num=project.fps_num, fps_den=project.fps_den)


def _resolve_range(project, clip, memo, visiting, intro=None):
    """Absolute ``(start, end)`` of one clip; links are followed with cycle detection."""
    if clip.id in memo:
        return memo[clip.id]
    if clip.id in visiting:
        path = ' → '.join(visiting + (clip.id,))
        raise CycleError('time dependency cycle: %s' % path,
                         path='clip:%s' % clip.id,
                         hint='解除其中一个绑定（UnbindStart）后重新求解')
    expr = clip.start
    if expr.is_absolute:
        start = expr.ticks
    else:
        reference = project.clip(expr.ref)
        if reference is None:
            raise DanglingRefError('clip %s follows unknown clip %r' % (clip.id, expr.ref),
                                   path='clip:%s' % clip.id,
                                   hint='删除该绑定，或恢复被引用的片段')
        ref_start, ref_end = _resolve_range(project, reference, memo,
                                            visiting + (clip.id,), intro)
        start = (ref_start if expr.edge == 'start' else ref_end) + expr.offset_ticks
        if start < 0:
            raise InvalidTimeError('clip %s would start before the project' % clip.id,
                                   path='clip:%s' % clip.id)
    duration = _resolve_duration(project, clip, intro)
    if duration <= 0:
        raise InvalidTimeError('clip %s has no positive length' % clip.id,
                               path='clip:%s' % clip.id)
    memo[clip.id] = (start, start + duration)
    return memo[clip.id]


def _check_media(clip, table):
    """A speech clip needs measured media; a source window must exist inside it."""
    source = clip.source
    if source is None:
        return None
    info = table.get(source.asset_id)
    needs_media = clip.duration_ticks is None or clip.role in AUDIO_ROLES
    if info is None:
        if needs_media:
            raise UnknownDurationError(
                'media %r of clip %s has not been measured' % (source.asset_id, clip.id),
                path='clip:%s' % clip.id,
                hint='先准备并探测该配音，再用真实时长求解（不生成正式计划）')
        return None
    if (info.unit_num, info.unit_den) != (source.unit_num, source.unit_den):
        raise SchemaError(
            'clip %s uses grid %d/%d but media %r was measured on %d/%d'
            % (clip.id, source.unit_num, source.unit_den, source.asset_id,
               info.unit_num, info.unit_den),
            path='clip:%s' % clip.id)
    if source.source_end > info.units:
        raise SourceRangeError(
            'clip %s asks for source %d..%d but media %r has %d units'
            % (clip.id, source.source_start, source.source_end, source.asset_id,
               info.units),
            path='clip:%s' % clip.id,
            hint='缩小源区间或改用已探测的媒体')
    return info


def resolve(project, media, intro=None):
    """Resolved ``(start, end)`` ticks per clip id, plus the media it validated.

    ``intro`` is the measurement of the project's intro layer
    (:class:`IntroMeasurement`); it is required exactly when that layer exists.
    """
    table = media_table(media)
    check_duplicate_ids(project.records, project.clips)
    project.frame_ticks  # refuses a rate the tick base cannot express
    if intro is not None and project.intro_clip() is None:
        raise IntroMediaError('the project has no intro layer to measure',
                              path='project',
                              hint='删掉这次测量，或先给工程加片头素材')
    for clip in project.clips:
        if clip.record_id and project.record(clip.record_id) is None:
            raise DanglingRefError(
                'clip %s belongs to unknown record %r' % (clip.id, clip.record_id),
                path='clip:%s' % clip.id,
                hint='恢复该词条或删除该片段')
    memo = {}
    for clip in project.clips:
        _resolve_range(project, clip, memo, (), intro)
        _check_media(clip, table)
    return memo


def _items(project, ranges, intro=None):
    video, audio = [], []
    for clip in project.clips:
        start, end = ranges[clip.id]
        sound = None
        if clip.is_intro:
            measurement = _intro_measurement(project, clip, intro)
            sound = PlanSound(asset_id=measurement.sound_asset,
                              fallback_asset=clip.audio_asset,
                              source=measurement.sound_source)
        item = PlanItem(clip_id=clip.id, role=clip.role, record_id=clip.record_id,
                        start_ticks=start, end_ticks=end, text=clip.text,
                        source=clip.source, sound=sound)
        (audio if clip.is_speech else video).append(item)
    video.sort(key=lambda item: (VIDEO_ORDER.index(item.role), item.start_ticks,
                                 item.clip_id))
    audio.sort(key=lambda item: (item.start_ticks, AUDIO_ORDER.index(item.role),
                                 item.clip_id))
    return tuple(video), tuple(audio)


def sounding_items(video, audio):
    """Everything audible: the reading stages plus an intro that is not silent.

    An intro whose clip is silent and has no fallback occupies picture only, so it
    cannot make a lesson ambiguous; one that does sound has to be part of the same
    overlap question as the readings.
    """
    intro = next((item for item in video if item.is_intro), None)
    if intro is not None and intro.sound is not None and not intro.sound.silent:
        return tuple(audio) + (intro,)
    return tuple(audio)


def speech_overlaps(audio):
    """Every overlapping pair of speech items, found by a sweep, in a stable order."""
    ordered = sorted(audio, key=lambda item: (item.start_ticks, item.end_ticks,
                                              item.clip_id))
    pairs = []
    active = []
    for item in ordered:
        active = [other for other in active if other.end_ticks > item.start_ticks]
        for other in active:
            pairs.append((other, item))
        active.append(item)
    return tuple(pairs)


def _conflicts(sounds, speech):
    found = []
    for first, second in speech_overlaps(sounds):
        found.append(Conflict(
            code='OVERLAP_ACCEPTED',
            message='%s and %s sound at the same time (%d ticks)'
                    % (first.clip_id, second.clip_id,
                       min(first.end_ticks, second.end_ticks)
                       - max(first.start_ticks, second.start_ticks)),
            path='clip:%s' % first.clip_id, other_path='clip:%s' % second.clip_id,
            hint='混音可以表达重叠；教学交付需要先消除歧义'))
    for item in speech:
        if item.source is not None and item.duration_ticks < item.source.duration_ticks:
            found.append(Conflict(
                code='SOURCE_TRUNCATED',
                message='%s reserves %d ticks for %d ticks of audio'
                        % (item.clip_id, item.duration_ticks, item.source.duration_ticks),
                path='clip:%s' % item.clip_id,
                hint='目标时长比语音短，导出会截断这段朗读'))
    return tuple(found)


def render_plan(project, media, intro=None):
    """The mix/preview plan: it expresses overlap and reports what it accepted."""
    ranges = resolve(project, media, intro)
    video, audio = _items(project, ranges, intro)
    total = 0
    for item in video + audio:
        total = max(total, item.end_ticks)
    return RenderPlan(
        project_id=project.project_id, project_revision=project.revision,
        fps_num=project.fps_num, fps_den=project.fps_den, width=project.width,
        height=project.height, sample_rate=project.sample_rate,
        channels=project.channels, total_ticks=total, video=video, audio=audio,
        conflicts=_conflicts(sounding_items(video, audio), audio))


def _record_items(project, ranges):
    """``{record_id: {role: PlanItem}}``; two clips claiming one role is an error."""
    grouped = {}
    for clip in project.clips:
        if not clip.record_id:
            continue
        start, end = ranges[clip.id]
        roles = grouped.setdefault(clip.record_id, {})
        if clip.role in roles:
            raise AmbiguousRoleError(
                'record %s has two %s clips (%s and %s)'
                % (clip.record_id, clip.role, roles[clip.role].clip_id, clip.id),
                path='clip:%s' % clip.id,
                hint='教学交付每个角色只能有一个片段')
        roles[clip.role] = PlanItem(
            clip_id=clip.id, role=clip.role, record_id=clip.record_id,
            start_ticks=start, end_ticks=end, text=clip.text, source=clip.source)
    return grouped


def _check_teaching(project, ranges, sounds):
    grouped = _record_items(project, ranges)
    for record in project.records:
        roles = grouped.get(record.id, {})
        for role in TEACHING_STAGES + DISPLAY_LAYERS:
            if role not in roles:
                raise MissingRoleError(
                    'record %s has no %s clip' % (record.id, role),
                    path='record:%s' % record.id,
                    hint='教学交付需要女声英文、男声英文、中文释义与三条显示图层')
        stages = {role: roles[role] for role in TEACHING_STAGES}
        order = [(role, stages[role].start_ticks) for role in TEACHING_STAGES]
        if not (order[0][1] < order[1][1] < order[2][1]):
            raise TeachingOrderError(
                'record %s is out of teaching order: %s'
                % (record.id, ', '.join('%s@%d' % pair for pair in order)),
                path='record:%s' % record.id,
                hint='教学顺序固定为 %s' % ' → '.join(TEACHING_STAGES))
    for first, second in speech_overlaps(sounds):
        overlap = min(first.end_ticks, second.end_ticks) - max(first.start_ticks,
                                                              second.start_ticks)
        raise TeachingOverlapError(
            '%s and %s overlap by %d ticks' % (first.clip_id, second.clip_id, overlap),
            path='clip:%s' % first.clip_id, hint='移开或修剪其中一个片段后再交付')


def _cues(project, ranges):
    grouped = _record_items(project, ranges)
    counters = {track: 0 for track in CUE_TRACKS}
    cues = []

    def add(track, start, end, text, record_id, role):
        counters[track] += 1
        cues.append(Cue(track=track, index=counters[track], record_id=record_id,
                        role=role, start_ticks=start, end_ticks=end, text=text))

    for record in project.records:
        roles = grouped[record.id]
        female = roles['female']
        male = roles['male']
        chinese = roles['chinese']
        english = roles['english']
        # 01 repeats the English word: first reading, then second reading.  The
        # boundaries come from the speech stages, exactly like the verified engine.
        add('01', female.start_ticks, male.start_ticks, english.text, record.id, 'english')
        add('01', male.start_ticks, chinese.start_ticks, english.text, record.id, 'english')
        # 02/03/04 follow the on-screen layer that carries the text, so moving a
        # layer moves its own subtitle track.
        add('02', english.start_ticks, english.end_ticks, english.text, record.id, 'english')
        phonetic = roles['phonetic']
        add('03', phonetic.start_ticks, phonetic.end_ticks, phonetic.text, record.id,
            'phonetic')
        meaning = roles['meaning']
        add('04', meaning.start_ticks, meaning.end_ticks, meaning.text, record.id, 'meaning')
        add('05', chinese.start_ticks, chinese.end_ticks, chinese.text, record.id,
            'spoken_meaning')
    return tuple(cues)


def _total_ticks(items):
    total = 0
    for item in items:
        total = max(total, item.end_ticks)
    return total


def cue_plan(project, media, intro=None):
    """The five-track teaching projection; refused when it would be ambiguous."""
    ranges = resolve(project, media, intro)
    video, audio = _items(project, ranges, intro)
    _check_teaching(project, ranges, sounding_items(video, audio))
    return CuePlan(project_id=project.project_id, project_revision=project.revision,
                   fps_num=project.fps_num, fps_den=project.fps_den,
                   total_ticks=_total_ticks(video + audio),
                   cues=_cues(project, ranges))


def solve(project, media, intro=None):
    """Both derivations of one revision, resolved and checked once."""
    ranges = resolve(project, media, intro)
    video, audio = _items(project, ranges, intro)
    _check_teaching(project, ranges, sounding_items(video, audio))
    total = _total_ticks(video + audio)
    render = RenderPlan(
        project_id=project.project_id, project_revision=project.revision,
        fps_num=project.fps_num, fps_den=project.fps_den, width=project.width,
        height=project.height, sample_rate=project.sample_rate,
        channels=project.channels, total_ticks=total, video=video, audio=audio,
        conflicts=_conflicts(sounding_items(video, audio), audio))
    cues = CuePlan(project_id=project.project_id, project_revision=project.revision,
                   fps_num=project.fps_num, fps_den=project.fps_den,
                   total_ticks=total, cues=_cues(project, ranges))
    return Solution(render=render, cues=cues)
