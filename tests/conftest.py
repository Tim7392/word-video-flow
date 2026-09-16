"""Default regression suite must be offline (M0, 2026-09-16).

Reason: a missing-key test cleared ``MODEL_SPEECH_API_KEY`` while the
implementation reads ``VOLC_TTS_API_KEY``.  On a machine whose environment has a
real key, that unit test therefore sent a genuine request to the TTS service
before failing.  Two things now prevent a repeat:

  * every test clears the variable the implementation actually reads;
  * this hook blocks any real connection to a non-loopback address, so a test
    that forgets to mock its transport fails loudly instead of spending quota.

Service integration tests are opt-in: set ``WORD_VIDEO_ALLOW_NETWORK=1``.
Nothing here touches user-level or system environment; the block lives in the
test process only.

QA addition (task QA-1): the tests directory is put on ``sys.path`` so
``from acceptance import ...`` works no matter what pytest was pointed at, and
the ``acceptance_media`` marker is registered here as well as in the acceptance
package (pytest only reads conftest files it walks, and a run may name a single
test file).
"""
import os
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ALLOWED = os.environ.get('WORD_VIDEO_ALLOW_NETWORK') == '1'
BLOCKED_MESSAGE = 'OFFLINE_TEST_NETWORK_BLOCKED'
LOOPBACK = {'127.0.0.1', '::1', 'localhost', '', None}


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'acceptance_media: needs the real delivered batch and the fault tree; '
        'runs the full validator over real media, not a mock')


def _is_external(address):
    host = address[0] if isinstance(address, (tuple, list)) and address else address
    return host not in LOOPBACK


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _connect(self, address):
    if not ALLOWED and _is_external(address):
        raise RuntimeError('%s: refusing to connect to %r in the default test run'
                           % (BLOCKED_MESSAGE, address))
    return _real_connect(self, address)


def _connect_ex(self, address):
    if not ALLOWED and _is_external(address):
        raise RuntimeError('%s: refusing to connect to %r in the default test run'
                           % (BLOCKED_MESSAGE, address))
    return _real_connect_ex(self, address)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
