"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const { projectPrivateScopeProof } = require("../shared/private-invite-scope.cjs");
const { isObserverReport } = require("../agent/official-av-observer.cjs");
const { projectKernelDiagnostics, projectGroupTransition } = require("./kernel-diagnostics.cjs");

const LOOPBACK = "127.0.0.1";
const MAX_BODY_BYTES = 16 * 1024;
const READ_TIMEOUT_MS = 5000;
const MAX_ACTIVE_REQUESTS = 64;
const MAX_QUEUED_COMMANDS = 16;
const AUDIO_ROUTE_TIMEOUT_MS = 2250;
const AUDIO_ROUTE_STOP_DRAIN_TIMEOUT_MS = 2250;
const JOURNALED_STATUS_KINDS = new Set([
  "snapshot", "attached", "controller_invalidated", "provider_invalidated", "host_availability",
  "invite_observed", "invite_callback_ready", "login_posted", "login_post_failed", "login_ready",
  "login_failed", "accept_posted", "accept_post_failed", "accept_acknowledged", "call_connected",
  "call_terminal", "stopped", "detach_unconfirmed",
]);
const VERIFIED_INVITE_CORRELATIONS = new Set([
  "kernel_from_uid_matched",
  "kernel_relation_uid_matched",
]);
const GROUP_SCOPE_KEYS = Object.freeze([
  "kind", "serviceInstance", "hostGeneration", "connectionGeneration", "groupId", "roomId", "roomGeneration",
]);
const SAFE_CODES = new Set([
  "invalid_kernel_diagnostics_shape", "kernel_diagnostics_unavailable", "kernel_diagnostics_busy",
  "invalid_official_av_diagnostics_shape", "official_av_observer_unavailable", "official_av_observer_invalid_result",
  "official_av_observer_busy", "official_av_observer_already_started", "official_av_observer_not_started",
  "official_av_observer_operation_failed",
  "group_invite_stale",
  "invalid_private_diagnostics_shape",
  "invalid_bridge", "invalid_control_token", "invalid_account_id", "invalid_control_port",
  "invalid_lease_duration", "invalid_journal_limit", "bridge_status_unavailable",
  "adapter_owned", "adapter_lease_required", "adapter_unavailable", "invalid_call_scope", "call_scope_stale",
  "account_mismatch", "private_scope_unverified",
  "service_stopped", "service_not_started", "service_busy", "invalid_poll_shape",
  "invalid_client_instance", "invalid_event_cursor", "event_cursor_invalid", "event_cursor_expired",
  "invalid_release_shape", "adapter_command_in_flight", "invalid_command_shape", "invalid_operation",
  "invite_callback_not_ready", "accept_post_not_confirmed", "unauthorized", "body_too_large",
  "invalid_content_length", "request_timeout", "invalid_json", "request_aborted", "request_read_failed",
  "invalid_path", "not_found", "content_type_required", "control_port_in_use", "control_listen_failed",
  "control_loopback_bind_failed", "command_failed", "operation_failed", "bridge_closing",
  "bridge_not_attached", "bridge_stopping", "bridge_generation_invalidated", "provider_generation_invalidated",
  "host_generation_invalidated", "controller_generation_invalidated", "host_unavailable", "login_not_ready",
  "call_not_current", "call_not_accepting", "accept_already_requested", "request_id_exhausted",
  "invalid_occupancy_shape",
  "host_post_receipt_invalid", "host_post_not_confirmed", "host_stopped", "host_not_ready",
  "invoke_timeout", "plugin_post_failed", "plugin_post_not_confirmed", "host_busy", "accept_post_failed",
  "accept_post_not_confirmed", "operation_rejected", "invalid_audio_route_shape", "invalid_audio_route_name",
  "audio_route_observer_invalid", "observer_timeout", "observer_aborted", "observer_busy",
  "observer_report_invalid", "observer_report_missing", "host_route_unverified", "call_not_connected",
  "endpoint_not_found", "endpoint_ambiguous", "endpoint_metadata_unavailable", "session_enumeration_failed",
  "session_attribution_unavailable", "session_state_unavailable", "session_identity_unavailable",
  "shared_session_unverified", "routes_not_distinct", "host_input_not_active", "host_output_not_active",
  "target_process_unavailable", "target_process_stale", "com_initialization_failed", "platform_unsupported",
  "observer_unavailable", "request_invalid",
  "endpoint_session_conflict",
  "invalid_audio_device_selection", "invalid_audio_device_name", "audio_device_name_invalid",
  "audio_device_protocol_unverified", "audio_device_selection_unverified", "audio_device_selection_timeout",
  "audio_device_selection_busy", "audio_device_selection_quarantined", "audio_device_not_found",
  "audio_device_name_ambiguous", "audio_device_not_selectable", "audio_device_selection_mismatch",
  "audio_device_map_changed", "audio_device_query_busy", "audio_device_query_quarantined",
  "audio_device_metadata_invalid", "audio_device_selection_failed", "audio_device_selection_receipt_invalid",
  "audio_device_query_failed", "invalid_accept_guard", "accept_guard_async",
  "invalid_group_status_shape", "invalid_group_join_shape", "invalid_group_id",
  "invalid_group_current_shape", "invalid_group_quit_shape", "invalid_group_scope",
  "invalid_requester_user_id", "invalid_trigger_kind", "call_occupied", "unsupported_unverified_group_join",
  "group_protocol_unavailable", "group_control_not_configured", "group_driver_unavailable",
  "group_scope_stale", "group_scope_unverified", "group_context_stale", "group_entry_unconfirmed",
  "group_start_scope_missing", "group_quit_unconfirmed", "group_room_query_receipt_invalid",
  "invalid_group_room_query", "invalid_group_start", "invalid_group_quit", "group_start_receipt_invalid",
  "group_quit_receipt_invalid", "group_room_query_failed", "group_start_failed", "group_quit_failed",
  "group_room_query_timeout", "group_room_query_quarantined", "group_room_query_busy",
  "group_room_observed_empty",
  "group_room_query_post_failed", "group_room_metadata_invalid",
  "group_start_refused", "group_quit_refused", "group_command_ack_ignored", "unsupported_group_command",
  "invalid_group_start_shape", "invalid_group_quit_shape", "invalid_self_uid", "generation_mismatch",
  "invalid_connection_generation", "invalid_host_generation", "invalid_service_instance", "invalid_group_scope",
  "group_request_id_exhausted", "group_scope_mismatch", "group_preflight_timeout", "preflight_timeout",
  "query_id_unavailable", "start_params_invalid", "start_refusal_conflicts_with_callback", "start_refused",
  "start_outcome_unknown", "start_timeout", "unexpected_creator", "other_call_slot_occupied",
  "room_generation_exhausted", "quit_outcome_unknown", "quit_refused", "quit_timeout",
  "membership_confirmation_unavailable", "membership_confirmation_failed", "membership_confirmation_stale",
  "membership_absence_unconfirmed", "host_stopped", "stale_context", "room_query_failed", "room_query_invalid",
  "room_not_existing", "slot_occupied", "clock_unavailable", "group_scope_mismatch",
]);
const BRIDGE_ERROR_CODES = new Set([
  "bridge_closing", "bridge_not_attached", "bridge_stopping", "bridge_generation_invalidated",
  "provider_generation_invalidated", "host_generation_invalidated", "controller_generation_invalidated",
  "host_unavailable", "login_not_ready", "call_not_current", "call_not_accepting",
  "invite_callback_not_ready", "accept_already_requested", "request_id_exhausted",
  "host_post_receipt_invalid", "host_post_not_confirmed", "host_stopped", "host_not_ready",
  "invoke_timeout", "plugin_post_failed", "plugin_post_not_confirmed", "host_busy",
  "accept_post_not_confirmed", "operation_rejected",
  "enter_relation_reused_requires_new_host", "enter_relation_history_full_requires_new_host",
  "invalid_audio_device_selection", "invalid_audio_device_name", "audio_device_name_invalid",
  "audio_device_protocol_unverified", "audio_device_selection_unverified", "audio_device_selection_timeout",
  "audio_device_selection_busy", "audio_device_selection_quarantined", "audio_device_not_found",
  "audio_device_name_ambiguous", "audio_device_not_selectable", "audio_device_selection_mismatch",
  "audio_device_map_changed", "audio_device_query_busy", "audio_device_query_quarantined",
  "audio_device_metadata_invalid", "audio_device_selection_failed", "audio_device_selection_receipt_invalid",
  "audio_device_query_failed", "invalid_accept_guard", "accept_guard_async",
]);

const PRIVATE_DIAGNOSTIC_LABELS = new Set([
  "idle", "pending", "invited", "invite_observed", "invite_ready", "accepting", "accept_posted",
  "accept_acknowledged", "connected", "ending", "ending_kernel_verified", "ending_unconfirmed",
  "failed", "expired", "ended", "not_requested", "selecting_devices", "posting", "posted", "acknowledged",
  "reject_acknowledged_unconfirmed", "reject_confirmed", "reject_posted", "hangup_posted",
  "hangup_acknowledged_unconfirmed", "hangup_confirmed", "reject_ack_timeout", "hangup_ack_timeout",
]);
const PRIVATE_DIAGNOSTIC_CODES = new Set([
  "native_accept_failed", "native_login_failed", "native_reject_failed", "native_hangup_failed",
  "room_entry_failed", "call_timeout", "invite_callback_after_call_expired", "invite_identity_unavailable",
  "invite_caller_mismatch", "invite_caller_unavailable", "reject_terminal_unconfirmed", "unmatched_destroy_reason",
  "official_private_invite_end_unconfirmed",
  "private_invite_lifecycle_stale", "private_invite_lifecycle_busy", "private_invite_window_unproven",
  "official_private_invite_unavailable",
  "native_reject_before_any_room_admission", "native_reject_and_room_absence", "kernel_current_room_destroyed",
  "private_host_room_release_unconfirmed", "avsdk_crash", "host_disconnected", "provider_disconnected",
]);

function privateDiagnosticCall(call) {
  if (!call || typeof call !== "object") return null;
  const result = {};
  for (const key of ["phase", "acceptState", "endState"]) {
    result[key] = PRIVATE_DIAGNOSTIC_LABELS.has(call[key]) ? call[key] : null;
  }
  for (const key of ["uncertain", "connected", "inviteCallbackReady", "remoteEndedConfirmed",
    "roomDestroyedConfirmed", "invitationRejectedConfirmed"]) {
    result[key] = typeof call[key] === "boolean" ? call[key] : null;
  }
  result.terminalReason = privateDiagnosticCode(call.terminalReason);
  return result;
}

function privateDiagnosticCode(code) {
  if (code === null || code === undefined) return null;
  return PRIVATE_DIAGNOSTIC_CODES.has(code) || SAFE_CODES.has(code) || BRIDGE_ERROR_CODES.has(code) ? code : "unrecognized";
}

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function positiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function validAccountId(value) {
  return typeof value === "string" && /^[1-9][0-9]{0,19}$/.test(value);
}

function validGroupId(value) {
  return validAccountId(value);
}

function validClientInstance(value) {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
}

function validCallId(value) {
  return typeof value === "string" && value.length >= 1 && value.length <= 128 && /^[A-Za-z0-9-]+$/.test(value);
}

function validEndpointName(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 256 &&
    value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value);
}

function ownKeysExactly(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function validToken(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(value);
}

function safeCode(error, fallback = "command_failed") {
  const code = typeof error?.code === "string" ? error.code : "";
  if (SAFE_CODES.has(code) || BRIDGE_ERROR_CODES.has(code) || /^unsupported_unverified_command:(?:hangup|reject)$/.test(code)) return code;
  return fallback;
}

function equalToken(expected, received) {
  if (typeof received !== "string") return false;
  const expectedBytes = Buffer.from(expected, "ascii");
  const receivedBytes = Buffer.from(received, "ascii");
  return expectedBytes.length === receivedBytes.length && crypto.timingSafeEqual(expectedBytes, receivedBytes);
}

function sendJson(response, statusCode, body) {
  if (response.destroyed || response.writableEnded) return;
  const bytes = Buffer.from(JSON.stringify(body), "utf8");
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": bytes.byteLength,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    Connection: "close",
  });
  response.end(bytes);
}

function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    const declaredHeader = request.headers["content-length"];
    if (declaredHeader !== undefined) {
      const declared = Number(declaredHeader);
      if (!Number.isSafeInteger(declared) || declared < 0) {
        request.resume();
        reject(codedError("invalid_content_length"));
        return;
      }
      if (declared > MAX_BODY_BYTES) {
        request.resume();
        reject(codedError("body_too_large"));
        return;
      }
    }

    let settled = false;
    let bytesRead = 0;
    const chunks = [];
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(value);
    };
    const timer = setTimeout(() => {
      request.resume();
      finish(codedError("request_timeout"));
    }, READ_TIMEOUT_MS);
    timer.unref?.();
    request.on("data", (chunk) => {
      if (settled) return;
      bytesRead += chunk.byteLength;
      if (bytesRead > MAX_BODY_BYTES) {
        request.resume();
        finish(codedError("body_too_large"));
        return;
      }
      chunks.push(chunk);
    });
    request.once("end", () => {
      if (settled) return;
      try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, bytesRead));
        finish(null, JSON.parse(text));
      } catch {
        finish(codedError("invalid_json"));
      }
    });
    request.once("aborted", () => finish(codedError("request_aborted")));
    request.once("error", () => finish(codedError("request_read_failed")));
  });
}

const BRIDGE_NATIVE_EVIDENCE_KEYS = [
  "profile", "wrapperSha256", "callbackSequence", "listenerRegistrationSequence", "inviteType", "relationId", "fromUid",
  "targetPid", "kernelCreationTime100ns", "connectionGeneration", "listenerMutationEpoch", "ipcSequence",
  "inviteEventId",
];

function projectBridgeNativeEvidence(value, context) {
  if (!ownKeysExactly(value, BRIDGE_NATIVE_EVIDENCE_KEYS) ||
      !positiveSafeInteger(value.listenerRegistrationSequence) ||
      typeof value.inviteEventId !== "string" || !/^[A-Za-z0-9-]{1,64}$/.test(value.inviteEventId)) return null;
  const evidence = {
    profile: value.profile,
    wrapperSha256: value.wrapperSha256,
    callbackSequence: value.callbackSequence,
    inviteType: value.inviteType,
    relationId: value.relationId,
    fromUid: value.fromUid,
  };
  const provenance = {
    targetPid: value.targetPid,
    kernelCreationTime100ns: value.kernelCreationTime100ns,
    connectionGeneration: value.connectionGeneration,
    listenerMutationEpoch: value.listenerMutationEpoch,
    ipcSequence: value.ipcSequence,
  };
  return projectPrivateScopeProof(evidence, provenance, {
    ...context,
    inviteEventId: value.inviteEventId,
  });
}

function samePrivateScopeProof(left, right) {
  if (!left || typeof left !== "object" || Array.isArray(left) ||
      !right || typeof right !== "object" || Array.isArray(right)) return false;
  let leftKeys;
  let rightKeys;
  try {
    leftKeys = Object.keys(left).sort();
    rightKeys = Object.keys(right).sort();
  } catch {
    return false;
  }
  if (leftKeys.length !== rightKeys.length || leftKeys.some((key, index) => key !== rightKeys[index])) return false;
  for (const key of leftKeys) {
    let leftDescriptor;
    let rightDescriptor;
    try {
      leftDescriptor = Object.getOwnPropertyDescriptor(left, key);
      rightDescriptor = Object.getOwnPropertyDescriptor(right, key);
    } catch {
      return false;
    }
    if (!leftDescriptor || !rightDescriptor || !("value" in leftDescriptor) || !("value" in rightDescriptor) ||
        leftDescriptor.value !== rightDescriptor.value) return false;
  }
  return true;
}

function journalEntryForCurrentSnapshot(entry, currentCall) {
  const projected = {
    ...entry,
    ...(entry.busyInvite ? { busyInvite: { ...entry.busyInvite } } : {}),
    ...(entry.groupInvite ? { groupInvite: { ...entry.groupInvite } } : {}),
  };
  if (entry.call?.privateScopeProof &&
      (!currentCall || entry.call.callId !== currentCall.callId ||
        !samePrivateScopeProof(entry.call.privateScopeProof, currentCall.privateScopeProof))) {
    projected.call = { ...entry.call };
    delete projected.call.privateScopeProof;
  }
  return projected;
}

function normalizedCall(value, context) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const callId = validCallId(value.callId) ? value.callId : null;
  if (!callId || !positiveSafeInteger(value.generation)) return null;
  const inviteCallbackReady = value.inviteCallbackReady === true;
  let peerUid = null;
  if (inviteCallbackReady && typeof value.peerUid === "string" && value.peerUid.length > 0 && value.peerUid.length <= 512 && !/[\u0000-\u001f\u007f]/.test(value.peerUid)) {
    peerUid = value.peerUid;
  }
  const enumOrNull = (entry, limit = 64) => typeof entry === "string" && entry.length <= limit && /^[a-z][a-z0-9_:-]*$/.test(entry) ? entry : null;
  const result = {
    callId,
    generation: value.generation,
    peerUid,
    phase: inviteCallbackReady ? enumOrNull(value.phase) : "awaiting_20006",
    uncertain: value.uncertain === true,
    inviteCallbackReady,
    inviteCorrelation: inviteCallbackReady ? enumOrNull(value.inviteCorrelation) : "awaiting_20006",
    acceptState: enumOrNull(value.acceptState),
  };
  if (inviteCallbackReady && peerUid && value.nativeInviteEvidence) {
    const proof = projectBridgeNativeEvidence(value.nativeInviteEvidence, {
      ...context,
      scopeKind: "call",
      callId,
      callGeneration: value.generation,
      identityCorrelation: value.inviteCorrelation,
      resolvedPeerUid: peerUid,
    });
    if (proof) result.privateScopeProof = proof;
  }
  return result;
}

function statusCodeFor(code) {
  if (code === "kernel_diagnostics_unavailable") return 503;
  if (code === "kernel_diagnostics_busy") return 409;
  if (code === "official_av_observer_unavailable") return 503;
  if (["official_av_observer_busy", "official_av_observer_already_started", "official_av_observer_not_started"].includes(code)) return 409;
  if (["official_av_observer_invalid_result", "official_av_observer_operation_failed"].includes(code)) return 502;
  if (code === "unauthorized") return 401;
  if (code === "not_found") return 404;
  if (code === "body_too_large") return 413;
  if (code === "request_timeout") return 408;
  if (code === "service_busy") return 429;
  if (code === "service_stopped" || code === "service_not_started" || code === "bridge_status_unavailable") return 503;
  if (code.startsWith("unsupported_unverified_command")) return 501;
  if (code === "unsupported_unverified_group_join") return 501;
  if (["group_protocol_unavailable", "group_control_not_configured", "group_driver_unavailable"].includes(code)) return 501;
  if (["audio_device_protocol_unverified", "audio_device_selection_unverified"].includes(code)) return 501;
  if (code === "audio_device_selection_timeout") return 504;
  if (["start_timeout", "quit_timeout", "group_room_query_timeout"].includes(code)) return 504;
  if (["group_room_query_quarantined", "group_room_query_busy", "group_room_observed_empty"].includes(code)) return 409;
  if (["group_room_query_post_failed", "group_room_metadata_invalid"].includes(code)) return 502;
  if (["group_room_query_failed", "group_start_failed", "group_quit_failed", "start_outcome_unknown",
    "quit_outcome_unknown", "membership_confirmation_failed", "membership_absence_unconfirmed"].includes(code)) return 502;
  if ([
    "audio_device_selection_busy", "audio_device_selection_quarantined", "audio_device_not_found",
    "audio_device_name_ambiguous", "audio_device_not_selectable", "audio_device_selection_mismatch",
    "audio_device_map_changed", "audio_device_query_busy", "audio_device_query_quarantined",
    "audio_device_metadata_invalid",
  ].includes(code)) return 409;
  if (["invalid_audio_device_selection", "invalid_audio_device_name", "audio_device_name_invalid"].includes(code)) return 400;
  if (["audio_device_selection_failed", "audio_device_selection_receipt_invalid", "audio_device_query_failed"].includes(code)) return 502;
  if ([
    "account_mismatch", "adapter_owned", "adapter_lease_required", "adapter_unavailable", "adapter_command_in_flight",
    "event_cursor_invalid", "event_cursor_expired", "call_scope_stale", "call_not_current",
    "call_occupied", "private_scope_unverified",
    "group_scope_stale", "group_room_not_existing", "group_context_stale", "group_scope_unverified",
    "start_refused", "room_not_existing", "slot_occupied", "other_call_slot_occupied", "unexpected_creator",
    "quit_refused", "preflight_timeout", "stale_context", "membership_confirmation_stale",
    "login_not_ready", "call_not_accepting", "call_not_connected", "invite_callback_not_ready", "accept_already_requested",
    "enter_relation_reused_requires_new_host", "enter_relation_history_full_requires_new_host",
  ].includes(code)) return 409;
  if (code === "command_failed") return 502;
  if (code.startsWith("invalid_") || code === "content_type_required" || code === "request_aborted" || code === "invalid_json") return 400;
  return 500;
}

function createControlService({ bridge, token, accountId, port = 0, leaseMs = 10_000, journalLimit = 128, audioRouteObserver = null, officialAVObserver = null, kernelStatusReader = null } = {}) {
  if (!bridge || typeof bridge.status !== "function" || typeof bridge.subscribeStatus !== "function" ||
      typeof bridge.subscribeControlEvents !== "function" || typeof bridge.getIntegrationSnapshot !== "function" || typeof bridge.accept !== "function" ||
      typeof bridge.hangup !== "function" || typeof bridge.reject !== "function") throw codedError("invalid_bridge");
  if (!validToken(token)) throw codedError("invalid_control_token");
  if (!validAccountId(accountId)) throw codedError("invalid_account_id");
  if (kernelStatusReader !== null && typeof kernelStatusReader !== "function") throw codedError("kernel_diagnostics_unavailable");
  if (audioRouteObserver !== null && typeof audioRouteObserver !== "function") throw codedError("audio_route_observer_invalid");
  if (officialAVObserver !== null && !["start", "drain", "stop"].every((key) => typeof officialAVObserver?.[key] === "function")) {
    throw codedError("official_av_observer_unavailable");
  }
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw codedError("invalid_control_port");
  if (!Number.isSafeInteger(leaseMs) || leaseMs < 1000 || leaseMs > 120_000) throw codedError("invalid_lease_duration");
  if (!Number.isSafeInteger(journalLimit) || journalLimit < 1 || journalLimit > 4096) throw codedError("invalid_journal_limit");

  const serviceInstance = crypto.randomUUID();
  if (typeof bridge.configureGroupControl === "function") {
    // ControlService is normally created before login so group ownership can
    // bind its UUID once. Preserve established private-control construction
    // after login; Bridge then rejects group operations as unconfigured.
    try { bridge.configureGroupControl(serviceInstance); } catch { /* group routes fail closed */ }
  }
  const eventJournal = [];
  let sequence = 0;
  let currentLease = null;
  let adapterAvailable = true;
  let adapterPausePromise = null;
  let server = null;
  let actualPort = port;
  let started = false;
  let stopped = false;
  let startPromise = null;
  let stopPromise = null;
  let unsubscribeStatus = null;
  let unsubscribeControlEvents = null;
  let activeRequests = 0;
  let commandRunning = false;
  let commandQueue = [];
  let commandDrainPromise = null;
  let commandPending = 0;
  const activeAudioObservations = new Set();
  let groupLeaseBinding = null;
  let officialAVRequested = false;
  let officialAVBusy = false;
  let officialAVPending = null;

  function integrationSnapshot() {
    let value;
    try { value = bridge.getIntegrationSnapshot(); }
    catch { throw codedError("bridge_status_unavailable"); }
    if (!value || typeof value !== "object" || !value.status || typeof value.status !== "object") {
      throw codedError("bridge_status_unavailable");
    }
    const hostGeneration = positiveSafeInteger(value.status.hostGeneration) ? value.status.hostGeneration : null;
    const connectionGeneration = positiveSafeInteger(value.status.connectionGeneration) ? value.status.connectionGeneration : null;
    const call = normalizedCall(value.call, {
      accountId,
      serviceInstance,
      hostGeneration,
      connectionGeneration,
    });
    return { status: value.status, call, hostGeneration, connectionGeneration };
  }

  function appendEvent(kind) {
    if (!JOURNALED_STATUS_KINDS.has(kind)) return;
    const snapshot = integrationSnapshot();
    sequence += 1;
    eventJournal.push({ sequence, kind, call: snapshot.call });
    if (eventJournal.length > journalLimit) eventJournal.splice(0, eventJournal.length - journalLimit);
  }

  function appendBusyInvite(event) {
    if (event?.kind === "group_invite") {
      const invite = event.groupInvite;
      if (!ownKeysExactly(invite, ["inviteId", "groupId", "callerUid", "observedAtMs"]) ||
          !validCallId(invite.inviteId) || !validGroupId(invite.groupId) ||
          typeof invite.callerUid !== "string" || !/^u_[A-Za-z0-9_-]{22}$/.test(invite.callerUid) ||
          !Number.isSafeInteger(invite.observedAtMs) || Date.now() - invite.observedAtMs < 0 ||
          Date.now() - invite.observedAtMs >= 30_000) return;
      sequence += 1;
      eventJournal.push({ sequence, kind: "group_invite", call: null, groupInvite: { ...invite } });
      if (eventJournal.length > journalLimit) eventJournal.splice(0, eventJournal.length - journalLimit);
      return;
    }
    const busyInvite = event?.busyInvite;
    if (event?.kind !== "invite_busy" || !busyInvite || typeof busyInvite !== "object" || Array.isArray(busyInvite)) return;
    const callerUid = busyInvite.callerUid;
    const identityCorrelation = busyInvite.identityCorrelation;
    if (typeof callerUid !== "string" || callerUid.length < 1 || callerUid.length > 512 ||
        /[\u0000-\u001f\u007f]/.test(callerUid) || !VERIFIED_INVITE_CORRELATIONS.has(identityCorrelation)) return;
    const snapshot = integrationSnapshot();
    const proof = projectBridgeNativeEvidence(busyInvite.nativeInviteEvidence, {
      accountId,
      serviceInstance,
      hostGeneration: snapshot.hostGeneration,
      connectionGeneration: snapshot.connectionGeneration,
      scopeKind: "busy",
      callId: null,
      callGeneration: null,
      identityCorrelation,
      resolvedPeerUid: callerUid,
    });
    if (!proof) return;
    sequence += 1;
    eventJournal.push({
      sequence,
      kind: "invite_busy",
      call: null,
      busyInvite: { callerUid, identityCorrelation, privateScopeProof: proof },
    });
    if (eventJournal.length > journalLimit) eventJournal.splice(0, eventJournal.length - journalLimit);
  }

  function expireLease() {
    if (currentLease && currentLease.inFlight === 0 && Date.now() >= currentLease.expiresAt) currentLease = null;
  }

  function checkAccount(value) {
    if (value !== accountId) throw codedError("account_mismatch");
  }

  function claimLease(clientInstance) {
    if (!adapterAvailable) throw codedError("adapter_unavailable");
    expireLease();
    if (currentLease && currentLease.clientInstance !== clientInstance) throw codedError("adapter_owned");
    if (!currentLease) currentLease = { clientInstance, expiresAt: 0, inFlight: 0 };
    currentLease.expiresAt = Date.now() + leaseMs;
    return currentLease;
  }

  function requireLease(clientInstance, { refresh = true } = {}) {
    if (!adapterAvailable) throw codedError("adapter_unavailable");
    expireLease();
    if (!currentLease || currentLease.clientInstance !== clientInstance) {
      if (currentLease) throw codedError("adapter_owned");
      throw codedError("adapter_lease_required");
    }
    if (refresh) currentLease.expiresAt = Date.now() + leaseMs;
    return currentLease;
  }

  function validateScope(value) {
    const keys = ["serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration"];
    if (!ownKeysExactly(value, keys) || value.serviceInstance !== serviceInstance ||
        !positiveSafeInteger(value.hostGeneration) || !positiveSafeInteger(value.connectionGeneration) ||
        !validCallId(value.callId) || !positiveSafeInteger(value.callGeneration)) throw codedError("invalid_call_scope");
    return {
      serviceInstance: value.serviceInstance,
      hostGeneration: value.hostGeneration,
      connectionGeneration: value.connectionGeneration,
      callId: value.callId,
      callGeneration: value.callGeneration,
    };
  }

  function ensureScopeCurrent(scope, snapshot) {
    if (!snapshot.call || scope.serviceInstance !== serviceInstance ||
        scope.hostGeneration !== snapshot.hostGeneration ||
        scope.connectionGeneration !== snapshot.connectionGeneration ||
        scope.callId !== snapshot.call.callId || scope.callGeneration !== snapshot.call.generation) {
      throw codedError("call_scope_stale");
    }
  }

  function ensureAudioCallReady(snapshot) {
    if (!snapshot.call || snapshot.call.phase !== "connected" || snapshot.call.uncertain !== false) {
      throw codedError("call_not_connected");
    }
  }

  function routeNegative(scope, code) {
    return {
      version: 1,
      accountId,
      scope,
      verified: false,
      code: SAFE_CODES.has(code) ? code : "host_route_unverified",
    };
  }

  function sanitizedRouteProof(value, requestBody) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    if (value.verified === false && ownKeysExactly(value, ["verified", "code"])) {
      const code = typeof value.code === "string" && SAFE_CODES.has(value.code) ? value.code : "host_route_unverified";
      return routeNegative(requestBody.scope, code);
    }
    if (!value.verified || !ownKeysExactly(value, [
      "verified", "source", "hostPid", "observedAtMs", "hostInput", "hostOutput",
    ]) || value.source !== "windows_active_sessions" || !positiveSafeInteger(value.hostPid) ||
        !Number.isSafeInteger(value.observedAtMs)) return null;
    const ageMs = Date.now() - value.observedAtMs;
    if (ageMs < -250 || ageMs > 2500) return null;
    const validRoute = (entry, { deviceName, flow }) => entry && typeof entry === "object" && !Array.isArray(entry) &&
      ownKeysExactly(entry, ["deviceName", "flow", "state", "endpointIdHash", "processId"]) &&
      entry.deviceName === deviceName && entry.flow === flow && entry.state === "active" &&
      typeof entry.endpointIdHash === "string" && /^[a-f0-9]{64}$/i.test(entry.endpointIdHash) &&
      Number.isSafeInteger(entry.processId) && entry.processId > 0 && entry.processId <= 0xffffffff;
    if (!validRoute(value.hostInput, { deviceName: requestBody.playbackRenderName, flow: "capture" }) ||
        !validRoute(value.hostOutput, { deviceName: requestBody.captureRenderName, flow: "render" }) ||
        value.hostInput.endpointIdHash.toLowerCase() === value.hostOutput.endpointIdHash.toLowerCase()) return null;
    return {
      version: 1,
      accountId,
      scope: requestBody.scope,
      verified: true,
      source: "windows_active_sessions",
      hostPid: value.hostPid,
      observedAtMs: value.observedAtMs,
      hostInput: { ...value.hostInput, endpointIdHash: value.hostInput.endpointIdHash.toLowerCase() },
      hostOutput: { ...value.hostOutput, endpointIdHash: value.hostOutput.endpointIdHash.toLowerCase() },
    };
  }

  function ensureAudioRouteRequestCurrent(body, lease) {
    if (stopped) throw codedError("service_stopped");
    checkAccount(body.accountId);
    const activeLease = requireLease(body.clientInstance);
    if (activeLease !== lease) throw codedError("adapter_lease_required");
    const snapshot = integrationSnapshot();
    ensureScopeCurrent(body.scope, snapshot);
    ensureAudioCallReady(snapshot);
    return snapshot;
  }

  function ensureGroupAudioRouteRequestCurrent(body, lease) {
    if (stopped) throw codedError("service_stopped");
    checkAccount(body.accountId);
    const activeLease = requireLease(body.clientInstance);
    if (activeLease !== lease) throw codedError("adapter_lease_required");
    if (body.scope.serviceInstance !== serviceInstance || typeof bridge.getCurrentGroupScope !== "function") {
      throw codedError("group_scope_stale");
    }
    const snapshot = integrationSnapshot();
    if (!Object.hasOwn(snapshot.status, "activeCall")) throw codedError("bridge_status_unavailable");
    if ((snapshot.status.activeCall !== null && snapshot.status.activeCall !== undefined) || snapshot.call !== null) {
      throw codedError("call_occupied");
    }
    let liveScope;
    try { liveScope = bridge.getCurrentGroupScope(); }
    catch { throw codedError("bridge_status_unavailable"); }
    if (!sameGroupScope(body.scope, liveScope) || body.scope.hostGeneration !== snapshot.hostGeneration ||
        body.scope.connectionGeneration !== snapshot.connectionGeneration) throw codedError("group_scope_stale");
    return snapshot;
  }

  async function audioRoute(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "scope", "captureRenderName", "playbackRenderName"])) {
      throw codedError("invalid_audio_route_shape");
    }
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    let scope;
    let groupRoute = false;
    if (ownKeysExactly(body.scope, ["serviceInstance", "hostGeneration", "connectionGeneration", "callId", "callGeneration"])) {
      scope = validateScope(body.scope);
    } else if (ownKeysExactly(body.scope, GROUP_SCOPE_KEYS)) {
      scope = validateGroupScope(body.scope, { requireServiceInstance: false });
      groupRoute = true;
    } else {
      throw codedError("invalid_call_scope");
    }
    if (!validEndpointName(body.captureRenderName) || !validEndpointName(body.playbackRenderName) ||
        body.captureRenderName === body.playbackRenderName) throw codedError("invalid_audio_route_name");
    const lease = requireLease(body.clientInstance);
    let snapshot;
    if (groupRoute) snapshot = ensureGroupAudioRouteRequestCurrent({ ...body, scope }, lease);
    else {
      snapshot = integrationSnapshot();
      ensureScopeCurrent(scope, snapshot);
      ensureAudioCallReady(snapshot);
    }
    if (!audioRouteObserver) return routeNegative(scope, "host_route_unverified");

    lease.inFlight += 1;
    let pinReleased = false;
    const releasePin = () => {
      if (pinReleased) return;
      pinReleased = true;
      if (lease.inFlight > 0) lease.inFlight -= 1;
    };
    const controller = new AbortController();
    let deferPinRelease = false;
    let observerSettled = false;
    const record = { controller, done: null, releasePin: null };
    const observerWork = Promise.resolve().then(() => audioRouteObserver({
      accountId,
      clientInstance: body.clientInstance,
      scope,
      captureRenderName: body.captureRenderName,
      playbackRenderName: body.playbackRenderName,
      signal: controller.signal,
    })).then(
      (value) => { observerSettled = true; return { kind: "result", value }; },
      (error) => { observerSettled = true; return { kind: "error", error }; },
    );
    record.releasePin = releasePin;
    record.done = observerWork.then(() => {
      activeAudioObservations.delete(record);
      if (deferPinRelease) releasePin();
    });
    activeAudioObservations.add(record);

    let abortResolve;
    const aborted = new Promise((resolve) => { abortResolve = resolve; });
    const onAbort = () => abortResolve({ kind: "aborted", reason: controller.signal.reason });
    controller.signal.addEventListener("abort", onAbort, { once: true });
    let timeoutHandle;
    const timedOut = new Promise((resolve) => {
      timeoutHandle = setTimeout(() => resolve({ kind: "timeout" }), AUDIO_ROUTE_TIMEOUT_MS);
      timeoutHandle.unref?.();
    });
    try {
      const outcome = await Promise.race([observerWork, aborted, timedOut]);
      if (outcome.kind === "timeout") {
        deferPinRelease = !observerSettled;
        controller.abort("observer_timeout");
        if (groupRoute) ensureGroupAudioRouteRequestCurrent({ ...body, scope }, lease);
        else ensureAudioRouteRequestCurrent({ ...body, scope }, lease);
        return routeNegative(scope, "observer_timeout");
      }
      if (outcome.kind === "aborted") {
        deferPinRelease = !observerSettled;
        if (outcome.reason === "service_stopped" || stopped) throw codedError("service_stopped");
        if (groupRoute) ensureGroupAudioRouteRequestCurrent({ ...body, scope }, lease);
        else ensureAudioRouteRequestCurrent({ ...body, scope }, lease);
        return routeNegative(scope, "observer_aborted");
      }
      if (groupRoute) ensureGroupAudioRouteRequestCurrent({ ...body, scope }, lease);
      else ensureAudioRouteRequestCurrent({ ...body, scope }, lease);
      if (outcome.kind === "error") {
        const code = safeCode(outcome.error, "host_route_unverified");
        return routeNegative(scope, SAFE_CODES.has(code) ? code : "host_route_unverified");
      }
      return sanitizedRouteProof(outcome.value, body) || routeNegative(scope, "observer_report_invalid");
    } finally {
      clearTimeout(timeoutHandle);
      controller.signal.removeEventListener("abort", onAbort);
      if (observerSettled || !deferPinRelease) {
        activeAudioObservations.delete(record);
        releasePin();
      }
    }
  }

  function enqueueCommand(clientInstance, operation) {
    if (stopped) return Promise.reject(codedError("service_stopped"));
    if (commandPending >= MAX_QUEUED_COMMANDS) return Promise.reject(codedError("service_busy"));
    const lease = requireLease(clientInstance);
    commandPending += 1;
    const result = new Promise((resolve, reject) => {
      commandQueue.push({ clientInstance, lease, operation, resolve, reject });
    });
    void drainCommands();
    return result;
  }

  async function drainCommands() {
    if (commandRunning) return commandDrainPromise;
    commandRunning = true;
    commandDrainPromise = (async () => {
      while (commandQueue.length) {
        const item = commandQueue.shift();
        if (stopped) {
          item.reject(codedError("service_stopped"));
          commandPending -= 1;
          continue;
        }
        let executingLease = null;
        try {
          if (item.leaseExpired) throw codedError("adapter_lease_required");
          executingLease = requireLease(item.clientInstance);
          if (executingLease !== item.lease) throw codedError("adapter_lease_required");
          executingLease.inFlight += 1;
          item.resolve(await item.operation());
        } catch (error) {
          item.reject(error);
        } finally {
          if (executingLease === item.lease && executingLease.inFlight > 0) {
            const completedAt = Date.now();
            if (!stopped && currentLease === executingLease) {
              // Preserve the expiry of work already waiting when the last pin
              // drops. A response window must not revive stale queued work.
              if (executingLease.inFlight === 1 && completedAt >= executingLease.expiresAt) {
                for (const queued of commandQueue) {
                  if (queued.lease === executingLease) queued.leaseExpired = true;
                }
              }
              // Native entry can take longer than leaseMs. Give the exact owner
              // time to consume its response and request the next proof.
              executingLease.expiresAt = completedAt + leaseMs;
            }
            executingLease.inFlight -= 1;
          }
          commandPending -= 1;
        }
      }
    })().finally(() => {
      commandRunning = false;
      commandDrainPromise = null;
      if (commandQueue.length) void drainCommands();
    });
    return commandDrainPromise;
  }

  function validatePoll(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "after"])) throw codedError("invalid_poll_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    if (!Number.isSafeInteger(body.after) || body.after < 0) throw codedError("invalid_event_cursor");
    const oldest = eventJournal[0]?.sequence ?? sequence + 1;
    if (body.after > sequence) throw codedError("event_cursor_invalid");
    if (body.after !== 0 && body.after < oldest - 1) throw codedError("event_cursor_expired");
    return body;
  }

  async function poll(body) {
    validatePoll(body);
    claimLease(body.clientInstance);
    const snapshot = integrationSnapshot();
    const events = eventJournal.filter((entry) => entry.sequence > body.after)
      .map((entry) => journalEntryForCurrentSnapshot(entry, snapshot.call));
    return {
      version: 1,
      accountId,
      serviceInstance,
      hostGeneration: snapshot.hostGeneration,
      connectionGeneration: snapshot.connectionGeneration,
      cursor: sequence,
      events,
      currentCall: snapshot.call,
    };
  }

  async function release(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance"])) throw codedError("invalid_release_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    const lease = requireLease(body.clientInstance, { refresh: false });
    if (lease.inFlight > 0) throw codedError("adapter_command_in_flight");
    currentLease = null;
    return { version: 1, accountId, serviceInstance, released: true };
  }

  let kernelDiagnosticsBusy = false;
  const groupTransitions = [];
  async function kernelDiagnostics(body) {
    if (!ownKeysExactly(body, ["accountId"])) throw codedError("invalid_kernel_diagnostics_shape");
    checkAccount(body.accountId);
    if (!kernelStatusReader) throw codedError("kernel_diagnostics_unavailable");
    if (kernelDiagnosticsBusy) throw codedError("kernel_diagnostics_busy");
    kernelDiagnosticsBusy = true;
    try {
      const before = integrationSnapshot();
      if (!before.hostGeneration || !before.connectionGeneration) throw codedError("bridge_status_unavailable");
      let reply;
      try { reply = await kernelStatusReader(); }
      catch { throw codedError("kernel_diagnostics_unavailable"); }
      const after = integrationSnapshot();
      if (stopped || after.hostGeneration !== before.hostGeneration || after.connectionGeneration !== before.connectionGeneration ||
          ["stopping", "closing", "stopped", "providerInvalidated", "controllerInvalidated", "hostInvalidated"].some(key => after.status[key] === true)) {
        throw codedError("bridge_status_unavailable");
      }
      const diagnostic = projectKernelDiagnostics(reply, after.connectionGeneration);
      if (!diagnostic) throw codedError("kernel_diagnostics_unavailable");
      return { version: 1, diagnosticOnly: true, hostGeneration: after.hostGeneration,
        connectionGeneration: after.connectionGeneration, diagnostic,
        groupTransitions: groupTransitions.filter(item => item.hostGeneration === after.hostGeneration &&
          item.connectionGeneration === after.connectionGeneration).map(item => ({ ...item })) };
    } finally { kernelDiagnosticsBusy = false; }
  }

  function privateDiagnostics(body) {
    if (!ownKeysExactly(body, ["accountId"])) throw codedError("invalid_private_diagnostics_shape");
    checkAccount(body.accountId);
    const snapshot = bridge.status();
    if (!positiveSafeInteger(snapshot?.hostGeneration) || !positiveSafeInteger(snapshot?.connectionGeneration)) {
      throw codedError("bridge_status_unavailable");
    }
    const counts = {};
    for (const key of ["kernelEvents", "hostOutputs", "kernelSignals", "busyInvites", "unmatchedInviteCallbacks",
      "duplicateInviteCallbacks", "duplicateKernelInvites", "uncorrelatedAcks", "pendingAcks", "queued20001", "outstanding20001"]) {
      counts[key] = Number.isSafeInteger(snapshot.counts?.[key]) && snapshot.counts[key] >= 0 ? snapshot.counts[key] : null;
    }
    const result = { version: 1, diagnosticOnly: true, hostGeneration: snapshot.hostGeneration,
      connectionGeneration: snapshot.connectionGeneration, activeCall: privateDiagnosticCall(snapshot.activeCall),
      lastCall: privateDiagnosticCall(snapshot.lastCall), lastFailureCode: privateDiagnosticCode(snapshot.lastFailureCode), counts };
    const current = bridge.status();
    if (current?.hostGeneration !== snapshot.hostGeneration || current?.connectionGeneration !== snapshot.connectionGeneration) {
      throw codedError("bridge_status_unavailable");
    }
    return result;
  }

  async function occupancy(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance"])) throw codedError("invalid_occupancy_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    const lease = requireLease(body.clientInstance);
    const snapshot = integrationSnapshot();
    if (!Object.hasOwn(snapshot.status, "activeCall")) throw codedError("bridge_status_unavailable");

    const privateOccupied = (snapshot.status.activeCall !== null && snapshot.status.activeCall !== undefined) ||
      snapshot.call !== null;
    const readGroupOccupied = (value) => {
      const fields = ["groupCall", "groupCallStatus"].filter((key) => Object.hasOwn(value.status, key));
      let occupied = null;
      for (const key of fields) {
        const groupStatus = value.status[key];
        if (!groupStatus || typeof groupStatus !== "object" || typeof groupStatus.occupied !== "boolean" ||
            (occupied !== null && occupied !== groupStatus.occupied)) throw codedError("bridge_status_unavailable");
        occupied = groupStatus.occupied;
      }
      if (occupied !== null) return occupied;
      if (typeof bridge.getCurrentGroupScope === "function") {
        let liveScope;
        try { liveScope = bridge.getCurrentGroupScope(); }
        catch { throw codedError("bridge_status_unavailable"); }
        if (liveScope === null) return false;
        try { validateGroupScope(liveScope, { requireServiceInstance: false }); }
        catch { throw codedError("bridge_status_unavailable"); }
        return true;
      }
      return false;
    };
    const groupOccupied = readGroupOccupied(snapshot);
    if (privateOccupied && groupOccupied) throw codedError("bridge_status_unavailable");

    const finalLease = requireLease(body.clientInstance, { refresh: false });
    const finalSnapshot = integrationSnapshot();
    if (finalLease !== lease || finalSnapshot.hostGeneration !== snapshot.hostGeneration ||
        finalSnapshot.connectionGeneration !== snapshot.connectionGeneration ||
        !Object.hasOwn(finalSnapshot.status, "activeCall")) throw codedError("bridge_status_unavailable");
    const finalPrivate = finalSnapshot.status.activeCall !== null && finalSnapshot.status.activeCall !== undefined ||
      finalSnapshot.call !== null;
    const finalGroup = readGroupOccupied(finalSnapshot);
    if (finalPrivate && finalGroup) throw codedError("bridge_status_unavailable");
    return {
      version: 1,
      accountId,
      serviceInstance,
      hostGeneration: finalSnapshot.hostGeneration,
      connectionGeneration: finalSnapshot.connectionGeneration,
      occupied: finalPrivate || finalGroup,
      kind: finalPrivate ? "private" : finalGroup ? "group" : null,
    };
  }

  function validateGroupStatus(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "groupId"])) throw codedError("invalid_group_status_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    if (!validGroupId(body.groupId)) throw codedError("invalid_group_id");
    return body;
  }

  function validateGroupJoin(body) {
    const keys = ["accountId", "clientInstance", "groupId", "requesterUserId", "triggerKind"];
    if (body?.triggerKind === "invite") keys.push("inviteId");
    if (!ownKeysExactly(body, keys)) {
      throw codedError("invalid_group_join_shape");
    }
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    if (!validGroupId(body.groupId)) throw codedError("invalid_group_id");
    if (!validAccountId(body.requesterUserId)) throw codedError("invalid_requester_user_id");
    if (!["command", "natural", "invite"].includes(body.triggerKind) ||
        (body.triggerKind === "invite" && !validCallId(body.inviteId))) throw codedError("invalid_trigger_kind");
    return body;
  }

  function validateGroupScope(value, { requireServiceInstance = true } = {}) {
    if (!ownKeysExactly(value, GROUP_SCOPE_KEYS) || value.kind !== "qqcall_group" ||
        (requireServiceInstance && value.serviceInstance !== serviceInstance) ||
        typeof value.serviceInstance !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value.serviceInstance) ||
        !positiveSafeInteger(value.hostGeneration) ||
        !positiveSafeInteger(value.connectionGeneration) || !validGroupId(value.groupId) ||
        !validGroupId(value.roomId) || !positiveSafeInteger(value.roomGeneration)) {
      throw codedError("invalid_group_scope");
    }
    return {
      kind: value.kind,
      serviceInstance: value.serviceInstance,
      hostGeneration: value.hostGeneration,
      connectionGeneration: value.connectionGeneration,
      groupId: value.groupId,
      roomId: value.roomId,
      roomGeneration: value.roomGeneration,
    };
  }

  function sameGroupScope(left, right) {
    return Boolean(left && right && GROUP_SCOPE_KEYS.every((key) => left[key] === right[key]));
  }

  function callSlotOccupied(snapshot) {
    if (!Object.hasOwn(snapshot.status, "activeCall")) throw codedError("bridge_status_unavailable");
    const activeCall = snapshot.status.activeCall;
    let groupOccupied = Boolean(snapshot.status.groupCall?.occupied || snapshot.status.groupCallStatus?.occupied);
    if (!groupOccupied && typeof bridge.getCurrentGroupScope === "function") {
      try { groupOccupied = bridge.getCurrentGroupScope() !== null; }
      catch { throw codedError("bridge_status_unavailable"); }
    }
    return (activeCall !== undefined && activeCall !== null) || Boolean(snapshot.call) || groupOccupied;
  }

  async function groupStatus(body) {
    validateGroupStatus(body);
    requireLease(body.clientInstance);
    const snapshot = integrationSnapshot();
    let occupied = callSlotOccupied(snapshot);
    let room = null;
    if (!occupied && typeof bridge.getGroupRoomStatus === "function") {
      const proof = await bridge.getGroupRoomStatus(body.groupId);
      requireLease(body.clientInstance, { refresh: false });
      const current = integrationSnapshot();
      if (current.hostGeneration !== snapshot.hostGeneration ||
          current.connectionGeneration !== snapshot.connectionGeneration) {
        throw codedError("bridge_status_unavailable");
      }
      occupied = callSlotOccupied(current);
      if (!proof || proof.ok !== true || proof.serviceInstance !== serviceInstance ||
          proof.hostGeneration !== snapshot.hostGeneration ||
          proof.connectionGeneration !== snapshot.connectionGeneration || proof.groupId !== body.groupId ||
          typeof proof.ready !== "boolean" || !positiveSafeInteger(proof.roomGeneration)) {
        throw codedError("group_room_query_receipt_invalid");
      }
      if (!occupied && proof.ready === true) {
        if (!validGroupId(proof.logicalRoomId)) throw codedError("group_room_query_receipt_invalid");
        room = {
          groupId: body.groupId,
          roomId: proof.logicalRoomId,
          roomGeneration: proof.roomGeneration,
          live: true,
          observedAtMs: Date.now(),
        };
      }
    }
    return {
      version: 1,
      accountId,
      serviceInstance,
      hostGeneration: snapshot.hostGeneration,
      connectionGeneration: snapshot.connectionGeneration,
      groupId: body.groupId,
      occupied,
      // Admission proof for an existing native room; joined/current still
      // require the correlated scene-3 entry callback and a leased scope.
      nativeJoinVerified: room !== null,
      room,
    };
  }

  async function groupJoin(body) {
    validateGroupJoin(body);
    return enqueueCommand(body.clientInstance, async () => {
      requireLease(body.clientInstance);
      checkAccount(body.accountId);
      const snapshot = integrationSnapshot();
      if (callSlotOccupied(snapshot)) throw codedError("call_occupied");
      if (typeof bridge.joinGroup !== "function" || typeof bridge.getCurrentGroupScope !== "function") {
        throw codedError("unsupported_unverified_group_join");
      }
      let joined;
      try { joined = await bridge.joinGroup(body.groupId, body.triggerKind === "invite" ? body.inviteId : null); }
      catch (error) {
        const code = safeCode(error);
        if (["group_protocol_unavailable", "group_control_not_configured", "group_driver_unavailable"].includes(code)) {
          throw codedError("unsupported_unverified_group_join");
        }
        throw codedError(code);
      }
      const current = integrationSnapshot();
      if (current.hostGeneration !== snapshot.hostGeneration || current.connectionGeneration !== snapshot.connectionGeneration ||
          requireLease(body.clientInstance, { refresh: false }) === null) throw codedError("bridge_status_unavailable");
      if (joined?.joined !== true || joined.source !== "native_scene3_relation") throw codedError("group_entry_unconfirmed");
      const scope = validateGroupScope(joined.scope);
      const liveScope = bridge.getCurrentGroupScope();
      if (!sameGroupScope(scope, liveScope) || scope.groupId !== body.groupId) throw codedError("group_scope_unverified");
      groupLeaseBinding = { clientInstance: body.clientInstance, scope, requesterUserId: body.requesterUserId };
      return {
        version: 1,
        accountId,
        serviceInstance,
        hostGeneration: scope.hostGeneration,
        connectionGeneration: scope.connectionGeneration,
        joined: true,
        scope,
        source: "native_scene3_relation",
        requesterUserId: body.requesterUserId,
      };
    });
  }

  async function groupCurrent(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "scope"])) throw codedError("invalid_group_current_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    const scope = validateGroupScope(body.scope, { requireServiceInstance: false });
    requireLease(body.clientInstance);
    const snapshot = integrationSnapshot();
    let liveScope = null;
    if (typeof bridge.getCurrentGroupScope === "function") {
      try { liveScope = bridge.getCurrentGroupScope(); }
      catch { throw codedError("bridge_status_unavailable"); }
    }
    const active = Boolean(groupLeaseBinding && groupLeaseBinding.clientInstance === body.clientInstance &&
      sameGroupScope(scope, groupLeaseBinding.scope) && sameGroupScope(liveScope, groupLeaseBinding.scope) &&
      scope.hostGeneration === snapshot.hostGeneration && scope.connectionGeneration === snapshot.connectionGeneration);
    return {
      version: 1,
      accountId,
      serviceInstance,
      hostGeneration: snapshot.hostGeneration,
      connectionGeneration: snapshot.connectionGeneration,
      active,
      scope: active ? validateGroupScope(liveScope) : null,
      requesterUserId: active ? groupLeaseBinding.requesterUserId : null,
    };
  }

  async function groupQuit(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "scope"])) throw codedError("invalid_group_quit_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    const scope = validateGroupScope(body.scope, { requireServiceInstance: false });
    return enqueueCommand(body.clientInstance, async () => {
      const leasedOwner = requireLease(body.clientInstance);
      checkAccount(body.accountId);
      const snapshot = integrationSnapshot();
      if (!groupLeaseBinding || groupLeaseBinding.clientInstance !== body.clientInstance ||
          !sameGroupScope(scope, groupLeaseBinding.scope) ||
          scope.hostGeneration !== snapshot.hostGeneration || scope.connectionGeneration !== snapshot.connectionGeneration) {
        throw codedError("group_scope_stale");
      }
      // Bridge.quitGroup checks the driver's live scope directly. The public
      // current-scope projection may be suspended after a remote terminal
      // callback, but the leased original scope can still request a scoped
      // reset while the driver retains the occupied slot.
      if (typeof bridge.quitGroup !== "function") throw codedError("unsupported_unverified_group_join");
      let result;
      try { result = await bridge.quitGroup(scope); }
      catch (error) { throw codedError(safeCode(error)); }
      if (!result || typeof result !== "object" || result.ok !== true || !sameGroupScope(result.scope, scope)) {
        throw codedError("group_quit_unconfirmed");
      }
      // The scoped post receipt may precede the SDK ACK and the driver's
      // independent membership query. Give that existing proof a bounded
      // settling window so the adapter does not immediately quarantine an
      // already-releasing call. Timeout preserves the original receipt;
      // neither the receipt nor this wait authorizes releasing occupancy.
      for (let attempt = 0; attempt <= 50; attempt += 1) {
        if (stopped) throw codedError("service_stopped");
        if (requireLease(body.clientInstance, { refresh: false }) !== leasedOwner) {
          throw codedError("adapter_lease_required");
        }
        const current = integrationSnapshot();
        if (current.hostGeneration !== snapshot.hostGeneration ||
            current.connectionGeneration !== snapshot.connectionGeneration ||
            current.status.providerInvalidated === true || current.status.hostInvalidated === true ||
            current.status.controllerInvalidated === true || current.status.stopping === true ||
            current.status.closing === true || current.status.stopped === true) {
          throw codedError("group_scope_stale");
        }
        if (!callSlotOccupied(current) || attempt === 50) break;
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      return { version: 1, accountId, serviceInstance, operation: "group_quit", result };
    });
  }

  async function groupDiagnostics(body) {
    validateGroupStatus(body);
    const lease = requireLease(body.clientInstance, { refresh: false });
    const snapshot = integrationSnapshot();
    // Reading cached observations must work even when an admission query is
    // quarantined. It never invokes a native query or supplies admission proof.
    const diagnostic = typeof bridge.getGroupDiagnostics === "function"
      ? bridge.getGroupDiagnostics(body.groupId) : null;
    const current = integrationSnapshot();
    if (current.hostGeneration !== snapshot.hostGeneration ||
        current.connectionGeneration !== snapshot.connectionGeneration ||
        current.status.stopping === true || current.status.closing === true || current.status.stopped === true ||
        current.status.providerInvalidated === true || current.status.hostInvalidated === true ||
        current.status.controllerInvalidated === true ||
        requireLease(body.clientInstance, { refresh: false }) !== lease) {
      throw codedError("bridge_status_unavailable");
    }
    return { version: 1, accountId, serviceInstance, hostGeneration: current.hostGeneration,
      connectionGeneration: current.connectionGeneration, groupId: body.groupId,
      occupied: callSlotOccupied(current), nativeJoinVerified: false, room: null,
      diagnosticOnly: true, diagnostic };
  }

  async function command(body) {
    if (!ownKeysExactly(body, ["accountId", "clientInstance", "operation", "scope"])) throw codedError("invalid_command_shape");
    checkAccount(body.accountId);
    if (!validClientInstance(body.clientInstance)) throw codedError("invalid_client_instance");
    if (!new Set(["accept", "hangup", "reject"]).has(body.operation)) throw codedError("invalid_operation");
    const scope = validateScope(body.scope);
      const lease = requireLease(body.clientInstance);

    return enqueueCommand(body.clientInstance, async () => {
      requireLease(body.clientInstance);
      checkAccount(body.accountId);
      let snapshot = integrationSnapshot();
      ensureScopeCurrent(scope, snapshot);
      const initialPrivateScopeProof = body.operation === "accept" ? snapshot.call?.privateScopeProof ?? null : null;
      if (body.operation === "accept") {
        const call = snapshot.call;
        if (!call.inviteCallbackReady || !call.peerUid || !VERIFIED_INVITE_CORRELATIONS.has(call.inviteCorrelation)) {
          throw codedError("invite_callback_not_ready");
        }
        if (call.uncertain || call.phase !== "invite_ready") throw codedError("call_not_accepting");
        if (call.acceptState !== "not_requested") throw codedError("accept_already_requested");
      }
      const assertCurrent = () => {
        if (stopped) throw codedError("service_stopped");
        checkAccount(body.accountId);
        if (requireLease(body.clientInstance, { refresh: false }) !== lease) throw codedError("adapter_lease_required");
        snapshot = integrationSnapshot();
        ensureScopeCurrent(scope, snapshot);
        if (initialPrivateScopeProof &&
            !samePrivateScopeProof(initialPrivateScopeProof, snapshot.call?.privateScopeProof)) {
          throw codedError("private_scope_unverified");
        }
        return true;
      };
      let rawResult;
      try {
        rawResult = body.operation === "accept"
          ? await bridge.accept(scope.callId, { assertCurrent })
          : await bridge[body.operation](scope.callId);
      }
      catch (error) { throw codedError(safeCode(error)); }
      const result = rawResult && typeof rawResult === "object" ? {
        ...(rawResult.posted === true ? { posted: true } : {}),
        ...(Number.isSafeInteger(rawResult.id) && rawResult.id > 0 ? { id: rawResult.id } : {}),
        ...(rawResult.callId === scope.callId ? { callId: rawResult.callId } : {}),
        ...(rawResult.callGeneration === scope.callGeneration ? { callGeneration: rawResult.callGeneration } : {}),
      } : {};
      if (body.operation === "accept" && result.posted !== true) throw codedError("accept_post_not_confirmed");
      return { version: 1, accountId, serviceInstance, operation: body.operation, scope, result };
    });
  }

  async function officialAVDiagnostics(body, operation) {
    if (!ownKeysExactly(body, ["accountId"])) throw codedError("invalid_official_av_diagnostics_shape");
    checkAccount(body.accountId);
    if (stopped) throw codedError("service_stopped");
    if (!officialAVObserver) throw codedError("official_av_observer_unavailable");
    if (officialAVBusy) throw codedError("official_av_observer_busy");
    if (operation === "start" && officialAVRequested) throw codedError("official_av_observer_already_started");
    if (operation !== "start" && !officialAVRequested) throw codedError("official_av_observer_not_started");
    const before = integrationSnapshot();
    if (!before.hostGeneration || !before.connectionGeneration) throw codedError("bridge_status_unavailable");
    officialAVBusy = true;
    if (operation === "start") officialAVRequested = true;
    officialAVPending = (async () => {
      try {
        const report = await officialAVObserver[operation]();
        if (stopped) throw codedError("service_stopped");
        const after = integrationSnapshot();
        if (after.hostGeneration !== before.hostGeneration || after.connectionGeneration !== before.connectionGeneration) {
          throw codedError("bridge_status_unavailable");
        }
        if (!isObserverReport(report)) throw codedError("official_av_observer_invalid_result");
        return { version: 1, diagnosticOnly: true, hostGeneration: before.hostGeneration,
          connectionGeneration: before.connectionGeneration, report };
      } catch (error) {
        // An uncertain/late installation must not leave a renderer wrapper alive.
        try { await officialAVObserver.stop(); } catch { /* Agent also has its finite lease and disconnect cleanup. */ }
        throw codedError(safeCode(error, "official_av_observer_operation_failed"));
      }
    })();
    try { return await officialAVPending; }
    finally { officialAVBusy = false; officialAVPending = null; }
  }

  async function handleRequest(request, response) {
    const authorization = request.headers.authorization;
    const received = typeof authorization === "string" && authorization.startsWith("Bearer ")
      ? authorization.slice(7)
      : "";
    if (!equalToken(token, received)) {
      sendJson(response, 401, { ok: false, code: "unauthorized" });
      request.resume();
      return;
    }
    if (stopped) {
      sendJson(response, 503, { ok: false, code: "service_stopped" });
      request.resume();
      return;
    }
    if (activeRequests >= MAX_ACTIVE_REQUESTS) {
      sendJson(response, 429, { ok: false, code: "service_busy" });
      request.resume();
      return;
    }
    activeRequests += 1;
    try {
      let url;
      try { url = new URL(request.url || "/", `http://${LOOPBACK}:${actualPort || 80}`); }
      catch { sendJson(response, 400, { ok: false, code: "invalid_path" }); return; }
      if (url.search) { sendJson(response, 400, { ok: false, code: "invalid_path" }); return; }
      if (request.method !== "POST" || ![
        "/v1/adapter/poll", "/v1/adapter/release", "/v1/call/command", "/v1/call/occupancy", "/v1/call/diagnostics", "/v1/audio/route",
        "/v1/group/status", "/v1/group/join", "/v1/group/current", "/v1/group/quit", "/v1/group/diagnostics",
        "/v1/diagnostics/official-av/start", "/v1/diagnostics/official-av/drain", "/v1/diagnostics/official-av/stop",
        "/v1/kernel/diagnostics",
      ].includes(url.pathname)) {
        sendJson(response, 404, { ok: false, code: "not_found" });
        request.resume();
        return;
      }
      const contentType = request.headers["content-type"];
      if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
        sendJson(response, 400, { ok: false, code: "content_type_required" });
        request.resume();
        return;
      }
      const body = await readJsonBody(request);
      let result;
      if (url.pathname === "/v1/adapter/poll") result = await poll(body);
      else if (url.pathname === "/v1/adapter/release") result = await release(body);
      else if (url.pathname === "/v1/call/command") result = await command(body);
      else if (url.pathname === "/v1/call/occupancy") result = await occupancy(body);
      else if (url.pathname === "/v1/call/diagnostics") result = privateDiagnostics(body);
      else if (url.pathname === "/v1/kernel/diagnostics") result = await kernelDiagnostics(body);
      else if (url.pathname.startsWith("/v1/diagnostics/official-av/")) result = await officialAVDiagnostics(body, url.pathname.split("/").at(-1));
      else if (url.pathname === "/v1/audio/route") result = await audioRoute(body);
      else if (url.pathname === "/v1/group/status") result = await groupStatus(body);
      else if (url.pathname === "/v1/group/diagnostics") result = await groupDiagnostics(body);
      else if (url.pathname === "/v1/group/current") result = await groupCurrent(body);
      else if (url.pathname === "/v1/group/quit") result = await groupQuit(body);
      else result = await groupJoin(body);
      sendJson(response, 200, result);
    } catch (error) {
      const code = safeCode(error);
      sendJson(response, statusCodeFor(code), { ok: false, code });
    } finally {
      activeRequests -= 1;
    }
  }

  function serviceStatus() {
    expireLease();
    return {
      serviceInstance,
      accountId,
      started,
      stopped,
      listenHost: LOOPBACK,
      port: actualPort,
      cursor: sequence,
      journalEvents: eventJournal.length,
      leaseActive: Boolean(currentLease),
      activeRequests,
      queuedCommands: commandPending,
    };
  }

  async function start() {
    if (stopped) throw codedError("service_stopped");
    if (started) return serviceStatus();
    if (startPromise) return startPromise;
    startPromise = (async () => {
      unsubscribeStatus = bridge.subscribeStatus((event) => {
        try {
          const transition = projectGroupTransition(event, Date.now());
          if (transition) {
            groupTransitions.push(transition);
            if (groupTransitions.length > 32) groupTransitions.shift();
          }
        } catch { /* Diagnostics cannot change call ownership or event delivery. */ }
        appendEvent(event?.kind);
      });
      unsubscribeControlEvents = bridge.subscribeControlEvents(appendBusyInvite);
      appendEvent("snapshot");
      server = http.createServer((request, response) => { void handleRequest(request, response); });
      server.requestTimeout = READ_TIMEOUT_MS + 1000;
      server.headersTimeout = READ_TIMEOUT_MS + 2000;
      server.maxHeadersCount = 32;
      await new Promise((resolve, reject) => {
        const onError = (error) => {
          server?.removeListener("listening", onListening);
          reject(codedError(error?.code === "EADDRINUSE" ? "control_port_in_use" : "control_listen_failed"));
        };
        const onListening = () => {
          server?.removeListener("error", onError);
          const address = server.address();
          if (!address || typeof address === "string" || address.address !== LOOPBACK) {
            reject(codedError("control_loopback_bind_failed"));
            return;
          }
          actualPort = address.port;
          resolve();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(port, LOOPBACK);
      });
      started = true;
      return serviceStatus();
    })();
    try { return await startPromise; }
    catch (error) {
      unsubscribeStatus?.();
      unsubscribeStatus = null;
      unsubscribeControlEvents?.();
      unsubscribeControlEvents = null;
      if (server?.listening) await new Promise((resolve) => server.close(() => resolve()));
      server = null;
      throw codedError(safeCode(error, "control_listen_failed"));
    }
  }

  async function stop() {
    if (stopPromise) return stopPromise;
    stopped = true;
    currentLease = null;
    unsubscribeStatus?.();
    unsubscribeStatus = null;
    unsubscribeControlEvents?.();
    unsubscribeControlEvents = null;
    stopPromise = (async () => {
      if (adapterPausePromise) await adapterPausePromise.catch(() => {});
      if (officialAVPending) await officialAVPending.catch(() => {});
      if (officialAVRequested) {
        try { await officialAVObserver.stop(); } catch { /* Broker teardown also removes finite Agent observers. */ }
      }
      for (const observation of [...activeAudioObservations]) {
        try { observation.controller.abort("service_stopped"); } catch { /* route cleanup is best effort */ }
      }
      if (typeof audioRouteObserver?.stop === "function") {
        try { await audioRouteObserver.stop(); } catch { /* preserve local service cleanup */ }
      }
      const active = [...activeAudioObservations].map((observation) => observation.done);
      if (active.length) {
        await Promise.race([
          Promise.allSettled(active),
          new Promise((resolve) => {
            const timer = setTimeout(resolve, AUDIO_ROUTE_STOP_DRAIN_TIMEOUT_MS);
            timer.unref?.();
          }),
        ]);
      }
      if (commandDrainPromise) await commandDrainPromise;
      if (startPromise) await startPromise.catch(() => {});
      if (server?.listening) await new Promise((resolve) => server.close(() => resolve()));
      started = false;
      return serviceStatus();
    })();
    return stopPromise;
  }

  // Private controller stdin only; this is deliberately not an HTTP route.
  async function setAdapterAvailable(value) {
    if (typeof value !== "boolean") throw codedError("invalid_adapter_availability");
    if (stopped) throw codedError("service_stopped");
    if (!value && adapterAvailable) {
      adapterAvailable = false;
      currentLease = null;
      for (const observation of [...activeAudioObservations]) {
        try { observation.controller.abort("adapter_unavailable"); } catch { /* keep admission closed */ }
      }
      adapterPausePromise = (async () => {
        // Let already posted native work settle before ending its exact scope.
        // Queued work fails requireLease; no new native work can enter here.
        if (commandDrainPromise) await commandDrainPromise;
        const observations = [...activeAudioObservations].map((observation) => observation.done);
        if (observations.length) await Promise.race([
          Promise.allSettled(observations),
          new Promise((resolve) => setTimeout(resolve, AUDIO_ROUTE_STOP_DRAIN_TIMEOUT_MS)),
        ]);
        const snapshot = integrationSnapshot();
        const scope = bridge.getCurrentGroupScope?.() || groupLeaseBinding?.scope;
        if (scope && typeof bridge.quitGroup === "function") {
          try { await bridge.quitGroup(validateGroupScope(scope)); }
          catch { /* Occupancy remains until the existing native absence proof. */ }
        }
        if (snapshot.call?.phase === "connected") {
          try { await bridge.hangup(snapshot.call.callId); } catch { /* Never fake terminal proof. */ }
        } else if (snapshot.call?.phase === "invite_ready") {
          try { await bridge.reject(snapshot.call.callId); } catch { /* Invite timeout remains authoritative. */ }
        }
      })();
    }
    if (adapterPausePromise) await adapterPausePromise;
    if (stopped) throw codedError("service_stopped");
    if (value) adapterAvailable = true;
    return { adapterAvailable };
  }

  return Object.freeze({ start, stop, status: serviceStatus, setAdapterAvailable });
}

module.exports = { createControlService, MAX_BODY_BYTES };
