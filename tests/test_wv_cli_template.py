"""`voice` / `template` are top-level actions, and templates are versioned files.

H0's W05 review asked for the R1.1 CLI list (`project/voice/template`) to be
complete and for the pause semantics to be written down where an Agent can read
them.  These tests hold that: the two actions exist and answer one JSON object,
`template list/show` only reads, a version is never rewritten in place, and a bad
template id cannot reach outside the templates folder.

The template *document* is also where W08's upgrade starts, so the round trip
through JSON is pinned here: what a member saved is what a later version compares
against.
"""
from dataclasses import replace
import io
import json
from pathlib import Path

import pytest

from word_video.application import instantiate
from word_video.application.styles import merged_styles
from word_video.application.templates import TemplateDocument, document_from_project
from word_video.cli.main import main
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, MediaInfo, Project, Record,
                               StyleOverride)
from word_video.domain.errors import SchemaError
from word_video.storage.templates import (BUILTIN_TEMPLATE_ID, TemplateStore,
                                          checked_id)
from word_video.storage.project_store import save_project

CHECKOUT = Path(__file__).resolve().parents[1]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), err.getvalue()


@pytest.fixture
def root(tmp_path):
    work = tmp_path / 'root'
    work.mkdir()
    return work


def files_under(path):
    return sorted(str(item.relative_to(path)) for item in Path(path).rglob('*')
                  if item.is_file())


def styled_project(root):
    """A one-word project carrying a style override, as a member would leave it."""
    media = {('w151', role): MediaInfo('w151:%s' % role, 24000)
             for role in ('female', 'male', 'chinese')}
    from word_video.application import asset_id_for
    assets = {asset_id_for('w151', role): info
              for (record_id, role), info in media.items()}
    record = Record(id='w151', word='apple', phonetic='ˈæpl', meaning='n. 苹果',
                    spoken_meaning='苹果', index=151)
    folder = root / 'projects' / 'p1'
    folder.mkdir(parents=True)
    built = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), assets,
                        Project(project_id='p1', intro_s=0.0))
    built = built.with_styles({'english': {'size': 300}})
    save_project(built, folder)
    return built


# ---------------------------------------------------------------------------
# The action list itself
# ---------------------------------------------------------------------------
def test_capabilities_lists_voice_and_template_and_the_pause_rule(root):
    code, document, _ = run_cli('--root', str(root), 'capabilities')
    assert code == 0
    actions = document['result']['actions']
    assert {'capabilities', 'doctor', 'project', 'batch', 'job', 'artifacts',
            'receipts', 'reconcile', 'watch', 'import', 'voice',
            'template'} <= set(actions)
    notes = ' '.join(document['result']['notes'])
    # An Agent must not believe a running render can be suspended.
    assert 'pause' in notes and 'checkpoint' in notes
    assert 'queued' in notes


def test_both_new_actions_refuse_bad_usage_with_one_json(root):
    code, document, _ = run_cli('--root', str(root), 'voice')
    assert code == 2 and document['ok'] is False and document['error']['code'] == \
        'BAD_REQUEST'
    code, document, _ = run_cli('--root', str(root), 'template', 'show')
    assert code == 2
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert any('--template' in fix for fix in document['error']['fixes'])


# ---------------------------------------------------------------------------
# template list / show read the versioned files
# ---------------------------------------------------------------------------
def test_a_fresh_root_already_has_the_reference_template(root):
    code, document, _ = run_cli('--root', str(root), 'template', 'list')
    assert code == 0, document
    (entry,) = document['result']['templates']
    assert entry['template_id'] == BUILTIN_TEMPLATE_ID
    assert entry['versions'] == [] and entry['latest'] == 1 and entry['builtin'] is True
    assert document['result']['problems'] == []
    assert files_under(root) == []                    # listing writes nothing

    code, document, _ = run_cli('--root', str(root), 'template', 'show',
                                '--template', BUILTIN_TEMPLATE_ID)
    assert code == 0, document
    assert document['result']['source'] == 'builtin'
    shown = document['result']['template']
    assert shown['schema'] == 'wv-template@1'
    assert shown['intro'] is False
    assert [stage['role'] for stage in shown['stages']] == ['female', 'male', 'chinese']
    assert document['result']['styles'] == {}


def test_an_unknown_template_is_a_question_listing_what_exists(root):
    code, document, _ = run_cli('--root', str(root), 'template', 'show',
                                '--template', 'nope')
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'
    assert BUILTIN_TEMPLATE_ID in json.dumps(document['error']['fixes'],
                                            ensure_ascii=False)

    code, document, _ = run_cli('--root', str(root), 'template', 'show',
                                '--template', BUILTIN_TEMPLATE_ID,
                                '--version', '9')
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'


def test_a_stored_version_is_shown_and_the_latest_is_the_default(root):
    store = TemplateStore(root)
    first = TemplateDocument(template_id='lesson-styled', version=1,
                             styles=(StyleOverride('english', (('size', 300),)),),
                             note='第一版')
    store.save(first)
    store.save(TemplateDocument(template_id='lesson-styled', version=2,
                                styles=(StyleOverride('english', (('size', 360),)),),
                                note='第二版'))

    code, document, _ = run_cli('--root', str(root), 'template', 'list')
    entry = [item for item in document['result']['templates']
             if item['template_id'] == 'lesson-styled'][0]
    assert entry['versions'] == [1, 2] and entry['latest'] == 2
    assert entry['styles'] == ['english'] and entry['note'] == '第二版'

    code, document, _ = run_cli('--root', str(root), 'template', 'show',
                                '--template', 'lesson-styled')
    assert code == 0 and document['result']['template']['version'] == 2
    assert document['result']['styles'] == {'english': {'size': 360}}
    assert Path(document['result']['path']).is_file()

    code, document, _ = run_cli('--root', str(root), 'template', 'show',
                                '--template', 'lesson-styled', '--version', '1')
    assert code == 0 and document['result']['styles'] == {'english': {'size': 300}}


def test_an_existing_version_is_never_rewritten(root):
    store = TemplateStore(root)
    document = TemplateDocument(template_id='lesson-styled', version=1)
    path = store.save(document)
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        store.save(TemplateDocument(template_id='lesson-styled', version=1,
                                    note='偷偷改掉'))
    assert path.read_bytes() == before


def test_a_template_id_cannot_climb_out_of_the_templates_folder(root):
    for bad in ('../escape', 'a/b', '', '.', '..', 'C:tmp'):
        with pytest.raises(SchemaError):
            checked_id(bad)
    store = TemplateStore(root)
    with pytest.raises(SchemaError):
        store.load('../escape')
    assert not (root.parent / 'escape').exists()


# ---------------------------------------------------------------------------
# The document round trip (what a later upgrade compares against)
# ---------------------------------------------------------------------------
def test_a_document_survives_json_and_refuses_unknown_content():
    document = TemplateDocument(
        template_id='lesson-intro', version=3,
        lesson=replace(DEFAULT_LESSON_TEMPLATE, intro=True),
        styles=(StyleOverride('english', (('size', 300), ('bold', True))),
                StyleOverride('meaning', (('y', 12),))),
        note='含片头')
    payload = json.loads(json.dumps(document.to_dict(), ensure_ascii=False))
    assert TemplateDocument.from_dict(payload) == document

    for broken in ({**payload, 'schema': 'wv-template@9'},
                   {**payload, 'unexpected': 1},
                   {**payload, 'styles': [{'role': 'english', 'fields': {'font': 'x'}}]},
                   {key: value for key, value in payload.items() if key != 'version'}):
        with pytest.raises(SchemaError):
            TemplateDocument.from_dict(broken)


def test_capturing_a_project_keeps_the_intro_and_the_overrides(root):
    built = styled_project(root)
    document = document_from_project(built, template_id='lesson-captured', version=1)
    assert document.intro is False
    assert document.style_table() == {'english': {'size': 300}}
    assert document.lesson.displays == DEFAULT_LESSON_TEMPLATE.displays
    # The capture is a real template: its overrides merge over the same defaults
    # the renderer uses, so nothing about the picture is lost on the way.
    assert merged_styles(document.style_table())['english']['size'] == 300
    assert merged_styles(document.style_table()) == merged_styles(built.styles)
