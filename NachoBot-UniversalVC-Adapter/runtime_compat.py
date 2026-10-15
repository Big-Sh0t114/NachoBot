"""Compatibility exports for the namespaced runtime loader."""

from universalvc_service import runtime_compat as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
