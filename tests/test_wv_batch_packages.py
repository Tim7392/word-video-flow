"""A batch: fixed selection, fixed packaging, fixed template — and only a partial redo.

The three properties these tests hold are the ones a member feels:

* a batch is a **document** before it is work: planning it writes nothing, and the
  document says who, which words, how many synthesis requests and how much disk;
* every package gets its **own instance**, so one failing package cannot drag the
  others down and a redo of one touches nothing else;
* a redo re-does only what failed or changed — an untouched, already published
  package is *reused*, which is a number (0 requests, 0 renders), not a promise.

The recordings come from a synthetic ``audio-cache`` written the way the old batch
archive writes it (a folder per recording, a record naming the text), because the
whole point of the miss count is that a recording can be *there* and still be the
wrong one: an asset id names the slot, the record names what it says.
"""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import wave

import pytest

from test_wv_cli_contract import background, prepare as sign_in_place

from word_video.application import instantiate
from word_video.application.packages import (PackageRule, batch_id_for, cache_entries,
                                             instance_for, plan_document,
                                             recording_usage, split_packages)
from word_video.application.templates import TemplateDocument
from word_video.cli.main import main
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, MediaInfo, Project, Record,
                               StyleOverride)
from word_video.domain.errors import SchemaError
from word_video.storage import AssetIndex, AssetRef, save_project
from word_video.storage.batches import BatchStore
from word_video.storage.coordinator import Coordinator, JobStateError, NeedsInput

WORDS = (('w151', 151, 'apple', '苹果'), ('w152', 152, 'banana', '香蕉'),
         ('w153', 153, 'cherry', '樱桃'))
VOICES = {'female': 'BV503_streaming', 'male': 'BV504_streaming',
          'chinese': 'BV406_streaming'}
_SEQUENCE = [0]


def recording(path, seconds=0.5, rate=24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        _SEQUENCE[0] += 1
        stream.writeframes(bytes(((_SEQUENCE[0] + index) % 251 + 1)
                                 for index in range(2)) * int(rate * seconds))
    return path


def cache_token(cache, name, role, text, *, suffix='original.wav'):
    """One cache folder holding a recording and the record that names its text.

    The layout is the archive's own: a folder per recording, ``complete.json``
    naming the role/text/voice, and one ``original.*`` beside it.  A record whose
    file is not where its kind says is *skipped* by the scanner, which is exactly
    how a wrong fixture would silently turn every hit into "unverified".
    """
    folder = Path(cache) / name
    folder.mkdir(parents=True, exist_ok=True)
    path = recording(folder / suffix)
    (folder / 'complete.json').write_text(json.dumps({
        'spec': {'kind': 'jianying_original', 'role': role, 'text': text,
                 'voice': VOICES[role]},
        'raw': 0.5, 'rendered': 0.5, 'audio_digest': name}), encoding='utf-8')
    return path


@pytest.fixture
def source(tmp_path):
    """A 3-word project whose recordings live in an ``audio-cache`` tree."""
    archive = tmp_path / 'archive'
    cache = archive / 'audio-cache'
    refs = []
    for record_id, index, word, meaning in WORDS:
        for role, text in (('female', word), ('male', word), ('chinese', meaning)):
            path = cache_token(cache, '%s-%s' % (record_id, role), role, text)
            refs.append(AssetRef('%s:%s' % (record_id, role), str(path), units=24000,
                                 unit_num=1, unit_den=48000, kind='audio',
                                 voice=VOICES[role]))
    folder = tmp_path / 'root' / 'projects' / 'p1'
    folder.mkdir(parents=True)
    records = tuple(Record(id=record_id, word=word, phonetic='/x/', meaning='n. %s' % word,
                           spoken_meaning=meaning, index=index)
                    for record_id, index, word, meaning in WORDS)
    media = {ref.asset_id: MediaInfo(ref.asset_id, ref.units, ref.unit_num, ref.unit_den)
             for ref in refs}
    project = instantiate(DEFAULT_LESSON_TEMPLATE, records, media,
                          Project(project_id='p1', intro_s=0.0, speed=1.25))
    save_project(project, folder)
    AssetIndex.of(tuple(refs), project_id='p1', folder=str(folder)).save(folder)
    # The delivery background belongs to the delivery, and the editor keeps it in a
    # sidecar: the CLI must honour it rather than ask for the same path again.
    (folder / 'background.mp4').write_bytes(b'picture')
    (folder / 'delivery.json').write_text(json.dumps({
        'schema': 'wv-delivery@1', 'background': str(folder / 'background.mp4'),
        'intro_video': '', 'intro_audio': '', 'video_codec': 'h264'}), encoding='utf-8')
    return tmp_path / 'root', folder


def run_cli(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(['--root', str(root), *argv], stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), err.getvalue()


def plan_arguments(folder, *extra):
    return ('batch', 'plan', '--project', str(folder), '--batch', '151-153', *extra)


def files_under(path):
    return sorted(str(item.relative_to(path)) for item in Path(path).rglob('*')
                  if item.is_file())


# ---------------------------------------------------------------------------
# The rule and the split
# ---------------------------------------------------------------------------
def test_a_rule_cuts_the_selection_in_order_and_never_loses_a_word():
    records = tuple(Record(id='w%d' % index, word='w', index=index)
                    for index in range(151, 161))
    chunks = split_packages(records, PackageRule(per_package=4))
    assert [len(chunk) for chunk in chunks] == [4, 4, 2]
    assert [record.index for chunk in chunks for record in chunk] == \
        [record.index for record in records]
    even = split_packages(records, PackageRule(count=3))
    assert [len(chunk) for chunk in even] == [4, 3, 3]
    assert [len(chunk) for chunk in split_packages(records, PackageRule())] == [10]
    assert split_packages((), PackageRule(per_package=4)) == ()
    assert PackageRule().describe() == {'per_package': 0, 'count': 0}


def test_impossible_rules_are_refused():
    with pytest.raises(SchemaError):
        PackageRule(per_package=2, count=2)
    with pytest.raises(SchemaError):
        PackageRule(per_package=-1)
    with pytest.raises(SchemaError):
        PackageRule(per_package=True)
    with pytest.raises(SchemaError):
        split_packages((Record(id='w1', word='a', index=1),), PackageRule(count=3))


def test_the_same_batch_planned_twice_has_the_same_id(source):
    root, folder = source
    from word_video.application.batches import BatchSelection
    first = batch_id_for('p1', BatchSelection(first=151, last=153),
                         PackageRule(per_package=2), 'lesson-default', 1)
    again = batch_id_for('p1', BatchSelection(first=151, last=153),
                         PackageRule(per_package=2), 'lesson-default', 1)
    other = batch_id_for('p1', BatchSelection(first=151, last=153),
                         PackageRule(per_package=3), 'lesson-default', 1)
    assert first == again and first != other
    assert first.startswith('b151-153-') and '\\' not in first and '/' not in first


# ---------------------------------------------------------------------------
# The plan is a read-only document
# ---------------------------------------------------------------------------
def test_planning_a_batch_writes_nothing_and_counts_what_exists(source):
    root, folder = source
    before = files_under(root)
    code, document, _ = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert code == 0, document
    result = document['result']
    assert result['wrote_files'] is False and result['ready'] is True
    assert files_under(root) == before
    plan = result['plan']
    assert plan['schema'] == 'wv-batch@1'
    assert [item['package_id'] for item in plan['packages']] == ['p151-152', 'p153-153']
    assert plan['packages'][0]['record_ids'] == ['w151', 'w152']
    assert plan['packages'][0]['project_id'].endswith('-p151-152')
    # Every recording exists and its text matches, so nothing would be synthesised.
    assert plan['usage']['hits'] == 9 and plan['usage']['misses'] == 0
    assert plan['usage']['units'] == 0 and plan['usage']['unverified'] == 0
    assert plan['usage']['input_bytes'] > 0
    assert plan['disk']['free_bytes'] > 0
    # The delivery came from the editor's sidecar, not from a repeated flag.
    assert plan['profile']['background'].endswith('background.mp4')
    assert plan['packages'][0]['delivery']['ready'] is True


def test_an_edited_word_costs_exactly_its_own_recordings(source):
    root, folder = source
    code, before, _ = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert before['result']['plan']['usage']['units'] == 0

    # The member fixes one English word: the two English readings are now the old
    # word, and the miss count has to say so instead of reusing the wrong audio.
    stored = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
    stored['records'][0]['word'] = 'apples'
    (folder / 'project.json').write_text(json.dumps(stored, ensure_ascii=False),
                                         encoding='utf-8')
    code, after, _ = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert code == 0
    usage = after['result']['plan']['usage']
    assert usage['units'] == 2 and usage['misses'] == 2
    assert [(item['role'], item['text'], item['was']) for item in usage['changed']] == \
        [('female', 'apples', 'apple'), ('male', 'apples', 'apple')]
    assert usage['hits'] == 7
    # Only that package's number moved; the other package is untouched.
    first, second = after['result']['plan']['packages']
    assert first['usage']['units'] == 2 and second['usage']['units'] == 0
    assert first['digest'] != [item for item in before['result']['plan']['packages']
                               if item['package_id'] == 'p151-152'][0]['digest']


def test_each_package_instance_carries_only_its_own_records(source):
    root, folder = source
    coordinator = Coordinator(root)
    project = coordinator.load_project(str(folder))
    index = AssetIndex.load(folder)
    template = TemplateDocument(template_id='lesson-styled', version=1,
                                styles=(StyleOverride('english', (('size', 300),)),))
    records = {record.id: record for record in project.records}
    first = instance_for(project, (records['w151'], records['w152']), template,
                         index.media_map(), 'batch-p1',
                         asset_ids=None)
    second = instance_for(project, (records['w153'],), template, index.media_map(),
                          'batch-p2')
    assert [record.id for record in first.records] == ['w151', 'w152']
    assert not set(record.id for record in first.clips) & {'w153'}
    assert first.project_id == 'batch-p1' and first.revision == 0
    assert first.style_table() == {'english': {'size': 300}}    # the template's styles
    assert second.style_table() == {'english': {'size': 300}}
    # An instance built for a template without styles keeps the source's own.
    plain = instance_for(project, (records['w153'],), TemplateDocument(
        template_id='lesson-default', version=1), index.media_map(), 'batch-p3')
    assert plain.style_table() == project.style_table()


def test_an_instance_a_member_edited_is_not_overwritten(source):
    root, folder = source
    coordinator = Coordinator(root)
    project = coordinator.load_project(str(folder))
    index = AssetIndex.load(folder)
    template = TemplateDocument(template_id='lesson-default', version=1)
    records = {record.id: record for record in project.records}
    instance = instance_for(project, (records['w151'],), template, index.media_map(),
                            'batch-edited')
    coordinator.create_instance(instance, index)
    assert coordinator.instance_revision('batch-edited') == 0
    # Revision 0 is a derived instance nobody has touched, so a re-plan may rebuild
    # it; the moment a member saves it, that stops being true.
    coordinator.create_instance(instance, index)
    assert coordinator.instance_revision('batch-edited') == 0
    from word_video.application import MoveClip, apply
    edited = apply(instance, MoveClip('w151.male', 48000)).project
    coordinator.save_project(edited, expected_revision=0)
    assert coordinator.instance_revision('batch-edited') == 1
    with pytest.raises(NeedsInput) as error:
        coordinator.create_instance(instance, index)
    assert 'member edits' in error.value.message
    assert 'replace_edits' in ' '.join(error.value.fixes)
    assert coordinator.instance_revision('batch-edited') == 1   # nothing was thrown away
    # Saying so explicitly is allowed, and then it is the caller's decision.
    coordinator.create_instance(instance, index, replace_edits=True)
    assert coordinator.instance_revision('batch-edited') == 0


# ---------------------------------------------------------------------------
# The preflight: the numbers a caller approves, and the misses it names
# ---------------------------------------------------------------------------
def test_the_preflight_reports_hits_units_disk_and_the_target(source):
    root, folder = source
    code, document, _ = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert code == 0, document
    checks = document['result']['checks']
    assert checks['ready'] is True and checks['blocking'] == []
    by_name = {item['name']: item for item in checks['checks']}
    assert by_name['media']['hits'] == 9 and by_name['media']['units'] == 0
    assert all(by_name['fonts']['resolved'].values())      # every role resolves a face
    assert by_name['voices']['recorded'] == 9 and by_name['voices']['unrecorded'] == []
    assert by_name['disk']['free_bytes'] > 0 and by_name['disk']['packages'] == 2
    assert by_name['target']['codec'] == 'h264'
    assert (by_name['target']['width'], by_name['target']['height']) == (3840, 2160)
    assert by_name['target']['fps'] == '60/1'


def test_a_target_this_build_cannot_deliver_is_refused_with_a_fix():
    from word_video.application.batches import ExportProfile
    from word_video.application.packages import check_disk, check_fonts, check_target
    from word_video.domain import Project
    codec = check_target(ExportProfile(video_codec='vp9'), Project())
    assert codec.blocking and [item.code for item in codec.problems] == ['TARGET_CODEC']
    assert any('--codec' in fix for fix in codec.fixes)
    odd = check_target(ExportProfile(), Project(width=321, height=180))
    assert odd.blocking and [item.code for item in odd.problems] == ['TARGET_CANVAS']
    disk = check_disk(1000, 500, [object(), object(), object()])
    assert disk.blocking and disk.detail['required_bytes'] == 1500
    assert any('清理' in fix for fix in disk.fixes)


def test_a_font_that_resolves_to_nothing_is_a_blocking_problem(monkeypatch):
    """The exporter refuses a style role with no font; the plan must say so first."""
    from word_video.application.packages import check_fonts
    import word_video.template as template
    monkeypatch.setattr(template, 'resolve_fonts', lambda: {})
    check = check_fonts({})
    assert check.blocking is True
    assert {item.code for item in check.problems} == {'FONT_MISSING'}
    assert 'english' in check.detail['missing'] and check.fixes


def test_a_recording_that_is_gone_is_a_blocking_problem_with_a_fix(source):
    root, folder = source
    Path(AssetIndex.load(folder).path('w151:female')).unlink()
    code, document, _ = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert code == 0, document
    result = document['result']
    assert result['ready'] is False
    media = [item for item in result['checks']['checks'] if item['name'] == 'media'][0]
    assert media['blocking'] is True and media['missing'][0]['asset_id'] == 'w151:female'
    assert any('assets.json' in fix for fix in media['fixes'])
    code, refused, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                               '--batch', '151-153', '--key', 'k1')
    assert code == 2 and refused['error']['code'] == 'NEEDS_INPUT'


# ---------------------------------------------------------------------------
# The arrangement a batch inherits
# ---------------------------------------------------------------------------
def intro_project(folder, records, media, *, seconds=1.0):
    """A source project that carries an intro layer of its own.

    The layer is placed with a *measurement* rather than a real movie: measuring the
    media is the IO edge (``measure_project_intro``, exercised with real files by the
    acceptance run), while what is under test here is which arrangement a batch keeps.
    """
    from word_video.application import instantiate
    from word_video.domain import DEFAULT_LESSON_TEMPLATE, MediaSlice, Project
    from word_video.domain.compile import IntroMeasurement

    asset = 'layer.intro.mov'
    measure = IntroMeasurement(asset_id=asset, seconds=seconds, sound_asset='',
                               sound_source='silent', from_clip=False,
                               picture_seconds=seconds)
    slice_ = MediaSlice(asset_id=asset, source_start=0, source_end=60, unit_num=1,
                        unit_den=60, speed=1.0)
    template = replace(DEFAULT_LESSON_TEMPLATE, intro=True)
    project = instantiate(template, records, media,
                          Project(project_id='p1', intro_s=0.0, speed=1.25),
                          intro=slice_, intro_audio='', intro_measure=measure)
    save_project(project, folder)
    return project, measure


def test_the_reference_layout_keeps_the_intro_and_a_captured_one_pins_it(source,
                                                                        monkeypatch):
    """A member's layer is neither dropped nor invented — and the default keeps it.

    Nothing pinned means the **reference** layout is in use, and the reference is the
    layout the verified engine draws: that engine draws the countdown the project has,
    so the plan keeps it too (``intro_from: source``).  Pinning a template is the
    explicit choice, and *that* is where a template disagreeing with the source is
    refused — see ``test_wv_blocker_fix_closure`` for the refusal and its fix.
    """
    import importlib
    import word_video.cli.main as cli_function
    cli_module = importlib.import_module('word_video.cli.main')
    assert cli_function is not None
    from word_video.application.templates import TemplateDocument
    from word_video.storage.templates import TemplateStore

    root, folder = source
    coordinator = Coordinator(root)
    loaded = coordinator.load_project(str(folder))
    index = AssetIndex.load(folder)
    records = {record.id: record for record in loaded.records}
    project, measure = intro_project(folder, tuple(records.values()), index.media_map(),
                                     seconds=1.0)
    monkeypatch.setattr(cli_module, 'measure_project_intro', lambda *a, **k: measure)

    # The built-in template declares no intro layer of its own, and the reference
    # follows the project rather than dropping the layer the member placed.
    code, document, err = run_cli(root, *plan_arguments(folder, '--per-package', '2'))
    assert code == 0, (document, err)
    result = document['result']
    assert result['ready'] is True and result['plan']['problems'] == []
    arrangement = [item for item in result['checks']['checks']
                   if item['name'] == 'template'][0]
    assert arrangement['intro_in_source'] is True
    assert arrangement['intro_in_template'] is False
    assert arrangement['intro_from'] == 'source'
    assert arrangement['intro_used'] is True

    # Recording the arrangement the member actually has pins it, and then the layer is
    # in the instance, measured from its own media.
    TemplateStore(root).save(TemplateDocument(
        template_id='p1-lesson', version=1, lesson=replace(DEFAULT_LESSON_TEMPLATE,
                                                           intro=True)))
    code, planned, err = run_cli(root, *plan_arguments(folder, '--template',
                                                       'p1-lesson'))
    assert code == 0 and planned['result']['ready'] is True, (planned, err)
    code, submitted, err = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                   '--batch', '151-153', '--per-package', '2',
                                   '--template', 'p1-lesson', '--key', 'k1')
    assert code == 0, (submitted, err)
    submitted = submitted['result']
    instance = coordinator.load_project(submitted['packages'][0]['project_id'])
    clip = instance.intro_clip()
    assert clip is not None and clip.source.asset_id == 'layer.intro.mov'
    assert clip.duration_ticks == 720000                  # exactly 1.0s at 60 fps


def test_a_template_that_wants_an_intro_the_source_lacks_is_a_named_problem(source):
    """An intro cannot be invented: the plan says so instead of raising on the way in."""
    from word_video.application.packages import plan_document
    from word_video.application.templates import TemplateDocument
    from word_video.storage.templates import TemplateStore

    root, folder = source
    coordinator = Coordinator(root)
    loaded = coordinator.load_project(str(folder))
    index = AssetIndex.load(folder)
    template = TemplateDocument(template_id='lesson-with-intro', version=1,
                                lesson=replace(DEFAULT_LESSON_TEMPLATE, intro=True))
    document = plan_document(loaded, index.media_map(), index, template=template)
    assert document.ready is False
    codes = {problem.code for problem in document.problems}
    assert codes == {'TEMPLATE_INTRO_MISMATCH'}
    assert any('没有片头素材' in problem.message for problem in document.problems)
    assert document.fixes()                                # a fix an Agent can act on
    # The same document is what `batch plan` prints, with nothing written.
    TemplateStore(root).save(template)
    before = files_under(root)
    code, printed, _ = run_cli(root, *plan_arguments(folder, '--template',
                                                     'lesson-with-intro'))
    assert code == 0 and printed['result']['ready'] is False
    assert files_under(root) == before


# ---------------------------------------------------------------------------
# Submit: one instance and one job per package, keys derived from the caller's
# ---------------------------------------------------------------------------
def test_submitting_a_batch_creates_one_instance_and_job_per_package(source):
    root, folder = source
    code, document, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                               '--batch', '151-153', '--per-package', '2',
                               '--key', 'k1')
    assert code == 0, document
    result = document['result']
    assert result['batch'].startswith('b151-153-x2-')
    assert len(result['packages']) == 2
    batch = BatchStore(root).load(result['batch'])
    for entry in result['packages']:
        instance = root / 'projects' / entry['project_id']
        assert (instance / 'project.json').is_file()
        assert (instance / 'assets.json').is_file()
        assert not set(entry['records']) & {'w153'} if entry['package_id'] == 'p151-152' \
            else entry['records'] == ['w153']
    # The same submit again is the same work: same jobs, nothing created twice.
    again = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                    '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    assert [item['job'] for item in again['packages']] == \
        [item['job'] for item in result['packages']]
    assert all(item['created'] is False for item in again['packages'])
    keys = [row['idempotency_key'] for row in Coordinator(root).receipts()]
    assert sorted(keys) == ['k1#p151-152', 'k1#p153-153']
    assert batch.selection.first == 151 and batch.rule.per_package == 2


def test_showing_a_batch_reports_every_package_and_its_job(source):
    root, folder = source
    job_batch = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    code, document, _ = run_cli(root, 'batch', 'show', '--id', job_batch['batch'])
    assert code == 0, document
    assert [item['job'] for item in document['result']['packages']] == \
        [item['job'] for item in job_batch['packages']]
    assert {item['state'] for item in document['result']['packages']} == {'queued'}
    code, listing, _ = run_cli(root, 'batch', 'list')
    assert code == 0
    assert [item['batch_id'] for item in listing['result']['batches']] == \
        [job_batch['batch']]
    assert listing['result']['batches'][0]['packages'] == 2
    code, missing, _ = run_cli(root, 'batch', 'show', '--id', 'nope')
    assert code == 2 and missing['error']['code'] == 'NEEDS_INPUT'


# ---------------------------------------------------------------------------
# Redo: only what failed or changed
# ---------------------------------------------------------------------------
def publish(coordinator, job_id):
    """Mark a job as a finished delivery without rendering (the classification test).

    The digests are the real ones: ``reconcile`` verifies them, so a job published here
    is genuinely "healthy" until its files are touched — which is what lets a test tell
    a lost delivery from an intact one.
    """
    from word_video.storage.coordinator import connect
    target = coordinator.run_folder(job_id)
    (target / 'video').mkdir(parents=True, exist_ok=True)
    paths = []
    for name, kind in (('video/video.mp4', 'mp4'), ('srt/0151-0153_01_英文重复.srt', 'srt'),
                       ('editable-draft/draft_content.json', 'json')):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
        paths.append((str(path), kind))
    with connect(coordinator.db) as con:
        for path, kind in paths:
            con.execute('INSERT INTO artifacts (job_id,path,sha256,kind,published)'
                        ' VALUES (?,?,?,?,?)',
                        (job_id, path, hashlib.sha256(b'x').hexdigest(), kind, 0.0))
        con.execute("UPDATE jobs SET state='succeeded', phase='done' WHERE id=?",
                    (job_id,))
    return [path for path, _ in paths]


def test_a_redo_reuses_a_published_package_and_reruns_only_the_rest(source):
    root, folder = source
    submitted = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    coordinator = Coordinator(root)
    first, second = submitted['packages']
    publish(coordinator, first['job'])
    code, document, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'])
    assert code == 0, document
    result = document['result']
    assert result['untouched'] == ['p151-152']
    assert result['redo'] == ['p153-153']
    assert result['units'] == 0                       # nothing has to be synthesised
    scope = {item['package_id']: item for item in result['scope']}
    assert scope['p151-152']['action'] == 'reuse'
    assert scope['p153-153']['action'] == 'rerun'
    assert scope['p153-153']['job'] == second['job']
    assert result['applied'] is False
    # Nothing was touched by a dry run: the published package still has its run.
    assert coordinator.status(first['job'])['state'] == 'succeeded'
    assert (root / 'runs' / first['job']).is_dir()


def test_changing_one_word_redoes_only_that_package(source):
    root, folder = source
    submitted = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    coordinator = Coordinator(root)
    first, second = submitted['packages']
    publish(coordinator, first['job'])
    publish(coordinator, second['job'])
    stored = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
    stored['records'][2]['word'] = 'cherries'         # the word in p153-153
    (folder / 'project.json').write_text(json.dumps(stored, ensure_ascii=False),
                                         encoding='utf-8')
    code, document, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'])
    result = document['result']
    assert code == 0
    assert result['untouched'] == ['p151-152'] and result['redo'] == ['p153-153']
    assert result['units'] == 2                       # the two English readings only
    scope = {item['package_id']: item for item in result['scope']}
    assert scope['p153-153']['action'] == 'resubmit'
    assert scope['p153-153']['reason'] and '内容变了' in scope['p153-153']['reason']
    assert scope['p151-152']['digest_now'] == scope['p151-152']['digest_then']


def test_redo_apply_submits_only_the_changed_package(source):
    root, folder = source
    submitted = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    coordinator = Coordinator(root)
    first, second = submitted['packages']
    publish(coordinator, first['job'])
    publish(coordinator, second['job'])
    stored = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
    stored['records'][2]['word'] = 'cherries'
    (folder / 'project.json').write_text(json.dumps(stored, ensure_ascii=False),
                                         encoding='utf-8')
    before = [row['idempotency_key'] for row in coordinator.receipts()]
    code, document, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'],
                               '--apply')
    assert code == 0, document
    result = document['result']
    assert result['applied'] is True
    assert [item['package_id'] for item in result['prepared']] == ['p153-153']
    assert result['prepared'][0]['created'] is True
    keys = [row['idempotency_key'] for row in coordinator.receipts()]
    assert len(keys) == len(before) + 1               # exactly one new submission
    # The reused package was not re-rendered and its job was not touched.
    assert coordinator.status(first['job'])['state'] == 'succeeded'
    assert [item['job'] for item in result['prepared']] != [first['job']]


def test_a_lost_delivery_is_requeued_and_repaired(source):
    """A published delivery whose files vanished is repaired, not declared finished.

    ``reconcile`` marks it ``partial_failed`` (never success), and ``batch redo`` has to
    be able to put it back in the queue: otherwise the one state a member actually hits
    — a run folder they deleted, a disk hiccup — would be the one state a redo cannot
    fix, while every other package is reused.
    """
    root, folder = source
    submitted = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    coordinator = Coordinator(root)
    first, second = submitted['packages']
    publish(coordinator, first['job'])
    publish(coordinator, second['job'])
    import shutil
    shutil.move(str(coordinator.run_folder(second['job'])),
                str(root / 'moved-away'))
    reconciled = run_cli(root, 'reconcile')[1]['result']
    assert reconciled['partial_failed'] == [second['job']]
    code, scope, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'])
    assert code == 0
    assert scope['result']['untouched'] == [first['package_id']]
    assert scope['result']['redo'] == [second['package_id']]
    # Re-queueing is the repair, and only a delivery that no longer verifies may do it.
    with pytest.raises(JobStateError) as error:
        coordinator.requeue(first['job'])
    assert 'partial_failed' in error.value.message
    # A leftover in the run folder is kept aside, never merged into the next run.
    leftover = coordinator.run_folder(second['job'])
    leftover.mkdir(parents=True)
    (leftover / 'half-written.mp4').write_bytes(b'x')
    repaired = coordinator.requeue(second['job'])
    assert repaired['state'] == 'queued' and repaired['artifacts'] == []
    assert not leftover.exists()
    assert list((root / 'recovery').iterdir())
    # ... and a job that never ran is not "repaired" this way either.
    with pytest.raises(JobStateError):
        coordinator.requeue(second['job'])
    assert coordinator.status(first['job'])['state'] == 'succeeded'
def test_an_edited_instance_is_the_truth_for_its_package(source):
    """A member's edit is never overwritten by a redo: it is what gets rendered."""
    root, folder = source
    submitted = run_cli(root, 'batch', 'submit', '--project', str(folder), '--batch',
                        '151-153', '--per-package', '2', '--key', 'k1')[1]['result']
    coordinator = Coordinator(root)
    first = submitted['packages'][0]
    instance = coordinator.load_project(first['project_id'])
    from word_video.application import MoveClip, apply
    edited = apply(instance, MoveClip('w151.male', 96000)).project
    coordinator.save_project(edited, expected_revision=0)
    assert coordinator.instance_revision(first['project_id']) == 1
    code, document, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'])
    scope = {item['package_id']: item for item in document['result']['scope']}
    assert code == 0
    assert scope['p151-152']['basis'] == 'instance'
    assert scope['p151-152']['action'] == 'resubmit'
    assert '成员改过实例' in scope['p151-152']['reason']
    assert scope['p151-152']['digest_now'] != scope['p151-152']['digest_then']
    # Applying it submits the edited instance itself, and does not rebuild it.
    revision_before = coordinator.instance_revision(first['project_id'])
    code, applied, _ = run_cli(root, 'batch', 'redo', '--id', submitted['batch'],
                              '--apply', '--key', 'k2')
    assert code == 0, applied
    assert coordinator.instance_revision(first['project_id']) == revision_before
    assert coordinator.load_project(first['project_id']).clip('w151.male').start.ticks \
        == instance.clip('w151.male').start.ticks + 96000
