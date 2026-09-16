"""Desktop surface of the editor: the preview canvas and the audio device it runs on.

Only W04 lives here so far - the independent canvas (``preview_canvas``) and the
Qt PCM sink that the preview's clock is derived from (``audio_qt``).  The editor
shell itself is W06 and will host these rather than replace them.

Qt is confined to this package on purpose.  ``preview/`` is the engine and stays
importable without Qt so it can be unit-tested headlessly and so the packaged
headless CLI keeps excluding PySide6 (see ``packaging/build_member_package.py``).
"""
from .audio_qt import QtAudioOutput
from .preview_canvas import PreviewCanvas

__all__ = ['PreviewCanvas', 'QtAudioOutput']
