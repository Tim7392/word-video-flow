"""W06 acceptance: three words from import to three products, through the window.

This is a *script*, not a test, for the same reason ``desktop/measure_preview.py``
is: it drives a real window over real media, it renders a real 1080p lesson, and
the numbers only mean anything next to the machine they were taken on.  The fast
deterministic checks live in ``tests/test_desktop_editor_*.py``; this is the run
that says what the editor actually does end to end.

    & <work root>\\runtime\\venv\\Scripts\\python.exe desktop\\measure_editor.py
        --out <work root>\\out\\reports\\w06-editor.json

What it proves, and how it avoids fooling itself
------------------------------------------------
* **no hand-written JSON anywhere.**  The project is created by the window's own
  import path from the request fixture, edited by synthesized mouse gestures on
  the timeline, saved by A's store and exported by B's exporter; the report prints
  the file list it produced so a reader can see nothing was patched in;
* **per-object assertions.**  After every gesture the *whole* clip table is
  captured and diffed, so "only the selected clip moved" is a statement about
  every object, not about an aggregate;
* **the delivery is judged by an independent checker.**  ``tools/m0/accept_range.py``
  is run over the published run folder and its verdict is quoted verbatim, because
  the editor approving its own output is not evidence;
* **DPI is measured in fresh processes.**  ``QT_SCALE_FACTOR`` cannot change inside
  a running Qt application, so the interaction battery is re-run as a child
  process at each factor and both results are recorded.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

#: The platform is fixed before Qt is imported: a measurement run must not take
#: the machine's display or focus away from whoever is using it.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

SCALES = ('1', '1.5')
FIXTURE = 'data/fixtures/p1-有片头.json'
WORDS = (151, 152, 153)


def work_root():
    """The work root above this checkout, by the rule the packaging scripts use."""
    for candidate in list(CHECKOUT.parents)[:4]:
        if (candidate / 'runtime').is_dir() and (candidate / 'worktrees').is_dir():
            return candidate
    return CHECKOUT.parent


# --------------------------------------------------------------- gestures
def bar_point(editor, clip_id, fraction=0.5):
    from PySide6 import QtCore
    item = editor.timeline.item(clip_id)
    if item is None:
        raise SystemExit('the timeline does not draw %s' % clip_id)
    rect = editor.timeline.bar_rect(item)
    return QtCore.QPoint(int(rect.left() + rect.width() * fraction),
                         int(rect.center().y()))


def drag(app, editor, point, dx=0, dy=0):
    from PySide6 import QtCore
    from PySide6.QtTest import QTest
    target = QtCore.QPoint(point.x() + dx, point.y() + dy)
    QTest.mousePress(editor.timeline, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    settle(app)
    QTest.mouseMove(editor.timeline, target)
    settle(app)
    QTest.mouseRelease(editor.timeline, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, target)
    settle(app)


def click(app, editor, point, modifier=None):
    from PySide6 import QtCore
    from PySide6.QtTest import QTest
    mode = QtCore.Qt.NoModifier if modifier is None else modifier
    QTest.mouseClick(editor.timeline, QtCore.Qt.LeftButton, mode, point)
    settle(app)


def settle(app, times=3):
    for _ in range(times):
        app.processEvents()


def stored(editor):
    """Every clip as stored: id -> (start, duration).  The per-object oracle."""
    return {clip.id: (clip.start.to_dict(), clip.duration_ticks)
            for clip in editor.state.project.clips}


def solved(editor):
    return dict(editor.state.ranges())


def changed_ids(before, after):
    return sorted(key for key in before if before[key] != after[key])


def tick_pixels(editor):
    return editor.timeline.diagnostics()['pixels_per_second']


# ------------------------------------------------------- the interaction run
def interactions(editor, app, seconds=0.3):
    """Drive the window with real gestures and report what each one did."""
    from PySide6 import QtCore
    from desktop.editor_export import blocking_notices
    facts = {}
    imported = stored(editor)
    facts['panels'] = {'canvas': editor.canvas is not None,
                       'session': editor.showing.session is not None,
                       'timeline_rows': editor.timeline.diagnostics()['rows'],
                       'timeline_bars': len(editor.timeline.items()),
                       'side_tabs': editor.side_tabs.count(),
                       'notices_before_editing': [notice.code
                                                  for notice in editor._last_notices]}
    facts['scale'] = {'device_pixel_ratio': editor.devicePixelRatioF(),
                      'window': [editor.width(), editor.height()],
                      'timeline_width': editor.timeline.diagnostics()['width'],
                      'pixels_per_second': tick_pixels(editor)}

    # (1) a plain click selects exactly one clip, ctrl+click extends explicitly.
    click(app, editor, bar_point(editor, 'w151.male'))
    facts['click'] = {'selection': list(editor.state.selection)}
    click(app, editor, bar_point(editor, 'w151.chinese'))
    click(app, editor, bar_point(editor, 'w151.male'), QtCore.Qt.ControlModifier)
    facts['ctrl_click'] = {'selection': sorted(editor.state.selection)}

    # (2) a single-clip drag moves that clip and nothing else.
    click(app, editor, bar_point(editor, 'w151.male'))
    before_stored, before_solved = stored(editor), solved(editor)
    revision_before = editor.state.revision
    started = time.monotonic()
    drag(app, editor, bar_point(editor, 'w151.male'), dx=int(seconds * tick_pixels(editor)))
    facts['single_drag'] = {
        'clip': 'w151.male',
        'revision': [revision_before, editor.state.revision],
        'stored_changed': changed_ids(before_stored, stored(editor)),
        'solved_moved': [key for key in before_solved
                         if before_solved[key] != solved(editor)[key]],
        'start_ticks': [before_solved['w151.male'][0], solved(editor)['w151.male'][0]],
        'delta_ticks': solved(editor)['w151.male'][0] - before_solved['w151.male'][0],
        'delta_is_whole_frames': (solved(editor)['w151.male'][0]
                                  - before_solved['w151.male'][0])
                                 % editor.state.project.frame_ticks == 0,
        'seconds': round(time.monotonic() - started, 4)}

    # (3) an explicit group drag moves exactly the pair.
    click(app, editor, bar_point(editor, 'w151.male'))
    click(app, editor, bar_point(editor, 'w151.chinese'), QtCore.Qt.ControlModifier)
    before_stored = stored(editor)
    drag(app, editor, bar_point(editor, 'w151.male'), dx=int(0.2 * tick_pixels(editor)))
    facts['group_drag'] = {'clip_ids': sorted(editor.timeline.last_gesture.get('clip_ids', ())),
                           'stored_changed': changed_ids(before_stored, stored(editor)),
                           'command': sorted(editor.timeline.last_gesture.get('clip_ids', ()))}

    # (4) an explicit link, and the follower named in the notice it produces.
    editor.state.select('layer.subtitle')
    bound = editor.state.bind_start('layer.subtitle', 'w151.female', edge='end')
    click(app, editor, bar_point(editor, 'w151.female'))
    drag(app, editor, bar_point(editor, 'w151.female'), dx=int(0.2 * tick_pixels(editor)))
    facts['follower'] = {'bind_changed': bound.changed,
                         'notes': [notice.message for notice in editor.state.last_notices],
                         'moved': [notice.detail.get('moved')
                                   for notice in editor.state.last_notices],
                         'subtitle_start': solved(editor)['layer.subtitle'][0],
                         'female_end': solved(editor)['w151.female'][1]}

    # (5) trim, then undo: every object back to where it was.
    before_stored, before_solved = stored(editor), solved(editor)
    editor.state.select('w151.chinese')
    item = editor.timeline.item('w151.chinese')
    rect = editor.timeline.bar_rect(item)
    handle = QtCore.QPoint(int(rect.right() - 3), int(rect.center().y()))
    drag(app, editor, handle, dx=-int(0.25 * tick_pixels(editor)))
    trimmed_solved = solved(editor)
    facts['trim'] = {
        'clip': 'w151.chinese',
        'stored_changed': changed_ids(before_stored, stored(editor)),
        'start_kept': trimmed_solved['w151.chinese'][0] == before_solved['w151.chinese'][0],
        'end_delta_ticks': trimmed_solved['w151.chinese'][1] - before_solved['w151.chinese'][1],
        'source_untouched': editor.state.project.clip('w151.chinese').source.source_end}
    revision_after_trim = editor.state.revision
    editor.action_undo.trigger()
    settle(app)
    facts['undo_after_trim'] = {
        'revision': [revision_after_trim, editor.state.revision],
        'stored_identical': stored(editor) == before_stored,
        'solved_identical': solved(editor) == before_solved}
    editor.action_redo.trigger()
    settle(app)
    facts['redo'] = {'solved_identical_to_trim': solved(editor) == trimmed_solved,
                     'revision': editor.state.revision}

    # (6) a cut on a per-record clip is refused; a cut on the intro layer works.
    before_stored = stored(editor)
    revision_before = editor.state.revision
    click(app, editor, bar_point(editor, 'w151.chinese', 0.4), QtCore.Qt.AltModifier)
    facts['split_refused'] = {
        'clip': 'w151.chinese',
        'project_unchanged': stored(editor) == before_stored,
        'revision': [revision_before, editor.state.revision],
        'notices': [notice.code for notice in editor._last_notices],
        'headline': [notice.headline() for notice in editor._last_notices]}
    facts['split_intro'] = split_the_intro(editor, app)
    facts['style_edit'] = edit_the_font_size(editor, app)
    # Everything above left the document in a deliberately broken teaching state
    # (that is what the notices are about).  Undo the whole battery and prove the
    # imported revision comes back exactly and is deliverable again - which is also
    # the deepest undo/redo check the editor can make.
    while editor.state.can_undo:
        editor.state.undo()
    editor.after_edit(reason='已全部撤销')
    settle(app)
    facts['undo_to_the_imported_revision'] = {
        'revision': editor.state.revision,
        'everything_restored': stored(editor) == imported,
        'blockers': [notice.code for notice in
                     blocking_notices(editor.folder, editor.state.project)],
        'log_notices': [notice.code for notice in editor._last_notices]}

    # (7) mode switching does not touch the business data.
    before = editor.state.project.to_dict()
    modes = []
    for index in (1, 0, 1, 0):
        editor.mode_box.setCurrentIndex(index)
        settle(app)
        modes.append(editor.state.mode)
        if editor.state.project.to_dict() != before:
            raise SystemExit('switching mode changed the document')
    facts['modes'] = {'sequence': modes, 'document_identical': True}
    return facts


def split_the_intro(editor, app):
    """Cut the countdown in two through A's command, then put it back.

    This is acceptance item 3's other half: a split that *happens* (the refusal for
    a per-record clip is recorded just above), the delivery gate it raises while
    B's projection still draws one window per layer, and an undo that restores every
    object.
    """
    from desktop.editor_export import blocking_notices
    clip = editor.state.project.intro_clip()
    if clip is None:
        return {'available': False, 'reason': 'this project has no intro layer'}
    before = stored(editor)
    # Select it the way a member does - a click on its bar - so the property panel
    # (and through it the button's enabled state) is refreshed exactly as in use.
    click(app, editor, bar_point(editor, clip.id))
    check = editor.state.split_check(clip.id)
    editor.seek(clip.duration_ticks // 2)
    settle(app)
    enabled_before = editor.split_button.isEnabled()
    editor.split_button.click()
    settle(app)
    halves = [item.id for item in editor.state.project.clips if item.role == 'intro']
    facts = {'available': True, 'can_split': bool(check and check.ok),
             'button_enabled': enabled_before,
             'clip_id': clip.id, 'duration_ticks': clip.duration_ticks,
             'revision': editor.state.revision, 'halves': halves,
             'clip_count': len(editor.state.project.clips),
             'blockers_while_split': [notice.code for notice
                                      in blocking_notices(editor.folder,
                                                          editor.state.project)],
             'export_refused': editor.export() is None,
             'exporting_flag': editor.diagnostics()['exporting']}
    editor.action_undo.trigger()
    settle(app)
    facts['undo_restored_every_object'] = stored(editor) == before
    facts['revision_after_undo'] = editor.state.revision
    facts['blockers_after_undo'] = [notice.code for notice
                                    in blocking_notices(editor.folder, editor.state.project)]
    return facts


def edit_the_font_size(editor, app, size=520.0):
    """Change one role's size through ``SetStyle`` and read it back where it matters.

    "Where it matters" is the merged table the canvas and the exporter both use, and
    the placement the canvas actually receives - not the spin box.
    """
    from word_video.application.styles import merged_styles
    clip = next(item for item in editor.state.project.clips if item.role == 'english')
    click(app, editor, bar_point(editor, clip.id))
    before_table = merged_styles(editor.state.plan().style_table())
    before_size = before_table['english']['size']
    canvas_before = placement_size(editor, 'english')
    editor.size_edit.setValue(size)
    editor.size_button.click()
    settle(app)
    after_table = merged_styles(editor.state.plan().style_table())
    facts = {'role': 'english', 'default_size': before_size,
             'override': editor.state.plan().style_table().get('english'),
             'merged_size': after_table['english']['size'],
             'canvas_placement_size': [canvas_before, placement_size(editor, 'english')],
             'canvas_height': editor.showing.session.canvas_height,
             'other_roles_unchanged': all(
                 after_table[role]['size'] == before_table[role]['size']
                 for role in before_table if role != 'english'),
             'revision': editor.state.revision,
             'notices': [notice.code for notice in editor._last_notices]}
    editor.size_reset.click()
    settle(app)
    facts['after_reset_size'] = merged_styles(
        editor.state.plan().style_table())['english']['size']
    facts['after_reset_canvas_size'] = placement_size(editor, 'english')
    return facts


def placement_size(editor, role):
    """The pixel size the canvas is handed for one role, at a tick where it is due."""
    session = editor.showing.session
    if session is None or session.display is None:
        return None
    item = next((entry for entry in session.plan.video + session.plan.audio
                 if entry.role == role), None)
    if item is None:
        return None
    due = session.display.placements_at((item.start_ticks + item.end_ticks) // 2)
    found = [placed.size for placed in due if placed.role == role]
    return found[0] if found else None


def layout_across_canvases(editor):
    """B7: the same placements at 320x180, 720p and 1080p - through W04's checker.

    Reused, not re-implemented: ``preview/verify_layout_binding.py`` is the thing
    that already compares canvases and it knows which quantities are exact (canvas
    fractions) and which are measured (a glyph's advance at a smaller size).  What
    W06 adds is the *input*: the editor's own solved plan and the table
    ``merged_styles`` produces, which is what the canvas actually draws.
    """
    from preview.verify_layout_binding import check
    from word_video.application.styles import style_fonts
    styles = editor.state.styles()
    paths, names = style_fonts(styles)
    evidence = check(editor.showing.session.plan, styles, paths, names)
    return {'ok': evidence['ok'], 'problem_count': evidence['problem_count'],
            'problems': evidence['problems'],
            'canvases': evidence['canvases']}


# ------------------------------------------------- the preview's real cost
def preview_cost(editor, folder, state, workspace):
    """Cold versus warm preview cost, and where the cache that makes the difference lives.

    A member feels three separate things and they must not be averaged into one
    number: building the picture's windowed copy and its 720p proxy (once per
    window length), preparing the speech at the project's speed (once per asset),
    and opening a session over files that are already there (every edit).
    """
    import shutil
    from preview.sources import (DEFAULT_PROXY_BUDGET, proxy_cache_dir,
                                 resolve_preview_source)
    fact = {}
    background = folder.delivery.background
    plan = editor.showing.session.plan
    item = next((entry for entry in plan.video if entry.role == 'background'), None)
    needed = (item.duration_ticks / 720000.0) if item is not None else None
    cold_dir = Path(workspace) / 'proxy-cold'
    if cold_dir.exists():
        shutil.rmtree(cold_dir)
    if background:
        started = time.monotonic()
        resolve_preview_source(background, 720, cache_dir=cold_dir, needed_seconds=needed)
        fact['proxy_cold_seconds'] = round(time.monotonic() - started, 3)
        fact['proxy_cold_files'] = sorted(
            (path.name.split('.')[-2], path.stat().st_size)
            for path in cold_dir.rglob('*') if path.is_file())
        started = time.monotonic()
        resolve_preview_source(background, 720, cache_dir=cold_dir, needed_seconds=needed)
        fact['proxy_warm_seconds'] = round(time.monotonic() - started, 4)

    started = time.monotonic()
    folder.preview_assets(state.project, refresh=True)
    fact['prepared_speech_seconds'] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    editor._rebuild_preview(editor.showing.position_ticks)
    from PySide6 import QtWidgets
    settle(QtWidgets.QApplication.instance())
    fact['open_warm_seconds'] = (None if editor.showing.session is None
                                 else round(editor.showing.session.open_seconds, 4))
    fact['rebuild_wall_seconds'] = round(time.monotonic() - started, 3)

    cache = proxy_cache_dir(folder.preview_dir)
    files = [path for path in cache.rglob('*') if path.is_file()] if cache.is_dir() else []
    fact['cache'] = {
        'project_preview_dir': str(folder.preview_dir),
        'proxy_cache_dir': str(cache),
        'proxy_files': len(files),
        'proxy_bytes': sum(path.stat().st_size for path in files),
        'budget_bytes': DEFAULT_PROXY_BUDGET,
        'naming': 'source stem + window length in ms + source timestamp, then the'
                  ' proxy is <same>.720p.<...>.mp4',
        'inside_the_project_folder': str(cache).startswith(str(folder.path)),
        'survives_restart': True,
        'invalidated_by': ['source file changed (its timestamp is in the name)',
                           'the lesson window length changed by more than the bucket',
                           'prune_proxies() over the byte budget, oldest first',
                           'deleting .preview/ by hand: costs one cold start,'
                           ' changes nothing about the delivery']}
    return fact


# ------------------------------------------------- the style in the product
def ass_sizes(path):
    """``{style name: font size}`` read back out of a delivered ``captions.ass``."""
    sizes = {}
    text = Path(path).read_text(encoding='utf-8-sig', errors='replace')
    for line in text.splitlines():
        if line.startswith('Style:'):
            fields = line[len('Style:'):].split(',')
            if len(fields) > 2:
                sizes[fields[0].strip()] = fields[2].strip()
    return sizes


def band_brightness(video, seconds, crop='iw:ih/8:0:ih*0.30'):
    """Mean luma of a band of one frame, through ffmpeg's own statistics.

    The same probe the acceptance suite uses (``metadata=print:file=-`` writes to
    stdout): "the caption got bigger" has to be a number read off the delivered
    picture, not a widget value.  The path is never put *inside* the filter string -
    a Windows path's colon breaks the filtergraph, which is how this failed first.
    """
    import subprocess
    from word_video.media import executable
    result = subprocess.run(
        [executable('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-ss', '%.4f' % seconds,
         '-i', str(video), '-frames:v', '1', '-vf',
         'crop=%s,signalstats,metadata=print:file=-' % crop, '-f', 'null', '-'],
        capture_output=True, check=False)
    text = (result.stdout or b'').decode('utf-8', 'replace')
    values = re.findall(r'lavfi\.signalstats\.YAVG=([0-9.]+)', text)
    return round(float(values[-1]) / 255.0, 5) if values else None


def style_in_the_delivery(editor, app, output, seconds, sizes=(800.0, 700.0, 600.0)):
    """B4's other half: change a size, deliver again, read the change off the product.

    The comparison is between two published runs of the same lesson: the ASS the
    renderer burns in, the SRT and the draft (which must not move), and a band of
    the MP4 itself.  A style that only changed a spin box would leave the ASS and
    the pixels identical, which is what this is for.

    The size is chosen by *asking the layout* first: B's safe-area guard refuses a
    box that leaves the frame (``LayoutOverflow``), and rendering for half a minute
    to learn that would be silly - and the sizes it refuses are themselves worth
    reporting, because they show the guard is alive.
    """
    from desktop.editor_project import ProjectFolder
    from desktop.editor_window import EditorWindow

    path = editor.folder.path
    before_ass = ass_sizes(Path(editor.last_export.run_dir) / 'captions.ass')
    before_srt = sorted(Path(editor.last_export.run_dir).rglob('*.srt'))
    before_lines = [(item.name, item.read_bytes()) for item in before_srt]
    before_band = band_brightness(Path(editor.last_export.run_dir) / 'video' / 'video.mp4',
                                  seconds)
    before_document = editor.state.project.to_dict()

    window = EditorWindow()
    window.attach(ProjectFolder.open(path))
    window.resize(*WINDOW_SIZE)
    window.show()
    settle(app)
    role = 'english'
    window.state.select(next(item.id for item in window.state.project.clips
                             if item.role == role))
    fact = {'role': role, 'probes': [], 'refused_by_the_guard': []}

    def fits(size):
        window.size_edit.setValue(size)
        window.size_button.click()
        settle(app)
        display = window.showing.session.display
        overflowing = [placed.clip_id for placed in (display.overflowing() if display else ())]
        fact['probes'].append({'size': size, 'overflowing': overflowing})
        if overflowing:
            fact['refused_by_the_guard'].append({'size': size, 'clip_ids': overflowing})
            window.size_reset.click()
            settle(app)
        return not overflowing

    # Bisect for the largest size B's safe-area guard accepts: the guard is the
    # layout's rule (not the editor's), and finding its edge is what makes the
    # number below meaningful rather than lucky.
    low, high, chosen = 450.0, 900.0, None
    while high - low > 10.0:
        middle = round((low + high) / 2.0, 1)
        if fits(middle):
            chosen, low = middle, middle
        else:
            high = middle
    fact['chosen_size'] = chosen
    if chosen is None:
        window.close()
        settle(app)
        return fact
    # The last probe may have been a refusal (which resets the style), so the
    # chosen size is applied once more before delivering - a bisection leaves the
    # state wherever it stopped, and the export must carry the number being tested.
    fits(chosen)
    fact['style_override_before_export'] = window.state.plan().style_table().get(role)
    from desktop.editor_export import export_folder
    outcome = export_folder(window.folder, window.state, output=output, run_name='styled')
    fact.update({'ok': outcome.ok, 'run_dir': outcome.run_dir,
                 'seconds': round(outcome.seconds, 3),
                 'notices': [notice.code for notice in outcome.notices]})
    if outcome.ok:
        after_ass = ass_sizes(Path(outcome.run_dir) / 'captions.ass')
        fact['ass_before'] = before_ass
        fact['ass_after'] = after_ass
        fact['english_grew'] = float(after_ass.get(role, 0)) > float(before_ass.get(role, 0))
        fact['other_styles_unchanged'] = all(
            after_ass.get(name) == value for name, value in before_ass.items()
            if name != role)
        after_lines = [(item.name, item.read_bytes())
                       for item in sorted(Path(outcome.run_dir).rglob('*.srt'))]
        fact['srt_unchanged'] = [name for name, _ in before_lines] == \
            [name for name, _ in after_lines] and \
            all(left[1] == right[1] for left, right in zip(before_lines, after_lines))
        fact['band_before'] = before_band
        fact['band_after'] = band_brightness(Path(outcome.run_dir) / 'video' / 'video.mp4',
                                             seconds)
        fact['band_changed'] = fact['band_before'] != fact['band_after']
        fact['clip_table_unchanged'] = [item['id'] for item in
                                        window.state.project.to_dict()['clips']] == \
            [item['id'] for item in before_document['clips']]
        fact['style_override'] = window.state.plan().style_table().get(role)
    window.close()
    settle(app)
    return fact


def save_reopen_cycle(editor, app, project_dir):
    """Save, close, reopen, edit again, save again: nothing lost, nothing left.

    The reopened window is a *new* window over the folder the first one wrote, so
    "重开后编辑仍在" is decided by the file, not by an object still in memory.
    """
    from desktop.editor_project import ProjectFolder
    from desktop.editor_window import EditorWindow
    path = Path(project_dir)
    editor.state.select('w151.male')
    drag(app, editor, bar_point(editor, 'w151.male'), dx=int(0.2 * tick_pixels(editor)))
    solved_before = solved(editor)
    revision_saved = editor.state.revision
    editor.save()
    leftovers = sorted(item.name for item in path.iterdir() if '.tmp' in item.name)
    document_saved = editor.state.project.to_dict()
    editor.close()
    settle(app)

    reopened = EditorWindow()
    reopened.attach(ProjectFolder.open(path))
    reopened.resize(*WINDOW_SIZE)
    reopened.show()
    settle(app)
    facts = {'path': str(path), 'revision_when_saved': revision_saved,
             'reopened_solved_identical': solved(reopened) == solved_before,
             'reopened_document_identical': reopened.state.project.to_dict() == document_saved,
             'reopened_dirty': reopened.state.dirty,
             'temp_files_after_first_save': leftovers,
             'preview_builds_reopened': reopened.showing.preview_builds}
    reopened.state.select('w151.male')
    drag(app, reopened, bar_point(reopened, 'w151.male'), dx=int(0.2 * tick_pixels(reopened)))
    reopened.save()
    facts['revision_after_second_edit'] = reopened.state.revision
    facts['revision_is_forward'] = reopened.state.revision > revision_saved
    facts['temp_files_after_second_save'] = sorted(
        item.name for item in path.iterdir() if '.tmp' in item.name)
    facts['second_edit_survived_the_save'] = \
        reopened.state.project.clip('w151.male').start.ticks != \
        document_saved_clip_start(document_saved, 'w151.male')
    reopened.close()
    settle(app)
    return facts


def document_saved_clip_start(document, clip_id):
    for clip in document['clips']:
        if clip['id'] == clip_id:
            return clip['start'].get('ticks')
    return None


WINDOW_SIZE = (1280, 840)


# ------------------------------------------------------------- the delivery
def export_through_window(editor, app, timeout=2400):
    """Press 导出 and wait for the worker; the report is the exporter's own."""
    started = time.monotonic()
    editor.export()
    deadline = time.monotonic() + timeout
    while editor.diagnostics()['exporting'] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()
    outcome = editor.last_export
    if outcome is None:
        return {'started': False, 'seconds': round(time.monotonic() - started, 3)}
    payload = outcome.to_dict()
    payload['seconds_wall'] = round(time.monotonic() - started, 3)
    payload['notices'] = [notice.code for notice in outcome.notices]
    return payload


def run_checker(run_dir, wordlist, out_json):
    """The independent acceptance checker, quoted verbatim."""
    checker = work_root() / 'tools' / 'm0' / 'accept_range.py'
    if not checker.is_file():
        return {'ran': False, 'reason': 'checker not present at %s' % checker}
    command = [sys.executable, str(checker), str(run_dir),
               '--source', str(wordlist), '--range', '%d-%d' % (WORDS[0], WORDS[-1]),
               '--cleaning', 'v2', '--pixels', 'all', '--json', str(out_json)]
    started = time.monotonic()
    # utf-8 explicitly: the checker prints Chinese, and a Windows console default
    # (gbk) turns reading its own output into a UnicodeDecodeError that hides the
    # verdict - which is exactly the kind of "the tool said nothing" failure this
    # project has already been bitten by once.
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=3600)
    payload = {'ran': True, 'command': command, 'returncode': result.returncode,
               'seconds': round(time.monotonic() - started, 3),
               'stdout_tail': (result.stdout or '').strip().splitlines()[-12:],
               'stderr_tail': (result.stderr or '').strip().splitlines()[-6:]}
    if Path(out_json).is_file():
        report = json.loads(Path(out_json).read_text(encoding='utf-8'))
        payload['verdict'] = report.get('verdict')
        payload['failures'] = report.get('failures')
        payload['faces'] = sorted(report)
        payload['face_summaries'] = {
            name: ({key: value[key] for key in ('problem_count', 'problems')
                    if key in value} if isinstance(value, dict) else value)
            for name, value in report.items()
            if name in ('integrity', 'timing', 'srt', 'draft', 'pixels', 'mix',
                        'video', 'audio_e2e')}
    return payload


# ------------------------------------------------------------------- driver
def run(workspace, fixture, out_path):
    """Import twice: one folder is edited (the gestures), one is delivered.

    Two folders, not one, and for a reason worth stating: the independent
    acceptance checker recomputes each reading stage's length from the recording
    and the rhythm, so a delivery whose speech stages were trimmed **cannot** pass
    it - by design, that is the teaching-rhythm contract, not an editor bug.  So
    the exported folder is the one the same import path just created and nobody
    edited, and every edit is proven per object on its own copy.  Both come out of
    the window's own import, and no JSON is written by hand anywhere.
    """
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from desktop.editor_import import resolve_wordlist
    from desktop.editor_window import EditorWindow

    import_root = Path(workspace)
    if import_root.exists():
        import shutil
        shutil.rmtree(import_root)
    import_root.mkdir(parents=True, exist_ok=True)

    evidence = {'started': time.strftime('%Y-%m-%d %H:%M:%S'),
                'python': sys.version.split()[0],
                'scale_factor_env': os.environ.get('QT_SCALE_FACTOR'),
                'platform': os.environ.get('QT_QPA_PLATFORM'),
                'fixture': str(fixture), 'workspace': str(workspace)}

    # -- the folder whose edits are measured ------------------------------
    edit_dir = import_root / 'edit'
    editor = EditorWindow()
    started = time.monotonic()
    folder = editor.import_request(str(fixture), str(edit_dir))
    evidence['import'] = {'seconds': round(time.monotonic() - started, 3),
                          'project_dir': str(edit_dir),
                          'records': len(folder.project.records),
                          'clips': len(folder.project.clips),
                          'files': sorted(item.name for item in edit_dir.iterdir()),
                          'intro_s_measured': folder.project.intro_s,
                          'delivery': folder.delivery.to_dict(),
                          'diagnostics': [notice.code for notice in
                                          folder.diagnostics(folder.project)]}
    editor.resize(*WINDOW_SIZE)
    editor.show()
    settle(app)
    evidence['preview'] = {'open_seconds': editor.showing.session.open_seconds,
                           'canvas': '%dx%d' % (editor.showing.session.canvas_width,
                                                editor.showing.session.canvas_height),
                           'sources': {key: value.to_dict() for key, value
                                       in editor.showing.session.sources.items()},
                           'builds': editor.showing.preview_builds}

    evidence['interactions'] = interactions(editor, app)
    # These two need the live session, so they run before the save/reopen cycle
    # closes this window (the reopened one is a separate window over the same files).
    evidence['preview_cost'] = preview_cost(editor, editor.folder, editor.state, workspace)
    evidence['preview_cost']['open_cold_seconds'] = evidence['preview'].get('open_seconds')
    evidence['layout_across_canvases'] = layout_across_canvases(editor)
    evidence['interactions']['save_reopen'] = save_reopen_cycle(editor, app, edit_dir)

    started = time.monotonic()
    editor.state.select('w151.male')
    editor.on_clips_moved(('w151.male',), int(0.2 * 720000))
    evidence['interactions']['edit_to_preview_seconds'] = round(time.monotonic() - started, 3)
    evidence['interactions']['preview_builds_after_edits'] = editor.showing.preview_builds
    evidence['window'] = editor.diagnostics()
    editor.close()
    settle(app)

    # -- the folder that is delivered -------------------------------------
    deliver_dir = import_root / 'project'
    window = EditorWindow()
    started = time.monotonic()
    deliver = window.import_request(str(fixture), str(deliver_dir))
    evidence['delivery_import'] = {
        'seconds': round(time.monotonic() - started, 3),
        'project_dir': str(deliver_dir),
        'revision': deliver.project.revision,
        'diagnostics': [notice.code for notice in deliver.diagnostics(deliver.project)]}
    window.resize(*WINDOW_SIZE)
    window.show()
    settle(app)
    # The delivered folder is the import's own document, untouched: the report can
    # show that by comparing it with the file the import wrote.
    evidence['delivery_import']['document_is_the_imported_one'] = (
        window.state.project.to_dict() == deliver.project.to_dict())
    wordlist = resolve_wordlist(json.loads(Path(fixture).read_text(encoding='utf-8-sig')))
    evidence['export'] = export_through_window(window, app)
    run_dir = evidence['export'].get('run_dir') or ''
    if run_dir:
        evidence['files'] = sorted(str(path.relative_to(run_dir)).replace('\\', '/')
                                   for path in Path(run_dir).rglob('*') if path.is_file())
        evidence['acceptance'] = run_checker(run_dir, wordlist,
                                             import_root / 'acceptance-report.json')
        # B4: the same lesson delivered again with one role's size changed, so the
        # change is read back off the products rather than off the panel.
        midpoint = next(item for item in window.state.project.clips
                        if item.role == 'english')
        start = window.state.clip_range(midpoint.id)[0] / 720000.0 + 0.3
        evidence['style_in_delivery'] = style_in_the_delivery(
            window, app, import_root / 'styled-out', start)
        if evidence['style_in_delivery'].get('run_dir'):
            evidence['acceptance_styled'] = run_checker(
                evidence['style_in_delivery']['run_dir'], wordlist,
                import_root / 'acceptance-report-styled.json')
    evidence['environment'] = {
        'tts_credentials_present': sorted(name for name in os.environ
                                          if 'TTS' in name.upper() or 'VOLC' in name.upper()),
        'network_used': False,
        'provider_kind': 'local (existing cached audio prepared with ffmpeg)',
        'target_machine_measured': False,
        'target_note': 'Ryzen 9 7940H / 32 GB with discrete graphics: an upper bound,'
                       ' not the ordinary integrated-graphics thin-and-light the'
                       ' requirement is written for'}
    evidence['delivery_window'] = window.diagnostics()
    window.close()
    settle(app)
    evidence['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return evidence


def scale_probe(scale, workspace):
    """One fresh process at one scale factor: are the gestures still right?"""
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from desktop.editor_import import import_request
    from desktop.editor_window import EditorWindow
    from desktop.timeline_widget import ROW_HEIGHT

    target = Path(workspace) / ('scale-%s' % scale)
    if target.exists():
        import shutil
        shutil.rmtree(target)
    window = EditorWindow()
    window.import_request(str(work_root() / FIXTURE), str(target))
    window.resize(*WINDOW_SIZE)
    window.show()
    settle(app)
    facts = {'requested_scale': scale, 'device_pixel_ratio': window.devicePixelRatioF(),
             'window': [window.width(), window.height()],
             'timeline_width': window.timeline.diagnostics()['width'],
             'row_height': ROW_HEIGHT}
    hits = {}
    for clip_id in ('w151.female', 'w151.male', 'w151.chinese', 'layer.title'):
        click(app, window, bar_point(window, clip_id))
        hits[clip_id] = window.state.selection == (clip_id,)
    facts['clicks_hit_the_intended_clip'] = hits
    before = solved(window)
    drag(app, window, bar_point(window, 'w151.male'), dx=int(0.3 * tick_pixels(window)))
    after = solved(window)
    moved = [key for key in before if before[key] != after[key]]
    facts['drag'] = {'moved': moved, 'revision': window.state.revision,
                     'delta_ticks': after['w151.male'][0] - before['w151.male'][0]}
    item = window.timeline.item('w151.chinese')
    rect = window.timeline.bar_rect(item)
    from PySide6 import QtCore
    handle = QtCore.QPoint(int(rect.right() - 3), int(rect.center().y()))
    before_trim = solved(window)
    drag(app, window, handle, dx=-int(0.2 * tick_pixels(window)))
    after_trim = solved(window)
    facts['trim'] = {'moved': [key for key in before_trim
                               if before_trim[key] != after_trim[key]],
                     'chinese_end_delta': after_trim['w151.chinese'][1]
                                          - before_trim['w151.chinese'][1]}
    facts['notice_panel_usable'] = window.notice_area.width() > 50
    facts['property_panel_readable'] = window.property_labels['clip'].text() != ''
    facts['preview_error'] = window.showing.preview_error
    window.close()
    settle(app)
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=None, help='write the evidence JSON here')
    parser.add_argument('--workspace', default=None)
    parser.add_argument('--fixture', default=None)
    parser.add_argument('--scale-only', action='store_true',
                        help='interaction battery only; used by the DPI children')
    parser.add_argument('--scale', default=None, help='scale factor for --scale-only')
    parser.add_argument('--json', default=None, help='where a child writes its result')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    root = work_root()
    args.fixture = args.fixture or str(root / FIXTURE)
    args.workspace = args.workspace or str(root / 'runtime' / 'tmp' / 'C' / 'w06')
    Path(args.workspace).mkdir(parents=True, exist_ok=True)

    if args.scale_only:
        payload = scale_probe(args.scale or os.environ.get('QT_SCALE_FACTOR') or '1',
                              args.workspace)
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if args.json:
            Path(args.json).write_text(text, encoding='utf-8')
        print(text)
        return 0

    evidence = run(args.workspace, Path(args.fixture), args.out)
    evidence['scales'] = {}
    for scale in SCALES:
        child_json = Path(args.workspace) / ('scale-%s.json' % scale)
        environment = dict(os.environ)
        environment['QT_SCALE_FACTOR'] = scale
        environment['QT_QPA_PLATFORM'] = 'offscreen'
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--scale-only',
             '--scale', scale, '--workspace', args.workspace, '--json', str(child_json)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=1800, env=environment, cwd=str(CHECKOUT))
        if child_json.is_file():
            evidence['scales'][scale] = json.loads(child_json.read_text(encoding='utf-8'))
        else:
            evidence['scales'][scale] = {'error': (result.stderr or '').strip()[-600:],
                                         'returncode': result.returncode}
    payload = json.dumps(evidence, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload, encoding='utf-8')
    print(payload)
    return 0


if __name__ == '__main__':
    sys.exit(main())
