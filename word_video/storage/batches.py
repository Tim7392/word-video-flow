"""Batch plans on disk: one document per batch, with the plan it replaced kept.

A batch plan is a read-only product a member or an Agent can print, and it is also
the baseline a redo compares against ("this package changed, that one did not").  So
a re-plan does not overwrite the old document in place: the previous one moves to
``plans/rev-<n>.json`` first, exactly like a project keeps the revision it replaced.
That is what makes "重做范围" auditable after the fact instead of a claim.
"""
from pathlib import Path
import json

from ..application.packages import SCHEMA, BatchDocument
from ..domain.errors import SchemaError
from .project_store import read_document, write_document

BATCHES_DIR = 'batches'
PLAN_FILENAME = 'plan.json'
JOBS_FILENAME = 'jobs.json'
PLANS_DIR = 'plans'
UPGRADES_DIR = 'upgrades'
UPGRADE_PLAN = 'upgrade.json'
INSTANCE_PREFIX = 'project-'


class BatchStore:
    """The batch documents under one work root."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / BATCHES_DIR

    # -- layout ----------------------------------------------------------
    def batch_folder(self, batch_id):
        return self.folder / _checked(batch_id)

    def path(self, batch_id):
        return self.batch_folder(batch_id) / PLAN_FILENAME

    def backups(self, batch_id):
        folder = self.batch_folder(batch_id) / PLANS_DIR
        if not folder.is_dir():
            return []
        return sorted(item for item in folder.iterdir() if item.suffix == '.json')

    def batch_ids(self):
        if not self.folder.is_dir():
            return []
        return sorted(item.name for item in self.folder.iterdir() if item.is_dir())

    # -- reading ---------------------------------------------------------
    def load(self, batch_id):
        path = self.path(batch_id)
        if not path.is_file():
            raise SchemaError('no batch %r under %s' % (batch_id, self.folder),
                              path='batch',
                              hint='可用批次：%s' % '、'.join(self.batch_ids() or ['（无）']))
        return BatchDocument.from_dict(read_document(path, 'batch'))

    def index(self):
        """What exists, for a listing: id, packages, readiness, when it was planned."""
        found, problems = [], []
        for batch_id in self.batch_ids():
            try:
                document = self.load(batch_id)
            except SchemaError as error:
                problems.append({'batch_id': batch_id, 'error': str(error)})
                continue
            found.append({'batch_id': batch_id,
                          'source_project': document.source_project,
                          'template': '%s@%d' % (document.template_id,
                                                 document.template_version),
                          'selection': document.selection.describe(),
                          'rule': document.rule.describe(),
                          'packages': len(document.packages),
                          'ready': document.ready,
                          'units': document.usage.units,
                          'created': document.created,
                          'revisions': len(self.backups(batch_id)) + 1})
        return {'batches': found, 'problems': problems, 'schema': SCHEMA}

    def jobs_path(self, batch_id):
        return self.batch_folder(batch_id) / JOBS_FILENAME

    def jobs(self, batch_id):
        """``{package_id: {...}}``: which job each package was submitted as.

        Kept beside the plan because it is the other half of "what did this batch
        do": the plan says what it would run, this says which job it became, and a
        redo needs both to tell "already done" from "still to do".
        """
        path = self.jobs_path(batch_id)
        if not path.is_file():
            return {}
        return read_document(path, 'jobs').get('packages', {})

    def save_jobs(self, batch_id, key, packages):
        """Record the job of every package; merged with what is already recorded."""
        document = {'key': key, 'packages': {**self.jobs(batch_id), **packages}}
        return write_document(document, self.jobs_path(batch_id))

    # -- upgrades --------------------------------------------------------
    def upgrades_folder(self, batch_id):
        return self.batch_folder(batch_id) / UPGRADES_DIR

    def upgrades(self, batch_id):
        """Every recorded upgrade of one batch, oldest first (``1``, ``2``, ...)."""
        folder = self.upgrades_folder(batch_id)
        if not folder.is_dir():
            return []
        found = []
        for item in sorted(folder.iterdir()):
            if item.is_dir() and item.name.isdigit():
                found.append(int(item.name))
        return sorted(found)

    def upgrade_folder(self, batch_id, number):
        return self.upgrades_folder(batch_id) / str(int(number))

    def save_upgrade(self, batch_id, report, documents, *, number=None):
        """Record one upgrade *before* it is applied: the report and every instance.

        This is the undo: the pre-upgrade documents are copied here verbatim, so
        putting them back is not a re-derivation that might differ from what the
        member had — it is the same bytes.  Written before the instances change, so
        a crash in the middle still leaves a batch that can be restored.
        """
        number = number or (1 + max(self.upgrades(batch_id) or [0]))
        folder = self.upgrade_folder(batch_id, number)
        folder.mkdir(parents=True, exist_ok=True)
        for project_id, document in sorted(documents.items()):
            write_document(document, folder / ('%s%s.json' % (INSTANCE_PREFIX,
                                                              _checked(project_id))))
        write_document(report, folder / UPGRADE_PLAN)
        return folder

    def load_upgrade(self, batch_id, number=None):
        """``(report, {project_id: document})`` for one recorded upgrade."""
        numbers = self.upgrades(batch_id)
        if not numbers:
            raise SchemaError('batch %s has no recorded upgrade' % batch_id, path='batch',
                              hint='只有 --apply 过的升级才能撤销')
        number = numbers[-1] if number is None else int(number)
        if number not in numbers:
            raise SchemaError('batch %s has no upgrade %d' % (batch_id, number),
                              path='batch', hint='已记录：%s' % numbers)
        folder = self.upgrade_folder(batch_id, number)
        report = read_document(folder / UPGRADE_PLAN, 'upgrade')
        documents = {}
        for item in sorted(folder.glob('%s*.json' % INSTANCE_PREFIX)):
            name = item.name[len(INSTANCE_PREFIX):-len('.json')]
            documents[name] = read_document(item, 'project')
        return report, documents

    # -- writing ---------------------------------------------------------
    def save(self, document):
        """Write the plan, keeping the one it replaces; returns the document path."""
        if not isinstance(document, BatchDocument):
            raise SchemaError('save needs a BatchDocument', path='batch')
        folder = self.batch_folder(document.batch_id)
        target = folder / PLAN_FILENAME
        if target.is_file():
            previous = read_document(target, 'batch')
            if previous != document.to_dict():
                backups = folder / PLANS_DIR
                backups.mkdir(parents=True, exist_ok=True)
                number = 1 + max([_revision(item.name) for item in self.backups(
                    document.batch_id)] or [0])
                write_document(previous, backups / ('rev-%d.json' % number))
        return write_document(document.to_dict(), target)


def _revision(name):
    stem = Path(name).stem
    _, _, number = stem.partition('-')
    try:
        return int(number)
    except ValueError:
        return 0


def _checked(batch_id):
    if not isinstance(batch_id, str) or not batch_id \
            or any(character in batch_id for character in '\\/:*?"<>|'):
        raise SchemaError('batch id %r is not usable as a folder name' % (batch_id,),
                          path='batch')
    return batch_id
