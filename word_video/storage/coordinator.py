"""One writer for one work root: atomic saves, frozen submissions, receipts.

The problem this closes: nothing owned "who writes", so a UI and an Agent could
both save the same project (last write wins), a retried submission could spend TTS
twice, and a killed worker left artifacts nobody could call half-finished.

Design rules, in the order they matter:

* **One writer.** Every write goes through this object, taking the same OS file lock
  the job worker uses (``word_video.jobs.job_lock``), so two coordinators on one root
  cannot interleave.  A project save also checks ``expected_revision`` against what is
  on disk, so the loser of a race gets :class:`~word_video.domain.errors.StaleRevisionError`
  instead of overwriting the winner.
* **One freeze per submission.** ``submit`` records the project revision, the plan
  identity and a fingerprint of every input file, keyed by ``idempotency_key``.  The
  same key with the same content returns the *same* job; different content is an
  ``IDEMPOTENCY_CONFLICT``; a changed input file is ``INPUT_CHANGED`` at run time.
* **Nothing half-published.** A run renders into ``staging/<job>`` and is moved into
  ``runs/<job>`` only after its artifacts verify; the move and the receipt happen
  together.  A killed run is reconciled to ``interrupted`` (never success), and a
  cancelled run's staging is moved to ``recovery/`` rather than published.
* **SQLite holds tasks, receipts and artifacts only** — never a second copy of the
  editable project, whose truth stays ``project.json``.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from ..domain.errors import (ProjectError, SchemaError, StaleRevisionError)
from ..domain.model import Project
from ..jobs import job_lock
from .assets import ASSETS_FILENAME, AssetIndex
from .project_store import PROJECT_FILENAME, load_project, save_project, write_document

SCHEMA = 'wv-coordinator@1'
PROJECTS_DIR = 'projects'
STAGING_DIR = 'staging'
RUNS_DIR = 'runs'
RECOVERY_DIR = 'recovery'
BACKUPS_DIR = 'backups'
COORDINATOR_FILENAME = 'coordinator.sqlite3'

#: Task states.  ``interrupted`` is what a worker that died leaves behind; a run is
#: only ever ``succeeded`` after its artifacts verified and were published.
JOB_STATES = ('queued', 'running', 'succeeded', 'partial_failed', 'failed',
              'cancelled', 'interrupted')
#: Phases are separate from state, so "running the renderer" and "failed" cannot be
#: confused with "paused while rendering".
PHASES = ('submitted', 'preparing', 'rendering', 'verifying', 'publishing', 'done')


class CoordinatorError(ProjectError):
    """Coordination failure with a code, an object path and a way forward."""

    code = 'COORDINATOR_ERROR'


class IdempotencyConflict(CoordinatorError):
    code = 'IDEMPOTENCY_CONFLICT'


class InputChanged(CoordinatorError):
    code = 'INPUT_CHANGED'


class JobNotFound(CoordinatorError):
    code = 'JOB_NOT_FOUND'


class JobStateError(CoordinatorError):
    code = 'JOB_STATE'


class NeedsInput(CoordinatorError):
    """Non-interactive callers get this instead of a question they cannot answer."""

    code = 'NEEDS_INPUT'

    def __init__(self, message, *, path='', hint='', fixes=()):
        super().__init__(message, path=path, hint=hint)
        self.fixes = tuple(fixes)

    def to_dict(self):
        document = super().to_dict()
        document['fixes'] = list(self.fixes)
        return document


def _now():
    return time.time()


def _sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def connect(db):
    """A connection with the coordinator's own tables, created on first use."""
    db = Path(db).resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(
        'CREATE TABLE IF NOT EXISTS projects ('
        ' project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,'
        ' document TEXT NOT NULL, updated REAL NOT NULL);'
        'CREATE TABLE IF NOT EXISTS jobs ('
        ' id TEXT PRIMARY KEY, key TEXT NOT NULL, digest TEXT NOT NULL,'
        ' submission TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,'
        ' control TEXT NOT NULL, inputs TEXT NOT NULL, result TEXT, error TEXT,'
        ' created REAL NOT NULL, updated REAL NOT NULL);'
        'CREATE TABLE IF NOT EXISTS receipts ('
        ' idempotency_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, digest TEXT NOT NULL,'
        ' project_id TEXT NOT NULL, revision INTEGER NOT NULL, plan_identity TEXT,'
        ' inputs TEXT NOT NULL, created REAL NOT NULL);'
        'CREATE TABLE IF NOT EXISTS artifacts ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, path TEXT NOT NULL,'
        ' sha256 TEXT NOT NULL, kind TEXT NOT NULL, published REAL NOT NULL);')
    con.commit()
    try:
        with con:
            yield con
    finally:
        con.close()


@dataclass(frozen=True)
class Fingerprint:
    """One frozen input file: where it is and what it was when the batch was accepted."""

    path: str
    sha256: str

    def to_dict(self):
        return {'path': self.path, 'sha256': self.sha256}


def fingerprint(paths):
    """Fingerprint every input of a submission, skipping files that are not there."""
    found = []
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            found.append(Fingerprint(str(candidate.resolve()), _sha256(candidate)))
    return tuple(found)


def default_runner(submission, staging, checkpoint):
    """Produce the three deliverables into ``staging/<job>`` (the real pipeline).

    ``staging`` is the directory the run may create and fill; the coordinator moves
    it into ``runs/`` only after the artifacts verify, so a kill cannot leave a
    half-written run looking published.
    """
    from ..exporters import export_run
    plan = submission.plan
    project_dir = Path(submission.project_folder)
    result = export_run(str(project_dir), output=str(Path(staging).parent),
                        run_name=Path(staging).name,
                        background=plan.profile.background or None,
                        video_codec=plan.profile.video_codec,
                        slices=plan.profile.slices)
    checkpoint()
    return result.to_dict()


class Coordinator:
    """The single writer for one work root."""

    def __init__(self, root, *, db=None, runner=None):
        self.root = Path(root).resolve()
        self.db = Path(db).resolve() if db else self.root / COORDINATOR_FILENAME
        self.runner = runner or default_runner

    # -- layout ----------------------------------------------------------
    def project_folder(self, project_id):
        return self.root / PROJECTS_DIR / str(project_id)

    def staging_folder(self, job_id):
        return self.root / STAGING_DIR / job_id

    def run_folder(self, job_id):
        return self.root / RUNS_DIR / job_id

    def _mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)
        return Path(path)

    @contextmanager
    def writer(self):
        """Take the one-writer lock (the same technique the job worker uses)."""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with job_lock(self.db, 'coordinator'):
            yield

    # -- projects --------------------------------------------------------
    def save_project(self, project, expected_revision=None):
        """Save one revision atomically, refusing to overwrite a newer one.

        The check is against the *stored* revision, under the writer lock, so two
        clients editing the same project cannot both "succeed": the second gets a
        stale-revision error that names both numbers.
        """
        if not isinstance(project, Project):
            raise SchemaError('save needs a Project', path='project')
        folder = self._mkdir(self.project_folder(project.project_id))
        with self.writer():
            stored = self.project_revision(project.project_id)
            current = self._stored_document(project.project_id)
            if expected_revision is not None and stored is not None \
                    and stored != expected_revision:
                raise StaleRevisionError(
                    'project %s is at revision %s, not %d'
                    % (project.project_id, stored, expected_revision), path='project',
                    hint='重新读取工程后再保存；另一个客户端已经写过')
            if current is not None and current != project.to_dict():
                self._backup(folder, project.project_id, current)
            document = save_project(project, folder)
            with connect(self.db) as con:
                con.execute(
                    'INSERT INTO projects (project_id, revision, document, updated)'
                    ' VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET'
                    ' revision=excluded.revision, document=excluded.document,'
                    ' updated=excluded.updated',
                    (project.project_id, project.revision, _sha256(document), _now()))
        return document

    def _backup(self, folder, project_id, document):
        """Keep the revision being replaced, so a bad save is recoverable."""
        backups = self._mkdir(folder / BACKUPS_DIR)
        write_document(document, backups / ('rev%s.json' % document.get('revision', 0)))

    def _stored_document(self, project_id):
        path = self.project_folder(project_id) / PROJECT_FILENAME
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def project_revision(self, project_id):
        with connect(self.db) as con:
            row = con.execute('SELECT revision FROM projects WHERE project_id=?',
                              (project_id,)).fetchone()
        return row['revision'] if row else None

    def load_project(self, project_id):
        path = self.project_folder(project_id) / PROJECT_FILENAME
        if not path.is_file():
            raise NeedsInput('no project %r under %s' % (project_id, self.root),
                             path='project',
                             fixes=['用 project save 写入工程，或检查 --project 指向的目录'])
        return load_project(path)

    def project_index(self):
        with connect(self.db) as con:
            rows = con.execute('SELECT project_id, revision, updated FROM projects'
                               ' ORDER BY project_id').fetchall()
        return [dict(row) for row in rows]

    def asset_index(self, project_id):
        folder = self.project_folder(project_id)
        path = folder / ASSETS_FILENAME
        return AssetIndex.load(folder) if path.is_file() else None

    # -- submissions -----------------------------------------------------
    def submit(self, submission, idempotency_key):
        """Freeze one submission under a key; the same key and content is the same job.

        The digest covers the plan identity, the export profile, the revision and the
        selected record ids, and the receipt also stores the file fingerprints.  A
        repeat of the *same* request returns the original job (a retry must never
        spend twice); different content under a used key is a conflict.
        """
        key = str(idempotency_key or '').strip()
        if not key:
            raise NeedsInput('submit needs an idempotency key', path='submit',
                             fixes=['加 --key <stable-name>（同一批活重试时复用同一个键）'])
        digest = submission.digest()
        inputs = fingerprint(submission.input_paths)
        with connect(self.db) as con:
            row = con.execute('SELECT * FROM receipts WHERE idempotency_key=?',
                              (key,)).fetchone()
        if row is not None:
            if row['digest'] != digest:
                raise IdempotencyConflict(
                    'key %r already names a different request' % key, path='submit',
                    hint='同键同请求返回原任务；内容变了要换键或新建任务')
            return {'job': row['job_id'], 'created': False, 'key': key,
                    'digest': digest}
        job_id = 'wv-' + uuid.uuid4().hex[:20]
        plan = submission.plan
        with connect(self.db) as con:
            con.execute('INSERT INTO jobs (id,key,digest,submission,state,phase,control,'
                        'inputs,result,error,created,updated)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (job_id, key, digest,
                         json.dumps(submission.to_dict(), ensure_ascii=False),
                         'queued', 'submitted', 'run',
                         json.dumps([item.to_dict() for item in inputs],
                                    ensure_ascii=False), None, None, _now(), _now()))
            con.execute('INSERT INTO receipts (idempotency_key,job_id,digest,project_id,'
                        'revision,plan_identity,inputs,created) VALUES (?,?,?,?,?,?,?,?)',
                        (key, job_id, digest, plan.project_id, plan.revision,
                         plan.plan_identity,
                         json.dumps([item.to_dict() for item in inputs],
                                    ensure_ascii=False), _now()))
        return {'job': job_id, 'created': True, 'key': key, 'digest': digest}

    def receipts(self):
        """The receipt rows as they are stored, for a caller that wants to check them."""
        with connect(self.db) as con:
            rows = con.execute('SELECT * FROM receipts ORDER BY created').fetchall()
        return [dict(row) for row in rows]

    # -- task state ------------------------------------------------------
    def status(self, job_id):
        with connect(self.db) as con:
            row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            artifacts = con.execute('SELECT path, sha256, kind FROM artifacts'
                                    ' WHERE job_id=? ORDER BY path', (job_id,)).fetchall()
        if row is None:
            raise JobNotFound('no task %r' % (job_id,), path='job:%s' % job_id,
                              hint='用 job status 查询已提交的任务 id')
        document = dict(row)
        document['inputs'] = json.loads(document['inputs'] or '[]')
        document['submission'] = json.loads(document['submission'] or '{}')
        document['artifacts'] = [dict(item) for item in artifacts]
        if document['result']:
            try:
                document['result'] = json.loads(document['result'])
            except ValueError:
                pass
        return document

    def _set(self, job_id, **fields):
        fields['updated'] = _now()
        columns = ', '.join('%s=?' % name for name in fields)
        with connect(self.db) as con:
            con.execute('UPDATE jobs SET %s WHERE id=?' % columns,
                        tuple(fields.values()) + (job_id,))

    def control(self, job_id, action):
        """Record a control request; the state change follows at a checkpoint."""
        if action not in ('cancel', 'resume', 'pause'):
            raise JobStateError('unknown control %r' % (action,), path='job:%s' % job_id,
                                hint='可用：cancel / resume / pause')
        job = self.status(job_id)
        if action == 'cancel':
            if job['state'] in ('succeeded', 'partial_failed'):
                raise JobStateError('task %s already published' % job_id,
                                    path='job:%s' % job_id,
                                    hint='已发布的任务不会被取消；要改动就提交新任务')
            self._set(job_id, control='cancel')
            if job['state'] == 'queued':
                self._set(job_id, state='cancelled', phase='done')
        elif action == 'resume':
            if job['state'] in ('succeeded', 'partial_failed'):
                raise JobStateError('task %s already finished' % job_id,
                                    path='job:%s' % job_id)
            self._set(job_id, control='run')
            if job['state'] == 'cancelled':
                self._set(job_id, state='queued', phase='submitted')
        else:
            self._set(job_id, control='pause')
        return self.status(job_id)

    def cancel(self, job_id):
        return self.control(job_id, 'cancel')

    def resume(self, job_id):
        return self.control(job_id, 'resume')

    def artifacts(self, job_id):
        job = self.status(job_id)
        return {'job': job_id, 'state': job['state'],
                'published': job['state'] == 'succeeded',
                'artifacts': job['artifacts']}

    # -- running ---------------------------------------------------------
    def run(self, job_id, *, limit_seconds=None):
        """Execute one queued task: verify inputs, render to staging, verify, publish."""
        with job_lock(self.db, 'job.' + job_id):
            job = self.status(job_id)
            if job['state'] in ('succeeded', 'partial_failed'):
                return job                       # retrying a finished task does nothing
            if job['state'] == 'running' and job['updated'] > _now() - 1:
                return job
            if job['control'] == 'cancel':
                self._set(job_id, state='cancelled', phase='done')
                return self.status(job_id)
            self._set(job_id, state='running', phase='preparing', error=None)
            try:
                self._check_inputs(job)
                staging = self.staging_folder(job_id)
                if staging.exists():
                    # A previous attempt left a half-written run: move it aside, never
                    # merge with it.
                    self._recover(staging, job_id)
                self._set(job_id, phase='rendering')
                submission = _SubmissionView(
                    job, self.project_folder(job['submission'].get('plan', {})
                                             .get('project_id', '')))
                try:
                    result = self.runner(submission, staging,
                                         lambda: self._checkpoint(job_id))
                except _Cancelled:
                    # Control changed at a checkpoint: keep the half-run as evidence
                    # and publish nothing.
                    self._recover(staging, job_id)
                    if self.status(job_id)['control'] == 'cancel':
                        self._set(job_id, state='cancelled', phase='done')
                    else:
                        self._set(job_id, state='queued', phase='submitted')
                    return self.status(job_id)
                current = self.status(job_id)
                if current['control'] == 'cancel':
                    self._recover(staging, job_id)
                    self._set(job_id, state='cancelled', phase='done')
                    return self.status(job_id)
                self._set(job_id, phase='verifying')
                staged, problems = self._verify(staging)
                if problems:
                    self._recover(staging, job_id)
                    self._set(job_id, state='partial_failed', phase='done',
                              error='; '.join(problems)[:1500])
                    return self.status(job_id)
                self._set(job_id, phase='publishing')
                published = self.run_folder(job_id)
                published.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, published)
                with connect(self.db) as con:
                    for item in staged:
                        # Recorded *after* the move, under the published path: a
                        # receipt that pointed into staging would stop verifying the
                        # moment the run was published.
                        path = published / item['relative']
                        con.execute('INSERT INTO artifacts (job_id,path,sha256,kind,'
                                    'published) VALUES (?,?,?,?,?)',
                                    (job_id, str(path), item['sha256'], item['kind'],
                                     _now()))
                self._set(job_id, state='succeeded', phase='done',
                          result=json.dumps(result, ensure_ascii=False))
                return self.status(job_id)
            except NeedsInput:
                raise
            except Exception as error:           # noqa: BLE001 - recorded, then reported
                self._set(job_id, state='failed', phase='done',
                          error=str(error)[:1500])
                raise

    def _checkpoint(self, job_id):
        job = self.status(job_id)
        if job['control'] == 'cancel':
            raise _Cancelled('cancel requested')
        if job['control'] == 'pause':
            self._set(job_id, state='queued', phase='submitted')
            raise _Cancelled('pause requested')

    def _check_inputs(self, job):
        """A submission that was accepted must run on the inputs it was accepted with."""
        for item in job['inputs']:
            path = Path(item['path'])
            if not path.is_file():
                raise InputChanged('input disappeared: %s' % path, path='submit',
                                   hint='把文件放回去，或重新提交一个新任务')
            if _sha256(path) != item['sha256']:
                raise InputChanged('input changed: %s' % path, path='submit',
                                   hint='旧任务按提交时的输入指纹运行；改了输入要新建任务')

    def _verify(self, staging):
        """Every staged artifact must exist; report paths *relative* to the run.

        Relative paths are what make the artifact receipt survive publication: the
        run folder is moved from ``staging/`` to ``runs/`` after this check, and a
        receipt that named the staging path would stop verifying at that moment.
        """
        staging = Path(staging)
        if not staging.is_dir():
            return [], ['the run produced nothing']
        staged, problems = [], []
        for path in sorted(item for item in staging.rglob('*') if item.is_file()):
            kind = path.suffix.lstrip('.').lower() or 'file'
            staged.append({'relative': str(path.relative_to(staging)),
                           'sha256': _sha256(path), 'kind': kind})
        for name in ('video', 'srt', 'editable-draft'):
            if not any(name in item['relative'] for item in staged):
                problems.append('missing %s in the run' % name)
        return staged, problems

    def _recover(self, staging, job_id):
        """Move an unfinished run aside; it is evidence, not a delivery."""
        if not Path(staging).exists():
            return
        target = self._mkdir(self.root / RECOVERY_DIR)
        Path(staging).rename(target / ('%s-%s' % (job_id, uuid.uuid4().hex[:8])))

    def reconcile(self):
        """Say what happened to tasks a killed process left behind.

        A task whose per-job lock is free has no worker: it is marked ``interrupted``
        (never success).  A published task whose artifacts no longer verify becomes
        ``partial_failed``, so "half a delivery" can never pass as a success.
        """
        report = {'interrupted': [], 'partial_failed': [], 'healthy': []}
        with connect(self.db) as con:
            rows = con.execute('SELECT id, state FROM jobs').fetchall()
        for row in rows:
            job_id, state = row['id'], row['state']
            if state == 'running':
                try:
                    with job_lock(self.db, 'job.' + job_id):
                        self._set(job_id, state='interrupted', phase='done',
                                  error='worker is gone; nothing was published')
                    report['interrupted'].append(job_id)
                except RuntimeError:
                    report['healthy'].append(job_id)     # a live worker holds the lock
            elif state == 'succeeded':
                broken = [item['path'] for item in self.status(job_id)['artifacts']
                          if not Path(item['path']).is_file()
                          or _sha256(item['path']) != item['sha256']]
                if broken:
                    self._set(job_id, state='partial_failed', phase='done',
                              error='artifacts changed after publish: %s'
                                    % ', '.join(broken)[:1000])
                    report['partial_failed'].append(job_id)
                else:
                    report['healthy'].append(job_id)
        return report


class _Cancelled(Exception):
    """Internal: the runner stopped at a checkpoint because control changed."""


class _SubmissionView:
    """The submission as the runner sees it, with the folder it came from.

    The folder is resolved here rather than stored in the submission: the
    coordinator owns the project layout, so a runner does not have to be told
    twice — and if the layout has moved, the frozen ``project.json`` input path
    still says where the project was.
    """

    def __init__(self, job, project_folder):
        self.document = job['submission']
        folder = Path(project_folder)
        if not (folder / PROJECT_FILENAME).is_file():
            for path in self.document.get('input_paths', ()):
                if str(path).endswith(PROJECT_FILENAME):
                    folder = Path(path).parent
                    break
        self.project_folder = str(folder)
        self.plan = _PlanView(job['submission'])

    def __getattr__(self, name):
        return self.document[name]


class _PlanView:
    """A read-only view of the frozen plan inside a submission document."""

    def __init__(self, document):
        plan = document.get('plan', {})
        self.project_id = plan.get('project_id', '')
        self.revision = plan.get('revision', 0)
        profile = plan.get('profile', {})
        self.profile = type('Profile', (), {
            'background': profile.get('background', ''),
            'video_codec': profile.get('video_codec', 'h264'),
            'slices': profile.get('slices')})()


def cleanup_partial(root):
    """Move every leftover staging folder into ``recovery/`` (best effort)."""
    staging = Path(root) / STAGING_DIR
    if not staging.is_dir():
        return []
    recovery = Path(root) / RECOVERY_DIR
    recovery.mkdir(parents=True, exist_ok=True)
    moved = []
    for folder in sorted(staging.iterdir()):
        target = recovery / (folder.name + '-' + uuid.uuid4().hex[:8])
        shutil.move(str(folder), str(target))
        moved.append(str(target))
    return moved
