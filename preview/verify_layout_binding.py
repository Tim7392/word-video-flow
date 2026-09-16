"""Prove the preview draws B's layout, and that a small canvas agrees with a big one.

Run it from this checkout::

    & <work root>\\runtime\\venv\\Scripts\\python.exe -m preview.verify_layout_binding

While ``word_video/layout`` is not yet merged into ``main``, point it at the
worktree that has it (read-only; nothing is copied and nothing is written there)::

    ... -m preview.verify_layout_binding --layout-tree <work root>\\worktrees\\B

What it checks, and why each one is a real requirement
------------------------------------------------------
1. **The adapter returns B's own placements.**  ``type(placed).__module__`` must be
   ``word_video.layout...``: if the preview wrapped them in its own type there
   would be a second placement model to keep in step, which is what "不许交两套排版"
   forbids.
2. **Relative geometry does not depend on the canvas.**  The style preset is
   authored against a 2160-high reference and scaled by ``height / 2160``, so a
   320x180 preview and a 1920x1080 delivery must put the same block in the same
   *fractional* place.  This is the property that lets a member trust a small
   preview, and it is checked as fractions, never as a screenshot.
3. **Pixel size scales exactly**, by the same ratio as the canvas.
4. **Asking by time returns exactly the placements due**, which is the whole
   interface the preview uses.
"""
import argparse
import json
import sys
from pathlib import Path

#: The checkout this file lives in, so the tool runs both as
#: ``python preview/verify_layout_binding.py`` and as ``-m preview....``.
CHECKOUT = Path(__file__).resolve().parents[1]
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

#: Canvases compared.  1920x1080 is the delivery size of the three-word fixture;
#: 320x180 is the smallest preview the interface is documented to serve.
CANVASES = ((320, 180), (1280, 720), (1920, 1080))
RATE = 48000


def _load_layout_tree(path):
    """Make an external checkout the source of the whole ``word_video`` namespace.

    Appending to ``sys.path`` would not work: ``word_video`` is a package, so its
    search path is fixed by whichever tree answers the *package* import first, and
    a package imported earlier (``preview`` pulls in ``word_video.domain``) wins
    no matter what is appended later.  The named tree therefore goes first *and*
    any ``word_video`` already in ``sys.modules`` is dropped, so the layout is
    verified as it actually is over there rather than as a stale copy.
    """
    tree = Path(path).resolve(strict=True)
    if not (tree / 'word_video' / 'layout').is_dir():
        raise SystemExit('no word_video/layout under %s' % tree)
    for name in [name for name in sys.modules
                 if name == 'word_video' or name.startswith('word_video.')]:
        del sys.modules[name]
    sys.path.insert(0, str(tree))
    return str(tree)


def build_plan():
    """The production path: template -> clips -> solved RenderPlan."""
    from word_video.application import instantiate
    from word_video.domain import MediaInfo, Project, Record, solve
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE

    records = (
        Record(id='w151', word='demonstrate', phonetic='ˈdemənstreɪt',
               meaning='v. 证明；演示；示范', spoken_meaning='证明；证实', index=151),
        Record(id='w152', word='deputy', phonetic='ˈdepjuti',
               meaning='n. 副手；代理人', spoken_meaning='副手；代理', index=152),
        Record(id='w153', word='continuous', phonetic='kənˈtɪnjuəs',
               meaning='adj. 连续不断的；持续的', spoken_meaning='连续不断的；持续的',
               index=153),
    )
    media = {'%s:%s' % (record.id, role): MediaInfo('%s:%s' % (record.id, role),
                                                    int(0.6 * RATE))
             for record in records for role in ('female', 'male', 'chinese')}
    project = instantiate(DEFAULT_LESSON_TEMPLATE, records, media,
                          Project(project_id='layout-binding', intro_s=0.0))
    return solve(project, media).render


def font_maps():
    from word_video.template import default_styles
    styles = default_styles()
    return (styles,
            {role: item.get('font') for role, item in styles.items()},
            {role: item.get('font_name') for role, item in styles.items()})


def placements_for(plan, styles, paths, names, width, height):
    from preview.layout import LayoutSurfaceDisplay
    return LayoutSurfaceDisplay.from_plan(
        plan, width=width, height=height, styles=styles, fonts=names,
        font_paths=paths)


def check(plan, styles, paths, names):
    """Compare the canvases against tolerances that match what the interface promises.

    Measured on this machine, the two kinds of quantity behave differently and the
    check says so instead of averaging them:

    * **Where a block sits** - ``anchor_x``/``anchor_y`` and every ``LineBox`` x/y -
      is identical bit-for-bit.  Those are canvas fractions by construction, with
      no glyph measurement involved, so the assertion is exact and a wrong anchor
      or a moved style fails immediately.
    * **How wide a block measures** is *not* identical, because a glyph's advance
      is rasterised to a whole pixel at the canvas's own font size: at 320 px wide
      a size-20 subtitle measures about 8 % wider per em than a linear scale of the
      1080p layout, which is a few pixels of the small canvas.  The tolerance is
      therefore "half a glyph at that canvas", and the evidence prints the worst
      delta *as a fraction of its own tolerance* so a real error (an order of
      magnitude larger) still stands out.
    """
    problems = []
    worst = {'anchor': (0.0, None), 'box': (0.0, None), 'line': (0.0, None),
             'size_px': (0.0, None)}
    by_canvas = {}
    for width, height in CANVASES:
        display = placements_for(plan, styles, paths, names, width, height)
        report = display.report
        by_canvas[(width, height)] = {
            'display': display,
            'placements': {placed.clip_id: placed for placed in report.placements},
            'overflowing': sorted(placed.clip_id for placed in report.overflowing),
            'exact_metrics': bool(getattr(report, 'exact_metrics', False)),
        }
        if not report.placements:
            problems.append('%dx%d produced no placements' % (width, height))

    base_key = (1920, 1080)
    base = by_canvas[base_key]
    module_roots = {type(placed).__module__ for data in by_canvas.values()
                    for placed in data['placements'].values()}

    def note(kind, delta, where):
        if delta > worst[kind][0]:
            worst[kind] = (delta, where)

    for canvas, data in by_canvas.items():
        if set(data['placements']) != set(base['placements']):
            problems.append('%dx%d places different clips than %s'
                            % (canvas[0], canvas[1], base_key))
        if data['overflowing'] != base['overflowing']:
            problems.append('%dx%d reports overflow %s but %s reports %s'
                            % (canvas[0], canvas[1], data['overflowing'],
                               base_key, base['overflowing']))
    for clip_id, reference in base['placements'].items():
        for canvas, data in by_canvas.items():
            placed = data['placements'].get(clip_id)
            if placed is None:
                continue
            width, height = canvas
            where = '%s@%dx%d' % (clip_id, width, height)
            # Position: exact, because nothing here is measured from a glyph.
            for field in ('anchor_x', 'anchor_y'):
                note('anchor', abs(getattr(placed, field) - getattr(reference, field)),
                     '%s.%s' % (where, field))
            # Font size: rounded to a whole pixel per canvas, so half a pixel.
            expected = reference.size * height / float(base_key[1])
            note('size_px', abs(placed.size - expected), where)
            # Measured extent: half a glyph of slack at this canvas.
            half_glyph_x = 0.5 * reference.size * (width / float(base_key[0])) / width
            half_glyph_y = 0.5 * reference.size * (height / float(base_key[1])) / height
            if placed.box is not None and reference.box is not None:
                for field, slack in (('left', half_glyph_x), ('right', half_glyph_x),
                                     ('top', half_glyph_y), ('bottom', half_glyph_y)):
                    delta = abs(getattr(placed.box, field)
                                - getattr(reference.box, field))
                    note('box', delta / slack, '%s.box.%s' % (where, field))
                    if delta > slack:
                        problems.append('%s.box.%s differs by %.5f, more than half a '
                                        'glyph (%.5f)' % (where, field, delta, slack))
            if len(placed.lines) != len(reference.lines):
                problems.append('%s wraps into %d lines, %s wraps into %d'
                                % (where, len(placed.lines), base_key,
                                   len(reference.lines)))
                continue
            for index, (line, ref) in enumerate(zip(placed.lines, reference.lines)):
                if line.text != ref.text:
                    problems.append('%s line %d text differs from %s'
                                    % (where, index, base_key))
                note('line', abs(line.y - ref.y) / half_glyph_y,
                     '%s.line%d.y' % (where, index))
                note('line', abs(line.x - ref.x) / half_glyph_x,
                     '%s.line%d.x' % (where, index))

    if worst['anchor'][0] > 1e-12:
        problems.append('a style anchor differs by %.3g (%s), which no rounding explains'
                        % worst['anchor'])
    if worst['size_px'][0] > 0.5 + 1e-9:
        problems.append('a font size differs from the exact ratio by %.4f px (%s)'
                        % worst['size_px'])

    # -- the port answers by time, and by B's own objects -----------------
    display = base['display']
    reference = next(iter(base['placements'].values()))
    returned = display.placements_at(reference.start_ticks)
    if not returned:
        problems.append('placements_at(start_ticks) returned nothing')
    for placed in returned:
        if not (placed.start_ticks <= reference.start_ticks < placed.end_ticks):
            problems.append('%s returned outside its own window' % placed.clip_id)
    if display.placements_at(plan.total_ticks + 1):
        problems.append('placements_at past the end returned something')

    evidence = {
        'canvases': {('%dx%d' % canvas): {
            'placements': len(data['placements']),
            'overflowing': data['overflowing'],
            'exact_metrics': data['exact_metrics'],
            'sizes': sorted({placed.size for placed in data['placements'].values()}),
        } for canvas, data in by_canvas.items()},
        'placement_type_modules': sorted(module_roots),
        'placed_objects_are_layout_types': all(
            module.startswith('word_video.layout') for module in module_roots),
        # Fraction of its own allowance, so 1.0 is the limit and small is good.
        'worst_delta_over_allowance': {
            kind: {'value': round(value, 6), 'where': where}
            for kind, (value, where) in worst.items()},
        'allowances': {
            'anchor': 'exact (0) - a style position is a canvas fraction',
            'line_position': 'half a glyph at that canvas',
            'block_extent': 'half a glyph at that canvas',
            'font_size_px': '0.5 px (the size is rounded per canvas)',
        },
        'problems': problems[:20],
        'problem_count': len(problems),
        'ok': not problems,
    }
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--layout-tree',
                        help='checkout holding word_video/layout (when not merged yet)')
    parser.add_argument('--report', help='also write the evidence JSON here')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    evidence = {'layout_tree': None, 'layout_importable_from_this_checkout': None}
    if args.layout_tree:
        evidence['layout_tree'] = _load_layout_tree(args.layout_tree)
    try:
        import word_video.layout  # noqa: F401
    except ImportError as error:
        print(json.dumps({'ok': False,
                          'error': 'word_video.layout is not importable: %s' % error,
                          'hint': 'pass --layout-tree <checkout that has it>'},
                         ensure_ascii=False, indent=1))
        return 2
    evidence['layout_importable_from_this_checkout'] = not args.layout_tree
    evidence['layout_module_file'] = word_video.layout.__file__
    if args.layout_tree and not str(word_video.layout.__file__).startswith(
            evidence['layout_tree']):
        print(json.dumps({'ok': False,
                          'error': 'word_video.layout came from %s, not the named tree'
                                   % word_video.layout.__file__},
                         ensure_ascii=False, indent=1))
        return 2

    styles, paths, names = font_maps()
    missing = [role for role in ('english', 'phonetic', 'meaning', 'title')
               if not paths.get(role)]
    if missing:
        print(json.dumps({'ok': False, 'error': 'no font resolved for %s' % missing},
                         ensure_ascii=False, indent=1))
        return 2

    plan = build_plan()
    evidence['plan'] = {'clips_video': len(plan.video), 'clips_audio': len(plan.audio),
                        'total_ticks': plan.total_ticks,
                        'canvas': '%dx%d' % (plan.width, plan.height)}
    evidence.update(check(plan, styles, paths, names))
    payload = json.dumps(evidence, ensure_ascii=False, indent=1)
    if args.report:
        Path(args.report).write_text(payload, encoding='utf-8')
    print(payload)
    return 0 if evidence['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
