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

__all__ = ['read_request', 'resolve_wordlist', 'read_entries', 'records_for',
           'assets_for', 'import_request']

#: Every refusal below is a ``ValueError`` whose message names the file or the
#: index that is wrong, because that is what the import dialog shows the member.
ImportError_ = ValueError


def read_request(path):
    """The request JSON, with the encoding the fixtures are written in."""
    source = Path(path)
    if not source.is_file():
        raise ImportError_('请求文件不存在：%s' % source)
    try:
        value = json.loads(source.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        raise ImportError_('请求文件不是合法 JSON：%s（%s）' % (source, error)) from None
    if not isinstance(value, dict):
        raise ImportError_('请求文件必须是一个 JSON 对象：%s' % source)
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
        raise ImportError_('请求里没有 source.path，无法确定词表')
    path = Path(named)
    if path.is_file():
        return path
    local = work_root() / 'data' / 'wordlists' / path.name
    if local.is_file():
        return local
    raise ImportError_('词表不存在：%s（本地也没有 %s）' % (path, local))


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
                               % (len(lines), index, wordlist))
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
        raise ImportError_('请求里没有 provider.items，无法知道配音在哪里')
    grouped = {}
    for item in items:
        if not isinstance(item, dict) or 'index' not in item or 'role' not in item:
            raise ImportError_('provider.items 的每一项都要有 index 与 role')
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
                raise ImportError_('第 %d 个词的 %s 没有配音路径' % (index, role))
            assets['w%d:%s' % (index, role)] = (path, str(item.get('voice') or ''))
    return assets


def registry_for(assets):
    """Register and measure every speech asset on its own source grid.

    Measured here rather than left for later because the solver needs the raw
    length to expand the template, and a missing or unreadable file has to fail the
    import with the path named - not silently produce a project that cannot solve.
    """
    refs, media = [], {}
    for asset_id, (path, voice) in sorted(assets.items()):
        info = catalog_module.measure(Asset(asset_id=asset_id, path=path, voice=voice))
        refs.append(AssetRef(asset_id=asset_id, path=path, units=info.units,
                             unit_num=info.unit_num, unit_den=info.unit_den,
                             kind='audio'))
        media[asset_id] = info
    return tuple(refs), media


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
        raise ImportError_('片头文件不存在：%s' % path)
    measurement = measure_intro(path, fallback_audio=intro_audio, clip_id='layer.intro')
    seconds = float(measurement.picture_seconds or measurement.seconds)
    if not seconds > 0:
        raise ImportError_('片头没有可用的画面长度：%s' % path)
    units = max(1, int(round(seconds * 1000)))
    ref = AssetRef(asset_id=path, path=path, units=units, unit_num=1, unit_den=1000,
                   kind='video')
    slice_ = MediaSlice(asset_id=path, source_start=0, source_end=units,
                        unit_num=1, unit_den=1000)
    return ref, slice_, measurement


def import_request(request_path, target_dir, *, indices=None, project_id='', fps=None):
    """Expand a request into an editable project folder; no network, no synthesis.

    ``indices`` selects a subset of the request's words (1-based, as everywhere in
    the app); the default is every word the request carries.
    """
    request = read_request(request_path)
    grouped = _items(request)
    lesson = request.get('lesson') if isinstance(request.get('lesson'), dict) else {}
    records = records_for(request, grouped, indices)
    assets = assets_for(grouped, indices)
    refs, media = registry_for(assets)

    intro = lesson.get('intro') if isinstance(lesson.get('intro'), dict) else {}
    intro_audio = str(lesson.get('intro_audio') or '')
    intro_ref, intro_slice, intro_measure = intro_layer(str(intro.get('video') or ''),
                                                        intro_audio)
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
    project = expand(template, records, media, base, intro=intro_slice,
                     intro_measure=intro_measure)
    if intro_ref is not None:
        refs = refs + (intro_ref,)
    voices = {asset_id: voice for asset_id, (_, voice) in assets.items()}
    if intro_ref is not None and intro_audio:
        voices[intro_ref.asset_id] = intro_audio
    delivery = Delivery(background=str(lesson.get('background') or ''),
                        video_codec=str(lesson.get('video_codec') or 'h264'))
    folder = ProjectFolder.create(target_dir, project, refs, voices, delivery)
    folder._measured = True                     # every ref was just measured
    return folder
