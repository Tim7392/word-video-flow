"""``LayoutSurface``: the one text layout the preview and the MP4 both use.

Input is a :class:`~word_video.domain.plan.RenderPlan` (solved ticks) plus the
canvas, the font map and the style map; output is one
:class:`PlacedText` per display clip with its anchor, its wrapped lines, the box
those lines occupy and the font file and pixel size a rasteriser needs.  Nothing
here writes a file or draws a pixel: the exporters turn placements into ASS,
into a draft and (through W04) into on-screen text, and all three describe the
same geometry.

Coordinates are canvas fractions (``0..1``) because that is the one form that
survives every canvas: the style map is written for a 2160-pixel-high reference
canvas and scaled by ``height / 2160``, exactly like the verified ASS exporter,
so a 320x180 test and a 4K delivery place text in the same relative spot.

Two decisions worth stating, because they are interface, not detail:

* **A placement is a block.**  Lines are wrapped to the safe width, laid out
  downwards and the *block* is centred on the anchor - which is what ASS's
  ``\\an5`` (centred) means.  Vertical centring makes the MP4 and a preview that
  draws the same box agree without either of them guessing at a baseline.
* **Overflow is data, not an exception.**  ``boxes`` reports whether a block
  leaves the safe area so a caller can show it in the editor; :func:`require_fit`
  is the strict door a *delivery* export uses, and it refuses rather than
  shrinking text, because silently changing the font size would change the
  deliverable the user approved.
"""
from dataclasses import dataclass, field
import math

from ..domain.model import DISPLAY_LAYERS, PROJECT_LAYERS
from .metrics import is_full_width, metrics_for

#: Style size is authored against this canvas height and scaled from it.
REFERENCE_HEIGHT = 2160.0
#: Fraction of the canvas kept free on the left and right before text counts as
#: overflow.
SAFE_MARGIN_X = 0.04
#: Vertical allowance, as a fraction of the block's own height: how far past the
#: frame edge a block may sit before it counts as leaving the picture.  It is not
#: zero because the approved preset *is* the approved layout - its title block is
#: centred on y=0.065 and its footer on y=0.935, so together they use the frame's
#: height almost completely - and it is proportional because a taller block is
#: centred on the same anchor and therefore reaches further by construction.  What
#: this still catches is exactly what matters: a block that has grown far past the
#: size its role was designed for.
VERTICAL_OVERHANG = 0.6
#: ...plus a fixed slack, so a single short line (whose measured height comes from
#: the glyphs, not from the em box) can still be at the frame edge as designed.
MIN_OVERHANG = 0.02
#: A text block taller than this fraction of the canvas is reported: it no longer
#: reads as a caption but as a wall of text over the lesson.
MAX_BLOCK_HEIGHT = 0.4
#: Roles that carry text on screen: the per-word layers plus the project's own.
TEXT_ROLES = DISPLAY_LAYERS + PROJECT_LAYERS[:3]
#: Characters that may never start a line when wrapping (avoid a leading 。).
NO_LINE_START = '，。、；：？！）》」』%,.;:?!)]}'
#: Characters that may never end a line.
NO_LINE_END = '（《「『([{'

DEFAULT_STYLE = {'size': 240, 'x': 0.5, 'y': 0.5, 'color': '#FFFFFF', 'bold': False}


class LayoutOverflow(Exception):
    """A delivery export was asked to place text that does not fit."""


@dataclass(frozen=True)
class LineBox:
    """One laid-out line, in canvas fractions plus its own pixel extents."""

    text: str
    x: float
    y: float                # centre of the line
    width: float
    height: float
    baseline: float         # distance from the line's top to its baseline

    def to_dict(self):
        return {'text': self.text, 'x': self.x, 'y': self.y, 'width': self.width,
                'height': self.height, 'baseline': self.baseline}


@dataclass(frozen=True)
class TextBox:
    """The block one display clip occupies, in canvas fractions."""

    x: float
    y: float
    width: float
    height: float
    left: float
    top: float
    right: float
    bottom: float
    margin_x: float = SAFE_MARGIN_X
    margin_y: float = 0.0

    @property
    def overflows_horizontally(self):
        return (self.left < self.margin_x - 1e-9
                or self.right > 1 - self.margin_x + 1e-9)

    @property
    def overflows_vertically(self):
        """The block left the canvas by more than its own overhang, or grew too tall.

        The bounds are the *drawn* block, so the comparison allows the same
        proportional overhang a centred block at a preset anchor needs - the
        approved preset's title does sit a few pixels above the top edge - without
        that allowance changing the numbers a preview draws.
        """
        slack = max(self.height * VERTICAL_OVERHANG, MIN_OVERHANG)
        return (self.top < -slack - 1e-9 or self.bottom > 1 + slack + 1e-9
                or self.height > MAX_BLOCK_HEIGHT + 1e-9)

    @property
    def inside_safe_area(self):
        return not (self.overflows_horizontally or self.overflows_vertically)

    def reason(self, clip_id):
        """Human-readable explanation of what left the safe area."""
        slack = max(self.height * VERTICAL_OVERHANG, MIN_OVERHANG)
        parts = []
        if self.left < self.margin_x - 1e-9:
            parts.append('%s 左边超出安全区 %.3f' % (clip_id, self.margin_x - self.left))
        if self.right > 1 - self.margin_x + 1e-9:
            parts.append('%s 右边超出安全区 %.3f' % (clip_id, self.right - (1 - self.margin_x)))
        if self.top < -slack - 1e-9:
            parts.append('%s 上边离开画面 %.3f' % (clip_id, -self.top))
        if self.bottom > 1 + slack + 1e-9:
            parts.append('%s 下边离开画面 %.3f' % (clip_id, self.bottom - 1))
        if self.height > MAX_BLOCK_HEIGHT + 1e-9:
            parts.append('%s 占画面高度 %.3f，超过 %.2f'
                         % (clip_id, self.height, MAX_BLOCK_HEIGHT))
        return '；'.join(parts) or '%s 在安全区内' % clip_id

    def to_dict(self):
        return {'x': self.x, 'y': self.y, 'width': self.width, 'height': self.height,
                'left': self.left, 'top': self.top, 'right': self.right,
                'bottom': self.bottom, 'margin_x': self.margin_x,
                'margin_y': self.margin_y, 'inside_safe_area': self.inside_safe_area}


@dataclass(frozen=True)
class PlacedText:
    """One display clip, placed: where it is, what it says, how big it draws."""

    clip_id: str
    role: str
    record_id: str
    text: str
    start_ticks: int
    end_ticks: int
    anchor_x: float
    anchor_y: float
    font_path: str
    font_name: str
    size: int                       # pixels on this canvas
    size_em: float                  # size in units of the canvas height
    color: str
    bold: bool
    lines: tuple = ()
    box: TextBox | None = None
    measured_exactly: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'lines', tuple(self.lines))

    @property
    def fits(self):
        return self.box is not None and self.box.inside_safe_area

    def to_dict(self):
        return {'clip_id': self.clip_id, 'role': self.role, 'record_id': self.record_id,
                'text': self.text, 'start_ticks': self.start_ticks,
                'end_ticks': self.end_ticks, 'anchor_x': self.anchor_x,
                'anchor_y': self.anchor_y, 'font_path': self.font_path,
                'font_name': self.font_name, 'size': self.size, 'size_em': self.size_em,
                'color': self.color, 'bold': self.bold,
                'lines': [line.to_dict() for line in self.lines],
                'box': self.box.to_dict() if self.box else None,
                'measured_exactly': self.measured_exactly}


@dataclass(frozen=True)
class LayoutReport:
    """What the layout found: the placements and anything that did not fit."""

    width: int
    height: int
    placements: tuple = ()
    overflowing: tuple = ()
    exact_metrics: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'placements', tuple(self.placements))
        object.__setattr__(self, 'overflowing', tuple(self.overflowing))

    def by_role(self):
        grouped = {role: [] for role in DISPLAY_LAYERS}
        for placed in self.placements:
            grouped.setdefault(placed.role, []).append(placed)
        return {role: tuple(items) for role, items in grouped.items()}

    def to_dict(self):
        return {'width': self.width, 'height': self.height,
                'exact_metrics': self.exact_metrics,
                'overflowing': [placed.clip_id for placed in self.overflowing],
                'placements': [placed.to_dict() for placed in self.placements]}


def style_for(styles, role):
    """One role's style, merged over the defaults (never a silent substitution)."""
    resolved = dict(DEFAULT_STYLE)
    resolved.update(styles.get(role) or {})
    return resolved


class LayoutSurface:
    """Places the display clips of a render plan on one canvas.

    ``fonts`` maps a style role to a family *name* and ``font_paths`` maps it to
    the file that face lives in (the same resolution the renderer uses for
    ``fontsdir``); a role with no file is refused rather than drawn in whatever
    the rasteriser picks, because a substituted face changes the metrics the
    layout was checked against.
    """

    def __init__(self, plan, width=None, height=None, styles=None, fonts=None,
                 font_paths=None, dpi=72):
        self.plan = plan
        self.width = int(plan.width if width is None else width)
        self.height = int(plan.height if height is None else height)
        if self.width <= 0 or self.height <= 0:
            raise ValueError('LayoutSurface needs a positive canvas')
        if not math.isfinite(float(dpi)) or float(dpi) <= 0:
            raise ValueError('LayoutSurface needs a positive DPI')
        self.dpi = float(dpi)
        self.styles = dict(styles or {})
        self.fonts = dict(fonts or {})
        self.font_paths = {key: str(value) for key, value in (font_paths or {}).items()}
        #: Vertical allowance for a caller that wants its own shoulder room; the
        #: verdict does not depend on it (a block's own overhang is computed per
        #: placement), so a preview can set it without changing the report.
        self.margin_y = 0.0

    # -- style -----------------------------------------------------------
    def scale(self, size):
        """Author size (2160-high reference) -> pixels on this canvas."""
        return max(1, int(round(float(size) * self.height / REFERENCE_HEIGHT)))

    def metrics(self, role):
        style = style_for(self.styles, role)
        size = self.scale(style.get('size', DEFAULT_STYLE['size']))
        return metrics_for(self.font_paths.get(role, ''), size)

    # -- placement -------------------------------------------------------
    def place(self, item, margin_y=None):
        """Place one plan item; ``item`` needs role/text/start/end/clip_id."""
        role = item.role
        style = style_for(self.styles, role)
        size = self.scale(style.get('size', DEFAULT_STYLE['size']))
        metrics = metrics_for(self.font_paths.get(role, ''), size)
        text = item.text or ''
        lines = self.wrap(text, metrics)
        laid = self._lines(lines, metrics, style, size)
        box = self._box(laid, style, self.margin_y if margin_y is None else margin_y)
        return PlacedText(            clip_id=item.clip_id, role=role, record_id=item.record_id or '', text=text,
            start_ticks=item.start_ticks, end_ticks=item.end_ticks,
            anchor_x=float(style.get('x', DEFAULT_STYLE['x'])),
            anchor_y=float(style.get('y', DEFAULT_STYLE['y'])),
            font_path=self.font_paths.get(role, ''),
            font_name=str(self.fonts.get(role) or ''), size=size,
            size_em=size / float(self.height),
            color=str(style.get('color', DEFAULT_STYLE['color'])),
            bold=bool(style.get('bold', DEFAULT_STYLE['bold'])),
            lines=laid, box=box, measured_exactly=metrics.exact)

    def wrap(self, text, metrics):
        """Wrap ``text`` to the safe width; author line breaks are kept.

        A line break is inserted only where the language allows one: after a
        space for Latin text, and between two CJK characters.  A Latin word that
        is wider than the safe area stays whole and is *reported* as overflow -
        breaking it would invent a hyphen the author never wrote.
        """
        safe_width = self._safe_pixels_x()
        if not text:
            return ('',)
        out = []
        for paragraph in text.split('\n'):
            out.extend(self._wrap_paragraph(paragraph, metrics, safe_width))
        return tuple(out) if out else ('',)

    def _wrap_paragraph(self, paragraph, metrics, safe_width):
        if not paragraph:
            return ['']
        if metrics.text_extent(paragraph).width <= safe_width:
            return [paragraph]
        lines, current, current_width = [], '', 0.0
        pieces = self._pieces(paragraph)
        for piece, is_space in pieces:
            width = metrics.text_extent(piece).width
            if current == '' and is_space:
                continue
            if current_width + width <= safe_width or current == '':
                current += piece
                current_width += width
                continue
            trimmed = current.rstrip()
            if trimmed and trimmed[-1] in NO_LINE_END:
                # Never leave an opening bracket at the end of a line: move it on.
                moved = trimmed[-1]
                trimmed = trimmed[:-1].rstrip()
                current = moved + piece
                current_width = metrics.text_extent(current).width
                if trimmed:
                    lines.append(trimmed)
                continue
            lines.append(trimmed if trimmed else current)
            current = '' if is_space else piece
            current_width = 0.0 if is_space else width
            if current and current[0] in NO_LINE_START:
                # A closing punctuation may not open a line; keep it with the
                # previous line instead of inventing a rule the reader can see.
                if lines:
                    lines[-1] = lines[-1] + current[0]
                    current = current[1:]
                    current_width = metrics.text_extent(current).width if current else 0.0
        if current:
            lines.append(current.rstrip())
        return [line for line in lines if line != ''] or ['']

    @staticmethod
    def _pieces(paragraph):
        """Split into break opportunities: words, single spaces, CJK characters."""
        pieces = []
        buffer = ''

        def flush():
            nonlocal buffer
            if buffer:
                pieces.append((buffer, False))
                buffer = ''

        for char in paragraph:
            if char == ' ':
                flush()
                pieces.append((' ', True))
            elif is_full_width(char):
                flush()
                pieces.append((char, False))
            else:
                buffer += char
        flush()
        return pieces

    def _lines(self, lines, metrics, style, size):
        """Lay the wrapped lines out as one block, every line centred on the anchor."""
        anchor_x = float(style.get('x', DEFAULT_STYLE['x']))
        anchor_y = float(style.get('y', DEFAULT_STYLE['y']))
        extents = [metrics.text_extent(line) for line in lines]
        line_height = max((extent.height for extent in extents), default=size) or size
        block_height = line_height * len(lines)
        top = anchor_y * self.height - block_height / 2.0
        placed = []
        for position, (line, extent) in enumerate(zip(lines, extents)):
            placed.append(LineBox(
                text=line,
                x=anchor_x,
                y=(top + line_height * (position + 0.5)) / self.height,
                width=extent.width / self.width,
                height=extent.height / self.height,
                baseline=extent.ascent))
        return tuple(placed)

    def _box(self, laid, style, margin_y=None):
        anchor_x = float(style.get('x', DEFAULT_STYLE['x']))
        anchor_y = float(style.get('y', DEFAULT_STYLE['y']))
        allowance = self.margin_y if margin_y is None else margin_y
        if not laid:
            return TextBox(anchor_x, anchor_y, 0.0, 0.0, anchor_x, anchor_y,
                           anchor_x, anchor_y, SAFE_MARGIN_X, allowance)
        widest = max(line.width for line in laid)
        height = sum(line.height for line in laid)
        # The box is the widest line's extent around the anchor, and the block's
        # vertical extent around it; that is what "does it fit" has to mean.  It is
        # the *drawn* geometry: the verdict in :meth:`TextBox.overflows_vertically`
        # adds the allowance a centred block needs, so a preview can draw these
        # numbers directly.
        left = anchor_x - widest / 2.0
        right = anchor_x + widest / 2.0
        top = anchor_y - height / 2.0
        bottom = anchor_y + height / 2.0
        return TextBox(x=anchor_x, y=anchor_y, width=widest, height=height,
                       left=left, top=top, right=right, bottom=bottom,
                       margin_x=SAFE_MARGIN_X, margin_y=allowance)

    def _safe_pixels_x(self):
        return self.width * (1 - 2 * SAFE_MARGIN_X)

    # -- whole surface ---------------------------------------------------
    def layout(self):
        """Place every on-screen clip of the plan and report what overflows."""
        items = [item for item in self.plan.video if item.role in TEXT_ROLES]
        placements = tuple(self.place(item) for item in items)
        overflowing = tuple(placed for placed in placements if not placed.fits)
        return LayoutReport(width=self.width, height=self.height,
                            placements=placements, overflowing=overflowing,
                            exact_metrics=all(placed.measured_exactly
                                              for placed in placements) if placements
                            else False)

    def require_fit(self):
        """The delivery door: layout or refuse, never shrink text silently."""
        report = self.layout()
        if report.overflowing:
            detail = '；'.join(placed.box.reason(placed.clip_id)
                              for placed in report.overflowing)
            raise LayoutOverflow('文字超出安全区：%s' % detail)
        return report
