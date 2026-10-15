"""Compatibility exports for the namespaced audio pipeline."""

import sys

from universalvc_service import audio_pipeline as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
if __name__ == "audio_pipeline":
    # Preserve module-global monkeypatches used by legacy integrations/tests.
    sys.modules[__name__] = _implementation
