"""Compatibility exports for the namespaced speaker tracker."""

from universalvc_service import speaker_tracker as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
