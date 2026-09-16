"""W10's two remaining acceptance items, on the real archive - one command each.

This file is behind ``acceptance_media`` because it reads the read-only word list and
two delivered archive batches, so it belongs to the重 acceptance run
(``python -m pytest -m acceptance_media tests``), not to the suite a developer runs on
every save.  It never skips: if the word list, a batch or its media are missing, the
test fails with the path, because "not verified" must not look like "green".

What it establishes, and why both parts are needed
-------------------------------------------------
**The equivalence long run is a gate, not an anecdote.**  ``legacy_adapter.gate`` runs
two ranges - 301-350 and 501-550 - and this test judges its evidence: five tracks per
range byte-identical between the old entry called directly and the new entry through
the adapter, every one of the 600 cues equal **to the millisecond** (the table is in
the report cue by cue, so "0 differences" is readable rather than asserted), both
timing calibers printed with their real numbers, the archive's own delivered track
differing in time over the same words (the comparator demonstrably can see a
difference), and - the part that makes it a gate rather than a demo - the six protected
old-core files and every archive file the run read unchanged, with git and H0's own
tool agreeing.

**Failure isolation is reproduced, not assumed.**  ``legacy_adapter.failure_isolation``
makes the old entry fail for two different reasons and then exports with the new one,
makes the new entry fail on a missing media file and then produces five tracks with the
old one, and finally blocks the whole new engine from importing and shows the old entry
still working.  Both directions run as separate processes, which is the property being
claimed.

The pinned numbers below come from outside this code: the media totals are the last cue
of the archive's own delivered tracks (read directly from the archive), and the old
rule's totals are the old core's own plan for the same 50 words.  They are here so a
silent change in either - or in the word list - fails loudly instead of being compared
only against itself.
"""
import json
from pathlib import Path

import pytest

from legacy_adapter import failure_isolation, gate
from legacy_adapter.environment import work_root
from legacy_adapter.report import read_run_tracks

pytestmark = pytest.mark.acceptance_media

EVIDENCE = gate.evidence_dir()
GATE_REPORT = EVIDENCE / 'gate-legacy-equivalence.json'
ISOLATION_REPORT = EVIDENCE / 'gate-legacy-isolation.json'

#: ``{label: the numbers the artefacts themselves give}``.  ``media_ms`` equals the
#: archive track 02's last cue; ``min_word_delta_ms`` has opposite signs in the two
#: ranges on purpose - which is why one range is not enough to carry the verdict.
EXPECTED = {
    '0301-0350': {'legacy_ms': 199400, 'media_ms': 173733, 'frames': 10424,
                  'delta_ms': 25667, 'min_word_delta_ms': 300, 'max_word_delta_ms': 25667,
                  'first_word': 'evaluate', 'last_word': 'disintegrate',
                  'control_first': {'left': (0, 3600), 'right': (0, 3300)}},
    '0501-0550': {'legacy_ms': 215600, 'media_ms': 192467, 'frames': 11548,
                  'delta_ms': 23133, 'min_word_delta_ms': -1250, 'max_word_delta_ms': 23133,
                  'first_word': 'formulate', 'last_word': 'consultant',
                  'control_first': {'left': (0, 4800), 'right': (1867, 6050)}},
}
TRACK_CUES = [100, 50, 50, 50, 50]


def test_the_archive_equivalence_gate_runs_both_ranges_and_writes_stable_evidence():
    assert gate.main(['--evidence', str(EVIDENCE)]) == 0
    assert GATE_REPORT.is_file(), GATE_REPORT
    report = json.loads(GATE_REPORT.read_text(encoding='utf-8'))

    assert report['ok'] is True and report['verdict'] == 'PASS' and report['problems'] == []
    assert report['totals'] == {'ranges': 2, 'tracks_identical': 10, 'tracks': 10,
                                'cues_compared': 600, 'mismatched_cues': 0,
                                'max_abs_delta_ms': 0}
    assert [row['label'] for row in report['ranges']] == ['0301-0350', '0501-0550']

    for row in report['ranges']:
        expected = EXPECTED[row['label']]
        # -- the two entries agree, byte for byte and cue for cue -----------
        assert row['ok'] is True and row['problems'] == []
        assert row['exit_code'] == 0 and row['verdict'] == 'identical'
        assert row['difference_count'] == 0 and row['tracks_missing'] == []
        assert [track['verdict'] for track in row['tracks']] == ['identical'] * 5
        assert [track['left_cues'] for track in row['tracks']] == TRACK_CUES
        assert [track['right_cues'] for track in row['tracks']] == TRACK_CUES
        assert [track['left_bytes'] for track in row['tracks']] == \
            [track['right_bytes'] for track in row['tracks']]
        assert all(track['left_sha256'] == track['right_sha256'] for track in row['tracks'])
        # Every track the adapter published was re-hashed from disk and matches the hash
        # the old core reported for it inside its own answer.
        journal = read_run_tracks(Path(row['root']) / 'new-entry-report.json')
        assert len(journal) == 5
        assert all(facts['sha256'] == facts['recorded_sha256'] for facts in journal.values())

        # -- the table itself: every cue, both sides, deltas of 0 -----------
        table = row['cue_table']
        assert table['cues'] == sum(TRACK_CUES) == 300 and table['truncated'] is False
        assert table['mismatched'] == 0 and table['max_abs_delta_ms'] == 0
        for track in table['tracks']:
            assert len(track['rows']) == track['cues_left']
            assert track['mismatched_cues'] == 0 and track['bytes_identical'] is True
            assert all(item['same'] is True and item['delta_start_ms'] == 0
                       and item['delta_end_ms'] == 0 for item in track['rows'])
        words = [item['word'] for item in table['tracks'][1]['rows']]
        assert words[0] == expected['first_word'] and words[-1] == expected['last_word']

        # -- both calibers, with the numbers the artefacts themselves give --
        calibers = row['calibers']
        assert calibers['legacy_total_ms'] == expected['legacy_ms']
        assert calibers['media_total_ms'] == expected['media_ms'] == \
            calibers['delivered_track_end_ms']
        assert calibers['delta_ms'] == expected['delta_ms']
        assert calibers['media_frames'] == expected['frames'] and calibers['media_fps'] == 60
        assert calibers['media_words'] == 50
        assert calibers['media_first_word'] == expected['first_word']
        assert calibers['media_last_word'] == expected['last_word']
        assert calibers['legacy_packages'] == [{'number': 1, 'word_count': 50,
                                                'first_word': expected['first_word'],
                                                'last_word': expected['last_word'],
                                                'duration_ms': expected['legacy_ms']}]
        per_word = row['per_word_caliber_table']
        assert per_word['words'] == 50 and per_word['same_words'] == 50
        assert per_word['min_delta_ms'] == expected['min_word_delta_ms']
        assert per_word['max_delta_ms'] == expected['max_word_delta_ms']

        # -- the negative control: same comparator, a pair that must differ --
        control = row['control']
        assert control['available'] is True and control['texts_identical'] is True
        assert control['times_identical'] is False and control['differing_cues'] == 50
        first = control['first_difference']
        assert (first['left']['start_ms'], first['left']['end_ms']) == \
            expected['control_first']['left']
        assert (first['right']['start_ms'], first['right']['end_ms']) == \
            expected['control_first']['right']

    # The two ranges are genuinely two selections, not one run written out twice.
    first, second = report['ranges']
    assert first['archive'] != second['archive']
    assert first['wordlist_sha256'] == second['wordlist_sha256'] == report['wordlist_sha256']
    assert (first['cue_table']['tracks'][1]['rows'][0]['word']
            != second['cue_table']['tracks'][1]['rows'][0]['word'])

    # -- and nothing protected moved while all of that ran ----------------
    guard = report['protection']
    assert guard['ok'] is True and guard['problems'] == []
    assert guard['files']['checked'] == 6 and guard['files']['unchanged'] == 6
    assert all(row['unchanged'] and row['sha256_changed'] is False
               and row['mtime_changed'] is False for row in guard['files']['files'])
    assert guard['git']['ok'] is True and guard['git']['changed'] == {
        'worktree': [], 'staged': [], 'status': [], 'since_merge_base': []}
    assert guard['git']['head']
    assert guard['archive']['ok'] is True and guard['archive']['checked'] == 14
    assert [(Path(row['batch']).name, row['entries'], row['ok'], row['changed'], row['missing'])
            for row in guard['batches']] == [('0301-0350', 168, 168, 0, 0),
                                             ('0501-0550', 172, 172, 0, 0)]
    assert guard['origin_checked'] is True, guard['h0_tool'].get('reason')
    assert guard['h0_tool']['verdict'] == 'PASS'
    assert guard['h0_tool']['legacy_core']['identical'] == 6

    # -- the evidence is where the project says evidence goes -------------
    for path in [GATE_REPORT, EVIDENCE / 'gate-legacy-summary.txt',
                 EVIDENCE / 'gate-legacy-0301-0350.json',
                 EVIDENCE / 'gate-legacy-0501-0550.json',
                 EVIDENCE / 'gate-legacy-protection.json',
                 *(row['evidence'] for row in report['ranges'])]:
        assert Path(path).is_file(), path
        assert Path(path).parent == EVIDENCE
        assert Path(path).stat().st_size > 0
    assert Path(report['area']).is_relative_to(work_root() / 'out' / 'legacy')
    for row in report['ranges']:
        per_range = json.loads(Path(row['evidence']).read_text(encoding='utf-8'))
        assert per_range['schema'] == 'wv-legacy-equivalence@1' and per_range['ok'] is True


def test_failure_isolation_is_reproduced_with_stable_evidence():
    assert failure_isolation.main(['--report', str(ISOLATION_REPORT)]) == 0
    assert ISOLATION_REPORT.is_file(), ISOLATION_REPORT
    report = json.loads(ISOLATION_REPORT.read_text(encoding='utf-8'))

    assert report['ok'] is True and report['verdict'] == 'PASS' and report['problems'] == []
    assert report['totals'] == {'steps': 8, 'checks': 40, 'failed_checks': 0}
    steps = {row['name']: row for row in report['steps']}
    assert set(steps) == set(report['directions']['old_fails_new_ok']['steps']
                             + report['directions']['new_fails_old_ok']['steps'])
    assert all(item['ok'] for row in report['steps'] for item in row['checks'])

    # Direction 1: the old entry fails twice over, the new one still exports.
    missing = steps['old_entry_missing_wordlist']
    assert missing['command']['exit_code'] == 3
    assert missing['evidence']['error']['code'] == 'INPUT_ERROR'
    assert missing['evidence']['error']['details']['field'] == 'wordlist'
    assert missing['evidence']['published_tracks'] == 0
    direct = steps['old_cli_returns_nonzero']
    assert direct['command']['exit_code'] == 3
    assert direct['evidence']['error']['code'] == 'INPUT_ERROR'
    through = steps['old_entry_broken_wordlist_through_adapter']
    assert through['command']['exit_code'] == 3
    assert through['evidence']['error']['code'] == 'INPUT_ERROR'
    assert through['evidence']['error']['details']['lines']
    fresh = steps['new_entry_works_after_the_old_failures']
    assert fresh['command']['exit_code'] == 0 and fresh['evidence']['ok'] is True
    assert [track['cues'] for track in fresh['evidence']['tracks']] == \
        failure_isolation.EXPECTED_CUES
    assert all(track['bytes'] for track in fresh['evidence']['tracks'])

    # Direction 2: the new entry fails on a missing recording, the old one still works.
    broken = steps['new_entry_missing_media']
    assert broken['command']['exit_code'] == 2 and broken['evidence']['ok'] is False
    assert broken['evidence']['error']['code'] == 'ASSET_FILE_MISSING'
    assert broken['evidence']['error']['path'] == 'asset:%s' % failure_isolation.BROKEN_ASSET
    assert broken['evidence']['broken_asset']['exists'] is False
    assert broken['evidence']['published_tracks'] == 0, '失败必须发生在产出之前'
    raw = steps['new_entry_cli_raw']
    assert raw['command']['exit_code'] != 0 and raw['evidence']['published_tracks'] == 0
    assert raw['evidence']['structured_json'] is False, 'B 若已修好这个缺口，这条会变成 True'
    assert 'ProjectError' in raw['note']
    old = steps['old_entry_works_after_the_new_failure']
    assert old['command']['exit_code'] == 0 and old['evidence']['ok'] is True
    assert [row['cues'] for row in old['evidence']['tracks']] == failure_isolation.EXPECTED_CUES
    assert old['evidence']['counts']['file_count'] == 5
    blocked = steps['old_entry_survives_a_blocked_engine']
    assert blocked['control']['exit_code'] != 0, '屏蔽本身必须真的让 import word_video 失败'
    assert blocked['command']['exit_code'] == 0 and blocked['evidence']['ok'] is True
    assert blocked['evidence']['media_caliber']['available'] is False
    assert 'word_video' in blocked['evidence']['media_caliber']['reason']
    assert [row['cues'] for row in blocked['evidence']['tracks']] == \
        failure_isolation.EXPECTED_CUES

    assert Path(report['area']).is_relative_to(work_root() / 'out' / 'legacy')
    # Every child runs either in the work tree (to import ``legacy_adapter``) or inside
    # this run's own D-drive directory - never in the old tree and never on C.
    for row in report['steps']:
        cwd = Path(row['command']['cwd'])
        assert cwd == failure_isolation.repo_root() or \
            cwd.is_relative_to(work_root() / 'out' / 'legacy'), row['name']
