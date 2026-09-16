"""Content rules of the member package scan (packaging/build_member_package.py).

The scan is the evidence behind "the package carries no keys, no real material,
no audio cache, no full word list and no font without a distribution basis", so
the rules are tested in both directions: what must be reported and what must not
be a false alarm.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('packaging_build', 'build_member_package.py')
WORDLIST_LINE = 'peril /ˈperəl/ n. 严重危险\n'


def rules(result):
    return [item['rule'] for item in result['findings']]


def test_scan_passes_a_clean_tree(tmp_path):
    (tmp_path / 'WordVideo').mkdir()
    (tmp_path / 'WordVideo' / 'main.py').write_text('print("ok")\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('member readme\n', encoding='utf-8')
    result = builder.scan_package(tmp_path)
    assert result['errors'] == 0
    assert result['warnings'] == 0
    assert result['evidence']['files'] == 2
    assert result['evidence']['magic_checked'] == 2


def test_scan_flags_forbidden_suffix_even_when_the_bytes_are_harmless(tmp_path):
    (tmp_path / 'cached.ogg').write_bytes(b'not really audio')
    (tmp_path / 'words.sqlite3').write_bytes(b'SQLite format 3\x00')
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['forbidden-extension', 'forbidden-extension']
    assert result['errors'] == 2


def test_scan_flags_font_header_whatever_the_extension(tmp_path):
    (tmp_path / 'renamed.bin').write_bytes(b'\x00\x01\x00\x00' + b'\x00' * 64)
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['font-magic']


def test_scan_flags_media_headers_whatever_the_extension(tmp_path):
    (tmp_path / 'clip.dat').write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64)
    (tmp_path / 'sound.dat').write_bytes(b'OggS' + b'\x00' * 64)
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['media-magic', 'media-magic']


def test_scan_flags_audio_cache_paths(tmp_path):
    cache = tmp_path / 'WordVideo' / 'audio-cache' / 'ab'
    cache.mkdir(parents=True)
    (cache / 'original.ogg').write_bytes(b'\x00' * 8)
    result = builder.scan_package(tmp_path)
    assert 'forbidden-path-part' in rules(result)
    assert 'forbidden-extension' in rules(result)


def test_scan_flags_a_vocabulary_list_by_shape(tmp_path):
    (tmp_path / 'words.txt').write_text(WORDLIST_LINE * 120, encoding='utf-8')
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['wordlist-shape']
    assert result['evidence']['max_wordlist_lines'] == 120


def test_scan_does_not_mistake_code_for_a_vocabulary_list(tmp_path):
    # 'items [i]' matches the loose shape but carries no phonetic character.
    (tmp_path / 'code.py').write_text('items [i]\n' * 300, encoding='utf-8')
    result = builder.scan_package(tmp_path)
    assert result['findings'] == []
    assert result['evidence']['max_wordlist_lines'] == 0


def test_scan_reports_the_wordlist_byte_for_byte_copy(tmp_path):
    guard = tmp_path / 'guard' / '四级核心1500词_已清理.txt'
    guard.parent.mkdir()
    guard.write_text(WORDLIST_LINE * 3, encoding='utf-8')
    package = tmp_path / 'package'
    package.mkdir()
    (package / 'copy.txt').write_text(WORDLIST_LINE * 3, encoding='utf-8')
    # Same size, different bytes: the scan must still reload and compare hashes.
    (package / 'other.txt').write_text((WORDLIST_LINE * 3).replace('peril', 'PERIL'),
                                       encoding='utf-8')
    result = builder.scan_package(package, guard_files=[guard])
    flagged = [item['path'] for item in result['findings'] if item['rule'] == 'guard-file']
    assert flagged == ['copy.txt']
    assert result['evidence']['guard_files'] == 1


def test_scan_flags_secret_environment_values_without_echoing_them(tmp_path):
    secret = 'volc-access-token-9f3c1d'
    (tmp_path / 'config.json').write_text(json.dumps({'note': secret}), encoding='utf-8')
    result = builder.scan_package(tmp_path, env_values=[('VOLC_TTS_ACCESS_TOKEN', secret)])
    assert rules(result) == ['secret-env-value']
    assert result['findings'][0]['detail'] == 'contains the value of VOLC_TTS_ACCESS_TOKEN'
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_scan_flags_credential_shaped_literals(tmp_path):
    (tmp_path / 'settings.py').write_text('api_key = "0123456789abcdef0123"\n', encoding='utf-8')
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['secret-literal']


def test_scan_flags_development_paths_in_text_and_warns_in_binaries(tmp_path):
    (tmp_path / 'notes.md').write_text('built in SECRET-TREE/C\n', encoding='utf-8')
    (tmp_path / 'blob.bin').write_bytes(b'\x00\x01MZ' + b'SECRET-TREE/C' + b'\x00' * 32)
    result = builder.scan_package(tmp_path, dev_markers=('SECRET-TREE',))
    assert [(item['path'], item['severity']) for item in result['findings']] == [
        ('blob.bin', 'warning'), ('notes.md', 'error')]
    assert result['errors'] == 1 and result['warnings'] == 1


def test_scan_allows_only_the_library_document_template(tmp_path):
    allowed = tmp_path / 'WordVideo' / '_internal' / 'docx' / 'templates' / 'default.docx'
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b'PK\x03\x04' + b'\x00' * 32)
    assert builder.scan_package(tmp_path)['findings'] == []


def test_scan_still_flags_a_member_document(tmp_path):
    (tmp_path / 'words.docx').write_bytes(b'PK\x03\x04' + b'\x00' * 32)
    result = builder.scan_package(tmp_path)
    assert rules(result) == ['forbidden-extension']
    assert result['findings'][0]['detail'] == 'document material (.docx)'


def test_scan_can_exclude_the_self_describing_manifest(tmp_path):
    (tmp_path / 'VERSION.txt').write_text('files=1\n', encoding='utf-8')
    (tmp_path / 'app.py').write_text('pass\n', encoding='utf-8')
    result = builder.scan_package(tmp_path, exclude=('VERSION.txt',))
    assert result['evidence']['files'] == 1
    assert result['evidence']['excluded'] == ['VERSION.txt']


def test_version_file_reports_its_own_size(tmp_path):
    facts = {'version': '9.9.9', 'built': '2026-09-16 12:00:00 +0800', 'commit': 'abc123',
             'dirty': False, 'built_with': 'CPython 3.14.0 / PyInstaller 6.22.3',
             'tools': {'ffmpeg_version': 'test'},
             'scan': {'result': 'PASS', 'errors': 0, 'warnings': 0}}
    builder.write_version_file(tmp_path, facts)
    text = (tmp_path / builder.VERSION_NAME).read_text(encoding='utf-8')
    assert int(re.search(r'^files=(\d+)$', text, re.M).group(1)) == 1
    assert int(re.search(r'^bytes=(\d+)$', text, re.M).group(1)) \
        == (tmp_path / builder.VERSION_NAME).stat().st_size
    assert 'source_commit=abc123' in text
    assert 'content_scan=PASS errors=0 warnings=0' in text


def test_version_file_lists_every_delivered_part(tmp_path):
    facts = {'version': '1.0.0', 'built': 'now', 'commit': 'deadbeef', 'dirty': True,
             'built_with': 'x', 'tools': {},
             'scan': {'result': 'PASS', 'errors': 0, 'warnings': 0}}
    builder.write_version_file(tmp_path, facts)
    text = (tmp_path / builder.VERSION_NAME).read_text(encoding='utf-8')
    assert 'source_dirty=true' in text
    for part in (builder.APP_DIR, builder.FFMPEG_DIR, builder.LAUNCHER_NAME,
                 builder.README_NAME, builder.VERSION_NAME):
        assert part in text


def test_read_version_comes_from_the_version_file():
    assert builder.read_version(ROOT) == (PACKAGING / builder.VERSION_SOURCE).read_text(
        encoding='utf-8').strip()


def test_read_version_rejects_a_bad_value(tmp_path):
    (tmp_path / 'packaging').mkdir()
    (tmp_path / 'packaging' / builder.VERSION_SOURCE).write_text('not a version!\n',
                                                                 encoding='utf-8')
    with pytest.raises(SystemExit):
        builder.read_version(tmp_path)


def test_guard_files_cover_the_fixture_inputs():
    guards = [path.name for path in builder.guard_files(ROOT)]
    assert any(name.endswith('_已清理.txt') for name in guards)
    assert any(name.endswith('.ogg') for name in guards)


def test_readme_template_has_placeholders_and_no_development_paths():
    text = (PACKAGING / builder.README_NAME).read_text(encoding='utf-8')
    assert '{{VERSION}}' in text and '{{BUILT}}' in text and '{{COMMIT}}' in text
    for marker in ('word_video_flow', 'PycharmProjects', 'runtime\\venv'):
        assert marker not in text


def test_rendered_readme_has_no_placeholder_left():
    text = builder.render_readme((PACKAGING / builder.README_NAME).read_text(encoding='utf-8'),
                                 {'version': '0.1.0', 'built': 'now', 'commit': 'abc'})
    assert '{{' not in text
    assert '0.1.0' in text
