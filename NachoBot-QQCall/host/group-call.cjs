"use strict";

const GROUP_SCENE = 3;
const GROUP_START_COMMAND = 4;
const GROUP_QUIT_COMMAND = 8;
const GROUP_ENTER_OUTPUT_COMMAND = 20004;
const GROUP_TERMINAL_OUTPUT_COMMAND = 20011;
const DEFAULT_START_TTL_MS = 10_000;
const MAX_START_TTL_MS = 10_000;
const MAX_JOIN_TTL_MS = 30_000;
const MAX_GROUP_ID_BYTES = 20;
const MAX_UID_BYTES = 128;
const MAX_ERROR_MESSAGE_BYTES = 1024;
const MAX_OPAQUE_ROOM_ID_BYTES = 256;
const MAX_ROOM_USERS = 1000;
const MAX_SDK_PROVIDER = 0x7fffffff;
const NATIVE_GROUP_SOURCE = "native_scene3_relation";
const QQ_UID_PATTERN = /^u_[A-Za-z0-9_-]{22}$/;
const START_KEYS = Object.freeze(["id", "hostGeneration", "groupId"]);
const QUIT_KEYS = Object.freeze(["id", "hostGeneration", "scope"]);
const SCOPE_KEYS = Object.freeze([
  "kind", "serviceInstance", "hostGeneration", "connectionGeneration",
  "groupId", "roomId", "roomGeneration",
]);
const QUERY_KEYS = Object.freeze([
  "ok", "hostGeneration", "id", "groupId", "diagnosticOnly", "nativeJoinVerified", "roomInfo",
]);
const ROOM_INFO_KEYS = Object.freeze(["sceneId", "relationId", "userNum", "userList"]);
const CONTEXT_KEYS = Object.freeze(["serviceInstance", "hostGeneration", "connectionGeneration"]);

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function isPositiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function isBoundedString(value, maxBytes, { nonEmpty = true, token = false } = {}) {
  if (typeof value !== "string" || (nonEmpty && value.length === 0)) return false;
  if (Buffer.byteLength(value, "utf8") > maxBytes) return false;
  return !token || (value.length === 0 && !nonEmpty) || /^[A-Za-z0-9._:-]+$/.test(value);
}

function isQqUid(value) {
  return typeof value === "string" && QQ_UID_PATTERN.test(value);
}

function isCanonicalGroupId(value) {
  return typeof value === "string" &&
    Buffer.byteLength(value, "utf8") <= MAX_GROUP_ID_BYTES &&
    /^[1-9][0-9]{0,19}$/.test(value);
}

function readPlainDataObject(value, expectedKeys, { allowExtra = false } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return null;

  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.some((key) => typeof key !== "string")) return null;
  if (!allowExtra) {
    if (ownKeys.length !== expectedKeys.length || expectedKeys.some((key) => !ownKeys.includes(key))) return null;
  } else if (expectedKeys.some((key) => !ownKeys.includes(key))) {
    return null;
  }

  const output = Object.create(null);
  for (const key of ownKeys) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || descriptor.enumerable !== true) {
      return null;
    }
    output[key] = descriptor.value;
  }
  return output;
}

function readDenseDataArray(value, expectedLength, { maximumLength = expectedLength } = {}) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
  const length = lengthDescriptor && lengthDescriptor.value;
  if (!Number.isSafeInteger(length) || length !== expectedLength || length > maximumLength) return null;

  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.length !== length + 1 || !ownKeys.includes("length")) return null;
  const output = [];
  for (let index = 0; index < length; index += 1) {
    const key = String(index);
    if (!ownKeys.includes(key)) return null;
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || descriptor.enumerable !== true) {
      return null;
    }
    output.push(descriptor.value);
  }
  return output;
}

function readBoundedDenseDataArray(value, maximumLength) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null;
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
  const length = lengthDescriptor && lengthDescriptor.value;
  if (!Number.isSafeInteger(length) || length < 0 || length > maximumLength) return null;
  return readDenseDataArray(value, length, { maximumLength });
}

function buildStartParams({ groupId, selfUid } = {}) {
  if (!isCanonicalGroupId(groupId)) throw codedError("invalid_group_id");
  if (!isQqUid(selfUid)) throw codedError("invalid_self_uid");
  const payload = {
    scene_id: GROUP_SCENE,
    self_uid: selfUid,
    invite_count: 0,
    invite_uids: [],
    relation_id: groupId,
    invite_original: 3,
    sub_business_type: 1,
    invite_reason: 0,
  };
  return Object.freeze([JSON.stringify(payload)]);
}

function parseExistingRoomQueryResult(value, { id, hostGeneration, groupId } = {}) {
  const body = readPlainDataObject(value, QUERY_KEYS);
  if (!body || body.ok !== true || body.hostGeneration !== hostGeneration || body.id !== id ||
      body.groupId !== groupId || body.diagnosticOnly !== true || body.nativeJoinVerified !== false) {
    return { valid: false, existing: false };
  }
  const roomInfo = readPlainDataObject(body.roomInfo, ROOM_INFO_KEYS);
  if (!roomInfo || roomInfo.sceneId !== GROUP_SCENE || roomInfo.relationId !== groupId ||
      !Number.isSafeInteger(roomInfo.userNum) || roomInfo.userNum < 0 || roomInfo.userNum > MAX_ROOM_USERS) {
    return { valid: false, existing: false };
  }
  const userList = readBoundedDenseDataArray(roomInfo.userList, MAX_ROOM_USERS);
  if (!Array.isArray(userList) || userList.length > MAX_ROOM_USERS) return { valid: false, existing: false };
  const seenUids = new Set();
  for (const user of userList) {
    const row = readPlainDataObject(user, ["uid", "accountType"]);
    if (!row || !isBoundedString(row.uid, MAX_UID_BYTES, { token: true }) ||
        !Number.isSafeInteger(row.accountType) || row.accountType < 0 || row.accountType > 65_535 ||
        seenUids.has(row.uid)) return { valid: false, existing: false };
    seenUids.add(row.uid);
  }
  return {
    valid: true,
    existing: roomInfo.userNum > 0 && userList.length > 0,
  };
}

function parseGroupEnterCallback(value, expectedGroupId) {
  const fields = readDenseDataArray(value, 7);
  if (!fields) return { matched: false, unexpectedCreator: false };
  const [ret, sceneId, relationId, peerUid, isCreator, errorMessage, sdkProvider] = fields;
  if (ret !== 0 || sceneId !== GROUP_SCENE || relationId !== expectedGroupId) {
    return { matched: false, unexpectedCreator: false };
  }
  if (isCreator === true || isCreator === 1) {
    return { matched: false, unexpectedCreator: true, relationId };
  }
  if (isCreator !== false && isCreator !== 0) return { matched: false, unexpectedCreator: false };
  if (!isBoundedString(peerUid, MAX_UID_BYTES, { nonEmpty: false }) ||
      !isBoundedString(errorMessage, MAX_ERROR_MESSAGE_BYTES, { nonEmpty: false }) ||
      !Number.isSafeInteger(sdkProvider) || sdkProvider < 0 || sdkProvider > MAX_SDK_PROVIDER) {
    return { matched: false, unexpectedCreator: false };
  }
  return { matched: true, relationId, unexpectedCreator: false };
}

function parseGroupTerminalCallback(value, expectedRelationId) {
  const fields = readDenseDataArray(value, 3);
  if (!fields) return { matched: false };
  const [sceneId, relationId, opaqueRoomId] = fields;
  if (sceneId !== GROUP_SCENE || relationId !== expectedRelationId ||
      !isBoundedString(opaqueRoomId, MAX_OPAQUE_ROOM_ID_BYTES)) {
    return { matched: false };
  }
  return { matched: true, sceneId, relationId };
}

function cloneScope(scope) {
  return Object.freeze({
    kind: scope.kind,
    serviceInstance: scope.serviceInstance,
    hostGeneration: scope.hostGeneration,
    connectionGeneration: scope.connectionGeneration,
    groupId: scope.groupId,
    roomId: scope.roomId,
    roomGeneration: scope.roomGeneration,
  });
}

function scopeMatches(left, right) {
  return Boolean(left && right && SCOPE_KEYS.every((key) => left[key] === right[key]));
}

function createGroupCallDriver(options = {}) {
  const {
    hostGeneration,
    connectionGeneration,
    serviceInstance,
    selfUid,
    queryRoomInfo,
    sendCommand,
    allocateRequestId: allocateRequestIdFromContext,
    slotFree,
    currentContext,
    confirmNoMembership,
    confirmRemoteNoMembership,
    startTtlMs = DEFAULT_START_TTL_MS,
    joinTtlMs = startTtlMs,
    clock = Date.now,
    schedule = setTimeout,
    cancel = clearTimeout,
    onStatus = () => {},
  } = options;

  if (!isPositiveSafeInteger(hostGeneration)) throw codedError("invalid_host_generation");
  if (!isPositiveSafeInteger(connectionGeneration)) throw codedError("invalid_connection_generation");
  if (!isBoundedString(serviceInstance, 128, { token: true })) throw codedError("invalid_service_instance");
  if (!isQqUid(selfUid)) throw codedError("invalid_self_uid");
  if (typeof queryRoomInfo !== "function") throw codedError("group_room_query_required");
  if (typeof sendCommand !== "function") throw codedError("group_command_sender_required");
  if (allocateRequestIdFromContext !== undefined && typeof allocateRequestIdFromContext !== "function") {
    throw codedError("group_request_id_allocator_invalid");
  }
  if (typeof slotFree !== "function") throw codedError("group_slot_check_required");
  if (typeof currentContext !== "function") throw codedError("group_context_check_required");
  if (confirmNoMembership !== undefined && typeof confirmNoMembership !== "function") {
    throw codedError("membership_confirmation_invalid");
  }
  if (confirmRemoteNoMembership !== undefined && typeof confirmRemoteNoMembership !== "function") {
    throw codedError("remote_membership_confirmation_invalid");
  }
  if (!Number.isSafeInteger(startTtlMs) || startTtlMs < 1 || startTtlMs > MAX_START_TTL_MS) {
    throw codedError("group_start_ttl_invalid");
  }
  if (!Number.isSafeInteger(joinTtlMs) || joinTtlMs < 1 || joinTtlMs > MAX_JOIN_TTL_MS) {
    throw codedError("group_join_ttl_invalid");
  }
  if (typeof clock !== "function" || typeof schedule !== "function" || typeof cancel !== "function" ||
      typeof onStatus !== "function") throw codedError("group_runtime_dependencies_invalid");

  const context = Object.freeze({ serviceInstance, hostGeneration, connectionGeneration });
  let stopped = false;
  let slot = null;
  let roomGeneration = 0;
  let terminalSequence = 0;
  let generatedId = 0;
  let lastStatusCode = "idle";

  function readNow() {
    const now = clock();
    if (typeof now !== "number" || !Number.isFinite(now)) throw codedError("group_clock_invalid");
    return now;
  }

  function emit(state, code) {
    lastStatusCode = code;
    const record = Object.freeze({
      state,
      code,
      hostGeneration,
      connectionGeneration,
      roomGeneration,
      terminalSequence,
    });
    try { onStatus(record); } catch { /* Status observers cannot affect call ownership. */ }
  }

  function contextIsCurrent() {
    try { return currentContext(context) === true; }
    catch { return false; }
  }

  function otherCallSlotIsFree() {
    try { return slotFree(context) === true; }
    catch { return false; }
  }

  function clearTimer(active) {
    if (!active || active.timer === null) return;
    const timer = active.timer;
    active.timer = null;
    try { cancel(timer); } catch { /* Timer cancellation failure keeps the slot held. */ }
  }

  function finishStart(active, result) {
    if (!active || active.startResolved) return;
    active.startResolved = true;
    active.resolveStart(Object.freeze(result));
  }

  function finishQuit(active, result) {
    if (!active || active.quitResolved || typeof active.resolveQuit !== "function") return;
    active.quitResolved = true;
    active.resolveQuit(Object.freeze(result));
  }

  function safeFailure(code, state = slot ? slot.state : "idle") {
    return Object.freeze({ ok: false, code, state });
  }

  function owned(active) {
    return Boolean(active && slot === active && active.owner === ownerToken && !stopped);
  }

  function release(active, code, resultCode = code) {
    if (!active || slot !== active) return;
    clearTimer(active);
    active.state = "released";
    slot = null;
    finishStart(active, safeFailure(resultCode, "idle"));
    finishQuit(active, safeFailure(resultCode, "idle"));
    emit("idle", code);
  }

  function quarantine(active, code) {
    if (!active || slot !== active) return;
    clearTimer(active);
    active.state = "quarantined";
    active.quarantineCode = code;
    finishStart(active, safeFailure(code, "quarantined"));
    finishQuit(active, safeFailure(code, "quarantined"));
    emit("quarantined", code);
  }

  function armDeadline(active) {
    clearTimer(active);
    // Joining can include cold AVSDK network setup. Keep that bounded budget
    // separate from quit/membership confirmation and from audio proof expiry.
    active.deadline = readNow() + (active.state === "preflight" ? joinTtlMs : startTtlMs);
    const arm = () => {
      if (!owned(active) || active.timer !== null) return;
      const remaining = Math.max(0, active.deadline - readNow());
      active.timer = schedule(() => {
        active.timer = null;
        if (slot !== active) return;
        let now;
        try { now = readNow(); }
        catch { quarantine(active, "clock_unavailable"); return; }
        if (now < active.deadline) { arm(); return; }
        if (active.remoteTerminal || active.membershipCheckPending) {
          quarantine(active, "membership_confirmation_timeout");
        } else if (active.startCommandAttempted || active.quitRequested) {
          quarantine(active, active.quitRequested ? "quit_timeout" : "start_timeout");
        } else {
          release(active, "preflight_timeout", "preflight_timeout");
        }
      }, remaining);
      active.timer?.unref?.();
    };
    arm();
  }

  function expireIfDue(active) {
    if (!active || slot !== active || active.timer === null) return false;
    try {
      if (readNow() < active.deadline) return false;
    } catch {
      quarantine(active, "clock_unavailable");
      return true;
    }
    clearTimer(active);
    if (active.remoteTerminal || active.membershipCheckPending) {
      quarantine(active, "membership_confirmation_timeout");
    } else if (active.startCommandAttempted || active.quitRequested) {
      quarantine(active, active.quitRequested ? "quit_timeout" : "start_timeout");
    } else {
      release(active, "preflight_timeout", "preflight_timeout");
    }
    return true;
  }

  function nextGeneratedId(excludedId) {
    if (allocateRequestIdFromContext !== undefined) {
      for (let attempts = 0; attempts < 2; attempts += 1) {
        let id;
        try { id = allocateRequestIdFromContext(); }
        catch { throw codedError("group_request_id_unavailable"); }
        if (!isPositiveSafeInteger(id)) throw codedError("group_request_id_unavailable");
        if (id !== excludedId) return id;
      }
      throw codedError("group_request_id_unavailable");
    }
    for (let attempts = 0; attempts < 2; attempts += 1) {
      if (generatedId >= Number.MAX_SAFE_INTEGER) throw codedError("group_request_id_exhausted");
      generatedId += 1;
      if (generatedId !== excludedId) return generatedId;
    }
    throw codedError("group_request_id_exhausted");
  }

  function startReceiptKind(receipt, id, command) {
    const record = readPlainDataObject(receipt, ["id", "command", "posted"], { allowExtra: true });
    if (!record || record.id !== id || record.command !== command) return "unknown";
    if (record.posted === true) return "posted";
    if (record.posted === false) return "refused";
    return "unknown";
  }

  function startBody(request) {
    const body = readPlainDataObject(request, START_KEYS);
    if (!body) throw codedError("invalid_group_start_shape");
    if (!isPositiveSafeInteger(body.id)) throw codedError("invalid_request_id");
    if (!isPositiveSafeInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
      throw codedError("generation_mismatch");
    }
    if (!isCanonicalGroupId(body.groupId)) throw codedError("invalid_group_id");
    return { id: body.id, hostGeneration: body.hostGeneration, groupId: body.groupId };
  }

  function quitBody(request) {
    const body = readPlainDataObject(request, QUIT_KEYS);
    if (!body) throw codedError("invalid_group_quit_shape");
    if (!isPositiveSafeInteger(body.id)) throw codedError("invalid_request_id");
    if (!isPositiveSafeInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
      throw codedError("generation_mismatch");
    }
    const scope = readPlainDataObject(body.scope, SCOPE_KEYS);
    if (!scope || scope.kind !== "qqcall_group" || scope.serviceInstance !== serviceInstance ||
        scope.hostGeneration !== hostGeneration || scope.connectionGeneration !== connectionGeneration ||
        !isCanonicalGroupId(scope.groupId) || !isCanonicalGroupId(scope.roomId) ||
        !isPositiveSafeInteger(scope.roomGeneration)) throw codedError("invalid_group_scope");
    return { id: body.id, hostGeneration: body.hostGeneration, scope };
  }

  function contextAndSlotAreCurrent(active, { expire = true } = {}) {
    if (!owned(active)) return false;
    if (expire && expireIfDue(active)) return false;
    if (!contextIsCurrent()) {
      if (active.startCommandAttempted || active.quitRequested) quarantine(active, "stale_context");
      else release(active, "stale_context", "stale_context");
      return false;
    }
    return owned(active);
  }

  function publicSuccess(active) {
    return Object.freeze({
      ok: true,
      source: NATIVE_GROUP_SOURCE,
      scope: cloneScope(active.scope),
    });
  }

  function acceptEntry(active, relationId) {
    if (!contextAndSlotAreCurrent(active) ||
        !["posting_start", "awaiting_entry"].includes(active.state) || !active.startCommandAttempted) return false;
    if (!otherCallSlotIsFree()) {
      quarantine(active, "other_call_slot_occupied");
      return false;
    }
    if (roomGeneration >= Number.MAX_SAFE_INTEGER) {
      quarantine(active, "room_generation_exhausted");
      return false;
    }
    roomGeneration += 1;
    active.scope = Object.freeze({
      kind: "qqcall_group",
      serviceInstance,
      hostGeneration,
      connectionGeneration,
      groupId: active.groupId,
      roomId: relationId,
      roomGeneration,
    });
    active.state = "joined";
    clearTimer(active);
    finishStart(active, publicSuccess(active));
    emit("joined", "entry_confirmed");
    return true;
  }

  function autoQuitUnexpectedCreator(active, failureCode = "unexpected_creator") {
    if (!owned(active) || active.safeQuitAttempted) return;
    active.safeQuitAttempted = true;
    active.state = "quarantined";
    active.quarantineCode = failureCode;
    clearTimer(active);
    finishStart(active, safeFailure(failureCode, "quarantined"));
    emit("quarantined", failureCode);

    if (!contextIsCurrent() || !owned(active)) return;
    let id;
    try { id = nextGeneratedId(active.requestId); }
    catch { emit("quarantined", "safe_quit_id_unavailable"); return; }
    let pending;
    try {
      pending = sendCommand(Object.freeze({
        command: GROUP_QUIT_COMMAND,
        id,
        params: Object.freeze([GROUP_SCENE, 0]),
        hostGeneration,
      }));
    } catch {
      emit("quarantined", "safe_quit_outcome_unknown");
      return;
    }
    Promise.resolve(pending).then((receipt) => {
      if (!owned(active) || active.state !== "quarantined") return;
      if (!contextIsCurrent()) {
        emit("quarantined", "stale_context");
        return;
      }
      const kind = startReceiptKind(receipt, id, GROUP_QUIT_COMMAND);
      emit("quarantined", kind === "posted" ? "safe_quit_posted" : "safe_quit_outcome_unknown");
    }, () => {
      if (owned(active) && active.state === "quarantined") emit("quarantined", "safe_quit_outcome_unknown");
    });
  }

  async function driveStart(active) {
    let queryId;
    try { queryId = nextGeneratedId(active.requestId); }
    catch {
      release(active, "query_id_unavailable", "query_id_unavailable");
      return;
    }

    if (!contextAndSlotAreCurrent(active) || active.state !== "preflight") return;
    let roomResult;
    try {
      roomResult = await queryRoomInfo(Object.freeze({
        id: queryId,
        hostGeneration,
        groupId: active.groupId,
      }));
    } catch {
      if (slot === active && active.state === "preflight") {
        const code = !contextIsCurrent() ? "stale_context" :
          !otherCallSlotIsFree() ? "slot_occupied" : "room_query_failed";
        release(active, code, code);
      }
      return;
    }

    if (!contextAndSlotAreCurrent(active) || active.state !== "preflight") return;
    if (!otherCallSlotIsFree()) {
      release(active, "slot_occupied", "slot_occupied");
      return;
    }
    const checked = parseExistingRoomQueryResult(roomResult, {
      id: queryId,
      hostGeneration,
      groupId: active.groupId,
    });
    if (!checked.valid) {
      release(active, "room_query_invalid", "room_query_invalid");
      return;
    }
    if (!checked.existing) {
      release(active, "room_not_existing", "room_not_existing");
      return;
    }

    if (!contextAndSlotAreCurrent(active) || active.state !== "preflight") return;
    let params;
    try { params = buildStartParams({ groupId: active.groupId, selfUid }); }
    catch {
      release(active, "start_params_invalid", "start_params_invalid");
      return;
    }
    if (!contextAndSlotAreCurrent(active) || active.state !== "preflight") return;
    if (!otherCallSlotIsFree()) {
      release(active, "slot_occupied", "slot_occupied");
      return;
    }
    active.state = "posting_start";
    active.startCommandAttempted = true;
    emit("posting_start", "start_posting");

    let pending;
    try {
      pending = sendCommand(Object.freeze({
        command: GROUP_START_COMMAND,
        id: active.requestId,
        params,
        hostGeneration,
      }));
    } catch {
      quarantine(active, "start_outcome_unknown");
      return;
    }

    let receipt;
    try { receipt = await pending; }
    catch {
      if (slot === active) {
        quarantine(active, otherCallSlotIsFree() ? "start_outcome_unknown" : "other_call_slot_occupied");
      }
      return;
    }
    if (!otherCallSlotIsFree()) {
      quarantine(active, "other_call_slot_occupied");
      return;
    }
    if (!contextAndSlotAreCurrent(active) || active.state !== "posting_start") return;

    const kind = startReceiptKind(receipt, active.requestId, GROUP_START_COMMAND);
    if (kind === "refused") {
      if (active.entryCandidate) quarantine(active, "start_refusal_conflicts_with_callback");
      else release(active, "start_refused", "start_refused");
      return;
    }
    if (kind !== "posted") {
      quarantine(active, "start_outcome_unknown");
      return;
    }
    active.startPosted = true;
    active.state = "awaiting_entry";
    emit("awaiting_entry", "start_posted");
    if (active.entryCandidate) acceptEntry(active, active.entryCandidate.relationId);
  }

  async function maybeConfirmMembership(active) {
    const remoteEnd = active?.state === "ending_remote" && active.remoteTerminal;
    const ownedQuitEnd = active?.state === "ending" && active.quitCommandAttempted &&
      (active.terminalAfterQuit || (active.quitPosted && active.quitAcknowledged));
    if (!owned(active) || (!remoteEnd && !ownedQuitEnd) || active.membershipCheckPending || active.membershipCheckBlocked) return;
    if (!contextAndSlotAreCurrent(active)) return;
    const confirm = remoteEnd ? confirmRemoteNoMembership : confirmNoMembership;
    if (typeof confirm !== "function") {
      active.membershipCheckBlocked = true;
      quarantine(active, "membership_confirmation_unavailable");
      return;
    }

    const ticket = Object.freeze({
      scope: active.scope,
      terminalSequence: active.terminalSequence,
      state: active.state,
      remoteEnd,
      quitRequestId: ownedQuitEnd ? active.quitRequestId : null,
    });
    active.membershipCheckPending = true;
    active.membershipTicket = ticket;
    emit(ticket.state, "membership_confirmation_pending");
    let proof;
    try {
      const request = {
        scope: cloneScope(ticket.scope),
        terminalSequence: ticket.terminalSequence,
      };
      if (!ticket.remoteEnd) request.quitRequestId = ticket.quitRequestId;
      proof = await confirm(Object.freeze(request));
    } catch {
      if (slot === active && active.membershipTicket === ticket) quarantine(active, "membership_confirmation_failed");
      return;
    }

    // A bounded retry owns a new ticket. Late completion of the old check
    // must not quarantine or release the retry (or a subsequent room).
    if (active.membershipTicket !== ticket) return;
    if (slot !== active || active.owner !== ownerToken || stopped || active.state !== ticket.state ||
        active.scope !== ticket.scope ||
        (ticket.quitRequestId !== null && active.quitRequestId !== ticket.quitRequestId) ||
        active.remoteTerminal !== ticket.remoteEnd || active.terminalSequence !== ticket.terminalSequence ||
        terminalSequence !== ticket.terminalSequence || !contextIsCurrent()) {
      if (slot === active) quarantine(active, "membership_confirmation_stale");
      return;
    }
    if (proof !== true) {
      active.membershipCheckBlocked = true;
      quarantine(active, "membership_absence_unconfirmed");
      return;
    }
    active.membershipCheckPending = false;
    active.membershipCheckBlocked = true;
    finishQuit(active, Object.freeze({
      ok: true,
      state: "idle",
      ended: true,
      source: NATIVE_GROUP_SOURCE,
      scope: cloneScope(active.scope),
    }));
    release(active, "membership_absence_confirmed", "membership_absence_confirmed");
  }

  async function driveQuit(active, requestId) {
    if (!contextAndSlotAreCurrent(active) || active.state !== "ending") return;
    let pending;
    active.quitCommandAttempted = true;
    try {
      pending = sendCommand(Object.freeze({
        command: GROUP_QUIT_COMMAND,
        id: requestId,
        params: Object.freeze([GROUP_SCENE, 0]),
        hostGeneration,
      }));
    } catch {
      quarantine(active, "quit_outcome_unknown");
      return;
    }
    let receipt;
    try { receipt = await pending; }
    catch {
      if (slot === active) quarantine(active, "quit_outcome_unknown");
      return;
    }
    if (!contextAndSlotAreCurrent(active) || active.state !== "ending") return;
    const kind = startReceiptKind(receipt, requestId, GROUP_QUIT_COMMAND);
    if (kind === "refused") {
      active.quitRequested = false;
      active.quitRequestId = null;
      active.quitPosted = false;
      active.quitCommandAttempted = false;
      active.quitAcknowledged = false;
      active.terminalAfterQuit = false;
      active.terminalSequence = 0;
      active.state = "joined";
      clearTimer(active);
      finishQuit(active, safeFailure("quit_refused", "joined"));
      emit("joined", "quit_refused");
      return;
    }
    if (kind !== "posted") {
      quarantine(active, "quit_outcome_unknown");
      return;
    }
    // This is only the Host post receipt; it is not a native SDK ACK or terminal event.
    active.quitPosted = true;
    finishQuit(active, Object.freeze({
      ok: true,
      state: "ending",
      ended: false,
      source: NATIVE_GROUP_SOURCE,
      scope: cloneScope(active.scope),
    }));
    emit("ending", "quit_posted_waiting_terminal");
    await maybeConfirmMembership(active);
  }

  const ownerToken = Object.freeze({});

  function start(request) {
    const body = startBody(request);
    if (stopped) return Promise.resolve(safeFailure("host_stopped", "idle"));
    if (slot) return Promise.resolve(safeFailure("slot_occupied", slot.state));
    if (!contextIsCurrent()) return Promise.resolve(safeFailure("stale_context", "idle"));
    let free = false;
    try { free = slotFree(context) === true; } catch { free = false; }
    if (!free) return Promise.resolve(safeFailure("slot_occupied", "idle"));

    let resolveStart;
    const promise = new Promise((resolve) => { resolveStart = resolve; });
    const active = {
      owner: ownerToken,
      state: "preflight",
      requestId: body.id,
      groupId: body.groupId,
      startResolved: false,
      resolveStart,
      timer: null,
      deadline: 0,
      startCommandAttempted: false,
      startPosted: false,
      entryCandidate: null,
      scope: null,
      quitRequested: false,
      quitPosted: false,
      quitCommandAttempted: false,
      quitAcknowledged: false,
      quitRequestId: null,
      terminalAfterQuit: false,
      terminalSequence: 0,
      membershipCheckPending: false,
      membershipCheckBlocked: false,
      membershipTicket: null,
      remoteTerminal: false,
      safeQuitAttempted: false,
    };
    slot = active;
    try { armDeadline(active); }
    catch {
      release(active, "clock_unavailable", "clock_unavailable");
      return promise;
    }
    emit("preflight", "room_query_pending");
    void driveStart(active);
    return promise;
  }

  function quit(request) {
    const body = quitBody(request);
    const active = slot;
    const recoveryAllowed = active?.state === "quarantined" && [
      "membership_absence_unconfirmed", "membership_confirmation_failed",
      "membership_confirmation_timeout", "membership_confirmation_stale",
    ].includes(active.quarantineCode) && (active.remoteTerminal ||
      (active.quitCommandAttempted && (active.terminalAfterQuit || (active.quitPosted && active.quitAcknowledged))));
    if ((active?.state === "ending_remote" || recoveryAllowed) && active.scope && scopeMatches(body.scope, active.scope)) {
      if (!owned(active) || !contextIsCurrent() || !otherCallSlotIsFree()) {
        return Promise.resolve(safeFailure("stale_context", active.state));
      }
      if (!active.quitResolved && typeof active.resolveQuit === "function") return active.quitPromise;
      let resolveQuit;
      const promise = new Promise((resolve) => { resolveQuit = resolve; });
      active.quitPromise = promise;
      active.quitResolved = false;
      active.resolveQuit = resolveQuit;
      if (recoveryAllowed) {
        active.state = active.remoteTerminal ? "ending_remote" : "ending";
        active.membershipCheckPending = false;
        active.membershipCheckBlocked = false;
        active.membershipTicket = null;
        try { armDeadline(active); }
        catch { quarantine(active, "clock_unavailable"); return promise; }
      }
      // Retry confirmation only: the original terminal/ACK still fences it.
      // Reposting Quit against an already-destroyed room is unnecessary.
      void maybeConfirmMembership(active);
      return promise;
    }
    if (!active || active.state !== "joined" || !active.scope || !scopeMatches(body.scope, active.scope)) {
      return Promise.resolve(safeFailure("group_scope_mismatch", active ? active.state : "idle"));
    }
    if (stopped) return Promise.resolve(safeFailure("host_stopped", active.state));
    if (!contextIsCurrent()) {
      quarantine(active, "stale_context");
      return Promise.resolve(safeFailure("stale_context", "quarantined"));
    }

    let resolveQuit;
    const promise = new Promise((resolve) => { resolveQuit = resolve; });
    active.quitPromise = promise;
    active.quitResolved = false;
    active.resolveQuit = resolveQuit;
    active.quitRequested = true;
    active.quitRequestId = body.id;
    active.quitPosted = false;
    active.quitCommandAttempted = false;
    active.quitAcknowledged = false;
    active.terminalAfterQuit = false;
    active.terminalSequence = 0;
    active.membershipCheckPending = false;
    active.membershipCheckBlocked = false;
    active.membershipTicket = null;
    active.state = "ending";
    try { armDeadline(active); }
    catch {
      quarantine(active, "clock_unavailable");
      return promise;
    }
    emit("ending", "quit_posting");
    void driveQuit(active, body.id);
    return promise;
  }

  function onOutput(frame) {
    const record = readPlainDataObject(frame, ["command", "hostGeneration", "value"], { allowExtra: true });
    if (!record || record.hostGeneration !== hostGeneration ||
        ![GROUP_ENTER_OUTPUT_COMMAND, GROUP_TERMINAL_OUTPUT_COMMAND, GROUP_QUIT_COMMAND].includes(record.command)) return false;
    const active = slot;
    if (!active || !owned(active)) return false;
    if (record.command === GROUP_QUIT_COMMAND) {
      const values = Array.isArray(record.value) ? record.value : null;
      if (!active.scope || active.state !== "ending" || !active.quitRequested || !active.quitCommandAttempted ||
          frame.id !== active.quitRequestId || !isPositiveSafeInteger(frame.id) ||
          !values || values.length !== 2 || values[0] !== 0 || typeof values[1] !== "string" ||
          !contextIsCurrent()) return false;
      if (active.quitAcknowledged || active.terminalAfterQuit) return true;
      active.quitAcknowledged = true;
      if (terminalSequence < Number.MAX_SAFE_INTEGER) terminalSequence += 1;
      active.terminalSequence = terminalSequence;
      void maybeConfirmMembership(active);
      return true;
    }
    if (record.command === GROUP_ENTER_OUTPUT_COMMAND) {
      // A late successful callback cannot restore an expired admission. It can
      // only cancel the exact StartCall we still own, preventing a silent room
      // participant after the caller has already received a failed join.
      if (active.startCommandAttempted && active.state === "quarantined" &&
          active.quarantineCode === "start_timeout" && contextIsCurrent()) {
        const late = parseGroupEnterCallback(record.value, active.groupId);
        if (late.matched || late.unexpectedCreator) autoQuitUnexpectedCreator(active, "start_timeout");
        return false;
      }
      if (!active.startCommandAttempted || !["posting_start", "awaiting_entry"].includes(active.state)) return false;
      const parsed = parseGroupEnterCallback(record.value, active.groupId);
      if ((parsed.matched || parsed.unexpectedCreator) && expireIfDue(active)) {
        if (active.quarantineCode === "start_timeout" && contextIsCurrent()) {
          autoQuitUnexpectedCreator(active, "start_timeout");
        }
        return false;
      }
      if (parsed.unexpectedCreator) {
        autoQuitUnexpectedCreator(active);
        return true;
      }
      if (!parsed.matched) return false;
      if (!contextIsCurrent()) {
        quarantine(active, "stale_context");
        return false;
      }
      if (active.state === "posting_start") {
        active.entryCandidate = Object.freeze({ relationId: parsed.relationId });
        return true;
      }
      return acceptEntry(active, parsed.relationId);
    }

    if (!active.scope || !["joined", "ending", "ending_remote"].includes(active.state)) return false;
    const parsed = parseGroupTerminalCallback(record.value, active.scope.roomId);
    if (!parsed.matched) return false;
    if (expireIfDue(active)) return false;
    if ((active.state === "ending_remote" && active.remoteTerminal) ||
        (active.state === "ending" && active.quitRequested && (active.terminalAfterQuit || active.quitAcknowledged))) return true;
    if (terminalSequence < Number.MAX_SAFE_INTEGER) terminalSequence += 1;
    if (active.membershipCheckPending) {
      active.terminalSequence = terminalSequence;
      active.membershipTicket = null;
      quarantine(active, "membership_confirmation_stale");
      return true;
    }
    if (active.state === "ending_remote") {
      active.terminalSequence = terminalSequence;
      active.membershipTicket = null;
      quarantine(active, "membership_confirmation_stale");
      return true;
    }
    if (active.state === "ending" && active.quitRequested) {
      if (active.terminalAfterQuit) return true;
      active.terminalAfterQuit = true;
      active.terminalSequence = terminalSequence;
      active.membershipTicket = null;
      void maybeConfirmMembership(active);
      emit("ending", "terminal_observed_waiting_proof");
      return true;
    }
    if (active.state === "joined" && active.startPosted && active.scope) {
      active.remoteTerminal = true;
      active.terminalSequence = terminalSequence;
      active.state = "ending_remote";
      try { armDeadline(active); }
      catch {
        quarantine(active, "clock_unavailable");
        return true;
      }
      emit("ending_remote", "remote_terminal_waiting_proof");
      void maybeConfirmMembership(active);
      return true;
    }
    emit(active.state, "remote_terminal_observed");
    return true;
  }

  function observeRoomEnd(scope) {
    const active = slot;
    if (!owned(active) || !scopeMatches(scope, active.scope) || !active.startPosted ||
        !["joined", "ending", "ending_remote", "quarantined"].includes(active.state) ||
        !contextIsCurrent() || !otherCallSlotIsFree()) return false;
    if (active.state === "ending_remote" && active.remoteTerminal) return true;
    if (terminalSequence < Number.MAX_SAFE_INTEGER) terminalSequence += 1;
    active.remoteTerminal = true;
    active.terminalSequence = terminalSequence;
    active.membershipTicket = null;
    active.membershipCheckPending = false;
    active.membershipCheckBlocked = false;
    active.state = "ending_remote";
    try { armDeadline(active); }
    catch { quarantine(active, "clock_unavailable"); return true; }
    emit("ending_remote", "kernel_room_end_waiting_proof");
    void maybeConfirmMembership(active);
    return true;
  }

  function stop() {
    if (stopped) return status();
    stopped = true;
    const active = slot;
    if (active) {
      clearTimer(active);
      if (active.startCommandAttempted || active.quitRequested || active.scope) {
        active.state = "quarantined";
        active.quarantineCode = "host_stopped";
        finishStart(active, safeFailure("host_stopped", "quarantined"));
        finishQuit(active, safeFailure("host_stopped", "quarantined"));
        emit("quarantined", "host_stopped");
      } else {
        release(active, "host_stopped", "host_stopped");
      }
    }
    return status();
  }

  function status() {
    return Object.freeze({
      occupied: slot !== null,
      state: slot ? slot.state : "idle",
      code: lastStatusCode,
      stopped,
      hostGeneration,
      connectionGeneration,
      roomGeneration,
      terminalSequence,
    });
  }

  function currentScope() {
    const active = slot;
    if (!active || active.owner !== ownerToken || stopped || active.state !== "joined" || !active.scope ||
        !contextIsCurrent()) return null;
    return cloneScope(active.scope);
  }

  return Object.freeze({ start, quit, onOutput, observeRoomEnd, stop, status, currentScope });
}

module.exports = {
  GROUP_SCENE,
  GROUP_START_COMMAND,
  GROUP_QUIT_COMMAND,
  GROUP_ENTER_OUTPUT_COMMAND,
  GROUP_TERMINAL_OUTPUT_COMMAND,
  DEFAULT_START_TTL_MS,
  NATIVE_GROUP_SOURCE,
  buildStartParams,
  parseExistingRoomQueryResult,
  parseGroupEnterCallback,
  parseGroupTerminalCallback,
  createGroupCallDriver,
};
