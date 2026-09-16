"""Make `tests/acceptance` importable, offline, and keep default paths in one place.

The acceptance tools are standalone scripts (they must run from a shell without
importing the production pipeline), so the package is reached through this
directory's parent rather than through pytest's rootdir.  Nothing here or in the
acceptance modules opens a socket; the tools only read local files and call
ffmpeg/ffprobe.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Read-only archive of the delivered batches (never written to), the fault tree
# the matrix judges (built from hard links into that archive) and the read-only
# word list the expectations come from.  All three sit outside the repository.
DEFAULT_GOOD_BATCH = Path(
    r'D:\单词速记自动化_测试归档_0915\范围151-200-切片渲染'
    r'\wv-b349c24dbf19ce65bc4e5422\0151-0200')
DEFAULT_WORDLIST = Path(
    r'D:\1\1-AI_workflow\word_video_flow\data\wordlists\四级核心1500词_已清理.txt')
DEFAULT_FAULTS = Path(
    r'D:\1\1-AI_workflow\word_video_flow\runtime\tmp\QA\faults')


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'acceptance_media: needs the real delivered batch and the fault tree; '
        'runs the full validator over real media, not a mock')
