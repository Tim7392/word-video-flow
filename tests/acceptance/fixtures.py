"""Shared fixtures for the acceptance self-tests.

The self-test has to run against the *same* negative evidence a human would run
by hand, so it reuses the very scripts in this directory instead of a private
copy of their logic:

  * ``fault_tree`` builds (or reuses) the fault samples with ``make_faults.py``;
  * the matrix cases run ``run_fault_matrix.py``, which calls ``accept_range.py``
    and ``accept_all.py`` as subprocesses, exactly like the shell does;
  * when the read-only archive is not reachable, the tests **fail** with the
    missing path in the message.  They never skip quietly: a skipped negative
    test would turn "not verified" into "green".
"""
import os
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEFAULT_GOOD_BATCH = Path(
    r'D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染'
    r'\wv-b349c24dbf19ce65bc4e5422\0151-0200')
DEFAULT_WORDLIST = Path(
    r'D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt')
DEFAULT_FAULTS = Path(
    r'D:\1\1-AI_workflow\word_video_flow\runtime\tmp\QA\faults')

RANGE = '151-200'


def _env(name, default):
    value = os.environ.get(name)
    return Path(value) if value else default


def require_good_batch():
    batch = _env('WORD_VIDEO_ACCEPTANCE_BATCH', DEFAULT_GOOD_BATCH)
    if not (batch / 'timeline.json').exists():
        pytest.fail('正常对照归档不可用：%s 下没有 timeline.json。'
                    '设置 WORD_VIDEO_ACCEPTANCE_BATCH 指向已交付批次后再跑。' % batch)
    return batch


def require_wordlist():
    wordlist = _env('WORD_VIDEO_ACCEPTANCE_WORDLIST', DEFAULT_WORDLIST)
    if not wordlist.exists():
        pytest.fail('只读词表不可用：%s 不存在。'
                    '设置 WORD_VIDEO_ACCEPTANCE_WORDLIST 后再跑。' % wordlist)
    return wordlist


def fault_tree_path():
    return _env('WORD_VIDEO_ACCEPTANCE_FAULTS', DEFAULT_FAULTS)


@pytest.fixture(scope='session')
def good_batch():
    return require_good_batch()


@pytest.fixture(scope='session')
def wordlist():
    return require_wordlist()


@pytest.fixture(scope='session')
def fault_tree(good_batch, wordlist):
    """Build the fault samples once per session (hard links: cheap, ~1 GB of links)."""
    import make_faults as faults_module

    root = fault_tree_path()
    marker = root / 'good' / good_batch.name / 'timeline.json'
    if not marker.exists():
        builder = faults_module.Faults(good_batch, root, wordlist, (151, 200))
        builder.build()
        problems = builder.verify_original_untouched()
        assert not problems, '构建坏样本时动了原归档：%s' % problems
    return root
