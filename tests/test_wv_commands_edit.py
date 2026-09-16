"""Edit commands: local moves, explicit linkage, undo/redo and revision checks.

The behaviour these tests pin is the team decision: selecting one clip moves that
clip only; whole-group movement and following are explicit operations; undo and
redo move the revision forward instead of rewinding it.
"""
import pytest

from test_wv_project_golden import (CHINESE, FEMALE, MALE, golden_media, golden_project)

from word_video.application import (BindStart, MODE_ADVANCED, MoveClip, MoveClips,
                                    Session, SetMediaSlice, TrimClip, UnbindStart,
                                    apply)
from word_video.domain import (ClipBoundError, CycleError, DanglingRefError,
                               InvalidTimeError, MediaInfo, StaleRevisionError,
                               TeachingOverlapError, TimeExpr, UnknownCommandError,
                               cue_plan, render_plan)

STEP = 144000          # 0.2 s at 720000 tick/s
FRAME = 12000          # one frame at 60 fps


@pytest.fixture
def media():
    return golden_media()


@pytest.fixture
def project():
    return golden_project()


def _ticks(project):
    return {clip.id: (clip.start.ticks, clip.duration_ticks) for clip in project.clips}


def _plan_ticks(plan):
    return {item.clip_id: (item.start_ticks, item.end_ticks)
            for item in plan.video + plan.audio}


def test_moving_the_male_clip_moves_nothing_else(project, media):
    before = _ticks(project)
    before_plan = _plan_ticks(render_plan(project, media))
    result = apply(project, MoveClip('w1.male', STEP))
    assert result.changed is True
    assert result.moved == ('w1.male',)
    assert result.notes == ()                       # nothing followed silently
    assert result.project.revision == project.revision + 1

    after = _ticks(result.project)
    for clip_id, ticks in before.items():           # per object, not an aggregate
        if clip_id == 'w1.male':
            continue
        assert after[clip_id] == ticks, clip_id
    assert after['w1.male'] == (MALE[0] + STEP, MALE[1] - MALE[0])

    after_plan = _plan_ticks(render_plan(result.project, media))
    for clip_id, ticks in before_plan.items():
        if clip_id == 'w1.male':
            continue
        assert after_plan[clip_id] == ticks, clip_id
    assert after_plan['w1.male'] == (MALE[0] + STEP, MALE[1] + STEP)

    # 0.2 s later, the male reading now overlaps the Chinese stage: the teaching
    # delivery is refused with both clips named, the mix still solves.
    with pytest.raises(TeachingOverlapError) as error:
        cue_plan(result.project, media)
    assert error.value.path == 'clip:w1.male'
    plan = render_plan(result.project, media)
    assert [conflict.code for conflict in plan.conflicts] == ['OVERLAP_ACCEPTED']


def test_group_move_is_an_explicit_operation(project, media):
    before = _ticks(project)
    result = apply(project, MoveClips(('w1.male', 'w1.phonetic'), STEP))
    assert result.moved == ('w1.male', 'w1.phonetic')
    after = _ticks(result.project)
    for clip_id, ticks in before.items():
        if clip_id in result.moved:
            assert after[clip_id] == (ticks[0] + STEP, ticks[1]), clip_id
        else:
            assert after[clip_id] == ticks, clip_id


def test_trim_sets_the_target_range_and_leaves_the_voice_alone(project, media):
    shorter = CHINESE[1] - FRAME
    result = apply(project, TrimClip('w1.chinese', end_ticks=shorter))
    clip = result.project.clip('w1.chinese')
    assert (clip.start.ticks, clip.duration_ticks) == (CHINESE[0], shorter - CHINESE[0])
    assert clip.source == project.clip('w1.chinese').source    # source window intact
    assert result.moved == ('w1.chinese',)
    # A shorter stage leaves a gap: the teaching delivery still solves.
    cues = cue_plan(result.project, media).by_track()['05']
    assert (cues[0].start_ticks, cues[0].end_ticks) == (CHINESE[0], shorter)


def test_trimming_a_linked_clip_is_refused_with_a_way_forward(project):
    linked = apply(project, BindStart('w1.chinese', 'w1.male')).project
    with pytest.raises(ClipBoundError) as error:
        apply(linked, TrimClip('w1.chinese', end_ticks=CHINESE[1] - FRAME))
    assert error.value.code == 'CLIP_BOUND'
    assert 'UnbindStart' in error.value.hint
    # The recovery path the hint names really works.
    unbound = apply(linked, UnbindStart('w1.chinese', ticks=CHINESE[0]))
    assert unbound.changed is True
    assert unbound.project.clip('w1.chinese').start == TimeExpr.at(CHINESE[0])
    assert apply(unbound.project, TrimClip('w1.chinese',
                                           end_ticks=CHINESE[1] - FRAME)).changed is True


def test_a_declared_link_makes_the_follower_move_and_says_so(project, media):
    bound = apply(project, BindStart('w1.chinese', 'w1.male')).project
    assert bound.clip('w1.chinese').start == TimeExpr.after('w1.male')
    result = apply(bound, MoveClip('w1.male', STEP))
    assert result.moved == ('w1.male',)
    assert result.notes == ('w1.chinese 跟随 w1.male 的结束端点，会一起移动',)
    # The dependency is what moved the follower; the document says so in advance.
    plan = _plan_ticks(render_plan(result.project, media))
    assert plan['w1.male'] == (MALE[0] + STEP, MALE[1] + STEP)
    assert plan['w1.chinese'] == (CHINESE[0] + STEP, CHINESE[1] + STEP)
    assert plan['w1.female'] == FEMALE                            # untouched
    # Still a well-formed lesson: the chain simply moved 0.2 s later.
    cue_plan(result.project, media)


def test_undo_restores_the_dependency_chain(project, media):
    session = Session(project=project)
    session = session.apply(BindStart('w1.chinese', 'w1.male'))
    session = session.apply(MoveClip('w1.male', STEP))
    moved = _plan_ticks(render_plan(session.project, media))
    assert moved['w1.chinese'] == (CHINESE[0] + STEP, CHINESE[1] + STEP)

    session = session.undo()
    assert session.project.revision == 3
    back = _plan_ticks(render_plan(session.project, media))
    assert back['w1.chinese'] == CHINESE                 # position restored
    assert session.project.clip('w1.chinese').start == TimeExpr.after('w1.male')
    assert session.can_undo is True

    session = session.undo()
    assert session.project.revision == 4
    assert session.project.clip('w1.chinese').start == TimeExpr.at(CHINESE[0])
    assert session.can_undo is False
    assert session.can_redo is True

    session = session.redo()
    assert session.project.revision == 5
    assert session.project.clip('w1.chinese').start == TimeExpr.after('w1.male')


def test_undo_and_redo_never_move_the_revision_backwards(project):
    session = Session(project=project)
    session = session.apply(MoveClip('w1.male', STEP))
    assert session.project.revision == 1
    session = session.undo()
    assert session.project.revision == 2
    assert _ticks(session.project) == _ticks(project)
    session = session.redo()
    assert session.project.revision == 3
    assert session.project.clip('w1.male').start.ticks == MALE[0] + STEP
    # A new command after an undo clears the redo stack.
    session = session.undo().apply(MoveClip('w1.female', FRAME))
    assert session.can_redo is False
    assert session.project.clip('w1.female').start.ticks == FEMALE[0] + FRAME


def test_stale_revision_is_refused(project):
    with pytest.raises(StaleRevisionError) as error:
        apply(project, MoveClip('w1.male', STEP), expected_revision=project.revision - 1)
    assert error.value.code == 'STALE_REVISION'
    result = apply(project, MoveClip('w1.male', STEP),
                   expected_revision=project.revision)
    assert result.project.revision == 1


def test_mode_switch_does_not_change_business_data(project):
    session = Session(project=project)
    advanced = session.with_mode(MODE_ADVANCED)
    assert advanced.mode == MODE_ADVANCED
    assert advanced.project is project
    assert advanced.project.revision == project.revision
    assert advanced.project.to_dict() == project.to_dict()
    from word_video.domain import SchemaError
    with pytest.raises(SchemaError):
        session.with_mode('expert')


def test_no_op_unknown_and_missing_targets(project):
    same = apply(project, MoveClip('w1.male', 0))
    assert same.changed is False and same.project is project
    with pytest.raises(DanglingRefError):
        apply(project, MoveClip('w1.ghost', STEP))
    with pytest.raises(InvalidTimeError):
        apply(project, MoveClips((), STEP))
    with pytest.raises(UnknownCommandError) as error:
        apply(project, 'move everything')
    assert error.value.code == 'UNKNOWN_COMMAND'
    with pytest.raises(CycleError):
        apply(project, BindStart('w1.male', 'w1.male'))
    # Unbinding an absolute clip is simply nothing to do.
    assert apply(project, UnbindStart('w1.male')).changed is False


def test_media_edit_changes_the_source_window_and_re_derives_policy_lengths(project, media):
    # An instantiated clip keeps the length it was given: editing the source
    # window does not silently resize it.
    edited = apply(project, SetMediaSlice('w1.chinese', source_end=48000)).project
    assert edited.clip('w1.chinese').duration_ticks == project.clip('w1.chinese').duration_ticks
    assert edited.clip('w1.chinese').source.source_end == 48000
    assert render_plan(edited, media).item('w1.chinese').duration_ticks == \
        project.clip('w1.chinese').duration_ticks

    # A clip that still follows the rhythm re-derives its length at solve time.
    from dataclasses import replace
    wide_media = dict(media)
    wide_media['w1:male'] = MediaInfo('w1:male', 96000)      # 2.0 s of source
    open_ended = project.with_clips(tuple(
        replace(clip, duration_ticks=None) if clip.id == 'w1.male' else clip
        for clip in project.clips))
    wide = open_ended.with_clips(tuple(
        replace(clip, source=replace(clip.source, source_end=96000))
        if clip.id == 'w1.male' else clip for clip in open_ended.clips))
    # max(1.0, 2.0+0.1) / 1.25 = 1.68 s → ceil(100.8) = 101 frames
    assert render_plan(wide, wide_media).item('w1.male').duration_ticks == 101 * FRAME
    narrow = apply(wide, SetMediaSlice('w1.male', source_end=48000))
    assert any('重算' in note for note in narrow.notes)
    # max(1.0, 1.0+0.1) / 1.25 = 0.88 s → ceil(52.8) = 53 frames
    assert render_plan(narrow.project,
                       wide_media).item('w1.male').duration_ticks == 53 * FRAME
