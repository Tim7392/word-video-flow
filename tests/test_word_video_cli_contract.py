"""Black-box CLI contract (M0, 2026-09-16).

Findings this pins down:
  * ``schema`` listed 12 of the 14 actions the parser actually registered, so an
    agent that trusted the schema could not discover ``doctor``/``schema``;
  * a bad action exited 2 with a usage line on stderr and *no* JSON on stdout,
    which breaks the "stdout is one JSON object" contract.

Both are checked against the real process, not against the function that builds
the answer.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / 'word_video_cli.py'


def run_cli(*args):
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True,
                          cwd=str(PROJECT), timeout=120)


class CliContractTests(unittest.TestCase):
    def test_schema_lists_every_registered_action(self):
        """The help text is the ground truth for what the parser accepts."""
        helped = run_cli('--help')
        text = helped.stdout.decode('utf-8', 'replace')
        start = text.find('{')
        end = text.find('}', start)
        self.assertGreater(start, -1, text)
        registered = set(text[start + 1:end].replace('\n', '').split(','))
        self.assertTrue(registered, 'no subcommands found in --help')
        schema = json.loads(run_cli('schema').stdout.decode('utf-8'))
        self.assertTrue(schema['ok'])
        self.assertEqual(registered, set(schema['result']['actions']))

    def test_unknown_action_prints_json_and_exits_2(self):
        result = run_cli('definitely-not-an-action')
        self.assertEqual(2, result.returncode)
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertEqual('ArgumentError', payload['error']['type'])
        self.assertIn('definitely-not-an-action', payload['error']['message'])

    def test_missing_required_argument_prints_json(self):
        result = run_cli('work')          # --job is required
        self.assertEqual(2, result.returncode)
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertIn('--job', payload['error']['message'])


if __name__ == '__main__':
    unittest.main()
