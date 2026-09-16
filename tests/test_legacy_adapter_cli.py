"""The old factory as a new entry: the subprocess, the mapping, the refusal to guess.

What these tests are for
------------------------
The task's hard indicator is that the new entry produces **exactly** what the old one
does, so the first test here runs both - the old CLI directly and through the adapter -
from the *same request document* and compares the five published tracks byte for byte
and cue by cue.  Everything else in this file is about the properties that make that
comparison trustworthy rather than lucky:

* the adapter never imports the old modules (checked on the modules of a fresh
  subprocess, not by reading the source);
* the working directory, the temp directory and the output stay out of the protected
  trees, and a request that points into one is refused before anything runs;
* a failure of the old tool arrives as a structured code with the old tool's own
  details attached, and a failure of the *new* video software cannot reach this path
  at all (proved by running the adapter with ``word_video`` blocked).

The word list is synthetic and tiny: these are tests about a boundary, and the real
archive comparison lives in the ``acceptance_media`` suite where the real data is.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from legacy_adapter import (CALIBER_LEGACY, CALIBER_MEDIA, EXIT_CODES, LegacyAdapterError,
                            LegacyRequest, UI_NOTICE, calibers, compare_runs,
                            default_output_dir, default_python, locate_legacy_entry,
                            run_legacy, timeline_caliber)
from legacy_adapter import cli as adapter_cli
from legacy_adapter import environment as adapter_environment
from legacy_adapter.measure_equivalence import run_old_entry
from legacy_adapter.runner import legacy_environment

#: Six entries, in the old tool's own line format.  The last one carries more than six
#: Hanzi on purpose: that is the only way the ``extra`` timing parameter is used, and a
#: fixture that never crosses the boundary would not exercise the old rule at all.
WORDS = '''apple [ˈæpl] n. 苹果
banana [bəˈnɑːnə] n. 香蕉；芭蕉
candidate [ˈkændɪdət] n. 候选人；应试者；申请人；候补者；竞选人；报名者
diligent [ˈdɪlɪdʒənt] adj. 勤勉的；用功的
effort [ˈefət] n. 努力；尝试
fragile [ˈfrædʒaɪl] adj. 易碎的；脆弱的
'''


@pytest.fixture
def wordlist(tmp_path):
    path = tmp_path / 'words.txt'
    path.write_text(WORDS, encoding='utf-8')
    return path


def request_for(wordlist, tmp_path, **overrides):
    values = {'wordlist': str(wordlist), 'start': 1, 'end': 6, 'batch_size': 3,
              'output': str(tmp_path / 'out'), 'work_dir': str(tmp_path / 'work')}
    values.update(overrides)
    resolved = LegacyRequest(**values).resolve()
    Path(resolved.work_dir).mkdir(parents=True, exist_ok=True)
    Path(resolved.output).mkdir(parents=True, exist_ok=True)
    return resolved


def test_the_new_entry_reproduces_the_old_entry_track_for_track(wordlist, tmp_path):
    """The hard indicator, on a synthetic list: same request, same bytes, same ms."""
    old_request = request_for(wordlist, tmp_path / 'old')
    new_request = request_for(wordlist, tmp_path / 'new')
    entry = locate_legacy_entry()
    old = run_old_entry(old_request, entry, default_python())
    assert old['returncode'] == 0, old
    new = run_legacy(new_request, action='generate',
                     report_path=str(tmp_path / 'new-report.json'))
    # Same request document apart from where the files go: the mapping is the old
    # tool's own field names, so "the same parameters" is a fact, not an intention.
    assert ({key: value for key, value in new['legacy_request'].items() if key != 'output'}
            == {key: value for key, value in old_request.to_legacy_request('generate').items()
                if key != 'output'})
    comparison = compare_runs(old['stdout_file'], tmp_path / 'new-report.json')
    assert comparison['tracks_missing'] == []
    assert [row['verdict'] for row in comparison['tracks']] == ['identical'] * 5
    assert comparison['difference_count'] == 0
    assert all(row['left_cues'] == row['right_cues'] for row in comparison['tracks'])
    # Two packages of three words: 5 tracks each, and the old core's own counts.
    assert new['counts'] == {'total_count': 6, 'selected_count': 6, 'package_count': 2,
                             'file_count': 10, 'published_checked': 10}
    assert len(list(Path(new['directory']).glob('*.srt'))) == 10
    # Every published file was re-read from disk and hashed outside the old process.
    assert all(row['recorded_sha256'] == row['sha256'] for row in new['tracks'])


def test_a_one_millisecond_difference_is_not_rounded_away(tmp_path):
    """The equivalence is exact, so the comparison must be able to see 1 ms."""
    left = tmp_path / 'left.srt'
    right = tmp_path / 'right.srt'
    left.write_text('1\n00:00:00,000 --> 00:00:01,000\napple\n\n', encoding='utf-8-sig')
    right.write_text('1\n00:00:00,000 --> 00:00:01,001\napple\n\n', encoding='utf-8-sig')
    files = [{'track': 1, 'relative_path': 'a_01.srt', 'path': str(left), 'sha256': ''}]
    other = [{'track': 1, 'relative_path': 'b_01.srt', 'path': str(right), 'sha256': ''}]
    comparison = compare_runs({'tracks': files}, {'tracks': other})
    assert comparison['verdict'] == 'differs'
    assert comparison['tracks'][0]['bytes_identical'] is False
    assert comparison['tracks'][0]['verdict'] == 'times_differ'
    assert comparison['differences'][0]['left']['end_ms'] == 1000
    assert comparison['differences'][0]['right']['end_ms'] == 1001
    assert comparison['differences'][0]['word'] == 'apple'


def test_the_adapter_never_imports_the_old_core_into_its_own_process():
    """A subprocess boundary, not an import: the old modules stay out of this process."""
    code = ('import json, sys; import legacy_adapter; '
            'print(json.dumps(sorted(name for name in sys.modules '
            'if name.startswith(("subtitle_factory", "Words_SRT")))))')
    completed = subprocess.run([sys.executable, '-c', code], cwd=str(Path.cwd()),
                               capture_output=True, text=True, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == []


def test_the_child_runs_in_the_given_directory_with_temp_inside_it(wordlist, tmp_path):
    """cwd, TEMP and TMP are the D-drive directory this run made - nothing on C."""
    request = request_for(wordlist, tmp_path)
    environment = legacy_environment(request.work_dir)
    assert Path(environment['TEMP']) == Path(request.work_dir) / 'tmp'
    assert Path(environment['TMP']) == Path(request.work_dir) / 'tmp'
    new = run_legacy(request, action='generate')
    assert Path(new['adapter']['work_dir']) == Path(request.work_dir)
    assert (Path(request.work_dir) / 'legacy-request.json').is_file()
    assert (Path(request.work_dir) / 'legacy-run.json').is_file()
    # The child's own report of what it did was written inside that directory.
    journal = json.loads((Path(request.work_dir) / 'legacy-run.json').read_text(encoding='utf-8'))
    assert journal['schema'] == 'wv-legacy-run@1' and journal['tracks']


def test_the_default_directories_are_new_directories_on_the_d_work_root():
    output, work = Path(default_output_dir()), Path(adapter_environment.default_work_dir())
    for path in (output, work):
        assert path.drive.upper() == 'D:'
        assert str(path).startswith(str(adapter_environment.work_root()))
        assert not path.exists() or not any(path.iterdir()), '默认目录必须是新目录'


def test_writing_into_a_protected_tree_is_refused_before_anything_runs(wordlist):
    """The old core, the archive and the draft folder are read-only by rule."""
    for protected in adapter_environment.protected_roots():
        request = LegacyRequest(wordlist=str(wordlist), start=1, end=1,
                                output=str(protected / 'w10-must-not-exist'),
                                work_dir=str(protected / 'w10-work'))
        with pytest.raises(LegacyAdapterError) as raised:
            request.resolve()
        assert raised.value.code == 'LEGACY_OUTPUT_UNSAFE'
        assert str(protected) in raised.value.details['protected_root']
    # And the factory's own directory is not an output directory either.
    factory = locate_legacy_entry().parent
    request = LegacyRequest(wordlist=str(wordlist), start=1, end=1,
                            output=str(factory / 'w10-must-not-exist'))
    with pytest.raises(LegacyAdapterError) as raised:
        request.resolve()
    assert raised.value.code == 'LEGACY_OUTPUT_UNSAFE'
    assert raised.value.details['factory_dir'] == str(factory)


def test_a_missing_old_entry_is_a_structured_refusal_with_the_places_it_looked(wordlist):
    request = LegacyRequest(wordlist=str(wordlist), start=1, end=1,
                            legacy_script=str(wordlist) + '-nope')
    with pytest.raises(LegacyAdapterError) as raised:
        run_legacy(request)
    assert raised.value.code == 'LEGACY_ENTRY_MISSING'
    assert raised.value.exit_code == EXIT_CODES['LEGACY_ENTRY_MISSING'] == 4
    assert raised.value.details['explicit'].endswith('-nope')


def test_a_bad_word_list_keeps_the_old_tools_own_code_and_details(tmp_path):
    broken = tmp_path / 'broken.txt'
    broken.write_text('!!! not a word list\n', encoding='utf-8')
    with pytest.raises(LegacyAdapterError) as raised:
        run_legacy(request_for(broken, tmp_path))
    # The old core's code is passed through, not translated into ours.
    assert raised.value.code == 'INPUT_ERROR'
    assert raised.value.exit_code == 3
    assert raised.value.details['lines'], raised.value.details


def test_the_new_video_software_cannot_break_the_old_entry(wordlist, tmp_path):
    """Failure isolation, direction one: break ``word_video``, the old entry still runs.

    The blocker is installed through ``sitecustomize`` in the child's ``PYTHONPATH``,
    so *every* process started with that environment fails to import the new engine -
    which is exactly the failure this entry has to survive, and which the second half
    of the test proves is real rather than assumed.
    """
    blocker = tmp_path / 'blocker'
    blocker.mkdir()
    (blocker / 'sitecustomize.py').write_text(
        'import sys\n'
        'class _Block:\n'
        '    def find_spec(self, fullname, path=None, target=None):\n'
        '        if fullname == "word_video" or fullname.startswith("word_video."):\n'
        '            raise ImportError("blocked: the new video software is down")\n'
        '        return None\n'
        'sys.meta_path.insert(0, _Block())\n', encoding='utf-8')
    environment = dict(os.environ, PYTHONPATH=str(blocker))
    broken = subprocess.run([sys.executable, '-c', 'import word_video'],
                            cwd=str(Path.cwd()), env=environment, capture_output=True,
                            text=True, encoding='utf-8')
    assert broken.returncode != 0 and 'blocked' in broken.stderr, broken.stderr

    output = tmp_path / 'blocked-out'
    timeline = tmp_path / 'blocked-timeline.json'
    timeline.write_text(json.dumps({'fps': 60, 'words': [{'word': 'apple', 'end_frame': 198}]},
                                   ensure_ascii=False), encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, '-m', 'legacy_adapter', 'generate', '--wordlist', str(wordlist),
         '--start', '1', '--end', '3', '--batch-size', '3', '--output', str(output),
         '--work-dir', str(tmp_path / 'blocked-work'), '--timeline', str(timeline)],
        cwd=str(Path.cwd()), env=environment, capture_output=True, text=True, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip())
    assert report['ok'] is True and report['counts']['file_count'] == 5
    assert len(list(output.glob('*.srt'))) == 5
    # The new caliber is a label, not a dependency: it says why it cannot be shown.
    assert report['media_caliber']['available'] is False
    assert 'word_video' in report['media_caliber']['reason']


def test_the_reverse_direction_new_side_works_while_the_old_one_is_broken(tmp_path):
    """Failure isolation, direction two: a broken old entry leaves the engine intact."""
    from word_video.storage.assets import ASSETS_FILENAME  # noqa: F401 - engine import

    broken = tmp_path / 'broken.txt'
    broken.write_text('!!! not a word list\n', encoding='utf-8')
    with pytest.raises(LegacyAdapterError):
        run_legacy(request_for(broken, tmp_path))
    # Nothing about that failure reached the new software: it still imports, and the
    # same process can keep using it (the editor tests drive the window the same way).
    import word_video.domain.timebase as timebase
    assert timebase.ticks_to_milliseconds(720000) == 1000


def test_both_calibers_are_declared_and_shown_with_real_numbers(wordlist, tmp_path, capsys):
    """The two clocks, as data and as numbers, from the old core's own plan."""
    declared = {block['id']: block for block in calibers()}
    assert set(declared) == {CALIBER_LEGACY, CALIBER_MEDIA}
    assert '文字规则' in declared[CALIBER_LEGACY]['label']
    assert '实际时长' in declared[CALIBER_MEDIA]['label']
    assert '文字规则' in UI_NOTICE and '音频实际时长' in UI_NOTICE

    timeline = tmp_path / 'timeline.json'
    timeline.write_text(json.dumps({'first_index': 1, 'last_index': 2, 'fps': 60,
                                    'total_frames': 400,
                                    'words': [{'index': 1, 'word': 'apple', 'end_frame': 198},
                                              {'index': 2, 'word': 'banana',
                                               'end_frame': 400}]},
                                   ensure_ascii=False), encoding='utf-8')
    code = adapter_cli.main(['calibers', '--wordlist', str(wordlist), '--start', '1',
                             '--end', '2', '--batch-size', '2', '--timeline', str(timeline)])
    assert code == 0
    report = json.loads(capsys.readouterr().out.strip())
    legacy, media = report['legacy_numbers'], report['media_numbers']
    assert legacy['available'] is True and legacy['total_duration_ms'] > 0
    assert legacy['formula'] == declared[CALIBER_LEGACY]['formula']
    assert media['available'] is True
    # 198 frames at 60 fps is 3300 ms: the engine's own conversion, not a second one.
    assert media['measured']['first_end_ms'] == 3300
    assert media['measured']['last_end_ms'] == 6667
    assert media['measured']['total_duration_ms'] == 6667
    assert (report['comparison']['legacy_total_ms'] - report['comparison']['media_total_ms']
            == report['comparison']['delta_ms'])
    assert report['comparison']['same_selection'] is True
    assert '同一词表同一选区' in report['comparison']['scope_note']


def test_calibers_without_a_timeline_says_what_is_missing(wordlist, capsys):
    assert adapter_cli.main(['calibers', '--wordlist', str(wordlist), '--start', '1',
                             '--end', '2']) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report['media_numbers']['available'] is False
    assert '--timeline' in report['media_numbers']['reason']
    assert report['comparison'] is None


def test_a_timeline_that_is_not_one_is_reported_not_guessed(tmp_path):
    wrong = tmp_path / 'timeline.json'
    wrong.write_text('{"random": true}', encoding='utf-8')
    block = timeline_caliber(wrong)
    assert block['available'] is False and 'words' in block['reason']
    missing = timeline_caliber(tmp_path / 'nowhere')
    assert missing['available'] is False and '找不到' in missing['reason']


def test_doctor_reports_the_old_tool_and_the_d_directories(capsys):
    assert adapter_cli.main(['doctor']) == 0
    report = json.loads(capsys.readouterr().out.strip())
    assert report['ok'] is True
    assert Path(report['legacy']['entry']).name == 'subtitle_factory_cli.py'
    assert report['legacy']['doctor']['result']['gui_required'] is False
    assert report['legacy']['doctor']['result']['network_required'] is False
    assert Path(report['defaults']['output']).drive.upper() == 'D:'
    assert {block['id'] for block in report['calibers']} == {CALIBER_LEGACY, CALIBER_MEDIA}


def test_doctor_without_the_old_entry_still_answers(wordlist, tmp_path, capsys):
    """A support call must get a structured answer even when the entry is missing."""
    code = adapter_cli.main(['doctor', '--legacy-script', str(tmp_path / 'nope.py')])
    assert code == 4
    report = json.loads(capsys.readouterr().out.strip())
    assert report['ok'] is False
    assert report['problems'][0]['code'] == 'LEGACY_ENTRY_MISSING'
    assert report['defaults']['work_dir']


def test_a_request_that_is_not_runnable_is_refused_by_code(wordlist):
    with pytest.raises(LegacyAdapterError) as raised:
        LegacyRequest(wordlist=str(wordlist), start=5, end=1).resolve()
    assert raised.value.code == 'INVALID_REQUEST'
    with pytest.raises(LegacyAdapterError) as raised:
        LegacyRequest(wordlist=str(wordlist), batch_size=0).resolve()
    assert raised.value.code == 'INVALID_REQUEST'
    with pytest.raises(LegacyAdapterError) as raised:
        LegacyRequest(wordlist=str(Path(wordlist).parent / 'nope.txt')).resolve()
    assert raised.value.code == 'INPUT_ERROR'
    with pytest.raises(LegacyAdapterError) as raised:
        LegacyRequest(wordlist=str(wordlist), first_six=0).resolve()
    assert raised.value.code == 'INVALID_REQUEST'
