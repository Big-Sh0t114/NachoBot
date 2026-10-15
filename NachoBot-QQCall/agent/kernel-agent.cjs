"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { nativeInviteEvidenceFromCallback } = require("../shared/private-invite-scope.cjs");

const KNOWN_LISTENER_METHODS = Object.freeze([
  "onS2CActionToAVSDK",
  "onActionToAVSDK",
  "onAVSDKData",
  "onAVSdkCrash",
  "onReceiveInvite",
  "onInviteActionToAVSDK",
  "onGroupVideoActionToAVSDK",
  "onGroupVideoServerPushToAVSDK",
]);

const CALLBACK_DIAGNOSTIC_METHODS = Object.freeze([
  ...KNOWN_LISTENER_METHODS,
  "onGroupAudioMemNumChange",
  "onGroupVideoMemNumPushInfo",
  "onGroupVideoInviteMemberUpdate",
]);

const MAX_CALLBACK_DIAGNOSTIC_COUNT = 1_000_000;
const MAX_CALLBACK_DIAGNOSTIC_LENGTH = 1_000_000;
const MAX_CALLBACK_DIAGNOSTIC_ARGUMENTS = 8;
const KNOWN_CALLBACK_BY_LOWERCASE = new Map(CALLBACK_DIAGNOSTIC_METHODS.map((name) => [name.toLowerCase(), name]));
const TYPED_ARRAY_BYTE_LENGTH_GETTER = Object.getOwnPropertyDescriptor(
  Object.getPrototypeOf(Uint8Array.prototype), "byteLength",
)?.get;
const DATA_VIEW_BYTE_LENGTH_GETTER = Object.getOwnPropertyDescriptor(DataView.prototype, "byteLength")?.get;
const ARRAY_BUFFER_BYTE_LENGTH_GETTER = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, "byteLength")?.get;
const SHARED_ARRAY_BUFFER_BYTE_LENGTH_GETTER = typeof SharedArrayBuffer === "function"
  ? Object.getOwnPropertyDescriptor(SharedArrayBuffer.prototype, "byteLength")?.get
  : null;

function incrementDiagnosticCount(value) {
  return value >= MAX_CALLBACK_DIAGNOSTIC_COUNT ? MAX_CALLBACK_DIAGNOSTIC_COUNT : value + 1;
}

function boundedDiagnosticLength(value) {
  return Number.isSafeInteger(value) && value >= 0
    ? { length: Math.min(value, MAX_CALLBACK_DIAGNOSTIC_LENGTH), lengthCapped: value > MAX_CALLBACK_DIAGNOSTIC_LENGTH }
    : {};
}

function binaryPayloadLength(value) {
  if (ArrayBuffer.isView(value)) {
    for (const getter of [TYPED_ARRAY_BYTE_LENGTH_GETTER, DATA_VIEW_BYTE_LENGTH_GETTER]) {
      if (typeof getter !== "function") continue;
      try { return getter.call(value); }
      catch { /* Try the other native view brand; no payload properties are read. */ }
    }
    return null;
  }
  for (const getter of [ARRAY_BUFFER_BYTE_LENGTH_GETTER, SHARED_ARRAY_BUFFER_BYTE_LENGTH_GETTER]) {
    if (typeof getter !== "function") continue;
    try { return getter.call(value); }
    catch { /* Not this native binary brand. */ }
  }
  return null;
}

function summarizeKernelArgument(value) {
  try {
    if (value === null) return { type: "null" };
    if (value === undefined) return { type: "undefined" };
    if (typeof value === "string") return { type: "string", ...boundedDiagnosticLength(value.length) };
    if (typeof value === "number") return { type: "number" };
    if (typeof value === "boolean") return { type: "boolean" };
    if (typeof value === "bigint") return { type: "bigint" };
    if (typeof value === "symbol") return { type: "symbol" };
    if (typeof value === "function") return { type: "function" };
    if (typeof value !== "object") return { type: "other" };

    const byteLength = binaryPayloadLength(value);
    if (byteLength !== null) return { type: "binary", ...boundedDiagnosticLength(byteLength) };
    if (Array.isArray(value)) {
      const length = safeReadDescriptor(value, "length");
      return { type: "array", ...(Number.isSafeInteger(length) ? boundedDiagnosticLength(length) : {}) };
    }
    return { type: "object" };
  } catch {
    // Diagnostics must not interfere with the existing native-event forwarding path.
    return { type: "unavailable" };
  }
}

function safeReadDescriptor(object, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && "value" in descriptor ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function ownFieldKind(object, key) {
  try {
    if (object === null || (typeof object !== "object" && typeof object !== "function")) return "absent";
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    if (!descriptor) return "absent";
    return "value" in descriptor ? summarizeKernelArgument(descriptor.value).type : "accessor";
  } catch {
    return "unavailable";
  }
}

function summarizeMembershipShape(argument) {
  const info = safeReadDescriptor(argument, "notifyInfo");
  const shape = { argumentKind: summarizeKernelArgument(argument).type, notifyInfoKind: ownFieldKind(argument, "notifyInfo") };
  for (const [label, key] of [
    ["GroupCode", "group_code"], ["CamelGroupCode", "groupCode"], ["OnlineCount", "onlineCount"],
    ["MemberUins", "memberUins"], ["BusinessType", "businessType"], ["NativeTimeout", "isNativeTimeout"],
  ]) {
    shape[`root${label}Kind`] = ownFieldKind(argument, key);
    shape[`notify${label}Kind`] = ownFieldKind(info, key);
  }
  return shape;
}

function summarizeKernelArguments(args) {
  const argumentCount = Math.min(args.length, MAX_CALLBACK_DIAGNOSTIC_COUNT);
  const descriptors = [];
  for (let index = 0; index < Math.min(args.length, MAX_CALLBACK_DIAGNOSTIC_ARGUMENTS); index += 1) {
    descriptors.push(summarizeKernelArgument(args[index]));
  }
  const omittedArgumentCount = args.length - descriptors.length;
  return {
    type: "array",
    length: argumentCount,
    lengthCapped: args.length > MAX_CALLBACK_DIAGNOSTIC_COUNT,
    items: descriptors,
    omittedArgumentCount: Math.min(omittedArgumentCount, MAX_CALLBACK_DIAGNOSTIC_COUNT),
    omittedArgumentCountCapped: omittedArgumentCount > MAX_CALLBACK_DIAGNOSTIC_COUNT,
  };
}

function safeRead(object, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && "value" in descriptor ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function summarizePayload(value, depth = 0, seen = new WeakSet()) {
  if (value === null) return { type: "null" };
  if (value === undefined) return { type: "undefined" };
  if (Buffer.isBuffer(value) || value instanceof Uint8Array) return { type: "binary", length: value.byteLength };
  if (typeof value === "string") return { type: "string", length: value.length };
  if (typeof value === "number") return { type: "number" };
  if (typeof value === "boolean") return { type: "boolean" };
  if (typeof value === "bigint") return { type: "bigint" };
  if (typeof value === "function") return { type: "function" };
  if (typeof value !== "object") return { type: typeof value };
  if (depth >= 3) return { type: Array.isArray(value) ? "array" : "object" };
  if (seen.has(value)) return { type: "circular" };
  seen.add(value);
  if (Array.isArray(value)) {
    return { type: "array", length: value.length, items: value.slice(0, 12).map((item) => summarizePayload(item, depth + 1, seen)) };
  }
  let keys;
  try { keys = Object.keys(value).slice(0, 24); }
  catch { return { type: "object", unreadable: true }; }
  return { type: "object", keys: keys.map((key) => ({ key, value: summarizePayload(safeRead(value, key), depth + 1, seen) })) };
}

function serializePrivatePayload(value, seen = new WeakSet(), depth = 0) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (value === undefined) return { $qqcall: "undefined" };
  if (typeof value === "number") return Number.isFinite(value) ? value : { $qqcall: "number", value: String(value) };
  if (typeof value === "bigint") return { $qqcall: "bigint", value: value.toString(10) };
  if (typeof value === "function") return { $qqcall: "function" };
  if (Buffer.isBuffer(value)) return { $qqcall: "binary", encoding: "base64", length: value.byteLength, data: value.toString("base64") };
  if (value instanceof Uint8Array) {
    const bytes = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    return { $qqcall: "binary", encoding: "base64", length: bytes.byteLength, data: bytes.toString("base64") };
  }
  if (value instanceof ArrayBuffer) {
    const bytes = Buffer.from(value);
    return { $qqcall: "binary", encoding: "base64", length: bytes.byteLength, data: bytes.toString("base64") };
  }
  if (typeof value !== "object") return { $qqcall: typeof value };
  if (depth >= 24) return { $qqcall: "depth_limit" };
  if (seen.has(value)) return { $qqcall: "circular" };
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => serializePrivatePayload(item, seen, depth + 1));

  const result = Object.create(null);
  let keys;
  try { keys = Object.keys(value); }
  catch { return { $qqcall: "unreadable_object" }; }
  for (const key of keys) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    result[key] = descriptor && "value" in descriptor
      ? serializePrivatePayload(descriptor.value, seen, depth + 1)
      : { $qqcall: "accessor" };
  }
  return result;
}

function mapKernelAction(method, args) {
  const normalized = String(method).toLowerCase();
  let actionType;
  let payload;
  if (normalized === "oninviteactiontoavsdk") {
    actionType = args[1];
    payload = args[2];
  } else if (normalized === "onactiontoavsdk") {
    actionType = args[0];
    payload = args[1];
  } else {
    return null;
  }
  return typeof actionType === "number" && typeof payload === "string" ? { actionType, payload } : null;
}

function canonicalUinToNumber(uin) {
  if (typeof uin !== "string" || !/^[1-9][0-9]{0,19}$/.test(uin)) return null;
  const numeric = Number(uin);
  return Number.isSafeInteger(numeric) && numeric > 0 ? numeric : null;
}

function validAbsoluteWindowsPath(value, { requireExistingDirectory = false } = {}) {
  if (typeof value !== "string" || value.length < 1 || value.length > 32768 || value.includes("\0") ||
      !path.win32.isAbsolute(value) || path.win32.parse(value).root.length < 2) return false;
  if (!requireExistingDirectory) return true;
  try { return fs.statSync(value).isDirectory(); }
  catch { return false; }
}

function createKernelAgent({ wrapper, sessionName = "nt_1", identity, emit = () => {}, connectionGeneration = 1, removalMode = "returned-id", nativeInviteScopeProfile = null } = {}) {
  let session = null;
  let avsdkService = null;
  let listener = null;
  let listenerId;
  let hasListenerId = false;
  let probed = false;
  let readinessCode = null;
  let state = "unprobed";
  let lastFailureCode = null;
  let lastKernelEvent = null;
  let lastOutput = null;
  let kernelEventCount = 0;
  let outputCount = 0;
  let forwardCount = 0;
  let unknownCallbackCount = 0;
  const callbackDiagnostics = Object.create(null);
  for (const method of CALLBACK_DIAGNOSTIC_METHODS) {
    callbackDiagnostics[method] = {
      count: 0,
      mappedCount: 0,
      unmappedCount: 0,
      lastActionMapped: null,
      lastArgumentCount: 0,
      lastArguments: [],
      lastMembershipShape: null,
      omittedArgumentCount: 0,
      omittedArgumentCountCapped: false,
    };
  }
  let nativeInviteCallbackSequence = 0;
  let listenerRegistrationSequence = 0;
  let accountPathAvailable = false;
  let sessionAccessorAvailable = false;
  let getServiceAvailable = false;
  let addListenerAvailable = false;
  let removeListenerAvailable = false;
  let setActionAvailable = false;
  let sessionFound = false;
  let serviceAvailable = false;
  let listenerRegistered = false;
  let listenerRemovalAvailable = false;
  const listenerRemovalMode = removalMode === "listener-object" || removalMode === "returned-id" ? removalMode : "unverified";
  const identityPresent = identity !== undefined && identity !== null;
  let operationTail = Promise.resolve();

  function enqueueOperation(operation) {
    const result = operationTail.then(operation, operation);
    operationTail = result.catch(() => {});
    return result;
  }

  function fail(code, nextState = state) {
    lastFailureCode = code;
    state = nextState;
    return { ok: false, code, status: status() };
  }

  function probeFailure(code) {
    readinessCode = code;
    return fail(code, "unavailable");
  }

  function nativeNullState(value) {
    let check;
    try { check = value?.isNull; }
    catch { return "error"; }
    if (typeof check !== "function") return "available";
    try { return check.call(value) === true ? "null" : "available"; }
    catch { return "error"; }
  }

  async function lookupExistingSession() {
    if (session) {
      const currentNullState = nativeNullState(session);
      if (currentNullState === "null") return { ok: false, code: "session_native_null" };
      if (currentNullState === "error") return { ok: false, code: "session_is_null_check_failed" };
      sessionFound = true;
      return { ok: true, session };
    }
    let sessionApi;
    try { sessionApi = wrapper?.NodeIQQNTWrapperSession; }
    catch { return { ok: false, code: "wrapper_accessor_unreadable" }; }
    sessionAccessorAvailable = typeof sessionApi?.getNTWrapperSession === "function";
    if (!sessionAccessorAvailable) return { ok: false, code: "wrapper_session_accessor_missing" };
    try {
      // Retrieve an existing session only. Never create, initialize, or log in.
      session = await sessionApi.getNTWrapperSession(sessionName);
    } catch {
      return { ok: false, code: "session_lookup_failed" };
    }
    if (!session) return { ok: false, code: "session_not_found" };
    const sessionNullState = nativeNullState(session);
    if (sessionNullState === "null") return { ok: false, code: "session_native_null" };
    if (sessionNullState === "error") return { ok: false, code: "session_is_null_check_failed" };
    sessionFound = true;
    return { ok: true, session };
  }

  function publish(event) {
    try {
      const result = emit(event);
      if (result && typeof result.then === "function") {
        result.catch(() => { lastFailureCode = "event_emit_failed"; });
      }
    } catch {
      lastFailureCode = "event_emit_failed";
    }
  }

  function makeListenerCallback(method, currentListenerRegistrationSequence) {
    return (...args) => {
      kernelEventCount += 1;
      const action = mapKernelAction(method, args);
      const safeMethod = typeof method === "string" && method.length <= 128
        ? KNOWN_CALLBACK_BY_LOWERCASE.get(method.toLowerCase()) || null
        : null;
      const argumentSummary = summarizeKernelArguments(args);
      if (safeMethod) {
        const diagnostics = callbackDiagnostics[safeMethod];
        diagnostics.count = incrementDiagnosticCount(diagnostics.count);
        diagnostics.lastActionMapped = Boolean(action);
        if (action) diagnostics.mappedCount = incrementDiagnosticCount(diagnostics.mappedCount);
        else diagnostics.unmappedCount = incrementDiagnosticCount(diagnostics.unmappedCount);
        diagnostics.lastArgumentCount = argumentSummary.length;
        diagnostics.lastArguments = argumentSummary.items.map((argument) => ({ ...argument }));
        if (safeMethod === "onGroupAudioMemNumChange" || safeMethod === "onGroupVideoMemNumPushInfo") {
          diagnostics.lastMembershipShape = summarizeMembershipShape(args[0]);
        }
        diagnostics.omittedArgumentCount = argumentSummary.omittedArgumentCount;
        diagnostics.omittedArgumentCountCapped = argumentSummary.omittedArgumentCountCapped;
      } else {
        unknownCallbackCount = incrementDiagnosticCount(unknownCallbackCount);
      }
      let nativeInviteEvidence = null;
      if (typeof method === "string" && method.toLowerCase() === "oninviteactiontoavsdk") {
        nativeInviteCallbackSequence += 1;
        nativeInviteEvidence = nativeInviteEvidenceFromCallback(
          nativeInviteScopeProfile,
          method,
          args,
          nativeInviteCallbackSequence,
          currentListenerRegistrationSequence,
        );
      }
      lastKernelEvent = { method, argumentCount: args.length, arguments: summarizePayload(args), actionMapped: Boolean(action) };
      try {
        const event = {
          kind: "kernel_event",
          method,
          args: serializePrivatePayload(args),
          action: action ? { command: 55, value: serializePrivatePayload([action.actionType, action.payload]) } : null,
          connectionGeneration,
        };
        if (nativeInviteEvidence) event.nativeInviteEvidence = nativeInviteEvidence;
        publish(event);
      } catch {
        lastFailureCode = "event_serialization_failed";
      }
    };
  }

  function makeListener(currentListenerRegistrationSequence) {
    const target = {};
    for (const method of KNOWN_LISTENER_METHODS) {
      target[method] = makeListenerCallback(method, currentListenerRegistrationSequence);
    }
    return new Proxy(target, {
      get(object, property, receiver) {
        if (Reflect.has(object, property)) return Reflect.get(object, property, receiver);
        if (typeof property === "string") {
          const callback = makeListenerCallback(property, currentListenerRegistrationSequence);
          Reflect.set(object, property, callback);
          return callback;
        }
        return Reflect.get(object, property, receiver);
      },
    });
  }

  async function probeInternal() {
    if (probed) return { ok: readinessCode === null, code: readinessCode, status: status() };
    if (listenerRegistered) return { ok: true, status: status() };
    state = "probing";
    lastFailureCode = null;
    const existingSession = await lookupExistingSession();
    if (!existingSession.ok) return probeFailure(existingSession.code);

    getServiceAvailable = typeof session.getAVSDKService === "function";
    if (!getServiceAvailable) return probeFailure("session_avsdk_accessor_missing");
    try { avsdkService = await session.getAVSDKService(); }
    catch { return probeFailure("avsdk_service_lookup_failed"); }
    if (!avsdkService) return probeFailure("avsdk_service_unavailable");
    const serviceNullState = nativeNullState(avsdkService);
    if (serviceNullState === "null") return probeFailure("service_native_null");
    if (serviceNullState === "error") return probeFailure("service_is_null_check_failed");
    serviceAvailable = true;

    addListenerAvailable = typeof avsdkService.addKernelAVSDKListener === "function";
    removeListenerAvailable = typeof avsdkService.removeKernelAVSDKListener === "function";
    setActionAvailable = typeof avsdkService.setActionFromAVSDK === "function";
    probed = true;
    if (!addListenerAvailable) return probeFailure("listener_add_unavailable");
    if (!removeListenerAvailable) return probeFailure("listener_remove_unavailable");
    if (!setActionAvailable) return probeFailure("set_action_from_avsdk_unavailable");
    state = "ready";
    readinessCode = null;
    lastFailureCode = null;
    return { ok: true, readyForAttach: listenerRemovalMode !== "unverified", status: status() };
  }

  async function attachInternal() {
    if (listenerRegistered) return { ok: true, status: status() };
    const readiness = await probeInternal();
    if (!readiness.ok) return readiness;
    if (listenerRemovalMode === "unverified") return fail("listener_removal_contract_unverified", "ready");
    if (!Number.isSafeInteger(listenerRegistrationSequence + 1)) {
      return fail("listener_registration_sequence_exhausted", "ready");
    }
    listenerRegistrationSequence += 1;
    listener = makeListener(listenerRegistrationSequence);
    try { listenerId = await avsdkService.addKernelAVSDKListener(listener); }
    catch {
      listener = null;
      return fail("listener_registration_failed", "ready");
    }
    hasListenerId = listenerId !== null && listenerId !== undefined;
    listenerRegistered = true;
    listenerRemovalAvailable = removeListenerAvailable && (listenerRemovalMode === "listener-object" || hasListenerId);
    if (!listenerRemovalAvailable) {
      return fail("listener_id_unavailable", "attached_without_removal_handle");
    }
    state = "attached";
    lastFailureCode = null;
    return { ok: true, status: status() };
  }

  async function forwardAVSDKOutputInternal({ command, value, generation } = {}) {
    outputCount += 1;
    lastOutput = { command: Number.isInteger(command) ? command : null, value: summarizePayload(value) };
    if (generation !== undefined && generation !== connectionGeneration) return fail("stale_generation");
    if (!listenerRegistered || !avsdkService) return fail("agent_not_attached");
    if (command !== 20001) return fail("unsupported_avsdk_output_command");
    if (!Array.isArray(value) || typeof value[0] !== "number" || typeof value[1] !== "string") return fail("invalid_20001_output");
    if (!setActionAvailable || typeof avsdkService.setActionFromAVSDK !== "function") return fail("set_action_from_avsdk_unavailable");
    try {
      // Verified inverse mapping: setActionFromAVSDK(actionType, payload).
      await avsdkService.setActionFromAVSDK(value[0], value[1]);
      forwardCount += 1;
    } catch {
      return fail("set_action_from_avsdk_failed");
    }
    lastFailureCode = null;
    return { ok: true, forwarded: true, status: status() };
  }

  async function getAccountPathInternal({ uin, generation } = {}) {
    if (generation !== undefined && generation !== connectionGeneration) {
      return { ok: false, code: "stale_generation" };
    }
    const numericUin = canonicalUinToNumber(uin);
    if (numericUin === null) return { ok: false, code: "invalid_account_uin" };

    const existingSession = await lookupExistingSession();
    if (!existingSession.ok) return { ok: false, code: existingSession.code };
    let getter;
    try { getter = session.getAccountPath; }
    catch { return { ok: false, code: "account_path_accessor_unreadable" }; }
    if (typeof getter === "function") {
      let accountPath;
      try { accountPath = await getter.call(session, numericUin); }
      catch { return { ok: false, code: "account_path_lookup_failed" }; }
      if (accountPath !== null && accountPath !== undefined && accountPath !== "") {
        if (!validAbsoluteWindowsPath(accountPath)) return { ok: false, code: "account_path_unavailable" };
        accountPathAvailable = true;
        return { ok: true, accountPath };
      }
    } else if (getter !== undefined) {
      return { ok: false, code: "account_path_accessor_unreadable" };
    }

    let wrapperUtil;
    try { wrapperUtil = wrapper?.NodeQQNTWrapperUtil; }
    catch { return { ok: false, code: "account_path_root_accessor_unreadable" }; }
    if (!wrapperUtil || (typeof wrapperUtil !== "object" && typeof wrapperUtil !== "function")) {
      return { ok: false, code: "account_path_root_accessor_missing" };
    }
    let rootGetter;
    try { rootGetter = wrapperUtil.getNTUserDataInfoConfig; }
    catch { return { ok: false, code: "account_path_root_accessor_unreadable" }; }
    if (typeof rootGetter !== "function") return { ok: false, code: "account_path_root_accessor_missing" };
    let rootPath;
    try { rootPath = rootGetter.call(wrapperUtil); }
    catch { return { ok: false, code: "account_path_root_lookup_failed" }; }
    if (!validAbsoluteWindowsPath(rootPath, { requireExistingDirectory: true })) {
      return { ok: false, code: "account_path_root_invalid" };
    }
    accountPathAvailable = true;
    return { ok: true, accountPath: rootPath };
  }

  async function detachInternal() {
    if (!listenerRegistered) {
      state = probed ? "ready" : "detached";
      return { ok: true, status: status() };
    }
    if (!listenerRemovalAvailable || !avsdkService || (listenerRemovalMode === "returned-id" && !hasListenerId) || !listener) {
      return fail("listener_removal_handle_unavailable", "attached_without_removal_handle");
    }
    state = "detaching";
    try {
      // The exact pinned wrapper build consumes the original listener object;
      // legacy isolated contracts can still remove by a returned listenerId.
      await avsdkService.removeKernelAVSDKListener(listenerRemovalMode === "listener-object" ? listener : listenerId);
    } catch {
      return fail("listener_removal_failed", "attached");
    }
    listenerRegistered = false;
    listenerRemovalAvailable = false;
    listener = null;
    listenerId = undefined;
    hasListenerId = false;
    // Keep the verified service/session references so a later explicit attach
    // can reuse this existing session without another lookup or initialization.
    state = "ready";
    lastFailureCode = null;
    return { ok: true, status: status() };
  }

  function status() {
    const safeCallbackDiagnostics = Object.create(null);
    for (const method of CALLBACK_DIAGNOSTIC_METHODS) {
      const current = callbackDiagnostics[method];
      safeCallbackDiagnostics[method] = {
        count: current.count,
        mappedCount: current.mappedCount,
        unmappedCount: current.unmappedCount,
        lastActionMapped: current.lastActionMapped,
        lastArgumentCount: current.lastArgumentCount,
        lastArguments: current.lastArguments.map((argument) => ({ ...argument })),
        lastMembershipShape: current.lastMembershipShape ? { ...current.lastMembershipShape } : null,
        omittedArgumentCount: current.omittedArgumentCount,
        omittedArgumentCountCapped: current.omittedArgumentCountCapped,
      };
    }
    return {
      state,
      connectionGeneration,
      sessionNameConfigured: typeof sessionName === "string" && sessionName.length > 0,
      identityPresent,
      sessionFound,
      serviceAvailable,
      listenerRegistered,
      listenerRegistrationSequence,
      listenerIdAvailable: hasListenerId,
      listenerRemovalMode,
      accountPathAvailable,
      capabilities: {
        sessionAccessor: sessionAccessorAvailable,
        getAVSDKService: getServiceAvailable,
        addKernelAVSDKListener: addListenerAvailable,
        removeKernelAVSDKListener: removeListenerAvailable,
        removeHandleAvailable: listenerRemovalAvailable,
        listenerRemovalContractVerified: listenerRemovalMode !== "unverified",
        setActionFromAVSDK: setActionAvailable,
        forward20001: listenerRegistered && setActionAvailable,
      },
      counts: { kernelEvents: kernelEventCount, outputRequests: outputCount, forwarded20001: forwardCount },
      callbackDiagnostics: {
        unknownCallbackCount,
        knownCallbacks: safeCallbackDiagnostics,
      },
      lastKernelEvent,
      lastOutput,
      lastFailureCode,
    };
  }

  return Object.freeze({
    probe: () => enqueueOperation(probeInternal),
    attach: () => enqueueOperation(attachInternal),
    forwardAVSDKOutput: (output) => enqueueOperation(() => forwardAVSDKOutputInternal(output)),
    detach: () => enqueueOperation(detachInternal),
    getAccountPath: (query) => enqueueOperation(() => getAccountPathInternal(query)),
    status,
  });
}

module.exports = { canonicalUinToNumber, createKernelAgent, mapKernelAction, serializePrivatePayload, summarizePayload };
