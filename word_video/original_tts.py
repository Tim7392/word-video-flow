"""Original draft voice IDs over legacy HTTP TTS; live parity needs audition.

Protocol reference: xialaup/pyvideotrans videotrans/tts/_volcengine.py.
This is an explicit candidate route, not a claim of Jianying API compatibility.
"""
import base64
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid

from .media import duration

ENDPOINT = 'https://openspeech.bytedance.com/api/v1/tts'
VOICES = {
    'female': {'speaker': 'BV503_streaming', 'resource_id': '7381388043711681087',
               'name': 'Energetic Female(English)', 'language': 'en'},
    'male': {'speaker': 'BV504_streaming', 'resource_id': '7381388164134343180',
             'name': 'Energetic Male(English)', 'language': 'en'},
    'chinese': {'speaker': 'BV406_streaming', 'resource_id': '7241089193122730533',
                'name': '网文解说', 'language': 'cn'},
}
ENV_NAMES = ('VOLC_TTS_APPID', 'VOLC_TTS_ACCESS_TOKEN', 'VOLC_TTS_CLUSTER')


def readiness():
    """Only disclose configuration presence, never account/credential values."""
    missing = [name for name in ENV_NAMES if not os.environ.get(name, '').strip()]
    return {'configured': not missing, 'missing': missing,
            'live_voice_parity': 'NOT_VERIFIED', 'voices': VOICES}


def synthesize_original(text, role, target, speaker=None):
    if role not in VOICES:
        raise ValueError('Unknown original voice role')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Text must be nonempty')
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    missing = readiness()['missing']
    if missing:
        raise PermissionError('Configure in worker environment: ' + ', '.join(missing))
    appid, token, cluster = (os.environ[name].strip() for name in ENV_NAMES)
    voice = VOICES[role]
    # The account's entitlement decides whether a chosen id speaks; the id is
    # passed through verbatim and never swapped for a similar voice.
    voice_type = (speaker or voice['speaker']).strip()
    if not voice_type:
        raise ValueError('A voice id is required')
    body = {'app': {'appid': appid, 'token': token, 'cluster': cluster},
            'user': {'uid': 'word-video-local'},
            'audio': {'voice_type': voice_type, 'encoding': 'mp3',
                      'language': voice['language'], 'speed_ratio': 1.0,
                      'volume_ratio': 1.0, 'pitch_ratio': 1.0},
            'request': {'reqid': str(uuid.uuid4()), 'text': text,
                        'text_type': 'plain', 'operation': 'query'}}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer;' + token}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'Original TTS HTTP {exc.code}; check service/voice entitlement') from None
    except (OSError, ValueError):
        raise RuntimeError('Original TTS connection or response invalid') from None
    # Never return partial audio on an error, or expose the server's raw message.
    if not isinstance(result, dict) or result.get('code') != 3000:
        code = result.get('code') if isinstance(result, dict) else None
        code = code if type(code) is int else 'unknown'
        raise RuntimeError(f'Original TTS service error code={code}; no alternate voice used')
    try:
        payload = base64.b64decode(result['data'], validate=True)
    except (KeyError, ValueError, TypeError):
        raise RuntimeError('Original TTS returned invalid audio') from None
    if not payload:
        raise RuntimeError('Original TTS returned empty audio')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial.mp3')
    try:
        with partial.open('xb') as stream:
            stream.write(payload)
        duration(partial)
        # Hard-link publication cannot overwrite a concurrently created target.
        os.link(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def audition(output, voices=None):
    """Generate new phrases, separate from the original draft's cached words.

    ``voices`` may name a speaker id per role (see ``word_video.voices`` for the
    ones this machine has used); the default is the three original ids.
    """
    if not readiness()['configured']:
        raise PermissionError('Configure in worker environment: ' + ', '.join(readiness()['missing']))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    texts = {'female': 'Curiosity opens a window to the world.',
             'male': 'Curiosity opens a window to the world.',
             'chinese': '好奇心为我们打开一扇了解世界的窗户。'}
    records = []
    for role, text in texts.items():
        speaker = (voices or {}).get(role) or VOICES[role]['speaker']
        target = output / (role + '.mp3')
        try:
            synthesize_original(text, role, target, speaker=speaker)
            record = {'role': role, 'text': text, 'speaker': speaker,
                      'path': str(target), 'duration_s': duration(target), 'state': 'generated'}
        except (RuntimeError, PermissionError, ValueError) as exc:
            record = {'role': role, 'text': text, 'speaker': speaker,
                      'state': 'failed', 'error': str(exc)}
        records.append(record)
    report = {'route': 'volcengine_legacy', 'chosen': bool(voices), 'results': records,
              'synthesis_complete': all(x['state'] == 'generated' for x in records),
              'listening_parity': 'PENDING_USER_AUDITION'}
    with (output / 'audition.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report
