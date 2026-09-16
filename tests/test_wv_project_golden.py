"""Hand-computed golden case for ``wv-project@1``: one 5.5 second lesson.

Every expected value below is arithmetic done by hand from the verified rhythm
rules — speed 1.25, gap 0.1 s, English floor 1.0 s, Chinese floor
``max(0.5, min(n,6)*0.4 + (n-6)*0.2)`` — and the 720000 tick/s base.  No expected
number is produced by calling the production solver; the last section checks the
same boundaries against ``word_video/timing.py`` + ``word_video/srt_export.py``,
which M0 recomputed independently.
"""
from fractions import Fraction
import math

import pytest

from word_video import srt_export, timing
from word_video.contracts import LessonSpec, SpeechAsset, WordEntry
from word_video.application import TrimClip, apply, instantiate
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, TEACHING_STAGES, Clip,
                               DuplicateIdError, DanglingRefError, CycleError,
                               MediaInfo, MediaSlice, MissingRoleError,
                               AmbiguousRoleError, Project, Record,
                               SourceRangeError, TeachingOrderError,
                               TeachingOverlapError, TimeExpr, UnknownDurationError,
                               UnsupportedRateError, cue_plan, render_plan, solve,
                               ticks_to_milliseconds)

WORD = 'promise'
PHONETIC = 'ˈprɒmɪs'
MEANING = 'n. 承诺；诺言'
SPOKEN = '承诺诺言誓言'          # 6 Hanzi → floor 6 * 0.4 = 2.4 s
SOURCE_SECONDS = {'female': '1.15', 'male': '0.85', 'chinese': '3.275'}

# ---------------------------------------------------------------------------
# Hand computation, fps 60 → 1 frame = 12000 ticks; speed 1.25; gap 0.1 s.
#   intro   : ceil(1.0 * 60) = 60 frames                     -> 720000 ticks
#   female  : max(1.0, 1.15+0.1) = 1.25 / 1.25 = 1.00 s → 60 frames
#                                                      -> [720000, 1440000)
#   male    : max(1.0, 0.85+0.1) = 1.00 / 1.25 = 0.80 s → 48 frames
#                                                      -> [1440000, 2016000)
#   chinese : max(2.4, 3.275+0.1) = 3.375 / 1.25 = 2.70 s → 162 frames
#                                                      -> [2016000, 3960000)
#   total   : 60 + 60 + 48 + 162 = 330 frames = 5.5 s    -> 3960000 ticks
# ---------------------------------------------------------------------------
INTRO_TICKS = 720000
FEMALE = (720000, 1440000)
MALE = (1440000, 2016000)
CHINESE = (2016000, 3960000)
TOTAL_TICKS = 3960000
# 5.5 s of SRT: 60 frames = 1000 ms, 120 = 2000 ms, 168 = 2800 ms, 330 = 5500 ms
CUE_MILLISECONDS = {
    '01': [(1000, 2000, WORD), (2000, 2800, WORD)],
    '02': [(1000, 5500, WORD)],
    '03': [(2000, 5500, PHONETIC)],
    '04': [(2800, 5500, MEANING)],
    '05': [(2800, 5500, SPOKEN)],
}


def golden_record(record_id='w1', index=0):
    return Record(id=record_id, word=WORD, phonetic=PHONETIC, meaning=MEANING,
                  spoken_meaning=SPOKEN, index=index)


def golden_media(record_id='w1'):
    """One measured asset per stage: seconds * 48000 lands on the sample grid."""
    return {('%s:%s' % (record_id, role)):
            MediaInfo('%s:%s' % (record_id, role),
                      int(Fraction(seconds) * 48000), 1, 48000)
            for role, seconds in SOURCE_SECONDS.items()}


def golden_base(**overrides):
    settings = dict(project_id='golden', intro_s=1.0)
    settings.update(overrides)
    return Project(**settings)


def golden_project(**overrides):
    return instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), golden_media(),
                       golden_base(**overrides))


@pytest.fixture
def media():
    return golden_media()


@pytest.fixture
def project():
    return golden_project()


def test_golden_clip_ranges_are_the_hand_computed_ticks(project):
    ranges = {clip.id: (clip.start.ticks, clip.start.ticks + clip.duration_ticks)
              for clip in project.clips}
    assert project.intro_ticks == INTRO_TICKS
    assert ranges['w1.female'] == FEMALE
    assert ranges['w1.male'] == MALE
    assert ranges['w1.chinese'] == CHINESE
    # Display layers default to the verified picture: English for the whole word,
    # phonetic from the male reading, meaning from the Chinese reading.
    assert ranges['w1.english'] == (FEMALE[0], CHINESE[1])
    assert ranges['w1.phonetic'] == (MALE[0], CHINESE[1])
    assert ranges['w1.meaning'] == (CHINESE[0], CHINESE[1])
    assert ranges['layer.title'] == (0, TOTAL_TICKS)
    assert ranges['layer.subtitle'] == (0, TOTAL_TICKS)
    assert ranges['layer.footer'] == (0, TOTAL_TICKS)


def test_golden_media_slices_keep_the_source_grid(project):
    female = project.clip('w1.female').source
    assert female.asset_id == 'w1:female'
    assert (female.source_start, female.source_end) == (0, 55200)  # 1.15 s * 48000
    assert female.seconds == Fraction(55200, 48000)
    assert female.speed == 1.25
    chinese = project.clip('w1.chinese').source
    assert (chinese.source_start, chinese.source_end) == (0, 157200)  # 3.275 s


def test_golden_plan_is_five_and_a_half_seconds(project, media):
    solution = solve(project, media)
    assert solution.render.total_ticks == TOTAL_TICKS
    assert solution.cues.total_ticks == TOTAL_TICKS
    assert Fraction(TOTAL_TICKS, 720000) == Fraction(11, 2)
    # Audio items, one per teaching stage, in teaching order.
    assert [(item.role, item.start_ticks, item.end_ticks)
            for item in solution.render.audio] == [
        ('female',) + FEMALE, ('male',) + MALE, ('chinese',) + CHINESE]
    assert solution.render.conflicts == ()


def test_golden_cue_plan_matches_the_five_track_semantics(project, media):
    cues = cue_plan(project, media)
    for track, expected in CUE_MILLISECONDS.items():
        actual = [(ticks_to_milliseconds(cue.start_ticks),
                   ticks_to_milliseconds(cue.end_ticks), cue.text)
                  for cue in cues.by_track()[track]]
        assert actual == expected, track
    # Cue numbers restart per track and stay sequential.
    assert [cue.index for cue in cues.by_track()['01']] == [1, 2]
    assert [cue.index for cue in cues.by_track()['03']] == [1]


def test_golden_boundaries_match_the_verified_engine(project, media):
    """The same lesson through ``timing`` + ``srt_export`` gives the same times."""
    lesson = LessonSpec(entries=[WordEntry(index=1, word=WORD, phonetic=PHONETIC,
                                           meaning=MEANING, spoken_meaning=SPOKEN)],
                        fps=60, intro_s=1.0)
    assets = []
    for role in TEACHING_STAGES:
        text = SPOKEN if role == 'chinese' else WORD
        # The old engine floors a stage at the *rendered* WAV length; a new
        # project declares speed on the clip and the mixer applies it once, so
        # an original-duration asset with no extra rendered length is the
        # matching input.
        assets.append(SpeechAsset(word_index=1, role=role, text=text, path='unused.wav',
                                  duration_s=float(Fraction(SOURCE_SECONDS[role])),
                                  rendered_duration_s=0.0))
    manifest = timing.build_timeline(lesson, assets)
    assert manifest.total_frames == 330
    assert manifest.intro_frames == 60
    word = manifest.words[0]
    assert (word['start_frame'], word['male_frame'], word['chinese_frame'],
            word['end_frame']) == (60, 120, 168, 330)
    old_tracks = srt_export._build_tracks(manifest)
    new_cues = cue_plan(project, media)
    for suffix, body in zip(srt_export.SRT_SUFFIXES, old_tracks):
        old = []
        for block in [part for part in body.split('\n\n') if part.strip()]:
            lines = block.split('\n')
            start, end = lines[1].split(' --> ')
            old.append((start, end, lines[2]))
        track_id = suffix[1:3]
        new = []
        for cue in new_cues.by_track()[track_id]:
            new.append((_ms_text(ticks_to_milliseconds(cue.start_ticks)),
                        _ms_text(ticks_to_milliseconds(cue.end_ticks)), cue.text))
        assert new == old, suffix


def _ms_text(milliseconds):
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return '%02d:%02d:%02d,%03d' % (hours, minutes, seconds, millis)


def test_rhythm_constants_still_come_from_the_verified_engine(project):
    """Drift guard: the shared rules must not be re-stated or silently moved."""
    from word_video.domain import rhythm
    assert rhythm.ENGLISH_MINIMUM_S == Fraction(1)
    assert rhythm.DEFAULT_SPEED == 1.25
    assert rhythm.DEFAULT_GAP_S == 0.1
    assert rhythm.DEFAULT_FIRST_SIX == 0.4
    assert rhythm.DEFAULT_EXTRA == 0.2
    assert rhythm.ENGLISH_MINIMUM_S == Fraction(str(timing._ENGLISH_MINIMUM))


def test_chinese_floor_matches_the_hand_computed_formula():
    """Hand-computed: ``max(0.5, min(n,6)*0.4 + max(n-6,0)*0.2)`` over Hanzi.

    The verified engine returns a double, so the comparison is a float one; what
    must match exactly is the frame decision, which the next test checks.
      6 Hanzi -> 6*0.4            = 2.4
      3 Hanzi -> 3*0.4            = 1.2
      2 Hanzi -> 2*0.4            = 0.8
      1 Hanzi -> 0.4 -> floored at 0.5
      no Hanzi, len('international')//4 = 3 -> 3*0.4 = 1.2
    """
    from word_video.domain import rhythm
    cases = [('承诺诺言誓言', 2.4), ('苹果树', 1.2), ('苹果', 0.8), ('苹', 0.5), ('', 1.2)]
    for spoken, expected in cases:
        record = Record(id='r', word='international', spoken_meaning=spoken)
        assert math.isclose(rhythm.chinese_minimum_seconds(record), expected,
                            rel_tol=1e-12, abs_tol=1e-12), spoken


def test_stage_frames_match_the_verified_engine_exactly():
    """The same floor *and* the same frame decision as ``timing._stage_frames``.

    25 Hanzi counts x 3 rates x 4 source lengths x 3 speeds: the new model must
    reserve exactly the frames the verified engine reserved, including the
    engine's double rounding at a frame boundary.
    """
    from word_video.domain import rhythm

    class _Asset:
        path = 'unused.wav'
        voice = ''
        rendered_duration_s = 0.0

    checked = 0
    for count in range(0, 25):
        record = Record(id='r', word='international', spoken_meaning='苹' * count)
        entry = WordEntry(index=1, word=record.word, phonetic='', meaning='',
                          spoken_meaning=record.spoken_meaning)
        for fps in (24, 25, 30, 50, 60):
            for duration in (0.3, 1.15, 0.85, 3.275):
                for speed in (1.0, 1.25, 1.5):
                    asset = _Asset()
                    asset.duration_s = duration
                    engine_minimum = max(
                        timing.chinese_minimum_duration(entry, 0.4, 0.2),
                        asset.duration_s + 0.1)
                    engine = timing._stage_frames(engine_minimum, asset, speed, fps)
                    mine = rhythm.stage_duration_ticks(
                        'chinese', record, Fraction(duration), gap_s=0.1, speed=speed,
                        fps_num=fps)
                    assert mine == engine * (720000 // fps), (count, fps, duration, speed)
                    checked += 1
    assert checked == 25 * 5 * 4 * 3


def test_frame_rate_conversions_are_hand_computed():
    # 30 fps: 1 frame = 24000 ticks; the same 5.5 s lesson is 165 frames.
    at30 = golden_project(fps_num=30)
    assert [(clip.id, clip.start.ticks, clip.duration_ticks) for clip in at30.clips
            if clip.record_id == 'w1' and clip.role in TEACHING_STAGES] == [
        ('w1.female', 720000, 720000),      # 30 frames
        ('w1.male', 1440000, 576000),       # 24 frames
        ('w1.chinese', 2016000, 1944000),   # 81 frames
    ]
    assert at30.intro_ticks == 720000       # 30 frames
    # 30000/1001 (29.97): 1 frame = 720000*1001/30000 = 24024 ticks exactly.
    at2997 = golden_project(fps_num=30000, fps_den=1001)
    assert [(clip.id, clip.start.ticks, clip.duration_ticks) for clip in at2997.clips
            if clip.record_id == 'w1' and clip.role in TEACHING_STAGES] == [
        ('w1.female', 720720, 720720),      # ceil(1.0 * 30000/1001) = 30 frames
        ('w1.male', 1441440, 576576),       # ceil(0.8 * 30000/1001) = 24 frames
        ('w1.chinese', 2018016, 1945944),   # ceil(2.7 * 30000/1001) = 81 frames
    ]
    assert at2997.intro_ticks == 720720
    total = sum(clip.duration_ticks for clip in at2997.clips if clip.id == 'layer.title')
    assert total == 3963960                 # 165 frames, 5.5055 s
    assert ticks_to_milliseconds(3963960) == 5506


def test_chinese_floor_wins_and_lands_on_the_frame_grid():
    """A 3-Hanzi meaning floors at 1.2 s, so 0.96 s after speed → whole frames."""
    record = Record(id='w2', word='apple', spoken_meaning='苹果树')   # 3 Hanzi
    media = {'w2:female': MediaInfo('w2:female', 9600),   # 0.2 s
             'w2:male': MediaInfo('w2:male', 9600),
             'w2:chinese': MediaInfo('w2:chinese', 9600)}
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media,
                          golden_base(fps_num=30))
    # floor = 3*0.4 = 1.2 s beats media+gap = 0.3 s; 1.2 / 1.25 = 0.96 s
    # 30 fps: ceil(0.96 * 30) = ceil(28.8) = 29 frames = 696000 ticks
    assert project.clip('w2.chinese').duration_ticks == 696000
    at60 = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media, golden_base())
    # 60 fps: ceil(0.96 * 60) = ceil(57.6) = 58 frames = 696000 ticks — the same
    # instant as the 30 fps case, a different frame count.
    assert at60.clip('w2.chinese').duration_ticks == 696000
    # The English floors are exact: max(1.0, 0.2+0.1) = 1.0 / 1.25 = 0.8 s, which
    # is 24 frames at 30 fps and 48 at 60 fps — 576000 ticks either way.
    assert project.clip('w2.female').duration_ticks == 576000
    assert at60.clip('w2.female').duration_ticks == 576000


def test_source_timebase_conversions_are_hand_computed():
    # 48 kHz: one sample is 15 ticks, so 48000 samples are exactly 1.0 s.
    assert TimeExpr.from_units(1, 1, 48000).ticks == 15
    assert TimeExpr.from_units(48000, 1, 48000).ticks == 720000
    # 44.1 kHz: one sample is 800/49 tick = 16.326..., half up 16; 3 samples
    # (2400/49 = 48.98) -> 49; 4410 samples are exactly 0.1 s = 72000 ticks.
    assert TimeExpr.from_units(1, 1, 44100).ticks == 16
    assert TimeExpr.from_units(3, 1, 44100).ticks == 49
    assert TimeExpr.from_units(4410, 1, 44100).ticks == 72000
    # 30000/1001 video: one frame is exactly 24024 ticks.
    assert TimeExpr.from_units(1, 1001, 30000).ticks == 24024
    slice_ = MediaSlice('a', 0, 4410, 1, 44100)
    assert slice_.seconds == Fraction(4410, 44100)
    assert slice_.duration_ticks == 72000
    assert MediaSlice('a', 0, 4410, 1, 44100, speed=1.25).duration_ticks == 57600


# ---------------------------------------------------------------------------
# Refusals.  Each case is the smallest document that must fail, and the code is
# asserted so the CLI/UI can act on it.
# ---------------------------------------------------------------------------
def test_duplicate_clip_and_record_ids_fail(project, media):
    twin = Clip(id='w1.english', role='english', record_id='w1', start=TimeExpr.at(0),
                duration_ticks=12000)
    broken = project.with_clips(project.clips + (twin,))
    with pytest.raises(DuplicateIdError) as error:
        render_plan(broken, media)
    assert error.value.code == 'DUPLICATE_ID'
    assert error.value.path == 'clip:w1.english'
    broken = project.with_records(project.records + (golden_record(),))
    with pytest.raises(DuplicateIdError) as error:
        render_plan(broken, media)
    assert error.value.code == 'DUPLICATE_ID'


def test_dangling_record_and_clip_references_fail(project, media):
    orphan = Clip(id='w9.female', role='female', record_id='w9', start=TimeExpr.at(0),
                  duration_ticks=12000, source=MediaSlice('w9:female', 0, 48000))
    broken = project.with_clips(project.clips + (orphan,))
    with pytest.raises(DanglingRefError) as error:
        render_plan(broken, media)
    assert error.value.code == 'DANGLING_REF'
    assert error.value.path == 'clip:w9.female'

    bound = project.with_clips(tuple(
        Clip(id=clip.id, role=clip.role, record_id=clip.record_id,
             start=TimeExpr.after('w1.ghost') if clip.id == 'w1.male' else clip.start,
             duration_ticks=clip.duration_ticks, text=clip.text, source=clip.source)
        for clip in project.clips))
    with pytest.raises(DanglingRefError) as error:
        render_plan(bound, media)
    assert error.value.code == 'DANGLING_REF'
    assert error.value.path == 'clip:w1.male'


def test_time_dependency_cycle_fails(project, media):
    # male follows chinese, chinese follows male: the smallest 2-clip cycle.
    clips = []
    for clip in project.clips:
        if clip.id == 'w1.male':
            clip = Clip(id=clip.id, role=clip.role, record_id=clip.record_id,
                        start=TimeExpr.after('w1.chinese'),
                        duration_ticks=clip.duration_ticks, text=clip.text,
                        source=clip.source)
        elif clip.id == 'w1.chinese':
            clip = Clip(id=clip.id, role=clip.role, record_id=clip.record_id,
                        start=TimeExpr.after('w1.male'),
                        duration_ticks=clip.duration_ticks, text=clip.text,
                        source=clip.source)
        clips.append(clip)
    with pytest.raises(CycleError) as error:
        render_plan(project.with_clips(clips), media)
    assert error.value.code == 'CYCLE'
    assert error.value.path in ('clip:w1.male', 'clip:w1.chinese')


def test_unknown_media_length_fails(project):
    # A speech clip may not be solved from an estimate: the asset is not measured.
    partial = {'w1:female': MediaInfo('w1:female', 55200)}
    with pytest.raises(UnknownDurationError) as error:
        render_plan(project, partial)
    assert error.value.code == 'UNKNOWN_DURATION'
    assert error.value.path == 'clip:w1.male'


def test_source_window_outside_the_media_fails(project, media):
    over = dict(media)
    over['w1:female'] = MediaInfo('w1:female', 55200 - 1)
    with pytest.raises(SourceRangeError) as error:
        render_plan(project, over)
    assert error.value.code == 'SOURCE_RANGE'
    # One unit short of the slice is already outside: no silent clamp, no guess.
    assert 'w1:female' in error.value.message


def test_unknown_frame_rate_is_refused_not_rounded():
    with pytest.raises(UnsupportedRateError) as error:
        golden_project(fps_num=7)
    assert error.value.code == 'UNSUPPORTED_RATE'


def test_teaching_overlap_is_refused_with_a_one_tick_counterexample(project, media):
    """Smallest counterexample: the Chinese stage starts 1 tick before the male ends."""
    trimmed = apply(project, TrimClip('w1.chinese', start_ticks=CHINESE[0] - 1))
    assert trimmed.project.clip('w1.chinese').start.ticks == CHINESE[0] - 1
    with pytest.raises(TeachingOverlapError) as error:
        cue_plan(trimmed.project, media)
    assert error.value.code == 'TEACHING_OVERLAP'
    assert (error.value.path, 'w1.chinese' in str(error.value)) == ('clip:w1.male', True)
    # The same document still solves as a mix, but the overlap is reported, never
    # silently accepted.
    plan = render_plan(trimmed.project, media)
    assert [conflict.code for conflict in plan.conflicts] == ['OVERLAP_ACCEPTED']
    assert plan.conflicts[0].other_path == 'clip:w1.chinese'


def test_teaching_order_and_missing_roles_are_refused(project, media):
    swapped = tuple(clip for clip in project.clips if clip.id != 'w1.male')
    with pytest.raises(MissingRoleError) as error:
        cue_plan(project.with_clips(swapped), media)
    assert error.value.code == 'MISSING_ROLE'
    assert error.value.path == 'record:w1'

    reversed_order = apply(project, TrimClip('w1.chinese', start_ticks=FEMALE[0],
                                             end_ticks=FEMALE[1]))
    # chinese now sits exactly where the female stage is: the order check names
    # the real problem instead of letting the overlap speak first.
    with pytest.raises(TeachingOrderError) as error:
        cue_plan(reversed_order.project, media)
    assert error.value.code == 'TEACHING_ORDER'
    assert error.value.path == 'record:w1'


def test_two_clips_claiming_one_role_are_refused(project, media):
    twin = Clip(id='w1.english2', role='english', record_id='w1', start=TimeExpr.at(0),
                duration_ticks=TOTAL_TICKS)
    with pytest.raises(AmbiguousRoleError) as error:
        cue_plan(project.with_clips(project.clips + (twin,)), media)
    assert error.value.code == 'ROLE_AMBIGUOUS'


def test_a_trim_shorter_than_the_voice_is_reported_not_silent(project, media):
    """Trimming the target does not cut the voice behind the user's back."""
    short = apply(project, TrimClip('w1.female', end_ticks=FEMALE[0] + 24000))
    clip = short.project.clip('w1.female')
    assert clip.duration_ticks == 24000                       # 0.2 s
    assert clip.source.seconds == Fraction(55200, 48000)      # source untouched
    assert any('截断' in note for note in short.notes)
    plan = render_plan(short.project, media)
    assert [conflict.code for conflict in plan.conflicts] == ['SOURCE_TRUNCATED']
    # A shorter stage leaves a gap instead of an overlap, so the teaching
    # delivery is still well formed: 01 covers female start → male start.
    cues = cue_plan(short.project, media)
    first = cues.by_track()['01'][0]
    assert (first.start_ticks, first.end_ticks) == (FEMALE[0], MALE[0])
