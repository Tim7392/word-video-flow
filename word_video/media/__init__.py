"""Local media: IO, the intro's sound policy, mixing, proxies and stream facts.

The package is split by question rather than by layer:

``core``
    process/file primitives and the tempo pass: ``run``, ``probe``,
    ``prepare_audio``, ``atomic_json``.
``intro``
    the single owner of "which file sounds during the intro".
``audio``
    sample-accurate assembly with bounded overlap and per-clip gain.
``streams``
    what a file really contains: sample counts and presentation timestamps.
``proxy``
    derived lower-resolution copies for work that does not need the original.

No subprocess is started with a shell string, and no media URL is ever fetched.
"""
from .core import *          # noqa: F401,F403 - the historical word_video.media API
from .core import __all__ as _core_all
from .audio import *         # noqa: F401,F403
from .audio import __all__ as _audio_all
from .intro import *         # noqa: F401,F403
from .intro import __all__ as _intro_all
from .proxy import *         # noqa: F401,F403
from .proxy import __all__ as _proxy_all
from .streams import *       # noqa: F401,F403
from .streams import __all__ as _streams_all

__all__ = list(_core_all) + list(_audio_all) + list(_intro_all) + list(_proxy_all) \
    + list(_streams_all)
