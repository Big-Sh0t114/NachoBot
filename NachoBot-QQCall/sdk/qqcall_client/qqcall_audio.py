"""Compatibility exports for the optional QQCall PCM transport package.

The audio implementation lives under ``NachoBot-QQCall/qqcall_pcm`` so shared
adapter and Core modules do not own or import the transport during control-only
startup. Import this module only when the QQCall audio extra is enabled.
"""

from qqcall_pcm import (
    AudioDeviceInfo,
    CORE_CHANNELS,
    CORE_SAMPLE_RATE,
    PCM_CODEC,
    QQCallAudioBackend,
    QQCallAudioConfig,
    QQCallAudioDeviceScope,
    QQCallAudioError,
    SAMPLE_WIDTH,
)

__all__ = [
    "AudioDeviceInfo",
    "CORE_CHANNELS",
    "CORE_SAMPLE_RATE",
    "PCM_CODEC",
    "QQCallAudioBackend",
    "QQCallAudioConfig",
    "QQCallAudioDeviceScope",
    "QQCallAudioError",
    "SAMPLE_WIDTH",
]
