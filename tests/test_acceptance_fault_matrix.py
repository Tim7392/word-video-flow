"""Every known fault must be refused: the M0 holes as a permanent regression.

This is the formalised version of the M0 negative evidence.  The cases come from
``acceptance/make_faults.py`` (fault samples derived from one delivered batch with
hard links) and are judged by ``acceptance/run_fault_matrix.py``, which calls the
same ``accept_range.py`` / ``accept_all.py`` a shell would call.  Nothing here
imports the production pipeline, and no expectation is produced by it.

Cost: the matrix runs the full validator - pixels included - over the good batch
and its derived faults, because a fault caught only by a cheaper run is not the
same evidence as the delivered check.  It therefore needs the read-only archive
and is marked ``acceptance_media``; when that archive is missing these tests
**fail** instead of skipping, so "not verified" can never look green.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
ACCEPTANCE = HERE / 'acceptance'
sys.path.insert(0, str(HERE))

from acceptance.fixtures import (good_batch, wordlist, fault_tree,  # noqa: E402,F401
                                 fault_tree_path, require_good_batch)

pytestmark = pytest.mark.acceptance_media


@pytest.fixture(scope='session')
def matrix(good_batch, wordlist, fault_tree, tmp_path_factory):
    """Run the whole fault matrix once and hand every test the same evidence."""
    out = tmp_path_factory.mktemp('acceptance-matrix')
    summary = out / 'matrix.json'
    command = [sys.executable, str(ACCEPTANCE / 'run_fault_matrix.py'),
               '--good-batch', str(good_batch), '--faults', str(fault_tree),
               '--wordlist', str(wordlist), '--out', str(out),
               '--range', '151-200', '--pixels', 'all', '--json', str(summary)]
    result = subprocess.run(command, capture_output=True, cwd=str(ACCEPTANCE))
    text = result.stdout.decode('utf-8', 'replace')
    assert summary.exists(), (
        '矩阵跑批没有产出汇总报告\n%s\n%s'
        % (text[-3000:], result.stderr.decode('utf-8', 'replace')[-3000:]))
    data = json.loads(summary.read_text(encoding='utf-8'))
    data['_stdout'] = text
    data['_runner_exit'] = result.returncode
    return data


def case_of(matrix, name):
    for row in matrix['rows']:
        if row['case'] == name:
            return row
    raise AssertionError('矩阵里没有用例 %s' % name)


def _fail(row):
    return ('用例 %s：期望 %s，实际 %s（面：%s，退出码 %s，新鲜报告 %s）\n%s'
            % (row['case'], row['expected'], row['verdict'], row['faces'],
               row['exit_code'], row['fresh'],
               json.dumps(row.get('failures'), ensure_ascii=False, indent=1)))


def test_control_good_batch_still_passes(matrix):
    """The control: a validator that fails everything proves nothing."""
    row = case_of(matrix, 'good')
    assert row['ok'], _fail(row)
    assert row['verdict'] == 'PASS'
    assert row['faces'] == []


@pytest.mark.parametrize('name', [
    'bad-missing-phonetic-track',
    'bad-phonetic-start-shifted',
    'bad-word2-text-changed',
    'bad-timeline-only-changed',
    'bad-mix-truncated',
    'bad-mp4-tail-silenced',
    'bad-timeline-unreadable',
    'bad-draft-unreadable',
])
def test_every_bad_batch_is_failed(matrix, name):
    """One deliberate defect per batch; the named face must catch it."""
    row = case_of(matrix, name)
    assert row['ok'], _fail(row)
    assert row['verdict'] == 'FAIL'
    assert row['expected_faces'], '坏样本必须声明期望抓住它的面'
    assert set(row['expected_faces']) <= set(row['faces']), _fail(row)


@pytest.mark.parametrize('name', [
    'root-missing-batch',
    'root-extra-batch',
    'root-empty',
    'root-duplicate-batch',
    'root-stale-pass-no-batch',
])
def test_every_bad_archive_is_failed(matrix, name):
    """Archive level: a missing batch, an extra batch, an empty root, a duplicate
    range and a stale PASS report are all failures."""
    row = case_of(matrix, name)
    assert row['ok'], _fail(row)
    assert row['verdict'] == 'FAIL'
    assert set(row['expected_faces']) <= set(row['faces']), _fail(row)


def test_a_crashed_validator_is_never_covered_by_a_stale_report(matrix):
    """The M0 accident, reproduced deliberately.

    ``root-stale-report`` holds a readable batch next to an ``acc-*.json`` that
    already says PASS; the child validator is pointed at a word list that does
    not exist, so it dies before writing anything.  The only acceptable outcome
    is "the validator produced no fresh report" - not the PASS that is lying at
    the report path, and not a silent PASS for the whole archive.
    """
    row = case_of(matrix, 'root-stale-report')
    assert row['ok'], _fail(row)
    assert row['verdict'] == 'FAIL', _fail(row)
    assert 'validator' in row['faces'], _fail(row)


def test_matrix_runner_itself_agrees(matrix):
    """A runner that reports success while a row failed would hide the evidence."""
    assert matrix['_runner_exit'] == 0, matrix['_stdout'][-3000:]
    assert matrix['all_ok'], matrix['_stdout'][-3000:]
    assert len(matrix['rows']) == 15


def test_no_run_can_read_a_stale_summary(matrix):
    """The exact M0 accident: a previous run's PASS sitting at the output path.

    ``run_fault_matrix.py`` writes its summary through the same ``--json`` path a
    stale file would occupy, and every archive case pre-positions a PASS summary
    right there before calling ``accept_all.py``.  If any of those files had been
    handed back, its ``stale`` marker or its verdict would still show PASS - so
    neither may be true of the evidence this session is using.
    """
    assert matrix['rows'], '没有跑过任何用例就不算证据'
    assert 'stale' not in matrix, '本次汇总报告带着旧运行的 stale 标记'
    for row in matrix['rows']:
        assert 'stale' not in row, '%s 的判定来自旧报告' % row['case']
        assert row['fresh'], '%s 读到的不是本次运行写出的报告' % row['case']


def test_source_archive_is_still_byte_identical(good_batch):
    """Hard links were the M0 accident; prove the original is untouched.

    Recomputes the sha256 of every file the archive's own complete.json lists
    (~780 MB, a few seconds) so a write through a hard link cannot hide.
    """
    import hashlib

    complete = json.loads((good_batch / 'complete.json').read_text(encoding='utf-8'))
    changed, missing = [], []
    for item in complete['files']:
        path = Path(item['path'])
        if not path.exists():
            missing.append(str(path))
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
        if digest.hexdigest() != item['sha256']:
            changed.append(str(path))
    assert not missing, '原归档文件缺失：%s' % missing[:5]
    assert not changed, '原归档被改写了：%s' % changed[:5]
