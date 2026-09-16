"""Create a project from a word list and the audio that already exists for it.

This is the "导入" half of the editor, and it deliberately does *not* synthesise
anything: it takes a request (the same JSON the CLI submits), the words it names,
and the audio files the request points at, measures those files, and expands A's
template into a project on disk.  A member who already has 剪映 audio for a word
list therefore gets an editable project with zero network calls - which is the
"优先已有音频" rule, not a convenience.

What is authoritative for what
------------------------------
* the **word list** supplies the display fields (phonetic, meaning);
* the **provider item's own text** supplies the spoken English word and the spoken
  Chinese meaning, because that is the text the file in hand actually says.  When
  the two differ (A's v2 cleaning removed a derived connector, say) the audio is
  right and the caption has to match the audio, not the other way round;
* **`MediaInfo` describes the raw source**, measured on its own grid: that is the
  grid the solver divides by ``speed`` to get a timeline length.  The file the
  preview plays is prepared separately
  (:meth:`desktop.editor_project.ProjectFolder.preview_assets`) and the file the
  exporter mixes is prepared by B's exporter.  Feeding a prepared file here would
  apply the tempo twice.

The background and the intro are **delivery settings**, not project content: B's
exporter takes them as parameters (``export_run(background=..., intro_video=...)``)
and never reads a background clip, so import records them in ``delivery.json``
where the exporter will actually see them.
"""
from dataclasses import replace
from pathlib import Path
import json
import time

from word_video.application import expand
from word_video.application.intro import measure_intro
from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
from word_video.domain.model import MediaSlice, Project, Record
from word_video.domain.rhythm import (DEFAULT_FOOTER, DEFAULT_FPS_NUM, DEFAULT_INTRO_S,
                                      DEFAULT_TITLE)
from word_video.exporters import catalog as catalog_module
from word_video.exporters.catalog import Asset
from word_video.storage.assets import AssetRef

from .editor_project import Delivery, ProjectFolder

__all__ = ['ImportFailure', 'read_request', 'resolve_wordlist', 'read_entries',
           'records_for', 'assets_for', 'import_request']


class ImportFailure(ValueError):
    """A refusal a member can act on: what is wrong, and what to change.

    ``ValueError`` because every caller already catches that, and a separate ``fix``
    because "导入失败" without the next step is the report H0 could not act on: the
    window's notice card shows ``fix`` and the driven run writes both into its JSON.
    """

    def __init__(self, message, fix=''):
        super().__init__(message)
        self.fix = fix


#: Every refusal below names the file or the index that is wrong, plus the change
#: that fixes it, because that is what the import dialog shows the member.
ImportError_ = ImportFailure

DEFAULT_FIX = '检查请求 JSON、词表路径、provider.items 里的配音文件与片头/背景是否都在本机'


def read_request(path):
    """The request JSON, with the encoding the fixtures are written in."""
    source = Path(path)
    if not source.is_file():
        raise ImportError_('请求文件不存在：%s' % source,
                           '把请求 JSON 放到这个路径，或在文件选择框里重新选一次')
    try:
        value = json.loads(source.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        raise ImportError_('请求文件不是合法 JSON：%s（%s）' % (source, error),
                           '用 UTF-8 重新保存这个 JSON（编辑器不接受带注释或多对象的文件）') from None
    if not isinstance(value, dict):
        raise ImportError_('请求文件必须是一个 JSON 对象：%s' % source,
                           '请求的顶层要是一个对象，含 source / lesson / provider 三个字段')
    return value


def work_root():
    """The work root above this checkout, by the same rule the packaging uses."""
    checkout = Path(__file__).resolve().parents[1]
    for candidate in list(checkout.parents)[:4]:
        if (candidate / 'runtime').is_dir() and (candidate / 'worktrees').is_dir():
            return candidate
    return checkout.parent


def resolve_wordlist(request):
    """The word list the request names, falling back to this work root's copy.

    The request stores the path the batch was built on, which is often another
    machine's.  The name is what matters, and the work root keeps the cleaned list
    under ``data/wordlists``.
    """
    named = ((request.get('source') or {}) if isinstance(request.get('source'), dict)
             else {}).get('path')
    if not named:
        raise ImportError_('请求里没有 source.path，无法确定词表',
                           '在请求 JSON 里补上 source.path（词表文件路径），'
                           '例如 "source": {"path": "D:\\\\...\\\\四级核心1500词_已清理.txt"}')
    path = Path(named)
    if path.is_file():
        return path
    local = work_root() / 'data' / 'wordlists' / path.name
    if local.is_file():
        return local
    raise ImportError_('词表不存在：%s（本地也没有 %s）' % (path, local),
                       '把词表放到上面两个路径之一，或把 source.path 改成这台机器上的词表路径')


def read_entries(wordlist, indices):
    """``{index: (word, phonetic, meaning)}``; ``index`` is 1-based, as in the app.

    One entry per line, ``word /phonetic/ meaning``; the request's ``range`` counts
    *lines*, which is why entry 151 is line 150.
    """
    lines = [line for line in Path(wordlist).read_text(encoding='utf-8-sig').splitlines()
             if line.strip()]
    entries = {}
    for index in indices:
        if index < 1 or index > len(lines):
            raise ImportError_('词表只有 %d 行，取不到第 %d 个词（%s）'
                               % (len(lines), index, wordlist),
                               '把 range 改到词表的行数以内，或换一个词表文件')
        head, _, rest = lines[index - 1].strip().partition(' ')
        phonetic, meaning = '', rest.strip()
        if meaning.startswith('/') and '/' in meaning[1:]:
            inner, _, tail = meaning[1:].partition('/')
            phonetic, meaning = '/' + inner + '/', tail.strip()
        entries[index] = (head, phonetic, meaning)
    return entries


def _items(request):
    provider = request.get('provider') or {}
    items = provider.get('items') if isinstance(provider, dict) else None
    if not isinstance(items, list) or not items:
        raise ImportError_('请求里没有 provider.items，无法知道配音在哪里',
                           '请求要在 provider.items 里给出每个词的配音：'
                           '每项含 index / role / path（provider.kind 为 local 时用已有音频）')
    grouped = {}
    for item in items:
        if not isinstance(item, dict) or 'index' not in item or 'role' not in item:
            raise ImportError_('provider.items 的每一项都要有 index 与 role',
                               '缺字段的那一项补上 index（第几个词）与 role'
                               '（female / male / chinese）')
        grouped.setdefault(int(item['index']), {})[str(item['role'])] = item
    return grouped


def records_for(request, grouped, indices=None):
    """The frozen word entries, with the spoken text taken from the audio's side."""
    chosen = sorted(grouped) if indices is None else sorted(set(int(item) for item in indices))
    entries = read_entries(resolve_wordlist(request), chosen)
    records = []
    for index in chosen:
        word, phonetic, meaning = entries[index]
        roles = grouped[index]
        spoken_word = str((roles.get('female') or {}).get('text') or word)
        spoken_meaning = str((roles.get('chinese') or {}).get('text') or meaning)
        records.append(Record(id='w%d' % index, word=spoken_word, phonetic=phonetic,
                              meaning=meaning, spoken_meaning=spoken_meaning,
                              index=index))
    return tuple(records)


def assets_for(grouped, indices=None):
    """``{asset_id: (path, voice)}`` from the provider items, in a stable order."""
    chosen = sorted(grouped) if indices is None else sorted(set(int(item) for item in indices))
    assets = {}
    for index in chosen:
        for role, item in sorted(grouped[index].items()):
            path = str(item.get('path') or '')
            if not path:
                raise ImportError_('第 %d 个词的 %s 没有配音路径' % (index, role),
                                   '给这一项补上 path，或把 provider.kind 改成能取到音频的方式')
            assets['w%d:%s' % (index, role)] = (path, str(item.get('voice') or ''))
    return assets


def registry_for(assets, timings=None, workers=None):
    """Register and measure every speech asset on its own source grid.

    Measured here rather than left for later because the solver needs the raw
    length to expand the template, and a missing or unreadable file has to fail the
    import with the path named - not silently produce a project that cannot solve.

    The measuring is **parallel** (``workers`` threads) because one probe is one
    short-lived ``ffprobe``/MediaInfo process and a 50-word lesson has 150 of them:
    measured 2026-09-16, 150 sequential probes cost 103 s of a 107 s import.  The
    results are deterministic all the same - the table is assembled in ``asset_id``
    order and the first failure *in that order* is the one reported - so a parallel
    import refuses exactly what a sequential one refused, with the same asset named.
    """
    from concurrent.futures import ThreadPoolExecutor

    ordered = sorted(assets.items())
    workers = default_measure_workers(len(ordered)) if workers is None else int(workers)
    started = time.monotonic()
    measured = {}
    if workers <= 1 or len(ordered) <= 1:
        for asset_id, (path, voice) in ordered:
            try:
                measured[asset_id] = catalog_module.measure(
                    Asset(asset_id=asset_id, path=path, voice=voice))
            except Exception as error:                  # noqa: BLE001 - reported below
                raise measurement_error(asset_id, path, error) from None
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='measure') as pool:
            pending = {pool.submit(catalog_module.measure,
                                   Asset(asset_id=asset_id, path=path, voice=voice)):
                       (asset_id, path)
                       for asset_id, (path, voice) in ordered}
            failures = []
            for future, (asset_id, path) in pending.items():
                try:
                    measured[asset_id] = future.result()
                except Exception as error:              # noqa: BLE001 - reported below
                    failures.append((asset_id, path, error))
        if failures:
            asset_id, path, error = sorted(failures, key=lambda item: item[0])[0]
            raise measurement_error(asset_id, path, error) from None
    refs, media = [], {}
    for asset_id, (path, voice) in ordered:
        info = measured[asset_id]
        refs.append(AssetRef(asset_id=asset_id, path=path, units=info.units,
                             unit_num=info.unit_num, unit_den=info.unit_den,
                             kind='audio'))
        media[asset_id] = info
    _record(timings, '③ 逐资产探测', started,
            {'assets': len(ordered), 'workers': workers})
    return tuple(refs), media


def measurement_error(asset_id, path, error):
    """The media layer's own refusal, plus which file to look at and what to do.

    The exception *type* is kept: a caller that catches ``CatalogError`` today keeps
    working, and the media layer's message already names the asset.  What this adds is
    the actionable half - the file on disk and the fix - as a ``fix`` attribute the
    window's notice card and the driven report both read.
    """
    try:
        error.fix = ('确认这条配音存在且能播放：%s（%s）；缺文件就从旧工程或音频缓存里'
                     '补回来，或把该词的 provider.items[].path 指向另一条配音'
                     % (asset_id, path))
    except AttributeError:                          # an exception with __slots__
        return ImportError_('配音无法测量：%s（%s）→ %s' % (asset_id, path, error),
                            '确认这个文件存在且能播放；缺文件就从旧工程或音频缓存里补回来')
    return error


def default_measure_workers(count):
    """How many probes at once: enough to hide process spawn, not one per asset.

    Measured: each probe is process startup rather than CPU (a 1 s ogg measures in
    ~0.7 s wall, most of it spawn), so the lever is concurrency.  Eight keeps the
    machine usable for the member while the import runs and still removes most of the
    103 s; more workers stop helping once the disk is the queue.
    """
    if count <= 1:
        return 1
    return max(2, min(8, count))


def _record(timings, phase, started, extra=None):
    row = {'phase': phase, 'seconds': round(time.monotonic() - started, 3)}
    row.update(extra or {})
    if timings is not None:
        timings.append(row)
    return row


def intro_layer(intro_video, intro_audio=''):
    """The intro as a *clip*: its own media slice and the measurement it needs.

    Since B's W07 the exporter measures a project's intro layer from that layer
    (``measure_project_intro``) and ignores the ``--intro-video`` parameter, so the
    editor creates the layer rather than passing a delivery argument - one source of
    truth for the countdown, and the countdown is editable like anything else.

    The asset id **is the file path**: the measurement and the manifest's
    ``intro_video`` both treat it as the file to open, which is the id-as-path
    fallback ``wv-assets@1`` documents.  Keeping the path here is what lets the
    renderer find the countdown without a second resolver in the export path.

    The window is built on the **picture**, in milliseconds, from the same
    measurement the exporter uses.  ``catalog.measure`` is deliberately *not* asked:
    it prefers the audio stream, and for a packetised codec it reports the packet
    count as a sample count - the reference countdown's AAC track (1.856 s, 89 088
    samples) comes back as 88 "samples", a window 1 000 times too short to cut
    (reported to H0).  A video layer's window is its picture, and the picture
    length is already the resolved number.
    """
    path = str(intro_video or '').strip()
    if not path:
        return None, None, None
    if not Path(path).is_file():
        raise ImportError_('片头文件不存在：%s' % path,
                           '把 lesson.intro.video 指向存在的片头文件，或删掉 intro 字段'
                           '（没有片头也能出片）')
    measurement = measure_intro(path, fallback_audio=intro_audio, clip_id='layer.intro')
    seconds = float(measurement.picture_seconds or measurement.seconds)
    if not seconds > 0:
        raise ImportError_('片头没有可用的画面长度：%s' % path,
                           '换一个能读出画面长度的片头文件（片头时长只能来自媒体）')
    units = max(1, int(round(seconds * 1000)))
    ref = AssetRef(asset_id=path, path=path, units=units, unit_num=1, unit_den=1000,
                   kind='video')
    slice_ = MediaSlice(asset_id=path, source_start=0, source_end=units,
                        unit_num=1, unit_den=1000)
    return ref, slice_, measurement


def import_request(request_path, target_dir, *, indices=None, project_id='', fps=None,
                   timings=None, workers=None):
    """Expand a request into an editable project folder; no network, no synthesis.

    ``indices`` selects a subset of the request's words (1-based, as everywhere in
    the app); the default is every word the request carries.

    ``timings`` is an optional list the four phases are appended to (request read,
    word list, registration, probing, writing).  It exists because "import takes
    107 s" is not actionable and "probing 150 assets takes 103 s of it" is; the
    numbers come from the code that does the work rather than from a stopwatch
    around it.
    """
    started = time.monotonic()
    request = read_request(request_path)
    _record(timings, '⓪ 读取请求 JSON', started, {'path': str(request_path)})

    started = time.monotonic()
    grouped = _items(request)
    _record(timings, '⓪b 解析 provider.items', started, {'items': sum(len(v) for v in grouped.values())})

    started = time.monotonic()
    lesson = request.get('lesson') if isinstance(request.get('lesson'), dict) else {}
    records = records_for(request, grouped, indices)
    _record(timings, '① 词表读取', started, {'records': len(records)})

    started = time.monotonic()
    assets = assets_for(grouped, indices)
    _record(timings, '② 资产登记', started, {'assets': len(assets)})

    refs, media = registry_for(assets, timings=timings, workers=workers)

    started = time.monotonic()
    intro = lesson.get('intro') if isinstance(lesson.get('intro'), dict) else {}
    intro_audio = str(lesson.get('intro_audio') or '')
    intro_ref, intro_slice, intro_measure = intro_layer(str(intro.get('video') or ''),
                                                        intro_audio)
    _record(timings, '③b 片头探测', started, {'intro': bool(intro_ref)})

    base = Project(
        project_id=project_id or ('p-%s' % Path(request_path).stem),
        speed=float(lesson.get('speed') or 1.25),
        fps_num=int(fps or lesson.get('fps') or DEFAULT_FPS_NUM),
        width=int(lesson.get('width') or 1920),
        height=int(lesson.get('height') or 1080),
        intro_s=float(lesson.get('intro_s') or DEFAULT_INTRO_S),
        title=str(lesson.get('title') or DEFAULT_TITLE),
        footer=str(lesson.get('footer') or DEFAULT_FOOTER),
    )
    template = replace(DEFAULT_LESSON_TEMPLATE, intro=True) if intro_slice is not None \
        else DEFAULT_LESSON_TEMPLATE
    started = time.monotonic()
    project = expand(template, records, media, base, intro=intro_slice,
                     intro_measure=intro_measure)
    _record(timings, '③c 模板展开（解算）', started, {'clips': len(project.clips)})

    if intro_ref is not None:
        refs = refs + (intro_ref,)
    voices = {asset_id: voice for asset_id, (_, voice) in assets.items()}
    if intro_ref is not None and intro_audio:
        voices[intro_ref.asset_id] = intro_audio
    delivery = Delivery(background=str(lesson.get('background') or ''),
                        video_codec=str(lesson.get('video_codec') or 'h264'))
    started = time.monotonic()
    folder = ProjectFolder.create(target_dir, project, refs, voices, delivery)
    _record(timings, '④ 写盘', started,
            {'bytes': sum(path.stat().st_size for path in Path(target_dir).glob('*.json'))})
    folder._measured = True                     # every ref was just measured
    folder.import_timings = list(timings or ())
    return folder
