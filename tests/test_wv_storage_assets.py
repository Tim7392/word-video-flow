"""Resolving asset ids to files and measurements, with structured refusals.

The gap this closes: a clip names an ``asset_id`` and nothing could say which file
that is, so an editor could not locate a broken reference and the preview had to be
handed an ``assets`` mapping by its caller.  These tests pin the two shapes that
must work — a project with a registry, and today's projects whose asset ids *are*
paths — plus every refusal W06 needs in order to show a fix.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from test_wv_project_golden import (golden_base, golden_media, golden_project,
                                    golden_record)

from word_video.application import instantiate
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, AssetFileError,
                               DuplicateAssetError, IntroMeasurement, MediaInfo,
                               MediaSlice, MissingAssetError, SchemaError,
                               UnknownDurationError, solve)
from word_video.storage import (ASSETS_FILENAME, AssetIndex, AssetRef, assets_path,
                                referenced_ids, resolve, save_project)


def touch(folder, name):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'placeholder media; the registry never reads its content')
    return path


def path_project(tmp_path, **asset_ids):
    """A golden lesson whose asset ids are real files, as today's projects are."""
    ids, media = {}, {}
    for role, units in (('female', 55200), ('male', 40800), ('chinese', 157200)):
        path = str(touch(tmp_path, 'media/%s.wav' % role))
        ids[('w1', role)] = path
        media[path] = MediaInfo(path, units)
    return instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), media,
                       golden_base(), asset_ids=ids)


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------
def test_without_a_registry_the_asset_id_is_the_path(tmp_path):
    project = path_project(tmp_path)
    female = project.clip('w1.female').source.asset_id
    index = resolve(project, folder=tmp_path)
    assert index.path(female) == female                  # already absolute
    assert index.path(female).endswith('female.wav')
    # Paths resolve, but the project does not store measurements, so a caller that
    # wants lengths either registers them or hands in a prober — and until then the
    # report says exactly that instead of inventing a duration.
    assert [problem.code for problem in index.problems()] == ['UNKNOWN_DURATION'] * 3
    assert index.media_map() == {}

    units = {'female': 55200, 'male': 40800, 'chinese': 157200}
    measured = resolve(project, folder=tmp_path,
                       measure=lambda asset_id: (units[Path(asset_id).stem], 1, 48000))
    assert measured.problems() == ()
    assert measured.media_info(female).units == 55200
    assert solve(project, measured.media_map()).render.total_ticks == 3960000


def test_a_registry_resolves_ids_to_paths_and_measurements(tmp_path):
    project = golden_project()                           # ids are w1:female, ...
    for role in ('female', 'male', 'chinese'):
        touch(tmp_path, 'media/%s.wav' % role)
    index = AssetIndex.of(
        (AssetRef('w1:female', 'media/female.wav', units=55200),
         AssetRef('w1:male', 'media/male.wav', units=40800, kind='audio'),
         AssetRef('w1:chinese', 'media/chinese.wav', units=157200)),
        project_id=project.project_id, folder=str(tmp_path))
    assert index.ref('w1:female').path == 'media/female.wav'
    assert index.path('w1:female') == str(tmp_path / 'media' / 'female.wav')
    assert index.media_info('w1:female') == MediaInfo('w1:female', 55200)
    assert index.problems() == ()
    # The registry wins over "the id is a path", which is what lets a member move a
    # file and fix one entry instead of editing every clip.
    resolved = resolve(project, registry=index, folder=tmp_path)
    assert resolved.path('w1:female').endswith('female.wav')
    assert resolved.media_map()['w1:male'].units == 40800
    assert solve(project, resolved.media_map()).render.total_ticks == 3960000


def test_a_measure_callback_fills_missing_measurements(tmp_path):
    project = path_project(tmp_path)
    stub = lambda asset_id: (48000, 1, 48000)            # noqa: E731
    index = resolve(project, folder=tmp_path, measure=stub)
    assert set(index.media_map()) == {clip.source.asset_id for clip in project.clips
                                      if clip.source}
    assert index.media_map()[project.clip('w1.male').source.asset_id].units == 48000
    # MediaInfo, a triple or None are the shapes a prober may return.
    assert resolve(project, folder=tmp_path,
                   measure=lambda asset_id: MediaInfo(asset_id, 96000)).media_map()
    with pytest.raises(SchemaError):
        resolve(project, folder=tmp_path, measure=lambda asset_id: 'nonsense')


def test_referenced_ids_covers_slices_and_the_intro_fallback(tmp_path):
    video = touch(tmp_path, 'intro.mp4')
    fallback = touch(tmp_path, 'intro.wav')
    project = instantiate(
        replace(DEFAULT_LESSON_TEMPLATE, intro=True), (golden_record(),),
        golden_media(), golden_base(),
        intro=MediaSlice(str(video), 0, 48000), intro_audio=str(fallback),
        intro_measure=IntroMeasurement(asset_id=str(video), seconds=1.0))
    ids = referenced_ids(project)
    assert str(video) in ids and str(fallback) in ids
    assert len(ids) == len(set(ids)) == 5                # 3 stages + video + fallback
    index = resolve(project, folder=tmp_path)
    assert index.path(str(fallback)).endswith('intro.wav')


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_missing_duplicate_and_stale_entries_are_structured(tmp_path):
    project = path_project(tmp_path)
    index = resolve(project, folder=tmp_path)

    with pytest.raises(MissingAssetError) as error:
        index.path('w1:ghost')
    assert error.value.code == 'MISSING_ASSET'
    assert error.value.path == 'asset:w1:ghost'
    assert error.value.hint

    with pytest.raises(DuplicateAssetError) as error:
        index.with_ref(AssetRef(project.clip('w1.female').source.asset_id, 'x.wav'))
    assert error.value.code == 'DUPLICATE_ASSET'
    with pytest.raises(DuplicateAssetError):
        AssetIndex.of((AssetRef('a', 'x.wav'), AssetRef('a', 'y.wav')))
    with pytest.raises(DuplicateAssetError):
        AssetIndex.from_dict({'schema': 'wv-assets@1', 'project_id': 'p',
                              'assets': [{'asset_id': 'a', 'path': 'x'},
                                         {'asset_id': 'a', 'path': 'y'}]})

    gone = index.without('w1:male').with_ref(AssetRef('w1:male', 'media/gone.wav'))
    with pytest.raises(AssetFileError) as error:
        gone.path('w1:male')
    assert error.value.code == 'ASSET_FILE_MISSING'
    assert 'gone.wav' in error.value.message

    unmeasured = AssetIndex.of((AssetRef('w1:f', 'media/female.wav'),),
                               folder=str(tmp_path))
    with pytest.raises(UnknownDurationError) as error:
        unmeasured.media_info('w1:f')
    assert error.value.code == 'UNKNOWN_DURATION'


def test_problems_lists_every_broken_reference_at_once(tmp_path):
    index = AssetIndex.of(
        (AssetRef('a', 'media/gone.wav'),                 # file missing
         AssetRef('b', 'media/there.wav'),                # not measured
         AssetRef('c', 'media/there.wav', units=48000)),  # fine
        folder=str(tmp_path))
    touch(tmp_path, 'media/there.wav')
    codes = [problem.code for problem in index.problems()]
    assert codes == ['ASSET_FILE_MISSING', 'UNKNOWN_DURATION']
    paths = [problem.path for problem in index.problems()]
    assert paths == ['asset:a', 'asset:b']
    # A whole-index report can be narrowed to the ids one screen cares about.
    assert [p.code for p in index.problems(['c'])] == []


# ---------------------------------------------------------------------------
# The registry document
# ---------------------------------------------------------------------------
def test_the_registry_round_trips_and_is_read_strictly(tmp_path):
    index = AssetIndex.of((AssetRef('a', 'media/a.wav', units=48000, kind='audio',
                                    sha256='abc'),), project_id='p1',
                          folder=str(tmp_path))
    written = index.save(tmp_path)
    assert written == tmp_path / ASSETS_FILENAME
    assert assets_path(tmp_path) == written
    again = AssetIndex.load(tmp_path)
    assert again.to_dict() == index.to_dict()
    assert again.folder == str(tmp_path)                 # folder comes from the file
    assert again.ref('a').sha256 == 'abc'
    assert not [item for item in tmp_path.iterdir() if item.name.endswith('.tmp')]

    document = index.to_dict()
    document['surprise'] = 1
    with pytest.raises(SchemaError):
        AssetIndex.from_dict(document)
    document = index.to_dict()
    document['schema'] = 'wv-assets@2'
    with pytest.raises(SchemaError):
        AssetIndex.from_dict(document)
    document = index.to_dict()
    document['assets'][0]['colour'] = 'red'
    with pytest.raises(SchemaError):
        AssetIndex.from_dict(document)
    document = index.to_dict()
    document['assets'][0].pop('path')
    with pytest.raises(SchemaError):
        AssetIndex.from_dict(document)
    # A project document is not an asset document, and vice versa.
    assert save_project(golden_project(), tmp_path).name == 'project.json'


def test_an_asset_can_record_the_voice_it_already_has(tmp_path):
    """``voice`` is recorded, never validated: it says which voice this file is."""
    index = AssetIndex.of((AssetRef('w1:female', 'media/female.wav', units=55200,
                                    kind='audio', voice='BV503_streaming'),),
                          project_id='p1', folder=str(tmp_path))
    written = index.save(tmp_path)
    again = AssetIndex.load(tmp_path)
    assert again.ref('w1:female') == index.ref('w1:female')
    assert again.ref('w1:female').voice == 'BV503_streaming'
    assert again.to_dict() == index.to_dict()
    # The key is written only when there is a voice, so a registry that never had one
    # keeps exactly the fields it had before the field existed.
    plain = AssetIndex.of((AssetRef('a', 'media/a.wav', units=1),), project_id='p1')
    assert plain.to_dict()['assets'][0] == {
        'asset_id': 'a', 'path': 'media/a.wav', 'units': 1, 'unit_num': 1,
        'unit_den': 48000, 'kind': '', 'sha256': ''}
    # A registry written before the field existed still loads, with no voice.
    import json
    document = json.loads(written.read_text(encoding='utf-8'))
    document['assets'][0].pop('voice')
    older = AssetIndex.from_dict(document, folder=str(tmp_path))
    assert older.ref('w1:female').voice == ''
    assert older.ref('w1:female').units == 55200
    # An unknown value is kept verbatim; nothing here decides whether a voice is real.
    odd = AssetIndex.from_dict({'schema': 'wv-assets@1', 'project_id': 'p',
                                'assets': [{'asset_id': 'x', 'path': 'x.wav',
                                            'voice': 'not-a-known-voice'}]})
    assert odd.ref('x').voice == 'not-a-known-voice'


def test_relative_entries_survive_moving_the_project_folder(tmp_path):
    original = tmp_path / 'proj'
    touch(original, 'media/a.wav')
    AssetIndex.of((AssetRef('a', 'media/a.wav', units=48000),), project_id='p1',
                  folder=str(original)).save(original)
    moved = tmp_path / 'moved'
    moved.mkdir()
    (moved / ASSETS_FILENAME).write_bytes((original / ASSETS_FILENAME).read_bytes())
    for item in (original / 'media').iterdir():
        touch(moved, 'media/%s' % item.name)
    loaded = AssetIndex.load(moved)
    assert loaded.path('a') == str(moved / 'media' / 'a.wav')
    assert loaded.problems() == ()
    # An absolute entry ignores the folder, so a shared asset stays found.
    absolute = AssetIndex.of((AssetRef('b', str(moved / 'media/a.wav'), units=1),),
                             folder=str(tmp_path / 'elsewhere'))
    assert absolute.path('b') == str(moved / 'media' / 'a.wav')
