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
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

from ..application.batches import (BatchSelection, ExportProfile, plan_batch,
                                   submission_for)
from ..application.intro import measure_project_intro
from ..domain.errors import ProjectError
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
    batch.add_argument('sub', choices=('list', 'plan', 'submit', 'show', 'redo',
                                       'upgrade'))
    batch.add_argument('--project', default='', help='source project id or folder')
    batch.add_argument('--batch', default='', help='word-list range, e.g. 151-153')
    batch.add_argument('--records', default='', help='comma separated record ids')
    batch.add_argument('--per-package', dest='per_package', type=int, default=0,
                       help='words per package')
    batch.add_argument('--packages', type=int, default=0, help='how many packages')
    batch.add_argument('--template', default='', help='template id to pin')
    batch.add_argument('--template-version', dest='template_version', type=int,
                       default=0, help='template version (default: the latest)')
    batch.add_argument('--background', default='', help='delivery background file')
    batch.add_argument('--codec', default='', choices=('', 'h264', 'h265'))
    batch.add_argument('--slices', type=int, default=None)
    batch.add_argument('--key', default='', help='idempotency key (submit/redo)')
    batch.add_argument('--folder', default='', help='project folder holding the assets')
    batch.add_argument('--id', dest='batch_id', default='', help='batch id (show/redo)')
    batch.add_argument('--apply', action='store_true',
                       help='redo: prepare the failed/changed packages; upgrade: write it')
    batch.add_argument('--run', action='store_true', help='redo: also run them')
    batch.add_argument('--replace-edits', dest='replace_edits', action='store_true',
                       help='rebuild an instance even though it has member edits')
    batch.add_argument('--take-template', dest='take_template', action='store_true',
                       help='upgrade: the template wins where a member edit conflicts')
    batch.add_argument('--undo', action='store_true',
                       help='upgrade: put back the state the last upgrade replaced')

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
    template.add_argument('sub', choices=('list', 'show', 'save', 'export', 'import',
                                          'verify'))
    template.add_argument('--template', default='', help='template id')
    template.add_argument('--version', type=int, default=0,
                          help='template version (default: the latest stored one)')
    template.add_argument('--from-project', dest='from_project', default='',
                          help='template save: capture this project as a new version')
    template.add_argument('--note', default='', help='template save: what changed')
    template.add_argument('--out', default='', help='template export: target folder')
    template.add_argument('--from', dest='source', default='',
                          help='template import/verify: the package folder')
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
    # The intro layer's media is measured by the intro resolver, not registered in the
    # asset store: it is a layer of its own (wv-project@2), so asking the registry for
    # its units would refuse every project that has an intro.
    intro_assets = {clip.source.asset_id for clip in project.clips
                    if clip.is_intro and clip.source is not None}
    missing = [asset_id for asset_id in _referenced(project)
               if asset_id not in media and asset_id not in intro_assets]
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


def _profile(args, folder):
    """The delivery profile: the flag wins, then the editor's own ``delivery.json``.

    A member who set a background in the editor must not have to pass it again, and
    an Agent planning a batch must see the delivery the editor would render — so the
    sidecar is read here (read-only; the editor owns that file).
    """
    from ..storage.delivery import delivery_settings
    settings = delivery_settings(folder)
    return ExportProfile(background=args.background or settings['background'],
                         video_codec=args.codec or settings['video_codec'] or 'h264',
                         slices=args.slices)


def _rule(args):
    from ..application.packages import PackageRule
    return PackageRule(per_package=args.per_package, count=args.packages)


def _template(args, coordinator):
    """The template a batch pins: explicit by id, otherwise the reference one."""
    from ..storage.templates import BUILTIN_TEMPLATE_ID, TemplateStore
    store = TemplateStore(coordinator.root)
    known = store.template_ids()
    template_id = args.template or BUILTIN_TEMPLATE_ID
    if args.template and args.template not in known:
        raise NeedsInput('no template %r' % args.template, path='template',
                         fixes=['可用模板：%s' % '、'.join(known),
                                'python -m word_video.cli template list'])
    if args.template_version and store.source(template_id, args.template_version) \
            == 'missing':
        raise NeedsInput('template %s has no version %d'
                         % (template_id, args.template_version), path='template',
                         fixes=['已存版本：%s' % (store.versions(template_id) or ['（无）'])])
    return store.load(template_id, args.template_version or None)


def _document(args, coordinator):
    """Plan the whole batch (read-only) from the source project and its delivery.

    Returns the source project, its registry, the plan document, and the intro the
    expansion will use — measured **once** here, so a submit that follows does not probe
    the same movie again for every package.
    """
    from ..application.packages import (free_bytes_for, intro_from, plan_document,
                                        reference_bytes_for)
    from ..storage.templates import BUILTIN_TEMPLATE_ID
    project, index, media, intro = _prepare(args, coordinator)
    template = _template(args, coordinator)
    try:
        intro = intro_from(project, template, measured=intro)
    except ProjectError:
        intro = (None, '', None)        # reported per package by the plan itself
    reference, note = reference_bytes_for(coordinator)
    document = plan_document(project, media, index, selection=_selection(args),
                             rule=_rule(args), profile=_profile(args, args.folder or
                                                               coordinator.project_folder(
                                                                   args.project)),
                             template=template, intro=intro,
                             free_bytes=free_bytes_for(coordinator.root),
                             reference_bytes=reference, reference_note=note,
                             source_folder=str(args.folder or
                                               coordinator.project_folder(
                                                   args.project)))
    return project, index, document, intro


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
                      'template list/show/save/export/import/verify only read or add'
                      ' versions: an existing version is never rewritten, and a package'
                      ' carries no path, no credential and no executable',
                      'batch redo changes nothing without --apply, and re-does only the'
                      ' packages that failed or changed; batch upgrade keeps member'
                      ' edits (conflicts are reported) and --undo puts back the state'
                      ' it replaced']}


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


def _plan_view(document, project):
    """The plan answer: the batch document, plus the keys a W05 caller already reads.

    A batch **is** the plan, so the document is the answer — but a caller that only
    ever asks for one selection keeps reading ``records``, ``plan_identity``,
    ``total_ticks`` and ``delivery`` exactly as before, because those are projections
    of the same document and not a second plan.  With one package they are that
    package's own numbers; with several, the delivery verdict is the batch's (one
    profile, one answer) and the rest are sums over the packages.
    """
    view = dict(document.to_dict())
    view.update(
        {'project_id': document.source_project, 'revision': project.revision,
         'records': [dict(word) for word in document.records],
         'assets': _batch_assets(document),
         'problems': [problem.to_dict() for problem in document.problems],
         'plan_identity': document.plan_identity,
         'total_ticks': document.total_ticks,
         'estimated_seconds': round(document.total_ticks / 720000.0, 3),
         'delivery': document.delivery.to_dict()})
    return view


def _batch_assets(document):
    found = []
    for package in document.packages:
        for asset_id in package.usage.assets:
            if asset_id not in found:
                found.append(asset_id)
    return found


def action_batch(args, coordinator):
    """``batch plan|submit|show|redo``: plan a whole batch, then run only what must run.

    ``plan`` and ``show`` only read.  ``submit`` creates one project instance per
    package and freezes one job per package (keys derived from the caller's key, so a
    retry is still the same work), and ``redo`` compares the plan on disk with the
    source as it is now: packages that already succeeded and did not change are
    *reused* — not re-rendered and not re-synthesised — while only the failed or
    changed ones are prepared again.
    """
    if args.sub == 'list':
        from ..storage.batches import BatchStore
        return BatchStore(coordinator.root).index()
    if args.sub == 'show':
        return _batch_show(args, coordinator)
    if args.sub == 'redo':
        return _batch_redo(args, coordinator)
    if args.sub == 'upgrade':
        return _batch_upgrade(args, coordinator)
    project, index, document, intro = _document(args, coordinator)
    if args.sub == 'plan':
        # The preflight prints what the batch still needs (delivery included) and
        # writes nothing: an Agent can act on `delivery.fixes` before submitting.
        return {'batch': document.batch_id, 'plan': _plan_view(document, project),
                'wrote_files': False, 'ready': document.ready,
                'checks': document.checks.to_dict(),
                'fixes': document.fixes(),
                'delivery_fixes': list(document.delivery.fixes)}
    # submit
    if not args.key:
        raise NeedsInput('submit needs an idempotency key', path='submit',
                         fixes=['加 --key <stable-name>；同一批活重试时复用同一个键'])
    if not document.ready:
        raise NeedsInput('the batch is not ready: %d problem(s)'
                         % (len(document.problems) + len(document.checks.blocking)),
                         path='plan', fixes=list(document.fixes()) or
                               ['修好工程或素材后重新 plan'])
    result = _submit_document(args, coordinator, document, index, key=args.key,
                              template=_template(args, coordinator), intro=intro,
                              replace_edits=args.replace_edits)
    # One package is the plain single-batch case, so the answer keeps the fields a
    # caller of W05 already reads (job/created/digest) and adds the batch view.
    single = result[0] if len(result) == 1 else None
    return {'batch': document.batch_id, 'key': args.key, 'ready': True,
            'units': document.usage.units, 'packages': result,
            'job': single['job'] if single else None,
            'created': single['created'] if single else None,
            'digest': single['submission_digest'] if single else None,
            'plan_identity': document.plan_identity}


def _submit_document(args, coordinator, document, index, *, key, template, intro,
                     replace_edits=False, only=None, run=False, existing=()):
    """Create the instance of every package that must run, and freeze its job.

    ``existing`` names packages whose instance must be used **as it is** (a member has
    edited it): the instance is the member's work, so it is neither rebuilt from the
    source nor overwritten — it is submitted again with its own content.
    """
    from ..application.packages import instance_for
    from ..storage.batches import BatchStore
    store = BatchStore(coordinator.root)
    project = coordinator.load_project(args.project)
    intro_slice, intro_audio, intro_measure = intro
    asset_ids = {(clip.record_id, clip.role): clip.source.asset_id
                 for clip in project.clips
                 if clip.source is not None and clip.record_id}
    records = {record.id: record for record in project.records}
    media = index.media_map() if index is not None else {}
    # The plan is on disk before the first job exists, and each package's job is
    # recorded the moment it is frozen: a submit that stops half way (a package whose
    # instance a member edited, say) leaves a batch that says exactly which packages
    # were submitted, so `batch redo` resumes the rest instead of redoing the lot.
    store.save(document)
    submitted = []
    for package in document.packages:
        if only is not None and package.package_id not in only:
            continue
        if package.package_id in existing:
            instance = coordinator.load_project(package.project_id)
        else:
            chunk = tuple(records[record_id] for record_id in package.record_ids
                          if record_id in records)
            instance = instance_for(project, chunk, template, media,
                                    package.project_id,
                                    intro_slice=intro_slice, intro_audio=intro_audio,
                                    intro_measure=intro_measure, asset_ids=asset_ids)
            coordinator.create_instance(instance, index, replace_edits=replace_edits)
        folder = coordinator.project_folder(package.project_id)
        plan = plan_batch(instance, media, None, document.profile, intro_measure)
        inputs = _instance_inputs(folder, document, index, plan)
        submission = submission_for(plan, inputs)
        package_key = _package_key(key, package.package_id, len(document.packages))
        receipt = coordinator.submit(submission, package_key)
        # What a later redo compares against: the package as it was actually
        # submitted (its solved identity and the instance revision it ran with).
        actual = replace(package, plan_identity=plan.plan_identity,
                         instance_revision=coordinator.instance_revision(
                             package.project_id) or 0)
        entry = {'package_id': package.package_id, 'project_id': package.project_id,
                 'job': receipt['job'], 'created': receipt['created'],
                 'key': receipt['key'], 'digest': actual.digest(),
                 'submission_digest': receipt['digest'],
                 'units': package.usage.units,
                 'records': list(package.record_ids)}
        if run:
            entry['state'] = coordinator.run(receipt['job'])['state']
        submitted.append(entry)
        store.save_jobs(document.batch_id, key,
                        {entry['package_id']: {'job': entry['job'],
                                               'digest': entry['digest'],
                                               'key': entry['key']}})
    return submitted


def _package_key(key, package_id, packages=1):
    """The idempotency key of one package's job.

    A batch that is not cut into packages keeps the caller's key **verbatim**, which
    is what a W05 caller already relies on ("the same key returns the same task").  A
    cut batch derives one key per package from it, so retrying the whole batch is
    still the same work, package by package.
    """
    if packages <= 1:
        return str(key)
    return '%s#%s' % (key, package_id)


def _instance_inputs(folder, document, index, plan):
    """Every file this package's job depends on, so a later change is a conflict."""
    paths = [str(Path(folder) / 'project.json'), str(Path(folder) / 'assets.json')]
    if document.profile.background:
        paths.append(document.profile.background)
    for asset_id in plan.assets:
        if index is not None and index.has(asset_id):
            paths.append(index.candidate_path(asset_id))
    return tuple(str(path) for path in paths)


def _batch_show(args, coordinator):
    from ..storage.batches import BatchStore
    if not args.batch_id:
        raise NeedsInput('batch show needs --id', path='batch',
                         fixes=['--id <批次 id>（python -m word_video.cli batch list）'])
    store = BatchStore(coordinator.root)
    if args.batch_id not in store.batch_ids():
        raise NeedsInput('no batch %r' % args.batch_id, path='batch',
                         fixes=['已知批次：%s' % '、'.join(store.batch_ids() or ['（无）'])])
    document = store.load(args.batch_id)
    jobs = store.jobs(args.batch_id)
    packages = []
    for package in document.packages:
        entry = package.to_dict()
        recorded = jobs.get(package.package_id) or {}
        entry['job'] = recorded.get('job')
        if entry['job']:
            job = coordinator.status(entry['job'])
            entry['state'] = job['state']
            entry['published'] = job['state'] == 'succeeded'
            entry['artifacts'] = len(job['artifacts'])
        packages.append(entry)
    return {'batch': document.batch_id, 'plan': document.to_dict(),
            'packages': packages, 'ready': document.ready,
            'units': document.usage.units, 'jobs_recorded': len(jobs)}


def _batch_redo(args, coordinator):
    """What a re-run would touch, and (with ``--apply``) only that.

    The comparison is per package: its digest now against the digest recorded when it
    was submitted.  A package that succeeded and did not change is *reused*: no
    re-render, no re-synthesis, and the published run stays where it is.
    """
    from ..storage.batches import BatchStore
    if not args.batch_id:
        raise NeedsInput('batch redo needs --id', path='batch',
                         fixes=['--id <批次 id>（batch show --id <id> 查看）'])
    store = BatchStore(coordinator.root)
    recorded = store.load(args.batch_id)
    jobs = store.jobs(args.batch_id)
    # Plan the batch again from the same source, selection, rule and delivery: the
    # difference between the two documents *is* the redo scope.  The source folder is
    # recorded with the plan, so a redo does not depend on the caller remembering it.
    args.project = args.project or recorded.source_folder or recorded.source_project
    args.batch, args.records = _selection_flags(recorded)
    args.per_package = recorded.rule.per_package
    args.packages = recorded.rule.count
    args.background = args.background or recorded.profile.background
    args.codec = args.codec or recorded.profile.video_codec
    args.slices = recorded.profile.slices if args.slices is None else args.slices
    args.template = recorded.template_id
    args.template_version = recorded.template_version
    project, index, fresh, intro = _document(args, coordinator)
    media = index.media_map() if index is not None else {}
    scope = []
    for planned in fresh.packages:
        package, edited = planned, coordinator.instance_revision(planned.project_id)
        # An instance a member saved (revision > 0) is the truth for its package: it
        # is planned as it is, never rebuilt from the source behind their back.
        if edited:
            instance = coordinator.load_project(package.project_id)
            plan = plan_batch(instance, media, None, recorded.profile, None)
            package = replace(package, basis='instance', instance_revision=edited,
                              plan_identity=plan.plan_identity,
                              problems=plan.problems, delivery=plan.delivery)
        entry = jobs.get(package.package_id) or {}
        job_id = entry.get('job')
        state = coordinator.status(job_id)['state'] if job_id else None
        changed = bool(job_id) and entry.get('digest') != package.digest()
        if job_id is None:
            action, reason = 'new', '这个包还没有作业'
        elif state == 'succeeded' and not changed:
            action, reason = 'reuse', '已发布且内容未变'
        elif changed:
            action, reason = 'resubmit', ('内容变了（%s）'
                                          % ('成员改过实例' if edited
                                             else '词/时间/交付不同'))
        else:
            action, reason = 'rerun', '内容未变但作业没成功（%s）' % state
        scope.append({'package_id': package.package_id, 'job': job_id, 'state': state,
                      'action': action, 'reason': reason,
                      'basis': package.basis, 'units': package.usage.units,
                      'records': list(package.record_ids),
                      'words': [dict(item) for item in package.words],
                      'digest_now': package.digest(), 'digest_then': entry.get('digest')})
    result = {'batch': fresh.batch_id, 'plan': fresh.to_dict(), 'scope': scope,
              'redo': [item['package_id'] for item in scope
                       if item['action'] in ('new', 'resubmit', 'rerun')],
              'untouched': [item['package_id'] for item in scope
                            if item['action'] == 'reuse'],
              'units': sum(item['units'] for item in scope
                           if item['action'] in ('new', 'resubmit')),
              'applied': False}
    if not args.apply:
        result['note'] = ('只报告重做范围；加 --apply 才建实例/提交，'
                          '再加 --run 立刻执行')
        return result
    key = args.key or ('redo-%s' % fresh.batch_id)
    resubmit = [item['package_id'] for item in scope if item['action'] == 'resubmit']
    new = [item['package_id'] for item in scope if item['action'] == 'new']
    # A resubmit whose instance a member edited is submitted *from that instance*:
    # rebuilding it from the source would throw their edits away, which is the one
    # thing an upgrade or a redo must never do quietly.
    existing = {item['package_id'] for item in scope
                if item['action'] == 'resubmit' and item['basis'] == 'instance'}
    prepared = []
    if resubmit or new:
        prepared = _submit_document(args, coordinator, fresh, index, key=key,
                                    template=_template(args, coordinator), intro=intro,
                                    replace_edits=args.replace_edits,
                                    only=set(resubmit) | set(new),
                                    run=args.run, existing=existing)
    # A package whose content did not change keeps its job: retrying it is "run the
    # same job again", which is exactly what the W05 idempotency already promises.  Two
    # states need a word first, or running would do nothing at all: a cancelled job
    # still carries its cancel bit, and a delivery that no longer verifies
    # (`partial_failed`) refuses to run again until it is re-queued.
    rerun = [item for item in scope if item['action'] == 'rerun']
    for item in rerun:
        if not args.run:
            continue
        if item['state'] == 'cancelled':
            item['state'] = coordinator.resume(item['job'])['state']
        elif item['state'] == 'partial_failed':
            item['state'] = coordinator.requeue(item['job'])['state']
        item['state'] = coordinator.run(item['job'])['state']
    result['applied'] = True
    result['key'] = key
    result['prepared'] = prepared
    result['rerun'] = [{'package_id': item['package_id'], 'job': item['job'],
                        'state': item['state']} for item in rerun]
    result['note'] = '只重做了失败/变化的包；未变且已发布的包没有重渲染、没有重新合成'
    return result


def _selection_flags(document):
    if document.selection.record_ids:
        return '', ','.join(document.selection.record_ids)
    return '%d-%d' % (document.selection.first or 1, document.selection.last or 0), ''


def _batch_upgrade(args, coordinator):
    """Move a batch to a newer template version — or put back what the last one replaced.

    Only this batch is touched: another batch that pinned the same template keeps its
    own version, which is what "升级只影响显式升级的批" means in practice.  A member's
    hand edits survive: the newer version's own changes are applied field by field, a
    value the member changed stays theirs and is reported as a conflict, and the whole
    thing is recorded before it is written so ``--undo`` restores the same bytes.
    """
    from ..application.upgrade import plan_upgrade
    from ..domain.model import Project
    from ..storage.batches import BatchStore
    from ..storage.templates import TemplateStore

    if not args.batch_id:
        raise NeedsInput('batch upgrade needs --id', path='batch',
                         fixes=['--id <批次 id>（python -m word_video.cli batch list）'])
    store = BatchStore(coordinator.root)
    templates = TemplateStore(coordinator.root)
    recorded = store.load(args.batch_id)
    if args.undo:
        return _batch_undo(args, coordinator, store, recorded)

    template_id = args.template or recorded.template_id
    old = templates.load(recorded.template_id, recorded.template_version)
    version = args.template_version or templates.latest(template_id)
    if not version:
        raise NeedsInput('no template %r to upgrade to' % template_id, path='template',
                         fixes=['可用模板：%s' % '、'.join(templates.template_ids()),
                                'python -m word_video.cli template list'])
    new = templates.load(template_id, version)
    packages, documents, blocked = [], {}, []
    for package in recorded.packages:
        revision_now = coordinator.instance_revision(package.project_id)
        if revision_now is None:
            packages.append({'package_id': package.package_id,
                             'project_id': package.project_id, 'missing': True,
                             'note': '这个包还没有工程实例（先 batch submit）'})
            continue
        instance = coordinator.load_project(package.project_id)
        upgraded, merge = plan_upgrade(instance, old, new,
                                       take_template=args.take_template)
        entry = {'package_id': package.package_id, 'project_id': package.project_id,
                 'missing': False, 'revision_before': instance.revision,
                 'revision_after': upgraded.revision,
                 'styles_before': instance.style_table(),
                 'styles_after': upgraded.style_table(),
                 'member_edits': revision_now > 0,
                 'changed': merge.changed, **merge.to_dict()}
        if merge.wiring:
            blocked.append(entry)
        elif merge.changed and merge.appliable:
            documents[package.project_id] = instance.to_dict()
        packages.append(entry)
    report = {'schema': 'wv-upgrade@1', 'batch': recorded.batch_id,
              'template_before': {'template_id': old.template_id, 'version': old.version},
              'template_after': {'template_id': new.template_id, 'version': new.version},
              'take_template': bool(args.take_template),
              'packages': packages, 'applied': False,
              'batch_plan_before': recorded.to_dict()}
    result = {'batch': recorded.batch_id, 'from': '%s@%d' % (old.template_id, old.version),
              'to': '%s@%d' % (new.template_id, new.version), 'packages': packages,
              'conflicts': [dict(item, package_id=entry['package_id'])
                            for entry in packages
                            for item in entry.get('conflicts', ())],
              'kept': [dict(item, package_id=entry['package_id'])
                       for entry in packages for item in entry.get('kept', ())],
              'changed': sorted({entry['package_id'] for entry in packages
                                 if entry.get('changed')}),
              'blocked': [entry['package_id'] for entry in blocked],
              'applied': False}
    if not result['changed']:
        result['note'] = '这个批已经是 %s：没有要改的地方' % result['to']
        return result
    if blocked:
        raise NeedsInput(
            '%s@%d 改的是版式，不是样式：原地升级会重建片段并丢掉成员改过的时间/拆分'
            % (new.template_id, new.version), path='template',
            fixes=['新建一个包（batch submit --template %s --template-version %d）'
                   % (new.template_id, new.version),
                   '受影响的包：%s' % '、'.join(entry['package_id'] for entry in blocked)])
    if not args.apply:
        result['note'] = ('只报告升级范围与冲突；加 --apply 才写入实例'
                          '（写入前会记录可撤销的旧状态）')
        return result
    number = 1 + max(store.upgrades(recorded.batch_id) or [0])
    report['number'] = number
    store.save_upgrade(recorded.batch_id, report, documents, number=number)
    applied = []
    for entry in packages:
        if entry.get('missing') or entry['project_id'] not in documents:
            continue
        instance = Project.from_dict(documents[entry['project_id']])
        upgraded, merge = plan_upgrade(instance, old, new,
                                       take_template=args.take_template)
        # The revision the instance had when it was planned: a member who saved in
        # between gets a stale-revision refusal instead of losing that save.
        coordinator.save_project(upgraded, expected_revision=entry['revision_before'])
        entry['revision_after'] = upgraded.revision
        entry['styles_after'] = upgraded.style_table()
        applied.append(entry['package_id'])
    store.save(replace(recorded, template_id=new.template_id,
                       template_version=new.version))
    report['applied'] = True
    report['packages'] = packages
    store.save_upgrade(recorded.batch_id, report, documents, number=number)
    result['applied'] = True
    result['upgrade'] = number
    result['note'] = ('已按 %s@%d 升级 %d 个包；未升级的批仍钉在它们自己的版本；'
                      '--undo 可整体撤销' % (new.template_id, new.version, len(applied)))
    return result


def _batch_undo(args, coordinator, store, recorded):
    """Put back the instance documents the last upgrade replaced, byte for byte."""
    from ..application.upgrade import describe_undo
    from ..domain.model import Project

    report, documents = store.load_upgrade(recorded.batch_id)
    before = report.get('template_after') or {}
    after = report.get('template_before') or {}
    restored = []
    for project_id, document in sorted(documents.items()):
        current = coordinator.instance_revision(project_id)
        if current is None:
            restored.append({'project_id': project_id, 'restored': False,
                             'note': '实例已不在'})
            continue
        previous = Project.from_dict(document)
        coordinator.save_project(previous, expected_revision=current)
        restored.append({'project_id': project_id,
                         'revision_restored': previous.revision,
                         'revision_replaced': current,
                         **describe_undo(previous,
                                         coordinator.load_project(project_id))})
    store.save(replace(recorded, template_id=after.get('template_id',
                                                       recorded.template_id),
                       template_version=after.get('version', recorded.template_version)))
    return {'batch': recorded.batch_id, 'undo': report.get('number'),
            'from': '%s@%s' % (before.get('template_id'), before.get('version')),
            'to': '%s@%s' % (after.get('template_id'), after.get('version')),
            'packages': restored, 'applied': True,
            'note': '已把升级前的实例文档原样放回，批的计划版本也回到升级前'}


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
    """``template``: the shareable arrangement + styles, as versions and as a package.

    Reading never rewrites: a version is what a batch was planned against, so the CLI
    only ever *adds* a version (``save`` writes the next number; ``import`` refuses one
    that already exists).  ``export``/``import`` move one version between work roots as
    a package — a manifest plus the document — and refuse anything that would make the
    package machine-specific or executable.
    """
    from ..storage.template_package import (import_package, read_package,
                                            write_package)
    from ..storage.templates import TemplateStore

    store = TemplateStore(coordinator.root)
    if args.sub == 'list':
        return store.index()
    if args.sub == 'save':
        return _template_save(args, coordinator, store)
    if args.sub in ('export', 'import', 'verify'):
        if args.sub == 'export':
            return _template_export(args, coordinator, store, write_package)
        if not args.source:
            raise NeedsInput('template %s needs a package folder' % args.sub,
                             path='template',
                             fixes=['--from <目录>（export 用 --out <目录> 指定目标）'])
        document, report = read_package(args.source)
        if args.sub == 'verify':
            return {**report, 'template': document.to_dict(),
                    'styles': document.style_table(),
                    'note': '只扫描，不导入；import 会在这里报出的问题都清掉之后才写入'}
        return {**import_package(args.source, store),
                'note': '版本只增不改：目标根已有同名版本时会被拒绝'}
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


def _template_save(args, coordinator, store):
    """Capture a project as the *next* version of a template.

    The number is computed, never given by accident: writing over a version a batch
    already pinned is refused by the store, and the note is what a later reader has
    instead of a diff.
    """
    from ..application.templates import document_from_project
    from ..storage.templates import BUILTIN_TEMPLATE_ID

    if not args.from_project:
        raise NeedsInput('template save needs --from-project', path='template',
                         fixes=['--from-project <工程 id 或目录>：把它的片头/样式存成模板版本'])
    project = coordinator.load_project(args.from_project)
    template_id = args.template or BUILTIN_TEMPLATE_ID
    version = args.version or (max(store.versions(template_id) or [0]) + 1)
    document = document_from_project(project, template_id=template_id, version=version,
                                     note=args.note)
    path = store.save(document)
    return {'template': document.to_dict(), 'saved': str(path),
            'versions': store.versions(template_id), 'styles': document.style_table(),
            'note': '版本只增不改：已有版本不会被覆盖，升级要写下一个号'}


def _template_export(args, coordinator, store, write_package):
    """Write one version as a package folder; a machine-specific template is refused."""
    if not args.template:
        raise NeedsInput('template export needs --template', path='template',
                         fixes=['--template <模板 id> [--version N] [--out <空目录>]'])
    document = store.load(args.template, args.version or None)
    out = Path(args.out) if args.out else \
        coordinator.root / 'template-packages' / ('%s-v%d' % (document.template_id,
                                                              document.version))
    written = write_package(document, out)
    return {**written, 'styles': document.style_table(),
            'note': '包内只有 package.json 与 template.json：不含路径、凭据与可执行文件'}


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
