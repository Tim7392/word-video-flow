"""Turn a refusal or a pre-flight problem into something a member can click.

An editor that says "TeachingOverlapError: w3.female and w3.chinese overlap by
48000 ticks" has told the member nothing they can act on, and one that says
"导出失败" has told them less.  Every message here therefore carries three
things: the **word and the role** it is about, what to do next in plain words,
and one or more :class:`Action` objects the UI turns into buttons.

Two rules make this honest rather than decorative:

* **localisation is recomputed from the document, never parsed out of a message.**
  A's own module docstring says no error message is a control channel, and it is
  right: ``MissingRoleError`` mentions the role only in its prose, and
  ``TeachingOverlapError`` names only one of the two clips in ``path``.  So the
  offending roles/clips are re-derived by looking at the project (and, for a time
  overlap, by asking the same public pair-finder the solver used) instead of by
  matching text.  A message that changes wording cannot silently break the repair
  buttons;
* **an action is a request, not an edit.**  This module never touches the project;
  it names what the UI could do (select this clip, unbind it, choose a file, undo).
  The UI performs it through A's commands like any other edit.

The codes are the delivery's own: ``word_video.domain.errors.ProjectError.code``
for refusals, the plan's ``Conflict.code`` for what a plan accepted anyway, and a
small ``EDITOR_*`` set for conditions that are about this folder rather than about
the document (a missing file, an unmeasured asset, an exporter that cannot read a
layer yet).
"""
from dataclasses import dataclass, field

__all__ = ['Action', 'Notice', 'SEVERITY_BLOCK', 'SEVERITY_WARN', 'SEVERITY_INFO',
           'ACTION_KINDS', 'label_for_clip', 'role_label', 'locate',
           'notice_from_error', 'notice_from_conflict', 'notice_from_result',
           'notice_from_asset_conflict', 'missing_asset_notice', 'unmeasured_asset_notice',
           'missing_voice_notice', 'missing_font_notice', 'intro_layer_notice',
           'split_unsupported_notice', 'stale_document_notice', 'missing_roles',
           'duplicate_roles', 'overlapping_pairs',
           'EDITOR_MISSING_FILE', 'EDITOR_MISSING_VOICE', 'EDITOR_MISSING_FONT',
           'EDITOR_SPLIT_UNSUPPORTED', 'EDITOR_STALE_DOCUMENT']

#: The delivery cannot be produced until this is dealt with.
SEVERITY_BLOCK = 'block'
#: The delivery can be produced, but the plan reports something the member should
#: decide about (a truncated reading, two readings sounding at once).
SEVERITY_WARN = 'warn'
#: Worth knowing; nothing is wrong.
SEVERITY_INFO = 'info'

# -- what the UI knows how to do -----------------------------------------
ACTION_SELECT_CLIP = 'select_clip'
ACTION_SELECT_RECORD = 'select_record'
ACTION_UNDO = 'undo'
ACTION_UNBIND = 'unbind'
ACTION_TRIM_TO_SOURCE = 'trim_to_source'
ACTION_RELOAD = 'reload'
ACTION_CHOOSE_ASSET = 'choose_asset'
ACTION_CHOOSE_VOICE = 'choose_voice'
ACTION_SHOW_FONT_FOLDER = 'show_font_folder'
ACTION_OPEN_DELIVERY = 'open_delivery'
ACTION_RECHECK = 'recheck'
ACTION_DO_SPLIT = 'do_split'
ACTION_TRIM_TO_TICKS = 'trim_to_ticks'
ACTION_OPEN_FOLDER = 'open_folder'

ACTION_KINDS = (ACTION_SELECT_CLIP, ACTION_SELECT_RECORD, ACTION_UNDO, ACTION_UNBIND,
                ACTION_TRIM_TO_SOURCE, ACTION_RELOAD, ACTION_CHOOSE_ASSET,
                ACTION_CHOOSE_VOICE, ACTION_SHOW_FONT_FOLDER, ACTION_OPEN_DELIVERY,
                ACTION_RECHECK, ACTION_DO_SPLIT, ACTION_TRIM_TO_TICKS,
                ACTION_OPEN_FOLDER)

# -- conditions that are about the project folder or this build ----------
#: A delivery *setting* names a file that is not there (background, intro file).
#: An asset reference uses the storage layer's own codes instead
#: (``ASSET_FILE_MISSING`` / ``MISSING_ASSET`` / ``UNKNOWN_DURATION``).
EDITOR_MISSING_FILE = 'EDITOR_MISSING_FILE'
EDITOR_MISSING_VOICE = 'EDITOR_MISSING_VOICE'
EDITOR_MISSING_FONT = 'EDITOR_MISSING_FONT'
EDITOR_SPLIT_UNSUPPORTED = 'EDITOR_SPLIT_UNSUPPORTED'
EDITOR_STALE_DOCUMENT = 'EDITOR_STALE_DOCUMENT'

#: Role names as the member sees them in the app, not as the document spells them.
ROLE_LABELS = {
    'female': '女声英文', 'male': '男声英文', 'chinese': '中文释义朗读',
    'english': '英文字幕', 'phonetic': '音标', 'meaning': '中文释义字幕',
    'title': '标题', 'subtitle': '批次副标题', 'footer': '页脚',
    'background': '背景', 'intro': '片头',
}


def role_label(role):
    return ROLE_LABELS.get(role, role or '未命名角色')


@dataclass(frozen=True)
class Action:
    """One thing the UI can do about a notice; ``kind`` is the handler to call."""

    kind: str
    label: str
    clip_id: str = ''
    record_id: str = ''
    asset_id: str = ''
    ticks: int = 0
    path: str = ''

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError('unknown action kind %r' % (self.kind,))

    def to_dict(self):
        return {'kind': self.kind, 'label': self.label, 'clip_id': self.clip_id,
                'record_id': self.record_id, 'asset_id': self.asset_id,
                'ticks': self.ticks, 'path': self.path}


@dataclass(frozen=True)
class Notice:
    """One actionable message: what happened, where, and what to do about it."""

    code: str
    message: str
    severity: str = SEVERITY_BLOCK
    object_path: str = ''
    hint: str = ''
    clip_id: str = ''
    record_id: str = ''
    word: str = ''
    role: str = ''
    actions: tuple = ()
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'actions', tuple(self.actions))

    @property
    def location(self):
        """``词 demonstrate（w151）的 女声英文`` — empty when the notice is global."""
        if self.clip_id and self.word:
            return '词 %s（%s）的 %s' % (self.word, self.record_id or self.clip_id,
                                        role_label(self.role))
        if self.record_id and self.word:
            return '词 %s（%s）' % (self.word, self.record_id)
        if self.clip_id:
            return '片段 %s（%s）' % (self.clip_id, role_label(self.role))
        return ''

    def headline(self):
        location = self.location
        return '%s：%s' % (location, self.message) if location else self.message

    def to_dict(self):
        return {'code': self.code, 'severity': self.severity, 'message': self.message,
                'hint': self.hint, 'object_path': self.object_path,
                'location': self.location, 'clip_id': self.clip_id,
                'record_id': self.record_id, 'word': self.word, 'role': self.role,
                'actions': [action.to_dict() for action in self.actions],
                'detail': dict(self.detail)}


def label_for_clip(project, clip_id):
    """``(record_id, word, role)`` of a clip, from the document."""
    clip = project.clip(clip_id) if project is not None else None
    if clip is None:
        return '', '', ''
    record = project.record(clip.record_id) if clip.record_id else None
    return (clip.record_id, record.word if record else '', clip.role)


def locate(project, object_path):
    """``(clip_id, record_id, word, role)`` for ``clip:x`` / ``record:x`` / ``project``.

    A record path has no role and a clip path carries both, which is exactly the
    information an error's ``object_path`` already holds - so this reads the
    document to fill in the word, and never the error's prose.
    """
    if not object_path or not isinstance(object_path, str):
        return '', '', '', ''
    kind, _, name = object_path.partition(':')
    if kind == 'clip' and name:
        record_id, word, role = label_for_clip(project, name)
        return name, record_id, word, role
    if kind == 'record' and name:
        record = project.record(name) if project is not None else None
        return '', name, record.word if record else '', ''
    return '', '', '', ''


def _base(project, code, message, *, severity, object_path='', hint='', actions=(),
          detail=None, clip_id='', record_id='', word='', role=''):
    found_clip, found_record, found_word, found_role = locate(project, object_path)
    return Notice(code=code, message=message, severity=severity,
                  object_path=object_path, hint=hint,
                  clip_id=clip_id or found_clip, record_id=record_id or found_record,
                  word=word or found_word, role=role or found_role,
                  actions=actions, detail=detail or {})


def _select(clip_id, label=None, project=None):
    _, _, word, role = locate(project, 'clip:%s' % clip_id)
    name = label or ('%s 的%s' % (word, role_label(role)) if word else clip_id)
    return Action(ACTION_SELECT_CLIP, '定位到%s' % name, clip_id=clip_id)


def missing_roles(project, record_id):
    """Roles this record lacks, read from the document - not from a message."""
    from word_video.domain.model import DISPLAY_LAYERS, TEACHING_STAGES
    have = {clip.role for clip in project.clips_of(record_id)}
    return tuple(role for role in TEACHING_STAGES + DISPLAY_LAYERS if role not in have)


def duplicate_roles(project, record_id):
    """``{role: (clip_id, clip_id)}`` for roles this record claims twice."""
    seen, duplicates = {}, {}
    for clip in project.clips_of(record_id):
        if clip.role in seen:
            duplicates.setdefault(clip.role, (seen[clip.role], clip.id))
        else:
            seen[clip.role] = clip.id
    return duplicates


def overlapping_pairs(plan, clip_id=''):
    """The overlapping pairs the solver itself would see, from public helpers.

    Recomputing beats parsing: ``TeachingOverlapError`` names one clip in ``path``
    and the other only inside its message, and a message is not a control channel.
    """
    from word_video.domain.compile import sounding_items, speech_overlaps
    pairs = speech_overlaps(sounding_items(plan.video, plan.audio))
    if clip_id:
        return tuple((first, second) for first, second in pairs
                     if clip_id in (first.clip_id, second.clip_id))
    return pairs


def notice_from_error(project, error, *, plan=None, extra_actions=()):
    """A :class:`Notice` for a refused operation, with repairs where they exist.

    ``plan`` is the last plan that could be solved (``None`` when the project does
    not solve at all); it is only used to name the other clip of an overlap pair.
    """
    code = getattr(error, 'code', 'PROJECT_ERROR')
    path = getattr(error, 'path', '') or ''
    message = getattr(error, 'message', None) or str(error)
    hint = getattr(error, 'hint', '') or ''
    clip_id, record_id, word, role = locate(project, path)
    actions = list(extra_actions)
    detail = {}

    if code == 'TEACHING_OVERLAP' and clip_id:
        pairs = overlapping_pairs(plan, clip_id) if plan is not None else ()
        pair = pairs[0] if pairs else None
        if pair is not None:
            other = pair[1] if pair[0].clip_id == clip_id else pair[0]
            other_word = ''
            record = project.record(other.record_id) if other.record_id else None
            other_word = record.word if record else ''
            detail = {'other_clip_id': other.clip_id, 'other_path': 'clip:%s' % other.clip_id,
                      'overlap_ticks': min(pair[0].end_ticks, pair[1].end_ticks)
                      - max(pair[0].start_ticks, pair[1].start_ticks)}
            actions = [_select(clip_id, project=project),
                       Action(ACTION_SELECT_CLIP, '定位到另一段（%s 的%s）'
                              % (other_word or other.record_id, role_label(other.role)),
                              clip_id=other.clip_id)]
            hint = hint or '教学交付要求同一时刻只有一段朗读；把其中一段移开或改短'
    elif code == 'TEACHING_ORDER' and record_id:
        missing = missing_roles(project, record_id)
        detail = {'missing_roles': list(missing)}
        from word_video.domain.model import TEACHING_STAGES
        for stage in TEACHING_STAGES:
            clip = next((item for item in project.clips_of(record_id)
                         if item.role == stage), None)
            if clip is not None:
                actions.append(_select(clip.id, project=project))
        hint = hint or '顺序固定为 女声英文 → 男声英文 → 中文释义朗读'
    elif code == 'MISSING_ROLE' and record_id:
        missing = missing_roles(project, record_id)
        detail = {'missing_roles': list(missing)}
        if missing:
            # A's own message names the record and the role in its own words; the
            # member needs them in the app's words, and all of them at once.
            message = '缺少%s' % '、'.join(role_label(item) for item in missing)
        hint = hint or '用同一个词表重新导入即可补齐缺失的角色'
        actions.append(Action(ACTION_OPEN_DELIVERY, '打开交付设置'))
    elif code == 'ROLE_AMBIGUOUS' and record_id:
        duplicates = duplicate_roles(project, record_id)
        detail = {'duplicate_roles': {key: list(value) for key, value in duplicates.items()}}
        for role_name, ids in duplicates.items():
            for clip in ids:
                actions.append(_select(clip, project=project))
        hint = hint or '每个角色只能有一个片段；删掉多余的那个（或等 SplitClip 只用于图层）'
    elif code == 'CLIP_BOUND' and clip_id:
        actions.insert(0, Action(ACTION_UNBIND, '先解绑，再修剪', clip_id=clip_id,
                                 record_id=record_id))
        hint = hint or '它现在跟随另一个片段的端点，没有自己的绝对位置'
    elif code == 'STALE_REVISION':
        actions.insert(0, Action(ACTION_RELOAD, '重新读取工程'))
        hint = hint or '工程在别处被改过；重新读取后再改，避免覆盖别人的修改'
    elif code in ('SOURCE_RANGE', 'INVALID_TIME') and clip_id:
        actions.insert(0, _select(clip_id, project=project))
    elif code in ('UNKNOWN_DURATION', 'SCHEMA', 'DANGLING_REF', 'DUPLICATE_ID', 'CYCLE'):
        if clip_id:
            actions.insert(0, _select(clip_id, project=project))
    if clip_id:
        detail.setdefault('clip_id', clip_id)
    return _base(project, code, message, severity=SEVERITY_BLOCK, object_path=path,
                 hint=hint, actions=tuple(actions), detail=detail)


def notice_from_conflict(project, conflict, plan=None):
    """A warned-about thing the plan could still express (overlap, truncation)."""
    code = getattr(conflict, 'code', 'CONFLICT')
    path = getattr(conflict, 'path', '') or ''
    other_path = getattr(conflict, 'other_path', '') or ''
    clip_id = path.partition(':')[2]
    actions = []
    detail = {}
    if clip_id:
        actions.append(_select(clip_id, project=project))
    if other_path:
        other = other_path.partition(':')[2]
        _, _, other_word, other_role = locate(project, other_path)
        detail['other_clip_id'] = other
        actions.append(Action(ACTION_SELECT_CLIP, '定位到另一段（%s 的%s）'
                              % (other_word or other, role_label(other_role)),
                              clip_id=other))
    if code == 'SOURCE_TRUNCATED' and plan is not None:
        item = plan.item(clip_id) if clip_id else None
        if item is not None and item.source is not None:
            detail = {'source_duration_ticks': item.source.duration_ticks,
                      'target_duration_ticks': item.duration_ticks}
            actions.insert(0, Action(ACTION_TRIM_TO_SOURCE,
                                     '把目标时长恢复到语音长度（%.2fs）'
                                     % (item.source.duration_ticks / 720000.0),
                                     clip_id=clip_id,
                                     ticks=item.source.duration_ticks))
    return _base(project, code, getattr(conflict, 'message', str(conflict)),
                 severity=SEVERITY_WARN, object_path=path,
                 hint=getattr(conflict, 'hint', '') or '',
                 actions=tuple(actions), detail=detail)


def notice_from_result(project, result):
    """The notes a command returned - above all, who else is about to move.

    A single-clip move must never drag an unselected clip *silently*, and the
    command already reports the followers it knows about; this is where that
    becomes a message the member reads before it happens.
    """
    notes = tuple(getattr(result, 'notes', ()) or ())
    if not notes:
        return ()
    moved = tuple(getattr(result, 'moved', ()) or ())
    notices = []
    for note in notes:
        notices.append(Notice(code='COMMAND_NOTE', message=note, severity=SEVERITY_INFO,
                              clip_id=moved[0] if len(moved) == 1 else '',
                              record_id=label_for_clip(project, moved[0])[0] if len(moved) == 1 else '',
                              word=label_for_clip(project, moved[0])[1] if len(moved) == 1 else '',
                              role=label_for_clip(project, moved[0])[2] if len(moved) == 1 else '',
                              detail={'moved': list(moved)}))
    return tuple(notices)


def missing_asset_notice(project, clip_id, asset_id, *, path='', reason='', code=''):
    """A clip's media is not in the registry, or the file it names is gone.

    The code is the storage layer's (``ASSET_FILE_MISSING`` / ``MISSING_ASSET``),
    so a reader that knows ``wv-assets@1`` recognises it here; the editor adds the
    word and the role and the click that fixes it.
    """
    code = code or ('ASSET_FILE_MISSING' if path else 'MISSING_ASSET')
    message = ('配音文件不存在：%s' % path) if path else ('工程引用的媒体 %r 没有登记，'
                                                         '也不是一个文件' % (asset_id,))
    return _base(project, code, message, severity=SEVERITY_BLOCK,
                 object_path='clip:%s' % clip_id if clip_id else 'asset:%s' % asset_id,
                 hint=reason or '选一个新文件，或重新导入该词',
                 actions=(Action(ACTION_CHOOSE_ASSET, '选择配音文件…', clip_id=clip_id,
                                 asset_id=asset_id),
                          Action(ACTION_RECHECK, '重新检查')))


def unmeasured_asset_notice(project, clip_id, asset_id, error=''):
    return _base(project, 'UNKNOWN_DURATION',
                 '媒体 %r 还没测量过，时长无法求解' % (asset_id,), severity=SEVERITY_BLOCK,
                 object_path='clip:%s' % clip_id if clip_id else 'asset:%s' % asset_id,
                 hint=error or '重新导入或点"重新检查"即可重新测量',
                 detail={'probe_error': error} if error else {},
                 actions=(Action(ACTION_CHOOSE_ASSET, '选择配音文件…', clip_id=clip_id,
                                 asset_id=asset_id),
                          Action(ACTION_RECHECK, '重新检查')))


def notice_from_asset_conflict(project, conflict, clip_id='', path='', reason=''):
    """A broken asset reference, in A's own code (``wv-assets@1``).

    The path is passed in when the caller knows it, rather than parsed back out of
    the conflict's message: the registry reports *what* is missing, and the folder
    is the layer that knows where the file was supposed to be.  ``reason`` is the
    probe's own refusal, which is what makes an unmeasurable file diagnosable.
    """
    code = getattr(conflict, 'code', 'ASSET')
    object_path = getattr(conflict, 'path', '') or ''
    asset_id = object_path.partition(':')[2]
    if code in ('ASSET_FILE_MISSING', 'MISSING_ASSET'):
        return missing_asset_notice(project, clip_id, asset_id, path=path,
                                    reason=reason or getattr(conflict, 'hint', ''),
                                    code=code)
    if code == 'UNKNOWN_DURATION':
        return unmeasured_asset_notice(project, clip_id, asset_id, reason)
    return _base(project, code, getattr(conflict, 'message', str(conflict)),
                 severity=SEVERITY_BLOCK, object_path=object_path, clip_id=clip_id,
                 hint=getattr(conflict, 'hint', ''),
                 actions=(Action(ACTION_CHOOSE_ASSET, '选择配音文件…', clip_id=clip_id,
                                 asset_id=asset_id),
                          Action(ACTION_RECHECK, '重新检查')))


def missing_voice_notice(project, clip_id, asset_id, role=''):
    return _base(project, EDITOR_MISSING_VOICE,
                 '媒体 %r 没有记录音色' % (asset_id,), severity=SEVERITY_WARN,
                 object_path='clip:%s' % clip_id,
                 hint='音色只影响交付说明与草稿标注，不会替换声音',
                 actions=(Action(ACTION_CHOOSE_VOICE, '选择音色…', clip_id=clip_id,
                                 asset_id=asset_id),
                          Action(ACTION_RECHECK, '重新检查')))


def missing_font_notice(style, path):
    return Notice(code=EDITOR_MISSING_FONT,
                  message='样式 %s 的字体文件不存在：%s' % (style, path),
                  severity=SEVERITY_BLOCK, hint='字体不会被静默替换；补回该文件或改样式表',
                  actions=(Action(ACTION_SHOW_FONT_FOLDER, '打开字体所在目录',
                                  path=str(path)),))


def intro_layer_notice(project, error=''):
    """The intro layer's own media cannot be measured (missing, shorter than its sound)."""
    clip = project.intro_clip()
    return _base(project, 'INTRO_MEDIA',
                 '片头图层的素材无法测量%s' % (('：%s' % error) if error else ''),
                 severity=SEVERITY_BLOCK,
                 object_path='clip:%s' % (clip.id if clip else 'layer.intro'),
                 hint='检查片头文件的画面与音轨；片头时长只能来自媒体',
                 actions=(Action(ACTION_OPEN_DELIVERY, '打开交付设置'),
                          Action(ACTION_RECHECK, '重新检查')))


def split_refused_notice(project, clip_id, at_ticks, check):
    """A refused split, in the code A's ``can_split`` would have reported.

    The editor keeps no copy of the rule about *what may be split*: it asks the
    application layer, and reports its answer with the clip's word and role
    attached - so a greyed-out button and this message always agree.
    """
    _, _, word, role = locate(project, 'clip:%s' % clip_id)
    reason = getattr(check, 'reason', '') or '该片段不能拆分'
    return _base(project, getattr(check, 'code', 'SPLIT_NOT_APPLICABLE'),
                 '不能拆分：%s' % reason, severity=SEVERITY_BLOCK,
                 object_path='clip:%s' % clip_id, word=word, role=role,
                 hint='可拆分的角色：背景、片头；每个词的角色只允许一个',
                 actions=(_select(clip_id, project=project),
                          Action(ACTION_UNDO, '撤销')),
                 detail={'at_ticks': int(at_ticks)})


def split_intro_notice(project, clip_ids):
    """The countdown is cut in two; this build delivers one intro.

    Reported at the *delivery*, not at the edit: a member may split the intro and
    keep working and saving, but B's projection refuses two intros by naming the
    second item rather than half-drawing the countdown.  Saying so here - with the
    clip named and an undo offered - is cheaper than surfacing that refusal after a
    render, and it cannot be mistaken for a silent success.
    """
    clip_id = clip_ids[1] if len(clip_ids) > 1 else (clip_ids[0] if clip_ids else '')
    return _base(project, 'SPLIT_INTRO_NOT_EXPORTABLE',
                 '片头不支持拆分：现在有 %d 段片头，这一版只能出一段' % len(clip_ids),
                 severity=SEVERITY_BLOCK, object_path='clip:%s' % clip_id,
                 hint='撤销这次拆分后再导出；工程本身可以保留拆分（背景拆分是可以出片的）',
                 actions=(Action(ACTION_UNDO, '撤销最近一次编辑'),
                          _select(clip_id, project=project)),
                 detail={'role': 'intro', 'clip_ids': list(clip_ids)})


def split_unsupported_notice(project, clip_id, at_ticks):
    """Split needs a command this checkout does not have; say so and offer the rest.

    Kept for a tree whose application layer predates ``SplitClip`` (an older
    engine, or a package built from one): the editor would rather explain what it
    cannot do than build the second clip itself and own a slice of the domain.
    """
    _, _, word, role = locate(project, 'clip:%s' % clip_id)
    return _base(project, EDITOR_SPLIT_UNSUPPORTED,
                 '拆分需要 A 的应用命令 SplitClip，这个检出里没有它',
                 severity=SEVERITY_BLOCK, object_path='clip:%s' % clip_id,
                 word=word, role=role,
                 hint='这一轮可以先把右边界修剪到切口（后续片段要一起移就多选后再拖）',
                 actions=(_select(clip_id, project=project),
                          Action(ACTION_TRIM_TO_TICKS, '改为：把右边界修剪到切口',
                                 clip_id=clip_id, ticks=int(at_ticks)),
                          Action(ACTION_UNDO, '撤销')),
                 detail={'at_ticks': int(at_ticks)})


def stale_document_notice(message, path=''):
    return Notice(code=EDITOR_STALE_DOCUMENT, message=message, severity=SEVERITY_BLOCK,
                  object_path=path, hint='换一个工程目录，或用兼容版本打开',
                  actions=(Action(ACTION_OPEN_DELIVERY, '打开交付设置'),))
