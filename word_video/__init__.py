"""Headless word-learning video and editable-draft production.

Package map: :mod:`word_video.domain` (project, time, derived plans),
:mod:`word_video.application` (edit commands, sessions, template expansion),
:mod:`word_video.storage` (the authoritative ``project.json``).
"""

from . import application, domain, storage

__version__ = '0.1.0'
__all__ = ['application', 'domain', 'storage']
