"""The editor's own state: selection, single-clip moves, trims, undo, notices.

Everything here runs without a display and without a sound device, because these
are the decisions the team already made and they must be checkable on their own:

* a plain click selects **one** clip, and moving it moves nothing else - asserted
  per object on the solved plan, not on an aggregate;
* a group move is explicit and the followers a command knows about are **named**,
  never carried along silently;
* an edit that A's commands refuse comes back as a notice located at the word and
  the role, with the repair that lifts it;
* split is refused rather than faked: the editor does not own a second way to
  build a ``Clip``;
* undo restores every object, mode switching changes no business data, and save /
  reopen keep the edits.
"""
import pytest

from desktop import editor_notices as notices
from desktop.editor_model import EditorState, nearest_candidate, snap_to_grid
from test_editor_support import revision_snapshot, solved_snapshot, tiny_state
from test_preview_support import scratch

from test_wv_project_golden import CHINESE, FEMALE, MALE, golden_media, golden_project

STEP = 144000          # 0.2 s
FRAME = 12000          # one frame at 60 fps


@pytest.fixture
def state():
    return EditorState(golden_project(), media=golden_media())


@pytest.fixture
def on_disk():
    with scratch('editor-model-') as folder:
        yield tiny_state(folder)


def one_clip_changed(before, after, clip_id):
    """Every entry but ``clip_id`` is identical; the one named did change."""
    assert set(before) == set(after)
    for key, value in before.items():
        if key != clip_id:
            assert after[key] == value, key
    assert after[clip_id] != before[clip_id]


# ---------------------------------------------------------------- selection
def test_a_plain_click_selects_exactly_one_clip(state):
    assert state.selection == ()
    state.select('w1.male')
    assert state.selection == ('w1.male',)
    state.select('w1.chinese')
    assert state.selection == ('w1.chinese',)      # replaced, not extended


def test_ctrl_click_is_the_explicit_way_to_build_a_group(state):
    state.select('w1.male')
    state.toggle('w1.chinese')
    assert state.selection == ('w1.male', 'w1.chinese')
    state.toggle('w1.male')
    assert state.selection == ('w1.chinese',)


def test_the_selection_drops_ids_this_revision_no_longer_has(state):
    state.select('w1.male')
    assert state.set_selection(('w1.male', 'ghost')) == ('w1.male',)


# ------------------------------------------------------------------- moves
def test_moving_the_selected_clip_moves_nothing_else(state):
    state.select('w1.male')
    before_solved = solved_snapshot(state)
    before = revision_snapshot(state)

    outcome = state.move_selection(STEP)

    assert outcome.changed is True
    assert type(outcome.command).__name__ == 'MoveClip'
    assert outcome.moved == ('w1.male',)
    assert outcome.notices == ()                    # nothing followed silently
    one_clip_changed(before, revision_snapshot(state), 'w1.male')
    after_solved = solved_snapshot(state)
    one_clip_changed(before_solved, after_solved, 'w1.male')
    assert after_solved['w1.male'] == (MALE[0] + STEP, MALE[1] + STEP)
    assert state.revision == 1


def test_a_linked_clip_that_follows_is_named_in_the_notice(state):
    """The one explicit linkage, and the message that says who else will move."""
    state.select('layer.subtitle')
    bound = state.bind_start('layer.subtitle', 'w1.female', edge='end')
    assert bound.changed is True
    assert any('跟随' in notice.message for notice in bound.notices)

    state.select('w1.female')
    outcome = state.move_selection(STEP)
    assert type(outcome.command).__name__ == 'MoveClip'
    followers = '；'.join(notice.message for notice in outcome.notices)
    assert 'layer.subtitle' in followers and 'w1.female' in followers
    # The follower really did move: the notice is not decoration.
    assert solved_snapshot(state)['layer.subtitle'][0] == FEMALE[1] + STEP


def test_an_explicit_group_move_moves_exactly_the_group(state):
    state.select('w1.male')
    state.toggle('w1.chinese')
    before = revision_snapshot(state)
    outcome = state.move_selection(STEP)
    assert type(outcome.command).__name__ == 'MoveClips'
    assert set(outcome.moved) == {'w1.male', 'w1.chinese'}
    after = revision_snapshot(state)
    for clip_id, value in before.items():
        if clip_id not in ('w1.male', 'w1.chinese'):
            assert after[clip_id] == value, clip_id
    assert after['w1.male'] != before['w1.male']
    assert after['w1.chinese'] != before['w1.chinese']


def test_a_drag_is_clamped_at_zero_and_rounded_to_whole_frames(state):
    state.select('w1.male')
    state.move_selection(-(MALE[0] + 5000))        # asks to cross zero by 5000 ticks
    assert solved_snapshot(state)['w1.male'][0] == 0
    assert state.move_selection(-10 ** 9).changed is False   # already at zero
    state.select('w1.chinese')
    state.move_selection(FRAME + 1)                # snapped to one whole frame
    assert solved_snapshot(state)['w1.chinese'][0] == CHINESE[0] + FRAME


def test_a_clamped_move_uses_the_solved_start_not_a_link_offset(state):
    state.select('w1.male')
    state.bind_start('w1.male', 'w1.female', edge='end', offset_ticks=STEP)
    state.select('w1.male')
    state.move_selection(-(MALE[0] + STEP))
    # A linked clip has no absolute tick of its own, so the clamp has to use where
    # it actually is (female's end plus its offset); using the raw offset would
    # refuse a legal move.
    assert solved_snapshot(state)['w1.male'][0] == 0


# ------------------------------------------------------------------ trims
def test_trim_then_undo_restores_every_object(state):
    before = revision_snapshot(state)
    before_solved = solved_snapshot(state)

    outcome = state.trim('w1.female', 'end', FEMALE[0] + 24000)
    assert outcome.changed is True
    assert solved_snapshot(state)['w1.female'] == (FEMALE[0], FEMALE[0] + 24000)
    # The source is untouched: trimming the target never cuts the recording.
    assert state.project.clip('w1.female').source.source_end == 55200
    assert any('截断' in notice.message for notice in outcome.notices)

    state.undo()
    assert revision_snapshot(state) == before
    assert solved_snapshot(state) == before_solved
    assert state.revision == 2                     # undo moves the revision forward
    state.redo()
    assert solved_snapshot(state)['w1.female'] == (FEMALE[0], FEMALE[0] + 24000)


def test_trimming_a_linked_clip_offers_unbinding_first(state):
    state.select('w1.male')
    state.bind_start('w1.male', 'w1.female')
    before = revision_snapshot(state)
    outcome = state.trim('w1.male', 'start', MALE[0] + STEP)
    assert outcome.changed is False
    assert outcome.notices[0].code == 'CLIP_BOUND'
    assert notices.ACTION_UNBIND in [action.kind for action in outcome.notices[0].actions]
    assert revision_snapshot(state) == before      # refused, not half applied
    # The offered repair works, and it is a real command.
    state.unbind_start('w1.male')
    assert state.project.clip('w1.male').start.is_absolute
    assert state.trim('w1.male', 'start', MALE[0] + STEP).changed is True


def test_restoring_a_truncated_reading_clears_the_conflict(state):
    state.trim('w1.female', 'end', FEMALE[0] + 24000)
    conflicts = [notice for notice in state.document_notices()
                 if notice.code == 'SOURCE_TRUNCATED']
    assert conflicts, 'a target shorter than the voice must be reported'
    action = next(item for item in conflicts[0].actions
                  if item.kind == notices.ACTION_TRIM_TO_SOURCE)
    # 55200 samples at 48 kHz is 1.15 s of source, and the target length is that
    # window on the project grid with speed applied exactly once: 662400 ticks.
    assert action.ticks == 662400 == 55200 * 720000 // 48000 * 4 // 5
    state.trim_to_source_length('w1.female')
    assert not [notice for notice in state.document_notices()
                if notice.code == 'SOURCE_TRUNCATED']


def test_a_trim_leaves_a_gap_instead_of_an_error(state):
    state.trim('w1.female', 'end', FEMALE[0] + 24000)
    ranges = solved_snapshot(state)
    assert ranges['w1.male'][0] > ranges['w1.female'][1]   # a real gap, no overlap
    assert state.plan() is not None
    assert state.cues_error is None                        # still deliverable


# ------------------------------------------------------- teaching refusals
def test_an_overlap_is_refused_with_both_clips_named(state):
    state.select('w1.male')
    state.move_selection(STEP)          # male now runs into the Chinese stage
    plan = state.plan()
    assert plan is not None, 'the timeline must survive a teaching problem'
    error = state.cues_error
    assert error is not None and error.code == 'TEACHING_OVERLAP'
    found = [notice for notice in state.document_notices()
             if notice.code == 'TEACHING_OVERLAP']
    assert found, 'the delivery refusal must reach the member'
    notice = found[0]
    assert notice.word == 'promise' and notice.record_id == 'w1' and notice.role
    assert 'promise' in notice.headline()
    targets = [action.clip_id for action in notice.actions
               if action.kind == notices.ACTION_SELECT_CLIP]
    assert len(targets) == 2 and set(targets) == {'w1.male', 'w1.chinese'}
    assert notice.detail['other_clip_id'] == 'w1.chinese'
    assert notice.detail['overlap_ticks'] == STEP
    # The plan itself reports the accepted overlap, for the mix.
    assert [conflict.code for conflict in plan.conflicts] == ['OVERLAP_ACCEPTED']
    warn = [item for item in state.document_notices() if item.code == 'OVERLAP_ACCEPTED']
    assert warn and warn[0].detail.get('other_clip_id') == 'w1.chinese'


def test_a_wrong_teaching_order_names_the_word_and_all_three_stages(state):
    state.trim('w1.chinese', 'start', FEMALE[0], snap=False)
    error = state.cues_error
    assert error is not None and error.code == 'TEACHING_ORDER'
    notice = [item for item in state.document_notices()
              if item.code == 'TEACHING_ORDER'][0]
    assert notice.word == 'promise' and notice.record_id == 'w1'
    targets = {action.clip_id for action in notice.actions}
    assert targets == {'w1.female', 'w1.male', 'w1.chinese'}


def test_missing_roles_are_recomputed_from_the_document_not_parsed(state):
    """No message parsing: the roles come from the clips the record actually has."""
    clips = tuple(clip for clip in state.project.clips
                  if clip.id not in ('w1.male', 'w1.phonetic'))
    broken = EditorState(state.project.with_clips(clips), media=golden_media())
    error = broken.cues_error
    assert error is not None and error.code == 'MISSING_ROLE'
    notice = broken.document_notices()[0]
    assert notice.record_id == 'w1' and notice.word == 'promise'
    assert notice.detail['missing_roles'] == ['male', 'phonetic']
    assert '男声英文' in notice.message and '音标' in notice.message


def test_duplicate_roles_are_recomputed_from_the_document(state):
    from dataclasses import replace
    twin = replace(state.project.clip('w1.english'), id='w1.english2')
    broken = EditorState(state.project.with_clips(state.project.clips + (twin,)),
                         media=golden_media())
    error = broken.cues_error
    assert error is not None and error.code == 'ROLE_AMBIGUOUS'
    notice = broken.document_notices()[0]
    assert notice.detail['duplicate_roles'] == {'english': ['w1.english', 'w1.english2']}


# ------------------------------------------------------------------- split
def test_split_is_refused_for_a_per_record_role_and_builds_no_clip(state):
    """A reading stage exists once per word, so a split is refused up front.

    The code is A's ``SPLIT_NOT_APPLICABLE`` (from ``can_split``, which the button
    state also uses), and the editor neither fabricates the second clip nor bumps
    the revision - it is a request that was answered "no".
    """
    before = revision_snapshot(state)
    revision = state.revision
    outcome = state.split_at_ticks('w1.chinese', CHINESE[0] + 24000)
    assert outcome.changed is False
    assert state.revision == revision
    assert revision_snapshot(state) == before      # nothing was fabricated
    notice = outcome.notices[0]
    assert notice.code == 'SPLIT_NOT_APPLICABLE'
    assert notice.word == 'promise' and notice.role == 'chinese'
    assert '每个词' in notice.message or 'per-record' in notice.message
    check = state.split_check('w1.chinese')
    assert check is not None and check.ok is False
    assert check.code == notice.code               # button and message agree


def test_only_the_media_project_layers_are_offered_as_splittable(state):
    """The rule lives in A; the editor only asks it, role by role."""
    offered = {clip.role: state.split_check(clip.id).ok for clip in state.project.clips}
    assert offered['title'] is False and offered['footer'] is False
    assert offered['subtitle'] is False
    assert offered['female'] is False and offered['english'] is False
    assert offered['male'] is False and offered['chinese'] is False
    # This golden lesson has no intro layer, so nothing is splittable in it at all.
    assert not any(offered.values()), offered


# ------------------------------------------------------- mode and storage
def test_switching_mode_three_times_changes_no_business_data(state):
    state.select('w1.female')
    state.move_selection(STEP)
    before = state.project.to_dict()
    for mode in ('advanced', 'template', 'advanced', 'template'):
        state.set_mode(mode)
        assert state.project.to_dict() == before
    assert state.revision == 1


def test_save_then_reopen_keeps_the_edit_and_leaves_no_temp_file(on_disk):
    folder, state = on_disk
    state.select('w1.male')
    state.move_selection(STEP)
    moved = solved_snapshot(state)['w1.male'][0]
    assert state.dirty is True
    state.save()
    assert state.dirty is False
    leftovers = [path.name for path in folder.path.iterdir() if path.name.startswith('.')]
    assert leftovers == [], leftovers

    from word_video.application.session import Session
    reopened = Session.open(folder.project_path)
    assert reopened.project.revision == state.revision
    assert reopened.project.clip('w1.male').start.ticks == moved
    assert reopened.project.to_dict() == state.project.to_dict()


def test_reload_throws_away_unsaved_edits(on_disk):
    folder, state = on_disk
    state.mark_saved()
    original = state.project.clip('w1.male').start.ticks
    state.select('w1.male')
    state.move_selection(STEP)
    assert state.dirty is True
    assert state.reload() is True
    assert state.dirty is False
    assert state.project.clip('w1.male').start.ticks == original


def test_undo_and_redo_never_move_the_revision_backwards(state):
    state.select('w1.male')
    state.move_selection(STEP)
    state.move_selection(STEP)
    revisions = [state.revision]
    for _ in range(2):
        state.undo()
        revisions.append(state.revision)
    for _ in range(2):
        state.redo()
        revisions.append(state.revision)
    assert revisions == [2, 3, 4, 5, 6]


def test_undo_with_nothing_to_undo_is_a_no_op(state):
    assert state.undo().changed is False
    assert state.redo().changed is False
    assert state.revision == 0


# ------------------------------------------------------------------ timing
def test_snapping_helpers_are_hand_computed():
    assert snap_to_grid(12001, 12000) == 12000
    assert snap_to_grid(6000, 12000) == 12000          # exactly half a frame, up
    assert snap_to_grid(5999, 12000) == 0
    assert snap_to_grid(0, 12000) == 0
    assert nearest_candidate(1000, [0, 900, 5000]) == 900
    assert nearest_candidate(1000, [0, 900, 5000], tolerance=50) == 1000    # too far
    assert nearest_candidate(0, [4], tolerance=4) == 4
    assert nearest_candidate(1000, [0]) == 0       # the default reach is 0.02 s


def test_candidates_include_clip_edges_whole_seconds_and_both_ends(state):
    points = state.candidates()
    assert FEMALE[0] in points and CHINESE[1] in points
    assert 0 in points and state.total_ticks() in points
    assert 720000 in points                                        # one second
    # 2.8 s is the Chinese stage's own edge and not a whole second, so excluding
    # every clip proves the edges - not just the ruler - are being enumerated.
    every = tuple(clip.id for clip in state.project.clips)
    without_clips = state.candidates(exclude=every)
    assert CHINESE[0] in points and CHINESE[0] not in without_clips
    assert 720000 in without_clips                                 # the ruler stays
    assert set(without_clips) <= {0, state.total_ticks()} | {
        index * 720000 for index in range(7)}


def test_seconds_and_ticks_round_trip_through_the_frame_grid(state):
    ticks = state.ticks_from_seconds(1.0)
    assert ticks == 720000
    assert state.seconds(ticks) == 1.0
    assert state.ticks_from_seconds(0.2001) % FRAME == 0           # snapped by default
