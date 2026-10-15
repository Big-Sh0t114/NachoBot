"use strict";

const GROUP_DIAGNOSTIC_TTL_MS = 30_000;
const MAX_TRACKED_GROUPS = 32;
const MAX_GROUP_ID_LENGTH = 20;
const MAX_ROOM_ID_LENGTH = 512;
const MAX_EVENT_ARGUMENTS = 8;
const MAX_EVENT_ARRAY_ITEMS = 8192;
const MAX_NONNEGATIVE_COUNT = 1_000_000;
const MAX_EVENT_KEYS = 64;
const MAX_SEQUENCE_DIGITS = 20;

function validGroupId(value) {
  return typeof value === "string" && value.length <= MAX_GROUP_ID_LENGTH && /^[1-9][0-9]{0,19}$/.test(value);
}

function getOwnData(object, key) {
  if (!object || typeof object !== "object") return undefined;
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value") ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function isPlainRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  try {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) return false;
    return Reflect.ownKeys(value).length <= MAX_EVENT_KEYS;
  } catch {
    return false;
  }
}

function getOwnArrayItems(value, maxItems = MAX_EVENT_ARRAY_ITEMS) {
  if (!Array.isArray(value)) return null;
  try {
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
    const length = lengthDescriptor && "value" in lengthDescriptor ? lengthDescriptor.value : null;
    if (!Number.isSafeInteger(length) || length < 0 || length > maxItems) return null;
    const items = [];
    for (let index = 0; index < length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value")) return null;
      items.push(descriptor.value);
    }
    return items;
  } catch {
    return null;
  }
}

function safeNonemptyString(value, maxLength) {
  return typeof value === "string" && value.length >= 1 && value.length <= maxLength &&
    value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value);
}

function nonnegativeInteger(value, max = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= 0 && value <= max;
}

function boundedCount(value) {
  if (nonnegativeInteger(value, MAX_NONNEGATIVE_COUNT)) return value;
  if (typeof value !== "string" || value.length > String(MAX_NONNEGATIVE_COUNT).length || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const parsed = Number(value);
  return nonnegativeInteger(parsed, MAX_NONNEGATIVE_COUNT) ? parsed : null;
}

function boundedSequence(value) {
  if (nonnegativeInteger(value)) return value;
  return typeof value === "string" && value.length <= MAX_SEQUENCE_DIGITS && /^(0|[1-9][0-9]*)$/.test(value)
    ? value
    : null;
}

function createGroupKernelObserver({ connectionGeneration, now = Date.now } = {}) {
  if (!Number.isSafeInteger(connectionGeneration) || connectionGeneration < 1) {
    throw Object.assign(new Error("invalid_connection_generation"), { code: "invalid_connection_generation" });
  }
  if (typeof now !== "function") {
    throw Object.assign(new Error("invalid_clock"), { code: "invalid_clock" });
  }

  const groups = new Map();
  let membershipEvents = 0;
  let invitationEvents = 0;
  let roomEndObservations = 0;
  let stopped = false;

  function readNow() {
    try {
      const value = now();
      return Number.isSafeInteger(value) && value >= 0 ? value : null;
    } catch {
      return null;
    }
  }

  function expire(nowMs) {
    for (const [groupId, record] of groups) {
      for (const category of ["membership", "invitation", "roomEndObservation"]) {
        const item = record[category];
        if (item && (nowMs < item.observedAtMs || nowMs - item.observedAtMs >= GROUP_DIAGNOSTIC_TTL_MS)) {
          record[category] = null;
        }
      }
      if (!record.membership && !record.invitation && !record.roomEndObservation) groups.delete(groupId);
    }
  }

  function clear() {
    groups.clear();
    membershipEvents = 0;
    invitationEvents = 0;
    roomEndObservations = 0;
  }

  function upsert(groupId, category, value, nowMs) {
    let record = groups.get(groupId);
    if (!record) record = { groupId, membership: null, invitation: null, roomEndObservation: null };
    record[category] = { ...value, observedAtMs: nowMs };
    groups.delete(groupId);
    groups.set(groupId, record);
    while (groups.size > MAX_TRACKED_GROUPS) groups.delete(groups.keys().next().value);
  }

  function infoFromEvent(event, directArgument = false) {
    const args = getOwnArrayItems(getOwnData(event, "args"), MAX_EVENT_ARGUMENTS);
    if (!args || args.length !== 1 || !isPlainRecord(args[0])) return null;
    if (directArgument) return args[0];
    const info = getOwnData(args[0], "notifyInfo");
    return isPlainRecord(info) ? info : null;
  }

  function observeMembership(method, info, nowMs) {
    const groupId = getOwnData(info, "group_code");
    const onlineCount = getOwnData(info, "onlineCount");
    if (!validGroupId(groupId) || !nonnegativeInteger(onlineCount, MAX_NONNEGATIVE_COUNT)) return false;

    let memberCount;
    const memberUins = getOwnData(info, "memberUins");
    if (memberUins !== undefined) {
      const members = getOwnArrayItems(memberUins);
      if (!members) return false;
      memberCount = members.length;
    }

    let businessType;
    const rawBusinessType = getOwnData(info, "businessType");
    if (rawBusinessType !== undefined) {
      if (!nonnegativeInteger(rawBusinessType, 0x7fffffff)) return false;
      businessType = rawBusinessType;
    }

    let nativeTimeout;
    const rawNativeTimeout = getOwnData(info, "isNativeTimeout");
    if (rawNativeTimeout !== undefined) {
      if (typeof rawNativeTimeout !== "boolean") return false;
      nativeTimeout = rawNativeTimeout;
    }
    if (method === "ongroupaudiomemnumchange" && nativeTimeout === undefined) return false;

    const membership = {
      source: method === "ongroupaudiomemnumchange" ? "group_audio_member_count" : "group_video_member_count",
      onlineCount,
      ...(memberCount !== undefined ? { memberCount } : {}),
      ...(businessType !== undefined ? { businessType } : {}),
      ...(nativeTimeout !== undefined ? { nativeTimeout } : {}),
    };
    upsert(groupId, "membership", membership, nowMs);
    membershipEvents += 1;

    if (method === "ongroupaudiomemnumchange" && nativeTimeout === true && onlineCount === 0) {
      upsert(groupId, "roomEndObservation", {
        source: "native_timeout_zero_members",
        nativeTimeout: true,
        onlineCount: 0,
      }, nowMs);
      roomEndObservations += 1;
    }
    return true;
  }

  function observeInvitation(info, nowMs) {
    const groupId = getOwnData(info, "groupId");
    const roomId = getOwnData(info, "roomId");
    const totalInvitedMembers = boundedCount(getOwnData(info, "totalInvitedMembers"));
    const seq = boundedSequence(getOwnData(info, "seq"));
    const join = getOwnArrayItems(getOwnData(info, "join"), MAX_EVENT_ARRAY_ITEMS);
    const quit = getOwnArrayItems(getOwnData(info, "quit"), MAX_EVENT_ARRAY_ITEMS);
    if (!validGroupId(groupId) || !safeNonemptyString(roomId, MAX_ROOM_ID_LENGTH) ||
        totalInvitedMembers === null || seq === null ||
        !join || !quit) return false;

    upsert(groupId, "invitation", {
      source: "group_video_invite_member_update",
      roomId,
      totalInvitedMembers,
      seq,
      joinCount: join.length,
      quitCount: quit.length,
    }, nowMs);
    invitationEvents += 1;
    return true;
  }

  function observe(event) {
    if (stopped || !isPlainRecord(event) || getOwnData(event, "connectionGeneration") !== connectionGeneration) return false;
    const nowMs = readNow();
    if (nowMs === null) return false;
    expire(nowMs);
    const rawMethod = getOwnData(event, "method");
    if (typeof rawMethod !== "string" || rawMethod.length > 80) return false;
    const method = rawMethod.toLowerCase();
    if (!["ongroupaudiomemnumchange", "ongroupvideomemnumpushinfo", "ongroupvideoinvitememberupdate"].includes(method)) return false;
    // The pinned native audio callback carries direct args[0] fields. The
    // renderer adds notifyInfo later; that renderer envelope is not this wire.
    const info = infoFromEvent(event, method === "ongroupvideoinvitememberupdate" || method === "ongroupaudiomemnumchange");
    if (!info) return false;
    return method === "ongroupvideoinvitememberupdate"
      ? observeInvitation(info, nowMs)
      : observeMembership(method, info, nowMs);
  }

  function copyRecord(record) {
    const copyItem = (item) => item ? Object.freeze({ ...item }) : null;
    return Object.freeze({
      groupId: record.groupId,
      membership: copyItem(record.membership),
      invitation: copyItem(record.invitation),
      roomEndObservation: copyItem(record.roomEndObservation),
      diagnosticOnly: true,
      nativeJoinVerified: false,
    });
  }

  function getGroupDiagnostics(groupId) {
    if (stopped || !validGroupId(groupId)) return null;
    const nowMs = readNow();
    if (nowMs === null) return null;
    expire(nowMs);
    const record = groups.get(groupId);
    return record ? copyRecord(record) : null;
  }

  function status() {
    const nowMs = readNow();
    if (nowMs !== null) expire(nowMs);
    return Object.freeze({
      trackedGroups: groups.size,
      membershipEvents,
      invitationEvents,
      roomEndObservations,
    });
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    clear();
  }

  return Object.freeze({ observe, getGroupDiagnostics, status, clear, stop });
}

module.exports = {
  GROUP_DIAGNOSTIC_TTL_MS,
  MAX_TRACKED_GROUPS,
  createGroupKernelObserver,
};
