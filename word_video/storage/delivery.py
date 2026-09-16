"""Read the delivery sidecar the editor writes (``delivery.json``).

The background and the codec belong to the *delivery*, not to the lesson, and the
editor owns that file (``desktop/editor_project.py``).  The CLI must honour it: a
member who set a background in the editor should not have to pass the same path
again on the command line, and an Agent that plans a batch should see the delivery
the editor would render.

This module is deliberately tiny and read-only, and it imports nothing from
``desktop`` — the CLI has to work without the GUI package at all.  It is also
tolerant on purpose: the editor may add fields this build does not know, and a
delivery it cannot read must degrade to "no background found" (a question with a
fix) rather than break every batch on the machine.
"""
from pathlib import Path
import json

DELIVERY_FILENAME = 'delivery.json'
DELIVERY_SCHEMA = 'wv-delivery@1'
BACKGROUND_KEYS = ('background',)


def delivery_path(folder):
    return Path(folder) / DELIVERY_FILENAME


def delivery_settings(folder):
    """``{background, video_codec}`` from the sidecar, or empty defaults.

    A missing, unreadable, wrongly versioned or non-object document yields no
    settings: the caller then asks for what it still needs instead of guessing.
    """
    empty = {'background': '', 'video_codec': ''}
    path = delivery_path(folder)
    if not path.is_file():
        return dict(empty)
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(empty)
    if not isinstance(document, dict) or document.get('schema') != DELIVERY_SCHEMA:
        return dict(empty)
    settings = dict(empty)
    for key in ('background', 'video_codec'):
        value = document.get(key)
        if isinstance(value, str):
            settings[key] = value.strip()
    return settings
