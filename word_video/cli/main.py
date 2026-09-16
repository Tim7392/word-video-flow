"""Agent-facing JSON CLI: one action, exactly one JSON object on stdout.

The contract W05 promises an Agent, implemented here:

* stdout carries **one** JSON object for every outcome — success, bad arguments,
  unknown action, or a failure deep in a run — and the exit code says which
  (0 ok, 2 needs input / bad usage, 1 failed).  Logs and tracebacks go to stderr;
  ``watch`` is the one action that streams NDJSON instead.
* nothing opens a dialog: information that is missing comes back as
  ``NEEDS_INPUT`` with a ``fixes`` list the caller can act on.
* ``batch plan`` preflights (solve, estimate, list problems) and writes nothing;
  ``batch submit`` freezes the plan and needs an idempotency key, so a retry can
  never spend twice.
* writes go through the coordinator (one writer per root), never straight to disk.

Run it as ``python -m word_video.cli <action> ...``.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from ..application.batches import (BatchSelection, ExportProfile, plan_batch,
                                   submission_for)
from ..application.intro import measure_project_intro
from ..domain.errors import ProjectError
from ..storage.assets import AssetIndex
from ..storage.coordinator import (Coordinator, NeedsInput, RECOVERY_DIR, connect)
from ..storage.project_store import load_project, save_project

SCHEMA = 'wv-cli@1'
ACTIONS = ('capabilities', 'doctor', 'project', 'batch', 'job', 'artifacts',
           'receipts', 'reconcile', 'watch')


class BadRequest(ProjectError):
    """Bad usage or arguments: still one JSON object, still exit code 2."""

    code = 'BAD_REQUEST'


class _Parser(argparse.ArgumentParser):
    """Argparse's own errors must leave one JSON object on stdout."""

    def error(self, message):
        raise BadRequest(message, path='cli',
                         hint='python -m word_video.cli capabilities 列出可用动作')


def default_root():
    return Path(os.environ.get('WORD_VIDEO_ROOT')
                or (Path.home() / 'word-video')).resolve()


def build_parser():
    parser = _Parser(prog='word_video.cli', description=__doc__)
    parser.add_argument('--root', default=str(default_root()),
                        help='work root (projects, runs, coordinator.sqlite3)')
    parser.add_argument('--db', default='', help='coordinator database (default: root)')
    commands = parser.add_subparsers(dest='action', required=True)

    commands.add_parser('capabilities')
    commands.add_parser('doctor')

    project = commands.add_parser('project')
    project.add_argument('sub', choices=('show', 'save', 'list'))
    project.add_argument('--project', default='', help='project folder or project id')
    project.add_argument('--file', default='', help='project.json to save (project save)')

    batch = commands.add_parser('batch')
    batch.add_argument('sub', choices=('plan', 'submit'))
    batch.add_argument('--project', required=True, help='project id under the root')
    batch.add_argument('--batch', default='', help='word-list range, e.g. 151-153')
    batch.add_argument('--records', default='', help='comma separated record ids')
    batch.add_argument('--background', default='', help='delivery background file')
    batch.add_argument('--codec', default='h264', choices=('h264', 'h265'))
    batch.add_argument('--slices', type=int, default=None)
    batch.add_argument('--key', default='', help='idempotency key (submit)')
    batch.add_argument('--folder', default='', help='project folder holding the assets')

    job = commands.add_parser('job')
    job.add_argument('sub', choices=('status', 'cancel', 'resume', 'run'))
    job.add_argument('--job', required=True)

    artifacts = commands.add_parser('artifacts')
    artifacts.add_argument('--job', required=True)

    commands.add_parser('receipts')
    commands.add_parser('reconcile')

    watch = commands.add_parser('watch')
    watch.add_argument('--job', required=True)
    watch.add_argument('--until-seconds', type=float, default=30.0)
    watch.add_argument('--interval', type=float, default=0.2)
    return parser


def _coordinator(args):
    return Coordinator(args.root, db=args.db or None)


def _selection(args):
    if args.records:
        return BatchSelection(record_ids=tuple(part.strip() for part in
                                               args.records.split(',') if part.strip()))
    if args.batch:
        first, _, last = args.batch.partition('-')
        try:
            return BatchSelection(first=int(first), last=int(last or first))
        except ValueError:
            raise BadRequest('--batch must look like 151-153', path='batch') from None
    raise NeedsInput('a batch needs --batch or --records', path='batch',
                     fixes=['用 --batch 151-153 按词表序号，或 --records w1,w2 按词条 id',
                            'python -m word_video.cli project show --project <id> 查看词条'])


def _prepare(args, coordinator):
    """Load the project, its registry and its intro measurement (the IO edge)."""
    project = coordinator.load_project(args.project)
    index = coordinator.asset_index(args.project)
    media = index.media_map() if index is not None else {}
    if index is None:
        raise NeedsInput('project %r has no assets.json' % args.project, path='assets',
                         fixes=['用资产登记写入 %s（AssetIndex.save 或编辑器保存）'
                                % args.project])
    missing = [asset_id for asset_id in _referenced(project) if asset_id not in media]
    if missing:
        raise NeedsInput('%d asset(s) have no measurement' % len(missing),
                         path='assets',
                         fixes=['先准备并登记这些素材的 units：%s' % ', '.join(missing[:5]),
                                '准备好配音后再提交（这一步可能需要 TTS 权限）'])
    intro = measure_project_intro(project) if project.intro_clip() else None
    return project, index, media, intro


def _referenced(project):
    ids = []
    for clip in project.clips:
        for asset_id in ((clip.source.asset_id if clip.source else ''), clip.audio_asset):
            if asset_id and asset_id not in ids:
                ids.append(asset_id)
    return ids


def _plan(args, coordinator):
    project, index, media, intro = _prepare(args, coordinator)
    profile = ExportProfile(background=args.background, video_codec=args.codec,
                            slices=args.slices)
    plan = plan_batch(project, media, _selection(args), profile, intro)
    return project, index, plan


def _inputs(project, index, plan, folder):
    """Every file a submission depends on, so a later change is a conflict."""
    paths = [str(Path(folder) / 'project.json'), str(Path(folder) / 'assets.json')]
    if plan.profile.background:
        paths.append(plan.profile.background)
    for asset_id in plan.assets:
        if index is not None and index.has(asset_id):
            paths.append(index.candidate_path(asset_id))
    return tuple(str(path) for path in paths)


def action_capabilities(args, coordinator):
    from ..domain.model import SCHEMA as PROJECT_SCHEMA
    from ..storage.assets import SCHEMA as ASSETS_SCHEMA
    from ..storage.coordinator import SCHEMA as COORDINATOR_SCHEMA
    return {'schema': SCHEMA, 'actions': list(ACTIONS),
            'schemas': {'project': PROJECT_SCHEMA, 'assets': ASSETS_SCHEMA,
                        'coordinator': COORDINATOR_SCHEMA},
            'root': str(coordinator.root),
            'database': str(coordinator.db),
            'notes': ['stdout is one JSON object; watch streams NDJSON',
                      'batch plan writes nothing; batch submit needs --key',
                      'job run executes in the foreground; cancel is honoured at '
                      'checkpoints (between steps)']}


def action_doctor(args, coordinator):
    from ..media import executable
    from ..template import font_report
    report = {'root': str(coordinator.root), 'root_writable': False,
              'ffmpeg': None, 'ffprobe': None, 'font_substitutions': [],
              'font_missing': [], 'tts_key_configured': False}
    root = Path(coordinator.root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / '.doctor'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        report['root_writable'] = True
    except OSError as error:
        report['root_error'] = str(error)
    for name in ('ffmpeg', 'ffprobe'):
        try:
            report[name] = executable(name)
        except FileNotFoundError:
            report[name] = None
    fonts = font_report()
    report['font_substitutions'] = sorted(role for role, item in fonts.items()
                                          if item['source'] == 'substitute')
    report['font_missing'] = sorted(role for role, item in fonts.items()
                                    if item['source'] == 'missing')
    report['tts_key_configured'] = bool(os.environ.get('MODEL_SPEECH_API_KEY')
                                        or os.environ.get('VOLC_TTS_API_KEY'))
    return report


def action_project(args, coordinator):
    if args.sub == 'list':
        return {'projects': coordinator.project_index()}
    if args.sub == 'show':
        if not args.project:
            raise NeedsInput('project show needs --project', path='project',
                             fixes=['--project <project id>（工程在 <root>/projects/<id>）'])
        project = coordinator.load_project(args.project)
        return {'project_id': project.project_id, 'schema': project.schema,
                'revision': project.revision, 'stored_revision':
                    coordinator.project_revision(project.project_id),
                'records': [{'id': record.id, 'index': record.index, 'word': record.word}
                            for record in project.records],
                'clips': len(project.clips),
                'styles': project.style_table(),
                'intro': bool(project.intro_clip())}
    if not args.file:
        raise NeedsInput('project save needs --file', path='project',
                         fixes=['--file <project.json> 指向要写入的工程文档'])
    document = json.loads(Path(args.file).read_text(encoding='utf-8'))
    from ..domain.model import Project
    project = Project.from_dict(document)
    expected = document.get('revision')
    path = coordinator.save_project(project, expected_revision=expected)
    return {'saved': str(path), 'project_id': project.project_id,
            'revision': project.revision,
            'stored_revision': coordinator.project_revision(project.project_id)}


def action_batch(args, coordinator):
    project, index, plan = _plan(args, coordinator)
    if args.sub == 'plan':
        return {'plan': plan.to_dict(), 'wrote_files': False}
    if not args.key:
        raise NeedsInput('submit needs an idempotency key', path='submit',
                         fixes=['加 --key <stable-name>；同一批活重试时复用同一个键'])
    if not plan.ok:
        raise NeedsInput('the plan has %d problem(s)' % len(plan.problems),
                         path='plan',
                         fixes=[problem.message for problem in plan.problems] or
                               ['修好工程或素材后重新 plan'])
    folder = args.folder or coordinator.project_folder(args.project)
    submission = submission_for(plan, _inputs(project, index, plan, folder))
    receipt = coordinator.submit(submission, args.key)
    return {'job': receipt['job'], 'created': receipt['created'], 'key': receipt['key'],
            'digest': receipt['digest'], 'plan_identity': plan.plan_identity}


def action_job(args, coordinator):
    job_id = args.job
    if args.sub == 'status':
        job = coordinator.status(job_id)
        return {'job': job['id'], 'state': job['state'], 'phase': job['phase'],
                'control': job['control'], 'error': job['error'],
                'artifacts': len(job['artifacts'])}
    if args.sub == 'cancel':
        job = coordinator.cancel(job_id)
        return {'job': job['id'], 'state': job['state'], 'control': job['control']}
    if args.sub == 'resume':
        job = coordinator.resume(job_id)
        return {'job': job['id'], 'state': job['state'], 'control': job['control']}
    job = coordinator.run(job_id)
    return {'job': job['id'], 'state': job['state'], 'phase': job['phase'],
            'artifacts': len(job['artifacts']), 'error': job['error']}


def action_artifacts(args, coordinator):
    return coordinator.artifacts(args.job)


def action_receipts(args, coordinator):
    return {'receipts': coordinator.receipts()}


def action_reconcile(args, coordinator):
    return coordinator.reconcile()


def action_watch(args, coordinator, stream):
    """Stream one NDJSON line per state change until the task settles."""
    deadline = time.monotonic() + args.until_seconds
    previous = None
    while True:
        job = coordinator.status(args.job)
        snapshot = (job['state'], job['phase'], job['control'], len(job['artifacts']))
        if snapshot != previous:
            previous = snapshot
            stream.write(json.dumps({'job': job['id'], 'state': job['state'],
                                     'phase': job['phase'], 'control': job['control'],
                                     'artifacts': len(job['artifacts'])},
                                    ensure_ascii=False) + '\n')
            stream.flush()
        if job['state'] in ('succeeded', 'partial_failed', 'failed', 'cancelled'):
            return {'job': job['id'], 'state': job['state'], 'final': True}
        if time.monotonic() >= deadline:
            return {'job': args.job, 'state': job['state'], 'final': False,
                    'note': 'watch window ended; the task is still %s' % job['state']}
        time.sleep(max(0.05, args.interval))


HANDLERS = {'capabilities': action_capabilities, 'doctor': action_doctor,
            'project': action_project, 'batch': action_batch, 'job': action_job,
            'artifacts': action_artifacts, 'receipts': action_receipts,
            'reconcile': action_reconcile}


def main(argv=None, *, stdout=None, stderr=None):
    """Run one action; print exactly one JSON object and return an exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        coordinator = _coordinator(args)
        if args.action == 'watch':
            result = action_watch(args, coordinator, stdout)
        else:
            result = HANDLERS[args.action](args, coordinator)
    except NeedsInput as error:
        _print_error(stdout, stderr, getattr(args, 'action', 'cli'), error, extra=True)
        return 2
    except BadRequest as error:
        _print_error(stdout, stderr, 'cli', error)
        return 2
    except ProjectError as error:
        _print_error(stdout, stderr, getattr(args, 'action', 'cli'), error)
        return 1
    except Exception as error:                    # noqa: BLE001 - one JSON, always
        print('%s: %s' % (type(error).__name__, error), file=stderr)
        stdout.write(json.dumps({'ok': False, 'error': {
            'type': type(error).__name__, 'code': 'INTERNAL',
            'message': str(error)[:1000]}}, ensure_ascii=False) + '\n')
        return 1
    if args.action != 'watch':
        stdout.write(json.dumps({'ok': True, 'action': args.action, 'result': result},
                                ensure_ascii=False, allow_nan=False) + '\n')
    return 0


def _print_error(stdout, stderr, action, error, *, extra=False):
    document = {'type': type(error).__name__, **error.to_dict()}
    print('%s: %s' % (error.code, error), file=stderr)
    stdout.write(json.dumps({'ok': False, 'action': action, 'error': document},
                            ensure_ascii=False) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
