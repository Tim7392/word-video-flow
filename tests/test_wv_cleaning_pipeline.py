"""The cleaning policy inside the request pipeline.

``jobs.lesson_from_request`` is where a request becomes a lesson, so this is where
the approved rule has to land: a word-list request gets its Chinese reading text
derived from the display meaning, a request that brings its own text keeps it
verbatim, and what was applied is recorded for later inspection.

Two properties are pinned here because planning depends on them:

* only the derived reading field changes -- the word, the phonetic and the display
  meaning stay exactly what the word list says;
* the frame grid does **not** move while the audio stays the same.  v2 removes only
  non-Hanzi characters (tags, connectors, embedded phonetics) and the Chinese floor
  counts Hanzi, so both policies reserve the same frames; a stage changes length
  only once the cleaned text has been re-synthesised into a different recording.
"""
from pathlib import Path

import pytest

from word_video import jobs, text_policy
from word_video.contracts import SpeechAsset, TimelineManifest
from word_video.timing import build_timeline, split_lesson

LINES = [
    'demonstrate /ˈdemənstreɪt/ v. 证明；证实',
    'murder /ˈmɜːdə/ n. & v. 谋 杀',
    'content /kənˈtent/ n.  内容；目录 /kənˈtent/ adj. 满意的',
    'board /bɔːd/ n. 木板；董事会；伙食膳食 上（船、车、飞机）； 寄宿',
]
DISPLAY_MEANINGS = ['v. 证明；证实', 'n. & v. 谋 杀',
                    'n.  内容；目录 /kənˈtent/ adj. 满意的',
                    'n. 木板；董事会；伙食膳食 上（船、车、飞机）； 寄宿']
V2_SPOKEN = ['证明；证实', '谋 杀', '内容；目录 满意的',
             '木板；董事会；伙食膳食 上（船、车、飞机）； 寄宿']
LEGACY_SPOKEN = ['证明；证实', '&  谋 杀', '内容；目录 /kənˈtent/  满意的',
                 '木板；董事会；伙食膳食 上（船、车、飞机）； 寄宿']
SOURCE_DURATIONS = {'female': 1.2, 'male': 1.1, 'chinese': 2.0}


def make_request(tmp_path, policy=None, entries=None, lines=LINES):
    """A request shaped like the real ones, pointing only at files in tmp."""
    wordlist = tmp_path / 'words.txt'
    wordlist.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    background = tmp_path / 'background.mp4'
    background.write_bytes(b'placeholder; only resolved, never probed here')
    lesson = {'background': str(background), 'width': 640, 'height': 360,
              'batch_size': 50}
    if policy is not None:
        lesson['spoken_policy'] = policy
    request = {'lesson': lesson}
    if entries is None:
        request['source'] = {'path': str(wordlist)}
        request['range'] = {'start': 1, 'end': len(lines)}
    else:
        lesson['entries'] = entries
    return request


def assets_for(lesson):
    """Declared speech assets with the *same* durations for both policies."""
    assets = []
    for entry in lesson.entries:
        for role in ('female', 'male', 'chinese'):
            text = entry.spoken_meaning if role == 'chinese' else entry.word
            seconds = SOURCE_DURATIONS[role]
            assets.append(SpeechAsset(word_index=entry.index, role=role, text=text,
                                      path='unused.wav', duration_s=seconds,
                                      rendered_duration_s=seconds))
    return assets


def hanzi(text):
    return sum(1 for char in text if '\u4e00' <= char <= '\u9fff')


def test_the_default_policy_is_v2_and_only_the_reading_changes(tmp_path):
    lesson = jobs.lesson_from_request(make_request(tmp_path))
    assert lesson.spoken_policy == 'v2'
    assert [entry.meaning for entry in lesson.entries] == DISPLAY_MEANINGS
    assert [entry.spoken_meaning for entry in lesson.entries] == V2_SPOKEN
    # The word and the phonetic are never touched by the policy.
    assert [entry.word for entry in lesson.entries] == ['demonstrate', 'murder',
                                                        'content', 'board']
    assert [entry.phonetic for entry in lesson.entries] == ['/ˈdemənstreɪt/', '/ˈmɜːdə/',
                                                            '/kənˈtent/', '/bɔːd/']


def test_legacy_reproduces_the_reading_of_a_delivered_batch(tmp_path):
    lesson = jobs.lesson_from_request(make_request(tmp_path, policy='legacy'))
    assert lesson.spoken_policy == 'legacy'
    assert [entry.spoken_meaning for entry in lesson.entries] == LEGACY_SPOKEN
    assert [entry.meaning for entry in lesson.entries] == DISPLAY_MEANINGS


def test_a_request_that_brings_its_own_reading_text_is_not_cleaned_again(tmp_path):
    supplied = [{'index': 1, 'word': 'apple', 'phonetic': '/ˈæpəl/',
                 'meaning': 'n. 苹果', 'spoken_meaning': 'n. 苹果'}]
    lesson = jobs.lesson_from_request(make_request(tmp_path, entries=supplied))
    assert lesson.spoken_policy == 'supplied'
    assert lesson.entries[0].spoken_meaning == 'n. 苹果'
    assert lesson.entries[0].meaning == 'n. 苹果'


def test_a_stored_request_is_replayed_without_re_cleaning(tmp_path):
    """A job freezes ``asdict(lesson)`` and reads it back: that must round-trip.

    The regression this pins: the stored lesson carries both ``entries`` and the
    policy it was built with, so a pipeline that refused that pair failed every
    job at work time.
    """
    from dataclasses import asdict
    for policy in (None, 'v2', 'legacy'):
        first = jobs.lesson_from_request(make_request(tmp_path, policy=policy))
        stored = {'lesson': asdict(first)}
        again = jobs.lesson_from_request(stored)
        assert again.spoken_policy == first.spoken_policy
        assert [entry.spoken_meaning for entry in again.entries] == \
               [entry.spoken_meaning for entry in first.entries]
        assert again.entries == first.entries

    supplied = [{'index': 1, 'word': 'apple', 'phonetic': '/ˈæpəl/',
                 'meaning': 'n. 苹果', 'spoken_meaning': 'n. 苹果'}]
    given = jobs.lesson_from_request(make_request(tmp_path, entries=supplied))
    replayed = jobs.lesson_from_request({'lesson': asdict(given)})
    assert replayed.spoken_policy == 'supplied'
    assert replayed.entries == given.entries


def test_the_recorded_policy_travels_with_supplied_entries(tmp_path):
    """A policy may say what supplied text was produced with; it never re-derives it."""
    supplied = [{'index': 1, 'word': 'murder', 'phonetic': '/ˈmɜːdə/',
                 'meaning': 'n. & v. 谋 杀', 'spoken_meaning': '&  谋 杀'}]
    lesson = jobs.lesson_from_request(
        make_request(tmp_path, policy='legacy', entries=supplied))
    assert lesson.spoken_policy == 'legacy'
    assert lesson.entries[0].spoken_meaning == '&  谋 杀'      # verbatim, not re-ruled


def test_supplied_policy_without_supplied_entries_is_refused(tmp_path):
    with pytest.raises(ValueError) as error:
        jobs.lesson_from_request(make_request(tmp_path, policy='supplied'))
    assert 'needs lesson.entries' in str(error.value)


def test_unknown_policies_are_refused_before_anything_is_built(tmp_path):
    for bad in ('v3', 'V2', 2):
        with pytest.raises(ValueError):
            jobs.lesson_from_request(make_request(tmp_path, policy=bad))
    with pytest.raises(ValueError):
        jobs.lesson_from_request(make_request(tmp_path, policy='v3', entries=[
            {'index': 1, 'word': 'a', 'phonetic': '', 'meaning': 'n. a',
             'spoken_meaning': 'a'}]))


def test_the_policy_is_recorded_in_the_published_timeline(tmp_path):
    lesson = jobs.lesson_from_request(make_request(tmp_path))
    manifest = build_timeline(split_lesson(lesson)[0], assets_for(lesson))
    # Exactly what the job does before writing timeline.json.
    manifest.spoken_policy = lesson.spoken_policy
    document = manifest.to_dict()
    assert document['spoken_policy'] == 'v2'
    assert TimelineManifest.from_dict(document).spoken_policy == 'v2'

    legacy = jobs.lesson_from_request(make_request(tmp_path, policy='legacy'))
    legacy_manifest = build_timeline(split_lesson(legacy)[0], assets_for(legacy))
    legacy_manifest.spoken_policy = legacy.spoken_policy
    assert legacy_manifest.to_dict()['spoken_policy'] == 'legacy'

    # A timeline published before the field existed claims nothing about it.
    older = dict(document)
    older.pop('spoken_policy')
    assert TimelineManifest.from_dict(older).spoken_policy == ''
    with pytest.raises(ValueError):
        TimelineManifest.from_dict(dict(document, spoken_policy='v3'))
    with pytest.raises(ValueError):
        TimelineManifest.from_dict(dict(document, surprise=1))


def test_cleaning_changes_the_text_but_not_the_frame_grid(tmp_path):
    """Same audio in, same frames out: only the Hanzi count drives the floor."""
    v2 = jobs.lesson_from_request(make_request(tmp_path))
    legacy = jobs.lesson_from_request(make_request(tmp_path, policy='legacy'))
    assert [entry.spoken_meaning for entry in v2.entries] != \
           [entry.spoken_meaning for entry in legacy.entries]
    # The reason the grid cannot move: the policy removes non-Hanzi characters only.
    assert [hanzi(entry.spoken_meaning) for entry in v2.entries] == \
           [hanzi(entry.spoken_meaning) for entry in legacy.entries] == [4, 2, 7, 16]

    v2_manifest = build_timeline(split_lesson(v2)[0], assets_for(v2))
    legacy_manifest = build_timeline(split_lesson(legacy)[0], assets_for(legacy))
    frames = lambda manifest: [(word['start_frame'], word['male_frame'],
                                word['chinese_frame'], word['end_frame'])
                               for word in manifest.words]
    assert frames(v2_manifest) == frames(legacy_manifest)
    # The reading text that reaches the timeline is the policy's, per word.
    assert [word['spoken_meaning'] for word in v2_manifest.words] == V2_SPOKEN
    assert [word['spoken_meaning'] for word in legacy_manifest.words] == LEGACY_SPOKEN
    # 4.0 s ... hand-checked: intro 2.0 s -> 120 frames; the Chinese floor is
    # max(0.5, min(16,6)*0.4 + 10*0.2) = 4.4 s, so the recording (2.0 s) never wins.
    assert v2_manifest.words[3]['chinese_frame'] > v2_manifest.words[3]['male_frame']


def test_leftovers_are_visible_for_the_change_report(tmp_path):
    lesson = jobs.lesson_from_request(make_request(tmp_path))
    text = lesson.entries[3].spoken_meaning
    assert text_policy.leftover_connectors(text) == ('、',)   # kept, reported
    assert text_policy.leftover_connectors(lesson.entries[1].spoken_meaning) == ()
