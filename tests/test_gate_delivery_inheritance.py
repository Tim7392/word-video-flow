"""Gate step: the headless exporter honours the project's delivery settings.

B's fix (``dca48a3``) made ``python -m word_video.exporters`` read the project's
own ``delivery.json`` when no ``--background`` is given, and refuse with the
one-JSON-object contract when the project states nothing.  Both halves are locked
here, through a **real subprocess** rather than an in-process call, because the
contract under test is the process contract: exit code, one JSON object on stdout,
nothing written on refusal.

Two projects, one tiny background (320x180, a few KB - the gate must not drag the
331 MB delivery background in):

  * **with** ``delivery.json`` (background + ``video_codec: h265``), no
    ``--background``: exit 0, and the draft's
    ``editable-draft/word_video_manifest.json`` names exactly the file the project
    stated (compared with the file, not inferred from the exit code);
  * **without** ``delivery.json``, draft still requested (``--no-video`` only - the
    ``--no-video --no-draft`` path needs no picture and would misjudge the refusal):
    exit 2, exactly one JSON object, ``code=BACKGROUND_MISSING``,
    ``object_path=profile.background``, non-empty ``fixes``, **no output directory
    created**, the working directory unchanged, and no ``PermissionError``
    anywhere in the output.

Each run's ``stage_seconds`` is recorded in the evidence: the wall clock alone
cannot say where a slow export spent its time.

Evidence: ``out/reports/gate-delivery-<date>.json``.
"""
import json
import os
from pathlib import Path
import subprocess
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
ARCHIVE = gate.GOOD_BATCH
CLI = [sys.executable, '-m', 'word_video.exporters']


def run_exporter(args, cwd):
    """Run the exporter with the source tree importable and decode its output here.

    The bytes are decoded in Python on purpose: a shell redirection on Windows may
    write UTF-16, and a report about "one JSON object on stdout" cannot be built on
    a re-encoded capture.
    """
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(WORKTREE)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    started = time.monotonic()
    result = subprocess.run([str(part) for part in [*CLI, *args]], cwd=str(cwd),
                            capture_output=True, env=environment)
    return {'exit_code': result.returncode,
            'seconds': round(time.monotonic() - started, 1),
            'stdout': result.stdout.decode('utf-8-sig', 'replace'),
            'stderr': result.stderr.decode('utf-8', 'replace')}


def tiny_background(path):
    from word_video.media import executable
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([executable('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi',
                    '-i', 'color=c=navy:size=320x180:rate=30:duration=1.5',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                   check=True, capture_output=True)
    return path


def build(root, project, background, no_delivery=False):
    argv = ['--root', str(root), '--wordlist', str(gate.WORDLIST),
            '--archive', str(ARCHIVE), '--range', RANGE, '--project', project,
            '--background', str(background)]
    if no_delivery:
        argv.append('--no-delivery')
    assert make_real_project.main(argv) == 0, '真工程没有建起来：%s' % project
    return Path(root) / 'projects' / project


def one_json(text):
    """The single JSON object the process printed, or a reason it is not one."""
    stripped = text.strip()
    if not stripped:
        return None, 'stdout 是空的'
    try:
        return json.loads(stripped), None
    except ValueError as error:
        return None, 'stdout 不是一个 JSON 对象：%s' % error


def test_the_exporter_inherits_delivery_and_refuses_without_it():
    evidence, warning = gate.evidence_dir()
    today = time.strftime('%Y%m%d')
    root = gate.WORK_ROOT / 'runtime' / 'tmp' / 'QA' / 'gate-delivery-root'
    if root.exists():
        import shutil
        shutil.rmtree(root)
    if not (Path(ARCHIVE) / 'timeline.json').exists():
        pytest.fail('对照归档不可用：%s' % ARCHIVE)

    background = tiny_background(root / 'bg-small.mp4')
    assert background.stat().st_size < 1 << 20, '门禁用的小背景不该超过 1MB'

    # (1) The project states its delivery: no --background, no --codec.
    with_delivery = build(root, 'with-delivery', background)
    stated = json.loads((with_delivery / 'delivery.json').read_text(encoding='utf-8'))
    out = root / 'out-with'
    run = run_exporter(['--project', str(with_delivery), '--out', str(out),
                        '--run', 'r1'], root)
    inherited, why = one_json(run['stdout'])
    manifest_path = out / 'r1' / 'editable-draft' / 'word_video_manifest.json'
    manifest = (json.loads(manifest_path.read_text(encoding='utf-8-sig'))
                if manifest_path.exists() else {})
    copied = [item for item in
              (out / 'r1' / 'editable-draft' / 'Resources').glob('*.mp4')
              ] if (out / 'r1' / 'editable-draft' / 'Resources').exists() else []
    report = {'background_stated': stated.get('background'),
              'background_in_manifest': manifest.get('background'),
              'codec_in_manifest': manifest.get('video_codec'),
              'exit_code': run['exit_code'], 'seconds': run['seconds'],
              'one_json': why is None,
              'stage_seconds': (((inherited or {}).get('result') or {})
                                .get('report') or {}).get('stage_seconds'),
              'copied_background': [{'name': item.name,
                                     'bytes': item.stat().st_size}
                                    for item in copied],
              'source_bytes': background.stat().st_size,
              'stderr_tail': run['stderr'][-300:]}
    assert run['exit_code'] == 0, report
    assert why is None, report
    assert manifest_path.exists(), report
    assert manifest.get('background') == stated.get('background'), report
    assert manifest.get('video_codec') == stated.get('video_codec') == 'h265', report
    assert report['copied_background'], '草稿没有带上背景文件'
    assert report['copied_background'][0]['bytes'] == report['source_bytes'], report
    assert report['stage_seconds'], 'export.json 应带阶段秒数'

    # (2) The project states nothing: refusal, and nothing written.
    no_delivery = build(root, 'no-delivery', background, no_delivery=True)
    out_refused = root / 'out-refused'
    out_refused.mkdir()
    cwd = root / 'cwd'
    cwd.mkdir()
    (cwd / 'keep.txt').write_text('untouched', encoding='utf-8')
    before = sorted(item.name for item in cwd.iterdir())
    refused = run_exporter(['--project', str(no_delivery), '--out', str(out_refused),
                            '--run', 'r1', '--no-video'], cwd)
    payload, why_refused = one_json(refused['stdout'])
    error = ((payload or {}).get('error') or {})
    after = sorted(item.name for item in cwd.iterdir())
    report['refusal'] = {
        'exit_code': refused['exit_code'], 'one_json': why_refused is None,
        'json_problem': why_refused, 'code': error.get('code'),
        'object_path': error.get('object_path'), 'type': error.get('type'),
        'fixes': error.get('fixes'), 'out_entries': sorted(
            item.name for item in out_refused.iterdir()),
        'cwd_before': before, 'cwd_after': after,
        'permission_error': 'PermissionError' in refused['stdout']
        or 'PermissionError' in refused['stderr'],
        'stdout_bytes': len(refused['stdout'].encode('utf-8')),
        'stderr_tail': refused['stderr'][-200:]}
    assert refused['exit_code'] == 2, report['refusal']
    assert why_refused is None, report['refusal']
    assert error.get('code') == 'BACKGROUND_MISSING', report['refusal']
    assert error.get('object_path') == 'profile.background', report['refusal']
    assert error.get('fixes'), report['refusal']
    assert report['refusal']['out_entries'] == [], report['refusal']
    assert before == after, report['refusal']
    assert report['refusal']['permission_error'] is False, report['refusal']

    report['ok'] = True
    report['warning'] = warning
    target = evidence / ('gate-delivery-%s.json' % today)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding='utf-8')
    gate.write_stamp(evidence, 'delivery', WORKTREE, [RANGE, ARCHIVE.name],
                     note={'evidence': str(target),
                           'stage_seconds': report['stage_seconds'],
                           'warning': warning})
