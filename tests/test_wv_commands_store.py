"""Saving, reopening and template instantiation for ``wv-project@1``.

``project.json`` is the one authoritative document: it is written atomically,
read strictly, and reopening it never re-expands the template over what the user
edited.
"""
import json

import pytest

from test_wv_project_golden import golden_base, golden_media, golden_project, golden_record

from word_video.application import MoveClip, Session, apply, instantiate
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, MediaInfo, MediaSlice,
                               ProjectError, SchemaError, StaleRevisionError,
                               UnknownDurationError, render_plan)
from word_video.storage import (PROJECT_FILENAME, load_project, project_path,
                                save_project)


@pytest.fixture
def project():
    return golden_project()


def _media():
    return golden_media()


def test_save_and_reopen_round_trip(tmp_path, project):
    first = save_project(project, tmp_path)
    assert first == tmp_path / PROJECT_FILENAME
    assert load_project(tmp_path) == project

    second = tmp_path / 'copy.json'
    save_project(project, second)
    assert second.read_bytes() == first.read_bytes()      # byte-stable document
    assert load_project(second) == project
    # Atomic write leaves no staging file behind.
    assert sorted(item.name for item in tmp_path.iterdir()) == ['copy.json',
                                                               PROJECT_FILENAME]


def test_saving_replaces_the_previous_revision_atomically(tmp_path, project):
    save_project(project, tmp_path)
    edited = apply(project, MoveClip('w1.male', 144000)).project
    save_project(edited, tmp_path)
    reopened = load_project(tmp_path)
    assert reopened.revision == 1
    assert reopened.clip('w1.male').start.ticks == 1440000 + 144000
    assert not [item for item in tmp_path.iterdir() if item.name.endswith('.tmp')]
    assert json.loads((tmp_path / PROJECT_FILENAME).read_text(encoding='utf-8'))['revision'] == 1


def test_the_document_kind_fields_and_numbers_are_strict(tmp_path, project):
    def write(document):
        path = tmp_path / 'project.json'
        path.write_text(json.dumps(document, ensure_ascii=False, allow_nan=True),
                        encoding='utf-8')
        return path

    document = project.to_dict()
    document['schema'] = 'wv-project@4'
    with pytest.raises(SchemaError) as error:
        load_project(write(document))
    assert error.value.code == 'SCHEMA'

    document = project.to_dict()
    document['clips'][0]['colour'] = 'red'
    with pytest.raises(SchemaError) as error:
        load_project(write(document))
    assert 'unknown field' in error.value.message

    document = project.to_dict()
    document.pop('speed')
    with pytest.raises(SchemaError):
        load_project(write(document))

    document = project.to_dict()
    document['speed'] = float('nan')
    with pytest.raises(ProjectError) as error:
        load_project(write(document))
    assert error.value.code == 'INVALID_TIME'

    broken = tmp_path / 'broken.json'
    broken.write_text('{"schema": "wv-project@1",', encoding='utf-8')
    with pytest.raises(SchemaError) as error:
        load_project(broken)
    assert 'JSON' in error.value.message

    with pytest.raises(SchemaError):
        load_project(tmp_path / 'missing.json')


def test_reopening_does_not_re_expand_the_template(tmp_path, project):
    edited = apply(project, MoveClip('w1.male', 144000)).project
    save_project(edited, tmp_path)
    reopened = Session.open(tmp_path).project
    assert reopened.records == edited.records
    assert [clip.id for clip in reopened.clips] == [clip.id for clip in edited.clips]
    assert reopened.clip('w1.male').start.ticks == 1440000 + 144000
    assert render_plan(reopened, _media()).item('w1.male').start_ticks == 1440000 + 144000


def test_a_client_holding_an_old_revision_is_refused_after_reopen(tmp_path, project):
    edited = apply(project, MoveClip('w1.male', 144000)).project
    save_project(edited, tmp_path)
    reopened = Session.open(tmp_path)
    with pytest.raises(StaleRevisionError) as error:
        reopened.apply(MoveClip('w1.female', 12000), expected_revision=project.revision)
    assert error.value.code == 'STALE_REVISION'
    assert reopened.apply(MoveClip('w1.female', 12000),
                          expected_revision=reopened.revision).project.revision == 2


def test_project_path_accepts_the_directory_or_the_document(tmp_path):
    assert project_path(tmp_path) == tmp_path / PROJECT_FILENAME
    assert project_path(tmp_path / 'p.json') == tmp_path / 'p.json'


def test_instantiate_needs_measured_media_and_an_empty_base(project):
    missing = {'w1:female': MediaInfo('w1:female', 55200)}
    with pytest.raises(UnknownDurationError) as error:
        instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), missing, golden_base())
    assert error.value.code == 'UNKNOWN_DURATION'
    assert error.value.path == 'record:w1'
    with pytest.raises(SchemaError):
        instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), _media(), project)


def test_asset_ids_can_come_from_the_media_cache_layout():
    # ``asset_ids`` maps (record_id, role) -> stable media identity, so the media
    # cache of W02 decides the key while expansion keeps its own convention.
    media = {'cache:aaa': MediaInfo('cache:aaa', 55200),
             'w1:male': MediaInfo('w1:male', 40800),
             'w1:chinese': MediaInfo('w1:chinese', 157200)}
    export = {('w1', 'female'): 'cache:aaa'}
    built = instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), media,
                        golden_base(), asset_ids=export)
    assert built.clip('w1.female').source.asset_id == 'cache:aaa'
    assert built.clip('w1.male').source.asset_id == 'w1:male'


def test_a_background_layer_covers_the_whole_project():
    background = MediaSlice('bg', 0, 480000, 1, 48000)      # a 10 s still/video
    built = instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), _media(),
                        golden_base(), background=background)
    clip = built.clip('layer.background')
    assert (clip.start.ticks, clip.duration_ticks) == (0, 3960000)
    plan = render_plan(built, _media())
    assert [item.clip_id for item in plan.video][0] == 'layer.background'
    assert plan.item('layer.background').end_ticks == 3960000
    assert plan.conflicts == ()
