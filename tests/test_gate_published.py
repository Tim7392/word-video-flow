"""Gate step: the coordinator publishes a run a reader can trust.

Two projects, because "is this batch deliverable" has two answers by design:

  * a project that **carries delivery settings** (``delivery.json``, what the
    editor writes) is ready without ``--background``: the plan inherits the
    project's delivery and a command-line argument only overrides it.  The gate
    checks the inheritance really happened by comparing the plan's background with
    the file's, rather than by trusting the flag;
  * a project that carries **no delivery settings** is not ready: ``batch plan``
    says why, ``batch submit`` answers ``NEEDS_INPUT`` (exit 2) and leaves **no
    receipt** - a refusal must not leave a record that looks like something was
    produced.

Then the run itself: submit with the background, run it, and read the published
folder the way an outside reader would - every path in the three documents exists,
nothing names the staging folder the run worked in, and the acceptance tool's
timing face is **complete** (``speech.json`` published, every stage recomputed).

Preconditions, stated so the next reader does not think the gate is broken:

* the project's ``assets.json`` must carry a ``voice`` for every speech asset.
  ``batch submit`` refuses with ``NEEDS_INPUT`` when a record has no voice - that
  is the product's current behaviour (A-8), not a gate failure.  This test writes
  the field directly, which is the documented way, and never goes through
  ``import audio --apply`` while that command's write path is under repair.
* the refusal case needs the **no-delivery** project: a project that carries a
  background is supposed to be ready, so judging the refusal on it would be wrong.

Expectations come from the read-only word list, the hand-checked rhythm constants
and the project's own delivery settings - never from the run's output.

Evidence: ``out/reports/gate-published-<date>.json`` plus the acceptance report
and the per-document report next to it.
"""
import json
from pathlib import Path
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance import gate  # noqa: E402
from qa2 import make_real_project  # noqa: E402
from qa2 import publish_check  # noqa: E402

pytestmark = pytest.mark.acceptance_media

WORKTREE = gate.worktree_of(__file__)
RANGE = '151-153'
BACKGROUND = Path(r'D:\单词速记自动化_测试归档_0915\_prepared-1080p'
                   r'\background-from-reference-1080p.mp4')
ARCHIVE = gate.GOOD_BATCH


def build_project(root, project, background, no_delivery=False):
    """A real project: word-list records, archived recordings, engine's template."""
    argv = ['--root', str(root), '--wordlist', str(gate.WORDLIST),
            '--archive', str(ARCHIVE), '--range', RANGE, '--project', project,
            '--background', str(background)]
    if no_delivery:
        argv.append('--no-delivery')
    assert make_real_project.main(argv) == 0, '真工程没有建起来：%s' % project
    # Precondition (see the module docstring): a speech asset must name its voice,
    # otherwise `batch submit` answers NEEDS_INPUT and the gate would report a
    # product behaviour as a failure.
    assets_path = Path(root) / 'projects' / project / 'assets.json'
    assets = json.loads(assets_path.read_text(encoding='utf-8'))
    voices = {'female': 'BV503_streaming', 'male': 'BV504_streaming',
              'chinese': 'BV406_streaming'}
    for item in assets['assets']:
        role = item['asset_id'].split(':')[-1]
        item['voice'] = voices.get(role, item.get('voice', ''))
    assets_path.write_text(json.dumps(assets, ensure_ascii=False, indent=1),
                           encoding='utf-8')


def test_the_gate_publishes_and_reads_back_a_run():
    evidence, warning = gate.evidence_dir()
    today = time.strftime('%Y%m%d')
    root = gate.WORK_ROOT / 'runtime' / 'tmp' / 'QA' / 'gate-published-root'
    if root.exists():
        import shutil
        shutil.rmtree(root)
    if not BACKGROUND.exists():
        pytest.fail('背景素材不可用：%s' % BACKGROUND)
    if not (Path(ARCHIVE) / 'timeline.json').exists():
        pytest.fail('对照归档不可用：%s' % ARCHIVE)

    build_project(root, 'with-delivery', BACKGROUND)
    build_project(root, 'no-delivery', BACKGROUND, no_delivery=True)

    report_file = evidence / ('gate-published-%s.json' % today)
    finished = publish_check.main([
        '--worktree', str(WORKTREE), '--root', str(root),
        '--project', 'with-delivery', '--refusal-project', 'no-delivery',
        '--project-file', str(root / 'projects' / 'with-delivery' / 'delivery.json'),
        '--refusal-project-file', str(root / 'projects' / 'no-delivery' / 'delivery.json'),
        '--batch', RANGE, '--background', str(BACKGROUND), '--codec', 'h265',
        '--key', 'gate-published-%s' % today, '--json', str(report_file)])
    assert report_file.exists(), '发布检查没有写出报告'
    report = json.loads(report_file.read_text(encoding='utf-8'))

    # (1) A project that carries delivery settings is ready without --background,
    #     and the plan's background is the one the project states.
    inherited = report['plan_without_argument']
    assert inherited['ready'] is True, inherited
    assert inherited['background'] == str(BACKGROUND), inherited
    assert inherited['matches_delivery_json'] is True, inherited
    # (2) A project with no delivery settings is refused, and the refusal leaves
    #     no receipt.
    assert report['plan_refused_without_background'], \
        report['calls']['plan_without_background']
    assert report['refusal_is_needs_input'], report['calls']['submit_without_background']
    assert report['refusal_left_no_receipt'], '拒绝却留下了收据'
    # (3) The published run.
    assert report['run_state'] == 'succeeded', report.get('run_error')
    assert report['published_ok'], [d for d in report['documents'] if not d.get('ok')]
    assert finished == 0, '发布检查自评未通过'
    for document in report['documents']:
        assert document['missing'] == [] and document['staging'] == [], document

    accept_file = evidence / ('gate-published-accept-%s.json' % today)
    if accept_file.exists():
        accept_file.unlink()
    run_dir = report['run_dir']
    accepted = gate.run([sys.executable,
                         str(WORKTREE / 'tests' / 'acceptance' / 'accept_range.py'),
                         run_dir, '--source', str(gate.WORDLIST), '--range', RANGE,
                         '--cleaning', 'v2', '--pixels', 'all',
                         '--json', str(accept_file)], WORKTREE)
    assert accept_file.exists(), accepted
    verdict = json.loads(accept_file.read_text(encoding='utf-8'))
    assert verdict['verdict'] == 'PASS', verdict.get('failures')
    timing = verdict['timing']
    assert timing['speech_json'] is True, timing
    assert timing['complete'] is True, timing['skipped_checks']
    assert timing['recomputed_stages'] == timing['stages'] == 9, timing
    assert timing['digests_checked'] == 9, timing
    assert verdict['partial_faces'] == {}, verdict['partial_faces']

    gate.write_stamp(evidence, 'published', WORKTREE,
                     [RANGE, ARCHIVE.name, str(gate.WORDLIST)],
                     note={'evidence': str(report_file), 'accept': str(accept_file),
                           'run_dir': run_dir, 'job': report.get('job'),
                           'inherited_background': inherited['background'],
                           'documents': [{Path(d['document']).name: d['paths']}
                                         for d in report['documents']],
                           'warning': warning})
