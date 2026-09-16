"""Turn a project's style overrides into the table a consumer actually uses.

The project stores only what a member changed ("English text is bigger"), because
that is the editable intent.  A renderer and an on-screen canvas both need the
*whole* table, and they must agree on it: :class:`~word_video.layout.surface.LayoutSurface`
merges whatever it is given over its own neutral fallback, so handing it two bare
overrides silently loses every default the project did not override.

:func:`merged_styles` is that one merge — ``template.default_styles()`` first, then
the override field by field — and :func:`style_fonts` is the font maps the layout
and the ASS exporter also need.  Fonts still come from the resolution chain, so an
override can change size, colour or position and never substitute a face.
"""
from ..domain.model import style_overrides
from ..template import default_styles


def merged_styles(overrides=None):
    """``{role: {field: value}}``: the defaults with the project's overrides applied.

    ``overrides`` is either the plan's table (``{role: {field: value}}``) or a
    sequence of :class:`~word_video.domain.model.StyleOverride`, so a caller can
    pass what it has.
    """
    merged = {role: dict(style) for role, style in default_styles().items()}
    for override in style_overrides(overrides):
        merged.setdefault(override.role, {}).update(override.values)
    return merged


def style_fonts(styles=None):
    """``(paths, names)`` per style role, from the table a caller is about to use."""
    resolved = styles if styles is not None else merged_styles()
    return ({role: item.get('font') for role, item in resolved.items()},
            {role: item.get('font_name') for role, item in resolved.items()})
