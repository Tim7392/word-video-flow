"""A face that could not run every check must not report PASS.

C found this in the wild: the new exporter prepares speech under ``run/.speech``
with no per-asset record, the acceptance tool looked only for the verified chain's
``audio-cache/<token>/complete.json``, and the timing face answered

    verdict=PASS, failures={}, every face problem_count=0, timing.data_missing=9

- a PASS for a face that had not run.  These tests pin the rule that replaced it:
the stage frame count is recomputed from an expectation that comes from outside
the artefact (the published source window or the per-asset record, the floor from
the read-only word list, and the file's real duration), and whatever cannot be
recomputed is named in ``skipped_checks`` and turns the verdict into PARTIAL.

The batch here is a file-level fixture (a real WAV, a real timeline); no render is
needed, so this stays in the default suite.
"""
import json
import math
from pathlib import Path
import struct
import sys
import wave

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from acceptance import accept_range  # noqa: E402
from acceptance.fixtures import require_wordlist  # noqa: E402

FPS = 60
SPEED = 1.25
GAP = 0.1
WINDOW = 1.128            # the published source window for word 151 female
FLOOR = 1.0               # English floor from the read-only word list
HANZI_MEANING = '证明；证实'   # 4 Hanzi -> 1.6 s floor by the approved rule


def write_wav(path, seconds, rate=48000):
    """A real mono 16-bit WAV of the given duration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(round(seconds * rate))
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<%dh' % frames, *([1000] * frames)))
    return path


def expected_frames(floor, window, media):
    return max(math.ceil(max(floor, window + GAP) / SPEED * FPS),
               math.ceil(media * FPS), 1)


def make_batch(root, layout='filename', declared_frames=None, media_seconds=None):
    """One word (151) with its three stages; layout chooses how the plan is published.

    ``filename``  the new exporter: speech under ``.speech``, the source window and
                  the tempo in the file name, no per-asset record.
    ``cache``     the verified chain: a per-asset ``complete.json`` with the raw
                  (unscaled) seconds next to the prepared file.
    """
    word = {'index': 151, 'word': 'demonstrate', 'phonetic': '/ˈdemənstreɪt/',
            'meaning': 'v. 证明；证实', 'spoken_meaning': HANZI_MEANING,
            'start_frame': 0, 'male_frame': 100, 'chinese_frame': 200,
            'end_frame': 400}
    audios = []
    plan = {'female': (WINDOW, FLOOR, 100), 'male': (WINDOW, FLOOR, 100),
            'chinese': (1.536, 1.6, 200)}
    for role, (window, floor, start) in plan.items():
        # The frames the timeline declares come from the plan (window and floor);
        # ``media_seconds`` overrides only what the file on disk really contains, so
        # the two can disagree exactly the way a wrong render would.
        planned_media = window / SPEED
        media = media_seconds if media_seconds is not None else planned_media
        frames = declared_frames if declared_frames is not None else \
            expected_frames(floor, window, planned_media)
        if layout == 'filename':
            path = write_wav(root / '.speech' / ('w151_%s_%.4fs_%.4fx.wav'
                                                 % (role, window, SPEED)), media)
        elif layout == 'cache':
            folder = root / 'audio-cache' / ('%s-token' % role)
            path = write_wav(folder / 'prepared.wav', media)
            (folder / 'complete.json').write_text(json.dumps({
                'spec': {'role': role, 'text': 'demonstrate' if role != 'chinese'
                         else HANZI_MEANING, 'speed': SPEED, 'fps': FPS},
                'raw': window, 'rendered': media}), encoding='utf-8')
        elif layout == 'bare':
            # No record and no window in the name: the plan is not published at all.
            path = write_wav(root / '.speech' / ('w151_%s.wav' % role), media)
        else:
            raise ValueError(layout)
        audios.append({'word_index': 151, 'role': role,
                       'text': 'demonstrate' if role != 'chinese' else HANZI_MEANING,
                       'voice': 'v1', 'path': str(path), 'start_frame': start,
                       'duration_frames': frames})
    timeline = {'first_index': 151, 'last_index': 151, 'fps': FPS, 'width': 1920,
                'height': 1080, 'speed': SPEED, 'video_codec': 'h265',
                'total_frames': 500, 'intro_frames': 0, 'background': '', 'styles': {},
                'title': 't', 'footer': 'f', 'words': [word], 'audio': audios}
    (root / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    return root


def judge(batch, cleaning='v2'):
    report = {'batch': str(batch)}
    manifest = json.loads((batch / 'timeline.json').read_text(encoding='utf-8'))
    expectations = accept_range.load_expectations(require_wordlist(), 151, 151,
                                                  cleaning)['words']
    problems = accept_range.check_timing(manifest, report, expectations)
    return report['timing'], problems


def test_the_new_layout_is_judged_and_its_gap_is_named(tmp_path):
    """The published window recomputes all three stages; the missing raw is named."""
    batch = make_batch(tmp_path / 'filename-layout', layout='filename')
    timing, problems = judge(batch)
    assert not problems, problems
    assert timing['recomputed_stages'] == 3
    assert timing['window_sources'] == {'文件名里的源窗口与速度': 3}
    assert timing['complete'] is False
    assert any('raw' in note or '未加工' in note for note in timing['skipped_checks']), \
        timing


def test_the_verified_layout_is_complete(tmp_path):
    """The old chain publishes raw seconds, so nothing is left unchecked."""
    batch = make_batch(tmp_path / 'cache-layout', layout='cache')
    timing, problems = judge(batch, cleaning='legacy')
    assert not problems, problems
    assert timing['recomputed_stages'] == 3
    assert timing['window_sources'] == {'per-asset 记录': 3}
    assert timing['complete'] is True
    assert timing['skipped_checks'] == []


def test_a_stage_that_does_not_follow_the_rule_is_a_problem(tmp_path):
    """Wrong frame count for the published window/floor/media is caught."""
    batch = make_batch(tmp_path / 'too-short', layout='filename', declared_frames=10)
    timing, problems = judge(batch)
    assert problems and any('the rule gives' in line for line in problems), problems
    # It is a problem, not an exemption: the face stays partial as well.
    assert timing['complete'] is False


def test_the_published_prepared_audio_must_fit_its_stage(tmp_path):
    """A prepared file longer than the frames reserved is caught by the rule."""
    batch = make_batch(tmp_path / 'long-media', layout='filename',
                       media_seconds=WINDOW / SPEED + 0.5)
    timing, problems = judge(batch)
    assert problems, 'a prepared file 0.5 s too long must be reported'
    assert any('the rule gives' in line for line in problems), problems


def test_a_layout_without_any_plan_data_is_partial_not_pass(tmp_path):
    """No record and no window in the name: the stage cannot be checked at all."""
    batch = make_batch(tmp_path / 'no-plan-data', layout='bare')
    timing, problems = judge(batch)
    assert not problems, problems
    assert timing['recomputed_stages'] == 0
    assert timing['complete'] is False
    assert len(timing['skipped_checks']) == 3, timing['skipped_checks']


def test_the_partial_verdict_reaches_the_report(tmp_path):
    """The face flags itself, and the runner turns that into PARTIAL + exit 1."""
    batch = make_batch(tmp_path / 'verdict')
    report = {'batch': str(batch), 'timing': {'complete': False,
                                             'skipped_checks': ['x']}}
    failures = {}
    partial = {name: face.get('skipped_checks') for name, face in report.items()
               if isinstance(face, dict) and face.get('complete') is False}
    verdict = 'FAIL' if failures else 'PARTIAL' if partial else 'PASS'
    assert verdict == 'PARTIAL' and partial == {'timing': ['x']}
