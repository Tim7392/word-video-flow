"""The headless entry honours the project's own delivery settings.

H0 reproduced this on a real project (``out\\tim4\\project-501-550``, whose
``delivery.json`` names the 1080p background and ``video_codec: h265``):

    python -m word_video.exporters --project <工程> --out <新目录>
    {"ok": false, "error": {"type": "PermissionError",
     "message": "[Errno 13] Permission denied: 'D:\\...\\word_video_flow\\repo'"}}

The entry never read ``delivery.json``, so ``manifest.background`` was ``''``, and
``Path('')`` is the *current working directory*: the draft asked ``shutil.copy2`` to
copy a folder.  The same project exported through the editor used h265 and the real
background, the CLI used h264 and no background - so "three products from one
project" was not true, and the refusal that did arrive was about the cwd.

A's ``word_video/storage/delivery.py`` is the single source for those two settings
(its docstring says the CLI must honour it) and A's
``application/batches.py::check_delivery`` already names this failure with fixes.
These tests are real subprocesses: the defect lived in the entry point, and only a
real process shows what a caller actually receives.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
ERROR_KEYS = {'type', 'code', 'message', 'object_path', 'hint', 'fixes'}


def run_entry(*args):
    return subprocess.run([sys.executable, '-m', 'word_video.exporters', *args],
                          capture_output=True, cwd=str(REPO), timeout=600)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _media(root):
    from word_video.media import executable, run

    background = Path(root) / 'background.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
         '-i', 'testsrc=size=160x90:rate=30:duration=0.4', '-an', '-c:v', 'libx264',
         '-pix_fmt', 'yuv420p', str(background)])
    other = Path(root) / 'other.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
         '-i', 'testsrc=size=160x90:rate=30:duration=0.4', '-an', '-c:v', 'libx264',
         '-pix_fmt', 'yuv420p', str(other)])
    voice = Path(root) / 'voice.wav'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
         '-i', 'sine=frequency=440:duration=0.4:sample_rate=48000', '-ac', '1',
         '-c:a', 'pcm_s16le', str(voice)])
    return background, other, voice


def _project(root, *, background='', video_codec=''):
    """One solvable three-role project, with the delivery sidecar the editor writes."""
    from word_video.application import instantiate
    from word_video.domain import Project, Record
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.exporters import catalog
    from word_video.storage.assets import AssetIndex, AssetRef
    from word_video.storage.project_store import save_project

    _background, _other, voice = _media(root)
    project_dir = Path(root) / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)
    record = Record(id='w1', word='apple', phonetic='', meaning='n. 苹果',
                    spoken_meaning='苹果', index=1)
    assets = {asset_id: catalog.Asset(asset_id=asset_id, path=str(voice), voice='V')
              for asset_id in ('w1:female', 'w1:male', 'w1:chinese')}
    media = catalog.measure_all(assets)
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (record,), media,
                          Project(project_id='delivery-contract', intro_s=0.0,
                                  fps_num=24, width=320, height=180, speed=1.0))
    save_project(project, project_dir)
    index = AssetIndex(folder=str(project_dir))
    for asset_id, asset in assets.items():
        info = media[asset_id]
        index = index.with_ref(AssetRef(asset_id=asset_id, path=asset.path,
                                        units=info.units, unit_num=info.unit_num,
                                        unit_den=info.unit_den, kind='audio',
                                        voice=asset.voice))
    index.save(project_dir)
    if background or video_codec:
        (project_dir / 'delivery.json').write_text(json.dumps(
            {'schema': 'wv-delivery@1', 'background': background,
             'video_codec': video_codec}, ensure_ascii=False), encoding='utf-8')
    return project_dir


class DeliverySettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.background, self.other, _voice = _media(self.root)

    def _run(self, project_dir, *args, run='test'):
        out = self.root / ('out-%s' % run)
        result = run_entry('--project', str(project_dir), '--out', str(out), '--run',
                           run, '--no-video', *args)
        return result, out / run

    def test_the_delivery_background_and_codec_are_honoured(self):
        """No --background, no --codec: the run must be the one the editor would make."""
        project_dir = _project(self.root, background=str(self.background),
                               video_codec='h265')
        result, run_dir = self._run(project_dir)
        stdout = result.stdout.decode('utf-8', 'replace')
        self.assertEqual(0, result.returncode, stdout[-800:])
        self.assertTrue(json.loads(stdout)['ok'], stdout[-800:])
        timeline = json.loads((run_dir / 'timeline.json').read_text(encoding='utf-8'))
        self.assertEqual(str(self.background), timeline['background'])
        self.assertEqual('h265', timeline['video_codec'])
        # The draft's own record names the same source, and the copy in Resources is
        # that file: the editor and the CLI now deliver the same picture.
        draft = run_dir / 'editable-draft'
        manifest = json.loads((draft / 'word_video_manifest.json')
                              .read_text(encoding='utf-8'))
        self.assertEqual(str(self.background), manifest['background'])
        copies = [path for path in (draft / 'Resources').iterdir()
                  if path.suffix == '.mp4' and digest(path) == digest(self.background)]
        self.assertEqual(1, len(copies), sorted(p.name for p in
                                                (draft / 'Resources').iterdir()))

    def test_an_explicit_parameter_overrides_the_delivery_setting(self):
        project_dir = _project(self.root, background=str(self.background),
                               video_codec='h265')
        result, run_dir = self._run(project_dir, '--background', str(self.other),
                                    '--codec', 'h264')
        self.assertEqual(0, result.returncode, result.stdout.decode('utf-8', 'replace')[-800:])
        timeline = json.loads((run_dir / 'timeline.json').read_text(encoding='utf-8'))
        self.assertEqual(str(self.other), timeline['background'])
        self.assertEqual('h264', timeline['video_codec'])

    def test_no_background_anywhere_is_refused_before_any_output(self):
        """The defect itself: exit 2 with BACKGROUND_MISSING, never a cwd error."""
        project_dir = _project(self.root)                    # no delivery.json at all
        before = sorted(path.name for path in REPO.iterdir())
        result, run_dir = self._run(project_dir)
        stdout = result.stdout.decode('utf-8', 'replace')
        stderr = result.stderr.decode('utf-8', 'replace')
        payload = json.loads(stdout)                         # exactly one object
        self.assertFalse(payload['ok'], payload)
        self.assertEqual(ERROR_KEYS, set(payload['error']))
        self.assertEqual('BACKGROUND_MISSING', payload['error']['code'])
        self.assertTrue(payload['error']['hint'], payload)
        self.assertTrue(payload['error']['fixes'], payload)
        self.assertEqual(2, result.returncode, stdout[-400:])
        self.assertNotIn('PermissionError', stdout)
        self.assertNotIn('PermissionError', stderr)
        self.assertNotIn('Traceback', stdout + stderr)
        # Nothing was created: no run folder, and the working directory is untouched
        # (the old failure was "copy this folder" on the cwd).
        self.assertFalse(run_dir.exists(), run_dir)
        self.assertEqual(before, sorted(path.name for path in REPO.iterdir()))

    def test_a_missing_background_file_is_named_as_such(self):
        gone = self.root / 'gone-background.mp4'
        project_dir = _project(self.root, background=str(gone), video_codec='h265')
        result, run_dir = self._run(project_dir)
        payload = json.loads(result.stdout.decode('utf-8', 'replace'))
        self.assertFalse(payload['ok'], payload)
        self.assertEqual('BACKGROUND_FILE_MISSING', payload['error']['code'])
        self.assertEqual(2, result.returncode)
        self.assertIn('gone-background.mp4', payload['error']['message'])
        self.assertFalse(run_dir.exists())


if __name__ == '__main__':
    unittest.main()
