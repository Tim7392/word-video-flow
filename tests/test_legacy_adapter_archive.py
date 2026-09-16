"""The hard indicator on real archive data - and the two clocks, on the same 50 words.

Why this file is behind ``acceptance_media``
--------------------------------------------
It reads the read-only vocabulary list and a delivered archive batch, so it belongs to
the重 acceptance run (``python -m pytest -m acceptance_media tests``), not to the suite
a developer runs on every save.  It never skips: if the word list or the batch is not
there, the test fails with the missing path, because "not verified" must not look like
"green".

What it establishes
-------------------
* same word list, same selection, same packaging, same timing parameters: the entry in
  the new software and the old CLI called directly produce **byte-identical** five
  tracks, with zero millisecond differences - the task's acceptance item 1;
* the archive batch's own tracks (cut to the audio) are *not* those tracks: the words
  are identical and the times are not, which is the timing-caliber difference the
  interface has to make visible - measured here rather than asserted in prose;
* nothing in the archive changed: the batch's timeline and five SRT tracks are hashed
  before and after the run.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from legacy_adapter import compare_runs
from legacy_adapter.measure_equivalence import main as equivalence_main
from test_preview_support import scratch

pytestmark = pytest.mark.acceptance_media

WORDLIST = Path(os.environ.get('WORD_VIDEO_ACCEPTANCE_WORDLIST')
                or r'D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt')
ARCHIVE_BATCH = Path(os.environ.get('WORD_VIDEO_W10_ARCHIVE_BATCH')
                     or r'D:\单词速记自动化_测试归档_0915\50词本地配音'
                        r'\wv-423592ff3e80c4748f0fd573\0301-0350')
FIRST, LAST, BATCH = 301, 350, 50


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archive_watch():
    """The archive files this test may not disturb, and their hashes right now."""
    watched = [ARCHIVE_BATCH / 'timeline.json'] + sorted((ARCHIVE_BATCH / 'srt').glob('*.srt'))
    return {str(path): sha256(path) for path in watched}


def test_the_real_archive_batch_is_available():
    assert WORDLIST.is_file(), WORDLIST
    assert (ARCHIVE_BATCH / 'timeline.json').is_file(), ARCHIVE_BATCH
    assert len(list((ARCHIVE_BATCH / 'srt').glob('*.srt'))) == 5


def test_old_entry_and_new_entry_are_identical_on_a_real_archive_selection():
    before = archive_watch()
    with scratch('legacy-archive-') as area:
        root = area / 'equivalence'
        code = equivalence_main([
            '--wordlist', str(WORDLIST), '--start', str(FIRST), '--end', str(LAST),
            '--batch-size', str(BATCH), '--timeline', str(ARCHIVE_BATCH),
            '--root', str(root), '--report', str(area / 'report.json')])
        assert code == 0, '新入口没有逐字节复现旧入口'
        report = json.loads((area / 'report.json').read_text(encoding='utf-8'))

        comparison = report['comparison']
        assert comparison['verdict'] == 'identical'
        assert comparison['tracks_missing'] == []
        assert [row['verdict'] for row in comparison['tracks']] == ['identical'] * 5
        assert comparison['difference_count'] == 0
        assert [row['left_cues'] for row in comparison['tracks']] == [100, 50, 50, 50, 50]
        assert all(row['left_sha256'] == row['right_sha256'] for row in comparison['tracks'])

        # The old entry was called directly, with the same request document.
        assert report['old_entry']['returncode'] == 0
        assert report['new_entry']['counts']['selected_count'] == 50
        assert report['new_entry']['counts']['package_count'] == 1
        assert report['new_entry']['counts']['file_count'] == 5

        # The two clocks, on the same 50 words: the old rule is longer than the audio.
        # ``measure_equivalence`` publishes the old caliber as the measured block itself
        # (the old core's own plan) and the new one as the engine's caliber document.
        legacy = report['calibers']['legacy']
        media = report['calibers']['media']['measured']
        assert legacy['total_duration_ms'] == 199400
        assert media['total_duration_ms'] == 173733
        assert media['total_frames'] == 10424 and media['fps'] == 60
        summary = report['archive_reference']['summary']
        assert summary['words'] == 50 and summary['same_words'] == 50
        assert summary['delta_ms'] == 25667
        assert summary['min_delta_ms'] > 0, '旧口径不该在任何一词上短于音频'

        # And the archive's own tracks are a different clock over the same words.
        journal = json.loads((root / 'new-work' / 'legacy-run.json').read_text(encoding='utf-8'))
        legacy_track = next(row['path'] for row in journal['tracks'] if row['track'] == 2)
        archive_track = ARCHIVE_BATCH / 'srt' / '0301-0350_02_英文单次.srt'
        pair = compare_runs(
            {'tracks': [{'track': 2, 'relative_path': 'legacy_02.srt',
                         'path': legacy_track, 'sha256': ''}]},
            {'tracks': [{'track': 2, 'relative_path': 'archive_02.srt',
                         'path': str(archive_track), 'sha256': ''}]})
        row = pair['tracks'][0]
        assert row['bytes_identical'] is False and pair['verdict'] == 'differs'
        assert row['texts_identical'] is True, '同一批词：内容相同'
        assert row['times_identical'] is False, '不同口径：时间不同'
    assert archive_watch() == before, '本次验收动了只读归档'
