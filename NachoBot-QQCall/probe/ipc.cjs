"use strict";

const { randomUUID, timingSafeEqual } = require("node:crypto");
const net = require("node:net");
const { TextDecoder } = require("node:util");
const { isRendererCapabilityReport } = require("../agent/renderer-capability-probe.cjs");
const { isAsarSourceProbeReport } = require("../agent/asar-source-probe.cjs");
const { isAsarCandidateReport } = require("../agent/asar-candidate-probe.cjs");
const { isOfficialWindowReport } = require("../agent/official-window-probe.cjs");
const { isRendererScriptMetadataReport } = require("../agent/renderer-script-metadata-probe.cjs");
const { isObserverReport } = require("../agent/official-av-observer.cjs");
const { validRequest: validPrivateInviteLifecycleRequest, isLifecycleResult } = require("../agent/official-private-invite.cjs");

const SCHEMA = 1;
const MAX_FRAME_BYTES = 1024 * 1024;
const PIPE_PREFIX = "\\\\.\\pipe\\nachobot-qqcall-";
const MAX_QUEUED_BYTES = MAX_FRAME_BYTES * 2;

function isGeneration(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function validToken(token) {
  return typeof token === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(token);
}

function equalToken(expected, received) {
  if (!validToken(expected) || !validToken(received)) return false;
  const left = Buffer.from(expected, "ascii");
  const right = Buffer.from(received, "ascii");
  return left.length === right.length && timingSafeEqual(left, right);
}

function validatePipeName(pipeName) {
  if (typeof pipeName !== "string" || !pipeName.startsWith(PIPE_PREFIX)) throw codedError("invalid_pipe_name");
  const suffix = pipeName.slice(PIPE_PREFIX.length);
  if (!/^\d+-[a-f0-9]{16,64}$/i.test(suffix)) throw codedError("invalid_pipe_name");
  return pipeName;
}

function encodeFrame(message, maxFrameBytes = MAX_FRAME_BYTES) {
  const line = Buffer.from(`${JSON.stringify(message)}\n`, "utf8");
  if (line.byteLength > maxFrameBytes) throw codedError("frame_too_large");
  return line;
}

function createBoundedWriter(socket, { maxQueuedBytes = MAX_QUEUED_BYTES } = {}) {
  let blocked = false;
  let closed = false;
  let queuedBytes = 0;
  const queue = [];

  function flush() {
    if (closed || !socket || socket.destroyed) return;
    blocked = false;
    while (queue.length) {
      const frame = queue.shift();
      queuedBytes -= frame.byteLength;
      try {
        if (!socket.write(frame)) {
          blocked = true;
          return;
        }
      } catch {
        closed = true;
        queue.length = 0;
        queuedBytes = 0;
        return;
      }
    }
  }

  socket.on("drain", flush);
  socket.on("close", () => {
    closed = true;
    queue.length = 0;
    queuedBytes = 0;
  });

  return Object.freeze({
    write(frame) {
      if (closed || !socket || socket.destroyed) throw codedError("pipe_not_connected");
      const bytes = Buffer.isBuffer(frame) ? frame : Buffer.from(frame);
      if (blocked || queue.length) {
        if (queuedBytes + bytes.byteLength > maxQueuedBytes) throw codedError("backpressure_limit");
        queue.push(bytes);
        queuedBytes += bytes.byteLength;
        return true;
      }
      if (!socket.write(bytes)) blocked = true;
      return true;
    },
    close() {
      closed = true;
      queue.length = 0;
      queuedBytes = 0;
    },
    status() { return { queuedBytes, queuedFrames: queue.length, blocked, closed }; },
  });
}

function validateEnvelope(message, { expectedSequence, generation, token, direction = "any" } = {}) {
  if (!message || typeof message !== "object" || Array.isArray(message)) throw codedError("invalid_envelope");
  if (message.schema !== SCHEMA) throw codedError("unsupported_schema");
  if (!["request", "response", "event"].includes(message.type)) throw codedError("invalid_message_type");
  if (!Number.isSafeInteger(message.seq) || message.seq < 1) throw codedError("invalid_sequence");
  if (expectedSequence !== undefined && message.seq !== expectedSequence) throw codedError("out_of_order_sequence");
  if (!isGeneration(message.generation)) throw codedError("invalid_generation");
  if (generation !== undefined && message.generation !== generation) throw codedError("stale_generation");
  if (token !== undefined && !equalToken(token, message.authToken)) throw codedError("unauthorized");
  if (message.requestId !== null && (typeof message.requestId !== "string" || message.requestId.length < 1 || message.requestId.length > 128)) {
    throw codedError("invalid_request_id");
  }
  if (message.type === "event" && message.requestId !== null) throw codedError("event_request_id_must_be_null");
  if ((message.type === "request" || message.type === "response") && !message.requestId) throw codedError("request_id_required");
  if (direction === "agent-in" && !["request", "response", "event"].includes(message.type)) throw codedError("invalid_agent_message");
  if (direction === "broker-in" && !["response", "event", "request"].includes(message.type)) throw codedError("invalid_broker_message");
  if (message.type !== "event" && (typeof message.op !== "string" || message.op.length > 64)) throw codedError("operation_required");
  return message;
}

class FrameDecoder {
  constructor({ maxFrameBytes = MAX_FRAME_BYTES, onMessage, onError } = {}) {
    this.maxFrameBytes = maxFrameBytes;
    this.onMessage = onMessage;
    this.onError = onError;
    this.pending = Buffer.alloc(0);
    this.closed = false;
    this.decoder = new TextDecoder("utf-8", { fatal: true });
  }

  push(chunk) {
    if (this.closed) return;
    const input = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    this.pending = this.pending.length ? Buffer.concat([this.pending, input]) : input;
    if (this.pending.length > this.maxFrameBytes && this.pending.indexOf(0x0a) === -1) return this.fail(codedError("frame_too_large"));
    while (!this.closed) {
      const newline = this.pending.indexOf(0x0a);
      if (newline === -1) {
        if (this.pending.length > this.maxFrameBytes) this.fail(codedError("frame_too_large"));
        break;
      }
      if (newline + 1 > this.maxFrameBytes) return this.fail(codedError("frame_too_large"));
      const line = this.pending.subarray(0, newline);
      this.pending = this.pending.subarray(newline + 1);
      if (line.length === 0) return this.fail(codedError("empty_frame"));
      let message;
      try { message = JSON.parse(this.decoder.decode(line)); }
      catch { return this.fail(codedError("malformed_json")); }
      try { this.onMessage?.(message); }
      catch (error) { return this.fail(error?.code ? error : codedError("message_handler_failed")); }
    }
  }

  finish() {
    if (this.closed) return;
    if (this.pending.length) this.fail(codedError("unterminated_frame"));
    else this.closed = true;
  }

  fail(error) {
    if (this.closed) return;
    this.closed = true;
    this.pending = Buffer.alloc(0);
    this.onError?.(error);
  }
}

function makeEnvelope(type, seq, generation, requestId, op, payload, authToken) {
  const envelope = { schema: SCHEMA, type, seq, generation, requestId, authToken };
  if (op !== undefined) envelope.op = op;
  if (payload !== undefined) envelope.payload = payload;
  return envelope;
}

function createPipeClient({ pipeName, connectionGeneration, ipcToken, timeoutMs = 5000, onEvent = () => {}, onAgentStatus = () => {}, onProbeRendererCapabilities = null, onProbeAsarSources = null, onProbeAsarCandidates = null, onProbeOfficialWindows = null, onProbeRendererScriptMetadata = null, onStartOfficialAVObserver = null, onDrainOfficialAVObserver = null, onStopOfficialAVObserver = null, onPrivateInviteLifecycle = null } = {}) {
  validatePipeName(pipeName);
  if (!isGeneration(connectionGeneration)) throw codedError("invalid_generation");
  if (!validToken(ipcToken)) throw codedError("invalid_auth_token");

  let socket = null;
  let writer = null;
  let inSequence = 0;
  let outSequence = 0;
  let connected = false;
  let handshaken = false;
  let closed = false;
  let agent = null;
  let disconnectHandler = null;
  let disconnectNotified = false;
  let disconnectCode = null;
  let asarCandidateProbeUsed = false;
  let officialWindowProbeUsed = false;
  let rendererScriptMetadataProbeUsed = false;
  let officialAVObserverStarted = false;
  let officialAVObserverStopped = false;
  let bootstrapStatus = {
    bootstrapLoaded: true,
    wrapperFound: null,
    wrapperHashPinned: false,
    listenerRemovalMode: "unverified",
    sessionFound: null,
    serviceAvailable: null,
    listenerRegistered: false,
    lastFailureCode: null,
  };
  const pending = new Map();

  function write(type, requestId, op, payload) {
    if (!socket || socket.destroyed) throw codedError("pipe_not_connected");
    const message = makeEnvelope(type, ++outSequence, connectionGeneration, requestId, op, payload, ipcToken);
    writer?.write(encodeFrame(message));
    return message;
  }

  function rejectPending(code) {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(codedError(code));
    }
    pending.clear();
  }

  function updateStage(payload) {
    if (!payload || typeof payload !== "object") return;
    for (const key of ["bootstrapLoaded", "wrapperFound", "wrapperSource", "wrapperHashPinned", "listenerRemovalMode", "sessionFound", "serviceAvailable", "listenerRegistered", "connectionGeneration", "processPid", "nodeVersion", "electronVersion", "lastFailureCode"]) {
      if (Object.prototype.hasOwnProperty.call(payload, key)) bootstrapStatus[key] = payload[key];
    }
  }

  function sendResponse(request, payload, code = null) {
    write("response", request.requestId, request.op, { ok: code === null, code, ...payload });
  }

  async function handleRequest(message) {
    if (message.op === "probeRendererCapabilities") {
      const payload = message.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.getPrototypeOf(payload) !== Object.prototype || Object.keys(payload).length !== 0) {
        return sendResponse(message, {}, "invalid_renderer_probe_payload");
      }
      if (typeof onProbeRendererCapabilities !== "function") {
        return sendResponse(message, {}, "renderer_probe_unavailable");
      }
      let report;
      try { report = await onProbeRendererCapabilities(); }
      catch { return sendResponse(message, {}, "renderer_probe_failed"); }
      if (!isRendererCapabilityReport(report)) {
        return sendResponse(message, {}, "renderer_probe_invalid_result");
      }
      return sendResponse(message, { report });
    }
    if (message.op === "probeAsarSources") {
      const payload = message.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.getPrototypeOf(payload) !== Object.prototype || Object.keys(payload).length !== 0) {
        return sendResponse(message, {}, "invalid_asar_source_probe_payload");
      }
      if (typeof onProbeAsarSources !== "function") {
        return sendResponse(message, {}, "asar_source_probe_unavailable");
      }
      let report;
      try { report = await onProbeAsarSources(); }
      catch { return sendResponse(message, {}, "asar_source_probe_failed"); }
      if (!isAsarSourceProbeReport(report)) {
        return sendResponse(message, {}, "asar_source_probe_invalid_result");
      }
      return sendResponse(message, { report });
    }
    if (message.op === "probeAsarCandidates") {
      const payload = message.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.getPrototypeOf(payload) !== Object.prototype || Object.keys(payload).length !== 0) {
        return sendResponse(message, {}, "invalid_asar_candidate_probe_payload");
      }
      if (typeof onProbeAsarCandidates !== "function") {
        return sendResponse(message, {}, "asar_candidate_probe_unavailable");
      }
      if (!connected || !handshaken || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== connectionGeneration) {
        return sendResponse(message, {}, "asar_candidate_probe_not_ready");
      }
      if (asarCandidateProbeUsed) return sendResponse(message, {}, "asar_candidate_probe_already_dispatched");
      asarCandidateProbeUsed = true;
      let report;
      try { report = await onProbeAsarCandidates(); }
      catch { return sendResponse(message, {}, "asar_candidate_probe_failed"); }
      if (!isAsarCandidateReport(report)) return sendResponse(message, {}, "asar_candidate_probe_invalid_result");
      return sendResponse(message, { report });
    }
    if (message.op === "probeOfficialWindows") {
      const payload = message.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.getPrototypeOf(payload) !== Object.prototype || Object.keys(payload).length !== 0) {
        return sendResponse(message, {}, "invalid_official_window_probe_payload");
      }
      if (typeof onProbeOfficialWindows !== "function") {
        return sendResponse(message, {}, "official_window_probe_unavailable");
      }
      if (!connected || !handshaken || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== connectionGeneration) {
        return sendResponse(message, {}, "official_window_probe_not_ready");
      }
      if (officialWindowProbeUsed) return sendResponse(message, {}, "official_window_probe_already_dispatched");
      officialWindowProbeUsed = true;
      let report;
      try { report = await onProbeOfficialWindows(); }
      catch { return sendResponse(message, {}, "official_window_probe_failed"); }
      if (!isOfficialWindowReport(report)) return sendResponse(message, {}, "official_window_probe_invalid_result");
      return sendResponse(message, { report });
    }
    if (message.op === "probeRendererScriptMetadata") {
      const payload = message.payload;
      if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
          Object.getPrototypeOf(payload) !== Object.prototype || Object.keys(payload).length !== 0) {
        return sendResponse(message, {}, "invalid_renderer_script_metadata_probe_payload");
      }
      if (typeof onProbeRendererScriptMetadata !== "function") {
        return sendResponse(message, {}, "renderer_script_metadata_probe_unavailable");
      }
      if (!connected || !handshaken || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== connectionGeneration) {
        return sendResponse(message, {}, "renderer_script_metadata_probe_not_ready");
      }
      if (rendererScriptMetadataProbeUsed) return sendResponse(message, {}, "renderer_script_metadata_probe_already_dispatched");
      rendererScriptMetadataProbeUsed = true;
      let report;
      try { report = await onProbeRendererScriptMetadata(); }
      catch { return sendResponse(message, {}, "renderer_script_metadata_probe_failed"); }
      if (!isRendererScriptMetadataReport(report)) {
        return sendResponse(message, {}, "renderer_script_metadata_probe_invalid_result");
      }
      return sendResponse(message, { report });
    }
    if (["startOfficialAVObserver", "drainOfficialAVObserver", "stopOfficialAVObserver"].includes(message.op)) {
      const op = message.op;
      const startOperation = op === "startOfficialAVObserver";
      const stopOperation = op === "stopOfficialAVObserver";
      const operationCodes = {
        startOfficialAVObserver: ["invalid_official_av_observer_start_payload", "official_av_observer_start_unavailable", "official_av_observer_start_failed"],
        drainOfficialAVObserver: ["invalid_official_av_observer_drain_payload", "official_av_observer_drain_unavailable", "official_av_observer_drain_failed"],
        stopOfficialAVObserver: ["invalid_official_av_observer_stop_payload", "official_av_observer_stop_unavailable", "official_av_observer_stop_failed"],
      }[op];
      const notReadyCode = "official_av_observer_not_ready";
      const payload = message.payload;
      let startOptions = {};
      let validPayload = false;
      try {
        if (payload && typeof payload === "object" && !Array.isArray(payload) && Object.getPrototypeOf(payload) === Object.prototype) {
          const keys = Reflect.ownKeys(payload);
          if (keys.length === 0) validPayload = true;
          else if (startOperation && keys.length === 1 && keys[0] === "incomingOnly") {
            const descriptor = Object.getOwnPropertyDescriptor(payload, "incomingOnly");
            if (descriptor && Object.hasOwn(descriptor, "value") && descriptor.value === true) {
              startOptions = { incomingOnly: true };
              validPayload = true;
            }
          } else if (startOperation && keys.length === 1 && keys[0] === "nativeConditional") {
            const descriptor = Object.getOwnPropertyDescriptor(payload, "nativeConditional");
            if (descriptor && Object.hasOwn(descriptor, "value") && descriptor.value === true) {
              startOptions = { nativeConditional: true };
              validPayload = true;
            }
          }
        }
      } catch { validPayload = false; }
      if (!validPayload) {
        return sendResponse(message, {}, operationCodes[0]);
      }
      const callback = startOperation ? onStartOfficialAVObserver : stopOperation ? onStopOfficialAVObserver : onDrainOfficialAVObserver;
      if (typeof callback !== "function") return sendResponse(message, {}, operationCodes[1]);
      if (!connected || !handshaken || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== connectionGeneration) {
        return sendResponse(message, {}, notReadyCode);
      }
      if (startOperation) {
        if (officialAVObserverStarted) return sendResponse(message, {}, "official_av_observer_already_started");
        officialAVObserverStarted = true;
      } else if (!officialAVObserverStarted) {
        return sendResponse(message, {}, "official_av_observer_not_started");
      } else if (officialAVObserverStopped && !stopOperation) {
        return sendResponse(message, {}, "official_av_observer_stopped");
      }
      if (stopOperation && officialAVObserverStopped) {
        // The observer's stop contract is idempotent; call it again so repeated
        // callers receive the same final, strictly validated report.
      }
      let report;
      try { report = startOperation ? await callback(startOptions) : await callback(); }
      catch { return sendResponse(message, {}, operationCodes[2]); }
      if (!isObserverReport(report)) return sendResponse(message, {}, "official_av_observer_invalid_result");
      if (stopOperation) officialAVObserverStopped = true;
      return sendResponse(message, { report });
    }
    if (message.op === "privateInviteLifecycle") {
      if (!validPrivateInviteLifecycleRequest(message.payload)) return sendResponse(message, {}, "invalid_private_invite_lifecycle_request");
      if (!connected || !handshaken || bootstrapStatus.bootstrapLoaded !== true ||
          bootstrapStatus.processPid !== process.pid || bootstrapStatus.connectionGeneration !== connectionGeneration ||
          typeof onPrivateInviteLifecycle !== "function") return sendResponse(message, {}, "official_private_invite_unavailable");
      let result;
      try { result = await onPrivateInviteLifecycle(message.payload); }
      catch { return sendResponse(message, {}, "official_private_invite_operation_failed"); }
      if (!isLifecycleResult(result)) return sendResponse(message, {}, "official_private_invite_invalid_result");
      return sendResponse(message, { result: { confirmed: result.confirmed, code: result.code } });
    }
    if (message.op === "status") {
      return sendResponse(message, { bootstrap: bootstrapStatus, agent: agent ? agent.status() : null });
    }
    if (message.op === "attach") {
      const result = agent ? await agent.attach() : { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable" };
      updateStage({ listenerRegistered: result.status?.listenerRegistered ?? false, lastFailureCode: result.code || null });
      try { onAgentStatus("attach", result); } catch { /* reporting cannot affect listener ownership */ }
      sendResponse(message, { bootstrap: bootstrapStatus, result }, result.ok ? null : result.code);
      try { write("event", null, "bootstrap_status", { ...bootstrapStatus }); } catch { /* pipe may close during detach */ }
      return;
    }
    if (message.op === "detach") {
      const result = agent ? await agent.detach() : { ok: true, status: null };
      updateStage({ listenerRegistered: result.status?.listenerRegistered ?? false, lastFailureCode: result.code || null });
      try { onAgentStatus("detach", result); } catch { /* reporting cannot affect listener ownership */ }
      sendResponse(message, { bootstrap: bootstrapStatus, result }, result.ok ? null : result.code);
      try { write("event", null, "bootstrap_status", { ...bootstrapStatus }); } catch { /* pipe may close during detach */ }
      return;
    }
    if (message.op === "getAccountPath") {
      const payload = message.payload;
      const validPayload = payload && typeof payload === "object" && !Array.isArray(payload) &&
        Object.keys(payload).length === 1 && Object.keys(payload)[0] === "uin" &&
        typeof payload.uin === "string" && /^[1-9][0-9]{0,19}$/.test(payload.uin) &&
        Number.isSafeInteger(Number(payload.uin));
      if (!validPayload) return sendResponse(message, {}, "invalid_account_uin");
      const result = agent
        ? await agent.getAccountPath({ uin: payload.uin, generation: message.generation })
        : { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable" };
      // Account path is returned only in this authenticated private response;
      // it is never published as an event or copied into diagnostic status.
      return sendResponse(message, { result }, result.ok ? null : result.code);
    }
    if (message.op === "forwardAVSDKOutput") {
      const payload = message.payload || {};
      const result = agent
        ? await agent.forwardAVSDKOutput({ command: payload.command, value: payload.value, generation: message.generation })
        : { ok: false, code: bootstrapStatus.lastFailureCode || "agent_unavailable" };
      try { onAgentStatus("forwardAVSDKOutput", result); } catch { /* reporting cannot affect listener ownership */ }
      return sendResponse(message, { result }, result.ok ? null : result.code);
    }
    return sendResponse(message, {}, "unknown_operation");
  }

  function onFrame(message) {
    let valid;
    try {
      valid = validateEnvelope(message, { expectedSequence: inSequence + 1, generation: connectionGeneration, token: ipcToken, direction: "broker-in" });
    } catch (error) {
      bootstrapStatus.lastFailureCode = error.code || "invalid_broker_frame";
      rejectPending(bootstrapStatus.lastFailureCode);
      throw error;
    }
    inSequence = valid.seq;
    if (valid.type === "response") {
      const entry = pending.get(valid.requestId);
      if (!entry) throw codedError("unexpected_response");
      if (entry.op !== valid.op) throw codedError("response_operation_mismatch");
      pending.delete(valid.requestId);
      clearTimeout(entry.timer);
      if (valid.payload?.ok === false) entry.reject(codedError(valid.payload.code || "request_failed"));
      else entry.resolve(valid.payload);
      return;
    }
    if (valid.type === "event") {
      if (valid.op === "bootstrap_status") updateStage(valid.payload);
      try { onEvent(valid.op, valid.payload); } catch { /* observer errors cannot break the private pipe */ }
      return;
    }
    void handleRequest(valid).catch(() => {
      try { sendResponse(valid, {}, "request_handler_failed"); } catch { socket?.destroy(); }
    });
  }

  function request(op, payload) {
    if (!connected || (!handshaken && op !== "handshake")) return Promise.reject(codedError("agent_not_ready"));
    const requestId = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(codedError("request_timeout"));
      }, timeoutMs);
      timer.unref?.();
      pending.set(requestId, { resolve, reject, timer, op });
      try { write("request", requestId, op, payload); }
      catch (error) {
        pending.delete(requestId);
        clearTimeout(timer);
        reject(error);
      }
    });
  }

  return Object.freeze({
    async connect() {
      if (connected && handshaken) return { ok: true };
      if (closed) throw codedError("pipe_client_closed");
      await new Promise((resolve, reject) => {
        socket = net.createConnection(pipeName);
        const onError = () => {
          socket?.removeListener("connect", onConnect);
          reject(codedError("pipe_connect_failed"));
        };
        const onConnect = () => {
          socket?.removeListener("error", onError);
          resolve();
        };
        socket.once("error", onError);
        socket.once("connect", onConnect);
      });
      connected = true;
      const decoder = new FrameDecoder({ onMessage: onFrame, onError: (error) => {
        bootstrapStatus.lastFailureCode = error.code || "pipe_frame_error";
        rejectPending(bootstrapStatus.lastFailureCode);
        socket?.destroy();
      } });
      writer = createBoundedWriter(socket);
      socket.on("data", (chunk) => decoder.push(chunk));
      socket.on("end", () => decoder.finish());
      socket.on("error", () => {
        connected = false;
        rejectPending("pipe_disconnected");
        notifyDisconnect("pipe_disconnected");
      });
      socket.on("close", () => {
        connected = false;
        closed = true;
        rejectPending("pipe_disconnected");
        notifyDisconnect("pipe_disconnected");
      });
      await request("handshake", { agentProtocol: 1 });
      handshaken = true;
      return { ok: true };
    },
    setAgent(value) { agent = value; },
    setBootstrapStatus(value) { updateStage(value); },
    onDisconnect(handler) {
      disconnectHandler = typeof handler === "function" ? handler : null;
      if (disconnectNotified && disconnectHandler) {
        const installedHandler = disconnectHandler;
        queueMicrotask(() => {
          if (disconnectHandler === installedHandler) invokeDisconnectHandler(installedHandler, disconnectCode);
        });
      }
    },
    publishEvent(op, payload) { return write("event", null, op, payload); },
    request,
    status() {
      return { connected, handshaken, connectionGeneration, bootstrap: { ...bootstrapStatus } };
    },
    async close() {
      if (closed) return;
      connected = false;
      rejectPending("pipe_client_closed");
      writer?.close();
      if (socket && !socket.destroyed) await new Promise((resolve) => socket.end(resolve));
      closed = true;
    },
  });

  function notifyDisconnect(code) {
    if (disconnectNotified) return;
    disconnectNotified = true;
    disconnectCode = code || "pipe_disconnected";
    if (disconnectHandler) invokeDisconnectHandler(disconnectHandler, disconnectCode);
  }

  function invokeDisconnectHandler(handler, code) {
    try {
      const result = handler(code);
      if (result && typeof result.then === "function") result.catch(() => {});
    } catch { /* cleanup remains best effort */ }
  }
}

module.exports = {
  SCHEMA,
  MAX_FRAME_BYTES,
  PIPE_PREFIX,
  FrameDecoder,
  codedError,
  createBoundedWriter,
  createPipeClient,
  encodeFrame,
  equalToken,
  isGeneration,
  makeEnvelope,
  validToken,
  validateEnvelope,
  validatePipeName,
};
