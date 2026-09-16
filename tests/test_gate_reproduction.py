"""Gate step: reproduce the delivered archive from the local source, and judge it.

This is the "same input still produces the same batch" question, run inside the
gate (`pytest -m acceptance_media`) but written as its own test so its evidence
and its cost stay visible:

  build the request from the archive's own timeline (unprocessed audio in the
  audio cache), run it through the engine, then compare with the archive -
  timeline field by field, the five SRTs byte for byte and every file hash.

Cost: one full 50-word render (~90 s).  It is re-run only when the engine, the
acceptance tools or the word list changed; otherwise the previous run's evidence
is re-used and the test says so, because the fingerprint proves the same code
read the same inputs.  Evidence goes to ``out/reports/``.
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

pytestmark = pytest.mark.acceptance_media

WORKTREE = gate.worktree_of(__file__)
ANCHOR = WORKTREE / 'runtime' / 'tmp' / 'QA' / 'gate'


def archive_of():
    path = Path(gate.GOOD_BATCH)
    if not (path / 'timeline.json').exists():
        pytest.fail('正常对照归档不可用：%s' % path)
    return path


def timeline_of(archive):
    return json.loads((Path(archive) / 'timeline.json').read_text(encoding='utf-8'))


def produce(archive, evidence):
    """Run one fresh 50-word job and return (batch, seconds, state)."""
    timeline = timeline_of(archive)
    request = builder.build_request(builder.config(
        archive=str(archive), wordlist=str(gate.WORDLIST),
        out=str(evidence / 'reproduction-request.json'),
        output=str(evidence / 'reproduction-run'), key='gate-reproduction-r1',
        intro=timeline.get('intro_video') or None,
        first=int(timeline['first_index']), last=int(timeline['last_index'])))
    request_path = evidence / 'reproduction-request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=1),
                            encoding='utf-8')
    ANCHOR.mkdir(parents=True, exist_ok=True)
    db = ANCHOR / 'gate-reproduction.sqlite3'
    cli = [sys.executable, str(WORKTREE / 'word_video_cli.py'), '--db', str(db)]
    submit = gate.run([*cli, 'submit', '--request', str(request_path)], WORKTREE)
    assert submit['exit_code'] == 0, submit
    job = gate.job_id(submit)
    work = gate.run([*cli, 'work', '--job', job], WORKTREE)
    assert work['exit_code'] == 0, work
    state = (gate.last_json(work) or {}).get('result') or {}
    assert state.get('state') == 'generated', state
    root = Path(request['output']) / job
    return next(p.parent for p in root.rglob('timeline.json')), work['seconds'], state


def test_reproduction_matches_the_delivered_archive():
    archive = archive_of()
    evidence, warning = gate.evidence_dir()
    inputs = [archive.name, str(gate.WORDLIST)]
    usable, why = gate.reusable(evidence, 'reproduction', WORKTREE, inputs)

    batch = None
    if usable:
        recorded = evidence / 'reproduction-batch.txt'
        if recorded.exists():
            candidate = Path(recorded.read_text(encoding='utf-8').strip())
            if (candidate / 'timeline.json').exists():
                batch = candidate
        if batch is None:
            usable, why = False, '上次记录的批次已不在'

    seconds = None
    if batch is None:
        batch, seconds, _ = produce(archive, evidence)
        (evidence / 'reproduction-batch.txt').write_text(str(batch), encoding='utf-8')
        why = '本次重跑（%s）' % why

    comparison = evidence / 'reproduction-vs-archive.json'
    if comparison.exists():
        comparison.unlink()          # never let a stale comparison speak
    finished = gate.run([sys.executable, str(WORKTREE / 'tests' / 'qa2'
                                             / 'compare_to_archive.py'),
                         '--mine', str(batch), '--archive', str(archive),
                         '--json', str(comparison)], WORKTREE)
    assert comparison.exists(), finished
    report = json.loads(comparison.read_text(encoding='utf-8'))
    assert report['timeline']['timeline_equal'], \
        report['timeline']['field_differences'][:5]
    assert set(report['srt']['files'].values()) == {'identical'}, report['srt']['files']
    assert report['complete']['ok'], report['complete']
    gate.write_stamp(evidence, 'reproduction', WORKTREE, inputs,
                     note={'batch': str(batch), 'reused': usable, 'why': why,
                           'seconds': seconds, 'evidence': str(comparison),
                           'warning': warning})
