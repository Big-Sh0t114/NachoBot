"use strict";

const { randomBytes, randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const readline = require("node:readline");
const { isRendererCapabilityReport } = require("../agent/renderer-capability-probe.cjs");
const { isAsarSourceProbeReport } = require("../agent/asar-source-probe.cjs");
const { isAsarCandidateReport } = require("../agent/asar-candidate-probe.cjs");
const { isOfficialWindowReport } = require("../agent/official-window-probe.cjs");
const { isRendererScriptMetadataReport } = require("../agent/renderer-script-metadata-probe.cjs");
const { isObserverReport } = require("../agent/official-av-observer.cjs");
const { validRequest: validPrivateInviteLifecycleRequest, isLifecycleResult } = require("../agent/official-private-invite.cjs");
const {
  PRIVATE_INVITE_PROFILE,
  PINNED_WRAPPER_SHA256,
  canonicalFiletime,
  validateNativeInviteEvidence,
} = require("../shared/private-invite-scope.cjs");
const {
  MAX_FRAME_BYTES,
  PIPE_PREFIX,
  FrameDecoder,
  codedError,
  createBoundedWriter,
  encodeFrame,
  isGeneration,
  makeEnvelope,
  validToken,
  validateEnvelope,
  validatePipeName,
} = require("./ipc.cjs");

const DEFAULT_LIFETIME_MS = 10 * 60 * 1000;
const DETACH_AND_STOP_OWNER = Symbol("detach-and-stop-owner");

function positivePid(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function summarizeEvent(op, payload) {
  if (op !== "kernel_event" || !payload || typeof payload !== "object") return { kind: op };
  const args = Array.isArray(payload.args) ? payload.args : [];
  return {
    kind: "kernel_event",
    method: typeof payload.method === "string" ? payload.method : "unknown",
    argumentCount: args.length,
    actionMapped: Boolean(payload.action),
  };
}

function summarizeBootstrapStatus(payload) {
  if (!payload || typeof payload !== "object") return { kind: "bootstrap_status" };
  const fields = [
    "bootstrapLoaded", "wrapperFound", "wrapperSource", "sessionFound", "serviceAvailable",
    "listenerRegistered", "connectionGeneration", "processPid", "nodeVersion", "electronVersion", "lastFailureCode",
  ];
  const summary = { kind: "bootstrap_status" };
  for (const key of fields) if (Object.prototype.hasOwnProperty.call(payload, key)) summary[key] = payload[key];
  return summary;
}

function createBroker(options = {}) {
  const {
    targetPid,
    modulePath,
    appPath,
    wrapperModulePath,
    reportPath,
    kernelCreationTime100ns,
    sessionName = "nt_1",
    connectionGeneration = 1,
    maxLifetimeMs = DEFAULT_LIFETIME_MS,
    serviceMode = false,
    nonce = randomBytes(16).toString("hex"),
    ipcToken = randomBytes(32).toString("base64url"),
    onEvent = () => {},
    onStatus = () => {},
  } = options;
  if (typeof serviceMode !== "boolean") throw codedError("invalid_service_mode");
  if (serviceMode && Object.hasOwn(options, "maxLifetimeMs")) throw codedError("service_mode_lifetime_conflict");
  if (!positivePid(targetPid)) throw codedError("invalid_target_pid");
  if (!isGeneration(connectionGeneration)) throw codedError("invalid_generation");
  if (!Number.isSafeInteger(maxLifetimeMs) || maxLifetimeMs < 1000 || maxLifetimeMs > 60 * 60 * 1000) throw codedError("invalid_lifetime");
  if (!/^[a-f0-9]{16,64}$/i.test(nonce)) throw codedError("invalid_nonce");
  if (!validToken(ipcToken)) throw codedError("invalid_auth_token");
  if (typeof modulePath !== "string" || !path.isAbsolute(modulePath)) throw codedError("invalid_bootstrap_module_path");
  if (typeof appPath !== "string" || !path.isAbsolute(appPath)) throw codedError("invalid_app_path");
  if (wrapperModulePath !== undefined && (typeof wrapperModulePath !== "string" || !path.isAbsolute(wrapperModulePath))) {
    throw codedError("invalid_wrapper_module_path");
  }
  if (typeof reportPath !== "string" || !path.isAbsolute(reportPath)) throw codedError("invalid_report_path");
  if (kernelCreationTime100ns !== undefined && !canonicalFiletime(kernelCreationTime100ns)) {
    throw codedError("invalid_kernel_creation_time");
  }
  // This supervisor-derived identity is Broker-owned proof state. It must not
  // enter the private injector/bootstrap configuration schema.
  const expectedKernelCreationTime100ns = kernelCreationTime100ns;
  if (typeof sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(sessionName)) throw codedError("invalid_session_name");

  const pipeName = validatePipeName(`${PIPE_PREFIX}${targetPid}-${nonce}`);
  const config = Object.freeze({
    modulePath,
    pipeName,
    appPath,
    sessionName,
    connectionGeneration,
    ipcToken,
    ...(wrapperModulePath ? { wrapperModulePath } : {}),
    ...(reportPath ? { reportPath } : {}),
    attach: false,
  });

  let server = null;
  let activeSocket = null;
  let activeWriter = null;
  let decoder = null;
  let started = false;
  let stopping = false;
  let closing = false;
  let detachAndStopInFlight = false;
  let stopped = false;
  let authenticated = false;
  let everAuthenticated = false;
  let inSequence = 0;
  let outSequence = 0;
  let lifetimeTimer = null;
  let lastFailureCode = null;
  let lastEventSummary = null;
  let latestAgentStatus = null;
  let bootstrapStatusPublished = false;
  let attachDispatched = false;
  let rendererCapabilityProbeDispatched = false;
  let asarSourceProbeDispatched = false;
  let asarCandidateProbeDispatched = false;
  let officialWindowProbeDispatched = false;
  let rendererScriptMetadataProbeDispatched = false;
  let officialAVObserverDispatched = false;
  let officialAVObserverStarted = false;
  let officialAVObserverStopped = false;
  let officialAVObserverFinalReport = null;
  let mutationEpoch = 0;
  let confirmedListenerMutationEpoch = null;
  let lastNativeInviteCallbackSequence = 0;
  let detachConfirmedEpoch = null;
  let latestDetachRequestId = null;
  let eventCount = 0;
  let rejectedConnections = 0;
  const pending = new Map();
  const eventSubscribers = new Set();

  function agentNativeProfile(statusValue) {
    if (!statusValue || typeof statusValue !== "object") return null;
    const bootstrap = statusValue.bootstrap && typeof statusValue.bootstrap === "object"
      ? statusValue.bootstrap
      : statusValue;
    const agent = statusValue.agent && typeof statusValue.agent === "object" ? statusValue.agent : null;
    const listenerRegistered = agent
      ? agent.listenerRegistered === true
      : bootstrap.listenerRegistered === true;
    const listenerRegistrationSequence = agent
      ? agent.listenerRegistrationSequence
      : bootstrap.listenerRegistrationSequence;
    if (!listenerRegistered || bootstrap.wrapperHashPinned !== true ||
        bootstrap.processPid !== targetPid ||
        bootstrap.connectionGeneration !== connectionGeneration ||
        !Number.isSafeInteger(listenerRegistrationSequence) || listenerRegistrationSequence <= 0) return null;
    return PRIVATE_INVITE_PROFILE;
  }

  function currentAgentListenerRegistrationSequence(statusValue) {
    const agent = statusValue?.agent && typeof statusValue.agent === "object" ? statusValue.agent : null;
    const sequence = agent?.listenerRegistrationSequence;
    return Number.isSafeInteger(sequence) && sequence > 0 ? sequence : null;
  }

  function currentNativeInviteBinding() {
    if (!authenticated || !activeSocket || activeSocket.destroyed ||
        confirmedListenerMutationEpoch === null || confirmedListenerMutationEpoch !== mutationEpoch ||
        !canonicalFiletime(expectedKernelCreationTime100ns) ||
        agentNativeProfile(latestAgentStatus) !== PRIVATE_INVITE_PROFILE) return null;
    return Object.freeze({
      profile: PRIVATE_INVITE_PROFILE,
      wrapperSha256: PINNED_WRAPPER_SHA256,
      targetPid,
      kernelCreationTime100ns: expectedKernelCreationTime100ns,
      connectionGeneration,
      listenerMutationEpoch: confirmedListenerMutationEpoch,
      listenerRegistrationSequence: currentAgentListenerRegistrationSequence(latestAgentStatus),
    });
  }

  function payloadWithTrustedInviteProvenance(op, payload, ipcSequence) {
    if (op !== "kernel_event" || !payload || typeof payload !== "object") return payload;
    const forwarded = { ...payload };
    const suppliedNativeEvidence = Object.hasOwn(payload, "nativeInviteEvidence");
    // Provenance is always broker-owned; incoming values can never supply it.
    delete forwarded.nativeInviteProvenance;
    delete forwarded.nativeInviteEvidence;
    const binding = currentNativeInviteBinding();
    // Without a supervisor FILETIME/current binding, preserve the legacy event
    // path but strip its unproved marker. Once a current binding exists, an
    // explicitly evidence-bearing callback must match that registration; an
    // older captured callback cannot downgrade into generic invite/command 55.
    if (!binding) return forwarded;
    if (!suppliedNativeEvidence) return forwarded;
    if (payload.connectionGeneration !== connectionGeneration) return null;
    const evidence = validateNativeInviteEvidence(payload.nativeInviteEvidence, payload, binding);
    if (!evidence || evidence.callbackSequence <= lastNativeInviteCallbackSequence) return null;
    lastNativeInviteCallbackSequence = evidence.callbackSequence;
    forwarded.nativeInviteEvidence = evidence;
    forwarded.nativeInviteProvenance = Object.freeze({
      targetPid: binding.targetPid,
      kernelCreationTime100ns: binding.kernelCreationTime100ns,
      connectionGeneration: binding.connectionGeneration,
      listenerMutationEpoch: binding.listenerMutationEpoch,
      ipcSequence,
    });
    return forwarded;
  }

  function publishToSubscribers(op, payload) {
    for (const observer of [...eventSubscribers]) {
      try {
        const result = observer(op, payload);
        if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      } catch { /* subscribers cannot break the named-pipe transport */ }
    }
  }

  function notifyStatus() {
    const current = status();
    try {
      const result = onStatus(current);
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
    } catch { /* status observers are not part of the probe */ }
    publishToSubscribers("broker_status", current);
  }

  function status() {
    return {
      endpointReady: started && !stopped,
      connected: Boolean(activeSocket && !activeSocket.destroyed),
      authenticated,
      targetPid,
      connectionGeneration,
      nativeInviteBinding: currentNativeInviteBinding(),
      eventCount,
      rejectedConnections,
      lastEvent: lastEventSummary,
      agent: latestAgentStatus,
      lastFailureCode,
      stopped,
    };
  }

  function readReport() {
    if (!config.reportPath) return null;
    try {
      const report = JSON.parse(fs.readFileSync(`${config.reportPath}.agent.json`, "utf8"));
      if (report?.connectionGeneration !== connectionGeneration || report?.processPid !== targetPid) return null;
      return report;
    } catch {
      return null;
    }
  }

  function detachedIsConfirmed() {
    if (!everAuthenticated) return true;
    // Once attach has been dispatched, only the matching successful detach
    // response for the current mutation epoch can prove cleanup. Events and
    // disk reports may predate a listener mutation or its lost acknowledgement.
    if (attachDispatched) return detachConfirmedEpoch === mutationEpoch && detachConfirmedEpoch !== null;
    const agentStatus = latestAgentStatus?.agent;
    if (agentStatus?.listenerRegistered === false) return true;
    if (latestAgentStatus?.listenerRegistered === false) return true;
    const report = readReport();
    return report?.listenerRegistered === false && report?.lastFailureCode == null;
  }

  function write(type, requestId, op, payload) {
    if (!activeSocket || activeSocket.destroyed || !authenticated) throw codedError("agent_not_connected");
    const message = makeEnvelope(type, ++outSequence, connectionGeneration, requestId, op, payload, ipcToken);
    const frame = encodeFrame(message, MAX_FRAME_BYTES);
    activeWriter?.write(frame);
    return message;
  }

  function respond(request, payload, code = null) {
    write("response", request.requestId, request.op, { ok: code === null, code, ...payload });
  }

  function rejectPending(code) {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(codedError(code));
    }
    pending.clear();
  }

  function closeActive(code, socket = activeSocket) {
    if (socket && activeSocket !== socket) return;
    if (code) lastFailureCode = code;
    authenticated = false;
    confirmedListenerMutationEpoch = null;
    lastNativeInviteCallbackSequence = 0;
    rejectPending(code || "agent_disconnected");
    activeWriter?.close();
    activeWriter = null;
    if (activeSocket && !activeSocket.destroyed) activeSocket.destroy();
    activeSocket = null;
    decoder = null;
    notifyStatus();
  }

  function onFrame(message) {
    let valid;
    try {
      valid = validateEnvelope(message, {
        expectedSequence: inSequence + 1,
        generation: connectionGeneration,
        token: ipcToken,
        direction: "agent-in",
      });
    } catch (error) {
      rejectedConnections += 1;
      throw error;
    }
    inSequence = valid.seq;

    if (!authenticated) {
      if (valid.type !== "request" || valid.op !== "handshake" || valid.payload?.agentProtocol !== 1) {
        rejectedConnections += 1;
        throw codedError("handshake_required");
      }
      authenticated = true;
      everAuthenticated = true;
      lastFailureCode = null;
      try {
        const reply = makeEnvelope("response", ++outSequence, connectionGeneration, valid.requestId, "handshake", {
          ok: true,
          code: null,
          schema: 1,
          brokerProtocol: 1,
        }, ipcToken);
        activeWriter?.write(encodeFrame(reply, MAX_FRAME_BYTES));
      } catch {
        throw codedError("handshake_response_failed");
      }
      notifyStatus();
      return;
    }

    if (valid.type === "event") {
      eventCount += 1;
      if (valid.op === "bootstrap_status") {
        if (valid.payload?.bootstrapLoaded === true && valid.payload?.processPid === targetPid &&
            valid.payload?.connectionGeneration === connectionGeneration) bootstrapStatusPublished = true;
        const previousAgent = latestAgentStatus?.agent;
        const mayRefreshBootstrap = confirmedListenerMutationEpoch !== null &&
          previousAgent?.listenerRegistered === true &&
          currentAgentListenerRegistrationSequence(latestAgentStatus) !== null &&
          valid.payload?.listenerRegistered === true &&
          valid.payload?.wrapperHashPinned === true &&
          valid.payload?.processPid === targetPid &&
          valid.payload?.connectionGeneration === connectionGeneration;
        latestAgentStatus = mayRefreshBootstrap
          ? { bootstrap: valid.payload, agent: previousAgent }
          : valid.payload;
        if (agentNativeProfile(latestAgentStatus) !== PRIVATE_INVITE_PROFILE) {
          confirmedListenerMutationEpoch = null;
        }
      }
      const eventPayload = payloadWithTrustedInviteProvenance(valid.op, valid.payload, valid.seq);
      if (eventPayload === null) {
        // Keep only a value-free receipt that this IPC event was discarded.
        lastEventSummary = { kind: valid.op, dropped: true };
        notifyStatus();
        return;
      }
      lastEventSummary = summarizeEvent(valid.op, eventPayload);
      try {
        const result = onEvent(valid.op, eventPayload);
        if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      } catch { /* consumers cannot break the transport */ }
      publishToSubscribers(valid.op, eventPayload);
      notifyStatus();
      return;
    }
    if (valid.type === "response") {
      const entry = pending.get(valid.requestId);
      if (!entry) {
        throw codedError("unexpected_response");
      }
      if (entry.op !== valid.op) throw codedError("response_operation_mismatch");
      pending.delete(valid.requestId);
      clearTimeout(entry.timer);
      if (valid.op === "status") latestAgentStatus = valid.payload;
      if ((valid.op === "attach" || valid.op === "detach") && valid.payload?.result?.status) {
        latestAgentStatus = { bootstrap: valid.payload.bootstrap, agent: valid.payload.result.status };
      }
      if (valid.op === "attach") {
        const attachConfirmed = entry.mutationEpoch === mutationEpoch &&
          valid.payload?.ok === true && valid.payload?.result?.ok === true &&
          valid.payload?.result?.status?.listenerRegistered === true &&
          agentNativeProfile({ bootstrap: valid.payload.bootstrap, agent: valid.payload.result.status }) === PRIVATE_INVITE_PROFILE;
        confirmedListenerMutationEpoch = attachConfirmed ? entry.mutationEpoch : null;
        lastNativeInviteCallbackSequence = 0;
      } else if (valid.op === "detach") {
        confirmedListenerMutationEpoch = null;
      }
      if (valid.op === "detach" && valid.requestId === latestDetachRequestId &&
          entry.mutationEpoch === mutationEpoch && valid.payload?.ok === true &&
          valid.payload?.result?.ok === true && valid.payload?.result?.status?.listenerRegistered === false) {
        detachConfirmedEpoch = mutationEpoch;
      }
      if (valid.payload?.ok === false) entry.reject(codedError(valid.payload.code || "request_failed"));
      else {
        entry.resolve(valid.payload);
      }
      notifyStatus();
      return;
    }
    rejectedConnections += 1;
    throw codedError("unexpected_agent_request");
  }

  function onConnection(socket) {
    if (stopped || stopping || activeSocket || everAuthenticated) {
      rejectedConnections += 1;
      if (everAuthenticated && !activeSocket) lastFailureCode = "connection_generation_already_used";
      socket.destroy();
      notifyStatus();
      return;
    }
    activeSocket = socket;
    authenticated = false;
    inSequence = 0;
    outSequence = 0;
    latestAgentStatus = null;
    bootstrapStatusPublished = false;
    attachDispatched = false;
    mutationEpoch = 0;
    confirmedListenerMutationEpoch = null;
    lastNativeInviteCallbackSequence = 0;
    detachConfirmedEpoch = null;
    latestDetachRequestId = null;
    lastFailureCode = null;
    const frameDecoder = new FrameDecoder({ onMessage: onFrame, onError: (error) => closeActive(error.code || "frame_error", socket) });
    decoder = frameDecoder;
    activeWriter = createBoundedWriter(socket);
    socket.on("data", (chunk) => frameDecoder.push(chunk));
    socket.on("end", () => frameDecoder.finish());
    socket.on("error", () => closeActive("agent_pipe_error", socket));
    socket.on("close", () => {
      if (activeSocket === socket) {
        authenticated = false;
        confirmedListenerMutationEpoch = null;
        lastNativeInviteCallbackSequence = 0;
        activeSocket = null;
        activeWriter?.close();
        activeWriter = null;
        decoder = null;
        rejectPending("agent_disconnected");
        notifyStatus();
      }
    });
    notifyStatus();
  }

  function request(op, payload, timeoutMs = 5000) {
    if (!authenticated || !activeSocket) return Promise.reject(codedError("agent_not_connected"));
    if (op === "attach" && (closing || stopping || stopped)) return Promise.reject(codedError("broker_closing"));
    if (op === "attach" || op === "detach") {
      if (op === "attach") attachDispatched = true;
      mutationEpoch += 1;
      confirmedListenerMutationEpoch = null;
      lastNativeInviteCallbackSequence = 0;
      detachConfirmedEpoch = null;
      latestDetachRequestId = null;
      latestAgentStatus = null;
      notifyStatus();
    }
    const requestId = randomUUID();
    if (op === "detach") latestDetachRequestId = requestId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(codedError("request_timeout"));
      }, timeoutMs);
      timer.unref?.();
      pending.set(requestId, { resolve, reject, timer, op, mutationEpoch });
      try { write("request", requestId, op, payload); }
      catch (error) {
        pending.delete(requestId);
        clearTimeout(timer);
        reject(error);
      }
    });
  }

  async function statusRequest(timeoutMs) {
    return request("status", {}, timeoutMs);
  }

  async function probeRendererCapabilities(timeoutMs = 5000) {
    if (rendererCapabilityProbeDispatched) throw codedError("renderer_probe_already_dispatched");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true ||
        bootstrap?.processPid !== targetPid || bootstrap?.connectionGeneration !== connectionGeneration) {
      throw codedError("renderer_probe_not_ready");
    }
    // Readiness is asynchronous: reserve the generation immediately after it
    // returns, then recheck so simultaneous callers cannot both dispatch.
    if (rendererCapabilityProbeDispatched) throw codedError("renderer_probe_already_dispatched");
    // Spend the one allowed dispatch before writing to the pipe so concurrent
    // callers cannot issue a second probe for this authenticated generation.
    rendererCapabilityProbeDispatched = true;
    const reply = await request("probeRendererCapabilities", {}, timeoutMs);
    if (reply?.ok !== true || !isRendererCapabilityReport(reply.report)) {
      throw codedError("renderer_probe_invalid_result");
    }
    return reply.report;
  }

  async function probeAsarSources(timeoutMs = 5000) {
    if (asarSourceProbeDispatched) throw codedError("asar_source_probe_already_dispatched");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true || bootstrap?.processPid !== targetPid ||
        bootstrap?.connectionGeneration !== connectionGeneration) throw codedError("asar_source_probe_not_ready");
    if (asarSourceProbeDispatched) throw codedError("asar_source_probe_already_dispatched");
    asarSourceProbeDispatched = true;
    const reply = await request("probeAsarSources", {}, timeoutMs);
    if (reply?.ok !== true || !isAsarSourceProbeReport(reply.report)) {
      throw codedError("asar_source_probe_invalid_result");
    }
    return reply.report;
  }

  async function probeAsarCandidates(timeoutMs = 5000) {
    if (asarCandidateProbeDispatched) throw codedError("asar_candidate_probe_already_dispatched");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true || bootstrap?.processPid !== targetPid ||
        bootstrap?.connectionGeneration !== connectionGeneration) throw codedError("asar_candidate_probe_not_ready");
    if (asarCandidateProbeDispatched) throw codedError("asar_candidate_probe_already_dispatched");
    // Reserve before writing: one empty candidate read per authenticated generation.
    asarCandidateProbeDispatched = true;
    const reply = await request("probeAsarCandidates", {}, timeoutMs);
    if (reply?.ok !== true || !isAsarCandidateReport(reply.report)) {
      throw codedError("asar_candidate_probe_invalid_result");
    }
    return reply.report;
  }

  async function probeOfficialWindows(timeoutMs = 5000) {
    if (officialWindowProbeDispatched) throw codedError("official_window_probe_already_dispatched");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true || bootstrap?.processPid !== targetPid ||
        bootstrap?.connectionGeneration !== connectionGeneration) throw codedError("official_window_probe_not_ready");
    if (officialWindowProbeDispatched) throw codedError("official_window_probe_already_dispatched");
    officialWindowProbeDispatched = true;
    const reply = await request("probeOfficialWindows", {}, timeoutMs);
    if (reply?.ok !== true || !isOfficialWindowReport(reply.report)) {
      throw codedError("official_window_probe_invalid_result");
    }
    return reply.report;
  }

  async function probeRendererScriptMetadata(timeoutMs = 5000) {
    if (rendererScriptMetadataProbeDispatched) throw codedError("renderer_script_metadata_probe_already_dispatched");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true || bootstrap?.processPid !== targetPid ||
        bootstrap?.connectionGeneration !== connectionGeneration) throw codedError("renderer_script_metadata_probe_not_ready");
    if (rendererScriptMetadataProbeDispatched) throw codedError("renderer_script_metadata_probe_already_dispatched");
    rendererScriptMetadataProbeDispatched = true;
    const reply = await request("probeRendererScriptMetadata", {}, timeoutMs);
    if (reply?.ok !== true || !isRendererScriptMetadataReport(reply.report)) {
      throw codedError("renderer_script_metadata_probe_invalid_result");
    }
    return reply.report;
  }

  async function startOfficialAVObserver(timeoutMs = 5000, options = {}) {
    let incomingOnly = false;
    let nativeConditional = false;
    try {
      if (!options || typeof options !== "object" || Array.isArray(options) || Object.getPrototypeOf(options) !== Object.prototype) {
        throw codedError("invalid_official_av_observer_options");
      }
      const keys = Reflect.ownKeys(options);
      if (keys.length === 1 && keys[0] === "incomingOnly") {
        const descriptor = Object.getOwnPropertyDescriptor(options, "incomingOnly");
        if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.value !== true) {
          throw codedError("invalid_official_av_observer_options");
        }
        incomingOnly = true;
      } else if (keys.length === 1 && keys[0] === "nativeConditional") {
        const descriptor = Object.getOwnPropertyDescriptor(options, "nativeConditional");
        if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.value !== true) {
          throw codedError("invalid_official_av_observer_options");
        }
        nativeConditional = true;
      } else if (keys.length !== 0) throw codedError("invalid_official_av_observer_options");
    } catch (error) {
      if (error?.code === "invalid_official_av_observer_options") throw error;
      throw codedError("invalid_official_av_observer_options");
    }
    if (officialAVObserverDispatched) throw codedError("official_av_observer_already_started");
    const readiness = await statusRequest(timeoutMs);
    const bootstrap = readiness?.bootstrap;
    if (!readiness || readiness.ok !== true || !authenticated || !activeSocket || activeSocket.destroyed || !bootstrapStatusPublished ||
        bootstrap?.bootstrapLoaded !== true || bootstrap?.processPid !== targetPid ||
        bootstrap?.connectionGeneration !== connectionGeneration) throw codedError("official_av_observer_not_ready");
    if (officialAVObserverDispatched) throw codedError("official_av_observer_already_started");
    officialAVObserverDispatched = true;
    const payload = incomingOnly ? { incomingOnly: true } : nativeConditional ? { nativeConditional: true } : {};
    const reply = await request("startOfficialAVObserver", payload, timeoutMs);
    if (reply?.ok !== true || !isObserverReport(reply.report) || !["active", "rejected"].includes(reply.report.status)) {
      throw codedError("official_av_observer_invalid_result");
    }
    officialAVObserverStarted = true;
    return reply.report;
  }

  async function drainOfficialAVObserver(timeoutMs = 5000) {
    if (!officialAVObserverStarted || officialAVObserverStopped) throw codedError("official_av_observer_not_active");
    const reply = await request("drainOfficialAVObserver", {}, timeoutMs);
    if (reply?.ok !== true || !isObserverReport(reply.report)) throw codedError("official_av_observer_invalid_result");
    return reply.report;
  }

  async function stopOfficialAVObserver(timeoutMs = 5000) {
    if (!officialAVObserverStarted) throw codedError("official_av_observer_not_started");
    if (officialAVObserverStopped) return officialAVObserverFinalReport;
    const reply = await request("stopOfficialAVObserver", {}, timeoutMs);
    if (reply?.ok !== true || !isObserverReport(reply.report) || !["stopped", "expired"].includes(reply.report.status)) {
      throw codedError("official_av_observer_invalid_result");
    }
    officialAVObserverStopped = true;
    officialAVObserverFinalReport = reply.report;
    return reply.report;
  }

  async function attach(timeoutMs) {
    return request("attach", {}, timeoutMs);
  }

  async function privateInviteLifecycle(body, timeoutMs = 4000) {
    if (!validPrivateInviteLifecycleRequest(body) || Buffer.byteLength(JSON.stringify(body), "utf8") > 4096) {
      throw codedError("invalid_private_invite_lifecycle_request");
    }
    const reply = await request("privateInviteLifecycle", body, timeoutMs);
    if (reply?.ok !== true || !isLifecycleResult(reply.result)) throw codedError("official_private_invite_invalid_result");
    return { confirmed: reply.result.confirmed, code: reply.result.code };
  }

  async function detach(timeoutMs) {
    const reply = await request("detach", {}, timeoutMs);
    const listenerRegistered = reply?.result?.status?.listenerRegistered;
    if (reply?.result?.ok !== true || listenerRegistered !== false || !detachedIsConfirmed()) throw codedError("detach_unconfirmed");
    return { ...reply, detachedConfirmed: true };
  }

  async function getAccountPath(uin, timeoutMs) {
    if (typeof uin !== "string" || !/^[1-9][0-9]{0,19}$/.test(uin) || !Number.isSafeInteger(Number(uin))) {
      throw codedError("invalid_account_uin");
    }
    const reply = await request("getAccountPath", { uin }, timeoutMs);
    const result = reply?.result;
    if (result?.ok !== true || typeof result.accountPath !== "string" ||
        result.accountPath.length < 1 || result.accountPath.length > 32768 || result.accountPath.includes("\0")) {
      throw codedError(typeof result?.code === "string" ? result.code : "account_path_unavailable");
    }
    return result.accountPath;
  }

  async function detachAndStop(timeoutMs) {
    if (stopped) {
      if (!detachedIsConfirmed()) throw codedError("detach_unconfirmed");
      return { stopped: true, detachedConfirmed: true };
    }
    if (stopping || detachAndStopInFlight) return { stopped: false, stopping: true, detachedConfirmed: false };
    // Claim shutdown synchronously before the first await so attach cannot race.
    closing = true;
    detachAndStopInFlight = true;
    try {
      if (authenticated) await detach(timeoutMs);
      else if (everAuthenticated && !detachedIsConfirmed()) {
        throw codedError("detach_unconfirmed_agent_disconnected");
      }
      const stoppedResult = await stopInternal({ confirmDetach: false }, DETACH_AND_STOP_OWNER);
      if (!stoppedResult.stopped) return stoppedResult;
      return { stopped: true, detachedConfirmed: detachedIsConfirmed() };
    } finally {
      detachAndStopInFlight = false;
    }
  }

  async function writeConfigFile(filePath) {
    const target = path.resolve(filePath);
    await fs.promises.mkdir(path.dirname(target), { recursive: true });
    const temporary = `${target}.${process.pid}.${randomBytes(4).toString("hex")}.tmp`;
    await fs.promises.writeFile(temporary, `${JSON.stringify(config, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    await fs.promises.rename(temporary, target);
    return target;
  }

  async function start() {
    if (started && !stopped) return { pipeName, config: publicConfig() };
    if (stopped) throw codedError("broker_stopped");
    if (process.platform !== "win32") throw codedError("windows_named_pipe_required");
    await new Promise((resolve, reject) => {
      server = net.createServer(onConnection);
      server.once("error", () => reject(codedError("pipe_listen_failed")));
      server.listen(pipeName, resolve);
    });
    started = true;
    if (!serviceMode) {
      lifetimeTimer = setTimeout(() => {
        void stop({ confirmDetach: true }).catch((error) => {
          lastFailureCode = error.code || "detach_unconfirmed";
          notifyStatus();
          // The probe endpoint still has a hard lifetime bound. No success status
          // is recorded when the agent could not confirm listener removal.
          void stop({ confirmDetach: false });
        });
      }, maxLifetimeMs);
      lifetimeTimer.unref?.();
    }
    notifyStatus();
    return { pipeName, config: publicConfig() };
  }

  function publicConfig() {
    return {
      modulePath: config.modulePath,
      pipeName: config.pipeName,
      appPath: config.appPath,
      sessionName: config.sessionName,
      connectionGeneration: config.connectionGeneration,
      ...(config.wrapperModulePath ? { wrapperModulePath: config.wrapperModulePath } : {}),
      ...(config.reportPath ? { reportPath: config.reportPath } : {}),
      attach: false,
    };
  }

  async function stopInternal({ confirmDetach = true } = {}, owner = null) {
    if (stopped) return { stopped: true, detachedConfirmed: detachedIsConfirmed() };
    if (stopping) return { stopped: false, stopping: true, detachedConfirmed: false };
    if (detachAndStopInFlight && owner !== DETACH_AND_STOP_OWNER) {
      return { stopped: false, stopping: true, detachedConfirmed: false };
    }
    closing = true;
    stopping = true;
    if (lifetimeTimer) clearTimeout(lifetimeTimer);
    lifetimeTimer = null;
    if (confirmDetach && authenticated) {
      try { await detach(3000); }
      catch (error) {
        stopping = false;
        throw error;
      }
    } else if (confirmDetach && everAuthenticated && !detachedIsConfirmed()) {
      stopping = false;
      throw codedError("detach_unconfirmed_agent_disconnected");
    }
    stopped = true;
    authenticated = false;
    rejectPending("broker_stopped");
    if (activeSocket && !activeSocket.destroyed) activeSocket.destroy();
    activeSocket = null;
    if (server) await new Promise((resolve) => server.close(() => resolve()));
    server = null;
    started = false;
    stopping = false;
    notifyStatus();
    return { stopped: true, detachedConfirmed: detachedIsConfirmed() };
  }

  function stop(options) {
    return stopInternal(options, null);
  }

  function subscribe(handler) {
    if (typeof handler !== "function") throw codedError("invalid_event_subscriber");
    eventSubscribers.add(handler);
    let subscribed = true;
    return () => {
      if (!subscribed) return false;
      subscribed = false;
      return eventSubscribers.delete(handler);
    };
  }

  return Object.freeze({
    start,
    stop,
    detachAndStop,
    statusRequest,
    probeRendererCapabilities,
    probeAsarSources,
    probeAsarCandidates,
    probeOfficialWindows,
    probeRendererScriptMetadata,
    startOfficialAVObserver,
    drainOfficialAVObserver,
    stopOfficialAVObserver,
    privateInviteLifecycle,
    attach,
    detach,
    getAccountPath,
    forwardAVSDKOutput: (command, value, timeoutMs) => request("forwardAVSDKOutput", { command, value }, timeoutMs),
    subscribe,
    writeConfigFile,
    status,
    get config() { return publicConfig(); },
    get pipeName() { return pipeName; },
  });
}

if (require.main === module) {
  const options = new Map();
  for (let index = 2; index < process.argv.length; index += 1) {
    const key = process.argv[index];
    if (!key.startsWith("--") || index + 1 >= process.argv.length) throw codedError("invalid_cli_arguments");
    options.set(key.slice(2), process.argv[++index]);
  }
  const broker = createBroker({
    targetPid: Number(options.get("pid")),
    modulePath: path.resolve(options.get("bootstrap") || ""),
    appPath: path.resolve(options.get("app-path") || ""),
    wrapperModulePath: options.has("wrapper") ? path.resolve(options.get("wrapper")) : undefined,
    reportPath: path.resolve(options.get("report") || path.join(__dirname, ".runtime", `agent-${Number(options.get("pid"))}`)),
    sessionName: options.get("session") || "nt_1",
    connectionGeneration: Number(options.get("generation") || 1),
    maxLifetimeMs: Number(options.get("lifetime-ms") || DEFAULT_LIFETIME_MS),
    onEvent(op, payload) {
      const summary = op === "bootstrap_status" ? summarizeBootstrapStatus(payload) : summarizeEvent(op, payload);
      process.stdout.write(`${JSON.stringify({ event: summary })}\n`);
    },
  });
  const configPath = path.resolve(options.get("config") || path.join(__dirname, ".runtime", `agent-${process.pid}.config.json`));
  void (async () => {
    await broker.start();
    await broker.writeConfigFile(configPath);
    process.stdout.write(`QQCall broker ready for target PID ${broker.status().targetPid}, generation ${broker.status().connectionGeneration}\n`);
    process.stdout.write(`Bootstrap config written: ${configPath}\n`);
    const output = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
    const commands = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    commands.on("line", async (line) => {
      const command = line.trim().toLowerCase();
      if (!command) return;
      try {
        if (command === "status") output({ command, status: await broker.statusRequest() });
        else if (command === "attach") output({ command, result: await broker.attach() });
        else if (command === "detach") output({ command, result: await broker.detach() });
        else if (command === "quit") {
          output({ command, result: await broker.detachAndStop() });
          commands.close();
          process.exitCode = 0;
        } else output({ command, code: "unknown_command", accepted: ["status", "attach", "detach", "quit"] });
      } catch (error) {
        output({ command, code: error.code || "command_failed" });
      }
    });
    const shutdown = async () => {
      try { await broker.detachAndStop(); }
      catch (error) {
        process.stderr.write(`QQCall detach not confirmed: ${error.code || "detach_unconfirmed"}\n`);
        await broker.stop({ confirmDetach: false });
      }
      commands.close();
    };
    process.once("SIGINT", () => { void shutdown(); });
    process.once("SIGTERM", () => { void shutdown(); });
  })().catch((error) => {
    process.stderr.write(`QQCall broker failed: ${error.code || "startup_failed"}\n`);
    process.exitCode = 1;
  });
}

module.exports = { DEFAULT_LIFETIME_MS, createBroker, summarizeBootstrapStatus, summarizeEvent };
