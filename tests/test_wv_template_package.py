"""A template package: portable, checkable, and impossible to make executable.

The point of a package is that it can be handed to another machine, so the tests hold
exactly what "portable" costs: the folder carries a manifest and the document and
nothing else, it holds no absolute path and no credential, the receiving root has to be
able to read it without knowing anything about the sender's disk, and an import never
rewrites a version — the version is a fact once a batch has pinned it.

The refusals matter as much as the happy path: an extra file, an edited document, a
dropped script or a template captured with a machine font path must all be *refused*,
not quietly accepted with a warning nobody reads.
"""
import io
import json
from pathlib import Path

import pytest

from word_video.application.templates import TemplateDocument
from word_video.cli.main import main
from word_video.domain import StyleOverride
from word_video.domain.errors import SchemaError
from word_video.storage.template_package import (MANIFEST_FILENAME, PACKAGE_SCHEMA,
                                                 document_scan, folder_scan,
                                                 read_package, write_package)
from word_video.storage.templates import TemplateStore


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), err.getvalue()


@pytest.fixture
def styled(tmp_path):
    """One saved template version under ``root-a``, with a style to be portable."""
    root = tmp_path / 'root-a'
    store = TemplateStore(root)
    document = TemplateDocument(template_id='lesson-styled', version=1,
                                styles=(StyleOverride('english', (('size', 450),)),),
                                note='英文加大')
    store.save(document)
    return root, store, document


def files_of(folder):
    return sorted(str(path.relative_to(folder)).replace('\\', '/')
                  for path in Path(folder).rglob('*') if path.is_file())


# ---------------------------------------------------------------------------
# Export: a folder that is data and only data
# ---------------------------------------------------------------------------
def test_export_writes_a_manifest_and_the_document_and_nothing_else(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    code, result, err = run_cli('--root', str(root), 'template', 'export',
                                '--template', 'lesson-styled', '--out', str(out))
    assert code == 0, (result, err)
    assert files_of(out) == ['package.json', 'template.json']
    manifest = json.loads((out / MANIFEST_FILENAME).read_text(encoding='utf-8'))
    assert manifest['schema'] == PACKAGE_SCHEMA
    assert (manifest['template_id'], manifest['version']) == ('lesson-styled', 1)
    assert [item['path'] for item in manifest['files']] == ['template.json']
    assert manifest['files'][0]['sha256'] and manifest['files'][0]['bytes'] > 0
    # The whole payload is the versioned document; it is readable as one.
    carried = TemplateDocument.from_dict(
        json.loads((out / 'template.json').read_text(encoding='utf-8')))
    assert carried.to_dict() == document.to_dict()


def test_a_package_is_free_of_paths_secrets_and_scripts(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    write_package(document, out)
    scan = folder_scan(out)
    assert scan['problems'] == [] and scan['scripts'] == []
    carried = json.loads((out / 'template.json').read_text(encoding='utf-8'))
    problems, strings = document_scan(carried)
    assert problems == []
    # Nothing in the document even looks like a disk path on this machine.
    assert not [text for text in strings
                if text.startswith(('C:', 'D:', '\\\\', '/')) or ':\\' in text]


def test_a_template_that_carried_a_machine_path_is_refused_not_stripped(tmp_path):
    """A path belongs to the machine, not to a template, so a package never carries one.

    Two independent guards, and the test holds both: the typed model refuses a font
    *field* outright (the resolution chain owns fonts), and the package writer refuses
    any string in the document that is an absolute path — a ``note`` a member pasted a
    path into, for instance.
    """
    from word_video.domain.model import StyleOverride as Override
    with pytest.raises(SchemaError):
        TemplateDocument(template_id='lesson-fonted', version=1,
                         styles=(Override('english', (('font',
                                                       r'C:\Windows\Fonts\arial.ttf'),)),))
    problems, _ = document_scan({'template_id': 't', 'version': 1,
                                 'note': r'D:\1\1-AI_workflow\fonts'})
    assert [item['code'] for item in problems] == ['ABSOLUTE_PATH']
    document = TemplateDocument(template_id='lesson-noted', version=1,
                                note=r'见 C:\Users\Administrator\字体说明')
    with pytest.raises(SchemaError) as error:
        write_package(document, tmp_path / 'pkg')
    assert '绝对路径' in str(error.value)
    assert not (tmp_path / 'pkg').exists()          # refused before writing anything
    # A credential pasted into the note is refused for the same reason.
    secret = TemplateDocument(template_id='lesson-secret', version=1,
                              note='token: sk-abcdefghijklmnop')
    problems, _ = document_scan(secret.to_dict())
    assert [item['code'] for item in problems] == ['SECRET_VALUE']


# ---------------------------------------------------------------------------
# Import: another root, another disk, the same template
# ---------------------------------------------------------------------------
def test_a_package_copy_imports_at_another_root(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    assert run_cli('--root', str(root), 'template', 'export', '--template',
                   'lesson-styled', '--out', str(out))[0] == 0
    # Copy it somewhere else entirely, as a member's USB stick would be.
    moved = tmp_path / 'elsewhere' / 'handed-over'
    moved.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(out, moved)
    other = tmp_path / 'root-b'
    code, result, err = run_cli('--root', str(other), 'template', 'import',
                                '--from', str(moved))
    assert code == 0, (result, err)
    imported = TemplateStore(other).load('lesson-styled', 1)
    assert imported.to_dict() == document.to_dict()
    assert imported.style_table() == {'english': {'size': 450}}
    # ... and the receiving root can answer the same questions the sender could.
    shown = run_cli('--root', str(other), 'template', 'show', '--template',
                    'lesson-styled')[1]['result']
    assert shown['styles'] == {'english': {'size': 450}}
    assert shown['versions'] == [1]


def test_import_never_rewrites_a_version(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    write_package(document, out)
    with pytest.raises(SchemaError) as error:
        from word_video.storage.template_package import import_package
        import_package(out, store)
    assert 'already exists' in str(error.value)
    assert (root / 'templates' / 'lesson-styled' / 'v1.json').is_file()


def test_an_edited_or_extended_package_is_refused(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    write_package(document, out)
    stored = json.loads((out / 'template.json').read_text(encoding='utf-8'))
    stored['styles'] = [{'role': 'english', 'fields': {'size': 999}}]
    (out / 'template.json').write_text(json.dumps(stored, ensure_ascii=False),
                                       encoding='utf-8')
    with pytest.raises(SchemaError) as error:
        read_package(out)
    assert 'sha256' in str(error.value)
    # Restore the digest, then drop a script in: the folder itself is checked too.
    write_package(document, tmp_path / 'pkg2')
    (tmp_path / 'pkg2' / 'run.py').write_text('print(1)\n', encoding='utf-8')
    scan = folder_scan(tmp_path / 'pkg2')
    assert [item['code'] for item in scan['problems']] == ['SCRIPT_IN_PACKAGE']
    with pytest.raises(SchemaError) as error:
        read_package(tmp_path / 'pkg2')
    assert 'run.py' in str(error.value)
    code, document_result, _ = run_cli('--root', str(root), 'template', 'verify',
                                       '--from', str(tmp_path / 'pkg2'))
    assert code == 1 and document_result['error']['code'] == 'SCHEMA'


def test_verify_reads_without_importing(styled, tmp_path):
    root, store, document = styled
    out = tmp_path / 'pkg'
    write_package(document, out)
    other = tmp_path / 'root-b'
    code, result, err = run_cli('--root', str(other), 'template', 'verify',
                                '--from', str(out))
    assert code == 0, (result, err)
    assert result['result']['styles'] == {'english': {'size': 450}}
    assert result['result']['scan']['problems'] == []
    assert not (other / 'templates').exists()        # verifying writes nothing


def test_save_only_adds_the_next_version(styled, tmp_path):
    """A template version is a fact once a batch pinned it: saving never overwrites."""
    root, store, document = styled
    again = TemplateDocument(template_id='lesson-styled', version=1)
    with pytest.raises(SchemaError):
        store.save(again)
    assert store.versions('lesson-styled') == [1]
