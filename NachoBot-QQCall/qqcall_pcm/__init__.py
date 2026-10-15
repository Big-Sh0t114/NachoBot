"""QQCall's bounded PCM transport for its verified virtual endpoints."""

from .backend import (
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
