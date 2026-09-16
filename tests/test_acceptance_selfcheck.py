"""The acceptance validator's own guard rails, with no media needed.

These tests are about the parts that decide *what is expected*, because that is
where the M0 holes were: expectations used to be re-derived from the very
timeline being judged, so a coordinated mistake passed every face.  Everything
here is checked against the read-only word list and against constants recomputed
by hand - never against the production pipeline's own functions.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent

from acceptance import accept_range  # noqa: E402
from acceptance.fixtures import require_wordlist  # noqa: E402

# The delivered batch 151-200 was produced with these two policies; the word
# list is the only input both the pipeline and the validator may agree on.
WORDLIST_SHA256 = 'b42f381ab2a3757d9b31c11a22444bebf20ca3f26982aaba7e6283b97916e99a'
ENTRIES = 1654


@pytest.fixture(scope='module')
def wordlist():
    return require_wordlist()


def test_wordlist_identity_is_pinned(wordlist):
    """If the word list changes, every stored expectation silently changes too."""
    digest = hashlib.sha256(wordlist.read_bytes()).hexdigest()
    assert digest == WORDLIST_SHA256, (
        '只读词表内容变了（sha256 %s）。期望值必须重新复核，不能沿用旧验收。' % digest)


def test_entries_151_200_match_the_published_batch():
    """Hand-derived from the word list: the text the batch must show and speak.

    ``meaning`` keeps the original spacing ("原 则"), ``phonetic`` keeps the
    slashes, and the legacy cleaning removes every part-of-speech tag but keeps
    everything else - including the space the removed tag leaves behind
    ("主要的n. 校长" becomes "主要的 校长") and a tag glued to Chinese.
    """
    words = accept_range.load_expectations(require_wordlist(), 151, 200, 'legacy')['words']
    assert len(words) == 50
    assert [w['index'] for w in words] == list(range(151, 201))
    first = words[0]
    assert first == {'index': 151, 'word': 'demonstrate', 'phonetic': '/ˈdemənstreɪt/',
                     'meaning': 'v. 证明；证实', 'spoken_meaning': '证明；证实'}
    last = words[-1]
    assert last['word'] == 'principal'
    assert last['meaning'] == 'adj. 主要的n. 校长'
    # "adj." and "n." both go; the space left by "n." stays.
    assert last['spoken_meaning'] == '主要的 校长'
    by_index = {w['index']: w for w in words}
    assert by_index[159]['meaning'] == 'adj. 故意的；不慌不忙的 v. 仔细考虑'
    # Two spaces: the removed "v." leaves the space that separated it.
    assert by_index[159]['spoken_meaning'] == '故意的；不慌不忙的  仔细考虑'
    assert by_index[179]['meaning'] == 'adj. & n. & v. 倒 闭'
    # legacy cleaning removes the tags and nothing else, so the connectors stay.
    assert by_index[179]['spoken_meaning'] == '&  &  倒 闭'
    assert by_index[186]['meaning'] == 'n. 文物adj. 陈旧的'
    assert by_index[186]['spoken_meaning'] == '文物 陈旧的'


def test_wordlist_parser_rejects_a_line_without_a_word():
    with pytest.raises(ValueError):
        accept_range.split_entry(' /ˈnəʊ/ n. 没有词')


def test_v2_policy_differs_from_legacy_only_in_the_cleaning():
    words = accept_range.load_expectations(require_wordlist(), 179, 179, 'v2')['words']
    # v2 (the policy the user approved): a tag goes together with the connectors
    # that only join tags, so "n. & v." disappears as a unit.
    assert words[0]['meaning'] == 'adj. & n. & v. 倒 闭'
    assert words[0]['spoken_meaning'] == '倒 闭'


def test_pacing_constants_are_the_approved_contract():
    """The published contract (word_video/timing.py) in numbers, restated here so
    a silent change on either side fails this test."""
    assert (accept_range.ENGLISH_FLOOR, accept_range.CHINESE_FLOOR) == (1.0, 0.5)
    assert (accept_range.FIRST_SIX, accept_range.EXTRA) == (0.4, 0.2)
    assert (accept_range.GAP_S, accept_range.SPEED_TOLERANCE_FRAMES) == (0.1, 1)
    assert accept_range.MEDIA_TOLERANCE_FRAMES == 2
    assert accept_range.TAIL_TOLERANCE_SECONDS == 0.15
    assert accept_range.INK_THRESHOLD == 0.006
    assert accept_range.EARLY_INK_THRESHOLD == 0.015


def test_chinese_floor_by_hand():
    """max(0.5, min(n,6)*0.4 + (n-6)*0.2) over the Chinese characters only."""
    assert accept_range.chinese_floor('证明；证实', 'demonstrate') == pytest.approx(1.6)
    assert accept_range.chinese_floor('严重危险', 'peril') == pytest.approx(1.6)
    # punctuation is not counted: eight characters become six plus two.
    assert accept_range.chinese_floor('连续不断的；持续的', 'continuous') == pytest.approx(2.8)
    # word 179 in the delivered range: "倒 闭" is two characters after cleaning.
    assert accept_range.chinese_floor('&  &  倒 闭', 'bankrupt') == pytest.approx(0.8)
    # two characters are still above the floor; one character sits exactly on it.
    assert accept_range.chinese_floor('原 则', 'principle') == pytest.approx(0.8)
    assert accept_range.chinese_floor('挤', 'squeeze') == pytest.approx(0.5)
    # no Chinese at all falls back to the Latin word length: 8//4 = 2 -> 0.8.
    assert accept_range.chinese_floor('', 'abcdefgh') == pytest.approx(0.8)
    # a long meaning grows by 0.2 s per character after the sixth.
    assert accept_range.chinese_floor('一二三四五六七八九十', 'x') == pytest.approx(3.2)


def test_frame_to_ms_rounds_like_the_timeline():
    # 60 fps: frame 1 is 16.667 ms and must round to 17 ms, not truncate to 16.
    assert accept_range.frame_to_ms(1, 60) == 17
    assert accept_range.frame_to_ms(0, 60) == 0
    assert accept_range.frame_to_ms(10956, 60) == 182600


def test_validator_refuses_without_independent_expectations(tmp_path):
    """No --source/--range means no verdict: REFUSED, not PASS."""
    batch = tmp_path / 'any-batch'
    batch.mkdir()
    report = tmp_path / 'report.json'
    result = subprocess.run(
        [sys.executable, str(HERE / 'acceptance' / 'accept_range.py'), str(batch),
         '--json', str(report)], capture_output=True)
    assert result.returncode == 2, result.stderr.decode('utf-8', 'replace')[-800:]
    data = json.loads(report.read_text(encoding='utf-8'))
    assert data['verdict'] == 'REFUSED'
    assert 'expectations' in data['failures']


def test_archive_validator_refuses_an_empty_expectation(tmp_path):
    """--expect with nothing in it must not mean "expect nothing, so PASS"."""
    root = tmp_path / 'root'
    root.mkdir()
    result = subprocess.run(
        [sys.executable, str(HERE / 'acceptance' / 'accept_all.py'), str(root),
         '--source', str(require_wordlist()), '--expect', ' , ',
         '--out', str(tmp_path / 'reports')], capture_output=True, text=True)
    assert result.returncode == 2, result.stderr[-800:]
    assert json.loads(result.stdout)['verdict'] == 'REFUSED'


def test_srt_parser_reads_the_published_format(tmp_path):
    text = ('1\n00:00:01,000 --> 00:00:02,500\nhello\n\n'
            '2\n00:00:02,500 --> 00:00:03,000\nworld\n')
    path = tmp_path / 'probe.srt'
    path.write_text(text, encoding='utf-8')
    cues = accept_range.parse_srt(path)
    assert cues == [{'number': 1, 'start_ms': 1000, 'end_ms': 2500, 'text': 'hello'},
                    {'number': 2, 'start_ms': 2500, 'end_ms': 3000, 'text': 'world'}]
    assert accept_range.cjk('连续不断的；持续的') == '连续不断的持续的'
