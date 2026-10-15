"""Compatibility exports for the namespaced capture components."""

from universalvc_service import audio_capture as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
