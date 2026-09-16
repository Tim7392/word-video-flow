"""List the reading voices this machine has actually used, so a user can pick one.

A Jianying audio material records the voice it was made with::

    "tone_speaker": "BV503_streaming",        <- the id both routes need
    "tone_effect_name": "Energetic Female(English)",
    "resource_id": "7381388043711681087",
    "tone_platform": "sami"

So every readable draft on the machine is a catalogue of the voices its author
already chose, with the name they saw in the app next to the id the service
needs.  This module only reads; it never writes into a draft, and an encrypted
draft (Jianying rewrites them on save) is counted and skipped, never guessed at.
"""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re

from .original_tts import VOICES

# A speaker id shape, e.g. BV503_streaming.  Anything matching it is passed to the
# service verbatim; the service is the authority on entitlement.
ID_LIKE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{3,}$')

# Where the client keeps its drafts.  Both are read-only inspection targets.
DEFAULT_ROOTS = (Path(r'D:\jianying\JianyingPro Drafts'),
                 Path(os.environ.get('LOCALAPPDATA', ''))
                 / 'JianyingPro' / 'User Data' / 'Projects')
MAX_DRAFT_BYTES = 80 * 1024 * 1024


@dataclass
class Voice:
    speaker: str
    names: set = field(default_factory=set)
    resource_ids: set = field(default_factory=set)
    platforms: set = field(default_factory=set)
    projects: set = field(default_factory=set)
    uses: int = 0
    origin: str = 'draft'

    def record(self):
        return {'speaker': self.speaker,
                'name': sorted(self.names)[0] if self.names else None,
                'names': sorted(self.names),
                'resource_ids': sorted(self.resource_ids),
                'platform': sorted(self.platforms)[0] if self.platforms else None,
                'uses': self.uses,
                'projects': sorted(self.projects)[:5],
                'origin': self.origin}


def _payloads(data):
    """A draft plus any nested draft inside it (剪映 stores one inside another)."""
    yield data
    for inner in (data.get('materials', {}) or {}).get('drafts', []) or []:
        payload = inner.get('draft') if isinstance(inner, dict) else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if isinstance(payload, dict):
            yield payload


def _clean(value):
    """Drop lone surrogates: 剪映 stores some names with \\udXXX escapes and a
    stray half of a pair would break JSON output and console printing."""
    if not isinstance(value, str):
        return value
    return value.encode('utf-8', 'ignore').decode('utf-8', 'ignore')


def _harvest(data, project, voices):
    for payload in _payloads(data):
        for material in ((payload.get('materials', {}) or {}).get('audios') or []):
            speaker = _clean(material.get('tone_speaker'))
            if not isinstance(speaker, str) or not speaker:
                continue
            voice = voices.setdefault(speaker, Voice(speaker))
            voice.uses += 1
            voice.projects.add(_clean(project))
            for key, bucket in (('tone_effect_name', voice.names),
                                ('resource_id', voice.resource_ids),
                                ('tone_platform', voice.platforms)):
                value = _clean(material.get(key))
                if isinstance(value, str) and value:
                    bucket.add(value)


def discover(roots=None, progress=None):
    """Every voice found in every readable draft, plus the built-in originals.

    Returns ``{'voices': [...], 'roots': [...], 'skipped_encrypted': n,
    'unreadable': n}``; nothing here raises for a missing root.
    """
    voices, scanned, encrypted, unreadable = {}, 0, 0, 0
    roots = [Path(root) for root in (roots or DEFAULT_ROOTS)]
    for root in roots:
        if not root.is_dir():
            continue
        for content in root.rglob('draft_content.json'):
            try:
                if content.stat().st_size > MAX_DRAFT_BYTES:
                    unreadable += 1
                    continue
                raw = content.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                unreadable += 1
                continue
            if not raw.lstrip().startswith('{'):
                # Jianying 11.4 rewrites drafts encrypted; that is expected, not
                # a failure, and the file is left strictly alone.
                encrypted += 1
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                unreadable += 1
                continue
            scanned += 1
            _harvest(data, content.parent.name, voices)
            if progress:
                progress(content.parent.name)
    for role, meta in VOICES.items():
        voice = voices.setdefault(meta['speaker'], Voice(meta['speaker']))
        voice.names.add(meta['name'])
        voice.resource_ids.add(meta['resource_id'])
        if voice.origin == 'draft' and not voice.projects:
            voice.origin = 'builtin'
    listed = sorted((voice.record() for voice in voices.values()),
                    key=lambda item: (-item['uses'], item['name'] or item['speaker']))
    return {'voices': listed, 'roots': [str(root) for root in roots],
            'drafts_read': scanned, 'skipped_encrypted': encrypted,
            'unreadable': unreadable,
            'note': 'a voice is used verbatim; an unentitled id fails the job, '
                    'and nothing is ever substituted silently'}


def as_role_map(chosen, roles=('female', 'male', 'chinese')):
    """Turn a role -> id-or-name map into the role -> speaker map a request needs.

    Names are accepted because that is what the user recognises from the app; a
    name is resolved against the voices this machine has used.  A value that
    already looks like a speaker id is passed through untouched, because the
    *service* decides entitlement - refusing it here would block a voice the user
    legitimately knows about but has not used on this machine yet.  An unknown
    name, on the other hand, is a typo and is reported with the available names.
    """
    listing = discover()['voices']
    by_speaker = {item['speaker']: item for item in listing}
    by_name = {}
    for item in listing:
        for name in item['names']:
            by_name.setdefault(name.lower(), item['speaker'])
    resolved = {}
    for role in roles:
        value = chosen.get(role)
        if not value:
            continue
        if not isinstance(value, str):
            raise ValueError('The voice for %s must be a string' % role)
        value = value.strip()
        if value in by_speaker:
            resolved[role] = value
        elif ID_LIKE.match(value):
            resolved[role] = value
        elif value.lower() in by_name:
            resolved[role] = by_name[value.lower()]
        else:
            known = sorted({name for item in listing for name in item['names']})
            raise ValueError('Unknown voice for %s: %r; known names: %s'
                             % (role, value, ', '.join(known[:8])))
    return resolved
