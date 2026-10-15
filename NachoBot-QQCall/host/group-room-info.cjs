"use strict";

const GROUP_ROOM_QUERY_COMMAND = 13;
const GROUP_ROOM_INFO_OUTPUT_COMMAND = 20010;
const GROUP_ROOM_SCENE = 3;
const GROUP_ROOM_QUERY_TIMEOUT_MS = 10_000;
const MAX_GROUP_ID_BYTES = 20;
const MAX_GROUP_INFO_JSON_BYTES = 256 * 1024;
const MAX_GROUP_ROOM_USERS = 1000;
const MAX_GROUP_ROOM_UID_BYTES = 128;
const QUERY_OBSERVATION_COUNTS = Object.freeze([
  "outputCount", "matchingOutputs", "emptySuccessOutputs", "emptyErrorOutputs",
  "otherUnmatchedOutputs", "invalidMetadataOutputs",
  "queryAckOutputs", "matchingQueryAckOutputs",
]);

function copyGroupRoomQueryObservation(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const own = (key) => {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
    } catch { return undefined; }
  };
  const result = { diagnosticOnly: true, commandPosted: own("commandPosted") === true };
  for (const key of QUERY_OBSERVATION_COUNTS) {
    const count = own(key);
    result[key] = Number.isSafeInteger(count) && count >= 0 ? Math.min(count, 1_000_000) : 0;
  }
  const code = own("lastEmptyStatusCode");
  result.lastEmptyStatusCode = Number.isInteger(code) && code >= -0x80000000 && code <= 0x7fffffff ? code : null;
  const firstInteger = own("lastQueryAckFirstInteger");
  result.lastQueryAckFirstInteger = Number.isInteger(firstInteger) && firstInteger >= -0x80000000 && firstInteger <= 0x7fffffff ? firstInteger : null;
  result.lastQueryAckMatched = own("lastQueryAckMatched") === true;
  const kind = own("lastQueryAckValueKind");
  result.lastQueryAckValueKind = ["array", "null", "string", "number", "boolean", "object"].includes(kind) ? kind : null;
  const length = own("lastQueryAckArrayLength");
  result.lastQueryAckArrayLength = Number.isSafeInteger(length) && length >= 0 ? Math.min(length, 8192) : null;
  return Object.freeze(result);
}

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function ownKeysExactly(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

function isPositiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function isKnownPrepostRefusal(error) {
  return [
    "host_stopped", "host_not_ready", "renderer_crashed", "renderer_closed",
    "readiness_unavailable", "output_forward_failed", "host_busy",
    "audio_device_selection_busy", "audio_device_selection_quarantined",
    "audio_device_query_busy", "audio_device_query_quarantined", "group_call_reserved",
  ].includes(error?.code);
}

function validateGroupRoomInfoBody(body, hostGeneration) {
  if (!ownKeysExactly(body, ["id", "hostGeneration", "groupId"])) {
    throw codedError("invalid_request_shape");
  }
  if (!isPositiveInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
    throw codedError("generation_mismatch");
  }
  if (!isPositiveInteger(body.id)) throw codedError("invalid_request_id");
  if (typeof body.groupId !== "string" ||
      Buffer.byteLength(body.groupId, "utf8") > MAX_GROUP_ID_BYTES ||
      !/^[1-9][0-9]{0,19}$/.test(body.groupId)) {
    throw codedError("invalid_group_id");
  }
  return body;
}

function validateGroupRoomRecord(record, expectedGroupId) {
  if (!ownKeysExactly(record, ["scene_id", "relation_id", "user_num", "user_list"])) {
    throw codedError("group_room_metadata_invalid");
  }
  if (!Number.isSafeInteger(record.user_num) || record.user_num < 0 || record.user_num > MAX_GROUP_ROOM_USERS ||
      !Array.isArray(record.user_list) || record.user_list.length > MAX_GROUP_ROOM_USERS) {
    throw codedError("group_room_metadata_invalid");
  }

  const uids = new Set();
  const userList = record.user_list.map((user) => {
    if (!ownKeysExactly(user, ["uid", "account_type"]) ||
        typeof user.uid !== "string" ||
        Buffer.byteLength(user.uid, "utf8") < 1 ||
        Buffer.byteLength(user.uid, "utf8") > MAX_GROUP_ROOM_UID_BYTES ||
        !/^[A-Za-z0-9._:-]+$/.test(user.uid) ||
        !Number.isSafeInteger(user.account_type) || user.account_type < 0 || user.account_type > 65535 ||
        uids.has(user.uid)) {
      throw codedError("group_room_metadata_invalid");
    }
    uids.add(user.uid);
    return {
      uid: user.uid,
      accountType: user.account_type,
    };
  });

  return {
    sceneId: GROUP_ROOM_SCENE,
    relationId: expectedGroupId,
    userNum: record.user_num,
    userList,
  };
}

function parseGroupRoomInfoOutput(value, expectedGroupId) {
  if (!Array.isArray(value) || value.length !== 2 || typeof value[0] !== "string" ||
      typeof value[1] !== "number" || !Number.isInteger(value[1]) ||
      value[1] < -0x80000000 || value[1] > 0x7FFFFFFF || value[1] !== 0 ||
      value[0].length === 0 ||
      Buffer.byteLength(value[0], "utf8") > MAX_GROUP_INFO_JSON_BYTES) {
    return { matched: false };
  }

  let record;
  try { record = JSON.parse(value[0]); }
  catch { return { matched: false }; }
  if (!record || typeof record !== "object" || Array.isArray(record) ||
      record.scene_id !== GROUP_ROOM_SCENE || record.relation_id !== expectedGroupId) {
    return { matched: false };
  }
  return { matched: true, roomInfo: validateGroupRoomRecord(record, expectedGroupId) };
}

function createGroupRoomInfoQuery({
  hostGeneration,
  capability = false,
  repeatable = false,
  sendCommand,
  timeoutMs = GROUP_ROOM_QUERY_TIMEOUT_MS,
  schedule = setTimeout,
  cancel = clearTimeout,
} = {}) {
  if (!isPositiveInteger(hostGeneration)) throw codedError("invalid_host_generation");
  if (typeof capability !== "boolean") throw codedError("group_room_capability_invalid");
  if (typeof repeatable !== "boolean") throw codedError("group_room_repeatable_invalid");
  if (typeof sendCommand !== "function") throw codedError("group_room_sender_required");
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000) {
    throw codedError("group_room_timeout_invalid");
  }

  let stopped = false;
  let used = false;
  let quarantined = false;
  let quarantineCode = null;
  let outputSequence = 0;
  let ticket = null;
  let lastObservation = null;

  function clearTicket(active, error, result) {
    if (!active || active.settled) return;
    active.settled = true;
    lastObservation = copyGroupRoomQueryObservation(active.observation);
    if (active.timer !== null) cancel(active.timer);
    if (ticket === active) ticket = null;
    if (error) active.reject(error);
    else active.resolve(result);
  }

  function finishIfReady(active) {
    if (active !== ticket || active.settled || !active.commandPosted) return;
    if (active.emptyErrorReceived && active.queryAckAccepted) {
      // A completed negative query is not membership/absence proof. Settling
      // its paired ACK + error prevents a server-rejected lookup from being
      // mistaken for an uncorrelated timeout that poisons every future query.
      clearTicket(active, codedError("group_room_unavailable"));
      return;
    }
    if (!active.roomInfo) return;
    clearTicket(active, null, {
      ok: true,
      hostGeneration,
      id: active.id,
      groupId: active.groupId,
      diagnosticOnly: true,
      nativeJoinVerified: false,
      roomInfo: active.roomInfo,
    });
  }

  function query(body) {
    if (stopped) throw codedError("host_stopped");
    validateGroupRoomInfoBody(body, hostGeneration);
    if (!capability) throw codedError("group_room_info_protocol_unverified");
    if (quarantined) throw codedError("group_room_query_quarantined");
    if (ticket) throw codedError(repeatable ? "group_room_query_busy" : "group_room_query_exhausted");
    if (!repeatable && used) throw codedError("group_room_query_exhausted");
    used = true;

    let resolve;
    let reject;
    const result = new Promise((res, rej) => { resolve = res; reject = rej; });
    result.catch(() => {});
    const active = {
      id: body.id,
      groupId: body.groupId,
      sequence: outputSequence,
      commandPosted: false,
      observation: { commandPosted: false, lastEmptyStatusCode: null,
        ...Object.fromEntries(QUERY_OBSERVATION_COUNTS.map((key) => [key, 0])) },
      roomInfo: null,
      timer: null,
      settled: false,
      resolve,
      reject,
    };
    ticket = active;
    active.timer = schedule(() => {
      if (repeatable) { quarantined = true; quarantineCode = "group_room_query_timeout"; }
      clearTicket(active, codedError("group_room_query_timeout"));
    }, timeoutMs);
    active.timer?.unref?.();

    Promise.resolve().then(() => sendCommand({
      command: GROUP_ROOM_QUERY_COMMAND,
      id: active.id,
      params: [GROUP_ROOM_SCENE, active.groupId],
      hostGeneration,
    })).then((receipt) => {
      if (ticket !== active || active.settled) return;
      if (!receipt || receipt.posted !== true || receipt.id !== active.id || receipt.command !== GROUP_ROOM_QUERY_COMMAND) {
        if (repeatable) { quarantined = true; quarantineCode = "group_room_query_post_failed"; }
        clearTicket(active, codedError("group_room_query_post_failed"));
        return;
      }
      active.commandPosted = true;
      active.observation.commandPosted = true;
      finishIfReady(active);
    }, (error) => {
      const safePrepostRefusal = repeatable && isKnownPrepostRefusal(error);
      if (repeatable && !safePrepostRefusal) { quarantined = true; quarantineCode = "group_room_query_post_failed"; }
      clearTicket(active, codedError(safePrepostRefusal ? error.code : "group_room_query_post_failed"));
    });

    return result;
  }

  function observeOutput(frame) {
    if (frame?.command !== GROUP_ROOM_INFO_OUTPUT_COMMAND && frame?.command !== GROUP_ROOM_QUERY_COMMAND) return;
    if (frame.hostGeneration !== undefined && frame.hostGeneration !== hostGeneration) return;
    if (frame.command === GROUP_ROOM_INFO_OUTPUT_COMMAND && frame.id !== 0) return;
    outputSequence += 1;
    const active = ticket;
    if (!active || active.sequence >= outputSequence || active.settled) return;
    const observed = active.observation;
    const increment = (key) => { observed[key] = Math.min(1_000_000, observed[key] + 1); };
    if (frame.command === GROUP_ROOM_QUERY_COMMAND) {
      // Command acknowledgement and the room-result event are separate facts.
      // ACK alone never completes a query or proves room membership/absence.
      increment("queryAckOutputs");
      const matched = frame.id === active.id;
      if (matched) increment("matchingQueryAckOutputs");
      observed.lastQueryAckMatched = matched;
      const array = Array.isArray(frame.value) ? frame.value : null;
      const first = array ? Object.getOwnPropertyDescriptor(array, "0") : null;
      const value = first && Object.hasOwn(first, "value") ? first.value : null;
      observed.lastQueryAckFirstInteger = Number.isInteger(value) && value >= -0x80000000 && value <= 0x7fffffff ? value : null;
      const second = array ? Object.getOwnPropertyDescriptor(array, "1") : null;
      if (matched && array && array.length === 2 && value === 0 && second && Object.hasOwn(second, "value") && second.value === "") {
        active.queryAckAccepted = true;
      }
      observed.lastQueryAckValueKind = array ? "array" : frame.value === null ? "null" : typeof frame.value;
      const length = array ? Object.getOwnPropertyDescriptor(array, "length") : null;
      observed.lastQueryAckArrayLength = length && Object.hasOwn(length, "value") ? length.value : null;
      finishIfReady(active);
      return;
    }
    increment("outputCount");
    let parsed;
    try { parsed = parseGroupRoomInfoOutput(frame.value, active.groupId); }
    catch {
      increment("invalidMetadataOutputs");
      if (repeatable) { quarantined = true; quarantineCode = "group_room_metadata_invalid"; }
      clearTicket(active, codedError("group_room_metadata_invalid"));
      return;
    }
    if (!parsed.matched) {
      const payload = Array.isArray(frame.value) ? Object.getOwnPropertyDescriptor(frame.value, "0") : null;
      const status = Array.isArray(frame.value) ? Object.getOwnPropertyDescriptor(frame.value, "1") : null;
      const code = status && Object.hasOwn(status, "value") ? status.value : null;
      if (Array.isArray(frame.value) && frame.value.length === 2 && payload && Object.hasOwn(payload, "value") && payload.value === "" &&
          Number.isInteger(code) && code >= -0x80000000 && code <= 0x7fffffff) {
        increment(code === 0 ? "emptySuccessOutputs" : "emptyErrorOutputs");
        observed.lastEmptyStatusCode = code;
        if (code !== 0) {
          active.emptyErrorReceived = true;
          finishIfReady(active);
        }
      } else {
        increment("otherUnmatchedOutputs");
      }
      return;
    }
    increment("matchingOutputs");
    active.roomInfo = parsed.roomInfo;
    finishIfReady(active);
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    if (ticket) clearTicket(ticket, codedError("host_stopped"));
  }

  return Object.freeze({ query, observeOutput, stop,
    observation: () => copyGroupRoomQueryObservation(ticket?.observation || lastObservation),
    status: () => ({ used, pending: Boolean(ticket), stopped, repeatable, quarantined, quarantineCode }) });
}

module.exports = {
  GROUP_ROOM_QUERY_COMMAND,
  GROUP_ROOM_INFO_OUTPUT_COMMAND,
  GROUP_ROOM_SCENE,
  GROUP_ROOM_QUERY_TIMEOUT_MS,
  validateGroupRoomInfoBody,
  parseGroupRoomInfoOutput,
  copyGroupRoomQueryObservation,
  createGroupRoomInfoQuery,
};
