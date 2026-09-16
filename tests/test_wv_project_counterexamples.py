"""W01 counterexamples against the real `wv-project@1` surface.

The negative cases were written before A's interface landed (task QA-1) and are
now wired to it: every entry names the layer that must refuse, the machine
readable ``ProjectError.code`` and (where the contract fixes it) the object path.
A silent repair, a crash, or a *different* error code all fail the test.

Independence
------------
The lesson used here is built by hand, not by the production expander:

    word 151 "demonstrate", speed 1.25, gap 0.1 s, fps 60, intro 1.0 s
    female  : max(1.0, 1.15+0.1) / 1.25 = 1.00 s ->  60 frames =   720000 ticks
    male    : max(1.0, 0.85+0.1) / 1.25 = 0.80 s ->  48 frames =   576000 ticks
    chinese : max(1.6, 3.275+0.1) / 1.25 = 2.70 s -> 162 frames = 1944000 ticks
      ("证明；证实" = 4 Hanzi -> 4 * 0.4 = 1.6 s, the approved Chinese floor)
    intro   : ceil(1.0 * 60) = 60 frames                        =   720000 ticks

Those numbers come from the approved rhythm rules and the read-only word list;
none of them is produced by calling the production solver.  The production calls
in this file are the *subject* of the test (``cue_plan`` / ``apply`` /
``load_project``), never the source of an expectation.
"""
import json
from pathlib import Path
import sys
from fractions import Fraction

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# The interface under test.  It lives in the repository (W01, task/A); when it is
# not importable the tests fail loudly instead of quietly passing.
from word_video.application import Session, TrimClip, apply  # noqa: E402
from word_video.domain import (SCHEMA, AmbiguousRoleError, Clip,  # noqa: E402
                               ClipBoundError, CycleError, DanglingRefError,
                               DuplicateIdError, InvalidTimeError, MediaInfo,
                               MediaSlice, MissingRoleError, Project, ProjectError,
                               Record, SchemaError, SourceRangeError,
                               StaleRevisionError, TeachingOrderError,
                               TeachingOverlapError, TimeExpr, UnknownDurationError,
                               UnsupportedRateError, cue_plan, render_plan,
                               solve)
from word_video.storage import load_project, save_project  # noqa: E402

# --- the hand-built lesson -------------------------------------------------
WORD = 'demonstrate'
PHONETIC = 'ˈdemənstreɪt'
MEANING = 'v. 证明；证实'
SPOKEN = '证明；证实'
HANZI_COUNT = 4
CHINESE_FLOOR_S = HANZI_COUNT * 0.4          # 1.6 s (approved floor rule)
SPEED = 1.25
GAP_S = 0.1
FPS = 60

INTRO_TICKS = 720000                          # 60 frames
FEMALE = (720000, 1440000)                    # 60 frames
MALE = (1440000, 2016000)                     # 48 frames
CHINESE = (2016000, 3960000)                  # 162 frames
WORD_END = CHINESE[1]

FEMALE_SOURCE_S = Fraction('1.15')
MALE_SOURCE_S = Fraction('0.85')
CHINESE_SOURCE_S = Fraction('3.275')
UNIT_NUM, UNIT_DEN = 1, 48000


def units(seconds):
    return int(Fraction(seconds) * UNIT_DEN)


def golden_record(record_id='w1', index=151):
    return Record(id=record_id, word=WORD, phonetic=PHONETIC, meaning=MEANING,
                  spoken_meaning=SPOKEN, index=index)


def golden_media(record_id='w1'):
    lengths = {'female': FEMALE_SOURCE_S, 'male': MALE_SOURCE_S,
               'chinese': CHINESE_SOURCE_S}
    return {('%s:%s' % (record_id, role)):
            MediaInfo('%s:%s' % (record_id, role), units(seconds), UNIT_NUM, UNIT_DEN)
            for role, seconds in lengths.items()}


def _source(record_id, role, seconds):
    return MediaSlice('%s:%s' % (record_id, role), 0, units(seconds),
                      UNIT_NUM, UNIT_DEN, speed=SPEED)


def golden_clips(record_id='w1'):
    """The layout the default template expands, with absolute instants."""
    return (
        Clip(id='%s.female' % record_id, role='female', record_id=record_id,
             start=TimeExpr.at(FEMALE[0]), duration_ticks=FEMALE[1] - FEMALE[0],
             text=WORD, source=_source(record_id, 'female', FEMALE_SOURCE_S)),
        Clip(id='%s.male' % record_id, role='male', record_id=record_id,
             start=TimeExpr.at(MALE[0]), duration_ticks=MALE[1] - MALE[0],
             text=WORD, source=_source(record_id, 'male', MALE_SOURCE_S)),
        Clip(id='%s.chinese' % record_id, role='chinese', record_id=record_id,
             start=TimeExpr.at(CHINESE[0]), duration_ticks=CHINESE[1] - CHINESE[0],
             text=SPOKEN, source=_source(record_id, 'chinese', CHINESE_SOURCE_S)),
        Clip(id='%s.english' % record_id, role='english', record_id=record_id,
             start=TimeExpr.at(FEMALE[0]), duration_ticks=WORD_END - FEMALE[0],
             text=WORD),
        Clip(id='%s.phonetic' % record_id, role='phonetic', record_id=record_id,
             start=TimeExpr.at(MALE[0]), duration_ticks=WORD_END - MALE[0],
             text=PHONETIC),
        Clip(id='%s.meaning' % record_id, role='meaning', record_id=record_id,
             start=TimeExpr.at(CHINESE[0]), duration_ticks=WORD_END - CHINESE[0],
             text=MEANING),
        Clip(id='layer.title', role='title', start=TimeExpr.at(0),
             duration_ticks=WORD_END, text='四级1500高频词'),
        Clip(id='layer.subtitle', role='subtitle', start=TimeExpr.at(0),
             duration_ticks=WORD_END, text='速通（151–200）'),
        Clip(id='layer.footer', role='footer', start=TimeExpr.at(0),
             duration_ticks=WORD_END, text='不积小流 无以成江海'),
        Clip(id='layer.background', role='background', start=TimeExpr.at(0),
             duration_ticks=WORD_END),
    )


def golden_base(**overrides):
    settings = dict(project_id='qa-golden', intro_s=1.0, fps_num=FPS, fps_den=1,
                    speed=SPEED, gap_s=GAP_S, records=(golden_record(),),
                    clips=golden_clips())
    settings.update(overrides)
    return Project(**settings)


def media():
    return golden_media()


def with_clips(project, clips):
    return project.with_clips(clips)


def replace_clip(project, clip_id, **changes):
    """Rewrite one clip by field, keeping every other field as it was."""
    def fields(clip):
        return {'id': clip.id, 'role': clip.role, 'record_id': clip.record_id,
                'start': clip.start, 'duration_ticks': clip.duration_ticks,
                'text': clip.text, 'source': clip.source}

    return with_clips(project, tuple(
        Clip(**{**fields(clip), **changes}) if clip.id == clip_id else clip
        for clip in project.clips))


# --- builders: each returns a zero-argument callable -----------------------
def build_golden():
    return lambda: cue_plan(golden_base(), media())


def build_duplicate_record_id():
    project = golden_base(records=(golden_record(), golden_record()))
    return lambda: cue_plan(project, media())


def build_duplicate_clip_id():
    project = golden_base(clips=golden_clips() + (golden_clips()[0],))
    return lambda: cue_plan(project, media())


def build_duplicate_record_index():
    other = Record(id='w2', word='deputy', phonetic='ˈdepjuti', meaning='n. 副手；代理',
                   spoken_meaning='副手；代理', index=151)
    project = golden_base(records=(golden_record(), other))
    return lambda: cue_plan(project, media())


def build_dangling_record_id():
    orphan = Clip(id='w2.female', role='female', record_id='w2',
                  start=TimeExpr.at(0), duration_ticks=720000,
                  source=MediaSlice('w2:female', 0, 48000, speed=SPEED))
    return lambda: cue_plan(with_clips(golden_base(), golden_clips() + (orphan,)),
                            media())


def build_dangling_link():
    project = replace_clip(golden_base(), 'w1.male', start=TimeExpr.after('w1.ghost'))
    return lambda: cue_plan(project, media())


def build_cycle():
    project = replace_clip(golden_base(), 'w1.male', start=TimeExpr.after('w1.chinese'))
    project = replace_clip(project, 'w1.chinese', start=TimeExpr.after('w1.male'))
    return lambda: cue_plan(project, media())


def build_unknown_duration():
    partial = {'w1:female': MediaInfo('w1:female', units(FEMALE_SOURCE_S),
                                      UNIT_NUM, UNIT_DEN)}
    return lambda: cue_plan(golden_base(), partial)


def build_source_range():
    short = dict(media())
    short['w1:chinese'] = MediaInfo('w1:chinese', units(CHINESE_SOURCE_S) - 1,
                                    UNIT_NUM, UNIT_DEN)
    return lambda: cue_plan(golden_base(), short)


def build_unsupported_rate():
    return lambda: golden_base(fps_num=7, fps_den=1)


def build_missing_role():
    clips = tuple(clip for clip in golden_clips() if clip.id != 'w1.male')
    return lambda: cue_plan(golden_base(clips=clips), media())


def build_ambiguous_role():
    twin = Clip(id='w1.male2', role='male', record_id='w1', start=TimeExpr.at(MALE[0]),
                duration_ticks=MALE[1] - MALE[0], text=WORD,
                source=_source('w1', 'male', MALE_SOURCE_S))
    return lambda: cue_plan(with_clips(golden_base(), golden_clips() + (twin,)), media())


def build_teaching_order():
    # Chinese put where the female reading is: the order check must name the
    # record instead of letting the overlap speak first.
    project = replace_clip(golden_base(), 'w1.chinese', start=TimeExpr.at(FEMALE[0]))
    return lambda: cue_plan(project, media())


def build_teaching_overlap():
    """Smallest counterexample: the Chinese stage starts one tick early."""
    project = replace_clip(golden_base(), 'w1.chinese',
                           start=TimeExpr.at(CHINESE[0] - 1))
    return lambda: cue_plan(project, media())


def build_stale_revision():
    project = golden_base()
    newer = apply(project, TrimClip('w1.meaning', start_ticks=CHINESE[0] + 12000)).project
    return lambda: apply(newer, TrimClip('w1.female', start_ticks=MALE[1]),
                         expected_revision=project.revision)


def build_revision_regression():
    """Undo restores the old *content* under a new revision, never an old number."""
    session = Session(project=golden_base())
    session = session.apply(TrimClip('w1.meaning', start_ticks=CHINESE[0] + 12000))
    return session


def build_clip_bound():
    project = replace_clip(golden_base(), 'w1.male', start=TimeExpr.after('w1.female'))
    return lambda: apply(project, TrimClip('w1.male', end_ticks=MALE[1]))


def build_invalid_time():
    document = golden_base().to_dict()
    document['clips'][1]['duration_ticks'] = 0
    return lambda: Project.from_dict(document)


def build_unsupported_schema():
    return lambda: golden_base(schema='wv-project@99')


def build_unknown_required_field():
    document = golden_base().to_dict()
    document.pop('speed')
    return lambda: Project.from_dict(document)


# --- the inventory: one row per obligation --------------------------------
# (case, group, code, object path or None, builder, what, why)
COUNTEREXAMPLES = [
    ('duplicate-record-id', 'identity', 'DUPLICATE_ID', 'record:w1',
     build_duplicate_record_id,
     '两个词条共用同一个 record id',
     '按 id 定位/删除会改错词条，两个词的时间线互相覆盖'),
    ('duplicate-clip-id', 'identity', 'DUPLICATE_ID', 'clip:w1.female',
     build_duplicate_clip_id,
     '两个片段共用同一个 clip id',
     '单片段移动会连带改到另一个片段，用户看到"没选它却动了"'),
    ('duplicate-record-index', 'identity', 'DUPLICATE_ID', 'record:w2',
     build_duplicate_record_index,
     '两个词条共用同一个 index（范围切片会串）',
     '范围导出按 index 切片，重复序号会漏词或串到别的范围'),
    ('dangling-record-reference', 'identity', 'DANGLING_REF', 'clip:w2.female',
     build_dangling_record_id,
     '片段挂在已删除的 record 上',
     '草稿里出现没有归属的语音，时间线顺序无法判断'),
    ('dangling-clip-reference', 'identity', 'DANGLING_REF', 'clip:w1.male',
     build_dangling_link,
     '片段声明跟随一个不存在的片段',
     '求解时无法确定位置；静默回退会让整段内容错位'),
    ('time-dependency-cycle', 'identity', 'CYCLE', None,
     build_cycle,
     '两个片段互相跟随（最小 2 片段环）',
     '求解不终止或取值随机，预览/导出结果不可复现'),
    ('unknown-media-duration', 'timeline', 'UNKNOWN_DURATION', 'clip:w1.male',
     build_unknown_duration,
     '语音素材未探测，时间线却要按它的时长排',
     '验收会拿一个编造的时长自证（M0 之前正是这样放过坏归档的）'),
    ('source-window-outside-media', 'timeline', 'SOURCE_RANGE', 'clip:w1.chinese',
     build_source_range,
     '片段要的源区间比真实素材长 1 个单位',
     '导出会把不存在的样本当静音，末尾读出半个字'),
    ('unsupported-frame-rate', 'timeline', 'UNSUPPORTED_RATE', 'project',
     build_unsupported_rate,
     '帧率 7 fps 无法在 720000 tick/s 上精确表示',
     '四舍五入的帧率会让 MP4/SRT/草稿三产物时间轴对不上'),
    ('teaching-missing-role', 'teaching', 'MISSING_ROLE', 'record:w1',
     build_missing_role,
     '缺男声英文（缺角色）',
     '学生会看到中文却没听到第二次英文朗读，教学步骤缺失'),
    ('teaching-ambiguous-role', 'teaching', 'ROLE_AMBIGUOUS', 'clip:w1.male2',
     build_ambiguous_role,
     '同一个词的 male 角色有两个片段',
     '交付时不知道用哪一条，导出结果取决于遍历顺序'),
    ('teaching-order', 'teaching', 'TEACHING_ORDER', 'record:w1',
     build_teaching_order,
     '中文阶段被放到女声的位置（阶段顺序错乱）',
     '教学顺序是"英文两遍→音标→中文"，顺序错了等于换了教法'),
    ('teaching-overlap', 'teaching', 'TEACHING_OVERLAP', 'clip:w1.male',
     build_teaching_overlap,
     '中文阶段比男声结束早 1 tick 开始（歧义语音重叠）',
     '两条朗读同时响，听不出念的是哪个词（用户批准的决定 2 必须拦截）'),
    ('stale-revision-write', 'revision', 'STALE_REVISION', 'project',
     build_stale_revision,
     '基于旧 revision 提交编辑，覆盖更新的改动',
     '两个窗口/两次点击会静默丢掉一方的工作，用户以为已保存'),
    ('clip-bound-trim', 'revision', 'CLIP_BOUND', 'clip:w1.male',
     build_clip_bound,
     '对"跟随别人"的片段直接 Trim（它没有绝对起点）',
     '没有绝对起点的片段被硬当成 0，剪辑会跳到片头'),
    ('invalid-time-zero-duration', 'timeline', 'INVALID_TIME', 'clip:w1.male',
     build_invalid_time,
     '片段时长为 0（载入磁盘上的工程文档）',
     '零长度片段不显示也不发声，交付看起来却"完整"'),
    ('unsupported-schema-version', 'revision', 'SCHEMA', 'project',
     build_unsupported_schema,
     '工程声明 wv-project@99',
     '按当前版本猜字段会静默丢数据，用户在新机器上打开旧工程会缺内容'),
    ('unknown-required-field', 'revision', 'SCHEMA', 'project',
     build_unknown_required_field,
     '工程文档缺少契约要求的字段（speed）',
     '"尽量载入"不完整的工程会让验收器拿不完整数据判 PASS'),
]

# --- invariants: obligations that are not an error code --------------------
# (case, group, build, what, why).  ``build`` returns the state under test; the
# test reads it, performs the documented operation and compares.
INVARIANTS = [
    ('revision-regression', 'revision', build_revision_regression,
     '撤销把内容退回旧状态时，revision 必须继续前进（不能退版本号）',
     '版本号倒退会让"过期 revision"判断失效：持有旧号的客户端会以为自己的编辑仍然有效'),
]


def invariant_ids():
    return [row[0] for row in INVARIANTS]


def invariant(case_id):
    for row in INVARIANTS:
        if row[0] == case_id:
            return row
    raise KeyError(case_id)

# Errors that must carry an object path, not just a code.
PATH_OPTIONAL = ('CYCLE',)


def case_ids():
    return [row[0] for row in COUNTEREXAMPLES]


def case(case_id):
    for row in COUNTEREXAMPLES:
        if row[0] == case_id:
            return row
    raise KeyError(case_id)


# --- the tests -------------------------------------------------------------
def test_the_hand_built_lesson_is_the_one_the_rules_describe():
    """Guard the fixture itself: if this drifts, every counterexample is moot."""
    assert HANZI_COUNT == 4
    assert CHINESE_FLOOR_S == 1.6
    project = golden_base()
    solution = solve(project, media())
    assert project.intro_ticks == INTRO_TICKS
    assert [item.start_ticks for item in solution.render.audio] == \
        [FEMALE[0], MALE[0], CHINESE[0]]
    assert solution.render.total_ticks == WORD_END
    assert solution.render.conflicts == ()
    assert Fraction(WORD_END, 720000) == Fraction(11, 2)      # 5.5 s
    assert project.clip('w1.female').duration_ticks == FEMALE[1] - FEMALE[0]
    assert project.clip('w1.chinese').duration_ticks == CHINESE[1] - CHINESE[0]


def test_the_golden_lesson_comes_from_the_read_only_word_list():
    """The word list is the only source of the text; record 151 is 'demonstrate'."""
    import hashlib

    from acceptance.accept_range import load_expectations
    from acceptance.fixtures import require_wordlist

    wordlist = require_wordlist()
    digest = hashlib.sha256(wordlist.read_bytes()).hexdigest()
    assert digest == 'b42f381ab2a3757d9b31c11a22444bebf20ca3f26982aaba7e6283b97916e99a'
    entry = load_expectations(wordlist, 151, 151, 'legacy')['words'][0]
    assert (entry['word'], entry['phonetic'], entry['meaning'],
            entry['spoken_meaning']) == (WORD, '/' + PHONETIC + '/', MEANING, SPOKEN)
    assert sum(1 for char in entry['spoken_meaning']
               if '\u4e00' <= char <= '\u9fff') == HANZI_COUNT


def test_every_counterexample_is_complete():
    codes = {error.code for error in ProjectError.__subclasses__()} | {'SCHEMA'}
    for row in COUNTEREXAMPLES:
        case_id, group, code, path, builder, what, why = row
        assert group in ('identity', 'timeline', 'teaching', 'revision'), case_id
        assert code in codes, '%s 的 code %s 不在 ProjectError 的稳定码里' % (case_id, code)
        assert callable(builder) and what and why, case_id
        assert code in PATH_OPTIONAL or path, '%s 必须写明 object_path' % case_id
    ids = case_ids()
    assert len(ids) == len(set(ids)), '用例 id 必须唯一'
    assert len(ids) >= 18, '反例太少，覆盖不住已批准的交付规则'
    for row in INVARIANTS:
        assert len(row) == 5 and row[1] in ('identity', 'timeline', 'teaching',
                                            'revision'), row[0]


@pytest.mark.parametrize('case_id', case_ids())
def test_counterexample_is_refused_with_the_documented_code(case_id):
    """Build the malformed project, run it, and demand the documented refusal."""
    _, _, code, path, builder, what, why = case(case_id)
    make = builder()
    with pytest.raises(ProjectError) as raised:
        make()
    error = raised.value
    assert error.code == code, (
        '%s（%s）：期望 %s，实际 %s —— %s' % (case_id, what, code, error.code, error))
    if path:
        assert error.path == path, (
            '%s（%s）：期望 object_path %s，实际 %r' % (case_id, what, path, error.path))
    # A usable next step, not just a code: the CLI/UI surface these.
    assert error.hint or error.message, case_id


def test_no_counterexample_is_refused_by_an_unrelated_rule():
    """The refusal must come from the rule the case is about.

    Two cases could otherwise pass for the wrong reason (every malformed project
    failing some early generic check).  This pins the layer: the documented code
    is the *first* thing the surface says.  ``test_counterexample_...`` above
    already asserts the code per case; this one proves no case is silently
    un-refused.
    """
    seen = {}
    for case_id in case_ids():
        _, _, code, _, builder, _, _ = case(case_id)
        try:
            builder()()
        except ProjectError as error:
            seen[case_id] = error.code
    assert set(seen) == set(case_ids()), '有反例没有触发任何拒绝：%s' % (
        sorted(set(case_ids()) - set(seen)))
    for case_id, code in seen.items():
        assert code == case(case_id)[2], '%s 被别的规则先拒了：%s' % (case_id, code)


@pytest.mark.parametrize('case_id', invariant_ids())
def test_invariant_holds(case_id):
    """Obligations that are not an error code, asserted on the real state."""
    _, _, build, what, why = invariant(case_id)
    session = build()
    if case_id == 'revision-regression':
        before = session.revision
        restored = session.history[-1]
        undone = session.undo()
        assert undone.revision == before + 1, '%s：撤销后版本号没有前进' % what
        assert undone.project.revision == before + 1, what
        assert undone.project.clips == restored.clips, (
            '撤销没有恢复旧内容：%s' % why)
        # A client holding the pre-undo revision is now stale, exactly as intended.
        with pytest.raises(StaleRevisionError):
            undone.apply(TrimClip('w1.female', start_ticks=MALE[1]),
                         expected_revision=before)
    else:  # pragma: no cover - guards against a mis-typed inventory
        raise AssertionError('未接线的不变量：%s' % case_id)


def test_render_plan_accepts_what_cue_plan_refuses_for_overlap_only():
    """Overlap is legal for a mix and illegal for teaching: the two layers differ."""
    project = replace_clip(golden_base(), 'w1.chinese', start=TimeExpr.at(CHINESE[0] - 1))
    plan = render_plan(project, media())
    assert [conflict.code for conflict in plan.conflicts] == ['OVERLAP_ACCEPTED']
    with pytest.raises(TeachingOverlapError):
        cue_plan(project, media())


def test_saved_document_round_trips_and_keeps_the_revision(tmp_path):
    """Storage is part of the surface: a refused load must not repair silently."""
    project = golden_base(revision=7)
    path = save_project(project, tmp_path)
    assert load_project(path) == project
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    assert document['schema'] == SCHEMA
    assert document['revision'] == 7
