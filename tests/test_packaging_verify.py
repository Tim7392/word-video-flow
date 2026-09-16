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
    environment = verify.clean_environment(tmp_path)
    assert environment['PATH'] == 'C:\\Windows\\System32'
    assert environment['TEMP'] == str(tmp_path) == environment['TMP']
    assert not [name for name in environment if name.startswith('PYTHON')]
    for name in ('VOLC_TTS_API_KEY', 'MODEL_SPEECH_API_KEY', 'VOLC_TTS_ACCESS_TOKEN'):
        assert name not in environment


def test_clean_environment_keeps_what_windows_needs(tmp_path):
    environment = verify.clean_environment(tmp_path)
    for name in ('SystemRoot', 'windir', 'ComSpec', 'PATHEXT'):
        assert environment.get(name)


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
