"""Compatibility exports for the namespaced UniversalVC configuration types."""

from universalvc_service import config as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
