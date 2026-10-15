"""Compatibility imports for QQCall's UniversalVC public-API binding.

This module deliberately contains no VAD, ASR streaming, WAV, or playback
implementation. QQCall owns only the optional PCM device transport package.
"""

from .qqcall_universal_api import (
    TRUSTED_INVITE_CORRELATIONS,
    QQCallAudioTurn,
    QQCallRouteProof,
    QQCallVoiceError,
    QQCallVoiceRuntime,
    is_trusted_invite_correlation,
)


__all__ = [
    "TRUSTED_INVITE_CORRELATIONS",
    "QQCallAudioTurn",
    "QQCallRouteProof",
    "QQCallVoiceError",
    "QQCallVoiceRuntime",
    "is_trusted_invite_correlation",
]
