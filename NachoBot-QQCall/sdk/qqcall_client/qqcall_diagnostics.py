"""Privacy-filtered, change-only status logging for QQCall adapter bindings."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from typing import Any, Callable


_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
_MAX_COUNT = (1 << 31) - 1
_MAX_DIAGNOSTIC_MS = 60_000
_ALLOWED_ENGINES = frozenset({"rms", "universalvc"})
_SAFE_GROUP_STATES = frozenset({"new", "starting", "active", "stopping", "stopped", "failed", "quarantined"})
_SAFE_ROUTE_STAGES = frozenset({
    "scope_validation",
    "pre_context",
    "helper_request",
    "post_context",
    "proof_validation",
    "completed",
})
_SAFE_ROUTE_FAILURE_REASONS = frozenset({
    "capture_lifecycle_fence",
    "desired_identity_mismatch",
    "desired_scope_mismatch",
    "identity_check_error",
    "identity_check_false",
    "identity_check_timeout",
    "route_capture_mode_unverified",
    "route_check_timeout",
    "route_context_unverified",
    "route_device_scope_invalid",
    "route_helper_rejected_unclassified",
    "route_helper_transport_error",
    "route_identity_mismatch",
    "route_identity_unavailable",
    "route_monitor_error",
    "route_plan_changed",
    "route_plan_mismatch",
    "route_plan_unavailable",
    "route_proof_invalid",
    "route_proof_stale",
    "route_response_invalid",
    "route_scope_invalid",
    "route_scope_mismatch",
    "route_scope_unverified",
    "route_verifier_unavailable",
    "route_verifier_error",
    "request_invalid",
    "platform_unsupported",
    "observer_busy",
    "observer_timeout",
    "com_initialization_failed",
    "observer_unavailable",
    "target_process_unavailable",
    "target_process_stale",
    "endpoint_not_found",
    "endpoint_ambiguous",
    "endpoint_metadata_unavailable",
    "session_enumeration_failed",
    "session_attribution_unavailable",
    "session_state_unavailable",
    "session_identity_unavailable",
    "shared_session_unverified",
    "routes_not_distinct",
    "host_input_not_active",
    "host_output_not_active",
    "endpoint_session_conflict",
    "observer_report_invalid",
    "observer_report_missing",
    "observer_aborted",
    "host_route_unverified",
})
_SAFE_ROUTE_HELPER_ERROR_CODES = frozenset({
    "adapter_lease_required",
    "bridge_status_unavailable",
    "call_not_connected",
    "call_scope_stale",
    "request_timeout",
    "service_busy",
    "service_error_unclassified",
    "service_not_started",
    "service_redirect_rejected",
    "service_response_invalid",
    "service_stopped",
    "service_timeout",
    "service_unavailable",
    "unauthorized",
})

# Runtime error fields can contain values supplied by lower-level clients.
# Only known fixed codes pass through; regex-shaped identities become a generic
# marker just like arbitrary strings.
_SAFE_ERROR_CODES = frozenset(
    """
    account_mismatch adapter_lease_required audio_already_started audio_backend_busy
    audio_backend_failed audio_backend_start_failed audio_chunk_invalid audio_cleanup_failed
    audio_config_incomplete audio_config_invalid audio_config_scope_mismatch audio_dependency_unavailable
    audio_device_ambiguous audio_device_unavailable audio_disabled audio_drain_timeout audio_endpoint_not_virtual
    audio_fence_invalid audio_format_invalid audio_format_unsupported audio_frame_invalid audio_generation_stale audio_pcm_unaligned
    audio_playback_busy audio_playback_cancelled audio_playback_queue_full audio_resampler_closed
    audio_resampler_state_invalid audio_routes_must_be_distinct audio_runtime_not_ready audio_scope_stale
    audio_sequence_invalid audio_start_failed audio_start_invalid audio_stream_invalid audio_stream_not_found
    audio_unavailable audio_wav_invalid audio_wav_unsupported call_not_connected call_scope_stale
    capture_callback_failed capture_device_overflow capture_discontinuity capture_failed capture_format_invalid
    capture_generation_changed capture_metadata_invalid capture_overflow capture_processing_failed capture_queue_overflow
    capture_queue_overrun capture_device_unavailable controller_invalidated core_asr_client_closed
    core_asr_failed core_asr_http_error core_asr_pcm_invalid core_asr_request_too_large core_asr_response_invalid
    core_asr_response_too_large core_asr_scope_invalid core_asr_sequence_invalid core_asr_stream_invalid
    core_asr_timeout core_asr_transport_failed core_asr_unavailable core_event_delivery_failed
    core_message_delivery_failed core_response_send_failed delivery_acceptance_unconfirmed delivery_rejected
    delivery_rejection_unconfirmed event_cursor_expired event_cursor_invalid event_delivery_failed
    host_route_unverified universal_api_route_unverified invite_callback_not_ready lease_release_failed local_endpoint_drained
    peer_admission_failed peer_not_admitted platform_identity_changed platform_state_invalid
    platform_state_unavailable playback_abort_failed playback_callback_failed playback_chunk_failed playback_device_unavailable
    playback_drain_unconfirmed playback_end_failed playback_failed playback_start_failed poll_failed
    request_failed request_outcome_unknown request_too_large runtime_busy runtime_failed runtime_not_ready
    runtime_stopped service_call_invalid service_command_invalid service_event_invalid service_http_error
    service_poll_invalid service_redirect_rejected service_response_invalid service_response_too_large
    service_timeout service_unavailable stale_audio_turn stale_call_scope token_file_invalid
    token_file_too_large token_file_unavailable universalvc_vad_unavailable universalvc_wav_encoding_failed
    utterance_audio_invalid utterance_delivery_failed utterance_duration_exceeded utterance_processing_failed
    utterance_rejected utterance_too_short voice_failed voice_payload_invalid voice_stream_event_invalid
    voice_stream_not_current voice_stream_payload_invalid voice_stream_sequence_invalid
    universal_api_capture_frame_invalid universal_api_capture_unsupported universal_api_protocol_invalid
    universalvc_module_unavailable universalvc_dependency_unavailable universalvc_model_unavailable
    peer_manifest_invalid peer_manifest_stale peer_manifest_replaced auto_accept_refused
    auto_accept_invalid auto_accept_policy_revoked private_scope_unverified core_not_ready
    group_admission_failed group_id_invalid group_join_failed group_join_response_invalid
    group_not_admitted group_room_changed group_status_failed group_status_invalid
    group_status_stale group_trigger_invalid call_occupied unsupported_unverified_group_join
    group_room_observed_empty
    group_audio_unavailable group_audio_start_failed group_audio_lease_unavailable group_audio_lease_lost
    group_audio_stopped group_native_quit_unconfirmed universal_api_unavailable stale_group_scope
    starting
    """.split()
) | _SAFE_ROUTE_FAILURE_REASONS


def _safe_error_code(value: Any) -> str:
    from .qqcall_dependencies import dependency_notice
    if isinstance(value, str) and dependency_notice(value) is not None:
        return value
    if value is None or value == "":
        return "none"
    if isinstance(value, str) and _CODE_PATTERN.fullmatch(value) and value in _SAFE_ERROR_CODES:
        return value
    return "diagnostic_error_unclassified"


def _safe_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, _MAX_COUNT)


def _safe_diagnostic_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return -1
    return max(-250, min(value, _MAX_DIAGNOSTIC_MS))


def _safe_route_failure_reason(value: Any) -> str:
    if value is None or isinstance(value, str) and not value:
        return "none"
    if isinstance(value, str) and value in _SAFE_ROUTE_FAILURE_REASONS:
        return value
    return "diagnostic_error_unclassified"


def _safe_route_stage(value: Any) -> str:
    if value is None:
        return "none"
    if type(value) is str and value in _SAFE_ROUTE_STAGES:
        return value
    return "none"


def _safe_route_helper_http_status(value: Any) -> int | str:
    if type(value) is int and 100 <= value <= 599:
        return value
    return "unknown"


def _safe_route_helper_error_code(value: Any) -> str:
    if value is None:
        return "none"
    if type(value) is str and value in _SAFE_ROUTE_HELPER_ERROR_CODES:
        return value
    return "service_error_unclassified"


def _safe_capability_flag(
    audio: Mapping[str, Any], key: str, *, universal_api: bool
) -> bool | str:
    value = audio.get(key)
    if type(value) is bool:
        return value
    if universal_api:
        return "unknown"
    return value is True


def project_qqcall_status(event: Any) -> dict[str, bool | int | str] | None:
    """Return only stable, allowlisted diagnostic primitives from a runtime event."""

    if not isinstance(event, Mapping):
        return None
    status = event.get("status")
    if not isinstance(status, Mapping):
        return None
    audio = status.get("audio")
    if not isinstance(audio, Mapping):
        audio = {}
    group_audio = status.get("groupAudio")
    if not isinstance(group_audio, Mapping):
        group_audio = {}
    group_state = group_audio.get("state")
    route_diagnostics = audio.get("routeDiagnostics")
    if not isinstance(route_diagnostics, Mapping):
        route_diagnostics = {}

    engine = audio.get("vadEngine")
    pipeline = audio.get("pipeline")
    universal_api = type(pipeline) is str and pipeline == "universal_api"
    return {
        "running": status.get("running") is True,
        "account_verified": status.get("accountVerified") is True,
        "lease_claimed": status.get("leaseClaimed") is True,
        "group_state": group_state if type(group_state) is str and group_state in _SAFE_GROUP_STATES else "unknown",
        "group_ready": group_audio.get("ready") is True,
        "group_error": _safe_error_code(group_audio.get("lastFailureCode")),
        "group_turns": _safe_count(group_audio.get("acceptedTurns")),
        "group_results": _safe_count(group_audio.get("forwardedResults")),
        "group_playbacks": _safe_count(group_audio.get("playbacks")),
        "group_quit_unconfirmed": group_audio.get("nativeQuitUnconfirmed") is True,
        "audio_ready": audio.get("ready") is True,
        "capturing": audio.get("capturing") is True,
        "vad_available": _safe_capability_flag(audio, "vadAvailable", universal_api=universal_api),
        "asr_streaming": _safe_capability_flag(audio, "asrStreaming", universal_api=universal_api),
        "vad_engine": engine if isinstance(engine, str) and engine in _ALLOWED_ENGINES else "unknown",
        "runtime_error": _safe_error_code(status.get("lastFailureCode")),
        "audio_error": _safe_error_code(audio.get("lastFailureCode")),
        "audio_reason": _safe_error_code(audio.get("unavailableReason")),
        "utterances_sent": _safe_count(audio.get("utterancesSent")),
        "utterances_dropped": _safe_count(audio.get("utterancesDropped")),
        "playback_drains": _safe_count(audio.get("playbackDrainCount")),
        "frames_received": _safe_count(audio.get("framesReceived")),
        "frames_sent": _safe_count(audio.get("framesSent")),
        "route_monitor_step_ms": _safe_diagnostic_ms(route_diagnostics.get("monitorStepMs")),
        "route_proof_age_ms": _safe_diagnostic_ms(route_diagnostics.get("proofAgeMs")),
        "route_monitor_checks": _safe_count(route_diagnostics.get("monitorChecks")),
        "route_identity_checks": _safe_count(route_diagnostics.get("identityChecks")),
        "route_failure_count": _safe_count(route_diagnostics.get("failureCount")),
        "route_first_failure": _safe_route_failure_reason(route_diagnostics.get("firstFailure")),
        "route_check_stage": _safe_route_stage(route_diagnostics.get("routeCheckStage")),
        "route_first_failure_stage": _safe_route_stage(route_diagnostics.get("firstFailureStage")),
        "route_helper_http_status": _safe_route_helper_http_status(route_diagnostics.get("routeHelperHttpStatus")),
        "route_helper_error_code": _safe_route_helper_error_code(route_diagnostics.get("routeHelperErrorCode")),
    }


def make_qqcall_status_callback(
    *,
    adapter: str,
    logger: Any,
    downstream: Callable[[dict[str, Any]], Any] | None = None,
) -> Callable[[dict[str, Any]], Any]:
    """Log a safe projection only when it changes, then preserve the caller hook."""

    if adapter not in {"napcat", "snowluma"}:
        raise ValueError("unsupported QQCall diagnostics adapter")
    last_projection: tuple[tuple[str, bool | int | str], ...] | None = None
    projection_lock = threading.Lock()

    def on_status(event: dict[str, Any]) -> Any:
        nonlocal last_projection
        projection = project_qqcall_status(event)
        if projection is not None:
            # Use stable state for deduplication, then include a bounded
            # metrics snapshot whenever that state changes.
            logged_fields = (
                "running", "account_verified", "lease_claimed", "audio_ready", "capturing",
                "vad_available", "asr_streaming", "vad_engine", "runtime_error", "audio_error",
                "audio_reason", "utterances_sent", "utterances_dropped", "playback_drains",
                "route_first_failure", "route_first_failure_stage", "route_helper_http_status",
                "route_helper_error_code",
                "group_state", "group_ready", "group_error", "group_turns", "group_results",
                "group_playbacks", "group_quit_unconfirmed",
            )
            signature = tuple((key, projection[key]) for key in logged_fields)
            with projection_lock:
                changed = signature != last_projection
                if changed:
                    last_projection = signature
            if changed:
                try:
                    logger.info(
                        "QQCall diagnostics adapter={} running={} account_verified={} lease_claimed={} "
                        "audio_ready={} capturing={} vad_available={} asr_streaming={} vad_engine={} "
                        "runtime_error={} audio_error={} audio_reason={} utterances_sent={} "
                        "utterances_dropped={} playback_drains={} route_first_failure={} "
                        "route_check_stage={} route_first_failure_stage={} route_helper_http_status={} "
                        "route_helper_error_code={} "
                        "frames_received={} frames_sent={} route_monitor_step_ms={} route_proof_age_ms={} "
                        "route_monitor_checks={} route_identity_checks={} route_failure_count={} "
                        "group_state={} group_ready={} group_error={} group_turns={} group_results={} "
                        "group_playbacks={} group_quit_unconfirmed={}",
                        adapter,
                        projection["running"],
                        projection["account_verified"],
                        projection["lease_claimed"],
                        projection["audio_ready"],
                        projection["capturing"],
                        projection["vad_available"],
                        projection["asr_streaming"],
                        projection["vad_engine"],
                        projection["runtime_error"],
                        projection["audio_error"],
                        projection["audio_reason"],
                        projection["utterances_sent"],
                        projection["utterances_dropped"],
                        projection["playback_drains"],
                        projection["route_first_failure"],
                        projection["route_check_stage"],
                        projection["route_first_failure_stage"],
                        projection["route_helper_http_status"],
                        projection["route_helper_error_code"],
                        projection["frames_received"],
                        projection["frames_sent"],
                        projection["route_monitor_step_ms"],
                        projection["route_proof_age_ms"],
                        projection["route_monitor_checks"],
                        projection["route_identity_checks"],
                        projection["route_failure_count"],
                        projection["group_state"],
                        projection["group_ready"],
                        projection["group_error"],
                        projection["group_turns"],
                        projection["group_results"],
                        projection["group_playbacks"],
                        projection["group_quit_unconfirmed"],
                    )
                except Exception:
                    pass
        if downstream is not None:
            return downstream(event)
        return None

    return on_status


__all__ = ["make_qqcall_status_callback", "project_qqcall_status"]
