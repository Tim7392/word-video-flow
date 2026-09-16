"""Proofs that the default suite cannot spend real TTS quota.

The defect this guards against (M0 finding, 2026-09-16): the missing-key test
cleared ``MODEL_SPEECH_API_KEY`` while ``word_video.tts`` reads
``VOLC_TTS_API_KEY``, so on a configured machine the test issued a real request.
"""
import os
import socket
import unittest
from unittest.mock import patch

from word_video.tts import synthesize


class OfflineGuardTests(unittest.TestCase):
    def test_default_suite_cannot_reach_a_remote_host(self):
        """The guard itself must be installed, or the rest is decoration."""
        with self.assertRaises(RuntimeError) as caught:
            socket.create_connection(('203.0.113.9', 9), timeout=0.1)
        self.assertIn('OFFLINE_TEST_NETWORK_BLOCKED', str(caught.exception))

    def test_missing_api_key_is_refused_before_any_request(self):
        """Clear the variable the implementation reads, not an older name."""
        with patch.dict(os.environ, {'MODEL_SPEECH_API_KEY': '',
                                     'VOLC_TTS_API_KEY': ''}), \
                patch('word_video.tts.urllib.request.urlopen') as network:
            with self.assertRaises(PermissionError):
                synthesize('hello', 'test', 'unused.mp3')
            network.assert_not_called()

    def test_host_key_still_never_touches_the_network(self):
        """A machine that really has a key must not fire requests from unit tests.

        The transport is mocked, so a real call is impossible; what is asserted is
        that the code goes through the mock (call_count == 1) instead of some other
        path, and that the request never leaves the process.
        """
        with patch.dict(os.environ, {'VOLC_TTS_API_KEY': 'dummy-key-for-test'}), \
                patch('word_video.tts.urllib.request.urlopen',
                      side_effect=RuntimeError('transport must be mocked')) as network:
            with self.assertRaises(RuntimeError) as caught:
                synthesize('hello', 'test', 'unused.mp3')
            self.assertIn('transport must be mocked', str(caught.exception))
            self.assertEqual(network.call_count, 1)
            request = network.call_args.args[0]
            self.assertEqual('dummy-key-for-test', request.headers['X-api-key'])


if __name__ == '__main__':
    unittest.main()
