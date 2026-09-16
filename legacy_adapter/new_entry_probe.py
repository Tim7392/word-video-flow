"""Run the **new** entry on a minimal project, and report what came of it.

This is the other half of the failure-isolation evidence.  Showing that a failing old
factory leaves the new software usable needs a *new* run to point at, and showing that
a failing new run leaves the old factory usable needs a new run that fails the way a
member's would - a media file that is not there.  Both are cheap on one small project,
and both must happen in a process of their own, which is why this is a module with a
CLI rather than a helper inside the isolation script.

It is glue, not business: the project is built from the fixture's own nine cached
recordings (three words x three roles) through the engine's public API - ``Record``,
``instantiate``, ``AssetIndex``/``AssetRef`` (A's ``assets.json`` registry, the one
document that replaced the ``media.json`` bridge), ``save_project`` and B's
``export_run`` - and the five tracks are written by that exporter, not here.  The
probe adds no timing, no text and no layout of its own; its three words are a
plumbing check, not a teaching sample.

    python -m legacy_adapter.new_entry_probe --area D:\\...\\out\\legacy\\probe
    python -m legacy_adapter.new_entry_probe --area ... --break-asset w151:female

One JSON object on stdout either way: ``ok: true`` with the five published tracks and
their hashes, or ``ok: false`` with the failure's own ``code``/``path``/``hint`` (the
engine's structured refusals) plus the traceback tail.  Exit code 0 on success, 2 on a
refusal (a ``ProjectError``, a bad value or a missing file), 1 on anything else.
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from .environment import work_root
from .report import parse_track, read_text, sha256
from .runner import write_json

SCHEMA = 'wv-new-entry-probe@1'

#: The delivered three-word fixture: 3 records x 3 roles, all nine recordings cached
#: in the read-only archive, which is why this probe needs no TTS and no network.
DEFAULT_FIXTURE = work_root() / 'data' / 'fixtures' / 'p1-有片头.json'
#: The canvas stays small: nothing about the export chain depends on the size.
CANVAS = {'width': 320, 'height': 180, 'fps_num': 24, 'speed': 1.25}


def _items(fixture):
    """The fixture's local-provider items, grouped ``{index: {role: item}}``."""
    document = json.loads(Path(fixture).read_text(encoding='utf-8'))
    grouped = {}
    for item in (document.get('provider') or {}).get('items') or ():
        grouped.setdefault(int(item['index']), {})[str(item['role'])] = item
    if not grouped:
        raise ValueError('夹具里没有 provider.items（本地配音条目）：%s' % fixture)
    return grouped


def build_project(fixture, area, *, break_asset=''):
    """Write a minimal project and its registry; optionally break one media path.

    The registry is A's ``assets.json`` on purpose: it is the document the editor and
    the exporter now share, and the "missing media" case is exactly the one a member
    hits when a recording was moved - ``AssetFileError`` naming the asset and the path.
    """
    from word_video.application import instantiate
    from word_video.domain import Project, Record
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.exporters import catalog
    from word_video.storage.assets import SCHEMA as ASSET_REGISTRY_SCHEMA, AssetIndex, AssetRef
    from word_video.storage.project_store import save_project

    area = Path(area)
    project_dir = area / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)
    grouped = _items(fixture)
    records, assets = [], []
    for index in sorted(grouped):
        roles = grouped[index]
        record_id = 'w%d' % index
        # The fixture carries the two texts a record needs (English and Chinese); it
        # carries no phonetic, so track 03 is empty here by construction.
        records.append(Record(id=record_id, word=roles['female']['text'], phonetic='',
                              meaning=roles['chinese']['text'],
                              spoken_meaning=roles['chinese']['text'], index=index))
        for role in ('female', 'male', 'chinese'):
            assets.append(catalog.Asset(asset_id='%s:%s' % (record_id, role),
                                        path=roles[role]['path'], voice=roles[role]['voice']))
    catalogue = {asset.asset_id: asset for asset in assets}
    media = catalog.measure_all(catalogue)
    project = instantiate(DEFAULT_LESSON_TEMPLATE, tuple(records), media,
                          Project(project_id='legacy-isolation-probe', intro_s=0.0,
                                  fps_num=CANVAS['fps_num'], width=CANVAS['width'],
                                  height=CANVAS['height'], speed=CANVAS['speed']))
    save_project(project, project_dir)
    index = AssetIndex(folder=str(project_dir))
    broken = []
    for asset in assets:
        path, info = asset.path, media[asset.asset_id]
        if break_asset and asset.asset_id == str(break_asset):
            path = str(project_dir / ('missing-%s.ogg' % asset.asset_id.replace(':', '-')))
            broken.append({'asset_id': asset.asset_id, 'registered_path': path,
                           'was': asset.path, 'exists': Path(path).is_file()})
        index = index.with_ref(AssetRef(asset_id=asset.asset_id, path=path, units=info.units,
                                        unit_num=info.unit_num, unit_den=info.unit_den,
                                        kind='audio', voice=asset.voice))
    index.save(project_dir)
    return {'project_dir': str(project_dir), 'registry_schema': ASSET_REGISTRY_SCHEMA,
            'records': len(records), 'assets': len(assets),
            'record_ids': [record.id for record in records], 'broken': broken,
            'project': str(project_dir / 'project.json'),
            'assets_file': str(project_dir / 'assets.json')}


def _tracks(paths):
    """What each published track is, read back from disk by this side of the fence."""
    rows = []
    for path in paths:
        target = Path(path)
        cues = parse_track(read_text(target)) if target.is_file() else []
        rows.append({'name': target.name, 'path': str(target), 'exists': target.is_file(),
                     'bytes': target.stat().st_size if target.is_file() else None,
                     'sha256': sha256(target) if target.is_file() else None,
                     'cues': len(cues),
                     'first_text': cues[0]['text'].splitlines()[0] if cues and cues[0]['text'] else '',
                     'last_end_ms': cues[-1]['end_ms'] if cues else None})
    return rows


def structured(error):
    """One failure in the project's shape: code, path, hint, message, traceback tail."""
    return {'type': type(error).__name__, 'code': str(getattr(error, 'code', '') or ''),
            'path': str(getattr(error, 'path', '') or ''),
            'hint': str(getattr(error, 'hint', '') or ''),
            'message': str(error), 'traceback': traceback.format_exc()[-2000:]}


def refusal(error):
    """Is this a refusal the caller should read as one (exit 2), or a surprise (1)?"""
    from word_video.domain.errors import ProjectError
    return isinstance(error, (ProjectError, ValueError, OSError, RuntimeError))


def probe(*, area, fixture=DEFAULT_FIXTURE, break_asset='', render=False, draft=False):
    """Build the project, export it, and return the one document this probe publishes."""
    from word_video.exporters import export_run

    started = time.perf_counter()
    document = {'schema': SCHEMA, 'action': 'new-entry-probe', 'area': str(area),
                'fixture': str(fixture), 'break_asset': str(break_asset or ''),
                'render': bool(render), 'draft': bool(draft),
                'python': sys.executable, 'frozen': bool(getattr(sys, 'frozen', False)),
                'ok': False, 'seconds': 0.0}
    try:
        document['project'] = build_project(fixture, area, break_asset=break_asset)
        output = Path(area) / 'out'
        result = export_run(str(Path(document['project']['project_dir'])), output=output,
                            render=render, draft=draft)
        document.update({'ok': True, 'output': str(output), 'run_dir': str(result.run_dir),
                         'video': str(result.video), 'draft_dir': str(result.draft),
                         'timeline': str(result.timeline),
                         'tracks': _tracks(result.srts),
                         'report': {'asset_source': result.report.get('asset_source'),
                                    'plan_identity': result.report.get('plan_identity'),
                                    'cue_identity': result.report.get('cue_identity'),
                                    'conflicts': result.report.get('conflicts')}})
    except Exception as error:                          # noqa: BLE001 - one JSON always
        document['error'] = structured(error)
        document['exit_code'] = 2 if refusal(error) else 1
    document.setdefault('exit_code', 0 if document['ok'] else 1)
    document['seconds'] = round(time.perf_counter() - started, 3)
    return document


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m legacy_adapter.new_entry_probe',
        description='用新软件跑一个最小工程（3 词 × 3 角色，本地缓存音频），'
                    '成功则给出五轨 SRT 与其哈希，失败则给出结构化错误。')
    parser.add_argument('--area', required=True, help='本次运行的新目录（工程与输出都写在里面）')
    parser.add_argument('--fixture', default=str(DEFAULT_FIXTURE), help='请求夹具（默认 p1-有片头）')
    parser.add_argument('--break-asset', default='',
                        help='把某个素材登记到一个不存在的文件上，例如 w151:female（缺媒体反例）')
    parser.add_argument('--render', action='store_true', help='同时渲染 MP4（默认只出五轨 SRT）')
    parser.add_argument('--draft', action='store_true', help='同时产出剪映草稿（默认不产）')
    parser.add_argument('--report', default='', help='把这份 JSON 同时写到这个文件')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    document = probe(area=args.area, fixture=args.fixture, break_asset=args.break_asset,
                     render=args.render, draft=args.draft)
    if args.report:
        write_json(args.report, document)
        document['report_file'] = str(Path(args.report))
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(json.dumps(document, ensure_ascii=False))
    return int(document['exit_code'])


if __name__ == '__main__':
    raise SystemExit(main())
