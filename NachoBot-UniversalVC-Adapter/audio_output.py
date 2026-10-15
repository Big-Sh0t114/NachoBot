"""Compatibility exports for the namespaced output components."""

import sys

from universalvc_service import audio_output as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
if __name__ == "audio_output":
    sys.modules[__name__] = _implementation
