"""Find the real speech already on this machine and reuse it without a network.

Eight old batches left ~2300 service originals in ``audio-cache`` folders.  They
cannot be found by cache key any more - W02 changed the token to a content
identity, which orphaned all 150/150 folders of one batch by measurement - but
they do not need to be: each folder still holds the *audio* and a record naming
the role, the exact text spoken and the voice that spoke it.  That is enough to
match a word list entry to its recording.

Three rules make the result trustworthy rather than convenient:

* **the file is identity, the record only names it.**  Nothing here reads a
  duration out of a record: the engine re-measures the chosen original and applies
  the speed itself.  The reason is not that the records are always wrong - for the
  file a record actually names, ``raw`` matches to the millisecond (measured over
  all 2310 usable originals) - but that the *right file* has to be chosen first: a
  ``parallel`` folder holds two channels whose lengths differ (1.248 s vs 1.160 s
  in the first folder examined), so reading the wrong one would invent a stage
  length.  A record is therefore used to select a file, never to describe one.
* **an ambiguous folder yields nothing.**  A ``parallel`` token holds two
  originals (the Jianying channel and the paid legacy channel).  Which one is the
  lesson's voice is stated by ``spec.timeline_route``; when that field is absent
  the folder is *reported* as ambiguous instead of guessed at, because taking the
  wrong one puts a different voice on the timeline.
* **voice identity is canonicalised, not compared as text.**  The old runs wrote
  either the speaker id (``BV503_streaming``) or the name the user saw
  (``Energetic Female(English)``); both name the same voice, so both resolve to
  one identity through the definitions this project already ships.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import unicodedata

#: Route name -> the file suffix the old runs wrote for that channel.
ROUTE_SUFFIX = {'jianying': '.jianying.ogg', 'volcengine_legacy': '.volcengine_legacy.ogg',
                'volcengine_sse': '.volcengine_sse.mp3'}

#: Candidate file names in the order a *single-route* folder would use them.
SINGLE_ROUTE_FILES = ('original.mp3', 'original.ogg', 'original.wav')


class ImportError_(Exception):
    """An import cannot proceed without guessing; the caller must decide."""


@dataclass(frozen=True)
class CacheEntry:
    """One usable original found on disk, with the identity that names it."""

    folder: str
    role: str
    text: str
    voice: str
    voice_id: str
    kind: str
    route: str
    path: str
    token: str = ''

    def to_dict(self):
        return {'folder': self.folder, 'role': self.role, 'text': self.text,
                'voice': self.voice, 'voice_id': self.voice_id, 'kind': self.kind,
                'route': self.route, 'path': self.path, 'token': self.token}


@dataclass(frozen=True)
class AmbiguousEntry:
    """One (role, text) with more than one recording; the caller picks one."""

    role: str
    text: str
    voice_ids: tuple = ()
    folders: tuple = ()
    reason: str = ''

    def to_dict(self):
        return {'role': self.role, 'text': self.text, 'voice_ids': list(self.voice_ids),
                'folders': list(self.folders), 'reason': self.reason,
                'options': [{'voice_id': voice_id, 'folder': folder}
                            for voice_id, folder in zip(self.voice_ids, self.folders)]}


@dataclass
class ScanReport:
    roots: tuple = ()
    entries: tuple = ()
    ambiguous: tuple = ()
    skipped: tuple = ()
    counts: dict = field(default_factory=dict)

    def to_dict(self):
        return {'roots': list(self.roots), 'counts': dict(self.counts),
                'entries': len(self.entries), 'ambiguous': len(self.ambiguous),
                'skipped': len(self.skipped),
                'skipped_sample': list(self.skipped[:5])}


def normalize_text(text):
    """Compare spoken text by what is said, not by how it was encoded.

    NFKC folds full-width forms to their ASCII equivalents; the whitespace
    collapse covers a line break or a doubled space that a word list and a cache
    record disagree about.  Nothing else is touched: two genuinely different
    strings stay different, which is what makes a wrong word fail loudly later.
    """
    if not isinstance(text, str):
        raise ImportError_('spoken text must be a string')
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def canonical_voices():
    """``{alias: voice_id}`` from the voice definitions this project ships.

    Both the speaker id and the display name map to the id, so a cache record
    written with either form matches the same voice.  A value that is not known
    keeps its own text as its identity (an unknown voice is still a distinct
    voice, and matching it silently to a known one would substitute a sound).
    """
    from ..original_tts import VOICES
    aliases = {}
    for role, meta in VOICES.items():
        speaker = str(meta['speaker']).strip()
        for name in (speaker, meta.get('name')):
            if isinstance(name, str) and name.strip():
                aliases[name.strip().lower()] = speaker
    return aliases


def canonical_voice(value):
    """The one identity of a voice named by id or by display name."""
    text = str(value or '').strip()
    if not text:
        return ''
    return canonical_voices().get(text.lower(), text)


def _route_file(folder, route):
    """The original file of one named route, or ``None``."""
    suffix = ROUTE_SUFFIX.get(route)
    if not suffix:
        return None
    for path in sorted(folder.glob('original.*')):
        if path.name.endswith(suffix):
            return path
    return None


def _single_file(folder):
    """The only original in a folder written by a non-parallel provider."""
    for name in SINGLE_ROUTE_FILES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def scan_roots(roots, progress=None):
    """Read every cache folder under ``roots``; never writes anything.

    A folder whose record and files disagree (no ``spec``, no original, a
    ``parallel`` record without ``timeline_route``) is reported in ``skipped`` or
    ``ambiguous`` with the reason, rather than being silently dropped: someone has
    to be able to ask "why is word 42 missing?" and get an answer.
    """
    entries, ambiguous, skipped = [], [], []
    counts = {'folders': 0, 'records_missing': 0, 'records_unreadable': 0,
              'originals_missing': 0, 'route_undecided': 0}
    resolved = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise ImportError_('cache root is not a directory: %s' % root)
        if root.name == 'audio-cache':
            resolved.append(root)
        else:
            for found in sorted(root.rglob('audio-cache')):
                if found.is_dir():
                    resolved.append(found)
    for root in resolved:
        for folder in sorted(path for path in root.iterdir() if path.is_dir()):
            counts['folders'] += 1
            if progress:
                progress(folder)
            record = folder / 'complete.json'
            if not record.is_file():
                counts['records_missing'] += 1
                skipped.append({'folder': str(folder), 'reason': 'no complete.json'})
                continue
            try:
                saved = json.loads(record.read_text(encoding='utf-8'))
            except ValueError as error:
                counts['records_unreadable'] += 1
                skipped.append({'folder': str(folder),
                                'reason': 'complete.json unreadable: %s' % error})
                continue
            spec = saved.get('spec') or {}
            role = str(spec.get('role') or '')
            text = spec.get('text')
            if role not in ('female', 'male', 'chinese') or not isinstance(text, str):
                counts['records_missing'] += 1
                skipped.append({'folder': str(folder),
                                'reason': 'record is not a speech stage'})
                continue
            voice = canonical_voice(spec.get('voice'))
            route = str(spec.get('timeline_route') or '')
            if spec.get('kind') == 'parallel':
                path = _route_file(folder, route) if route else None
                if path is None:
                    counts['route_undecided'] += 1
                    choices = sorted(item.name for item in folder.glob('original.*'))
                    ambiguous.append(AmbiguousEntry(
                        role=role, text=text, voice_ids=(voice,),
                        folders=(str(folder),),
                        reason=('parallel record without a usable timeline_route; '
                                'found %s' % (', '.join(choices) or 'nothing'))))
                    continue
            else:
                path = _route_file(folder, route) if route else _single_file(folder)
            if path is None:
                counts['originals_missing'] += 1
                skipped.append({'folder': str(folder),
                                'reason': 'no original.* file for the record'})
                continue
            entries.append(CacheEntry(
                folder=str(folder), role=role, text=text, voice=voice,
                voice_id=voice, kind=str(spec.get('kind') or ''),
                route=route or _route_of(path), path=str(path), token=folder.name))
    report = ScanReport(roots=tuple(str(root) for root in resolved),
                        entries=tuple(entries), ambiguous=tuple(ambiguous),
                        skipped=tuple(skipped), counts=counts)
    return report


def _route_of(path):
    name = Path(path).name
    for route, suffix in ROUTE_SUFFIX.items():
        if name.endswith(suffix):
            return route
    return ''


def group_entries(entries):
    """``{(role, normalized text): [entry, ...]}`` for matching."""
    grouped = {}
    for entry in entries:
        grouped.setdefault((entry.role, normalize_text(entry.text)), []).append(entry)
    return grouped


def resolve_entry(entries, voice_id=None):
    """Pick one recording from the candidates, or report why it is not decided.

    With ``voice_id`` the choice is the caller's and is honoured (and a voice that
    is not present is reported, not replaced).  Without it, a single candidate is
    unambiguous; several candidates mean several *voices*, which is a decision
    about sound and therefore never automatic.
    """
    if voice_id:
        wanted = canonical_voice(voice_id)
        found = [entry for entry in entries if entry.voice_id == wanted]
        if not found:
            raise ImportError_(
                'no recording for voice %r; available: %s'
                % (voice_id, ', '.join(sorted({item.voice_id for item in entries})) or 'none'))
        if len(found) > 1:
            # The same voice recorded twice (two batches): identical identity, so
            # either can be used; the first by path keeps the choice reproducible.
            found = sorted(found, key=lambda item: item.path)
        return found[0], None
    if len(entries) == 1:
        return entries[0], None
    voices = sorted({entry.voice_id for entry in entries})
    if len(voices) == 1:
        # One voice, several copies: the same sound recorded twice, pick the first.
        return sorted(entries, key=lambda item: item.path)[0], None
    return None, AmbiguousEntry(
        role=entries[0].role, text=entries[0].text, voice_ids=tuple(voices),
        folders=tuple(entry.folder for entry in entries),
        reason='the same words are on disk in %d different voices' % len(voices))
