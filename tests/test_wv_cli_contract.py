"""The Agent-facing CLI contract: one JSON object, structured errors, no dialogs.

An Agent cannot read a usage line on stderr or answer a dialog, so these tests pin
the three things it depends on: every outcome leaves **one** JSON object on stdout
(verified through a real subprocess for at least one case, because an in-process
call cannot prove nothing else printed); a missing piece of information comes back
as ``NEEDS_INPUT`` with a ``fixes`` list; and ``batch plan`` writes nothing at all.
"""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_wv_coordinator_store import fake_runner

from word_video.cli.main import main
from word_video.storage.coordinator import Coordinator
from word_video.storage.project_store import save_project
from word_video.application import instantiate
from word_video.domain import DEFAULT_LESSON_TEMPLATE

from test_wv_project_golden import golden_base, golden_media, golden_record


def run_cli(*argv, root=None):
    """Call the CLI in process; return (exit code, parsed stdout, stderr text)."""
    out, err = io.StringIO(), io.StringIO()
    arguments = list(argv)
    if root is not None:
        arguments = ['--root', str(root)] + arguments
    code = main(arguments, stdout=out, stderr=err)
    text = out.getvalue()
    try:
        document = json.loads(text)
    except ValueError:                      # pragma: no cover - the contract itself
        raise AssertionError('stdout was not one JSON object: %r' % text[:400])
    return code, document, err.getvalue()


def prepare(root, *, project_id='golden'):
    """A project + registry under the coordinator's own layout, with a picture."""
    from word_video.storage import AssetIndex, AssetRef
    folder = root / 'projects' / project_id
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(index=151),), golden_media(),
                          golden_base())
    folder.mkdir(parents=True, exist_ok=True)
    save_project(project, folder)
    (folder / 'media').mkdir(exist_ok=True)
    for name in ('female', 'male', 'chinese'):
        (folder / 'media' / ('%s.wav' % name)).write_bytes(b'x' * 16)
    # A delivery background is a *delivery* input, not project content: a batch
    # without one is refused by name (see `batch submit ... --background`).
    (folder / 'background.mp4').write_bytes(b'picture')
    AssetIndex.of((AssetRef('w1:female', 'media/female.wav', units=55200),
                   AssetRef('w1:male', 'media/male.wav', units=40800),
                   AssetRef('w1:chinese', 'media/chinese.wav', units=157200)),
                  project_id=project.project_id, folder=str(folder)).save(folder)
    return folder


def background(root, project_id='golden'):
    return str(root / 'projects' / project_id / 'background.mp4')


# ---------------------------------------------------------------------------
# One JSON object, always
# ---------------------------------------------------------------------------
def test_a_real_subprocess_prints_exactly_one_json_object(tmp_path):
    result = subprocess.run(
        [sys.executable, '-m', 'word_video.cli', '--root', str(tmp_path),
         'capabilities'],
        capture_output=True, text=True, encoding='utf-8',
        cwd=str(Path(__file__).resolve().parents[1]))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)             # one object, nothing else
    assert payload['ok'] is True
    assert set(payload['result']['actions']) >= {'capabilities', 'doctor', 'project',
                                                 'batch', 'job', 'artifacts', 'watch'}
    assert payload['result']['schemas']['project'].startswith('wv-project@')


def test_bad_usage_and_unknown_actions_are_still_json(tmp_path):
    for argv in ((), ('nonsense',), ('job',), ('project', 'show')): 
        code, document, _ = run_cli(*argv, root=tmp_path)
        assert code == 2, argv
        assert document['ok'] is False, argv
        assert document['error']['code'] in ('BAD_REQUEST', 'NEEDS_INPUT'), argv
        assert document['error']['message'], argv


def test_needs_input_carries_actionable_fixes(tmp_path):
    prepare(tmp_path)
    code, document, _ = run_cli('batch', 'submit', '--project', 'golden',
                                '--batch', '151-151', root=tmp_path)
    assert code == 2
    assert document['error']['code'] == 'NEEDS_INPUT'
    assert document['error']['fixes']
    assert any('file' in fix or '工程' in fix or '--file' in fix
               or 'key' in fix or '词条' in fix for fix in document['error']['fixes'])
    # A missing project is a NEEDS_INPUT too, never a traceback.
    code, document, _ = run_cli('batch', 'plan', '--project', 'ghost', '--batch',
                                '151-151', root=tmp_path)
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'


def test_stdout_is_utf8_even_where_the_console_code_page_cannot_hold_it(tmp_path):
    """A character the code page lacks must not turn the JSON into a traceback.

    The phonetic field carries IPA (``ˈæpl``), which a GBK console cannot encode:
    writing it after a successful action would raise *after* the work was done, so
    the caller would see a crash instead of the answer it asked for.
    """
    prepare(tmp_path)
    project = tmp_path / 'projects' / 'golden' / 'project.json'
    document = json.loads(project.read_text(encoding='utf-8'))
    document['records'][0]['word'] = 'ˈæpl'         # U+02C8 is not in GBK
    project.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    program = (
        'import sys\n'
        'sys.path.insert(0, sys.argv[1])\n'
        'from word_video.cli.main import main\n'
        'raise SystemExit(main(sys.argv[2:]))\n')
    result = subprocess.run(
        [sys.executable, '-c', program, str(Path(__file__).resolve().parents[1]),
         '--root', str(tmp_path), 'project', 'show', '--project', 'golden'],
        capture_output=True, cwd=str(Path(__file__).resolve().parents[1]))
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    payload = json.loads(result.stdout.decode('utf-8'))     # bytes, not locale text
    assert payload['result']['records'][0]['word'] == 'ˈæpl'


# ---------------------------------------------------------------------------
# Plan writes nothing; submit freezes; receipts are checkable
# ---------------------------------------------------------------------------
def test_plan_reports_the_batch_and_writes_nothing(tmp_path):
    prepare(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*'))
    code, document, _ = run_cli('batch', 'plan', '--project', 'golden', '--batch',
                                '151-151', root=tmp_path)
    assert code == 0, document
    result = document['result']
    assert result['wrote_files'] is False
    assert result['plan']['plan_identity']
    assert result['plan']['total_ticks'] == 3960000
    # The picture is a delivery input, so the preflight names the miss and hands back
    # the fix instead of letting a run discover it as PermissionError on the cwd.
    assert result['ready'] is False
    assert result['plan']['delivery']['ready'] is False
    assert [problem['code'] for problem in result['plan']['delivery']['problems']] == \
        ['BACKGROUND_MISSING']
    assert '--background' in ' '.join(result['delivery_fixes'])

    with_picture = run_cli('batch', 'plan', '--project', 'golden', '--batch', '151-151',
                           '--background', background(tmp_path), root=tmp_path)[1]
    assert with_picture['result']['ready'] is True
    assert with_picture['result']['delivery_fixes'] == []
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')) == \
        before


def test_submit_without_a_background_is_needs_input_not_internal(tmp_path):
    """H0's second finding, as a counterexample: a missing picture is a question.

    The empty background used to reach the exporter as ``Path('')`` — the current
    working directory — and came back as ``PermissionError: [Errno 13]`` on a folder,
    reported as INTERNAL.  Nothing may be frozen either: the refusal happens before
    the receipt exists.
    """
    prepare(tmp_path)
    code, document, _ = run_cli('batch', 'submit', '--project', 'golden', '--batch',
                                '151-151', '--key', 'no-picture', root=tmp_path)
    assert code == 2, document
    assert document['error']['code'] == 'NEEDS_INPUT'      # not INTERNAL
    assert '--background' in ' '.join(document['error']['fixes'])
    assert run_cli('receipts', root=tmp_path)[1]['result']['receipts'] == []

    # A background that is not there is named as itself, not probed as the cwd.
    code, document, _ = run_cli('batch', 'submit', '--project', 'golden', '--batch',
                                '151-151', '--key', 'ghost',
                                '--background', str(tmp_path / 'ghost.mp4'),
                                root=tmp_path)
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'
    assert 'ghost.mp4' in json.dumps(document['error'], ensure_ascii=False)


def test_submit_status_cancel_and_receipts_through_the_cli(tmp_path):
    prepare(tmp_path)
    picture = background(tmp_path)
    code, document, _ = run_cli('batch', 'submit', '--project', 'golden', '--batch',
                                '151-151', '--background', picture, '--key', 'cli-1',
                                root=tmp_path)
    assert code == 0, document
    job = document['result']['job']
    assert document['result']['created'] is True

    again = run_cli('batch', 'submit', '--project', 'golden', '--batch', '151-151',
                    '--background', picture, '--key', 'cli-1', root=tmp_path)[1]
    assert again['result']['created'] is False and again['result']['job'] == job

    status = run_cli('job', 'status', '--job', job, root=tmp_path)[1]
    assert status['result'] == {'job': job, 'state': 'queued', 'phase': 'submitted',
                               'control': 'run', 'error': None, 'artifacts': 0}

    receipts = run_cli('receipts', root=tmp_path)[1]['result']['receipts']
    assert [row['idempotency_key'] for row in receipts] == ['cli-1']
    assert receipts[0]['job_id'] == job
    assert json.loads(receipts[0]['inputs'])[0]['sha256']

    # Conflicting content under the same key is a conflict, not a new task.
    conflict = run_cli('batch', 'submit', '--project', 'golden', '--batch', '151-151',
                       '--background', picture, '--codec', 'h265', '--key', 'cli-1',
                       root=tmp_path)
    assert conflict[0] == 1
    assert conflict[1]['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    cancelled = run_cli('job', 'cancel', '--job', job, root=tmp_path)[1]
    assert cancelled['result']['state'] == 'cancelled'
    # Running a cancelled task does nothing (and needs no renderer to prove it).
    ran = run_cli('job', 'run', '--job', job, root=tmp_path)[1]
    assert ran['result']['state'] == 'cancelled'
    assert run_cli('artifacts', '--job', job, root=tmp_path)[1]['result']['published'] \
        is False
    assert not (tmp_path / 'runs' / job).exists()


def test_batch_range_selects_by_word_list_index(tmp_path):
    prepare(tmp_path)
    code, document, _ = run_cli('batch', 'plan', '--project', 'golden', '--records',
                                'w1', root=tmp_path)
    assert code == 0
    assert [record['id'] for record in document['result']['plan']['records']] == ['w1']
    code, document, _ = run_cli('batch', 'plan', '--project', 'golden', '--records',
                                'nope', root=tmp_path)
    assert code == 1 and document['error']['code'] == 'SCHEMA'


# ---------------------------------------------------------------------------
# Watch streams NDJSON, and reconcile/reporting work through the CLI
# ---------------------------------------------------------------------------
def test_watch_streams_ndjson_until_the_task_settles(tmp_path):
    prepare(tmp_path)
    job = run_cli('batch', 'submit', '--project', 'golden', '--batch', '151-151',
                  '--background', background(tmp_path), '--key', 'w',
                  root=tmp_path)[1]['result']['job']
    run_cli('job', 'cancel', '--job', job, root=tmp_path)
    out = io.StringIO()
    code = main(['--root', str(tmp_path), 'watch', '--job', job,
                 '--until-seconds', '2'], stdout=out, stderr=io.StringIO())
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert code == 0
    assert lines and lines[-1]['state'] == 'cancelled'
    assert all({'job', 'state', 'phase'} <= set(line) for line in lines)


def test_reconcile_reports_a_task_a_dead_worker_left_behind(tmp_path):
    prepare(tmp_path)
    job = run_cli('batch', 'submit', '--project', 'golden', '--batch', '151-151',
                  '--background', background(tmp_path), '--key', 'r',
                  root=tmp_path)[1]['result']['job']
    from word_video.storage.coordinator import connect
    with connect(tmp_path / 'coordinator.sqlite3') as con:
        con.execute("UPDATE jobs SET state='running', phase='rendering' WHERE id=?",
                    (job,))
    code, document, _ = run_cli('reconcile', root=tmp_path)
    assert code == 0
    assert job in document['result']['interrupted']
    assert run_cli('job', 'status', '--job', job, root=tmp_path)[1]['result']['state'] \
        == 'interrupted'


def test_doctor_reports_what_it_can_see(tmp_path):
    code, document, _ = run_cli('doctor', root=tmp_path)
    assert code == 0
    report = document['result']
    assert report['root_writable'] is True
    assert report['ffmpeg'] and report['ffprobe']
    assert isinstance(report['font_substitutions'], list)
