import base64
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from word_video.draft import _load_library
from word_video.media import prepare_audio, duration, run, executable
from word_video.tts import read_sse, synthesize


class MediaTests(unittest.TestCase):
    def test_headless_import(self):
        _load_library()
        self.assertFalse(any(n.startswith(('uiautomation','pyautogui')) or n.endswith('jianying_controller') for n in sys.modules))

    def test_speed_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'中文.wav'; out=Path(directory)/'prepared.wav'
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','sine=frequency=440:duration=1','-y',source])
            raw,prepared=prepare_audio(source,out,1.25)
            self.assertAlmostEqual(raw,1,places=2)
            self.assertAlmostEqual(prepared,50/60,places=3)
            with self.assertRaises(FileExistsError): prepare_audio(source,out,1.25)

    def test_sse_failure_never_returns_partial(self):
        good='data: '+json.dumps({'code':0,'data':base64.b64encode(b'audio').decode()})
        self.assertEqual(read_sse([good]),b'audio')
        with self.assertRaises(RuntimeError): read_sse([good,'data: {"code":500}'])
        with self.assertRaises(RuntimeError): read_sse(['event: done'])
        with self.assertRaises(ValueError): read_sse(['data: invalid'])

    def test_missing_key_no_request(self):
        """The variable must be the one the implementation reads.

        It reads ``VOLC_TTS_API_KEY`` (word_video.tts._api_key).  Clearing only
        ``MODEL_SPEECH_API_KEY`` left a real host key visible and the test sent a
        genuine request from a unit run; see tests/test_offline_guard.py.
        """
        with patch.dict('os.environ', {'VOLC_TTS_API_KEY': '',
                                       'MODEL_SPEECH_API_KEY': ''}), \
                patch('word_video.tts.urllib.request.urlopen') as network:
            with self.assertRaises(PermissionError):
                synthesize('hello', 'test', 'unused.mp3')
            network.assert_not_called()

if __name__=='__main__': unittest.main()
