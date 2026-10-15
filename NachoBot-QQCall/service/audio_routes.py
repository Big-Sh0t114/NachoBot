"""Pure resolution of the audio endpoints pinned by the native route guard."""

from __future__ import annotations

import re
from typing import Any

MAX_ACTIVE_ENDPOINTS = 512
MAX_ENDPOINT_ID_CHARS = 1024
MAX_ENDPOINT_NAME_CHARS = 256

_CAPTURE_SUFFIX = re.compile(r"\s+(?:\(loopback\)|\[loopback\])$", re.IGNORECASE)
_VIRTUAL_MARKERS = (
    "virtual audio cable",
    "vb-audio",
    "voicemeeter",
    "steelseries sonar",
    " sonar",
    "mixline",
    "steam streaming",
    "oculus virtual audio",
)


class AudioRouteError(ValueError):
    """A sanitized route-resolution failure with a stable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _bounded_text(value: Any, maximum: int, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise AudioRouteError(code)
    return value


def _endpoint_inventory(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_ACTIVE_ENDPOINTS:
        raise AudioRouteError("audio_endpoint_inventory_invalid")
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for endpoint in value:
        if not isinstance(endpoint, dict) or set(endpoint) != {"id", "name", "flow"}:
            raise AudioRouteError("audio_endpoint_inventory_invalid")
        endpoint_id = _bounded_text(endpoint["id"], MAX_ENDPOINT_ID_CHARS, "audio_endpoint_metadata_invalid")
        name = _bounded_text(endpoint["name"], MAX_ENDPOINT_NAME_CHARS, "audio_endpoint_metadata_invalid")
        if not name.strip():
            raise AudioRouteError("audio_endpoint_metadata_invalid")
        flow = endpoint["flow"]
        if type(flow) is not str or flow not in ("capture", "render"):
            raise AudioRouteError("audio_endpoint_metadata_invalid")
        folded_id = endpoint_id.casefold()
        if folded_id in seen_ids:
            raise AudioRouteError("audio_endpoint_id_ambiguous")
        seen_ids.add(folded_id)
        result.append({"id": endpoint_id, "name": name.strip(), "flow": flow})
    return result


def _by_id(endpoints: list[dict[str, str]], endpoint_id: str, flow: str) -> dict[str, str]:
    matches = [
        endpoint for endpoint in endpoints
        if endpoint["id"].casefold() == endpoint_id.casefold()
    ]
    if len(matches) != 1:
        raise AudioRouteError("audio_guard_endpoint_unavailable")
    if matches[0]["flow"] != flow:
        raise AudioRouteError("audio_guard_endpoint_wrong_flow")
    return matches[0]


def _unique_name(
    endpoints: list[dict[str, str]],
    name: str,
    flow: str,
) -> dict[str, str]:
    folded_name = name.strip().casefold()
    matches = [
        endpoint for endpoint in endpoints
        if endpoint["flow"] == flow and endpoint["name"].strip().casefold() == folded_name
    ]
    if not matches:
        raise AudioRouteError("audio_endpoint_counterpart_unavailable")
    if len(matches) != 1:
        raise AudioRouteError("audio_endpoint_name_ambiguous")
    return matches[0]


def _is_virtual(endpoint: dict[str, str]) -> bool:
    name = endpoint["name"].casefold()
    return any(marker in name for marker in _VIRTUAL_MARKERS)


def _validate_override(value: Any, expected: str, *, capture: bool) -> None:
    if type(value) is not str or len(value) > MAX_ENDPOINT_NAME_CHARS:
        raise AudioRouteError("audio_device_override_invalid")
    if not value:
        return
    candidate = value.strip()
    if capture:
        candidate = _CAPTURE_SUFFIX.sub("", candidate).strip()
    if not candidate or candidate.casefold() != expected.casefold():
        raise AudioRouteError("audio_device_override_mismatch")


def resolve_audio_route(
    guard: Any,
    active_endpoints: Any,
    overrides: Any,
) -> dict[str, Any]:
    """Resolve stable guard IDs to a frozen, distinct virtual audio route.

    The incoming guard IDs choose the endpoints. Friendly names are used only to
    verify the corresponding opposite-flow endpoints required by the Host's
    existing mapping; this function never selects a default device.
    """
    if not isinstance(guard, dict):
        raise AudioRouteError("audio_guard_invalid")
    capture_guard_id = guard.get("captureEndpointId")
    render_guard_id = guard.get("renderEndpointId")
    capture_guard_id = _bounded_text(capture_guard_id, MAX_ENDPOINT_ID_CHARS - 1, "audio_guard_endpoint_invalid")
    render_guard_id = _bounded_text(render_guard_id, MAX_ENDPOINT_ID_CHARS - 1, "audio_guard_endpoint_invalid")
    if capture_guard_id.casefold() == render_guard_id.casefold():
        raise AudioRouteError("audio_guard_endpoint_invalid")

    if not isinstance(overrides, dict) or set(overrides) - {"capture_device", "playback_device"}:
        raise AudioRouteError("audio_device_override_invalid")
    endpoints = _endpoint_inventory(active_endpoints)

    capture_guard = _by_id(endpoints, capture_guard_id, "capture")
    render_guard = _by_id(endpoints, render_guard_id, "render")
    capture_render_name = render_guard["name"].strip()
    playback_render_name = capture_guard["name"].strip()
    if not capture_render_name or not playback_render_name:
        raise AudioRouteError("audio_endpoint_metadata_invalid")
    if capture_render_name.casefold() == playback_render_name.casefold():
        raise AudioRouteError("audio_routes_not_distinct")

    # Pin each native guard route by both its stable ID and unique friendly name.
    # The Host's existing mapping additionally requires the input's same-name
    # render endpoint for playback; freeze that ID to detect same-name replacement.
    unique_capture_guard = _unique_name(endpoints, capture_guard["name"], "capture")
    unique_render_guard = _unique_name(endpoints, render_guard["name"], "render")
    playback_render = _unique_name(endpoints, playback_render_name, "render")
    if unique_capture_guard["id"].casefold() != capture_guard_id.casefold():
        raise AudioRouteError("audio_guard_endpoint_ambiguous")
    if unique_render_guard["id"].casefold() != render_guard_id.casefold():
        raise AudioRouteError("audio_guard_endpoint_ambiguous")
    selected = (capture_guard, render_guard, playback_render)
    selected_ids = [endpoint["id"].casefold() for endpoint in selected]
    if len(set(selected_ids)) != len(selected_ids):
        raise AudioRouteError("audio_routes_not_distinct")
    if not all(_is_virtual(endpoint) for endpoint in selected):
        raise AudioRouteError("physical_audio_route_unsupported")

    _validate_override(overrides.get("capture_device", ""), capture_render_name, capture=True)
    _validate_override(overrides.get("playback_device", ""), playback_render_name, capture=False)
    return {
        "guard": dict(guard),
        "captureRenderName": capture_render_name,
        "playbackRenderName": playback_render_name,
        "playbackRenderEndpointId": playback_render["id"],
    }


def audio_route_identity(source: dict[str, Any] | None) -> tuple[Any, ...] | None:
    """Return the non-logged identity used to detect route changes in a run."""
    if source is None:
        return None
    guard = source["guard"]
    return (
        guard["nodeArtifact"], guard["nodeSha256"], guard["guardDllPath"], guard["dllSha256"],
        guard["captureEndpointId"], guard["renderEndpointId"],
        source["captureRenderName"].casefold(), source["playbackRenderName"].casefold(),
        source["playbackRenderEndpointId"],
    )


def select_audio_guard_route(base: dict[str, Any], active_endpoints: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """Select only one valid virtual route; ambiguity requires public name overrides."""
    endpoints = _endpoint_inventory(active_endpoints)
    if type(overrides) is not dict or set(overrides) - {"capture_device", "playback_device"}:
        raise AudioRouteError("audio_device_override_invalid")
    if any(type(value) is not str or len(value) > MAX_ENDPOINT_NAME_CHARS for value in overrides.values()):
        raise AudioRouteError("audio_device_override_invalid")
    found = None
    captures = [item for item in endpoints if item["flow"] == "capture" and _is_virtual(item)]
    renders = [item for item in endpoints if item["flow"] == "render" and _is_virtual(item)]
    for capture in captures:
        for render in renders:
            candidate = {**base, "captureEndpointId": capture["id"], "renderEndpointId": render["id"]}
            try:
                resolved = resolve_audio_route(candidate, endpoints, overrides)
            except AudioRouteError:
                continue
            if found is not None:
                raise AudioRouteError("audio_route_selection_required")
            found = resolved
    if found is None:
        raise AudioRouteError("audio_route_selection_unavailable")
    return found


def describe_virtual_endpoints(active_endpoints: Any) -> list[dict[str, str]]:
    """Expose only friendly names and flow for an explicit setup inventory."""
    return [{"name": item["name"], "flow": item["flow"]}
            for item in _endpoint_inventory(active_endpoints) if _is_virtual(item)]


__all__ = ["AudioRouteError", "audio_route_identity", "describe_virtual_endpoints", "resolve_audio_route", "select_audio_guard_route"]
