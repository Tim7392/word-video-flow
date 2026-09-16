"""Burn-in captions from a plan, using the layout the preview also uses.

The ASS format is the verified one - header, one ``Style:`` line per role, one
``Dialogue:`` per cue, ``ass=`` last in the filter chain - so this module keeps
the *format* and replaces the *geometry source*: every position comes from
:class:`word_video.layout.LayoutSurface` rather than from a second reading of the
style anchors.  That is the whole point of the W03 interface: the picture, the
preview and the editable draft must not each decide where a word goes.

Text is escaped exactly like the verified exporter (backslashes, braces, newlines)
so a word can never inject an override tag, and the countdown's pulse animation
stays attached to the countdown role only.
"""
from pathlib import Path

from .. import template as template_module
from ..layout import LayoutSurface
from ..render.ass import (_EVENT_FORMAT, _HEADER, _countdown_tags, _cs, _escape_text,
                          _num, _style_line, _styles, _timestamp)


def build_layout_ass(plan, report, *, styles=None, frame_limit=None,
                     offset_frames=0, fps=None):
    """Return a full ASS document for the *placements* of one plan.

    ``report`` is a :class:`~word_video.layout.LayoutReport`; the caller builds it
    with the same styles and fonts, so a slice render and the preview agree.
    ``styles`` is the merged style table (the project's overrides over the preset);
    ``frame_limit``/``offset_frames`` render a slice: every event is shifted back
    to the slice's own zero and clipped to it, exactly like the verified exporter.
    """
    rate = int(fps or plan.fps_num)
    lines = [row.format(width=plan.width, height=plan.height) for row in _HEADER]
    for name, style in _styles_map(styles).items():
        lines.append(_style_line(name, style, plan.height))
    lines.extend(['', '[Events]', _EVENT_FORMAT])
    for placed in report.placements:
        start, end = _frames(placed, rate)
        if frame_limit is not None:
            start = max(start, offset_frames)
            end = min(end, offset_frames + frame_limit)
            if end <= start:
                continue
        start_cs = _cs(start - offset_frames, rate)
        end_cs = max(_cs(end - offset_frames, rate), start_cs + 1)
        tags = '\\an5\\pos(%s,%s)' % (_num(placed.anchor_x * plan.width),
                                      _num(placed.anchor_y * plan.height))
        if placed.role == 'countdown' and _animation(styles, 'countdown') == 'candidate_pulse':
            tags += _countdown_tags()
        text = '\\N'.join(_escape_text(line.text) for line in placed.lines)
        lines.append('Dialogue: 0,%s,%s,%s,,0,0,0,,{%s}%s'
                     % (_timestamp(start_cs), _timestamp(end_cs), placed.role, tags, text))
    return '\n'.join(lines) + '\n'


def _frames(placed, fps):
    """A placement's tick window as frames, half up - the renderer works in frames."""
    from .plan import frame_at
    return frame_at(placed.start_ticks, fps), frame_at(placed.end_ticks, fps)


def _styles_map(styles):
    """The style table the ASS header carries; project styles win over the preset."""
    merged = {name: dict(style) for name, style in template_module.default_styles().items()}
    for name, override in (styles or {}).items():
        merged.setdefault(name, {}).update(override or {})
    if any(not style.get('font') for style in merged.values()):
        raise ValueError('No usable font for a rendered style; run doctor')
    return merged


def _animation(styles, role):
    style = (styles or {}).get(role) or {}
    return style.get('animation')


def layout_for(plan, styles, fonts, font_paths, width=None, height=None):
    """The one way to build a surface for a plan (used by every caller)."""
    return LayoutSurface(plan, width=width or plan.width, height=height or plan.height,
                         styles=styles, fonts=fonts, font_paths=font_paths)


def write_ass(text, target):
    """Write the document as UTF-8 with BOM, which is what libass expects here."""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8-sig')
    return str(path)
