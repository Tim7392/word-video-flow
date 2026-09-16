"""Reject partial/stale shader deliveries before replacing readable controls."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from liquid_glass.renderer import ASSETS, validate_assets

class GlassResourceTests(unittest.TestCase):
    def test_shipped_resources_match_their_manifest_and_current_sources(self):
        self.assertTrue(validate_assets())
        manifest=json.loads((ASSETS/"assets.json").read_text(encoding="utf-8"))
        for name,digest in manifest["source_sha256"].items():
            self.assertEqual(hashlib.sha256((ASSETS/name).read_bytes()).hexdigest(),digest,
                             "Shader changed without being recompiled: "+name)

    def make_package(self,folder):
        destination=Path(folder)
        manifest=json.loads((ASSETS/"assets.json").read_text(encoding="utf-8"))
        (destination/"assets.json").write_text(json.dumps(manifest),encoding="utf-8")
        for name in manifest["files"]:
            target=destination/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((ASSETS/name).read_bytes())
        return destination

    def test_missing_shader_is_rejected_before_gpu_initialization(self):
        with tempfile.TemporaryDirectory(prefix="subtitle_glass_assets_") as folder:
            package=self.make_package(folder)
            (package/"shaders/glass.frag.qsb").unlink()
            self.assertFalse(validate_assets(package))

    def test_changed_shader_and_changed_scene_are_rejected(self):
        for name in ("shaders/glass.frag.qsb","GlassScene.qml"):
            with self.subTest(resource=name),tempfile.TemporaryDirectory(prefix="subtitle_glass_assets_") as folder:
                package=self.make_package(folder)
                target=package/name
                target.write_bytes(target.read_bytes()+b"\nmodified")
                self.assertFalse(validate_assets(package))

