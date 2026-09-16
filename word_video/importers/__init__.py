"""Import what already exists: legacy speech caches and Jianying voices.

``cache``
    finds the real recordings the old batches left in ``audio-cache`` folders,
    matches them to a word list by content identity (role + normalised text +
    voice), and refuses to guess between voices.
``store``
    publishes the chosen originals into a project's asset folder, content
    addressed, and returns a ``provider.kind=local`` block the engine consumes -
    it re-measures and re-times them, so nothing here trusts an old record.
``jianying``
    lists the voices a member's own drafts used, cuts an audition clip from the
    member's own audio, records an explicitly confirmed mapping, and says for each
    voice whether it is reusable, synthesis-capable, unverified, needs the client,
    or comes from an unpublished channel.

Nothing in this package starts a synthesis call: every entry point is read-only or
publishes files that already exist on disk.
"""
from .cache import (ROUTE_SUFFIX, SINGLE_ROUTE_FILES, AmbiguousEntry, CacheEntry,
                    ImportError_, ScanReport, canonical_voice, group_entries,
                    normalize_text, resolve_entry, scan_roots)
from .jianying import (NEEDS_AUDITION, NEEDS_JIANYING_ACTION, REUSABLE,
                       RISK_UNPUBLISHED_ROUTE, STATUSES, STATUS_MEANING,
                       SYNTHESIS_ENTITLED, Candidate, ImportRefused, VoiceReport,
                       audition, confirm, scan as scan_voices, status_for)
from .store import LOCAL_KIND, ImportPlan, ImportResult, import_lesson, materialize, plan_import

__all__ = ['AmbiguousEntry', 'CacheEntry', 'Candidate', 'ImportError_', 'ImportPlan',
           'ImportRefused', 'ImportResult', 'LOCAL_KIND', 'NEEDS_AUDITION',
           'NEEDS_JIANYING_ACTION', 'REUSABLE', 'RISK_UNPUBLISHED_ROUTE',
           'ROUTE_SUFFIX', 'SINGLE_ROUTE_FILES', 'STATUSES', 'STATUS_MEANING',
           'SYNTHESIS_ENTITLED', 'ScanReport', 'VoiceReport', 'audition',
           'canonical_voice', 'confirm', 'group_entries', 'import_lesson',
           'materialize', 'normalize_text', 'plan_import', 'resolve_entry',
           'scan_roots', 'scan_voices', 'status_for']
