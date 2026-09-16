"""The W10 gate and its protection checks, on synthetic inputs - fast, every save.

The real long run lives behind ``acceptance_media`` (``test_legacy_adapter_gate_archive``);
what is tested here is everything that makes that run trustworthy, and each of these
tests is a *negative control* for one of its claims:

* the cue table answers "show me" - so a one-millisecond edit in a synthetic track must
  appear as exactly one mismatched cue with a delta of 1, not as "nearly equal";
* the protection snapshot must catch three different kinds of "changed": different
  bytes, a touched mtime, and a file that is gone;
* the archive ledger check must reproduce H0's rule (absolute and relative entries, the
  recorded hash as the reference) and must report a wrong hash and a missing file;
* ``git`` must be able to *fail*: a temporary repository with one edited protected file
  is used to prove the check sees it, so a clean answer in a real run means something;
* the gate and the isolation script must not import the new engine, because their claim
  is precisely that the old entry keeps working while that engine is broken.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from legacy_adapter import failure_isolation, gate, new_entry_probe, protection
from legacy_adapter.environment import work_root
from legacy_adapter.errors import INPUT_ERROR, INVALID_REQUEST, LegacyAdapterError


def write_track(path, cues):
    """A legacy-format track: number, ``HH:MM:SS,mmm --> HH:MM:SS,mmm``, text, blank."""
    def stamp(milliseconds):
        hours, rest = divmod(int(milliseconds), 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, millis = divmod(rest, 1000)
        return '%02d:%02d:%02d,%03d' % (hours, minutes, seconds, millis)

    body = ''.join('%d\n%s --> %s\n%s\n\n' % (number, stamp(start), stamp(end), text)
                   for number, (start, end, text) in enumerate(cues, start=1))
    path.write_text(body, encoding='utf-8-sig')
    return path


def run_report(*paths):
    """The shape ``read_run_tracks`` accepts: one entry per track index."""
    return {'tracks': [{'track': index, 'relative_path': Path(path).name,
                        'path': str(path), 'sha256': ''}
                       for index, path in enumerate(paths, start=1)]}


def test_a_cue_table_lists_every_cue_and_sees_a_single_millisecond(tmp_path):
    left = write_track(tmp_path / 'left.srt', [(0, 3600, 'evaluate'), (3600, 7200, 'imitate')])
    same = write_track(tmp_path / 'same.srt', [(0, 3600, 'evaluate'), (3600, 7200, 'imitate')])
    shifted = write_track(tmp_path / 'shifted.srt', [(0, 3600, 'evaluate'), (3600, 7201, 'imitate')])

    table = gate.cue_table(run_report(left), run_report(same))
    assert table['cues'] == 2 and table['mismatched'] == 0
    assert table['max_abs_delta_ms'] == 0 and table['truncated'] is False
    assert table['tracks'][0]['bytes_identical'] is True
    rows = table['tracks'][0]['rows']
    assert [row['word'] for row in rows] == ['evaluate', 'imitate']
    assert all(row['same'] and row['delta_start_ms'] == 0 and row['delta_end_ms'] == 0
               for row in rows)

    table = gate.cue_table(run_report(left), run_report(shifted))
    assert table['mismatched'] == 1 and table['max_abs_delta_ms'] == 1
    assert table['tracks'][0]['rows'][0]['same'] is True
    assert table['tracks'][0]['rows'][1]['delta_end_ms'] == -1
    assert table['tracks'][0]['bytes_identical'] is False


def test_a_cue_table_reports_a_missing_track_instead_of_hiding_it(tmp_path):
    left = write_track(tmp_path / 'left.srt', [(0, 3600, 'evaluate')])
    other = write_track(tmp_path / 'other.srt', [(0, 3600, 'evaluate')])
    table = gate.cue_table(run_report(left, other), run_report(left))
    assert table['missing_right'] == [2] and table['missing_left'] == []
    row = table['tracks'][1]['rows'][0]
    assert row['right_start_ms'] is None and row['same'] is False


def test_the_gate_selects_known_ranges_by_number_and_refuses_the_rest():
    assert [spec['label'] for spec in gate.parse_ranges()] == ['0301-0350', '0501-0550']
    chosen = gate.parse_ranges(['301-350'])
    assert len(chosen) == 1 and chosen[0]['start'] == 301 and chosen[0]['end'] == 350
    # The same range written the other way round is the same range, not a second one.
    assert gate.parse_ranges(['0301-0350']) == chosen
    assert gate.parse_ranges(['301-350', '301-350']) == chosen
    with pytest.raises(LegacyAdapterError) as raised:
        gate.parse_ranges(['401-450'])
    assert raised.value.code == INVALID_REQUEST
    assert '301-350' in raised.value.details['known']
    # Every known range carries a real batch and the batch size the archive was cut at.
    for spec in gate.parse_ranges():
        assert spec['batch_size'] == 50 and spec['archive'] and spec['end'] - spec['start'] == 49


def test_the_evidence_directory_is_the_stable_path_under_the_work_root():
    assert gate.evidence_dir() == work_root() / 'out' / 'reports'
    assert gate.evidence_dir(r'D:\somewhere\else') == Path(r'D:\somewhere\else')
    assert Path(gate.DEFAULT_WORDLIST).drive.upper() == 'D:'


def test_a_refused_gate_run_does_not_overwrite_the_last_evidence(tmp_path):
    """Unknown range and missing word list are failures with their own code - and the
    previous run's report stays where it is until a run actually produces one."""
    (tmp_path / 'gate-legacy-equivalence.json').write_text('{"ok": true}', encoding='utf-8')
    assert gate.main(['--evidence', str(tmp_path), '--ranges', '401-450']) == 2
    assert json.loads((tmp_path / 'gate-legacy-error.json').read_text(encoding='utf-8'))[
        'error']['code'] == INVALID_REQUEST
    assert gate.main(['--evidence', str(tmp_path),
                      '--wordlist', str(tmp_path / 'nope.txt')]) == 3
    assert json.loads((tmp_path / 'gate-legacy-equivalence.json').read_text(
        encoding='utf-8')) == {'ok': True}

    assert failure_isolation.main(['--report', str(tmp_path / 'iso.json'),
                                   '--wordlist', str(tmp_path / 'nope.txt')]) == 3
    payload = json.loads((tmp_path / 'iso.json').read_text(encoding='utf-8'))
    assert payload['ok'] is False and payload['error']['code'] == INPUT_ERROR


def test_a_protection_snapshot_sees_edits_touches_and_disappearing_files(tmp_path):
    kept, edited, touched = (tmp_path / name for name in ('kept.py', 'edited.py', 'touched.py'))
    for path in (kept, edited, touched):
        path.write_text('original\n', encoding='utf-8')
    missing = tmp_path / 'never-existed.py'
    before = protection.snapshot([kept, edited, touched, missing])

    assert protection.compare(before, protection.snapshot([kept, edited, touched, missing]))['ok'] \
        is True
    # A file that was never there is not a change - but the gate still fails on it.
    verdict = protection.check(before, protection.snapshot([kept, edited, touched, missing]),
                               {}, {}, {'ok': True, 'clean': True, 'changed': {}, 'head': 'x'},
                               {'available': False}, [])
    assert verdict['ok'] is False and str(missing) in verdict['problems'][0]

    edited.write_text('changed\n', encoding='utf-8')
    stat = touched.stat()
    os.utime(touched, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    result = protection.compare(before, protection.snapshot([kept, edited, touched, missing]))
    assert result['ok'] is False
    assert result['changed'] == [str(edited), str(touched)]
    rows = {row['path']: row for row in result['files']}
    assert rows[str(edited)]['sha256_changed'] is True
    assert rows[str(touched)]['sha256_changed'] is False
    assert rows[str(touched)]['mtime_changed'] is True
    assert rows[str(kept)]['unchanged'] is True and rows[str(missing)]['unchanged'] is True


def test_the_archive_ledger_check_reproduces_h0s_rule(tmp_path):
    (tmp_path / 'srt').mkdir()
    track = write_track(tmp_path / 'srt' / '0301-0350_01_英文重复.srt', [(0, 100, 'word')])
    absolute = tmp_path / 'timeline.json'
    absolute.write_text('{"fps": 60}\n', encoding='utf-8')
    ledger = {'files': [
        {'path': str(absolute), 'sha256': protection.sha256(absolute)},
        {'path': 'srt/0301-0350_01_英文重复.srt', 'sha256': protection.sha256(track)},
        {'path': 'missing.bin', 'sha256': 'whatever'},
        {'path': 'srt/0301-0350_01_英文重复.srt', 'sha256': 'not-the-hash'},
    ]}
    (tmp_path / 'complete.json').write_text(json.dumps(ledger), encoding='utf-8')
    result = protection.verify_complete(tmp_path)
    assert result['entries'] == 4 and result['ok'] == 2
    assert result['missing'] == 1 and result['changed'] == 1
    assert any('differs' in problem for problem in result['problems'])
    assert any('missing' in problem for problem in result['problems'])
    # A batch without a ledger is an unavailable check, not a silent pass.
    empty = tmp_path / 'no-ledger'
    empty.mkdir()
    assert protection.verify_complete(empty)['available'] is False


def test_git_checks_report_a_real_edit_in_a_temporary_repository(tmp_path):
    """The check has to be able to fail, or a clean answer means nothing."""

    def git(*arguments):
        return subprocess.run(['git', '-c', 'user.email=t@local', '-c', 'user.name=t',
                               '-C', str(tmp_path), *arguments],
                              capture_output=True, text=True, encoding='utf-8', check=True)

    if subprocess.run(['git', '--version'], capture_output=True).returncode != 0:
        pytest.fail('git 不可用：保护核对需要它')
    git('init', '-q')
    (tmp_path / 'subtitle_factory_core.py').write_text('original\n', encoding='utf-8')
    (tmp_path / 'Words_SRT.py').write_text('original\n', encoding='utf-8')
    git('add', 'subtitle_factory_core.py', 'Words_SRT.py')
    git('commit', '-q', '-m', 'base')

    clean = protection.git_checks(tmp_path, files=('subtitle_factory_core.py', 'Words_SRT.py'))
    assert clean['ok'] is True and clean['diff_name_only'] == []
    assert clean['head'] and clean['merge_base']['available'] is False

    (tmp_path / 'subtitle_factory_core.py').write_text('edited\n', encoding='utf-8')
    dirty = protection.git_checks(tmp_path, files=('subtitle_factory_core.py', 'Words_SRT.py'))
    assert dirty['ok'] is False
    assert dirty['changed']['worktree'] == ['subtitle_factory_core.py']
    assert dirty['changed']['status'] == [' M subtitle_factory_core.py']


def test_the_protected_list_is_the_six_legacy_files_the_task_names():
    assert protection.PROTECTED_SOURCES == (
        'subtitle_factory_api.py', 'subtitle_factory_core.py', 'subtitle_factory_cli.py',
        'subtitle_factory_ui.py', 'Words_SRT.py', 'word.py')
    assert protection.ARCHIVE_WATCH == ('complete.json', 'timeline.json')


@pytest.mark.parametrize('module', ['legacy_adapter.gate', 'legacy_adapter.failure_isolation',
                                    'legacy_adapter.new_entry_probe'])
def test_the_gate_and_the_isolation_script_do_not_import_the_new_engine(module):
    """Their whole claim is that the old entry works while the new engine is broken."""
    code = ('import json, sys; import %s; '
            'print(json.dumps(sorted(name for name in sys.modules '
            'if name == "word_video" or name.startswith("word_video."))))' % module)
    completed = subprocess.run([sys.executable, '-c', code], cwd=str(Path.cwd()),
                               capture_output=True, text=True, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == []


def test_the_isolation_script_keeps_its_subprocess_boundary():
    """Every step is a child process; the blocker is a real one, not a mock."""
    source = Path(failure_isolation.__file__).read_text(encoding='utf-8')
    assert 'subprocess.run' in source and 'sitecustomize' in source
    assert failure_isolation.EXPECTED_CUES == [6, 3, 3, 3, 3]
    assert failure_isolation.BROKEN_ASSET == 'w151:female'
    assert Path(failure_isolation.DEFAULT_REPORT).name == 'gate-legacy-isolation.json'
    assert failure_isolation.repo_root().joinpath('word_video_cli.py').is_file()


def test_a_failed_check_folds_into_one_verdict_with_the_numbers_that_failed():
    steps = [
        {'name': 'step_one', 'checks': [{'check': 'a', 'expected': 1, 'actual': 1, 'ok': True},
                                        {'check': 'b', 'expected': True, 'actual': False,
                                         'ok': False}]},
        {'name': 'step_two', 'checks': [{'check': 'c', 'expected': 0, 'actual': 0, 'ok': True}]},
    ]
    folded = failure_isolation.fold_checks(steps)
    assert folded['checks'] == 3 and folded['failed_checks'] == 1
    assert folded['failed'] == [{'step': 'step_one', 'check': 'b', 'expected': True,
                                 'actual': False}]
    assert 'step_one：b' in folded['problems'][0]
    assert failure_isolation.fold_checks([]) == {'failed': [], 'checks': 0,
                                                 'failed_checks': 0, 'problems': []}


def test_the_probe_classifies_a_refusal_and_keeps_the_engines_own_code():
    from word_video.domain.errors import AssetFileError

    error = AssetFileError("asset 'w151:female' points at missing.ogg",
                           path='asset:w151:female', hint='把文件放回去')
    described = new_entry_probe.structured(error)
    assert described['type'] == 'AssetFileError'
    assert described['code'] == 'ASSET_FILE_MISSING'
    assert described['path'] == 'asset:w151:female'
    assert described['hint'] == '把文件放回去'
    assert new_entry_probe.refusal(error) is True
    assert new_entry_probe.refusal(ValueError('x')) is True
    assert new_entry_probe.refusal(OSError('x')) is True
    assert new_entry_probe.refusal(KeyError('x')) is False


def test_the_probe_reads_the_fixtures_own_items_and_refuses_an_empty_one(tmp_path):
    fixture = tmp_path / 'fixture.json'
    fixture.write_text(json.dumps({'provider': {'items': [
        {'index': 151, 'role': 'female', 'text': 'demonstrate', 'voice': 'BV503', 'path': 'a.ogg'},
        {'index': 151, 'role': 'chinese', 'text': '证明', 'voice': 'BV406', 'path': 'b.ogg'}]}}),
        encoding='utf-8')
    grouped = new_entry_probe._items(fixture)
    assert sorted(grouped) == [151] and sorted(grouped[151]) == ['chinese', 'female']
    empty = tmp_path / 'empty.json'
    empty.write_text('{"provider": {"items": []}}', encoding='utf-8')
    with pytest.raises(ValueError):
        new_entry_probe._items(empty)
