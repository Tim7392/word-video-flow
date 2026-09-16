"""``python -m word_video.exporters`` keeps the CLI contract when it refuses.

W10's failure-isolation gate recorded the gap: a project whose registered media is
gone makes the exporter raise A's ``AssetFileError``, which is *not* one of
``ValueError``/``OSError``/``RuntimeError`` - so the standalone entry printed a
traceback, wrote nothing on stdout, and exited 1.  A's CLI contract (``wv-cli@1``)
says stdout carries one JSON object for every outcome, with a structured error
carrying ``code``/``object_path``/``hint`` and a ``fixes`` list, and the exit code
says whether the caller has to supply something (2) or the run failed (1).

The project here is built the way W10's ``new_entry_probe`` builds one - A's
``assets.json`` registry, the three speech files measured through the catalogue -
and then one registry entry is pointed at a file that is not there.  The check is
against the real subprocess, not against the function that builds the answer: the
defect was in the entry point, and only the real process shows it.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]

#: The shapes this entry point is allowed to answer with.
ERROR_KEYS = {'type', 'code', 'message', 'object_path', 'hint', 'fixes'}


def run_entry(*args):
    return subprocess.run([sys.executable, '-m', 'word_video.exporters', *args],
                          capture_output=True, cwd=str(REPO), timeout=300)


def _project(root):
    """A minimal solvable project whose registry names one missing file."""
    from word_video.application import instantiate
    from word_video.domain import Project, Record
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.exporters import catalog
    from word_video.media import executable, run
    from word_video.storage.assets import AssetIndex, AssetRef
    from word_video.storage.project_store import save_project

    project_dir = Path(root) / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)
    record = Record(id='w1', word='apple', phonetic='', meaning='n. 苹果',
                    spoken_meaning='苹果', index=1)
    voice = project_dir / 'voice.wav'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
         '-i', 'sine=frequency=440:duration=0.4:sample_rate=48000', '-ac', '1',
         '-c:a', 'pcm_s16le', str(voice)])
    assets = {asset_id: catalog.Asset(asset_id=asset_id, path=str(voice), voice='V')
              for asset_id in ('w1:female', 'w1:male', 'w1:chinese')}
    media = catalog.measure_all(assets)
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media,
                          Project(project_id='cli-contract', intro_s=0.0, fps_num=24,
                                  width=320, height=180, speed=1.0))
    save_project(project, project_dir)
    index = AssetIndex(folder=str(project_dir))
    for asset_id, asset in assets.items():
        info = media[asset_id]
        path = asset.path
        if asset_id == 'w1:male':
            # A recording that was moved or deleted: the failure a member hits.
            path = str(project_dir / 'gone.wav')
        index = index.with_ref(AssetRef(asset_id=asset_id, path=path, units=info.units,
                                        unit_num=info.unit_num, unit_den=info.unit_den,
                                        kind='audio', voice=asset.voice))
    index.save(project_dir)
    return project_dir


class ExportEntryContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_missing_asset_is_one_json_object_exit_2_and_no_traceback(self):
        project_dir = _project(self.root)
        out = self.root / 'out'
        result = run_entry('--project', str(project_dir), '--out', str(out),
                           '--no-video', '--no-draft')
        stdout = result.stdout.decode('utf-8', 'replace')
        payload = json.loads(stdout)               # exactly one object, or this raises
        self.assertFalse(payload['ok'], payload)
        error = payload['error']
        self.assertEqual(ERROR_KEYS, set(error))
        self.assertEqual('AssetFileError', error['type'])
        self.assertEqual('ASSET_FILE_MISSING', error['code'])
        self.assertTrue(error['object_path'], error)
        self.assertIn('w1:male', error['object_path'])
        self.assertTrue(error['hint'], 'a refusal has to name a way forward')
        self.assertIsInstance(error['fixes'], list)
        # The contract's exit code: the caller has to put the file back (2), not a
        # crash (1).
        self.assertEqual(2, result.returncode, stdout[-400:])
        self.assertNotIn('Traceback', stdout)
        self.assertNotIn('Traceback', result.stderr.decode('utf-8', 'replace'))
        # A refusal is not a partial delivery.
        self.assertFalse(list(out.rglob('*.srt')) if out.is_dir() else [])

    def test_a_good_project_still_answers_ok_and_exits_zero(self):
        project_dir = _project(self.root)
        (project_dir / 'assets.json').write_text(
            (project_dir / 'assets.json').read_text(encoding='utf-8')
            .replace('gone.wav', 'voice.wav'), encoding='utf-8')
        result = run_entry('--project', str(project_dir), '--out',
                           str(self.root / 'good'), '--no-video', '--no-draft')
        stdout = result.stdout.decode('utf-8', 'replace')
        payload = json.loads(stdout)
        self.assertTrue(payload['ok'], stdout[-600:])
        self.assertEqual(0, result.returncode)
        self.assertEqual(5, len(payload['result']['srts']))

    def test_bad_usage_is_one_json_object_and_exits_2(self):
        result = run_entry('--project')
        payload = json.loads(result.stdout.decode('utf-8', 'replace'))
        self.assertFalse(payload['ok'])
        self.assertEqual('BAD_REQUEST', payload['error']['code'])
        self.assertEqual(2, result.returncode)
        self.assertNotIn('usage:', result.stdout.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main()
