"""Editable visual preset; normalized coordinates share the same two outputs.

Font selection is never a hard crash.  Preferred faces come from the reference
project (``喜鹊古字典体``/``喜鹊招牌体``) and live in Jianying's effect cache,
which Jianying purges on its own schedule; a purge used to abort a whole render.
``resolve_fonts`` therefore walks an explicit candidate list and reports the
choice, so a substituted face is visible in ``doctor`` and in the job result
instead of being silently swapped.  The reference look still wins whenever the
preferred file is present.

Because a substituted file has a different family name than the reference face,
the family name used by ASS is read out of the chosen file instead of being
hardcoded: a style naming a family that is not inside ``fontsdir`` makes libass
draw nothing at all.
"""
import math
import struct
from pathlib import Path

FONT_ROLES = ('heading', 'footer', 'bold', 'heavy', 'arial')

# B5 physical evidence (1920x1080): runtime/tmp/A/drafts/production-251-300/
# draft_cover.jpg, versus runtime/tmp/B/calib/ours-frame0.png, extracted from
# out/production/251-300/wv-b76c0d0ea16030c203876e93/0251-0300/video/video.mp4.
# Re-measured with runtime/tmp/B/calib/measure_runs.py (row gap <=4):
# RGB distance <=46 from #FFFF99, >=3 pixels per row. title/subtitle/footer ink height: cover 94/72/51 px,
# video 126/93/69 px; centres: cover 70.5/193.5/1009, video 84.5/203/1017.
# For each layer: new draft_size = old draft_size * video_height / cover_height;
# reference px per unit = style_size / new draft_size. No cross-face averaging.
# Sample faces: title/subtitle XQguzidianZJ, footer XQzhaopaitizj; source styles
# and original sizes 9/7/6 are in the adjacent word_video_manifest.json.
# These are one-sample linear fits, NOT post-change Jianying measurements.
# Fresh B5-final/draft readback: 12.0638297872/9.0416666667/8.1176470588;
# that verifies JSON serialization only, NOT post-change ink heights.
# TextStyle 0.3.0 writes floats unchanged; the old integer quantisation claim was
# incorrect. Font substitutions and teaching layers still need physical samples.
# Teaching/countdown coefficients below preserve the original default sizes;
# they are explicitly UNCALIBRATED, not a guessed application of the gold fit.
_DRAFT_PX_PER_UNIT = {
    'title': 315 * 94 / (9 * 126),
    'subtitle': 240 * 72 / (7 * 93),
    'footer': 210 * 51 / (6 * 69),
    'english': 450 / 13,
    'phonetic': 174 / 5,
    'meaning': 210 / 6,
    'countdown': 675 / 20,
}


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('Text size must be finite and positive') from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Text size must be finite and positive')
    return value


def draft_text_size(size, role):
    """Single style-size -> draft-size mapping, after project overrides merge.

    Role is required: the three measured layers must not calibrate other faces.
    Legacy ``draft_size`` fields are not an independent rendering authority.
    """
    return _positive(size) / _DRAFT_PX_PER_UNIT[role]


def style_size(draft_size, role):
    """Exact inverse, shared with future import consumers (no rounding/clamping)."""
    return _positive(draft_size) * _DRAFT_PX_PER_UNIT[role]


def draft_text_style(style, role):
    """Map a merged project style once, for the draft exporter only.

    Returns TextStyle and ClipSettings keyword dictionaries. Font copying and
    candidate_pulse keyframe serialization stay with the draft library adapter.
    ``draft_size`` is legacy metadata: project ``size`` is the single authority.
    """
    color = style['color'].lstrip('#')
    # Match the unchanged ASS exporter: #AARRGGBB ignores leading alpha.
    # This fixes RGB channel shifting, not transparency support; that capability
    # needs a separate cross-output decision, not a draft-only new default.
    if len(color) == 8:
        color = color[2:]
    rgb = tuple(int(color[i:i+2], 16) / 255 for i in (0, 2, 4))
    # NO ink-centre correction yet. The original sample's centres differ by
    # +14/+9.5/+8 px (title/subtitle/footer). A font-bbox model predicts offsets
    # but has not been checked in Jianying after resizing, with auto-wrapping or
    # teaching/countdown text. Preserve explicit project anchors until measured.
    return (dict(size=draft_text_size(style['size'], role), bold=style['bold'],
                 color=rgb, align=1, auto_wrapping=True, max_line_width=.9),
            dict(transform_x=float(style['x']) * 2 - 1,
                 transform_y=1 - float(style['y']) * 2))


# (font file name, Jianying effect id) in preference order per role.
_FONT_CANDIDATES = {
    'heading': (('喜鹊古字典体(字节试用版).ttf', '7035911487646339598'),
                ('SourceHanSansSC-Bold-2.otf', None),
                ('SourceHanSansCN-Heavy.otf', None),
                ('msyhbd.ttc', None), ('msyh.ttc', None)),
    'footer': (('喜鹊招牌体(字节试用版).ttf', '7035914068053463565'),
               ('SourceHanSansSC-Bold-2.otf', None),
               ('AlimamaShuHeiTi-Bold.ttf', None),
               ('msyhbd.ttc', None), ('msyh.ttc', None)),
    'bold': (('SourceHanSansSC-Bold-2.otf', None),
             ('SourceHanSansSC-Bold.otf', None),
             ('msyhbd.ttc', None), ('simhei.ttf', None)),
    'heavy': (('SourceHanSansSC-Heavy-2.otf', None),
              ('SourceHanSansSC-Heavy.otf', None),
              ('SourceHanSansCN-Heavy.otf', None),
              ('msyhbd.ttc', None), ('simhei.ttf', None)),
    'arial': (('arial.ttf', None), ('Arial.ttf', None), ('msyh.ttc', None)),
}
_FONT_CACHE = None
_NAME_CACHE = {}


def _font_roots():
    roots = [Path.home() / 'AppData/Local/Microsoft/Windows/Fonts',
             Path('C:/Windows/Fonts')]
    cache = Path.home() / 'AppData/Local/JianyingPro/User Data/Cache/effect'
    if cache.is_dir():
        roots.append(cache)
    return roots


def _read_name_table(path):
    """Return (family, subfamily) from a TTF/OTF/TTC without extra dependencies."""
    data = Path(path).read_bytes()
    offset = 0
    if data[:4] == b'ttcf':  # collection: first face's table directory
        if len(data) < 16:
            return None
        offset = struct.unpack('>I', data[12:16])[0]
    if len(data) < offset + 12:
        return None
    count = struct.unpack('>H', data[offset + 4:offset + 6])[0]
    table = None
    for index in range(count):
        entry = offset + 12 + index * 16
        if entry + 16 > len(data):
            return None
        if data[entry:entry + 4] == b'name':
            table = struct.unpack('>II', data[entry + 8:entry + 16])
            break
    if table is None:
        return None
    start, length = table
    if start + length > len(data) or length < 6:
        return None
    block = data[start:start + length]
    stored, count, strings = struct.unpack('>HHH', block[:6])
    names = {}
    for index in range(count):
        record = 6 + index * 12
        if record + 12 > len(block):
            break
        platform, encoding, language, name_id, size, position = struct.unpack(
            '>HHHHHH', block[record:record + 12])
        if name_id not in (1, 2, 16) or not size:
            continue
        raw = block[strings + position:strings + position + size]
        try:
            if platform == 3 or (platform == 0 and encoding in (0, 1, 3)):
                text = raw.decode('utf-16-be')
            else:
                text = raw.decode('latin-1')
        except (UnicodeDecodeError, LookupError):
            continue
        # English (0x409) and the neutral entry win over localized names.
        score = 2 if language in (0x409, 0) else 1
        if name_id not in names or score > names[name_id][0]:
            names[name_id] = (score, text)
    family = names.get(16) or names.get(1)
    subfamily = names.get(2)
    return ((family[1] if family else None),
            (subfamily[1] if subfamily else None))


def font_name(path):
    """Family name recorded inside a font file; falls back to the file name."""
    key = str(path)
    if key not in _NAME_CACHE:
        try:
            family, _ = _read_name_table(path) or (None, None)
        except OSError:
            family = None
        _NAME_CACHE[key] = family or Path(key).name
    return _NAME_CACHE[key]


def resolve_fonts():
    """Map every font role to a real file plus the reason it was chosen.

    ``source`` is ``preferred`` when the reference face was found, otherwise the
    role reports a substitution so callers can surface it.  A missing role is
    omitted rather than guessed.
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    roots = _font_roots()
    resolved = {}
    for role, candidates in _FONT_CANDIDATES.items():
        for position, (name, effect_id) in enumerate(candidates):
            found = None
            if effect_id:
                # The effect folder name is the resource id; the inner folder is
                # a hash that changes, so glob the file name below it.
                effect = Path.home() / ('AppData/Local/JianyingPro/User Data/Cache/effect/'
                                        + effect_id)
                if effect.is_dir():
                    found = next((p for p in sorted(effect.rglob(name)) if p.is_file()), None)
            if found is None:
                found = next((root / name for root in roots
                              if (root / name).is_file()), None)
            if found is not None:
                resolved[role] = {'path': str(found), 'name': name,
                                  'family': font_name(found),
                                  'source': 'preferred' if position == 0 else 'substitute'}
                break
    _FONT_CACHE = resolved
    return resolved


def font_report():
    """Read-only status for ``doctor``; names only, no account or credential data."""
    resolved = resolve_fonts()
    return {role: (dict(resolved[role]) if role in resolved else
                   {'path': None, 'name': None, 'family': None, 'source': 'missing'})
            for role in FONT_ROLES}


def default_styles(overrides=None):
    """Style preset.  ``overrides`` may pin a usable font path for a caller.

    An override that does not resolve to a real file is rejected, so a bad
    override cannot turn into text that silently never renders.
    """
    fonts = resolve_fonts()
    values = dict(overrides or {})

    def usable(value):
        if isinstance(value, dict):
            value = value.get('path')
        if not value:
            return None
        candidate = Path(str(value))
        return str(candidate) if candidate.is_file() else None

    def font(role, *fallbacks):
        for name in (role,) + fallbacks:
            path = usable(values.get(name)) or (fonts.get(name) or {}).get('path')
            if path:
                return path, font_name(path), (fonts.get(name) or {}).get('source', 'override')
        return None, None, 'missing'

    bold, bold_name, _ = font('bold', 'heading')
    heavy, heavy_name, _ = font('heavy', 'bold')
    arial, arial_name, _ = font('arial', 'bold')
    heading, heading_name, _ = font('heading', 'bold')
    footer, footer_name, _ = font('footer', 'bold')
    # Size in px relative to a 2160-high canvas; draft_size retains source units.
    # ASS/draft rendering differences must be checked on the first physical sample.
    return {
        'english': dict(font=bold, font_name=bold_name, size=450,
                        draft_size=13, x=.5, y=(1-.1875886524822694)/2,
                        color='#FFFFFF', bold=True),
        'phonetic': dict(font=arial, font_name=arial_name, size=174,
                         draft_size=5, x=.5, y=(1+.08518518518518518)/2,
                         color='#FFFFFF', bold=False),
        'meaning': dict(font=heavy, font_name=heavy_name, size=210,
                        draft_size=6, x=.5, y=(1+.2712962962962963)/2,
                        color='#FFFFFF', bold=True),
        'title': dict(font=heading, font_name=heading_name, size=315,
                      draft_size=9, x=.5, y=.065, color='#FFFF99', bold=True),
        'subtitle': dict(font=heading, font_name=heading_name, size=240,
                         draft_size=7, x=.5, y=.18, color='#FFFF99', bold=True),
        'footer': dict(font=footer, font_name=footer_name, size=210,
                       draft_size=6, x=.5, y=.935, color='#FFFF99', bold=True),
        'countdown': dict(font=bold, font_name=bold_name, size=675,
                          draft_size=20, x=.5, y=.5, color='#FFFFFF', bold=True,
                          animation='placeholder_static'),
    }


def text_events(manifest):
    """Return (track, text, start frame, end frame); both exporters use this.

    A real intro clip already contains the countdown digits, so no synthetic
    ``countdown`` text is emitted for it — that would draw a second set of
    digits over the clip's own.  The synthetic countdown remains only for the
    legacy path that reserves intro seconds without any clip.  The golden title
    block is on screen from the first frame; only word content waits for the
    intro to finish.
    """
    synthetic = 0 if manifest.intro_video else manifest.intro_frames
    # The title block is part of the lesson frame, not of the countdown: it is
    # on screen from the very first frame, exactly as the reference export shows
    # (golden title/subtitle/footer visible while the digits count down).
    events = [('title', manifest.title, 0, manifest.total_frames),
              ('subtitle', manifest.subtitle, 0, manifest.total_frames),
              ('footer', manifest.footer, 0, manifest.total_frames)]
    if synthetic:
        half = synthetic // 2
        if half:
            events.append(('countdown', '2', 0, half))
        events.append(('countdown', '1', half, synthetic))
    for word in manifest.words:
        events.extend([
            ('english', word['word'], word['start_frame'], word['end_frame']),
            ('phonetic', word['phonetic'], word['male_frame'], word['end_frame']),
            ('meaning', word['meaning'], word['chinese_frame'], word['end_frame']),
        ])
    return events
