from __future__ import annotations

import hashlib
import json
import unittest
from collections import UserDict

from audio import group_protocol as legacy_group_protocol
from audio.group_protocol import (
    anonymous_speaker_annotation,
    group_session_id,
    universal_group_options,
    validate_group_scope,
)
from qqcall_pcm import group_protocol as packaged_group_protocol


def _scope() -> dict[str, object]:
    return {
        "kind": "qqcall_group",
        "serviceInstance": "qq-service-instance-7",
        "hostGeneration": 3,
        "connectionGeneration": 11,
        "groupId": "9876543210123",
        "roomId": "native-room:8bd2",
        "roomGeneration": 4,
    }


def _uvc_session_identity() -> dict[str, object]:
    return {
        "service_instance_id": "uvc-service-token-4061728395",
        "session_id": "uvc-session-token-7182635409",
        "session_generation": "generation-token-6053148297",
        "source_id": "main",
    }


class GroupProtocolTests(unittest.TestCase):
    def test_legacy_module_reexports_packaged_implementation_by_identity(self) -> None:
        self.assertEqual(legacy_group_protocol.__all__, packaged_group_protocol.__all__)
        for name in packaged_group_protocol.__all__:
            self.assertIs(
                getattr(legacy_group_protocol, name),
                getattr(packaged_group_protocol, name),
            )

    def test_valid_scope_returns_a_fresh_plain_dict(self) -> None:
        source = _scope()
        validated = validate_group_scope(source)
        self.assertEqual(validated, source)
        self.assertIsNot(validated, source)
        validated["roomId"] = "changed"
        self.assertEqual(source["roomId"], "native-room:8bd2")

    def test_universal_options_match_frozen_group_contract_and_are_fresh(self) -> None:
        source = _scope()
        options = universal_group_options(source)
        self.assertEqual(
            options,
            {
                "platform": "qq",
                "scope": group_session_id(source),
                "mode": "pipeline",
                "sources": [
                    {
                        "id": "main",
                        "input": "external",
                        "format": "s16le",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "capture": {},
                        "identity": {"mode": "speaker"},
                    }
                ],
                "output": {"mode": "none"},
                "denoise": False,
                "speaker": True,
                "speaker_log": True,
                "max_duration_seconds": 60,
            },
        )
        self.assertEqual(source, _scope())

        options["sources"][0]["capture"]["mutated"] = True
        fresh = universal_group_options(source)
        self.assertEqual(fresh["sources"][0]["capture"], {})
        self.assertEqual(fresh["sources"][0]["identity"], {"mode": "speaker"})

    def test_scope_hash_separates_group_room_and_namespace_identities(self) -> None:
        base = _scope()
        changed_group = {**base, "groupId": "9876543210124"}
        changed_room = {**base, "roomId": "native-room:8bd3"}
        changed_generation = {**base, "roomGeneration": 5}

        self.assertNotEqual(group_session_id(base), group_session_id(changed_group))
        self.assertNotEqual(group_session_id(base), group_session_id(changed_room))
        self.assertNotEqual(group_session_id(base), group_session_id(changed_generation))

        unnamespaced = {key: value for key, value in base.items() if key != "kind"}
        unnamespaced_hash = hashlib.sha256(
            json.dumps(unnamespaced, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(group_session_id(base), unnamespaced_hash)

    def test_private_scope_namespace_and_private_shape_are_rejected(self) -> None:
        private_namespace = {**_scope(), "kind": "qqcall"}
        private_shape = {
            "serviceInstance": "qq-service-instance-7",
            "hostGeneration": 3,
            "connectionGeneration": 11,
            "callId": "call-77",
            "callGeneration": 4,
        }
        for value in (private_namespace, private_shape):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_group_scope(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                group_session_id(value)

    def test_scope_requires_plain_dict_exact_keys_and_valid_field_types(self) -> None:
        base = _scope()
        invalid_values = [
            None,
            list(base.items()),
            UserDict(base),
            {key: value for key, value in base.items() if key != "roomId"},
            {**base, "unexpected": "field"},
            {**base, "kind": None},
            {**base, "serviceInstance": None},
            {**base, "hostGeneration": "3"},
            {**base, "groupId": 9876543210123},
            {**base, "roomId": None},
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_group_scope(value)

    def test_scope_rejects_zero_boolean_and_out_of_range_generations(self) -> None:
        for field in ("hostGeneration", "connectionGeneration", "roomGeneration"):
            for invalid in (0, False, True, 2**53, -1):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    validate_group_scope({**_scope(), field: invalid})

    def test_scope_rejects_noncanonical_oversized_and_control_ids(self) -> None:
        invalid_values = [
            {**_scope(), "serviceInstance": ""},
            {**_scope(), "serviceInstance": "x" * 129},
            {**_scope(), "serviceInstance": "service\ninstance"},
            {**_scope(), "groupId": "0"},
            {**_scope(), "groupId": "0123"},
            {**_scope(), "groupId": "9" * 21},
            {**_scope(), "groupId": "123\t4"},
            {**_scope(), "roomId": ""},
            {**_scope(), "roomId": "r" * 129},
            {**_scope(), "roomId": "room\x00id"},
            {**_scope(), "roomId": "room\x7fid"},
            {**_scope(), "roomId": " leading-space"},
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_group_scope(value)

    def test_room_id_matches_existing_wire_bounds_and_remains_a_separate_role(self) -> None:
        base = _scope()
        accepted_room_ids = (
            "r" * 128,
            "room reference",
            "room\u200dreference",
            base["groupId"],
        )
        for room_id in accepted_room_ids:
            with self.subTest(room_id=room_id):
                scope = {**base, "roomId": room_id}
                validated = validate_group_scope(scope)
                self.assertIs(type(validated["roomId"]), str)
                self.assertEqual(validated["roomId"], room_id)
                self.assertEqual(validated["groupId"], base["groupId"])
                self.assertEqual(validated["roomGeneration"], base["roomGeneration"])

        equal_text_scope = {**base, "roomId": base["groupId"]}
        other_room_scope = {**equal_text_scope, "roomId": "room reference"}
        self.assertNotEqual(group_session_id(equal_text_scope), group_session_id(other_room_scope))

    def test_speaker_cluster_is_anonymous_and_local_to_room_generation(self) -> None:
        speaker_id = "9876543210987654321"
        session_identity = _uvc_session_identity()
        first = anonymous_speaker_annotation(_scope(), speaker_id, session_identity)
        same_identity = anonymous_speaker_annotation(_scope(), speaker_id, session_identity)
        changed_room = anonymous_speaker_annotation(
            {**_scope(), "roomId": "native-room:next"}, speaker_id, session_identity
        )
        changed_generation = anonymous_speaker_annotation(
            {**_scope(), "roomGeneration": 5}, speaker_id, session_identity
        )
        changed_uvc_session = anonymous_speaker_annotation(
            _scope(),
            speaker_id,
            {**session_identity, "session_id": "uvc-session-token-next"},
        )
        changed_uvc_generation = anonymous_speaker_annotation(
            _scope(),
            speaker_id,
            {**session_identity, "session_generation": "generation-token-next"},
        )
        changed_uvc_service = anonymous_speaker_annotation(
            _scope(),
            speaker_id,
            {**session_identity, "service_instance_id": "uvc-service-token-next"},
        )

        self.assertEqual(set(first), {"version", "speaker_ref", "label"})
        self.assertEqual(first["version"], 1)
        self.assertRegex(first["speaker_ref"], r"^qqg-[0-9a-f]{16}$")
        self.assertEqual(first["label"], f"语音用户 {first['speaker_ref'][4:]}")
        self.assertLessEqual(len(first["label"]), 64)
        self.assertEqual(first, same_identity)
        self.assertNotEqual(first["speaker_ref"], changed_room["speaker_ref"])
        self.assertNotEqual(first["speaker_ref"], changed_generation["speaker_ref"])
        self.assertNotEqual(first["speaker_ref"], changed_uvc_session["speaker_ref"])
        self.assertNotEqual(first["speaker_ref"], changed_uvc_generation["speaker_ref"])
        self.assertNotEqual(first["speaker_ref"], changed_uvc_service["speaker_ref"])

    def test_uvc_session_identity_is_required_and_exact(self) -> None:
        with self.assertRaises(TypeError):
            anonymous_speaker_annotation(_scope(), "speaker_1")

        base = _uvc_session_identity()
        invalid_values = [
            None,
            UserDict(base),
            {key: value for key, value in base.items() if key != "session_generation"},
            {**base, "unexpected": "field"},
            {**base, "service_instance_id": ""},
            {**base, "service_instance_id": "service has space"},
            {**base, "session_id": "s" * 129},
            {**base, "session_id": "session/unsafe"},
            {**base, "session_generation": ""},
            {**base, "session_generation": "g" * 129},
            {**base, "session_generation": 4},
            {**base, "session_generation": True},
            {**base, "source_id": "external"},
            {**base, "source_id": "main-secondary"},
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                anonymous_speaker_annotation(_scope(), "speaker_1", value)

    def test_uvc_identity_accepts_maximum_ascii_safe_identifier_lengths(self) -> None:
        identity = {
            "service_instance_id": "s" * 128,
            "session_id": "i" * 128,
            "session_generation": "g" * 128,
            "source_id": "main",
        }
        annotation = anonymous_speaker_annotation(_scope(), "speaker_1", identity)
        self.assertEqual(set(annotation), {"version", "speaker_ref", "label"})

    def test_projection_omits_speaker_and_actual_session_identifiers(self) -> None:
        speaker_id = "9876543210987654321"
        identity = _uvc_session_identity()
        annotation = anonymous_speaker_annotation(_scope(), speaker_id, identity)
        options = universal_group_options(_scope())
        annotation_text = json.dumps(annotation, ensure_ascii=False, sort_keys=True)
        options_text = json.dumps(options, ensure_ascii=False, sort_keys=True)

        for raw_id in (
            speaker_id,
            identity["service_instance_id"],
            identity["session_id"],
            identity["session_generation"],
            _scope()["groupId"],
            _scope()["roomId"],
        ):
            with self.subTest(raw_id=raw_id):
                self.assertNotIn(raw_id, annotation_text)
                self.assertNotIn(raw_id, options_text)
        self.assertEqual(set(annotation), {"version", "speaker_ref", "label"})
        self.assertEqual(options["sources"][0]["id"], "main")

    def test_speaker_token_and_identity_fields_do_not_escape_projection(self) -> None:
        speaker_id = "9876543210987654321"
        annotation = anonymous_speaker_annotation(_scope(), speaker_id, _uvc_session_identity())
        options = universal_group_options(_scope())
        serialized = json.dumps([annotation, options], ensure_ascii=False, sort_keys=True)

        self.assertNotIn(speaker_id, serialized)
        self.assertNotIn(_scope()["groupId"], serialized)
        self.assertNotIn(_scope()["roomId"], serialized)
        self.assertNotIn("user_info", serialized)
        self.assertNotIn("peer_user_id", serialized)
        self.assertNotIn("speaker_name", serialized)
        self.assertEqual(options["sources"][0]["identity"], {"mode": "speaker"})
        self.assertEqual(set(annotation), {"version", "speaker_ref", "label"})

    def test_speaker_id_must_be_bounded_and_control_free(self) -> None:
        for value in (None, "", "speaker\x00id", " speaker", "s" * 129, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                anonymous_speaker_annotation(_scope(), value, _uvc_session_identity())


if __name__ == "__main__":
    unittest.main()
