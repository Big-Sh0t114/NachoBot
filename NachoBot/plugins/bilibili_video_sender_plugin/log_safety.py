"""Side-effect-free logging helpers for the Bilibili video sender plugin."""

from __future__ import annotations


def video_link_log_fields(url: str, detection_source: str, *, video_id: str | None = None) -> dict[str, str]:
    """Return useful fields without exposing a URL sourced from a QQ card."""

    fields = {"source": detection_source}
    if detection_source == "qq_card":
        if video_id:
            fields["video_id"] = video_id
        return fields
    fields["url"] = url
    return fields
