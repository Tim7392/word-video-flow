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
           'receipts', 'reconcile', 'watch', 'import', 'voice', 'template')


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

    importer = commands.add_parser('import')
    importer.add_argument('sub', choices=('audio',))
    importer.add_argument('--roots', default='',
                          help='legacy cache roots (comma separated); audio-cache is found')
    importer.add_argument('--project', default='',
                          help='project id to match the recordings against')
    importer.add_argument('--batch', default='', help='word-list range, e.g. 151-153')
    importer.add_argument('--records', default='', help='comma separated record ids')
    importer.add_argument('--voices', default='',
                          help='pin a voice per role, e.g. female=BV503_streaming')
    importer.add_argument('--asset-dir', default='',
                          help='where to publish (default: the project folder)')
    importer.add_argument('--apply', action='store_true',
                          help='publish the found originals (default: plan only)')

    voices = commands.add_parser('voice')
    voices.add_argument('sub', choices=('list', 'import'))
    voices.add_argument('--drafts', default='',
                        help='Jianying draft roots (comma separated)')
    voices.add_argument('--listen', default='', help='speaker id to cut a sample for')
    voices.add_argument('--out', default='', help='where the sample goes')
    voices.add_argument('--seconds', type=float, default=8.0)
    voices.add_argument('--confirm', default='',
                        help='role=speaker pairs, e.g. female=BV503_streaming')
    voices.add_argument('--yes', action='store_true',
                        help="the member's explicit confirmation of --confirm")
    voices.add_argument('--preset', default='', help='preset file to write')

    template = commands.add_parser('template')
    template.add_argument('sub', choices=('list', 'show'))
    template.add_argument('--template', default='', help='template id')
    template.add_argument('--version', type=int, default=0,
                          help='template version (default: the latest stored one)')
    return parser


def _roots(value, name):
    """Split a comma separated list of directories; refuse an empty one."""
    found = [Path(part.strip()) for part in str(value or '').split(',') if part.strip()]
    if not found:
        raise NeedsInput('%s needs at least one directory' % name, path='import',
                         fixes=['--%s <dir>[,<dir>...]' % name])
    return found


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
                      'checkpoints (between steps)',
                      'there is no suspended job: pause is a checkpoint control too,'
                      ' so a render in progress is not held — it stops at the next'
                      ' checkpoint and the task falls back to queued; resume only'
                      ' re-queues an unfinished task and never touches a published run',
                      'voice list/import only reads the member\'s own drafts;'
                      ' importing existing audio is not a new synthesis capability',
                      'template list/show only reads the versioned template files'
                      ' under the root; an existing version is never rewritten']}


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
        # The preflight prints what the batch still needs (delivery included) and
        # writes nothing: an Agent can act on `delivery.fixes` before submitting.
        return {'plan': plan.to_dict(), 'wrote_files': False,
                'ready': plan.ready, 'delivery_fixes': list(plan.delivery.fixes)}
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
    # An incomplete delivery is refused by the coordinator itself (submit and run use
    # the one rule), so a missing background comes back as NEEDS_INPUT with fixes.
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


def action_import(args, coordinator):
    """``import audio``: find the recordings a member already has, and publish them.

    Two steps, deliberately separable: the scan and the plan only read, and
    ``--apply`` publishes the *existing* originals into the project's asset folder
    and hands back a ``provider.kind=local`` block.  Nothing on this path can
    synthesise: the importers are read-only or they publish files that already
    exist, which is what makes "导入已有音频 ≠ 获得新文字合成能力" checkable.
    """
    from ..importers import ImportError_, plan_import, scan_roots
    from ..importers.store import materialize

    roots = _roots(args.roots, 'roots')
    try:
        scan = scan_roots(roots)
    except ImportError_ as error:
        raise BadRequest(str(error), path='import',
                         hint='--roots 指向包含 audio-cache 的目录') from None
    report = {'scan': scan.to_dict()}
    if not args.project:
        return {**report, 'ready': False, 'applied': False,
                'note': '只扫描，未匹配工程；加 --project 与 --batch/--records 再导入'}
    project = coordinator.load_project(args.project)
    selection = _selection(args)
    records = [record for record in _select(project, selection)]
    voice_by_role = {}
    for pair in [part for part in args.voices.split(',') if part.strip()]:
        role, _, voice = pair.partition('=')
        voice_by_role[role.strip()] = voice.strip()
    plan = plan_import(records, scan, voice_by_role=voice_by_role)
    report['plan'] = plan.to_dict()
    if not plan.ok:
        fixes = ['缺 %d 条朗读：%s' % (len(plan.missing),
                                      ', '.join('%s/%s' % (item.get('word', '?'),
                                                           item.get('role', '?'))
                                                for item in list(plan.missing)[:3])),
                 '歧义 %d 条：%s（用 --voices role=voice 指定）'
                 % (len(plan.ambiguous),
                    ', '.join('%s/%s' % (item.role, item.text)
                              for item in list(plan.ambiguous)[:3]))]
        raise NeedsInput('the import is not ready', path='import', fixes=fixes)
    if not args.apply:
        return {**report, 'ready': True, 'applied': False,
                'note': '计划就绪；加 --apply 才会把原件发布到资产目录'}
    asset_dir = Path(args.asset_dir) if args.asset_dir else \
        coordinator.project_folder(args.project)
    result = materialize(plan, asset_dir)
    return {**report, 'ready': True, 'applied': True, **result.to_dict()}


def _select(project, selection):
    from ..application.batches import select_records
    return select_records(project, selection)


def action_voice(args, coordinator):
    """``voice list`` / ``voice import``: what the member's own drafts used.

    ``list`` and ``--listen`` only read the drafts and cut a sample from the
    member's own audio; ``--confirm`` writes a preset and refuses without ``--yes``,
    because reading a catalogue never selects a voice.
    """
    from ..importers import ImportRefused, audition, confirm, scan_voices

    if args.sub == 'list':
        if not args.drafts:
            raise NeedsInput('voice list needs --drafts', path='voice',
                             fixes=['--drafts <剪映草稿目录>[,<目录>...]'])
        report = scan_voices(_roots(args.drafts, 'drafts'))
        return {'voices': report.to_dict(),
                'by_status': {status: [item.speaker for item in items]
                              for status, items in report.by_status().items()}}
    report = scan_voices(_roots(args.drafts, 'drafts'),
                         sample_dir=(coordinator.root / 'auditions'
                                     if args.listen else None))
    result = {'voices': report.to_dict()}
    if args.listen:
        output = Path(args.out) if args.out else \
            coordinator.root / 'auditions' / ('%s.wav' % args.listen)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            sample = audition(report.candidates, args.listen, output,
                              seconds=args.seconds)
        except ImportRefused as error:
            raise NeedsInput(str(error), path='voice',
                             fixes=['这条音色没有可复用的音频；换一条，或用剪映自己导一段']) \
                from None
        # ``audition`` answers with the clip it cut (speaker, path, seconds).
        result['sample'] = str(sample['path'] if isinstance(sample, dict) else sample)
        result['note'] = '试听后用 --confirm role=speaker --yes 记录确认结果'
    if args.confirm:
        if not args.yes:
            raise NeedsInput("voice mapping needs the member's confirmation",
                             path='voice',
                             fixes=['先 --listen 试听，再用 --confirm ... --yes 记录',
                                    '未确认的映射不会被保存'])
        mapping = {}
        for pair in [part for part in args.confirm.split(',') if part.strip()]:
            role, _, speaker = pair.partition('=')
            mapping[role.strip()] = speaker.strip()
        preset = Path(args.preset) if args.preset else \
            coordinator.root / 'voices' / 'confirmed.json'
        preset.parent.mkdir(parents=True, exist_ok=True)
        try:
            written = confirm(report.candidates, mapping, confirmed=True,
                              preset_path=preset)
        except ImportRefused as error:
            raise NeedsInput(str(error), path='voice',
                             fixes=['--confirm 里的音色必须在 --drafts 扫描结果里']) \
                from None
        result['preset'] = str(written if isinstance(written, (str, Path)) else preset)
    return result


def action_template(args, coordinator):
    """``template list`` / ``template show``: the shareable arrangement + styles.

    Reading only: a template version is what a batch was planned against, so the
    CLI never rewrites one.  ``show`` answers with the document a member or an Agent
    needs to compare two versions by hand.
    """
    from ..storage.templates import TemplateStore

    store = TemplateStore(coordinator.root)
    if args.sub == 'list':
        return store.index()
    if not args.template:
        raise NeedsInput('template show needs --template', path='template',
                         fixes=['--template <模板 id>（template list 列出可用模板）'])
    known = store.template_ids()
    if args.template not in known:
        raise NeedsInput('no template %r' % args.template, path='template',
                         fixes=['可用模板：%s' % '、'.join(known),
                                '--root 指向存有 templates/ 的工作根'])
    version = args.version or None
    source = store.source(args.template, version)
    if source == 'missing':
        raise NeedsInput('template %s has no version %d'
                         % (args.template, version), path='template',
                         fixes=['已存版本：%s'
                                % (store.versions(args.template) or ['（无）'])])
    document = store.load(args.template, version)
    result = {'template': document.to_dict(), 'source': source,
              'versions': store.versions(args.template),
              'styles': document.style_table()}
    if source == 'stored':
        result['path'] = str(store.path(document.template_id, document.version))
    return result


HANDLERS = {'capabilities': action_capabilities, 'doctor': action_doctor,
            'project': action_project, 'batch': action_batch, 'job': action_job,
            'artifacts': action_artifacts, 'receipts': action_receipts,
            'reconcile': action_reconcile, 'import': action_import,
            'voice': action_voice, 'template': action_template}


def main(argv=None, *, stdout=None, stderr=None):
    """Run one action; print exactly one JSON object and return an exit code."""
    stdout = stdout or _utf8(sys.stdout)
    stderr = stderr or _utf8(sys.stderr)
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


def _utf8(stream):
    """Make the one JSON object UTF-8 whatever the console code page says.

    A code page that cannot hold a character the answer contains (phonetics, a
    member's own words) would otherwise raise *while writing* — after the action
    succeeded — and the caller would get a traceback instead of the JSON the
    contract promises.  Best effort: a stream that cannot be reconfigured is used
    as it is.
    """
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is not None:
        try:
            reconfigure(encoding='utf-8')
        except (ValueError, OSError):
            pass
    return stream


def _print_error(stdout, stderr, action, error, *, extra=False):
    document = {'type': type(error).__name__, **error.to_dict()}
    print('%s: %s' % (error.code, error), file=stderr)
    stdout.write(json.dumps({'ok': False, 'action': action, 'error': document},
                            ensure_ascii=False) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
