"""QA-3 self-test: the policy check must notice a change it did not expect.

The interesting failure is the quiet one - a policy change that also moves a
border, a caption or a setting - so the checker's diff is tested against
fabricated batch pairs: the intended difference (Chinese reading text only) must
be accepted, and a change to any other field, track or setting must be reported.
"""
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from qa3 import policy_check as check  # noqa: E402


def write_batch(root, spoken='同意；赞成', meaning='n. & v. 同意；赞成',
                chinese_frame=100, phonetic='ˈpiːs', fps=60, track05=None,
                chinese_duration=50):
    """A one-word batch; the 05 track normally carries the spoken reading, while
    the other four tracks carry text that the policy must not touch."""
    root.mkdir(parents=True, exist_ok=True)
    word = {'index': 151, 'word': 'peace', 'phonetic': phonetic, 'meaning': meaning,
            'spoken_meaning': spoken, 'start_frame': 0, 'male_frame': 50,
            'chinese_frame': chinese_frame, 'end_frame': chinese_frame + chinese_duration}
    timeline = {'first_index': 151, 'last_index': 151, 'fps': fps, 'width': 1920,
                'height': 1080, 'speed': 1.25, 'video_codec': 'h265',
                'total_frames': 200, 'intro_frames': 0,
                'words': [word],
                'audio': [{'word_index': 151, 'role': role,
                           # Only the Chinese stage reads the meaning; the two
                           # English stages read the word itself.
                           'text': spoken if role == 'chinese' else 'peace',
                           'voice': 'v', 'start_frame': 0,
                           'duration_frames': chinese_duration if role == 'chinese' else 50}
                          for role in ('female', 'male', 'chinese')]}
    (root / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    (root / 'srt').mkdir(exist_ok=True)
    fixed = {'_01_英文重复.srt': 'peace', '_02_英文单次.srt': 'peace',
             '_03_音标.srt': phonetic, '_04_中文带词性.srt': meaning,
             '_05_中文无词性.srt': track05 if track05 is not None else spoken}
    for suffix, text in fixed.items():
        (root / 'srt' / ('0151-0151' + suffix)).write_text(
            '1\n00:00:01,000 --> 00:00:02,000\n%s\n' % text, encoding='utf-8')
    return root


def test_only_the_reading_text_may_change(tmp_path):
    v2 = write_batch(tmp_path / 'v2', spoken='同意；赞成')
    legacy = write_batch(tmp_path / 'legacy', spoken='&  同意；赞成', track05='&  同意；赞成')
    report = check.diff_words(v2, legacy)
    assert report['word_fields'] == {'spoken_meaning': {'count': 1, 'indexes': [151]}}
    assert report['setting_fields'] == []
    assert report['audio_fields'] == {'text': {'count': 1, 'keys': [[151, 'chinese']]}}
    assert report['srt_tracks']['0151-0151_05_中文无词性.srt'] == 'differs'
    others = [name for name, state in report['srt_tracks'].items()
              if name != '0151-0151_05_中文无词性.srt']
    assert all(report['srt_tracks'][name] == 'identical' for name in others)


@pytest.mark.parametrize('change', [
    {'meaning': 'n. 和平'},                 # the display meaning moved
    {'phonetic': 'ˈpiːsɪz'},                # the phonetic moved
    {'chinese_frame': 101},                 # a stage boundary moved
    {'chinese_duration': 51},               # a stage length moved
    {'fps': 30},                            # a timing setting moved
])
def test_any_other_change_is_reported(tmp_path, change):
    v2 = write_batch(tmp_path / 'v2', spoken='同意；赞成')
    legacy = write_batch(tmp_path / 'legacy', spoken='同意；赞成', **change)
    report = check.diff_words(v2, legacy)
    if change.get('fps'):
        assert report['setting_fields'] == ['fps'], report
    elif change.get('chinese_frame'):
        assert 'chinese_frame' in report['word_fields'], report
    elif change.get('meaning'):
        assert 'meaning' in report['word_fields'], report
    elif change.get('phonetic'):
        assert 'phonetic' in report['word_fields'], report
    else:
        assert report['audio_fields'], report


def test_a_batch_pair_that_is_identical_reports_no_difference(tmp_path):
    v2 = write_batch(tmp_path / 'v2', spoken='同意；赞成')
    legacy = write_batch(tmp_path / 'legacy', spoken='同意；赞成')
    report = check.diff_words(v2, legacy)
    assert report['word_fields'] == {}
    assert report['audio_fields'] == {}
    assert report['setting_fields'] == []
    assert set(report['srt_tracks'].values()) == {'identical'}
