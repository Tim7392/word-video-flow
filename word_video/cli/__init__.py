"""JSON CLI package: ``python -m word_video.cli <action>``.

One action, one JSON object on stdout, structured errors, no dialogs; see
:mod:`word_video.cli.main` for the contract.
"""
from .main import ACTIONS, SCHEMA, main

__all__ = ['ACTIONS', 'SCHEMA', 'main']
