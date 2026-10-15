"""Narrow compatibility helpers for the pinned Pycord voice client."""

from __future__ import annotations


def stop_playback_only(voice_client) -> None:
    """Stop Pycord playback while preserving its active receive reader.

    The pinned Pycord ``VoiceClient.stop`` stops both ``_player`` and
    ``_reader``. The adapter uses this mirror of its playback cleanup where it
    must interrupt TTS/music without ending DAVE voice receive.
    """
    player = getattr(voice_client, "_player", None)
    if player is not None:
        player.stop()

    player_future = getattr(voice_client, "_player_future", None)
    if player_future is not None:
        voice_client.loop.call_soon_threadsafe(
            voice_client._set_future_result_if_pending,
            player_future,
            None,
        )

    voice_client._player = None
    voice_client._player_future = None
