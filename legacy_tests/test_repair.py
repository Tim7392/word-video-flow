r"""Behavior regressions for the repaired desktop subtitle factory.

Run with: .venv\Scripts\python.exe -B -m unittest discover -s tests -v
All input, output and application logs stay in temporary directories.  No
desktop folders are opened, and message boxes are replaced with mocks.
"""

import importlib.util
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "Words_SRT.py"
VOCABULARY = "apple [ˈæpəl] n. 苹果\nbanana [bəˈnɑːnə] n. 香蕉"
EXPECTED = [
    {"w": "apple", "p": "[ˈæpəl]", "d_f": "n. 苹果", "d_c": "苹果"},
    {"w": "banana", "p": "[bəˈnɑːnə]", "d_f": "n. 香蕉", "d_c": "香蕉"},
]


def setUpModule():
    global factory, qt_app, module_temp, environment, previous_hook
    module_temp = tempfile.TemporaryDirectory(prefix="subtitle_factory_tests_")
    environment = mock.patch.dict(os.environ, {
        "QT_QPA_PLATFORM": "offscreen",
        "LOCALAPPDATA": str(Path(module_temp.name) / "localappdata"),
    })
    environment.start()
    previous_hook = sys.excepthook
    spec = importlib.util.spec_from_file_location("subtitle_factory_under_test", SOURCE)
    factory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(factory)
    qt_app = factory.QApplication.instance() or factory.QApplication([])


def tearDownModule():
    sys.excepthook = previous_hook
    for handler in list(logging.getLogger("subtitle_factory").handlers):
        handler.close()
        logging.getLogger("subtitle_factory").removeHandler(handler)
    environment.stop()
    module_temp.cleanup()


def read_cues(path):
    """Read externally visible SRT cues without using the generator's helpers."""
    content = path.read_text(encoding="utf-8-sig").strip()
    cues = []
    for block in re.split(r"\n\s*\n", content):
        lines = block.splitlines()
        start, end = lines[1].split(" --> ")
        cues.append((int(lines[0]), start, end, "\n".join(lines[2:])))
    return cues


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=module_temp.name, prefix="case_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.core = factory.LogicCore()

    def write_input(self, name, content=VOCABULARY, encoding="utf-8"):
        path = self.root / name
        path.write_text(content, encoding=encoding)
        return path


class ParsingTests(IsolatedCase):
    def test_missing_phonetic_does_not_swallow_part_of_speech(self):
        data, errors = self.core.parse(txt="1. apple n. 苹果\n2. turn off vt. 关闭")
        self.assertEqual(errors, [])
        self.assertEqual(data, [
            {"w": "apple", "p": "", "d_f": "n. 苹果", "d_c": "苹果"},
            {"w": "turn off", "p": "", "d_f": "vt. 关闭", "d_c": "关闭"},
        ])

    def test_smart_apostrophe_is_preserved(self):
        data, errors = self.core.parse(txt="don’t v. 不要\nmother-in-law n. 岳母")
        self.assertEqual(errors, [])
        self.assertEqual([item["w"] for item in data], ["don’t", "mother-in-law"])
        self.assertEqual([item["d_c"] for item in data], ["不要", "岳母"])

    def test_txt_supported_encodings(self):
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            with self.subTest(encoding=encoding):
                path = self.write_input(encoding + ".txt", "apple n. 苹果", encoding)
                self.assertEqual(self.core.parse(path=str(path)), ([{
                    "w": "apple", "p": "", "d_f": "n. 苹果", "d_c": "苹果",
                }], []))

    def test_docx_table_and_paragraph_read_in_document_order(self):
        document = factory.docx.Document()
        document.add_paragraph("apple [ˈæpəl] n. 苹果")
        table = document.add_table(rows=1, cols=3)
        for cell, value in zip(table.rows[0].cells, ("banana", "[bəˈnɑːnə]", "n. 香蕉")):
            cell.text = value
        document.add_paragraph("pear n. 梨")
        path = self.root / "table.docx"
        document.save(path)
        data, errors = self.core.parse(path=str(path))
        self.assertEqual(errors, [])
        self.assertEqual(data[:2], EXPECTED)
        self.assertEqual(data[2], {"w": "pear", "p": "", "d_f": "n. 梨", "d_c": "梨"})

    def test_srt_multiline_word_entries_ignore_timestamps_and_markup(self):
        path = self.write_input("words.srt", (
            "1\n00:00:00,000 --> 00:00:02,800\n<b>apple</b>\n[ˈæpəl]\nn. 苹果\n\n"
            "2\n00:00:02,800 --> 00:00:05,600\nbanana [bəˈnɑːnə] n. 香蕉\n"
        ), "utf-8-sig")
        self.assertEqual(self.core.parse(path=str(path)), (EXPECTED, []))


class ExportTests(IsolatedCase):
    def test_all_five_tracks_have_correct_content_numbering_and_millisecond_times(self):
        destination, packages, files = factory._generate_packages(
            self.core, EXPECTED, str(self.root / "output"), .4, .2, None,
        )
        self.assertEqual((packages, files), (1, 5))
        expected_tracks = {
            "apple_01_英文重复.srt": [
                (1, "00:00:00,000", "00:00:01,000", "apple"),
                (2, "00:00:01,000", "00:00:02,000", "apple"),
                (3, "00:00:02,800", "00:00:03,800", "banana"),
                (4, "00:00:03,800", "00:00:04,800", "banana"),
            ],
            "apple_02_英文单次.srt": [
                (1, "00:00:00,000", "00:00:02,800", "apple"),
                (2, "00:00:02,800", "00:00:05,600", "banana"),
            ],
            "apple_03_音标.srt": [
                (1, "00:00:01,000", "00:00:02,800", "[ˈæpəl]"),
                (2, "00:00:03,800", "00:00:05,600", "[bəˈnɑːnə]"),
            ],
            "apple_04_中文带词性.srt": [
                (1, "00:00:02,000", "00:00:02,800", "n. 苹果"),
                (2, "00:00:04,800", "00:00:05,600", "n. 香蕉"),
            ],
            "apple_05_中文无词性.srt": [
                (1, "00:00:02,000", "00:00:02,800", "苹果"),
                (2, "00:00:04,800", "00:00:05,600", "香蕉"),
            ],
        }
        self.assertEqual({p.name for p in destination.iterdir()}, set(expected_tracks))
        for name, cues in expected_tracks.items():
            with self.subTest(track=name):
                self.assertEqual(read_cues(destination / name), cues)
                self.assertTrue((destination / name).read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_conflicting_export_preserves_existing_file_and_uses_new_folder(self):
        output = self.root / "output"
        output.mkdir()
        original = output / "apple_01_英文重复.srt"
        original.write_bytes(b"existing user export")
        destination, _, count = factory._generate_packages(
            self.core, EXPECTED, str(output), .4, .2, None,
        )
        self.assertEqual(original.read_bytes(), b"existing user export")
        self.assertNotEqual(destination, output)
        self.assertEqual(destination.parent, output)
        self.assertEqual(count, 5)
        self.assertEqual(len(list(destination.glob("*.srt"))), 5)
        self.assertEqual({p.name for p in output.iterdir()}, {original.name, destination.name})

    def test_second_package_failure_leaves_no_partial_export(self):
        output = self.root / "output"
        original_gen = self.core.gen

        def fail_second_package(data, folder, t1, t2, prefix=""):
            if prefix == "_Part2":
                raise OSError("simulated generation failure")
            return original_gen(data, folder, t1, t2, prefix)

        with mock.patch.object(self.core, "gen", side_effect=fail_second_package):
            with self.assertRaisesRegex(OSError, "simulated generation failure"):
                factory._generate_packages(self.core, EXPECTED, str(output), .4, .2, 1)
        self.assertEqual(list(output.iterdir()), [])

    def test_publish_failure_rolls_back_files_already_published(self):
        output = self.root / "output"
        path_open = Path.open

        def fail_second_publish(path, mode="r", *args, **kwargs):
            if mode == "xb" and path.name == "apple_02_英文单次.srt":
                raise OSError("simulated destination write failure")
            return path_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=fail_second_publish):
            with self.assertRaisesRegex(OSError, "simulated destination write failure"):
                factory._generate_packages(self.core, EXPECTED, str(output), .4, .2, None)
        self.assertEqual(list(output.iterdir()), [])

    def test_invalid_timing_is_rejected_without_outputs(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(timing=value):
                output = self.root / "output"
                with self.assertRaises(ValueError):
                    factory._generate_packages(self.core, EXPECTED, str(output), value, .2, None)
                self.assertFalse(output.exists())


class InterfaceTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        for name, owner in (("information", factory.QMessageBox),
                            ("warning", factory.QMessageBox),
                            ("critical", factory.QMessageBox),
                            ("startfile", factory.os)):
            patcher = mock.patch.object(owner, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.window = factory.MainApp()
        self.addCleanup(self.window.close)
        self.output = self.root / "output"
        self.window.io.setText(str(self.output))
        self.button = self.window.findChild(factory.QPushButton, "generate_button")
        self.assertIsNotNone(self.button, "The actual generate button must be discoverable")

    def prepare_file(self):
        path = self.write_input("words.txt")
        self.window.inp_f.setText(str(path))
        self.assertTrue(self.window.loadf())
        return path

    def prepare_text(self):
        self.window.sw(1)
        self.window.tx.setPlainText(VOCABULARY)

    def click_generate(self):
        self.button.click()
        qt_app.processEvents()
        self.critical.assert_not_called()
        self.assertFalse(self.window._generating)

    def test_single_word_range_is_valid_and_generates_only_selected_word(self):
        self.prepare_file()
        self.window.in_s.setText("2")
        self.window.in_e.setText("2")
        self.assertEqual(factory._selection(self.window), EXPECTED[1:])
        self.assertIn("已选：1 个", self.window.infot.text())
        self.click_generate()
        self.information.assert_called_once()
        self.warning.assert_not_called()
        cues = read_cues(self.output / "banana_02_英文单次.srt")
        self.assertEqual(cues, [(1, "00:00:00,000", "00:00:02,800", "banana")])

    def test_invalid_ranges_are_reported_without_export(self):
        self.prepare_file()
        for start, end in (("0", "2"), ("1", "3"), ("2", "1"), ("-1", "2"), ("x", "2")):
            with self.subTest(start=start, end=end):
                self.warning.reset_mock()
                self.window.in_s.setText(start)
                self.window.in_e.setText(end)
                with self.assertRaises(ValueError):
                    factory._selection(self.window)
                self.click_generate()
                self.warning.assert_called_once()
                self.assertFalse(self.output.exists())
        self.information.assert_not_called()
        self.startfile.assert_not_called()

    def test_real_button_generates_text_packages_using_visible_paste_controls(self):
        self.prepare_text()
        self.window.paste_batch.setChecked(True)
        self.window.paste_size.setText("1")
        self.assertTrue(self.window.chk_batch.isChecked())
        self.assertEqual(self.window.inp_batch.text(), "1")
        self.click_generate()
        self.information.assert_called_once()
        self.warning.assert_not_called()
        self.assertEqual(len(list(self.output.glob("*.srt"))), 10)
        self.assertEqual(read_cues(self.output / "apple_Part1_02_英文单次.srt"), [
            (1, "00:00:00,000", "00:00:02,800", "apple"),
        ])
        self.assertEqual(read_cues(self.output / "banana_Part2_02_英文单次.srt"), [
            (1, "00:00:00,000", "00:00:02,800", "banana"),
        ])
        self.startfile.assert_called_once_with(str(self.output))

    def test_empty_output_is_caught_by_ui(self):
        self.prepare_text()
        self.window.io.setText("   ")
        self.click_generate()
        self.warning.assert_called_once()
        self.assertIn("输出文件夹", self.warning.call_args.args[2])
        self.information.assert_not_called()
        self.startfile.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_zero_negative_and_noninteger_package_sizes_are_caught_by_ui(self):
        self.prepare_text()
        self.window.paste_batch.setChecked(True)
        for size in ("0", "-1", "1.5", "abc"):
            with self.subTest(size=size):
                self.warning.reset_mock()
                self.window.paste_size.setText(size)
                self.click_generate()
                self.warning.assert_called_once()
                self.assertIn("大于 0 的整数", self.warning.call_args.args[2])
                self.assertFalse(self.output.exists())
        self.information.assert_not_called()
        self.startfile.assert_not_called()

    def test_generation_error_is_caught_and_ui_can_retry(self):
        self.prepare_text()
        with mock.patch.object(self.window.core, "gen", side_effect=OSError("simulated disk failure")):
            self.click_generate()
        self.warning.assert_called_once()
        self.assertIn("simulated disk failure", self.warning.call_args.args[2])
        self.assertEqual(list(self.output.iterdir()), [])
        self.information.assert_not_called()
        self.click_generate()
        self.information.assert_called_once()
        self.assertEqual(len(list(self.output.glob("*.srt"))), 5)

    def test_file_path_change_invalidates_cached_words_and_loads_new_file(self):
        self.prepare_file()
        replacement = self.write_input("replacement.txt", "pear n. 梨")
        self.window.inp_f.setText(str(replacement))
        self.assertEqual(self.window.mem_data, [])
        self.click_generate()
        self.information.assert_called_once()
        self.warning.assert_not_called()
        self.assertEqual(self.window.mem_data[0]["w"], "pear")
        self.assertEqual(read_cues(self.output / "pear_02_英文单次.srt"), [
            (1, "00:00:00,000", "00:00:02,500", "pear"),
        ])
        self.assertFalse(list(self.output.glob("apple*.srt")))

    def test_same_path_file_edit_reloads_before_generation(self):
        path = self.prepare_file()
        prior_stat = path.stat()
        path.write_text("pear n. 梨", encoding="utf-8")
        os.utime(path, ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns + 2_000_000_000))
        self.click_generate()
        self.information.assert_called_once()
        self.warning.assert_not_called()
        self.assertEqual([item["w"] for item in self.window.mem_data], ["pear"])
        self.assertTrue((self.output / "pear_02_英文单次.srt").is_file())
        self.assertFalse(list(self.output.glob("apple*.srt")))

    def test_deleted_source_does_not_export_cached_words(self):
        path = self.prepare_file()
        path.unlink()
        self.click_generate()
        self.warning.assert_called_once()
        self.information.assert_not_called()
        self.startfile.assert_not_called()
        self.assertEqual(self.window.mem_data, [])
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
