"""Compatibility exports for the namespaced denoiser."""

from universalvc_service import denoise as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
