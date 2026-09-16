"""One batch = a fixed selection + a fixed packaging rule + a fixed template version.

The problem this module solves is the second lesson: a member who finished 50 words
wants "the same thing for the next 50", and an Agent asked to submit that must be able
to show *what* it is about to run before anything is spent.  So a batch is planned as
a **document** first — who, which words, how many synthesis requests, how much disk —
and the plan is a read-only product: it names the instances it *would* create and the
jobs it *would* submit, and writes nothing.

Each package gets its own project instance, expanded from the pinned template for
exactly that package's records.  Two consequences are the point:

* packages share no mutable state, so one failing package cannot leave the others
  half-edited, and a redo of one package touches nothing else;
* the instance is the member's to edit afterwards (the editor saves its own
  revisions), which is what makes a later template upgrade a *merge* rather than a
  rebuild — see :mod:`word_video.application.upgrade`.

Costs are counted from what already exists: a recording that is registered and on
disk is a **hit** (no synthesis, no request), anything else is a **miss** and costs
one synthesis request.  That is the number a member is really being asked to approve,
so it is in the plan document rather than discovered at run time.
"""
from dataclasses import dataclass, field, replace
import datetime
import hashlib
import json
from pathlib import Path
import shutil

from ..domain.errors import ProjectError, SchemaError
from ..domain.lesson import LessonTemplate
from ..domain.model import TEACHING_STAGES, Project
from ..domain.plan import Conflict
from .batches import (BatchSelection, DeliveryCheck, ExportProfile, check_delivery,
                      plan_batch, select_records)
from .instantiate import asset_id_for, instantiate
from .intro import measure_project_intro
from .templates import TemplateDocument

SCHEMA = 'wv-batch@1'
BATCH_KEYS = ('schema', 'batch_id', 'source_project', 'source_folder', 'template_id',
              'template_version', 'selection', 'rule', 'profile', 'packages', 'usage',
              'disk', 'checks', 'created', 'notes')

#: Codecs a delivery may ask for; anything else is refused before a run starts.
DELIVERY_CODECS = ('h264', 'h265')


@dataclass(frozen=True)
class PackageRule:
    """How a selection is cut into packages; the rule is part of the batch's identity.

    ``per_package`` and ``count`` are two ways to say the same thing, and a rule that
    says neither is one package — the smallest thing a batch can be.  The rule is
    fixed at plan time on purpose: package ids come from it, and a job is identified
    by its package, so "the same batch" must cut the same way.
    """

    per_package: int = 0
    count: int = 0

    def __post_init__(self):
        for name in ('per_package', 'count'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SchemaError('package %s must be a non-negative integer' % name,
                                  path='rule')
        if self.per_package and self.count:
            raise SchemaError('give --per-package or --packages, not both', path='rule')

    def describe(self):
        if self.per_package:
            return {'per_package': self.per_package}
        if self.count:
            return {'count': self.count}
        return {'per_package': 0, 'count': 0}

    @property
    def label(self):
        if self.per_package:
            return 'x%d' % self.per_package
        if self.count:
            return '/%d' % self.count
        return 'x1'


def split_packages(records, rule):
    """Cut ``records`` into chunks: in order, no empty package, no reordering."""
    records = tuple(records)
    if not records:
        return ()
    if rule.per_package:
        size = rule.per_package
        return tuple(records[start:start + size]
                     for start in range(0, len(records), size))
    if rule.count:
        if rule.count > len(records):
            raise SchemaError('%d packages for %d records' % (rule.count, len(records)),
                              path='rule', hint='包数不能多于词数')
        base, extra = divmod(len(records), rule.count)
        chunks, cursor = [], 0
        for index in range(rule.count):
            size = base + (1 if index < extra else 0)
            chunks.append(records[cursor:cursor + size])
            cursor += size
        return tuple(chunks)
    return (records,)


def package_id_for(records, ordinal):
    """A stable, printable id: the word range when the list has numbers."""
    if records and all(record.index > 0 for record in records):
        return 'p%d-%d' % (records[0].index, records[-1].index)
    return 'p%02d' % ordinal


@dataclass(frozen=True)
class UsageReport:
    """What the recordings that already exist cover, and what a run would need."""

    hits: int = 0
    misses: int = 0
    units: int = 0                 # synthesis requests: one per recording to (re)make
    missing: tuple = ()            # no recording for this text at all
    changed: tuple = ()            # a recording exists but speaks a different text
    unverified: int = 0            # a recording whose spoken text cannot be checked
    input_bytes: int = 0           # measured size of the recordings this package reads
    assets: tuple = ()             # every asset id this package's speech needs

    def to_dict(self):
        return {'hits': self.hits, 'misses': self.misses, 'units': self.units,
                'missing': [dict(item) for item in self.missing],
                'changed': [dict(item) for item in self.changed],
                'unverified': self.unverified, 'input_bytes': self.input_bytes,
                'assets': list(self.assets)}

    def merge(self, other):
        return UsageReport(hits=self.hits + other.hits,
                           misses=self.misses + other.misses,
                           units=self.units + other.units,
                           missing=self.missing + other.missing,
                           changed=self.changed + other.changed,
                           unverified=self.unverified + other.unverified,
                           input_bytes=self.input_bytes + other.input_bytes,
                           assets=self.assets + other.assets)


def cache_entries(paths):
    """B's read-only cache reader, pointed at the folders the assets already live in.

    A recording that lives in an ``audio-cache`` folder carries a record naming the
    text it speaks, which is the only way to tell "this word still has its audio"
    from "this word was edited and its audio is now the *old* word" — an asset id
    like ``w151:female`` says which slot it fills, not what it says.  Reading that
    record is what makes the miss count below a fact rather than an assumption.
    """
    from ..importers.cache import scan_roots
    roots = set()
    for path in paths:
        for parent in Path(path).parents:
            if parent.name == 'audio-cache':
                roots.add(parent.parent)
                break
    if not roots:
        return {}
    scan = scan_roots(sorted(roots))
    return {str(Path(entry.path).resolve()).lower(): entry for entry in scan.entries}


def recording_usage(project, registry, records, lesson, cache=None):
    """Which recordings this selection already has, and which it would have to make.

    A hit is a registered asset whose file is there, has been measured, and — when
    the cache record can be read — speaks exactly the text this stage needs now.  An
    unmeasured file cannot go into a plan, so it is not a usable hit even though the
    bytes exist; a recording that speaks an older text is a **miss with a reason**,
    because rendering it would put the wrong audio under the right caption.
    """
    from ..importers.cache import normalize_text
    records = tuple(records)
    hits = misses = unverified = 0
    missing, changed, assets = [], [], []
    total = 0
    asset_ids = _asset_ids(project)
    for record in records:
        for role in TEACHING_STAGES:
            text = record.field(_field_of(lesson, role))
            asset_id = asset_ids.get((record.id, role)) or asset_id_for(record.id, role)
            assets.append(asset_id)
            ref = registry.ref(asset_id) if registry is not None and registry.has(asset_id) \
                else None
            candidate = _candidate(registry, asset_id) if ref is not None \
                and ref.media_info is not None else None
            if candidate is None or not candidate.is_file():
                misses += 1
                missing.append({'record_id': record.id, 'role': role, 'text': text,
                                'asset_id': asset_id})
                continue
            total += candidate.stat().st_size
            entry = cache.get(str(candidate.resolve()).lower()) if cache else None
            if entry is None:
                # The file is not in a cache folder (a TTS write straight into the
                # project, say): usable, but its text cannot be checked from here.
                hits += 1
                unverified += 1
            elif normalize_text(entry.text) == normalize_text(text):
                hits += 1
            else:
                misses += 1
                changed.append({'record_id': record.id, 'role': role, 'text': text,
                                'was': entry.text, 'asset_id': asset_id})
    return UsageReport(hits=hits, misses=misses, units=misses,
                       missing=tuple(missing), changed=tuple(changed),
                       unverified=unverified, input_bytes=total,
                       assets=tuple(assets))


def _candidate(registry, asset_id):
    try:
        return Path(registry.candidate_path(asset_id))
    except Exception:                    # noqa: BLE001 - an unusable entry is a miss
        return None


def _pairs(items):
    """Stored per-recording records back as the tuples the report carries."""
    return tuple(tuple(sorted(item.items())) for item in items or ())


def _conflict(item):
    """A problem from a stored document, back in the shape a planner reports."""
    if isinstance(item, Conflict):
        return item
    if not isinstance(item, dict):
        return Conflict(code='BATCH_PROBLEM', message=str(item))
    return Conflict(code=str(item.get('code', 'BATCH_PROBLEM')),
                    message=str(item.get('message', '')),
                    path=str(item.get('object_path', '') or item.get('path', '')),
                    hint=str(item.get('hint', '')))


def _field_of(lesson, role):
    node = lesson.stage(role)
    return node.text_field if node is not None else 'word'


def _asset_ids(project):
    """``(record_id, role) -> asset_id`` from the clips a member already has."""
    found = {}
    for clip in project.clips:
        if clip.source is not None and clip.record_id and clip.role in TEACHING_STAGES:
            found[(clip.record_id, clip.role)] = clip.source.asset_id
    return found


@dataclass(frozen=True)
class PackagePlan:
    """One package: its records, the instance it runs as, and what it would spend."""

    package_id: str
    project_id: str
    record_ids: tuple = ()
    words: tuple = ()
    first: int = 0
    last: int = 0
    plan_identity: str = ''
    total_ticks: int = 0
    usage: UsageReport = UsageReport()
    delivery: DeliveryCheck = DeliveryCheck()
    problems: tuple = ()
    basis: str = 'source'          # where the records came from: source | instance
    instance_revision: int = 0

    @property
    def ok(self):
        return not self.problems

    @property
    def ready(self):
        return self.ok and self.delivery.ready

    def digest(self):
        """Content identity: what a redo compares to decide "this package changed"."""
        payload = {'plan': self.plan_identity, 'records': list(self.words),
                   'project': self.project_id, 'revision': self.instance_revision,
                   'basis': self.basis,
                   'delivery': self.delivery.to_dict()['background']}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')
                              ).hexdigest()

    def to_dict(self):
        return {'package_id': self.package_id, 'project_id': self.project_id,
                'record_ids': list(self.record_ids),
                'words': [dict(item) for item in self.words],
                'first': self.first, 'last': self.last,
                'plan_identity': self.plan_identity, 'total_ticks': self.total_ticks,
                'usage': self.usage.to_dict(), 'delivery': self.delivery.to_dict(),
                'problems': [problem.to_dict() for problem in self.problems],
                'basis': self.basis, 'instance_revision': self.instance_revision,
                'digest': self.digest()}

    @classmethod
    def from_dict(cls, document, path='batch'):
        if not isinstance(document, dict):
            raise SchemaError('a package plan must be an object', path=path)
        usage = document.get('usage') or {}
        delivery = document.get('delivery') or {}
        return cls(package_id=document['package_id'],
                   project_id=document['project_id'],
                   record_ids=tuple(document.get('record_ids') or ()),
                   words=tuple(tuple(sorted(item.items()))
                               for item in document.get('words') or ()),
                   first=document.get('first', 0), last=document.get('last', 0),
                   plan_identity=document.get('plan_identity', ''),
                   total_ticks=document.get('total_ticks', 0),
                   usage=UsageReport(hits=usage.get('hits', 0),
                                     misses=usage.get('misses', 0),
                                     units=usage.get('units', 0),
                                     missing=_pairs(usage.get('missing')),
                                     changed=_pairs(usage.get('changed')),
                                     unverified=usage.get('unverified', 0),
                                     input_bytes=usage.get('input_bytes', 0),
                                     assets=tuple(usage.get('assets') or ())),
                   delivery=DeliveryCheck(ready=bool(delivery.get('ready')),
                                          background=delivery.get('background', ''),
                                          fixes=tuple(delivery.get('fixes') or ())),
                   problems=tuple(_conflict(item)
                                  for item in document.get('problems') or ()),
                   basis=document.get('basis', 'source'),
                   instance_revision=document.get('instance_revision', 0))


@dataclass(frozen=True)
class Check:
    """One area of the preflight: what was looked at, and what is wrong with it.

    ``blocking`` is the difference between "you should know this" and "this batch
    cannot be produced": a missing recording or a font that resolves to nothing stops
    a delivery (the exporter refuses it), while an unrecorded voice only means the
    delivery cannot *name* the voice it reused.  Both carry fixes, because a caller
    that only learns "no" cannot act.
    """

    name: str
    ok: bool = True
    blocking: bool = False
    problems: tuple = ()
    fixes: tuple = ()
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        return {'name': self.name, 'ok': self.ok, 'blocking': self.blocking,
                'problems': [problem.to_dict() if isinstance(problem, Conflict)
                             else dict(problem) for problem in self.problems],
                'fixes': list(self.fixes), **dict(self.detail)}


#: What a caller can do about a recording that is not there.  Kept beside the check
#: that produces it so every refusal (plan, submit) says the same thing.
MEDIA_FIXES = (
    '把缺失的配音放进工程资产目录，并在 assets.json 里登记（wv-assets@1）',
    r'或复用旧批次已有音频：import audio --roots <含 audio-cache 的目录>'
    r' --project <源工程> --batch <范围> --apply',
    '缺媒体不能靠估算绕过：计划只接受已测量的真实媒体',
)

FONT_FIXES = (
    '给该角色选一个本机已安装的字体（编辑器里改字号/字体，或 doctor 看字体解析结果）',
    '模板与工程都不写字体路径：字体由本机解析链决定，缺字体时必须显式处理',
)

VOICE_FIXES = (
    'import audio --voices female=BV503_streaming,male=BV504_streaming 记录音色后 --apply',
    '或在 assets.json 的 voice 字段写出这条配音的音色（复用已有音频不会因此获得合成能力）',
)


def check_media(packages, registry=None):
    """Every recording the batch would read: present, or a named problem.

    A *changed* recording (the file is there but speaks an older text) is not a
    problem here — it is the cost line, and it is what ``usage.units`` counts.  A
    recording that is not there at all is: the exporter refuses to run without it.
    """
    missing, fixes = [], []
    for package in packages:
        for item in package.usage.missing:
            entry = dict(item)
            entry['package_id'] = package.package_id
            missing.append(entry)
    problems = tuple(Conflict(code='MEDIA_MISSING',
                              message='%s 的 %s 没有可用录音（%s）'
                                      % (item['record_id'], item['role'],
                                         item['asset_id']),
                              path='asset:%s' % item['asset_id'],
                              hint='先准备这条配音再提交，不要用估算代替')
                     for item in missing)
    if missing:
        fixes.append('缺失 %d 条：%s'
                     % (len(missing), ', '.join(sorted({item['asset_id']
                                                        for item in missing})[:5])))
        fixes.extend(MEDIA_FIXES)
    return Check(name='media', ok=not missing, blocking=bool(missing),
                 problems=problems, fixes=tuple(fixes),
                 detail={'missing': missing,
                         'hits': sum(package.usage.hits for package in packages),
                         'units': sum(package.usage.units for package in packages)})


def check_fonts(styles):
    """The fonts the plan's own style table resolves to.

    The exporter refuses a style role with no usable font (``_styles``), so a plan
    that does not look is a plan that fails after the batch was accepted.  Fonts come
    from the resolution chain — never from the template — so this check reads the same
    merged table the renderer will read.
    """
    from .styles import merged_styles, style_fonts

    table = merged_styles(styles)
    paths, _ = style_fonts(table)
    problems, missing, unreadable = [], [], []
    for role in sorted(paths):
        path = paths[role]
        if not path:
            missing.append(role)
            problems.append(Conflict(code='FONT_MISSING',
                                     message='样式 %s 没有解析到字体文件' % role,
                                     path='styles.%s.font' % role,
                                     hint='字体由本机解析链决定；缺字体时导出会在渲染前失败'))
            continue
        try:
            with open(path, 'rb') as stream:
                stream.read(1)
        except OSError as error:
            unreadable.append(role)
            problems.append(Conflict(code='FONT_UNREADABLE',
                                     message='样式 %s 的字体打不开：%s（%s）'
                                             % (role, path, error),
                                     path='styles.%s.font' % role,
                                     hint='换一个可读的字体文件'))
    fixes = list(FONT_FIXES) if problems else []
    if missing or unreadable:
        fixes.append('有问题的角色：%s' % ', '.join(sorted(missing + unreadable)))
    return Check(name='fonts', ok=not problems, blocking=bool(problems),
                 problems=tuple(problems), fixes=tuple(fixes),
                 detail={'roles': sorted(table), 'missing': missing,
                         'unreadable': unreadable,
                         'resolved': {role: bool(paths[role]) for role in sorted(paths)}})


def check_voices(packages, registry=None):
    """Which speech recordings have no *recorded* voice.

    Not blocking on purpose: this build reuses audio it already has and never
    synthesises, so an unrecorded voice does not stop a delivery — it means the
    delivery cannot say whose voice it reused, which is a fact the caller wants
    before a batch is published, not a reason to refuse it.
    """
    unrecorded, recorded = [], 0
    for package in packages:
        for asset_id in package.usage.assets:
            voice = ''
            if registry is not None and registry.has(asset_id):
                voice = str(getattr(registry.ref(asset_id), 'voice', '') or '')
            if voice:
                recorded += 1
            else:
                unrecorded.append({'package_id': package.package_id, 'asset_id': asset_id})
    fixes = list(VOICE_FIXES) if unrecorded else []
    if unrecorded:
        fixes.append('未记录音色的素材 %d 条：%s'
                     % (len(unrecorded),
                        ', '.join(sorted({item['asset_id'] for item in unrecorded})[:5])))
    return Check(name='voices', ok=not unrecorded, blocking=False,
                 problems=((Conflict(code='VOICE_UNRECORDED',
                                     message='%d 条朗读没有记录音色' % len(unrecorded),
                                     path='assets.voice',
                                     hint='复用已有音频时音色只作记录；不记录就无法核对交付'),)
                           if unrecorded else ()),
                 fixes=tuple(fixes),
                 detail={'recorded': recorded, 'unrecorded': unrecorded})


def check_disk(free_bytes, reference_bytes, packages):
    """Free space against the size of the last published run, times the packages.

    A plan cannot know what an encoder will produce, so it does not guess: it takes
    the last published run of this root as the measured stand-in and refuses only when
    even that is already larger than what is free.
    """
    count = max(1, len(packages))
    required = int(reference_bytes) * count if reference_bytes else None
    free = int(free_bytes or 0)
    low = bool(required) and free < required
    problems = ()
    fixes = ()
    if low:
        problems = (Conflict(code='DISK_LOW',
                             message='可用磁盘 %d 字节 < 预计需要 %d 字节'
                                     '（最近一次运行 %d 字节 × %d 个包）'
                                     % (free, required, reference_bytes, count),
                             path='disk',
                             hint='清理工作根里的旧运行/缓存，或减少本批包数'),)
        fixes = ('磁盘余量不足：先清理 <root>/runs 或缓存目录，再提交本批',
                 '也可以加 --per-package 让单包更小、分批交付')
    return Check(name='disk', ok=not low, blocking=low, problems=problems, fixes=fixes,
                 detail={'free_bytes': free, 'reference_output_bytes': reference_bytes,
                         'required_bytes': required, 'packages': count})


def check_target(profile, project):
    """Whether this delivery target is one the encoder chain can actually produce.

    Two rules that are real and cheap to state: the codec has to be one this build
    delivers, and a canvas has to have even dimensions (h264/h265 encode in chroma
    pairs, so an odd width or height fails at the encoder, minutes in).
    """
    problems, fixes = [], []
    codec = str(profile.video_codec or '')
    if codec not in DELIVERY_CODECS:
        problems.append(Conflict(code='TARGET_CODEC',
                                 message='不支持的编码 %r（可用：%s）'
                                         % (codec, '/'.join(DELIVERY_CODECS)),
                                 path='profile.video_codec',
                                 hint='--codec h264 或 --codec h265'))
        fixes.append('--codec %s' % '/'.join(DELIVERY_CODECS))
    odd = [name for name in ('width', 'height') if int(getattr(project, name, 0)) % 2]
    if odd:
        problems.append(Conflict(code='TARGET_CANVAS',
                                 message='画布 %sx%s 有奇数边长，h264/h265 无法编码'
                                         % (project.width, project.height),
                                 path='project.%s' % odd[0],
                                 hint='把画布改成偶数尺寸'))
        fixes.append('把工程画布改成偶数边长（当前 %dx%d）'
                     % (project.width, project.height))
    if int(project.fps_num or 0) <= 0:
        problems.append(Conflict(code='TARGET_FPS', message='工程帧率无效',
                                 path='project.fps_num', hint='帧率必须是正整数'))
        fixes.append('修正工程帧率')
    return Check(name='target', ok=not problems, blocking=bool(problems),
                 problems=tuple(problems), fixes=tuple(fixes),
                 detail={'codec': codec, 'width': project.width, 'height': project.height,
                         'fps': '%d/%d' % (project.fps_num, project.fps_den),
                         'slices': profile.slices})


def check_template(source, template):
    """Where this batch's arrangement comes from — the answer is not always obvious.

    A template supplies the arrangement: the layers it declares and the styles it
    carries.  A source project supplies the media (including its intro layer, which the
    template has to declare to keep — see :func:`intro_from`).  A plan that does not say
    which of the two decided what leaves an Agent unable to explain the delivery it is
    about to freeze, so the plan says it.
    """
    intro = source.intro_clip() is not None
    from_template = bool(template.styles)
    detail = {'template': '%s@%d' % (template.template_id, template.version),
              'styles_from': 'template' if from_template else 'source',
              'styles': sorted((template.style_table() if from_template
                                else source.style_table())),
              'intro_in_source': intro, 'intro_in_template': bool(template.intro),
              'intro_used': bool(intro and template.intro)}
    if intro and not template.intro:
        detail['note'] = ('源工程有片头图层，但模板没有声明：这一批不会画片头'
                          '（要保留就先用 template save --from-project 记录含片头的模板）')
    return Check(name='template', ok=True, blocking=False, detail=detail)


@dataclass(frozen=True)
class Preflight:
    """The preflight as a whole: every area, and whether the batch may be submitted."""

    checks: tuple = ()

    def check(self, name):
        for item in self.checks:
            if item.name == name:
                return item
        return None

    @property
    def blocking(self):
        return tuple(item for item in self.checks if item.blocking and not item.ok)

    @property
    def ready(self):
        return not self.blocking

    @property
    def fixes(self):
        found = []
        for item in self.checks:
            if not item.ok:
                found.extend(item.fixes)
        return found

    def to_dict(self):
        return {'ready': self.ready,
                'blocking': [item.name for item in self.blocking],
                'checks': [item.to_dict() for item in self.checks],
                'fixes': self.fixes}

    @classmethod
    def from_dict(cls, document, path='batch'):
        if not isinstance(document, dict):
            raise SchemaError('preflight must be an object', path=path)
        checks = []
        for item in document.get('checks') or ():
            if not isinstance(item, dict) or 'name' not in item:
                raise SchemaError('every check needs a name', path=path)
            rest = {key: value for key, value in item.items()
                    if key not in ('name', 'ok', 'blocking', 'problems', 'fixes')}
            checks.append(Check(name=item['name'], ok=bool(item.get('ok', True)),
                                blocking=bool(item.get('blocking', False)),
                                problems=tuple(_conflict(problem)
                                               for problem in item.get('problems') or ()),
                                fixes=tuple(item.get('fixes') or ()), detail=rest))
        return cls(checks=tuple(checks))


@dataclass(frozen=True)
class BatchDocument:
    """The whole plan: one selection, one rule, one template version, N packages."""

    batch_id: str
    source_project: str = ''
    source_folder: str = ''
    template_id: str = ''
    template_version: int = 1
    selection: BatchSelection = BatchSelection()
    rule: PackageRule = PackageRule()
    profile: ExportProfile = ExportProfile()
    packages: tuple = ()
    created: str = ''
    notes: str = ''
    disk: dict = field(default_factory=dict)
    checks: Preflight = Preflight()
    schema: str = SCHEMA

    @property
    def usage(self):
        total = UsageReport()
        for package in self.packages:
            total = total.merge(package.usage)
        return total

    @property
    def ready(self):
        return bool(self.packages) and self.checks.ready \
            and all(package.ready for package in self.packages)

    @property
    def plan_identity(self):
        """The identity of what this batch would run.

        One package answers with its own solved identity, so a batch that is not cut
        into packages answers exactly what a single batch always did.  Several
        packages answer with one digest over the ordered package identities: it names
        the whole batch without pretending the packages are one solve.
        """
        if not self.packages:
            return ''
        if len(self.packages) == 1:
            return self.packages[0].plan_identity
        payload = [[package.package_id, package.plan_identity]
                   for package in self.packages]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode('utf-8')
                              ).hexdigest()

    @property
    def total_ticks(self):
        return sum(package.total_ticks for package in self.packages)

    @property
    def records(self):
        """Every selected record, in package order."""
        return tuple(dict(word) for package in self.packages for word in package.words)

    @property
    def delivery(self):
        """The delivery verdict, which belongs to the batch, not to one package."""
        return self.packages[0].delivery if self.packages else DeliveryCheck()

    @property
    def problems(self):
        found = []
        for package in self.packages:
            found.extend(package.problems)
        return tuple(found)

    def package(self, package_id):
        for package in self.packages:
            if package.package_id == package_id:
                return package
        return None

    def fixes(self):
        """What to do about everything that is not ready, in one flat list."""
        fixes = []
        for package in self.packages:
            if not package.ok:
                fixes.extend(problem.hint or problem.message
                             for problem in package.problems)
            elif not package.delivery.ready:
                fixes.extend(package.delivery.fixes)
        fixes.extend(self.checks.fixes)
        return fixes

    def to_dict(self):
        return {'schema': self.schema, 'batch_id': self.batch_id,
                'source_project': self.source_project,
                'source_folder': self.source_folder,
                'template_id': self.template_id,
                'template_version': self.template_version,
                'selection': self.selection.describe(), 'rule': self.rule.describe(),
                'profile': self.profile.to_dict(),
                'packages': [package.to_dict() for package in self.packages],
                'usage': self.usage.to_dict(), 'disk': dict(self.disk),
                'checks': self.checks.to_dict(),
                'created': self.created, 'notes': self.notes}

    @classmethod
    def from_dict(cls, document, path='batch'):
        if not isinstance(document, dict):
            raise SchemaError('a batch document must be a JSON object', path=path)
        unknown = sorted(set(document) - set(BATCH_KEYS))
        if unknown:
            raise SchemaError('batch document has unknown field(s): %s'
                              % ', '.join(unknown), path=path)
        if document.get('schema') != SCHEMA:
            raise SchemaError('unknown batch schema %r' % (document.get('schema'),),
                              path=path, hint='本版本只读 %s' % SCHEMA)
        selection = document.get('selection') or {}
        rule = document.get('rule') or {}
        profile = document.get('profile') or {}
        return cls(batch_id=document['batch_id'],
                   source_project=document.get('source_project', ''),
                   source_folder=document.get('source_folder', ''),
                   template_id=document.get('template_id', ''),
                   template_version=document.get('template_version', 1),
                   selection=BatchSelection(record_ids=tuple(selection.get('record_ids')
                                                             or ()),
                                            first=selection.get('first', 0),
                                            last=selection.get('last', 0)),
                   rule=PackageRule(per_package=rule.get('per_package', 0),
                                    count=rule.get('count', 0)),
                   profile=ExportProfile(background=profile.get('background', ''),
                                         video_codec=profile.get('video_codec', 'h264'),
                                         slices=profile.get('slices')),
                   packages=tuple(PackagePlan.from_dict(item, path)
                                  for item in document.get('packages') or ()),
                   created=document.get('created', ''),
                   notes=document.get('notes', ''),
                   disk=dict(document.get('disk') or {}),
                   checks=Preflight.from_dict(document.get('checks') or {}, path))


def batch_id_for(source_project, selection, rule, template_id, template_version):
    """A stable id: the same batch re-planned is the same batch, not a new one.

    Readable where it can be (the word range) and hashed where it must be (the rule
    and the template version), because the id names the instances and the job keys.
    """
    payload = json.dumps({'project': str(source_project),
                          'selection': selection.describe(), 'rule': rule.describe(),
                          'template': '%s@%d' % (template_id, template_version)},
                         sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()[:8]
    if selection.first or selection.last:
        label = '%s-%s' % (selection.first or 1, selection.last or 'all')
    elif selection.record_ids:
        label = '%dw' % len(selection.record_ids)
    else:
        label = 'all'
    return 'b%s-%s-%s' % (label, rule.label, digest)


class TemplateIntroMismatch(ProjectError):
    """The pinned template would silently drop — or invent — the source's intro layer."""

    code = 'TEMPLATE_INTRO_MISMATCH'


def intro_from(source, template, measured=None):
    """``(slice, audio, measurement)`` for the intro the expansion will build.

    A batch must not change the lesson the member built without saying so, and it must
    not invent a layer either.  Both directions are therefore explicit:

    * source has an intro layer and the template declares one → the layer is kept, with
      the measurement the caller already took (``measured``) so the same file is not
      probed once per package;
    * source has one and the template does not declare one → **refused** by name.  The
      template is the arrangement, so the member is asked to record the arrangement they
      actually have (``template save --from-project``) rather than getting a delivery
      without the countdown they placed;
    * source has none and the template declares one → refused: an intro cannot be
      invented from a recipe.
    """
    if not isinstance(template, TemplateDocument):
        raise SchemaError('a batch needs a TemplateDocument', path='batch')
    clip = source.intro_clip()
    has_intro = clip is not None and clip.source is not None
    if has_intro and not template.intro:
        raise TemplateIntroMismatch(
            '模板 %s@%d 没有片头图层，但源工程 %s 有：这一批不会画片头'
            % (template.template_id, template.version, source.project_id),
            path='template',
            hint='要把成员做好的片头带进这批：template save --from-project <源工程> '
                 '--template <新模板 id>，再用 --template 指定它；'
                 '确实不要片头就换一个不含片头的模板')
    if template.intro and not has_intro:
        raise TemplateIntroMismatch(
            '模板 %s@%d 有片头图层，但源工程 %s 没有片头素材'
            % (template.template_id, template.version, source.project_id),
            path='template', hint='给源工程加片头图层，或用不含片头的模板')
    if not has_intro:
        return None, '', None
    return clip.source, str(clip.audio_asset or ''), \
        (measured if measured is not None else measure_project_intro(source))


def instance_for(source, records, template, media, instance_id, *, intro_slice=None,
                 intro_audio='', intro_measure=None, asset_ids=None):
    """One package's own project instance: the template expanded for its records.

    The instance carries the source's settings and styles and nothing else: it is a
    fresh expansion, so it has a revision of 0 and no member edits until someone
    edits *it*.  That is what lets a redo or an upgrade tell "the template changed"
    from "the member changed it".
    """
    if not isinstance(source, Project):
        raise SchemaError('a batch needs a source Project', path='batch')
    if not isinstance(template, TemplateDocument):
        raise SchemaError('a batch needs a TemplateDocument', path='batch')
    records = tuple(records)
    if not records:
        raise SchemaError('a package needs at least one record', path='batch')
    if intro_slice is None and intro_audio == '' and intro_measure is None:
        intro_slice, intro_audio, intro_measure = intro_from(source, template)
    base = replace(source, project_id=instance_id, records=(), clips=(), revision=0)
    # The styles of an explicit template are its point; a template that carries none
    # leaves the source project's own overrides alone rather than wiping them.
    if template.styles:
        base = replace(base, styles=template.styles)
    lesson = template.lesson
    if not isinstance(lesson, LessonTemplate):
        raise SchemaError('the template document carries no lesson', path='batch')
    return instantiate(lesson, records, media, base, intro=intro_slice,
                       intro_audio=intro_audio, intro_measure=intro_measure,
                       asset_ids=asset_ids or _asset_ids(source))


def plan_document(source, media, registry=None, *, selection=None, rule=None,
                  profile=None, template=None, batch_id='', intro_measure=None,
                  intro=None, free_bytes=0, reference_bytes=None, reference_note='',
                  notes='', source_folder=''):
    """Plan a whole batch: every package, its instance and what it would spend.

    Read-only.  A package that cannot be solved (an unmeasured recording, a template
    that wants an intro the source lacks) is reported with its problem instead of
    raising, exactly like a single batch plan: the preflight is where a caller wants
    the whole list, and ``ready`` says whether anything may be submitted.

    ``intro`` is the already-resolved ``(slice, audio, measurement)`` when the caller
    took it once (the CLI does): without it the source's own intro is resolved here.
    """
    selection = selection or BatchSelection()
    rule = rule or PackageRule()
    profile = profile or ExportProfile()
    template = template or TemplateDocument(template_id='lesson-default', version=1)
    selection = selection if isinstance(selection, BatchSelection) else \
        BatchSelection(**selection)
    records = select_records(source, selection)
    identifier = batch_id or batch_id_for(source.project_id, selection, rule,
                                          template.template_id, template.version)
    asset_ids = _asset_ids(source)
    intro_problem = None
    if intro is not None:
        intro_slice, intro_audio, measure = intro
    else:
        try:
            intro_slice, intro_audio, measure = intro_from(source, template,
                                                          measured=intro_measure)
        except Exception as error:                 # noqa: BLE001 - reported, not raised
            # A template that wants an intro the source cannot supply (or that would
            # drop the one it has) is one problem for the whole batch, and the preflight
            # is where a caller wants to read it: it comes back as a named problem with
            # a fix, not as a crash on the way in.
            intro_slice, intro_audio, measure = None, '', None
            intro_problem = Conflict(code=getattr(error, 'code', type(error).__name__),
                                     message=str(error),
                                     path=getattr(error, 'path', 'batch'),
                                     hint=getattr(error, 'hint', ''))
    # One cache read for the whole plan: the recordings the registry already names.
    cache = cache_entries([registry.candidate_path(ref.asset_id)
                           for ref in registry.refs]) if registry is not None else {}
    packages = []
    for ordinal, chunk in enumerate(split_packages(records, rule), start=1):
        package_id = package_id_for(chunk, ordinal)
        instance_id = '%s-%s' % (identifier, package_id)
        words = tuple((('id', record.id), ('index', record.index), ('word', record.word))
                      for record in chunk)
        usage = recording_usage(source, registry, chunk, template.lesson, cache)
        problems, plan = (), None
        try:
            instance = instance_for(source, chunk, template, media, instance_id,
                                    intro_slice=intro_slice, intro_audio=intro_audio,
                                    intro_measure=measure, asset_ids=asset_ids)
            plan = plan_batch(instance, media, None, profile, measure)
        except Exception as error:                 # noqa: BLE001 - reported, not raised
            problems = (Conflict(code=getattr(error, 'code', type(error).__name__),
                                 message=str(error),
                                 path=getattr(error, 'path', 'batch'),
                                 hint=getattr(error, 'hint', '')),)
        delivery = check_delivery(profile) if plan is None else plan.delivery
        packages.append(PackagePlan(
            package_id=package_id, project_id=instance_id,
            record_ids=tuple(record.id for record in chunk), words=words,
            first=(chunk[0].index if chunk[0].index > 0 else 0),
            last=(chunk[-1].index if chunk[-1].index > 0 else 0),
            plan_identity=plan.plan_identity if plan is not None else '',
            total_ticks=plan.total_ticks if plan is not None else 0,
            usage=usage, delivery=delivery,
            problems=((intro_problem,) if intro_problem else ())
            + problems + (plan.problems if plan is not None else ()),
            basis='source', instance_revision=0))
    document = BatchDocument(
        batch_id=identifier, source_project=source.project_id,
        source_folder=str(source_folder or ''),
        template_id=template.template_id, template_version=template.version,
        selection=selection, rule=rule, profile=profile, packages=tuple(packages),
        created=_now(), notes=notes,
        disk={'input_bytes': sum(package.usage.input_bytes for package in packages),
              'free_bytes': int(free_bytes),
              'reference_output_bytes': reference_bytes,
              'reference_note': reference_note},
        checks=preflight(source, template, profile, packages, registry,
                         free_bytes=free_bytes, reference_bytes=reference_bytes))
    return document


def preflight(source, template, profile, packages, registry=None, *, free_bytes=0,
              reference_bytes=None):
    """Every area a submission must be complete in, checked before anything is frozen.

    The order is the order a caller can act in: media (what the words need), fonts
    (what the picture needs), voices (what the delivery should record), disk and the
    target.  Nothing here writes, renders or synthesises.
    """
    styles = template.style_table() or source.style_table()
    return Preflight(checks=(check_media(packages, registry),
                             check_fonts(styles),
                             check_voices(packages, registry),
                             check_disk(free_bytes, reference_bytes, packages),
                             check_target(profile, source),
                             check_template(source, template)))


def _now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def reference_bytes_for(coordinator):
    """Size of the last published run, as the measured stand-in for "how much disk".

    A plan cannot know what an encoder will produce, so it does not pretend to: it
    reports the size of the most recent published run of this root, which is a real
    number with a stated basis, and ``None`` when there is nothing to measure.

    Reading must not *create* anything either: on a root with no database yet this
    returns nothing rather than opening (and therefore writing) one, because
    "planning writes nothing" has to hold for the very first plan as well.
    """
    if not Path(coordinator.db).is_file():
        return None, ''
    try:
        receipts = coordinator.receipts()
    except Exception:                    # noqa: BLE001 - a plan must not need a database
        return None, ''
    for row in reversed(receipts):
        try:
            job = coordinator.status(row['job_id'])
        except Exception:                # noqa: BLE001
            continue
        if job['state'] != 'succeeded':
            continue
        total = 0
        for item in job['artifacts']:
            try:
                total += Path(item['path']).stat().st_size
            except OSError:
                return None, ''
        return total, '最近一次已发布运行（job %s）的产物字节数' % job['id']
    return None, ''


def free_bytes_for(root):
    try:
        return shutil.disk_usage(str(root)).free
    except OSError:
        return 0
