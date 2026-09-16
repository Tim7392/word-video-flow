"""What a submission freezes, and how a batch is planned before anything runs.

A submission is the smallest thing a caller can be held to: which project revision,
which records, which export profile, and the *identity* of the plan those produce.
Freezing it here — as plain data, with no IO — is what lets the coordinator promise
"the same key returns the same task" and "a changed input is a conflict" without
either side re-deriving what was meant.

Nothing in this module renders, synthesises or writes a file: planning is the
preflight an Agent runs before it spends anything, so a plan that cannot be solved
comes back as structured problems instead of half a batch.  The one thing a plan
does touch is the delivery's own inputs (the background file), because "the picture
this delivery uses" is not in the project and a run cannot invent it: see
:func:`check_delivery`.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path

from ..domain.errors import SchemaError
from ..domain.model import Project, Record
from ..domain.plan import Conflict, RenderPlan
from ..domain.compile import render_plan


@dataclass(frozen=True)
class BatchSelection:
    """Which records this submission covers, by id or by source-list range."""

    record_ids: tuple = ()
    first: int = 0
    last: int = 0

    def __post_init__(self):
        object.__setattr__(self, 'record_ids', tuple(self.record_ids))
        for name in ('first', 'last'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaError('batch %s must be a non-negative integer' % name,
                                  path='batch')
        if self.record_ids and (self.first or self.last):
            raise SchemaError('select records by id or by range, not both', path='batch')
        if self.first and self.last and self.last < self.first:
            raise SchemaError('batch range ends before it starts', path='batch')

    def describe(self):
        if self.record_ids:
            return {'record_ids': list(self.record_ids)}
        return {'first': self.first, 'last': self.last}


@dataclass(frozen=True)
class ExportProfile:
    """Delivery settings that are *not* project content (today: the background)."""

    background: str = ''
    video_codec: str = 'h264'
    slices: int | None = None

    def to_dict(self):
        return {'background': self.background, 'video_codec': self.video_codec,
                'slices': self.slices}


#: What a caller can do about a delivery with no picture.  Kept as data with the
#: check that produces it, so every refusal (plan, submit, run) says the same thing.
BACKGROUND_FIXES = (
    '--background <文件>：这次交付的画面底（导出背景是交付设置，不在工程内容里）',
    r'例如 D:\单词速记自动化_测试归档_0915\_prepared-1080p'
    r'\background-from-reference-1080p.mp4',
    '或在编辑器里给工程设置交付背景，导出时由 editor_export 传入',
)


@dataclass(frozen=True)
class DeliveryCheck:
    """Whether a delivery profile is complete enough to produce a picture.

    A background file is a *delivery* setting: ``export_run``/``build_manifest`` take
    it as a parameter and never read one out of the project, so a batch without one
    reaches the draft and render steps with an **empty** path.  ``Path('')`` is the
    current working directory, so that failure arrives as ``PermissionError: [Errno
    13]`` on a folder — an INTERNAL error about the cwd, days after the cause.  The
    check turns it into a named problem with a fix, before anything is frozen.
    """

    ready: bool = False
    background: str = ''
    problems: tuple = ()
    fixes: tuple = ()

    def to_dict(self):
        return {'ready': self.ready, 'background': self.background,
                'problems': [problem.to_dict() for problem in self.problems],
                'fixes': list(self.fixes)}


def check_delivery(profile):
    """Check the delivery inputs a run cannot invent (today: the background file)."""
    if not isinstance(profile, ExportProfile):
        raise SchemaError('a delivery check needs an ExportProfile', path='profile')
    background = str(profile.background or '').strip()
    if not background:
        return DeliveryCheck(
            ready=False, background='',
            problems=(Conflict(code='BACKGROUND_MISSING',
                               message='这次交付没有背景文件（导出背景不是工程内容）',
                               path='profile.background',
                               hint='给出一个存在的视频文件，或先在编辑器里设置交付背景'),),
            fixes=BACKGROUND_FIXES)
    if not Path(background).is_file():
        return DeliveryCheck(
            ready=False, background=background,
            problems=(Conflict(code='BACKGROUND_FILE_MISSING',
                               message='背景文件不存在：%s' % background,
                               path='profile.background',
                               hint='--background 指向存在的视频文件'),),
            fixes=('--background <存在的视频文件>（当前：%s）' % background,))
    try:
        with open(background, 'rb') as stream:
            stream.read(1)
    except OSError as error:
        return DeliveryCheck(
            ready=False, background=background,
            problems=(Conflict(code='BACKGROUND_UNREADABLE',
                               message='背景文件打不开：%s（%s）' % (background, error),
                               path='profile.background',
                               hint='换一个可读的文件，或修好它的权限'),),
            fixes=('--background <可读的视频文件>（当前打不开：%s）' % background,))
    return DeliveryCheck(ready=True, background=background)


@dataclass(frozen=True)
class BatchPlan:
    """The frozen preflight: what would run, over which revision, and its identity."""

    project_id: str
    revision: int
    selection: BatchSelection
    profile: ExportProfile
    records: tuple = ()
    plan_identity: str = ''
    total_ticks: int = 0
    assets: tuple = ()
    problems: tuple = ()
    estimated_seconds: float = 0.0
    #: The delivery inputs, checked at plan time.  ``problems`` stays about solving
    #: the lesson, so a caller can tell "this project cannot be solved" from
    #: "this delivery is missing its picture".
    delivery: DeliveryCheck = DeliveryCheck()

    @property
    def ok(self):
        return not self.problems

    @property
    def ready(self):
        """Solved *and* deliverable: what a submission needs to be accepted."""
        return self.ok and self.delivery.ready

    def to_dict(self):
        return {'project_id': self.project_id, 'revision': self.revision,
                'selection': self.selection.describe(),
                'profile': self.profile.to_dict(),
                'records': [{'id': record.id, 'index': record.index, 'word': record.word}
                            for record in self.records],
                'plan_identity': self.plan_identity, 'total_ticks': self.total_ticks,
                'assets': list(self.assets),
                'estimated_seconds': round(self.estimated_seconds, 3),
                'delivery': self.delivery.to_dict(),
                'problems': [problem.to_dict() for problem in self.problems]}


@dataclass(frozen=True)
class Submission:
    """One batch to run, plus the digests that make "same request" checkable."""

    plan: BatchPlan
    input_paths: tuple = ()
    key: str = ''

    def digest(self):
        """Content identity: the plan's identity, profile and record ids, nothing else."""
        payload = {'plan': self.plan.plan_identity, 'profile': self.plan.profile.to_dict(),
                   'selection': self.plan.selection.describe(),
                   'records': [record.id for record in self.plan.records],
                   'revision': self.plan.revision}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def to_dict(self):
        return {'plan': self.plan.to_dict(), 'input_paths': list(self.input_paths),
                'key': self.key, 'digest': self.digest()}


def select_records(project, selection):
    """The records this selection covers, in project order, or a structured refusal."""
    if not isinstance(project, Project):
        raise SchemaError('a batch needs a Project', path='project')
    if selection.record_ids:
        wanted = set(selection.record_ids)
        known = {record.id for record in project.records}
        missing = sorted(wanted - known)
        if missing:
            raise SchemaError('unknown record id(s): %s' % ', '.join(missing),
                              path='batch', hint='先读工程里的词条 id（project show）')
        return tuple(record for record in project.records if record.id in wanted)
    if selection.first or selection.last:
        first = selection.first or 1
        last = selection.last or len(project.records)
        picked = tuple(record for record in project.records
                       if first <= (record.index or 0) <= last)
        if not picked:
            raise SchemaError('no record falls in %d-%d' % (first, last), path='batch',
                              hint='范围按词表序号（record.index）；用 project show 查看')
        return picked
    return tuple(project.records)


def _estimated_seconds(plan):
    """Reading time reserved by the plan, from the solved stage lengths."""
    return plan.total_ticks / 720000.0


def plan_batch(project, media, selection=None, profile=None, intro=None):
    """Preflight one batch: solve it, collect its problems, write nothing.

    ``media`` is the resolved measurement map (``AssetIndex.media_map()``), so a
    missing measurement shows up as a problem here rather than as a failure after
    the batch was accepted.
    """
    selection = selection or BatchSelection()
    profile = profile or ExportProfile()
    records = select_records(project, selection)
    problems = []
    plan = None
    try:
        plan = render_plan(project, media, intro)
    except Exception as error:                     # noqa: BLE001 - reported, not raised
        code = getattr(error, 'code', type(error).__name__)
        problems.append(Conflict(code=code, message=str(error),
                                 path=getattr(error, 'path', 'project'),
                                 hint=getattr(error, 'hint', '')))
    assets = []
    for clip in project.clips:
        if clip.source is not None and clip.source.asset_id not in assets:
            assets.append(clip.source.asset_id)
        if clip.audio_asset and clip.audio_asset not in assets:
            assets.append(clip.audio_asset)
    total_ticks = plan.total_ticks if plan is not None else 0
    return BatchPlan(
        project_id=project.project_id, revision=project.revision,
        selection=selection, profile=profile, records=records,
        plan_identity=plan.identity() if plan is not None else '',
        total_ticks=total_ticks, assets=tuple(assets), problems=tuple(problems),
        estimated_seconds=_estimated_seconds(plan) if plan is not None else 0.0,
        delivery=check_delivery(profile))


def submission_for(plan, input_paths=(), key=''):
    """Wrap a plan into a submission; the coordinator adds the file fingerprints."""
    return Submission(plan=plan, input_paths=tuple(input_paths), key=key)
