"use strict";

const { randomUUID } = require("node:crypto");
const {
  PRIVATE_INVITE_PROFILE,
  PINNED_WRAPPER_SHA256,
  validateNativeInviteEvidence,
  validateNativeInviteProvenance,
} = require("../shared/private-invite-scope.cjs");
const { createGroupKernelObserver } = require("./group-kernel-observer.cjs");
const {
  createGroupCallDriver,
  NATIVE_GROUP_SOURCE,
  parseExistingRoomQueryResult,
} = require("../host/group-call.cjs");
const {
  PRIVATE_CALL_SCENE,
  QRTC_COMMAND_QUIT,
  QRTC_COMMAND_REJECT,
  QRTC_COMMAND_CLOSE,
  buildPrivateHangupParams,
  buildPrivateRejectParams,
  buildPrivateCloseParams,
} = require("../host/private-end.cjs");

const MAX_SIGNAL_QUEUE = 64;
const MAX_ENTER_RELATION_HISTORY = 64;
const BUSY_INVITE_COOLDOWN_MS = 60_000;
const MAX_BUSY_INVITE_FINGERPRINTS = 128;
const MAX_DIAGNOSTIC_INTEGER = Number.MAX_SAFE_INTEGER;
const EMPTY_GROUP_OBSERVATION_TTL_MS = 5000;
const ACK_COMMANDS = new Set([1, 5, 20004]);
const PRIVATE_END_COMMANDS = new Set([QRTC_COMMAND_QUIT, QRTC_COMMAND_REJECT, QRTC_COMMAND_CLOSE]);
const PRIVATE_END_ACK_TIMEOUT_MS = 5000;
const SAFE_OUTPUT_REJECTION_CODES = new Set([
  "kernel_listener_not_attached",
  "invalid_host_output",
  "pre_attach_output_overflow",
  "stale_host_generation",
  "host_generation_invalidated",
  "provider_generation_invalidated",
  "bridge_closing",
]);
const VERIFIED_INVITE_CORRELATIONS = new Set([
  "kernel_from_uid_matched",
  "kernel_relation_uid_matched",
]);
const CANONICAL_UIN = /^[1-9][0-9]{0,19}$/;
const QQ_UID = /^u_[A-Za-z0-9_-]{22}$/;
const CANONICAL_GROUP_ID = /^[1-9][0-9]{0,19}$/;
const MAX_GROUP_ID_BYTES = 20;

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function safeCode(error, fallback = "operation_failed") {
  const value = error && typeof error.code === "string" ? error.code : "";
  return /^[A-Za-z0-9_:-]{1,64}$/.test(value) ? value : fallback;
}

function positiveGeneration(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function validGroupId(value) {
  return typeof value === "string" && Buffer.byteLength(value, "utf8") <= MAX_GROUP_ID_BYTES &&
    CANONICAL_GROUP_ID.test(value);
}

const GROUP_SCOPE_KEYS = Object.freeze([
  "kind", "serviceInstance", "hostGeneration", "connectionGeneration", "groupId", "roomId", "roomGeneration",
]);

function exactGroupScope(scope, expected = undefined) {
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return false;
  const keys = Reflect.ownKeys(scope);
  if (keys.length !== GROUP_SCOPE_KEYS.length || keys.some((key) => typeof key !== "string" || !GROUP_SCOPE_KEYS.includes(key))) return false;
  if (scope.kind !== "qqcall_group" || typeof scope.serviceInstance !== "string" ||
      !positiveGeneration(scope.hostGeneration) || !positiveGeneration(scope.connectionGeneration) ||
      !validGroupId(scope.groupId) || !validGroupId(scope.roomId) || !positiveGeneration(scope.roomGeneration)) return false;
  if (expected && (scope.serviceInstance !== expected.serviceInstance || scope.hostGeneration !== expected.hostGeneration ||
      scope.connectionGeneration !== expected.connectionGeneration ||
      (expected.groupId !== undefined && scope.groupId !== expected.groupId))) return false;
  return true;
}

function groupScopesEqual(left, right) {
  return exactGroupScope(left) && exactGroupScope(right) && GROUP_SCOPE_KEYS.every((key) => left[key] === right[key]);
}

function validInt32(value) {
  return Number.isInteger(value) && value >= -0x80000000 && value <= 0x7fffffff;
}

function validNativeRelationId(value) {
  return typeof value === "string" && value.length >= 1 && value.length <= 512;
}

function copyKernelPeerUidByUin(value) {
  if (value === undefined) return null;
  try {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw codedError("invalid_kernel_peer_uid_mapping");
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) throw codedError("invalid_kernel_peer_uid_mapping");
    const keys = Reflect.ownKeys(value);
    if (keys.length > 64) throw codedError("invalid_kernel_peer_uid_mapping");
    const copy = Object.create(null);
    for (const key of keys) {
      if (typeof key !== "string" || key === "__proto__" || key === "prototype" || key === "constructor" || !CANONICAL_UIN.test(key)) {
        throw codedError("invalid_kernel_peer_uid_mapping");
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !("value" in descriptor) || typeof descriptor.value !== "string" || !QQ_UID.test(descriptor.value)) {
        throw codedError("invalid_kernel_peer_uid_mapping");
      }
      copy[key] = descriptor.value;
    }
    return Object.freeze(copy);
  } catch (error) {
    if (error?.code === "invalid_kernel_peer_uid_mapping") throw error;
    throw codedError("invalid_kernel_peer_uid_mapping");
  }
}

function pendingKey(command, id) {
  return `${command}:${id}`;
}

function getOwnData(object, key) {
  if (!object || typeof object !== "object") return undefined;
  try {
    const descriptor = Object.getOwnPropertyDescriptor(object, key);
    return descriptor && "value" in descriptor ? descriptor.value : undefined;
  } catch {
    return undefined;
  }
}

function getOwnArrayData(value, expectedLength) {
  if (!Array.isArray(value) || getOwnData(value, "length") !== expectedLength) return null;
  const items = [];
  for (let index = 0; index < expectedLength; index += 1) {
    try {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (!descriptor || !("value" in descriptor)) return null;
      items.push(descriptor.value);
    } catch {
      return null;
    }
  }
  return items;
}

function unwrapResult(reply) {
  if (reply?.ok === false) throw codedError(safeCode(reply, "operation_rejected"));
  if (reply?.result && typeof reply.result === "object") return reply.result;
  return reply;
}

function createQQCallBridge({
  provider,
  host,
  connectionGeneration,
  hostGeneration,
  audioDeviceTarget,
  kernelPeerUidByUin,
  callTimeoutMs = 60_000,
  privateEndAckTimeoutMs = PRIVATE_END_ACK_TIMEOUT_MS,
  officialPrivateInviteLifecycle = null,
  now = Date.now,
  maxSignalQueue = MAX_SIGNAL_QUEUE,
  maxForwardQueue = 64,
  maxForwardQueueBytes = 2 * 1024 * 1024,
  onStatus = () => {},
} = {}) {
  if (!provider || typeof provider.attach !== "function" || typeof provider.detach !== "function" ||
      typeof provider.status !== "function" || typeof provider.forwardAVSDKOutput !== "function" ||
      typeof provider.subscribe !== "function") throw codedError("invalid_kernel_provider");
  if (!host || typeof host.invoke !== "function" || typeof host.subscribe !== "function") throw codedError("invalid_host_client");
  if (officialPrivateInviteLifecycle !== null && typeof officialPrivateInviteLifecycle !== "function") throw codedError("invalid_private_invite_lifecycle");
  if (audioDeviceTarget !== undefined && audioDeviceTarget !== null) {
    const targetKeys = audioDeviceTarget && typeof audioDeviceTarget === "object" && !Array.isArray(audioDeviceTarget)
      ? Object.keys(audioDeviceTarget).sort() : [];
    if (typeof host.selectAudioDevices !== "function" || !audioDeviceTarget ||
        targetKeys.length !== 2 || targetKeys[0] !== "inputDeviceName" || targetKeys[1] !== "outputDeviceName" ||
        typeof audioDeviceTarget.inputDeviceName !== "string" || !audioDeviceTarget.inputDeviceName ||
        Buffer.byteLength(audioDeviceTarget.inputDeviceName, "utf8") > 512 ||
        typeof audioDeviceTarget.outputDeviceName !== "string" || !audioDeviceTarget.outputDeviceName ||
        Buffer.byteLength(audioDeviceTarget.outputDeviceName, "utf8") > 512 ||
        audioDeviceTarget.inputDeviceName.trim() !== audioDeviceTarget.inputDeviceName ||
        audioDeviceTarget.outputDeviceName.trim() !== audioDeviceTarget.outputDeviceName ||
        /[\u0000-\u001f\u007f]/.test(audioDeviceTarget.inputDeviceName) ||
        /[\u0000-\u001f\u007f]/.test(audioDeviceTarget.outputDeviceName)) {
      throw codedError("invalid_audio_device_target");
    }
  }
  const groupAudioDeviceTarget = audioDeviceTarget ? Object.freeze({
    inputDeviceName: audioDeviceTarget.inputDeviceName,
    outputDeviceName: audioDeviceTarget.outputDeviceName,
  }) : null;
  if (!positiveGeneration(connectionGeneration) || !positiveGeneration(hostGeneration)) throw codedError("invalid_generation");
  if (typeof now !== "function") throw codedError("invalid_clock");
  const privateKernelPeerUidByUin = copyKernelPeerUidByUin(kernelPeerUidByUin);
  if (!Number.isSafeInteger(callTimeoutMs) || callTimeoutMs < 100 || callTimeoutMs > 60 * 60 * 1000) throw codedError("invalid_call_timeout");
  if (!Number.isSafeInteger(privateEndAckTimeoutMs) || privateEndAckTimeoutMs < 100 || privateEndAckTimeoutMs > 120_000) {
    throw codedError("invalid_private_end_ack_timeout");
  }
  if (!Number.isSafeInteger(maxSignalQueue) || maxSignalQueue < 1 || maxSignalQueue > 1024) throw codedError("invalid_signal_queue_limit");
  if (!Number.isSafeInteger(maxForwardQueue) || maxForwardQueue < 1 || maxForwardQueue > 1024 ||
      !Number.isSafeInteger(maxForwardQueueBytes) || maxForwardQueueBytes < 1024 || maxForwardQueueBytes > 64 * 1024 * 1024) {
    throw codedError("invalid_forward_queue_limit");
  }
  const groupKernelObserver = createGroupKernelObserver({ connectionGeneration, now });

  let attached = false;
  let attaching = false;
  let attachPromise = null;
  let attachUncertain = false;
  let stopping = false;
  let closing = false;
  let stopped = false;
  let stopResult = null;
  let providerUnsubscribe = null;
  let hostUnsubscribe = null;
  let hostAvailable = false;
  let hostInvalidated = false;
  let providerInvalidated = false;
  let providerConnectionConfirmed = false;
  let controllerInvalidated = false;
  let loginState = "logged_out";
  let loginRequestId = null;
  let trustedSelfUid = null;
  let trustedSelfUin = null;
  let groupServiceInstance = null;
  let groupDriver = null;
  let groupStartRequestId = null;
  let groupStartGroupId = null;
  let groupPreflightRequestId = null;
  let groupPreflightSelectionTicket = null;
  let groupQuitRequestId = null;
  let groupCurrentScopeProof = null;
  let groupLeaseSuspended = false;
  let groupKernelEndTicket = null;
  const groupCommandOutputIds = new Set();
  let pendingAutoGroupInvite = null;
  let groupInvitationObservation = null;
  let groupStartInvitation = null;
  const seenAutoGroupInvites = new Set();
  let nextRequestId = 0;
  let nextCallGeneration = 0;
  let activeCall = null;
  let lastCall = null;
  let timer = null;
  let eventCount = 0;
  let outputCount = 0;
  let firstRejectedOutput = null;
  let preAttachOutputQueue = [];
  let preAttachOutputCount = 0;
  let preAttachOutputBytes = 0;
  let unassociatedPreAttachOutputCount = 0;
  let signalCount = 0;
  let busyInviteCount = 0;
  let busyInviteDedupeGeneration = null;
  const busyInviteFingerprints = new Map();
  let lastNativeInviteCallbackSequence = 0;
  let lastNativeInviteIpcSequence = 0;
  let unmatchedCallbackCount = 0;
  let uncorrelatedAckCount = 0;
  let lastFailureCode = null;
  let signalQueue = [];
  let drainingSignals = false;
  let hostOutputTail = Promise.resolve();
  let forwardQueue = [];
  let forwardQueuedBytes = 0;
  let forwardOutstanding = 0;
  let drainingForwards = false;
  let duplicateInviteCallbackCount = 0;
  let duplicateKernelInviteCount = 0;
  const statusSubscribers = new Set();
  const controlEventSubscribers = new Set();
  const pending = new Map();
  const enterRelationHistory = new Set();

  function allocateRequestId() {
    nextRequestId += 1;
    if (!Number.isSafeInteger(nextRequestId) || nextRequestId <= 0) throw codedError("request_id_exhausted");
    return nextRequestId;
  }

  function emitStatus(kind = "status") {
    const event = { kind, status: status() };
    try {
      const result = onStatus(event);
      if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
    } catch { /* observers do not participate in call state */ }
    for (const handler of statusSubscribers) {
      try {
        const result = handler(event);
        if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      } catch { /* observers do not participate in call state */ }
    }
  }

  function emitControlEvent(event) {
    for (const handler of controlEventSubscribers) {
      try {
        const result = handler(event);
        if (result && typeof result.then === "function") Promise.resolve(result).catch(() => {});
      } catch { /* control observers do not participate in call state */ }
    }
  }

  function resetBusyInviteDedupe() {
    busyInviteDedupeGeneration = null;
    busyInviteFingerprints.clear();
  }

  function shouldPublishBusyInvite(call, metadata) {
    if (busyInviteDedupeGeneration !== call.generation) {
      busyInviteDedupeGeneration = call.generation;
      busyInviteFingerprints.clear();
    }
    let currentTime;
    try { currentTime = now(); }
    catch { return false; }
    if (typeof currentTime !== "number" || !Number.isFinite(currentTime)) return false;

    for (const [fingerprint, timestamp] of busyInviteFingerprints) {
      if (currentTime < timestamp || currentTime - timestamp >= BUSY_INVITE_COOLDOWN_MS) {
        busyInviteFingerprints.delete(fingerprint);
      }
    }
    const previous = busyInviteFingerprints.get(metadata.fingerprint);
    if (previous !== undefined) return false;
    busyInviteFingerprints.set(metadata.fingerprint, currentTime);
    while (busyInviteFingerprints.size > MAX_BUSY_INVITE_FINGERPRINTS) {
      const oldest = busyInviteFingerprints.keys().next().value;
      busyInviteFingerprints.delete(oldest);
    }
    return true;
  }

  function busyInviteIdentity(metadata) {
    if (!metadata || !VERIFIED_INVITE_CORRELATIONS.has(metadata.identityCorrelation) ||
        !metadata.nativeInviteEvidence ||
        typeof metadata.fromUid !== "string" || !QQ_UID.test(metadata.fromUid) ||
        /[\u0000-\u001f\u007f]/.test(metadata.fromUid) ||
        (metadata.nativeInviteEvidence.fromUid === ""
          ? metadata.identityCorrelation !== "kernel_relation_uid_matched" ||
            !CANONICAL_UIN.test(metadata.relationId) ||
            privateKernelPeerUidByUin?.[metadata.relationId] !== metadata.fromUid
          : metadata.identityCorrelation !== "kernel_from_uid_matched" ||
            metadata.nativeInviteEvidence.fromUid !== metadata.fromUid)) return null;
    const inviteEventId = randomUUID();
    return {
      callerUid: metadata.fromUid,
      identityCorrelation: metadata.identityCorrelation,
      nativeInviteEvidence: { ...metadata.nativeInviteEvidence, inviteEventId },
      inviteEventId,
    };
  }

  function currentCallSummary(call = activeCall) {
    if (!call) return null;
    return {
      callId: call.callId,
      generation: call.generation,
      phase: call.phase,
      uncertain: Boolean(call.uncertain),
      inviteCallbackReady: Boolean(call.inviteParams),
      inviteCorrelation: call.inviteCorrelation,
      acceptState: call.acceptState,
      connected: call.phase === "connected",
      remoteEndedConfirmed: Boolean(call.remoteEndedConfirmed),
      ...(call.endState ? { endState: call.endState } : {}),
      ...(call.roomDestroyedConfirmed === true ? { roomDestroyedConfirmed: true } : {}),
      ...(call.invitationRejectedConfirmed === true ? { invitationRejectedConfirmed: true } : {}),
      observedDestroyReason: call.observedDestroyReason,
    };
  }

  // This predicate is deliberately side-effect free: it compares the captured
  // per-invite proof with the Broker's live binding, including the listener
  // registration that produced the callback. The caller separately retires a
  // proof once a mismatch has been observed so a restored binding cannot
  // revive evidence from an earlier listener lifetime.
  function privateScopeBindingStillMatches(call, providerStatus) {
    const evidence = call?.privateScopeEvidence;
    const binding = providerStatus?.nativeInviteBinding;
    return Boolean(evidence && !call.privateScopeEvidenceInvalidated && binding &&
      providerStatus.connected === true && providerStatus.authenticated === true &&
      providerStatus.connectionGeneration === connectionGeneration &&
      evidence.profile === PRIVATE_INVITE_PROFILE && evidence.wrapperSha256 === PINNED_WRAPPER_SHA256 &&
      evidence.targetPid === binding.targetPid &&
      evidence.kernelCreationTime100ns === binding.kernelCreationTime100ns &&
      evidence.connectionGeneration === binding.connectionGeneration &&
      evidence.listenerMutationEpoch === binding.listenerMutationEpoch &&
      evidence.listenerRegistrationSequence === binding.listenerRegistrationSequence &&
      binding.profile === PRIVATE_INVITE_PROFILE && binding.wrapperSha256 === PINNED_WRAPPER_SHA256 &&
      Number.isSafeInteger(binding.targetPid) && binding.targetPid > 0 &&
      typeof binding.kernelCreationTime100ns === "string" &&
      Number.isSafeInteger(binding.connectionGeneration) &&
      Number.isSafeInteger(binding.listenerMutationEpoch) &&
      Number.isSafeInteger(binding.listenerRegistrationSequence));
  }

  function retirePrivateScopeEvidenceIfStale(call = activeCall, providerStatus = undefined) {
    if (!call?.privateScopeEvidence || call.privateScopeEvidenceInvalidated) return false;
    let statusValue = providerStatus;
    if (statusValue === undefined) {
      try { statusValue = provider.status(); }
      catch { statusValue = null; }
    }
    if (privateScopeBindingStillMatches(call, statusValue)) return true;
    call.privateScopeEvidenceInvalidated = true;
    call.privateScopeEvidence = null;
    return false;
  }

  function assertPrivateScopeEvidenceCurrent(call) {
    if (!call) return;
    if (call.privateScopeEvidenceInvalidated ||
        (call.privateScopeEvidence && !retirePrivateScopeEvidenceIfStale(call))) {
      throw codedError("private_scope_unverified");
    }
  }

  function requirePrivateEndScope(callId, operation) {
    canOperate();
    if (!audioDeviceTarget) throw codedError(`unsupported_unverified_command:${operation}`);
    const call = activeCall;
    if (!call || typeof callId !== "string" || call.callId !== callId) throw codedError("call_not_current");
    if (call.connectionGeneration !== connectionGeneration || call.hostGeneration !== hostGeneration ||
        call.generation !== activeCall?.generation) throw codedError("call_not_current");
    if (loginState !== "ready") throw codedError("login_not_ready");
    if (!hostAvailable || hostInvalidated) throw codedError("host_unavailable");
    if (!call.inviteParams || call.inviteParams[0] !== PRIVATE_CALL_SCENE ||
        typeof call.fromUid !== "string" || !QQ_UID.test(call.fromUid) ||
        call.inviteParams[1] !== call.fromUid || call.inviteCallbackMismatch || !call.inviteCallbackMatched ||
        !VERIFIED_INVITE_CORRELATIONS.has(call.inviteCorrelation)) {
      throw codedError(`unsupported_unverified_command:${operation}`);
    }
    if (!call.privateScopeEvidence || call.privateScopeEvidenceInvalidated) {
      throw codedError("private_scope_unverified");
    }
    const capturedEvidence = call.privateScopeEvidence;
    assertPrivateScopeEvidenceCurrent(call);
    let providerStatus;
    try { providerStatus = provider.status(); }
    catch { providerStatus = null; }
    if (!providerStatus || providerStatus.connected !== true || providerStatus.authenticated !== true ||
        providerStatus.connectionGeneration !== connectionGeneration ||
        !privateScopeBindingStillMatches(call, providerStatus) || call.privateScopeEvidence !== capturedEvidence) {
      throw codedError("private_scope_unverified");
    }
    return { call, capturedEvidence };
  }

  function clearPrivateEndAckTimer(call) {
    if (call?.endAckTimer) clearTimeout(call.endAckTimer);
    if (call) call.endAckTimer = null;
  }

  function clearPrivateCloseAckTimer(call) {
    if (call?.closeAckTimer) clearTimeout(call.closeAckTimer);
    if (call) call.closeAckTimer = null;
  }

  function schedulePrivateEndAckTimeout(call, operation, id, entry) {
    clearPrivateEndAckTimer(call);
    call.endAckTimer = setTimeout(() => {
      if (activeCall !== call || call.endRequestId !== id || call.endOperation !== operation ||
          pending.get(pendingKey(entry.command, id)) !== entry) return;
      pending.delete(pendingKey(entry.command, id));
      call.endAckTimer = null;
      call.endState = `${operation}_ack_timeout`;
      call.phase = operation === "hangup" ? "ending_unconfirmed" : "reject_unconfirmed";
      call.uncertain = true;
      lastFailureCode = `${operation}_ack_timeout`;
      emitStatus(`${operation}_ack_timeout`);
    }, privateEndAckTimeoutMs);
    call.endAckTimer.unref?.();
  }

  function markPrivateCloseUnconfirmed(call, code, event = "hangup_close_unconfirmed") {
    if (activeCall !== call || call.roomDestroyedConfirmed) return;
    clearPrivateCloseAckTimer(call);
    call.closeState = code;
    call.phase = "ending_unconfirmed";
    call.endState = `hangup_close_${code}`;
    call.uncertain = true;
    lastFailureCode = `hangup_close_${code}`;
    emitStatus(event);
  }

  function schedulePrivateCloseAckTimeout(call, id, entry) {
    clearPrivateCloseAckTimer(call);
    call.closeAckTimer = setTimeout(() => {
      const key = pendingKey(QRTC_COMMAND_CLOSE, id);
      if (activeCall !== call || call.closeRequestId !== id || !["posting", "posted"].includes(call.closeState) ||
          pending.get(key) !== entry) return;
      pending.delete(key);
      call.closeAckTimer = null;
      markPrivateCloseUnconfirmed(call, "ack_timeout", "hangup_close_ack_timeout");
    }, privateEndAckTimeoutMs);
    call.closeAckTimer.unref?.();
  }

  function reserveAndDispatchPrivateClose(call, quitEntry) {
    let id;
    let params;
    try {
      id = allocateRequestId();
      params = buildPrivateCloseParams(quitEntry.peerUid);
    } catch {
      markPrivateCloseUnconfirmed(call, "not_posted");
      return;
    }
    const key = pendingKey(QRTC_COMMAND_CLOSE, id);
    const entry = {
      command: QRTC_COMMAND_CLOSE,
      requestId: id,
      hostGeneration: quitEntry.hostGeneration,
      callGeneration: quitEntry.callGeneration,
      connectionGeneration: quitEntry.connectionGeneration,
      callId: quitEntry.callId,
      operation: "hangup",
      stage: "close",
      quitRequestId: quitEntry.requestId,
      peerUid: quitEntry.peerUid,
      privateScopeEvidence: quitEntry.privateScopeEvidence,
      dispatchStarted: false,
    };

    // Keep endRequestId bound to the public hangup (cmd 8). Close gets its
    // own private id and state so a late cmd 8 receipt cannot rewrite it.
    call.closeRequestId = id;
    call.closeState = "posting";
    call.endState = "hangup_close_posting";
    call.phase = "ending";
    pending.set(key, entry);
    schedulePrivateCloseAckTimeout(call, id, entry);
    emitStatus("hangup_close_posting");

    const assertDispatchCurrent = () => {
      canOperate();
      if (activeCall !== call || call.callId !== entry.callId || call.generation !== entry.callGeneration ||
          call.hostGeneration !== entry.hostGeneration || hostGeneration !== entry.hostGeneration ||
          call.connectionGeneration !== entry.connectionGeneration || connectionGeneration !== entry.connectionGeneration ||
          call.fromUid !== entry.peerUid || call.inviteParams?.[1] !== entry.peerUid ||
          call.endOperation !== "hangup" || call.endRequestId !== entry.quitRequestId ||
          call.closeRequestId !== id || call.closeState !== "posting" || pending.get(key) !== entry ||
          entry.stage !== "close" || entry.operation !== "hangup") return false;
      const live = requirePrivateEndScope(entry.callId, "hangup");
      if (live.call !== call || live.capturedEvidence !== entry.privateScopeEvidence ||
          call.privateScopeEvidence !== entry.privateScopeEvidence) return false;
      entry.dispatchStarted = true;
      return true;
    };

    // Host output callbacks are serialized and the callback HTTP response
    // waits for them. Do not await this second HTTP receipt from that handler.
    void (async () => {
      try {
        const receipt = await host.invoke(
          { command: QRTC_COMMAND_CLOSE, id, params, hostGeneration: entry.hostGeneration },
          { assertCurrent: assertDispatchCurrent },
        );
        if (receipt?.posted !== true || receipt.id !== id || receipt.command !== QRTC_COMMAND_CLOSE) {
          throw codedError("host_post_receipt_invalid");
        }
        if (activeCall === call && pending.get(key) === entry && call.closeState === "posting") {
          call.closeState = "posted";
          call.endState = "hangup_close_posted";
          schedulePrivateCloseAckTimeout(call, id, entry);
          emitStatus("hangup_close_posted");
        }
      } catch (error) {
        if (activeCall === call && pending.get(key) === entry && call.closeRequestId === id) {
          pending.delete(key);
          const code = safeCode(error, "private_close_post_failed");
          markPrivateCloseUnconfirmed(call, code === "host_post_receipt_invalid" ? "receipt_invalid" : "post_unconfirmed",
            "hangup_close_post_unconfirmed");
        }
      }
    })();
  }

  function integrationCallSummary() {
    if (!activeCall) return null;
    const hasCurrentPrivateScopeEvidence = retirePrivateScopeEvidenceIfStale(activeCall);
    const callbackAllowsPeer = !activeCall.inviteCallbackObserved ||
      (activeCall.inviteCallbackMatched && !activeCall.inviteCallbackMismatch);
    return {
      callId: activeCall.callId,
      generation: activeCall.generation,
      peerUid: callbackAllowsPeer && typeof activeCall.fromUid === "string" ? activeCall.fromUid : null,
      phase: activeCall.phase,
      uncertain: Boolean(activeCall.uncertain),
      inviteCallbackReady: Boolean(activeCall.inviteParams),
      inviteCorrelation: activeCall.inviteCorrelation,
      acceptState: activeCall.acceptState,
      ...(activeCall.endState ? { endState: activeCall.endState } : {}),
      // The Kernel profile identifies a C2C producer; only the matched SDK
      // callback's scene establishes that this active call is private.
      ...(hasCurrentPrivateScopeEvidence && activeCall.inviteCallbackMatched && !activeCall.inviteCallbackMismatch &&
          activeCall.inviteParams?.[0] === PRIVATE_CALL_SCENE
        ? { nativeInviteEvidence: { ...activeCall.privateScopeEvidence, inviteEventId: activeCall.inviteEventId } }
        : {}),
    };
  }

  function status() {
    const hostStatus = typeof host.status === "function" ? host.status() : null;
    const result = {
      attached,
      attachUncertain,
      stopping,
      closing,
      stopped,
      connectionGeneration,
      hostGeneration,
      ...(Number.isSafeInteger(hostStatus?.targetPid) ? { hostPid: hostStatus.targetPid } : {}),
      hostAvailable,
      hostInvalidated,
      providerInvalidated,
      controllerInvalidated,
      loginState,
      groupInvitationObservation: groupInvitationObservation ? { ...groupInvitationObservation } : null,
      activeCall: currentCallSummary(),
      lastCall: lastCall ? { ...lastCall } : null,
      counts: {
        kernelEvents: eventCount,
        hostOutputs: outputCount,
        kernelSignals: signalCount,
        busyInvites: busyInviteCount,
        unmatchedInviteCallbacks: unmatchedCallbackCount,
        duplicateInviteCallbacks: duplicateInviteCallbackCount,
        duplicateKernelInvites: duplicateKernelInviteCount,
        uncorrelatedAcks: uncorrelatedAckCount,
        queuedSignals: signalQueue.length,
        pendingAcks: pending.size,
        queued20001: forwardQueue.length,
        outstanding20001: forwardOutstanding,
        queued20001Bytes: forwardQueuedBytes,
        preAttachStoredOutputs: preAttachOutputCount,
        preAttachStoredOutputBytes: preAttachOutputBytes,
        unassociatedPreAttachOutputs: unassociatedPreAttachOutputCount,
        groupDiagnosticCounts: groupKernelObserver.status(),
      },
      ...(firstRejectedOutput ? { firstRejectedOutput: { ...firstRejectedOutput } } : {}),
      lastFailureCode,
    };
    if (typeof hostStatus?.groupCallProtocolAvailable === "boolean") {
      result.groupCallProtocolAvailable = hostStatus.groupCallProtocolAvailable;
    }
    if (groupDriver) result.groupCall = groupDriver.status();
    return result;
  }

  function canOperate() {
    if (stopped || stopping || closing) throw codedError("bridge_closing");
    if (!attached) throw codedError("bridge_not_attached");
    if (providerInvalidated) throw codedError("provider_generation_invalidated");
    if (hostInvalidated) throw codedError("host_generation_invalidated");
    if (controllerInvalidated) throw codedError("controller_generation_invalidated");
  }

  function clearCallTimer() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function invalidatePendingForCall(callGeneration) {
    for (const [id, entry] of pending) {
      if (entry.callGeneration === callGeneration) pending.delete(id);
    }
  }

  function finishCall(phase, reason, remoteEndedConfirmed = false, roomDestroyedConfirmed = false, invitationRejectedConfirmed = false) {
    if (!activeCall) return;
    const finished = activeCall;
    // A confirmed room destruction is terminal. In particular, status
    // subscribers run synchronously while the terminal projection is emitted;
    // a re-entrant disconnect must not replace that evidence with a weaker
    // uncertain terminal result.
    if (finished.roomDestroyedConfirmed || finished.invitationRejectedConfirmed) return;
    const remoteConfirmed = Boolean(remoteEndedConfirmed);
    const roomDestroyed = Boolean(roomDestroyedConfirmed);
    const invitationRejected = Boolean(invitationRejectedConfirmed);
    clearCallTimer();
    clearPrivateEndAckTimer(finished);
    clearPrivateCloseAckTimer(finished);
    invalidatePendingForCall(finished.generation);
    finished.phase = phase;
    finished.remoteEndedConfirmed = remoteConfirmed;
    finished.roomDestroyedConfirmed = roomDestroyed;
    finished.invitationRejectedConfirmed = invitationRejected;
    if (!remoteConfirmed && !roomDestroyed && !invitationRejected && finished.endOperation &&
        ["posting", "posted"].includes(finished.endState)) {
      finished.endState = `${finished.endOperation}_interrupted`;
    }
    lastCall = {
      callId: finished.callId,
      generation: finished.generation,
      phase,
      terminalReason: reason,
      remoteEndedConfirmed: remoteConfirmed,
      ...(finished.endOperation ? { endOperation: finished.endOperation } : {}),
      ...(finished.endState ? { endState: finished.endState } : {}),
      ...(roomDestroyed ? { roomDestroyedConfirmed: true } : {}),
      ...(invitationRejected ? { invitationRejectedConfirmed: true } : {}),
      destroyReasonObserved: Boolean(finished.observedDestroyReason),
    };
    finished.uncertain = !(remoteConfirmed || roomDestroyed || invitationRejected);
    if (remoteConfirmed && !roomDestroyed) {
      activeCall = null;
      resetBusyInviteDedupe();
    }
    emitStatus("call_terminal");
    if ((roomDestroyed || invitationRejected) && activeCall === finished) {
      activeCall = null;
      resetBusyInviteDedupe();
      emitStatus("call_cleared");
    }
  }

  function failCall(reason, phase = "failed") {
    finishCall(phase, reason, false);
  }

  function officialPrivateInviteRequest(call, operation) {
    const source = call.privateScopeEvidence;
    const evidence = {};
    for (const key of ["profile", "wrapperSha256", "callbackSequence", "listenerRegistrationSequence", "inviteType", "relationId", "fromUid"]) {
      evidence[key] = source?.[key];
    }
    return { operation, evidence, peerUid: call.fromUid, selfUid: trustedSelfUid, selfUin: trustedSelfUin,
      inviteParams: JSON.parse(JSON.stringify(call.inviteParams)) };
  }

  function confirmPrivateRejectReadback(call, entry) {
    if (typeof host.confirmPrivateRejectAbsence !== "function" || entry.posted !== true ||
        call.rejectReadbackStarted || call.endState !== "reject_acknowledged_unconfirmed") return;
    const current = () => {
      try {
        const live = requirePrivateEndScope(entry.callId, "reject");
        return live.call === call && call.privateScopeEvidence === entry.privateScopeEvidence &&
          call.generation === entry.callGeneration && call.connectionGeneration === entry.connectionGeneration &&
          call.hostGeneration === entry.hostGeneration && call.endOperation === "reject" &&
          call.endRequestId === entry.requestId && call.endState === "reject_acknowledged_unconfirmed" &&
          call.acceptState === "not_requested" && call.acceptRequestId === null &&
          call.enterRelationId === null && call.roomEntry === null;
      } catch { return false; }
    };
    if (!current()) return;
    call.rejectReadbackStarted = true;
    let id;
    try { id = allocateRequestId(); } catch { return; }
    const expected = { id, hostGeneration, peerUid: entry.peerUid, rejectRequestId: entry.requestId };
    void Promise.resolve().then(async () => {
      if (officialPrivateInviteLifecycle) {
        if (!current()) return null;
        const ended = await officialPrivateInviteLifecycle(officialPrivateInviteRequest(call, "reject"));
        if (!current()) return null;
        if (ended?.confirmed !== true || !["official_private_invite_document_ended", "official_private_invite_receiving_closed"].includes(ended.code)) {
          lastFailureCode = ["private_invite_lifecycle_stale", "private_invite_lifecycle_busy", "private_invite_window_unproven",
            "official_private_invite_end_unconfirmed", "official_private_invite_unavailable"].includes(ended?.code)
            ? ended.code : "official_private_invite_end_unconfirmed";
          emitStatus("official_private_invite_end_unconfirmed");
          return null;
        }
      }
      if (!current()) return null;
      return host.confirmPrivateRejectAbsence(expected, { assertCurrent: current });
    })
      .then((proof) => {
        if (!current() || proof?.ok !== true || proof.confirmed !== true ||
            !["native_reject_ack_and_sdk_room_absence", "native_reject_ack_and_no_room_admission"].includes(proof.source) ||
            !Object.keys(expected).every((key) => proof[key] === expected[key])) return;
        call.endState = "reject_confirmed";
        lastFailureCode = null;
        finishCall("ended", proof.source === "native_reject_ack_and_no_room_admission"
          ? "native_reject_before_any_room_admission" : "native_reject_and_room_absence", false, false, true);
      }, () => { /* An absent/invalid readback keeps the original uncertain busy barrier. */ });
  }

  function synchronizePrivateKernelRoomEnd(call) {
    if (typeof host.confirmPrivateEndedRoomAbsence !== "function") {
      finishCall("ended", "kernel_current_room_destroyed", false, true);
      return;
    }
    if (call.kernelRoomEndReadbackStarted) return;
    const evidence = call.privateScopeEvidence;
    const relation = call.enterRelationId;
    const peer = call.fromUid;
    const current = () => {
      try {
        const live = requirePrivateEndScope(call.callId, "hangup");
        return live.call === call && call.privateScopeEvidence === evidence && call.enterRelationId === relation &&
          call.fromUid === peer && call.hostGeneration === hostGeneration && call.connectionGeneration === connectionGeneration;
      } catch { return false; }
    };
    if (!current()) return;
    call.kernelRoomEndReadbackStarted = true;
    call.phase = "ending_kernel_verified";
    call.uncertain = true;
    emitStatus("kernel_room_end_readback_pending");
    let id;
    try { id = allocateRequestId(); } catch { return; }
    const expected = { id, hostGeneration, peerUid: peer, enterRelationId: relation };
    const unconfirmed = () => {
      if (!current()) return;
      call.phase = "ending_unconfirmed";
      lastFailureCode = "private_host_room_release_unconfirmed";
      emitStatus("private_host_room_release_unconfirmed");
    };
    void Promise.resolve().then(() => host.confirmPrivateEndedRoomAbsence(expected, { assertCurrent: current }))
      .then((proof) => {
        if (!current()) return;
        if (proof?.ok !== true || proof.confirmed !== true || proof.source !== "native_entered_private_room_absence" ||
            !Object.keys(expected).every(key => proof[key] === expected[key])) { unconfirmed(); return; }
        lastFailureCode = null;
        finishCall("ended", "kernel_current_room_destroyed", false, true);
      }, unconfirmed);
  }

  function clearForwardQueue(code) {
    for (const item of forwardQueue.splice(0)) {
      forwardQueuedBytes -= item.bytes;
      forwardOutstanding -= 1;
      item.reject(codedError(code));
    }
  }

  function clearPreAttachOutputQueue() {
    preAttachOutputQueue = [];
    preAttachOutputCount = 0;
    preAttachOutputBytes = 0;
  }

  function invalidateController(code) {
    if (controllerInvalidated) return;
    controllerInvalidated = true;
    groupDriver?.stop();
    groupKernelObserver.stop();
    loginState = "invalidated";
    loginRequestId = null;
    pending.clear();
    signalQueue = [];
    clearForwardQueue(code);
    clearPreAttachOutputQueue();
    failCall(code, "failed");
    lastFailureCode = code;
    emitStatus("controller_invalidated");
  }

  function enqueue20001(value, metadata = undefined) {
    if (closing || stopped || providerInvalidated || hostInvalidated || controllerInvalidated) {
      lastFailureCode = closing || stopped ? "bridge_closing" : "bridge_generation_invalidated";
      emitStatus("output_rejected");
      return Promise.reject(codedError(lastFailureCode));
    }
    let bytes;
    try { bytes = Buffer.byteLength(JSON.stringify(value), "utf8"); }
    catch { bytes = maxForwardQueueBytes + 1; }
    if (forwardOutstanding >= maxForwardQueue || forwardQueuedBytes + bytes > maxForwardQueueBytes) {
      invalidateController("forward_queue_overflow");
      return Promise.reject(codedError("forward_queue_overflow"));
    }
    let resolveQueued;
    const queued = new Promise((resolve, reject) => {
      resolveQueued = resolve;
      forwardOutstanding += 1;
      forwardQueuedBytes += bytes;
      forwardQueue.push({
        value,
        bytes,
        resolve,
        reject,
        hostGeneration: metadata?.hostGeneration ?? hostGeneration,
        connectionGeneration: metadata?.connectionGeneration ?? connectionGeneration,
        sequence: metadata?.sequence ?? outputCount,
        receivedBeforeAttach: metadata?.receivedBeforeAttach === true,
      });
    });
    if (attached) void drain20001();
    else {
      resolveQueued({
        receivedStored: true,
        sequence: metadata?.sequence ?? outputCount,
        receivedBeforeAttach: true,
      });
    }
    return queued;
  }

  function providerIsCurrent() {
    if (providerInvalidated) return false;
    try {
      const current = provider.status();
      return current?.connected === true && current.authenticated !== false &&
        (current.connectionGeneration === undefined || current.connectionGeneration === connectionGeneration);
    }
    catch { return false; }
  }

  function hostIsCurrent() {
    if (!hostAvailable || hostInvalidated) return false;
    if (typeof host.status !== "function") return true;
    try {
      const hostStatus = host.status();
      return hostStatus?.available === true &&
        (hostStatus.hostGeneration === undefined || hostStatus.hostGeneration === hostGeneration);
    } catch { return false; }
  }

  function consumePreAttachOutputs() {
    if (!preAttachOutputCount && !preAttachOutputQueue.length) return true;
    if (!attached || closing || stopping || stopped || controllerInvalidated || !providerIsCurrent() || !hostIsCurrent()) {
      return false;
    }
    const validStoredOutput = (item) => item?.receivedBeforeAttach === true &&
      item.hostGeneration === hostGeneration && item.connectionGeneration === connectionGeneration &&
      Number.isSafeInteger(item.sequence) && item.sequence > 0;
    if (preAttachOutputQueue.some((item) => !validStoredOutput(item)) ||
        forwardQueue.some((item) => item.receivedBeforeAttach && !validStoredOutput(item))) {
      invalidateController("stale_host_generation");
      return false;
    }
    const observationCount = preAttachOutputQueue.length;
    clearPreAttachOutputQueue();
    if (observationCount) {
      unassociatedPreAttachOutputCount = Math.min(0x7fffffff, unassociatedPreAttachOutputCount + observationCount);
      emitStatus("preattach_outputs_unassociated");
    }
    return true;
  }

  async function drain20001() {
    if (!attached || !hostIsCurrent() || !providerIsCurrent() || controllerInvalidated ||
        closing || stopping || stopped || drainingForwards) return;
    drainingForwards = true;
    try {
      while (forwardQueue.length && hostIsCurrent() && providerIsCurrent() &&
          !closing && !stopping && !stopped && !controllerInvalidated) {
        const item = forwardQueue.shift();
        try {
          if (item.hostGeneration !== hostGeneration || item.connectionGeneration !== connectionGeneration ||
              !Number.isSafeInteger(item.sequence) || item.sequence < 1) {
            throw codedError("stale_host_generation");
          }
          const reply = unwrapResult(await provider.forwardAVSDKOutput(20001, item.value));
          if (reply?.forwarded !== true) throw codedError(safeCode(reply, "forward_not_confirmed"));
          item.resolve({ forwarded: true });
        } catch (error) {
          item.reject(error);
          invalidateController(safeCode(error, "forward_failed"));
        } finally {
          forwardOutstanding -= 1;
          forwardQueuedBytes -= item.bytes;
        }
      }
    } finally {
      drainingForwards = false;
    }
  }

  function onHostAvailability(available) {
    const next = Boolean(available);
    const wasAvailable = hostAvailable;
    hostAvailable = next;
    if (wasAvailable && !next) {
      hostInvalidated = true;
      groupDriver?.stop();
      trustedSelfUid = null;
      trustedSelfUin = null;
      groupKernelObserver.stop();
      loginState = "invalidated";
      loginRequestId = null;
      pending.clear();
      signalQueue = [];
      clearForwardQueue("host_generation_invalidated");
      clearPreAttachOutputQueue();
      const call = activeCall;
      const hangupCloseReserved = call?.endOperation === "hangup" &&
        Number.isSafeInteger(call.closeRequestId) && call.closeRequestId > 0;
      if (hangupCloseReserved) {
        if (["posting", "posted", "acknowledged"].includes(call.closeState)) {
          call.closeState = "interrupted";
          call.endState = "hangup_close_interrupted";
        }
        // Command 8 already succeeded, so a later Host loss cannot turn the
        // unresolved hangup into a failed call or release its occupied slot.
        failCall("host_disconnected", "ending_unconfirmed");
      } else {
        failCall("host_disconnected", "failed");
      }
      lastFailureCode = "host_disconnected";
    } else if (next && attached) {
      if (consumePreAttachOutputs()) void drain20001();
    }
    emitStatus("host_availability");
  }

  function onProviderStatus(payload) {
    retirePrivateScopeEvidenceIfStale();
    const connected = payload?.connected;
    if (payload?.connectionGeneration !== undefined && payload.connectionGeneration !== connectionGeneration) return;
    const authenticated = payload?.authenticated;
    if (connected === true && authenticated !== false) {
      providerConnectionConfirmed = true;
      if (attached) {
        if (consumePreAttachOutputs()) void drain20001();
      }
      return;
    }
    // The Broker reports connected=false throughout its normal wait for the
    // Kernel agent. Only invalidate after this generation had a usable,
    // authenticated connection; an unconfirmed startup state is not a loss.
    if ((connected === false || authenticated === false) && providerConnectionConfirmed && !providerInvalidated) {
      providerInvalidated = true;
      groupDriver?.stop();
      trustedSelfUid = null;
      trustedSelfUin = null;
      groupKernelObserver.stop();
      loginState = "invalidated";
      loginRequestId = null;
      pending.clear();
      signalQueue = [];
      clearForwardQueue("provider_generation_invalidated");
      clearPreAttachOutputQueue();
      failCall("provider_disconnected", "failed");
      lastFailureCode = "provider_disconnected";
      emitStatus("provider_invalidated");
    }
  }

  function validEventGeneration(event) {
    return getOwnData(event, "connectionGeneration") === connectionGeneration;
  }

  function privateScopeEvidenceForEvent(event) {
    if (!validEventGeneration(event)) return null;
    let providerStatus;
    try { providerStatus = provider.status(); }
    catch { return null; }
    if (providerStatus?.connected !== true || providerStatus.authenticated !== true ||
        providerStatus.connectionGeneration !== connectionGeneration) return null;
    const binding = providerStatus.nativeInviteBinding;
    const evidence = validateNativeInviteEvidence(event?.nativeInviteEvidence, event, binding);
    if (!evidence) return null;
    const ipcSequence = event?.nativeInviteProvenance?.ipcSequence;
    const provenance = validateNativeInviteProvenance(event?.nativeInviteProvenance, binding, ipcSequence);
    if (!provenance || provenance.connectionGeneration !== connectionGeneration ||
        evidence.callbackSequence <= lastNativeInviteCallbackSequence ||
        provenance.ipcSequence <= lastNativeInviteIpcSequence) return null;
    lastNativeInviteCallbackSequence = evidence.callbackSequence;
    lastNativeInviteIpcSequence = provenance.ipcSequence;
    return Object.freeze({ ...evidence, ...provenance });
  }

  function inviteMetadata(args) {
    const value = Array.isArray(args) ? args[0] : undefined;
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    let relationDescriptor;
    let typeDescriptor;
    let fromDescriptor;
    try {
      relationDescriptor = Object.getOwnPropertyDescriptor(value, "relation_id");
      typeDescriptor = Object.getOwnPropertyDescriptor(value, "invite_type");
      fromDescriptor = Object.getOwnPropertyDescriptor(value, "from_uid");
    } catch {
      return null;
    }
    const hasRelation = Boolean(relationDescriptor);
    const hasType = Boolean(typeDescriptor);
    const hasFrom = Boolean(fromDescriptor);
    if (!hasRelation && !hasType && !hasFrom) return null;
    const relationId = relationDescriptor && "value" in relationDescriptor ? relationDescriptor.value : undefined;
    const inviteType = typeDescriptor && "value" in typeDescriptor ? typeDescriptor.value : undefined;
    const rawFromUid = fromDescriptor && "value" in fromDescriptor ? fromDescriptor.value : undefined;
    const hasUsableNativeUid = typeof rawFromUid === "string" && rawFromUid.length > 0;
    const fallbackEligible = !fromDescriptor || ("value" in fromDescriptor &&
      (rawFromUid === undefined || rawFromUid === null || rawFromUid === ""));
    let fromUid = hasUsableNativeUid ? rawFromUid : null;
    let identityCorrelation = hasUsableNativeUid ? "kernel_from_uid_matched" : null;
    if (!identityCorrelation && fallbackEligible && typeof relationId === "string" && CANONICAL_UIN.test(relationId) &&
        privateKernelPeerUidByUin && Object.prototype.hasOwnProperty.call(privateKernelPeerUidByUin, relationId)) {
      fromUid = privateKernelPeerUidByUin[relationId];
      identityCorrelation = "kernel_relation_uid_matched";
    }
    let fingerprint;
    try { fingerprint = JSON.stringify([relationId ?? null, inviteType ?? null, fromUid ?? null]); }
    catch { return null; }
    return {
      fingerprint,
      relationId,
      inviteType,
      fromUid,
      identityCorrelation,
    };
  }

  function onKernelInvite(event, nativeInviteEvidence = null) {
    if (!attached || stopping || stopped || providerInvalidated || !validEventGeneration(event)) return;
    const metadata = inviteMetadata(event.args);
    if (!metadata) {
      unmatchedCallbackCount += 1;
      lastFailureCode = "invite_identity_unavailable";
      emitStatus("invite_unmatched");
      return;
    }
    if (nativeInviteEvidence && (nativeInviteEvidence.inviteType !== 1 ||
        nativeInviteEvidence.relationId !== metadata.relationId ||
        (nativeInviteEvidence.fromUid === ""
          ? metadata.identityCorrelation !== "kernel_relation_uid_matched" ||
            !CANONICAL_UIN.test(metadata.relationId) ||
            !QQ_UID.test(metadata.fromUid || "") ||
            privateKernelPeerUidByUin?.[metadata.relationId] !== metadata.fromUid
          : metadata.identityCorrelation !== "kernel_from_uid_matched" ||
            nativeInviteEvidence.fromUid !== metadata.fromUid))) nativeInviteEvidence = null;
    metadata.nativeInviteEvidence = nativeInviteEvidence;
    if (groupDriver?.status().occupied) {
      if (nativeInviteEvidence && groupPreflightSelectionTicket?.phase === "selecting_audio") {
        groupPreflightSelectionTicket.privateInviteObserved = true;
      }
      busyInviteCount += 1;
      const busyIdentity = busyInviteIdentity(metadata);
      const groupGeneration = `group:${hostGeneration}:${connectionGeneration}:${groupDriver.status().roomGeneration}`;
      if (busyIdentity && shouldPublishBusyInvite({ generation: groupGeneration }, metadata)) {
        emitControlEvent(Object.freeze({ kind: "invite_busy", busyInvite: Object.freeze({ ...busyIdentity }) }));
      }
      emitStatus("invite_busy");
      return;
    }
    if (activeCall) {
      if (activeCall.inviteFingerprint === metadata.fingerprint) {
        duplicateKernelInviteCount += 1;
        emitStatus("invite_duplicate");
        return; // exact duplicate must not reset call state/timer
      }
      busyInviteCount += 1;
      const busyIdentity = busyInviteIdentity(metadata);
      if (busyIdentity && shouldPublishBusyInvite(activeCall, metadata)) {
        emitControlEvent(Object.freeze({
          kind: "invite_busy",
          busyInvite: Object.freeze({ ...busyIdentity }),
        }));
      }
      emitStatus("invite_busy");
      return;
    }
    nextCallGeneration += 1;
    const call = {
      callId: randomUUID(),
      generation: nextCallGeneration,
      connectionGeneration,
      hostGeneration,
      phase: "invite_observed",
      inviteFingerprint: metadata.fingerprint,
      fromUid: metadata.fromUid,
      identityCorrelation: metadata.identityCorrelation,
      inviteEventId: nativeInviteEvidence ? randomUUID() : null,
      privateScopeEvidence: nativeInviteEvidence,
      inviteParams: null,
      inviteCallbackObserved: false,
      inviteCallbackMatched: false,
      inviteCallbackMismatch: false,
      inviteCorrelation: "awaiting_20006",
      acceptState: "not_requested",
      acceptRequestId: null,
      observedDestroyReason: false,
      enterRelationFailureCode: null,
      enterRelationId: null,
      relationId: getOwnData(Array.isArray(event.args) ? event.args[0] : null, "relation_id"),
      roomEntry: null,
      endOperation: null,
      endRequestId: null,
      endState: null,
      endAckTimer: null,
      closeRequestId: null,
      closeState: null,
      closeAckTimer: null,
    };
    activeCall = call;
    clearCallTimer();
    timer = setTimeout(() => {
      if (activeCall?.generation === call.generation) finishCall("expired", "call_timeout", false);
    }, callTimeoutMs);
    timer.unref?.();
    emitStatus("invite_observed");
  }

  function onKernelEvent(op, payload) {
    if (op === "broker_status") {
      onProviderStatus(payload);
      return;
    }
    if (op !== "kernel_event" || !payload || typeof payload !== "object") return;
    eventCount += 1;
    if (!validEventGeneration(payload)) {
      emitStatus("stale_kernel_event");
      return;
    }
    let groupEventObserved = false;
    try { groupEventObserved = groupKernelObserver.observe(payload) === true; } catch { /* Invalid diagnostics never affect lifecycle. */ }
    const rawMethod = getOwnData(payload, "method");
    const method = typeof rawMethod === "string" && rawMethod.length <= 80 ? rawMethod.toLowerCase() : "";
    if (groupEventObserved && method === "ongroupaudiomemnumchange" && groupCurrentScopeProof && groupDriver?.status().occupied &&
        typeof host.clearGroupRoomAbsence === "function" && host.status()?.groupLocalQuitProofAvailable === true) {
      const args = getOwnArrayData(getOwnData(payload, "args"), 1);
      const info = args?.[0];
      const members = getOwnArrayData(getOwnData(info, "memberUins"), 0);
      const scope = groupCurrentScopeProof;
      if (getOwnData(info, "group_code") === scope.groupId && getOwnData(info, "onlineCount") === 0 && members &&
          typeof getOwnData(info, "isNativeTimeout") === "boolean" && groupContextIsCurrent(scope) &&
          positiveGeneration(groupStartRequestId) && activeCall === null) {
        if (groupKernelEndTicket?.scope !== scope) {
          groupKernelEndTicket = Object.freeze({ scope, startRequestId: groupStartRequestId,
            selfUid: trustedSelfUid, selfUin: trustedSelfUin });
        }
        groupDriver.observeRoomEnd(scope);
      }
    }
    if (method === "oninviteactiontoavsdk") onKernelInvite(payload, privateScopeEvidenceForEvent(payload));
    if (method === "onavsdkcrash" && (activeCall || groupDriver?.status().occupied)) {
      hostInvalidated = true;
      groupDriver?.stop();
      trustedSelfUid = null;
      trustedSelfUin = null;
      groupKernelObserver.stop();
      loginState = "invalidated";
      pending.clear();
      signalQueue = [];
      failCall("avsdk_crash", "failed");
    }
    if (method === "ons2cactiontoavsdk" && activeCall) {
      const endData = getOwnData(getOwnData(payload, "args"), "0");
      const destroyReason = getOwnData(endData, "destroyReason");
      const endRelationId = getOwnData(endData, "relation_id");
      const destroyedCall = correlatedKernelRoomDestroyed(payload);
      if (destroyedCall) {
        synchronizePrivateKernelRoomEnd(destroyedCall);
      } else if (activeCall.endOperation !== null) {
        const pushRoomInfoList = getOwnData(endData, "pushRoomInfoList");
        if (destroyReason !== undefined || pushRoomInfoList !== undefined) {
          lastFailureCode = "kernel_room_destroy_unmatched";
          emitStatus("kernel_room_destroy_unmatched");
        }
      } else {
        const pushRoomInfoList = getOwnData(endData, "pushRoomInfoList");
        if (destroyReason !== undefined || pushRoomInfoList !== undefined) {
          lastFailureCode = "kernel_room_destroy_unmatched";
          emitStatus("kernel_room_destroy_unmatched");
        }
        if (typeof destroyReason === "number" && Number.isFinite(destroyReason)) {
          if (activeCall.relationId !== undefined && endRelationId === activeCall.relationId) {
            activeCall.observedDestroyReason = true;
            // The Linux-derived destroyReason is only observed on Windows. Keep the
            // call slot uncertain until a verified end signal or fresh host generation.
            finishCall("end_observed", "destroy_reason_observed_linux_assumption", false);
          } else {
            lastFailureCode = "unmatched_destroy_reason";
            emitStatus("destroy_reason_unmatched");
          }
        }
      }
    }
    const action = getOwnData(payload, "action");
    const actionValue = getOwnArrayData(getOwnData(action, "value"), 2);
    if (getOwnData(action, "command") === 55 && actionValue) {
      enqueueKernelSignal(actionValue);
    }
  }

  function enqueueKernelSignal(value) {
    signalCount += 1;
    if (!attached || providerInvalidated || hostInvalidated || stopping || stopped) return;
    const actionType = value[0];
    const actionPayload = value[1];
    if (typeof actionType !== "number" || !Number.isFinite(actionType) || typeof actionPayload !== "string") {
      lastFailureCode = "invalid_kernel_signal";
      emitStatus("signal_rejected");
      return;
    }
    if (signalQueue.length >= maxSignalQueue) {
      lastFailureCode = "kernel_signal_queue_full";
      emitStatus("signal_queue_full");
      return;
    }
    signalQueue.push([actionType, actionPayload]);
    emitStatus("signal_queued");
    void flushSignals();
  }

  async function postSignal(params) {
    if (loginState !== "ready" || hostInvalidated || providerInvalidated) return;
    const id = allocateRequestId();
    try {
      const receipt = await host.invoke({ command: 55, id, params, hostGeneration });
      if (receipt?.posted !== true || receipt.id !== id || receipt.command !== 55) throw codedError("host_post_receipt_invalid");
    } catch (error) {
      lastFailureCode = safeCode(error, "signal_post_failed");
      emitStatus("signal_post_failed");
    }
  }

  async function flushSignals() {
    if (drainingSignals || loginState !== "ready" || hostInvalidated || providerInvalidated) return;
    drainingSignals = true;
    try {
      while (signalQueue.length && loginState === "ready" && !hostInvalidated && !providerInvalidated && !stopping) {
        const next = signalQueue.shift();
        await postSignal(next);
        if (hostInvalidated || providerInvalidated) signalQueue = [];
      }
    } finally {
      drainingSignals = false;
    }
  }

  function outputEnvelope(event) {
    if (!event || typeof event !== "object") return null;
    const output = event.output && typeof event.output === "object" ? event.output : event;
    return {
      command: output.command,
      id: output.id,
      value: output.value,
      hostGeneration: output.hostGeneration,
      hasValue: Object.prototype.hasOwnProperty.call(output, "value"),
    };
  }

  function serializePreAttachOutput(output) {
    if (!output.hasValue || !Number.isSafeInteger(output.command) || output.command < 0 ||
        !Number.isSafeInteger(output.id) || output.id < 0 || output.hostGeneration !== hostGeneration ||
        !Number.isSafeInteger(outputCount) || outputCount < 1 || outputCount > 0x7fffffff) {
      return { error: "invalid_host_output" };
    }
    let serialized;
    let bytes;
    let record;
    try {
      serialized = JSON.stringify({
        command: output.command,
        id: output.id,
        value: output.value,
        hostGeneration,
        connectionGeneration,
        sequence: outputCount,
        receivedBeforeAttach: true,
      });
      if (typeof serialized !== "string") return { error: "invalid_host_output" };
      bytes = Buffer.byteLength(serialized, "utf8");
      record = JSON.parse(serialized);
    } catch {
      return { error: "invalid_host_output" };
    }
    if (!Object.prototype.hasOwnProperty.call(record, "value")) return { error: "invalid_host_output" };
    if (preAttachOutputCount >= maxForwardQueue || preAttachOutputBytes + bytes > maxForwardQueueBytes) {
      return { error: "pre_attach_output_overflow" };
    }
    preAttachOutputCount += 1;
    preAttachOutputBytes += bytes;
    return { record, bytes };
  }

  function latchFirstRejectedOutput(output, reasonCode) {
    if (firstRejectedOutput !== null) return;
    if (!SAFE_OUTPUT_REJECTION_CODES.has(reasonCode)) return;
    if (!Number.isSafeInteger(output.command) || output.command < 0 || output.command > 0x7fffffff ||
        !Number.isSafeInteger(output.id) || output.id < 0 || output.id > 0x7fffffff ||
        !Number.isSafeInteger(output.hostGeneration) || output.hostGeneration < 1 || output.hostGeneration > MAX_DIAGNOSTIC_INTEGER ||
        !Number.isSafeInteger(connectionGeneration) || connectionGeneration < 1 || connectionGeneration > MAX_DIAGNOSTIC_INTEGER ||
        !Number.isSafeInteger(outputCount) || outputCount < 1 || outputCount > MAX_DIAGNOSTIC_INTEGER) return;
    firstRejectedOutput = Object.freeze({
      command: output.command,
      id: output.id,
      hostGeneration: output.hostGeneration,
      connectionGeneration,
      sequence: outputCount,
      reasonCode,
    });
  }

  function correlatedZeroIdRoomEntry(output) {
    if (!audioDeviceTarget || output.command !== 20004 || output.id !== 0 ||
        output.hostGeneration !== hostGeneration || !attached || !hostAvailable || hostInvalidated ||
        providerInvalidated || controllerInvalidated || closing || stopping || stopped) return null;

    const call = activeCall;
    if (!call || call.uncertain || call.connectionGeneration !== connectionGeneration ||
        call.hostGeneration !== hostGeneration || call.generation !== activeCall?.generation ||
        !call.inviteParams || call.inviteCallbackMismatch || !call.inviteCallbackMatched ||
        !VERIFIED_INVITE_CORRELATIONS.has(call.inviteCorrelation) ||
        !["accept_posting", "accept_acknowledged"].includes(call.phase) ||
        !["posting", "posted", "acknowledged"].includes(call.acceptState) ||
        !Number.isSafeInteger(call.acceptRequestId) || call.acceptRequestId <= 0) return null;

    const id = call.acceptRequestId;
    const value = output.value;
    if (!Array.isArray(value) || value.length !== 7) return null;
    const [result, scene, relationId, peerUid, isCreator, errorMessage, sentinel] = value;
    if (!validInt32(result) || !validInt32(scene) ||
        !validNativeRelationId(relationId) ||
        typeof peerUid !== "string" || peerUid.length === 0 ||
        (isCreator !== 0 && isCreator !== 1) || typeof errorMessage !== "string" || sentinel !== 1) return null;

    // This 20004 relation_id is assigned during entry and differs from the
    // invite relation in 20006. Bind it to the current pending accept by scene
    // and the already matched peer identity.
    if (scene !== call.inviteParams[0] ||
        peerUid !== call.fromUid || peerUid !== call.inviteParams[1]) return null;

    if (call.enterRelationId !== null) {
      if (call.enterRelationId === relationId && Array.isArray(call.roomEntry) && call.roomEntry[0] === result) {
        return { call, id, result, enterRelationId: relationId, duplicate: true };
      }
      return null;
    }

    const roomPending = pending.get(pendingKey(20004, id));
    if (!roomPending || roomPending.command !== 20004 || roomPending.requestId !== id ||
        roomPending.hostGeneration !== hostGeneration || roomPending.callGeneration !== call.generation) return null;

    const acceptPending = pending.get(pendingKey(5, id));
    if (call.acceptState === "acknowledged") {
      if (acceptPending !== undefined) return null;
    } else if (!acceptPending || acceptPending.command !== 5 || acceptPending.hostGeneration !== hostGeneration ||
        acceptPending.callGeneration !== call.generation) {
      return null;
    }
    if (enterRelationHistory.has(relationId)) {
      return { call, id, failureCode: "enter_relation_reused_requires_new_host" };
    }
    if (enterRelationHistory.size >= MAX_ENTER_RELATION_HISTORY) {
      return { call, id, failureCode: "enter_relation_history_full_requires_new_host" };
    }

    return { call, id, result, enterRelationId: relationId };
  }

  function correlatedRoomDestroyed(output) {
    if (!audioDeviceTarget || output.command !== 20011 || output.id !== 0 ||
        output.hostGeneration !== hostGeneration || !attached || !hostAvailable || hostInvalidated ||
        providerInvalidated || controllerInvalidated || closing || stopping || stopped) return null;

    const call = activeCall;
    const hangupInFlight = call?.endOperation === "hangup" &&
      ["ending", "ending_unconfirmed"].includes(call.phase) && Number.isSafeInteger(call.endRequestId) && call.endRequestId > 0;
    const kernelReadbackInFlight = call?.kernelRoomEndReadbackStarted === true &&
      ["ending_kernel_verified", "ending_unconfirmed"].includes(call.phase);
    const terminalInFlight = hangupInFlight || kernelReadbackInFlight;
    if (!call || (call.uncertain && !terminalInFlight) || call.roomDestroyedConfirmed ||
        call.connectionGeneration !== connectionGeneration || call.hostGeneration !== hostGeneration ||
        call.generation !== activeCall?.generation || !call.inviteParams ||
        call.inviteCallbackMismatch || !call.inviteCallbackMatched ||
        !VERIFIED_INVITE_CORRELATIONS.has(call.inviteCorrelation) ||
        typeof call.enterRelationId !== "string" || !validNativeRelationId(call.enterRelationId) ||
        !enterRelationHistory.has(call.enterRelationId) ||
        typeof call.fromUid !== "string" || call.fromUid.length === 0 ||
        !(terminalInFlight
          ? call.acceptState === "connected"
          : ["invite_ready", "accept_posting", "accept_acknowledged", "connected"].includes(call.phase) &&
            ["not_requested", "selecting_devices", "posting", "posted", "acknowledged", "connected"].includes(call.acceptState))) return null;

    if (terminalInFlight && (call.inviteParams[0] !== PRIVATE_CALL_SCENE || call.fromUid !== call.inviteParams[1] ||
        !retirePrivateScopeEvidenceIfStale(call))) return null;

    const value = output.value;
    if (!Array.isArray(value) || value.length !== 3) return null;
    const [scene, relationId, roomId] = value;
    // Validate room_id only as opaque protocol data. Cross-callback relation
    // equality is not statically proven, so require an exact match to the
    // current 20004 relation and ignore mismatches without claiming hangup.
    if (!validInt32(scene) || !validNativeRelationId(relationId) ||
        typeof roomId !== "string" || roomId.length > 512 ||
        scene !== call.inviteParams[0] || relationId !== call.enterRelationId) return null;

    return call;
  }

  function correlatedKernelRoomDestroyed(event) {
    if (!audioDeviceTarget || !attached || !hostAvailable || hostInvalidated ||
        providerInvalidated || controllerInvalidated || closing || stopping || stopped ||
        !validEventGeneration(event)) return null;
    try {
      if (provider.status()?.connected !== true) return null;
    } catch {
      return null;
    }

    const args = getOwnArrayData(getOwnData(event, "args"), 2);
    if (!args) return null;
    const endData = args[0];
    if (!endData || typeof endData !== "object" || Array.isArray(endData) ||
        getOwnData(endData, "destroyReason") !== 1) return null;

    const roomList = getOwnArrayData(getOwnData(endData, "pushRoomInfoList"), 1);
    if (!roomList) return null;
    const roomInfo = roomList[0];
    if (!roomInfo || typeof roomInfo !== "object" || Array.isArray(roomInfo)) return null;

    const roomId = getOwnData(roomInfo, "roomId");
    const peerUid = getOwnData(roomInfo, "peerUid");
    const peerUin = getOwnData(roomInfo, "peerUin");
    // Require the complete observed Windows payload shape. These ancillary
    // fields are literal guards from the captured event, not interpreted
    // protocol enums; unknown shapes stay diagnostic and cannot end a call.
    if (getOwnData(roomInfo, "termAction") !== 0 ||
        getOwnData(roomInfo, "termType") !== 0 ||
        getOwnData(roomInfo, "bussinessType") !== 0 ||
        typeof peerUin !== "string" || peerUin.length !== 1) return null;

    const call = activeCall;
    const hangupInFlight = call?.endOperation === "hangup" &&
      ["ending", "ending_unconfirmed"].includes(call.phase) && Number.isSafeInteger(call.endRequestId) && call.endRequestId > 0;
    const kernelReadbackInFlight = call?.kernelRoomEndReadbackStarted === true &&
      ["ending_kernel_verified", "ending_unconfirmed"].includes(call.phase);
    const terminalInFlight = hangupInFlight || kernelReadbackInFlight;
    if (!call || (call.uncertain && !terminalInFlight) || call.roomDestroyedConfirmed ||
        call.connectionGeneration !== connectionGeneration || call.hostGeneration !== hostGeneration ||
        call.generation !== activeCall?.generation || !call.inviteParams ||
        call.inviteCallbackMismatch || !call.inviteCallbackMatched ||
        !VERIFIED_INVITE_CORRELATIONS.has(call.inviteCorrelation) ||
        typeof call.enterRelationId !== "string" || !validNativeRelationId(call.enterRelationId) ||
        !enterRelationHistory.has(call.enterRelationId) ||
        typeof call.fromUid !== "string" || call.fromUid.length === 0 ||
        call.fromUid !== call.inviteParams[1] ||
        !(call.phase === "connected" || terminalInFlight) || call.acceptState !== "connected") return null;

    if (terminalInFlight && (call.inviteParams[0] !== PRIVATE_CALL_SCENE || call.fromUid !== call.inviteParams[1] ||
        !retirePrivateScopeEvidenceIfStale(call))) return null;

    // Correlate only the current typed 20004 entry and its already verified
    // caller. Kernel relation_id values are not a substitute identity.
    if (typeof roomId !== "string" || roomId.length === 0 || roomId.length > 512 ||
        roomId !== call.enterRelationId ||
        typeof peerUid !== "string" || peerUid.length === 0 || peerUid !== call.fromUid) return null;

    return call;
  }

  function invalidateForEnterRelation(call, code) {
    if (activeCall !== call) return;
    call.uncertain = true;
    call.phase = "invite_uncertain";
    call.enterRelationFailureCode = code;
    hostInvalidated = true;
    groupKernelObserver.stop();
    loginState = "invalidated";
    loginRequestId = null;
    pending.clear();
    signalQueue = [];
    lastFailureCode = code;
    emitStatus("controller_invalidated");
  }

  function onHostEvent(event) {
    if (!event || typeof event !== "object") return;
    if (event.hostGeneration !== undefined && event.hostGeneration !== hostGeneration) {
      lastFailureCode = "stale_host_generation";
      if (event.type === "output") {
        const staleOutput = outputEnvelope(event);
        outputCount += 1;
        if (staleOutput) latchFirstRejectedOutput(staleOutput, "stale_host_generation");
      }
      emitStatus("stale_host_output");
      return event.type === "output" ? Promise.reject(codedError("stale_host_generation")) : undefined;
    }
    if (event.type === "availability") {
      onHostAvailability(event.available);
      return;
    }
    if (closing || stopping || stopped || hostInvalidated || providerInvalidated || controllerInvalidated) {
      lastFailureCode = closing || stopping || stopped ? "bridge_closing" :
        providerInvalidated ? "provider_generation_invalidated" : "host_generation_invalidated";
      const rejectedOutput = event.type === "output" ? outputEnvelope(event) : null;
      if (rejectedOutput) {
        outputCount += 1;
        latchFirstRejectedOutput(rejectedOutput, lastFailureCode);
      }
      emitStatus("host_output_ignored");
      return event.type === "output" ? Promise.reject(codedError(lastFailureCode)) : undefined;
    }
    if (event.type !== "output") return;
    const output = outputEnvelope(event);
    if (!providerUnsubscribe || !output) {
      lastFailureCode = !providerUnsubscribe ? "kernel_listener_not_attached" : "invalid_host_output";
      emitStatus("output_rejected");
      return Promise.reject(codedError(lastFailureCode));
    }
    outputCount += 1;
    if (output.hostGeneration !== hostGeneration) {
      lastFailureCode = "stale_host_generation";
      latchFirstRejectedOutput(output, lastFailureCode);
      invalidateController(lastFailureCode);
      emitStatus("stale_host_output");
      return Promise.reject(codedError(lastFailureCode));
    }
    if (!attached) {
      const stored = serializePreAttachOutput(output);
      if (stored.error) {
        latchFirstRejectedOutput(output, stored.error);
        lastFailureCode = stored.error;
        emitStatus("output_rejected");
        invalidateController(stored.error);
        return Promise.reject(codedError(stored.error));
      }
      if (output.command === 20001) return enqueue20001(stored.record.value, stored.record);
      preAttachOutputQueue.push(stored.record);
      return Promise.resolve({
        receivedStored: true,
        sequence: stored.record.sequence,
        receivedBeforeAttach: true,
      });
    }
    // 20001 is independent of login ACK state. Do not queue it behind login or
    // signaling work; this avoids a Host callback/login ACK cycle deadlock.
    if (output.command === 20001) {
      return enqueue20001(output.value, {
        hostGeneration: output.hostGeneration,
        connectionGeneration,
        sequence: outputCount,
        receivedBeforeAttach: false,
      });
    }
    const task = hostOutputTail.then(() => processHostOutput(output));
    hostOutputTail = task.catch(() => {});
    return task;
  }

  async function processHostOutput(output) {
    const groupOccupied = Boolean(groupDriver?.status().occupied);
    if (groupDriver && (output.command === 20004 || output.command === 20011)) {
      // Group SDK callbacks are unsolicited Host frames and use id=0. Keep
      // the Host envelope correlation gate here: the pure driver intentionally
      // receives only a projected frame and cannot distinguish a positive-id
      // command reply that happens to contain the same tuple.
      const consumed = output.id === 0 && groupDriver.onOutput({
        command: output.command,
        hostGeneration: output.hostGeneration,
        value: output.value,
      });
      if (consumed) {
        if (output.command === 20011) groupLeaseSuspended = true;
        emitStatus("group_output_observed");
        return;
      }
      if (groupOccupied) {
        emitStatus("group_output_ignored");
        return;
      }
    }
    if (output.command === 20006 && groupOccupied) {
      unmatchedCallbackCount += 1;
      emitStatus("private_invite_callback_ignored_group_busy");
      return;
    }
    if ((output.command === 4 || output.command === 8) && groupCommandOutputIds.has(`${output.command}:${output.id}`)) {
      if (output.command === 8) groupDriver?.onOutput({
        command: output.command, id: output.id, hostGeneration: output.hostGeneration, value: output.value,
      });
      emitStatus("group_command_ack_observed");
      return;
    }
    if (output.command === 20006) {
      if (Array.isArray(output.value) && output.value[0] === 3) {
        const invite = output.value;
        groupInvitationObservation = {
          kernelInvitePresent: Boolean(activeCall),
          kernelCallerPresent: Boolean(activeCall?.fromUid),
          callerCorrelated: Boolean(activeCall && (!activeCall.fromUid || activeCall.fromUid === invite[1])),
          groupCorrelated: Boolean(activeCall && String(activeCall.relationId) === invite[4]),
          addressedToSelf: Array.isArray(invite[2]) && invite[2].some((uid) => uid === trustedSelfUid || uid === trustedSelfUin),
          ticketPublished: false,
        };
      }
      if (!activeCall) {
        unmatchedCallbackCount += 1;
        lastFailureCode = "invite_callback_without_kernel_invite";
        emitStatus("invite_callback_unmatched");
        return;
      }
      activeCall.inviteCallbackObserved = true;
      if (activeCall.enterRelationFailureCode) {
        unmatchedCallbackCount += 1;
        emitStatus("invite_callback_unmatched");
        return;
      }
      const invite = output.value;
      if (!Array.isArray(invite) || invite.length < 12) {
        activeCall.inviteCallbackMismatch = true;
        unmatchedCallbackCount += 1;
        lastFailureCode = "invalid_invite_callback";
        emitStatus("invite_callback_unmatched");
        return;
      }
      if (activeCall.inviteParams) {
        duplicateInviteCallbackCount += 1;
        emitStatus("invite_callback_duplicate");
        return;
      }
      if (audioDeviceTarget && invite.length === 12 && invite[0] === 3 &&
          typeof invite[1] === "string" && QQ_UID.test(invite[1]) &&
          validInt32(invite[3]) && validGroupId(invite[4]) && typeof invite[11] === "string" &&
          [true, false, 0, 1].includes(invite[10]) &&
          validGroupId(String(activeCall.relationId)) && String(activeCall.relationId) === invite[4] &&
          (!activeCall.fromUid || activeCall.fromUid === invite[1]) &&
          activeCall.acceptState === "not_requested" && activeCall.acceptRequestId === null &&
          activeCall.endOperation === null && activeCall.enterRelationId === null) {
        // A correlated scene-3 callback classifies only an unaccepted invite.
        // It grants no room membership or audio scope, even after timeout.
        clearCallTimer();
        invalidatePendingForCall(activeCall.generation);
        // Only a native scene-3 invitation explicitly addressed to this login
        // may trigger adapter admission. Member-count notifications alone do not.
        const targets = invite[2];
        let publishedInvitation = null;
        if (invite[3] === 1 &&
            Array.isArray(targets) && targets.length >= 1 && targets.length <= 1000 &&
            targets.every((uid) => typeof uid === "string" && (QQ_UID.test(uid) || validGroupId(uid))) &&
            targets.some((uid) => uid === trustedSelfUid || uid === trustedSelfUin)) {
          const fingerprint = JSON.stringify([invite[1], targets, invite[3], invite[4], invite[11]]);
          if (!seenAutoGroupInvites.has(fingerprint)) {
            seenAutoGroupInvites.add(fingerprint);
            while (seenAutoGroupInvites.size > 256) seenAutoGroupInvites.delete(seenAutoGroupInvites.values().next().value);
            pendingAutoGroupInvite = Object.freeze({
              inviteId: randomUUID(), groupId: invite[4], callerUid: invite[1],
              observedAtMs: now(), selfUid: trustedSelfUid, selfUin: trustedSelfUin,
            });
            publishedInvitation = pendingAutoGroupInvite;
            groupInvitationObservation.ticketPublished = true;
          }
        }
        activeCall = null;
        resetBusyInviteDedupe();
        emitStatus("group_invite_classified");
        if (publishedInvitation) {
          const { inviteId, groupId, callerUid, observedAtMs } = publishedInvitation;
          emitControlEvent(Object.freeze({ kind: "group_invite", groupInvite: Object.freeze({ inviteId, groupId, callerUid, observedAtMs }) }));
        }
        return;
      }
      if (audioDeviceTarget && invite[0] === 3) {
        unmatchedCallbackCount += 1;
        emitStatus("group_invite_uncorrelated");
        return;
      }
      if (activeCall.phase !== "invite_observed") {
        activeCall.inviteCallbackMismatch = true;
        unmatchedCallbackCount += 1;
        lastFailureCode = "invite_callback_after_call_expired";
        emitStatus("invite_callback_unmatched");
        return;
      }
      if (activeCall.fromUid && typeof invite[1] === "string" && invite[1] !== activeCall.fromUid) {
        activeCall.inviteCallbackMismatch = true;
        unmatchedCallbackCount += 1;
        lastFailureCode = "invite_caller_mismatch";
        emitStatus("invite_callback_unmatched");
        return;
      }
      if (activeCall.fromUid && typeof invite[1] !== "string") {
        activeCall.inviteCallbackMismatch = true;
        unmatchedCallbackCount += 1;
        lastFailureCode = "invite_caller_unavailable";
        emitStatus("invite_callback_unmatched");
        return;
      }
      let params;
      try {
        params = [invite[0], invite[1], [invite[1]], invite[3], invite[4], Boolean(invite[10]), invite[11]];
        if (typeof params[0] !== "number" || !Number.isFinite(params[0]) || typeof params[1] !== "string" || !params[1] ||
            !Array.isArray(params[2]) || typeof params[3] !== "number" || !Number.isFinite(params[3]) ||
            typeof params[4] !== "string" || typeof params[6] !== "string") throw codedError("invalid_invite_callback");
        if (audioDeviceTarget && (!validInt32(params[0]) || !validNativeRelationId(params[4]))) {
          throw codedError("invalid_invite_callback");
        }
        if (audioDeviceTarget && (!activeCall.fromUid || activeCall.fromUid !== invite[1])) {
          throw codedError("invite_identity_unavailable");
        }
      } catch (error) {
        activeCall.inviteCallbackMismatch = true;
        unmatchedCallbackCount += 1;
        lastFailureCode = safeCode(error, "invalid_invite_callback");
        emitStatus("invite_callback_unmatched");
        return;
      }

      const callbackMatched = Boolean(activeCall.fromUid && activeCall.fromUid === invite[1]);
      activeCall.inviteParams = params;
      activeCall.inviteCallbackMatched = callbackMatched;
      activeCall.inviteCorrelation = callbackMatched
        ? activeCall.identityCorrelation
        : "single_active_invite_uncorrelated";
      activeCall.phase = "invite_ready";
      lastFailureCode = null;
      emitStatus("invite_callback_ready");
      return;
    }

    if (output.command === 20004 && output.id === 0) {
      const correlated = correlatedZeroIdRoomEntry(output);
      if (!correlated) {
        uncorrelatedAckCount += 1;
        emitStatus("ack_uncorrelated");
        return;
      }
      if (correlated.failureCode) {
        invalidateForEnterRelation(correlated.call, correlated.failureCode);
        return;
      }
      if (correlated.duplicate) return;
      // Retain only the result code. The native relation, peer, and message
      // fields are used for correlation and never enter status or logs.
      pending.delete(pendingKey(20004, correlated.id));
      correlated.call.roomEntry = [correlated.result];
      correlated.call.enterRelationId = correlated.enterRelationId;
      enterRelationHistory.add(correlated.enterRelationId);
      if (correlated.call.acceptState === "acknowledged") {
        applyRoomEntry(correlated.call, correlated.call.roomEntry);
      } else {
        emitStatus("room_entry_observed");
      }
      return;
    }

    if (audioDeviceTarget && output.command === 20011 && output.id === 0) {
      const destroyedCall = correlatedRoomDestroyed(output);
      if (!destroyedCall) {
        unmatchedCallbackCount += 1;
        emitStatus("room_destroy_unmatched");
        return;
      }
      finishCall("ended", "native_room_destroyed", false, true);
      return;
    }

    if (PRIVATE_END_COMMANDS.has(output.command)) {
      if (!Number.isSafeInteger(output.id) || output.id <= 0) {
        uncorrelatedAckCount += 1;
        emitStatus("ack_uncorrelated");
        return;
      }
      const key = pendingKey(output.command, output.id);
      const entry = pending.get(key);
      const call = activeCall;
      if (output.command === QRTC_COMMAND_CLOSE) {
        if (!entry || entry.command !== QRTC_COMMAND_CLOSE || entry.stage !== "close" ||
            entry.hostGeneration !== output.hostGeneration || entry.callGeneration !== call?.generation ||
            entry.callId !== call?.callId || entry.connectionGeneration !== connectionGeneration ||
            entry.privateScopeEvidence !== call?.privateScopeEvidence || entry.peerUid !== call?.fromUid ||
            call?.inviteParams?.[1] !== entry?.peerUid || entry.operation !== "hangup" ||
            entry.quitRequestId !== call?.endRequestId || entry.dispatchStarted !== true ||
            call.closeRequestId !== output.id || call.endOperation !== "hangup" ||
            !["posting", "posted"].includes(call.closeState) || !retirePrivateScopeEvidenceIfStale(call)) {
          uncorrelatedAckCount += 1;
          emitStatus("ack_uncorrelated");
          return;
        }
        pending.delete(key);
        clearPrivateCloseAckTimer(call);
        const value = output.value;
        const validResultTuple = Array.isArray(value) && value.length === 2 &&
          validInt32(value[0]) && typeof value[1] === "string";
        if (!validResultTuple || (value[0] === 0 && value[1] !== "")) {
          markPrivateCloseUnconfirmed(call, "ack_invalid", "hangup_close_ack_invalid");
        } else if (value[0] !== 0) {
          markPrivateCloseUnconfirmed(call, "failed", "hangup_close_failed");
        } else {
          call.closeState = "acknowledged";
          call.endState = "hangup_close_acknowledged";
          call.phase = "ending";
          lastFailureCode = null;
          emitStatus("hangup_close_acknowledged");
        }
        return;
      }
      if (!entry || entry.command !== output.command || entry.hostGeneration !== output.hostGeneration ||
          entry.callGeneration !== call?.generation || entry.callId !== call?.callId ||
          entry.connectionGeneration !== connectionGeneration || entry.privateScopeEvidence !== call?.privateScopeEvidence ||
          entry.peerUid !== call?.fromUid || call?.inviteParams?.[1] !== entry?.peerUid ||
          entry.operation !== (output.command === QRTC_COMMAND_QUIT ? "hangup" : "reject") ||
          entry.dispatchStarted !== true ||
          call.endRequestId !== output.id || call.endOperation !== entry.operation ||
          !["posting", "posted"].includes(call.endState) || !retirePrivateScopeEvidenceIfStale(call)) {
        uncorrelatedAckCount += 1;
        emitStatus("ack_uncorrelated");
        return;
      }
      pending.delete(key);
      clearPrivateEndAckTimer(call);
      const operation = entry.operation;
      const value = output.value;
      const validResultTuple = Array.isArray(value) && value.length === 2 &&
        validInt32(value[0]) && typeof value[1] === "string";
      if (!validResultTuple || (value[0] === 0 && value[1] !== "")) {
        call.endState = `${operation}_ack_invalid_unconfirmed`;
        call.phase = operation === "hangup" ? "ending_unconfirmed" : "reject_unconfirmed";
        call.uncertain = true;
        lastFailureCode = "private_end_ack_invalid";
        emitStatus(`${operation}_ack_invalid`);
      } else if (value[0] !== 0) {
        call.endState = `${operation}_failed_unconfirmed`;
        call.phase = operation === "hangup" ? "ending_unconfirmed" : "reject_unconfirmed";
        call.uncertain = true;
        lastFailureCode = `native_${operation}_failed`;
        emitStatus(`native_${operation}_failed`);
      } else if (output.command === QRTC_COMMAND_QUIT) {
        call.endState = "hangup_acknowledged";
        call.phase = "ending";
        lastFailureCode = null;
        reserveAndDispatchPrivateClose(call, entry);
      } else {
        call.endState = "reject_acknowledged_unconfirmed";
        call.phase = "reject_unconfirmed";
        call.uncertain = true;
        lastFailureCode = "reject_terminal_unconfirmed";
        emitStatus("reject_acknowledged_unconfirmed");
        confirmPrivateRejectReadback(call, entry);
      }
      return;
    }

    if (!ACK_COMMANDS.has(output.command)) return;
    if (audioDeviceTarget && output.command === 20004) {
      // In audio mode, only the typed id=0 result establishes an
      // enterRelationId. A generic positive-id ACK cannot connect the call.
      uncorrelatedAckCount += 1;
      emitStatus("ack_uncorrelated");
      return;
    }
    if (!Number.isSafeInteger(output.id) || output.id <= 0) {
      uncorrelatedAckCount += 1;
      emitStatus("ack_uncorrelated");
      return;
    }
    const key = pendingKey(output.command, output.id);
    const entry = pending.get(key);
    if (!entry || entry.command !== output.command || entry.hostGeneration !== output.hostGeneration ||
        (entry.callGeneration !== null && entry.callGeneration !== activeCall?.generation)) {
      uncorrelatedAckCount += 1;
      emitStatus("ack_uncorrelated");
      return;
    }
    if (output.command === 1) {
      if (output.value?.[0] === 0 && output.value?.[1] === "") {
        pending.delete(key);
        trustedSelfUid = typeof entry.selfUid === "string" ? entry.selfUid : null;
        trustedSelfUin = typeof entry.selfUin === "string" ? entry.selfUin : null;
        loginState = "ready";
        loginRequestId = null;
        lastFailureCode = null;
        emitStatus("login_ready");
        void flushSignals();
      } else {
        pending.delete(key);
        trustedSelfUid = null;
        trustedSelfUin = null;
        loginState = "failed";
        loginRequestId = null;
        lastFailureCode = "native_login_failed";
        signalQueue = [];
        emitStatus("login_failed");
      }
      return;
    }
    if (output.command === 5) {
      if (Array.isArray(output.value) && output.value[0] === 0) {
        activeCall.acceptState = "acknowledged";
        if (activeCall.phase !== "connected") activeCall.phase = "accept_acknowledged";
        pending.delete(key);
        lastFailureCode = null;
        emitStatus("accept_acknowledged");
        if (activeCall.roomEntry) applyRoomEntry(activeCall, activeCall.roomEntry);
      } else {
        pending.delete(key);
        lastFailureCode = "native_accept_failed";
        finishCall("failed", "native_accept_failed", false);
      }
      return;
    }
    // A 20004 success is meaningful only for the exact pending accept and call.
    pending.delete(key);
    activeCall.roomEntry = output.value;
    if (activeCall.acceptState === "acknowledged") applyRoomEntry(activeCall, output.value);
  }

  function applyRoomEntry(call, value) {
    if (activeCall?.generation !== call.generation || call.acceptState !== "acknowledged") return;
    if (audioDeviceTarget && (typeof call.enterRelationId !== "string" || !enterRelationHistory.has(call.enterRelationId))) return;
    if (Array.isArray(value) && value[0] === 0) {
      call.phase = "connected";
      call.acceptState = "connected";
      call.roomEntry = null;
      clearCallTimer();
      lastFailureCode = null;
      emitStatus("call_connected");
    } else {
      lastFailureCode = "room_entry_failed";
      finishCall("failed", "room_entry_failed", false);
    }
  }

  function subscribe() {
    if (!providerUnsubscribe) providerUnsubscribe = provider.subscribe(onKernelEvent);
    if (!hostUnsubscribe) hostUnsubscribe = host.subscribe(onHostEvent);
  }

  function prepareSubscriptions() {
    if (stopping || closing || stopped) throw codedError("bridge_closing");
    subscribe();
    return { prepared: true, status: status() };
  }

  async function attachWork() {
    let attempted = false;
    if (stopping || closing || stopped) throw codedError("bridge_closing");
    if (attached) return { attached: true, status: status() };
    if (attachUncertain) throw codedError("listener_attach_state_uncertain");
    if (providerInvalidated || hostInvalidated) throw codedError("bridge_generation_invalidated");
    prepareSubscriptions();
    try {
      const initialHostStatus = typeof host.status === "function" ? host.status() : null;
      if (initialHostStatus && Object.prototype.hasOwnProperty.call(initialHostStatus, "available")) {
        onHostAvailability(initialHostStatus.available);
      }
      attempted = true;
      const reply = unwrapResult(await provider.attach());
      const listenerRegistered = reply?.status?.listenerRegistered;
      if (reply?.ok !== true || listenerRegistered !== true) throw codedError(safeCode(reply, "listener_attach_unconfirmed"));
      attached = true;
      attachUncertain = false;
      if (stopping) throw codedError("bridge_stopping");
      onProviderStatus(provider.status());
      consumePreAttachOutputs();
      emitStatus("attached");
      void drain20001();
      return { attached: true, status: status() };
    } catch (error) {
      // The broker rejects a failed attach and does not return a fresh listener
      // state. Its cached status may predate a successful add whose ACK was lost.
      if (attempted && !attached) {
        attachUncertain = true;
        clearForwardQueue("listener_attach_failed");
        clearPreAttachOutputQueue();
      }
      if (!attached && !attachUncertain) {
        providerUnsubscribe?.();
        hostUnsubscribe?.();
        providerUnsubscribe = null;
        hostUnsubscribe = null;
      }
      lastFailureCode = safeCode(error, "listener_attach_failed");
      throw error;
    }
  }

  function attach() {
    if (stopping || closing || stopped) return Promise.reject(codedError("bridge_closing"));
    if (attached) return Promise.resolve({ attached: true, status: status() });
    if (attachUncertain) return Promise.reject(codedError("listener_attach_state_uncertain"));
    if (attaching) return Promise.reject(codedError("bridge_attach_in_progress"));
    attaching = true;
    let resolveAttach;
    let rejectAttach;
    attachPromise = new Promise((resolve, reject) => {
      resolveAttach = resolve;
      rejectAttach = reject;
    });
    const exposedPromise = attachPromise;
    Promise.resolve().then(attachWork).then(resolveAttach, rejectAttach).finally(() => {
      attaching = false;
      if (attachPromise === exposedPromise) attachPromise = null;
    });
    return exposedPromise;
  }

  async function login(identity) {
    canOperate();
    if (loginState === "pending") throw codedError("login_already_pending");
    if (loginState === "ready") throw codedError("already_logged_in");
    if (!hostAvailable) throw codedError("host_unavailable");
    if (!identity || typeof identity.selfUid !== "string" || !identity.selfUid ||
        typeof identity.selfUin !== "string" || !identity.selfUin ||
        typeof identity.accountPath !== "string" || !identity.accountPath) throw codedError("invalid_identity");
    const id = allocateRequestId();
    // Keep the Native default TinyId slot empty; it is fixed, not caller-configurable.
    const params = [identity.selfUid, identity.selfUin, "", identity.accountPath, ""];
    trustedSelfUid = null;
    trustedSelfUin = null;
    pending.set(pendingKey(1, id), {
      command: 1,
      hostGeneration,
      callGeneration: null,
      selfUid: identity.selfUid,
      selfUin: identity.selfUin,
    });
    loginRequestId = id;
    loginState = "pending";
    lastFailureCode = null;
    emitStatus("login_posting");
    try {
      const receipt = await host.invoke({ command: 1, id, params, hostGeneration });
      if (receipt?.posted !== true || receipt.id !== id || receipt.command !== 1) throw codedError("host_post_receipt_invalid");
      // The native ACK may arrive before this posted receipt resolves. Preserve
      // the ACK-driven state if the pending entry has already been consumed.
      if (pending.has(pendingKey(1, id))) emitStatus("login_posted");
      return { posted: true, id, loginState };
    } catch (error) {
      const wasPending = pending.delete(pendingKey(1, id));
      if (wasPending) {
        trustedSelfUid = null;
        trustedSelfUin = null;
        loginState = "failed";
        loginRequestId = null;
        lastFailureCode = safeCode(error, "login_post_failed");
        emitStatus("login_post_failed");
      }
      throw error;
    }
  }

  async function accept(callId, options = undefined) {
    let assertCurrent;
    if (options !== undefined) {
      const optionKeys = options && typeof options === "object" && !Array.isArray(options)
        ? Object.keys(options) : [];
      if (!options || typeof options !== "object" || Array.isArray(options) ||
          optionKeys.length !== 1 || optionKeys[0] !== "assertCurrent" || typeof options.assertCurrent !== "function") {
        throw codedError("invalid_accept_guard");
      }
      assertCurrent = options.assertCurrent;
    }
    const assertAcceptCurrent = () => {
      if (!assertCurrent) return;
      const result = assertCurrent();
      if (result && typeof result.then === "function") throw codedError("accept_guard_async");
      if (result === false) throw codedError("call_not_current");
    };
    assertAcceptCurrent();
    if (activeCall?.callId === callId && activeCall.enterRelationFailureCode) {
      throw codedError(activeCall.enterRelationFailureCode);
    }
    canOperate();
    if (loginState !== "ready") throw codedError("login_not_ready");
    if (!hostAvailable) throw codedError("host_unavailable");
    if (!activeCall || activeCall.callId !== callId) throw codedError("call_not_current");
    assertPrivateScopeEvidenceCurrent(activeCall);
    if (activeCall.phase !== "invite_ready") throw codedError("call_not_accepting");
    if (!activeCall.inviteParams) throw codedError("invite_callback_not_ready");
    if (audioDeviceTarget && activeCall.inviteParams[0] !== PRIVATE_CALL_SCENE) {
      throw codedError("private_scope_unverified");
    }
    if (activeCall.acceptState !== "not_requested") throw codedError("accept_already_requested");
    if (audioDeviceTarget && enterRelationHistory.size >= MAX_ENTER_RELATION_HISTORY) {
      invalidateForEnterRelation(activeCall, "enter_relation_history_full_requires_new_host");
      throw codedError("enter_relation_history_full_requires_new_host");
    }
    const acceptingCall = activeCall;
    const callGeneration = acceptingCall.generation;
    const inviteParams = acceptingCall.inviteParams;
    const inviteCorrelation = acceptingCall.inviteCorrelation;
    const inviteCallbackMismatchAtAcceptStart = acceptingCall.inviteCallbackMismatch;
    const privateScopeEvidenceAtAcceptStart = acceptingCall.privateScopeEvidence;
    if (audioDeviceTarget) {
      // Reserve before device enumeration. A second accept must not race a
      // selector flight or post cmd 5 while device state remains uncertain.
      assertAcceptCurrent();
      acceptingCall.acceptState = "selecting_devices";
      emitStatus("audio_device_selection_posting");
      try {
        const selectionId = allocateRequestId();
        const selection = await host.selectAudioDevices({
          id: selectionId,
          hostGeneration,
          inputDeviceName: audioDeviceTarget.inputDeviceName,
          outputDeviceName: audioDeviceTarget.outputDeviceName,
        });
        assertAcceptCurrent();
        if (selection?.selected !== true || selection.id !== selectionId || selection.command !== 102) {
          throw codedError("audio_device_selection_receipt_invalid");
        }
        canOperate();
        if (activeCall !== acceptingCall || activeCall.callId !== callId || activeCall.generation !== callGeneration ||
            activeCall.phase !== "invite_ready" || activeCall.acceptState !== "selecting_devices" ||
            activeCall.uncertain || activeCall.inviteParams !== inviteParams || !activeCall.inviteParams ||
            activeCall.inviteCorrelation !== inviteCorrelation || activeCall.inviteCallbackMismatch) {
          throw codedError("call_not_current");
        }
        if (!hostAvailable) throw codedError("host_unavailable");
        emitStatus("audio_device_selection_verified");
      } catch (error) {
        if (activeCall === acceptingCall && activeCall.generation === callGeneration &&
            activeCall.acceptState === "selecting_devices") {
          activeCall.acceptState = "selection_failed";
          lastFailureCode = safeCode(error, "audio_device_selection_failed");
          emitStatus("audio_device_selection_failed");
        }
        throw error;
      }
    }

    // Reserve cmd 5 synchronously before its first await; posted receipt is not success.
    assertAcceptCurrent();
    activeCall.acceptState = "posting";
    activeCall.phase = "accept_posting";
    const id = allocateRequestId();
    activeCall.acceptRequestId = id;
    pending.set(pendingKey(5, id), { command: 5, hostGeneration, callGeneration });
    pending.set(pendingKey(20004, id), { command: 20004, hostGeneration, callGeneration, requestId: id });
    emitStatus("accept_posting");
    const capturedPrivateScopeEvidence = privateScopeEvidenceAtAcceptStart;
    const assertAcceptDispatchCurrent = () => {
      // HostClient calls this synchronously when cmd 5 reaches the front of
      // its real HTTP queue. Keep the original call object and proof captured
      // here; a replacement call must never inherit this queued command.
      assertAcceptCurrent();
      canOperate();
      if (activeCall !== acceptingCall || activeCall?.callId !== callId ||
          activeCall.generation !== callGeneration || activeCall.connectionGeneration !== connectionGeneration ||
          activeCall.hostGeneration !== hostGeneration ||
          activeCall.phase !== "accept_posting" || activeCall.acceptState !== "posting" ||
          activeCall.acceptRequestId !== id || activeCall.inviteParams !== inviteParams ||
          activeCall.inviteCorrelation !== inviteCorrelation ||
          activeCall.inviteCallbackMismatch !== inviteCallbackMismatchAtAcceptStart || activeCall.uncertain ||
          !pending.has(pendingKey(5, id)) || pending.get(pendingKey(5, id))?.callGeneration !== callGeneration) {
        throw codedError("call_not_current");
      }
      if (!hostAvailable) throw codedError("host_unavailable");
      if (loginState !== "ready") throw codedError("login_not_ready");
      if (acceptingCall.privateScopeEvidenceInvalidated ||
          acceptingCall.privateScopeEvidence !== capturedPrivateScopeEvidence) {
        if (capturedPrivateScopeEvidence || acceptingCall.privateScopeEvidenceInvalidated) {
          throw codedError("private_scope_unverified");
        }
        throw codedError("call_not_current");
      }
      if (capturedPrivateScopeEvidence) assertPrivateScopeEvidenceCurrent(acceptingCall);
      return true;
    };
    try {
      // A direct Bridge caller may not supply the Control service's guard.
      // Recheck a formerly proved invite at the last synchronous boundary
      // before posting cmd 5 as well.
      assertAcceptDispatchCurrent();
      if (officialPrivateInviteLifecycle && capturedPrivateScopeEvidence) {
        // Retirement only disables old-window cleanup. Failure cannot authorize
        // any UI operation or change the existing Native accept guard.
        void Promise.resolve().then(() => officialPrivateInviteLifecycle(officialPrivateInviteRequest(acceptingCall, "accepted")))
          .catch(() => {});
      }
      const receipt = await host.invoke(
        { command: 5, id, params: inviteParams, hostGeneration },
        { assertCurrent: assertAcceptDispatchCurrent },
      );
      if (receipt?.posted !== true || receipt.id !== id || receipt.command !== 5) throw codedError("host_post_receipt_invalid");
      if (activeCall?.generation === callGeneration && activeCall.acceptState === "posting") activeCall.acceptState = "posted";
      emitStatus("accept_posted");
      return { posted: true, id, callId, callGeneration };
    } catch (error) {
      const ackStillPending = pending.delete(pendingKey(5, id));
      if (activeCall?.generation === callGeneration && ackStillPending) {
        activeCall.acceptState = "failed";
        activeCall.phase = "failed";
      }
      pending.delete(pendingKey(20004, id));
      if (ackStillPending) {
        lastFailureCode = safeCode(error, "accept_post_failed");
        emitStatus("accept_post_failed");
      }
      throw error;
    }
  }

  async function privateEnd(operation, callId) {
    const { call, capturedEvidence } = requirePrivateEndScope(callId, operation);
    if (call.endOperation !== null || call.endRequestId !== null) throw codedError("operation_rejected");
    if (operation === "hangup") {
      if (call.phase !== "connected" || call.acceptState !== "connected" ||
          !validNativeRelationId(call.enterRelationId) || !enterRelationHistory.has(call.enterRelationId) ||
        call.roomEntry !== null) throw codedError("call_not_connected");
    } else if (operation === "reject") {
      if (call.phase !== "invite_ready" || call.acceptState !== "not_requested" ||
        call.enterRelationId !== null || call.roomEntry !== null || call.acceptRequestId !== null) {
        throw codedError("call_not_accepting");
      }
    } else {
      throw codedError("invalid_private_end_operation");
    }

    const command = operation === "hangup" ? QRTC_COMMAND_QUIT : QRTC_COMMAND_REJECT;
    const params = operation === "hangup" ? buildPrivateHangupParams() : buildPrivateRejectParams(call.fromUid);
    const previous = { phase: call.phase, acceptState: call.acceptState, uncertain: call.uncertain };
    const id = allocateRequestId();
    const key = pendingKey(command, id);
    const entry = {
      command,
      requestId: id,
      hostGeneration,
      callGeneration: call.generation,
      connectionGeneration,
      callId: call.callId,
      operation,
      peerUid: call.fromUid,
      privateScopeEvidence: capturedEvidence,
      dispatchStarted: false,
    };

    // Reserve the single-call end operation before entering HostClient's queue.
    // The saved 20004 relation remains the only terminal correlation key.
    call.endOperation = operation;
    call.endRequestId = id;
    call.endState = "posting";
    call.phase = operation === "hangup" ? "ending" : "reject_posting";
    pending.set(key, entry);
    schedulePrivateEndAckTimeout(call, operation, id, entry);
    lastFailureCode = null;
    emitStatus(`${operation}_posting`);

    const assertDispatchCurrent = () => {
      canOperate();
      if (activeCall !== call || call.callId !== callId || call.generation !== entry.callGeneration ||
          call.connectionGeneration !== connectionGeneration || call.hostGeneration !== hostGeneration ||
          call.fromUid !== entry.peerUid || call.inviteParams?.[1] !== entry.peerUid ||
          call.endOperation !== operation || call.endRequestId !== id || call.endState !== "posting" ||
          pending.get(key) !== entry) return false;
      const live = requirePrivateEndScope(callId, operation);
      if (live.call !== call || live.capturedEvidence !== capturedEvidence || call.privateScopeEvidence !== capturedEvidence) return false;
      entry.dispatchStarted = true;
      return true;
    };

    try {
      const receipt = await host.invoke(
        { command, id, params, hostGeneration },
        { assertCurrent: assertDispatchCurrent },
      );
      if (receipt?.posted !== true || receipt.id !== id || receipt.command !== command) {
        throw codedError("host_post_receipt_invalid");
      }
      entry.posted = true;
      if (operation === "reject" && activeCall === call) confirmPrivateRejectReadback(call, entry);
      if (activeCall === call && pending.get(key) === entry && call.endState === "posting") {
        call.endState = "posted";
        if (operation === "reject") call.phase = "reject_posted";
        emitStatus(`${operation}_posted`);
      }
      return { posted: true, id, callId, callGeneration: call.generation };
    } catch (error) {
      if (activeCall === call && pending.get(key) === entry && call.endRequestId === id) {
        pending.delete(key);
        clearPrivateEndAckTimer(call);
        const code = safeCode(error, "private_end_post_failed");
        const definitelyNotPosted = new Set([
          "private_end_protocol_unverified",
          "host_invoke_guard_rejected",
          "call_not_current",
          "private_scope_unverified",
          "private_end_scope_unverified",
          "host_unavailable",
          "host_client_not_running",
          "bridge_closing",
          "bridge_not_attached",
          "host_generation_invalidated",
          "provider_generation_invalidated",
          "controller_generation_invalidated",
          "login_not_ready",
        ]).has(code);
        if (definitelyNotPosted) {
          call.phase = previous.phase;
          call.acceptState = previous.acceptState;
          call.uncertain = previous.uncertain;
          call.endOperation = null;
          call.endRequestId = null;
          call.endState = null;
          lastFailureCode = code;
          emitStatus(`${operation}_not_posted`);
          if (code === "private_end_protocol_unverified" || code === "private_end_scope_unverified") {
            throw codedError(`unsupported_unverified_command:${operation}`);
          }
          if (code === "host_invoke_guard_rejected") throw codedError("call_not_current");
        } else {
          call.phase = operation === "hangup" ? "ending_unconfirmed" : "reject_unconfirmed";
          call.endState = `${operation}_post_unconfirmed`;
          call.uncertain = true;
          lastFailureCode = `${operation}_post_unconfirmed`;
          emitStatus(`${operation}_post_unconfirmed`);
        }
      }
      throw error;
    }
  }

  function groupProtocolAvailable() {
    try {
      const hostStatus = typeof host.status === "function" ? host.status() : null;
      return hostStatus?.available === true && hostStatus?.groupCallProtocolAvailable === true &&
        typeof host.queryGroupRoomInfo === "function" && typeof host.startGroup === "function" &&
        typeof host.quitGroup === "function";
    } catch { return false; }
  }

  function groupContextIsCurrent(context) {
    if (!context || context.serviceInstance !== groupServiceInstance ||
        context.hostGeneration !== hostGeneration || context.connectionGeneration !== connectionGeneration ||
        !groupServiceInstance || !trustedSelfUid || loginState !== "ready" || !attached ||
        stopping || closing || stopped || hostInvalidated || providerInvalidated || controllerInvalidated ||
        !hostAvailable || !groupProtocolAvailable()) return false;
    return hostIsCurrent() && providerIsCurrent();
  }

  function assertConfiguredGroupContext() {
    if (!groupServiceInstance) throw codedError("group_control_not_configured");
    canOperate();
    if (loginState !== "ready" || !trustedSelfUid) throw codedError("login_not_ready");
    if (!hostAvailable || !groupProtocolAvailable()) throw codedError("group_protocol_unavailable");
    return Object.freeze({
      serviceInstance: groupServiceInstance,
      hostGeneration,
      connectionGeneration,
    });
  }

  function rememberGroupCommand(command, id) {
    groupCommandOutputIds.add(`${command}:${id}`);
    while (groupCommandOutputIds.size > 1024) groupCommandOutputIds.delete(groupCommandOutputIds.values().next().value);
  }

  let driver = null;

  async function confirmNativeAbsence({ context, scope, terminalSequence, expectedState, quitRequestId = null }) {
    const selfUid = trustedSelfUid;
    const selfUin = trustedSelfUin;
    if (!exactGroupScope(scope, { ...context, groupId: groupStartGroupId }) ||
        !Number.isSafeInteger(terminalSequence) || terminalSequence <= 0 ||
        !groupScopesEqual(scope, groupCurrentScopeProof) || !groupContextIsCurrent(context) ||
        !QQ_UID.test(selfUid || "") || !CANONICAL_GROUP_ID.test(selfUin || "")) return false;
    const current = driver?.status();
    if (!current || current.state !== expectedState || current.terminalSequence !== terminalSequence ||
        current.hostGeneration !== hostGeneration || current.connectionGeneration !== connectionGeneration ||
        (expectedState === "ending" &&
          (!positiveGeneration(quitRequestId) || groupQuitRequestId !== quitRequestId))) return false;

    const kernelEnd = groupKernelEndTicket;
    if (expectedState === "ending_remote" && kernelEnd && groupScopesEqual(kernelEnd.scope, scope) &&
        kernelEnd.startRequestId === groupStartRequestId && kernelEnd.selfUid === selfUid && kernelEnd.selfUin === selfUin &&
        typeof host.clearGroupRoomAbsence === "function" && host.status()?.groupLocalQuitProofAvailable === true) {
      const queryId = allocateRequestId();
      const clearCurrent = () => groupContextIsCurrent(context) && groupKernelEndTicket === kernelEnd &&
        groupScopesEqual(scope, groupCurrentScopeProof) && trustedSelfUid === selfUid && trustedSelfUin === selfUin &&
        driver?.status().state === expectedState && driver.status().terminalSequence === terminalSequence;
      let proof;
      try {
        proof = await host.clearGroupRoomAbsence({ id: queryId, hostGeneration,
          groupId: scope.groupId, startRequestId: groupStartRequestId }, { assertCurrent: clearCurrent });
      } catch { return false; }
      return Boolean(clearCurrent() && proof?.ok === true && proof.confirmed === true && proof.id === queryId &&
        proof.hostGeneration === hostGeneration && proof.groupId === scope.groupId &&
        proof.startRequestId === groupStartRequestId && proof.source === "native_group_clear_absence");
    }

    if (expectedState === "ending_remote" && typeof host.confirmGroupRemoteAbsence === "function" &&
        host.status()?.groupLocalQuitProofAvailable === true) {
      const queryId = allocateRequestId();
      let proof;
      try {
        proof = await host.confirmGroupRemoteAbsence({ id: queryId, hostGeneration,
          groupId: scope.groupId, startRequestId: groupStartRequestId }, {
          assertCurrent: () => groupContextIsCurrent(context),
        });
      } catch { /* Retain the independent complete-roster fallback. */ }
      const after = driver?.status();
      if (!groupContextIsCurrent(context) || trustedSelfUid !== selfUid || trustedSelfUin !== selfUin ||
          !groupScopesEqual(scope, groupCurrentScopeProof) || after?.state !== expectedState ||
          after.terminalSequence !== terminalSequence) return false;
      if (proof?.ok === true && proof.confirmed === true && proof.id === queryId &&
          proof.hostGeneration === hostGeneration && proof.groupId === scope.groupId &&
          proof.startRequestId === groupStartRequestId && proof.source === "native_entered_group_room_absence") return true;
    }

    if (expectedState === "ending" && typeof host.confirmGroupQuitAbsence === "function" &&
        host.status()?.groupLocalQuitProofAvailable === true) {
      const queryId = allocateRequestId();
      let result = null;
      try {
        result = await host.confirmGroupQuitAbsence({ id: queryId, hostGeneration, groupId: scope.groupId,
          startRequestId: groupStartRequestId, quitRequestId }, { assertCurrent: () => groupContextIsCurrent(context) });
      } catch { /* A failed/unsupported readback never replaces the membership fallback. */ }
      const after = driver?.status();
      if (!groupContextIsCurrent(context) || trustedSelfUid !== selfUid || trustedSelfUin !== selfUin ||
          !groupScopesEqual(scope, groupCurrentScopeProof) || after?.state !== expectedState ||
          after.terminalSequence !== terminalSequence || groupQuitRequestId !== quitRequestId) return false;
      if (result?.ok === true && result.confirmed === true && result.id === queryId &&
          result.hostGeneration === hostGeneration && result.groupId === scope.groupId &&
          result.startRequestId === groupStartRequestId && result.quitRequestId === quitRequestId &&
          result.source === "native_sdk_room_id_readback") return true;
    }

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const beforeQuery = driver?.status();
      if (!groupContextIsCurrent(context) || trustedSelfUid !== selfUid || trustedSelfUin !== selfUin ||
          !groupScopesEqual(scope, groupCurrentScopeProof) || beforeQuery?.state !== expectedState ||
          beforeQuery.terminalSequence !== terminalSequence) return false;
      const queryId = allocateRequestId();
      let result;
      try {
        result = await host.queryGroupRoomInfo({ id: queryId, hostGeneration, groupId: scope.groupId }, {
          assertCurrent: () => groupContextIsCurrent(context),
        });
      } catch { return false; }

      const after = driver.status();
      if (!groupContextIsCurrent(context) || trustedSelfUid !== selfUid || trustedSelfUin !== selfUin ||
          !groupScopesEqual(scope, groupCurrentScopeProof) || after.state !== expectedState ||
          after.terminalSequence !== terminalSequence || after.hostGeneration !== hostGeneration ||
          after.connectionGeneration !== connectionGeneration) return false;
      const checked = parseExistingRoomQueryResult(result, { id: queryId, hostGeneration, groupId: scope.groupId });
      if (!checked.valid || result.roomInfo.userNum !== result.roomInfo.userList.length) return false;
      if (!result.roomInfo.userList.every((user) =>
        QQ_UID.test(user.uid) || CANONICAL_GROUP_ID.test(user.uid))) return false;
      const selfPresent = result.roomInfo.userList.some((user) => user.uid === selfUid || user.uid === selfUin);
      if (!selfPresent) return true;
      // Only an owned quit may settle through a briefly lagging complete roster.
      // Each retry gets a new native query ID and rechecks the original context;
      // malformed/partial/unknown evidence and remote-end checks still fail closed.
      if (attempt === 2) return false;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return false;
  }

  function createBridgeGroupDriver(context) {
    driver = createGroupCallDriver({
      serviceInstance: context.serviceInstance,
      joinTtlMs: 20_000,
      hostGeneration,
      connectionGeneration,
      selfUid: trustedSelfUid,
      queryRoomInfo: async (request) => {
        const target = groupAudioDeviceTarget;
        if (!target || typeof host.selectAudioDevices !== "function") {
          throw codedError("audio_device_selection_unverified");
        }
        const before = driver?.status();
        const ticket = {
          phase: "selecting_audio",
          requestId: groupPreflightRequestId,
          queryId: request?.id,
          groupId: request?.groupId,
          roomGeneration: before?.roomGeneration,
          terminalSequence: before?.terminalSequence,
          selfUid: trustedSelfUid,
          selfUin: trustedSelfUin,
          privateInviteObserved: false,
        };
        if (groupPreflightSelectionTicket !== null) throw codedError("call_occupied");
        groupPreflightSelectionTicket = ticket;
        const current = () => {
          const state = driver?.status();
          return groupPreflightSelectionTicket === ticket &&
            driver === groupDriver && state?.occupied === true && state.state === "preflight" &&
            state.roomGeneration === ticket.roomGeneration && state.terminalSequence === ticket.terminalSequence &&
            groupPreflightRequestId === ticket.requestId && Number.isSafeInteger(ticket.requestId) && ticket.requestId > 0 &&
            groupStartRequestId === null && groupStartGroupId === ticket.groupId &&
            Number.isSafeInteger(ticket.queryId) && ticket.queryId > 0 &&
            ticket.queryId !== ticket.requestId && request?.hostGeneration === hostGeneration &&
            request?.groupId === ticket.groupId && groupCurrentScopeProof === null &&
            trustedSelfUid === ticket.selfUid && trustedSelfUin === ticket.selfUin &&
            loginState === "ready" && activeCall === null && !ticket.privateInviteObserved &&
            audioDeviceTarget?.inputDeviceName === target.inputDeviceName &&
            audioDeviceTarget?.outputDeviceName === target.outputDeviceName &&
            groupContextIsCurrent(context);
        };
        try {
          if (!current()) throw codedError("stale_context");
          const selectionId = allocateRequestId();
          if (selectionId === ticket.requestId || selectionId === ticket.queryId) {
            throw codedError("request_id_exhausted");
          }
          const selection = await host.selectAudioDevices({
            id: selectionId,
            hostGeneration,
            inputDeviceName: target.inputDeviceName,
            outputDeviceName: target.outputDeviceName,
          });
          if (!current()) throw codedError("stale_context");
          if (selection?.selected !== true || selection.id !== selectionId || selection.command !== 102) {
            throw codedError("audio_device_selection_receipt_invalid");
          }
          ticket.phase = "room_querying";
          if (!current()) throw codedError("stale_context");
          const result = await host.queryGroupRoomInfo(request, { assertCurrent: current });
          if (!current()) throw codedError("stale_context");
          return result;
        } finally {
          if (groupPreflightSelectionTicket === ticket) groupPreflightSelectionTicket = null;
        }
      },
      allocateRequestId,
      sendCommand: (request) => {
        if (!request || request.hostGeneration !== hostGeneration || !Number.isSafeInteger(request.id) || request.id <= 0) {
          throw codedError("invalid_group_command");
        }
        const guard = { assertCurrent: () => groupContextIsCurrent(context) };
        if (request.command === 4) {
          if (!validGroupId(groupStartGroupId)) throw codedError("invalid_group_id");
          if (groupStartInvitation && !autoGroupInviteCurrent(groupStartInvitation)) {
            return { posted: false, id: request.id, command: 4 };
          }
          groupStartRequestId = request.id;
          rememberGroupCommand(4, request.id);
          return host.startGroup({ id: request.id, hostGeneration, groupId: groupStartGroupId }, guard);
        }
        if (request.command === 8) {
          if (!validGroupId(groupStartGroupId) || !positiveGeneration(groupStartRequestId)) {
            throw codedError("group_start_scope_missing");
          }
          groupQuitRequestId = request.id;
          rememberGroupCommand(8, request.id);
          return host.quitGroup({
            id: request.id,
            hostGeneration,
            groupId: groupStartGroupId,
            startRequestId: groupStartRequestId,
          }, guard);
        }
        throw codedError("unsupported_group_command");
      },
      slotFree: () => activeCall === null,
      currentContext: (candidate) => groupContextIsCurrent(candidate),
      confirmNoMembership: async ({ scope, terminalSequence, quitRequestId }) =>
        confirmNativeAbsence({ context, scope, terminalSequence, expectedState: "ending", quitRequestId }),
      confirmRemoteNoMembership: async ({ scope, terminalSequence }) =>
        confirmNativeAbsence({ context, scope, terminalSequence, expectedState: "ending_remote" }),
      onStatus: (record) => {
        if (record.state === "joined") {
          groupCurrentScopeProof = driver?.currentScope() || null;
        } else if (record.state === "ending_remote") {
          groupLeaseSuspended = true;
        } else if (record.state === "idle") {
          groupKernelEndTicket = null;
          groupCurrentScopeProof = null;
          groupLeaseSuspended = false;
          groupStartRequestId = null;
          groupStartGroupId = null;
          groupPreflightRequestId = null;
          groupQuitRequestId = null;
          groupPreflightSelectionTicket = null;
        }
        emitStatus(`group_${record.state}`);
      },
    });
    return driver;
  }

  function configureGroupControl(serviceInstance) {
    if (groupServiceInstance !== null || loginState !== "logged_out" || trustedSelfUid !== null ||
        typeof serviceInstance !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(serviceInstance)) {
      throw codedError("group_control_already_configured");
    }
    groupServiceInstance = serviceInstance;
    return { configured: true };
  }

  function ensureGroupDriver() {
    const context = assertConfiguredGroupContext();
    if (!groupDriver) groupDriver = createBridgeGroupDriver(context);
    return groupDriver;
  }

  function autoGroupInviteCurrent(ticket) {
    const age = now() - ticket.observedAtMs;
    return age >= 0 && age < 30_000 && ticket.selfUid === trustedSelfUid && ticket.selfUin === trustedSelfUin &&
      loginState === "ready" && !hostInvalidated && !providerInvalidated && !controllerInvalidated && !stopping && !stopped;
  }

  async function joinGroup(groupId, inviteId = null) {
    if (!validGroupId(groupId)) throw codedError("invalid_group_id");
    const driver = ensureGroupDriver();
    if (activeCall !== null || driver.status().occupied) throw codedError("call_occupied");
    let invitation = null;
    if (inviteId !== null) {
      invitation = pendingAutoGroupInvite;
      if (!invitation || invitation.inviteId !== inviteId || invitation.groupId !== groupId || !autoGroupInviteCurrent(invitation)) {
        throw codedError("group_invite_stale");
      }
      // Consume before the asynchronous room/device checks. Failed admission
      // never retries this invitation or creates a new group call.
      pendingAutoGroupInvite = null;
    }
    groupStartInvitation = invitation;
    groupStartRequestId = null;
    groupStartGroupId = groupId;
    groupPreflightRequestId = allocateRequestId();
    groupQuitRequestId = null;
    groupCurrentScopeProof = null;
    groupLeaseSuspended = false;
    resetBusyInviteDedupe();
    const requestId = groupPreflightRequestId;
    let result;
    try { result = await driver.start({ id: requestId, hostGeneration, groupId }); }
    finally { if (groupStartInvitation === invitation) groupStartInvitation = null; }
    if (result?.ok !== true || !exactGroupScope(result.scope, {
      serviceInstance: groupServiceInstance,
      hostGeneration,
      connectionGeneration,
      groupId,
    })) throw codedError(safeCode(result, "group_entry_unconfirmed"));
    const scope = driver.currentScope();
    if (!groupScopesEqual(scope, result.scope)) throw codedError("group_scope_unverified");
    return {
      joined: true,
      scope,
      source: NATIVE_GROUP_SOURCE,
    };
  }

  async function quitGroup(scope) {
    const driver = ensureGroupDriver();
    const groupStatus = driver.status();
    const currentScope = driver.currentScope();
    if (!exactGroupScope(scope, { serviceInstance: groupServiceInstance, hostGeneration, connectionGeneration }) ||
        !groupScopesEqual(groupCurrentScopeProof, scope) ||
        (!["ending_remote", "quarantined"].includes(groupStatus.state) && !groupScopesEqual(currentScope, scope))) {
      throw codedError("group_scope_stale");
    }
    if (!positiveGeneration(groupStartRequestId) || groupStartGroupId !== scope.groupId) {
      throw codedError("group_start_scope_missing");
    }
    const result = await driver.quit({ id: allocateRequestId(), hostGeneration, scope });
    if (result?.ok !== true) {
      const code = safeCode(result, "group_quit_unconfirmed");
      throw codedError(code === "group_ending_remote" ? "group_scope_stale" : code);
    }
    return result;
  }

  async function getGroupRoomStatus(groupId) {
    if (!validGroupId(groupId)) throw codedError("invalid_group_id");
    const driver = ensureGroupDriver();
    const context = Object.freeze({
      serviceInstance: groupServiceInstance,
      hostGeneration,
      connectionGeneration,
    });
    const observed = groupKernelObserver.getGroupDiagnostics(groupId);
    const membership = observed?.membership;
    let observedNow = null;
    try { observedNow = now(); } catch { /* An unavailable clock cannot establish a fresh negative hint. */ }
    const age = Number.isSafeInteger(observedNow) && Number.isSafeInteger(membership?.observedAtMs)
      ? observedNow - membership.observedAtMs : null;
    const newerInvite = pendingAutoGroupInvite?.groupId === groupId && autoGroupInviteCurrent(pendingAutoGroupInvite) &&
      pendingAutoGroupInvite.observedAtMs >= membership?.observedAtMs;
    const newerInvitationUpdate = observed?.invitation?.observedAtMs >= membership?.observedAtMs &&
      observed.invitation.joinCount > 0;
    if (!activeCall && !driver.status().occupied && groupContextIsCurrent(context) &&
        membership?.source === "group_audio_member_count" && membership.onlineCount === 0 && membership.memberCount === 0 &&
        typeof membership.nativeTimeout === "boolean" && Number.isSafeInteger(observedNow) &&
        Number.isSafeInteger(membership.observedAtMs) && age >= 0 && age < EMPTY_GROUP_OBSERVATION_TTL_MS &&
        !newerInvite && !newerInvitationUpdate) {
      // A fresh complete zero-member notification is a negative admission hint,
      // never a positive room proof or authority to release an existing scope.
      throw codedError("group_room_observed_empty");
    }
    const id = allocateRequestId();
    const response = await host.queryGroupRoomInfo({ id, hostGeneration, groupId }, {
      assertCurrent: () => groupContextIsCurrent(context),
    });
    if (!groupContextIsCurrent(context)) throw codedError("group_context_stale");
    const checked = parseExistingRoomQueryResult(response, { id, hostGeneration, groupId });
    if (!checked.valid) throw codedError("group_room_query_receipt_invalid");
    const driverStatus = driver.status();
    const currentScope = driver.currentScope();
    const roomGeneration = currentScope?.groupId === groupId
      ? currentScope.roomGeneration : driverStatus.roomGeneration + 1;
    return {
      ok: true,
      serviceInstance: groupServiceInstance,
      hostGeneration,
      connectionGeneration,
      groupId,
      ready: checked.existing,
      logicalRoomId: checked.existing ? response.roomInfo.relationId : null,
      roomGeneration,
      occupied: driverStatus.occupied || activeCall !== null,
      nativeJoinVerified: false,
      room: null,
      nativeRoomInfo: response.roomInfo,
    };
  }

  function getCurrentGroupScope() {
    if (groupLeaseSuspended) return null;
    return groupDriver?.currentScope() || null;
  }

  function stop() {
    if (stopped) return Promise.resolve(stopResult);
    if (stopping) return Promise.reject(codedError("bridge_stop_in_progress"));
    // Claim shutdown before awaiting attach; attach() will reject new work and
    // stop waits for the in-flight registration before attempting detach.
    closing = true;
    stopping = true;
    clearPrivateEndAckTimer(activeCall);
    clearPrivateCloseAckTimer(activeCall);
    const operation = (async () => {
      groupDriver?.stop();
      groupKernelObserver.stop();
      clearCallTimer();
      signalQueue = [];
      pending.clear();
      clearForwardQueue("bridge_closing");
      clearPreAttachOutputQueue();
      if (attachPromise) {
        try { await attachPromise; } catch { /* inspect registration and detach below */ }
      }
      hostUnsubscribe?.();
      providerUnsubscribe?.();
      hostUnsubscribe = null;
      providerUnsubscribe = null;
      if (attached || attachUncertain) {
        const reply = unwrapResult(await provider.detach());
        if (reply?.ok !== true || reply?.status?.listenerRegistered !== false) throw codedError("detach_unconfirmed");
        attached = false;
        attachUncertain = false;
      }
      if (activeCall) failCall("bridge_stopped", "failed");
      loginState = "logged_out";
      stopped = true;
      stopResult = { stopped: true, detachedConfirmed: true };
      stopping = false;
      emitStatus("stopped");
      statusSubscribers.clear();
      controlEventSubscribers.clear();
      return stopResult;
    })().catch((error) => {
      // Keep truthful state and allow a later explicit stop retry.
      stopping = false;
      lastFailureCode = safeCode(error, "detach_unconfirmed");
      emitStatus("detach_unconfirmed");
      throw error;
    });
    return operation;
  }

  function subscribeStatus(handler) {
    if (typeof handler !== "function") throw codedError("invalid_status_handler");
    statusSubscribers.add(handler);
    let subscribed = true;
    return () => {
      if (!subscribed) return false;
      subscribed = false;
      return statusSubscribers.delete(handler);
    };
  }

  function subscribeControlEvents(handler) {
    if (typeof handler !== "function") throw codedError("invalid_control_event_handler");
    controlEventSubscribers.add(handler);
    let subscribed = true;
    return () => {
      if (!subscribed) return false;
      subscribed = false;
      return controlEventSubscribers.delete(handler);
    };
  }

  function getIntegrationSnapshot() {
    return { status: status(), call: integrationCallSummary() };
  }

  function getGroupDiagnostics(groupId) {
    return groupKernelObserver.getGroupDiagnostics(groupId);
  }

  return Object.freeze({
    prepareSubscriptions,
    attach,
    start: attach,
    login,
    accept,
    hangup: (callId) => privateEnd("hangup", callId),
    reject: (callId) => privateEnd("reject", callId),
    status,
    subscribeStatus,
    subscribeControlEvents,
    getIntegrationSnapshot,
    getGroupDiagnostics,
    configureGroupControl,
    joinGroup,
    quitGroup,
    getCurrentGroupScope,
    getGroupRoomStatus,
    stop,
  });
}

module.exports = { createQQCallBridge, MAX_SIGNAL_QUEUE };
