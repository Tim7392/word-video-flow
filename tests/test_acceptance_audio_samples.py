"""A delivered asset's audio sample count, recomputed independently.

C found the bug class and B fixed it: a packet count is not a sample count.  For
``4级1500开头_透明通道-1080p.mov`` the container reports 88 AAC packets; 88 * 1024
= 90112 samples (1.8773 s), while the stream's own duration is 89088 samples
(1.856 s) - exactly one packet less, because the audio is trimmed to the video and
the last packet is partial.  Reading ``nb_frames`` as samples is wrong by 21 ms.

What this test asserts, and where each expectation comes from:

* the sample count is **89088**.  QA recomputed it twice, without the engine:
  decoding the stream to raw PCM at 48 kHz and counting the frames (89088), and a
  WAV written from the same decode (89088 frames).  The container's
  ``duration_ts`` x ``time_base`` x rate gives the same number.
* ``exact`` is **False**.  The approved contract says ``exact`` is true only for a
  PCM WAV header or a codec whose ``nb_frames`` really counts samples; this is a
  packetised codec, so a caller must be told the count is derived.
* the packet-based estimate is **different**, which is what makes this asset a
  guard: if 88 * 1024 ever equals the answer, the check has stopped discriminating.

The heavy part (decoding) lives in ``tests/qa2/aac_samples.py`` and runs under the
``acceptance_media`` marker, writing its evidence to ``out/reports/``; the engine
comparison above it needs no decode and stays in the default suite.
"""
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance import gate  # noqa: E402

INTRO_MOV = Path(r'D:\单词速记自动化_测试归档_0915\_prepared-1080p'
                 r'\4级1500开头_透明通道-1080p.mov')
#: QA's own recomputation: decode to 48 kHz mono PCM and count the frames.
DECODED_SAMPLES = 89088
PACKET_FRAMES = 88
AAC_FRAME_SAMPLES = 1024


def require_intro():
    if not INTRO_MOV.exists():
        pytest.fail('资产不可用：%s' % INTRO_MOV)
    return INTRO_MOV


def test_the_engine_reports_the_recomputed_count_and_admits_it_is_derived():
    from word_video.media import audio_sample_count

    measured = audio_sample_count(require_intro())
    assert measured['samples'] == DECODED_SAMPLES, measured
    assert measured['sample_rate'] == 48000, measured
    # A packetised codec's count is derived from duration x rate, never "exact".
    assert measured['exact'] is False, measured
    # The trap this guards: the packet count would give a different number.
    assert PACKET_FRAMES * AAC_FRAME_SAMPLES != DECODED_SAMPLES
    assert PACKET_FRAMES * AAC_FRAME_SAMPLES - DECODED_SAMPLES == AAC_FRAME_SAMPLES


@pytest.mark.acceptance_media
def test_the_decode_itself_is_measured_and_the_packet_estimate_is_rejected():
    """Run the decode-based guard and keep its evidence at a stable path."""
    import subprocess

    evidence, warning = gate.evidence_dir()
    target = evidence / 'gate-aac-samples.json'
    work = gate.WORK_ROOT / 'runtime' / 'tmp' / 'QA' / 'gate' / 'intro-decode.wav'
    work.parent.mkdir(parents=True, exist_ok=True)
    finished = gate.run([sys.executable, str(HERE / 'qa2' / 'aac_samples.py'),
                         '--input', str(require_intro()),
                         '--expect-samples', str(DECODED_SAMPLES),
                         '--work', str(work), '--json', str(target)], HERE)
    assert target.exists(), finished
    report = json.loads(target.read_text(encoding='utf-8'))
    assert report['ok'], report['problems']
    assert report['counts']['decode'] == DECODED_SAMPLES, report['counts']
    assert report['counts']['container_duration_ts'] == DECODED_SAMPLES, report['counts']
    assert report['counts']['packets_times_frame_samples'] == \
        PACKET_FRAMES * AAC_FRAME_SAMPLES, report['counts']
    assert report['decoded_rate'] == 48000, report
    if warning:
        report.setdefault('warnings', []).append(warning)
