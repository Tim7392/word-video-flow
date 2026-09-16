"""Edit commands: pure ``(project, command) -> result + new state``.

The same commands serve the desktop and the CLI (architecture §12), so they carry
no Qt, no media probing and no file IO: a command edits the document and nothing
else.  A single-clip move changes that one clip; nothing else follows implicitly.
Following is either an explicit group (:class:`MoveClips`) or a declared link
(:class:`BindStart`), and a command that did move followers says so in
``CommandResult.notes`` instead of moving them silently.

Every command checks ``expected_revision`` first when the caller passes one, so
two clients editing one project cannot silently overwrite each other.
"""
from dataclasses import dataclass, replace

from ..domain.errors import (ClipBoundError, CycleError, DanglingRefError,
                             InvalidTimeError, SchemaError, StaleRevisionError,
                             UnknownCommandError)
from ..domain.model import MediaSlice, Project
from ..domain.timebase import TimeExpr


@dataclass(frozen=True)
class MoveClip:
    """Move one clip by ``delta_ticks``; a linked clip moves its own offset."""

    clip_id: str
    delta_ticks: int


@dataclass(frozen=True)
class MoveClips:
    """Move exactly the listed clips together (explicit group / linkage)."""

    clip_ids: tuple
    delta_ticks: int

    def __post_init__(self):
        object.__setattr__(self, 'clip_ids', tuple(self.clip_ids))


@dataclass(frozen=True)
class TrimClip:
    """Set a clip's target range; the source window is untouched.

    Cutting inside the media is a separate, explicit decision
    (:class:`SetMediaSlice`), so trimming never silently shortens someone's
    recorded voice.
    """

    clip_id: str
    start_ticks: int | None = None
    end_ticks: int | None = None


@dataclass(frozen=True)
class SetMediaSlice:
    """Edit the source window or the processing of one media clip."""

    clip_id: str
    source_start: int | None = None
    source_end: int | None = None
    speed: float | None = None
    gain_db: float | None = None


@dataclass(frozen=True)
class BindStart:
    """Make one clip start where another clip's edge is (explicit linkage)."""

    clip_id: str
    ref_id: str
    edge: str = 'end'
    offset_ticks: int = 0


@dataclass(frozen=True)
class UnbindStart:
    """Replace a link with an absolute position."""

    clip_id: str
    ticks: int | None = None


COMMANDS = (MoveClip, MoveClips, TrimClip, SetMediaSlice, BindStart, UnbindStart)


@dataclass(frozen=True)
class CommandResult:
    """What one command did: the new revision, and what else it had to touch."""

    project: Project
    command: object
    changed: bool
    moved: tuple = ()
    notes: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'moved', tuple(self.moved))
        object.__setattr__(self, 'notes', tuple(self.notes))


def _clip_or_fail(project, clip_id):
    clip = project.clip(clip_id)
    if clip is None:
        raise DanglingRefError('unknown clip %r' % (clip_id,), path='clip:%s' % clip_id,
                               hint='该片段可能已被删除；重新读取工程')
    return clip


def _selected(project, clip_ids):
    ids = []
    for clip_id in clip_ids:
        _clip_or_fail(project, clip_id)
        if clip_id not in ids:
            ids.append(clip_id)
    if not ids:
        raise InvalidTimeError('no clip selected', path='command',
                               hint='显式给出要操作的片段')
    return tuple(ids)


def _follower_notes(project, moved_ids):
    """Name the clips whose declared link makes them follow, instead of hiding it."""
    notes = []
    for clip in project.clips:
        if clip.id in moved_ids or clip.start.is_absolute:
            continue
        if clip.start.ref in moved_ids:
            notes.append('%s 跟随 %s 的%s端点，会一起移动'
                         % (clip.id, clip.start.ref,
                            '结束' if clip.start.edge == 'end' else '开始'))
    return tuple(sorted(notes))


def _shift(project, clip_ids, delta_ticks):
    if isinstance(delta_ticks, bool) or not isinstance(delta_ticks, int):
        raise InvalidTimeError('delta must be an integer tick count', path='command')
    ids = _selected(project, clip_ids)
    if delta_ticks == 0:
        return project.clips, (), ()
    moved = {}
    for clip_id in ids:
        clip = _clip_or_fail(project, clip_id)
        moved[clip_id] = replace(clip, start=clip.start.shift(delta_ticks))
    clips = tuple(moved.get(clip.id, clip) for clip in project.clips)
    return clips, tuple(sorted(ids)), _follower_notes(project, set(ids))


def _trim(project, command):
    clip = _clip_or_fail(project, command.clip_id)
    if command.start_ticks is None and command.end_ticks is None:
        raise InvalidTimeError('trim needs a new start or a new end',
                               path='clip:%s' % clip.id)
    if clip.start.is_linked:
        raise ClipBoundError(
            'clip %s follows %s and has no absolute start'
            % (clip.id, clip.start.ref), path='clip:%s' % clip.id,
            hint='先用 UnbindStart 解绑，或改用 MoveClip 调整它的偏移')
    start = clip.start.ticks if command.start_ticks is None else command.start_ticks
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise InvalidTimeError('trim start must be a non-negative integer tick count',
                               path='clip:%s' % clip.id)
    if command.end_ticks is not None:
        if isinstance(command.end_ticks, bool) or not isinstance(command.end_ticks, int):
            raise InvalidTimeError('trim end must be an integer tick count',
                                   path='clip:%s' % clip.id)
        duration = command.end_ticks - start
        if duration <= 0:
            raise InvalidTimeError('trimmed clip would have no length',
                                   path='clip:%s' % clip.id,
                                   hint='结束必须先于开始之后')
    else:
        duration = clip.duration_ticks
    notes = []
    if clip.source is not None and duration is not None and duration < clip.source.duration_ticks:
        notes.append('%s 的目标时长 %d 短于语音 %d tick，导出会截断这段朗读'
                     % (clip.id, duration, clip.source.duration_ticks))
    changed = (start != clip.start.ticks or duration != clip.duration_ticks)
    new_clip = replace(clip, start=TimeExpr.at(start), duration_ticks=duration)
    clips = tuple(new_clip if item.id == clip.id else item for item in project.clips)
    return clips, changed, (clip.id,) if changed else (), tuple(notes)


def _set_media(project, command):
    clip = _clip_or_fail(project, command.clip_id)
    if clip.source is None:
        raise SchemaError('clip %s has no media source' % clip.id,
                          path='clip:%s' % clip.id)
    source = clip.source
    fields = {'source_start': source.source_start, 'source_end': source.source_end,
              'speed': source.speed, 'gain_db': source.gain_db}
    for name in fields:
        value = getattr(command, name)
        if value is not None:
            fields[name] = value
    if fields == {'source_start': source.source_start, 'source_end': source.source_end,
                  'speed': source.speed, 'gain_db': source.gain_db}:
        return project.clips, False, (), ()
    new_source = MediaSlice(asset_id=source.asset_id, unit_num=source.unit_num,
                            unit_den=source.unit_den, **fields)
    notes = []
    if new_source.speed != source.speed:
        notes.append('%s 的播放速度改为 %s；片段时长随求解重算，变速只应用一次'
                     % (clip.id, new_source.speed))
    if clip.duration_ticks is None and new_source.seconds != source.seconds:
        notes.append('%s 的源区间变了，它的默认时长会在求解时按节奏重算' % clip.id)
    new_clip = replace(clip, source=new_source)
    clips = tuple(new_clip if item.id == clip.id else item for item in project.clips)
    return clips, True, (clip.id,), tuple(notes)


def _bind(project, command):
    clip = _clip_or_fail(project, command.clip_id)
    reference = _clip_or_fail(project, command.ref_id)
    if reference.id == clip.id:
        raise CycleError('clip %s cannot follow itself' % clip.id,
                         path='clip:%s' % clip.id, hint='绑定到另一个片段')
    start = (TimeExpr.after(reference.id, command.offset_ticks)
             if command.edge == 'end'
             else TimeExpr.at_start_of(reference.id, command.offset_ticks))
    if start == clip.start:
        return project.clips, False, (), ()
    new_clip = replace(clip, start=start)
    clips = tuple(new_clip if item.id == clip.id else item for item in project.clips)
    note = ('%s 现在跟随 %s 的%s端点（偏移 %d tick）'
            % (clip.id, reference.id, '结束' if command.edge == 'end' else '开始',
               command.offset_ticks))
    return clips, True, (clip.id,), (note,)


def _unbind(project, command):
    clip = _clip_or_fail(project, command.clip_id)
    if command.ticks is None:
        if clip.start.is_absolute:
            return project.clips, False, (), ()
        raise InvalidTimeError(
            'clip %s is linked to %s; unbinding needs an absolute position'
            % (clip.id, clip.start.ref), path='clip:%s' % clip.id,
            hint='先求解取得当前位置，再把 ticks 传进来')
    new_clip = replace(clip, start=TimeExpr.at(command.ticks))
    if new_clip.start == clip.start:
        return project.clips, False, (), ()
    clips = tuple(new_clip if item.id == clip.id else item for item in project.clips)
    if clip.start.is_linked:
        note = '%s 已解绑，改为绝对位置 %d tick' % (clip.id, command.ticks)
    else:
        note = '%s 移到绝对位置 %d tick' % (clip.id, command.ticks)
    return clips, True, (clip.id,), (note,)


def apply(project, command, expected_revision=None):
    """Apply one command and return the new revision (the old one is untouched)."""
    if not isinstance(project, Project):
        raise SchemaError('apply needs a Project', path='project')
    if expected_revision is not None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise InvalidTimeError('expected_revision must be an integer',
                                   path='project')
        if expected_revision != project.revision:
            raise StaleRevisionError(
                'project %s is at revision %d, not %d'
                % (project.project_id, project.revision, expected_revision),
                path='project',
                hint='重新读取工程后再提交；过期修订不能覆盖新内容')
    if isinstance(command, (MoveClip, MoveClips)):
        ids = ((command.clip_id,) if isinstance(command, MoveClip) else command.clip_ids)
        clips, moved, notes = _shift(project, ids, command.delta_ticks)
        changed = bool(moved)
    elif isinstance(command, TrimClip):
        clips, changed, moved, notes = _trim(project, command)
    elif isinstance(command, SetMediaSlice):
        clips, changed, moved, notes = _set_media(project, command)
    elif isinstance(command, BindStart):
        clips, changed, moved, notes = _bind(project, command)
    elif isinstance(command, UnbindStart):
        clips, changed, moved, notes = _unbind(project, command)
    else:
        raise UnknownCommandError('unsupported command %r' % type(command).__name__,
                                  path='command',
                                  hint='可用命令：%s'
                                       % '、'.join(item.__name__ for item in COMMANDS))
    if not changed:
        return CommandResult(project=project, command=command, changed=False)
    return CommandResult(project=project.with_clips(clips,
                                                    revision=project.revision + 1),
                         command=command, changed=True, moved=moved, notes=notes)
