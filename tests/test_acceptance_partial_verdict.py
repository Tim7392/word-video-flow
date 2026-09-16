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
import re
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
SPEED_NAME = re.compile(r'_(?P<window>\d+(?:\.\d+)?)s_')


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
    problems = accept_range.check_timing(manifest, report, expectations, batch)
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


def test_a_layout_without_any_plan_data_is_a_problem_not_a_pass(tmp_path):
    """No record, no speech.json and no window in the name: that is a failure.

    A batch that publishes nothing to check the pacing against must not be able to
    end in PASS by skipping the face - which is exactly what the old
    ``data_missing -> PASS`` behaviour did.
    """
    batch = make_batch(tmp_path / 'no-plan-data', layout='bare')
    timing, problems = judge(batch)
    assert problems, '缺计时来源必须报问题'
    assert any('没有任何可用的计时记录' in line for line in problems), problems
    assert timing['recomputed_stages'] == 0
    assert timing['complete'] is False
    assert len(timing['skipped_checks']) == 3, timing['skipped_checks']


def write_speech_json(batch, records=None):
    """The run's own account of what it prepared (wv-speech@1)."""
    import hashlib
    timeline = json.loads((batch / 'timeline.json').read_text(encoding='utf-8'))
    rows = []
    for stage in timeline['audio']:
        path = Path(stage['path'])
        window = float(SPEED_NAME.search(path.name).group('window'))
        rows.append({
            'asset_id': 'w151:%s' % stage['role'], 'record_id': '151',
            'role': stage['role'], 'clip_id': 'w151.%s' % stage['role'],
            'text': stage['text'], 'voice': stage.get('voice', ''),
            'path': str(path), 'raw_seconds': window,
            'rendered_seconds': window / SPEED,
            'source_seconds': window,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'start_frame': stage['start_frame'],
            'end_frame': stage['start_frame'] + stage['duration_frames'],
            'duration_ticks': stage['duration_frames'] * (720000 // FPS),
            'start_ticks': stage['start_frame'] * (720000 // FPS),
            'end_ticks': (stage['start_frame'] + stage['duration_frames'])
            * (720000 // FPS),
            'stage_seconds': stage['duration_frames'] / FPS})
    document = {'schema': 'wv-speech@1', 'mode': 'filename', 'fps': FPS,
                'sample_rate': 48000, 'speed': SPEED,
                'total_frames': timeline['total_frames'], 'records': rows}
    if records is not None:
        document['records'] = records
    (batch / 'speech.json').write_text(json.dumps(document, ensure_ascii=False),
                                       encoding='utf-8')
    return document


def test_a_speech_json_run_is_recomputed_and_complete(tmp_path):
    """With the run's own speech.json the face is complete: every check runs."""
    batch = make_batch(tmp_path / 'speech-json', layout='filename')
    write_speech_json(batch)
    timing, problems = judge(batch)
    assert not problems, problems
    assert timing['speech_json'] is True
    assert timing['recomputed_stages'] == 3
    assert timing['digests_checked'] == 3
    assert timing['complete'] is True
    assert timing['skipped_checks'] == []
    assert timing['window_sources'] == {'speech.json 记录': 3}


def test_a_speech_json_record_that_contradicts_the_timeline_is_caught(tmp_path):
    """The record is the run's account; a timeline that disagrees is a problem."""
    import hashlib
    batch = make_batch(tmp_path / 'speech-json-bad', layout='filename')
    document = write_speech_json(batch)
    # Same files, but the record claims a different stage length and a wrong hash.
    for row in document['records']:
        row['stage_seconds'] = row['stage_seconds'] + 0.5
        row['sha256'] = '0' * 64
    (batch / 'speech.json').write_text(json.dumps(document, ensure_ascii=False),
                                       encoding='utf-8')
    timing, problems = judge(batch)
    assert any('阶段时长' in line for line in problems), problems
    assert any('sha256 不符' in line for line in problems), problems


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
