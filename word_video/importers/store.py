"""Import found speech into a project's asset store, without a network or a copy.

Materialising is deliberately *boring*: each chosen original becomes a file in the
project's asset folder named after the digest of its own bytes, published with a
hard link when the file system allows it and a copy when it does not.  Two
consequences matter more than the code:

* **importing twice does not grow the disk.**  The digest is the identity, so the
  second import finds the file already there and adds nothing - the "one asset,
  referenced by id" rule from the architecture, enforced by the file name.
* **the engine still does the work.**  Nothing here measures a duration or applies
  a speed: the product is a set of real files plus ``provider.kind=local`` items,
  which is the documented route for reusing audio, and the engine re-measures each
  original and applies the tempo itself.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil

from ..media import sha256
from .cache import ImportError_, resolve_entry, normalize_text

#: Provider kind whose items are raw audio supplied by the caller.
LOCAL_KIND = 'local'


@dataclass
class ImportPlan:
    """What an import would do, before it touches the disk."""

    items: tuple = ()
    missing: tuple = ()
    ambiguous: tuple = ()
    unmatched: tuple = ()

    @property
    def ok(self):
        return not self.missing and not self.ambiguous

    def to_dict(self):
        return {'items': len(self.items), 'missing': [dict(item) for item in self.missing],
                'ambiguous': [item.to_dict() for item in self.ambiguous],
                'unmatched': list(self.unmatched),
                'ready': self.ok}


@dataclass
class ImportResult:
    provider: dict = field(default_factory=dict)
    assets: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)

    def to_dict(self):
        return {'provider': self.provider, 'assets': len(self.assets),
                'report': self.report}


def plan_import(records, scan, voice_by_role=None, text_field='spoken_meaning'):
    """Match a word selection against the scanned recordings.

    ``records`` is an iterable of objects with ``index``, ``word``,
    ``spoken_meaning`` (the same records a project carries).  ``voice_by_role``
    pins the voice per teaching role; without it, one candidate per (role, text)
    is used and several candidates become a question for the caller.
    """
    from ..domain.model import TEACHING_STAGES

    voice_by_role = {role: value for role, value in (voice_by_role or {}).items() if value}
    grouped = {}
    for entry in scan.entries:
        grouped.setdefault((entry.role, normalize_text(entry.text)), []).append(entry)
    items, missing, ambiguous = [], [], []
    seen_roles = set()
    for record in records:
        for role in TEACHING_STAGES:
            seen_roles.add(role)
            spoken = record.word if role in ('female', 'male') else record.spoken_meaning
            key = (role, normalize_text(spoken))
            candidates = grouped.get(key) or []
            if not candidates:
                missing.append({'index': record.index, 'role': role, 'text': spoken,
                                'reason': 'no recording on disk for this role and text'})
                continue
            entry, question = resolve_entry(candidates, voice_by_role.get(role))
            if question is not None:
                ambiguous.append(question)
                continue
            items.append({'index': record.index, 'role': role, 'text': spoken,
                          'voice': entry.voice_id or entry.voice,
                          'source': entry.path, 'folder': entry.folder,
                          'kind': entry.kind, 'route': entry.route})
    return ImportPlan(items=tuple(items), missing=tuple(missing),
                      ambiguous=tuple(ambiguous),
                      unmatched=tuple(sorted(seen_roles - set(TEACHING_STAGES))))


def materialize(plan, asset_dir, *, progress=None):
    """Publish every planned original into ``asset_dir`` and return the provider.

    The returned provider can be handed to the engine as-is: it is
    ``kind: local`` with ``path`` pointing at the published asset, which the
    engine re-measures and re-times.  Nothing in this function contacts a service.
    """
    if not plan.ok:
        raise ImportError_('import is not ready: %d missing, %d ambiguous'
                           % (len(plan.missing), len(plan.ambiguous)))
    asset_dir = Path(asset_dir)
    asset_dir.mkdir(parents=True, exist_ok=True)
    items, assets, reused, linked, copied = [], {}, 0, 0, 0
    for item in plan.items:
        source = Path(item['source'])
        if not source.is_file():
            raise ImportError_('recording disappeared: %s' % source)
        digest = sha256(source)
        target = asset_dir / ('%s%s' % (digest, source.suffix.lower()))
        if target.exists():
            reused += 1
        else:
            partial = target.with_name('.' + digest + '.partial')
            try:
                try:
                    os.link(source, partial)
                    linked += 1
                except OSError:
                    shutil.copy2(source, partial)
                    copied += 1
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        item = dict(item, path=str(target), asset_id='%s:%s' % (item['index'], item['role']))
        items.append(item)
        assets[item['asset_id']] = {'path': str(target), 'voice': item['voice'],
                                    'source': item['folder'], 'sha256': digest}
        if progress:
            progress(item)
    provider = {'kind': LOCAL_KIND,
                'items': [{key: item[key] for key in
                           ('index', 'role', 'text', 'voice', 'path')}
                          for item in items]}
    report = {'items': len(items), 'assets': len(assets), 'published_new': linked + copied,
              'reused': reused, 'hard_linked': linked, 'copied': copied}
    return ImportResult(provider=provider, assets=assets, report=report)


def import_lesson(records, scan, asset_dir, voice_by_role=None, progress=None):
    """Plan and materialise in one call; refuses rather than guessing."""
    plan = plan_import(records, scan, voice_by_role=voice_by_role)
    plan_report = plan.to_dict()
    if not plan.ok:
        return ImportResult(provider={}, assets={},
                            report={'ready': False, **plan_report})
    result = materialize(plan, asset_dir, progress=progress)
    result.report.update(plan_report)
    result.report['ready'] = True
    return result
