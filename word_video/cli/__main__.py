"""``python -m word_video.cli`` -- the Agent-facing entry point."""
import sys

from .main import main

if __name__ == '__main__':
    raise SystemExit(main())
