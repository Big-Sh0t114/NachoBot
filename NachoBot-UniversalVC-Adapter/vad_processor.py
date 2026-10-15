"""Compatibility exports for the namespaced VAD processor."""

from universalvc_service import vad_processor as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
