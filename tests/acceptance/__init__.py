"""Acceptance tools: one batch, one archive, the fault samples and the matrix.

Importing this package must stay cheap and side-effect free - it does **not**
import the tools themselves, because ``accept_range``/``make_faults`` locate
ffmpeg/ffprobe at import time and module-scope work would break the fast tests.
Import the module you need explicitly::

    from acceptance import accept_range
    from acceptance import make_faults
"""
