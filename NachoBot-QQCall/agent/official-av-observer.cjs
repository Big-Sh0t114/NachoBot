"use strict";

const { randomBytes } = require("node:crypto");
const { exactRendererRoute } = require("./official-window-probe.cjs");
const { createNativeCallObserver } = require("./native-call-observer.cjs");

const SCHEMA = "qqcall.official.av.observer.v1";
const PROOF_LEVEL = "diagnostic_only";
const MAX_WINDOWS = 16;
const MAX_EVENTS = 256;
const MAX_EVENT_BYTES = 96 * 1024;
const MAX_REPORT_BYTES = 128 * 1024;
const MAX_DOM_WAIT_MS = 3000;
const MAX_LEASE_MS = 180000;
const ROUTE_DISCOVERY_INTERVAL_MS = 250;
const MAX_RAW_DATA_BYTES = 8192;
const OUTGOING_COMMANDS = new Set([1, 4, 5, 7, 8, 9, 10, 11, 13, 14, 57]);
const INCOMING_COMMANDS = new Set([9, 20003, 20004, 20005, 20006, 20010, 20011, 20017, 20001, 120003]);
const INSTALL_FAILURE_STATUSES = new Set([
  "install_embed_method_failed", "install_embed_listener_failed", "install_pagehide_listener_failed",
  "install_state_publish_failed", "install_lease_timer_failed",
]);
const NATIVE_CONDITIONAL_STATUSES = new Set([
  "not_started", "breakpoint_set", "debugger_unavailable", "debugger_already_attached",
  "attach_failed", "attach_protocol_unsupported", "attach_no_target", "attach_target_retained",
  "attach_not_connected", "attach_state_unknown", "debugger_enable_failed", "pause_policy_failed", "skip_all_pauses_unsupported",
  "target_unavailable", "breakpoint_rejected", "timed_out", "unexpected_pause", "debugger_detached",
  "cleanup_incomplete",
]);
const ARMED_WINDOW_STATUSES = new Set(["armed", "armed_incoming_only", "armed_native_conditional"]);
const WINDOW_STATUSES = new Set([
  "awaiting_dom", "not_av", ...ARMED_WINDOW_STATUSES, "ambiguous_embed", "unavailable",
  "timed_out", "stale_context", "destroyed", "cleanup_incomplete", ...INSTALL_FAILURE_STATUSES,
]);
const REPORT_CODES = new Set([
  "ok", "process_not_browser", "electron_not_present", "electron_not_builtin",
  "electron_unavailable", "browser_window_unavailable", "window_enumeration_failed",
  "window_list_invalid", "observer_already_started", "observer_not_started",
]);
const REPORT_KEYS = [
  "schema", "proofLevel", "status", "code", "coverageGap", "comparisonScope",
  "outboundAttemptsOnly", "windowsTruncated", "windows", "events", "droppedEvents", "encodedEventBytes",
  "incomplete", "queueLossPossible", "cleanupComplete",
];
const WINDOW_KEYS = [
  "ordinal", "documentEpoch", "status", "embedCount", "postMessageAvailable", "cleanupComplete",
  "nativeConditionalStatus", "nativeCallAttempts", "nativeCallAttemptsSaturated",
];
const EVENT_KEYS = [
  "eventIndex", "direction", "command", "requestId", "windowOrdinal", "documentEpoch",
  "parameterKind", "parameterLength", "parameterTruncated", "parameterItems", "command4",
];
const EVENT_KEYS_WITH_ENTER_ROOM = [...EVENT_KEYS, "enterRoom"];
const COMMAND4_KEYS = [
  "available", "sceneId", "inviteCount", "subBusinessType", "inviteOriginal", "isCreator",
  "relationId", "roomId", "selfUid", "inviteUidRefs", "inviteUidCount", "inviteUidTruncated",
];
const ENTER_ROOM_KEYS = ["ret", "scene", "relationId", "peerUid", "isCreator", "sdkProvider"];
const ENTER_ROOM_ID_KEYS = ["ref", "length"];
const LOGIN_STARTUP_KEYS = [
  "tinyIdKind", "tinyIdEqualsSelfUin", "tinyIdEqualsSelfUid",
  "dbPathKind", "dbPathLength", "dbPathIsAbsolute", "dbPathEndsSelfUin",
  "machineIdKind", "machineIdLength",
];
const SDK_INFO_STARTUP_KEYS = ["kind", "length", "exactKnownVersionMatches1_0_1168"];
const STARTUP_VALUE_KINDS = new Set(["undefined", "null", "number", "boolean", "string", "array", "object", "other"]);
const TINY_ID_KINDS = new Set(["undefined", "null", "number_zero", "number_other", "empty_string", "zero_string", "nonempty_string", "other"]);

function exactObject(value, keys) {
  try {
    return Boolean(value && typeof value === "object" && !Array.isArray(value) &&
      Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length &&
      keys.every((key) => Object.hasOwn(value, key)));
  } catch { return false; }
}

function exactOwnDataObject(value, keys) {
  try {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) return false;
    const ownKeys = Reflect.ownKeys(value);
    if (ownKeys.length !== keys.length || ownKeys.some((key) => typeof key !== "string" || !keys.includes(key))) return false;
    return keys.every((key) => {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      return descriptor && Object.hasOwn(descriptor, "value");
    });
  } catch { return false; }
}

function isDescriptor(value) {
  try {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) return false;
    const typeDescriptor = Object.getOwnPropertyDescriptor(value, "type");
    if (!typeDescriptor || !Object.hasOwn(typeDescriptor, "value")) return false;
    const type = typeDescriptor.value;
    if (!["string", "number", "boolean", "null", "undefined", "object", "array", "other", "absent", "unknown"].includes(type)) return false;
    if (type === "string") {
      const keys = Object.hasOwn(value, "truncated") ? ["type", "length", "ref", "truncated"] : ["type", "length", "ref"];
      return exactOwnDataObject(value, keys) && Number.isInteger(value.length) && value.length >= 0 && value.length <= 8192 &&
      (value.ref === null || (typeof value.ref === "string" && /^v[1-9][0-9]{0,2}$/.test(value.ref))) &&
      (!Object.hasOwn(value, "truncated") || value.truncated === true);
    }
    if (type === "array") return exactOwnDataObject(value, ["type", "length", "truncated"]) && Number.isInteger(value.length) && value.length >= 0 && value.length <= 16 &&
      typeof value.truncated === "boolean";
    if (type === "boolean") return exactOwnDataObject(value, ["type", "value"]) && typeof value.value === "boolean";
    return exactOwnDataObject(value, ["type"]);
  } catch { return false; }
}

function canonicalDescriptor(value) {
  if (value.type === "string") {
    const result = { type: "string", length: value.length, ref: value.ref };
    if (value.truncated === true) result.truncated = true;
    return result;
  }
  if (value.type === "array") return { type: "array", length: value.length, truncated: value.truncated };
  if (value.type === "boolean") return { type: "boolean", value: value.value };
  return { type: value.type };
}

function canonicalEvent(value) {
  if (!isObserverEvent(value)) return null;
  let enterRoom = null;
  let loginStartup;
  let sdkInfoStartup;
  let privateReject;
  let rejectAck;
  try {
    const enterRoomDescriptor = Object.getOwnPropertyDescriptor(value, "enterRoom");
    if (enterRoomDescriptor && Object.hasOwn(enterRoomDescriptor, "value")) enterRoom = canonicalEnterRoom(enterRoomDescriptor.value);
    const loginStartupDescriptor = Object.getOwnPropertyDescriptor(value, "loginStartup");
    if (loginStartupDescriptor && Object.hasOwn(loginStartupDescriptor, "value")) {
      loginStartup = canonicalLoginStartup(loginStartupDescriptor.value);
    }
    const sdkInfoStartupDescriptor = Object.getOwnPropertyDescriptor(value, "sdkInfoStartup");
    if (sdkInfoStartupDescriptor && Object.hasOwn(sdkInfoStartupDescriptor, "value")) {
      sdkInfoStartup = canonicalSDKInfoStartup(sdkInfoStartupDescriptor.value);
    }
    const rejectDescriptor = Object.getOwnPropertyDescriptor(value, "privateReject");
    if (rejectDescriptor && Object.hasOwn(rejectDescriptor, "value")) {
      privateReject = { sceneId: rejectDescriptor.value.sceneId, rejectReason: rejectDescriptor.value.rejectReason };
    }
    const rejectAckDescriptor = Object.getOwnPropertyDescriptor(value, "rejectAck");
    if (rejectAckDescriptor && Object.hasOwn(rejectAckDescriptor, "value")) {
      rejectAck = { returnCode: rejectAckDescriptor.value.returnCode };
    }
  } catch { /* malformed renderer projections are omitted without affecting other fields */ }
  const result = {
    eventIndex: value.eventIndex,
    direction: value.direction,
    command: value.command,
    requestId: { present: value.requestId.present, type: value.requestId.type, length: value.requestId.length, ref: value.requestId.ref },
    windowOrdinal: value.windowOrdinal,
    documentEpoch: value.documentEpoch,
    parameterKind: value.parameterKind,
    parameterLength: value.parameterLength,
    parameterTruncated: value.parameterTruncated,
    parameterItems: value.parameterItems.map(canonicalDescriptor),
    command4: {
      available: value.command4.available,
      sceneId: value.command4.sceneId,
      inviteCount: value.command4.inviteCount,
      subBusinessType: value.command4.subBusinessType,
      inviteOriginal: value.command4.inviteOriginal,
      isCreator: value.command4.isCreator,
      relationId: value.command4.relationId,
      roomId: value.command4.roomId,
      selfUid: value.command4.selfUid,
      inviteUidRefs: value.command4.inviteUidRefs.slice(),
      inviteUidCount: value.command4.inviteUidCount,
      inviteUidTruncated: value.command4.inviteUidTruncated,
    },
    enterRoom,
  };
  if (loginStartup !== undefined) result.loginStartup = loginStartup;
  if (sdkInfoStartup !== undefined) result.sdkInfoStartup = sdkInfoStartup;
  if (privateReject !== undefined) result.privateReject = privateReject;
  if (rejectAck !== undefined) result.rejectAck = rejectAck;
  return result;
}

function isLoginStartupProjection(value) {
  try {
    return exactOwnDataObject(value, LOGIN_STARTUP_KEYS) &&
      TINY_ID_KINDS.has(value.tinyIdKind) && STARTUP_VALUE_KINDS.has(value.dbPathKind) && STARTUP_VALUE_KINDS.has(value.machineIdKind) &&
      typeof value.tinyIdEqualsSelfUin === "boolean" && typeof value.tinyIdEqualsSelfUid === "boolean" &&
      (value.dbPathLength === null || (Number.isInteger(value.dbPathLength) && value.dbPathLength >= 0 && value.dbPathLength <= MAX_RAW_DATA_BYTES)) &&
      typeof value.dbPathIsAbsolute === "boolean" && typeof value.dbPathEndsSelfUin === "boolean" &&
      (value.machineIdLength === null || (Number.isInteger(value.machineIdLength) && value.machineIdLength >= 0 && value.machineIdLength <= MAX_RAW_DATA_BYTES)) &&
      (value.dbPathKind === "string" ? value.dbPathLength !== null : value.dbPathLength === null) &&
      (value.machineIdKind === "string" ? value.machineIdLength !== null : value.machineIdLength === null) &&
      (value.tinyIdKind === "empty_string" || value.tinyIdKind === "zero_string" || value.tinyIdKind === "nonempty_string" ||
        (!value.tinyIdEqualsSelfUin && !value.tinyIdEqualsSelfUid)) &&
      (value.dbPathKind === "string" || (!value.dbPathIsAbsolute && !value.dbPathEndsSelfUin));
  } catch { return false; }
}

function canonicalLoginStartup(value) {
  const result = {};
  for (const key of LOGIN_STARTUP_KEYS) result[key] = Object.getOwnPropertyDescriptor(value, key).value;
  return result;
}

function isSDKInfoStartupProjection(value) {
  try {
    return exactOwnDataObject(value, SDK_INFO_STARTUP_KEYS) && value.kind === "string" &&
      Number.isInteger(value.length) && value.length >= 0 && value.length <= MAX_RAW_DATA_BYTES &&
      typeof value.exactKnownVersionMatches1_0_1168 === "boolean" &&
      (!value.exactKnownVersionMatches1_0_1168 || value.length === "1.0.1168".length);
  } catch { return false; }
}

function canonicalSDKInfoStartup(value) {
  return {
    kind: Object.getOwnPropertyDescriptor(value, "kind").value,
    length: Object.getOwnPropertyDescriptor(value, "length").value,
    exactKnownVersionMatches1_0_1168: Object.getOwnPropertyDescriptor(value, "exactKnownVersionMatches1_0_1168").value,
  };
}

function isEnterRoomIdentity(value) {
  try {
    if (!exactOwnDataObject(value, ENTER_ROOM_ID_KEYS)) return false;
    const ref = Object.getOwnPropertyDescriptor(value, "ref").value;
    const length = Object.getOwnPropertyDescriptor(value, "length").value;
    return Number.isInteger(length) && length >= 0 && length <= 128 &&
      (length === 0 ? ref === null : typeof ref === "string" && /^v[1-9][0-9]{0,2}$/.test(ref));
  } catch { return false; }
}

function isEnterRoomProjection(value) {
  if (value === null) return true;
  try {
    if (!exactOwnDataObject(value, ENTER_ROOM_KEYS)) return false;
    const ret = Object.getOwnPropertyDescriptor(value, "ret").value;
    const scene = Object.getOwnPropertyDescriptor(value, "scene").value;
    const relationId = Object.getOwnPropertyDescriptor(value, "relationId").value;
    const peerUid = Object.getOwnPropertyDescriptor(value, "peerUid").value;
    const isCreator = Object.getOwnPropertyDescriptor(value, "isCreator").value;
    const sdkProvider = Object.getOwnPropertyDescriptor(value, "sdkProvider").value;
    return Number.isInteger(ret) && ret >= -2147483648 && ret <= 2147483647 &&
      Number.isSafeInteger(scene) && scene >= 0 && scene <= 1000000 &&
      isEnterRoomIdentity(relationId) && isEnterRoomIdentity(peerUid) &&
      (typeof isCreator === "boolean" || isCreator === 0 || isCreator === 1) &&
      Number.isSafeInteger(sdkProvider);
  } catch { return false; }
}

function canonicalEnterRoom(value) {
  if (value === null) return null;
  try {
    const own = (object, key) => Object.getOwnPropertyDescriptor(object, key).value;
    const relationId = own(value, "relationId");
    const peerUid = own(value, "peerUid");
    return {
      ret: own(value, "ret"),
      scene: own(value, "scene"),
      relationId: { ref: own(relationId, "ref"), length: own(relationId, "length") },
      peerUid: { ref: own(peerUid, "ref"), length: own(peerUid, "length") },
      isCreator: own(value, "isCreator"),
      sdkProvider: own(value, "sdkProvider"),
    };
  } catch { return null; }
}

function isCommand4(value) {
  if (!exactOwnDataObject(value, COMMAND4_KEYS) || typeof value.available !== "boolean" ||
      !["sceneId", "inviteCount", "subBusinessType", "inviteOriginal"].every((key) =>
        value[key] === null || (Number.isSafeInteger(value[key]) && value[key] >= 0 && value[key] <= 1000000)) ||
      !(value.isCreator === null || typeof value.isCreator === "boolean") ||
      !["relationId", "roomId", "selfUid"].every((key) => value[key] === null || /^v[1-9][0-9]{0,2}$/.test(value[key])) ||
      !Array.isArray(value.inviteUidRefs) || value.inviteUidRefs.length > 16 ||
      !value.inviteUidRefs.every((entry) => typeof entry === "string" && /^v[1-9][0-9]{0,2}$/.test(entry)) ||
      !Number.isInteger(value.inviteUidCount) || value.inviteUidCount < 0 || value.inviteUidCount > 1000000 ||
      typeof value.inviteUidTruncated !== "boolean") return false;
  return true;
}

function isObserverEvent(value) {
  try {
    const enterRoomDescriptor = value && typeof value === "object"
      ? Object.getOwnPropertyDescriptor(value, "enterRoom")
      : null;
    const loginStartupDescriptor = value && typeof value === "object"
      ? Object.getOwnPropertyDescriptor(value, "loginStartup")
      : null;
    const sdkInfoStartupDescriptor = value && typeof value === "object"
      ? Object.getOwnPropertyDescriptor(value, "sdkInfoStartup")
      : null;
    const hasEnterRoom = Boolean(enterRoomDescriptor);
    const hasLoginStartup = Boolean(loginStartupDescriptor);
    const hasSDKInfoStartup = Boolean(sdkInfoStartupDescriptor);
    const privateRejectDescriptor = value && typeof value === "object"
      ? Object.getOwnPropertyDescriptor(value, "privateReject") : null;
    const hasPrivateReject = Boolean(privateRejectDescriptor);
    const rejectAckDescriptor = value && typeof value === "object"
      ? Object.getOwnPropertyDescriptor(value, "rejectAck") : null;
    const hasRejectAck = Boolean(rejectAckDescriptor);
    if (hasLoginStartup && hasSDKInfoStartup) return false;
    let baseKeys = hasEnterRoom ? EVENT_KEYS_WITH_ENTER_ROOM : EVENT_KEYS;
    if (hasPrivateReject) baseKeys = [...baseKeys, "privateReject"];
    if (hasRejectAck) baseKeys = [...baseKeys, "rejectAck"];
    const expectedKeys = hasLoginStartup ? [...baseKeys, "loginStartup"]
      : hasSDKInfoStartup ? [...baseKeys, "sdkInfoStartup"] : baseKeys;
    if (!exactOwnDataObject(value, expectedKeys) ||
        (hasEnterRoom && (!Object.hasOwn(enterRoomDescriptor, "value") || !isEnterRoomProjection(enterRoomDescriptor.value))) ||
        (hasLoginStartup && (!Object.hasOwn(loginStartupDescriptor, "value") || !isLoginStartupProjection(loginStartupDescriptor.value))) ||
        (hasSDKInfoStartup && (!Object.hasOwn(sdkInfoStartupDescriptor, "value") || !isSDKInfoStartupProjection(sdkInfoStartupDescriptor.value))) ||
        !Number.isInteger(value.eventIndex) || value.eventIndex < 1 || value.eventIndex > MAX_EVENTS ||
        !["outgoing", "incoming"].includes(value.direction) || !Number.isInteger(value.command) ||
        !Number.isInteger(value.windowOrdinal) || value.windowOrdinal < 1 || value.windowOrdinal > MAX_WINDOWS ||
        !Number.isInteger(value.documentEpoch) || value.documentEpoch < 1 || value.documentEpoch > 1000000 ||
        !["absent", "array", "object", "scalar", "invalid"].includes(value.parameterKind) ||
        !(value.parameterLength === null || (Number.isInteger(value.parameterLength) && value.parameterLength >= 0 && value.parameterLength <= 1000000)) ||
        typeof value.parameterTruncated !== "boolean" || !Array.isArray(value.parameterItems) || value.parameterItems.length > 16 ||
        !value.parameterItems.every(isDescriptor) || !isCommand4(value.command4)) return false;
    const parameterShapeValid = value.parameterKind === "array"
      ? value.parameterLength !== null && value.parameterItems.length === Math.min(value.parameterLength, 16) &&
        value.parameterTruncated === (value.parameterLength > 16)
      : value.parameterKind === "scalar" || value.parameterKind === "object"
        ? value.parameterLength === null && value.parameterItems.length === 1 && value.parameterTruncated === false
        : value.parameterLength === null && value.parameterItems.length === 0 && value.parameterTruncated === false;
    if (!parameterShapeValid) return false;
    if (hasPrivateReject) {
      if (!Object.hasOwn(privateRejectDescriptor, "value")) return false;
      const rejection = privateRejectDescriptor.value;
      if (!exactOwnDataObject(rejection, ["sceneId", "rejectReason"]) || rejection.sceneId !== 1 ||
          ![1, 2, 3, 100].includes(rejection.rejectReason) || value.direction !== "outgoing" || value.command !== 9 ||
          value.parameterKind !== "array" || value.parameterLength !== 3 || value.parameterTruncated ||
          value.parameterItems[0]?.type !== "number" || value.parameterItems[1]?.type !== "string" ||
          value.parameterItems[2]?.type !== "number") return false;
    }
    if (hasRejectAck) {
      if (!Object.hasOwn(rejectAckDescriptor, "value")) return false;
      const ack = rejectAckDescriptor.value;
      if (!exactOwnDataObject(ack, ["returnCode"]) || !Number.isInteger(ack.returnCode) ||
          ack.returnCode < -2147483648 || ack.returnCode > 2147483647 ||
          value.direction !== "incoming" || value.command !== 9 || value.parameterKind !== "array" ||
          value.parameterLength !== 2 || value.parameterTruncated ||
          value.parameterItems[0]?.type !== "number" || value.parameterItems[1]?.type !== "string") return false;
    }
    if (hasLoginStartup && (value.direction !== "outgoing" || value.command !== 1 || value.parameterKind !== "array" ||
        value.parameterLength !== 5 || value.parameterItems.length !== 5 || value.parameterTruncated ||
        value.parameterItems.some((item) => item.type === "absent" || item.type === "unknown"))) return false;
    if (hasLoginStartup) {
      const [selfUid, selfUin, tinyId, dbPath, machineId] = value.parameterItems;
      const kindMatches = (item, kind) => item.type === kind || (kind === "other" && ["boolean", "array", "object", "other"].includes(item.type));
      const tinyMatches = value.loginStartup.tinyIdKind === "undefined" ? tinyId.type === "undefined"
        : value.loginStartup.tinyIdKind === "null" ? tinyId.type === "null"
          : value.loginStartup.tinyIdKind === "number_zero" || value.loginStartup.tinyIdKind === "number_other" ? tinyId.type === "number"
          : value.loginStartup.tinyIdKind === "empty_string" || value.loginStartup.tinyIdKind === "zero_string" || value.loginStartup.tinyIdKind === "nonempty_string" ? tinyId.type === "string"
            : ["boolean", "array", "object", "other"].includes(tinyId.type);
      const tinyStringShapeMatches = value.loginStartup.tinyIdKind === "empty_string" ? tinyId.length === 0
        : value.loginStartup.tinyIdKind === "zero_string" ? tinyId.length === 1
          : value.loginStartup.tinyIdKind === "nonempty_string" ? tinyId.length > 0 : true;
      if (!tinyMatches || !tinyStringShapeMatches || !kindMatches(dbPath, value.loginStartup.dbPathKind) || !kindMatches(machineId, value.loginStartup.machineIdKind) ||
          (value.loginStartup.dbPathKind === "string" && dbPath.length !== value.loginStartup.dbPathLength) ||
          (value.loginStartup.machineIdKind === "string" && machineId.length !== value.loginStartup.machineIdLength) ||
          (value.loginStartup.tinyIdEqualsSelfUid && (tinyId.type !== "string" || selfUid.type !== "string")) ||
          (value.loginStartup.tinyIdEqualsSelfUin && (tinyId.type !== "string" || selfUin.type !== "string"))) return false;
    }
    if (hasSDKInfoStartup && (value.direction !== "outgoing" || value.command !== 57 || value.parameterKind !== "array" ||
        value.parameterLength !== 1 || value.parameterItems.length !== 1 || value.parameterTruncated ||
        value.parameterItems[0].type !== "string" || value.parameterItems[0].truncated === true ||
        value.parameterItems[0].length !== value.sdkInfoStartup.length)) return false;
    if (hasEnterRoom && enterRoomDescriptor.value !== null &&
        (value.direction !== "incoming" || value.command !== 20004 || value.parameterKind !== "array" ||
         value.parameterLength !== 7 || value.parameterItems.length !== 7 || value.parameterTruncated)) return false;
    const commands = value.direction === "outgoing" ? OUTGOING_COMMANDS : INCOMING_COMMANDS;
    return commands.has(value.command) && isRequestId(value.requestId);
  } catch { return false; }
}

function isRequestId(value) {
  return exactOwnDataObject(value, ["present", "type", "length", "ref"]) && typeof value.present === "boolean" &&
    ["absent", "string", "number", "unsupported"].includes(value.type) &&
    (value.length === null || (Number.isInteger(value.length) && value.length >= 0 && value.length <= 8192)) &&
    (value.ref === null || (typeof value.ref === "string" && /^v[1-9][0-9]{0,2}$/.test(value.ref))) &&
    (value.present === (value.type === "string" || value.type === "number"));
}

function isObserverReport(value) {
  try {
    if (!exactOwnDataObject(value, REPORT_KEYS) || value.schema !== SCHEMA || value.proofLevel !== PROOF_LEVEL ||
        !["active", "stopped", "expired", "rejected"].includes(value.status) || !REPORT_CODES.has(value.code) ||
        typeof value.coverageGap !== "boolean" || value.coverageGap !== true ||
        value.comparisonScope !== "document_epoch_only" || value.outboundAttemptsOnly !== true || typeof value.windowsTruncated !== "boolean" ||
        !Array.isArray(value.windows) || value.windows.length > MAX_WINDOWS || !Array.isArray(value.events) ||
        value.events.length > MAX_EVENTS || !Number.isInteger(value.droppedEvents) || value.droppedEvents < 0 ||
        value.droppedEvents > 1000000 || !Number.isInteger(value.encodedEventBytes) || value.encodedEventBytes < 0 ||
        value.encodedEventBytes > MAX_EVENT_BYTES || typeof value.incomplete !== "boolean" ||
        typeof value.queueLossPossible !== "boolean" || (value.queueLossPossible && !value.incomplete) ||
        !(value.cleanupComplete === null || typeof value.cleanupComplete === "boolean")) return false;
    if ((value.status === "rejected") !== (value.code !== "ok")) return false;
    if (value.windowsTruncated && value.windows.length !== MAX_WINDOWS) return false;
    if (!value.windows.every((window, index) => exactOwnDataObject(window, WINDOW_KEYS) && window.ordinal === index + 1 &&
        Number.isInteger(window.documentEpoch) && window.documentEpoch >= 0 && window.documentEpoch <= 1000000 &&
        WINDOW_STATUSES.has(window.status) &&
        (window.embedCount === null || (Number.isInteger(window.embedCount) && window.embedCount >= 0 && window.embedCount <= 4)) &&
        (window.postMessageAvailable === null || typeof window.postMessageAvailable === "boolean") &&
        (window.cleanupComplete === null || typeof window.cleanupComplete === "boolean") &&
        (window.nativeConditionalStatus === null || NATIVE_CONDITIONAL_STATUSES.has(window.nativeConditionalStatus)) &&
        Number.isInteger(window.nativeCallAttempts) && window.nativeCallAttempts >= 0 && window.nativeCallAttempts <= 1000000 &&
        typeof window.nativeCallAttemptsSaturated === "boolean")) return false;
    if (!value.events.every(isObserverEvent)) return false;
    if (Buffer.byteLength(JSON.stringify(value), "utf8") > MAX_REPORT_BYTES) return false;
    return true;
  } catch { return false; }
}

function isOfficialAVStartReport(value) { return isObserverReport(value) && value.status !== "stopped" && value.status !== "expired"; }

function exactAVRoute(urlValue) {
  if (typeof urlValue !== "string" || urlValue.length > 8192) return false;
  const canonical = exactRendererRoute(urlValue);
  if (!canonical.matches) return false;
  const hash = urlValue.indexOf("#");
  return hash >= 0 && urlValue.slice(hash + 1) === "/av";
}

function canonicalRendererURL(urlValue) {
  return typeof urlValue === "string" && urlValue.length <= 8192 && exactRendererRoute(urlValue).matches;
}

function fixedInstallScript(runId, ordinal, epoch, leaseExpiresAtMs, incomingOnly = false, nativeConditional = false) {
  return `(${rendererInstall.toString()})(${JSON.stringify(runId)},${ordinal},${epoch},${leaseExpiresAtMs},${incomingOnly === true},${nativeConditional === true})`;
}
function fixedDisableCaptureScript(runId, ordinal, epoch) {
  return `(${rendererDisableCapture.toString()})(${JSON.stringify(runId)},${ordinal},${epoch})`;
}
function fixedDrainScript(runId, ordinal, epoch) {
  return `(${rendererDrain.toString()})(${JSON.stringify(runId)},${ordinal},${epoch})`;
}
function fixedCleanupScript(runId, ordinal, epoch, leaseExpiresAtMs, flushPending = false) {
  return `(${rendererCleanup.toString()})(${JSON.stringify(runId)},${ordinal},${epoch},${leaseExpiresAtMs},${flushPending === true})`;
}

function rendererDisableCapture(runId, ordinal, epoch) {
  try {
    const key = `qqcall.official.av.observer.${runId}.${epoch}`;
    const ownValue = (object, name) => {
      const descriptor = Object.getOwnPropertyDescriptor(object, name);
      return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
    };
    const descriptor = Object.getOwnPropertyDescriptor(window, Symbol.for(key));
    const state = descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : null;
    if (!state || ownValue(state, "schema") !== "qqcall.official.av.observer.page.v1" ||
        ownValue(state, "runId") !== runId || ownValue(state, "ordinal") !== ordinal ||
        ownValue(state, "epoch") !== epoch || ownValue(state, "nativeConditional") !== true) return false;
    const capture = Object.getOwnPropertyDescriptor(state, "captureEnabled");
    if (!capture || !Object.hasOwn(capture, "value") || capture.writable !== true) return false;
    Object.defineProperty(state, "captureEnabled", { ...capture, value: false });
    return true;
  } catch { return false; }
}

function rendererInstall(runId, ordinal, epoch, leaseExpiresAtMs, incomingOnly = false, nativeConditional = false) {
  const schema = "qqcall.official.av.observer.page.v1";
  const closedSchema = "qqcall.official.av.observer.closed.v1";
  const key = Symbol.for(`qqcall.official.av.observer.${runId}.${epoch}`);
  const cancelledKey = Symbol.for(`qqcall.official.av.observer.cancelled.${runId}`);
  const cancelledSchema = "qqcall.official.av.observer.cancelled.v1";
  try {
    const cancelled = window[cancelledKey];
    if ((cancelled?.schema === cancelledSchema && Number.isSafeInteger(cancelled.cancelledThrough) && epoch <= cancelled.cancelledThrough) ||
        Date.now() >= leaseExpiresAtMs) return { status: "timed_out", embedCount: null, postMessageAvailable: null };
  }
  catch { return { status: "unavailable", embedCount: null, postMessageAvailable: null }; }
  const exact = (value, keys) => {
    try { return !!value && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length && keys.every((k) => Object.hasOwn(value, k)); }
    catch { return false; }
  };
  let count;
  try { count = document.querySelectorAll('embed[type="application/x-ppapi-avSDK"]').length; }
  catch { return { status: "unavailable", embedCount: null, postMessageAvailable: null }; }
  if (!Number.isSafeInteger(count) || count < 0 || count > 4) return { status: "ambiguous_embed", embedCount: null, postMessageAvailable: null };
  if (count !== 1) return { status: count > 1 ? "ambiguous_embed" : "no_embed", embedCount: count, postMessageAvailable: false };
  let embed;
  try { embed = document.querySelectorAll('embed[type="application/x-ppapi-avSDK"]')[0]; }
  catch { return { status: "unavailable", embedCount: null, postMessageAvailable: null }; }
  let original;
  try { original = embed?.postMessage; } catch { original = null; }
  if (!embed || typeof original !== "function") return { status: "no_method", embedCount: 1, postMessageAvailable: false };
  let current;
  try { current = window[key]; } catch { current = null; }
  if (current && current.schema === schema && current.epoch === epoch && current.embed === embed && current.active &&
      current.incomingOnly === incomingOnly && current.nativeConditional === nativeConditional) {
    return { status: incomingOnly ? "armed_incoming_only" : nativeConditional ? "armed_native_conditional" : "armed",
      embedCount: 1, postMessageAvailable: true };
  }
  let originalOwnDescriptor;
  let originalWindowDescriptor;
  try {
    originalOwnDescriptor = Object.getOwnPropertyDescriptor(embed, "postMessage");
    originalWindowDescriptor = Object.getOwnPropertyDescriptor(window, key);
  } catch { return { status: "unavailable", embedCount: 1, postMessageAvailable: true }; }
  if (originalWindowDescriptor) {
    return { status: "install_state_publish_failed", embedCount: 1, postMessageAvailable: true, cleanupComplete: true };
  }

  const allowedOutgoing = new Set([1, 4, 5, 7, 8, 9, 10, 11, 13, 14, 57]);
  const allowedIncoming = new Set([9, 20003, 20004, 20005, 20006, 20010, 20011, 20017, 20001, 120003]);
  const scalar = (value) => {
    if (value === undefined) return { type: "undefined" };
    if (value === null) return { type: "null" };
    if (typeof value === "string") {
      const length = value.length;
      if (length > 8192) { state.incomplete = true; return { type: "string", length: 8192, ref: null, truncated: true }; }
      return { type: "string", length, ref: null };
    }
    if (typeof value === "boolean") return { type: "boolean", value };
    if (typeof value === "number") return { type: "number" };
    if (Array.isArray(value)) {
      let length = null;
      try {
        const descriptor = Object.getOwnPropertyDescriptor(value, "length");
        length = descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : null;
      } catch { return { type: "array", length: 0, truncated: true }; }
      if (!Number.isSafeInteger(length) || length < 0) return { type: "array", length: 0, truncated: true };
      return { type: "array", length: Math.min(length, 16), truncated: length > 16 };
    }
    if (typeof value === "object") return { type: "object" };
    return { type: "other" };
  };
  const dataValue = (object, name) => {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(object, name);
      return descriptor && Object.hasOwn(descriptor, "value") ? { present: true, value: descriptor.value } : { present: false, value: undefined };
    } catch { return { present: false, value: undefined }; }
  };
  const normalize = (input, refs, direction, second, secondPresent) => {
    let value = input;
    if (typeof value === "string") {
      if (value.length > 8192) { state.incomplete = true; return null; }
      try { value = JSON.parse(value); } catch { return null; }
    }
    if (value && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype) {
      const c1 = dataValue(value, "cmd");
      const c2 = dataValue(value, "command");
      if (!c1.present && !c2.present) return null;
      const commandValue = c1.present ? c1.value : c2.value;
      if (c1.present && c2.present && c1.value !== c2.value) return null;
      const p1 = dataValue(value, "param");
      const p2 = dataValue(value, "value");
      if (p1.present && p2.present && p1.value !== p2.value) return null;
      const id = dataValue(value, "id");
      return { commandValue, parameterPresent: p1.present || p2.present, parameter: p1.present ? p1.value : p2.value,
        requestId: id.present ? id.value : undefined };
    }
    if (direction === "outgoing" && input !== undefined) return { commandValue: input, parameterPresent: secondPresent, parameter: second, requestId: undefined };
    return null;
  };
  const numberCommand = (value) => {
    if (Number.isSafeInteger(value)) return value;
    if (typeof value === "string" && /^[0-9]{1,6}$/.test(value)) return Number(value);
    return null;
  };
  const numericField = (obj, name, booleanAllowed = false) => {
    const entry = dataValue(obj, name);
    if (!entry.present) return null;
    const value = entry.value;
    if (booleanAllowed && typeof value === "boolean") return value;
    return Number.isSafeInteger(value) && value >= 0 && value <= 1000000 ? value : null;
  };
  const startupValueKind = (value) => {
    if (value === undefined) return "undefined";
    if (value === null) return "null";
    if (typeof value === "string") return "string";
    if (typeof value === "number") return "number";
    if (typeof value === "boolean") return "boolean";
    if (Array.isArray(value)) return "array";
    if (typeof value === "object") return "object";
    return "other";
  };
  const tinyIdKind = (value) => {
    if (value === undefined) return "undefined";
    if (value === null) return "null";
    if (typeof value === "number") return value === 0 ? "number_zero" : "number_other";
    if (typeof value === "string") return value.length === 0 ? "empty_string" : value === "0" ? "zero_string" : "nonempty_string";
    return "other";
  };
  const exactDenseArrayValues = (value, expectedLength) => {
    try {
      if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
      const keys = Reflect.ownKeys(value);
      if (keys.length !== expectedLength + 1 || !keys.includes("length")) return null;
      const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
      if (!lengthDescriptor || !Object.hasOwn(lengthDescriptor, "value") || lengthDescriptor.value !== expectedLength) return null;
      const result = [];
      for (let index = 0; index < expectedLength; index += 1) {
        const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
        if (!descriptor || !Object.hasOwn(descriptor, "value")) return null;
        if (typeof descriptor.value === "string" && descriptor.value.length > 8192) return null;
        result.push(descriptor.value);
      }
      if (keys.some((key) => typeof key !== "string" || (key !== "length" && !/^(0|[1-9][0-9]*)$/.test(key)) ||
          (key !== "length" && Number(key) >= expectedLength))) return null;
      return result;
    } catch { return null; }
  };
  const loginStartupProjection = (parameter) => {
    const values = exactDenseArrayValues(parameter, 5);
    if (!values) return null;
    const [selfUid, selfUin, tinyId, dbPath, machineId] = values;
    const dbPathIsString = typeof dbPath === "string";
    return {
      tinyIdKind: tinyIdKind(tinyId),
      tinyIdEqualsSelfUin: typeof tinyId === "string" && typeof selfUin === "string" && tinyId === selfUin,
      tinyIdEqualsSelfUid: typeof tinyId === "string" && typeof selfUid === "string" && tinyId === selfUid,
      dbPathKind: startupValueKind(dbPath),
      dbPathLength: dbPathIsString ? dbPath.length : null,
      dbPathIsAbsolute: dbPathIsString && (/^[a-zA-Z]:[\\/]/.test(dbPath) || /^\\\\[^\\/]+[\\/][^\\/]+/.test(dbPath)),
      dbPathEndsSelfUin: dbPathIsString && typeof selfUin === "string" &&
        dbPath.replace(/[\\/]+$/, "").split(/[\\/]/).pop() === selfUin,
      machineIdKind: startupValueKind(machineId),
      machineIdLength: typeof machineId === "string" ? machineId.length : null,
    };
  };
  const sdkInfoStartupProjection = (parameter) => {
    const values = exactDenseArrayValues(parameter, 1);
    if (!values || typeof values[0] !== "string") return null;
    return {
      kind: "string",
      length: values[0].length,
      exactKnownVersionMatches1_0_1168: values[0] === "1.0.1168",
    };
  };
  const identityRef = (value, refs) => {
    if (!(typeof value === "string" || Number.isSafeInteger(value))) return null;
    const sourceType = typeof value;
    const canonical = String(value);
    if (!canonical || canonical.length > 128) { state.incomplete = true; return null; }
    const key = `${sourceType}:${canonical}`;
    let ref = refs.get(key);
    if (!ref) {
      if (refs.size >= 128) { state.incomplete = true; return null; }
      ref = `v${refs.size + 1}`;
      refs.set(key, ref);
    }
    return ref;
  };
  const identityFieldRef = (obj, name, refs) => {
    const entry = dataValue(obj, name);
    return entry.present ? identityRef(entry.value, refs) : null;
  };
  const command4Projection = (command, parameter, refs) => {
    const empty = { available: false, sceneId: null, inviteCount: null, subBusinessType: null, inviteOriginal: null,
      isCreator: null, relationId: null, roomId: null, selfUid: null, inviteUidRefs: [], inviteUidCount: 0, inviteUidTruncated: false };
    if (command !== 4 || !Array.isArray(parameter)) return empty;
    let candidate;
    try {
      const descriptor = Object.getOwnPropertyDescriptor(parameter, "0");
      if (!descriptor || !Object.hasOwn(descriptor, "value")) return empty;
      candidate = descriptor.value;
    } catch { return empty; }
    if (typeof candidate === "string") {
      if (candidate.length > 8192) return empty;
      try { candidate = JSON.parse(candidate); } catch { return empty; }
    }
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate) || Object.getPrototypeOf(candidate) !== Object.prototype) return empty;
    let uidValues = [];
    let uidTruncated = false;
    const invite = dataValue(candidate, "invite_uids");
    if (invite.present && Array.isArray(invite.value)) {
      try {
        const lengthDescriptor = Object.getOwnPropertyDescriptor(invite.value, "length");
        const length = lengthDescriptor && Object.hasOwn(lengthDescriptor, "value") ? lengthDescriptor.value : null;
        if (!Number.isSafeInteger(length) || length < 0) return empty;
        uidTruncated = length > 16;
        for (let index = 0; index < Math.min(length, 16); index += 1) {
          const d = Object.getOwnPropertyDescriptor(invite.value, String(index));
          if (!d || !Object.hasOwn(d, "value") || !(typeof d.value === "string" || Number.isSafeInteger(d.value))) continue;
          const ref = identityRef(d.value, refs);
          if (ref) uidValues.push(ref);
        }
      } catch { uidTruncated = true; }
    }
    let actualInviteCount = 0;
    try {
      const lengthDescriptor = invite.present && Array.isArray(invite.value) ? Object.getOwnPropertyDescriptor(invite.value, "length") : null;
      const length = lengthDescriptor && Object.hasOwn(lengthDescriptor, "value") ? lengthDescriptor.value : 0;
      actualInviteCount = Number.isSafeInteger(length) && length >= 0 ? Math.min(length, 1000000) : 0;
    } catch { actualInviteCount = 0; }
    return {
      available: true,
      sceneId: numericField(candidate, "scene_id"),
      inviteCount: numericField(candidate, "invite_count"),
      subBusinessType: numericField(candidate, "sub_business_type"),
      inviteOriginal: numericField(candidate, "invite_original"),
      isCreator: numericField(candidate, "is_creator", true),
      relationId: identityFieldRef(candidate, "relation_id", refs),
      roomId: identityFieldRef(candidate, "room_id", refs),
      selfUid: identityFieldRef(candidate, "self_uid", refs),
      inviteUidRefs: uidValues,
      inviteUidCount: actualInviteCount,
      inviteUidTruncated: uidTruncated,
    };
  };
  const enterRoomProjection = (direction, command, parameter, refs) => {
    if (direction !== "incoming" || command !== 20004) return null;
    try {
      if (!Array.isArray(parameter) || Object.getPrototypeOf(parameter) !== Array.prototype) return null;
      const keys = Reflect.ownKeys(parameter);
      const expectedKeys = new Set(["0", "1", "2", "3", "4", "5", "6", "length"]);
      if (keys.length !== expectedKeys.size || keys.some((key) => typeof key !== "string" || !expectedKeys.has(key))) return null;
      const lengthDescriptor = Object.getOwnPropertyDescriptor(parameter, "length");
      if (!lengthDescriptor || !Object.hasOwn(lengthDescriptor, "value") || lengthDescriptor.value !== 7) return null;
      const values = [];
      for (let index = 0; index < 7; index += 1) {
        const descriptor = Object.getOwnPropertyDescriptor(parameter, String(index));
        if (!descriptor || !Object.hasOwn(descriptor, "value")) return null;
        values.push(descriptor.value);
      }

      const [ret, scene, relationValue, peerValue, isCreator, errMsg, sdkProvider] = values;
      if (!Number.isInteger(ret) || ret < -2147483648 || ret > 2147483647 ||
          !Number.isSafeInteger(scene) || scene < 0 || scene > 1000000 ||
          !(typeof isCreator === "boolean" || isCreator === 0 || isCreator === 1) ||
          !Number.isSafeInteger(sdkProvider)) return null;
      // Slot five is only checked as a bounded scalar so cloning cannot traverse or
      // retain an arbitrary object. Its content is never projected or persisted.
      const errMsgIsBoundedScalar = errMsg === null || errMsg === undefined || typeof errMsg === "boolean" ||
        (typeof errMsg === "string" && errMsg.length <= 8192) ||
        (typeof errMsg === "number" && Number.isFinite(errMsg));
      if (!errMsgIsBoundedScalar) return null;
      const identity = (value) => {
        if (!(typeof value === "string" || Number.isSafeInteger(value))) return null;
        const text = String(value);
        if (text.length > 128) return null;
        if (text.length === 0) return { key: null, length: 0 };
        return { key: `${typeof value}:${text}`, length: text.length };
      };
      const relation = identity(relationValue);
      const peer = identity(peerValue);
      if (!relation || !peer) return null;
      const newKeys = new Set([relation.key, peer.key].filter((key) => key !== null && !refs.has(key)));
      if (refs.size + newKeys.size > 128) { state.incomplete = true; return null; }
      const relationRef = relation.key === null ? null : identityRef(relationValue, refs);
      const peerRef = peer.key === null ? null : identityRef(peerValue, refs);
      if ((relation.key !== null && !relationRef) || (peer.key !== null && !peerRef)) return null;
      return {
        ret,
        scene,
        relationId: { ref: relationRef, length: relation.length },
        peerUid: { ref: peerRef, length: peer.length },
        isCreator,
        sdkProvider,
      };
    } catch { return null; }
  };
  const sameDescriptor = (left, right) => {
    if (left === right) return true;
    if (!left || !right || left.configurable !== right.configurable || left.enumerable !== right.enumerable) return false;
    const leftData = Object.hasOwn(left, "value");
    const rightData = Object.hasOwn(right, "value");
    if (leftData !== rightData) return false;
    return leftData ? left.value === right.value && left.writable === right.writable
      : left.get === right.get && left.set === right.set;
  };
  const state = {
    schema, runId, ordinal, epoch, embed, original, incomingOnly, nativeConditional, captureEnabled: true,
    observeNativeAttempt: null,
    originalOwnDescriptor, originalWindowDescriptor,
    wrapper: null, onIncoming: null, pagehide: null, active: true,
    refs: new Map(), events: [], dropped: 0, incomplete: false, sequence: 0, totalBytes: 0, reportedDropped: 0,
    droppedSaturated: false, leaseTimer: null, cleanupComplete: true,
    nativeCallAttempts: 0, nativeCallAttemptsSaturated: false, nativeObserverActive: false,
    resources: {
      wrapperAttempted: false, wrapperInstalled: false,
      embedListenerAttempted: false, embedListenerInstalled: false,
      pagehideListenerAttempted: false, pagehideListenerInstalled: false,
      statePublishAttempted: false, statePublished: false,
      leaseTimerAttempted: false, leaseTimerInstalled: false,
    },
  };
  const append = (direction, raw, second, secondPresent = false) => {
    try {
      if (!state.active || !state.captureEnabled) return;
      const refs = state.refs;
      let source = raw;
      if (direction === "incoming") {
        if (raw && typeof raw === "object") {
          const data = dataValue(raw, "data");
          if (!data.present) return;
          source = data.value;
        }
      }
      const normalized = normalize(source, refs, direction, second, secondPresent);
      if (!normalized) return;
      const command = numberCommand(normalized.commandValue);
      if (!Number.isSafeInteger(command) || !(direction === "outgoing" ? allowedOutgoing : allowedIncoming).has(command)) return;
      let parameterKind = "absent";
      let parameterLength = null;
      let parameterTruncated = false;
      let parameterItems = [];
      const parameter = normalized.parameter;
      if (normalized.parameterPresent) {
        let parameterIsArray = false;
        try { parameterIsArray = Array.isArray(parameter); }
        catch { parameterKind = "invalid"; }
        if (parameterKind === "invalid") {
          parameterLength = null;
          parameterItems = [];
        } else if (parameterIsArray) {
          parameterKind = "array";
          try {
            const lengthDescriptor = Object.getOwnPropertyDescriptor(parameter, "length");
            parameterLength = lengthDescriptor && Object.hasOwn(lengthDescriptor, "value") ? lengthDescriptor.value : null;
            if (!Number.isSafeInteger(parameterLength) || parameterLength < 0 || parameterLength > 1000000) return;
            parameterTruncated = parameterLength > 16;
            const count = Math.min(parameterLength, 16);
            for (let i = 0; i < count; i += 1) {
              const d = Object.getOwnPropertyDescriptor(parameter, String(i));
              parameterItems.push(!d ? { type: "absent" } : Object.hasOwn(d, "value") ? scalar(d.value) : { type: "unknown" });
            }
          } catch { parameterKind = "invalid"; parameterLength = null; parameterItems = []; }
        } else if (parameter && typeof parameter === "object") {
          parameterKind = "object";
          parameterItems = [{ type: "object" }];
        } else {
          parameterKind = "scalar";
          parameterItems = [scalar(parameter)];
        }
      }
      const command4 = command4Projection(command, parameter, refs);
      const enterRoom = enterRoomProjection(direction, command, parameter, refs);
      const rawId = normalized.requestId;
      let requestId = { present: false, type: "absent", length: null, ref: null };
      if (typeof rawId === "string") {
        const ref = identityRef(rawId, refs);
        requestId = { present: true, type: "string", length: Math.min(rawId.length, 8192), ref };
        if (rawId.length > 128) state.incomplete = true;
      } else if (Number.isSafeInteger(rawId)) {
        requestId = { present: true, type: "number", length: null, ref: identityRef(rawId, refs) };
      } else if (rawId !== undefined) {
        requestId = { present: false, type: "unsupported", length: null, ref: null };
      }
      const event = { eventIndex: ++state.sequence, direction, command, requestId, windowOrdinal: ordinal, documentEpoch: epoch,
        parameterKind, parameterLength, parameterTruncated, parameterItems, command4, enterRoom };
      if (direction === "outgoing" && command === 9 && parameterKind === "array" && parameterLength === 3 && !parameterTruncated) {
        const fields = [0, 1, 2].map((index) => dataValue(parameter, String(index)));
        if (fields.every((field) => field.present) && fields[0].value === 1 && typeof fields[1].value === "string" &&
            fields[1].value.length > 0 && fields[1].value.length <= 128 && [1, 2, 3, 100].includes(fields[2].value)) {
          event.privateReject = { sceneId: 1, rejectReason: fields[2].value };
        }
      }
      if (direction === "incoming" && command === 9 && parameterKind === "array" && parameterLength === 2 && !parameterTruncated) {
        const fields = [0, 1].map((index) => dataValue(parameter, String(index)));
        if (fields.every((field) => field.present) && Number.isInteger(fields[0].value) &&
            fields[0].value >= -2147483648 && fields[0].value <= 2147483647 && typeof fields[1].value === "string") {
          event.rejectAck = { returnCode: fields[0].value };
        }
      }
      if (direction === "outgoing" && command === 1) {
        const loginStartup = loginStartupProjection(parameter);
        if (loginStartup) event.loginStartup = loginStartup;
      } else if (direction === "outgoing" && command === 57) {
        const sdkInfoStartup = sdkInfoStartupProjection(parameter);
        if (sdkInfoStartup) event.sdkInfoStartup = sdkInfoStartup;
      }
      const bytes = JSON.stringify(event).length * 2;
      if (state.sequence > 256 || state.events.length >= 256 || state.totalBytes + bytes > 96 * 1024) {
        if (state.dropped < 1000000) state.dropped += 1;
        else state.droppedSaturated = true;
        state.incomplete = true;
        return;
      }
      state.events.push(event);
      state.totalBytes += bytes;
    } catch {
      if (state.dropped < 1000000) state.dropped += 1;
      else state.droppedSaturated = true;
      state.incomplete = true;
    }
  };
  state.observeNativeAttempt = function (receiver, args) {
    let expired = true;
    try { expired = Date.now() >= leaseExpiresAtMs; } catch { /* fail closed */ }
    if (expired || !state.active || !state.captureEnabled || !nativeConditional || receiver !== embed || state.nativeObserverActive) return;
    if (state.nativeCallAttempts >= 1000000) {
      state.nativeCallAttemptsSaturated = true;
      return;
    }
    state.nativeCallAttempts += 1;
    state.nativeObserverActive = true;
    try {
      let lengthDescriptor;
      try { lengthDescriptor = Object.getOwnPropertyDescriptor(args, "length"); } catch { lengthDescriptor = null; }
      const argc = lengthDescriptor && Object.hasOwn(lengthDescriptor, "value") ? lengthDescriptor.value : null;
      if (!Number.isSafeInteger(argc) || argc < 0 || argc > 2 || state.events.length >= 256 ||
          state.totalBytes >= 96 * 1024 || state.refs.size >= 128) {
        if (state.dropped < 1000000) state.dropped += 1; else state.droppedSaturated = true;
        state.incomplete = true;
        return;
      }
      let first;
      let second;
      if (argc > 0) {
        const descriptor = Object.getOwnPropertyDescriptor(args, "0");
        if (!descriptor || !Object.hasOwn(descriptor, "value")) throw new Error("argument_unavailable");
        first = descriptor.value;
      }
      if (argc > 1) {
        const descriptor = Object.getOwnPropertyDescriptor(args, "1");
        if (!descriptor || !Object.hasOwn(descriptor, "value")) throw new Error("argument_unavailable");
        second = descriptor.value;
      }
      append("outgoing", first, second, argc > 1);
    } catch {
      if (state.dropped < 1000000) state.dropped += 1; else state.droppedSaturated = true;
      state.incomplete = true;
    } finally { state.nativeObserverActive = false; }
  };
  const wrapper = incomingOnly || nativeConditional ? null : function (...args) {
    let expired = true;
    try { expired = Date.now() >= leaseExpiresAtMs; } catch { /* skip capture if the lease clock is unavailable */ }
    if (expired || !state.active || !state.captureEnabled) {
      if (expired) { try { state.cleanup(false, true); } catch { /* observation cannot change SDK behavior */ } }
      return Reflect.apply(original, this, args);
    }
    try { append("outgoing", args[0], args[1], args.length > 1); } catch { /* observation cannot change SDK behavior */ }
    return Reflect.apply(original, this, args);
  };
  state.wrapper = wrapper;
  state.onIncoming = (event) => {
    let expired = true;
    try { expired = Date.now() >= leaseExpiresAtMs; } catch { /* an unread event is safer when the deadline cannot be checked */ }
    if (expired || !state.active) {
      if (expired) { try { state.cleanup(false, true); } catch { /* incoming observation is best effort */ } }
      return;
    }
    let data;
    try { data = event?.data; } catch { return; }
    append("incoming", { data });
  };
  state.pagehide = () => { try { window[key]?.cleanup?.(false, true); } catch { /* renderer is leaving */ } };
  state.cleanup = (flushPending = false, retainLossSummary = false) => {
    if (!state.active) return { cleanupComplete: state.cleanupComplete !== false, events: [], dropped: 0,
      incomplete: false, totalBytes: 0, queueLossPossible: false, nativeCallAttempts: state.nativeCallAttempts,
      nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated };
    state.active = false;
    let complete = state.cleanupComplete !== false;
    if (state.resources.leaseTimerAttempted) {
      if (!state.resources.leaseTimerInstalled) complete = false;
      else {
        try {
          clearTimeout(state.leaseTimer);
          state.resources.leaseTimerInstalled = false;
          state.resources.leaseTimerAttempted = false;
        } catch { complete = false; }
      }
    }
    if (state.resources.embedListenerAttempted) {
      try {
        embed.removeEventListener("message", state.onIncoming);
        state.resources.embedListenerInstalled = false;
        state.resources.embedListenerAttempted = false;
      } catch { complete = false; }
    }
    if (state.resources.pagehideListenerAttempted) {
      try {
        window.removeEventListener("pagehide", state.pagehide);
        state.resources.pagehideListenerInstalled = false;
        state.resources.pagehideListenerAttempted = false;
      } catch { complete = false; }
    }
    if (state.resources.wrapperAttempted) {
      try {
        const current = Object.getOwnPropertyDescriptor(embed, "postMessage");
        if (current?.value === wrapper) {
          if (state.originalOwnDescriptor) Object.defineProperty(embed, "postMessage", state.originalOwnDescriptor);
          else if (!delete embed.postMessage) complete = false;
          state.resources.wrapperInstalled = false;
          state.resources.wrapperAttempted = false;
        } else if (sameDescriptor(current, state.originalOwnDescriptor)) {
          state.resources.wrapperInstalled = false;
          state.resources.wrapperAttempted = false;
        } else complete = false;
      } catch { complete = false; }
    }
    const pendingEvents = state.events.splice(0);
    const pendingDropped = Math.max(0, state.dropped - state.reportedDropped);
    const deliveredEvents = flushPending ? pendingEvents : [];
    const dropped = flushPending ? pendingDropped : Math.min(1000000, pendingEvents.length + pendingDropped);
    const queueLossPossible = state.droppedSaturated;
    const wasIncomplete = state.incomplete || dropped > 0 || queueLossPossible;
    try { state.refs.clear(); } catch { complete = false; }
    state.cleanupComplete = complete;
    if (state.resources.statePublishAttempted) {
      try {
        const current = Object.getOwnPropertyDescriptor(window, key);
        if (current?.value === state) {
          if (retainLossSummary) {
            Object.defineProperty(window, key, { configurable: true, enumerable: false, writable: false, value: {
              schema: closedSchema, ordinal, epoch, cleanupComplete: complete,
              dropped: Math.min(1000000, pendingEvents.length + pendingDropped),
              incomplete: state.incomplete || pendingEvents.length > 0 || pendingDropped > 0,
              totalBytes: 0, queueLossPossible, nativeCallAttempts: state.nativeCallAttempts,
              nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated,
            } });
          } else if (!delete window[key]) complete = false;
          state.resources.statePublished = false;
          state.resources.statePublishAttempted = false;
        } else if (sameDescriptor(current, state.originalWindowDescriptor)) {
          state.resources.statePublished = false;
          state.resources.statePublishAttempted = false;
        } else complete = false;
      } catch { complete = false; }
    }
    state.cleanupComplete = complete;
    return { cleanupComplete: complete, events: deliveredEvents, dropped, incomplete: wasIncomplete,
      totalBytes: state.totalBytes, queueLossPossible, nativeCallAttempts: state.nativeCallAttempts,
      nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated };
  };
  let stage = "install_embed_method_failed";
  try {
    if (!incomingOnly && !nativeConditional) {
      state.resources.wrapperAttempted = true;
      Object.defineProperty(embed, "postMessage", { configurable: true, enumerable: state.originalOwnDescriptor?.enumerable ?? false,
        writable: true, value: wrapper });
      state.resources.wrapperInstalled = true;
    }
    stage = "install_embed_listener_failed";
    state.resources.embedListenerAttempted = true;
    embed.addEventListener("message", state.onIncoming);
    state.resources.embedListenerInstalled = true;
    stage = "install_pagehide_listener_failed";
    state.resources.pagehideListenerAttempted = true;
    window.addEventListener("pagehide", state.pagehide, { once: true });
    state.resources.pagehideListenerInstalled = true;
    stage = "install_state_publish_failed";
    state.resources.statePublishAttempted = true;
    Object.defineProperty(window, key, { configurable: true, enumerable: false, writable: false, value: state });
    state.resources.statePublished = true;
    stage = "install_lease_timer_failed";
    const remaining = Math.max(1, leaseExpiresAtMs - Date.now());
    state.resources.leaseTimerAttempted = true;
    state.leaseTimer = setTimeout(() => { try { state.cleanup(false, true); } catch { /* lease expiry must release only our wrapper */ } }, remaining);
    state.resources.leaseTimerInstalled = true;
    return { status: incomingOnly ? "armed_incoming_only" : nativeConditional ? "armed_native_conditional" : "armed",
      embedCount: 1, postMessageAvailable: true };
  } catch {
    let cleanupComplete = false;
    try { cleanupComplete = state.cleanup().cleanupComplete === true; } catch { /* return only a fixed cleanup result */ }
    return { status: stage, embedCount: 1, postMessageAvailable: true, cleanupComplete };
  }
}

function rendererDrain(runId, ordinal, epoch) {
  const closedSchema = "qqcall.official.av.observer.closed.v1";
  const key = Symbol.for(`qqcall.official.av.observer.${runId}.${epoch}`);
  let state;
  try { state = window[key]; } catch { state = null; }
  if (state?.schema === closedSchema && state.ordinal === ordinal && state.epoch === epoch) {
    try { if (window[key] === state) delete window[key]; } catch { /* loss summary is bounded and contains no source data */ }
    return { status: "closed", events: [], dropped: state.dropped, incomplete: state.incomplete,
      totalBytes: state.totalBytes, queueLossPossible: state.queueLossPossible,
      nativeCallAttempts: state.nativeCallAttempts, nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated };
  }
  if (!state || state.schema !== "qqcall.official.av.observer.page.v1" || state.ordinal !== ordinal || state.epoch !== epoch || !state.active) {
    return { status: "stale", events: [], dropped: 0, incomplete: false, totalBytes: 0, queueLossPossible: false,
      nativeCallAttempts: 0, nativeCallAttemptsSaturated: false };
  }
  const dropped = Math.max(0, state.dropped - state.reportedDropped);
  state.reportedDropped = state.dropped;
  return { status: "ok", events: state.events.splice(0), dropped, incomplete: state.incomplete, totalBytes: state.totalBytes,
    queueLossPossible: state.droppedSaturated, nativeCallAttempts: state.nativeCallAttempts,
    nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated };
}

function rendererCleanup(runId, ordinal, epoch, leaseExpiresAtMs, flushPending = false) {
  const closedSchema = "qqcall.official.av.observer.closed.v1";
  const key = Symbol.for(`qqcall.official.av.observer.${runId}.${epoch}`);
  const cancelledKey = Symbol.for(`qqcall.official.av.observer.cancelled.${runId}`);
  const cancelledSchema = "qqcall.official.av.observer.cancelled.v1";
  try {
    let marker = window[cancelledKey];
    if (marker === undefined) {
      marker = { schema: cancelledSchema, cancelledThrough: epoch, expiryTimer: null };
      Object.defineProperty(window, cancelledKey, { configurable: true, enumerable: false, writable: true, value: marker });
      marker.expiryTimer = setTimeout(() => {
        try { if (window[cancelledKey] === marker) delete window[cancelledKey]; } catch { /* expired marker is safe to leave */ }
      }, Math.max(1, leaseExpiresAtMs - Date.now()));
      try { marker.expiryTimer?.unref?.(); } catch { /* renderer timers do not expose unref */ }
    } else if (marker && marker.schema === cancelledSchema && Number.isSafeInteger(marker.cancelledThrough)) {
      marker.cancelledThrough = Math.max(marker.cancelledThrough, epoch);
    } else return { cleanupComplete: false };
  } catch { return { cleanupComplete: false }; }
  let state;
  try { state = window[key]; } catch { state = null; }
  if (!state) return { cleanupComplete: true, events: [], dropped: 0, incomplete: false, totalBytes: 0, queueLossPossible: false,
    nativeCallAttempts: 0, nativeCallAttemptsSaturated: false };
  if (state.schema === closedSchema && state.ordinal === ordinal && state.epoch === epoch) {
    try { if (window[key] === state) delete window[key]; } catch { /* the summary is fixed and contains only counts */ }
    return { cleanupComplete: state.cleanupComplete, events: [], dropped: state.dropped,
      incomplete: state.incomplete, totalBytes: state.totalBytes, queueLossPossible: state.queueLossPossible,
      nativeCallAttempts: state.nativeCallAttempts, nativeCallAttemptsSaturated: state.nativeCallAttemptsSaturated };
  }
  if (state.schema !== "qqcall.official.av.observer.page.v1" || state.ordinal !== ordinal || state.epoch !== epoch) {
    return { cleanupComplete: false, events: [], dropped: 0, incomplete: true, totalBytes: 0, queueLossPossible: true,
      nativeCallAttempts: 0, nativeCallAttemptsSaturated: false };
  }
  try { return state.cleanup(flushPending, false); }
  catch { return { cleanupComplete: false, events: [], dropped: 0, incomplete: true, totalBytes: 0, queueLossPossible: true,
    nativeCallAttempts: 0, nativeCallAttemptsSaturated: false }; }
}

function boundedResult(value, deadline, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout) {
  const pending = Promise.resolve(value);
  pending.then(() => {}, () => {});
  const remaining = deadline - Date.now();
  if (remaining <= 0) return Promise.resolve({ kind: "timed_out" });
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeoutFn(() => { if (!settled) { settled = true; resolve({ kind: "timed_out" }); } }, remaining);
    pending.then((result) => {
      if (!settled) { settled = true; clearTimeoutFn(timer); resolve({ kind: "resolved", result }); }
    }, () => { if (!settled) { settled = true; clearTimeoutFn(timer); resolve({ kind: "rejected" }); } });
  });
}

function createOfficialAVObserver({
  processObject = process,
  requireFunction = require,
  now = Date.now,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  runId = randomBytes(16).toString("hex"),
  incomingOnly = false,
  nativeConditional = false,
  maxLeaseMs = MAX_LEASE_MS,
  probeTimeoutMs = MAX_DOM_WAIT_MS,
} = {}) {
  if (!/^[a-f0-9]{32}$/.test(runId)) throw new TypeError("invalid run id");
  if (typeof incomingOnly !== "boolean") throw new TypeError("invalid incoming-only mode");
  if (typeof nativeConditional !== "boolean" || (incomingOnly && nativeConditional)) throw new TypeError("invalid native conditional mode");
  maxLeaseMs = Number.isSafeInteger(maxLeaseMs) ? Math.min(Math.max(maxLeaseMs, 1), MAX_LEASE_MS) : MAX_LEASE_MS;
  probeTimeoutMs = Number.isSafeInteger(probeTimeoutMs) ? Math.min(Math.max(probeTimeoutMs, 1), MAX_DOM_WAIT_MS) : MAX_DOM_WAIT_MS;
  let status = "rejected";
  let code = "observer_not_started";
  let started = false;
  let stopping = false;
  let stopPromise = null;
  let timer = null;
  let electronApp = null;
  let browserWindow = null;
  let browserWindowCreated = null;
  let windowsTruncated = false;
  let cleanupComplete = null;
  let incomplete = false;
  let queueLossPossible = false;
  let droppedEvents = 0;
  let encodedEventBytes = 0;
  let nextEventIndex = 0;
  let pendingDrainBatch = null;
  let drainAcceptanceClosed = false;
  let reportFrozen = false;
  let finalSnapshot = null;
  const windows = [];
  const events = [];

  const recordFor = (window) => windows.find((record) => record.window === window) || null;
  const live = (record) => {
    try { return record.window?.isDestroyed?.() === false && record.webContents?.isDestroyed?.() === false && record.window.webContents === record.webContents; }
    catch { return false; }
  };
  const getURL = (record) => {
    try { const url = record.webContents.getURL(); return typeof url === "string" && url.length <= 8192 ? url : null; }
    catch { return null; }
  };
  const stable = (record, url) => live(record) && getURL(record) === url;

  function snapshot() {
    if (finalSnapshot) return finalSnapshot;
    const report = {
      schema: SCHEMA, proofLevel: PROOF_LEVEL, status, code, coverageGap: true,
      comparisonScope: "document_epoch_only", outboundAttemptsOnly: true, windowsTruncated,
      windows: windows.map((record) => ({ ordinal: record.ordinal, documentEpoch: record.epoch,
        status: record.status, embedCount: record.embedCount, postMessageAvailable: record.postMessageAvailable,
        cleanupComplete: record.cleanupComplete, nativeConditionalStatus: record.nativeConditionalStatus,
        nativeCallAttempts: record.nativeCallAttempts, nativeCallAttemptsSaturated: record.nativeCallAttemptsSaturated })),
      events: events.slice(), droppedEvents, encodedEventBytes,
      incomplete: incomplete || droppedEvents > 0 || queueLossPossible, queueLossPossible,
      cleanupComplete,
    };
    if (Buffer.byteLength(JSON.stringify(report), "utf8") > MAX_REPORT_BYTES) {
      report.incomplete = true;
      while (report.events.length && Buffer.byteLength(JSON.stringify(report), "utf8") > MAX_REPORT_BYTES) report.events.pop();
      report.encodedEventBytes = Buffer.byteLength(JSON.stringify(report.events), "utf8");
    }
    return report;
  }

  function statusCodeForRuntime() {
    let processType;
    try { processType = processObject?.type; } catch { processType = null; }
    if (processType !== "browser") return "process_not_browser";
    let version;
    try { version = processObject?.versions?.electron; } catch { version = null; }
    if (typeof version !== "string" || !version || version.length > 64) return "electron_not_present";
    try {
      if (typeof requireFunction !== "function" || typeof requireFunction.resolve !== "function") return "electron_unavailable";
      if (requireFunction.resolve("electron") !== "electron") return "electron_not_builtin";
      const electron = requireFunction("electron");
      electronApp = electron?.app || null;
      browserWindow = electron?.BrowserWindow || null;
    } catch { return "electron_unavailable"; }
    if (!electronApp || typeof electronApp.on !== "function" || !browserWindow || typeof browserWindow.getAllWindows !== "function") {
      return "browser_window_unavailable";
    }
    return "ok";
  }

  function addDroppedEvents(count) {
    if (Number.isInteger(count) && count > 0) droppedEvents = Math.min(1000000, droppedEvents + count);
  }

  function acceptRendererEvents(record, contextEpoch, result) {
    if (reportFrozen) return;
    if (result.dropped > 0 || result.incomplete) incomplete = true;
    if (result.queueLossPossible) { queueLossPossible = true; incomplete = true; }
    addDroppedEvents(result.dropped);
    for (const candidate of result.events) {
      const event = canonicalEvent(candidate);
      if (!event || event.windowOrdinal !== record.ordinal || event.documentEpoch !== contextEpoch || events.length >= MAX_EVENTS) {
        addDroppedEvents(1);
        incomplete = true;
        continue;
      }
      const eventIndex = nextEventIndex + 1;
      const safe = { ...event, eventIndex };
      const bytes = Buffer.byteLength(JSON.stringify(safe), "utf8");
      if (encodedEventBytes + bytes > MAX_EVENT_BYTES) {
        addDroppedEvents(1);
        incomplete = true;
        continue;
      }
      events.push(safe);
      encodedEventBytes += bytes;
      nextEventIndex = eventIndex;
    }
  }

  function mergeNativeCounters(record, contextEpoch, attempts, saturated) {
    if (record.epoch !== contextEpoch || record.status === "destroyed") return false;
    record.nativeCallAttempts = Math.max(record.nativeCallAttempts, attempts);
    record.nativeCallAttemptsSaturated = record.nativeCallAttemptsSaturated || saturated;
    return true;
  }

  function noteNativeCleanupFailure(record, contextEpoch) {
    record.nativeCleanupDirty = true;
    incomplete = true;
    if (record.epoch === contextEpoch && record.status !== "destroyed") record.cleanupComplete = false;
  }

  function beginNativeCleanup(record, nativeObserver, deadline, destroyed = false) {
    if (!nativeObserver) return Promise.resolve(null);
    if (!destroyed && record.nativeCleanupObserver === nativeObserver && record.nativeCleanupPromise) {
      return record.nativeCleanupPromise;
    }
    const contextEpoch = record.epoch;
    let operation;
    try { operation = nativeObserver.stop({ deadline, destroyed }); }
    catch { operation = Promise.resolve(null); }
    const cleanup = Promise.resolve(operation).then((result) => {
      record.nativeCleanupResultObserver = nativeObserver;
      record.nativeCleanupResult = result || null;
      record.nativeCleanupSettled = true;
      if (!result || result.cleanupComplete !== true) noteNativeCleanupFailure(record, contextEpoch);
      if (record.epoch === contextEpoch) {
        record.nativeConditionalStatus = result?.status || "cleanup_incomplete";
      }
      return result || null;
    }, () => {
      record.nativeCleanupResultObserver = nativeObserver;
      record.nativeCleanupResult = null;
      record.nativeCleanupSettled = true;
      noteNativeCleanupFailure(record, contextEpoch);
      if (record.epoch === contextEpoch) record.nativeConditionalStatus = "cleanup_incomplete";
      return null;
    });
    record.nativeCleanupObserver = nativeObserver;
    record.nativeCleanupPromise = cleanup;
    record.nativeCleanupResultObserver = null;
    record.nativeCleanupResult = null;
    record.nativeCleanupSettled = false;
    return cleanup;
  }

  async function awaitNativeCleanup(record, nativeObserver, deadline, destroyed = false) {
    const cleanup = beginNativeCleanup(record, nativeObserver, deadline, destroyed);
    if (record.nativeCleanupResultObserver === nativeObserver && record.nativeCleanupSettled) {
      return record.nativeCleanupResult;
    }
    const result = await boundedResult(cleanup, deadline, setTimeoutFn, clearTimeoutFn);
    if (result.kind !== "resolved") {
      noteNativeCleanupFailure(record, record.epoch);
      return null;
    }
    return result.result;
  }

  async function cleanupRecord(record, timeoutMs = probeTimeoutMs, allowUrlChange = false, flushPending = false) {
    if (record.status === "destroyed") return record.cleanupComplete === true;
    const epoch = record.epoch;
    const url = record.lastUrl;
    const couldLoseQueuedEvents = ARMED_WINDOW_STATUSES.has(record.status);
    const cleanupDeadline = now() + timeoutMs;
    let nativeCleanupOk = record.nativeCleanupDirty !== true;
    if (record.nativeCallObserver) {
      const nativeObserver = record.nativeCallObserver;
      const nativeResult = await awaitNativeCleanup(record, nativeObserver, cleanupDeadline);
      if (record.nativeCallObserver === nativeObserver && record.nativeCleanupSettled &&
          record.nativeCleanupResultObserver === nativeObserver) record.nativeCallObserver = null;
      nativeCleanupOk = Boolean(nativeResult && nativeResult.cleanupComplete === true && record.nativeCleanupDirty !== true);
      if (!nativeCleanupOk) noteNativeCleanupFailure(record, epoch);
    }
    if (!url) return record.nativeCleanupDirty ? false : (record.cleanupComplete ?? true);
    if (!(allowUrlChange ? live(record) : stable(record, url))) {
      if (couldLoseQueuedEvents) { queueLossPossible = true; incomplete = true; }
      if (record.epoch === epoch && record.status !== "destroyed") {
        record.cleanupComplete = false;
        if (ARMED_WINDOW_STATUSES.has(record.status)) record.status = "cleanup_incomplete";
      }
      return false;
    }
    const deadline = Math.min(cleanupDeadline, now() + timeoutMs);
    let operation;
    try { operation = record.webContents.executeJavaScript(fixedCleanupScript(runId, record.ordinal, epoch, leaseDeadline, flushPending)); }
    catch {
      if (couldLoseQueuedEvents) { queueLossPossible = true; incomplete = true; }
      if (record.epoch === epoch && record.status !== "destroyed") {
        record.cleanupComplete = false;
        if (ARMED_WINDOW_STATUSES.has(record.status)) record.status = "cleanup_incomplete";
      }
      return false;
    }
    const result = await boundedResult(operation, deadline, setTimeoutFn, clearTimeoutFn);
    const response = result.kind === "resolved" && exactObject(result.result,
      ["cleanupComplete", "events", "dropped", "incomplete", "totalBytes", "queueLossPossible", "nativeCallAttempts", "nativeCallAttemptsSaturated"]) &&
      typeof result.result.cleanupComplete === "boolean" && Array.isArray(result.result.events) && result.result.events.length <= MAX_EVENTS &&
      Number.isInteger(result.result.dropped) && result.result.dropped >= 0 && result.result.dropped <= 1000000 &&
      typeof result.result.incomplete === "boolean" && Number.isInteger(result.result.totalBytes) && result.result.totalBytes >= 0 &&
      result.result.totalBytes <= MAX_EVENT_BYTES && typeof result.result.queueLossPossible === "boolean" &&
      Number.isInteger(result.result.nativeCallAttempts) && result.result.nativeCallAttempts >= 0 && result.result.nativeCallAttempts <= 1000000 &&
      typeof result.result.nativeCallAttemptsSaturated === "boolean";
    if (response) {
      acceptRendererEvents(record, epoch, result.result);
      mergeNativeCounters(record, epoch, result.result.nativeCallAttempts, result.result.nativeCallAttemptsSaturated);
    }
    else if (couldLoseQueuedEvents) { queueLossPossible = true; incomplete = true; }
    const installCleanupFailed = record.installCleanupDirty === true;
    const ok = nativeCleanupOk && record.nativeCleanupDirty !== true && response && result.result.cleanupComplete === true && !installCleanupFailed &&
      (allowUrlChange ? live(record) : stable(record, url));
    if (record.epoch === epoch && record.status !== "destroyed") {
      record.cleanupComplete = ok;
      if (!ok && ARMED_WINDOW_STATUSES.has(record.status)) record.status = "cleanup_incomplete";
    }
    return ok;
  }

  function cancelArmWait(record) {
    try { record.armWaitCancel?.(); } catch {}
  }

  function cancelRouteDiscovery(record) {
    if (record.routePollScheduled) {
      try { clearTimeoutFn(record.routePollTimer); } catch { incomplete = true; }
    }
    record.routePollScheduled = false;
    record.routePollTimer = null;
    record.routePollEpoch = null;
    record.routePollEligibleEpoch = null;
  }

  function blockDirtyDocument(record) {
    if (record.installCleanupDirty !== true && record.nativeCleanupDirty !== true) return false;
    record.status = "cleanup_incomplete";
    record.cleanupComplete = false;
    incomplete = true;
    return true;
  }

  function scheduleRouteDiscovery(record, contextEpoch, initialUrl = null) {
    if (!started || stopping || record.status !== "not_av" || record.epoch !== contextEpoch || record.navPending ||
        record.status === "destroyed" || now() >= leaseDeadline || record.routePollScheduled) return;
    if (record.routePollEligibleEpoch !== contextEpoch) {
      const observedUrl = initialUrl || getURL(record);
      if (!canonicalRendererURL(observedUrl)) return;
      record.routePollEligibleEpoch = contextEpoch;
    }
    const remaining = leaseDeadline - now();
    if (remaining <= 0) return;
    record.routePollScheduled = true;
    record.routePollEpoch = contextEpoch;
    try {
      record.routePollTimer = setTimeoutFn(() => {
        if (!record.routePollScheduled || record.routePollEpoch !== contextEpoch) return;
        record.routePollScheduled = false;
        record.routePollTimer = null;
        record.routePollEpoch = null;
        if (!started || stopping || now() >= leaseDeadline || record.epoch !== contextEpoch || record.navPending ||
            record.status !== "not_av" || !live(record) || blockDirtyDocument(record)) return;
        const currentUrl = getURL(record);
        if (currentUrl && exactAVRoute(currentUrl)) {
          let mainFrameReady = false;
          try { mainFrameReady = record.webContents.isLoadingMainFrame() === false; } catch {}
          if (!mainFrameReady) {
            scheduleRouteDiscovery(record, contextEpoch, currentUrl);
            return;
          }
          record.routePollEligibleEpoch = null;
          const attemptDeadline = Math.min(now() + probeTimeoutMs, leaseDeadline);
          void requestArm(record, attemptDeadline).catch(() => {
            if (record.epoch === contextEpoch && !stopping && record.status !== "destroyed") record.status = "unavailable";
            incomplete = true;
          });
          return;
        }
        scheduleRouteDiscovery(record, contextEpoch, currentUrl);
      }, Math.min(ROUTE_DISCOVERY_INTERVAL_MS, remaining));
    } catch {
      record.routePollScheduled = false;
      record.routePollTimer = null;
      record.routePollEpoch = null;
      incomplete = true;
    }
  }

  function waitForArmRetry(record, contextEpoch, url, deadline) {
    const remaining = Math.min(deadline, leaseDeadline) - now();
    if (remaining <= 0) return Promise.resolve(false);
    return new Promise((resolve) => {
      let settled = false;
      let timerHandle = null;
      const finish = (retry) => {
        if (settled) return;
        settled = true;
        try { clearTimeoutFn(timerHandle); } catch {}
        if (record.armWaitCancel === cancel) record.armWaitCancel = null;
        resolve(retry);
      };
      const cancel = () => finish(false);
      record.armWaitCancel = cancel;
      timerHandle = setTimeoutFn(() => finish(true), Math.min(50, remaining));
      if (!started || stopping || record.epoch !== contextEpoch || record.navPending || !stable(record, url)) cancel();
    });
  }

  async function armRecord(record, deadline) {
    if (!started || stopping || record.status === "destroyed") return;
      if (!live(record)) { record.status = "destroyed"; return; }
    if (blockDirtyDocument(record)) return;
    const url = getURL(record);
    if (!url) { record.status = "unavailable"; return; }
    const contextEpoch = record.epoch || (record.epoch = 1);
    if (!exactAVRoute(url)) {
      record.status = "not_av";
      record.embedCount = null;
      record.postMessageAvailable = null;
      scheduleRouteDiscovery(record, contextEpoch, url);
      return;
    }
    record.armAttemptedEpoch = contextEpoch;
    const attemptDeadline = Math.min(deadline, leaseDeadline);
    if (now() >= attemptDeadline) { record.status = "timed_out"; return; }
    if (ARMED_WINDOW_STATUSES.has(record.status) && record.lastUrl === url) return;
    record.lastUrl = url;

    while (true) {
      if (!started || stopping || now() >= attemptDeadline) {
        if (now() >= attemptDeadline && !stopping) record.status = "timed_out";
        return;
      }
      if (record.epoch !== contextEpoch || record.navPending || !stable(record, url)) {
        if (record.epoch === contextEpoch && !stopping) record.status = "stale_context";
        incomplete = true;
        return;
      }
      let operation;
      try { operation = record.webContents.executeJavaScript(fixedInstallScript(runId, record.ordinal, contextEpoch, leaseDeadline, incomingOnly, nativeConditional)); }
      catch { record.status = "unavailable"; return; }
      const result = await boundedResult(operation, attemptDeadline, setTimeoutFn, clearTimeoutFn);
      if (stopping || now() >= leaseDeadline) { await cleanupRecord(record); return; }
      if (!stable(record, url) || record.epoch !== contextEpoch || record.navPending) {
        if (record.epoch === contextEpoch) { record.status = "stale_context"; record.cleanupComplete = false; }
        incomplete = true;
        return;
      }
      if (result.kind === "timed_out") { record.status = "timed_out"; return; }
      const installFailure = result.kind === "resolved" && INSTALL_FAILURE_STATUSES.has(result.result?.status);
      const validInstallFailure = installFailure && exactObject(result.result,
        ["status", "embedCount", "postMessageAvailable", "cleanupComplete"]) &&
        result.result.embedCount === 1 && result.result.postMessageAvailable === true &&
        typeof result.result.cleanupComplete === "boolean";
      const validOrdinaryResult = result.kind === "resolved" && !installFailure &&
        exactObject(result.result, ["status", "embedCount", "postMessageAvailable"]) &&
        [...ARMED_WINDOW_STATUSES, "ambiguous_embed", "no_embed", "no_method", "unavailable"].includes(result.result.status);
      if ((!validInstallFailure && !validOrdinaryResult) ||
          !(result.result.embedCount === null || (Number.isInteger(result.result.embedCount) && result.result.embedCount >= 0 && result.result.embedCount <= 4)) ||
          !(result.result.postMessageAvailable === null || typeof result.result.postMessageAvailable === "boolean")) {
        record.status = "unavailable"; return;
      }
      record.embedCount = result.result.embedCount;
      record.postMessageAvailable = result.result.postMessageAvailable;
      if (validInstallFailure) {
        record.status = result.result.status;
        record.cleanupComplete = result.result.cleanupComplete;
        if (result.result.cleanupComplete === false) record.installCleanupDirty = true;
        if (!result.result.cleanupComplete) incomplete = true;
        return;
      }
      if (ARMED_WINDOW_STATUSES.has(result.result.status)) {
        const expectedStatus = incomingOnly ? "armed_incoming_only" : nativeConditional ? "armed_native_conditional" : "armed";
        if (result.result.status !== expectedStatus) { record.status = "unavailable"; incomplete = true; return; }
        if (nativeConditional) {
          record.status = "armed_native_conditional";
          let debuggerApi = null;
          try { debuggerApi = record.webContents.debugger; } catch { debuggerApi = null; }
          const nativeObserver = createNativeCallObserver({
            debuggerApi, runId, ordinal: record.ordinal, epoch: contextEpoch,
            now, setTimeoutFn, clearTimeoutFn, timeoutMs: Math.min(3000, Math.max(1, attemptDeadline - now())),
            onAbort() {
              if (record.epoch !== contextEpoch || stopping || record.status === "destroyed") return;
              record.nativeConditionalStatus = "unexpected_pause";
              record.status = "unavailable";
              incomplete = true;
              try {
                const disable = record.webContents.executeJavaScript(fixedDisableCaptureScript(runId, record.ordinal, contextEpoch));
                void boundedResult(disable, now() + probeTimeoutMs, setTimeoutFn, clearTimeoutFn).then(() => {}, () => {});
              } catch { /* cleanupRecord below remains the authoritative bounded cleanup attempt */ }
              void cleanupRecord(record, probeTimeoutMs, false, true);
            },
          });
          record.nativeCallObserver = nativeObserver;
          const nativeResult = await nativeObserver.start();
          if (record.epoch !== contextEpoch || stopping || record.status === "destroyed" || !stable(record, url)) {
            await cleanupRecord(record, Math.max(1, attemptDeadline - now()), true, true);
            return;
          }
          record.nativeConditionalStatus = nativeResult.status;
          if (nativeResult.status !== "breakpoint_set") {
            record.status = "unavailable";
            if (nativeResult.cleanupComplete === false) { record.cleanupComplete = false; incomplete = true; }
            await cleanupRecord(record, Math.max(1, attemptDeadline - now()), false, true);
            return;
          }
        } else record.status = result.result.status;
        return;
      }
      if (result.result.status === "ambiguous_embed") { record.status = "ambiguous_embed"; return; }
      if (result.result.status === "unavailable") { record.status = "unavailable"; return; }
      if (now() >= attemptDeadline) { record.status = "timed_out"; return; }
      record.status = "awaiting_dom";
      const retry = await waitForArmRetry(record, contextEpoch, url, attemptDeadline);
      if (!retry) {
        if (!stopping && record.epoch === contextEpoch && now() >= attemptDeadline) record.status = "timed_out";
        return;
      }
    }
  }

  function requestArm(record, deadline) {
    const contextEpoch = record.epoch || (record.epoch = 1);
    const url = getURL(record);
    if (!exactAVRoute(url)) return armRecord(record, deadline);
    if (record.armPromise && record.armPromiseEpoch === contextEpoch) return record.armPromise;
    if (record.armAttemptedEpoch === contextEpoch) return Promise.resolve();
    const pending = armRecord(record, deadline);
    record.armPromise = pending;
    record.armPromiseEpoch = contextEpoch;
    pending.then(() => {
      if (record.armPromise === pending) { record.armPromise = null; record.armPromiseEpoch = null; }
    }, () => {
      if (record.armPromise === pending) { record.armPromise = null; record.armPromiseEpoch = null; }
    });
    return pending;
  }

  function armAfterNavigation(record, deadline) {
    cancelRouteDiscovery(record);
    const contextEpoch = record.epoch;
    const pendingCleanup = record.navigationCleanup;
    void (async () => {
      if (pendingCleanup) {
        const cleanup = await boundedResult(pendingCleanup, deadline, setTimeoutFn, clearTimeoutFn);
        if (record.navigationCleanup === pendingCleanup) record.navigationCleanup = null;
        const replacedDocumentReady = record.fullDocumentReadyEpoch === contextEpoch;
        if ((cleanup.kind !== "resolved" || cleanup.result !== true) && !replacedDocumentReady) {
          if (record.epoch === contextEpoch) {
            record.cleanupComplete = false;
            record.status = "cleanup_incomplete";
          }
          incomplete = true;
          return;
        }
      }
      if (stopping || record.epoch !== contextEpoch || record.navPending || record.status === "destroyed") return;
      if (blockDirtyDocument(record)) return;
      const url = getURL(record);
      if (!url) { record.status = "unavailable"; return; }
      if (!exactAVRoute(url)) {
        record.status = "not_av";
        record.embedCount = null;
        record.postMessageAvailable = null;
        scheduleRouteDiscovery(record, contextEpoch, url);
        return;
      }
      await requestArm(record, deadline);
    })().catch(() => {
      if (record.epoch === contextEpoch) record.status = "unavailable";
      incomplete = true;
    });
  }

  function attachRecord(window) {
    if (!window || recordFor(window)) return recordFor(window);
    if (windows.length >= MAX_WINDOWS) { windowsTruncated = true; return null; }
    let webContents;
    try { webContents = window.webContents; } catch { webContents = null; }
    const record = { window, webContents, ordinal: windows.length + 1, epoch: 0, status: "awaiting_dom",
      embedCount: null, postMessageAvailable: null, cleanupComplete: null, listeners: null, lastUrl: null,
      navPending: false, navigationCleanup: null, armAttemptedEpoch: null, armPromise: null, armPromiseEpoch: null, armWaitCancel: null,
      routePollTimer: null, routePollScheduled: false, routePollEpoch: null, routePollEligibleEpoch: null,
      installCleanupDirty: false, fullDocumentNavigationEpoch: null, fullDocumentReadyEpoch: null,
      nativeCallObserver: null, nativeCleanupObserver: null, nativeCleanupPromise: null,
      nativeCleanupResultObserver: null, nativeCleanupResult: null, nativeCleanupSettled: false, nativeCleanupDirty: false,
      nativeConditionalStatus: null, nativeCallAttempts: 0, nativeCallAttemptsSaturated: false };
    windows.push(record);
    if (!webContents || typeof webContents.on !== "function") { record.status = "unavailable"; return record; }
    const domReady = () => {
      if (record.epochExhausted) { record.status = "timed_out"; incomplete = true; return; }
      if (!record.epoch) record.epoch = 1;
      record.navPending = false;
      cancelRouteDiscovery(record);
      if (record.fullDocumentNavigationEpoch === record.epoch) {
        record.fullDocumentReadyEpoch = record.epoch;
        record.installCleanupDirty = false;
        record.cleanupComplete = null;
      }
      armAfterNavigation(record, Math.min(now() + probeTimeoutMs, leaseDeadline));
    };
    const startNavigation = (_event, _url, _inPlace, isMainFrame) => {
      if (isMainFrame !== true) return;
      cancelArmWait(record);
      cancelRouteDiscovery(record);
      if (ARMED_WINDOW_STATUSES.has(record.status) || record.lastUrl) {
        const previous = record.navigationCleanup;
        const cleanup = cleanupRecord(record, probeTimeoutMs, true);
        record.navigationCleanup = previous
          ? Promise.all([previous, cleanup]).then((results) => results.every((result) => result === true)).catch(() => false)
          : cleanup.catch(() => false);
      }
      if (record.epoch >= 1000000) {
        record.epochExhausted = true;
        record.status = "timed_out";
        record.navPending = true;
        record.embedCount = null;
        record.postMessageAvailable = null;
        record.lastUrl = null;
        incomplete = true;
        return;
      }
      record.epoch += 1;
      record.fullDocumentNavigationEpoch = _inPlace === false ? record.epoch : null;
      record.fullDocumentReadyEpoch = null;
      record.navPending = true;
      record.nativeConditionalStatus = null;
      record.nativeCallAttempts = 0;
      record.nativeCallAttemptsSaturated = false;
      record.status = _inPlace === true ? "stale_context" : "awaiting_dom";
      record.embedCount = null;
      record.postMessageAvailable = null;
      record.lastUrl = null;
      incomplete = true;
    };
    const didNavigateInPage = (_event, navigationUrl, isMainFrame) => {
      if (isMainFrame !== true || !started || stopping || record.status === "destroyed" || !live(record)) return;
      if (typeof navigationUrl !== "string" || navigationUrl !== getURL(record)) return;
      if (record.epochExhausted) { record.status = "timed_out"; incomplete = true; return; }
      if (!record.epoch) record.epoch = 1;
      record.navPending = false;
      cancelRouteDiscovery(record);
      armAfterNavigation(record, Math.min(now() + probeTimeoutMs, leaseDeadline));
    };
    const destroyed = () => {
      cancelArmWait(record);
      cancelRouteDiscovery(record);
      if (ARMED_WINDOW_STATUSES.has(record.status) || record.lastUrl || record.navigationCleanup) {
        queueLossPossible = true;
        incomplete = true;
      }
      record.status = "destroyed";
      record.cleanupComplete = record.nativeCleanupDirty !== true;
      if (record.nativeCallObserver) {
        const nativeObserver = record.nativeCallObserver;
        record.nativeCallObserver = null;
        try {
          void beginNativeCleanup(record, nativeObserver, now() + probeTimeoutMs, true);
          const result = nativeObserver.status;
          record.nativeConditionalStatus = result.status;
          if (result.cleanupComplete !== true) noteNativeCleanupFailure(record, record.epoch);
          record.cleanupComplete = record.nativeCleanupDirty !== true;
        } catch {
          record.nativeConditionalStatus = "cleanup_incomplete";
          noteNativeCleanupFailure(record, record.epoch);
        }
      }
      record.installCleanupDirty = false;
    };
    record.listeners = { domReady, startNavigation, didNavigateInPage, destroyed };
    try {
      webContents.on("dom-ready", domReady);
      webContents.on("did-start-navigation", startNavigation);
      webContents.on("did-navigate-in-page", didNavigateInPage);
      webContents.on("destroyed", destroyed);
    } catch { record.status = "unavailable"; }
    return record;
  }

  async function drainRecord(record, deadline) {
    if (!ARMED_WINDOW_STATUSES.has(record.status) || !live(record)) return;
    const contextEpoch = record.epoch;
    const url = getURL(record);
    if (!url || !exactAVRoute(url) || !stable(record, record.lastUrl)) { record.status = "stale_context"; incomplete = true; return; }
    if (now() >= deadline) { record.status = "timed_out"; incomplete = true; return; }
    let operation;
    try { operation = record.webContents.executeJavaScript(fixedDrainScript(runId, record.ordinal, record.epoch)); }
    catch { record.status = "unavailable"; incomplete = true; return; }
    const result = await boundedResult(operation, deadline, setTimeoutFn, clearTimeoutFn);
    if (reportFrozen || drainAcceptanceClosed) return;
    if (!stable(record, url) || record.epoch !== contextEpoch) {
      if (record.epoch === contextEpoch) record.status = "stale_context";
      queueLossPossible = true;
      incomplete = true;
      return;
    }
    if (result.kind !== "resolved" || !exactObject(result.result, ["status", "events", "dropped", "incomplete", "totalBytes", "queueLossPossible", "nativeCallAttempts", "nativeCallAttemptsSaturated"]) ||
        !["ok", "closed"].includes(result.result.status) || !Array.isArray(result.result.events) || result.result.events.length > MAX_EVENTS ||
        !Number.isInteger(result.result.dropped) || result.result.dropped < 0 ||
        result.result.dropped > 1000000 || typeof result.result.incomplete !== "boolean" ||
        !Number.isInteger(result.result.totalBytes) || result.result.totalBytes < 0 || result.result.totalBytes > MAX_EVENT_BYTES ||
        typeof result.result.queueLossPossible !== "boolean" || !Number.isInteger(result.result.nativeCallAttempts) ||
        result.result.nativeCallAttempts < 0 || result.result.nativeCallAttempts > 1000000 ||
        typeof result.result.nativeCallAttemptsSaturated !== "boolean") {
      if (record.epoch === contextEpoch) record.status = "stale_context";
      queueLossPossible = true;
      incomplete = true;
      return;
    }
    acceptRendererEvents(record, contextEpoch, result.result);
    mergeNativeCounters(record, contextEpoch, result.result.nativeCallAttempts, result.result.nativeCallAttemptsSaturated);
    if (nativeConditional && record.nativeCallObserver) {
      const nativeStatus = record.nativeCallObserver.status;
      record.nativeConditionalStatus = nativeStatus.status;
      if (nativeStatus.status !== "breakpoint_set" && record.status === "armed_native_conditional") {
        record.status = "unavailable";
        incomplete = true;
      }
    }
    if (result.result.status === "closed" && record.epoch === contextEpoch) record.status = "timed_out";
  }

  async function start() {
    if (started) return snapshot();
    started = true;
    code = statusCodeForRuntime();
    if (code !== "ok") { status = "rejected"; return snapshot(); }
    status = "active";
    leaseDeadline = now() + maxLeaseMs;
    let found;
    try { found = browserWindow.getAllWindows(); }
    catch { status = "rejected"; code = "window_enumeration_failed"; return snapshot(); }
    if (!Array.isArray(found)) { status = "rejected"; code = "window_list_invalid"; return snapshot(); }
    windowsTruncated = found.length > MAX_WINDOWS;
    for (const window of found.slice(0, MAX_WINDOWS)) attachRecord(window);
    browserWindowCreated = (_event, window) => {
      const record = attachRecord(window);
      if (record && !record.webContents) record.status = "unavailable";
    };
    try { electronApp.on("browser-window-created", browserWindowCreated); }
    catch { status = "rejected"; code = "browser_window_unavailable"; return snapshot(); }
    leaseDeadline = now() + maxLeaseMs;
    const deadline = now() + probeTimeoutMs;
    await Promise.all(windows.map((record) => {
      if (record.epoch === 0) record.epoch = 1;
      return requestArm(record, deadline);
    }));
    if (stopping || status !== "active") return snapshot();
    timer = setTimeoutFn(() => { void stop(true); }, Math.max(1, leaseDeadline - now()));
    return snapshot();
  }

  async function drain() {
    if (!started) return snapshot();
    if (status !== "active" || stopping) return snapshot();
    if (pendingDrainBatch) return pendingDrainBatch;
    if (now() >= leaseDeadline) { await stop(true); return snapshot(); }
    const deadline = now() + probeTimeoutMs;
    const batch = Promise.all(windows.map((record) => drainRecord(record, deadline)))
      .then(() => snapshot(), () => {
        if (!reportFrozen) { queueLossPossible = true; incomplete = true; }
        return snapshot();
      });
    pendingDrainBatch = batch;
    batch.then(() => {
      if (pendingDrainBatch === batch) pendingDrainBatch = null;
    }, () => {
      if (pendingDrainBatch === batch) pendingDrainBatch = null;
    });
    return batch;
  }

  let leaseDeadline = 0;
  async function stop(expired = false) {
    if (stopPromise) return stopPromise;
    if (!started) return snapshot();
    stopping = true;
    stopPromise = (async () => {
      clearTimeoutFn(timer);
      timer = null;
      for (const record of windows) cancelArmWait(record);
      for (const record of windows) cancelRouteDiscovery(record);
      let removeListenersSucceeded = true;
      try { if (browserWindowCreated) electronApp?.removeListener?.("browser-window-created", browserWindowCreated); }
      catch { removeListenersSucceeded = false; }
      const deadline = now() + probeTimeoutMs;
      const nativeDeadline = now() + probeTimeoutMs;
      const nativeCleanups = windows.flatMap((record) => {
        const nativeObserver = record.nativeCallObserver;
        if (!nativeObserver) return [];
        return [{ record, nativeObserver,
          promise: beginNativeCleanup(record, nativeObserver, nativeDeadline) }];
      });
      const drainInFlight = pendingDrainBatch;
      if (drainInFlight) {
        const pending = await boundedResult(drainInFlight, deadline, setTimeoutFn, clearTimeoutFn);
        if (pending.kind !== "resolved" || now() >= deadline) {
          queueLossPossible = true;
          incomplete = true;
          drainAcceptanceClosed = true;
        }
      }
      const results = await Promise.all(windows.map(async (record) => {
        const pendingCleanup = record.navigationCleanup;
        if (pendingCleanup) {
          const pending = await boundedResult(pendingCleanup, deadline, setTimeoutFn, clearTimeoutFn);
          if (record.navigationCleanup === pendingCleanup) record.navigationCleanup = null;
          if (pending.kind !== "resolved" || pending.result !== true || now() >= deadline) {
            if (record.status !== "destroyed") {
              record.cleanupComplete = false;
              record.status = "cleanup_incomplete";
            }
            return false;
          }
        }
        if (now() >= deadline) {
          if (record.status !== "destroyed") {
            record.cleanupComplete = false;
            record.status = "cleanup_incomplete";
          }
          return false;
        }
        return cleanupRecord(record, Math.max(1, deadline - now()), false, true);
      }));
      for (const record of windows) {
        const listeners = record.listeners;
        if (!listeners) continue;
        try {
          record.webContents.removeListener("dom-ready", listeners.domReady);
          record.webContents.removeListener("did-start-navigation", listeners.startNavigation);
          record.webContents.removeListener("did-navigate-in-page", listeners.didNavigateInPage);
          record.webContents.removeListener("destroyed", listeners.destroyed);
        } catch { removeListenersSucceeded = false; }
      }
      const nativeResults = await Promise.all(nativeCleanups.map(async ({ record, nativeObserver, promise }) => {
        if (record.nativeCleanupObserver === nativeObserver && record.nativeCleanupPromise === promise && record.nativeCleanupSettled) {
          return record.nativeCleanupResult?.cleanupComplete === true;
        }
        const nativeResult = await awaitNativeCleanup(record, nativeObserver, nativeDeadline);
        return nativeResult?.cleanupComplete === true && record.nativeCleanupDirty !== true;
      }));
      cleanupComplete = removeListenersSucceeded && results.every(Boolean) && nativeResults.every(Boolean) &&
        windows.every((record) => record.status !== "cleanup_incomplete" && record.nativeCleanupDirty !== true);
      status = expired ? "expired" : "stopped";
      code = "ok";
      reportFrozen = true;
      finalSnapshot = snapshot();
      return finalSnapshot;
    })();
    return stopPromise;
  }

  return Object.freeze({ start, drain, stop: () => stop(false), cancel: () => stop(false), get status() { return snapshot(); } });
}

module.exports = Object.freeze({
  SCHEMA, PROOF_LEVEL, MAX_WINDOWS, MAX_EVENTS, MAX_EVENT_BYTES, MAX_REPORT_BYTES,
  MAX_DOM_WAIT_MS, MAX_LEASE_MS, exactAVRoute, isObserverReport, isOfficialAVStartReport,
  createOfficialAVObserver,
});
