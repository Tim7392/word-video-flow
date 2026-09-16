"""QA-2 self-test for the request builder: the words must come from the word list.

The builder is the piece that decides what the reproduction asks for, so its
independence is checked here without the archive: given a timeline that disagrees
with the read-only word list it must refuse, and given one that agrees it must
produce a request whose entries are exactly the word-list entries (never the
timeline's copy of them) and whose provider items are the timeline's own
unprocessed audio files.
"""
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance.fixtures import require_wordlist  # noqa: E402
from qa2 import build_request as builder  # noqa: E402


def make_archive(root, wordlist, count=3, wrong_word=False, use_original=True):
    root.mkdir(parents=True, exist_ok=True)
    lines = [line.strip() for line in
             wordlist.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    entries = []
    audio = []
    for offset, line in enumerate(lines[:count]):
        phonetic = line[line.index('/'):line.index('/', line.index('/') + 1) + 1]
        word = line[:line.index('/')].strip()
        rest = line[line.index('/', line.index('/') + 1) + 1:].strip()
        meaning = rest
        spoken = rest.split('. ', 1)[-1] if '. ' in rest else rest
        entries.append({'index': 1 + offset, 'word': word + ('x' if wrong_word and offset == 0 else ''),
                        'phonetic': phonetic, 'meaning': meaning, 'spoken_meaning': spoken,
                        'start_frame': 60, 'male_frame': 120, 'chinese_frame': 180,
                        'end_frame': 300})
        cache = root / 'audio-cache' / ('w%d' % (1 + offset))
        cache.mkdir(parents=True, exist_ok=True)
        name = 'original.volcengine_legacy.ogg' if use_original else 'prepared.wav'
        (cache / name).write_bytes(b'OggS')
        audio.append({'word_index': 1 + offset, 'role': 'female', 'text': word,
                      'voice': 'BV503_streaming', 'path': str(cache / 'prepared.wav'),
                      'start_frame': 60, 'duration_frames': 60})
    timeline = {'first_index': 1, 'last_index': count, 'fps': 60, 'width': 1920,
                'height': 1080, 'speed': 1.25, 'video_codec': 'h265',
                'total_frames': 300, 'title': 't', 'footer': 'f',
                'background': str(root / 'background.mp4'), 'intro_video': '',
                'styles': {}, 'words': entries, 'audio': audio}
    (root / 'background.mp4').write_bytes(b'\x00')
    (root / 'timeline.json').write_text(json.dumps(timeline, ensure_ascii=False),
                                        encoding='utf-8')
    return root


def arguments(archive, wordlist, out, output):
    return type('Args', (), {
        'archive': str(archive), 'wordlist': str(wordlist), 'out': str(out),
        'output': str(output), 'key': 'k', 'first': 1, 'last': 3,
        'background': None, 'intro': None, 'intro_s': 1.0, 'width': 1920,
        'height': 1080, 'fps': 60, 'speed': 1.25, 'video_codec': None,
        'concurrency': 1,
        # QA-3 added a source+range mode with a spoken policy; the entries mode
        # tested here must stay the default.
        'source_mode': False, 'policy': None, 'spoken_file': None})()


def test_request_words_come_from_the_word_list_not_the_timeline(tmp_path):
    wordlist = require_wordlist()
    archive = make_archive(tmp_path / 'archive', wordlist)
    request = builder.build_request(arguments(archive, wordlist, tmp_path / 'r.json',
                                              tmp_path / 'out'))
    entries = request['lesson']['entries']
    lines = [line.strip() for line in
             wordlist.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    assert [e['word'] for e in entries] == [line.split(' ')[0] for line in lines[:3]]
    # The provider is fed the archive's own *unprocessed* audio, never prepared.wav.
    assert len(request['provider']['items']) == 3
    assert all(item['path'].endswith('original.volcengine_legacy.ogg')
               for item in request['provider']['items'])
    # Exactly one speech source: a provider or prepared_speech, never both.
    assert 'provider' in request and 'prepared_speech' not in request


def test_a_timeline_that_disagrees_with_the_word_list_is_refused(tmp_path):
    wordlist = require_wordlist()
    archive = make_archive(tmp_path / 'archive', wordlist, wrong_word=True)
    with pytest.raises(SystemExit) as raised:
        builder.build_request(arguments(archive, wordlist, tmp_path / 'r.json',
                                        tmp_path / 'out'))
    assert '与只读词表不一致' in str(raised.value)


def test_a_missing_unprocessed_recording_is_refused(tmp_path):
    wordlist = require_wordlist()
    archive = make_archive(tmp_path / 'archive', wordlist, use_original=False)
    with pytest.raises(SystemExit) as raised:
        builder.build_request(arguments(archive, wordlist, tmp_path / 'r.json',
                                        tmp_path / 'out'))
    assert '缺少未加工音频' in str(raised.value)


def test_one_provider_item_per_stage_of_every_word(tmp_path):
    wordlist = require_wordlist()
    archive = make_archive(tmp_path / 'archive', wordlist)
    timeline_path = archive / 'timeline.json'
    timeline = json.loads(timeline_path.read_text(encoding='utf-8'))
    for base in list(timeline['audio']):
        for role in ('male', 'chinese'):
            timeline['audio'].append(dict(base, role=role))
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False), encoding='utf-8')
    request = builder.build_request(arguments(archive, wordlist, tmp_path / 'r.json',
                                              tmp_path / 'out'))
    assert len(request['provider']['items']) == 9
    assert {item['role'] for item in request['provider']['items']} == \
        {'female', 'male', 'chinese'}
    assert request['lesson']['batch_size'] == 3
