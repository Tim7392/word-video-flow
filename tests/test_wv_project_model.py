"""Model and plan invariants for ``wv-project@1``.

The point of these tests is the contract other modules depend on: a solved plan is
a read-only value (nothing in it can be edited or written back), solving does not
touch the document, the template expands exactly once, and no malformed number
reaches a document.
"""
import dataclasses
from fractions import Fraction

import pytest

from test_wv_project_golden import golden_base, golden_media, golden_project, golden_record

from word_video.application import MoveClip, apply, instantiate
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, DisplayNode, DuplicateIdError,
                               InvalidTimeError, LessonTemplate, MediaInfo,
                               MediaSlice, Project, Record, SchemaError,
                               SourceRangeError, StageNode, cue_plan, render_plan,
                               solve, ticks_to_microseconds, ticks_to_milliseconds)

_IMMUTABLE = (type(None), bool, int, float, str, Fraction)


def _assert_deep_frozen(value, path='plan'):
    """Every value reachable from a plan must be immutable."""
    if isinstance(value, _IMMUTABLE):
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _assert_deep_frozen(item, '%s[%d]' % (path, index))
        return
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), '__dataclass_params__', None)
        assert params is not None and params.frozen, path
        for field in dataclasses.fields(value):
            _assert_deep_frozen(getattr(value, field.name), '%s.%s' % (path, field.name))
        return
    raise AssertionError('plan holds a mutable value at %s: %r' % (path, type(value)))


@pytest.fixture
def media():
    return golden_media()


@pytest.fixture
def project():
    return golden_project()


def test_plans_are_read_only_values(project, media):
    solution = solve(project, media)
    _assert_deep_frozen(solution.render, 'render')
    _assert_deep_frozen(solution.cues, 'cues')
    with pytest.raises(dataclasses.FrozenInstanceError):
        solution.render.total_ticks = 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        solution.render.audio[0].start_ticks = 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        solution.cues.cues[0].text = 'changed'
    with pytest.raises(TypeError):
        solution.render.audio[0] = None
    # No back reference to the editable document, so a plan cannot write to it.
    for value in vars(solution.render).values():
        assert not isinstance(value, Project)
    for value in vars(solution.cues).values():
        assert not isinstance(value, Project)


def test_solving_never_touches_the_document(project, media):
    before = project.to_dict()
    solution = solve(project, media)
    render_plan(project, media)
    cue_plan(project, media)
    assert project.to_dict() == before
    assert project.revision == 0
    assert solution.render.project_revision == project.revision
    assert solution.cues.project_revision == project.revision


def test_plan_identity_is_stable_and_follows_the_revision(project, media):
    first = solve(project, media)
    second = solve(project, media)
    assert first.render.identity() == second.render.identity()
    assert first.cues.identity() == second.cues.identity()
    moved = apply(project, MoveClip('w1.english', 12000)).project
    later = solve(moved, media)
    assert later.render.identity() != first.render.identity()
    assert later.render.project_revision == 1


def test_plan_covers_every_clip_once_in_a_deterministic_order(project, media):
    plan = render_plan(project, media)
    ids = [item.clip_id for item in plan.video] + [item.clip_id for item in plan.audio]
    assert sorted(ids) == sorted(clip.id for clip in project.clips)
    assert len(ids) == len(set(ids)) == len(project.clips)
    # Paint order: project layers under the word layers; speech in teaching order.
    assert [item.role for item in plan.video] == ['title', 'subtitle', 'footer',
                                                  'english', 'phonetic', 'meaning']
    assert [item.role for item in plan.audio] == ['female', 'male', 'chinese']


def test_template_expands_once_and_only_into_an_empty_project(project, media):
    with pytest.raises(SchemaError) as error:
        instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), media, project)
    assert error.value.code == 'SCHEMA'
    # Stable ids: what a later explicit upgrade matches against must not drift.
    assert [clip.id for clip in project.clips] == [
        'w1.female', 'w1.male', 'w1.chinese', 'w1.english', 'w1.phonetic',
        'w1.meaning', 'layer.title', 'layer.subtitle', 'layer.footer']
    assert project.records == (golden_record(),)


def test_template_keeps_the_teaching_order_as_a_business_rule():
    with pytest.raises(SchemaError):
        LessonTemplate(template_id='t', version=1,
                       stages=(StageNode('male', 'word'), StageNode('female', 'word'),
                               StageNode('chinese', 'spoken_meaning')),
                       displays=DEFAULT_LESSON_TEMPLATE.displays)
    with pytest.raises(SchemaError):
        LessonTemplate(template_id='t', version=1,
                       stages=DEFAULT_LESSON_TEMPLATE.stages,
                       displays=(DisplayNode('english', 'word', 'nowhere'),))
    with pytest.raises(SchemaError):
        LessonTemplate(template_id='t', version=0,
                       stages=DEFAULT_LESSON_TEMPLATE.stages,
                       displays=DEFAULT_LESSON_TEMPLATE.displays)


def test_document_refuses_bad_numbers_bad_versions_and_unknown_fields(project):
    with pytest.raises(InvalidTimeError):
        Project(project_id='p', speed=float('nan'))
    with pytest.raises(InvalidTimeError):
        Project(project_id='p', intro_s=float('inf'))
    with pytest.raises(InvalidTimeError):
        Project(project_id='p', gap_s=-1.0)
    with pytest.raises(InvalidTimeError):
        Project(project_id='p', width=0)
    with pytest.raises(SchemaError) as error:
        Project(project_id='p', schema='wv-project@3')
    assert error.value.code == 'SCHEMA'

    document = project.to_dict()
    document['surprise'] = 1
    with pytest.raises(SchemaError) as error:
        Project.from_dict(document)
    assert 'unknown field' in error.value.message
    document.pop('surprise')
    document.pop('gap_s')
    with pytest.raises(SchemaError) as error:
        Project.from_dict(document)
    assert 'missing' in error.value.message
    document = project.to_dict()
    document['schema'] = 'wv-project@9'
    with pytest.raises(SchemaError):
        Project.from_dict(document)
    document = project.to_dict()
    document['clips'][0]['start'] = {'ticks': 0, 'ref': 'w1.male'}
    with pytest.raises(InvalidTimeError):
        Project.from_dict(document)


def test_media_values_are_validated():
    with pytest.raises(SourceRangeError):
        MediaSlice('a', 100, 100)
    with pytest.raises(InvalidTimeError):
        MediaSlice('a', 0, 100, speed=0)
    with pytest.raises(InvalidTimeError):
        MediaSlice('a', 0, 100, gain_db=float('nan'))
    with pytest.raises(InvalidTimeError):
        MediaInfo('a', 0)
    with pytest.raises(SchemaError):
        MediaInfo('a', 48000, unit_den=0)


def test_a_source_window_on_another_grid_is_refused(project, media):
    mismatched = dict(media)
    mismatched['w1:male'] = MediaInfo('w1:male', 40800, 1, 44100)
    with pytest.raises(SchemaError) as error:
        render_plan(project, mismatched)
    assert 'grid' in error.value.message


def test_media_durations_apply_speed_exactly_once():
    # 4410 samples at 44.1 kHz are exactly 0.1 s = 72000 ticks; speed 1.25 -> 57600.
    assert MediaSlice('a', 0, 4410, 1, 44100).duration_ticks == 72000
    assert MediaSlice('a', 0, 4410, 1, 44100, speed=1.25).duration_ticks == 57600
    assert MediaSlice('a', 0, 4410, 1, 44100, speed=2).duration_ticks == 36000
    # 48 kHz: 15 ticks per sample, so 48000 samples fill one second.
    assert MediaSlice('a', 0, 48000, 1, 48000).duration_ticks == 720000


def test_endpoint_conversions_happen_once_not_by_accumulation():
    """SRT/draft adapters convert absolute endpoints; they never sum roundings.

    25000 ticks is 34.72 ms -> 35 ms.  Three segments converted at their own
    absolute endpoints are 35 / 69 / 104 ms; adding the per-segment roundings
    would give 105 ms, which is the error this rule exists to prevent.
    """
    assert ticks_to_milliseconds(25000) == 35
    assert ticks_to_milliseconds(50000) == 69
    assert ticks_to_milliseconds(75000) == 104
    assert 3 * ticks_to_milliseconds(25000) == 105
    assert ticks_to_milliseconds(12000) == 17          # 1/60 s, half up
    assert ticks_to_milliseconds(330 * 12000) == 5500  # the golden total
    assert ticks_to_microseconds(12000) == 16667


def _two_words(indexes):
    records = (Record(id='w1', word='promise', phonetic='p', meaning='m',
                      spoken_meaning='承诺', index=indexes[0]),
               Record(id='w2', word='apple', phonetic='a', meaning='n',
                      spoken_meaning='苹果', index=indexes[1]))
    assets = {}
    for record in records:
        for role in ('female', 'male', 'chinese'):
            key = '%s:%s' % (record.id, role)
            assets[key] = MediaInfo(key, 48000)
    return instantiate(DEFAULT_LESSON_TEMPLATE, records, assets, golden_base())


def test_record_numbering_reaches_the_on_screen_subtitle():
    assert _two_words((151, 152)).clip('layer.subtitle').text == '速通（151–152）'
    # Without source numbers the batch falls back to its own positions.
    assert _two_words((0, 0)).clip('layer.subtitle').text == '速通（1–2）'


def test_duplicate_record_numbers_are_refused():
    with pytest.raises(DuplicateIdError):
        _two_words((7, 7))
