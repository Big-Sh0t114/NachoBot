"use strict";

const crypto = require("node:crypto");
const http = require("node:http");
const path = require("node:path");
const { TextDecoder } = require("node:util");
const {
  AUDIO_DEVICE_INFO_COMMAND,
  MAX_DEVICE_NAME_BYTES,
  parseAudioDeviceSnapshot,
  resolveUniqueDevice,
  sameDeviceMappings,
} = require("./audio-devices.cjs");
const {
  GROUP_ROOM_INFO_OUTPUT_COMMAND,
  GROUP_ROOM_QUERY_COMMAND,
  GROUP_ROOM_QUERY_TIMEOUT_MS,
  GROUP_ROOM_SCENE,
  createGroupRoomInfoQuery,
  parseGroupRoomInfoOutput,
  validateGroupRoomInfoBody,
} = require("./group-room-info.cjs");
const { parseGroupEnterCallback } = require("./group-call.cjs");
const { createChattingRoomIdQuery } = require("./chatting-room-id.cjs");
const {
  QRTC_COMMAND_QUIT,
  QRTC_COMMAND_REJECT,
  QRTC_COMMAND_CLOSE,
  validatePrivateEndParams,
} = require("./private-end.cjs");

const LOOPBACK = "127.0.0.1";
const MAX_FRAME_BYTES = 1024 * 1024;
const MAX_LOGIN_FIELD_BYTES = 8192;
const MAX_ACCEPT_FIELD_BYTES = 8192;
const MAX_ACTION_BYTES = 256 * 1024;
const DEFAULT_OUTPUT_QUEUE_BYTES = MAX_FRAME_BYTES * 4;
const ALLOWED_COMMANDS = new Set([1, 5, 55]);
const SDK_INFO_COMMAND = 57;
const SDK_INFO_PARAMS = Object.freeze(["1.0.1168"]);
const MAX_UNCERTAIN_LOGIN_REQUEST_IDS = 256;
const GROUP_START_COMMAND = 4;
const GROUP_QUIT_COMMAND = 8;
const GROUP_ERROR_OUTPUT_COMMAND = 20003;
const GROUP_ENTER_OUTPUT_COMMAND = 20004;
const GROUP_TIMEOUT_OUTPUT_COMMAND = 20005;
const GROUP_TERMINAL_OUTPUT_COMMAND = 20011;
const GROUP_ACCEPT_COMMAND = 5;
const GROUP_NATIVE_OBSERVATION_MAX_COMMANDS = 16;
const GROUP_NATIVE_OBSERVATION_MAX_COUNT = 1000;
const GROUP_ERROR_MESSAGE_MAX_BYTES = 8192;
const GROUP_ERROR_REASON_KEYS = Object.freeze([
  "invalid_parameters", "permission_denied", "not_in_group", "group_not_found", "network_error",
  "login_required", "unsupported", "room_error", "timeout", "unknown",
]);
const GROUP_ERROR_REASON_PATTERNS = Object.freeze([
  ["invalid_parameters", /\binvalid\s+(?:parameters?|params?)\b|\bbad\s+parameters?\b|\bparameter\s+error\b|invalid_argument|参数无效|参数错误|无效参数|参数不合法/iu],
  ["permission_denied", /\bpermission\s+denied\b|\binsufficient\s+permissions?\b|\bno\s+permission\b|\bunauthori[sz]ed\b|\bforbidden\b|权限不足|没有权限|无权限|未授权/iu],
  ["not_in_group", /\bnot\s+(?:a\s+)?member(?:\s+of\s+(?:the\s+)?group)?\b|\bnot\s+in\s+(?:the\s+)?group\b|\bnot\s+joined(?:\s+the)?\s+group\b|非群成员|不在群聊|不在群|未加入群|不是群成员/iu],
  ["group_not_found", /\bgroup\s+(?:not\s+found|does\s+not\s+exist|not\s+exist)\b|\bno\s+such\s+group\b|群(?:聊)?(?:不存在|未找到)|群号不存在/iu],
  ["network_error", /\bnetwork\s+(?:error|failure|unavailable)\b|\bconnection\s+(?:failed|error|refused|lost)\b|\bsocket\s+error\b|网络错误|网络异常|连接失败|连接异常|连接中断|无法连接/iu],
  ["login_required", /\blogin\s+required\b|\bnot\s+logged\s+in\b|\bplease\s+log\s+in\b|\bauthentication\s+required\b|登录失效|登录过期|请先登录|未登录/iu],
  ["unsupported", /\bunsupported\b|\bnot\s+supported\b|\bnot\s+available\b|暂不支持|不支持|未实现/iu],
  ["room_error", /\broom\s+(?:error|failed|unavailable)\b|房间错误|房间异常|房间失败|进房失败|加入房间失败|语音房失败|群通话失败/iu],
  ["timeout", /\btime[\s_-]?out\b|\btimed\s+out\b|超时/iu],
]);
const PRIVATE_INVITE_OUTPUT_COMMAND = 20006;
const GROUP_INVITE_OUTPUT_COMMAND = PRIVATE_INVITE_OUTPUT_COMMAND;
const PRIVATE_CALL_SCENE = 1;
const GROUP_INVITE_OBSERVATION_TTL_MS = 30_000;
const GROUP_INVITE_ACCEPT_TTL_MS = 30_000;
const GROUP_INVITE_MAX_LIST_ENTRIES = 1000;
const GROUP_INVITE_CONSUMED_FINGERPRINT_LIMIT = 256;
const GROUP_CONTROL_PRE_DISPATCH_REFUSAL = Symbol("group-control-pre-dispatch-refusal");
const QQ_UID_PATTERN = /^u_[A-Za-z0-9_-]{22}$/;
const CANONICAL_UIN_PATTERN = /^[1-9][0-9]{0,19}$/;
const VIRTUAL_AUDIO_MARKERS = Object.freeze([
  "virtual audio cable", "vb-audio", "voicemeeter", "steelseries sonar",
  " sonar", "mixline", "steam streaming", "oculus virtual audio",
]);
const AUDIO_DEVICE_QUERY_COMMAND = 103;
const AUDIO_DEVICE_SELECT_COMMAND = 102;
const AUDIO_DEVICE_INFO_OUTPUT_COMMAND = AUDIO_DEVICE_INFO_COMMAND;
const AUDIO_DEVICE_SELECT_TIMEOUT_MS = 5000;
const FAILURE_CODE_PATTERN = /^[a-z][a-z0-9_]{1,63}$/;
const PRIVATE_END_TRACE_WINDOW_MS = 5000;
const PRIVATE_END_TRACE_MAX_WINDOWS = 16;
const PRIVATE_END_TRACE_MAX_COMMANDS = 32;
const PRIVATE_END_TRACE_MAX_OUTPUTS_PER_WINDOW = 64;
const PRIVATE_END_TRACE_OUTPUT_COMMANDS = new Set([QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE, 20001, 20011, 20017]);

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function groupControlPreDispatchError(error, id, command, fallbackCode) {
  const refusal = codedError(safeFailureCode(error, fallbackCode));
  Object.defineProperty(refusal, GROUP_CONTROL_PRE_DISPATCH_REFUSAL, {
    value: Object.freeze({ id, command }),
  });
  return refusal;
}

function groupControlErrorBody(error, code, command) {
  const refusal = error?.[GROUP_CONTROL_PRE_DISPATCH_REFUSAL];
  if (ownKeysExactly(refusal, ["id", "command"]) && isPositiveInteger(refusal.id) && refusal.command === command) {
    return { ok: false, code, id: refusal.id, command, posted: false };
  }
  return { ok: false, code };
}

function isPositiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function isInt32(value) {
  return Number.isInteger(value) && value >= -0x80000000 && value <= 0x7fffffff;
}

function createGroupNativeObservation() {
  return {
    startAck4CorrelatedSeen: false,
    startAck4ReturnCode: null,
    startAck4IdZeroSeen: false,
    startAck4IdZeroUntrustedReturnCode: null,
    startAck4OtherIdSeen: false,
    startAck4OtherIdUntrustedReturnCode: null,
    groupEnter20004Seen: false,
    groupEnter20004Matched: false,
    groupEnter20004ReturnCode: null,
    groupEnter20004Creator: "unknown",
    error20003Seen: false,
    error20003Matched: false,
    error20003MessageLength: null,
    error20003MessageSha256: null,
    error20003ReasonFlags: createGroupErrorReasonFlags(),
    error20003CodeLabel: null,
    error20003Code: null,
    timeout20005Observed: false,
    nativeCommandCounts: Object.create(null),
  };
}

function createGroupErrorReasonFlags() {
  return Object.fromEntries(GROUP_ERROR_REASON_KEYS.map((key) => [key, false]));
}

function classifyGroupErrorMessage(message) {
  const flags = createGroupErrorReasonFlags();
  for (const [key, pattern] of GROUP_ERROR_REASON_PATTERNS) flags[key] = pattern.test(message);
  flags.unknown = !GROUP_ERROR_REASON_PATTERNS.some(([key]) => flags[key]);
  return flags;
}

function extractGroupErrorCode(message) {
  const pattern = /(?:^|[^A-Za-z0-9_])(?<label>error(?:[\s_-]*code)?|ret|errno)\s*[:=]\s*(?<code>-?\d+)(?![A-Za-z0-9_.+-])/ig;
  for (const match of message.matchAll(pattern)) {
    let parsed;
    try { parsed = BigInt(match.groups.code); }
    catch { continue; }
    if (parsed < -0x80000000n || parsed > 0x7fffffffn) continue;
    const rawLabel = match.groups.label.toLowerCase();
    return { label: rawLabel.startsWith("error") ? "error" : rawLabel, code: Number(parsed) };
  }
  return { label: null, code: null };
}

function groupErrorObservation(value, expectedGroupId) {
  if (!isDenseCallbackArray(value, 3) || !isInt32(value[0]) || value[0] !== GROUP_ROOM_SCENE ||
      value[1] !== expectedGroupId || !stringWithin(value[2], GROUP_ERROR_MESSAGE_MAX_BYTES, { nonempty: false })) return null;
  const message = value[2];
  const code = extractGroupErrorCode(message);
  return {
    messageLength: Buffer.byteLength(message, "utf8"),
    messageSha256: crypto.createHash("sha256").update(message, "utf8").digest("hex"),
    reasonFlags: classifyGroupErrorMessage(message),
    codeLabel: code.label,
    code: code.code,
  };
}

function cloneGroupNativeObservation(value) {
  if (!value || typeof value !== "object") return null;
  const nativeCommandCounts = Object.create(null);
  for (const [command, count] of Object.entries(value.nativeCommandCounts || {}).slice(0, GROUP_NATIVE_OBSERVATION_MAX_COMMANDS)) {
    if (!/^[1-9][0-9]{0,9}$/.test(command) || !Number.isSafeInteger(Number(command)) ||
        Number(command) > 0x7fffffff || !Number.isSafeInteger(count) || count < 0) continue;
    nativeCommandCounts[command] = Math.min(count, GROUP_NATIVE_OBSERVATION_MAX_COUNT);
  }
  return {
    startAck4CorrelatedSeen: value.startAck4CorrelatedSeen === true,
    startAck4ReturnCode: isInt32(value.startAck4ReturnCode) ? value.startAck4ReturnCode : null,
    startAck4IdZeroSeen: value.startAck4IdZeroSeen === true,
    startAck4IdZeroUntrustedReturnCode: isInt32(value.startAck4IdZeroUntrustedReturnCode)
      ? value.startAck4IdZeroUntrustedReturnCode : null,
    startAck4OtherIdSeen: value.startAck4OtherIdSeen === true,
    startAck4OtherIdUntrustedReturnCode: isInt32(value.startAck4OtherIdUntrustedReturnCode)
      ? value.startAck4OtherIdUntrustedReturnCode : null,
    groupEnter20004Seen: value.groupEnter20004Seen === true,
    groupEnter20004Matched: value.groupEnter20004Matched === true,
    groupEnter20004ReturnCode: isInt32(value.groupEnter20004ReturnCode) ? value.groupEnter20004ReturnCode : null,
    groupEnter20004Creator: ["true", "false", "unknown"].includes(value.groupEnter20004Creator)
      ? value.groupEnter20004Creator : "unknown",
    error20003Seen: value.error20003Seen === true,
    error20003Matched: value.error20003Matched === true,
    error20003MessageLength: Number.isSafeInteger(value.error20003MessageLength) &&
      value.error20003MessageLength >= 0 && value.error20003MessageLength <= GROUP_ERROR_MESSAGE_MAX_BYTES
      ? value.error20003MessageLength : null,
    error20003MessageSha256: typeof value.error20003MessageSha256 === "string" &&
      /^[a-f0-9]{64}$/.test(value.error20003MessageSha256) ? value.error20003MessageSha256 : null,
    error20003ReasonFlags: Object.fromEntries(GROUP_ERROR_REASON_KEYS.map((key) =>
      [key, value.error20003ReasonFlags?.[key] === true])),
    error20003CodeLabel: ["error", "ret", "errno"].includes(value.error20003CodeLabel) ? value.error20003CodeLabel : null,
    error20003Code: isInt32(value.error20003Code) ? value.error20003Code : null,
    timeout20005Observed: value.timeout20005Observed === true,
    nativeCommandCounts,
  };
}

function groupInviteDiagnostic(value, freshRoomQuery, outputSequence, observedAtMs) {
  if (!isDenseCallbackArray(value, 12) || !isInt32(value[0]) || value[0] !== GROUP_ROOM_SCENE ||
      !stringWithin(value[1], 128, { nonempty: true }) || !Array.isArray(value[2]) ||
      !isDenseCallbackArray(value[2], 0, GROUP_INVITE_MAX_LIST_ENTRIES) ||
      typeof value[4] !== "string" || Buffer.byteLength(value[4], "utf8") > 20 ||
      !CANONICAL_UIN_PATTERN.test(value[4]) ||
      !stringWithin(value[11], MAX_ACCEPT_FIELD_BYTES)) return null;

  const freshQueryAgeMs = freshRoomQuery ? observedAtMs - freshRoomQuery.seenAtMs : -1;
  const relationMatchesFreshRoom = Boolean(freshRoomQuery && freshQueryAgeMs >= 0 &&
    freshQueryAgeMs <= GROUP_INVITE_OBSERVATION_TTL_MS &&
    outputSequence > freshRoomQuery.outputSequence && value[4] === freshRoomQuery.groupId);
  const inviterKind = QQ_UID_PATTERN.test(value[1]) ? "uid_pattern" :
    CANONICAL_UIN_PATTERN.test(value[1]) ? "uin_pattern" : "other";
  const showUiFlagKind = typeof value[10] === "boolean" ? "boolean" :
    value[10] === 0 || value[10] === 1 ? "zero_one" : "other";
  return {
    seen: true,
    tupleLength: 12,
    relationMatchesFreshRoom,
    inviterKind,
    inviteListCount: value[2].length,
    subBusinessType: isInt32(value[3]) ? value[3] : null,
    showUiFlagKind,
    sequenceStringLength: Buffer.byteLength(value[11], "utf8"),
  };
}

function groupAcceptInviteRecord(value, identity) {
  if (!identity || !isDenseCallbackArray(value, 12) || !isInt32(value[0]) ||
      value[0] !== GROUP_ROOM_SCENE || !QQ_UID_PATTERN.test(value[1]) ||
      !isDenseCallbackArray(value[2], 1, GROUP_INVITE_MAX_LIST_ENTRIES) ||
      !isInt32(value[3]) || value[3] !== 1 ||
      typeof value[4] !== "string" || Buffer.byteLength(value[4], "utf8") > 20 ||
      !CANONICAL_UIN_PATTERN.test(value[4]) ||
      !(typeof value[10] === "boolean" || value[10] === 0 || value[10] === 1) ||
      !stringWithin(value[11], MAX_ACCEPT_FIELD_BYTES)) return null;

  const inviteUids = value[2];
  if (!inviteUids.every((uid) => typeof uid === "string" && Buffer.byteLength(uid, "utf8") <= 128 &&
      (QQ_UID_PATTERN.test(uid) || CANONICAL_UIN_PATTERN.test(uid))) ||
      !inviteUids.some((uid) => uid === identity.selfUid || uid === identity.selfUin)) return null;

  const fingerprint = crypto.createHash("sha256").update(JSON.stringify([
    value[0], value[1], inviteUids, value[3], value[4], value[10], value[11],
  ])).digest("hex");
  return { inviterUid: value[1], groupId: value[4], fingerprint };
}

function cloneGroupInviteObservation(value) {
  if (!value || typeof value !== "object") return null;
  return {
    seen: value.seen === true,
    tupleLength: value.seen === true && value.tupleLength === 12 ? 12 : null,
    relationMatchesFreshRoom: value.relationMatchesFreshRoom === true,
    inviterKind: ["uid_pattern", "uin_pattern", "other"].includes(value.inviterKind) ? value.inviterKind : "other",
    inviteListCount: Number.isSafeInteger(value.inviteListCount) &&
      value.inviteListCount >= 0 && value.inviteListCount <= GROUP_INVITE_MAX_LIST_ENTRIES
      ? value.inviteListCount : null,
    subBusinessType: isInt32(value.subBusinessType) ? value.subBusinessType : null,
    showUiFlagKind: ["boolean", "zero_one", "other"].includes(value.showUiFlagKind) ? value.showUiFlagKind : "other",
    sequenceStringLength: Number.isSafeInteger(value.sequenceStringLength) &&
      value.sequenceStringLength >= 0 && value.sequenceStringLength <= MAX_ACCEPT_FIELD_BYTES
      ? value.sequenceStringLength : null,
  };
}

function groupEnterObservation(value, expectedGroupId) {
  if (!isDenseCallbackArray(value, 7) || !isInt32(value[0]) || value[1] !== GROUP_ROOM_SCENE ||
      value[2] !== expectedGroupId || !stringWithin(value[3], 128, { nonempty: false }) ||
      typeof value[5] !== "string" || Buffer.byteLength(value[5], "utf8") > 1024 ||
      !Number.isSafeInteger(value[6]) || value[6] < 0 || value[6] > 0x7fffffff) return null;
  const creator = value[4] === true || value[4] === 1 ? "true" :
    value[4] === false || value[4] === 0 ? "false" : "unknown";
  return {
    returnCode: value[0],
    creator,
    matched: parseGroupEnterCallback(value, expectedGroupId).matched,
  };
}

function validToken(token) {
  return typeof token === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(token);
}

function equalToken(expected, received) {
  if (typeof expected !== "string" || typeof received !== "string") return false;
  const left = Buffer.from(expected, "utf8");
  const right = Buffer.from(received, "utf8");
  return left.length === right.length && left.length > 0 && crypto.timingSafeEqual(left, right);
}

function ownKeysExactly(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index]);
}

function stringWithin(value, maxBytes, { nonempty = false } = {}) {
  return typeof value === "string" && (!nonempty || value.length > 0) && Buffer.byteLength(value, "utf8") <= maxBytes;
}

function validateParams(command, params, privateEndProtocolVerified = false) {
  if (!Array.isArray(params)) return false;
  if (command === 1) {
    return params.length === 5 && params.every((value) => stringWithin(value, MAX_LOGIN_FIELD_BYTES));
  }
  if (command === 5) {
    return params.length === 7 &&
      Number.isFinite(params[0]) &&
      stringWithin(params[1], MAX_ACCEPT_FIELD_BYTES, { nonempty: true }) &&
      Array.isArray(params[2]) && params[2].length === 1 &&
      stringWithin(params[2][0], MAX_ACCEPT_FIELD_BYTES, { nonempty: true }) &&
      Number.isFinite(params[3]) &&
      stringWithin(params[4], MAX_ACCEPT_FIELD_BYTES) &&
      typeof params[5] === "boolean" &&
      stringWithin(params[6], MAX_ACCEPT_FIELD_BYTES);
  }
  if (command === 55) {
    return params.length === 2 && Number.isFinite(params[0]) && stringWithin(params[1], MAX_ACTION_BYTES);
  }
  if ([QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(command) && privateEndProtocolVerified) {
    return validatePrivateEndParams(command, params);
  }
  return false;
}

function validateInvokeBody(body, hostGeneration, { privateEndProtocolVerified = false } = {}) {
  if (!ownKeysExactly(body, ["command", "id", "params", "hostGeneration"])) {
    throw codedError("invalid_request_shape");
  }
  if (!isPositiveInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
    throw codedError("generation_mismatch");
  }
  if (!isPositiveInteger(body.id)) throw codedError("invalid_request_id");
  const commandAllowed = ALLOWED_COMMANDS.has(body.command) ||
    (privateEndProtocolVerified && [QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(body.command));
  if (!Number.isSafeInteger(body.command) || !commandAllowed) {
    throw codedError("command_not_allowed");
  }
  if (!validateParams(body.command, body.params, privateEndProtocolVerified)) throw codedError("invalid_params");
  if (Buffer.byteLength(JSON.stringify(body.params), "utf8") > MAX_FRAME_BYTES / 2) {
    throw codedError("params_too_large");
  }
  return body;
}

function validateAudioDevicesBody(body, hostGeneration) {
  if (!ownKeysExactly(body, ["id", "hostGeneration"])) throw codedError("invalid_request_shape");
  if (!isPositiveInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
    throw codedError("generation_mismatch");
  }
  if (!isPositiveInteger(body.id)) throw codedError("invalid_request_id");
  return body;
}

function validateAudioDeviceSelectionBody(body, hostGeneration) {
  if (!ownKeysExactly(body, ["id", "hostGeneration", "inputDeviceName", "outputDeviceName"])) {
    throw codedError("invalid_request_shape");
  }
  if (!isPositiveInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
    throw codedError("generation_mismatch");
  }
  if (!isPositiveInteger(body.id)) throw codedError("invalid_request_id");
  for (const field of ["inputDeviceName", "outputDeviceName"]) {
    if (!stringWithin(body[field], 512, { nonempty: true }) ||
        body[field].trim() !== body[field] || /[\u0000-\u001f\u007f]/.test(body[field])) {
      throw codedError("invalid_audio_device_name");
    }
  }
  return body;
}

function validateGroupStartBody(body, hostGeneration) {
  return validateGroupRoomInfoBody(body, hostGeneration);
}

function validateGroupAcceptBody(body, hostGeneration) {
  return validateGroupRoomInfoBody(body, hostGeneration);
}

function validateGroupQuitBody(body, hostGeneration) {
  if (!ownKeysExactly(body, ["id", "hostGeneration", "groupId", "startRequestId"])) {
    throw codedError("invalid_request_shape");
  }
  if (!isPositiveInteger(body.hostGeneration) || body.hostGeneration !== hostGeneration) {
    throw codedError("generation_mismatch");
  }
  if (!isPositiveInteger(body.id)) throw codedError("invalid_request_id");
  if (typeof body.groupId !== "string" || Buffer.byteLength(body.groupId, "utf8") > 20 ||
      !/^[1-9][0-9]{0,19}$/.test(body.groupId)) throw codedError("invalid_group_id");
  if (!isPositiveInteger(body.startRequestId)) throw codedError("invalid_start_request_id");
  return body;
}

function trustedLoginFromParams(params) {
  if (!Array.isArray(params) || params.length !== 5 || !QQ_UID_PATTERN.test(params[0]) ||
      typeof params[1] !== "string" || !CANONICAL_UIN_PATTERN.test(params[1]) ||
      (params[2] !== "" && params[2] !== params[1]) ||
      !stringWithin(params[3], MAX_LOGIN_FIELD_BYTES, { nonempty: true }) || !path.isAbsolute(params[3]) || params[4] !== "") return null;
  // Native defaults TinyId to empty. Matching self-UIN is legacy compatibility only;
  // an arbitrary third identity field never becomes trusted login data.
  return Object.freeze({ selfUid: params[0], selfUin: params[1], accountPath: params[3] });
}

function isDenseCallbackArray(value, minimumLength, maximumLength = minimumLength) {
  if (!Array.isArray(value) || value.length < minimumLength || value.length > maximumLength) return false;
  const ownKeys = Reflect.ownKeys(value);
  if (ownKeys.length !== value.length + 1 || !ownKeys.includes("length")) return false;
  for (let index = 0; index < value.length; index += 1) {
    if (!ownKeys.includes(String(index))) return false;
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value")) return false;
  }
  return true;
}

function privateInviteRecord(value) {
  if (!isDenseCallbackArray(value, 12) || value[0] !== PRIVATE_CALL_SCENE ||
      !QQ_UID_PATTERN.test(value[1]) || !Number.isFinite(value[3]) ||
      typeof value[4] !== "string" || value[4].length < 1 || Buffer.byteLength(value[4], "utf8") > 512 ||
      (value[10] !== true && value[10] !== false && value[10] !== 0 && value[10] !== 1) ||
      typeof value[11] !== "string" ||
      Buffer.byteLength(value[11], "utf8") > MAX_ACCEPT_FIELD_BYTES) return null;
  return { peerUid: value[1], inviteRelationId: value[4] };
}

function hasVirtualAudioMarker(name) {
  if (typeof name !== "string" || Buffer.byteLength(name, "utf8") > MAX_DEVICE_NAME_BYTES) return false;
  const folded = name.toLowerCase();
  return VIRTUAL_AUDIO_MARKERS.some((marker) => folded.includes(marker));
}

function isDistinctVirtualAudioSelection(selection) {
  if (!selection || typeof selection.inputName !== "string" || typeof selection.outputName !== "string" ||
      selection.inputName.toLowerCase() === selection.outputName.toLowerCase()) return false;
  return hasVirtualAudioMarker(selection.inputName) && hasVirtualAudioMarker(selection.outputName);
}

function privateEntryRecord(value) {
  if (!isDenseCallbackArray(value, 7) || !Number.isInteger(value[0]) || value[0] < -0x80000000 || value[0] > 0x7fffffff ||
      value[1] !== PRIVATE_CALL_SCENE || typeof value[2] !== "string" || value[2].length < 1 ||
      Buffer.byteLength(value[2], "utf8") > 512 || !QQ_UID_PATTERN.test(value[3]) ||
      (value[4] !== 0 && value[4] !== 1 && value[4] !== false && value[4] !== true) ||
      typeof value[5] !== "string" || Buffer.byteLength(value[5], "utf8") > MAX_ACCEPT_FIELD_BYTES || value[6] !== 1) return null;
  return { result: value[0], relationId: value[2], peerUid: value[3] };
}

function privateTerminalRecord(value) {
  if (!isDenseCallbackArray(value, 3) || value[0] !== PRIVATE_CALL_SCENE ||
      typeof value[1] !== "string" || value[1].length < 1 || Buffer.byteLength(value[1], "utf8") > 512 ||
      typeof value[2] !== "string" || value[2].length < 1 || Buffer.byteLength(value[2], "utf8") > 512) return null;
  return { relationId: value[1] };
}

function groupTerminalRecord(value) {
  if (!isDenseCallbackArray(value, 3) || value[0] !== GROUP_ROOM_SCENE || value[1] === undefined ||
      typeof value[1] !== "string" || value[1].length < 1 || Buffer.byteLength(value[1], "utf8") > 512 ||
      typeof value[2] !== "string" || value[2].length < 1 || Buffer.byteLength(value[2], "utf8") > 512) return null;
  return { relationId: value[1] };
}

function validateAudioDeviceTail(snapshot) {
  const [currentOutput, currentInput, defaultOutput, defaultInput] = snapshot?.tail || [];
  const outputIds = new Set(snapshot?.output?.map((device) => device.id) || []);
  const inputIds = new Set(snapshot?.input?.map((device) => device.id) || []);
  if (!snapshot || snapshot.tail.length !== 4 ||
      (currentOutput !== 0 && !outputIds.has(currentOutput)) ||
      (currentInput !== 0 && !inputIds.has(currentInput)) ||
      (defaultOutput !== 0 && !outputIds.has(defaultOutput)) ||
      (defaultInput !== 0 && !inputIds.has(defaultInput))) {
    throw codedError("audio_device_metadata_invalid");
  }
  return { currentOutput, currentInput, defaultOutput, defaultInput };
}

function readinessSnapshot(provider) {
  let value;
  try {
    value = provider();
  } catch {
    return {
      ready: false,
      pluginFound: false,
      hasPostMessage: false,
      rendererGone: false,
      readinessError: true,
    };
  }
  if (typeof value === "boolean") {
    return {
      ready: value,
      pluginFound: value,
      hasPostMessage: value,
      rendererGone: false,
      rendererGoneCode: null,
      readinessError: false,
    };
  }
  const pluginFound = value?.pluginFound === true;
  const hasPostMessage = value?.hasPostMessage === true;
  const rendererGone = value?.rendererGone === true;
  return {
    ready: value?.ready === true && pluginFound && hasPostMessage && !rendererGone,
    pluginFound,
    hasPostMessage,
    rendererGone,
    rendererGoneCode: rendererGone ? safeFailureCode(value?.rendererGoneCode, "renderer_crashed") : null,
    readinessError: false,
  };
}

function safeFailureCode(errorOrCode, fallback = "host_failure") {
  const value = typeof errorOrCode === "string" ? errorOrCode : errorOrCode?.code;
  return typeof value === "string" && FAILURE_CODE_PATTERN.test(value) ? value : fallback;
}

function traceValueType(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "object") return "object";
  if (["string", "number", "boolean", "undefined"].includes(typeof value)) return typeof value;
  return "undefined";
}

function safeTraceScene(value) {
  if (Array.isArray(value)) value = value[0];
  else if (value && typeof value === "object") {
    value = Object.prototype.hasOwnProperty.call(value, "scene") ? value.scene : value.sceneId;
  }
  return Number.isSafeInteger(value) && value >= 0 && value <= 3 ? value : null;
}

function sendJson(response, statusCode, body) {
  if (response.destroyed || response.writableEnded) return;
  const bytes = Buffer.from(JSON.stringify(body), "utf8");
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": bytes.byteLength,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Connection": "close",
  });
  response.end(bytes);
}

function readJsonBody(request, maxBytes = MAX_FRAME_BYTES) {
  return new Promise((resolve, reject) => {
    const rawLength = request.headers["content-length"];
    if (rawLength !== undefined) {
      const declared = Number(rawLength);
      if (!Number.isSafeInteger(declared) || declared < 0) {
        request.resume();
        reject(codedError("invalid_content_length"));
        return;
      }
      if (declared > maxBytes) {
        request.resume();
        reject(codedError("body_too_large"));
        return;
      }
    }

    const chunks = [];
    let size = 0;
    let settled = false;
    request.on("data", (chunk) => {
      if (settled) return;
      size += chunk.byteLength;
      if (size > maxBytes) {
        settled = true;
        chunks.length = 0;
        request.resume();
        reject(codedError("body_too_large"));
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (settled) return;
      settled = true;
      try {
        const bytes = Buffer.concat(chunks, size);
        const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
        resolve(JSON.parse(text));
      } catch {
        reject(codedError("invalid_json"));
      }
    });
    request.on("error", () => {
      if (settled) return;
      settled = true;
      reject(codedError("request_read_failed"));
    });
    request.on("aborted", () => {
      if (settled) return;
      settled = true;
      reject(codedError("request_aborted"));
    });
  });
}

function parseBearer(request) {
  const value = request.headers.authorization;
  if (typeof value !== "string" || !value.startsWith("Bearer ")) return null;
  const token = value.slice("Bearer ".length);
  return token.length ? token : null;
}

function postJson({ port, token, body, timeoutMs, signal }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(codedError("host_stopped"));
      return;
    }
    const payload = Buffer.from(JSON.stringify(body), "utf8");
    const request = http.request({
      hostname: LOOPBACK,
      port,
      path: "/v1/avsdk/output",
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "Content-Length": payload.byteLength,
        "Connection": "close",
      },
      timeout: timeoutMs,
    }, (response) => {
      response.resume();
      response.once("end", () => {
        if (response.statusCode >= 200 && response.statusCode < 300) resolve({ ok: true });
        else reject(codedError("output_controller_rejected"));
      });
    });
    const onAbort = () => request.destroy(codedError("host_stopped"));
    signal?.addEventListener("abort", onAbort, { once: true });
    request.once("timeout", () => request.destroy(codedError("output_forward_timeout")));
    request.once("error", (error) => reject(error?.code === "output_forward_timeout" ? codedError("output_forward_timeout") : safeFailureCode(error, "output_forward_failed") === "host_stopped" ? codedError("host_stopped") : codedError("output_forward_failed")));
    request.once("close", () => signal?.removeEventListener("abort", onAbort));
    request.end(payload);
  });
}

function createHostTransport({
  token,
  listenPort = 6111,
  outputPort = 6110,
  hostGeneration,
  targetPid,
  invokePlugin,
  getReadiness = () => false,
  onFailure = () => {},
  outputSink,
  requestTimeoutMs = 5000,
  outputTimeoutMs = 5000,
  maxOutputQueueBytes = DEFAULT_OUTPUT_QUEUE_BYTES,
  maxConcurrentInvokes = 64,
  verifiedAudioDeviceProtocol = false,
  verifiedAudioDeviceSelectionProtocol = false,
  audioDeviceSelectionTimeoutMs = AUDIO_DEVICE_SELECT_TIMEOUT_MS,
  verifiedGroupRoomInfoProtocol = false,
  verifiedGroupCallProtocol = false,
  verifiedLocalRoomIdProtocol = false,
  repeatableGroupRoomQueries = false,
  verifiedPrivateEndProtocol = false,
  groupRoomInfoTimeoutMs = GROUP_ROOM_QUERY_TIMEOUT_MS,
  onPrivateEndTrace = () => {},
} = {}) {
  if (!validToken(token)) throw codedError("invalid_auth_token");
  if (!isPositiveInteger(hostGeneration)) throw codedError("invalid_host_generation");
  if (targetPid !== undefined && (!Number.isSafeInteger(targetPid) || targetPid < 1 || targetPid > 0x7fffffff)) {
    throw codedError("invalid_host_pid");
  }
  if (!Number.isInteger(listenPort) || listenPort < 0 || listenPort > 65535) throw codedError("invalid_listen_port");
  if (!Number.isInteger(outputPort) || outputPort < 1 || outputPort > 65535) throw codedError("invalid_output_port");
  if (typeof invokePlugin !== "function") throw codedError("invoke_plugin_required");
  if (typeof getReadiness !== "function") throw codedError("readiness_provider_required");
  if (typeof onFailure !== "function") throw codedError("failure_hook_invalid");
  if (outputSink !== undefined && typeof outputSink !== "function") throw codedError("output_sink_invalid");
  if (typeof onPrivateEndTrace !== "function") throw codedError("private_end_trace_hook_invalid");
  if (!Number.isSafeInteger(requestTimeoutMs) || requestTimeoutMs < 1 || requestTimeoutMs > 120000) throw codedError("invalid_request_timeout");
  if (!Number.isSafeInteger(outputTimeoutMs) || outputTimeoutMs < 1 || outputTimeoutMs > 120000) throw codedError("invalid_output_timeout");
  if (!Number.isSafeInteger(maxOutputQueueBytes) || maxOutputQueueBytes < 1 || maxOutputQueueBytes > 64 * MAX_FRAME_BYTES) throw codedError("invalid_output_queue_limit");
  if (!Number.isSafeInteger(maxConcurrentInvokes) || maxConcurrentInvokes < 1 || maxConcurrentInvokes > 256) throw codedError("invalid_invoke_concurrency");
  if (typeof verifiedAudioDeviceProtocol !== "boolean") throw codedError("audio_protocol_capability_invalid");
  if (typeof verifiedAudioDeviceSelectionProtocol !== "boolean") throw codedError("audio_selection_protocol_capability_invalid");
  if (typeof verifiedGroupRoomInfoProtocol !== "boolean") throw codedError("group_room_capability_invalid");
  if (typeof verifiedGroupCallProtocol !== "boolean") throw codedError("group_call_capability_invalid");
  if (typeof verifiedLocalRoomIdProtocol !== "boolean") throw codedError("group_local_room_capability_invalid");
  if (typeof repeatableGroupRoomQueries !== "boolean") throw codedError("group_room_repeatable_invalid");
  if (typeof verifiedPrivateEndProtocol !== "boolean") throw codedError("private_end_capability_invalid");
  if (!Number.isSafeInteger(audioDeviceSelectionTimeoutMs) || audioDeviceSelectionTimeoutMs < 50 ||
      audioDeviceSelectionTimeoutMs > AUDIO_DEVICE_SELECT_TIMEOUT_MS) throw codedError("invalid_audio_selection_timeout");
  if (!Number.isSafeInteger(groupRoomInfoTimeoutMs) || groupRoomInfoTimeoutMs < 50 || groupRoomInfoTimeoutMs > 120_000) {
    throw codedError("group_room_timeout_invalid");
  }

  const sendOutput = outputSink || ((frame, options) => postJson({
    port: outputPort,
    token,
    body: frame,
    timeoutMs: outputTimeoutMs,
    signal: options.signal,
  }));

  let server = null;
  let startPromise = null;
  let actualListenPort = listenPort;
  let stopped = false;
  let outputFailed = false;
  let outputDrainPromise = null;
  let outputInFlight = null;
  let queuedOutputBytes = 0;
  let invokeAttempts = 0;
  let invokePosted = 0;
  let outputForwarded = 0;
  let lastFailureCode = null;
  const outputQueue = [];
  const activeInvokes = new Set();
  let audioDeviceOutputSequence = 0;
  let audioDeviceQueryTicket = null;
  let audioDeviceQueryQuarantined = false;
  let audioDeviceSelectionInFlight = false;
  let audioDeviceSelectionQuarantined = false;
  let audioDeviceSelectionReady = false;
  let selectedAudioDeviceNames = null;
  let trustedLogin = null;
  let groupLocalQuitObservation = null;
  let loginAttempt = null;
  let sdkInfoStartupState = "idle";
  let sdkInfoStartupPromise = null;
  const uncertainLoginRequestIds = new Set();
  let uncertainLoginRequestHistoryFull = false;
  let privateCall = null;
  let privateRoomReadbackIdentity = null;
  let confirmedGroupRoomReadbackIdentity = null;
  let privateNativeStateSequence = 0;
  let privateRoomEndObservation = null;
  let privateRejectObservation = null;
  const nativeControlPosts = new Map();
  const nativeControlObservations = Object.create(null);
  // Monotonic for this Host incarnation, including uncertain room posts.
  let roomAdmissionMayHavePosted = false;
  const privateRejectRequestIds = new Set();
  let groupReservation = null;
  let groupBusyPrivateInvitesObserved = 0;
  let groupTerminalSequence = 0;
  let outputFrameSequence = 0;
  let activeTypedGroupRoomQuery = null;
  let lastSuccessfulTypedGroupRoomQuery = null;
  let groupInviteObservation = null;
  let pendingGroupInvite = null;
  let groupInviteBlockedThroughQueryOutputSequence = 0;
  const consumedGroupInviteFingerprints = new Map();
  const privateEndTraceWindows = [];
  const privateEndRequestHistory = new Map();
  let nextPrivateEndTraceId = 0;

  function fail(code) {
    const safeCode = safeFailureCode(code);
    lastFailureCode = safeCode;
    try {
      onFailure({ code: safeCode, hostGeneration });
    } catch {
      // Failure observers must not disrupt transport cleanup or request handling.
    }
  }

  function clearAudioDeviceSelectionProof() {
    audioDeviceSelectionReady = false;
    selectedAudioDeviceNames = null;
  }

  function notifyPrivateEndTrace(event) {
    try {
      const result = onPrivateEndTrace(event);
      if (result && typeof result.then === "function") void Promise.resolve(result).catch(() => {});
    } catch { /* A diagnostic observer cannot delay or change Host behavior. */ }
  }

  function privateEndTraceIdentity(trace) {
    return {
      traceId: trace.traceId,
      requestId: trace.requestId,
      command: trace.command,
      hostGeneration,
      targetPid: targetPid ?? process.pid,
    };
  }

  function emitPrivateEndTraceStage(trace, postedStage, failureCode = null) {
    if (!trace || trace.closed) return;
    trace.postedStage = postedStage;
    const event = {
      type: "post_stage",
      ...privateEndTraceIdentity(trace),
      atMs: Date.now(),
      postedStage,
    };
    if (typeof failureCode === "string" && FAILURE_CODE_PATTERN.test(failureCode)) event.failureCode = failureCode;
    notifyPrivateEndTrace(event);
  }

  function closePrivateEndTraceWindow(trace, reason) {
    if (!trace || trace.closed) return;
    trace.closed = true;
    clearTimeout(trace.timer);
    const elapsedMs = Math.min(PRIVATE_END_TRACE_WINDOW_MS, Math.max(0, Date.now() - trace.startedAtMs));
    notifyPrivateEndTrace({
      type: "window_end",
      ...privateEndTraceIdentity(trace),
      atMs: Date.now(),
      elapsedMs,
      reason,
      commandCounts: [...trace.commandCounts].map(([command, count]) => ({ command, count })),
      otherCommandRecords: trace.otherCommandRecords,
      network20001Count: trace.network20001Count,
      droppedRelevantRecords: trace.droppedRelevantRecords,
    });
    const index = privateEndTraceWindows.indexOf(trace);
    if (index >= 0) privateEndTraceWindows.splice(index, 1);
  }

  function expirePrivateEndTraceWindows(now = Date.now()) {
    for (const trace of [...privateEndTraceWindows]) {
      if (!trace.closed && now >= trace.expiresAtMs) closePrivateEndTraceWindow(trace, "window_elapsed");
    }
  }

  function beginPrivateEndTrace(body) {
    if (!verifiedPrivateEndProtocol || ![QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(body.command)) return null;
    expirePrivateEndTraceWindows();
    while (privateEndTraceWindows.length >= PRIVATE_END_TRACE_MAX_WINDOWS) {
      const oldest = privateEndTraceWindows[0];
      if (!oldest) break;
      closePrivateEndTraceWindow(oldest, "window_superseded");
    }
    nextPrivateEndTraceId = nextPrivateEndTraceId >= Number.MAX_SAFE_INTEGER ? 1 : nextPrivateEndTraceId + 1;
    while (privateEndTraceWindows.some((trace) => trace.traceId === nextPrivateEndTraceId)) {
      nextPrivateEndTraceId = nextPrivateEndTraceId >= Number.MAX_SAFE_INTEGER ? 1 : nextPrivateEndTraceId + 1;
    }
    const startedAtMs = Date.now();
    const requestKey = `${body.command}:${body.id}`;
    const correlationEligible = !privateEndRequestHistory.has(requestKey);
    privateEndRequestHistory.delete(requestKey);
    privateEndRequestHistory.set(requestKey, startedAtMs);
    while (privateEndRequestHistory.size > 256) privateEndRequestHistory.delete(privateEndRequestHistory.keys().next().value);
    const params = body.command === QRTC_COMMAND_QUIT
      ? { scene: body.params[0], reason: body.params[1] }
      : body.command === QRTC_COMMAND_REJECT
      ? {
        scene: body.params[0],
        peerUidType: "string",
        peerUidLength: Buffer.byteLength(body.params[1], "utf8"),
        rejectReason: body.params[2],
      }
      : {
        scene: body.params[0],
        peerUidType: "string",
        peerUidLength: Buffer.byteLength(body.params[1], "utf8"),
        closeReason: body.params[2],
      };
    const trace = {
      traceId: nextPrivateEndTraceId,
      requestId: body.id,
      command: body.command,
      correlationEligible,
      startedAtMs,
      expiresAtMs: startedAtMs + PRIVATE_END_TRACE_WINDOW_MS,
      postedStage: "dispatching",
      commandCounts: new Map(),
      otherCommandRecords: 0,
      network20001Count: 0,
      droppedRelevantRecords: 0,
      relevantOutputCount: 0,
      closed: false,
      timer: null,
    };
    privateEndTraceWindows.push(trace);
    notifyPrivateEndTrace({
      type: "request",
      ...privateEndTraceIdentity(trace),
      atMs: startedAtMs,
      postedStage: "dispatching",
      correlationEligible,
      params,
    });
    trace.timer = setTimeout(() => closePrivateEndTraceWindow(trace, "window_elapsed"), PRIVATE_END_TRACE_WINDOW_MS);
    trace.timer.unref?.();
    return trace;
  }

  function tracePrivateEndOutput(frame, rawIdState = "unknown") {
    expirePrivateEndTraceWindows();
    if (!privateEndTraceWindows.length) return;
    const active = privateEndTraceWindows.filter((trace) => !trace.closed);
    if (!active.length) return;
    for (const trace of active) {
      if (frame.command === 20001) trace.network20001Count += 1;
      if (trace.commandCounts.has(frame.command)) trace.commandCounts.set(frame.command, trace.commandCounts.get(frame.command) + 1);
      else if (trace.commandCounts.size < PRIVATE_END_TRACE_MAX_COMMANDS) trace.commandCounts.set(frame.command, 1);
      else trace.otherCommandRecords += 1;
    }
    if (!PRIVATE_END_TRACE_OUTPUT_COMMANDS.has(frame.command)) return;
    let windowsWithOutputCapacity = 0;
    for (const trace of active) {
      if (trace.relevantOutputCount >= PRIVATE_END_TRACE_MAX_OUTPUTS_PER_WINDOW) {
        trace.droppedRelevantRecords += 1;
        continue;
      }
      trace.relevantOutputCount += 1;
      windowsWithOutputCapacity += 1;
    }
    if (!windowsWithOutputCapacity) return;
    const candidates = [QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(frame.command) && frame.id > 0
      ? active.filter((trace) => trace.command === frame.command && trace.requestId === frame.id)
      : [];
    const matching = candidates.filter((trace) => trace.correlationEligible);
    const association = candidates.length > 1
      ? "multiple_id_matches"
      : candidates.length === 1 && matching.length === 1
      ? "request_id_match"
        : [QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(frame.command) && frame.id === 0
          ? "id_zero_uncorrelated"
          : [QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(frame.command) && frame.id > 0
            ? "no_matching_request_id"
            : active.length === 1 ? "temporal_only" : "multiple_active_windows";
    const value = frame.value;
    const valueType = traceValueType(value);
    const event = {
      type: "sdk_output",
      atMs: Date.now(),
      hostGeneration,
      targetPid: targetPid ?? process.pid,
      command: frame.command,
      normalizedId: frame.id,
      rawIdState: ["valid", "missing", "invalid", "unknown"].includes(rawIdState) ? rawIdState : "unknown",
      valueType,
      association,
      activeWindowCount: active.length,
    };
    if (candidates.length === 1 && matching.length === 1) {
      event.traceId = matching[0].traceId;
      event.requestId = matching[0].requestId;
    }
    if (Array.isArray(value)) {
      event.arrayLength = value.length;
      event.elementTypes = value.slice(0, 8).map(traceValueType);
    }
    if ([QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(frame.command)) {
      if (Array.isArray(value) && Number.isSafeInteger(value[0]) && value[0] >= -0x80000000 && value[0] <= 0x7fffffff) {
        event.ackReturnCode = value[0];
      }
      event.ackSecondEmptyString = Array.isArray(value) && typeof value[1] === "string" && value[1] === "";
    }
    if (frame.command === 20011) {
      const scene = safeTraceScene(value);
      if (scene !== null) event.scene = scene;
    }
    if (frame.command === 20017 && Array.isArray(value)) {
      for (const [field, index] of [["closeReason", 0], ["destroyReason", 1]]) {
        if (Number.isSafeInteger(value[index]) && value[index] >= 0 && value[index] <= 255) event[field] = value[index];
      }
      event.inRoomValueKind = value[2] === false ? "false_boolean" : value[2] === true ? "true_boolean"
        : value[2] === 0 ? "zero_integer" : value[2] === 1 ? "one_integer" : "unknown";
    }
    notifyPrivateEndTrace(event);
  }

  function snapshot(readinessOverride) {
    const readiness = readinessOverride || readinessSnapshot(getReadiness);
    const reservation = groupReservation;
    const entryProof = reservation?.nativeEntryProof;
    const groupCallNativeEntryVerified = !stopped && Boolean(reservation?.startMayHavePosted && entryProof &&
      entryProof.groupId === reservation.groupId && entryProof.startRequestId === reservation.startRequestId);
    return {
      hostGeneration,
      ...(targetPid !== undefined ? { targetPid } : {}),
      ready: readiness.ready && !stopped && !outputFailed,
      pluginFound: readiness.pluginFound,
      hasPostMessage: readiness.hasPostMessage,
      rendererGone: readiness.rendererGone,
      readinessError: readiness.readinessError,
      stopped,
      listening: Boolean(server?.listening),
      listenHost: LOOPBACK,
      listenPort: actualListenPort,
      outputPort,
      invokeAttempts,
      invokePosted,
      activeInvokes: activeInvokes.size,
      outputForwarded,
      queuedOutputBytes,
      queuedOutputFrames: outputQueue.length,
      outputFailed,
      audioDeviceProtocolVerified: verifiedAudioDeviceProtocol,
      audioDeviceSelectionProtocolVerified: verifiedAudioDeviceSelectionProtocol,
      audioDeviceSelectionInFlight,
      audioDeviceSelectionQuarantined,
      audioDeviceQueryPending: Boolean(audioDeviceQueryTicket),
      audioDeviceQueryQuarantined,
      groupRoomInfoProtocolVerified: verifiedGroupRoomInfoProtocol,
      groupCallProtocolAvailable: verifiedGroupCallProtocol,
      groupLocalQuitProofAvailable: verifiedGroupCallProtocol && verifiedLocalRoomIdProtocol,
      groupLocalQuitWitnessAvailable: Boolean(groupReservation?.localRoomWitness),
      groupLocalQuitObservation: !stopped ? groupLocalQuitObservation : null,
      groupBusyPrivateInvitesObserved,
      groupRoomInfoRepeatable: repeatableGroupRoomQueries,
      audioDeviceSelectionReady,
      // `ready` is only the renderer's post receipt; native command-57 output is not a login proof.
      sdkInfoStartupState,
      loginAcknowledged: trustedLogin !== null,
      groupCallReserved: groupReservation !== null,
      groupCallState: groupReservation?.state || "idle",
      groupCallQuarantined: groupReservation?.state === "quarantined",
      groupCallNativeEntryVerified,
      groupNativeObservation: !stopped ? cloneGroupNativeObservation(reservation?.groupNativeObservation) : null,
      groupInviteObservation: !stopped ? cloneGroupInviteObservation(groupInviteObservation) : null,
      groupRoomInfoQueryPending: groupRoomInfoQuery.status().pending,
      groupRoomInfoQueryQuarantined: groupRoomInfoQuery.status().quarantined,
      groupRoomInfoQueryQuarantineCode: groupRoomInfoQuery.status().quarantineCode,
      groupRoomQueryObservation: groupRoomInfoQuery.observation(),
      groupQuitPosted: reservation?.quitPosted === true,
      groupQuitAcknowledged: Boolean(reservation?.quitAckProof),
      groupQuitTerminalObserved: Boolean(reservation?.terminalAfterQuit),
      privateEndProtocolVerified: verifiedPrivateEndProtocol,
      roomAdmissionMayHavePosted,
      privateRoomEndObservation,
      privateRejectObservation,
      nativeControlObservations: Object.fromEntries(Object.entries(nativeControlObservations).map(([key, value]) => [key, { ...value }])),
      lastFailureCode: lastFailureCode || readiness.rendererGoneCode || null,
    };
  }

  function currentReadiness() {
    const readiness = readinessSnapshot(getReadiness);
    const status = snapshot(readiness);
    if (stopped) return { ready: false, code: "host_stopped", status };
    if (readiness.rendererGone) return { ready: false, code: readiness.rendererGoneCode || "renderer_crashed", status };
    if (status.readinessError) return { ready: false, code: "readiness_unavailable", status };
    if (outputFailed) return { ready: false, code: "output_forward_failed", status };
    if (!status.ready) return { ready: false, code: "host_not_ready", status };
    return { ready: true, code: null, status };
  }

  function authenticate(request) {
    return equalToken(token, parseBearer(request));
  }

  function responseForUnavailable(result) {
    const statusCode = result.code === "host_stopped" ? 503 : result.code === "renderer_crashed" ? 503 : 503;
    return { statusCode, body: { ok: false, code: result.code, status: result.status } };
  }

  function requestErrorStatus(code) {
    if (code === "body_too_large") return 413;
    if (code === "generation_mismatch") return 409;
    if (code === "invoke_timeout") return 504;
    if (code === "host_not_ready" || code === "renderer_crashed" || code === "renderer_closed" || code === "host_stopped" ||
        code === "readiness_unavailable" || code === "output_forward_failed") return 503;
    if (code === "host_busy") return 429;
    if (code === "audio_device_protocol_unverified" || code === "audio_device_selection_unverified" ||
        code === "group_room_info_protocol_unverified" || code === "group_call_protocol_unverified" ||
        code === "private_end_protocol_unverified") return 501;
    if (code === "audio_device_selection_timeout" || code === "group_room_query_timeout") return 504;
    if (["audio_device_selection_busy", "audio_device_selection_quarantined", "audio_device_not_found",
      "audio_device_name_ambiguous", "audio_device_not_selectable",
      "audio_device_selection_mismatch", "audio_device_map_changed", "audio_device_query_busy",
      "audio_device_query_quarantined", "audio_device_metadata_invalid", "group_room_query_exhausted",
      "group_room_query_busy", "group_room_query_quarantined", "group_call_reserved", "group_start_busy",
      "group_call_unowned", "group_room_not_existing", "group_private_call_pending",
      "group_login_unverified", "group_audio_device_selection_required", "group_audio_device_route_not_virtual",
      "group_invite_unavailable", "group_invite_stale", "group_invite_group_mismatch",
      "group_accept_identity_changed", "group_accept_unowned", "private_call_pending",
      "private_local_room_unowned", "private_local_room_context_changed",
      "login_request_id_uncertain", "login_request_history_full", "host_busy"].includes(code)) return 409;
    if (code === "command_not_allowed" || code === "invalid_params" || code === "invalid_request_shape" ||
        code === "invalid_request_id" || code === "invalid_group_id" || code === "params_too_large" || code === "invalid_json" ||
        code === "invalid_start_request_id" || code === "invalid_content_length" || code === "content_type_required" ||
        code === "invalid_audio_device_name") return 400;
    if (code === "plugin_post_failed" || code === "plugin_post_not_confirmed" || code === "sdk_info_post_uncertain" ||
        code === "group_room_metadata_invalid" || code === "group_room_query_post_failed") return 502;
    return 500;
  }

  function timeoutOperation(promise, timeoutMs, controller, timeoutCode) {
    let timer = null;
    let abortHandler = null;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(codedError(timeoutCode));
        controller.abort();
      }, timeoutMs);
      timer.unref?.();
    });
    const aborted = new Promise((_, reject) => {
      abortHandler = () => reject(codedError("host_stopped"));
      controller.signal.addEventListener("abort", abortHandler, { once: true });
    });
    return Promise.race([promise, timeout, aborted]).finally(() => {
      if (timer) clearTimeout(timer);
      if (abortHandler) controller.signal.removeEventListener("abort", abortHandler);
    });
  }

  async function postPluginCommand({ command, id, params, timeoutMs = requestTimeoutMs, selectorOwned = false,
    nonfatal = false, onDispatch, groupAcceptReservation = null }, privateEndTrace = null) {
    const readiness = currentReadiness();
    if (!readiness.ready) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", readiness.code);
      throw codedError(readiness.code);
    }
    if (stopped) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "host_stopped");
      throw codedError("host_stopped");
    }
    if (audioDeviceSelectionQuarantined && command !== 55) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "audio_device_selection_quarantined");
      throw codedError("audio_device_selection_quarantined");
    }
    if (audioDeviceSelectionInFlight && !selectorOwned && (command === 1 || command === 5 || command === AUDIO_DEVICE_QUERY_COMMAND)) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "audio_device_selection_busy");
      throw codedError("audio_device_selection_busy");
    }
    const groupAcceptOwned = command === GROUP_ACCEPT_COMMAND && groupAcceptReservation === groupReservation &&
      groupAcceptReservation?.operation === "group_accept" && groupAcceptReservation.state === "starting" &&
      !groupAcceptReservation.startMayHavePosted;
    if (groupAcceptReservation && !groupAcceptOwned) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "group_accept_unowned");
      throw codedError("group_accept_unowned");
    }
    if (groupReservation && (command === 1 || command === GROUP_ACCEPT_COMMAND) && !groupAcceptOwned) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "group_call_reserved");
      throw codedError("group_call_reserved");
    }
    if (onDispatch !== undefined && typeof onDispatch !== "function") throw codedError("invoke_dispatch_hook_invalid");
    if (activeInvokes.size >= maxConcurrentInvokes) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "host_busy");
      throw codedError("host_busy");
    }
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1) {
      emitPrivateEndTraceStage(privateEndTrace, "preflight_rejected", "audio_device_selection_timeout");
      throw codedError("audio_device_selection_timeout");
    }

    const controller = new AbortController();
    const deadlineMs = Date.now() + timeoutMs;
    const entry = { controller, command };
    activeInvokes.add(entry);
    invokeAttempts += 1;
    try {
      onDispatch?.();
      if (command === GROUP_START_COMMAND || command === GROUP_ACCEPT_COMMAND) roomAdmissionMayHavePosted = true;
      if (command === 5 || command === 55) {
        const key = `${command}:${id}`;
        const reused = nativeControlPosts.has(key);
        nativeControlPosts.set(key, { atMs: Date.now(), reused, observed: false });
        if (nativeControlPosts.size > 256) nativeControlPosts.delete(nativeControlPosts.keys().next().value);
      }
      emitPrivateEndTraceStage(privateEndTrace, "renderer_dispatch_started");
      const operation = Promise.resolve().then(() => invokePlugin({
        command,
        id,
        params,
        hostGeneration,
        deadlineMs,
        signal: controller.signal,
      }));
      const result = await timeoutOperation(operation, timeoutMs, controller, "invoke_timeout");
      if (stopped) throw codedError("host_stopped");
      const confirmed = result && typeof result === "object" &&
        result.posted === true && result.id === id && result.command === command;
      if (!confirmed) throw codedError("plugin_post_not_confirmed");
      invokePosted += 1;
      emitPrivateEndTraceStage(privateEndTrace, "renderer_posted");
      return { posted: true, id, command };
    } catch (error) {
      const code = safeFailureCode(error, "plugin_post_failed");
      emitPrivateEndTraceStage(privateEndTrace, code === "plugin_post_not_confirmed" ? "post_receipt_invalid" : "invoke_failed", code);
      if (!nonfatal) {
        if (code === "invoke_timeout") fail("invoke_timeout");
        else if (code !== "host_stopped") fail(code === "plugin_post_not_confirmed" ? code : "plugin_post_failed");
      }
      throw codedError(code === "host_stopped" ? code : code === "invoke_timeout" ? code : "plugin_post_failed");
    } finally {
      activeInvokes.delete(entry);
    }
  }

  function finishLoginAttempt(attempt) {
    if (!attempt || loginAttempt !== attempt || !attempt.posted || attempt.acknowledged !== true) return;
    trustedLogin = attempt.identity;
    loginAttempt = null;
  }

  function rememberUncertainLoginRequestId(id) {
    if (uncertainLoginRequestIds.has(id)) return;
    if (uncertainLoginRequestIds.size >= MAX_UNCERTAIN_LOGIN_REQUEST_IDS) {
      uncertainLoginRequestHistoryFull = true;
      return;
    }
    uncertainLoginRequestIds.add(id);
  }

  function ensureSDKInfoStartup(requestId) {
    if (sdkInfoStartupState === "ready") return Promise.resolve();
    if (sdkInfoStartupState === "stopped" || stopped) return Promise.reject(codedError("host_stopped"));
    if (sdkInfoStartupState === "uncertain") return Promise.reject(codedError("sdk_info_post_uncertain"));
    if (sdkInfoStartupState === "in_flight" && sdkInfoStartupPromise) return sdkInfoStartupPromise;
    if (groupReservation) return Promise.reject(codedError("group_call_reserved"));
    if (privateCall) return Promise.reject(codedError("private_call_pending"));
    if (audioDeviceSelectionInFlight) return Promise.reject(codedError("audio_device_selection_busy"));

    sdkInfoStartupState = "in_flight";
    let dispatchStarted = false;
    const operation = (async () => {
      try {
        await postPluginCommand({
          command: SDK_INFO_COMMAND,
          id: requestId,
          params: [...SDK_INFO_PARAMS],
          onDispatch() { dispatchStarted = true; },
        });
        if (stopped) throw codedError("host_stopped");
        sdkInfoStartupState = "ready";
      } catch (error) {
        if (stopped) sdkInfoStartupState = "stopped";
        else sdkInfoStartupState = dispatchStarted ? "uncertain" : "idle";
        if (dispatchStarted && !stopped) throw codedError("sdk_info_post_uncertain");
        throw error;
      } finally {
        if (sdkInfoStartupPromise === operation) sdkInfoStartupPromise = null;
      }
    })();
    sdkInfoStartupPromise = operation;
    return operation;
  }

  function observeLoginAck(frame) {
    const attempt = loginAttempt;
    if (!attempt || frame.command !== 1 || frame.id !== attempt.id || attempt.acknowledged !== null) return;
    if (!isDenseCallbackArray(frame.value, 2) || frame.value.length !== 2 ||
        !Number.isInteger(frame.value[0]) || frame.value[0] < -0x80000000 || frame.value[0] > 0x7fffffff ||
        typeof frame.value[1] !== "string") return;
    if (frame.value[0] !== 0 || frame.value[1] !== "") {
      attempt.acknowledged = false;
      if (loginAttempt === attempt) loginAttempt = null;
      trustedLogin = null;
      return;
    }
    attempt.acknowledged = true;
    finishLoginAttempt(attempt);
  }

  function markPrivateCallPending(record) {
    privateNativeStateSequence += 1;
    blockPendingGroupInvite();
    if (!privateCall) {
      privateCall = {
        peerUid: record.peerUid,
        inviteRelationId: record.inviteRelationId || null,
        entryRelationId: record.entryRelationId || null,
        ambiguous: false,
      };
    } else if (privateCall.ambiguous ||
        (privateCall.peerUid && record.peerUid && privateCall.peerUid !== record.peerUid) ||
        (privateCall.inviteRelationId && record.inviteRelationId && privateCall.inviteRelationId !== record.inviteRelationId) ||
        (privateCall.entryRelationId && record.entryRelationId && privateCall.entryRelationId !== record.entryRelationId)) {
      privateCall.ambiguous = true;
      privateCall.entryRelationId = null;
    } else {
      privateCall.peerUid = privateCall.peerUid || record.peerUid;
      privateCall.inviteRelationId = privateCall.inviteRelationId || record.inviteRelationId || null;
      privateCall.entryRelationId = privateCall.entryRelationId || record.entryRelationId || null;
    }
    if (groupReservation?.state === "starting_post" && groupReservation.startMayHavePosted) {
      groupReservation.state = "quarantined";
      groupReservation.quarantineCode = "private_call_observed_during_start";
    }
  }

  function observePrivateCallOutput(frame) {
    const rejectWitness = privateCall?.rejectWitness;
    if (frame.command === QRTC_COMMAND_REJECT && rejectWitness && frame.id === rejectWitness.requestId) {
      rejectWitness.acknowledged = Array.isArray(frame.value) && frame.value.length === 2 &&
        frame.value[0] === 0 && frame.value[1] === "";
    }
    if (frame.id !== 0) return;
    if (frame.command === PRIVATE_INVITE_OUTPUT_COMMAND) {
      const invite = privateInviteRecord(frame.value);
      if (invite) {
        const group = groupReservation;
        // A scene-1 invitation is not room entry. Cmd5 remains blocked by the
        // owned group reservation; do not let its busy notification replace the
        // exact, independently verified group room used by Quit readback.
        const enteredGroupOwnsRoom = !privateCall && group && trustedLogin && group.identity === trustedLogin &&
          ["reserved", "quitting"].includes(group.state) && group.localEntryEpochEligible === true &&
          Number.isSafeInteger(group.nativeEntrySequence) && group.nativeEntryProof?.groupId === group.groupId &&
          group.nativeEntryProof.startRequestId === group.startRequestId;
        if (enteredGroupOwnsRoom) {
          groupBusyPrivateInvitesObserved = Math.min(groupBusyPrivateInvitesObserved + 1, 1_000_000);
        } else {
          markPrivateCallPending(invite);
        }
      }
      return;
    }
    if (frame.command === GROUP_ENTER_OUTPUT_COMMAND) {
      const entry = privateEntryRecord(frame.value);
      if (!entry || entry.result !== 0) return;
      markPrivateCallPending({ peerUid: entry.peerUid, entryRelationId: entry.relationId });
      // A successful typed private SDK entry establishes that this login's
      // room manager was initialized; no manual diagnostic is required.
      if (trustedLogin) privateRoomReadbackIdentity = trustedLogin;
      return;
    }
    if (frame.command !== GROUP_TERMINAL_OUTPUT_COMMAND || !privateCall || privateCall.ambiguous ||
        !privateCall.entryRelationId) return;
    const terminal = privateTerminalRecord(frame.value);
    if (terminal && terminal.relationId === privateCall.entryRelationId) {
      privateNativeStateSequence += 1;
      privateCall = null;
    }
  }

  function observeGroupCallOutput(frame) {
    const reservation = groupReservation;
    if (reservation?.startMayHavePosted && reservation.groupNativeObservation) {
      observeGroupNativeOutput(frame, reservation);
    }
    if (frame.command === GROUP_QUIT_COMMAND) {
      if (reservation?.quitMayHavePosted && reservation.nativeEntryProof &&
          frame.id === reservation.quitRequestId && Number.isSafeInteger(frame.id) && frame.id > 0 &&
          Array.isArray(frame.value) && frame.value.length === 2 && frame.value[0] === 0 &&
          typeof frame.value[1] === "string" && !reservation.terminalAfterQuit && !reservation.quitAckProof) {
        reservation.quitAckProof = Object.freeze({
          kind: "owned_quit_ack", quitRequestId: frame.id, startRequestId: reservation.startRequestId,
        });
      }
      return;
    }
    if (frame.command === GROUP_ENTER_OUTPUT_COMMAND) {
      if (!reservation || frame.id !== 0 || !reservation.startMayHavePosted) return;
      reservation.nativeEntrySequence = outputFrameSequence;
      const entry = parseGroupEnterCallback(frame.value, reservation.groupId);
      reservation.localEntryEpochEligible = entry.matched === true;
      if (entry.matched && !reservation.nativeEntryProof) {
        reservation.nativeEntryProof = Object.freeze({
          groupId: reservation.groupId,
          startRequestId: reservation.startRequestId,
        });
      } else if (entry.unexpectedCreator && !reservation.nativeCreatorEntryProof) {
        reservation.nativeCreatorEntryProof = Object.freeze({
          groupId: reservation.groupId,
          startRequestId: reservation.startRequestId,
        });
      }
      return;
    }
    if (frame.command !== GROUP_TERMINAL_OUTPUT_COMMAND) return;
    if (!reservation || frame.id !== 0) return;
    const terminal = groupTerminalRecord(frame.value);
    if (!terminal || terminal.relationId !== reservation.groupId) return;
    // A terminal already accepted for this exact ownership proof is a
    // duplicate. Keep its ticket stable while the corresponding room query is
    // in flight, and let unrelated/private callbacks leave the sequence alone.
    if ((reservation.quitMayHavePosted && reservation.terminalAfterQuit) || reservation.remoteTerminalProof) return;
    groupTerminalSequence += 1;
    if (reservation.quitMayHavePosted && groupTerminalSequence > reservation.quitTerminalSequenceStart) {
      if (!reservation.terminalAfterQuit) {
        reservation.terminalAfterQuit = Object.freeze({
          kind: "owned_quit",
          sequence: groupTerminalSequence,
        });
      }
      return;
    }
    if (reservation.nativeEntryProof && !reservation.remoteTerminalProof) {
      reservation.remoteTerminalProof = Object.freeze({
        kind: "remote_end",
        sequence: groupTerminalSequence,
        entryProof: reservation.nativeEntryProof,
      });
    }
  }

  function commitTypedGroupRoomQueryIfComplete(query) {
    if (activeTypedGroupRoomQuery !== query || !query.commandPosted || !query.positiveFullRoom ||
        !Number.isSafeInteger(query.roomOutputSequence) || !Number.isSafeInteger(query.roomOutputSeenAtMs)) return;
    lastSuccessfulTypedGroupRoomQuery = {
      groupId: query.groupId,
      seenAtMs: query.roomOutputSeenAtMs,
      outputSequence: query.roomOutputSequence,
      requestId: query.requestId,
      queryRef: query,
    };
  }

  function blockPendingGroupInvite(ticket = pendingGroupInvite) {
    if (!ticket) return;
    groupInviteBlockedThroughQueryOutputSequence = Math.max(
      groupInviteBlockedThroughQueryOutputSequence,
      ticket.roomQueryOutputSequence,
    );
    if (pendingGroupInvite === ticket) pendingGroupInvite = null;
  }

  function rememberConsumedGroupInvite(ticket) {
    if (!ticket) return;
    consumedGroupInviteFingerprints.delete(ticket.fingerprint);
    // Keep this replay fence for the Host generation. Invitation tickets
    // expire after 30 seconds, but a delayed duplicate callback must not turn
    // the same SDK sequence into a fresh ticket after a new room query.
    consumedGroupInviteFingerprints.set(ticket.fingerprint, true);
    while (consumedGroupInviteFingerprints.size > GROUP_INVITE_CONSUMED_FINGERPRINT_LIMIT) {
      const oldest = consumedGroupInviteFingerprints.keys().next().value;
      consumedGroupInviteFingerprints.delete(oldest);
    }
  }

  function observeTypedGroupRoomQueryOutput(frame, outputSequence) {
    const query = activeTypedGroupRoomQuery;
    if (!query || frame.command !== GROUP_ROOM_INFO_OUTPUT_COMMAND || frame.id !== 0 ||
        outputSequence <= query.requestOutputSequence) return;
    let parsed;
    try { parsed = parseGroupRoomInfoOutput(frame.value, query.groupId); }
    catch { return; }
    if (!parsed.matched) return;
    query.roomOutputSequence = outputSequence;
    query.roomOutputSeenAtMs = Date.now();
    query.positiveFullRoom = parsed.roomInfo.userNum > 0 &&
      parsed.roomInfo.userNum === parsed.roomInfo.userList.length;
    commitTypedGroupRoomQueryIfComplete(query);
  }

  function observeGroupInviteOutput(frame, outputSequence) {
    if (frame.command !== GROUP_INVITE_OUTPUT_COMMAND || frame.id !== 0) return;
    const observedAtMs = Date.now();
    const diagnostic = groupInviteDiagnostic(
      frame.value,
      lastSuccessfulTypedGroupRoomQuery,
      outputSequence,
      observedAtMs,
    );
    if (diagnostic) groupInviteObservation = diagnostic;

    const invite = groupAcceptInviteRecord(frame.value, trustedLogin);
    if (!invite) return;
    if (pendingGroupInvite) {
      blockPendingGroupInvite();
      return;
    }

    const roomQuery = lastSuccessfulTypedGroupRoomQuery;
    const roomQueryAgeMs = roomQuery ? observedAtMs - roomQuery.seenAtMs : -1;
    if (!roomQuery || roomQueryAgeMs < 0 || roomQueryAgeMs > GROUP_INVITE_ACCEPT_TTL_MS ||
        outputSequence <= roomQuery.outputSequence || invite.groupId !== roomQuery.groupId ||
        roomQuery.outputSequence <= groupInviteBlockedThroughQueryOutputSequence ||
        !verifiedGroupCallProtocol || !verifiedGroupRoomInfoProtocol || !currentReadiness().ready ||
        !audioDeviceSelectionReady || !selectedAudioDeviceNames || groupReservation || privateCall ||
        audioDeviceSelectionInFlight || [...activeInvokes].some((entry) => entry.command === 1 || entry.command === GROUP_ACCEPT_COMMAND)) {
      return;
    }

    if (consumedGroupInviteFingerprints.has(invite.fingerprint)) {
      groupInviteBlockedThroughQueryOutputSequence = Math.max(
        groupInviteBlockedThroughQueryOutputSequence,
        roomQuery.outputSequence,
      );
      return;
    }

    pendingGroupInvite = {
      inviterUid: invite.inviterUid,
      groupId: invite.groupId,
      receivedAtMs: observedAtMs,
      outputSequence,
      loginIdentityRef: trustedLogin,
      roomQueryOutputSequence: roomQuery.outputSequence,
      roomQuerySeenAtMs: roomQuery.seenAtMs,
      fingerprint: invite.fingerprint,
    };
  }

  async function queryTypedGroupRoomInfo(body, { preserveGroupInviteTicket = null } = {}) {
    if (pendingGroupInvite && pendingGroupInvite !== preserveGroupInviteTicket) blockPendingGroupInvite();
    lastSuccessfulTypedGroupRoomQuery = null;
    try {
      return await groupRoomInfoQuery.query(body);
    } finally {
      const query = activeTypedGroupRoomQuery;
      if (query && query.requestId === body.id && query.groupId === body.groupId) {
        activeTypedGroupRoomQuery = null;
      }
    }
  }

  function observeGroupNativeOutput(frame, reservation) {
    const observation = reservation.groupNativeObservation;
    if (!observation) return;

    if (Number.isSafeInteger(frame.command) && frame.command > 0 && frame.command <= 0x7fffffff) {
      const key = String(frame.command);
      if (Object.prototype.hasOwnProperty.call(observation.nativeCommandCounts, key)) {
        observation.nativeCommandCounts[key] = Math.min(
          GROUP_NATIVE_OBSERVATION_MAX_COUNT,
          observation.nativeCommandCounts[key] + 1,
        );
      } else if (Object.keys(observation.nativeCommandCounts).length < GROUP_NATIVE_OBSERVATION_MAX_COMMANDS) {
        observation.nativeCommandCounts[key] = 1;
      }
    }

    if (frame.command === GROUP_START_COMMAND) {
      const wellShapedAck = isDenseCallbackArray(frame.value, 2) &&
        isInt32(frame.value[0]) && typeof frame.value[1] === "string";
      if (frame.id === reservation.startRequestId) {
        observation.startAck4CorrelatedSeen = true;
        if (wellShapedAck) observation.startAck4ReturnCode = frame.value[0];
      } else if (frame.id === 0) {
        observation.startAck4IdZeroSeen = true;
        if (wellShapedAck) observation.startAck4IdZeroUntrustedReturnCode = frame.value[0];
      } else {
        observation.startAck4OtherIdSeen = true;
        if (wellShapedAck) observation.startAck4OtherIdUntrustedReturnCode = frame.value[0];
      }
      return;
    }

    if (frame.command === GROUP_TIMEOUT_OUTPUT_COMMAND) {
      observation.timeout20005Observed = true;
      return;
    }

    if (frame.command === GROUP_ERROR_OUTPUT_COMMAND && frame.id === 0) {
      const error = groupErrorObservation(frame.value, reservation.groupId);
      if (!error) return;
      observation.error20003Seen = true;
      observation.error20003Matched = true;
      observation.error20003MessageLength = error.messageLength;
      observation.error20003MessageSha256 = error.messageSha256;
      observation.error20003ReasonFlags = error.reasonFlags;
      observation.error20003CodeLabel = error.codeLabel;
      observation.error20003Code = error.code;
      return;
    }

    if (frame.command === GROUP_ENTER_OUTPUT_COMMAND && frame.id === 0) {
      const entry = groupEnterObservation(frame.value, reservation.groupId);
      if (!entry) return;
      observation.groupEnter20004Seen = true;
      observation.groupEnter20004Matched = entry.matched;
      observation.groupEnter20004ReturnCode = entry.returnCode;
      observation.groupEnter20004Creator = entry.creator;
    }
  }

  async function handleInvoke(requestBody) {
    // Parse and shape-check a private end candidate so an unpinned Host can
    // return a stable typed capability refusal instead of a generic allowlist
    // error. No command reaches the renderer until the capability check below.
    const body = validateInvokeBody(requestBody, hostGeneration, { privateEndProtocolVerified: true });
    if ([QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE].includes(body.command) && !verifiedPrivateEndProtocol) {
      throw codedError("private_end_protocol_unverified");
    }
    const privateEndTrace = beginPrivateEndTrace(body);
    if (body.command === 5 && privateCall) privateCall.acceptMayHavePosted = true;
    const newRejectId = body.command === QRTC_COMMAND_REJECT && privateRejectRequestIds.size < 4096 &&
      !privateRejectRequestIds.has(body.id);
    if (body.command === QRTC_COMMAND_REJECT) {
      if (privateRejectRequestIds.size < 4096) privateRejectRequestIds.add(body.id);
      if (privateCall) privateCall.rejectWitness = null;
    }
    if (body.command === QRTC_COMMAND_REJECT && privateCall && !privateCall.ambiguous &&
        !privateCall.entryRelationId && !privateCall.acceptMayHavePosted && trustedLogin &&
        privateCall.peerUid === body.params[1] && newRejectId) {
      const call = privateCall;
      const witness = { requestId: body.id, identity: trustedLogin, relationId: call.inviteRelationId,
        stateSequence: privateNativeStateSequence,
        posted: false, acknowledged: false };
      call.rejectWitness = witness;
      const receipt = await postPluginCommand(body, privateEndTrace);
      if (privateCall === call && call.rejectWitness === witness) witness.posted = receipt.posted === true;
      return receipt;
    }
    if (body.command !== 1) return postPluginCommand(body, privateEndTrace);
    if (groupReservation) throw codedError("group_call_reserved");
    if (uncertainLoginRequestIds.has(body.id)) throw codedError("login_request_id_uncertain");
    if (uncertainLoginRequestHistoryFull) throw codedError("login_request_history_full");
    if (loginAttempt || [...activeInvokes].some((entry) => entry.command === 1)) {
      if (sdkInfoStartupState === "in_flight") await ensureSDKInfoStartup(body.id);
      throw codedError("host_busy");
    }
    if (sdkInfoStartupState === "uncertain") throw codedError("sdk_info_post_uncertain");

    blockPendingGroupInvite();
    lastSuccessfulTypedGroupRoomQuery = null;
    consumedGroupInviteFingerprints.clear();
    trustedLogin = null;
    clearAudioDeviceSelectionProof();
    const identity = trustedLoginFromParams(body.params);
    const attempt = { id: body.id, identity, posted: false, acknowledged: null };
    loginAttempt = attempt;
    const startupRequiredForThisLogin = sdkInfoStartupState === "idle";
    let loginDispatchStarted = false;
    try {
      await ensureSDKInfoStartup(body.id);
      if (stopped) throw codedError("host_stopped");
      if (groupReservation) throw codedError("group_call_reserved");
      if (startupRequiredForThisLogin && privateCall) throw codedError("private_call_pending");
      if (loginAttempt !== attempt || [...activeInvokes].some((entry) => entry.command === 1)) {
        throw codedError("host_busy");
      }
      const loginReceipt = await postPluginCommand({
        ...body,
        onDispatch() { loginDispatchStarted = true; },
      }, privateEndTrace);
      if (loginAttempt === attempt) {
        attempt.posted = true;
        finishLoginAttempt(attempt);
      }
      return loginReceipt;
    } catch (error) {
      if (loginAttempt === attempt) loginAttempt = null;
      if (loginDispatchStarted) rememberUncertainLoginRequestId(body.id);
      trustedLogin = null;
      clearAudioDeviceSelectionProof();
      throw error;
    }
  }

  async function handleAudioDevices(requestBody) {
    const body = validateAudioDevicesBody(requestBody, hostGeneration);
    if (!verifiedAudioDeviceProtocol) throw codedError("audio_device_protocol_unverified");
    if (groupReservation) throw codedError("group_call_reserved");
    if (audioDeviceSelectionInFlight) throw codedError("audio_device_selection_busy");
    if (audioDeviceSelectionQuarantined) throw codedError("audio_device_selection_quarantined");
    if (audioDeviceQueryQuarantined) throw codedError("audio_device_query_quarantined");
    if (audioDeviceQueryTicket) throw codedError("audio_device_query_busy");
    const ticket = createAudioDeviceQueryTicket();
    ticket.timer = setTimeout(() => {
      audioDeviceQueryQuarantined = true;
      clearAudioDeviceQueryTicket(ticket, codedError("audio_device_query_quarantined"));
    }, AUDIO_DEVICE_SELECT_TIMEOUT_MS);
    ticket.timer.unref?.();
    try {
      const receipt = await postPluginCommand({
        command: AUDIO_DEVICE_QUERY_COMMAND,
        id: body.id,
        params: [],
      });
      return receipt;
    } catch (error) {
      if (safeFailureCode(error) === "invoke_timeout") {
        audioDeviceQueryQuarantined = true;
        clearAudioDeviceSelectionProof();
      }
      clearAudioDeviceQueryTicket(ticket, error);
      throw error;
    }
  }

  function remainingSelectionMs(deadline) {
    const remaining = deadline - Date.now();
    if (remaining <= 0 || stopped) throw codedError(stopped ? "host_stopped" : "audio_device_selection_timeout");
    return Math.max(1, Math.min(remaining, requestTimeoutMs));
  }

  function createAudioDeviceQueryTicket() {
    if (audioDeviceQueryQuarantined) throw codedError("audio_device_query_quarantined");
    if (audioDeviceQueryTicket) throw codedError("audio_device_query_busy");
    let resolve;
    let reject;
    const result = new Promise((res, rej) => { resolve = res; reject = rej; });
    const ticket = { sequence: audioDeviceOutputSequence, result, resolve, reject, timer: null, settled: false };
    result.catch(() => {});
    audioDeviceQueryTicket = ticket;
    return ticket;
  }

  function clearAudioDeviceQueryTicket(ticket, error) {
    if (!ticket || ticket.settled) return;
    ticket.settled = true;
    if (ticket.timer) clearTimeout(ticket.timer);
    if (audioDeviceQueryTicket === ticket) audioDeviceQueryTicket = null;
    ticket.reject(error || codedError("audio_device_query_failed"));
  }

  function observeAudioDeviceOutput(frame) {
    if (frame.command !== AUDIO_DEVICE_INFO_OUTPUT_COMMAND) return;
    audioDeviceOutputSequence += 1;
    const ticket = audioDeviceQueryTicket;
    if (!ticket) return;
    if (audioDeviceOutputSequence <= ticket.sequence) return;
    if (ticket.timer) clearTimeout(ticket.timer);
    ticket.timer = null;
    ticket.settled = true;
    audioDeviceQueryTicket = null;
    try {
      ticket.resolve({ sequence: audioDeviceOutputSequence, snapshot: parseAudioDeviceSnapshot(frame.value) });
    } catch {
      ticket.reject(codedError("audio_device_metadata_invalid"));
    }
  }

  async function queryFreshAudioDeviceSnapshot(id, deadline) {
    const ticket = createAudioDeviceQueryTicket();
    try {
      const remaining = remainingSelectionMs(deadline);
      ticket.timer = setTimeout(() => {
        audioDeviceQueryQuarantined = true;
        clearAudioDeviceQueryTicket(ticket, codedError("audio_device_selection_timeout"));
      }, remaining);
      ticket.timer.unref?.();
      await postPluginCommand({
        command: AUDIO_DEVICE_QUERY_COMMAND,
        id,
        params: [],
        timeoutMs: remainingSelectionMs(deadline),
        selectorOwned: true,
      });
    } catch (error) {
      if (safeFailureCode(error) === "invoke_timeout") {
        audioDeviceQueryQuarantined = true;
        clearAudioDeviceSelectionProof();
      }
      clearAudioDeviceQueryTicket(ticket, error);
      throw error;
    }
    return ticket.result;
  }

  async function handleAudioDeviceSelection(requestBody) {
    const body = validateAudioDeviceSelectionBody(requestBody, hostGeneration);
    if (!verifiedAudioDeviceProtocol || !verifiedAudioDeviceSelectionProtocol) {
      throw codedError("audio_device_selection_unverified");
    }
    if (groupReservation) throw codedError("group_call_reserved");
    if (audioDeviceSelectionQuarantined) throw codedError("audio_device_selection_quarantined");
    if (audioDeviceQueryQuarantined) throw codedError("audio_device_query_quarantined");
    if (audioDeviceSelectionInFlight || [...activeInvokes].some((item) => item.command === 1 || item.command === 5)) {
      throw codedError("audio_device_selection_busy");
    }
    clearAudioDeviceSelectionProof();
    audioDeviceSelectionInFlight = true;
    const deadline = Date.now() + audioDeviceSelectionTimeoutMs;
    let setterMayHaveBeenPosted = false;
    try {
      const beforeResult = await queryFreshAudioDeviceSnapshot(body.id, deadline);
      const inputDevice = resolveUniqueDevice(beforeResult.snapshot, "input", body.inputDeviceName);
      const outputDevice = resolveUniqueDevice(beforeResult.snapshot, "output", body.outputDeviceName);
      validateAudioDeviceTail(beforeResult.snapshot);
      const defaults = [beforeResult.snapshot.tail[2], beforeResult.snapshot.tail[3]];
      setterMayHaveBeenPosted = true;
      // SDK-side storage and callback serialization establish the tail as
      // current output, current input, default output, default input.
      await postPluginCommand({
        command: AUDIO_DEVICE_SELECT_COMMAND,
        id: body.id,
        params: [inputDevice.id, outputDevice.id],
        timeoutMs: remainingSelectionMs(deadline),
        selectorOwned: true,
      });
      const afterResult = await queryFreshAudioDeviceSnapshot(body.id, deadline);
      validateAudioDeviceTail(afterResult.snapshot);
      if (!sameDeviceMappings(beforeResult.snapshot, afterResult.snapshot)) throw codedError("audio_device_map_changed");
      if (afterResult.snapshot.tail[0] !== outputDevice.id || afterResult.snapshot.tail[1] !== inputDevice.id ||
          afterResult.snapshot.tail[2] !== defaults[0] || afterResult.snapshot.tail[3] !== defaults[1]) {
        throw codedError("audio_device_selection_mismatch");
      }
      selectedAudioDeviceNames = Object.freeze({
        inputName: inputDevice.name,
        outputName: outputDevice.name,
      });
      audioDeviceSelectionReady = true;
      return { selected: true, id: body.id, command: AUDIO_DEVICE_SELECT_COMMAND };
    } catch (error) {
      const code = safeFailureCode(error, "audio_device_selection_failed");
      if (setterMayHaveBeenPosted && code !== "audio_device_selection_mismatch" && code !== "audio_device_map_changed") {
        audioDeviceSelectionQuarantined = true;
      }
      if (audioDeviceSelectionQuarantined || audioDeviceQueryQuarantined) clearAudioDeviceSelectionProof();
      throw codedError(code);
    } finally {
      if (!audioDeviceSelectionQuarantined) audioDeviceSelectionInFlight = false;
    }
  }

  const groupRoomInfoQuery = createGroupRoomInfoQuery({
    hostGeneration,
    capability: verifiedGroupRoomInfoProtocol,
    repeatable: repeatableGroupRoomQueries,
    timeoutMs: groupRoomInfoTimeoutMs,
    sendCommand: (request) => {
      const isTypedGroupQuery = request?.command === GROUP_ROOM_QUERY_COMMAND && isPositiveInteger(request.id) &&
        Array.isArray(request.params) && request.params.length === 2 && request.params[0] === GROUP_ROOM_SCENE &&
        typeof request.params[1] === "string" && CANONICAL_UIN_PATTERN.test(request.params[1]);
      if (!isTypedGroupQuery) return postPluginCommand({ ...request, nonfatal: true });

      const query = {
        requestId: request.id,
        groupId: request.params[1],
        requestOutputSequence: outputFrameSequence,
        commandPosted: false,
        positiveFullRoom: false,
        roomOutputSequence: null,
        roomOutputSeenAtMs: null,
      };
      if (groupReservation?.operation === "group_accept" &&
          groupReservation.startRequestId === request.id && groupReservation.groupId === query.groupId) {
        groupReservation.acceptRoomQuery = query;
      }
      activeTypedGroupRoomQuery = query;
      return postPluginCommand({ ...request, nonfatal: true }).then((receipt) => {
        if (activeTypedGroupRoomQuery === query) {
          query.commandPosted = receipt?.posted === true && receipt.id === query.requestId &&
            receipt.command === GROUP_ROOM_QUERY_COMMAND;
          if (query.commandPosted) commitTypedGroupRoomQueryIfComplete(query);
          else activeTypedGroupRoomQuery = null;
        }
        return receipt;
      }, (error) => {
        if (activeTypedGroupRoomQuery === query) activeTypedGroupRoomQuery = null;
        throw error;
      });
    },
  });

  const chattingRoomIdQuery = createChattingRoomIdQuery({
    hostGeneration,
    sendCommand: (request, options) => postPluginCommand({ ...request, nonfatal: true,
      timeoutMs: Math.min(requestTimeoutMs, options.timeoutMs) }),
  });

  function localRoomContextCurrent(witness) {
    return Boolean(witness && groupReservation === witness.reservation && trustedLogin === witness.identity &&
      witness.identity && witness.reservation.nativeEntryProof === witness.entryProof &&
      witness.entryProof && selectedAudioDeviceNames === witness.audioSelection && witness.audioSelection &&
      witness.reservation.nativeEntrySequence === witness.entrySequence &&
      witness.reservation.localEntryEpochEligible === true &&
      witness.reservation.state === "quitting" && audioDeviceSelectionReady && !privateCall && currentReadiness().ready);
  }

  async function prepareLocalRoomWitness(reservation) {
    reservation.localRoomWitness = null;
    if (!verifiedLocalRoomIdProtocol || !reservation.nativeEntryProof || reservation.localEntryEpochEligible !== true ||
        !trustedLogin || !audioDeviceSelectionReady || privateCall) return;
    const witness = { reservation, identity: trustedLogin, entryProof: reservation.nativeEntryProof,
      audioSelection: selectedAudioDeviceNames, entrySequence: reservation.nativeEntrySequence };
    try {
      const result = await chattingRoomIdQuery.queryGenerated(750);
      if (localRoomContextCurrent(witness) && result.observation.returnCode === 0 &&
          result.observation.dataKind === "string" && result.observation.numericRoomId === true) {
        reservation.localRoomWitness = Object.freeze({ ...witness, roomIdSha256: result.observation.dataSha256 });
      }
    } catch { /* Missing readback evidence keeps the existing membership fallback. */ }
  }

  async function handleGroupQuitAbsence(requestBody) {
    if (!ownKeysExactly(requestBody, ["id", "hostGeneration", "groupId", "startRequestId", "quitRequestId"])) {
      throw codedError("invalid_request_shape");
    }
    const body = validateGroupQuitBody({ id: requestBody.id, hostGeneration: requestBody.hostGeneration,
      groupId: requestBody.groupId, startRequestId: requestBody.startRequestId }, hostGeneration);
    if (!isPositiveInteger(requestBody.quitRequestId)) throw codedError("invalid_request_id");
    if (!verifiedGroupCallProtocol || !verifiedLocalRoomIdProtocol) throw codedError("group_local_room_protocol_unverified");
    const reservation = groupReservation;
    if (!reservation || reservation.groupId !== body.groupId || reservation.startRequestId !== body.startRequestId ||
        reservation.quitRequestId !== requestBody.quitRequestId) throw codedError("group_call_unowned");
    const response = { ok: true, id: body.id, hostGeneration, groupId: body.groupId,
      startRequestId: body.startRequestId, quitRequestId: requestBody.quitRequestId,
      confirmed: false, source: "native_sdk_room_id_readback" };
    const witness = reservation.localRoomWitness;
    const ack = reservation.quitAckProof;
    groupLocalQuitObservation = Object.freeze({ outcome: "prerequisites_unavailable",
      contextCurrent: localRoomContextCurrent(witness), quitPosted: reservation.quitPosted === true,
      acknowledged: Boolean(ack), memberQueryPending: groupRoomInfoQuery.status().pending,
      returnCode: null, dataKind: null });
    if (!localRoomContextCurrent(witness) || !reservation.quitPosted || !ack ||
        ack.quitRequestId !== requestBody.quitRequestId || ack.startRequestId !== body.startRequestId ||
        groupRoomInfoQuery.status().pending) return response;
    const proofContextCurrent = () => localRoomContextCurrent(witness) && reservation.localRoomWitness === witness &&
      reservation.quitAckProof === ack && reservation.quitPosted && reservation.quitRequestId === requestBody.quitRequestId &&
      reservation.groupId === body.groupId && reservation.startRequestId === body.startRequestId &&
      !groupRoomInfoQuery.status().pending;
    const proofStartedAtMs = Date.now();
    const proofDeadlineMs = proofStartedAtMs + 5000; // Preserve the original single-read budget.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!proofContextCurrent()) throw codedError("group_local_room_context_changed");
      const remainingMs = proofDeadlineMs - Date.now();
      if (remainingMs <= 0) break;
      groupLocalQuitObservation = Object.freeze({ ...groupLocalQuitObservation, outcome: "readback_pending",
        readAttempts: attempt + 1 });
      let result;
      try { result = await chattingRoomIdQuery.queryGenerated(remainingMs); }
      catch (error) {
        groupLocalQuitObservation = Object.freeze({ ...groupLocalQuitObservation, outcome: "readback_failed",
          code: safeFailureCode(error, "group_local_room_query_failed") });
        throw error;
      }
      groupLocalQuitObservation = Object.freeze({ outcome: "readback_not_absent",
        contextCurrent: proofContextCurrent(), quitPosted: reservation.quitPosted === true,
        acknowledged: reservation.quitAckProof === ack, memberQueryPending: groupRoomInfoQuery.status().pending,
        returnCode: result.observation.returnCode, dataKind: result.observation.dataKind,
        readAttempts: attempt + 1, elapsedMs: Date.now() - proofStartedAtMs });
      if (!proofContextCurrent()) throw codedError("group_local_room_context_changed");
      if (Date.now() >= proofDeadlineMs) {
        groupLocalQuitObservation = Object.freeze({ ...groupLocalQuitObservation, outcome: "readback_budget_exhausted" });
        break;
      }
      if (result.observation.returnCode === 0 && result.observation.dataKind === "zero_string") {
        // This owned, entered group exercised the same SDK room getter through
        // a positive witness and a fresh post-Quit zero. Remember initialization
        // for this exact login only; the old zero never clears a later invite.
        confirmedGroupRoomReadbackIdentity = witness.identity;
        groupReservation = null;
        response.confirmed = true;
        groupLocalQuitObservation = Object.freeze({ ...groupLocalQuitObservation, outcome: "confirmed" });
        break;
      }
      // Quit's ACK can precede SDK state publication. Wait only while the same
      // witnessed physical room remains; a different room or unknown result is final.
      if (result.observation.returnCode !== 0 || result.observation.dataKind !== "string" ||
          result.observation.numericRoomId !== true || result.observation.dataSha256 !== witness.roomIdSha256 ||
          attempt === 2 || proofDeadlineMs - Date.now() <= 250) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return response;
  }

  async function handleChattingRoomId(requestBody) {
    const body = validateGroupRoomInfoBody(requestBody, hostGeneration);
    if (!verifiedGroupCallProtocol) throw codedError("group_call_protocol_unverified");
    const readiness = currentReadiness();
    if (!readiness.ready) throw codedError(readiness.code);
    const identity = trustedLogin;
    const reservation = groupReservation;
    if (!identity) throw codedError("group_login_unverified");
    if (privateCall) throw codedError("group_private_call_pending");
    if (reservation && reservation.groupId !== body.groupId) throw codedError("group_call_unowned");
    const result = await chattingRoomIdQuery.query(body.id);
    if (trustedLogin !== identity || groupReservation !== reservation || privateCall || !currentReadiness().ready) {
      throw codedError("group_local_room_context_changed");
    }
    // Diagnostic only. Even an empty successful result cannot release the slot.
    return result;
  }

  async function handlePrivateChattingRoomId(body) {
    if (!ownKeysExactly(body, ["id", "hostGeneration", "peerUid"]) ||
        !isPositiveInteger(body.id) || typeof body.peerUid !== "string" ||
        !QQ_UID_PATTERN.test(body.peerUid)) throw codedError("invalid_request_shape");
    if (body.hostGeneration !== hostGeneration) throw codedError("generation_mismatch");
    if (!verifiedPrivateEndProtocol || !verifiedLocalRoomIdProtocol) {
      throw codedError("private_end_protocol_unverified");
    }
    const readiness = currentReadiness();
    if (!readiness.ready) throw codedError(readiness.code);
    const identity = trustedLogin;
    const call = privateCall;
    if (!identity || !call || call.ambiguous || call.peerUid !== body.peerUid || groupReservation) {
      throw codedError("private_local_room_unowned");
    }
    if (!call.entryRelationId && !(privateRoomReadbackIdentity === identity &&
        call.rejectWitness?.posted && call.rejectWitness?.acknowledged)) {
      throw codedError("private_local_room_unowned");
    }
    const relation = call.inviteRelationId;
    const entry = call.entryRelationId;
    const stateSequence = privateNativeStateSequence;
    const result = await chattingRoomIdQuery.queryGenerated();
    if (trustedLogin !== identity || privateCall !== call || call.ambiguous ||
        call.peerUid !== body.peerUid || call.inviteRelationId !== relation ||
        call.entryRelationId !== entry || privateNativeStateSequence !== stateSequence ||
        groupReservation || !currentReadiness().ready) {
      throw codedError("private_local_room_context_changed");
    }
    if (entry && result.observation.returnCode === 0 && result.observation.numericRoomId === true) {
      call.roomReadbackHash = result.observation.dataSha256;
    }
    // This getter's private-room semantics require live positive/negative controls.
    // An empty readback never clears either the Host or Bridge reservation.
    return { ...result, id: body.id, diagnosticOnly: true };
  }

  async function handlePrivateRejectAbsence(body) {
    if (!ownKeysExactly(body, ["id", "hostGeneration", "peerUid", "rejectRequestId"]) ||
        !isPositiveInteger(body.id) || !isPositiveInteger(body.rejectRequestId) ||
        typeof body.peerUid !== "string" || !QQ_UID_PATTERN.test(body.peerUid)) {
      throw codedError("invalid_request_shape");
    }
    if (body.hostGeneration !== hostGeneration) throw codedError("generation_mismatch");
    if (!verifiedPrivateEndProtocol || !verifiedLocalRoomIdProtocol) throw codedError("private_end_protocol_unverified");
    const call = privateCall;
    const witness = call?.rejectWitness;
    const current = () => Boolean(currentReadiness().ready && trustedLogin && privateCall === call &&
      witness && call.rejectWitness === witness && witness.identity === trustedLogin &&
      privateNativeStateSequence === witness.stateSequence &&
      call.inviteRelationId === witness.relationId && !call.ambiguous && !call.entryRelationId &&
      !call.acceptMayHavePosted && call.peerUid === body.peerUid && !groupReservation &&
      witness.requestId === body.rejectRequestId && witness.posted && witness.acknowledged);
    const result = { ok: true, ...body, confirmed: false, source: "native_reject_ack_and_sdk_room_absence" };
    const roomInitialized = Boolean(trustedLogin && (privateRoomReadbackIdentity === trustedLogin ||
      confirmedGroupRoomReadbackIdentity === trustedLogin));
    privateRejectObservation = { outcome: "prerequisites_unavailable", callPresent: Boolean(call),
      witnessPresent: Boolean(witness), ambiguous: call?.ambiguous === true,
      entryPresent: Boolean(call?.entryRelationId), acceptMayHavePosted: call?.acceptMayHavePosted === true,
      posted: witness?.posted === true, acknowledged: witness?.acknowledged === true,
      roomInitialized, contextCurrent: current() };
    if (!current()) return result;
    if (!roomAdmissionMayHavePosted) {
      // Retire only the current unentered invitation: no room-admission command
      // or entry callback ever occurred. This is not room destruction and never
      // queries the uninitialized SDK room manager.
      privateCall = null;
      result.confirmed = true;
      result.source = "native_reject_ack_and_no_room_admission";
      privateRejectObservation.outcome = "confirmed_without_room_admission";
      return result;
    }
    if (!roomInitialized) return result;
    const started = Date.now();
    const deadline = started + 5000;
    let observedRoomHash = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!current()) {
        privateRejectObservation.outcome = "context_changed";
        privateRejectObservation.contextCurrent = false;
        throw codedError("private_local_room_context_changed");
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      privateRejectObservation.outcome = "readback_pending";
      let readback;
      try { readback = await chattingRoomIdQuery.queryGenerated(remaining); }
      catch (error) {
        privateRejectObservation.outcome = "readback_failed";
        privateRejectObservation.elapsedMs = Date.now() - started;
        privateRejectObservation.contextCurrent = current();
        throw error;
      }
      Object.assign(privateRejectObservation, { outcome: "readback_not_absent", readAttempts: attempt + 1,
        elapsedMs: Date.now() - started, returnCode: readback.observation.returnCode,
        dataKind: readback.observation.dataKind, numericRoomId: readback.observation.numericRoomId,
        dataLength: readback.observation.dataLength, contextCurrent: current() });
      if (!current()) {
        privateRejectObservation.outcome = "context_changed";
        throw codedError("private_local_room_context_changed");
      }
      if (Date.now() >= deadline) break;
      if (readback.observation.returnCode === 0 && readback.observation.dataKind === "zero_string") {
        privateCall = null;
        result.confirmed = true;
        privateRejectObservation.outcome = "confirmed";
        break;
      }
      // A stable numeric value only permits another read, never clearance or
      // attribution of that room to this unentered invitation.
      if (readback.observation.returnCode !== 0 || !readback.observation.numericRoomId ||
          (observedRoomHash && observedRoomHash !== readback.observation.dataSha256)) break;
      observedRoomHash = readback.observation.dataSha256;
      if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return result;
  }

  async function handlePrivateEndedRoomAbsence(body) {
    if (!ownKeysExactly(body, ["id", "hostGeneration", "peerUid", "enterRelationId"]) ||
        !isPositiveInteger(body.id) || typeof body.peerUid !== "string" || !QQ_UID_PATTERN.test(body.peerUid) ||
        typeof body.enterRelationId !== "string" || !body.enterRelationId ||
        Buffer.byteLength(body.enterRelationId, "utf8") > 512) throw codedError("invalid_request_shape");
    if (body.hostGeneration !== hostGeneration) throw codedError("generation_mismatch");
    if (!verifiedPrivateEndProtocol || !verifiedLocalRoomIdProtocol) throw codedError("private_end_protocol_unverified");
    const call = privateCall;
    const identity = trustedLogin;
    const sequence = privateNativeStateSequence;
    const current = () => Boolean(currentReadiness().ready && identity && trustedLogin === identity &&
      call && privateCall === call && !call.ambiguous && !groupReservation &&
      call.peerUid === body.peerUid && call.entryRelationId === body.enterRelationId &&
      privateNativeStateSequence === sequence);
    const result = { ok: true, ...body, confirmed: false, source: "native_entered_private_room_absence" };
    privateRoomEndObservation = { outcome: "prerequisites_unavailable", callPresent: Boolean(call),
      ambiguous: call?.ambiguous === true, entryMatched: call?.entryRelationId === body.enterRelationId,
      identityPresent: Boolean(identity) };
    if (!current()) return result;
    // The caller additionally holds a verified current Kernel room-end proof.
    // A saved successful SDK entry makes this an initialized-room read, unlike
    // the unsafe unentered first-invitation diagnostic.
    const started = Date.now();
    const deadline = started + 5000;
    let ownedRoomHash = call.roomReadbackHash || null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!current()) throw codedError("private_local_room_context_changed");
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      const readback = await chattingRoomIdQuery.queryGenerated(remaining);
      privateRoomEndObservation = { outcome: "readback_not_absent", readAttempts: attempt + 1,
        elapsedMs: Date.now() - started, returnCode: readback.observation.returnCode,
        dataKind: readback.observation.dataKind, contextCurrent: current() };
      if (!current()) throw codedError("private_local_room_context_changed");
      if (Date.now() >= deadline) break;
      if (readback.observation.returnCode === 0 && readback.observation.dataKind === "zero_string") {
        privateCall = null;
        result.confirmed = true;
        privateRoomEndObservation.outcome = "confirmed";
        break;
      }
      // The exact saved SDK entry plus verified Kernel end and unchanged
      // single-room context bind this first numeric read to the owned room.
      // Retry its lagging publication only; a different room is final.
      if (readback.observation.returnCode !== 0 || !readback.observation.numericRoomId ||
          (ownedRoomHash && readback.observation.dataSha256 !== ownedRoomHash) ||
          attempt === 2 || deadline - Date.now() <= 250) break;
      ownedRoomHash = readback.observation.dataSha256;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    return result;
  }

  async function handleGroupRemoteAbsence(requestBody, clearOwnedRoom = false) {
    if (!ownKeysExactly(requestBody, ["id", "hostGeneration", "groupId", "startRequestId"])) {
      throw codedError("invalid_request_shape");
    }
    const body = validateGroupQuitBody(requestBody, hostGeneration);
    if (!verifiedGroupCallProtocol || !verifiedLocalRoomIdProtocol) throw codedError("group_local_room_protocol_unverified");
    const reservation = groupReservation;
    const terminal = reservation?.remoteTerminalProof;
    const entry = reservation?.nativeEntryProof;
    const identity = trustedLogin;
    const devices = selectedAudioDeviceNames;
    const entrySequence = reservation?.nativeEntrySequence;
    const current = () => Boolean(reservation && groupReservation === reservation &&
      reservation.groupId === body.groupId && reservation.startRequestId === body.startRequestId &&
      reservation.startPosted && entry && reservation.nativeEntryProof === entry &&
      reservation.nativeEntrySequence === entrySequence &&
      (!reservation.clearAttempted || (reservation.clearEntrySequence === entrySequence &&
        reservation.clearIdentity === identity && reservation.clearDevices === devices)) &&
      (clearOwnedRoom || (terminal?.kind === "remote_end" && terminal.entryProof === entry && reservation.remoteTerminalProof === terminal)) &&
      trustedLogin === identity && identity && selectedAudioDeviceNames === devices && devices &&
      audioDeviceSelectionReady && !privateCall && currentReadiness().ready && !groupRoomInfoQuery.status().pending);
    if (!current()) throw codedError("group_call_unowned");
    const result = { ok: true, ...body, confirmed: false,
      source: clearOwnedRoom ? "native_group_clear_absence" : "native_entered_group_room_absence" };
    if (clearOwnedRoom && !reservation.clearPosted) {
      if (reservation.clearAttempted) throw codedError("group_clear_outcome_unknown");
      // QQ's own zero-member handler calls clearRoom({sceneId:3, relationId})
      // before hangup. Generic invocation of cmd11 remains blocked.
      await postPluginCommand({ command: 11, id: body.id, params: [GROUP_ROOM_SCENE, body.groupId],
        timeoutMs: 750, nonfatal: true, onDispatch() {
          reservation.clearAttempted = true;
          reservation.clearEntrySequence = entrySequence;
          reservation.clearIdentity = identity;
          reservation.clearDevices = devices;
        } });
      if (!current()) throw codedError("group_local_room_context_changed");
      reservation.clearPosted = true;
    }
    // Unlike an idle/pre-login zero, this read follows a successful native
    // scene-3 entry AND its matching room-destroy callback for the owned start.
    // Recheck object identities across every await; never infer absence from
    // a failed/empty member query or from the Kernel's member count alone.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (!current()) throw codedError("group_local_room_context_changed");
      const readback = await chattingRoomIdQuery.queryGenerated(750);
      if (!current()) throw codedError("group_local_room_context_changed");
      if (readback.observation.returnCode === 0 && readback.observation.dataKind === "zero_string") {
        groupReservation = null;
        result.confirmed = true;
        return result;
      }
      if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return result;
  }

  async function handleGroupRoomInfo(requestBody) {
    const body = validateGroupRoomInfoBody(requestBody, hostGeneration);
    if (!verifiedGroupRoomInfoProtocol) throw codedError("group_room_info_protocol_unverified");
    const readiness = currentReadiness();
    if (!readiness.ready) throw codedError(readiness.code);
    const reservation = groupReservation;
    const trustedIdentity = trustedLogin;
    const audioSelection = selectedAudioDeviceNames;
    const readinessSnapshotKey = (value) => [
      value?.ready,
      value?.status?.hostGeneration,
      value?.status?.pluginFound,
      value?.status?.hasPostMessage,
      value?.status?.rendererGone,
      value?.status?.stopped,
      value?.status?.outputFailed,
    ].join(":");
    const readinessKey = readinessSnapshotKey(readiness);
    const terminalProof = reservation?.terminalAfterQuit || reservation?.quitAckProof || reservation?.remoteTerminalProof || null;
    const nativeEntryProof = reservation?.nativeEntryProof || null;
    const result = await queryTypedGroupRoomInfo(body);

    const quitProofCurrent = Boolean(reservation?.quitMayHavePosted &&
      ((terminalProof?.kind === "owned_quit" && reservation.terminalAfterQuit === terminalProof) ||
       (terminalProof?.kind === "owned_quit_ack" && reservation.quitPosted && reservation.quitAckProof === terminalProof &&
        terminalProof.quitRequestId === reservation.quitRequestId && terminalProof.startRequestId === reservation.startRequestId)));
    const remoteProofCurrent = Boolean(terminalProof?.kind === "remote_end" && nativeEntryProof &&
      reservation?.nativeEntryProof === nativeEntryProof && terminalProof.entryProof === nativeEntryProof &&
      reservation.remoteTerminalProof === terminalProof);
    const afterReadiness = currentReadiness();
    if (reservation && (quitProofCurrent || remoteProofCurrent) && groupReservation === reservation &&
        body.groupId === reservation.groupId && trustedIdentity && trustedLogin === trustedIdentity &&
        QQ_UID_PATTERN.test(trustedIdentity.selfUid) && CANONICAL_UIN_PATTERN.test(trustedIdentity.selfUin) &&
        audioDeviceSelectionReady && selectedAudioDeviceNames === audioSelection && afterReadiness.ready &&
        readinessSnapshotKey(afterReadiness) === readinessKey &&
        result.ok === true &&
        result.hostGeneration === hostGeneration && result.id === body.id && result.groupId === body.groupId &&
        result.diagnosticOnly === true && result.nativeJoinVerified === false &&
        result.roomInfo?.sceneId === GROUP_ROOM_SCENE && result.roomInfo.relationId === reservation.groupId &&
        Array.isArray(result.roomInfo.userList) && result.roomInfo.userNum === result.roomInfo.userList.length) {
      const identityRepresentationsKnown = result.roomInfo.userList.every((user) =>
        QQ_UID_PATTERN.test(user.uid) || CANONICAL_UIN_PATTERN.test(user.uid));
      const selfPresent = result.roomInfo.userList.some((user) =>
        user.uid === trustedIdentity.selfUid || user.uid === trustedIdentity.selfUin);
      if (identityRepresentationsKnown && !selfPresent) {
        groupReservation = null;
      }
      // Membership may lag a successful Quit ACK. Keep the owned quit proof
      // and occupied reservation so another fresh query can confirm absence.
      // A present/unknown/partial roster never authorizes release or retrying Quit.
    }
    return result;
  }

  function validateGroupStartPrerequisites() {
    if (!verifiedGroupCallProtocol) throw codedError("group_call_protocol_unverified");
    const readiness = currentReadiness();
    if (!readiness.ready) throw codedError(readiness.code);
    if (!trustedLogin) throw codedError("group_login_unverified");
    if (!audioDeviceSelectionReady || !selectedAudioDeviceNames) throw codedError("group_audio_device_selection_required");
    if (!isDistinctVirtualAudioSelection(selectedAudioDeviceNames)) throw codedError("group_audio_device_route_not_virtual");
    if (groupReservation) throw codedError("group_start_busy");
    if (audioDeviceSelectionInFlight || [...activeInvokes].some((entry) => entry.command === 1 || entry.command === 5)) {
      throw codedError("group_start_busy");
    }
    if (privateCall) throw codedError("group_private_call_pending");
    return { readiness, identity: trustedLogin, audioSelection: selectedAudioDeviceNames };
  }

  async function handleGroupAccept(requestBody) {
    const body = validateGroupAcceptBody(requestBody, hostGeneration);
    let reservation = null;
    let ticket = null;
    let dispatchStarted = false;
    try {
      const prerequisites = validateGroupStartPrerequisites();
      ticket = pendingGroupInvite;
      if (!ticket) throw codedError("group_invite_unavailable");
      if (ticket.groupId !== body.groupId) throw codedError("group_invite_group_mismatch");
      if (ticket.loginIdentityRef !== prerequisites.identity || trustedLogin !== ticket.loginIdentityRef) {
        blockPendingGroupInvite(ticket);
        throw codedError("group_accept_identity_changed");
      }
      const inviteAgeMs = Date.now() - ticket.receivedAtMs;
      const witness = lastSuccessfulTypedGroupRoomQuery;
      if (inviteAgeMs < 0 || inviteAgeMs > GROUP_INVITE_ACCEPT_TTL_MS || !witness ||
          witness.groupId !== ticket.groupId || witness.outputSequence !== ticket.roomQueryOutputSequence ||
          witness.seenAtMs !== ticket.roomQuerySeenAtMs ||
          ticket.receivedAtMs - witness.seenAtMs < 0 ||
          ticket.receivedAtMs - witness.seenAtMs > GROUP_INVITE_ACCEPT_TTL_MS ||
          ticket.outputSequence <= witness.outputSequence) {
        blockPendingGroupInvite(ticket);
        throw codedError("group_invite_stale");
      }

      reservation = {
        state: "starting",
        operation: "group_accept",
        groupId: ticket.groupId,
        startRequestId: body.id,
        selfUid: prerequisites.identity.selfUid,
        identity: prerequisites.identity,
        acceptInviteTicket: ticket,
        acceptRoomQuery: null,
        startMayHavePosted: false,
        startPosted: false,
        quitInFlight: false,
        quitMayHavePosted: false,
        quitPosted: false,
        quitTerminalSequenceStart: 0,
        terminalAfterQuit: null,
        nativeEntryProof: null,
        nativeCreatorEntryProof: null,
        remoteTerminalProof: null,
        groupNativeObservation: createGroupNativeObservation(),
      };
      // Reserve before the fresh room recheck so private accept, login,
      // selection, and another group operation cannot race the invitation.
      groupReservation = reservation;
      groupLocalQuitObservation = null;

      const roomResult = await queryTypedGroupRoomInfo({
        id: body.id,
        hostGeneration,
        groupId: ticket.groupId,
      }, { preserveGroupInviteTicket: ticket });

      const readiness = currentReadiness();
      const acceptQuery = reservation.acceptRoomQuery;
      const completedQuery = lastSuccessfulTypedGroupRoomQuery;
      if (groupReservation !== reservation || pendingGroupInvite !== ticket || !readiness.ready) {
        throw codedError(!readiness.ready ? readiness.code : "group_invite_stale");
      }
      if (trustedLogin !== prerequisites.identity || trustedLogin !== ticket.loginIdentityRef) {
        blockPendingGroupInvite(ticket);
        throw codedError("group_accept_identity_changed");
      }
      if (!audioDeviceSelectionReady || selectedAudioDeviceNames !== prerequisites.audioSelection ||
          audioDeviceSelectionInFlight || [...activeInvokes].some((entry) =>
            entry.command === 1 || entry.command === GROUP_ACCEPT_COMMAND) || privateCall) {
        throw codedError(privateCall ? "group_private_call_pending" : "group_audio_device_selection_required");
      }
      if (Date.now() - ticket.receivedAtMs < 0 || Date.now() - ticket.receivedAtMs > GROUP_INVITE_ACCEPT_TTL_MS) {
        blockPendingGroupInvite(ticket);
        throw codedError("group_invite_stale");
      }
      if (!acceptQuery || !acceptQuery.commandPosted || !acceptQuery.positiveFullRoom ||
          acceptQuery.requestId !== body.id || acceptQuery.groupId !== ticket.groupId ||
          completedQuery?.queryRef !== acceptQuery || completedQuery.groupId !== ticket.groupId ||
          completedQuery.requestId !== body.id || completedQuery.outputSequence !== acceptQuery.roomOutputSequence ||
          !Number.isSafeInteger(acceptQuery.roomOutputSeenAtMs) ||
          Date.now() - acceptQuery.roomOutputSeenAtMs < 0 ||
          Date.now() - acceptQuery.roomOutputSeenAtMs > GROUP_ROOM_QUERY_TIMEOUT_MS ||
          roomResult.ok !== true || roomResult.hostGeneration !== hostGeneration || roomResult.id !== body.id ||
          roomResult.groupId !== ticket.groupId || roomResult.diagnosticOnly !== true ||
          roomResult.nativeJoinVerified !== false || roomResult.roomInfo?.sceneId !== GROUP_ROOM_SCENE ||
          roomResult.roomInfo.relationId !== ticket.groupId || roomResult.roomInfo.userNum <= 0 ||
          roomResult.roomInfo.userNum !== roomResult.roomInfo.userList.length) {
        throw codedError("group_room_metadata_invalid");
      }

      // SDK accept() serializes this exact seven-field order; the inviter is
      // taken only from the ticket created by the native scene-3 callback.
      const params = [
        GROUP_ROOM_SCENE,
        prerequisites.identity.selfUid,
        [ticket.inviterUid],
        1,
        ticket.groupId,
        false,
        "",
      ];
      const receipt = await postPluginCommand({
        command: GROUP_ACCEPT_COMMAND,
        id: body.id,
        params,
        groupAcceptReservation: reservation,
        onDispatch() {
          dispatchStarted = true;
          if (pendingGroupInvite === ticket) {
            blockPendingGroupInvite(ticket);
            rememberConsumedGroupInvite(ticket);
          }
          reservation.startMayHavePosted = true;
          reservation.acceptMayHavePosted = true;
          if (groupReservation === reservation && reservation.state === "starting") {
            reservation.state = "starting_post";
          }
        },
      });
      if (groupReservation === reservation) {
        reservation.startPosted = true;
        reservation.acceptPosted = true;
        if (reservation.state === "starting_post") reservation.state = "reserved";
      }
      return receipt;
    } catch (error) {
      if (reservation && groupReservation === reservation) {
        if (dispatchStarted && !reservation.quitMayHavePosted) {
          reservation.state = "quarantined";
          reservation.quarantineCode = safeFailureCode(error, "group_accept_outcome_unknown");
        } else if (!dispatchStarted) {
          groupReservation = null;
          blockPendingGroupInvite(ticket);
        }
      } else if (ticket && (safeFailureCode(error) === "group_invite_stale" ||
          safeFailureCode(error) === "group_accept_identity_changed")) {
        blockPendingGroupInvite(ticket);
      }
      if (!dispatchStarted) throw groupControlPreDispatchError(error, body.id, GROUP_ACCEPT_COMMAND, "group_accept_failed");
      throw error;
    }
  }

  async function handleGroupStart(requestBody) {
    const body = validateGroupStartBody(requestBody, hostGeneration);
    let reservation = null;
    let dispatchStarted = false;
    try {
      const prerequisites = validateGroupStartPrerequisites();
      reservation = {
        state: "starting",
        groupId: body.groupId,
        startRequestId: body.id,
        selfUid: prerequisites.identity.selfUid,
        identity: prerequisites.identity,
        startMayHavePosted: false,
        startPosted: false,
        quitInFlight: false,
        quitMayHavePosted: false,
        quitPosted: false,
        quitTerminalSequenceStart: 0,
        terminalAfterQuit: null,
        nativeEntryProof: null,
        nativeCreatorEntryProof: null,
        remoteTerminalProof: null,
        groupNativeObservation: createGroupNativeObservation(),
      };
      // Reserve synchronously before the native room check so command 5 cannot
      // slip between the check and the eventual Start4 post.
      groupReservation = reservation;
      groupLocalQuitObservation = null;

      const roomResult = await queryTypedGroupRoomInfo({
        id: body.id,
        hostGeneration,
        groupId: body.groupId,
      });
      if (groupReservation !== reservation) throw codedError("group_start_busy");
      const readiness = currentReadiness();
      if (!readiness.ready) throw codedError(readiness.code);
      if (trustedLogin !== prerequisites.identity || !audioDeviceSelectionReady ||
          selectedAudioDeviceNames !== prerequisites.audioSelection ||
          audioDeviceSelectionInFlight || [...activeInvokes].some((entry) => entry.command === 1 || entry.command === 5)) {
        throw codedError(!trustedLogin ? "group_login_unverified" : "group_audio_device_selection_required");
      }
      if (!isDistinctVirtualAudioSelection(selectedAudioDeviceNames)) throw codedError("group_audio_device_route_not_virtual");
      if (privateCall) throw codedError("group_private_call_pending");
      if (roomResult.roomInfo?.userNum <= 0) throw codedError("group_room_not_existing");
      if (roomResult.hostGeneration !== hostGeneration || roomResult.id !== body.id || roomResult.groupId !== body.groupId ||
          roomResult.diagnosticOnly !== true || roomResult.nativeJoinVerified !== false ||
          roomResult.roomInfo?.sceneId !== GROUP_ROOM_SCENE || roomResult.roomInfo?.relationId !== body.groupId) {
        throw codedError("group_room_metadata_invalid");
      }

      const params = [JSON.stringify({
        scene_id: GROUP_ROOM_SCENE,
        self_uid: prerequisites.identity.selfUid,
        invite_count: 0,
        invite_uids: [],
        relation_id: body.groupId,
        invite_original: 3,
        sub_business_type: 1,
        invite_reason: 0,
        use_ntrtc_dsp: false,
        c2c_extend_params: {},
        opensdk_enter_room_params: {},
      })];
      const receipt = await postPluginCommand({
        command: GROUP_START_COMMAND,
        id: body.id,
        params,
        onDispatch() {
          dispatchStarted = true;
          reservation.startMayHavePosted = true;
          if (groupReservation === reservation && reservation.state === "starting") reservation.state = "starting_post";
        },
      });
      if (groupReservation === reservation) {
        reservation.startPosted = true;
        if (reservation.state === "starting_post") reservation.state = "reserved";
      }
      return receipt;
    } catch (error) {
      if (reservation && groupReservation === reservation) {
        if (dispatchStarted && !reservation.quitMayHavePosted) {
          reservation.state = "quarantined";
          reservation.quarantineCode = safeFailureCode(error, "group_start_outcome_unknown");
        } else if (!dispatchStarted) {
          groupReservation = null;
        }
      }
      if (!dispatchStarted) throw groupControlPreDispatchError(error, body.id, GROUP_START_COMMAND, "group_start_failed");
      throw error;
    }
  }

  async function handleGroupQuit(requestBody) {
    const body = validateGroupQuitBody(requestBody, hostGeneration);
    let reservation = null;
    let dispatchStarted = false;
    let quitStateChanged = false;
    let creatorCleanupDuringStart = false;
    try {
      if (!verifiedGroupCallProtocol) throw codedError("group_call_protocol_unverified");
      const readiness = currentReadiness();
      if (!readiness.ready) throw codedError(readiness.code);
      reservation = groupReservation;
      if (!reservation || reservation.groupId !== body.groupId || reservation.startRequestId !== body.startRequestId ||
          !reservation.startMayHavePosted) throw codedError("group_call_unowned");
      creatorCleanupDuringStart = reservation.state === "starting_post" &&
        reservation.nativeCreatorEntryProof?.groupId === reservation.groupId &&
        reservation.nativeCreatorEntryProof?.startRequestId === reservation.startRequestId;
      if (reservation.state === "starting" || (reservation.state === "starting_post" && !creatorCleanupDuringStart) || reservation.quitInFlight ||
          reservation.quitPosted) throw codedError("group_start_busy");
      if (groupRoomInfoQuery.status().pending) throw codedError("group_room_query_busy");

      reservation.quitInFlight = true;
      reservation.state = "quitting";
      quitStateChanged = true;
      const identityBeforeReadback = trustedLogin;
      const devicesBeforeReadback = selectedAudioDeviceNames;
      const entrySequenceBeforeReadback = reservation.nativeEntrySequence;
      reservation.localRoomWitness = null;
      const readbackEligible = verifiedLocalRoomIdProtocol && reservation.nativeEntryProof &&
        reservation.localEntryEpochEligible === true && trustedLogin && audioDeviceSelectionReady && !privateCall;
      // The Bridge's existing Quit HTTP timeout is 4s. The optional witness and
      // renderer post share a shorter budget rather than extending that timeout.
      const quitPostDeadlineMs = readbackEligible ? Date.now() + 3500 : null;
      if (readbackEligible) {
        await prepareLocalRoomWitness(reservation);
        if (groupReservation !== reservation || !currentReadiness().ready || privateCall ||
            trustedLogin !== identityBeforeReadback || selectedAudioDeviceNames !== devicesBeforeReadback ||
            reservation.nativeEntrySequence !== entrySequenceBeforeReadback) throw codedError("group_call_unowned");
      }
      const quitPostBudgetMs = quitPostDeadlineMs === null ? requestTimeoutMs :
        Math.min(requestTimeoutMs, quitPostDeadlineMs - Date.now());
      if (quitPostBudgetMs <= 0) throw codedError("group_call_unowned");
      const receipt = await postPluginCommand({
        command: GROUP_QUIT_COMMAND,
        id: body.id,
        params: [GROUP_ROOM_SCENE, 0],
        timeoutMs: quitPostBudgetMs,
        onDispatch() {
          dispatchStarted = true;
          reservation.quitMayHavePosted = true;
          reservation.quitPosted = false;
          reservation.quitRequestId = body.id;
          reservation.quitTerminalSequenceStart = groupTerminalSequence;
          reservation.terminalAfterQuit = null;
          reservation.quitAckProof = null;
        },
      });
      if (groupReservation === reservation) {
        reservation.quitInFlight = false;
        reservation.quitPosted = true;
        reservation.state = "quitting";
      }
      return receipt;
    } catch (error) {
      if (reservation && quitStateChanged && groupReservation === reservation) {
        reservation.quitInFlight = false;
        if (dispatchStarted) {
          reservation.state = "quarantined";
          reservation.quarantineCode = safeFailureCode(error, "group_quit_outcome_unknown");
        } else {
          reservation.state = reservation.startPosted ? "reserved" :
            creatorCleanupDuringStart ? "starting_post" : "quarantined";
        }
      }
      if (!dispatchStarted) throw groupControlPreDispatchError(error, body.id, GROUP_QUIT_COMMAND, "group_quit_failed");
      throw error;
    }
  }

  function validateOutput(frame) {
    if (!ownKeysExactly(frame, ["command", "id", "value"]) &&
        !ownKeysExactly(frame, ["command", "id", "value", "hostGeneration"])) throw codedError("invalid_output_shape");
    if (Object.prototype.hasOwnProperty.call(frame, "hostGeneration") && frame.hostGeneration !== hostGeneration) {
      throw codedError("generation_mismatch");
    }
    if (!Number.isSafeInteger(frame.command) || frame.command < 1) throw codedError("invalid_output_command");
    if (!Number.isSafeInteger(frame.id) || frame.id < 0) throw codedError("invalid_output_id");
    const normalizedFrame = {
      command: frame.command,
      id: frame.id,
      value: frame.value,
      hostGeneration,
    };
    let encoded;
    try { encoded = Buffer.from(JSON.stringify(normalizedFrame), "utf8"); }
    catch { throw codedError("invalid_output_value"); }
    if (encoded.byteLength > MAX_FRAME_BYTES) throw codedError("output_frame_too_large");
    return { encoded, frame: normalizedFrame };
  }

  function rejectQueuedOutput(item, code) {
    item.reject(codedError(code));
  }

  async function drainOutputQueue() {
    while (outputQueue.length && !stopped && !outputFailed) {
      const item = outputQueue[0];
      const controller = new AbortController();
      outputInFlight = { controller, item };
      let timer = null;
      try {
        const sinkResult = Promise.resolve().then(() => sendOutput(item.frame, {
          signal: controller.signal,
          deadlineMs: Date.now() + outputTimeoutMs,
        }));
        const timeout = new Promise((_, reject) => {
          timer = setTimeout(() => {
            reject(codedError("output_forward_timeout"));
            controller.abort();
          }, outputTimeoutMs);
          timer.unref?.();
        });
        const stoppedSignal = new Promise((_, reject) => {
          controller.signal.addEventListener("abort", () => {
            if (stopped) reject(codedError("host_stopped"));
          }, { once: true });
        });
        await Promise.race([sinkResult, timeout, stoppedSignal]);
        if (stopped) throw codedError("host_stopped");
        outputForwarded += 1;
        item.resolve({ forwarded: true });
      } catch (error) {
        const code = safeFailureCode(error, "output_forward_failed");
        if (code !== "host_stopped") {
          outputFailed = true;
          fail(code === "output_forward_timeout" ? code : "output_forward_failed");
        }
        item.reject(codedError(code === "host_stopped" ? code : code === "output_forward_timeout" ? code : "output_forward_failed"));
      } finally {
        if (timer) clearTimeout(timer);
        if (outputQueue[0] === item) outputQueue.shift();
        queuedOutputBytes -= item.byteLength;
        outputInFlight = null;
      }
      if (outputFailed || stopped) break;
    }
    if (stopped || outputFailed) {
      const failureCode = stopped ? "host_stopped" : "output_forward_failed";
      while (outputQueue.length) {
        const item = outputQueue.shift();
        queuedOutputBytes -= item.byteLength;
        rejectQueuedOutput(item, failureCode);
      }
    }
  }

  function ensureOutputDrain() {
    if (outputDrainPromise) return outputDrainPromise;
    outputDrainPromise = drainOutputQueue().finally(() => {
      outputDrainPromise = null;
      if (outputQueue.length && !stopped && !outputFailed) void ensureOutputDrain();
    });
    return outputDrainPromise;
  }

  function observeNativeControlOutput(frame) {
    const key = { 5: "acceptAck", 55: "signalAck", 20003: "error", 20004: "entry", 20005: "timeout",
      20006: "invite", 20011: "roomDestroyed", 20017: "roomClosed" }[frame.command];
    if (!key) return;
    const dispatch = nativeControlPosts.get(`${frame.command}:${frame.id}`);
    const requestMatched = Boolean(dispatch && !dispatch.reused && !dispatch.observed && Date.now() - dispatch.atMs <= 5000);
    if (dispatch) dispatch.observed = true;
    const array = Array.isArray(frame.value) ? frame.value : null;
    const first = array?.[0];
    const error = frame.command === 20003 && isDenseCallbackArray(array, 3) &&
      stringWithin(array[2], GROUP_ERROR_MESSAGE_MAX_BYTES, { nonempty: false })
      ? extractGroupErrorCode(array[2]) : { label: null, code: null };
    nativeControlObservations[key] = {
      diagnosticOnly: true, count: Math.min(Number.MAX_SAFE_INTEGER, (nativeControlObservations[key]?.count || 0) + 1),
      lastObservedAtMs: Date.now(),
      requestMatched, firstInteger: Number.isInteger(first) && first >= -0x80000000 && first <= 0x7fffffff ? first : null,
      returnCode: [5, 20004].includes(frame.command) && Number.isInteger(first) && first >= -0x80000000 && first <= 0x7fffffff ? first : null,
      ...(frame.command === 20003 ? { errorCode: error.code, errorCodeLabel: error.label } : {}),
      valueKind: array ? "array" : frame.value === null ? "null" : typeof frame.value,
      arrayLength: array ? Math.min(array.length, 8192) : null,
    };
  }

  async function publishOutput(output, traceMetadata = {}) {
    if (stopped) throw codedError("host_stopped");
    if (outputFailed) throw codedError("output_forward_failed");
    const validated = validateOutput(output);
    observeNativeControlOutput(validated.frame);
    if (validated.frame.command === GROUP_ENTER_OUTPUT_COMMAND) roomAdmissionMayHavePosted = true;
    outputFrameSequence = outputFrameSequence >= Number.MAX_SAFE_INTEGER ? 1 : outputFrameSequence + 1;
    observeTypedGroupRoomQueryOutput(validated.frame, outputFrameSequence);
    observeGroupInviteOutput(validated.frame, outputFrameSequence);
    try { tracePrivateEndOutput(validated.frame, traceMetadata?.rawIdState); }
    catch { /* Sanitizing diagnostic data cannot reject an otherwise valid SDK output. */ }
    observeAudioDeviceOutput(validated.frame);
    observeLoginAck(validated.frame);
    observePrivateCallOutput(validated.frame);
    observeGroupCallOutput(validated.frame);
    chattingRoomIdQuery.observeOutput(validated.frame);
    groupRoomInfoQuery.observeOutput(validated.frame);
    // These correlated replies belong to a Host-local read operation. The
    // typed HTTP response carries its result; Bridge has no command-14 waiter.
    if (chattingRoomIdQuery.ownsOutput(validated.frame)) return { ok: true };
    if (queuedOutputBytes + validated.encoded.byteLength > maxOutputQueueBytes) {
      outputFailed = true;
      fail("output_queue_full");
      if (outputQueue.length) void ensureOutputDrain();
      throw codedError("output_queue_full");
    }
    queuedOutputBytes += validated.encoded.byteLength;
    const result = new Promise((resolve, reject) => {
      outputQueue.push({ frame: JSON.parse(validated.encoded.toString("utf8")), byteLength: validated.encoded.byteLength, resolve, reject });
    });
    void ensureOutputDrain();
    return result;
  }

  async function handleRequest(request, response) {
    let url;
    try { url = new URL(request.url || "/", `http://${LOOPBACK}:${actualListenPort || 80}`); }
    catch { sendJson(response, 400, { ok: false, code: "invalid_path" }); return; }

    if (!authenticate(request)) {
      sendJson(response, 401, { ok: false, code: "unauthorized" });
      return;
    }

    if (request.method === "GET" && url.pathname === "/healthz") {
      const status = snapshot();
      sendJson(response, status.ready ? 200 : 503, { ok: status.ready, status });
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/invoke") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleInvoke(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "invoke_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/audio/devices") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleAudioDevices(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "invoke_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/audio/select") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleAudioDeviceSelection(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "audio_device_selection_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && ["/v1/group/quit-absence", "/v1/group/remote-absence", "/v1/group/clear-absence"].includes(url.pathname)) {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) throw codedError("content_type_required");
        const body = await readJsonBody(request);
        sendJson(response, 200, await (url.pathname !== "/v1/group/quit-absence"
          ? handleGroupRemoteAbsence(body, url.pathname === "/v1/group/clear-absence") : handleGroupQuitAbsence(body)));
      } catch (error) {
        const code = safeFailureCode(error, "group_local_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/private/ended-room-absence") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        sendJson(response, 200, await handlePrivateEndedRoomAbsence(await readJsonBody(request)));
      } catch (error) {
        const code = safeFailureCode(error, "private_local_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/private/reject-absence") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        sendJson(response, 200, await handlePrivateRejectAbsence(await readJsonBody(request)));
      } catch (error) {
        const code = safeFailureCode(error, "private_local_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/private/current-room-id") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        sendJson(response, 200, await handlePrivateChattingRoomId(body));
      } catch (error) {
        const code = safeFailureCode(error, "private_local_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/group/current-room-id") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        sendJson(response, 200, await handleChattingRoomId(body));
      } catch (error) {
        const code = safeFailureCode(error, "group_local_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/group/room-info") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleGroupRoomInfo(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "group_room_query_failed");
        sendJson(response, requestErrorStatus(code), { ok: false, code });
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/group/accept") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleGroupAccept(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "group_accept_failed");
        sendJson(response, requestErrorStatus(code), groupControlErrorBody(error, code, GROUP_ACCEPT_COMMAND));
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/group/start") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleGroupStart(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "group_start_failed");
        sendJson(response, requestErrorStatus(code), groupControlErrorBody(error, code, GROUP_START_COMMAND));
      }
      return;
    }

    if (request.method === "POST" && url.pathname === "/v1/group/quit") {
      try {
        const contentType = request.headers["content-type"];
        if (typeof contentType !== "string" || !/^application\/json(?:\s*;|\s*$)/i.test(contentType)) {
          throw codedError("content_type_required");
        }
        const body = await readJsonBody(request);
        const result = await handleGroupQuit(body);
        sendJson(response, 200, result);
      } catch (error) {
        const code = safeFailureCode(error, "group_quit_failed");
        sendJson(response, requestErrorStatus(code), groupControlErrorBody(error, code, GROUP_QUIT_COMMAND));
      }
      return;
    }

    sendJson(response, 404, { ok: false, code: "not_found" });
  }

  async function start() {
    if (stopped) throw codedError("host_stopped");
    if (server?.listening) return snapshot();
    if (startPromise) return startPromise;
    startPromise = (async () => {
      server = http.createServer((request, response) => {
        void handleRequest(request, response).catch(() => {
          sendJson(response, 500, { ok: false, code: "host_failure" });
        });
      });
      server.requestTimeout = requestTimeoutMs + 1000;
      server.headersTimeout = Math.max(1000, requestTimeoutMs + 2000);
      await new Promise((resolve, reject) => {
        const onError = (error) => {
          server?.removeListener("listening", onListening);
          reject(codedError(safeFailureCode(error, "host_listen_failed")));
        };
        const onListening = () => {
          server?.removeListener("error", onError);
          const address = server.address();
          if (!address || typeof address === "string" || address.address !== LOOPBACK) {
            reject(codedError("host_loopback_bind_failed"));
            return;
          }
          actualListenPort = address.port;
          resolve();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(listenPort, LOOPBACK);
      });
      if (stopped) {
        await new Promise((resolve) => server.close(() => resolve()));
        throw codedError("host_stopped");
      }
      return snapshot();
    })();
    try {
      return await startPromise;
    } catch (error) {
      startPromise = null;
      if (server && !server.listening) server = null;
      throw error;
    }
  }

  async function stop() {
    if (stopped) return snapshot();
    stopped = true;
    sdkInfoStartupState = "stopped";
    trustedLogin = null;
    loginAttempt = null;
    clearAudioDeviceSelectionProof();
    privateCall = null;
    if (groupReservation) groupReservation.groupNativeObservation = null;
    activeTypedGroupRoomQuery = null;
    lastSuccessfulTypedGroupRoomQuery = null;
    groupInviteObservation = null;
    pendingGroupInvite = null;
    groupInviteBlockedThroughQueryOutputSequence = outputFrameSequence;
    consumedGroupInviteFingerprints.clear();
    expirePrivateEndTraceWindows();
    for (const trace of [...privateEndTraceWindows]) closePrivateEndTraceWindow(trace, "host_stopped");
    groupRoomInfoQuery.stop();
    chattingRoomIdQuery.stop();
    if (audioDeviceQueryTicket) clearAudioDeviceQueryTicket(audioDeviceQueryTicket, codedError("host_stopped"));
    for (const item of activeInvokes) item.controller.abort();
    if (outputInFlight) outputInFlight.controller.abort();
    if (startPromise) await startPromise.catch(() => {});
    if (server?.listening) {
      const closePromise = new Promise((resolve) => server.close(() => resolve()));
      await closePromise;
    }
    if (outputDrainPromise) await outputDrainPromise;
    return snapshot();
  }

  return Object.freeze({ start, stop, status: snapshot, publishOutput });
}

module.exports = {
  ALLOWED_COMMANDS,
  LOOPBACK,
  MAX_FRAME_BYTES,
  AUDIO_DEVICE_QUERY_COMMAND,
  AUDIO_DEVICE_SELECT_COMMAND,
  AUDIO_DEVICE_INFO_OUTPUT_COMMAND,
  validateAudioDeviceSelectionBody,
  validateAudioDeviceTail,
  createHostTransport,
  validateAudioDevicesBody,
  validateInvokeBody,
  validateParams,
};
