"""Splitting a clip: one media layer becomes two, and only where that is legal.

W06's editor could move and trim clips but not cut one, so "拆分" was impossible
through ``apply()`` — and a UI that builds its own ``Clip`` would have no revision,
no undo entry and no validation.  This file pins both halves of the answer: the
command *works* for the layers where two clips are meaningful, and it *refuses* the
roles where they are not, with the same code ``can_split`` hands a button.

Why the refusal is real and not a policy preference:

* a per-record role (a reading stage or a text layer) exists once per word;
  :func:`word_video.domain.compile._record_items` refuses a second one as
  ``ROLE_AMBIGUOUS``, so a split would make the lesson undeliverable;
* title/subtitle/footer are drawn as one window over the whole timeline, which the
  manifest projection verifies — two windows have nowhere to go.
"""
from dataclasses import replace
from fractions import Fraction

import pytest

from test_wv_project_golden import golden_base, golden_media, golden_record

from word_video.application import (BindStart, SPLITTABLE_ROLES, Session, SplitClip,
                                    apply, can_split, instantiate)
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, Clip, ClipBoundError,
                               DanglingRefError, DuplicateIdError, IntroMeasurement,
                               InvalidTimeError, MediaSlice, SplitNotAllowedError,
                               TimeExpr, cue_plan, render_plan, solve)

VIDEO = 'D:/fixtures/intro.mp4'


def background_project():
    """A golden lesson with a background layer to cut.

    The source window covers the whole target span (1 s = 720000 ticks = 48000
    units at speed 1), because a cut has to land inside the media, not past it.
    """
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), golden_media(),
                          golden_base())
    span = project.clip('layer.title').duration_ticks
    return project.with_clips(tuple(project.clips) + (
        Clip(id='layer.background', role='background', start=TimeExpr.at(0),
             duration_ticks=span, source=MediaSlice('bg', 0, span // 15, 1, 48000)),))


def background_source_units(project):
    return project.clip('layer.background').source.source_end


def intro_project():
    template = replace(DEFAULT_LESSON_TEMPLATE, intro=True)
    measurement = IntroMeasurement(asset_id=VIDEO, seconds=1.0, sound_asset=VIDEO,
                                   sound_source='FROM_CLIP', from_clip=True,
                                   picture_seconds=1.0)
    return instantiate(template, (golden_record(),), golden_media(), golden_base(),
                       intro=MediaSlice(VIDEO, 0, 48000),
                       intro_measure=measurement), measurement


def _ticks(project):
    return {clip.id: (clip.start.ticks, clip.duration_ticks, clip.source)
            for clip in project.clips}


def test_splitting_a_background_layer_keeps_every_other_object_untouched():
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    cut = total // 3
    before = _ticks(project)
    result = apply(project, SplitClip('layer.background', cut))
    assert result.changed is True
    assert result.moved == ('layer.background', 'layer.background.2')
    assert result.project.revision == project.revision + 1
    assert any('拆成' in note for note in result.notes)
    after = _ticks(result.project)
    left = result.project.clip('layer.background')
    right = result.project.clip('layer.background.2')
    assert (left.start.ticks, left.duration_ticks) == (0, cut)
    assert (right.start.ticks, right.duration_ticks) == (cut, total - cut)
    # The two halves are contiguous and cover exactly what the one clip covered.
    assert left.start.ticks + left.duration_ticks == right.start.ticks
    assert right.start.ticks + right.duration_ticks == total
    for clip_id, ticks in before.items():          # per object, not an aggregate
        if clip_id == 'layer.background':
            continue
        assert after[clip_id] == ticks, clip_id
    # Same picture, same times: a split does not move anything in the plan.
    media = golden_media()
    assert render_plan(result.project, media).total_ticks == \
        render_plan(project, media).total_ticks
    assert 'layer.background.2' in [item.clip_id
                                    for item in render_plan(result.project, media).video]


def test_a_split_cuts_the_source_window_on_its_own_grid():
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    cut = total // 4
    split = apply(project, SplitClip('layer.background', cut)).project
    left = split.clip('layer.background').source
    right = split.clip('layer.background.2').source
    assert left.source_start == 0
    assert right.source_start == left.source_end          # contiguous, no gap
    assert right.source_end == background_source_units(project)   # nothing invented
    # The cut lands on the unit nearest the requested tick, half up.
    assert left.source_end == int(Fraction(cut * 48000, 720000) + Fraction(1, 2))
    # Both halves keep the clip's playback speed, so the mixer stays in step.
    assert left.speed == right.speed == 1.0


def test_a_second_split_takes_the_next_free_id():
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    once = apply(project, SplitClip('layer.background', total // 2)).project
    twice = apply(once, SplitClip('layer.background.2', total // 4 * 3)).project
    assert {clip.id for clip in twice.clips if clip.role == 'background'} == \
        {'layer.background', 'layer.background.2', 'layer.background.2.2'}
    # An explicitly named right half is honoured, and a taken id is refused.
    named = apply(project, SplitClip('layer.background', total // 2,
                                     new_clip_id='layer.bg-right')).project
    assert named.clip('layer.bg-right').duration_ticks == total - total // 2
    with pytest.raises(DuplicateIdError):
        apply(project, SplitClip('layer.background', total // 2,
                                 new_clip_id='layer.background'))


def test_un_deliverable_roles_are_refused_before_anything_changes():
    project = background_project()
    reasons = {}
    for clip in project.clips:
        if clip.role == 'background':
            continue
        check = can_split(clip)
        assert check.ok is False, clip.id
        reasons[clip.role] = check.code
        with pytest.raises((SplitNotAllowedError, ClipBoundError)) as error:
            apply(project, SplitClip(clip.id, clip.start.ticks + 12000))
        assert error.value.path == 'clip:%s' % clip.id
        assert error.value.hint
    # Per-record roles and the whole-timeline text layers are both refused, and the
    # message says which of the two reasons applies.
    assert reasons['female'] == 'SPLIT_NOT_APPLICABLE'
    assert reasons['english'] == 'SPLIT_NOT_APPLICABLE'
    assert reasons['title'] == 'SPLIT_NOT_APPLICABLE'
    assert 'per-record' in can_split(project.clip('w1.female')).reason
    assert 'one window' in can_split(project.clip('layer.title')).reason
    assert SPLITTABLE_ROLES == ('background',)
    # Nothing changed: the refusal is a refusal, not a partial edit.
    assert _ticks(project) == _ticks(background_project())


def test_a_linked_clip_is_refused_for_the_same_reason_the_ui_shows():
    linked = apply(background_project(),
                   BindStart('layer.background', 'w1.chinese')).project
    check = can_split(linked.clip('layer.background'))
    assert check.ok is False and check.code == ClipBoundError.code
    with pytest.raises(ClipBoundError) as error:
        apply(linked, SplitClip('layer.background', 60000))
    assert error.value.code == 'CLIP_BOUND'
    assert error.value.path == 'clip:layer.background'


def test_the_cut_must_fall_strictly_inside_the_clip():
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    for at in (0, total, total + 1, -1):
        with pytest.raises(InvalidTimeError) as error:
            apply(project, SplitClip('layer.background', at))
        assert error.value.code == 'INVALID_TIME'
    with pytest.raises(InvalidTimeError):
        apply(project, SplitClip('layer.background', 'halfway'))
    with pytest.raises(DanglingRefError):
        apply(project, SplitClip('layer.ghost', 12000))


def test_an_explicit_length_is_what_a_split_needs():
    """A clip whose length still follows the media has no cut point yet."""
    project = background_project()
    open_ended = project.with_clips(tuple(
        replace(item, duration_ticks=None) if item.id == 'layer.background' else item
        for item in project.clips))
    with pytest.raises(InvalidTimeError) as error:
        apply(open_ended, SplitClip('layer.background', 12000))
    assert 'nothing to cut' in error.value.message


def test_undo_restores_the_single_clip_exactly():
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    before = _ticks(project)
    session = Session(project=project).apply(SplitClip('layer.background', total // 3))
    assert len(session.project.clips) == len(project.clips) + 1
    restored = session.undo().project
    assert _ticks(restored) == before
    assert [clip.id for clip in restored.clips] == [clip.id for clip in project.clips]
    assert restored.revision == project.revision + 2      # undo advances, never rewinds
    assert session.can_undo is True


def test_splitting_an_intro_layer_is_refused_like_the_exporter_refuses_it():
    """A split intro could never be exported, so the button must be dead too.

    The projection draws exactly one countdown stage and raises on a second
    (``exporters/plan.py``); offering the split here would be a button whose only
    outcome is an error at export time.
    """
    project, measurement = intro_project()
    check = can_split(project.clip('layer.intro'))
    assert check.ok is False and check.code == 'SPLIT_NOT_APPLICABLE'
    assert 'countdown' in check.reason
    total = project.clip('layer.intro').duration_ticks
    with pytest.raises(SplitNotAllowedError) as error:
        apply(project, SplitClip('layer.intro', total // 2))
    assert error.value.path == 'clip:layer.intro'
    assert 'set(SPLITTABLE_ROLES)' not in error.value.hint
    assert 'background' in error.value.hint
    # The intro still solves and projects exactly as before: nothing changed for it.
    solution = solve(project, golden_media(), measurement)
    assert [item.clip_id for item in solution.render.video if item.role == 'intro'] == \
        ['layer.intro']


def test_a_split_background_still_has_one_plan_item_per_half():
    """The plan expresses both halves, and the exporter draws both (B's W03 flip)."""
    project = background_project()
    total = project.clip('layer.background').duration_ticks
    split = apply(project, SplitClip('layer.background', total // 2)).project
    items = [item for item in render_plan(split, golden_media()).video
             if item.role == 'background']
    assert [(item.start_ticks, item.end_ticks) for item in items] == \
        [(0, total // 2), (total // 2, total)]
