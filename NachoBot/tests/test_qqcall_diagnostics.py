from __future__ import annotations

import unittest

from qqcall_client.qqcall_diagnostics import make_qqcall_status_callback, project_qqcall_status


class _RecordingLogger:
    def __init__(self):
        self.messages = []

    def info(self, template, *args):
        self.messages.append(template.format(*args))


class TestQQCallDiagnostics(unittest.TestCase):
    def test_group_lifecycle_errors_are_visible_without_scope_or_speaker_data(self):
        logger = _RecordingLogger()
        callback = make_qqcall_status_callback(adapter="snowluma", logger=logger)
        event = {"status": {"groupAudio": {"state": "active", "ready": True,
                 "acceptedTurns": 1, "forwardedResults": 2, "scope": "private_scope_44",
                 "speaker": "private_speaker_55", "lastFailureCode": None}}}
        callback(event)
        callback(event)
        self.assertEqual(len(logger.messages), 1)
        self.assertIn("group_ready=True", logger.messages[0])
        event["status"]["groupAudio"].update(state="failed", ready=False,
                                                lastFailureCode="group_audio_lease_lost")
        callback(event)
        self.assertEqual(len(logger.messages), 2)
        self.assertIn("group_error=group_audio_lease_lost", logger.messages[1])
        self.assertNotIn("private_scope_44", repr(logger.messages))
        self.assertNotIn("private_speaker_55", repr(logger.messages))
        event["status"]["groupAudio"].update(state="private_state", lastFailureCode="private_error",
                                                acceptedTurns=True, forwardedResults=1 << 80)
        p = project_qqcall_status(event)
        self.assertEqual(p["group_state"], "unknown")
        self.assertEqual(p["group_error"], "diagnostic_error_unclassified")
        self.assertEqual(p["group_turns"], 0)
        self.assertEqual(p["group_results"], (1 << 31) - 1)

    def test_only_safe_projection_is_logged_and_poll_counters_are_deduplicated(self):
        logger = _RecordingLogger()
        callback = make_qqcall_status_callback(adapter="napcat", logger=logger)
        private_uid = "u_private_caller_998877"
        private_token = "control-secret-value-41"
        private_transcript = "please read this private transcript"
        event = {
            "kind": "poll_failed",
            "status": {
                "running": True,
                "accountVerified": True,
                "leaseClaimed": True,
                "lastFailureCode": private_uid,
                "cursor": 31,
                "counts": {"polls": 70, "events": 8},
                "identity": {"account": private_uid, "token": private_token},
                "activeCall": {"peerUid": private_uid, "transcript": private_transcript},
                "audio": {
                    "ready": False,
                    "capturing": True,
                    "vadAvailable": True,
                    "asrStreaming": False,
                    "vadEngine": "universalvc",
                    "lastFailureCode": private_token,
                    "unavailableReason": "host_route_unverified",
                    "utterancesSent": 1,
                    "utterancesDropped": 0,
                    "playbackDrainCount": 0,
                    "framesReceived": 5000,
                    "audio": private_token,
                    "scope": {"callId": private_uid},
                    "routeDiagnostics": {
                        "routeCheckStage": private_uid,
                        "firstFailureStage": private_token,
                    },
                    "extra": {"secret": private_token},
                },
                "extra": private_token,
            },
        }

        callback(event)
        self.assertEqual(len(logger.messages), 1)
        message = logger.messages[0]
        for secret in (private_uid, private_token, private_transcript):
            self.assertNotIn(secret, message)
        self.assertIn("frames_received=5000", message)
        self.assertIn("runtime_error=diagnostic_error_unclassified", message)
        self.assertIn("audio_error=diagnostic_error_unclassified", message)
        self.assertIn("audio_reason=host_route_unverified", message)
        self.assertIn("route_check_stage=none", message)
        self.assertIn("route_first_failure_stage=none", message)

        # Cursor, poll, and capture-frame growth do not create log churn.
        event["status"]["cursor"] += 1
        event["status"]["counts"]["polls"] += 1
        event["status"]["audio"]["framesReceived"] += 20
        callback(event)
        self.assertEqual(len(logger.messages), 1)

        # A meaningful audio transition and utterance count update are logged.
        event["status"]["lastFailureCode"] = None
        event["status"]["audio"].update(
            ready=True,
            capturing=True,
            lastFailureCode=None,
            unavailableReason="",
            utterancesSent=2,
        )
        callback(event)
        self.assertEqual(len(logger.messages), 2)
        self.assertIn("audio_ready=True", logger.messages[1])
        self.assertIn("utterances_sent=2", logger.messages[1])

    def test_projection_is_primitive_allowlisted_and_bounds_counters(self):
        class _StageSubclass(str):
            pass

        projection = project_qqcall_status({
            "status": {
                "running": 1,
                "accountVerified": True,
                "leaseClaimed": False,
                "lastFailureCode": "caller_secret_7733",
                "audio": {
                    "ready": True,
                    "capturing": False,
                    "vadAvailable": True,
                    "asrStreaming": True,
                    "vadEngine": "private-model-name",
                    "lastFailureCode": "u_peer_123456",
                    "unavailableReason": "starting",
                    "utterancesSent": True,
                    "utterancesDropped": (1 << 80),
                    "playbackDrainCount": -1,
                    "framesReceived": (1 << 80),
                    "framesSent": -1,
                    "routeDiagnostics": {
                        "monitorStepMs": 900_000,
                        "proofAgeMs": -900,
                        "monitorChecks": 8,
                        "identityChecks": True,
                        "failureCount": (1 << 80),
                        "firstFailure": "u_private_caller_998877:token",
                        "routeCheckStage": "u_private_route_stage_secret",
                        "firstFailureStage": _StageSubclass("helper_request"),
                        "rawReason": "never-project-this",
                    },
                },
            }
        })

        self.assertEqual(projection["running"], False)
        self.assertEqual(projection["account_verified"], True)
        self.assertEqual(projection["vad_engine"], "unknown")
        self.assertEqual(projection["runtime_error"], "diagnostic_error_unclassified")
        self.assertEqual(projection["audio_error"], "diagnostic_error_unclassified")
        self.assertEqual(projection["audio_reason"], "starting")
        self.assertEqual(projection["utterances_sent"], 0)
        self.assertEqual(projection["utterances_dropped"], (1 << 31) - 1)
        self.assertEqual(projection["playback_drains"], 0)
        self.assertEqual(projection["frames_received"], (1 << 31) - 1)
        self.assertEqual(projection["frames_sent"], 0)
        self.assertEqual(projection["route_monitor_step_ms"], 60_000)
        self.assertEqual(projection["route_proof_age_ms"], -250)
        self.assertEqual(projection["route_monitor_checks"], 8)
        self.assertEqual(projection["route_identity_checks"], 0)
        self.assertEqual(projection["route_failure_count"], (1 << 31) - 1)
        self.assertEqual(projection["route_first_failure"], "diagnostic_error_unclassified")
        self.assertEqual(projection["route_check_stage"], "none")
        self.assertEqual(projection["route_first_failure_stage"], "none")
        self.assertNotIn("u_private_caller_998877", repr(projection))
        self.assertNotIn("u_private_route_stage_secret", repr(projection))
        self.assertNotIn("never-project-this", repr(projection))
        self.assertTrue(all(isinstance(value, (bool, int, str)) for value in projection.values()))

    def test_universal_api_missing_capabilities_remain_unknown_despite_activity(self):
        projection = project_qqcall_status({
            "status": {
                "audio": {
                    "pipeline": "universal_api",
                    "ready": True,
                    "capturing": True,
                    "utterancesSent": 2,
                },
            }
        })

        self.assertEqual(projection["vad_available"], "unknown")
        self.assertEqual(projection["asr_streaming"], "unknown")
        self.assertEqual(projection["vad_engine"], "unknown")
        self.assertEqual(projection["utterances_sent"], 2)

    def test_universal_api_explicit_boolean_capabilities_are_preserved(self):
        projection = project_qqcall_status({
            "status": {
                "audio": {
                    "pipeline": "universal_api",
                    "vadAvailable": False,
                    "asrStreaming": True,
                },
            }
        })

        self.assertIs(projection["vad_available"], False)
        self.assertIs(projection["asr_streaming"], True)

    def test_universal_api_hostile_capability_values_project_as_unknown(self):
        class _BoolLike:
            def __bool__(self):
                return True

        for value in (None, 0, 1, "true", [], {}, _BoolLike()):
            with self.subTest(value_type=type(value).__name__):
                projection = project_qqcall_status({
                    "status": {
                        "audio": {
                            "pipeline": "universal_api",
                            "vadAvailable": value,
                            "asrStreaming": value,
                        },
                    }
                })
                self.assertEqual(projection["vad_available"], "unknown")
                self.assertEqual(projection["asr_streaming"], "unknown")
                self.assertTrue(all(type(item) in (bool, int, str) for item in projection.values()))

    def test_non_universal_api_capabilities_keep_legacy_false_default(self):
        for pipeline, audio in (
            ("legacy", {"pipeline": "legacy"}),
            ("legacy", {"pipeline": "legacy", "vadAvailable": 1, "asrStreaming": "true"}),
            (None, {}),
        ):
            with self.subTest(pipeline=pipeline, audio=audio):
                projection = project_qqcall_status({
                    "status": {"audio": {**audio, "pipeline": pipeline}},
                })
                self.assertIs(projection["vad_available"], False)
                self.assertIs(projection["asr_streaming"], False)

    def test_universal_unknown_values_keep_safe_logging_dedup_and_downstream(self):
        logger = _RecordingLogger()
        received = []
        callback = make_qqcall_status_callback(
            adapter="snowluma", logger=logger, downstream=received.append,
        )
        event = {
            "status": {
                "audio": {
                    "pipeline": "universal_api",
                    "ready": True,
                    "vadAvailable": "private capability detail",
                },
                "secret": "private event detail",
            }
        }

        callback(event)
        callback(event)

        self.assertEqual(len(logger.messages), 1)
        self.assertIn("vad_available=unknown", logger.messages[0])
        self.assertIn("asr_streaming=unknown", logger.messages[0])
        self.assertNotIn("private", logger.messages[0])
        self.assertEqual(received, [event, event])
        self.assertIs(received[0], event)

    def test_route_rejection_reason_and_public_unverified_code_are_allowlisted(self):
        projection = project_qqcall_status({
            "status": {
                "running": True,
                "lastFailureCode": "universal_api_route_unverified",
                "audio": {
                    "lastFailureCode": "universal_api_route_unverified",
                    "unavailableReason": "universal_api_route_unverified",
                    "routeDiagnostics": {"firstFailure": "host_input_not_active"},
                },
            }
        })
        self.assertEqual(projection["runtime_error"], "universal_api_route_unverified")
        self.assertEqual(projection["audio_error"], "universal_api_route_unverified")
        self.assertEqual(projection["audio_reason"], "universal_api_route_unverified")
        self.assertEqual(projection["route_first_failure"], "host_input_not_active")

        rejected = project_qqcall_status({
            "status": {
                "audio": {
                    "lastFailureCode": "universal_api_route_unverified:private-token",
                    "unavailableReason": "universal_api_route_unverified:private-token",
                    "routeDiagnostics": {"firstFailure": "host_input_not_active:private-token"},
                },
            }
        })
        self.assertEqual(rejected["audio_error"], "diagnostic_error_unclassified")
        self.assertEqual(rejected["audio_reason"], "diagnostic_error_unclassified")
        self.assertEqual(rejected["route_first_failure"], "diagnostic_error_unclassified")
        self.assertNotIn("private-token", repr(rejected))

    def test_route_helper_transport_details_are_bounded_and_logs_drop_raw_fields(self):
        absent = project_qqcall_status({"status": {"audio": {}}})
        self.assertEqual(absent["route_helper_http_status"], "unknown")
        self.assertEqual(absent["route_helper_error_code"], "none")

        logger = _RecordingLogger()
        callback = make_qqcall_status_callback(adapter="snowluma", logger=logger)
        private_code = "u_private_caller_998877"
        private_header = "Bearer top-secret-header"
        private_body = "raw service response with private caller data"
        event = {
            "status": {
                "audio": {
                    "routeDiagnostics": {
                        "firstFailure": "route_helper_transport_error",
                        "routeHelperHttpStatus": 409,
                        "routeHelperErrorCode": "call_not_connected",
                        "headers": {"Authorization": private_header},
                        "body": private_body,
                    },
                },
            }
        }

        callback(event)
        projection = project_qqcall_status(event)
        self.assertEqual(projection["route_helper_http_status"], 409)
        self.assertEqual(projection["route_helper_error_code"], "call_not_connected")
        self.assertIn("route_helper_http_status=409", logger.messages[0])
        self.assertIn("route_helper_error_code=call_not_connected", logger.messages[0])
        for secret in (private_header, private_body):
            self.assertNotIn(secret, logger.messages[0])

        event["status"]["audio"]["routeDiagnostics"].update(
            routeHelperHttpStatus=True,
            routeHelperErrorCode=private_code,
        )
        callback(event)
        projected = project_qqcall_status(event)
        self.assertEqual(projected["route_helper_http_status"], "unknown")
        self.assertEqual(projected["route_helper_error_code"], "service_error_unclassified")
        self.assertIn("route_helper_http_status=unknown", logger.messages[1])
        self.assertIn("route_helper_error_code=service_error_unclassified", logger.messages[1])
        for secret in (private_code, private_header, private_body):
            self.assertNotIn(secret, repr(projected))
            self.assertNotIn(secret, logger.messages[1])

        for malformed_status in (99, 600, "503", 503.0):
            with self.subTest(status=malformed_status):
                self.assertEqual(
                    project_qqcall_status({
                        "status": {"audio": {"routeDiagnostics": {"routeHelperHttpStatus": malformed_status}}}
                    })["route_helper_http_status"],
                    "unknown",
                )

    def test_route_diagnostic_reason_allowlist_and_volatile_fields_do_not_churn_logs(self):
        logger = _RecordingLogger()
        callback = make_qqcall_status_callback(adapter="snowluma", logger=logger)
        event = {
            "status": {
                "running": True,
                "accountVerified": True,
                "leaseClaimed": True,
                "audio": {
                    "ready": True,
                    "capturing": True,
                    "framesReceived": 100,
                    "framesSent": 12,
                    "routeDiagnostics": {
                        "monitorStepMs": 90,
                        "proofAgeMs": 10,
                        "monitorChecks": 3,
                        "identityChecks": 7,
                        "failureCount": 0,
                        "firstFailure": None,
                        "routeCheckStage": "scope_validation",
                        "firstFailureStage": None,
                    },
                },
            }
        }

        callback(event)
        self.assertEqual(len(logger.messages), 1)
        self.assertIn("route_first_failure=none", logger.messages[0])
        self.assertIn("route_check_stage=scope_validation", logger.messages[0])
        self.assertIn("route_first_failure_stage=none", logger.messages[0])
        self.assertIn("frames_received=100", logger.messages[0])
        event["status"]["audio"].update(framesReceived=130, framesSent=15)
        event["status"]["audio"]["routeDiagnostics"].update(
            monitorStepMs=120, proofAgeMs=35, monitorChecks=4, identityChecks=9,
            routeCheckStage="helper_request",
        )
        callback(event)
        self.assertEqual(len(logger.messages), 1)

        event["status"]["audio"]["routeDiagnostics"].update(
            firstFailure="route_check_timeout", firstFailureStage="helper_request",
            routeCheckStage="helper_request", failureCount=1,
        )
        callback(event)
        self.assertEqual(len(logger.messages), 2)
        self.assertIn("route_first_failure=route_check_timeout", logger.messages[1])
        self.assertIn("route_check_stage=helper_request", logger.messages[1])
        self.assertIn("route_first_failure_stage=helper_request", logger.messages[1])
        for metric in (
            "frames_received=130",
            "frames_sent=15",
            "route_monitor_step_ms=120",
            "route_proof_age_ms=35",
            "route_monitor_checks=4",
            "route_identity_checks=9",
            "route_failure_count=1",
        ):
            self.assertIn(metric, logger.messages[1])

    def test_explicit_status_hook_receives_original_event(self):
        logger = _RecordingLogger()
        received = []
        callback = make_qqcall_status_callback(
            adapter="snowluma",
            logger=logger,
            downstream=received.append,
        )
        event = {"kind": "audio_ready", "status": {"running": True, "audio": {"ready": True}}}

        callback(event)

        self.assertEqual(received, [event])
        self.assertIs(received[0], event)
        self.assertEqual(len(logger.messages), 1)

    def test_healthy_empty_reasons_and_new_fixed_runtime_codes_are_projected_safely(self):
        healthy = project_qqcall_status({
            "status": {
                "running": True,
                "accountVerified": True,
                "leaseClaimed": True,
                "lastFailureCode": None,
                "audio": {"unavailableReason": "", "lastFailureCode": None},
            }
        })
        self.assertEqual(healthy["runtime_error"], "none")
        self.assertEqual(healthy["audio_reason"], "none")
        self.assertEqual(healthy["audio_error"], "none")

        fixed_codes = (
            "peer_manifest_invalid",
            "peer_manifest_stale",
            "peer_manifest_replaced",
            "auto_accept_refused",
            "auto_accept_invalid",
            "auto_accept_policy_revoked",
            "private_scope_unverified",
            "core_not_ready",
            "group_admission_failed",
            "group_id_invalid",
            "group_join_failed",
            "group_join_response_invalid",
            "group_not_admitted",
            "group_room_changed",
            "group_status_failed",
            "group_status_invalid",
            "group_status_stale",
            "group_trigger_invalid",
            "call_occupied",
            "unsupported_unverified_group_join",
            "audio_fence_invalid",
            "capture_callback_failed",
            "capture_device_overflow",
            "capture_processing_failed",
            "capture_queue_overrun",
            "playback_callback_failed",
            "universal_api_capture_frame_invalid",
            "universal_api_capture_unsupported",
            "universal_api_protocol_invalid",
        )
        for code in fixed_codes:
            with self.subTest(code=code):
                projected = project_qqcall_status({
                    "status": {
                        "lastFailureCode": code,
                        "audio": {"lastFailureCode": code, "unavailableReason": code},
                    }
                })
                self.assertEqual(projected["runtime_error"], code)
                self.assertEqual(projected["audio_error"], code)
                self.assertEqual(projected["audio_reason"], code)

        private_code = "u_unknown_998877:private-token"
        projected = project_qqcall_status({
            "status": {
                "lastFailureCode": private_code,
                "audio": {"lastFailureCode": private_code, "unavailableReason": private_code},
            }
        })
        self.assertEqual(projected["runtime_error"], "diagnostic_error_unclassified")
        self.assertEqual(projected["audio_error"], "diagnostic_error_unclassified")
        self.assertEqual(projected["audio_reason"], "diagnostic_error_unclassified")


if __name__ == "__main__":
    unittest.main()
