"""QA-2: judge QA's own archive-comparison helpers on fixture data.

The comparison logic is what decides "reproduced or not", so it gets its own
negative evidence instead of being trusted because it printed zeros once.  Every
fixture is built in the test, so this file needs no archive and stays in the
default suite:

  * an identical pair must be reported equal;
  * a one-tick timing change, a one-character caption change, a missing track, a
    wrong file hash and a missing published file must each be caught, with the
    difference named.
"""
import json
from pathlib import Path
import shutil
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from qa2 import compare_to_archive as compare  # noqa: E402


def write_batch(root, word='demonstrate', frame_shift=0, srt_text=None,
                drop_track=None, corrupt_hash=False, drop_file=False):
    """A minimal but complete batch: 5 SRTs, MP4, mix, draft, timeline, hashes.

    The fixture itself is checked below, so the negative cases cannot pass by
    producing an incomplete batch.
    """
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    timeline = {
        'first_index': 151, 'last_index': 151, 'fps': 60, 'width': 1920, 'height': 1080,
        'speed': 1.25, 'video_codec': 'h265', 'total_frames': 330, 'intro_frames': 60,
        'background': 'D:/media/background.mp4', 'intro_video': '', 'intro_audio': '',
        'title': 't', 'footer': 'f', 'styles': {'english': {'size': 450}},
        'words': [{'index': 151, 'word': word, 'phonetic': '/ˈdemənstreɪt/',
                   'meaning': 'v. 证明；证实', 'spoken_meaning': '证明；证实',
                   'start_frame': 60 + frame_shift, 'male_frame': 120 + frame_shift,
                   'chinese_frame': 168 + frame_shift, 'end_frame': 330 + frame_shift}],
        'audio': [{'word_index': 151, 'role': role, 'text': word if role != 'chinese'
                   else '证明；证实', 'voice': 'v1',
                   'path': 'D:/media/%s.wav' % role, 'start_frame': 60,
                   'duration_frames': 60} for role in ('female', 'male', 'chinese')],
    }
    (root / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    (root / 'srt').mkdir(exist_ok=True)
    for suffix in compare.SRT_SUFFIXES:
        if suffix == drop_track:
            continue
        body = '1\n00:00:01,000 --> 00:00:02,000\n%s\n' % (srt_text or word)
        (root / 'srt' / ('0151-0151' + suffix)).write_text(body, encoding='utf-8')
    (root / 'video').mkdir(exist_ok=True)
    (root / 'video' / 'video.mp4').write_bytes(b'not a real mp4')
    (root / 'video' / 'mix.wav').write_bytes(b'RIFF')
    (root / 'editable-draft').mkdir(exist_ok=True)
    (root / 'editable-draft' / 'draft_content.json').write_text('{}', encoding='utf-8')
    files = [root / 'timeline.json', root / 'video' / 'mix.wav',
             root / 'editable-draft' / 'draft_content.json']
    files += sorted((root / 'srt').glob('*.srt'))
    if not drop_file:
        files.append(root / 'video' / 'video.mp4')
    complete = {'files': [{'path': str(p),
                           'sha256': '0' * 64 if corrupt_hash else compare.sha256(p)}
                          for p in files],
                'batch': {}}
    (root / 'complete.json').write_text(json.dumps(complete), encoding='utf-8')
    return root


def append_phantom_entry(root, name='editable-draft/Resources/missing.wav'):
    """Record a file in complete.json that does not exist on disk."""
    complete_path = root / 'complete.json'
    complete = json.loads(complete_path.read_text(encoding='utf-8'))
    complete['files'].append({'path': str(root / name), 'sha256': '1' * 64})
    complete_path.write_text(json.dumps(complete), encoding='utf-8')


def test_the_fixture_itself_is_a_complete_batch(pair):
    """Positive control: the negative cases below are only meaningful if this is
    a batch that could pass."""
    mine, archive = pair
    report = compare.check_complete(mine)
    assert report['ok'], report
    assert report['srt_count'] == 5 and report['listed'] == 9
    assert compare.compare_timeline(mine, archive)['timeline_equal']
    assert compare.compare_srt(mine, archive)['equal']


@pytest.fixture
def pair(tmp_path):
    archive = write_batch(tmp_path / 'archive')
    mine = write_batch(tmp_path / 'mine')
    return mine, archive


def test_identical_batches_are_reported_equal(pair):
    mine, archive = pair
    assert compare.compare_timeline(mine, archive)['timeline_equal']
    assert compare.compare_srt(mine, archive)['equal']
    assert compare.check_complete(mine)['ok']


def test_a_shifted_word_is_caught_and_named(pair):
    mine, archive = pair
    write_batch(mine, frame_shift=1)
    report = compare.compare_timeline(mine, archive)
    assert not report['timeline_equal']
    assert any('start_frame' in line for line in report['field_differences'])
    assert any('word 151' in line for line in report['field_differences'])


def test_a_changed_caption_is_caught(pair):
    mine, archive = pair
    write_batch(mine, srt_text='deputys')
    report = compare.compare_srt(mine, archive)
    assert not report['equal']
    assert any('DIFFERENT' in value for value in report['files'].values())


def test_a_missing_track_is_caught(pair):
    mine, archive = pair
    write_batch(mine, drop_track='_03_音标.srt')
    report = compare.compare_srt(mine, archive)
    assert not report['equal']
    assert report['missing'] == ['0151-0151_03_音标.srt']


def test_a_bad_hash_and_a_missing_file_are_caught(pair):
    mine, archive = pair
    write_batch(mine, corrupt_hash=True)
    report = compare.check_complete(mine)
    assert not report['ok'] and report['hash_mismatch']
    write_batch(mine)
    append_phantom_entry(mine)
    report = compare.check_complete(mine)
    assert not report['ok'] and report['missing']
    write_batch(mine, drop_file=True)
    report = compare.check_complete(mine)
    assert not report['ok'] and not report['has_mp4']


def test_a_changed_setting_is_caught(pair):
    mine, archive = pair
    timeline = json.loads((mine / 'timeline.json').read_text(encoding='utf-8'))
    timeline['fps'] = 30
    (mine / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    report = compare.compare_timeline(mine, archive)
    assert not report['timeline_equal']
    assert any('fps' in line for line in report['field_differences'])


def test_media_paths_are_reported_but_do_not_fail_the_timeline(pair):
    mine, archive = pair
    timeline = json.loads((mine / 'timeline.json').read_text(encoding='utf-8'))
    timeline['background'] = 'E:/elsewhere/background.mp4'
    for stage in timeline['audio']:
        stage['path'] = stage['path'].replace('D:/media', 'E:/cache')
    (mine / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    report = compare.compare_timeline(mine, archive)
    # Paths differ by design, so the time axis is still equal - but nothing hides.
    assert report['timeline_equal']
    assert len(report['path_differences']) >= 2


def test_sample_words_are_spread_and_ordered(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()
    timeline = {'words': [{'index': i} for i in range(151, 201)]}
    (archive / 'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
    picks = compare.sample_words(archive, 10)
    assert picks == sorted(picks)
    assert picks[0] == 151 and picks[-1] == 200
    assert len(picks) == 10
