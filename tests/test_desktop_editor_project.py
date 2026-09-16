"""The project folder: import, pre-flight, repairs, preview assets, export gate.

These tests exercise the pieces a member interacts with *before* any picture is
drawn: turning a request plus the audio that already exists into an editable
folder, saying what is missing and offering the click that fixes it, and refusing
a delivery that is known to be undeliverable before a render starts.

The audio is synthesised here, so nothing in this file needs the network, a TTS
credential or the real cache, and every temporary lives under the work root.
"""
import json
from pathlib import Path

import pytest

from desktop import editor_notices as notices
from desktop.editor_export import blocking_notices, export_folder, free_run_name
from desktop.editor_import import import_request, read_entries, resolve_wordlist, work_root
from desktop.editor_model import EditorState
from desktop.editor_project import (BACKGROUND_BUCKET_TICKS, Delivery, ProjectFolder,
                                    assets_agree, with_background)
from test_editor_support import (assets_of, tiny_folder, tiny_state,
                                 write_intro_with_audio)
from test_preview_support import scratch, write_test_video, write_tone

WORDS = ['promise /ˈprɒmɪs/ n. 承诺', 'apple /ˈæpl/ n. 苹果', 'elephant /ˈelɪfənt/ n. 大象']
ROLES = ('female', 'male', 'chinese')


@pytest.fixture
def area():
    """A scratch directory on the work root's drive, removed afterwards."""
    with scratch('editor-project-') as folder:
        yield folder


def write_request(folder, *, tones, with_intro=False, voice=True):
    """A request JSON in the shape the CLI and the fixtures use."""
    folder = Path(folder)
    wordlist = folder / 'wordlist.txt'
    wordlist.write_text('\n'.join(WORDS) + '\n', encoding='utf-8')
    items = []
    for index in range(1, 4):
        for role in ROLES:
            text = '承诺' if role == 'chinese' else WORDS[index - 1].split(' ')[0]
            items.append({'index': index, 'role': role, 'text': text,
                          'voice': 'BV503_streaming' if voice else '',
                          'path': tones['w%d:%s' % (index, role)]})
    lesson = {'speed': 1.25, 'width': 640, 'height': 360, 'video_codec': 'h264'}
    if with_intro:
        intro = write_test_video(folder / 'intro.mp4', 1.0, 30, '160x120')
        lesson['intro'] = {'video': str(intro)}
        lesson['intro_s'] = 2.0                     # deliberately wrong: media wins
    request = {'idempotency_key': 'editor-fixture', 'source': {'path': str(wordlist)},
               'range': {'start': 1, 'end': 3}, 'lesson': lesson,
               'provider': {'kind': 'local', 'items': items}}
    path = folder / 'request.json'
    path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    return path, request


# ------------------------------------------------------------------- import
def test_word_list_entries_are_parsed_the_way_the_app_counts_them():
    with scratch('editor-import-') as folder:
        wordlist = Path(folder) / 'w.txt'
        wordlist.write_text('\n'.join(WORDS) + '\n', encoding='utf-8')
        entries = read_entries(wordlist, [1, 3])
        assert entries[1] == ('promise', '/ˈprɒmɪs/', 'n. 承诺')
        assert entries[3][0] == 'elephant'
        with pytest.raises(ValueError):
            read_entries(wordlist, [4])


def test_the_word_list_falls_back_to_this_work_root_by_name():
    missing = {'source': {'path': r'Z:\nowhere\not-there.txt'}}
    with pytest.raises(ValueError):
        resolve_wordlist(missing)
    root = work_root()
    if root is None:
        pytest.skip('no work root here; the fallback has nothing to find')
    named = root / 'data' / 'wordlists' / '四级核心1500词_已清理.txt'
    if not named.is_file():
        pytest.skip('the cleaned word list is not on this machine')
    found = resolve_wordlist({'source': {'path': r'Z:\nowhere\四级核心1500词_已清理.txt'}})
    assert found == named


def test_a_request_becomes_a_folder_with_no_hand_written_json(area):
    _, tones = assets_of(area, count=3)
    request_path, _ = write_request(area, tones=tones)
    target = area / 'project'
    folder = import_request(request_path, target)

    assert folder.project_path.is_file()
    assert folder.assets_path.is_file()
    assert not folder.catalog_path.exists(), '编辑器只写一份资产文档（assets.json）'
    assert folder.delivery_path.is_file()
    assert len(folder.project.records) == 3
    assert len(folder.project.clips) == 21                  # 3 words x 6 + 3 layers
    assert folder.delivery.video_codec == 'h264'
    # The document carries identities; the catalogue carries the paths.
    document = folder.project_path.read_text(encoding='utf-8')
    assert 'w1:female' in document
    assert str(area) not in document
    assert folder.asset_path('w1:female') == tones['w1:female']

    # And it solves: the folder is a real project, not a shape on disk.
    state = EditorState(folder.project, media=folder.media_table(),
                        path=folder.project_path)
    assert state.plan() is not None and state.cues_error is None
    assert folder.diagnostics(folder.project) == ()


def test_the_intro_window_is_the_picture_not_the_audio_packet_count(area):
    """Counterexample for a real measurement trap, kept as a regression.

    The reference countdown is a MOV whose audio is AAC.  ``catalog.measure``
    prefers the audio stream and trusts ``nb_frames``, which for a packetised codec
    is the **packet** count: the 1.856 s / 89 088-sample track came back as 88
    "samples" (1.8 ms), so a source window built from it is 1000x too short and every
    trim or cut on that layer would describe media that is not there.  The editor
    builds the intro's window from the measured *picture* instead, in milliseconds,
    and the delivery is untouched because the exporter measures the intro from the
    plan.

    The countdown can no longer be *cut* (A's rule: the intro is the one countdown
    stage, so ``can_split`` refuses it and ``SplitClip`` refuses it too), so the
    "a cut lands inside the window" half of this regression now lives in the
    background test; the window assertion - the part this trap is about - stays here.
    """
    import json as json_module
    _, tones = assets_of(area, count=3)
    request_path, request = write_request(area, tones=tones)
    intro = write_intro_with_audio(area / 'intro.mp4', 1.0, 30)
    request['lesson']['intro'] = {'video': str(intro)}
    request['lesson']['intro_s'] = 2.0
    Path(request_path).write_text(json_module.dumps(request, ensure_ascii=False),
                                  encoding='utf-8')
    folder = import_request(request_path, area / 'project')
    clip = folder.project.intro_clip()

    assert clip.source.unit_den == 1000                      # a picture grid, not audio
    assert clip.source.source_units == pytest.approx(1000, abs=40)   # ~1.0 s of it
    assert clip.source.source_units > 900                    # not 88 packets
    state = EditorState(folder.project, media=folder.media_table(),
                        intro=folder.intro_measurement(), path=folder.project_path)
    assert state.plan() is not None and state.cues_error is None
    assert state.plan().intro_item.end_ticks == clip.duration_ticks


def test_a_split_background_is_deliverable_and_a_split_intro_is_not(area):
    """B-5 draws every background piece; the countdown stays single by design.

    Two rules, both asserted here because they are the two halves of acceptance
    item 3 for the splittable layers:

    * a cut **background** is not blocked (B-5 delivers one segment per piece, each
      looping inside its own stage) - and the cut really happens through A's command;
    * a cut **intro** is refused by the delivery gate.  The countdown cannot even be
      cut any more (A's ``can_split`` refuses the role), so the state is *constructed*
      here instead: this is what keeps the gate a tested safety net rather than dead
      code, and it also documents that the gate is the second line of defence behind
      A's command-layer refusal.
    """
    from dataclasses import replace
    from word_video.application import expand
    from word_video.domain.compile import IntroMeasurement
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.domain.model import MediaSlice, Project
    from word_video.domain.timebase import TimeExpr
    from word_video.storage.assets import AssetRef

    folder, state = tiny_state(area / 'project')
    # The background is *longer* than the lesson, which is the case a cut can be
    # expressed in: the piece's source window is proportional to what it plays.  A
    # background shorter than the lesson is looped, and A's SplitClip refuses a cut
    # it cannot map onto the source window (reported, not worked around here).
    background = write_test_video(area / 'background.mp4', 20.0, 30, '160x120')
    intro = write_test_video(area / 'intro.mp4', 2.0, 30, '160x120')
    # Registered on the same grid the slices use, exactly as the importer registers
    # the countdown: a video layer's window is its picture, in milliseconds, and the
    # solver refuses a clip whose grid disagrees with the registry's measurement.
    # The intro's id is its path because the exporter measures that layer from the
    # id itself (``measure_project_intro``), which is the documented id-as-path case.
    for asset_id, path in (('layer:background', background), (str(intro), intro)):
        units = 20000 if asset_id == 'layer:background' else 2000
        folder.refs[asset_id] = AssetRef(asset_id=asset_id, path=str(path), units=units,
                                         unit_num=1, unit_den=1000, kind='video')
    folder.save_assets()
    folder._measured = True
    background_slice = MediaSlice(asset_id='layer:background', source_start=0,
                                  source_end=20000, unit_num=1, unit_den=1000)
    intro_slice = MediaSlice(asset_id=str(intro), source_start=0,
                             source_end=2000, unit_num=1, unit_den=1000)
    project = expand(replace(DEFAULT_LESSON_TEMPLATE, intro=True),
                     state.project.records, folder.media_table(),
                     Project(project_id='split-layers', fps_num=60, intro_s=1.0,
                             speed=1.25, width=640, height=360),
                     background=background_slice, intro=intro_slice,
                     intro_measure=IntroMeasurement(asset_id=str(intro), seconds=2.0,
                                                    picture_seconds=2.0))
    folder.save_project(project)
    state = EditorState(project, media=folder.media_table(),
                        intro=folder.intro_measurement(), path=folder.project_path)
    assert state.plan() is not None, state.plan_error
    assert state.cues_error is None
    assert blocking_notices(folder, project) == ()

    background_clip = next(clip for clip in project.clips if clip.role == 'background')
    intro_clip = project.intro_clip()
    assert state.split_check(background_clip.id).ok is True
    # The countdown is refused *before the fact*, with a reason and the clip named.
    intro_check = state.split_check(intro_clip.id)
    assert intro_check.ok is False
    assert intro_check.code == 'SPLIT_NOT_APPLICABLE'
    assert 'countdown' in intro_check.reason
    before = {clip.id: (clip.start.to_dict(), clip.duration_ticks)
              for clip in project.clips}
    revision_before = state.revision
    refused = state.split_at_ticks(intro_clip.id, intro_clip.duration_ticks // 2)
    assert refused.changed is False
    assert [notice.code for notice in refused.notices] == ['SPLIT_NOT_APPLICABLE']
    assert refused.notices[0].clip_id == intro_clip.id
    assert refused.notices[0].role == 'intro'
    # Refused with no side effect at all: A's command never touched the document.
    assert state.revision == revision_before
    assert {clip.id: (clip.start.to_dict(), clip.duration_ticks)
            for clip in state.project.clips} == before

    cut_background = state.split_at_ticks(background_clip.id,
                                          background_clip.duration_ticks // 2)
    assert cut_background.changed is True, [n.code for n in cut_background.notices]
    pieces = [clip for clip in state.project.clips if clip.role == 'background']
    assert len(pieces) == 2
    # Two background pieces are a delivery this build can produce: not blocked.
    assert blocking_notices(folder, state.project) == ()
    assert state.cues_error is None

    # The delivery gate is the *second* line of defence, so it is exercised on a
    # document that A's command could no longer produce: a project carrying two
    # intro clips.  Built directly - not by splitting - or the gate would be dead
    # code that nothing tests.
    twin = replace(intro_clip, id='layer.intro.2',
                   start=TimeExpr.at(intro_clip.duration_ticks))
    split_project = state.project.with_clips(state.project.clips + (twin,))
    blockers = blocking_notices(folder, split_project)
    assert [notice.code for notice in blockers] == ['SPLIT_INTRO_NOT_EXPORTABLE']
    assert blockers[0].clip_id == 'layer.intro.2'
    assert '片头不支持拆分' in blockers[0].message
    assert notices.ACTION_UNDO in [action.kind for action in blockers[0].actions]
    # The gate blocks the *delivery* only: the document is still a document - it
    # saves, reopens and keeps its two intro pieces.
    folder.save_project(split_project)
    reopened = ProjectFolder.open(folder.path)
    assert len([clip for clip in reopened.project.clips if clip.role == 'intro']) == 2
    assert reopened.project.revision == split_project.revision

    # And a project with the countdown in one piece stays deliverable, background cut
    # and all - the gate is about the intro, not about having edited anything.
    folder.save_project(state.project)
    assert blocking_notices(folder, state.project) == ()
    assert state.cues_error is None
    assert len([clip for clip in state.project.clips if clip.role == 'background']) == 2


def test_the_spoken_text_comes_from_the_audio_side_not_the_word_list(area):
    """What sounds wins: the caption has to match the file, not the other way round."""
    _, tones = assets_of(area, count=3)
    request_path, request = write_request(area, tones=tones)
    request['provider']['items'][2]['text'] = '承诺诺言'      # a v2-cleaned reading
    Path(request_path).write_text(json.dumps(request, ensure_ascii=False),
                                  encoding='utf-8')
    folder = import_request(request_path, area / 'project')
    record = folder.project.record('w1')
    assert record.spoken_meaning == '承诺诺言'
    assert record.meaning == 'n. 承诺'                        # the display field


def test_the_intro_becomes_a_layer_measured_from_its_own_media(area):
    """The intro is project content (A-4) and the exporter reads that layer (W07).

    So the importer creates the layer rather than passing a delivery argument, and
    the stage length is the media's - a template's round 2.0 s cannot contradict a
    1.0 s clip, because it is not consulted.
    """
    _, tones = assets_of(area, count=3)
    request_path, _ = write_request(area, tones=tones, with_intro=True)
    folder = import_request(request_path, area / 'project')
    clip = folder.project.intro_clip()
    assert clip is not None and clip.role == 'intro'
    assert clip.duration_ticks == 720000                    # 1.0 s, 60 fps, from media
    assert folder.project.intro_s == 2.0                    # the unused template number
    from word_video.domain.model import SCHEMA_V1
    assert folder.project.schema != SCHEMA_V1               # a document that can hold it
    # The renderer finds the countdown by the asset id, so it is the file path.
    assert clip.source.asset_id == str(area / 'intro.mp4')
    assert folder.asset_path(clip.source.asset_id) == str(area / 'intro.mp4')

    state = EditorState(folder.project, media=folder.media_table(),
                        intro=folder.intro_measurement(), path=folder.project_path)
    plan = state.plan()
    assert plan is not None, state.plan_error
    assert plan.intro_item is not None
    assert plan.intro_item.end_ticks == 720000
    assert plan.intro_item.source.asset_id == str(area / 'intro.mp4')
    assert state.cues_error is None
    assert folder.diagnostics(folder.project) == ()


def test_a_missing_voice_file_fails_the_import_with_the_path_named(area):
    _, tones = assets_of(area, count=3)
    Path(tones['w2:male']).unlink()
    request_path, _ = write_request(area, tones=tones)
    from word_video.exporters.catalog import CatalogError
    with pytest.raises(CatalogError) as error:
        import_request(request_path, area / 'project')
    assert 'w2:male' in str(error.value)


def test_an_imported_project_records_the_voice_of_every_reading_asset(area):
    """The voice travels from the request into the registry (H0's import finding).

    Without it an imported project recorded **no** voice at all, so the delivery
    pre-check - "a reading stage made from existing audio must have its voice recorded"
    - could never be satisfied by a project the editor itself had just created: the loop
    H0 spent a morning in, with a repair command that led back to the same refusal.
    """
    import json

    _, tones = assets_of(area, count=3)
    request_path, request = write_request(area, tones=tones)
    folder = import_request(request_path, area / 'project')

    wanted = {('w%d:%s' % (item['index'], item['role'])): item['voice']
              for item in request['provider']['items']}
    assert len(wanted) == 9 and all(wanted.values())
    registry = json.loads(folder.assets_path.read_text(encoding='utf-8'))
    stored = {row['asset_id']: row.get('voice', '') for row in registry['assets']}
    assert {key: stored.get(key, '') for key in wanted} == wanted
    # The folder agrees, which is what the repair UI and the exporter read.
    assert {key: folder.voices.get(key, '') for key in wanted} == wanted


def test_the_document_is_written_before_the_rest_of_the_folder(area):
    """A folder whose catalogue is missing is still openable and repairable."""
    folder = tiny_folder(area / 'project')
    assert folder.project_path.is_file()
    reopened = ProjectFolder.open(area / 'project')
    assert reopened.project.revision == folder.project.revision


# -------------------------------------------------------------- pre-flight
def test_a_missing_media_file_is_reported_with_a_one_click_repair(area):
    folder, state = tiny_state(area / 'project')
    gone = folder.asset_path('w1:male')
    Path(gone).unlink()

    found = [notice for notice in folder.diagnostics(state.project)
             if notice.code == 'ASSET_FILE_MISSING']
    assert len(found) == 1
    notice = found[0]
    assert notice.word == 'promise' and notice.role == 'male'
    assert gone in notice.message
    kinds = [action.kind for action in notice.actions]
    assert notices.ACTION_CHOOSE_ASSET in kinds and notices.ACTION_RECHECK in kinds

    # The repair is a real one: point the asset at another file and re-check.
    replacement = write_tone(area / 'replacement.wav', 1.0, 700.0)
    folder.set_asset_file(notice.actions[0].asset_id, str(replacement))
    assert [item.code for item in folder.diagnostics(state.project)] == []
    state.set_media_table(folder.media_table(refresh=True))
    assert state.plan() is not None


def test_a_file_that_is_not_audio_is_reported_as_unmeasured(area):
    folder, state = tiny_state(area / 'project')
    Path(folder.asset_path('w1:female')).write_bytes(b'not audio at all')
    # "重新检查" is what re-probes: the file check runs on every redraw because it
    # is cheap, and an ffprobe pass runs when the member asks for one.
    table = folder.media_table(refresh=True)
    assert 'w1:female' not in table
    assert len(table) == 2, 'one bad file must not hide the other two'
    found = [notice for notice in folder.diagnostics(state.project)
             if notice.code == 'UNKNOWN_DURATION']
    assert len(found) == 1
    assert found[0].word == 'promise' and found[0].role == 'female'
    assert 'RuntimeError' in found[0].hint      # the probe's own refusal is shown
    assert [action.kind for action in found[0].actions] == [notices.ACTION_CHOOSE_ASSET,
                                                            notices.ACTION_RECHECK]


def test_a_deleted_file_is_caught_without_re_measuring_anything(area):
    """The cheap path: the measurement cache stays valid, the file does not."""
    folder, state = tiny_state(area / 'project')
    cached = folder.media_table()
    Path(folder.asset_path('w1:female')).unlink()
    found = [notice for notice in folder.diagnostics(state.project)
             if notice.code == 'ASSET_FILE_MISSING']
    assert len(found) == 1 and found[0].role == 'female'
    assert folder.media_table() == cached      # no re-probe happened


def test_a_missing_voice_is_a_warning_with_a_picker(area):
    folder, state = tiny_state(area / 'project')
    folder.set_voice('w1:female', '')
    found = [notice for notice in folder.diagnostics(state.project)
             if notice.code == notices.EDITOR_MISSING_VOICE]
    assert len(found) == 1
    assert found[0].severity == notices.SEVERITY_WARN
    assert notices.ACTION_CHOOSE_VOICE in [a.kind for a in found[0].actions]
    folder.set_voice('w1:female', 'BV503_streaming')
    assert not [item for item in folder.diagnostics(state.project)
                if item.code == notices.EDITOR_MISSING_VOICE]


def test_a_project_without_a_registry_opens_and_says_so(area):
    folder = tiny_folder(area / 'project')
    folder.assets_path.unlink()
    reopened = ProjectFolder.open(area / 'project')
    assert reopened.refs == {}
    found = reopened.diagnostics(reopened.project)
    assert found and all(notice.code == 'MISSING_ASSET' for notice in found)
    assert all(notices.ACTION_CHOOSE_ASSET in [a.kind for a in notice.actions]
               for notice in found)


def test_assets_json_is_the_only_opinion_about_the_assets(area):
    """One registry names an asset, and its voice lives *in* it.

    The bridge this test used to guard wrote ``assets.json`` **and** ``media.json``
    from one ref list.  The exporter reads the registry first, so the second file was
    one writer away from drifting and the voice was the field that only existed there.
    Now there is one document: this asserts that after a re-point and after a voice is
    recorded, ``media.json`` is not written at all and the exporter - which reads
    ``assets.json`` first - finds the path *and* the voice in the same entry.
    """
    folder, state = tiny_state(area / 'project')
    ok, differences = assets_agree(area / 'project')
    assert ok, differences
    assert not folder.catalog_path.exists(), '编辑器不该再写第二份资产文档'

    replacement = write_tone(area / 'other.wav', 1.0, 640.0)
    folder.set_asset_file('w1:male', str(replacement))
    ok, differences = assets_agree(area / 'project')
    assert ok, differences
    assert folder.asset_path('w1:male') == str(replacement)

    folder.set_voice('w1:male', 'BV504_streaming')
    ok, differences = assets_agree(area / 'project')
    assert ok, differences
    assert not folder.catalog_path.exists()
    # The exporter's own reader sees the file and the voice the registry names.
    from word_video.exporters import render
    entries, _, from_registry = render._asset_source(area / 'project')
    assert from_registry is True
    assert entries['w1:male'].path == str(replacement)
    assert entries['w1:male'].voice == 'BV504_streaming'
    # Reopening and saving again still writes one document.
    again = ProjectFolder.open(area / 'project')
    again.save_assets()
    assert not again.catalog_path.exists()
    ok, differences = assets_agree(area / 'project')
    assert ok, differences


def test_a_pre_registry_project_keeps_the_fallback_and_migrates_on_save(area):
    """B's fallback reads a catalogue-only folder, and the first save adopts it.

    A project written before the registry existed has ``project.json`` and
    ``media.json`` only.  The exporter must keep working (that is B's documented
    fallback, ``from_registry=False``), and the editor must not break it by writing an
    empty registry next to it: the catalogue's entries and voices are adopted **and
    measured** on the next save, because a registry entry without a measurement is a
    refusal (``UNKNOWN_DURATION``) where the fallback used to measure the file itself.
    """
    from word_video.exporters import render
    from word_video.exporters.catalog import Asset, save_catalog

    folder, state = tiny_state(area / 'project')
    voices = {asset_id: 'BV%d_streaming' % (500 + index)
              for index, asset_id in enumerate(sorted(folder.refs))}
    # The folder a pre-registry build left behind: a catalogue and no registry.
    save_catalog({asset_id: Asset(asset_id=asset_id, path=ref.path, voice=voices[asset_id])
                  for asset_id, ref in folder.refs.items()}, folder.path)
    folder.assets_path.unlink()

    ok, differences = assets_agree(area / 'project')  # 旧工程：目录就是唯一真相
    assert ok, differences
    entries, _, from_registry = render._asset_source(area / 'project')
    assert from_registry is False and set(entries) == set(voices)

    reopened = ProjectFolder.open(area / 'project')
    reopened.measure()
    reopened.save_assets()                           # 编辑器写一次：目录被并进登记表
    entries, _, from_registry = render._asset_source(area / 'project')
    assert from_registry is True
    assert set(entries) == set(voices), '旧目录里的资产不能因为删桥而丢失'
    assert {asset_id: entry.voice for asset_id, entry in entries.items()} == voices
    assert all(entry.path == folder.refs[asset_id].path for asset_id, entry in entries.items())
    ok, differences = assets_agree(area / 'project')
    assert ok, ('迁移后两份文档一致，且音色已在登记表里', differences)


def test_a_missing_delivery_file_is_reported_in_the_delivery_panel(area):
    folder, state = tiny_state(area / 'project')
    folder.set_delivery(background=str(area / 'no-such-background.mp4'),
                        intro_video=str(area / 'no-such-intro.mov'))
    found = [notice for notice in folder.diagnostics(state.project)
             if notice.code == notices.EDITOR_MISSING_FILE]
    assert len(found) == 2
    assert {notice.message.split('：')[0] for notice in found} == {'背景视频不存在',
                                                                  '片头视频不存在'}
    assert all(notices.ACTION_OPEN_DELIVERY in [a.kind for a in notice.actions]
               for notice in found)
    assert folder.background_slice(state.project) is None


def test_an_intro_layer_is_deliverable_now_that_the_exporter_reads_it(area):
    """B's W07 turned the exporter into a read-only plan for the intro layer.

    This is the counterexample to the editor's own earlier refusal: the same
    project that used to be reported as unexportable now solves with its intro
    measurement and produces a manifest whose intro comes from the plan.
    """
    _, tones = assets_of(area, count=3)
    request_path, _ = write_request(area, tones=tones, with_intro=True)
    folder = import_request(request_path, area / 'project')
    from word_video.application.intro import measure_project_intro
    from word_video.domain import solve
    from word_video.exporters.plan import build_manifest
    from word_video.exporters.plan import SourceMedia

    measurement = measure_project_intro(folder.project)
    assert measurement is not None and measurement.seconds > 0
    assert measurement.asset_id == str(area / 'intro.mp4')
    media = folder.media_table()
    solution = solve(folder.project, media, measurement)
    assert solution.render.intro_item is not None
    sources = {clip.source.asset_id: SourceMedia(path=clip.source.asset_id)
               for clip in folder.project.clips
               if clip.role in ('female', 'male', 'chinese')}
    view = build_manifest(solution.render, folder.project, sources, background='')
    # The manifest takes the countdown from the plan, not from a parameter.
    assert view.intro_video == str(area / 'intro.mp4')
    assert view.intro_frames == 60
    # And the editor's pre-flight has nothing to say about it.
    assert [notice.code for notice in folder.diagnostics(folder.project)] == []


def test_unknown_project_schemas_are_refused_rather_than_opened(area):
    folder = tiny_folder(area / 'project')
    document = json.loads(folder.project_path.read_text(encoding='utf-8'))
    document['schema'] = 'wv-project@99'
    folder.project_path.write_text(json.dumps(document, ensure_ascii=False),
                                   encoding='utf-8')
    from word_video.domain.errors import ProjectError
    with pytest.raises(ProjectError) as error:
        ProjectFolder.open(area / 'project')
    assert error.value.code == 'SCHEMA'
    assert notices.stale_document_notice(str(error.value)).code == \
        notices.EDITOR_STALE_DOCUMENT


# ------------------------------------------------------------------ preview
def test_preview_audio_is_prepared_at_speed_once_and_then_cached(area):
    folder, state = tiny_state(area / 'project')
    first = folder.preview_assets(state.project)
    assert len(first) == 3                                   # three words, three roles
    assert all(Path(path).is_file() for path in first.values())
    stamps = {key: Path(path).stat().st_mtime_ns for key, path in first.items()}
    second = folder.preview_assets(state.project)
    assert second == first
    assert {key: Path(path).stat().st_mtime_ns for key, path in second.items()} == stamps

    # The prepared file is the source at the project's speed, not the source: a
    # preview that played the raw file would be 1.25x too long.
    from word_video.media import wav_duration
    source = wav_duration(Path(folder.asset_path('w1:female')))
    prepared = wav_duration(Path(first['w1:female']))
    assert prepared == pytest.approx(source / 1.25, abs=0.05)


def test_the_preview_plan_carries_the_delivery_background_first(area):
    folder, state = tiny_state(area / 'project')
    background = write_test_video(area / 'background.mp4', 2.0, 30, '160x120')
    folder.set_delivery(background=str(background))
    slice_ = folder.background_slice(state.project)
    assert slice_ is not None and slice_.asset_id == 'layer:background'
    plan = with_background(state.plan(), state.project, slice_)
    assert plan.video[0].role == 'background'
    # It covers the whole lesson (the preview must not run out of picture), and its
    # end is bucketed so a small edit reuses one cached window instead of copying
    # the background again - measured cost of the alternative: ~26 MB per drag.
    assert plan.video[0].end_ticks >= state.plan().total_ticks
    assert plan.video[0].end_ticks % BACKGROUND_BUCKET_TICKS == 0
    shorter = with_background(state.plan(), state.project, slice_)
    assert shorter.video[0].end_ticks == plan.video[0].end_ticks
    # Everything the document solved is still there, in the same order.
    assert [item.clip_id for item in plan.video[1:]] == \
        [item.clip_id for item in state.plan().video]
    assert [item.clip_id for item in plan.audio] == \
        [item.clip_id for item in state.plan().audio]


# ------------------------------------------------------------------- export
def test_export_refuses_an_undeliverable_project_before_rendering(area):
    folder, state = tiny_state(area / 'project')
    Path(folder.asset_path('w1:chinese')).unlink()
    output = area / 'out'
    blockers = blocking_notices(folder, state.project)
    assert [notice.code for notice in blockers] == ['ASSET_FILE_MISSING']

    outcome = export_folder(folder, state, output=output)
    assert outcome.ok is False
    assert not output.exists(), 'a refused export must not leave a run folder'
    assert outcome.notices[0].word == 'promise'
    assert outcome.notices[0].role == 'chinese'


def test_export_blocks_a_teaching_overlap_with_both_clips_named(area):
    folder, state = tiny_state(area / 'project')
    male = state.project.clip('w1.male').start.ticks
    chinese = state.project.clip('w1.chinese').start.ticks
    state.select('w1.male')
    state.move_selection(chinese - male - 12000)
    blockers = blocking_notices(folder, state.project)
    assert blockers and blockers[0].code == 'TEACHING_OVERLAP'
    assert blockers[0].word == 'promise'
    targets = [action.clip_id for action in blockers[0].actions
               if action.kind == notices.ACTION_SELECT_CLIP]
    assert set(targets) == {'w1.male', 'w1.chinese'}
    outcome = export_folder(folder, state, output=area / 'out')
    assert outcome.ok is False and outcome.video == ''


def test_a_run_folder_is_never_merged_with_an_older_one(area):
    output = area / 'out'
    assert free_run_name(output, 3) == 'rev3'
    (output / 'rev3').mkdir(parents=True)
    (output / 'rev3' / 'video.mp4').write_bytes(b'x')
    assert free_run_name(output, 3) == 'rev3-2'
    (output / 'rev3-2').mkdir()
    assert free_run_name(output, 3) == 'rev3-2'          # an empty folder is reusable


def test_delivery_settings_round_trip_and_refuse_unknown_fields(area):
    folder = tiny_folder(area / 'project')
    folder.set_delivery(background='D:\\bg.mp4', intro_video='D:\\intro.mov',
                        intro_audio='D:\\intro.wav', video_codec='h265')
    assert Delivery.load(area / 'project') == folder.delivery
    (area / 'project' / 'delivery.json').write_text(
        json.dumps({'background': 'a', 'mystery': 1}), encoding='utf-8')
    with pytest.raises(ValueError):
        Delivery.load(area / 'project')
