"""Independent behavior contract for the UI-only redesign.

Compare real button interactions and generated files against the captured,
hash-verified pre-redesign application. All work stays in temporary folders.
"""

import ast
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
AUDIT = PROJECT / "ui_redesign_20260912_151831"
CURRENT = PROJECT / "Words_SRT.py"
BASELINE = AUDIT / "baseline" / "Words_SRT.py"
MANIFEST = AUDIT / "baseline_contract.json"
VOCABULARY = "apple [ˈæpəl] n. 苹果\nbanana [bəˈnɑːnə] n. 香蕉\npear n. 梨"


def load_application(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setUpModule():
    global baseline, current, qt_app, module_temp, isolation, prior_hook, manifest
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    actual_hash = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    if actual_hash != manifest["source_sha256"]:
        raise AssertionError("Captured pre-redesign source no longer matches its recorded SHA256")
    module_temp = tempfile.TemporaryDirectory(prefix="subtitle_ui_contract_")
    isolation = ExitStack()
    isolation.enter_context(mock.patch.dict(os.environ, {
        "QT_QPA_PLATFORM": "offscreen",
        "LOCALAPPDATA": str(Path(module_temp.name) / "localappdata"),
    }))
    isolation.enter_context(mock.patch.object(sys, "path", [str(PROJECT), *sys.path]))
    prior_hook = sys.excepthook
    baseline = load_application("subtitle_factory_pre_ui", BASELINE)
    current = load_application("subtitle_factory_post_ui", CURRENT)
    qt_app = current.QApplication.instance() or current.QApplication([])


def tearDownModule():
    sys.excepthook = prior_hook
    for handler in list(logging.getLogger("subtitle_factory").handlers):
        handler.close()
        logging.getLogger("subtitle_factory").removeHandler(handler)
    isolation.close()
    module_temp.cleanup()


def button(window, name, original_text):
    found = window.findChild(current.QPushButton, name)
    if found is not None:
        return found
    candidates = [item for item in window.findChildren(current.QPushButton)
                  if item.text().strip() == original_text]
    if len(candidates) != 1:
        raise AssertionError(f"Missing unambiguous control: {name} ({original_text})")
    return candidates[0]


class UIContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=module_temp.name, prefix="case_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.messages = {kind: patches.enter_context(mock.patch.object(current.QMessageBox, kind))
                         for kind in ("information", "warning", "critical")}
        self.startfile = patches.enter_context(mock.patch.object(current.os, "startfile"))
        self.window = current.MainApp()
        self.addCleanup(self.window.close)

    def test_core_and_restored_patch_ast_are_unchanged(self):
        tree = ast.parse(CURRENT.read_text(encoding="utf-8-sig"))
        nodes = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        # The AI interface moves the canonical core into a headless module.
        # Keep every frozen AST hash and verify the GUI actually imports it.
        import subtitle_factory_core
        core_tree = ast.parse(Path(subtitle_factory_core.__file__).read_text(encoding='utf-8'))
        for node in core_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in manifest['protected_ast_sha256']:
                self.assertNotIn(node.name, nodes, 'There must be only one canonical definition')
                self.assertIs(getattr(current, node.name), getattr(subtitle_factory_core, node.name))
                nodes[node.name] = node
        self.assertEqual(subtitle_factory_core._suffixes, baseline._suffixes)
        for name in ('_pos_pattern', '_phone_pattern', '_time_pattern'):
            self.assertEqual(getattr(subtitle_factory_core, name).pattern, getattr(baseline, name).pattern)
            self.assertEqual(getattr(subtitle_factory_core, name).flags, getattr(baseline, name).flags)
        for name, expected in manifest["protected_ast_sha256"].items():
            with self.subTest(function=name):
                self.assertIn(name, nodes)
                actual = hashlib.sha256(ast.dump(nodes[name], include_attributes=False).encode()).hexdigest()
                self.assertEqual(actual, expected, "UI changes must not change core function syntax")
        for owner, name, expected in (
            (current.LogicCore, "parse", current._parse),
            (current.LogicCore, "gen", current._gen),
            (current.LogicCore, "gen_srt", current._gen),
            (current.MainApp, "browse", current._browse),
            (current.MainApp, "loadf", current._loadf),
            (current.MainApp, "upd_info", current._upd_info),
            (current.MainApp, "run", current._run),
        ):
            with self.subTest(runtime_method=name):
                self.assertIs(getattr(owner, name), expected)

    def test_public_widget_contract_and_navigation(self):
        fields = {
            "inp_f": current.QLineEdit, "in_s": current.QLineEdit,
            "in_e": current.QLineEdit, "in_sw": current.QLineEdit,
            "in_ew": current.QLineEdit, "inp_batch": current.QLineEdit,
            "paste_size": current.QLineEdit, "io": current.QLineEdit,
            "tx": current.QTextEdit, "chk_batch": current.QCheckBox,
            "paste_batch": current.QCheckBox, "logger": current.QLabel,
            "infot": current.QLabel, "stack": current.QStackedWidget,
        }
        for name, kind in fields.items():
            with self.subTest(widget=name):
                self.assertIsInstance(getattr(self.window, name), kind)
        self.assertIsInstance(self.window.core, current.LogicCore)
        self.assertEqual(self.window.stack.count(), 2)
        self.window.bm2.click()
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.window.bm1.click()
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertTrue(callable(self.window.stack.widget(1).layout().addLayout))

    def test_browse_load_and_word_range_controls_are_connected(self):
        source = self.root / "wordlist.txt"
        source.write_text(VOCABULARY, encoding="utf-8")
        with mock.patch.object(current.QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            button(self.window, "browse_file_button", "浏览").click()
        self.assertEqual(self.window.inp_f.text(), str(source))
        self.assertEqual([row["w"] for row in self.window.mem_data], ["apple", "banana", "pear"])
        self.window.mem_data = []
        button(self.window, "load_file_button", "加载").click()
        self.assertEqual(len(self.window.mem_data), 3)
        self.window.in_sw.setText("BANANA")
        button(self.window, "range_start_button", "定始").click()
        self.window.in_ew.setText("pear")
        button(self.window, "range_end_button", "定末").click()
        self.assertEqual((self.window.in_s.text(), self.window.in_e.text()), ("2", "3"))
        self.assertEqual([row["w"] for row in current._selection(self.window)], ["banana", "pear"])

    def test_paste_clear_and_output_browse_are_connected(self):
        self.window.bm2.click()
        self.window.tx.setPlainText(VOCABULARY)
        button(self.window, "clear_text_button", "清空").click()
        self.assertEqual(self.window.tx.toPlainText(), "")
        destination = self.root / "chosen_output"
        destination.mkdir()
        with mock.patch.object(current.QFileDialog, "getExistingDirectory", return_value=str(destination)):
            button(self.window, "browse_output_button", "...").click()
        self.assertEqual(self.window.io.text(), str(destination))

    def test_two_batch_controls_remain_bidirectionally_synchronized(self):
        self.assertFalse(self.window.chk_batch.isChecked())
        self.assertFalse(self.window.paste_batch.isChecked())
        self.assertFalse(self.window.inp_batch.isEnabled())
        self.assertFalse(self.window.paste_size.isEnabled())
        self.window.chk_batch.click()
        self.assertTrue(self.window.paste_batch.isChecked())
        self.assertTrue(self.window.inp_batch.isEnabled())
        self.assertTrue(self.window.paste_size.isEnabled())
        self.window.inp_batch.setText("17")
        self.assertEqual(self.window.paste_size.text(), "17")
        self.window.paste_size.setText("3")
        self.assertEqual(self.window.inp_batch.text(), "3")
        self.window.paste_batch.click()
        self.assertFalse(self.window.chk_batch.isChecked())
        self.assertFalse(self.window.inp_batch.isEnabled())
        self.assertFalse(self.window.paste_size.isEnabled())

    def test_both_duration_menus_keep_every_value_and_default(self):
        self.assertEqual((self.window.btn_t1.value, self.window.btn_t2.value), (.4, .2))
        for control in (self.window.btn_t1, self.window.btn_t2):
            actions = control.menu.actions()
            self.assertEqual(len(actions), 6)
            for action, expected in zip(actions, (.2, .25, .4, .5, .6, .8)):
                with self.subTest(control=control, value=expected):
                    action.trigger()
                    self.assertEqual(control.value, expected)
                    self.assertEqual(control.text(), f"{expected}s")

    def exercise(self, module, scenario, name):
        """Drive the unchanged user actions and capture only observable results."""
        output = self.root / name / "output"
        output.parent.mkdir()
        window = module.MainApp()
        self.addCleanup(window.close)
        for message in self.messages.values():
            message.reset_mock()
        self.startfile.reset_mock()
        window.io.setText(str(output) if scenario.get("output", True) else " ")
        text = scenario.get("text", VOCABULARY)
        if scenario.get("mode", "text") == "file":
            source = output.parent / "source.txt"
            source.write_text(text, encoding="utf-8-sig")
            window.inp_f.setText(str(source))
            button(window, "load_file_button", "加载").click()
            if "range" in scenario:
                window.in_s.setText(scenario["range"][0])
                window.in_e.setText(scenario["range"][1])
        else:
            window.bm2.click()
            window.tx.setPlainText(text)
        if "batch" in scenario:
            window.paste_batch.setChecked(True)
            window.paste_size.setText(scenario["batch"])
        if "timing" in scenario:
            window.btn_t1.set_val(scenario["timing"][0])
            window.btn_t2.set_val(scenario["timing"][1])
        sentinel = None
        if scenario.get("conflict"):
            output.mkdir()
            sentinel = output / "apple_Part1_01_英文重复.srt"
            sentinel.write_bytes(b"keep existing user export")
        button(window, "generate_button", "🚀 立即生成").click()
        qt_app.processEvents()
        self.messages["critical"].assert_not_called()
        self.assertFalse(window._generating)
        destination = Path(window.last_output_dir) if hasattr(window, "last_output_dir") else None
        files = {path.name: path.read_bytes() for path in destination.glob("*.srt")} if destination else {}
        messages = {}
        for kind, capture in self.messages.items():
            result = []
            for call in capture.call_args_list:
                title, content = call.args[1:3]
                if destination:
                    content = content.replace(str(destination), "<destination>")
                content = content.replace(str(output.parent), "<case>")
                result.append((title, content))
            messages[kind] = result
        if sentinel:
            self.assertEqual(sentinel.read_bytes(), b"keep existing user export")
            self.assertNotEqual(destination, output)
            self.assertEqual(destination.parent, output)
        return {
            "files": files, "messages": messages,
            "words": window.mem_data if scenario.get("mode") == "file" else None,
            "opened_output": self.startfile.call_count,
            "has_output_folder": output.exists(),
            "partial_output_files": sorted(path.name for path in output.rglob("*.srt")) if not destination else [],
        }

    def test_real_generation_matches_baseline_bytes_for_positive_inputs(self):
        scenarios = (
            ("ordinary", {}),
            ("no_phonetic_smart_apostrophe", {"text": "don’t v. 不要\napple n. 苹果"}),
            ("timing_long_meaning", {"text": "excellent adj. 极其出色非常优秀\nhello", "timing": (.25, .8)}),
            ("text_packages", {"batch": "2"}),
            ("source_range", {"mode": "file", "range": ("2", "2")}),
            ("source_packages", {"mode": "file", "batch": "1"}),
            ("output_conflict", {"batch": "2", "conflict": True}),
            ("srt_paste", {"text": "1\n00:00:00,000 --> 00:00:02,000\napple\n[ˈæpəl]\nn. 苹果\n"}),
        )
        for name, scenario in scenarios:
            with self.subTest(scenario=name):
                before = self.exercise(baseline, scenario, name + "_before")
                after = self.exercise(current, scenario, name + "_after")
                self.assertTrue(before["files"], "A positive baseline case must produce outputs")
                self.assertEqual(after, before)

    def test_real_generation_matches_baseline_for_invalid_inputs(self):
        scenarios = (
            ("empty_text", {"text": ""}),
            ("invalid_text", {"text": "纯中文无法构成单词"}),
            ("empty_output", {"output": False}),
            ("zero_package", {"batch": "0"}),
            ("negative_package", {"batch": "-2"}),
            ("invalid_package", {"batch": "1.5"}),
            ("invalid_range", {"mode": "file", "range": ("0", "2")}),
            ("backwards_range", {"mode": "file", "range": ("3", "1")}),
            ("invalid_timing", {"timing": (0, .2)}),
        )
        for name, scenario in scenarios:
            with self.subTest(scenario=name):
                before = self.exercise(baseline, scenario, name + "_before")
                after = self.exercise(current, scenario, name + "_after")
                self.assertTrue(before["messages"]["warning"], "An invalid baseline case must report failure")
                self.assertEqual(before["files"], {})
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
