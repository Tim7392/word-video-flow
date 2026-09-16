"""Gate step: the A-8 loop is closed through the real import command.

``import audio`` reads the recordings a member already has, pins a voice per role
and writes it into the project's ``assets.json``.  Before A-8 the command answered
``ok`` without writing anything, so the fix it advertised ("run this") could never
clear the blocker it named - a dead loop, and exactly the kind of thing a gate has
to keep closed.

This step drives the documented route on a project whose recordings are the
archive's own (hard links, original names - the shape the import matches against):

  1. a project with no voices: ``plan`` names the missing voice as a fix;
  2. ``import audio --roots <cache> --voices … --apply``: the command runs;
  3. ``assets.json`` now carries a voice for every speech asset, and running the
     import a second time changes nothing (idempotent);
  4. ``plan`` no longer blocks on that fix, and the batch submits.

Evidence: ``out/reports/gate-import-<date>.json``.
"""
import hashlib
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

pytestmark = pytest.mark.acceptance_media

WORKTREE = gate.worktree_of(__file__)
RANGE = '151-153'
BACKGROUND = Path(r'D:\单词速记自动化_测试归档_0915\_prepared-1080p'
                   r'\background-from-reference-1080p.mp4')
ARCHIVE = gate.GOOD_BATCH
VOICES = 'female=BV503_streaming,male=BV504_streaming,chinese=BV406_streaming'


def assets_digest(project_dir):
    return hashlib.sha256((Path(project_dir) / 'assets.json').read_bytes()).hexdigest()


def voices_of(project_dir):
    document = json.loads((Path(project_dir) / 'assets.json').read_text(encoding='utf-8'))
    return {item['asset_id']: item.get('voice') for item in document['assets']}


def test_the_import_route_writes_the_voice_and_clears_the_blocker():
    evidence, warning = gate.evidence_dir()
    today = time.strftime('%Y%m%d')
    root = gate.WORK_ROOT / 'runtime' / 'tmp' / 'QA' / 'gate-import-root'
    if root.exists():
        import shutil
        shutil.rmtree(root)
    if not (Path(ARCHIVE) / 'timeline.json').exists():
        pytest.fail('对照归档不可用：%s' % ARCHIVE)
    if not BACKGROUND.exists():
        pytest.fail('背景素材不可用：%s' % BACKGROUND)

    # A project whose recordings are the archive's own files, with no voice yet.
    project = 'p1'
    assert make_real_project.main([
        '--root', str(root), '--wordlist', str(gate.WORDLIST),
        '--archive', str(ARCHIVE), '--range', RANGE, '--project', project,
        '--background', str(BACKGROUND), '--link-media']) == 0
    project_dir = root / 'projects' / project
    before = voices_of(project_dir)
    assert set(before.values()) == {''}, before

    report = {'worktree': str(WORKTREE), 'root': str(root),
              'voices_before': before, 'calls': {}, 'warning': warning}
    cli = [sys.executable, '-m', 'word_video.cli', '--root', str(root)]

    plan_before = gate.run([*cli, 'batch', 'plan', '--project', project,
                            '--batch', RANGE], WORKTREE)
    report['calls']['plan_before'] = plan_before
    fixes_before = ((plan_before.get('payload') or {}).get('result') or {}).get(
        'delivery_fixes') or []
    report['fixes_before'] = fixes_before

    imported = gate.run([*cli, 'import', 'audio', '--project', project,
                         '--batch', RANGE, '--roots', str(ARCHIVE.parent),
                         '--voices', VOICES, '--apply'], WORKTREE)
    report['calls']['import_apply'] = imported
    report['import_exit'] = imported['exit_code']
    first_digest = assets_digest(project_dir)
    after = voices_of(project_dir)
    report['voices_after'] = after
    report['import_wrote_a_voice'] = bool(after) and all(after.values()) \
        and after != before

    # Idempotent: a second import must not change the bytes.
    again = gate.run([*cli, 'import', 'audio', '--project', project,
                      '--batch', RANGE, '--roots', str(ARCHIVE.parent),
                      '--voices', VOICES, '--apply'], WORKTREE)
    report['calls']['import_again'] = again
    report['import_is_idempotent'] = assets_digest(project_dir) == first_digest

    plan_after = gate.run([*cli, 'batch', 'plan', '--project', project,
                           '--batch', RANGE], WORKTREE)
    report['calls']['plan_after'] = plan_after
    payload = gate.last_json(plan_after) or {}
    document = payload.get('result') or {}
    delivery_after = document.get('delivery') or (document.get('plan') or {}).get(
        'delivery') or {}
    report['delivery_after'] = {'ready': delivery_after.get('ready'),
                                'problems': delivery_after.get('problems'),
                                'fixes': delivery_after.get('fixes'),
                                'top_ready': payload.get('ready'),
                                'top_fixes': payload.get('delivery_fixes')}
    # The blocker the import was supposed to clear is gone: no fix still points at
    # the voice import.  Readiness is taken from whichever of the two documented
    # places states it, so a shape change cannot silently turn this into a pass.
    fixes_after = (delivery_after.get('fixes') or payload.get('delivery_fixes') or [])
    still_voice = [line for line in fixes_after
                   if 'import' in line or '音色' in line or 'voice' in line.lower()]
    report['voice_fix_cleared'] = not still_voice
    ready_after = delivery_after.get('ready')
    if ready_after is None:
        ready_after = payload.get('ready')
    report['plan_ready_after'] = ready_after

    submitted = gate.run([*cli, 'batch', 'submit', '--project', project,
                          '--batch', RANGE, '--key', 'gate-import-%s' % today,
                          '--codec', 'h265'], WORKTREE)
    report['calls']['submit'] = submitted
    report['submit_after_import'] = submitted['exit_code'] == 0

    report['ok'] = (report['import_exit'] == 0 and report['import_wrote_a_voice']
                    and report['import_is_idempotent']
                    and report['voice_fix_cleared']
                    and report['plan_ready_after'] is True
                    and report['submit_after_import'])
    target = evidence / ('gate-import-%s.json' % today)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    gate.write_stamp(evidence, 'import', WORKTREE, [RANGE, str(gate.WORDLIST)],
                     note={'evidence': str(target), 'voices': after,
                           'fixes_before': fixes_before,
                           'warning': warning})
    assert report['import_exit'] == 0, imported
    assert report['import_wrote_a_voice'], after
    assert report['import_is_idempotent'], '第二次导入改了字节'
    assert report['voice_fix_cleared'], still_voice
    assert report['plan_ready_after'] is True, report['delivery_after']
    assert report['submit_after_import'], submitted
