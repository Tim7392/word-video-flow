"""Bring the voices a member already used into the new software, honestly.

A Jianying draft is a catalogue of the voices its author chose: every audio
material records ``tone_speaker`` (the id a synthesis route needs),
``tone_effect_name`` (the name the member saw in the app), the resource id and the
platform.  :mod:`word_video.voices` already reads that catalogue; this module adds
the three things the import flow needs and nothing else:

* **audio to listen to.**  A voice can only be confirmed by ear, so each candidate
  gets an audition clip cut from the draft's own media - the member hears the
  actual recording that will be reused, not a sample generated from the text.
* **an explicit confirmation.**  Reading a catalogue is not choosing a voice:
  nothing is written into a preset until the member confirms the mapping, and a
  refusal or a missing confirmation leaves the catalogue untouched.
* **a capability status that does not overclaim.**  "I have this file" and "I can
  make new speech in this voice" are different facts, and the status says which
  one holds:

  ==========================  =============================================
  ``REUSABLE``                audio exists and can be reused by asset id
  ``SYNTHESIS_ENTITLED``      a documented route is configured for this id
  ``NEEDS_AUDITION``          the identity is unconfirmed; listen before use
  ``NEEDS_JIANYING_ACTION``   only the client's own channel can read it
  ``RISK_UNPUBLISHED_ROUTE``  the id came from an undocumented channel
  ==========================  =============================================

The last two are the point of the taxonomy: the internal channel is described as
a risk, never promoted to a team capability, and a fixed client identity is never
turned into a default.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import uuid

REUSABLE = 'REUSABLE'
SYNTHESIS_ENTITLED = 'SYNTHESIS_ENTITLED'
NEEDS_AUDITION = 'NEEDS_AUDITION'
NEEDS_JIANYING_ACTION = 'NEEDS_JIANYING_ACTION'
RISK_UNPUBLISHED_ROUTE = 'RISK_UNPUBLISHED_ROUTE'

STATUSES = (REUSABLE, SYNTHESIS_ENTITLED, NEEDS_AUDITION, NEEDS_JIANYING_ACTION,
            RISK_UNPUBLISHED_ROUTE)

#: What each status means for the member, in one line (the UI shows this text).
STATUS_MEANING = {
    REUSABLE: '已有音频可直接复用；不因此获得新文字的合成能力',
    SYNTHESIS_ENTITLED: '已配置可用通道，可合成新文字',
    NEEDS_AUDITION: '尚未确认音色身份，先试听再决定',
    NEEDS_JIANYING_ACTION: '需要剪映客户端内的操作才能取得',
    RISK_UNPUBLISHED_ROUTE: '来自未公开的客户端内部通道，仅作风险提示，不作默认能力',
}

#: The documented route each built-in role's id can be synthesised through.
DOCUMENTED_ROUTES = {'volcengine_legacy': 'original_tts',
                     'volcengine_sse': 'tts',
                     'volcengine_bigmodel': 'tts'}


class ImportRefused(Exception):
    """The import would have to guess; the member has to decide instead."""


@dataclass(frozen=True)
class Candidate:
    """One voice found in a draft, with the audio a member can listen to."""

    speaker: str
    name: str
    resource_ids: tuple = ()
    platforms: tuple = ()
    uses: int = 0
    projects: tuple = ()
    sample: str = ''
    sample_seconds: float = 0.0
    status: str = NEEDS_AUDITION

    def to_dict(self):
        return {'speaker': self.speaker, 'name': self.name,
                'resource_ids': list(self.resource_ids),
                'platforms': list(self.platforms), 'uses': self.uses,
                'projects': list(self.projects), 'sample': self.sample,
                'sample_seconds': round(self.sample_seconds, 3), 'status': self.status,
                'status_meaning': STATUS_MEANING[self.status]}


@dataclass
class VoiceReport:
    roots: tuple = ()
    candidates: tuple = ()
    skipped_encrypted: int = 0
    unreadable: int = 0
    note: str = ''

    def by_status(self):
        grouped = {status: [] for status in STATUSES}
        for candidate in self.candidates:
            grouped[candidate.status].append(candidate)
        return {status: tuple(items) for status, items in grouped.items()}

    def to_dict(self):
        return {'roots': list(self.roots),
                'candidates': [item.to_dict() for item in self.candidates],
                'counts': {status: len(items)
                           for status, items in self.by_status().items()},
                'skipped_encrypted': self.skipped_encrypted,
                'unreadable': self.unreadable, 'note': self.note}


def _harvest_drafts(roots):
    """Every readable draft under ``roots``, with its audio materials."""
    from ..voices import MAX_DRAFT_BYTES, _harvest

    found, encrypted, unreadable = {}, 0, 0
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise ImportRefused('not a directory: %s' % root)
        for content in sorted(root.rglob('draft_content.json')):
            try:
                if content.stat().st_size > MAX_DRAFT_BYTES:
                    unreadable += 1
                    continue
                raw = content.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                unreadable += 1
                continue
            if not raw.lstrip().startswith('{'):
                # Jianying rewrites drafts encrypted; that is expected, not a
                # failure, and the file is left strictly alone.
                encrypted += 1
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                unreadable += 1
                continue
            _harvest(data, content.parent.name, found)
    return found, encrypted, unreadable


def _material_paths(root):
    """``{draft folder name: [audio file paths]}`` for auditioning."""
    from ..voices import _payloads

    listing = {}
    root = Path(root)
    for content in sorted(root.rglob('draft_content.json')):
        try:
            data = json.loads(content.read_text(encoding='utf-8', errors='ignore'))
        except (OSError, ValueError):
            continue
        for payload in _payloads(data):
            for material in ((payload.get('materials', {}) or {}).get('audios') or []):
                path = material.get('path')
                if isinstance(path, str) and path and Path(path).is_file():
                    listing.setdefault(content.parent.name, []).append(Path(path))
    return listing


def scan(roots, *, sample_dir=None, sample_seconds=3.0, progress=None):
    """List the voices in ``roots`` with (optionally) an audition clip each.

    ``sample_dir`` enables auditioning: one short clip per voice is cut from the
    first audio material that voice appears in.  The clip is a *reuse* of the
    member's own recording, which is what they are being asked to confirm.
    """
    from ..voices import Voice

    found, encrypted, unreadable = _harvest_drafts(roots)
    materials = {}
    for root in roots:
        for project, paths in _material_paths(root).items():
            materials.setdefault(project, []).extend(paths)
    candidates = []
    for speaker, voice in sorted(found.items()):
        sample, seconds = '', 0.0
        if sample_dir:
            sample, seconds = _cut_sample(speaker, voice, materials, Path(sample_dir),
                                          sample_seconds)
        candidates.append(Candidate(
            speaker=speaker, name=(sorted(voice.names)[0] if voice.names else ''),
            resource_ids=tuple(sorted(voice.resource_ids)),
            platforms=tuple(sorted(voice.platforms)), uses=voice.uses,
            projects=tuple(sorted(voice.projects)[:5]), sample=sample,
            sample_seconds=seconds, status=status_for(speaker, voice)))
        if progress:
            progress(speaker)
    return VoiceReport(roots=tuple(str(root) for root in roots),
                       candidates=tuple(candidates), skipped_encrypted=encrypted,
                       unreadable=unreadable,
                       note='导入音频只代表可以复用这段声音，不产生新文字的合成能力')


def _cut_sample(speaker, voice, materials, target_dir, seconds):
    """Cut a short clip of this voice out of the draft media that carries it."""
    from ..media import executable, run, wav_duration

    target_dir.mkdir(parents=True, exist_ok=True)
    for project in sorted(voice.projects):
        for source in materials.get(project, []):
            target = target_dir / ('%s-%s.wav' % (speaker, uuid.uuid4().hex[:8]))
            try:
                run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i',
                     str(source), '-t', '%.3f' % seconds, '-ar', '48000', '-ac', '1',
                     '-c:a', 'pcm_s16le', str(target)], timeout=120)
                length = wav_duration(target)
            except (RuntimeError, ValueError):
                target.unlink(missing_ok=True)
                continue
            if length > 0:
                return str(target), length
    return '', 0.0


def status_for(speaker, voice=None, routes=None):
    """What this voice can actually be used for, from facts, not optimism.

    The questions are asked in the order of the evidence available:

    1. is a documented route configured on this machine?  Then the id can be
       synthesised through it;
    2. is it one of the ids this project knows come from the client's own internal
       channel?  Then it is neither published nor entitled here, and saying so is
       the whole point - it is reported as a risk, never promoted to a capability;
    3. is it a shipped original?  Its audio is reusable, and making *new* speech
       with it still needs route (1), so without one the honest answer is "listen
       first", never "ready";
    4. anything else is a voice this machine only has a recording of: reusable, and
       nothing more is claimed.  An unfamiliar id is deliberately **not** called a
       risk - a voice the member cloned in their own account has an id we have
       never seen and is a perfectly legitimate capability through route (1).
    """
    from ..original_tts import VOICES as INTERNAL
    internal_ids = {meta['speaker'] for meta in INTERNAL.values()}
    configured = {name for name, value in (routes or {}).items() if value}
    if speaker in configured or (routes is None and _configured_route(speaker)):
        return SYNTHESIS_ENTITLED
    if speaker in internal_ids:
        return RISK_UNPUBLISHED_ROUTE
    if speaker in _built_in_voices():
        return NEEDS_AUDITION
    return REUSABLE


def _built_in_voices():
    from ..original_tts import VOICES
    return {meta['speaker']: meta for meta in VOICES.values()}


def _configured_route(speaker):
    """A documented route this machine is actually configured for, or ``''``."""
    import os
    from ..original_tts import ENV_NAMES
    if all(os.environ.get(name, '').strip() for name in ENV_NAMES):
        return 'volcengine_legacy'
    if os.environ.get('VOLC_TTS_API_KEY', '').strip():
        return 'volcengine_sse'
    return ''


def audition(candidates, speaker, output, seconds=8.0):
    """Cut a longer listenable clip of one candidate, from the draft's own media.

    Refuses a voice that has no reusable audio: there is nothing to listen to, and
    generating a sample would require exactly the synthesis ability the member has
    not been granted.
    """
    from ..media import executable, run, wav_duration

    chosen = next((item for item in candidates if item.speaker == speaker), None)
    if chosen is None:
        raise ImportRefused('unknown voice %r' % speaker)
    if not chosen.sample:
        raise ImportRefused('voice %r has no reusable audio to audition' % speaker)
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', chosen.sample,
         '-t', '%.3f' % seconds, '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le',
         str(output)], timeout=120)
    return {'speaker': speaker, 'path': str(output),
            'seconds': round(wav_duration(output), 3)}


def confirm(candidates, mapping, *, confirmed=False, preset_path=None,
            roles=('female', 'male', 'chinese')):
    """Write the member's confirmed voice mapping into a preset.

    ``mapping`` is ``{role: speaker-or-name}``; ``confirmed`` is the member's
    explicit approval, which is the whole point of the flow - reading a catalogue
    never selects a voice.  The preset records the *status* as well, so a later
    run can tell "this voice can be reused" from "this voice can be synthesised".
    """
    from ..voices import as_role_map

    if not confirmed:
        raise ImportRefused('the member has to confirm the mapping before it is saved')
    # Resolve names against the catalogue this scan produced, not by re-reading
    # every draft: the member chose a voice from *this* listing.
    listing = [{'speaker': item.speaker, 'name': item.name,
                'names': [name for name in (item.name,) if name],
                'resource_ids': list(item.resource_ids),
                'platforms': list(item.platforms), 'uses': item.uses,
                'projects': list(item.projects), 'origin': 'draft'}
               for item in candidates]
    resolved = as_role_map(mapping, roles=roles, listing=listing)
    by_speaker = {item.speaker: item for item in candidates}
    records = {}
    for role, speaker in resolved.items():
        found = by_speaker.get(speaker)
        status = found.status if found else NEEDS_AUDITION
        records[role] = {'speaker': speaker, 'name': found.name if found else '',
                         'status': status, 'status_meaning': STATUS_MEANING[status],
                         'confirmed': True,
                         'sample': found.sample if found else ''}
    preset = {'schema': 'wv-voices@1', 'roles': records,
              'note': '导入音频 ≠ 获得新文字合成能力；状态见 status'}
    if preset_path is not None:
        preset_path = Path(preset_path)
        preset_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = preset_path.with_name('.' + preset_path.name + '.tmp')
        temporary.write_text(json.dumps(preset, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        temporary.replace(preset_path)
    return preset
