"""The two import verbs: read what exists, publish it, and never synthesise.

A member's existing recordings are the cheapest voice they have, and the rule that
makes them safe is "导入已有音频 ≠ 获得新文字合成能力".  These tests hold both halves:
the verbs do the member's job (scan, match, publish, audition, confirm) with one
JSON object each, and every path through them is proven to make **zero** synthesis
calls — once with a counting fake route, once inside a subprocess whose sockets are
blocked, so a path that quietly reached for the network could not pass.
"""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest
from unittest.mock import patch

from test_wv_import_cache import SyntheticTree, _token
from test_wv_import_voices import _audio, _draft, _material

from word_video.application import instantiate
from word_video.cli.main import main
from word_video.domain import DEFAULT_LESSON_TEMPLATE, MediaInfo, Project, Record
from word_video.storage.project_store import save_project

CHECKOUT = Path(__file__).resolve().parents[1]
WORD = 'apple'
SPOKEN = '苹果'
VOICE = 'BV503_streaming'


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, json.loads(out.getvalue()), err.getvalue()


@pytest.fixture
def root(tmp_path):
    work = tmp_path / 'root'
    work.mkdir()
    return work


def cache_tree(tmp_path, *, with_chinese=True):
    """A cache tree holding exactly the recordings one word needs."""
    tree = tmp_path / 'archive'
    cache = tree / 'audio-cache'
    _token(cache, 'token-female-apple',
           {'kind': 'jianying_original', 'role': 'female', 'text': WORD,
            'voice': VOICE})
    _token(cache, 'token-male-apple',
           {'kind': 'jianying_original', 'role': 'male', 'text': WORD,
            'voice': 'BV504_streaming'})
    if with_chinese:
        _token(cache, 'token-chinese-apple',
               {'kind': 'jianying_original', 'role': 'chinese', 'text': SPOKEN,
                'voice': VOICE})
    return tree


def project(root, *, missing_chinese=False):
    """A one-word project that the cache tree above can (or cannot) fill.

    The durations are placeholders: this file is about *importing* existing audio,
    not about the lengths the engine will re-measure from what it publishes.
    """
    if missing_chinese:
        records = (Record(id='w151', word=WORD, phonetic='ˈæpl', meaning='n. 苹果',
                          spoken_meaning='别的释义', index=151),)
    else:
        records = (Record(id='w151', word=WORD, phonetic='ˈæpl', meaning='n. 苹果',
                          spoken_meaning=SPOKEN, index=151),)
    media = {('w151', role): MediaInfo('w151:%s' % role, 24000)
             for role in ('female', 'male', 'chinese')}
    from word_video.application import asset_id_for
    assets = {asset_id_for('w151', role): info
              for (record_id, role), info in media.items()}
    folder = root / 'projects' / 'p1'
    folder.mkdir(parents=True)
    built = instantiate(DEFAULT_LESSON_TEMPLATE, records, assets,
                        Project(project_id='p1', intro_s=0.0))
    save_project(built, folder)
    return folder


def files_under(path):
    return sorted(str(item.relative_to(path)) for item in Path(path).rglob('*')
                  if item.is_file())


# ---------------------------------------------------------------------------
# Scanning reads; applying publishes
# ---------------------------------------------------------------------------
def test_scanning_without_a_project_writes_nothing(tmp_path, root):
    tree = cache_tree(tmp_path)
    before = files_under(tree)
    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree))
    assert code == 0, document
    result = document['result']
    assert result['scan']['entries'] == 3
    assert result['scan']['counts']['folders'] == 3
    assert result['ready'] is False and result['applied'] is False
    assert files_under(tree) == before
    assert files_under(root) == []                    # nothing under the work root


def test_an_incomplete_cache_is_needs_input_with_fixes(tmp_path, root):
    tree = cache_tree(tmp_path)
    project(root, missing_chinese=True)
    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree), '--project', 'p1',
                                '--batch', '151-151')
    assert code == 2
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert any('朗读' in fix or '歧义' in fix for fix in document['error']['fixes'])


def test_applying_publishes_the_existing_recordings(tmp_path, root):
    tree = cache_tree(tmp_path)
    folder = project(root)
    before = files_under(folder)
    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree), '--project', 'p1',
                                '--batch', '151-151')
    assert code == 0 and document['result']['ready'] is True
    assert document['result']['applied'] is False      # a plan changes nothing
    assert files_under(folder) == before

    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree), '--project', 'p1',
                                '--batch', '151-151', '--apply')
    assert code == 0, document
    result = document['result']
    assert result['applied'] is True
    assert result['provider']['kind'] == 'local'
    assert len(result['provider']['items']) == 3       # one per role
    assert all(Path(item['path']).is_file() for item in result['provider']['items'])
    assert result['assets'] == 3
    # The originals were published beside the project, and the provider points at
    # those published copies rather than at the archive.
    published = set(files_under(folder)) - set(before)
    assert len(published) == 3
    assert all(str(folder) in item['path'] for item in result['provider']['items'])


def test_an_ambiguous_voice_is_a_question_not_a_guess(tmp_path, root):
    tree = cache_tree(tmp_path)
    _token(tree / 'audio-cache', 'token-female-apple-other',
           {'kind': 'jianying_original', 'role': 'female', 'text': WORD,
            'voice': 'BV999_streaming'})
    project(root)
    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree), '--project', 'p1',
                                '--batch', '151-151')
    assert code == 2
    assert 'BV' in ' '.join(document['error']['fixes']) or \
        '--voices' in ' '.join(document['error']['fixes'])
    # Pinning the voice answers it, and nothing was published before that.
    code, document, _ = run_cli('--root', str(root), 'import', 'audio',
                                '--roots', str(tree), '--project', 'p1',
                                '--batch', '151-151', '--voices',
                                'female=%s' % VOICE, '--apply')
    assert code == 0 and document['result']['applied'] is True


# ---------------------------------------------------------------------------
# Voices: list, audition, confirm
# ---------------------------------------------------------------------------
def draft_tree(tmp_path, *, encrypted=False):
    root_dir = tmp_path / 'drafts'
    audio = _audio(tmp_path / 'member.wav')
    _draft(root_dir, 'batch-151',
           [_material('BV503_streaming', '女声', 'res-1', audio)],
           encrypted=encrypted)
    return root_dir


def test_voice_list_reports_status_and_listen_cuts_a_sample(tmp_path, root):
    drafts = draft_tree(tmp_path)
    code, document, _ = run_cli('--root', str(root), 'voice', 'list',
                                '--drafts', str(drafts))
    assert code == 0, document
    assert 'BV503_streaming' in json.dumps(document['result']['voices'])
    assert document['result']['by_status']

    code, document, _ = run_cli('--root', str(root), 'voice', 'import',
                                '--drafts', str(drafts), '--listen',
                                'BV503_streaming')
    assert code == 0, document
    sample = Path(document['result']['sample'])
    assert sample.is_file() and sample.stat().st_size > 0
    assert '--confirm' in document['result']['note']


def test_confirming_a_voice_needs_the_members_approval(tmp_path, root):
    drafts = draft_tree(tmp_path)
    code, document, _ = run_cli('--root', str(root), 'voice', 'import',
                                '--drafts', str(drafts), '--confirm',
                                'female=BV503_streaming')
    assert code == 2
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert '确认' in json.dumps(document['error']['fixes'], ensure_ascii=False)

    preset = root / 'voices' / 'confirmed.json'
    code, document, _ = run_cli('--root', str(root), 'voice', 'import',
                                '--drafts', str(drafts), '--confirm',
                                'female=BV503_streaming', '--yes', '--preset',
                                str(preset))
    assert code == 0, document
    assert Path(document['result']['preset']).is_file()


def test_an_unknown_speaker_is_refused_with_a_way_forward(tmp_path, root):
    drafts = draft_tree(tmp_path)
    code, document, _ = run_cli('--root', str(root), 'voice', 'import',
                                '--drafts', str(drafts), '--listen', 'nobody')
    assert code == 2
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert document['error']['fixes']


def test_a_missing_drafts_flag_is_needs_input(root):
    code, document, _ = run_cli('--root', str(root), 'voice', 'list')
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'
    code, document, _ = run_cli('--root', str(root), 'import', 'audio')
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'


# ---------------------------------------------------------------------------
# No synthesis, twice over
# ---------------------------------------------------------------------------
def test_the_whole_import_path_makes_zero_synthesis_calls(tmp_path, root):
    class CountingRoute:
        def __init__(self):
            self.calls = []

        def __call__(self, *args, **kwargs):
            self.calls.append(args)
            raise AssertionError('import must not synthesise')

    tree, drafts = cache_tree(tmp_path), draft_tree(tmp_path)
    project(root)
    route = CountingRoute()
    with patch('word_video.tts.synthesize_role', side_effect=route), \
            patch('word_video.original_tts.synthesize_original', side_effect=route), \
            patch('word_video.jianying_tts.synthesize', side_effect=route):
        for argv in (('import', 'audio', '--roots', str(tree), '--project', 'p1',
                      '--batch', '151-151', '--apply'),
                     ('voice', 'list', '--drafts', str(drafts)),
                     ('voice', 'import', '--drafts', str(drafts), '--listen',
                      'BV503_streaming'),
                     ('voice', 'import', '--drafts', str(drafts), '--confirm',
                      'female=BV503_streaming', '--yes')):
            code, document, _ = run_cli('--root', str(root), *argv)
            assert code == 0, (argv, document)
    assert route.calls == []


def test_the_import_runs_in_a_subprocess_with_no_network(tmp_path, root):
    """The second lock: sockets blocked in the child, so a silent fetch cannot pass."""
    tree = cache_tree(tmp_path)
    project(root)
    program = (
        'import socket, sys\n'
        'def blocked(*args, **kwargs):\n'
        '    raise RuntimeError("OFFLINE_IMPORT_BLOCKED")\n'
        'socket.socket.connect = blocked\n'
        'socket.socket.connect_ex = blocked\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from word_video.cli.main import main\n'
        'raise SystemExit(main(sys.argv[2:]))\n')
    result = subprocess.run(
        [sys.executable, '-c', program, str(CHECKOUT), '--root', str(root),
         'import', 'audio', '--roots', str(tree), '--project', 'p1',
         '--batch', '151-151', '--apply'],
        capture_output=True, text=True, encoding='utf-8', cwd=str(CHECKOUT))
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document['result']['applied'] is True
    assert document['result']['provider']['kind'] == 'local'
    assert 'OFFLINE_IMPORT_BLOCKED' not in result.stdout + result.stderr
