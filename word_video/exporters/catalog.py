"""Where a project's assets actually live, and what they measure.

A project document carries *identities* (``asset_id``), never paths: the same
project must open on another machine, and a path stored in the document is a path
that can go stale.  The catalogue beside the document supplies the other half -
``{asset_id: path}`` - and this module turns each entry into the exact
:class:`~word_video.domain.model.MediaInfo` the solver needs.

Two rules make it exact rather than approximate:

* measurement happens on the **source's own grid**.  A 44.1 kHz file is measured
  in samples at 44100, not converted to 48000 first, so 4410 samples are exactly
  0.1 s and 72000 ticks with no float in the path;
* an asset that cannot be read is an **error**, never a guess.  The solver already
  refuses a stage with no measured media, and an estimated duration must never
  become a plan.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from ..domain.model import MediaInfo
from ..domain.timebase import finite_number

CATALOG_FILENAME = 'media.json'


class CatalogError(Exception):
    """The catalogue or one of its assets cannot be used."""


@dataclass(frozen=True)
class Asset:
    """One catalogue entry: the file and the role it plays."""

    asset_id: str
    path: str
    voice: str = ''

    def to_dict(self):
        return {'asset_id': self.asset_id, 'path': self.path, 'voice': self.voice}


def load_catalog(path):
    """Read ``media.json`` beside the project (or the file given directly)."""
    source = Path(path)
    if source.is_dir():
        source = source / CATALOG_FILENAME
    if not source.is_file():
        raise CatalogError('no media catalogue at %s' % source)
    try:
        value = json.loads(source.read_text(encoding='utf-8'))
    except ValueError as error:
        raise CatalogError('media catalogue is not valid JSON: %s' % error) from None
    items = value.get('assets') if isinstance(value, dict) else None
    if not isinstance(items, list) or not items:
        raise CatalogError('media catalogue must carry a non-empty "assets" list')
    base = source.parent
    assets = {}
    for item in items:
        if not isinstance(item, dict):
            raise CatalogError('media catalogue entries must be objects')
        unknown = sorted(set(item) - {'asset_id', 'path', 'voice'})
        if unknown:
            raise CatalogError('unknown catalogue field(s): %s' % ', '.join(unknown))
        for required in ('asset_id', 'path'):
            if not isinstance(item.get(required), str) or not item[required]:
                raise CatalogError('media catalogue needs %s for every asset' % required)
        found = Path(item['path'])
        resolved = found if found.is_absolute() else (base / found)
        assets[item['asset_id']] = Asset(asset_id=item['asset_id'],
                                         path=str(resolved), voice=item.get('voice', ''))
    return assets


def save_catalog(assets, path):
    """Write the catalogue atomically (a half-written one would fail every solve)."""
    target = Path(path)
    if target.is_dir():
        target = target / CATALOG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(assets.values(), key=lambda asset: asset.asset_id)
    text = json.dumps({'schema': 'wv-media@1',
                       'assets': [asset.to_dict() for asset in ordered]},
                      ensure_ascii=False, indent=2) + '\n'
    temporary = target.with_name('.%s.tmp' % target.name)
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    return target


def measure(asset):
    """Probe one asset into a :class:`MediaInfo` on its own grid."""
    from ..media import duration, has_audio, probe
    from ..media.streams import audio_sample_count

    path = Path(asset.path)
    if not path.is_file():
        raise CatalogError('asset %s is missing: %s' % (asset.asset_id, path))
    if has_audio(path):
        facts = audio_sample_count(path)
        if facts['samples'] <= 0:
            raise CatalogError('asset %s has no usable audio' % asset.asset_id)
        return MediaInfo(asset.asset_id, int(facts['samples']), 1,
                         int(facts['sample_rate']))
    # Picture-only assets (a background, a clip) are measured in their own frames.
    streams = [item for item in probe(path).get('streams', [])
               if item.get('codec_type') == 'video']
    if not streams:
        raise CatalogError('asset %s has neither audio nor video' % asset.asset_id)
    rate = str(streams[0].get('avg_frame_rate') or '0/1')
    number, _, denominator = rate.partition('/')
    frames = streams[0].get('nb_frames')
    if frames and float(number or 0) and float(denominator or 0):
        return MediaInfo(asset.asset_id, int(float(frames)), int(float(denominator)),
                         int(float(number)))
    seconds = finite_number(duration(path), 'asset duration', positive=True)
    if not seconds:
        raise CatalogError('asset %s has no usable duration' % asset.asset_id)
    return MediaInfo(asset.asset_id, int(round(seconds * 1000)), 1, 1000)


def measure_all(assets, only=None):
    """Measure every asset (or the named ones) into ``{asset_id: MediaInfo}``."""
    table = {}
    for asset_id, asset in assets.items():
        if only is not None and asset_id not in only:
            continue
        table[asset_id] = measure(asset)
    return table
