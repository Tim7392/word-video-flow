"""QA-7: what the published directory says, judged without the producer's script.

Four commands through A's coordinator (``word_video.cli``), then QA's own reading
of the result:

  1. ``batch plan`` without ``--background``: the delivery must come back
     ``ready: false`` with the background named as the missing input;
  2. ``batch submit`` without it: ``NEEDS_INPUT`` (exit 2) **and no receipt**, so
     a refusal cannot leave a claim that something was produced;
  3. ``batch submit`` with it and ``job run``: the run publishes;
  4. every path in ``complete.json``, ``timeline.json`` and
     ``editable-draft/draft_content.json`` must exist, must sit under the run
     directory, and must not mention the staging folder the run worked in - that
     is exactly the defect the coordinator fix (deede9f) addressed.

Expectations come from the run's own published structure and from the rule "a
published document may only point at published files"; the acceptance verdict
itself is produced by ``accept_range.py`` separately.

Usage:
  python publish_check.py --worktree DIR --root DIR --project p1 --batch 151-153
                          --background FILE [--codec h265] [--json OUT]
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

#: A path inside the run's staging area would mean the document points somewhere
#: that stops existing once publishing finishes.
STAGING_MARKERS = ('.staging', '.building-', 'staging', '.tmp-', 'temp-')

#: Which documents may name a file outside the run directory.
#:
#: ``complete.json`` is the run's own manifest and the draft has to be portable on
#: its own, so both must stay inside the run.  ``timeline.json`` legitimately names
#: the delivery's *inputs* - the background handed to this run and the installed
#: fonts - which are not copied into the run folder; requiring them inside would
#: be a different (and wrong) rule, so they are reported separately instead.
MAY_REFERENCE_INPUTS = ('timeline.json',)

#: Fields in the three documents that carry paths, by document.
PATH_FIELDS = {
    'complete.json': ('files',),
    'timeline.json': ('background', 'intro_video', 'intro_audio'),
    'editable-draft/draft_content.json': ('materials',),
}


def run(command, cwd):
    started = time.monotonic()
    result = subprocess.run([str(part) for part in command], cwd=str(cwd),
                            capture_output=True)
    stdout = result.stdout.decode('utf-8', 'replace')
    payload = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    return {'args': [str(part) for part in command], 'exit_code': result.returncode,
            'seconds': round(time.monotonic() - started, 1), 'payload': payload,
            'stdout_tail': stdout[-1500:],
            'stderr_tail': result.stderr.decode('utf-8', 'replace')[-800:]}


def walk_paths(document, where=''):
    """Every string value that looks like a filesystem path, with its location."""
    found = []
    if isinstance(document, dict):
        for key, value in document.items():
            found += walk_paths(value, '%s.%s' % (where, key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found += walk_paths(value, '%s[%d]' % (where, index))
    elif isinstance(document, str):
        text = document
        if len(text) > 2 and (':\\' in text or ':/' in text or text[:2] == '\\\\'):
            found.append((where, text))
    return found


def draft_paths(document):
    """Only the fields of a draft that really carry a path.

    A draft stores its text as a JSON *string* inside ``materials.texts[].content``,
    and that string contains ``D:\\...`` because the fonts live in the draft too.
    Sniffing strings for a drive letter therefore "finds" a path where there is a
    document - which is why the fields are read explicitly here.
    """
    found = []
    materials = document.get('materials') or {}
    for kind in ('videos', 'audios'):
        for index, item in enumerate(materials.get(kind) or []):
            if isinstance(item, dict) and item.get('path'):
                found.append(('.materials.%s[%d].path' % (kind, index), item['path']))
    for index, item in enumerate(materials.get('texts') or []):
        content = item.get('content') if isinstance(item, dict) else None
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except ValueError:
            continue
        for style_index, style in enumerate(parsed.get('styles') or []):
            font = style.get('font') if isinstance(style, dict) else None
            if isinstance(font, dict) and font.get('path'):
                found.append(('.materials.texts[%d].content.styles[%d].font.path'
                              % (index, style_index), font['path']))
    return found


def check_document(path, run_dir):
    """Paths exist, stay inside the run folder, and never name the staging area."""
    if not path.exists():
        return {'document': str(path), 'present': False, 'problems': ['文档不存在']}
    try:
        document = json.loads(path.read_bytes().decode('utf-8'))
    except Exception as error:
        return {'document': str(path), 'present': True,
                'problems': ['不可解析：%r' % error], 'ok': False}
    if path.name == 'draft_content.json':
        paths = draft_paths(document)
    else:
        paths = walk_paths(document)
    missing, outside, staging = [], [], []
    for where, value in paths:
        target = Path(value)
        if not target.exists():
            missing.append({'where': where, 'path': value})
        try:
            target.resolve().relative_to(Path(run_dir).resolve())
        except ValueError:
            outside.append({'where': where, 'path': value})
        lowered = str(value).lower()
        if any(marker in lowered for marker in STAGING_MARKERS):
            staging.append({'where': where, 'path': value})
    problems = []
    if missing:
        problems.append('%d 个路径不存在' % len(missing))
    if staging:
        problems.append('%d 个路径含 staging 痕迹' % len(staging))
    inputs_allowed = path.name in MAY_REFERENCE_INPUTS
    if outside and not inputs_allowed:
        problems.append('%d 个路径不在运行目录内' % len(outside))
    return {'document': str(path), 'present': True, 'paths': len(paths),
            'missing': missing[:5], 'staging': staging[:5],
            'outside_run': outside[:5],
            'outside_is_inputs': bool(outside) and inputs_allowed,
            'problems': problems,
            'ok': not missing and not staging and (inputs_allowed or not outside)}


def delivery_from_project(project_file):
    """The background the project's own ``delivery.json`` states, or None."""
    path = Path(project_file)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return None
    value = document.get('background') or ''
    return str(Path(value).resolve()) if value else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--worktree', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--project', default='p1')
    parser.add_argument('--batch', default='151-153')
    parser.add_argument('--background', required=True)
    parser.add_argument('--codec', default='h265')
    parser.add_argument('--refusal-key', default='qa7-refusal-1')
    parser.add_argument('--key', default='qa7-published-1')
    parser.add_argument('--project-file', dest='project_file', default=None,
                        help='该工程的 delivery.json，用来核对“继承”是否真的发生')
    parser.add_argument('--refusal-project', dest='refusal_project', default=None,
                        help='没有交付设置的工程（判拒绝面必须用它）')
    parser.add_argument('--refusal-project-file', dest='refusal_project_file',
                        default=None)
    parser.add_argument('--range', default=None, help='验收器用的范围，默认同 --batch')
    parser.add_argument('--wordlist', default=None)
    parser.add_argument('--json', default=None)
    args = parser.parse_args(argv)

    worktree = Path(args.worktree).resolve(strict=True)
    root = Path(args.root).resolve()
    python = sys.executable
    cli = [python, '-m', 'word_video.cli', '--root', str(root)]
    evidence, warning = None, None
    report = {'worktree': str(worktree), 'root': str(root),
              'project': args.project, 'batch': args.batch,
              'background': args.background, 'warning': warning, 'calls': {}}

    plan = run([*cli, 'batch', 'plan', '--project', args.project,
                '--batch', args.batch], worktree)
    report['calls']['plan_without_argument'] = plan
    delivery = ((plan['payload'] or {}).get('result') or {}).get('plan', {}).get('delivery') or {}
    # A project that carries delivery settings is ready without --background: the
    # plan inherits them.  Checked by comparing with the project's own file rather
    # than by trusting the flag.
    stated = None
    if args.project_file:
        stated = delivery_from_project(Path(args.project_file))
    inherited_background = delivery.get('background') or ''
    report['plan_without_argument'] = {
        'project': args.project, 'ready': delivery.get('ready'),
        'background': inherited_background,
        'delivery_json_background': stated,
        'matches_delivery_json': (stated is None or inherited_background == stated),
        'problems': delivery.get('problems')}

    # The refusal case has to use a project with *no* delivery settings; a project
    # that carries a background is supposed to be ready.
    refusal_project = args.refusal_project or args.project
    refusal_file = args.refusal_project_file
    plan_refusal = run([*cli, 'batch', 'plan', '--project', refusal_project,
                        '--batch', args.batch], worktree)
    report['calls']['plan_without_background'] = plan_refusal
    refused = ((plan_refusal['payload'] or {}).get('result') or {}).get('plan', {}).get('delivery') or {}
    report['plan_refused_without_background'] = bool(
        refused.get('ready') is False
        and any(item.get('code') == 'BACKGROUND_MISSING'
                for item in refused.get('problems', [])))
    if plan_refusal is not plan:
        report['refusal_project'] = refusal_project
        report['refusal_project_delivery_json'] = (delivery_from_project(Path(refusal_file))
                                                   if refusal_file else 'unknown')

    def receipt_rows():
        payload = run([*cli, 'receipts'], worktree)['payload'] or {}
        result = payload.get('result') or payload
        rows = result.get('receipts') if isinstance(result, dict) else None
        return rows if isinstance(rows, list) else []

    before = receipt_rows()
    submit_without = run([*cli, 'batch', 'submit', '--project', refusal_project,
                          '--batch', args.batch, '--key', args.refusal_key,
                          '--codec', args.codec], worktree)
    report['calls']['submit_without_background'] = submit_without
    after = receipt_rows()
    report['receipts_before_refusal'] = len(before)
    report['receipts_after_refusal'] = len(after)
    report['refusal_is_needs_input'] = bool(
        submit_without['exit_code'] == 2
        and ((submit_without['payload'] or {}).get('error') or {}).get('code')
        == 'NEEDS_INPUT')
    # A refusal must not leave a receipt: that is the record a later reader would
    # take as "this was produced".
    report['refusal_left_no_receipt'] = (
        len(after) == len(before)
        and not any(row.get('idempotency_key') == args.refusal_key for row in after))

    submit = run([*cli, 'batch', 'submit', '--project', args.project,
                  '--batch', args.batch, '--key', args.key,
                  '--codec', args.codec, '--background', args.background], worktree)
    report['calls']['submit'] = submit
    job = ((submit['payload'] or {}).get('result') or {}).get('job')
    report['job'] = job
    if job:
        executed = run([*cli, 'job', 'run', '--job', job], worktree)
        report['calls']['job_run'] = executed
        state = ((executed['payload'] or {}).get('result') or {})
        report['run_state'] = state.get('state')
        report['run_phase'] = state.get('phase')
        report['run_error'] = state.get('error')
        run_dir = root / 'runs' / job
        report['run_dir'] = str(run_dir)
        documents = [run_dir / 'complete.json', run_dir / 'timeline.json',
                     run_dir / 'editable-draft' / 'draft_content.json']
        report['documents'] = [check_document(path, run_dir) for path in documents]
        report['published_ok'] = all(item.get('ok') for item in report['documents'])
    else:
        report['published_ok'] = False
    report['ok'] = (report['plan_refused_without_background']
                    and report['refusal_is_needs_input']
                    and report['refusal_left_no_receipt']
                    and report['published_ok'])
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    summary = {key: report[key] for key in
               ('job', 'run_state', 'run_phase', 'run_dir',
                'plan_refused_without_background', 'refusal_is_needs_input',
                'refusal_left_no_receipt', 'published_ok', 'ok')}
    summary['documents'] = [{'document': Path(d['document']).name,
                             'paths': d.get('paths'), 'problems': d.get('problems')}
                            for d in report.get('documents', [])]
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
