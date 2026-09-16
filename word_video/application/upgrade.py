"""Upgrading a batch to a newer template version, without losing a member's edits.

A template version is only ever *added* (:mod:`word_video.storage.templates` refuses to
rewrite one), so "upgrade" can never mean "fix the template in place": it means
**re-apply the newer template to a batch** — and a batch is made of instances a member
may already have edited.  Two rules make that safe:

* **the newer version is a change, not a rewrite.**  The difference between the two
  versions is computed first (:func:`template_changes`) and only those fields move.  A
  field the newer version does not mention keeps whatever the instance says, so an
  upgrade cannot wipe an edit it never heard about.
* **a member's hand edit wins by default.**  Where the member changed the same field
  the template also changed, that is a **conflict**: both values are reported, the
  member's value stays, and only an explicit ``take_template`` lets the template win.
  A value the member left equal to the *old* template is not a hand edit of that field
  — it was following the template — so it follows the new one (a three-way merge with
  the old version as the base).

What this module deliberately refuses: a template whose *arrangement* changed (the
stages a record speaks, the layers shown, whether there is an intro layer).  Applying
that means re-expanding the clips, which would move or delete clips a member edited, so
it is reported as an un-appliable change with the reason instead of being done quietly.
Undo belongs to the caller who wrote the documents: it keeps the pre-upgrade state
(:class:`word_video.storage.batches.BatchStore`) and puts it back verbatim.
"""
from dataclasses import dataclass

from ..domain.errors import ProjectError, SchemaError
from ..domain.model import Project, StyleOverride, style_overrides, style_table
from .templates import TemplateDocument

SCHEMA = 'wv-upgrade@1'
MISSING = object()


class WiringChanged(ProjectError):
    """The new version changes the arrangement, which needs a rebuild the member owns."""

    code = 'TEMPLATE_WIRING_CHANGED'


@dataclass(frozen=True)
class Change:
    """One field the newer template version changes, and what it changes from."""

    role: str
    field: str
    was: object
    now: object

    def to_dict(self):
        return {'role': self.role, 'field': self.field, 'was': self.was, 'now': self.now}


@dataclass(frozen=True)
class Conflict:
    """A field both the template and the member changed; the member keeps it."""

    role: str
    field: str
    member: object
    template: object
    took: str                       # 'member' (default) or 'template'
    was: object = None

    def to_dict(self):
        return {'role': self.role, 'field': self.field, 'member': self.member,
                'template': self.template, 'took': self.took, 'was': self.was,
                'reason': ('成员手改与模板升级冲突；默认保留手改'
                           if self.took == 'member' else '成员手改与模板升级冲突；已按模板取值')}


@dataclass(frozen=True)
class MergeResult:
    """What an upgrade would do to one instance, and the styles it would leave."""

    styles: tuple = ()              # the merged StyleOverride tuple
    changes: tuple = ()             # every field the newer version changes
    applied: tuple = ()             # ... that the instance now follows
    kept: tuple = ()                # ... where the member's own value stayed
    conflicts: tuple = ()           # applied ∪ kept where the member had changed it
    wiring: tuple = ()              # arrangement changes; empty means appliable

    @property
    def appliable(self):
        return not self.wiring

    @property
    def changed(self):
        return bool(self.applied) or bool(self.kept)

    def to_dict(self):
        return {'schema': SCHEMA, 'appliable': self.appliable,
                'changes': [item.to_dict() for item in self.changes],
                'applied': [item.to_dict() for item in self.applied],
                'kept': [item.to_dict() for item in self.kept],
                'conflicts': [item.to_dict() for item in self.conflicts],
                'wiring': [item.to_dict() for item in self.wiring],
                'styles': style_table(self.styles)}


def _styles_of(template):
    if not isinstance(template, TemplateDocument):
        raise SchemaError('an upgrade needs TemplateDocument versions', path='template')
    return template.style_table()


def template_changes(old, new):
    """Every field the newer version changes, in role/field order.

    ``was=None`` means the field is new in this version (the member cannot have an
    opinion about a field that did not exist), and ``now=None`` means the version
    drops it — a removal is a change like any other and follows the same rules.
    """
    if not isinstance(old, TemplateDocument) or not isinstance(new, TemplateDocument):
        raise SchemaError('an upgrade needs TemplateDocument versions', path='template')
    if old.template_id != new.template_id:
        raise SchemaError('upgrade needs two versions of one template (%s -> %s)'
                          % (old.template_id, new.template_id), path='template')
    if new.version <= old.version:
        raise SchemaError('template %s@%d is not newer than @%d'
                          % (new.template_id, new.version, old.version), path='template',
                          hint='升级只往新版本走；要回退用 --undo 或显式升到另一个新版本')
    before, after = _styles_of(old), _styles_of(new)
    changes = []
    for role in sorted(set(before) | set(after)):
        fields = sorted(set(before.get(role, {})) | set(after.get(role, {})))
        for field in fields:
            was = before.get(role, {}).get(field, None)
            now = after.get(role, {}).get(field, None)
            if was != now:
                changes.append(Change(role=role, field=field, was=was, now=now))
    wiring = _wiring(old, new)
    return tuple(changes), wiring


def _wiring(old, new):
    """Arrangement differences an instance cannot absorb field by field."""
    found = []
    if old.lesson.stages != new.lesson.stages:
        found.append(Change(role='template', field='stages',
                            was=[node.role for node in old.lesson.stages],
                            now=[node.role for node in new.lesson.stages]))
    if old.lesson.displays != new.lesson.displays:
        found.append(Change(role='template', field='displays',
                            was=[node.role for node in old.lesson.displays],
                            now=[node.role for node in new.lesson.displays]))
    if bool(old.intro) != bool(new.intro):
        found.append(Change(role='layer.intro', field='present',
                            was=bool(old.intro), now=bool(new.intro)))
    return tuple(found)


def merge_styles(project, changes, *, take_template=False):
    """Three-way merge of one instance's styles with a template's own change.

    ``base`` is the *old* template: a member value equal to it was not an edit of
    that field, so the newer version's value applies.  Anything else the member
    wrote is theirs and stays, and is reported as a conflict when the newer version
    also moves that field.
    """
    if not isinstance(project, Project):
        raise SchemaError('an upgrade merges into a Project', path='project')
    table = {role: dict(fields) for role, fields in project.style_table().items()}
    applied, kept, conflicts = [], [], []
    for change in changes:
        member = table.get(change.role, {}).get(change.field, MISSING)
        if member is MISSING or member == change.was:
            _set(table, change.role, change.field, change.now)
            applied.append(change)
            continue
        if take_template:
            _set(table, change.role, change.field, change.now)
            applied.append(change)
            conflicts.append(Conflict(change.role, change.field, member, change.now,
                                      'template', change.was))
        else:
            kept.append(change)
            conflicts.append(Conflict(change.role, change.field, member, change.now,
                                      'member', change.was))
    return tuple(StyleOverride(role=role, fields=tuple(sorted(fields.items())))
                 for role, fields in sorted(table.items()) if fields), \
        tuple(applied), tuple(kept), tuple(conflicts)


def _set(table, role, field, value):
    fields = table.setdefault(role, {})
    if value is None:
        fields.pop(field, None)
        if not fields:
            table.pop(role, None)
        return
    fields[field] = value


def plan_upgrade(project, old, new, *, take_template=False, bump_revision=True):
    """``(project, MergeResult)``: the upgraded instance and what the merge did.

    The instance is only ever changed in its styles: the clips (times, splits, the
    words) are the member's, and nothing here reads or rewrites them.  ``revision``
    moves so the save is a new revision of *that* instance and two clients can still
    tell whose write is newer.
    """
    changes, wiring = template_changes(old, new)
    styles, applied, kept, conflicts = merge_styles(project, changes,
                                                    take_template=take_template)
    result = MergeResult(styles=styles, changes=changes, applied=applied, kept=kept,
                         conflicts=conflicts, wiring=wiring)
    if wiring:
        return project, result
    if not bump_revision:
        return project.with_styles(styles), result
    return project.with_styles(styles, revision=project.revision + 1), result


def apply_upgrade(project, old, new, **options):
    """Like :func:`plan_upgrade` but refuses an arrangement change by name."""
    upgraded, result = plan_upgrade(project, old, new, **options)
    if result.wiring:
        detail = '; '.join('%s.%s: %r -> %r' % (item.role, item.field, item.was, item.now)
                           for item in result.wiring)
        raise WiringChanged(
            'template %s@%d changes the arrangement, not just the styles: %s'
            % (new.template_id, new.version, detail), path='template',
            hint='版式变化要重建片段，会丢掉成员改过的时间/拆分；'
                 '请新建一个包（batch submit）而不是原地升级')
    return upgraded, result


def describe_undo(before, after):
    """The before/after a caller prints after restoring: a comparison, not a claim."""
    return {'styles_before': style_table(style_overrides(before.styles)),
            'styles_after': style_table(style_overrides(after.styles)),
            'revision_before': before.revision, 'revision_after': after.revision}
