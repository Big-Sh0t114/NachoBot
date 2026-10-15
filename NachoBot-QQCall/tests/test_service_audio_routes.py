from __future__ import annotations

import unittest

from service.audio_routes import AudioRouteError, audio_route_identity, resolve_audio_route


def guard() -> dict[str, object]:
    return {
        "captureEndpointId": "guard-capture-id",
        "renderEndpointId": "guard-render-id",
        "nodeArtifact": r"C:\\fixture\\QQCallAudioRouteGuard-v2.node",
        "nodeSha256": "a" * 64,
        "guardDllPath": r"C:\\fixture\\QQCallAudioRouteGuard-v2.dll",
        "dllSha256": "b" * 64,
    }


def inventory() -> list[dict[str, str]]:
    return [
        {"id": "guard-capture-id", "name": "Playback (VB-Audio Virtual Cable)", "flow": "capture"},
        {"id": "guard-render-id", "name": "Capture (VB-Audio Virtual Cable)", "flow": "render"},
        {"id": "playback-render-id", "name": "Playback (VB-Audio Virtual Cable)", "flow": "render"},
    ]


class ResolveAudioRouteTests(unittest.TestCase):
    def resolve(self, records: list[dict[str, str]] | None = None, **overrides: str):
        return resolve_audio_route(
            guard(), inventory() if records is None else records,
            {"capture_device": "", "playback_device": "", **overrides},
        )

    def test_pinned_three_endpoint_inventory_resolves_names_and_freezes_outgoing_render_id(self) -> None:
        result = self.resolve()
        self.assertEqual(result["captureRenderName"], "Capture (VB-Audio Virtual Cable)")
        self.assertEqual(result["playbackRenderName"], "Playback (VB-Audio Virtual Cable)")
        self.assertEqual(result["playbackRenderEndpointId"], "playback-render-id")
        self.assertNotIn("captureCounterpartEndpointId", result)
        self.assertEqual(audio_route_identity(result)[-1], "playback-render-id")

    def test_manual_capture_suffixes_match_only_pinned_route_and_partial_override_is_valid(self) -> None:
        self.resolve(capture_device="Capture (VB-Audio Virtual Cable) (loopback)")
        self.resolve(capture_device="Capture (VB-Audio Virtual Cable) [loopback]")
        self.resolve(playback_device="Playback (VB-Audio Virtual Cable)")
        with self.assertRaisesRegex(AudioRouteError, "audio_device_override_mismatch"):
            self.resolve(capture_device="Unrelated Virtual Audio Cable")
        with self.assertRaisesRegex(AudioRouteError, "audio_device_override_mismatch"):
            self.resolve(playback_device="Playback (VB-Audio Virtual Cable) [loopback]")

    def test_missing_or_wrong_flow_guard_endpoint_fails_closed(self) -> None:
        records = inventory()
        records.pop(0)
        with self.assertRaisesRegex(AudioRouteError, "audio_guard_endpoint_unavailable"):
            self.resolve(records)

        records = inventory()
        records[0]["flow"] = "render"
        with self.assertRaisesRegex(AudioRouteError, "audio_guard_endpoint_wrong_flow"):
            self.resolve(records)

    def test_casefold_duplicate_pinned_names_and_duplicate_ids_are_rejected(self) -> None:
        records = inventory() + [
            {"id": "duplicate-capture", "name": "playback (vb-audio virtual cable)", "flow": "capture"},
        ]
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_name_ambiguous"):
            self.resolve(records)

        records = inventory() + [
            {"id": "GUARD-CAPTURE-ID", "name": "Other (VB-Audio Virtual Cable)", "flow": "capture"},
        ]
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_id_ambiguous"):
            self.resolve(records)

    def test_duplicate_incoming_render_or_outgoing_capture_names_are_rejected(self) -> None:
        records = inventory() + [
            {"id": "duplicate-render", "name": "capture (vb-audio virtual cable)", "flow": "render"},
        ]
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_name_ambiguous"):
            self.resolve(records)

        records = inventory() + [
            {"id": "duplicate-capture", "name": "PLAYBACK (vb-audio virtual cable)", "flow": "capture"},
        ]
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_name_ambiguous"):
            self.resolve(records)

    def test_physical_and_same_routes_are_rejected(self) -> None:
        records = inventory()
        records[0]["name"] = "Built-in Microphone"
        records[2]["name"] = "Built-in Microphone"
        with self.assertRaisesRegex(AudioRouteError, "physical_audio_route_unsupported"):
            self.resolve(records)

        records = inventory()
        records[0]["name"] = records[1]["name"]
        with self.assertRaisesRegex(AudioRouteError, "audio_routes_not_distinct"):
            self.resolve(records)

    def test_inventory_and_override_shapes_are_strict(self) -> None:
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_inventory_invalid"):
            self.resolve([])
        with self.assertRaisesRegex(AudioRouteError, "audio_endpoint_metadata_invalid"):
            self.resolve([{ "id": "id", "name": " ", "flow": "render" }])
        with self.assertRaisesRegex(AudioRouteError, "audio_device_override_invalid"):
            resolve_audio_route(guard(), inventory(), {"capture_device": "", "playback_device": "", "enabled": False})


if __name__ == "__main__":
    unittest.main()
