"""Gate step: the spoken-policy judgement (v2 and legacy) inside the gate.

Two batches are produced from one input through ``source`` + ``range``, so the
job derives the reading text itself, and each is judged with the matching
policy: the legacy batch by ``--cleaning legacy`` and the v2 batch by
``--cleaning v2`` against **QA's own derived table** (the approved rule text
applied to the read-only word list, never A's implementation).  The legacy batch
must additionally still be identical to the delivered archive.

Cost: two 50-word renders (~165 s together).  Re-run only when the sources or the
word list changed; the previous run's evidence is re-used otherwise, and the
stamp records which batch and which table it belongs to.  Evidence goes to
``out/reports/``.
"""
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance import gate  # noqa: E402
from qa2 import build_request as builder  # noqa: E402
from qa3 import derive_spoken  # noqa: E402

pytestmark = pytest.mark.acceptance_media

WORKTREE = gate.worktree_of(__file__)
ANCHOR = WORKTREE / 'runtime' / 'tmp' / 'QA' / 'gate'


def archive_of():
    path = Path(gate.GOOD_BATCH)
    if not (path / 'timeline.json').exists():
        pytest.fail('正常对照归档不可用：%s' % path)
    return path


def derive_table(evidence):
    """QA's own reading text for every word-list line, as the expectation table."""
    table = evidence / 'spoken-v2-table.json'
    report = evidence / 'spoken-v2-derivation.json'
    finished = gate.run([sys.executable, str(WORKTREE / 'tests' / 'qa3'
                                             / 'derive_spoken.py'),
                         '--wordlist', str(gate.WORDLIST),
                         '--archive', str(archive_of()),
                         '--json', str(report), '--spoken-json', str(table)], WORKTREE)
    assert finished['exit_code'] == 0, finished
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['archive_anchor']['legacy_rule_confirmed'], data['archive_anchor']
    assert not data['residual_connectors'] and not data['v2_empty'], data
    return table, data


def produce(policy, table, evidence):
    timeline = json.loads((archive_of() / 'timeline.json').read_text(encoding='utf-8'))
    request = builder.build_request(builder.config(
        archive=str(archive_of()), wordlist=str(gate.WORDLIST),
        out=str(evidence / ('policy-request-%s.json' % policy)),
        output=str(evidence / ('policy-run-%s' % policy)),
        key='gate-policy-%s-r1' % policy, source_mode=True, policy=policy,
        spoken_file=str(table) if policy == 'v2' else None,
        intro=timeline.get('intro_video') or None,
        first=int(timeline['first_index']), last=int(timeline['last_index'])))
    request_path = evidence / ('policy-request-%s.json' % policy)
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=1),
                            encoding='utf-8')
    ANCHOR.mkdir(parents=True, exist_ok=True)
    db = ANCHOR / ('gate-policy-%s.sqlite3' % policy)
    cli = [sys.executable, str(WORKTREE / 'word_video_cli.py'), '--db', str(db)]
    submit = gate.run([*cli, 'submit', '--request', str(request_path)], WORKTREE)
    assert submit['exit_code'] == 0, submit
    job = gate.job_id(submit)
    work = gate.run([*cli, 'work', '--job', job], WORKTREE)
    assert work['exit_code'] == 0, work
    state = (gate.last_json(work) or {}).get('result') or {}
    assert state.get('state') == 'generated', state
    root = Path(request['output']) / job
    return next(p.parent for p in root.rglob('timeline.json')), work['seconds']


def test_both_policies_are_produced_and_judged(monkeypatch):
    archive = archive_of()
    evidence, warning = gate.evidence_dir()
    inputs = [archive.name, str(gate.WORDLIST), 'policy-v2-legacy']
    usable, why = gate.reusable(evidence, 'policy', WORKTREE, inputs)
    recorded = evidence / 'policy-batches.json'
    batches = json.loads(recorded.read_text(encoding='utf-8')) if recorded.exists() else {}
    if usable and not all(Path(path, 'timeline.json').exists()
                          for path in batches.values()) or not batches:
        usable, why = False, '上次记录的批次已不在'
    table, derivation = derive_table(evidence)

    seconds = {}
    if not usable:
        for policy in ('v2', 'legacy'):
            batch, seconds[policy] = produce(policy, table, evidence)
            batches[policy] = str(batch)
        recorded.write_text(json.dumps(batches, ensure_ascii=False, indent=1),
                            encoding='utf-8')
        why = '本次重跑（%s）' % why

    # Every judgement runs, re-used batch or not: it is cheap next to a render.
    reports = {}
    for policy, cleaning, spoken in (('v2', 'v2', table), ('legacy', 'legacy', None)):
        timeline = json.loads((Path(batches[policy]) / 'timeline.json').read_text(
            encoding='utf-8'))
        span = '%d-%d' % (int(timeline['first_index']), int(timeline['last_index']))
        target = evidence / ('policy-accept-%s.json' % policy)
        if target.exists():
            target.unlink()
        command = [sys.executable, str(WORKTREE / 'tests' / 'acceptance'
                                       / 'accept_range.py'), str(batches[policy]),
                   '--source', str(gate.WORDLIST), '--range', span,
                   '--cleaning', cleaning, '--pixels', 'all', '--json', str(target)]
        if spoken:
            command += ['--spoken-file', str(spoken)]
        finished = gate.run(command, WORKTREE)
        assert target.exists(), finished
        reports[policy] = json.loads(target.read_text(encoding='utf-8'))
        assert reports[policy]['verdict'] == 'PASS', reports[policy].get('failures')
    assert reports['v2']['expectations']['spoken_file'] == str(table), \
        'v2 判定没有用外部期望表，等于自己判自己'

    comparison = evidence / 'policy-legacy-vs-archive.json'
    finished = gate.run([sys.executable, str(WORKTREE / 'tests' / 'qa2'
                                             / 'compare_to_archive.py'),
                         '--mine', str(batches['legacy']), '--archive', str(archive),
                         '--json', str(comparison)], WORKTREE)
    assert comparison.exists(), finished
    legacy = json.loads(comparison.read_text(encoding='utf-8'))
    assert legacy['timeline']['timeline_equal'], legacy['timeline']['field_differences'][:5]
    assert set(legacy['srt']['files'].values()) == {'identical'}, legacy['srt']['files']

    diff = evidence / 'policy-v2-vs-legacy.json'
    finished = gate.run([sys.executable, str(WORKTREE / 'tests' / 'qa3'
                                             / 'policy_check.py'),
                         '--v2', str(batches['v2']), '--legacy', str(batches['legacy']),
                         '--archive', str(archive), '--wordlist', str(gate.WORDLIST),
                         '--spoken-file', str(table), '--out', str(evidence),
                         '--json', str(diff)], WORKTREE)
    assert diff.exists(), finished
    checked = json.loads(diff.read_text(encoding='utf-8'))
    assert checked['ok'], checked['problems']
    gate.write_stamp(evidence, 'policy', WORKTREE, inputs,
                     note={'batches': batches, 'reused': usable, 'why': why,
                           'seconds': seconds, 'table': str(table),
                           'changed_tracks': checked['v2_vs_legacy']['changed_tracks'],
                           'word_fields': checked['v2_vs_legacy']['word_fields'],
                           'warning': warning})
