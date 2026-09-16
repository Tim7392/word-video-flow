"""The stripped environment and manifest rules of the acceptance verifier.

Both rules below failed once in a way that mattered, so they are pinned:

  * the audio cache keeps a ``complete.json`` marker per cached utterance, and an
    unchecked ``rglob`` counted those empty markers as batch manifests - which made
    a good job look broken;
  * the packaged ffprobe path is ``ffmpeg/ffprobe.exe``, not ``WordVideo/ffmpeg/...``.
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load('packaging_verify', 'verify_member_package.py')


def test_clean_environment_keeps_only_system32_on_path(tmp_path):
    environment, _ = verify.clean_environment(tmp_path)
    assert environment['PATH'] == 'C:\\Windows\\System32'
    assert environment['TEMP'] == str(tmp_path) == environment['TMP']
    assert not [name for name in environment if name.startswith('PYTHON')]
    for name in ('VOLC_TTS_API_KEY', 'MODEL_SPEECH_API_KEY', 'VOLC_TTS_ACCESS_TOKEN'):
        assert name not in environment


def test_clean_environment_supplies_what_windows_needs_and_says_so(tmp_path):
    """A restricted runner without SystemRoot makes a packaged exe exit 1 with empty
    stderr, which is indistinguishable from a broken package.  The harness therefore
    *supplies* these names rather than inheriting them, and reports which ones the
    parent process was missing."""
    environment, provenance = verify.clean_environment(tmp_path)
    for name in ('SystemRoot', 'windir', 'SystemDrive', 'ComSpec', 'PATHEXT'):
        assert environment.get(name), name
    assert environment['windir'] == environment['SystemRoot'] == 'C:\\Windows'
    assert 'SystemRoot' in provenance['set_by_harness']
    assert 'windir' in provenance['set_by_harness']
    # On an ordinary machine these are inherited too, so the harness adds nothing;
    # in a stripped container it adds them.  Either way the fact is recorded.
    assert isinstance(provenance['added_by_harness'], list)
    assert set(provenance['added_by_harness']) <= set(provenance['set_by_harness'])


def test_the_environment_can_be_built_without_systemroot_in_the_parent(tmp_path, monkeypatch):
    """The regression this guards: QA's restricted run started the packaged exe with
    no SystemRoot, and the failure looked like a corrupt package."""
    for name in ('SystemRoot', 'windir', 'SystemDrive', 'ComSpec'):
        monkeypatch.delenv(name, raising=False)
    environment, provenance = verify.clean_environment(tmp_path)
    assert environment['SystemRoot'] == 'C:\\Windows'
    assert environment['windir'] == 'C:\\Windows'
    assert set(provenance['added_by_harness']) >= {'SystemRoot', 'windir', 'ComSpec'}


def test_ffprobe_is_the_copy_shipped_in_the_package():
    assert verify.FFPROBE == 'ffmpeg/ffprobe.exe'


def test_ffprobe_resolves_for_a_real_package():
    package = ROOT.parent.parent / 'out' / 'packages' / ('word-video-member-' + (ROOT / 'packaging' / 'member_version.txt').read_text(encoding='utf-8').strip())
    if not package.is_dir():
        return  # no built package in this checkout: nothing to resolve
    assert (package / verify.FFPROBE).is_file()


def test_batch_manifests_ignore_audio_cache_markers(tmp_path):
    batch = tmp_path / 'job' / '0151-0153'
    batch.mkdir(parents=True)
    (batch / 'complete.json').write_text(
        json.dumps({'files': [{'path': 'a', 'sha256': 'b'}], 'batch': {'first': 151}}),
        encoding='utf-8')
    marker = tmp_path / 'job' / 'audio-cache' / 'deadbeef'
    marker.mkdir(parents=True)
    (marker / 'complete.json').write_text(json.dumps({'files': [], 'cache': 'x'}),
                                          encoding='utf-8')
    manifests = verify.batch_manifests(tmp_path / 'job')
    assert [path.parent.name for path, _ in manifests] == ['0151-0153']
    assert manifests[0][1]['batch']['first'] == 151


def test_batch_manifests_skip_unreadable_files(tmp_path):
    (tmp_path / 'complete.json').write_text('{not json', encoding='utf-8')
    assert verify.batch_manifests(tmp_path) == []


def test_batch_manifests_are_empty_for_an_empty_job_folder(tmp_path):
    assert verify.batch_manifests(tmp_path) == []
