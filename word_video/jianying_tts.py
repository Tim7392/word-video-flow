"""Jianying's own text-reading channel (SAMI WebSocket), opt-in and isolated.

JianyingPro itself synthesises text reading through
``wss://sami.bytedance.com/internal/api/v2/ws``.  The protocol needs only the
locally installed client's ``device_id``/``iid`` plus a client app key, so the
three original voices can be exercised programmatically **without** any GUI
automation and without a Volcengine account.

Two honest constraints, both surfaced by the caller rather than hidden:

* This is an undocumented internal endpoint, not a published API. It can change
  or stop working without notice, and using it is the deployer's decision.
* Whether a given ``speaker`` is entitled is decided by the service. A refused
  voice raises; it is never swapped for a similar-sounding one.

Implemented on the standard library only (``socket`` + ``ssl``): no third-party
WebSocket dependency is added to this project for a single internal channel.
"""
import base64
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import uuid

HOST = 'sami.bytedance.com'
PATH = '/internal/api/v2/ws'
APP_ID = '3704'
APP_KEY = 'IZjhUeAYwP'
DEFAULT_DEVICE_ID = '1053764930506284'
DEFAULT_IID = '2314914062247833'
USER_AGENT = ('JianyingPro/5.9.0.11632 (Windows 10.0.19045; app_id:3704; device_id:%s)')
# The client requests ogg_opus; ffmpeg decodes it for the normal pipeline.
AUDIO_CONFIG = {'format': 'ogg_opus', 'sample_rate': 24000, 'bit_rate': 64000}


class SamiError(RuntimeError):
    """The channel failed, or the service refused the requested voice."""


def local_identity():
    """Read ``device_id``/``iid`` from this machine's JianyingPro installation.

    Falls back to the values published with the reference implementation when
    the local files are absent, and reports which source was used so a run is
    never mistaken for "read from my own client".
    """
    identity = {'device_id': DEFAULT_DEVICE_ID, 'iid': DEFAULT_IID,
                'source': 'builtin-defaults'}
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        return identity
    user_data = Path(local) / 'JianyingPro' / 'User Data'
    config = user_data / 'TTNet' / 'tt_net_config.config'
    if config.is_file():
        text = config.read_text(encoding='utf-8', errors='ignore')
        found = re.search(r'device_id[^0-9]{0,8}(\d{6,})', text)
        if found:
            identity['device_id'] = found.group(1)
            identity['source'] = 'local-config'
    logs = sorted((user_data / 'Log').glob('*.log'),
                  key=lambda p: p.stat().st_mtime, reverse=True) if (user_data / 'Log').is_dir() else []
    for entry in logs[:5]:
        found = re.search(r'iid=(\d+)', entry.read_text(encoding='utf-8', errors='ignore')[:1_000_000])
        if found:
            identity['iid'] = found.group(1)
            identity['source'] = 'local-config+log'
            break
    return identity


class _WebSocket:
    """Minimal RFC 6455 client: one handshake, then text/binary frames."""

    def __init__(self, host, path, extra_headers, timeout=20):
        self.sock = socket.create_connection((host, 443), timeout=timeout)
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=host)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = ['GET %s HTTP/1.1' % path, 'Host: %s' % host,
                   'Upgrade: websocket', 'Connection: Upgrade',
                   'Sec-WebSocket-Key: %s' % key, 'Sec-WebSocket-Version: 13']
        request += ['%s: %s' % item for item in extra_headers.items()]
        self.sock.sendall(('\r\n'.join(request) + '\r\n\r\n').encode())
        self.buffer = b''
        while b'\r\n\r\n' not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SamiError('connection closed during WebSocket handshake')
            self.buffer += chunk
        head, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        status = head.split(b'\r\n', 1)[0].decode('utf-8', 'replace')
        if ' 101 ' not in status:
            raise SamiError('WebSocket handshake refused: ' + status)
        self.closed = False

    def _read(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SamiError('connection closed by the service')
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send_text(self, text):
        payload = text.encode('utf-8')
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack('>H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('>Q', length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        """Return ('text', str) or ('binary', bytes); raises on close."""
        first, second = self._read(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack('>H', self._read(2))[0]
        elif length == 127:
            length = struct.unpack('>Q', self._read(8))[0]
        masked = bool(second & 0x80)
        mask = self._read(4) if masked else None
        payload = self._read(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise SamiError('service closed the stream')
        if opcode == 0x9:  # ping
            return self.recv()
        if opcode in (0x1, 0x2):
            return ('text' if opcode == 0x1 else 'binary',
                    payload.decode('utf-8', 'replace') if opcode == 0x1 else payload)
        return self.recv()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def synthesize(text, speaker, target, identity=None, timeout=20):
    """Synthesise one utterance with Jianying's own voice ``speaker``.

    Returns the written path.  A service refusal (``TaskFailed``) or a stream
    without audio raises :class:`SamiError`; nothing partial is published.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Text must be nonempty')
    if not isinstance(speaker, str) or not speaker.strip():
        raise ValueError('Speaker must be nonempty')
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    identity = identity or local_identity()
    query = '?device_id=%s&iid=%s' % (identity['device_id'], identity['iid'])
    headers = {'User-Agent': USER_AGENT % identity['device_id']}
    socket_ws = _WebSocket(HOST, PATH + query, headers, timeout=timeout)
    try:
        task_id = 'ai_gen_' + uuid.uuid4().hex[:8]
        start = {'app_id': APP_ID, 'appkey': APP_KEY, 'event': 'StartTask',
                 'namespace': 'TTS', 'task_id': task_id, 'message_id': task_id + '_0',
                 'payload': json.dumps({'text': text, 'speaker': speaker,
                                        'audio_config': AUDIO_CONFIG},
                                       ensure_ascii=False, separators=(',', ':'))}
        socket_ws.send_text(json.dumps(start, ensure_ascii=False, separators=(',', ':')))
        socket_ws.send_text(json.dumps({'appkey': APP_KEY, 'event': 'FinishTask',
                                        'namespace': 'TTS'}))
        audio = bytearray()
        status = {'code': None, 'text': None}
        while True:
            try:
                kind, message = socket_ws.recv()
            except (socket.timeout, TimeoutError):
                raise SamiError('timed out waiting for audio from the service') from None
            if kind == 'binary':
                audio.extend(message)
                continue
            event = json.loads(message)
            name = event.get('event')
            if name == 'TaskFailed':
                status = {'code': event.get('status_code'), 'text': event.get('status_text')}
                raise SamiError('service refused speaker %r: %s (code %s)'
                                % (speaker, status['text'], status['code']))
            if name == 'TaskFinished':
                break
        if not audio:
            raise SamiError('service returned no audio for speaker %r' % speaker)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
        try:
            with partial.open('xb') as stream:
                stream.write(bytes(audio))
            os.link(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target
    finally:
        socket_ws.close()


def audition(output, voices=None):
    """Generate the three reference voices on NEW text, for listening parity.

    Separate from any cached word audio: the phrases do not exist in the
    reference project, so success proves new synthesis rather than cache reuse.
    ``voices`` may name a speaker id per role, e.g. a voice the user picked in
    Jianying; the default is the three original ids.
    """
    import json as _json
    from .original_tts import VOICES
    from .media import duration as _duration
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    texts = {'female': 'Curiosity opens a window to the world.',
             'male': 'Curiosity opens a window to the world.',
             'chinese': '好奇心为我们打开一扇了解世界的窗户。'}
    identity = local_identity()
    records = []
    for role, text in texts.items():
        speaker = (voices or {}).get(role) or VOICES[role]['speaker']
        target = output / (role + '.ogg')
        record = {'role': role, 'speaker': speaker,
                  'name': VOICES[role]['name'], 'text': text}
        try:
            synthesize(text, speaker, target, identity)
            record.update(state='generated', path=str(target),
                          bytes=target.stat().st_size,
                          duration_s=round(_duration(target), 3))
        except (SamiError, OSError, ValueError) as error:
            record.update(state='failed', error=str(error))
        records.append(record)
    report = {'route': 'jianying_sami_wss', 'identity_source': identity['source'],
              'chosen': bool(voices),
              'warning': 'undocumented internal endpoint of the installed client; '
                         'entitlement is decided by the service',
              'results': records,
              'synthesis_complete': all(x['state'] == 'generated' for x in records),
              'listening_parity': 'PENDING_USER_AUDITION'}
    with (output / 'audition.json').open('x', encoding='utf-8') as stream:
        _json.dump(report, stream, ensure_ascii=False, indent=2)
    return report
