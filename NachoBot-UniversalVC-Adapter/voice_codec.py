"""Compatibility exports for the namespaced voice codec."""

from universalvc_service import voice_codec as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
