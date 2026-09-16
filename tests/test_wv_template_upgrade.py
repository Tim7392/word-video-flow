"""Upgrading a batch to a newer template version: what must survive the move.

A template upgrade is the one operation that can silently destroy a member's work, so
these tests hold the four properties that make it safe:

* the newer version's own changes are applied **field by field** — a field it does not
  mention keeps whatever the instance says;
* a member's hand edit stays theirs, and where the template also moved that field the
  two values are reported together (a conflict the member can compare), with the
  template winning only when the caller says ``--take-template``;
* the upgrade is recorded before it is written, so ``--undo`` puts back the same bytes
  — not a re-derivation that might differ;
* only the batch that was explicitly upgraded moves: another batch pinning the same
  template keeps its own version, and an arrangement change is refused instead of
  rebuilding clips the member had edited.
"""
from dataclasses import replace
import io
import json
from pathlib import Path
import wave

import pytest

from word_video.application import MoveClip, SetStyle, apply
from word_video.application.templates import TemplateDocument
from word_video.application.upgrade import (merge_styles, plan_upgrade,
                                            template_changes)
from word_video.cli.main import main
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, MediaInfo, Project, Record,
                               StyleOverride)
from word_video.domain.errors import SchemaError
from word_video.storage import AssetIndex, AssetRef, save_project
from word_video.storage.batches import BatchStore
from word_video.storage.coordinator import Coordinator
from word_video.storage.templates import TemplateStore

WORDS = (('w151', 151, 'apple', '苹果'), ('w152', 152, 'banana', '香蕉'),
         ('w153', 153, 'cherry', '樱桃'))
VOICES = {'female': 'BV503_streaming', 'male': 'BV504_streaming',
          'chinese': 'BV406_streaming'}


def recording(path, seconds=0.3, rate=24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes([7, 9]) * int(rate * seconds))
    return path


@pytest.fixture
def root(tmp_path):
    """A work root with a 3-word project whose recordings are registered."""
    work = tmp_path / 'root'
    folder = work / 'projects' / 'p1'
    folder.mkdir(parents=True)
    refs, media = [], {}
    for record_id, index, word, meaning in WORDS:
        for role, text in (('female', word), ('male', word), ('chinese', meaning)):
            asset_id = '%s:%s' % (record_id, role)
            path = recording(folder / 'media' / ('%s.wav' % asset_id.replace(':', '_')))
            refs.append(AssetRef(asset_id, str(path), units=7200, unit_num=1,
                                 unit_den=24000, kind='audio', voice=VOICES[role]))
            media[asset_id] = MediaInfo(asset_id, 7200, 1, 24000)
    records = tuple(Record(id=record_id, word=word, phonetic='/x/', meaning='n. %s' % word,
                           spoken_meaning=meaning, index=index)
                    for record_id, index, word, meaning in WORDS)
    from word_video.application import instantiate
    project = instantiate(DEFAULT_LESSON_TEMPLATE, records, media,
                          Project(project_id='p1', intro_s=0.0, speed=1.25))
    save_project(project, folder)
    AssetIndex.of(tuple(refs), project_id='p1', folder=str(folder)).save(folder)
    (folder / 'background.mp4').write_bytes(b'picture')
    (folder / 'delivery.json').write_text(json.dumps({
        'schema': 'wv-delivery@1', 'background': str(folder / 'background.mp4'),
        'video_codec': 'h264'}), encoding='utf-8')
    return work, folder


def run_cli(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(['--root', str(root), *argv], stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), err.getvalue()


def store_style(root, folder, size):
    """Give the source project one style override, as a member's edit would."""
    coordinator = Coordinator(root)
    project = coordinator.load_project(str(folder))
    edited = apply(project, SetStyle('english', {'size': size})).project
    coordinator.save_project(edited, expected_revision=project.revision)
    return edited


def register_template(root, folder, *, size, template_id='lesson-styled', version=0):
    """Write one template version from a project that carries a size override."""
    store_style(root, folder, size)
    argv = ['template', 'save', '--from-project', str(folder),
            '--template', template_id, '--note', 'english size %d' % size]
    if version:
        argv += ['--version', str(version)]
    code, document, err = run_cli(root, *argv)
    assert code == 0, (document, err)
    return document['result']['template']


def submit_batch(root, folder, *extra):
    code, document, err = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                 '--batch', '151-153', '--per-package', '2',
                                 '--template', 'lesson-styled', *extra, '--key', 'k1')
    assert code == 0, (document, err)
    return document['result']


def documents(root):
    """Every stored project document, for a byte-level before/after comparison."""
    found = {}
    for path in sorted((Path(root) / 'projects').rglob('project.json')):
        found[str(path.parent.name)] = json.loads(path.read_text(encoding='utf-8'))
    return found


# ---------------------------------------------------------------------------
# The merge itself
# ---------------------------------------------------------------------------
def test_only_the_fields_the_new_version_mentions_move():
    old = TemplateDocument(template_id='t', version=1,
                           styles=(StyleOverride('english', (('size', 300),)),))
    new = TemplateDocument(template_id='t', version=2,
                           styles=(StyleOverride('english', (('size', 300),
                                                             ('color', '#FFFFFF'))),))
    changes, wiring = template_changes(old, new)
    assert [(item.role, item.field, item.was, item.now) for item in changes] == \
        [('english', 'color', None, '#FFFFFF')]
    assert wiring == ()
    # The field the old version set and the new one leaves alone keeps the member's
    # value: an upgrade is a change, not a rewrite of the whole table.
    project = Project(project_id='p', styles=(StyleOverride('english', (('size', 999),)),))
    styles, applied, kept, conflicts = merge_styles(project, changes)
    assert project.style_table() == {'english': {'size': 999}}
    assert [item.field for item in applied] == ['color']
    assert kept == () and conflicts == ()


def test_a_member_edit_wins_and_is_reported_as_a_conflict():
    old = TemplateDocument(template_id='t', version=1,
                           styles=(StyleOverride('english', (('size', 300),)),))
    new = TemplateDocument(template_id='t', version=2,
                           styles=(StyleOverride('english', (('size', 800),)),))
    project = Project(project_id='p', styles=(StyleOverride('english', (('size', 450),)),))
    changes, _ = template_changes(old, new)
    styles, applied, kept, conflicts = merge_styles(project, changes)
    assert applied == () and [item.now for item in kept] == [800]
    assert [(item.member, item.template, item.took) for item in conflicts] == \
        [(450, 800, 'member')]
    assert {override.role: override.values for override in styles} == \
        {'english': {'size': 450}}                 # the member's number is still there
    # Explicitly taking the template is the caller's decision, and it is recorded too.
    styles, applied, kept, conflicts = merge_styles(project, changes, take_template=True)
    assert [item.now for item in applied] == [800] and kept == ()
    assert [item.took for item in conflicts] == ['template']
    assert {override.role: override.values for override in styles} == \
        {'english': {'size': 800}}


def test_a_value_the_member_left_at_the_old_template_is_not_a_hand_edit():
    """Three-way merge: equal to the base means "following the template", so it moves."""
    old = TemplateDocument(template_id='t', version=1,
                           styles=(StyleOverride('english', (('size', 300),)),))
    new = TemplateDocument(template_id='t', version=2,
                           styles=(StyleOverride('english', (('size', 800),)),))
    project = Project(project_id='p', styles=(StyleOverride('english', (('size', 300),)),))
    changes, _ = template_changes(old, new)
    styles, applied, kept, conflicts = merge_styles(project, changes)
    assert [item.now for item in applied] == [800] and kept == () and conflicts == ()


def test_a_newer_version_is_required_and_an_arrangement_change_is_named():
    old = TemplateDocument(template_id='t', version=1)
    with pytest.raises(SchemaError):
        template_changes(old, TemplateDocument(template_id='t', version=1))
    with pytest.raises(SchemaError):
        template_changes(old, TemplateDocument(template_id='other', version=2))
    richer = TemplateDocument(template_id='t', version=2,
                              lesson=replace(DEFAULT_LESSON_TEMPLATE, intro=True))
    changes, wiring = template_changes(old, richer)
    assert changes == ()
    assert [(item.role, item.field, item.was, item.now) for item in wiring] == \
        [('layer.intro', 'present', False, True)]
    project = Project(project_id='p')
    upgraded, result = plan_upgrade(project, old, richer)
    assert result.wiring and upgraded is project          # nothing was touched
    assert result.appliable is False


# ---------------------------------------------------------------------------
# The batch upgrade, end to end
# ---------------------------------------------------------------------------
def test_upgrading_a_batch_keeps_hand_edits_and_records_a_comparable_diff(root):
    work, folder = root
    assert register_template(work, folder, size=300)['version'] == 1
    submitted = submit_batch(work, folder)
    coordinator = Coordinator(work)
    batch = submitted['batch']
    first, second = submitted['packages']
    # The member edits one package: a style the template will also move, a clip time
    # the template knows nothing about, and a style of another role.
    instance = coordinator.load_project(first['project_id'])
    edited = apply(instance, SetStyle('english', {'size': 450})).project
    edited = apply(edited, SetStyle('phonetic', {'size': 123})).project
    edited = apply(edited, MoveClip('w151.male', 48000)).project
    coordinator.save_project(edited, expected_revision=0)
    moved_before = coordinator.load_project(first['project_id']).clip('w151.male').start.ticks
    revision_before = coordinator.instance_revision(first['project_id'])
    assert revision_before > 0                     # the member's saves are revisions
    assert coordinator.instance_revision(second['project_id']) == 0

    # A second version moves english size 300 -> 800; nothing else.
    assert register_template(work, folder, size=800)['version'] == 2
    before = documents(work)
    code, document, err = run_cli(work, 'batch', 'upgrade', '--id', batch)
    assert code == 0, (document, err)
    result = document['result']
    assert result['from'] == 'lesson-styled@1' and result['to'] == 'lesson-styled@2'
    assert result['applied'] is False and documents(work) == before     # dry run writes
    scope = {item['package_id']: item for item in result['packages']}
    assert scope[first['package_id']]['conflicts'][0]['member'] == 450
    assert scope[first['package_id']]['conflicts'][0]['template'] == 800
    assert scope[second['package_id']]['conflicts'] == []               # no edit there

    code, applied, err = run_cli(work, 'batch', 'upgrade', '--id', batch, '--apply')
    assert code == 0, (applied, err)
    assert applied['result']['applied'] is True
    after = documents(work)
    edited_instance = after[first['project_id']]
    styles = {override['role']: override['fields']
              for override in edited_instance['styles']}
    assert styles['english'] == {'size': 450}          # the member's 450 survived
    assert styles['phonetic'] == {'size': 123}         # ... and so did the other one
    moved = [clip for clip in edited_instance['clips'] if clip['id'] == 'w151.male'][0]
    assert moved['start']['ticks'] == moved_before     # the time edit survived too
    assert edited_instance['revision'] == revision_before + 1
    # The package nobody edited was still upgraded: the template's own change applies.
    other = {override['role']: override['fields']
             for override in after[second['project_id']]['styles']}
    assert other['english'] == {'size': 800}
    # The batch now pins @2, so a later redo plans against the version it was moved to.
    assert BatchStore(work).load(batch).template_version == 2


def test_undo_puts_back_the_same_documents(root):
    work, folder = root
    register_template(work, folder, size=300)
    submitted = submit_batch(work, folder)
    coordinator = Coordinator(work)
    first, second = submitted['packages']
    instance = coordinator.load_project(first['project_id'])
    edited = apply(instance, SetStyle('english', {'size': 450})).project
    coordinator.save_project(edited, expected_revision=0)
    register_template(work, folder, size=800)
    before = documents(work)

    code, document, err = run_cli(work, 'batch', 'upgrade', '--id', submitted['batch'],
                                 '--apply')
    assert code == 0, (document, err)
    assert documents(work) != before
    code, undone, err = run_cli(work, 'batch', 'upgrade', '--id', submitted['batch'],
                               '--undo')
    assert code == 0, (undone, err)
    assert undone['result']['to'] == 'lesson-styled@1'
    assert documents(work) == before                  # byte for byte, not "equivalent"
    assert BatchStore(work).load(submitted['batch']).template_version == 1


def test_only_the_upgraded_batch_moves(root):
    """Two batches share a template; upgrading one leaves the other pinned at @1."""
    work, folder = root
    register_template(work, folder, size=300)
    first_batch = submit_batch(work, folder)
    second_batch = submit_batch(work, folder, '--records', 'w153')
    register_template(work, folder, size=800)
    code, document, err = run_cli(work, 'batch', 'upgrade', '--id', first_batch['batch'],
                                 '--apply')
    assert code == 0, (document, err)
    store = BatchStore(work)
    assert store.load(first_batch['batch']).template_version == 2
    assert store.load(second_batch['batch']).template_version == 1
    # ... and the untouched batch's own package documents did not change either.
    untouched = Coordinator(work).load_project(second_batch['packages'][0]['project_id'])
    assert untouched.style_table() == {'english': {'size': 300}}


def test_an_arrangement_change_is_refused_with_a_fix_not_applied(root):
    work, folder = root
    register_template(work, folder, size=300)
    submitted = submit_batch(work, folder)
    store = TemplateStore(work)
    store.save(TemplateDocument(template_id='lesson-styled', version=2,
                                lesson=replace(DEFAULT_LESSON_TEMPLATE, intro=True)))
    before = documents(work)
    code, document, err = run_cli(work, 'batch', 'upgrade', '--id', submitted['batch'],
                                 '--apply', '--template-version', '2')
    assert code == 2, document
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert any('batch submit' in fix for fix in document['error']['fixes'])
    assert documents(work) == before
