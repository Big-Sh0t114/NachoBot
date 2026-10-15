"use strict";

const http = require("node:http");
const { timingSafeEqual } = require("node:crypto");

const MAX_BODY_BYTES = 1024 * 1024;
const MAX_GROUP_ID_BYTES = 20;
const MAX_GROUP_USERS = 1000;
// The Host's SDK query expires at 10s; its typed reply must arrive before the
// HTTP envelope expires. This does not extend the SDK proof deadline.
const GROUP_ROOM_QUERY_TIMEOUT_MS = 11_000;
const GROUP_START_TIMEOUT_MS = 14_500;
const GROUP_QUIT_TIMEOUT_MS = 4_000;
const VALIDATED_NEGATIVE_GROUP_RECEIPT = Symbol("validated_negative_group_receipt");

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function validGeneration(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function validGroupId(value) {
  return typeof value === "string" && Buffer.byteLength(value, "utf8") <= MAX_GROUP_ID_BYTES &&
    /^[1-9][0-9]{0,19}$/.test(value);
}

function exactKeys(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Reflect.ownKeys(value);
  return actual.length === keys.length && actual.every((key) => typeof key === "string" && keys.includes(key));
}

function groupDispatchGuard(options) {
  if (options === undefined) return null;
  try {
    if (!options || typeof options !== "object" || Array.isArray(options)) throw codedError("invalid_invoke_guard");
    const prototype = Object.getPrototypeOf(options);
    if (prototype !== Object.prototype && prototype !== null) throw codedError("invalid_invoke_guard");
    const keys = Reflect.ownKeys(options);
    if (keys.length !== 1 || keys[0] !== "assertCurrent") throw codedError("invalid_invoke_guard");
    const descriptor = Object.getOwnPropertyDescriptor(options, "assertCurrent");
    if (!descriptor || !("value" in descriptor) || typeof descriptor.value !== "function") {
      throw codedError("invalid_invoke_guard");
    }
    return descriptor.value;
  } catch { throw codedError("invalid_invoke_guard"); }
}

function assertGroupDispatchCurrent(assertCurrent) {
  if (!assertCurrent) return;
  const result = assertCurrent();
  if (result && (typeof result === "object" || typeof result === "function") &&
      typeof result.then === "function") {
    try { Promise.resolve(result).catch(() => {}); } catch { /* Consume an optional guard thenable. */ }
    throw codedError("host_invoke_guard_async");
  }
  if (result !== true) throw codedError("host_invoke_guard_rejected");
}

function validateGroupRoomInfo(payload, expected) {
  if (!exactKeys(payload, ["ok", "hostGeneration", "id", "groupId", "diagnosticOnly", "nativeJoinVerified", "roomInfo"]) ||
      payload.ok !== true || payload.hostGeneration !== expected.hostGeneration || payload.id !== expected.id ||
      payload.groupId !== expected.groupId || payload.diagnosticOnly !== true || payload.nativeJoinVerified !== false) {
    return false;
  }
  const roomInfo = payload.roomInfo;
  if (!exactKeys(roomInfo, ["sceneId", "relationId", "userNum", "userList"]) || roomInfo.sceneId !== 3 ||
      roomInfo.relationId !== expected.groupId || !Number.isSafeInteger(roomInfo.userNum) ||
      roomInfo.userNum < 0 || roomInfo.userNum > MAX_GROUP_USERS || !Array.isArray(roomInfo.userList) ||
      roomInfo.userList.length > MAX_GROUP_USERS || Reflect.ownKeys(roomInfo.userList).length !== roomInfo.userList.length + 1) {
    return false;
  }
  const seen = new Set();
  for (let index = 0; index < roomInfo.userList.length; index += 1) {
    if (!Object.hasOwn(roomInfo.userList, index)) return false;
    const user = roomInfo.userList[index];
    if (!exactKeys(user, ["uid", "accountType"]) || typeof user.uid !== "string" || !user.uid ||
        Buffer.byteLength(user.uid, "utf8") > 128 || !/^[A-Za-z0-9._:-]+$/.test(user.uid) ||
        !Number.isSafeInteger(user.accountType) || user.accountType < 0 || user.accountType > 65_535 || seen.has(user.uid)) {
      return false;
    }
    seen.add(user.uid);
  }
  return true;
}

function isExactNegativeGroupReceipt(payload, expected) {
  return exactKeys(payload, ["ok", "code", "id", "command", "posted"]) &&
    payload.ok === false && typeof payload.code === "string" && /^[a-z0-9_:-]{1,64}$/i.test(payload.code) &&
    payload.id === expected.id && payload.command === expected.command && payload.posted === false;
}

function sameSecret(left, right) {
  const a = Buffer.from(String(left));
  const b = Buffer.from(String(right));
  return a.length === b.length && timingSafeEqual(a, b);
}

function sendJson(response, status, body) {
  if (response.destroyed || response.writableEnded) return;
  const encoded = Buffer.from(JSON.stringify(body));
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": encoded.length,
    "cache-control": "no-store",
  });
  response.end(encoded);
}

function readJsonBody(request, limit = MAX_BODY_BYTES, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    let length = 0;
    const chunks = [];
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(codedError("request_body_timeout"));
      request.destroy();
    }, timeoutMs);
    timer.unref?.();
    const fail = (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(codedError(code));
    };
    const declaredLength = Number(request.headers["content-length"]);
    if (Number.isFinite(declaredLength) && declaredLength > limit) {
      fail("request_body_too_large");
      request.resume();
      return;
    }
    request.on("data", (chunk) => {
      length += chunk.length;
      if (length > limit) {
        chunks.length = 0;
        fail("request_body_too_large");
        return;
      }
      if (!settled) chunks.push(chunk);
    });
    request.once("error", () => {
      fail("request_read_failed");
    });
    request.once("aborted", () => fail("request_aborted"));
    request.once("end", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { resolve(JSON.parse(Buffer.concat(chunks, length).toString("utf8"))); }
      catch { reject(codedError("invalid_json")); }
    });
  });
}

function authMatches(request, token) {
  const header = request.headers.authorization;
  if (typeof header !== "string" || !header.startsWith("Bearer ")) return false;
  return sameSecret(header.slice(7), token);
}

function hostReady(payload, expectedGeneration, expectedTargetPid) {
  const state = payload?.status && typeof payload.status === "object" ? payload.status : payload;
  return state?.ready === true && state?.hostGeneration === expectedGeneration &&
    (expectedTargetPid === undefined || state?.targetPid === expectedTargetPid) &&
    state?.pluginFound === true && state?.hasPostMessage === true &&
    state?.rendererGone !== true && state?.stopped !== true && !state?.outputFailed && !state?.readinessError;
}

function callbackListenerReady(payload, expectedGeneration) {
  return payload?.ok === true && payload?.callbackReady === true &&
    payload?.hostGeneration === expectedGeneration && payload?.started === true &&
    payload?.subscriberReady === true && payload?.stopping === false && payload?.stopped === false;
}

function createHostClient({
  token,
  listenPort,
  outputPort,
  hostGeneration,
  targetPid,
  requestTimeoutMs = 2500,
  selectionTimeoutMs = 6500,
  outputTimeoutMs = 2500,
  healthPollMs = 1000,
  bodyTimeoutMs = 5000,
  maxQueueItems = 64,
  maxQueueBytes = 2 * 1024 * 1024,
  maxOutputBytes = MAX_BODY_BYTES,
  onFailure = () => {},
} = {}) {
  if (typeof token !== "string" || token.length < 24 || token.length > 256) throw codedError("invalid_host_token");
  if (!Number.isSafeInteger(listenPort) || listenPort < 1 || listenPort > 65535 ||
      !Number.isSafeInteger(outputPort) || outputPort < 0 || outputPort > 65535) throw codedError("invalid_host_port");
  if (!validGeneration(hostGeneration)) throw codedError("invalid_host_generation");
  if (targetPid !== undefined && (!Number.isSafeInteger(targetPid) || targetPid < 1 || targetPid > 0x7fffffff)) {
    throw codedError("invalid_host_pid");
  }
  if (![requestTimeoutMs, selectionTimeoutMs, outputTimeoutMs, healthPollMs, bodyTimeoutMs].every((value) => Number.isSafeInteger(value) && value >= 100 && value <= 60_000)) {
    throw codedError("invalid_host_timeout");
  }
  if (!Number.isSafeInteger(maxQueueItems) || maxQueueItems < 1 || maxQueueItems > 1024 ||
      !Number.isSafeInteger(maxQueueBytes) || maxQueueBytes < 1024 || maxQueueBytes > 64 * 1024 * 1024 ||
      !Number.isSafeInteger(maxOutputBytes) || maxOutputBytes < 1 || maxOutputBytes > MAX_BODY_BYTES) throw codedError("invalid_host_queue_limit");

  let server = null;
  let started = false;
  let stopping = false;
  let stopped = false;
  let available = false;
  let groupCallProtocolAvailable = false;
  let groupLocalQuitProofAvailable = false;
  let healthTimer = null;
  let healthInFlight = false;
  let healthPromise = null;
  let queue = [];
  let queuedBytes = 0;
  let queueBusy = false;
  let activeRequest = null;
  const activeRequests = new Set();
  let outputCount = 0;
  let invokeCount = 0;
  let selectionCount = 0;
  let failureCount = 0;
  let lastFailureCode = null;
  let boundOutputPort = outputPort;
  const subscribers = new Set();
  const incomingSockets = new Set();

  function status() {
    return {
      started,
      stopping,
      stopped,
      callbackReady: started && !stopping && !stopped && subscribers.size > 0,
      available,
      groupCallProtocolAvailable,
      groupLocalQuitProofAvailable,
      hostGeneration,
      ...(targetPid !== undefined ? { targetPid } : {}),
      outputPort: boundOutputPort,
      counts: { invokes: invokeCount, deviceSelections: selectionCount, outputs: outputCount, queued: queue.length, queuedBytes, failures: failureCount },
      lastFailureCode,
    };
  }

  function notifyFailure(code, operation) {
    lastFailureCode = code;
    failureCount += 1;
    try {
      const result = onFailure({ code, hostGeneration, ...(operation ? { operation } : {}) });
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
    } catch { /* diagnostics cannot break transport */ }
  }

  async function notify(event) {
    const completions = [];
    for (const observer of [...subscribers]) {
      try {
        const result = observer(event);
        completions.push(Promise.resolve(result));
      } catch (error) { completions.push(Promise.reject(error)); }
    }
    const outcomes = await Promise.allSettled(completions);
    return {
      failed: outcomes.some((outcome) => outcome.status === "rejected"),
      results: outcomes.filter((outcome) => outcome.status === "fulfilled").map((outcome) => outcome.value),
    };
  }

  function setAvailable(next, code = null) {
    const normalized = Boolean(next);
    if (!normalized) { groupCallProtocolAvailable = false; groupLocalQuitProofAvailable = false; }
    if (available === normalized) {
      if (!normalized && code) lastFailureCode = code;
      return;
    }
    available = normalized;
    if (normalized) lastFailureCode = null;
    else if (code) lastFailureCode = code;
    notify({ type: "availability", available, hostGeneration, code: available ? null : code });
  }

  function parseResponse(raw, fallbackCode) {
    let parsed;
    try { parsed = JSON.parse(raw.toString("utf8")); }
    catch { throw codedError(fallbackCode); }
    return parsed;
  }

  function requestHost(method, pathname, body, timeoutMs, fallbackCode = "host_request_failed", negativeReceiptExpectation = null) {
    const bodyBytes = body === undefined ? null : Buffer.from(JSON.stringify(body));
    if (bodyBytes && bodyBytes.length > MAX_BODY_BYTES) return Promise.reject(codedError("request_body_too_large"));
    return new Promise((resolve, reject) => {
      const request = http.request({
        host: "127.0.0.1",
        port: listenPort,
        method,
        path: pathname,
        headers: {
          authorization: `Bearer ${token}`,
          ...(bodyBytes ? { "content-type": "application/json", "content-length": bodyBytes.length } : {}),
        },
      }, (response) => {
        let size = 0;
        const chunks = [];
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > MAX_BODY_BYTES) {
            request.destroy(codedError("host_response_too_large"));
            return;
          }
          chunks.push(chunk);
        });
        response.once("end", () => {
          try {
            const payload = parseResponse(Buffer.concat(chunks, size), "invalid_host_response");
            if (response.statusCode < 200 || response.statusCode >= 300) {
              const error = codedError(typeof payload?.code === "string" && /^[a-z0-9_:-]{1,64}$/i.test(payload.code)
                ? payload.code : fallbackCode);
              error.statusCode = response.statusCode;
              if (negativeReceiptExpectation && isExactNegativeGroupReceipt(payload, negativeReceiptExpectation)) {
                Object.defineProperty(error, VALIDATED_NEGATIVE_GROUP_RECEIPT, { value: true });
              }
              reject(error);
            } else resolve(payload);
          } catch (error) { reject(error); }
        });
        response.once("aborted", () => reject(codedError("host_response_aborted")));
      });
      activeRequest = request;
      activeRequests.add(request);
      const timer = setTimeout(() => request.destroy(codedError("host_request_timeout")), timeoutMs);
      timer.unref?.();
      request.once("error", (error) => reject(codedError(
        error?.code === "host_request_timeout" ? "host_request_timeout" :
          error?.code === "host_client_stopped" ? "host_client_stopped" : "host_disconnected",
      )));
      request.once("close", () => {
        clearTimeout(timer);
        activeRequests.delete(request);
        if (activeRequest === request) activeRequest = null;
      });
      if (bodyBytes) request.write(bodyBytes);
      request.end();
    });
  }

  function checkHealth() {
    if (!started || stopping || stopped) return Promise.resolve(available);
    if (healthPromise) return healthPromise;
    healthInFlight = true;
    healthPromise = (async () => {
      try {
        const response = await requestHost("GET", "/healthz", undefined, requestTimeoutMs);
        const ready = hostReady(response, hostGeneration, targetPid);
        const healthStatus = response?.status && typeof response.status === "object" ? response.status : response;
        groupCallProtocolAvailable = ready && healthStatus?.groupCallProtocolAvailable === true;
        groupLocalQuitProofAvailable = groupCallProtocolAvailable && healthStatus?.groupLocalQuitProofAvailable === true;
        setAvailable(ready, ready ? null : "host_not_ready");
        return ready;
      } catch (error) {
        groupCallProtocolAvailable = false;
        const code = error.code === "host_disconnected" ? "host_disconnected" : error.code;
        notifyFailure(code, "health");
        setAvailable(false, code);
        return false;
      } finally {
        healthInFlight = false;
        healthPromise = null;
      }
    })();
    return healthPromise;
  }

  function outputServerHandler(request, response) {
    if (!authMatches(request, token)) {
      sendJson(response, 401, { ok: false, code: "unauthorized" });
      request.resume();
      return;
    }
    if (request.method === "GET" && request.url === "/v1/callback-ready") {
      if (request.headers["x-qqcall-host-generation"] !== String(hostGeneration)) {
        sendJson(response, 409, { ok: false, code: "generation_mismatch" });
        return;
      }
      const ready = started && !stopping && !stopped && subscribers.size > 0;
      sendJson(response, ready ? 200 : 503, {
        ok: ready,
        callbackReady: ready,
        hostGeneration,
        started,
        subscriberReady: subscribers.size > 0,
        stopping,
        stopped,
        ...(ready ? {} : { code: "callback_not_ready" }),
      });
      return;
    }
    if (request.method !== "POST" || request.url !== "/v1/avsdk/output") {
      sendJson(response, 404, { ok: false, code: "not_found" });
      request.resume();
      return;
    }
    void readJsonBody(request, maxOutputBytes, bodyTimeoutMs).then((payload) => {
      if (!payload || !Number.isSafeInteger(payload.command) || payload.command < 0 ||
          !Number.isSafeInteger(payload.id) || payload.id < 0 || payload.hostGeneration !== hostGeneration ||
          !Object.prototype.hasOwnProperty.call(payload, "value")) {
        sendJson(response, payload?.hostGeneration !== hostGeneration ? 409 : 400, {
          ok: false,
          code: payload?.hostGeneration !== hostGeneration ? "generation_mismatch" : "invalid_output",
        });
        return;
      }
      outputCount += 1;
      const event = { type: "output", command: payload.command, id: payload.id, value: payload.value, hostGeneration };
      let outputTimer;
      const completion = notify(event);
      const timedOut = new Promise((resolve) => {
        outputTimer = setTimeout(() => resolve({ kind: "timeout" }), outputTimeoutMs);
        outputTimer.unref?.();
      });
      void Promise.race([
        completion.then((outcome) => outcome.failed ? { kind: "failed" } : { kind: "ok", results: outcome.results }),
        timedOut,
      ]).then((result) => {
        clearTimeout(outputTimer);
        if (result.kind === "timeout") {
          notifyFailure("output_timeout", "output_callback");
          sendJson(response, 504, { ok: false, code: "output_timeout" });
          return;
        }
        if (result.kind === "failed") {
          notifyFailure("output_observer_failed", "output_callback");
          sendJson(response, 503, { ok: false, code: "output_observer_failed" });
          return;
        }
        const storedReceipt = result.results.find((receipt) => receipt?.receivedStored === true &&
          receipt.receivedBeforeAttach === true && Number.isSafeInteger(receipt.sequence) && receipt.sequence > 0);
        const forwardedReceipt = result.results.find((receipt) => receipt?.forwarded === true);
        sendJson(response, 202, {
          ok: true,
          accepted: true,
          ...(storedReceipt ? { receipt: { kind: "received_stored", sequence: storedReceipt.sequence } } :
            forwardedReceipt ? { receipt: { kind: "kernel_forwarded" } } : {}),
        });
      });
    }).catch((error) => {
      const code = ["request_body_too_large", "request_body_timeout", "request_aborted"].includes(error.code)
        ? error.code : "invalid_output";
      sendJson(response, code === "request_body_too_large" ? 413 : code === "request_body_timeout" ? 408 : 400, { ok: false, code });
    });
  }

  function invokeGuardFromOptions(options) {
    if (options === undefined) return null;
    try {
      if (!options || typeof options !== "object" || Array.isArray(options)) throw codedError("invalid_invoke_guard");
      const prototype = Object.getPrototypeOf(options);
      if (prototype !== Object.prototype && prototype !== null) throw codedError("invalid_invoke_guard");
      const keys = Reflect.ownKeys(options);
      if (keys.length !== 1 || keys[0] !== "assertCurrent") throw codedError("invalid_invoke_guard");
      const descriptor = Object.getOwnPropertyDescriptor(options, "assertCurrent");
      if (!descriptor || !("value" in descriptor) || typeof descriptor.value !== "function") {
        throw codedError("invalid_invoke_guard");
      }
      return descriptor.value;
    } catch {
      throw codedError("invalid_invoke_guard");
    }
  }

  function ensureGroupProtocol() {
    if (!started || stopping || stopped) throw codedError("host_client_not_running");
    if (!available) throw codedError("host_unavailable");
    if (!groupCallProtocolAvailable) throw codedError("group_protocol_unavailable");
  }

  async function queryGroupRoomInfo(input, options = undefined) {
    ensureGroupProtocol();
    const assertCurrent = groupDispatchGuard(options);
    if (!input || !Number.isSafeInteger(input.id) || input.id <= 0 || input.hostGeneration !== hostGeneration ||
        !validGroupId(input.groupId)) throw codedError("invalid_group_room_query");
    assertGroupDispatchCurrent(assertCurrent);
    const expected = { id: input.id, hostGeneration, groupId: input.groupId };
    try {
      const payload = await requestHost("POST", "/v1/group/room-info", expected, GROUP_ROOM_QUERY_TIMEOUT_MS, "group_room_query_failed");
      if (!validateGroupRoomInfo(payload, expected)) throw codedError("group_room_query_receipt_invalid");
      return payload;
    } catch (error) {
      notifyFailure(error.code || "group_room_query_failed", "group_room_query");
      if (error.code === "host_disconnected") setAvailable(false, error.code);
      throw error;
    }
  }

  async function startGroup(input, options = undefined) {
    ensureGroupProtocol();
    const assertCurrent = groupDispatchGuard(options);
    if (!input || !Number.isSafeInteger(input.id) || input.id <= 0 || input.hostGeneration !== hostGeneration ||
        !validGroupId(input.groupId)) throw codedError("invalid_group_start");
    assertGroupDispatchCurrent(assertCurrent);
    const body = { id: input.id, hostGeneration, groupId: input.groupId };
    try {
      const payload = await requestHost("POST", "/v1/group/start", body, GROUP_START_TIMEOUT_MS, "group_start_failed", {
        id: body.id, command: 4,
      });
      if (!exactKeys(payload, ["posted", "id", "command"]) || typeof payload.posted !== "boolean" ||
          payload.id !== body.id || payload.command !== 4) throw codedError("group_start_receipt_invalid");
      if (payload.posted) invokeCount += 1;
      else notifyFailure("group_start_refused", "group_start");
      return { posted: payload.posted, id: body.id, command: 4 };
    } catch (error) {
      if (error?.[VALIDATED_NEGATIVE_GROUP_RECEIPT] === true) {
        notifyFailure("group_start_refused", "group_start");
        return { posted: false, id: body.id, command: 4 };
      }
      notifyFailure(error.code || "group_start_failed", "group_start");
      if (error.code === "host_disconnected") setAvailable(false, error.code);
      throw error;
    }
  }

  async function quitGroup(input, options = undefined) {
    ensureGroupProtocol();
    const assertCurrent = groupDispatchGuard(options);
    if (!input || !Number.isSafeInteger(input.id) || input.id <= 0 || input.hostGeneration !== hostGeneration ||
        !validGroupId(input.groupId) || !Number.isSafeInteger(input.startRequestId) || input.startRequestId <= 0) {
      throw codedError("invalid_group_quit");
    }
    assertGroupDispatchCurrent(assertCurrent);
    const body = { id: input.id, hostGeneration, groupId: input.groupId, startRequestId: input.startRequestId };
    try {
      const payload = await requestHost("POST", "/v1/group/quit", body, GROUP_QUIT_TIMEOUT_MS, "group_quit_failed", {
        id: body.id, command: 8,
      });
      if (!exactKeys(payload, ["posted", "id", "command"]) || typeof payload.posted !== "boolean" ||
          payload.id !== body.id || payload.command !== 8) throw codedError("group_quit_receipt_invalid");
      if (payload.posted) invokeCount += 1;
      else notifyFailure("group_quit_refused", "group_quit");
      return { posted: payload.posted, id: body.id, command: 8 };
    } catch (error) {
      if (error?.[VALIDATED_NEGATIVE_GROUP_RECEIPT] === true) {
        notifyFailure("group_quit_refused", "group_quit");
        return { posted: false, id: body.id, command: 8 };
      }
      notifyFailure(error.code || "group_quit_failed", "group_quit");
      if (error.code === "host_disconnected") setAvailable(false, error.code);
      throw error;
    }
  }

  async function confirmPrivateRejectAbsence(input, options = undefined) {
    const guard = options?.assertCurrent;
    const assertCurrent = () => {
      if (!available || stopped || typeof guard !== "function") {
        throw codedError("private_end_scope_unverified");
      }
      const result = guard();
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      if (result !== true) throw codedError("private_end_scope_unverified");
    };
    if (!exactKeys(input, ["id", "hostGeneration", "peerUid", "rejectRequestId"]) ||
        input.hostGeneration !== hostGeneration || !validGeneration(input.id) ||
        !validGeneration(input.rejectRequestId) || typeof input.peerUid !== "string" ||
        !/^u_[A-Za-z0-9_-]{22}$/.test(input.peerUid)) throw codedError("invalid_private_reject_proof");
    assertCurrent();
    const expected = { ...input };
    const payload = await requestHost("POST", "/v1/private/reject-absence", expected, 6000, "private_local_room_query_failed");
    assertCurrent();
    if (!exactKeys(payload, ["ok", "id", "hostGeneration", "peerUid", "rejectRequestId", "confirmed", "source"]) ||
        payload.ok !== true || typeof payload.confirmed !== "boolean" ||
        !["native_reject_ack_and_sdk_room_absence", "native_reject_ack_and_no_room_admission"].includes(payload.source) ||
        !Object.keys(expected).every((key) => payload[key] === expected[key])) {
      throw codedError("private_reject_proof_invalid");
    }
    return payload;
  }

  async function confirmPrivateEndedRoomAbsence(input, options = undefined) {
    const guard = options?.assertCurrent;
    const assertCurrent = () => {
      if (!available || stopped || typeof guard !== "function") throw codedError("private_end_scope_unverified");
      const result = guard();
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      if (result !== true) throw codedError("private_end_scope_unverified");
    };
    if (!exactKeys(input, ["id", "hostGeneration", "peerUid", "enterRelationId"]) ||
        input.hostGeneration !== hostGeneration || !validGeneration(input.id) ||
        typeof input.peerUid !== "string" || !/^u_[A-Za-z0-9_-]{22}$/.test(input.peerUid) ||
        typeof input.enterRelationId !== "string" || !input.enterRelationId ||
        Buffer.byteLength(input.enterRelationId, "utf8") > 512) throw codedError("invalid_private_room_end_proof");
    assertCurrent();
    const expected = { ...input };
    const payload = await requestHost("POST", "/v1/private/ended-room-absence", expected, 6000, "private_local_room_query_failed");
    assertCurrent();
    if (!exactKeys(payload, ["ok", "id", "hostGeneration", "peerUid", "enterRelationId", "confirmed", "source"]) ||
        payload.ok !== true || typeof payload.confirmed !== "boolean" ||
        payload.source !== "native_entered_private_room_absence" ||
        !Object.keys(expected).every((key) => payload[key] === expected[key])) throw codedError("private_room_end_proof_invalid");
    return payload;
  }

  async function confirmGroupRemoteAbsence(input, options = undefined, clearOwnedRoom = false) {
    ensureGroupProtocol();
    if (!groupLocalQuitProofAvailable) throw codedError("group_local_room_protocol_unverified");
    const assertCurrent = groupDispatchGuard(options);
    if (!input || input.hostGeneration !== hostGeneration || !validGroupId(input.groupId) ||
        ![input.id, input.startRequestId].every((id) => Number.isSafeInteger(id) && id > 0)) {
      throw codedError("invalid_group_quit_proof");
    }
    const expected = { id: input.id, hostGeneration, groupId: input.groupId, startRequestId: input.startRequestId };
    assertGroupDispatchCurrent(assertCurrent);
    const payload = await requestHost("POST", clearOwnedRoom ? "/v1/group/clear-absence" : "/v1/group/remote-absence",
      expected, 4000, "group_local_room_query_failed");
    assertGroupDispatchCurrent(assertCurrent);
    if (!exactKeys(payload, [...Object.keys(expected), "ok", "confirmed", "source"]) || payload.ok !== true ||
        typeof payload.confirmed !== "boolean" || payload.source !== (clearOwnedRoom ? "native_group_clear_absence" : "native_entered_group_room_absence") ||
        !Object.keys(expected).every((key) => payload[key] === expected[key])) throw codedError("group_quit_proof_invalid");
    return payload;
  }

  async function confirmGroupQuitAbsence(input, options = undefined) {
    ensureGroupProtocol();
    if (!groupLocalQuitProofAvailable) throw codedError("group_local_room_protocol_unverified");
    const assertCurrent = groupDispatchGuard(options);
    if (!input || input.hostGeneration !== hostGeneration || !validGroupId(input.groupId) ||
        ![input.id, input.startRequestId, input.quitRequestId].every((id) => Number.isSafeInteger(id) && id > 0)) {
      throw codedError("invalid_group_quit_proof");
    }
    assertGroupDispatchCurrent(assertCurrent);
    const expected = { id: input.id, hostGeneration, groupId: input.groupId,
      startRequestId: input.startRequestId, quitRequestId: input.quitRequestId };
    const payload = await requestHost("POST", "/v1/group/quit-absence", expected, 6000, "group_local_room_query_failed");
    assertGroupDispatchCurrent(assertCurrent);
    if (!exactKeys(payload, ["ok", "id", "hostGeneration", "groupId", "startRequestId", "quitRequestId", "confirmed", "source"]) ||
        payload.ok !== true || typeof payload.confirmed !== "boolean" || payload.source !== "native_sdk_room_id_readback" ||
        !Object.keys(expected).every((key) => payload[key] === expected[key])) throw codedError("group_quit_proof_invalid");
    return payload;
  }

  function enqueueInvoke(input, options = undefined) {
    if (!started || stopping || stopped) return Promise.reject(codedError("host_client_not_running"));
    if (!available) return Promise.reject(codedError("host_unavailable"));
    let assertCurrent;
    try { assertCurrent = invokeGuardFromOptions(options); }
    catch (error) { return Promise.reject(error); }
    // Commands 8/9/10 pass through only as candidates. The authenticated Host
    // transport owns the pinned-DLL capability and exact private-call shape
    // gate, so this queue never grants protocol capability by itself.
    if (!input || ![1, 5, 55, 8, 9, 10].includes(input.command) || !Number.isSafeInteger(input.id) || input.id <= 0 ||
        !Array.isArray(input.params) || input.hostGeneration !== hostGeneration) return Promise.reject(codedError("invalid_invoke"));
    let body;
    let bytes;
    try {
      body = JSON.stringify({ command: input.command, id: input.id, params: input.params, hostGeneration });
      bytes = Buffer.byteLength(body, "utf8");
    } catch {
      return Promise.reject(codedError("invalid_invoke"));
    }
    if (bytes > MAX_BODY_BYTES) return Promise.reject(codedError("request_body_too_large"));
    if (queue.length >= maxQueueItems || queuedBytes + bytes > maxQueueBytes) return Promise.reject(codedError("host_queue_full"));
    return new Promise((resolve, reject) => {
      queuedBytes += bytes;
      queue.push({ input: { command: input.command, id: input.id, params: input.params, hostGeneration }, bytes, resolve, reject, assertCurrent });
      void pumpQueue();
    });
  }

  async function selectAudioDevices(input) {
    if (!started || stopping || stopped) throw codedError("host_client_not_running");
    if (!available) throw codedError("host_unavailable");
    if (!input || !Number.isSafeInteger(input.id) || input.id <= 0 || input.hostGeneration !== hostGeneration ||
        typeof input.inputDeviceName !== "string" || input.inputDeviceName.length < 1 || input.inputDeviceName.length > 512 ||
        typeof input.outputDeviceName !== "string" || input.outputDeviceName.length < 1 || input.outputDeviceName.length > 512) {
      throw codedError("invalid_audio_device_selection");
    }
    const body = {
      id: input.id,
      hostGeneration,
      inputDeviceName: input.inputDeviceName,
      outputDeviceName: input.outputDeviceName,
    };
    try {
      const payload = await requestHost("POST", "/v1/audio/select", body, selectionTimeoutMs, "audio_device_selection_failed");
      if (payload?.selected !== true || payload.id !== body.id || payload.command !== 102) {
        throw codedError("audio_device_selection_receipt_invalid");
      }
      selectionCount += 1;
      return { selected: true, id: body.id, command: 102 };
    } catch (error) {
      notifyFailure(error.code || "audio_device_selection_failed", "audio_device_selection");
      if (error.code === "host_disconnected") setAvailable(false, error.code);
      throw error;
    }
  }

  async function pumpQueue() {
    if (queueBusy) return;
    queueBusy = true;
    try {
      while (queue.length && !stopping && !stopped) {
        const item = queue.shift();
        let dispatchGuardFailed = false;
        try {
          if (!available) throw codedError("host_unavailable");
          if (item.assertCurrent) {
            dispatchGuardFailed = true;
            const guardResult = item.assertCurrent();
            let guardThen;
            if (guardResult && (typeof guardResult === "object" || typeof guardResult === "function")) {
              guardThen = guardResult.then;
            }
            if (typeof guardThen === "function") {
              try { Promise.resolve(guardResult).catch(() => {}); } catch { /* consume only the optional guard thenable */ }
              throw codedError("host_invoke_guard_async");
            }
            if (guardResult !== true) throw codedError("host_invoke_guard_rejected");
            dispatchGuardFailed = false;
          }
          const payload = await requestHost("POST", "/v1/invoke", item.input, requestTimeoutMs);
          if (payload?.posted !== true || payload.id !== item.input.id || payload.command !== item.input.command) {
            throw codedError("host_post_receipt_invalid");
          }
          invokeCount += 1;
          item.resolve({ posted: true, id: item.input.id, command: item.input.command });
        } catch (error) {
          if (!dispatchGuardFailed) {
            notifyFailure(error.code || "host_invoke_failed", "invoke");
            if (error.code === "host_disconnected") setAvailable(false, error.code);
          }
          item.reject(error);
        } finally {
          queuedBytes -= item.bytes;
        }
      }
    } finally {
      queueBusy = false;
    }
  }

  function subscribe(handler) {
    if (typeof handler !== "function") throw codedError("invalid_host_subscriber");
    subscribers.add(handler);
    let subscribed = true;
    try {
      const result = handler({ type: "availability", available, hostGeneration, code: lastFailureCode });
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
    } catch { /* subscriber errors are isolated */ }
    return () => {
      if (!subscribed) return false;
      subscribed = false;
      return subscribers.delete(handler);
    };
  }

  async function start() {
    if (started && !stopped) return status();
    if (stopped) throw codedError("host_client_stopped");
    await new Promise((resolve, reject) => {
      server = http.createServer(outputServerHandler);
      server.once("error", () => reject(codedError("output_listener_failed")));
      server.headersTimeout = Math.min(bodyTimeoutMs, 10_000);
      server.requestTimeout = bodyTimeoutMs + 1000;
      server.keepAliveTimeout = 1000;
      server.maxHeadersCount = 64;
      server.on("connection", (socket) => {
        incomingSockets.add(socket);
        socket.once("close", () => incomingSockets.delete(socket));
      });
      server.listen(outputPort, "127.0.0.1", resolve);
    });
    boundOutputPort = server.address().port;
    started = true;
    healthTimer = setInterval(() => { void checkHealth(); }, healthPollMs);
    healthTimer.unref?.();
    void checkHealth();
    return status();
  }

  async function stop() {
    if (stopped) return status();
    stopping = true;
    if (healthTimer) clearInterval(healthTimer);
    healthTimer = null;
    for (const request of activeRequests) request.destroy(codedError("host_client_stopped"));
    if (activeRequest) activeRequest.destroy(codedError("host_client_stopped"));
    for (const item of queue.splice(0)) {
      queuedBytes -= item.bytes;
      item.reject(codedError("host_client_stopped"));
    }
    setAvailable(false, "host_client_stopped");
    if (server) {
      const closing = new Promise((resolve) => server.close(() => resolve()));
      for (const socket of incomingSockets) socket.destroy();
      server.closeAllConnections?.();
      await closing;
    }
    server = null;
    started = false;
    stopping = false;
    stopped = true;
    subscribers.clear();
    return status();
  }

  return Object.freeze({
    start,
    stop,
    status,
    subscribe,
    invoke: enqueueInvoke,
    selectAudioDevices,
    queryGroupRoomInfo,
    startGroup,
    quitGroup,
    confirmGroupQuitAbsence,
    confirmGroupRemoteAbsence,
    clearGroupRoomAbsence: (input, options) => confirmGroupRemoteAbsence(input, options, true),
    confirmPrivateRejectAbsence,
    confirmPrivateEndedRoomAbsence,
    checkHealth,
  });
}

module.exports = { MAX_BODY_BYTES, callbackListenerReady, createHostClient, hostReady, readJsonBody };
