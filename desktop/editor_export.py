"""Export one editable revision into the three deliverables.

The three products are B's (``word_video.exporters.export_run``): MP4, five SRT
tracks and the editable 剪映 draft, all off one solved plan.  This module adds
exactly the three things a member's click needs around it and nothing about the
delivery itself:

* **the current revision is saved first.**  ``export_run`` reads ``project.json``,
  so exporting is the act of writing the revision - the alternative (exporting an
  older file than the one on screen) is how a member ends up delivering a lesson
  they already fixed;
* **a delivery is refused while it is known to be undeliverable.**  Missing media,
  a missing font and the teaching rules are checked *before* a render is started,
  so a refusal costs a second and comes back with a button rather than after two
  minutes of encoding;
* **every failure becomes a notice.**  A projection refusal, a missing catalogue
  entry, a renderer error and a validator failure all arrive as the same shape the
  error panel already renders, with the word and the role named where the problem
  is about one.

The render itself is unchanged and unpacked: nothing here re-encodes, re-times or
re-lays anything.
"""
from dataclasses import dataclass, field
from pathlib import Path
import time

from word_video.domain.errors import ProjectError
from word_video.exporters.render import export_run

from . import editor_notices as notices

__all__ = ['ExportOutcome', 'export_folder', 'blocking_notices']


@dataclass
class ExportOutcome:
    """What came of one export attempt: the products, or why there are none."""

    ok: bool
    run_dir: str = ''
    video: str = ''
    srts: tuple = ()
    draft: str = ''
    timeline: str = ''
    report: dict = field(default_factory=dict)
    notices: tuple = ()
    seconds: float = 0.0
    saved_revision: int = 0

    def to_dict(self):
        return {'ok': self.ok, 'run_dir': self.run_dir, 'video': self.video,
                'srts': list(self.srts), 'draft': self.draft, 'timeline': self.timeline,
                'report': dict(self.report),
                'notices': [notice.to_dict() for notice in self.notices],
                'seconds': round(self.seconds, 3),
                'saved_revision': self.saved_revision}


def blocking_notices(folder, project=None):
    """Everything that would stop the delivery, folder checks first.

    Recomputed from the **folder** rather than read off the editor's cached
    solution, because this runs inside the export worker: a worker that read the
    UI's ``EditorState`` would race a member who keeps editing while the render
    runs, and the answer it needs - "does this document solve, and is its teaching
    delivery unambiguous" - is a pure function of the document and its media.

    Blocking is not a veto the member cannot lift: each notice carries the action
    that fixes it, and the export can be tried again immediately afterwards.
    """
    from word_video.domain.compile import cue_plan, render_plan
    from word_video.domain.errors import ProjectError

    project = project if project is not None else folder.project
    media, intro = folder.media_table(), folder.intro_measurement()
    found = list(folder.diagnostics(project))
    plan = None
    try:
        plan = render_plan(project, media, intro)
    except ProjectError as error:
        found.append(notices.notice_from_error(project, error, plan=None))
    try:
        cue_plan(project, media, intro)
    except ProjectError as error:
        found.append(notices.notice_from_error(project, error, plan=plan))
    return tuple(notice for notice in found if notice.severity == notices.SEVERITY_BLOCK)


def free_run_name(output, revision):
    """``rev<N>``, then ``rev<N>-2``… because a run folder is never merged with."""
    output = Path(output)
    name = 'rev%d' % int(revision)
    candidate, index = name, 1
    while (output / candidate).is_dir() and any((output / candidate).iterdir()):
        index += 1
        candidate = '%s-%d' % (name, index)
    return candidate


def export_folder(folder, state, *, output=None, run_name='', render=True, draft=True,
                  progress=None, save=True):
    """Save the revision, refuse what cannot be delivered, then export it.

    ``state`` is the editor's :class:`~desktop.editor_model.EditorState`; it is the
    only thing that knows whether the document on screen differs from the file, and
    saving is the one thing this function does *to* it.  Everything after the save
    reads the folder, so the render can run in a worker while the member keeps
    editing - the file that was saved is the revision being delivered.
    """
    started = time.monotonic()
    project = state.project
    blockers = blocking_notices(folder, project)
    if blockers:
        return ExportOutcome(ok=False, notices=blockers,
                             seconds=time.monotonic() - started,
                             saved_revision=state.revision)
    if save:
        folder.save_project(project)
        state.mark_saved()
    # Everything below describes the revision that was *written*, not whatever the
    # member has moved on to: the plan is solved from the saved document, and the
    # revision reported is that document's, so a member who keeps editing during a
    # render cannot make the result claim to be their newer edit.
    delivered_revision = project.revision
    from word_video.domain.compile import render_plan
    try:
        plan = render_plan(project, folder.media_table(), folder.intro_measurement())
    except ProjectError:
        plan = None
    output = Path(output or (folder.path / 'out'))
    run_name = run_name or free_run_name(output, delivered_revision)
    delivery = folder.delivery
    try:
        result = export_run(folder.path, output=output, run_name=run_name,
                            background=delivery.background or '',
                            intro_video=delivery.intro_video or '',
                            intro_audio=delivery.intro_audio or '',
                            video_codec=delivery.video_codec or 'h264',
                            render=render, draft=draft, progress=progress)
    except ProjectError as error:
        return ExportOutcome(ok=False, notices=(notices.notice_from_error(
            project, error, plan=plan),),
            seconds=time.monotonic() - started, saved_revision=delivered_revision)
    except Exception as error:                      # noqa: BLE001 - reported, not raised
        import traceback
        notice = notices.Notice(
            code=type(error).__name__, message='导出失败：%s' % error,
            severity=notices.SEVERITY_BLOCK,
            hint='先按提示修好输入，再点一次导出；详细堆栈在下方详情里',
            actions=(notices.Action(notices.ACTION_RECHECK, '重新检查'),
                     notices.Action(notices.ACTION_OPEN_DELIVERY, '打开交付设置')),
            detail={'traceback': traceback.format_exc()[-4000:]})
        return ExportOutcome(ok=False, notices=(notice,),
                             seconds=time.monotonic() - started,
                             saved_revision=delivered_revision)
    return ExportOutcome(ok=True, run_dir=str(result.run_dir), video=str(result.video),
                         srts=tuple(str(item) for item in result.srts),
                         draft=str(result.draft), timeline=str(result.timeline),
                         report=dict(result.report),
                         notices=tuple(notices.notice_from_conflict(project, conflict, plan)
                                       for conflict in (plan.conflicts if plan else ())),
                         seconds=time.monotonic() - started,
                         saved_revision=delivered_revision)
