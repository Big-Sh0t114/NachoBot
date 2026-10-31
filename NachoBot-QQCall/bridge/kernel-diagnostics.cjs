"use strict";

// Project only fixed diagnostic fields. Never return callback payloads or IDs.
const METHODS = Object.freeze([
  "onS2CActionToAVSDK", "onActionToAVSDK", "onAVSDKData", "onAVSdkCrash",
  "onReceiveInvite", "onInviteActionToAVSDK", "onGroupVideoActionToAVSDK",
  "onGroupVideoServerPushToAVSDK", "onGroupAudioMemNumChange",
  "onGroupVideoMemNumPushInfo", "onGroupVideoInviteMemberUpdate",
]);
const TYPES = new Set(["undefined", "null", "number", "boolean", "string", "object", "array", "binary", "bigint", "function", "other"]);
function own(value, key) {
  try { return Object.getOwnPropertyDescriptor(value, key)?.value; }
  catch { return undefined; }
}
function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : null; }
function flag(value) { return typeof value === "boolean" ? value : null; }

function projectKernelDiagnostics(reply, connectionGeneration) {
  const agent = own(reply, "agent");
  const bootstrap = own(reply, "bootstrap");
  if (own(reply, "ok") !== true || own(agent, "connectionGeneration") !== connectionGeneration ||
      own(bootstrap, "connectionGeneration") !== connectionGeneration || own(agent, "listenerRegistered") !== true) return null;
  const diagnostics = own(agent, "callbackDiagnostics");
  const known = own(diagnostics, "knownCallbacks");
  const callbacks = {};
  for (const method of METHODS) {
    const item = own(known, method);
    const args = own(item, "lastArguments");
    const argumentTypes = [];
    if (Array.isArray(args)) {
      for (let i = 0; i < Math.min(args.length, 8); i += 1) {
        const type = own(own(args, String(i)), "type");
        argumentTypes.push(TYPES.has(type) ? type : "other");
      }
    }
    callbacks[method] = {
      count: count(own(item, "count")), mappedCount: count(own(item, "mappedCount")),
      unmappedCount: count(own(item, "unmappedCount")), lastActionMapped: flag(own(item, "lastActionMapped")),
      lastArgumentCount: count(own(item, "lastArgumentCount")), argumentTypes,
    };
  }
  return { unknownCallbackCount: count(own(diagnostics, "unknownCallbackCount")), callbacks };
}

const GROUP_STATES = new Set(["joined", "idle", "ending", "ending_remote", "quarantined"]);
const GROUP_CODES = new Set([
  "entry_confirmed", "quit_refused", "quit_posting", "quit_posted_waiting_terminal",
  "terminal_observed_waiting_proof", "remote_terminal_waiting_proof", "remote_terminal_observed",
  "kernel_room_end_waiting_proof", "membership_confirmation_pending", "membership_absence_confirmed",
  "membership_confirmation_timeout", "membership_confirmation_stale", "host_stopped", "stale_context",
  "clock_unavailable", "quit_timeout", "start_timeout", "safe_quit_posted", "safe_quit_outcome_unknown",
]);

function projectGroupTransition(event, observedAtMs) {
  const status = own(event, "status");
  const group = own(status, "groupCall");
  const state = own(group, "state");
  if (!GROUP_STATES.has(state) || own(event, "kind") !== `group_${state}` ||
      !Number.isSafeInteger(observedAtMs) || observedAtMs < 0) return null;
  const result = { observedAtMs, state, code: GROUP_CODES.has(own(group, "code")) ? own(group, "code") : "other" };
  for (const key of ["hostGeneration", "connectionGeneration", "roomGeneration", "terminalSequence"]) {
    const value = own(group, key);
    if (!Number.isSafeInteger(value) || value < 0) return null;
    result[key] = value;
  }
  if (result.hostGeneration !== own(status, "hostGeneration") ||
      result.connectionGeneration !== own(status, "connectionGeneration")) return null;
  return result;
}

module.exports = { projectKernelDiagnostics, projectGroupTransition };
