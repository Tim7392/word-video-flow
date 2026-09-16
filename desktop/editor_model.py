"""The editor's own state: what is selected, what was just refused, what is dirty.

Why there is a layer here at all
--------------------------------
The window is not the model.  Widgets come and go, and a member's edit has to be
reproducible in a test with no display, no sound device and no timer - so every
decision the editor makes about *editing* lives here, in one Qt-free object:

* **which clips are selected**, and the rule that a plain click selects exactly one
  (a single-clip move must not drag an unselected neighbour; a group move is an
  explicit ``MoveClips`` after an explicit multi-select);
* **every edit goes to A's application commands** through
  :class:`word_video.application.session.Session`.  This module never builds a
  ``Clip``, never writes ``duration_ticks`` and never touches ``revision``: it
  picks a command, hands it to ``apply``, and turns the structured refusal that
  comes back into a :class:`~desktop.editor_notices.Notice` with a repair path;
* **undo, redo, save and the template/advanced mode** are A's ``Session`` too, so
  the number a member sees never goes backwards and switching mode cannot change
  the document.

Solved time is *cached by revision* and never written back: the timeline and the
canvas both ask :meth:`EditorState.plan` for the one solved plan of the revision
on screen.  ``render_plan`` is used rather than the full ``solve`` because an
overlap or a wrong teaching order must not take the timeline away from the member
who is fixing it - those are reported as notices instead, and the same
``CuePlan`` the delivery uses is asked only when it is time to deliver.
"""
from dataclasses import dataclass

from word_video.application import commands as command_module
from word_video.application.session import MODE_ADVANCED, MODE_TEMPLATE, MODES, Session
from word_video.domain.compile import render_plan
from word_video.domain.errors import ProjectError
from word_video.domain.timebase import TICKS_PER_SECOND, half_up, rational

from . import editor_notices as notices

__all__ = ['EditorState', 'SNAP_TOLERANCE_TICKS', 'snap_to_grid', 'nearest_candidate',
           'SPLIT_COMMAND', 'split_available']

#: How close (in ticks) a dragged edge has to be to a neighbour's edge to stick.
#: 0.02 s: close enough to be deliberate, far enough not to fight a frame grid.
SNAP_TOLERANCE_TICKS = 14400
#: The command name a split needs.  A owns it; until it exists the editor refuses
#: the edit, says so, and offers what the current command set *can* do.
SPLIT_COMMAND = 'SplitClip'


def split_available():
    """Whether this checkout's application layer can create a clip (i.e. split)."""
    return getattr(command_module, SPLIT_COMMAND, None) is not None


def snap_to_grid(ticks, frame_ticks):
    """Round a tick to the nearest whole frame; the manifest is frame-based."""
    if frame_ticks <= 0:
        return int(ticks)
    steps = half_up(rational(int(ticks)) / frame_ticks)
    return int(steps) * int(frame_ticks)


def nearest_candidate(ticks, candidates, tolerance=SNAP_TOLERANCE_TICKS):
    """The nearest magnetic target within ``tolerance``, else ``ticks`` itself."""
    best, distance = None, None
    for candidate in candidates:
        gap = abs(int(candidate) - int(ticks))
        if gap <= tolerance and (distance is None or gap < distance):
            best, distance = int(candidate), gap
    return int(ticks) if best is None else best


@dataclass
class EditOutcome:
    """What one edit did: the command result, the plan, and what to tell the member."""

    changed: bool
    command: object = None
    moved: tuple = ()
    notices: tuple = ()

    def to_dict(self):
        return {'changed': self.changed,
                'command': type(self.command).__name__ if self.command else '',
                'moved': list(self.moved),
                'notices': [notice.to_dict() for notice in self.notices]}


class EditorState:
    """One editable project revision plus selection, notices and the plan cache."""

    def __init__(self, project, *, media=None, intro=None, path='', mode=MODE_TEMPLATE,
                 session=None):
        self.session = session if session is not None else Session(project=project, mode=mode)
        self.project_path = str(path or '')
        self.media = dict(media or {})
        self.intro = intro
        self.selection = ()
        self.last_notices = ()
        self._saved_revision = self.session.revision
        self._plan = None
        self._plan_error = None
        self._plan_revision = None
        self._cues = None
        self._cues_error = None
        self._cues_revision = None

    # -- reading ---------------------------------------------------------
    @property
    def project(self):
        return self.session.project

    @property
    def revision(self):
        return self.session.revision

    @property
    def mode(self):
        return self.session.mode

    @property
    def can_undo(self):
        return self.session.can_undo

    @property
    def can_redo(self):
        return self.session.can_redo

    @property
    def dirty(self):
        """True while there are edits the file on disk does not have.

        Undo raises the revision like any other edit, so a member who undoes back
        to the saved *content* is still asked whether to save: the revision never
        goes backwards (architecture §10) and pretending otherwise here would make
        the two disagree the moment a job froze the older number.
        """
        return self.revision != self._saved_revision

    def mark_saved(self):
        self._saved_revision = self.revision
        return self._saved_revision

    # -- solved time -----------------------------------------------------
    def set_intro(self, measurement):
        """Adopt the intro layer's measurement (A-4: the media owns its length).

        The measurement is an *input* to solving, exactly like a speech length, so
        it is handed in here rather than read from the project - and a new
        measurement drops the plan cache, because the intro's length may have
        changed with the file.
        """
        if measurement is not self.intro:
            self.intro = measurement
            self._invalidate()
        return self.intro

    def set_media_table(self, media, *, invalidate=True):
        """Adopt a new media table (a file was replaced); drop the solved cache.

        The revision does not change when a *file* changes, so the plan cache has
        to be dropped explicitly or the timeline would keep drawing the old
        measurements.  Named ``set_media_table`` and not ``set_media`` because
        :meth:`set_media` below is A's ``SetMediaSlice`` command on one clip, and a
        name that means two things is how a click ends up editing the wrong thing.
        """
        self.media = dict(media or {})
        if invalidate:
            self._invalidate()
        return self.media

    def _invalidate(self):
        self._plan_revision = None
        self._cues_revision = None

    def plan(self):
        """The one solved render plan of this revision, or ``None`` if unsolvable."""
        if self._plan_revision == self.revision:
            return self._plan
        try:
            self._plan = render_plan(self.project, self.media, self.intro)
            self._plan_error = None
        except ProjectError as error:
            self._plan, self._plan_error = None, error
        self._plan_revision = self.revision
        return self._plan

    @property
    def plan_error(self):
        self.plan()
        return self._plan_error

    def cues(self):
        """The five-track delivery projection of this revision, or ``None``."""
        if self._cues_revision == self.revision:
            return self._cues
        from word_video.domain.compile import cue_plan
        try:
            self._cues, self._cues_error = cue_plan(self.project, self.media, self.intro), None
        except ProjectError as error:
            self._cues, self._cues_error = None, error
        self._cues_revision = self.revision
        return self._cues

    @property
    def cues_error(self):
        self.cues()
        return self._cues_error

    def ranges(self):
        """``{clip_id: (start_ticks, end_ticks)}`` from the solved plan."""
        plan = self.plan()
        if plan is None:
            return {}
        ranges = {}
        for item in plan.video + plan.audio:
            ranges[item.clip_id] = (item.start_ticks, item.end_ticks)
        return ranges

    def clip_range(self, clip_id):
        """Solved range of one clip; falls back to ``(start, start+duration)``."""
        found = self.ranges().get(clip_id)
        if found is not None:
            return found
        clip = self.project.clip(clip_id)
        if clip is None or clip.duration_ticks is None or not clip.start.is_absolute:
            return None
        return clip.start.ticks, clip.start.ticks + clip.duration_ticks

    def candidates(self, exclude=()):
        """Magnetic targets: every clip's edges, the ends, and whole seconds.

        Whole seconds are in the list because that is the unit a member reads off
        the ruler; the frame grid is applied separately (``snap_to_grid``), so it
        does not need to be enumerated here.
        """
        points = {0, self.total_ticks()}
        for clip_id, (start, end) in self.ranges().items():
            if clip_id in exclude:
                continue
            points.add(start)
            points.add(end)
        seconds = self.total_ticks() // TICKS_PER_SECOND + 1
        points.update(index * TICKS_PER_SECOND for index in range(int(seconds) + 1))
        return tuple(sorted(point for point in points if point >= 0))

    def total_ticks(self):
        plan = self.plan()
        if plan is not None:
            return plan.total_ticks
        return max((end for _, end in self.ranges().values()), default=0)

    # -- notices ---------------------------------------------------------
    def document_notices(self):
        """Everything about the *document* a member should see right now."""
        found = []
        plan = self.plan()
        if self._plan_error is not None and plan is None:
            found.append(notices.notice_from_error(self.project, self._plan_error))
        if plan is not None:
            for conflict in plan.conflicts:
                found.append(notices.notice_from_conflict(self.project, conflict, plan))
        error = self.cues_error
        if error is not None:
            found.append(notices.notice_from_error(self.project, error, plan=plan))
        return tuple(self.last_notices) + tuple(found)

    # -- selection -------------------------------------------------------
    def set_selection(self, clip_ids):
        """Explicitly set the selection, dropping ids this revision does not have."""
        kept = []
        for clip_id in clip_ids:
            if self.project.clip(clip_id) is not None and clip_id not in kept:
                kept.append(clip_id)
        self.selection = tuple(kept)
        return self.selection

    def select(self, clip_id):
        """A plain click: exactly one clip.  Never extends by itself."""
        return self.set_selection((clip_id,))

    def toggle(self, clip_id):
        """An explicit multi-select gesture (ctrl+click in the timeline)."""
        chosen = [item for item in self.selection if item != clip_id]
        if len(chosen) == len(self.selection):
            chosen.append(clip_id)
        return self.set_selection(chosen)

    def clear_selection(self):
        self.selection = ()
        return self.selection

    def selected_clips(self):
        return tuple(self.project.clip(clip_id) for clip_id in self.selection
                     if self.project.clip(clip_id) is not None)

    # -- editing ---------------------------------------------------------
    def apply(self, command, expected_revision=None):
        """One command through A's ``Session``; refusals become notices.

        ``Session.apply`` owns the history, the redo stack and the revision, but it
        returns the new session only - and a single-clip move still has to *say*
        who else is about to move, which is what ``CommandResult.notes`` carries.
        So the same pure function is read once for its report and then handed to
        the session for the authoritative state change: both calls see the
        identical immutable document, the function has no side effects, and the
        edit path is still A's - the editor does not keep a history of its own.
        """
        self.last_notices = ()
        try:
            result = command_module.apply(self.project, command, expected_revision)
            if result.changed:
                self.session = self.session.apply(command, expected_revision)
                self._invalidate()
        except ProjectError as error:
            notice = notices.notice_from_error(self.project, error, plan=self.plan())
            self.last_notices = (notice,)
            return EditOutcome(changed=False, command=command, notices=(notice,))
        told = notices.notice_from_result(self.project, result)
        self.last_notices = told
        return EditOutcome(changed=result.changed, command=command, moved=result.moved,
                           notices=told)

    def clamp_delta(self, delta_ticks, clip_ids=None):
        """Shorten a drag so no selected clip would start before zero.

        The clamp uses the *solved* start, which is what the member sees, so a
        linked clip is clamped by where it actually is rather than by an offset it
        does not display.
        """
        ids = tuple(clip_ids if clip_ids is not None else self.selection)
        if not ids or delta_ticks >= 0:
            return int(delta_ticks)
        ranges = self.ranges()
        earliest = None
        for clip_id in ids:
            found = ranges.get(clip_id)
            if found is not None and (earliest is None or found[0] < earliest):
                earliest = found[0]
        if earliest is None:
            return int(delta_ticks)
        return max(int(delta_ticks), -int(earliest))

    def move_selection(self, delta_ticks, *, snap=True, clip_ids=None):
        """Move the selection: one clip with ``MoveClip``, a group with ``MoveClips``."""
        ids = tuple(clip_ids if clip_ids is not None else self.selection)
        if not ids:
            return EditOutcome(changed=False)
        delta = self.clamp_delta(int(delta_ticks), ids)
        if snap:
            frame = self.project.frame_ticks
            delta = snap_to_grid(delta, frame) if frame else delta
        if delta == 0:
            return EditOutcome(changed=False)
        command = (command_module.MoveClip(clip_id=ids[0], delta_ticks=delta)
                   if len(ids) == 1
                   else command_module.MoveClips(clip_ids=ids, delta_ticks=delta))
        return self.apply(command)

    def move_to(self, clip_id, ticks):
        """Absolute move of one clip: ``UnbindStart`` is A's absolute-position verb."""
        return self.apply(command_module.UnbindStart(clip_id=clip_id, ticks=int(ticks)))

    def trim(self, clip_id, edge, ticks, *, snap=True):
        """Drag one edge to ``ticks``; the other edge and the source stay put."""
        if edge not in ('start', 'end'):
            raise ValueError("edge must be 'start' or 'end'")
        frame = self.project.frame_ticks
        value = int(ticks)
        if snap and frame:
            value = snap_to_grid(value, frame)
        value = max(0, value)
        command = (command_module.TrimClip(clip_id=clip_id, start_ticks=value)
                   if edge == 'start'
                   else command_module.TrimClip(clip_id=clip_id, end_ticks=value))
        return self.apply(command)

    def trim_to_source_length(self, clip_id):
        """Restore the target length of a truncated reading (source length wins)."""
        plan = self.plan()
        item = plan.item(clip_id) if plan is not None else None
        if item is None or item.source is None:
            return EditOutcome(changed=False)
        return self.trim(clip_id, 'end', item.start_ticks + item.source.duration_ticks,
                         snap=False)

    def set_media(self, clip_id, **fields):
        """Speed, gain or the source window; A applies speed exactly once."""
        return self.apply(command_module.SetMediaSlice(clip_id=clip_id, **fields))

    def bind_start(self, clip_id, ref_id, edge='end', offset_ticks=0):
        """The *explicit* linkage: the follower then starts where the reference ends."""
        return self.apply(command_module.BindStart(clip_id=clip_id, ref_id=ref_id,
                                                   edge=edge, offset_ticks=offset_ticks))

    def unbind_start(self, clip_id, ticks=None):
        """Replace a link with an absolute position (defaults to where it is now)."""
        if ticks is None:
            found = self.clip_range(clip_id)
            if found is None:
                return EditOutcome(changed=False)
            ticks = found[0]
        return self.apply(command_module.UnbindStart(clip_id=clip_id, ticks=int(ticks)))

    def split_at_ticks(self, clip_id, at_ticks, new_clip_id=''):
        """Cut one clip in two through A's ``SplitClip``.

        The command refuses what cannot be split - a reading stage or a text layer
        (two clips would claim one per-record role), the title/subtitle/footer
        (drawn as one window over the whole project), a linked clip, or a cut
        outside the clip - and says which with the same code
        :meth:`split_check` reports, so a greyed-out button and its message cannot
        disagree.  The editor keeps no copy of that rule: it asks A.
        """
        at_ticks = int(at_ticks)
        command_type = getattr(command_module, SPLIT_COMMAND, None)
        if command_type is None:
            notice = notices.split_unsupported_notice(self.project, clip_id, at_ticks)
            self.last_notices = (notice,)
            return EditOutcome(changed=False, notices=(notice,))
        check = self.split_check(clip_id)
        if not check.ok:
            notice = notices.split_refused_notice(self.project, clip_id, at_ticks, check)
            self.last_notices = (notice,)
            return EditOutcome(changed=False, notices=(notice,))
        try:
            command = command_type(clip_id=clip_id, at_ticks=at_ticks,
                                   new_clip_id=new_clip_id)
        except TypeError:                           # pragma: no cover - older signature
            command = command_type(clip_id, at_ticks)
        return self.apply(command)

    def split_check(self, clip_id):
        """Whether this clip may be split, in A's own words (for the button state)."""
        clip = self.project.clip(clip_id)
        checker = getattr(command_module, 'can_split', None)
        if clip is None or checker is None:
            return None
        return checker(clip)

    def set_style(self, role, **fields):
        """Change one role's style (size, colour, position); fonts are not reachable.

        A's ``SetStyle`` merges into the role's current override, so changing the
        size never resets a colour the member already chose, and ``font`` /
        ``font_name`` are refused: a face still comes from the resolution chain.
        """
        command_type = getattr(command_module, 'SetStyle', None)
        if command_type is None:
            return EditOutcome(changed=False)
        return self.apply(command_type(role=role, fields=dict(fields)))

    def clear_style(self, role):
        """Drop one role's overrides; it goes back to following the defaults."""
        command_type = getattr(command_module, 'ClearStyle', None)
        if command_type is None:
            return EditOutcome(changed=False)
        return self.apply(command_type(role=role))

    def styles(self):
        """The whole style table this revision renders with (defaults + overrides).

        Merged through A's ``merged_styles`` rather than handed the bare overrides:
        ``LayoutSurface`` has its own neutral fallback, and feeding it only the
        overrides silently resets every other role to that fallback.
        """
        from word_video.application.styles import merged_styles
        plan = self.plan()
        return merged_styles(plan.style_table() if plan is not None else None)

    # -- history, mode, persistence --------------------------------------
    def undo(self):
        if not self.can_undo:
            return EditOutcome(changed=False)
        self.session = self.session.undo()
        self._invalidate()
        self.set_selection(self.selection)
        self.last_notices = ()
        return EditOutcome(changed=True)

    def redo(self):
        if not self.can_redo:
            return EditOutcome(changed=False)
        self.session = self.session.redo()
        self._invalidate()
        self.set_selection(self.selection)
        self.last_notices = ()
        return EditOutcome(changed=True)

    def set_mode(self, mode):
        """Switch template/advanced; A's session passes the project through."""
        if mode not in MODES:
            raise ValueError('unknown mode %r' % (mode,))
        self.session = self.session.with_mode(mode)
        return self.session.mode

    def save(self, path=None):
        """Write the current revision atomically and remember what was written."""
        target = path or self.project_path
        if not target:
            raise ValueError('no project path to save to')
        written = self.session.save(target)
        self.project_path = str(target)
        self.mark_saved()
        return written

    def reload(self):
        """Throw away unsaved edits and read the file again (same path)."""
        if not self.project_path:
            return False
        reopened = Session.open(self.project_path, mode=self.mode)
        self.session = reopened
        self.selection = ()
        self.last_notices = ()
        self._invalidate()
        self.mark_saved()
        return True

    @property
    def mode_template(self):
        return MODE_TEMPLATE

    @property
    def mode_advanced(self):
        return MODE_ADVANCED

    # -- time helpers for the UI -----------------------------------------
    def seconds(self, ticks):
        return int(ticks) / float(TICKS_PER_SECOND)

    def ticks_from_seconds(self, seconds, *, snap=True):
        ticks = half_up(rational(seconds) * TICKS_PER_SECOND)
        frame = self.project.frame_ticks
        return snap_to_grid(ticks, frame) if snap and frame else ticks
