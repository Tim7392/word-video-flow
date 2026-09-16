"""Explicit local-audio or official Volcengine SSE provider; never switch voices."""
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request
import urllib.error
import uuid

from .contracts import SpeechAsset, ROLES
from .media import atomic_json, duration, prepare_audio, sha256

ENDPOINT = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse'


def read_sse(lines):
    """Collect base64 audio from a v3 SSE body; raise on a service error code."""
    chunks = []
    for raw in lines:
        line = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        if not line.startswith('data:'):
            continue
        event = json.loads(line[5:].strip())
        code = event.get('code', 0)
        if code not in (0, 20000000):
            raise RuntimeError('TTS service error code=%s' % code)
        if event.get('data'):
            chunks.append(base64.b64decode(event['data'], validate=True))
    if not chunks:
        raise RuntimeError('TTS returned no audio')
    return b''.join(chunks)


def _api_key():
    key = (os.environ.get('VOLC_TTS_API_KEY') or '').strip()
    if not key:
        raise PermissionError('Configure VOLC_TTS_API_KEY in the worker environment')
    return key


def synthesize_bigmodel(text, speaker, target, resource='seed-tts-2.0'):
    """New-console route: ``X-Api-Key`` + ``X-Api-Resource-Id``, no appid.

    The API key is read from ``VOLC_TTS_API_KEY`` and never written to disk or
    to an error message.  ``resource`` selects the model generation and the
    billing line, so the caller names it explicitly.
    """
    key = _api_key()
    if not text.strip() or not speaker.strip():
        raise ValueError('Text and speaker must be nonempty')
    target = Path(target)
    if target.exists():
        raise FileExistsError(target)
    body = {'user': {'uid': 'word-video-local'},
            'req_params': {'text': text, 'speaker': speaker, 'sample_rate': 24000,
                           'audio_params': {'format': 'mp3'}}}
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-Api-Key': key,
                 'X-Api-Resource-Id': resource, 'X-Api-Request-Id': str(uuid.uuid4())},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = read_sse(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(_http_reason(error, key)) from None
    except urllib.error.URLError:
        raise RuntimeError('TTS connection failed') from None
    if not payload:
        raise RuntimeError('TTS returned empty audio')
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


def _http_reason(error, key):
    """Name the failure without ever echoing the credential."""
    said = ''
    try:
        said = error.read(600).decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001 - a missing body must not mask the status
        said = ''
    for fragment in ('"message"', '"code"'):
        if fragment in said:
            snippet = said[max(0, said.find(fragment) - 20):][:200].replace('\n', ' ')
            return 'TTS HTTP %s; %s' % (error.code, snippet)
    return 'TTS HTTP %s' % error.code


def synthesize(text, voice, target, resource='seed-tts-2.0'):
    """Alias for :func:`synthesize_bigmodel`, kept for existing callers."""
    return synthesize_bigmodel(text, voice, target, resource)


ROUTES = ('jianying', 'volcengine_legacy', 'volcengine_sse')
BIGMODEL_RESOURCES = ('seed-tts-2.0', 'seed-tts-1.0', 'seed-tts-1.0-concurr')


def synthesize_role(route, text, role, target, voice=None, resource=None, attempts=3):
    """Synthesise one role through one explicitly named route, with retries.

    Routes are never tried against each other: the caller names the route and a
    refusal is reported as a failure for *that* route.  A cached original voice
    must not silently change provider, which is why there is no fallback here.

    The service occasionally drops a connection mid-run, so transport failures
    are retried with backoff.  A service *refusal* (bad voice, entitlement) is
    not retried — it would simply fail again, and the message must stay honest.
    """
    def once():
        if route == 'jianying':
            from .jianying_tts import SamiError
            from .jianying_tts import synthesize as synthesize_jianying
            from .original_tts import VOICES
            speaker = voice or VOICES[role]['speaker']
            return synthesize_jianying(text, speaker, target)
        if route == 'volcengine_legacy':
            from .original_tts import synthesize_original
            return synthesize_original(text, role, target)
        if route == 'volcengine_sse':
            if not voice:
                raise ValueError('volcengine_sse requires an explicit speaker id')
            return synthesize(text, voice, target, resource or 'seed-tts-2.0')
        raise ValueError('Unknown speech route: %r' % (route,))
    if route not in ROUTES:
        raise ValueError('Unknown speech route: %r' % (route,))
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return once()
        except (PermissionError, FileExistsError, ValueError):
            raise
        except Exception as error:  # noqa: BLE001 - transport failures are retried
            last = error
            if attempt >= attempts:
                break
            time.sleep(min(8, 1.5 ** attempt))
    raise RuntimeError('%s route failed after %d attempts: %s'
                       % (route, attempts, last)) from None


# Measured ceilings (2026-09-16, see word_video_work/probe*.json): the paid
# Volcengine legacy endpoint sustains ~50 calls/s at 100 in flight with no
# failures, while Jianying's internal channel flattens at ~6-7 calls/s and gets
# *worse* past 32.  The numbers below are those measured safe points; a request
# may override them inside the validated range.
ROUTE_CONCURRENCY = {'volcengine_legacy': 100, 'jianying': 24, 'volcengine_sse': 16}
# Every item runs a local ffmpeg tempo pass.  It is cheap per file but spawning
# hundreds at once would thrash the machine, so local work gets its own bound.
LOCAL_PREP_CONCURRENCY = 8
MAX_CONCURRENCY = 100


def _whole(value, name, low, high):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError('%s must be an integer %d..%d' % (name, low, high))
    return value


def _provider_limits(provider, routes):
    """Validated in-flight limits for one job.

    ``provider.concurrency`` is how many items are worked on at once (default 1,
    i.e. the original serial behaviour: opt in explicitly so an account with a
    low ceiling is never flooded).  ``provider.route_concurrency`` overrides the
    per-route ceiling that keeps a slow channel from being swamped.
    """
    items = _whole(provider.get('concurrency', 1), 'provider.concurrency', 1, MAX_CONCURRENCY)
    overrides = provider.get('route_concurrency') or {}
    if not isinstance(overrides, dict):
        raise ValueError('provider.route_concurrency must be an object of route -> integer')
    unknown = sorted(set(overrides) - set(ROUTE_CONCURRENCY))
    if unknown:
        raise ValueError('Unknown route in provider.route_concurrency: ' + ', '.join(unknown))
    limits = {}
    for route in (routes or tuple(ROUTE_CONCURRENCY)):
        limits[route] = _whole(overrides.get(route, ROUTE_CONCURRENCY[route]),
                               'route_concurrency.%s' % route, 1, MAX_CONCURRENCY)
    return {'items': items, 'routes': limits,
            'local': max(1, min(LOCAL_PREP_CONCURRENCY, os.cpu_count() or 4))}


class _Gates:
    """Named in-flight limits, so a slow route cannot be flooded."""

    def __init__(self, limits, local):
        self._gates = {name: threading.Semaphore(value)
                       for name, value in limits.items()}
        self._gates['local'] = threading.Semaphore(value=local)

    @contextmanager
    def gate(self, name):
        semaphore = self._gates.get(name)
        if semaphore is None:
            raise ValueError('Unknown concurrency gate: %r' % (name,))
        with semaphore:
            yield


def _speech_plan(lesson, kind, voices, routes, timeline_route, resource, local, cache):
    """One work unit per distinct utterance, plus the item -> unit mapping.

    Two items can hash to the same token (a repeated word, or two different
    words sharing a spoken meaning), so units are deduplicated: under
    concurrency a shared folder would otherwise be written by two threads at
    once and the second write would trip the cache check.
    """
    units, plan, seen = [], [], {}
    for word in lesson.entries:
        for role in ROLES:
            text = word.spoken_meaning if role == 'chinese' else word.word
            spec = {'kind': kind, 'role': role, 'text': text, 'speed': lesson.speed,
                    'fps': lesson.fps}
            if kind == 'local':
                item = local.get((word.index, role))
                if not item or item['text'] != text:
                    raise ValueError('Missing or mismatched local speech: %d/%s'
                                     % (word.index, role))
                voice = item['voice']
                spec['source_digest'] = sha256(Path(item['path']).resolve(strict=True))
            else:
                voice = voices[role]
                if kind == 'volcengine_original':
                    spec['resource'] = 'volcengine_legacy_original_v1'
                elif kind == 'jianying_original':
                    spec['resource'] = 'jianying_sami_original_v1'
                else:
                    spec['resource'] = resource
            spec['voice'] = voice
            if kind == 'parallel':
                # Part of the spec *before* the token is computed: the token is
                # the hash of this dict, so anything added afterwards would make
                # a later run hash a different (clean) dict, land on the same
                # folder and then disagree with the stored spec.
                spec['routes'] = list(routes)
                spec['timeline_route'] = timeline_route
            token = hashlib.sha256(json.dumps(spec, sort_keys=True,
                                              ensure_ascii=False).encode()).hexdigest()
            if token in seen:
                plan.append(seen[token])
                continue
            seen[token] = len(units)
            units.append({'token': token, 'folder': cache / token, 'spec': spec,
                          'text': text, 'role': role, 'voice': voice,
                          'routes': list(routes) if kind == 'parallel' else None,
                          'timeline_route': timeline_route, 'source_kind': kind,
                          'local': local.get((word.index, role))})
            plan.append(seen[token])
    return units, plan


def _run_unit(unit, lesson, gates, checkpoint):
    """Synthesise one utterance (or reuse its committed cache record)."""
    checkpoint()
    folder = unit['folder']
    prepared = folder / 'prepared.wav'
    record = folder / 'complete.json'
    if record.exists():
        saved = json.loads(record.read_text(encoding='utf-8'))
        if saved['spec'] != unit['spec']:
            raise ValueError('Cached speech changed; preserve cache and inspect')
        if sha256(prepared) != saved['audio_digest']:
            raise ValueError('Cached speech changed; preserve cache and inspect')
        return {'raw': saved['raw'], 'rendered': saved['rendered']}
    folder.mkdir(parents=True, exist_ok=True)
    if prepared.exists():
        # A file without a committed record is never reused as complete.
        prepared.rename(folder / ('uncommitted-' + uuid.uuid4().hex + '.wav'))
    kind = unit['source_kind']
    if kind == 'local':
        # The caller already supplied the raw audio; it is never synthesised
        # here, only converted.
        source = Path(unit['local']['path']).resolve(strict=True)
    elif kind == 'parallel':
        source = None
        for route in unit['routes']:
            audio = folder / ('original.%s.ogg' % route)
            if not audio.exists():
                with gates.gate(route):
                    synthesize_role(route, unit['text'], unit['role'], audio, unit['voice'])
            if route == unit['timeline_route']:
                source = audio
    else:
        # Route-specific file name: two routes synthesise the same word with
        # different containers, so a shared name made them overwrite each other
        # and trip the cache check.
        route = {'volcengine_original': 'volcengine_legacy',
                 'jianying_original': 'jianying'}.get(kind, 'volcengine_sse')
        source = folder / ('original.%s' % route)
        if not source.exists():
            with gates.gate(route):
                synthesize_role(route, unit['text'], unit['role'], source,
                                unit['voice'], unit['spec'].get('resource'))
    with gates.gate('local'):
        raw, rendered = prepare_audio(source, prepared, lesson.speed, lesson.fps)
    atomic_json(record, {'spec': unit['spec'], 'raw': raw, 'rendered': rendered,
                         'audio_digest': sha256(prepared)})
    return {'raw': raw, 'rendered': rendered}


def _reference_voices(provider, kind):
    """The three ids a reference-voice route will speak with.

    The built-in originals are the default.  A caller may name others - the
    voices the user actually picked in Jianying, found by ``word_video.voices`` -
    but only as a complete set that was explicitly auditioned.  A partial or
    unconfirmed set is refused rather than completed by guessing, and a refused
    id fails the job instead of being replaced by a similar voice.
    """
    from .original_tts import VOICES
    fixed = {role: item['speaker'] for role, item in VOICES.items()}
    chosen = provider.get('voices') or {}
    if chosen:
        if set(chosen) != set(ROLES):
            raise ValueError('provider.voices must name all of: ' + ', '.join(ROLES))
        for role, speaker in chosen.items():
            if not isinstance(speaker, str) or not speaker.strip():
                raise ValueError('provider.voices.%s must be a non-empty id' % role)
        if provider.get('voices_confirmed') is not True:
            raise ValueError('Audition the chosen voice IDs before batch synthesis '
                             '(' + ', '.join('%s=%s' % item for item in sorted(chosen.items())) + ')')
        return {role: speaker.strip() for role, speaker in chosen.items()}
    if provider.get('voices_confirmed') is not True:
        raise ValueError('Audition all three original IDs before batch synthesis')
    return dict(fixed)


def build_speech(lesson, provider, cache, checkpoint=lambda: None, progress=None):
    """Local items: index, role, text, voice, path (original unscaled audio).

    ``volcengine`` voices are speaker ids the account is entitled to.  That
    includes voices the user created themselves in the service console, e.g. a
    cloned voice made from the original cached audio when the fixed 剪映 ids are
    not purchasable.  Whatever id is approved is used verbatim: a rejected id
    fails the job instead of falling back to a similar-sounding voice.

    ``jianying_original`` uses the locally installed JianyingPro client's own
    reading channel, which carries the reference voice ids directly.  It is a
    separate opt-in route (see ``word_video/jianying_tts.py``): callers must ask
    for it explicitly, and an entitled/refused voice fails the job rather than
    being replaced.  Kept independent so the official route cannot change
    behaviour because of it.

    Concurrency: items are worked on in parallel up to ``provider.concurrency``
    (default 1, the original serial behaviour), bounded per route and for local
    work by :data:`ROUTE_CONCURRENCY`.  Tokens, cache layout and the side-by-side
    route files are identical to a serial run, so a cache written either way
    stays reusable.
    """
    kind = provider.get('kind')
    if kind not in ('local', 'volcengine', 'volcengine_original', 'jianying_original',
                    'volcengine_bigmodel', 'parallel'):
        raise ValueError('Unknown speech provider')
    voices = provider.get('voices', {})
    routes = []
    timeline_route = None
    if kind in ('volcengine_original', 'jianying_original'):
        voices = _reference_voices(provider, kind)
    if kind == 'parallel':
        routes = list(provider.get('routes') or ())
        if not routes or any(r not in ROUTES for r in routes):
            raise ValueError('parallel requires routes drawn from ' + ', '.join(ROUTES))
        if sorted(routes) != ['jianying', 'volcengine_legacy']:
            raise ValueError('parallel currently supports jianying + volcengine_legacy')
        voices = _reference_voices(provider, kind)
        # Which route's audio feeds the timeline is an explicit choice: the
        # preferred route is used first, and any other configured route is kept
        # only as side-by-side material (never as a silent substitute).
        preferred = provider.get('timeline_route') or routes[0]
        if preferred not in routes:
            raise ValueError('parallel timeline_route must be one of ' + ', '.join(routes))
        timeline_route = preferred
    if kind == 'volcengine' and (provider.get('voices_confirmed') is not True
                                 or set(voices) != set(ROLES)):
        raise ValueError('Approve and configure all three voice IDs before batch synthesis')
    if kind == 'volcengine_bigmodel':
        # New-console route: an API key plus a resource id that selects the model
        # generation.  Speaker names are the caller's choice, so the job must
        # state that they were auditioned against this resource.
        if provider.get('voices_confirmed') is not True or set(voices) != set(ROLES):
            raise ValueError('Approve and configure all three voice IDs before batch synthesis')
        if provider.get('resource') not in BIGMODEL_RESOURCES:
            raise ValueError('volcengine_bigmodel resource must be one of '
                             + ', '.join(BIGMODEL_RESOURCES))
    local = {}
    for item in provider.get('items', []):
        key = (item['index'], item['role'])
        if key in local:
            raise ValueError('Duplicate local speech asset')
        local[key] = item
    cache = Path(cache).resolve()
    # Validate the scheduling limits before touching the filesystem: a rejected
    # request must not leave a half-created cache behind.
    limits = _provider_limits(provider, routes)
    cache.mkdir(parents=True, exist_ok=True)
    units, plan = _speech_plan(lesson, kind, voices, routes, timeline_route,
                               provider.get('resource', 'seed-tts-2.0'), local, cache)
    gates = _Gates(limits['routes'], limits['local'])
    results = [None] * len(units)
    workers = min(limits['items'], max(1, len(units)))
    if progress:
        progress({'stage': 'speech', 'done': 0, 'total': len(units),
                  'concurrency': limits['items'], 'route_limits': limits['routes'],
                  'local_limit': limits['local']})
    if workers == 1:
        for position, unit in enumerate(units):
            results[position] = _run_unit(unit, lesson, gates, checkpoint)
            if progress and (position + 1) % 10 == 0:
                progress({'stage': 'speech', 'done': position + 1, 'total': len(units)})
    else:
        finished = [0]
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_unit, unit, lesson, gates, checkpoint): index
                       for index, unit in enumerate(units)}
            try:
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    with lock:
                        finished[0] += 1
                        if progress and finished[0] % 10 == 0:
                            progress({'stage': 'speech', 'done': finished[0],
                                      'total': len(units)})
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    if progress:
        progress({'stage': 'speech', 'done': len(units), 'total': len(units)})
    assets, position = [], 0
    for word in lesson.entries:
        for role in ROLES:
            unit = units[plan[position]]
            outcome = results[plan[position]]
            position += 1
            assets.append(SpeechAsset(word.index, role, unit['text'],
                                      str(unit['folder'] / 'prepared.wav'),
                                      outcome['raw'], outcome['rendered'], unit['voice']))
    return assets
