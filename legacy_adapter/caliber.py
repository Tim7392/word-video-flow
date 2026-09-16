"""The two timing calibers, declared once, in the words the user reads.

The member's real risk is not a wrong number; it is delivering a batch whose
subtitles were timed by one rule while the video was cut by another.  There are
exactly two rules in this project and they are not approximations of each other:

``legacy-text-rule``
    旧字幕工厂（``subtitle_factory_core._gen``）never looks at media.  It gives each
    word a fixed 2 s English stage and a Chinese stage whose length is
    ``min(hanzi, 6) * first_six + max(0, hanzi - 6) * extra``, floored at 0.5 s.
``media-actual``
    本软件的导出链（计划 / ``timeline.json``）advances every stage by the audio it
    actually has, and converts frames to milliseconds once
    (``word_video/domain/timebase.py``).

So the adapter labels which rule produced a set of files, and reads the *other*
rule's real numbers from the artefacts that already exist rather than recomputing
them: the new caliber comes from a delivered ``timeline.json`` (or the editor's
current plan) through the engine's own time core.

Reading the new caliber therefore **imports the new software, lazily, and may fail**.
That failure is contained on purpose: a broken video module must not stop the old
entry, so an unusable import degrades this block to ``available: False`` with the
reason - the caliber is a label, never a dependency of generating subtitles.
"""
import json
import os
from pathlib import Path

CALIBER_LEGACY = 'legacy-text-rule'
CALIBER_MEDIA = 'media-actual'

LEGACY_LABEL = '旧口径：按文字规则推时长'
MEDIA_LABEL = '新口径：按音频/视频实际时长'

#: The old rule, transcribed from the protected core (not re-implemented here).
LEGACY_FORMULA = ('英文阶段固定 2.000s；中文阶段 = min(汉字数, 6) * first_six '
                  '+ max(0, 汉字数 - 6) * extra，下限 0.500s')
#: The new rule, in one sentence: the audio decides.
MEDIA_FORMULA = '每段按该段音频的实际时长推进；帧/采样只在边界换算一次（计划与 timeline.json）'

LEGACY_SOURCE = 'subtitle_factory_core._gen（受保护旧核心，本适配器不导入、只子进程调用）'
MEDIA_SOURCE = 'word_video/domain/plan.py + word_video/exporters/srt.py（B 的导出链）'

#: The sentence both the dialog and the CLI print.  One string, two readers: a
#: member reading the window and a support call reading the CLI JSON must not be
#: told two different things about which clock they are on.
UI_NOTICE = (
    '当前入口＝旧字幕工厂：时间按“文字规则”推算（英文阶段固定 2 秒 + 中文阶段按汉字数），'
    '与音频/视频的真实时长无关。\n'
    '本软件“导出三产物”用的是另一种口径：按“音频实际时长”（计划 / timeline.json）。'
    '两套时间不要混在同一批对外交付里。')

CALIBER_CHOICES = (CALIBER_LEGACY, CALIBER_MEDIA)


def caliber(name):
    """One caliber as data: which clock, whose implementation, which rule."""
    if name == CALIBER_LEGACY:
        return {'id': CALIBER_LEGACY, 'label': LEGACY_LABEL, 'entry': '旧字幕工厂（subtitle_factory_cli.py）',
                'source': LEGACY_SOURCE, 'formula': LEGACY_FORMULA,
                'measured_from': '本次结果的 packages[].duration_ms（旧核心实测末条时间）',
                'media_independent': True}
    if name == CALIBER_MEDIA:
        return {'id': CALIBER_MEDIA, 'label': MEDIA_LABEL,
                'entry': '本软件“导出三产物”', 'source': MEDIA_SOURCE,
                'formula': MEDIA_FORMULA,
                'measured_from': 'timeline.json 的 words[].end_frame / 编辑器当前计划的 total_ticks',
                'media_independent': False}
    raise KeyError(name)


def calibers():
    """Both calibers, in the order a user should read them (old, then new)."""
    return [caliber(name) for name in CALIBER_CHOICES]


def timeline_file(path):
    """A ``timeline.json``, or the run/project folder that holds one."""
    source = Path(path)
    if source.is_dir():
        source = source / 'timeline.json'
    return source


def _frame_rate(data):
    """``(num, den)`` of a manifest, tolerating an integer ``fps`` or a rational."""
    if 'fps_num' in data:
        return int(data['fps_num']), int(data.get('fps_den') or 1)
    fps = data.get('fps')
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        raise ValueError('timeline.json 没有可用的 fps（拿到 %r）' % (fps,))
    if isinstance(fps, int):
        return fps, 1
    return int(round(fps * 1000)), 1000


def timeline_caliber(path):
    """The media caliber's **actual** numbers for a delivered (or planned) run.

    Returns a block that is always shaped the same way; ``available`` says whether
    the numbers could be read, and ``reason`` says why not when they could not.  The
    engine's own time core does the frames-to-milliseconds conversion, so this is a
    reading of the new caliber, not a second implementation of it.
    """
    block = dict(caliber(CALIBER_MEDIA))
    source = timeline_file(path)
    block.update({'available': False, 'reason': '', 'timeline': str(source), 'measured': None})
    if not str(path or '').strip():
        block['reason'] = '没有指定 timeline.json（或工程/运行目录）'
        return block
    if not source.is_file():
        block['reason'] = '找不到 timeline.json：%s' % source
        return block
    try:
        data = json.loads(source.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        block['reason'] = 'timeline.json 读不出来：%s' % error
        return block
    try:
        from word_video.domain.timebase import TimeExpr, ticks_to_milliseconds
    except Exception as error:                      # noqa: BLE001 - 新软件坏了也要能报口径
        block['reason'] = ('新软件的计时核心 word_video.domain.timebase 不可用（%s: %s），'
                           '本次只报旧口径' % (type(error).__name__, error))
        return block
    try:
        words = list(data.get('words') or ())
        if not words:
            raise ValueError('timeline.json 里没有 words')
        fps_num, fps_den = _frame_rate(data)
        ends = [ticks_to_milliseconds(TimeExpr.frames(int(word['end_frame']),
                                                       fps_num, fps_den).ticks)
                for word in words]
        total_frames = data.get('total_frames')
        total_ms = ends[-1]
        if isinstance(total_frames, int) and total_frames >= 0:
            total_ms = max(total_ms, ticks_to_milliseconds(
                TimeExpr.frames(int(total_frames), fps_num, fps_den).ticks))
    except (KeyError, TypeError, ValueError) as error:
        block['reason'] = 'timeline.json 的字段不是本软件的时间线：%s' % error
        return block
    block.update({
        'available': True,
        'measured': {'fps': fps_num if fps_den == 1 else '%d/%d' % (fps_num, fps_den),
                     'word_count': len(words), 'total_frames': total_frames,
                     'first_index': data.get('first_index'), 'last_index': data.get('last_index'),
                     'first_word': words[0].get('word'), 'last_word': words[-1].get('word'),
                     'first_end_ms': ends[0], 'last_end_ms': ends[-1],
                     'total_duration_ms': total_ms,
                     'spoken_policy': data.get('spoken_policy') or '',
                     'intro_frames': data.get('intro_frames')}})
    return block


def default_timeline_path():
    """Where the new caliber usually is when nobody said: the work root's runs."""
    root = os.environ.get('WORD_VIDEO_WORK_ROOT') or r'D:\1\1-AI_workflow\word_video_flow'
    return str(Path(root) / 'out')


def missing_plan_block(reason):
    """The media block for "the editor has no plan yet", shaped like the real one."""
    block = dict(caliber(CALIBER_MEDIA))
    block.update({'available': False, 'reason': reason, 'timeline': '', 'measured': None})
    return block
