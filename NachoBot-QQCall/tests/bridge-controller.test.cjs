"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const net = require("node:net");
const { createHostClient } = require("../bridge/host-client.cjs");
const { createHostTransport } = require("../host/transport.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");
const {
  PRIVATE_INVITE_PROFILE,
  PINNED_WRAPPER_SHA256,
  nativeInviteEvidenceFromCallback,
} = require("../shared/private-invite-scope.cjs");
const { PRIVATE_CALL_SCENE } = require("../host/private-end.cjs");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function makeHarness({
  connectionGeneration = 21,
  hostGeneration = 31,
  providerInitiallyConnected = true,
  providerInitiallyAuthenticated = providerInitiallyConnected,
  providerAttach,
  hostInvoke,
  hostSelect,
  hostRejectAbsence,
  officialPrivateInviteLifecycle,
  hostKernelRoomAbsence,
  audioDeviceTarget,
  callTimeoutMs = 1000,
  maxSignalQueue,
  maxForwardQueue,
  maxForwardQueueBytes,
  onStatus,
  kernelPeerUidByUin,
  now,
  trustedNativeInviteProvider = false,
  verifiedPrivateEndProtocol = false,
  privateEndAckTimeoutMs = 5000,
  hostOverride = null,
  hostOutput = null,
} = {}) {
  const providerListeners = new Set();
  const hostListeners = new Set();
  const calls = [];
  let listenerRegistered = false;
  let providerConnected = providerInitiallyConnected;
  let providerAuthenticated = providerInitiallyAuthenticated;
  let hostAvailable = true;
  let nativeInviteCallbackSequence = 0;
  let nativeInviteIpcSequence = 0;
  const initialNativeInviteBinding = trustedNativeInviteProvider ? Object.freeze({
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    targetPid: 4455,
    kernelCreationTime100ns: "134000000000000000",
    connectionGeneration,
    listenerMutationEpoch: 2,
    listenerRegistrationSequence: 1,
  }) : null;
  let currentNativeInviteBinding = initialNativeInviteBinding;
  const provider = {
    async attach() {
      if (providerAttach) {
        const reply = await providerAttach();
        if (reply?.status?.listenerRegistered === true) listenerRegistered = true;
        return reply;
      }
      listenerRegistered = true;
      return { ok: true, status: { listenerRegistered: true } };
    },
    async detach() {
      calls.push({ kind: "detach" });
      listenerRegistered = false;
      return { ok: true, status: { listenerRegistered: false } };
    },
    status() {
      return {
        connected: providerConnected,
        authenticated: providerAuthenticated,
        connectionGeneration,
        agent: { agent: { listenerRegistered } },
        ...(listenerRegistered && currentNativeInviteBinding ? { nativeInviteBinding: currentNativeInviteBinding } : {}),
      };
    },
    async forwardAVSDKOutput(command, value) {
      calls.push({ kind: "forward", command, value });
      return { ok: true, result: { ok: true, forwarded: true } };
    },
    subscribe(handler) {
      providerListeners.add(handler);
      return () => providerListeners.delete(handler);
    },
    async emitKernel(method, args, options = {}) {
      const payload = {
        kind: "kernel_event",
        method,
        args,
        action: options.action || null,
        connectionGeneration: options.generation ?? connectionGeneration,
      };
      if (options.provenNative === true && currentNativeInviteBinding && listenerRegistered) {
        nativeInviteCallbackSequence += 1;
        const evidence = nativeInviteEvidenceFromCallback(
          PRIVATE_INVITE_PROFILE,
          method,
          args,
          nativeInviteCallbackSequence,
          currentNativeInviteBinding.listenerRegistrationSequence,
        );
        if (evidence) {
          nativeInviteIpcSequence += 1;
          payload.nativeInviteEvidence = evidence;
          payload.nativeInviteProvenance = {
            targetPid: currentNativeInviteBinding.targetPid,
            kernelCreationTime100ns: currentNativeInviteBinding.kernelCreationTime100ns,
            connectionGeneration,
            listenerMutationEpoch: currentNativeInviteBinding.listenerMutationEpoch,
            ipcSequence: nativeInviteIpcSequence,
          };
        }
      }
      await Promise.all([...providerListeners].map((listener) => listener("kernel_event", payload)));
    },
    async emitStatus(connected, authenticated = connected) {
      providerConnected = connected;
      providerAuthenticated = authenticated;
      await Promise.all([...providerListeners].map((listener) => listener("broker_status", {
        connected, authenticated, connectionGeneration,
      })));
    },
    setNativeInviteBinding(value) {
      currentNativeInviteBinding = value ? Object.freeze({ ...value }) : null;
    },
    initialNativeInviteBinding,
  };

  const fakeHost = {
    ...(hostRejectAbsence ? { confirmPrivateRejectAbsence: hostRejectAbsence } : {}),
    ...(hostKernelRoomAbsence ? { confirmPrivateEndedRoomAbsence: hostKernelRoomAbsence } : {}),
    status: () => ({ available: hostAvailable, hostGeneration }),
    subscribe(handler) {
      hostListeners.add(handler);
      handler({ type: "availability", available: hostAvailable, hostGeneration });
      return () => hostListeners.delete(handler);
    },
    async invoke(input, options = undefined) {
      calls.push({ kind: "invoke", ...input });
      if (hostInvoke) return hostInvoke(input, host, options);
      if (options?.assertCurrent && options.assertCurrent() !== true) {
        throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
      }
      if ([8, 9, 10].includes(input.command) && !verifiedPrivateEndProtocol) {
        throw Object.assign(new Error("private_end_protocol_unverified"), { code: "private_end_protocol_unverified" });
      }
      return { posted: true, id: input.id, command: input.command };
    },
    async selectAudioDevices(input) {
      calls.push({ kind: "select_devices", ...input });
      if (hostSelect) return hostSelect(input, host);
      return { selected: true, id: input.id, command: 102 };
    },
    async emitOutput(command, id, value, generation = hostGeneration) {
      const event = { type: "output", command, id, value, hostGeneration: generation };
      return Promise.all([...hostListeners].map((listener) => listener(event)));
    },
    async setAvailable(available, generation = hostGeneration) {
      hostAvailable = available;
      await Promise.all([...hostListeners].map((listener) => listener({
        type: "availability", available, hostGeneration: generation,
      })));
    },
  };
  const host = hostOverride ? {
    status: () => hostOverride.status(),
    subscribe: (handler) => hostOverride.subscribe(handler),
    async invoke(input, options = undefined) {
      calls.push({ kind: "invoke", ...input });
      return hostOverride.invoke(input, options);
    },
    async selectAudioDevices(input) {
      calls.push({ kind: "select_devices", ...input });
      if (hostSelect) return hostSelect(input, host);
      return hostOverride.selectAudioDevices(input);
    },
    async emitOutput(command, id, value, generation = hostGeneration) {
      if (typeof hostOutput !== "function") throw new Error("real_host_output_helper_missing");
      return hostOutput(command, id, value, generation);
    },
  } : fakeHost;

  const bridge = createQQCallBridge({
    provider,
    host,
    connectionGeneration,
    hostGeneration,
    ...(audioDeviceTarget ? { audioDeviceTarget } : {}),
    ...(kernelPeerUidByUin !== undefined ? { kernelPeerUidByUin } : {}),
    callTimeoutMs,
    ...(now ? { now } : {}),
    ...(maxSignalQueue ? { maxSignalQueue } : {}),
    ...(maxForwardQueue ? { maxForwardQueue } : {}),
    ...(maxForwardQueueBytes ? { maxForwardQueueBytes } : {}),
    privateEndAckTimeoutMs,
    ...(officialPrivateInviteLifecycle ? { officialPrivateInviteLifecycle } : {}),
    ...(onStatus ? { onStatus } : {}),
  });
  return {
    bridge,
    provider,
    host,
    calls,
    listenerCounts: () => ({ provider: providerListeners.size, host: hostListeners.size }),
  };
}

function inviteEvent(relationId, fromUid, generation = 21, method = "onInviteActionToAVSDK") {
  return {
    method,
    args: [{ relation_id: relationId, invite_type: 3, from_uid: fromUid }],
    action: { command: 55, value: [8, "private-signal-payload"] },
    generation,
  };
}

function inviteCallback(fromUid = "caller-private-uid") {
  return [PRIVATE_CALL_SCENE, fromUid, "self-private-uid", 9, "invite-private-field", null, null, null, null, null, true, "private-tail"];
}

const AUDIO_DEVICE_TARGET = {
  inputDeviceName: "Line 1 (Virtual Audio Cable)",
  outputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
};
const MAPPED_PEER_UIN = "123456789";
const MAPPED_PEER_UID = "u_ABCDEFGHIJKLMNOPQRSTUV";
const BUSY_CALLER_UID = `u_${"B".repeat(22)}`;

function matchedInviteCallback(scene, peerUid, relationId) {
  return [scene, peerUid, [peerUid], 3, relationId, null, null, null, null, null, true, "private-tail"];
}

function roomEntry(scene, enterRelationId, peerUid, result = 0) {
  return [result, scene, enterRelationId, peerUid, 0, "private-room-detail", 1];
}

function kernelRoomInfo(roomId, peerUid, overrides = {}) {
  return {
    roomId,
    peerUid,
    termAction: 0,
    termType: 0,
    bussinessType: 0,
    peerUin: "1",
    ...overrides,
  };
}

function kernelRoomEndArgs(roomId, peerUid, { reason = 1, relationId = "unrelated-kernel-relation", list } = {}) {
  const endData = { destroyReason: reason, relation_id: relationId };
  if (list !== undefined) endData.pushRoomInfoList = list;
  else endData.pushRoomInfoList = [kernelRoomInfo(roomId, peerUid)];
  return [endData, null];
}

async function prepareAudioAccept(harness, { scene = PRIVATE_CALL_SCENE, peerUid = "u_synthetic_peer_001", relationId = "synthetic-room-001" } = {}) {
  const invite = await prepareAudioInvite(harness, { scene, peerUid, relationId });
  const accepted = await harness.bridge.accept(invite.callId);
  return { ...invite, accepted };
}

async function prepareAudioInvite(harness, { scene = PRIVATE_CALL_SCENE, peerUid = "u_synthetic_peer_001", relationId = "synthetic-room-001" } = {}) {
  await harness.bridge.attach();
  if (harness.bridge.status().loginState !== "ready") {
    await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
    const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
    await harness.host.emitOutput(1, login.id, [0, ""]);
  }
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "kernel-relation", invite_type: 3, from_uid: peerUid }]);
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(scene, peerUid, relationId));
  return { callId, scene, peerUid, relationId };
}

async function prepareVerifiedPrivateInvite(harness, { scene = 1, peerUid = `u_${"P".repeat(22)}` } = {}) {
  await harness.bridge.attach();
  if (harness.bridge.status().loginState !== "ready") {
    await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
    const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
    await harness.host.emitOutput(1, login.id, [0, ""]);
  }
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "private-invite-relation",
    invite_type: 1,
    from_uid: peerUid,
  }], { provenNative: true });
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(scene, peerUid, "private-invite-callback-relation"));
  return { callId, scene, peerUid };
}

async function connectVerifiedPrivateCall(harness, options = {}) {
  const current = await prepareVerifiedPrivateInvite(harness, options);
  const accepted = await harness.bridge.accept(current.callId);
  const enterRelationId = `private-enter-${current.callId}`;
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  await harness.host.emitOutput(5, accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.connected, true);
  return { ...current, accepted, enterRelationId };
}

async function connectAudioCall(harness, options = {}) {
  const current = await prepareAudioAccept(harness, options);
  const enterRelationId = options.enterRelationId || `entered-${current.callId}`;
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.connected, true);
  return { ...current, enterRelationId };
}

async function waitFor(predicate, timeoutMs = 500) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) throw new Error("test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function unusedLoopbackPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

test("real Host transport records pre-attach command 20050 without failing Host or touching Kernel", async (t) => {
  const hostGeneration = 31;
  const connectionGeneration = 21;
  const token = "A".repeat(43);
  const hostPort = await unusedLoopbackPort();
  const client = createHostClient({
    token,
    listenPort: hostPort,
    outputPort: 0,
    hostGeneration,
    requestTimeoutMs: 1000,
    outputTimeoutMs: 1000,
    healthPollMs: 10_000,
  });
  let host = null;
  let bridge = null;
  t.after(async () => {
    if (bridge) await bridge.stop();
    if (host) await host.stop();
    await client.stop();
  });
  await client.start();
  let pluginInvokeCount = 0;
  host = createHostTransport({
    token,
    listenPort: hostPort,
    outputPort: client.status().outputPort,
    hostGeneration,
    requestTimeoutMs: 1000,
    outputTimeoutMs: 1000,
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    invokePlugin: async ({ command, id }) => {
      pluginInvokeCount += 1;
      return { posted: true, command, id };
    },
  });
  await host.start();
  let healthy = false;
  for (let attempt = 0; attempt < 20 && !healthy; attempt += 1) {
    healthy = await client.checkHealth();
    if (!healthy) await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(healthy, true);

  const providerListeners = new Set();
  const kernelOperations = [];
  const provider = {
    status: () => ({ connected: true, authenticated: true, connectionGeneration }),
    subscribe(handler) { providerListeners.add(handler); return () => providerListeners.delete(handler); },
    async attach() {
      kernelOperations.push("attach");
      return { ok: true, status: { listenerRegistered: true } };
    },
    async detach() {
      kernelOperations.push("detach");
      return { ok: true, status: { listenerRegistered: false } };
    },
    async forwardAVSDKOutput(command) {
      kernelOperations.push(`forward:${command}`);
      return { ok: true, result: { ok: true, forwarded: true } };
    },
  };
  bridge = createQQCallBridge({ provider, host: client, hostGeneration, connectionGeneration });
  bridge.prepareSubscriptions();

  const publication = await host.publishOutput({
    command: 20050,
    id: 0,
    value: { synthetic: true },
    hostGeneration,
  });
  assert.deepEqual(publication, { forwarded: true }, "Host delivery completes after a bounded receipt");
  assert.equal(host.status().outputFailed, false);
  assert.equal(host.status().ready, true);
  assert.equal(host.status().outputForwarded, 1);
  assert.equal(await client.checkHealth(), true);
  assert.equal(client.status().available, true);
  assert.equal(bridge.status().attached, false);
  assert.equal(bridge.status().loginState, "logged_out");
  assert.equal(bridge.status().counts.preAttachStoredOutputs, 1);
  assert.equal(bridge.status().counts.unassociatedPreAttachOutputs, 0);
  assert.deepEqual(kernelOperations, [], "receipt storage performs no attach, forward, or detach operation");
  assert.equal(pluginInvokeCount, 0);
});

test("initial and repeated disconnected Broker states preserve bounded startup observations during manual wait", async () => {
  let virtualNow = 0;
  const harness = makeHarness({
    providerInitiallyConnected: false,
    providerInitiallyAuthenticated: false,
    now: () => virtualNow,
  });
  harness.bridge.prepareSubscriptions();
  const receipt = await harness.host.emitOutput(20050, 0, { callback: "startup" });
  assert.deepEqual(receipt, [{ receivedStored: true, sequence: 1, receivedBeforeAttach: true }]);

  for (let minute = 0; minute < 180; minute += 1) {
    virtualNow += 60_000;
    await harness.provider.emitStatus(false, false);
  }
  assert.equal(harness.bridge.status().providerInvalidated, false);
  assert.equal(harness.bridge.status().controllerInvalidated, false);
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 1);
  assert.equal(harness.bridge.status().attached, false);
  assert.equal(harness.bridge.status().loginState, "logged_out");
  assert.equal(harness.calls.length, 0, "manual wait never starts Kernel or Host operations");

  await harness.bridge.attach();
  assert.equal(harness.bridge.status().attached, true);
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 1,
    "confirmed listener registration does not consume observations while Broker is still unconnected");
  await harness.provider.emitStatus(false, false);
  assert.equal(harness.bridge.status().providerInvalidated, false, "repeated disconnected status remains a wait state");
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 1);
  await harness.provider.emitStatus(true, true);
  assert.equal(harness.bridge.status().providerInvalidated, false);
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(harness.bridge.status().counts.unassociatedPreAttachOutputs, 1);
  assert.equal(harness.bridge.status().loginState, "logged_out");
  await harness.bridge.stop();
});

test("Bridge prepares real Host and Broker callbacks before attach and drains bounded early 20001 only after attach", async () => {
  const bridgeStatusEvents = [];
  const harness = makeHarness({ onStatus: (event) => bridgeStatusEvents.push(event) });
  assert.deepEqual(harness.listenerCounts(), { provider: 0, host: 0 });
  assert.equal(typeof harness.bridge.prepareSubscriptions, "function");
  harness.bridge.prepareSubscriptions();
  harness.bridge.prepareSubscriptions();
  assert.deepEqual(harness.listenerCounts(), { provider: 1, host: 1 }, "preparation is idempotent");
  assert.equal(harness.bridge.status().attached, false, "preparation does not attach the Kernel listener");
  assert.equal(harness.bridge.status().loginState, "logged_out", "preparation does not log in");

  const networkReceipt = await harness.host.emitOutput(20001, 0, [17, "pre-attach"]);
  assert.deepEqual(networkReceipt, [{ receivedStored: true, sequence: 1, receivedBeforeAttach: true }]);
  assert.equal(harness.calls.some((call) => call.kind === "forward"), false);
  assert.equal(harness.bridge.status().counts.queued20001, 1, "early output is accepted into the bounded queue");
  const secretMarkers = ["LEAK_UID_789", "LEAK_TOKEN_456", "LEAK_STDOUT_123"];
  const earlyAckReceipt = await harness.host.emitOutput(1, 0, {
    uid: secretMarkers[0], token: secretMarkers[1], stdout: secretMarkers[2],
  });
  assert.deepEqual(earlyAckReceipt, [{ receivedStored: true, sequence: 2, receivedBeforeAttach: true }]);
  const earlyCallReceipt = await harness.host.emitOutput(5, 77, { uid: "SECOND_UID", token: "SECOND_TOKEN" });
  assert.deepEqual(earlyCallReceipt, [{ receivedStored: true, sequence: 3, receivedBeforeAttach: true }]);
  assert.equal(harness.bridge.status().firstRejectedOutput, undefined, "stored outputs are not misreported as rejected");
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 3);
  assert.equal(harness.bridge.status().counts.unassociatedPreAttachOutputs, 0);
  const diagnosticText = JSON.stringify(bridgeStatusEvents);
  for (const marker of secretMarkers) assert.equal(diagnosticText.includes(marker), false, `${marker} must not reach diagnostics`);

  await harness.bridge.attach();
  assert.deepEqual(harness.listenerCounts(), { provider: 1, host: 1 });
  await waitFor(() => harness.calls.some((call) => call.kind === "forward" && call.command === 20001));
  assert.equal(harness.bridge.status().counts.queued20001, 0, "queued output drains only after attach is confirmed");
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(harness.bridge.status().counts.unassociatedPreAttachOutputs, 2);
  assert.equal(harness.bridge.status().loginState, "logged_out", "a stored old ACK cannot log in after attach");
  assert.equal(harness.bridge.status().activeCall, null, "stored callbacks do not create a call");
  assert.equal(harness.calls.find((call) => call.kind === "forward").value[1], "pre-attach");
  await harness.bridge.stop();
  assert.deepEqual(harness.listenerCounts(), { provider: 0, host: 0 });
});

test("pre-attach ACKs and call outputs are consumed as unassociated observations, never replayed into later state", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const peerUid = "u_synthetic_peer_001";
  const scene = PRIVATE_CALL_SCENE;
  const enterRelationId = "new-entered-room";
  const oldOutputs = [
    [1, 1, [0, ""]],
    [5, 3, [0, ""]],
    [20006, 0, matchedInviteCallback(scene, peerUid, "new-invite-relation")],
    [20004, 0, roomEntry(scene, enterRelationId, peerUid)],
    [20011, 0, [scene, enterRelationId, "new-room"]],
  ];
  harness.bridge.prepareSubscriptions();
  for (const [index, [command, id, value]] of oldOutputs.entries()) {
    const receipt = await harness.host.emitOutput(command, id, value);
    assert.deepEqual(receipt, [{
      receivedStored: true,
      sequence: index + 1,
      receivedBeforeAttach: true,
    }]);
  }
  await harness.bridge.attach();
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(harness.bridge.status().counts.unassociatedPreAttachOutputs, oldOutputs.length);
  assert.equal(harness.bridge.status().activeCall, null);

  const login = await harness.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "synthetic" });
  assert.equal(login.id, 1, "the old ACK deliberately reused this request ID");
  assert.equal(harness.bridge.status().loginState, "pending", "the stored ACK did not complete the new login");
  await harness.host.emitOutput(1, login.id, [0, ""]);
  assert.equal(harness.bridge.status().loginState, "ready");

  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "current-kernel-invite", invite_type: 3, from_uid: peerUid,
  }]);
  const callId = harness.bridge.status().activeCall.callId;
  assert.equal(harness.bridge.status().activeCall.phase, "invite_observed", "the old 20006 did not ready this call");
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(scene, peerUid, "current-invite-relation"));
  assert.equal(harness.bridge.status().activeCall.phase, "invite_ready");

  const accepted = await harness.bridge.accept(callId);
  assert.equal(accepted.id, 3, "the old cmd 5 ACK deliberately reused this later accept ID");
  assert.equal(harness.bridge.status().activeCall.phase, "accept_posting");
  assert.equal(harness.bridge.status().activeCall.acceptState, "posted");
  await harness.host.emitOutput(20004, 0, roomEntry(scene, enterRelationId, peerUid));
  assert.equal(harness.bridge.status().activeCall.phase, "accept_posting",
    "the current room entry waits for the current accept ACK; old destroy output did not end the call");
  await harness.host.emitOutput(5, accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.phase, "connected");
  await harness.bridge.stop();
});

test("attach-pending output receives a receipt but failed attach clears every pre-attach queue", async () => {
  const gate = deferred();
  const harness = makeHarness({ providerAttach: () => gate.promise });
  harness.bridge.prepareSubscriptions();
  const attaching = harness.bridge.attach();
  await waitFor(() => harness.listenerCounts().provider === 1 && harness.listenerCounts().host === 1);
  const observation = await harness.host.emitOutput(20050, 0, { synthetic: true });
  const network = await harness.host.emitOutput(20001, 0, [17, "network"]);
  assert.deepEqual(observation, [{ receivedStored: true, sequence: 1, receivedBeforeAttach: true }]);
  assert.deepEqual(network, [{ receivedStored: true, sequence: 2, receivedBeforeAttach: true }]);
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 2);

  gate.resolve({ ok: false, code: "listener_attach_failed", status: { listenerRegistered: false } });
  await assert.rejects(attaching, { code: "listener_attach_failed" });
  assert.equal(harness.bridge.status().attachUncertain, true);
  assert.equal(harness.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(harness.bridge.status().counts.queued20001, 0);
  assert.equal(harness.calls.some((call) => call.kind === "forward"), false);
  await assert.rejects(harness.bridge.attach(), { code: "listener_attach_state_uncertain" });
  await harness.bridge.stop();
});

test("pre-attach count and byte overflow fail closed without exposing output values", async () => {
  const countEvents = [];
  const countLimited = makeHarness({ maxForwardQueue: 2, onStatus: (event) => countEvents.push(event) });
  countLimited.bridge.prepareSubscriptions();
  await countLimited.host.emitOutput(20050, 0, { marker: "PRIVATE_COUNT_MARKER" });
  await countLimited.host.emitOutput(20050, 0, { marker: "PRIVATE_COUNT_MARKER" });
  await assert.rejects(countLimited.host.emitOutput(20050, 0, { marker: "PRIVATE_COUNT_MARKER" }), {
    code: "pre_attach_output_overflow",
  });
  assert.equal(countLimited.bridge.status().controllerInvalidated, true);
  assert.equal(countLimited.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(countLimited.bridge.status().firstRejectedOutput.reasonCode, "pre_attach_output_overflow");
  assert.equal(JSON.stringify(countEvents).includes("PRIVATE_COUNT_MARKER"), false);
  assert.equal(JSON.stringify(countLimited.bridge.status()).includes("PRIVATE_COUNT_MARKER"), false);
  await countLimited.bridge.stop();

  const byteEvents = [];
  const byteLimited = makeHarness({ maxForwardQueueBytes: 1024, onStatus: (event) => byteEvents.push(event) });
  byteLimited.bridge.prepareSubscriptions();
  await assert.rejects(byteLimited.host.emitOutput(20050, 0, { marker: `PRIVATE_BYTE_MARKER_${"x".repeat(1200)}` }), {
    code: "pre_attach_output_overflow",
  });
  assert.equal(byteLimited.bridge.status().controllerInvalidated, true);
  assert.equal(byteLimited.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(byteLimited.bridge.status().firstRejectedOutput.reasonCode, "pre_attach_output_overflow");
  assert.equal(JSON.stringify(byteEvents).includes("PRIVATE_BYTE_MARKER"), false);
  assert.equal(JSON.stringify(byteLimited.bridge.status()).includes("PRIVATE_BYTE_MARKER"), false);
  await byteLimited.bridge.stop();
});

test("confirmed Host/provider loss and stop clear pre-attach data while a stale frame is isolated", async () => {
  const hostLoss = makeHarness();
  hostLoss.bridge.prepareSubscriptions();
  await hostLoss.host.emitOutput(20050, 0, { kind: "host-loss" });
  await hostLoss.host.setAvailable(false);
  assert.equal(hostLoss.bridge.status().hostInvalidated, true);
  assert.equal(hostLoss.bridge.status().counts.preAttachStoredOutputs, 0);
  await hostLoss.bridge.stop();

  const providerLoss = makeHarness({ providerInitiallyConnected: false, providerInitiallyAuthenticated: false });
  providerLoss.bridge.prepareSubscriptions();
  await providerLoss.host.emitOutput(20050, 0, { kind: "provider-loss" });
  await providerLoss.provider.emitStatus(true, true);
  await providerLoss.provider.emitStatus(false, false);
  assert.equal(providerLoss.bridge.status().providerInvalidated, true);
  assert.equal(providerLoss.bridge.status().counts.preAttachStoredOutputs, 0);
  await providerLoss.bridge.stop();

  const generationLoss = makeHarness();
  generationLoss.bridge.prepareSubscriptions();
  await generationLoss.host.emitOutput(20050, 0, { kind: "generation-loss" });
  await assert.rejects(generationLoss.host.emitOutput(20050, 0, { kind: "stale" }, 32), {
    code: "stale_host_generation",
  });
  assert.equal(generationLoss.bridge.status().controllerInvalidated, false,
    "an old callback is rejected without invalidating this current generation");
  assert.equal(generationLoss.bridge.status().counts.preAttachStoredOutputs, 1,
    "the old frame does not erase an earlier valid current-generation observation");
  assert.equal(generationLoss.bridge.status().firstRejectedOutput.reasonCode, "stale_host_generation");
  await generationLoss.bridge.attach();
  assert.equal(generationLoss.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(generationLoss.bridge.status().counts.unassociatedPreAttachOutputs, 1);
  await generationLoss.bridge.stop();

  const stopped = makeHarness();
  stopped.bridge.prepareSubscriptions();
  await stopped.host.emitOutput(20050, 0, { kind: "stop" });
  await stopped.bridge.stop();
  assert.equal(stopped.bridge.status().counts.preAttachStoredOutputs, 0);
  assert.equal(stopped.bridge.status().attached, false);
});

test("Bridge login emits Native's fixed empty TinyId slot", async (t) => {
  const harness = makeHarness();
  t.after(() => harness.bridge.stop());
  await harness.bridge.attach();
  await harness.bridge.login({
    selfUid: "private-self-uid",
    selfUin: "private-self-uin",
    accountPath: "C:\\private\\account",
  });
  const loginInvoke = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
  assert.deepEqual(loginInvoke.params, ["private-self-uid", "private-self-uin", "", "C:\\private\\account", ""]);
});

test("explicit login, invite, 20006 mapping, ACK correlation, fast 20001, and uncertain end", async () => {
  const { bridge, provider, host, calls } = makeHarness();
  await bridge.attach();
  const login = bridge.login({ selfUid: "private-self-uid", selfUin: "private-self-uin", accountPath: "C:\\private\\account" });
  await login;
  const loginInvoke = calls.find((call) => call.kind === "invoke" && call.command === 1);
  assert.deepEqual(loginInvoke.params, ["private-self-uid", "private-self-uin", "", "C:\\private\\account", ""]);
  assert.equal(bridge.status().loginState, "pending", "posted receipt is not native login success");

  await provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: 8001, invite_type: 3, from_uid: "caller-private-uid" }], {
    action: { command: 55, value: [8, "private-signal-payload"] },
  });
  assert.equal(bridge.status().counts.queuedSignals, 1);
  assert.deepEqual(calls.filter((call) => call.kind === "invoke").map((call) => call.command), [1], "no automatic accept and signal waits for login ACK");

  await host.emitOutput(20001, 0, [8, "private-network-payload"]);
  assert.equal(calls.some((call) => call.kind === "forward" && call.command === 20001), true, "20001 bypasses the login wait");
  await host.emitOutput(1, 0, [0, ""]);
  assert.equal(bridge.status().loginState, "pending", "id=0 ACK remains diagnostic only");
  await host.emitOutput(1, loginInvoke.id, [0, ""]);
  await waitFor(() => calls.some((call) => call.kind === "invoke" && call.command === 55));
  assert.equal(bridge.status().loginState, "ready");

  const currentInvite = inviteEvent(8001, "caller-private-uid");
  await provider.emitKernel(currentInvite.method, currentInvite.args);
  const callId = bridge.status().activeCall.callId;
  const firstCallGeneration = bridge.status().activeCall.generation;
  await provider.emitKernel(currentInvite.method, currentInvite.args);
  assert.equal(bridge.status().activeCall.generation, firstCallGeneration, "duplicate kernel invite does not replace call");
  await provider.emitKernel("onInviteActionToAVSDK", [{ relation_id: 8002, invite_type: 3, from_uid: "other-private-uid" }]);
  assert.equal(bridge.status().counts.busyInvites, 1, "other active invite is reported busy");

  await host.emitOutput(20006, 0, inviteCallback("caller-private-uid"));
  assert.equal(bridge.status().activeCall.inviteCorrelation, "kernel_from_uid_matched");
  const expectedAccept = [PRIVATE_CALL_SCENE, "caller-private-uid", ["caller-private-uid"], 9, "invite-private-field", true, "private-tail"];
  await host.emitOutput(20006, 0, inviteCallback("caller-private-uid"));
  assert.equal(bridge.status().counts.duplicateInviteCallbacks, 1);
  assert.deepEqual(calls.filter((call) => call.kind === "invoke" && call.command === 5), [], "20006 never accepts automatically");

  const accepted = await bridge.accept(callId);
  const acceptInvoke = calls.find((call) => call.kind === "invoke" && call.command === 5);
  assert.deepEqual(acceptInvoke.params, expectedAccept);
  assert.equal(bridge.status().activeCall.connected, false, "posted accept is not connected");
  await host.emitOutput(20004, 0, [0, 7, "room-private-field", "caller-private-uid", 0, "private-room-detail", 1]);
  assert.equal(bridge.status().activeCall.connected, false, "id=0 room output is not accepted without audio device opt-in");
  await host.emitOutput(20004, accepted.id, [0, "room-private-field"]);
  assert.equal(bridge.status().activeCall.connected, false, "room callback waits for matched cmd5 ACK");
  await host.emitOutput(5, accepted.id, [0, ""]);
  assert.equal(bridge.status().activeCall.connected, true);

  await provider.emitKernel("OnS2CActionToAVSDK", [{ relation_id: 8001, destroyReason: { $qqcall: "undefined" } }]);
  assert.equal(bridge.status().activeCall.phase, "connected", "serialized undefined destroyReason is not an end observation");
  await provider.emitKernel("OnS2CActionToAVSDK", [{ relation_id: 8001, destroyReason: "1" }]);
  assert.equal(bridge.status().activeCall.phase, "connected", "non-numeric destroyReason is not an end observation");
  await provider.emitKernel("OnS2CActionToAVSDK", [{ relation_id: 9000, destroyReason: 1 }]);
  assert.equal(bridge.status().activeCall.phase, "connected", "unmatched destroyReason is diagnostic only");
  await provider.emitKernel("OnS2CActionToAVSDK", [{ relation_id: 8001, destroyReason: 1 }]);
  assert.equal(bridge.status().activeCall.phase, "end_observed");
  assert.equal(bridge.status().activeCall.remoteEndedConfirmed, false);
  await provider.emitKernel("onInviteActionToAVSDK", [{ relation_id: 8003, invite_type: 3, from_uid: "third-private-uid" }]);
  assert.equal(bridge.status().counts.busyInvites, 2, "unverified Windows end keeps the single-call slot uncertain");
  const statusText = JSON.stringify(bridge.status());
  for (const secret of ["private-self-uid", "private-self-uin", "private-signal-payload", "private-network-payload", "caller-private-uid", "C:\\\\private\\\\account"]) {
    assert.equal(statusText.includes(secret), false, `status redacts ${secret}`);
  }
  await assert.rejects(bridge.hangup(), { code: "unsupported_unverified_command:hangup" });
  await assert.rejects(bridge.reject(), { code: "unsupported_unverified_command:reject" });
  assert.equal(calls.some((call) => call.kind === "invoke" && [2, 6].includes(call.command)), false);
  assert.deepEqual(await bridge.stop(), { stopped: true, detachedConfirmed: true });
  assert.deepEqual(await bridge.stop(), { stopped: true, detachedConfirmed: true });
  assert.equal(calls.filter((call) => call.kind === "detach").length, 1);
});

test("busy invites publish only proven caller identity on the private control channel with cooldown", async () => {
  let currentTime = 10_000;
  const harness = makeHarness({ now: () => currentTime, callTimeoutMs: 60_000, trustedNativeInviteProvider: true });
  const controlEvents = [];
  const statusEvents = [];
  const unsubscribeControl = harness.bridge.subscribeControlEvents((event) => controlEvents.push(event));
  const unsubscribeStatus = harness.bridge.subscribeStatus((event) => statusEvents.push(event));
  await harness.bridge.attach();

  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "active-a",
    invite_type: 3,
    from_uid: "caller-A",
  }]);
  const activeBefore = harness.bridge.status().activeCall;
  const invokesBefore = harness.calls.filter((call) => call.kind === "invoke");
  const busyArgs = [{ relation_id: "busy-b", invite_type: 1, from_uid: BUSY_CALLER_UID }];

  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  assert.equal(controlEvents.length, 1);
  assert.equal(controlEvents[0].kind, "invite_busy");
  assert.equal(controlEvents[0].busyInvite.callerUid, BUSY_CALLER_UID);
  assert.equal(controlEvents[0].busyInvite.identityCorrelation, "kernel_from_uid_matched");
  assert.equal(controlEvents[0].busyInvite.nativeInviteEvidence.fromUid, BUSY_CALLER_UID);
  assert.equal(controlEvents[0].busyInvite.nativeInviteEvidence.inviteType, 1);
  assert.equal(controlEvents[0].busyInvite.nativeInviteEvidence.listenerRegistrationSequence, 1);
  assert.equal(typeof controlEvents[0].busyInvite.inviteEventId, "string");
  assert.deepEqual(harness.bridge.status().activeCall, activeBefore, "busy invite cannot mutate the active call slot");
  assert.deepEqual(harness.calls.filter((call) => call.kind === "invoke"), invokesBefore, "busy invite never posts a Host command");
  assert.equal(JSON.stringify(statusEvents).includes(BUSY_CALLER_UID), false, "caller identity is absent from ordinary status events");
  assert.equal(JSON.stringify(harness.bridge.status()).includes(BUSY_CALLER_UID), false, "caller identity is absent from public status");

  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  assert.equal(controlEvents.length, 1, "exact duplicate busy fingerprints are suppressed during cooldown");
  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "busy-b2", invite_type: 2, from_uid: BUSY_CALLER_UID }], { provenNative: true });
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "busy-b3", invite_type: 3, from_uid: BUSY_CALLER_UID }], { provenNative: true });
  assert.equal(controlEvents.length, 1, "missing proof and unclassified types 2/3 cannot emit a private busy notice");
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "unmapped", invite_type: 3 }]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "malformed-uid", invite_type: 3, from_uid: "not-a-kernel-uid" }]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "bad-uid", invite_type: 3, from_uid: { secret: "not a UID" } }]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { generation: 20 });
  assert.equal(controlEvents.length, 1, "missing, malformed, and stale-generation identity evidence cannot emit a notice");
  assert.deepEqual(harness.bridge.status().activeCall, activeBefore);

  currentTime += 60_001;
  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  assert.equal(controlEvents.length, 2, "same fingerprint may notify again after the bounded cooldown");
  assert.equal(controlEvents[1].busyInvite.callerUid, BUSY_CALLER_UID);
  assert.equal(controlEvents[1].busyInvite.identityCorrelation, "kernel_from_uid_matched");
  assert.notEqual(controlEvents[1].busyInvite.inviteEventId, controlEvents[0].busyInvite.inviteEventId);
  assert.deepEqual(harness.bridge.status().activeCall, activeBefore);
  assert.deepEqual(harness.calls.filter((call) => call.kind === "invoke"), invokesBefore);

  unsubscribeControl();
  unsubscribeStatus();
  await harness.bridge.stop();
});

test("private hangup posts cmd 8 with the caller-proven None reason, keeps the slot through ACK, and clears only on the exact saved room end", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await connectVerifiedPrivateCall(harness);

  const posted = await harness.bridge.hangup(current.callId);
  const invoke = harness.calls.find((call) => call.kind === "invoke" && call.command === 8);
  assert.deepEqual(invoke.params, [1, 0], "quit carries private scene and None reason, never enterRelationId");
  assert.equal(posted.id, invoke.id);
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  assert.equal(harness.bridge.status().activeCall.endState, "posted");
  assert.equal(harness.bridge.status().activeCall.connected, false);
  assert.equal(harness.calls.some((call) => call.kind === "invoke" && call.command === 10), false,
    "Close is not posted until the matching successful Quit ACK arrives");

  await harness.host.emitOutput(8, 0, [0]);
  await harness.host.emitOutput(8, invoke.id + 100, [0]);
  assert.equal(harness.bridge.status().activeCall.endState, "posted", "zero and unmatched ids cannot be assigned to this call");
  assert.equal(harness.bridge.status().counts.uncorrelatedAcks >= 2, true);

  await harness.host.emitOutput(8, invoke.id, [0, ""]);
  await waitFor(() => harness.calls.some((call) => call.kind === "invoke" && call.command === 10));
  const close = harness.calls.find((call) => call.kind === "invoke" && call.command === 10);
  assert.deepEqual(close.params, [1, current.peerUid, 0], "Close receives private scene, exact original caller UID, and default self-close reason");
  assert.equal(close.id > 0 && close.id !== invoke.id, true, "Close has a distinct positive request ID");
  assert.deepEqual(harness.calls.filter((call) => call.kind === "invoke" && [8, 10].includes(call.command))
    .map((call) => call.command), [8, 10], "the verified private end stages post in order");
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  await waitFor(() => harness.bridge.status().activeCall?.endState === "hangup_close_posted");
  await harness.host.emitOutput(10, 0, [0, ""]);
  await harness.host.emitOutput(10, close.id + 100, [0, ""]);
  await assert.rejects(harness.host.emitOutput(10, close.id, [0, ""], 32), { code: "stale_host_generation" });
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_posted",
    "zero, mismatched, and stale-generation Close ACKs are not associated");
  await harness.host.emitOutput(10, close.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_acknowledged");
  assert.equal(harness.bridge.status().activeCall.callId, current.callId, "neither private end ACK frees the call slot");
  await harness.host.emitOutput(10, close.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_acknowledged", "a duplicate Close ACK cannot alter the consumed stage");

  await harness.host.emitOutput(20011, 0, [1, current.enterRelationId, "native-room"]);
  assert.equal(harness.bridge.status().activeCall, null, "only current scene plus saved enter relation is terminal");
  assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);

  const replacement = await prepareVerifiedPrivateInvite(harness, { peerUid: `u_${"R".repeat(22)}` });
  assert.notEqual(replacement.callId, current.callId, "the confirmed room end releases the slot for a new invite");
  await harness.host.emitOutput(8, invoke.id, [0]);
  await harness.host.emitOutput(10, close.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.callId, replacement.callId, "a late ACK from the prior call cannot mutate its replacement");
  assert.equal(harness.bridge.status().activeCall.phase, "invite_ready");
  await harness.bridge.stop();
});

test("real HostClient Close business timeout preserves hangup occupancy without invalidating a healthy Host", async (t) => {
  const hostGeneration = 73;
  const connectionGeneration = 27;
  const token = "T".repeat(43);
  const listenPort = await unusedLoopbackPort();
  const client = createHostClient({
    token,
    listenPort,
    outputPort: 0,
    hostGeneration,
    requestTimeoutMs: 120,
    outputTimeoutMs: 1000,
    healthPollMs: 10_000,
  });
  const closeReceipt = deferred();
  const closeInvokeStarted = deferred();
  let transport = null;
  let harness = null;
  t.after(async () => {
    if (harness) await harness.bridge.stop().catch(() => {});
    if (transport) await transport.stop().catch(() => {});
    await client.stop().catch(() => {});
  });

  await client.start();
  transport = createHostTransport({
    token,
    listenPort,
    outputPort: client.status().outputPort,
    hostGeneration,
    requestTimeoutMs: 2000,
    verifiedPrivateEndProtocol: true,
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    invokePlugin: async ({ command, id }) => {
      if (command === 10) {
        closeInvokeStarted.resolve();
        return closeReceipt.promise;
      }
      return { posted: true, id, command };
    },
  });
  await transport.start();
  let healthy = false;
  for (let attempt = 0; attempt < 30 && !healthy; attempt += 1) {
    healthy = await client.checkHealth();
    if (!healthy) await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(healthy, true);

  harness = makeHarness({
    connectionGeneration,
    hostGeneration,
    hostOverride: client,
    hostOutput: (command, id, value, generation) => transport.publishOutput({
      command,
      id,
      value,
      hostGeneration: generation,
    }),
    hostSelect: async (input) => ({ selected: true, id: input.id, command: 102 }),
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
  });
  const current = await connectVerifiedPrivateCall(harness);
  await harness.bridge.hangup(current.callId);
  const quit = harness.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await harness.host.emitOutput(8, quit.id, [0, ""]);
  await closeInvokeStarted.promise;

  await waitFor(() => harness.bridge.status().activeCall?.endState === "hangup_close_post_unconfirmed", 1000);
  const disconnected = harness.bridge.status();
  assert.equal(disconnected.hostAvailable, true);
  assert.equal(disconnected.hostInvalidated, false, "a business RPC timeout is not Host loss");
  assert.equal(disconnected.activeCall.callId, current.callId);
  assert.equal(disconnected.activeCall.phase, "ending_unconfirmed");
  assert.equal(disconnected.activeCall.endState, "hangup_close_post_unconfirmed");
  assert.equal(disconnected.activeCall.uncertain, true);
  assert.equal(disconnected.counts.pendingAcks, 0);

  closeReceipt.resolve({ posted: true, id: harness.calls.find((call) => call.kind === "invoke" && call.command === 10).id, command: 10 });
  await waitFor(() => transport.status().invokePosted === 4, 1000);
  const afterLateReceipt = harness.bridge.status();
  assert.equal(afterLateReceipt.activeCall.callId, current.callId);
  assert.equal(afterLateReceipt.activeCall.phase, "ending_unconfirmed");
  assert.equal(afterLateReceipt.activeCall.endState, "hangup_close_post_unconfirmed");
  assert.notEqual(afterLateReceipt.lastCall?.roomDestroyedConfirmed, true,
    "late HTTP success is not terminal proof and cannot resurrect or rewrite the call");
});

test("successful cmd 8 reserves Close synchronously, while neither stage awaits or rolls back on the other's receipt", async () => {
  const quitReceipt = deferred();
  const closeReceipt = deferred();
  const closeDispatchStarted = deferred();
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, _host, options) => {
      if ([8, 10].includes(input.command)) assert.equal(options.assertCurrent(), true);
      if (input.command === 8) return quitReceipt.promise;
      if (input.command === 10) {
        closeDispatchStarted.resolve();
        return closeReceipt.promise;
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const current = await connectVerifiedPrivateCall(harness);
  const hangupResult = harness.bridge.hangup(current.callId).then(
    (value) => ({ value }),
    (error) => ({ error }),
  );
  await waitFor(() => harness.calls.some((call) => call.kind === "invoke" && call.command === 8));
  const quit = harness.calls.find((call) => call.kind === "invoke" && call.command === 8);

  // This host output handler must finish while the separate command-10
  // receipt remains pending; the bridge callback HTTP route waits for it.
  await harness.host.emitOutput(8, quit.id, [0, ""]);
  await closeDispatchStarted.promise;
  const close = harness.calls.find((call) => call.kind === "invoke" && call.command === 10);
  assert.deepEqual(close.params, [1, current.peerUid, 0]);
  assert.equal(close.id !== quit.id, true);
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_posting");

  quitReceipt.reject(Object.assign(new Error("late_quit_receipt_error"), { code: "host_unavailable" }));
  const quitOutcome = await hangupResult;
  assert.equal(quitOutcome.error?.code, "host_unavailable");
  assert.equal(harness.bridge.status().activeCall.callId, current.callId);
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_posting",
    "a late cmd 8 HTTP error cannot roll back the reserved Close stage");

  await harness.host.emitOutput(10, close.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_acknowledged",
    "a matched Close ACK can arrive before its Host receipt");
  closeReceipt.resolve({ posted: true, id: close.id, command: 10 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_acknowledged",
    "the delayed Close receipt cannot overwrite the ACK state");
  assert.equal(harness.bridge.status().activeCall.callId, current.callId,
    "both successful SDK ACKs remain nonterminal");
  await harness.bridge.stop();
});

test("Close dispatch rechecks current private identity and a terminal event cannot be undone by a late receipt", async () => {
  const identityGate = deferred();
  let actualPosts = 0;
  const staleProof = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, _host, options) => {
      if (input.command !== 10) {
        if (input.command === 8) assert.equal(options.assertCurrent(), true);
        return { posted: true, id: input.id, command: input.command };
      }
      await identityGate.promise;
      if (options.assertCurrent() !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
      actualPosts += 1;
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const staleCall = await connectVerifiedPrivateCall(staleProof);
  await staleProof.bridge.hangup(staleCall.callId);
  const quit = staleProof.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await staleProof.host.emitOutput(8, quit.id, [0, ""]);
  await waitFor(() => staleProof.calls.some((call) => call.kind === "invoke" && call.command === 10));
  staleProof.provider.setNativeInviteBinding(null);
  identityGate.resolve();
  await waitFor(() => staleProof.bridge.status().activeCall?.phase === "ending_unconfirmed");
  assert.equal(actualPosts, 0, "a retired native listener proof blocks the queued Close post");
  assert.equal(staleProof.bridge.status().activeCall.callId, staleCall.callId);
  assert.equal(staleProof.bridge.status().activeCall.uncertain, true);
  await staleProof.bridge.stop();

  const queuedPostGate = deferred();
  const dispatchChecked = deferred();
  let terminalRacePosts = 0;
  const terminalBeforePost = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, _host, options) => {
      if (input.command === 10) {
        await queuedPostGate.promise;
        const currentAtDispatch = options.assertCurrent();
        dispatchChecked.resolve(currentAtDispatch);
        if (currentAtDispatch !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
        terminalRacePosts += 1;
      } else if (input.command === 8) {
        assert.equal(options.assertCurrent(), true);
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const terminalCall = await connectVerifiedPrivateCall(terminalBeforePost);
  await terminalBeforePost.bridge.hangup(terminalCall.callId);
  const terminalQuit = terminalBeforePost.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await terminalBeforePost.host.emitOutput(8, terminalQuit.id, [0, ""]);
  await waitFor(() => terminalBeforePost.calls.some((call) => call.kind === "invoke" && call.command === 10));
  await terminalBeforePost.host.emitOutput(20011, 0, [1, terminalCall.enterRelationId, "native-room"]);
  assert.equal(terminalBeforePost.bridge.status().activeCall, null);
  queuedPostGate.resolve();
  assert.equal(await dispatchChecked.promise, false, "terminal proof invalidates the queued Close immediately before Host post");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(terminalRacePosts, 0);
  assert.equal(terminalBeforePost.bridge.status().activeCall, null);
  assert.equal(terminalBeforePost.bridge.status().lastCall.roomDestroyedConfirmed, true,
    "the blocked Close cannot replace terminal proof with an uncertain state");
  await terminalBeforePost.bridge.stop();

  const receiptGate = deferred();
  const receiptStarted = deferred();
  const terminalRace = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, _host, options) => {
      if ([8, 10].includes(input.command)) assert.equal(options.assertCurrent(), true);
      if (input.command === 10) {
        receiptStarted.resolve();
        return receiptGate.promise;
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const ending = await connectVerifiedPrivateCall(terminalRace);
  await terminalRace.bridge.hangup(ending.callId);
  const quitRequest = terminalRace.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await terminalRace.host.emitOutput(8, quitRequest.id, [0, ""]);
  await receiptStarted.promise;
  await terminalRace.host.emitOutput(20011, 0, [1, ending.enterRelationId, "native-room"]);
  assert.equal(terminalRace.bridge.status().activeCall, null);
  assert.equal(terminalRace.bridge.status().lastCall.roomDestroyedConfirmed, true);
  receiptGate.resolve({ posted: true, id: terminalRace.calls.find((call) => call.kind === "invoke" && call.command === 10).id, command: 10 });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(terminalRace.bridge.status().activeCall, null, "the late second-stage receipt cannot resurrect a terminal call");
  assert.equal(terminalRace.bridge.status().lastCall.roomDestroyedConfirmed, true);
  await terminalRace.bridge.stop();
});

test("invalid Close ACK and Close ACK timeout keep a private hangup occupied and unconfirmed", async () => {
  const invalid = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await connectVerifiedPrivateCall(invalid);
  await invalid.bridge.hangup(current.callId);
  const quit = invalid.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await invalid.host.emitOutput(8, quit.id, [0, ""]);
  await waitFor(() => invalid.calls.some((call) => call.kind === "invoke" && call.command === 10));
  const close = invalid.calls.find((call) => call.kind === "invoke" && call.command === 10);
  await invalid.host.emitOutput(10, close.id, [0, "unexpected"]);
  assert.equal(invalid.bridge.status().activeCall.callId, current.callId);
  assert.equal(invalid.bridge.status().activeCall.phase, "ending_unconfirmed");
  assert.equal(invalid.bridge.status().activeCall.endState, "hangup_close_ack_invalid");
  assert.equal(invalid.bridge.status().activeCall.uncertain, true);
  await invalid.bridge.stop();

  const timeout = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    privateEndAckTimeoutMs: 100,
  });
  const timeoutCall = await connectVerifiedPrivateCall(timeout);
  await timeout.bridge.hangup(timeoutCall.callId);
  const timeoutQuit = timeout.calls.find((call) => call.kind === "invoke" && call.command === 8);
  await timeout.host.emitOutput(8, timeoutQuit.id, [0, ""]);
  await waitFor(() => timeout.bridge.status().lastFailureCode === "hangup_close_ack_timeout");
  assert.equal(timeout.bridge.status().activeCall.callId, timeoutCall.callId);
  assert.equal(timeout.bridge.status().activeCall.phase, "ending_unconfirmed");
  assert.equal(timeout.bridge.status().activeCall.uncertain, true);
  await timeout.bridge.stop();
});

test("private reject uses cmd 9 Default but an ACK remains unconfirmed and occupied", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await prepareVerifiedPrivateInvite(harness);

  const posted = await harness.bridge.reject(current.callId);
  const invoke = harness.calls.find((call) => call.kind === "invoke" && call.command === 9);
  assert.deepEqual(invoke.params, [1, current.peerUid, 1]);
  assert.equal(posted.id, invoke.id);
  assert.equal(harness.bridge.status().activeCall.endState, "posted");

  await harness.host.emitOutput(9, 0, { opaque: "unmatched" });
  assert.equal(harness.bridge.status().activeCall.phase, "reject_posted", "a zero-id response is never attributed to this invite");
  await harness.host.emitOutput(9, invoke.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.phase, "reject_unconfirmed");
  assert.equal(harness.bridge.status().activeCall.endState, "reject_acknowledged_unconfirmed");
  assert.equal(harness.bridge.status().activeCall.uncertain, true);
  assert.equal(harness.bridge.status().activeCall.callId, current.callId);
  await assert.rejects(harness.bridge.accept(current.callId), { code: "call_not_accepting" });
  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs("unconfirmed-room", current.peerUid, {
    relationId: "private-invite-relation",
  }));
  assert.equal(harness.bridge.status().activeCall.phase, "reject_unconfirmed", "the invite relation alone cannot promote a reject into terminal proof");
  await harness.host.emitOutput(20011, 0, [1, "private-invite-callback-relation", "not-an-entered-room"]);
  assert.equal(harness.bridge.status().activeCall.callId, current.callId, "invite relation is not a saved enter relation");
  await harness.bridge.stop();
});

test("verified Kernel end also waits for current Host room absence before releasing the shared private slot", async () => {
  for (const scenario of ["confirmed", "still_present", "binding_changed", "sdk_terminal_race"]) {
    const gate = deferred();
    let requested = false;
    const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, trustedNativeInviteProvider: true,
      verifiedPrivateEndProtocol: true, hostKernelRoomAbsence: async (input, options) => {
        assert.equal(options.assertCurrent(), true);
        requested = true;
        await gate.promise;
        return { ok: true, ...input, confirmed: scenario !== "still_present", source: "native_entered_private_room_absence" };
      } });
    const current = await connectVerifiedPrivateCall(harness);
    await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(current.enterRelationId, current.peerUid));
    await waitFor(() => requested);
    assert.equal(harness.bridge.status().activeCall.phase, "ending_kernel_verified");
    if (scenario === "sdk_terminal_race") {
      await harness.host.emitOutput(20011, 0, [1, current.enterRelationId, "native-ended-room"]);
      assert.equal(harness.bridge.status().activeCall, null);
    }
    if (scenario === "binding_changed") harness.provider.setNativeInviteBinding(null);
    gate.resolve();
    await new Promise(resolve => setImmediate(resolve));
    if (["confirmed", "sdk_terminal_race"].includes(scenario)) assert.equal(harness.bridge.status().activeCall, null);
    else assert.equal(harness.bridge.status().activeCall.callId, current.callId);
    await harness.bridge.stop();
  }
});

test("private rejection releases only after current typed Host absence proof, including an ACK before post receipt", async () => {
  for (const scenario of ["confirmed", "cold_confirmed", "early_ack", "absent", "wrong_request", "binding_changed"]) {
    let proofChecks = 0;
    const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, trustedNativeInviteProvider: true,
      verifiedPrivateEndProtocol: true,
      ...(scenario === "early_ack" ? { hostInvoke: async (input, host, options) => {
        if (options?.assertCurrent) assert.equal(options.assertCurrent(), true);
        if (input.command === 9) await host.emitOutput(9, input.id, [0, ""]);
        return { posted: true, id: input.id, command: input.command };
      } } : {}),
      hostRejectAbsence: async (input, options) => {
        proofChecks += 1;
        assert.equal(options.assertCurrent(), true);
        if (scenario === "binding_changed") harness.provider.setNativeInviteBinding(null);
        return { ok: true, ...input, rejectRequestId: input.rejectRequestId + (scenario === "wrong_request" ? 1 : 0),
          confirmed: scenario !== "absent", source: scenario === "cold_confirmed"
            ? "native_reject_ack_and_no_room_admission" : "native_reject_ack_and_sdk_room_absence" };
      } });
    const current = await prepareVerifiedPrivateInvite(harness);
    const posted = await harness.bridge.reject(current.callId);
    if (scenario !== "early_ack") await harness.host.emitOutput(9, posted.id, [0, ""]);
    await waitFor(() => proofChecks === 1);
    await new Promise(resolve => setImmediate(resolve));
    if (["confirmed", "cold_confirmed", "early_ack"].includes(scenario)) {
      assert.equal(harness.bridge.status().activeCall, null);
      assert.equal(harness.bridge.status().lastCall.invitationRejectedConfirmed, true);
      assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, undefined);
      assert.equal(harness.bridge.status().lastCall.remoteEndedConfirmed, false);
      if (scenario === "cold_confirmed") assert.equal(harness.bridge.status().lastCall.terminalReason, "native_reject_before_any_room_admission");
    } else assert.equal(harness.bridge.status().activeCall.callId, current.callId);
    await harness.bridge.stop();
  }
});

test("official invitation cleanup must finish before fresh Host rejection absence can release ownership", async () => {
  const { validRequest } = require("../agent/official-private-invite.cjs");
  for (const scenario of ["confirmed", "receiving_closed", "unproven", "binding_changed", "wrong_completion_kind", "throwing"]) {
    const gate = deferred();
    let officialCalls = 0, absenceCalls = 0;
    const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, trustedNativeInviteProvider: true,
      verifiedPrivateEndProtocol: true,
      officialPrivateInviteLifecycle: async body => {
        officialCalls += 1;
        assert.equal(validRequest(body), true);
        assert.equal(body.operation, "reject");
        await gate.promise;
        if (scenario === "throwing") throw new Error("sensitive lifecycle exception");
        return { confirmed: scenario !== "unproven", code: scenario === "wrong_completion_kind"
          ? "private_invite_lifecycle_retired" : scenario === "receiving_closed"
            ? "official_private_invite_receiving_closed" : "official_private_invite_document_ended" };
      },
      hostRejectAbsence: async (input, options) => {
        absenceCalls += 1; assert.equal(options.assertCurrent(), true);
        return { ok: true, ...input, confirmed: true, source: "native_reject_ack_and_sdk_room_absence" };
      } });
    await harness.bridge.attach();
    await harness.bridge.login({ selfUid: `u_${"S".repeat(22)}`, selfUin: "10001", accountPath: "private" });
    await harness.host.emitOutput(1, harness.calls.find(call => call.command === 1).id, [0, ""]);
    const current = await prepareVerifiedPrivateInvite(harness);
    const posted = await harness.bridge.reject(current.callId);
    await harness.host.emitOutput(9, posted.id, [0, ""]);
    await waitFor(() => officialCalls === 1);
    assert.equal(absenceCalls, 0);
    assert.equal(harness.bridge.status().activeCall.callId, current.callId);
    if (scenario === "binding_changed") harness.provider.setNativeInviteBinding(null);
    gate.resolve();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(absenceCalls, ["confirmed", "receiving_closed"].includes(scenario) ? 1 : 0);
    if (["confirmed", "receiving_closed"].includes(scenario)) assert.equal(harness.bridge.status().activeCall, null);
    else assert.equal(harness.bridge.status().activeCall.callId, current.callId);
    assert.equal(JSON.stringify(harness.bridge.status()).includes("sensitive lifecycle exception"), false);
    await harness.bridge.stop();
  }
});

test("private end validates correlated cmd 8 and 9 result tuples and retains uncertain calls until terminal proof", async () => {
  const cases = [
    { operation: "hangup", value: [1, "native error"], endState: "hangup_failed_unconfirmed", failure: "native_hangup_failed" },
    { operation: "hangup", value: [0, "unexpected"], endState: "hangup_ack_invalid_unconfirmed", failure: "private_end_ack_invalid" },
    { operation: "reject", value: [1, "native error"], endState: "reject_failed_unconfirmed", failure: "native_reject_failed" },
    { operation: "reject", value: { opaque: true }, endState: "reject_ack_invalid_unconfirmed", failure: "private_end_ack_invalid" },
  ];

  for (const scenario of cases) {
    const harness = makeHarness({
      audioDeviceTarget: AUDIO_DEVICE_TARGET,
      trustedNativeInviteProvider: true,
      verifiedPrivateEndProtocol: true,
    });
    const current = scenario.operation === "hangup"
      ? await connectVerifiedPrivateCall(harness)
      : await prepareVerifiedPrivateInvite(harness);
    await harness.bridge[scenario.operation](current.callId);
    const command = scenario.operation === "hangup" ? 8 : 9;
    const invoke = harness.calls.find((call) => call.kind === "invoke" && call.command === command);

    await harness.host.emitOutput(command, invoke.id, scenario.value);
    const active = harness.bridge.status().activeCall;
    assert.equal(active.callId, current.callId, `${scenario.operation} result cannot release the slot`);
    assert.equal(active.endState, scenario.endState);
    assert.equal(active.phase, scenario.operation === "hangup" ? "ending_unconfirmed" : "reject_unconfirmed");
    assert.equal(active.connected, false);
    assert.equal(active.uncertain, true);
    assert.equal(harness.bridge.status().lastFailureCode, scenario.failure);
    assert.equal(harness.bridge.status().counts.pendingAcks, 0);
    assert.equal(harness.calls.some((call) => call.kind === "invoke" && call.command === 10), false,
      "failed, malformed, or ambiguous cmd 8 ACKs never reserve Close");

    if (scenario.operation === "hangup") {
      await harness.host.emitOutput(20011, 0, [1, current.enterRelationId, "native-room"]);
      assert.equal(harness.bridge.status().activeCall, null, "a later matching room-destroy event remains terminal proof");
      assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);
    }
    await harness.bridge.stop();
  }
});

test("correlated scene-3 invite releases only the unaccepted private placeholder without joining", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    kernelPeerUidByUin: { [MAPPED_PEER_UIN]: MAPPED_PEER_UID },
    trustedNativeInviteProvider: true,
  });
  await harness.bridge.attach();
  await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await harness.host.emitOutput(1, login.id, [0, ""]);

  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: MAPPED_PEER_UIN,
    invite_type: 1,
    from_uid: "",
  }], { provenNative: true });
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(3, MAPPED_PEER_UID, MAPPED_PEER_UIN));

  const current = harness.bridge.getIntegrationSnapshot().call;
  assert.equal(current, null);
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.getCurrentGroupScope(), null);
  await assert.rejects(harness.bridge.accept(callId), { code: "call_not_current" });
  assert.equal(harness.calls.some((call) => call.kind === "select_devices"), false,
    "scene validation runs before audio device selection");
  assert.equal(harness.calls.some((call) => call.kind === "invoke" && [5, 102].includes(call.command)), false,
    "a group-scene callback cannot post private accept or device-selection commands");
  await harness.bridge.stop();
});

test("a group callback cannot poison an unrelated private invitation", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const peer = MAPPED_PEER_UID;
  await harness.bridge.attach();
  await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = harness.calls.find(c => c.kind === "invoke" && c.command === 1);
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 1, from_uid: peer }]);
  const id = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(3, peer, "722852338"));
  assert.equal(harness.bridge.status().activeCall.callId, id);
  assert.equal(harness.bridge.status().activeCall.phase, "invite_observed");
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(1, peer, "private-callback"));
  assert.equal(harness.bridge.status().activeCall.phase, "invite_ready");
  await harness.bridge.stop();
});

test("late group classification works without a private UID map and never grants scope", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, callTimeoutMs: 100 });
  await harness.bridge.attach();
  await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = harness.calls.find(c => c.kind === "invoke" && c.command === 1);
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "722852338", invite_type: 3, from_uid: "" }]);
  await new Promise(resolve => setTimeout(resolve, 120));
  assert.equal(harness.bridge.status().activeCall.phase, "expired");
  await harness.host.emitOutput(20006, 0, matchedInviteCallback(3, MAPPED_PEER_UID, "722852338"));
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.getCurrentGroupScope(), null);
  assert.equal(harness.calls.some(c => c.kind === "invoke" && [4, 5, 8, 9, 10, 102].includes(c.command)), false);
  await harness.bridge.stop();
});

test("private end refuses group scenes, missing native profile proof, stale proof, and wrong call identity", async () => {
  const groupScene = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const groupInvite = await prepareVerifiedPrivateInvite(groupScene, { scene: 3 });
  await assert.rejects(groupScene.bridge.reject(groupInvite.callId), { code: "unsupported_unverified_command:reject" });
  assert.equal(groupScene.calls.some((call) => call.kind === "invoke" && [8, 9].includes(call.command)), false);
  await groupScene.bridge.stop();

  const noProof = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: false,
    verifiedPrivateEndProtocol: true,
  });
  const unproven = await connectVerifiedPrivateCall(noProof);
  await assert.rejects(noProof.bridge.hangup(unproven.callId), { code: "private_scope_unverified" });
  assert.equal(noProof.bridge.status().activeCall.phase, "connected");
  assert.equal(noProof.calls.some((call) => call.kind === "invoke" && call.command === 8), false);
  await noProof.bridge.stop();

  const staleProof = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const staleInvite = await prepareVerifiedPrivateInvite(staleProof);
  staleProof.provider.setNativeInviteBinding(null);
  await assert.rejects(staleProof.bridge.reject(staleInvite.callId), { code: "private_scope_unverified" });
  assert.equal(staleProof.bridge.status().activeCall.phase, "invite_ready");
  assert.equal(staleProof.calls.some((call) => call.kind === "invoke" && call.command === 9), false);
  await staleProof.bridge.stop();

  const wrongCaller = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const connected = await connectVerifiedPrivateCall(wrongCaller);
  await assert.rejects(wrongCaller.bridge.hangup("different-call-id"), { code: "call_not_current" });
  assert.equal(wrongCaller.bridge.status().activeCall.phase, "connected");
  assert.equal(wrongCaller.calls.some((call) => call.kind === "invoke" && call.command === 8), false);
  await wrongCaller.bridge.stop();
});

test("private end rejects accept-in-flight and rechecks the captured profile at actual Host dispatch", async () => {
  const selectionGate = deferred();
  const accepting = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostSelect: async (input) => selectionGate.promise.then(() => ({ selected: true, id: input.id, command: 102 })),
  });
  const invite = await prepareVerifiedPrivateInvite(accepting);
  const acceptPromise = accepting.bridge.accept(invite.callId);
  await waitFor(() => accepting.bridge.status().activeCall?.acceptState === "selecting_devices");
  await assert.rejects(accepting.bridge.reject(invite.callId), { code: "call_not_accepting" });
  assert.equal(accepting.calls.some((call) => call.kind === "invoke" && call.command === 9), false);
  selectionGate.resolve();
  await acceptPromise;
  await accepting.bridge.stop();

  const dispatchGate = deferred();
  let reachedHostPost = 0;
  const staleAtDispatch = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, _host, options) => {
      if (input.command === 8) {
        await dispatchGate.promise;
        if (options.assertCurrent() !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
        reachedHostPost += 1;
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const current = await connectVerifiedPrivateCall(staleAtDispatch);
  const hangup = staleAtDispatch.bridge.hangup(current.callId);
  await waitFor(() => staleAtDispatch.bridge.status().activeCall?.phase === "ending");
  staleAtDispatch.provider.setNativeInviteBinding(null);
  dispatchGate.resolve();
  await assert.rejects(hangup, { code: "private_scope_unverified" });
  assert.equal(reachedHostPost, 0, "a stale listener binding stops the queued cmd 8 before PPAPI");
  assert.equal(staleAtDispatch.bridge.status().activeCall.phase, "connected");
  assert.equal(staleAtDispatch.bridge.status().activeCall.endState, undefined);
  await staleAtDispatch.bridge.stop();
});

test("connected private calls cannot be rejected and invite-ready calls cannot be hung up", async () => {
  const connected = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await connectVerifiedPrivateCall(connected);
  await assert.rejects(connected.bridge.reject(current.callId), { code: "call_not_accepting" });
  assert.equal(connected.calls.some((call) => call.kind === "invoke" && call.command === 9), false);
  await connected.bridge.stop();

  const inviteOnly = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const invite = await prepareVerifiedPrivateInvite(inviteOnly);
  await assert.rejects(inviteOnly.bridge.hangup(invite.callId), { code: "call_not_connected" });
  assert.equal(inviteOnly.calls.some((call) => call.kind === "invoke" && call.command === 8), false);
  await inviteOnly.bridge.stop();
});

test("unverified Host capability is typed and restores the current private call without claiming a post", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: false,
  });
  const current = await connectVerifiedPrivateCall(harness);
  await assert.rejects(harness.bridge.hangup(current.callId), { code: "unsupported_unverified_command:hangup" });
  assert.equal(harness.bridge.status().activeCall.phase, "connected");
  assert.equal(harness.bridge.status().activeCall.endState, undefined);
  assert.equal(harness.bridge.status().counts.pendingAcks, 0);
  await harness.bridge.stop();
});

test("private end timeout ignores late ACKs but still accepts the current exact room-destroy proof", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    privateEndAckTimeoutMs: 100,
  });
  const current = await connectVerifiedPrivateCall(harness);
  const posted = await harness.bridge.hangup(current.callId);
  await waitFor(() => harness.bridge.status().activeCall?.endState === "hangup_ack_timeout");
  assert.equal(harness.bridge.status().activeCall.phase, "ending_unconfirmed");
  assert.equal(harness.bridge.status().activeCall.uncertain, true);
  const timeoutState = harness.bridge.status().activeCall;
  await harness.host.emitOutput(8, posted.id, [0]);
  assert.equal(harness.bridge.status().activeCall.endState, timeoutState.endState, "a timed-out request id cannot revive ACK state");
  assert.equal(harness.bridge.status().activeCall.phase, timeoutState.phase);
  await harness.host.emitOutput(20011, 0, [1, current.enterRelationId, "native-room"]);
  assert.equal(harness.bridge.status().activeCall, null, "a current terminal room event remains authoritative after ACK timeout");
  await harness.bridge.stop();
});

test("a posted end request cannot be acknowledged or cleared after its native profile binding goes stale", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await connectVerifiedPrivateCall(harness);
  const posted = await harness.bridge.hangup(current.callId);
  harness.provider.setNativeInviteBinding(null);
  await harness.host.emitOutput(8, posted.id, [0]);
  assert.equal(harness.bridge.status().activeCall.endState, "posted");
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  await harness.host.emitOutput(20011, 0, [1, current.enterRelationId, "native-room"]);
  assert.equal(harness.bridge.status().activeCall.callId, current.callId);
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  await harness.bridge.stop();
});

test("Kernel room teardown ends an ending call only when its saved enter relation and peer match", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
  });
  const current = await connectVerifiedPrivateCall(harness);
  await harness.bridge.hangup(current.callId);
  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs("unrelated-room", current.peerUid));
  assert.equal(harness.bridge.status().activeCall.phase, "ending", "unrelated Kernel room data cannot clear or mutate hangup state");
  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(current.enterRelationId, current.peerUid));
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);
  await harness.bridge.stop();
});

test("an ACK arriving before the post receipt is not regressed to posted", async () => {
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input, host, options) => {
      if (input.command === 8) {
        assert.equal(options.assertCurrent(), true);
        await host.emitOutput(8, input.id, [0, ""]);
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  const current = await connectVerifiedPrivateCall(harness);
  await harness.bridge.hangup(current.callId);
  await waitFor(() => harness.calls.some((call) => call.kind === "invoke" && call.command === 10) &&
    harness.bridge.status().activeCall?.endState === "hangup_close_posted");
  assert.equal(harness.bridge.status().activeCall.phase, "ending");
  assert.equal(harness.bridge.status().activeCall.endState, "hangup_close_posted",
    "the late first-stage receipt does not replace second-stage progress");
  assert.equal(harness.bridge.status().activeCall.callId, current.callId);
  await harness.bridge.stop();
});

test("disconnect and stop races retain an occupied call and prevent a delayed Host post receipt from reviving it", async () => {
  const deferredHostPost = deferred();
  const disconnected = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input) => input.command === 8
      ? deferredHostPost.promise
      : { posted: true, id: input.id, command: input.command },
  });
  const current = await connectVerifiedPrivateCall(disconnected);
  const pendingHangup = disconnected.bridge.hangup(current.callId);
  await disconnected.host.setAvailable(false);
  const disconnectedState = disconnected.bridge.status().activeCall;
  assert.equal(disconnectedState.phase, "failed");
  assert.equal(disconnectedState.endState, "hangup_interrupted");
  deferredHostPost.resolve({ posted: true, id: disconnected.calls.find((call) => call.kind === "invoke" && call.command === 8).id, command: 8 });
  await pendingHangup;
  assert.equal(disconnected.bridge.status().activeCall.phase, "failed");
  assert.equal(disconnected.bridge.status().activeCall.endState, "hangup_interrupted");
  await disconnected.bridge.stop();

  const stoppedHostPost = deferred();
  const stopping = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    trustedNativeInviteProvider: true,
    verifiedPrivateEndProtocol: true,
    hostInvoke: async (input) => input.command === 8
      ? stoppedHostPost.promise
      : { posted: true, id: input.id, command: input.command },
  });
  const stoppingCall = await connectVerifiedPrivateCall(stopping);
  const stoppingHangup = stopping.bridge.hangup(stoppingCall.callId);
  await stopping.bridge.stop();
  const stopState = stopping.bridge.status().activeCall;
  assert.equal(stopState.phase, "failed");
  assert.equal(stopState.endState, "hangup_interrupted");
  stoppedHostPost.resolve({
    posted: true,
    id: stopping.calls.find((call) => call.kind === "invoke" && call.command === 8).id,
    command: 8,
  });
  await stoppingHangup;
  assert.equal(stopping.bridge.status().activeCall.phase, "failed");
  assert.equal(stopping.bridge.status().activeCall.endState, "hangup_interrupted");
});

test("busy fingerprint cooldown resets when the globally occupied call slot changes", async () => {
  let currentTime = 20_000;
  const harness = makeHarness({ now: () => currentTime, callTimeoutMs: 60_000, audioDeviceTarget: AUDIO_DEVICE_TARGET, trustedNativeInviteProvider: true });
  const controlEvents = [];
  harness.bridge.subscribeControlEvents((event) => controlEvents.push(event));
  const firstCall = await connectAudioCall(harness, { peerUid: "caller-A" });
  const busyArgs = [{ relation_id: "busy-repeat", invite_type: 1, from_uid: BUSY_CALLER_UID }];
  const hostInvokesBefore = harness.calls.filter((call) => call.kind === "invoke");

  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  assert.equal(controlEvents.length, 1);
  assert.deepEqual(harness.bridge.status().activeCall.callId, firstCall.callId);
  await harness.host.emitOutput(20011, 0, [firstCall.scene, firstCall.enterRelationId, "room-one"]);
  assert.equal(harness.bridge.status().activeCall, null, "only the confirmed current room end frees the call slot");

  const secondCall = await prepareAudioInvite(harness, { peerUid: "caller-C" });
  assert.notEqual(secondCall.callId, firstCall.callId);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", busyArgs, { provenNative: true });
  assert.equal(controlEvents.length, 2, "same busy fingerprint is reportable in a new occupied slot");
  assert.deepEqual(harness.bridge.status().activeCall.callId, secondCall.callId);
  assert.deepEqual(harness.calls.filter((call) => call.kind === "invoke"), hostInvokesBefore);
  await harness.bridge.stop();
});

test("busy invite may use the Bridge's pinned UIN-to-UID mapping when Kernel UID is absent", async () => {
  const harness = makeHarness({ kernelPeerUidByUin: { [MAPPED_PEER_UIN]: MAPPED_PEER_UID }, trustedNativeInviteProvider: true });
  const controlEvents = [];
  harness.bridge.subscribeControlEvents((event) => controlEvents.push(event));
  await harness.bridge.attach();
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "active-a", invite_type: 3, from_uid: "caller-A" }]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 1, from_uid: "" }], { provenNative: true });
  assert.equal(controlEvents.length, 1);
  assert.equal(controlEvents[0].busyInvite.callerUid, MAPPED_PEER_UID);
  assert.equal(controlEvents[0].busyInvite.identityCorrelation, "kernel_relation_uid_matched");
  assert.equal(controlEvents[0].busyInvite.nativeInviteEvidence.fromUid, "");
  assert.equal(controlEvents[0].busyInvite.nativeInviteEvidence.relationId, MAPPED_PEER_UIN);
  assert.equal(JSON.stringify(harness.bridge.status()).includes(MAPPED_PEER_UID), false);
  await harness.bridge.stop();
});

test("audio-selected id=0 room result is private, single-use, and waits for the current cmd 5 ACK", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(harness);
  const enterRelationId = "new-enter-relation-private";
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  assert.equal(harness.bridge.status().activeCall.connected, false, "room result is buffered before native accept ACK");
  assert.equal(harness.bridge.status().activeCall.acceptState, "posted");

  // A duplicate cannot reset the captured relation or replace its result.
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid, 1));
  const serialized = JSON.stringify(harness.bridge.getIntegrationSnapshot());
  for (const secret of [current.relationId, enterRelationId, "private-room-detail"]) {
    assert.equal(serialized.includes(secret), false, "native room identifiers and error text stay private");
  }

  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.connected, true, "only cmd 5 success commits the buffered room result");
  assert.equal(harness.bridge.status().activeCall.acceptState, "connected");
  await harness.host.emitOutput(20011, 0, [current.scene, enterRelationId, "unrelated-room-id-is-not-a-correlation-key"]);
  assert.equal(harness.bridge.status().activeCall, null, "20011 correlates by the observed enter relation, not room_id");
  await harness.bridge.stop();
});

test("empty Kernel UID uses only the bound relation UIN map and still requires exact 20006 identity", async () => {
  for (const fromUidField of [
    {},
    { from_uid: undefined },
    { from_uid: null },
    { from_uid: "" },
  ]) {
    const map = { [MAPPED_PEER_UIN]: MAPPED_PEER_UID };
    const harness = makeHarness({
      audioDeviceTarget: AUDIO_DEVICE_TARGET,
      kernelPeerUidByUin: map,
    });
    map[MAPPED_PEER_UIN] = "u_ZZZZZZZZZZZZZZZZZZZZZZ";
    await harness.bridge.attach();
    await harness.bridge.login({ selfUid: "private-self", selfUin: "10001", accountPath: "private" });
    const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
    await harness.host.emitOutput(1, login.id, [0, ""]);
    await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
      relation_id: MAPPED_PEER_UIN,
      invite_type: 3,
      ...fromUidField,
    }]);
    const callId = harness.bridge.status().activeCall.callId;
    await harness.host.emitOutput(20006, 0, matchedInviteCallback(PRIVATE_CALL_SCENE, MAPPED_PEER_UID, MAPPED_PEER_UIN));
    const ready = harness.bridge.getIntegrationSnapshot().call;
    assert.equal(ready.phase, "invite_ready");
    assert.equal(ready.peerUid, MAPPED_PEER_UID);
    assert.equal(ready.inviteCorrelation, "kernel_relation_uid_matched");

    const accepted = await harness.bridge.accept(callId);
    await harness.host.emitOutput(5, accepted.id, [0, ""]);
    const enterRelationId = `enter-from-mapped-${accepted.id}`;
    await harness.host.emitOutput(20004, 0, roomEntry(PRIVATE_CALL_SCENE, enterRelationId, MAPPED_PEER_UID));
    assert.equal(harness.bridge.status().activeCall.phase, "connected", "the relation-derived source passes the same native room gate");
    await harness.host.emitOutput(20011, 0, [PRIVATE_CALL_SCENE, enterRelationId, "private-room"]);
    assert.equal(harness.bridge.status().lastCall.terminalReason, "native_room_destroyed");
    const bridgeStatus = JSON.stringify(harness.bridge.status());
    assert.equal(bridgeStatus.includes(MAPPED_PEER_UIN), false);
    assert.equal(bridgeStatus.includes(MAPPED_PEER_UID), false);
    assert.equal(bridgeStatus.includes(enterRelationId), false);
    await harness.bridge.stop();
  }
});

test("private call proof is permanently retired when the current native binding disappears or changes", async (t) => {
  const peerUid = `u_${"A".repeat(22)}`;
  const peerUin = "987654321";
  const harness = makeHarness({
    kernelPeerUidByUin: { [peerUin]: peerUid },
    trustedNativeInviteProvider: true,
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
  });
  t.after(() => harness.bridge.stop());
  await harness.bridge.attach();
  const login = await harness.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "fixture" });
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: peerUin,
    invite_type: 1,
    from_uid: "",
  }], { provenNative: true });
  const callId = harness.bridge.getIntegrationSnapshot().call.callId;
  await harness.host.emitOutput(20006, 0, [PRIVATE_CALL_SCENE, peerUid, "self", 9, peerUin, null, null, null, null, null, true, "tail"]);
  const current = harness.bridge.getIntegrationSnapshot().call;
  assert.equal(current.callId, callId);
  assert.equal(current.nativeInviteEvidence.listenerRegistrationSequence, 1);

  await harness.provider.setNativeInviteBinding(null);
  await harness.provider.emitStatus(true, true);
  const revoked = harness.bridge.getIntegrationSnapshot();
  assert.equal(revoked.status.connectionGeneration, 21);
  assert.equal(revoked.call.callId, callId);
  assert.equal(Object.hasOwn(revoked.call, "nativeInviteEvidence"), false,
    "a connected and authenticated generic provider does not keep stale invite evidence available");
  await assert.rejects(harness.bridge.accept(callId), { code: "private_scope_unverified" },
    "a direct Bridge/CLI accept cannot reinterpret a retired proof as a legacy manual call");
  assert.equal(harness.calls.some((call) => call.kind === "select_devices" ||
    (call.kind === "invoke" && call.command === 5)), false,
  "an accept started after proof retirement fails before device selection and cmd 5");

  await harness.provider.setNativeInviteBinding(harness.provider.initialNativeInviteBinding);
  await harness.provider.emitStatus(true, true);
  assert.equal(Object.hasOwn(harness.bridge.getIntegrationSnapshot().call, "nativeInviteEvidence"), false,
    "restoring the identical binding cannot revive evidence after a witnessed loss");

  await harness.provider.setNativeInviteBinding({
    ...harness.provider.initialNativeInviteBinding,
    listenerMutationEpoch: 3,
    listenerRegistrationSequence: 2,
  });
  await harness.provider.emitStatus(true, true);
  assert.equal(Object.hasOwn(harness.bridge.getIntegrationSnapshot().call, "nativeInviteEvidence"), false,
    "a same-connection listener replacement cannot transfer proof to the old call");
});

test("relation mapping does not override nonempty Kernel UID or fall back on invalid UID types", async () => {
  const mappedUid = MAPPED_PEER_UID;
  const nativeUid = "u_1234567890123456789012";
  const map = { [MAPPED_PEER_UIN]: mappedUid };

  const native = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, kernelPeerUidByUin: map });
  await native.bridge.attach();
  await native.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
  const login = native.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await native.host.emitOutput(1, login.id, [0, ""]);
  await native.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 3, from_uid: nativeUid }]);
  const callId = native.bridge.status().activeCall.callId;
  await native.host.emitOutput(20006, 0, matchedInviteCallback(73, mappedUid, MAPPED_PEER_UIN));
  assert.equal(native.bridge.status().activeCall.inviteCallbackReady, false, "a conflicting native UID takes precedence over the map");
  assert.equal(native.bridge.status().activeCall.callId, callId);
  await native.bridge.stop();

  const nativeMatch = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, kernelPeerUidByUin: map });
  await nativeMatch.bridge.attach();
  await nativeMatch.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
  const nativeLogin = nativeMatch.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await nativeMatch.host.emitOutput(1, nativeLogin.id, [0, ""]);
  await nativeMatch.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 3, from_uid: nativeUid }]);
  await nativeMatch.host.emitOutput(20006, 0, matchedInviteCallback(73, nativeUid, MAPPED_PEER_UIN));
  assert.equal(nativeMatch.bridge.status().activeCall.inviteCorrelation, "kernel_from_uid_matched");
  assert.equal(nativeMatch.bridge.getIntegrationSnapshot().call.peerUid, nativeUid);
  await nativeMatch.bridge.stop();

  for (const malformedUid of [17, {}, true]) {
    const malformed = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, kernelPeerUidByUin: map });
    await malformed.bridge.attach();
    await malformed.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
    const loginRequest = malformed.calls.find((call) => call.kind === "invoke" && call.command === 1);
    await malformed.host.emitOutput(1, loginRequest.id, [0, ""]);
    await malformed.provider.emitKernel("OnInviteActionToAVSDK", [{
      relation_id: MAPPED_PEER_UIN,
      invite_type: 3,
      from_uid: malformedUid,
    }]);
    const malformedCallId = malformed.bridge.status().activeCall.callId;
    await malformed.host.emitOutput(20006, 0, matchedInviteCallback(74, mappedUid, MAPPED_PEER_UIN));
    const call = malformed.bridge.getIntegrationSnapshot().call;
    assert.equal(call.callId, malformedCallId);
    assert.equal(call.inviteCallbackReady, false, "a wrong native field type cannot trigger map fallback");
    assert.equal(malformed.bridge.status().lastFailureCode, "invite_identity_unavailable");
    await malformed.bridge.stop();
  }
});

test("unmapped or noncanonical relation UIN stays uncorrelated, while repeated invite relations allow fresh entered relations", async () => {
  const map = { [MAPPED_PEER_UIN]: MAPPED_PEER_UID };
  for (const relationId of ["912345678", "0123456789", 123456789]) {
    const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, kernelPeerUidByUin: map });
    await harness.bridge.attach();
    await harness.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
    const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
    await harness.host.emitOutput(1, login.id, [0, ""]);
    await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: relationId, invite_type: 3 }]);
    const callbackRelation = typeof relationId === "string" ? relationId : String(relationId);
    await harness.host.emitOutput(20006, 0, matchedInviteCallback(75, MAPPED_PEER_UID, callbackRelation));
    assert.equal(harness.bridge.getIntegrationSnapshot().call.inviteCallbackReady, false);
    assert.equal(harness.bridge.status().lastFailureCode, "invite_identity_unavailable");
    await harness.bridge.stop();
  }

  const repeatedInvite = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, kernelPeerUidByUin: map });
  await repeatedInvite.bridge.attach();
  await repeatedInvite.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
  const login = repeatedInvite.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await repeatedInvite.host.emitOutput(1, login.id, [0, ""]);
  await repeatedInvite.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 3 }]);
  await repeatedInvite.host.emitOutput(20006, 0, matchedInviteCallback(PRIVATE_CALL_SCENE, MAPPED_PEER_UID, MAPPED_PEER_UIN));
  const firstCallId = repeatedInvite.bridge.status().activeCall.callId;
  const firstAccept = await repeatedInvite.bridge.accept(firstCallId);
  await repeatedInvite.host.emitOutput(20004, 0, roomEntry(PRIVATE_CALL_SCENE, "enter-relation-one", MAPPED_PEER_UID));
  await repeatedInvite.host.emitOutput(5, firstAccept.id, [0, ""]);
  assert.equal(repeatedInvite.bridge.status().activeCall.phase, "connected");
  await repeatedInvite.host.emitOutput(20011, 0, [PRIVATE_CALL_SCENE, "enter-relation-one", "room-one"]);
  assert.equal(repeatedInvite.bridge.status().activeCall, null);

  await repeatedInvite.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: MAPPED_PEER_UIN, invite_type: 3 }]);
  await repeatedInvite.host.emitOutput(20006, 0, matchedInviteCallback(PRIVATE_CALL_SCENE, MAPPED_PEER_UID, MAPPED_PEER_UIN));
  const secondCallId = repeatedInvite.bridge.status().activeCall.callId;
  assert.notEqual(secondCallId, firstCallId);
  assert.equal(repeatedInvite.bridge.status().activeCall.phase, "invite_ready", "same caller, scene, and invite relation may start a new call");
  const secondAccept = await repeatedInvite.bridge.accept(secondCallId);
  await repeatedInvite.host.emitOutput(20004, 0, roomEntry(PRIVATE_CALL_SCENE, "enter-relation-two", MAPPED_PEER_UID));
  await repeatedInvite.host.emitOutput(5, secondAccept.id, [0, ""]);
  await repeatedInvite.host.emitOutput(20011, 0, [PRIVATE_CALL_SCENE, "enter-relation-one", "stale-room-one"]);
  assert.equal(repeatedInvite.bridge.status().activeCall.phase, "connected", "an older entered relation cannot terminate the new call");
  await repeatedInvite.host.emitOutput(20011, 0, [PRIVATE_CALL_SCENE, "enter-relation-two", "room-two"]);
  assert.equal(repeatedInvite.bridge.status().activeCall, null);
  await repeatedInvite.bridge.stop();
});

test("Bridge copies and freezes only bounded canonical UIN-to-QQ-UID mappings", async () => {
  const valid = { [MAPPED_PEER_UIN]: MAPPED_PEER_UID };
  assert.doesNotThrow(() => makeHarness({ kernelPeerUidByUin: valid }));

  const invalidMappings = [
    null,
    [],
    Object.assign(Object.create({ inherited: MAPPED_PEER_UID }), { [MAPPED_PEER_UIN]: MAPPED_PEER_UID }),
    { "012345678": MAPPED_PEER_UID },
    { [MAPPED_PEER_UIN]: "not-a-qq-uid" },
    Object.fromEntries(Array.from({ length: 65 }, (_unused, index) => [`${index + 1}`, MAPPED_PEER_UID])),
  ];
  const prototypeKey = {};
  Object.defineProperty(prototypeKey, "__proto__", { value: MAPPED_PEER_UID, enumerable: true });
  invalidMappings.push(prototypeKey);
  const accessor = {};
  Object.defineProperty(accessor, MAPPED_PEER_UIN, { enumerable: true, get() { throw new Error("must-not-run"); } });
  invalidMappings.push(accessor);

  for (const mapping of invalidMappings) {
    assert.throws(() => makeHarness({ kernelPeerUidByUin: mapping }), { code: "invalid_kernel_peer_uid_mapping" });
  }
});

test("audio room destruction emits a terminal snapshot before clearing and permits a fresh enter relation", async () => {
  const terminalSnapshots = [];
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    onStatus(event) {
      if (event.kind === "call_terminal") terminalSnapshots.push(event.status);
    },
  });
  const invite = { scene: PRIVATE_CALL_SCENE, peerUid: "u_peer_one", relationId: "same-invite-relation" };
  const first = await prepareAudioAccept(harness, invite);
  const firstEnterRelation = "entered-relation-secret-one";
  await harness.host.emitOutput(20004, 0, roomEntry(first.scene, firstEnterRelation, first.peerUid));
  await harness.host.emitOutput(5, first.accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.phase, "connected");
  await harness.host.emitOutput(20011, 0, [first.scene, firstEnterRelation, "room-secret-one"]);

  assert.equal(harness.bridge.status().activeCall, null, "confirmed room destruction releases the active-call slot");
  assert.equal(harness.bridge.status().lastCall.phase, "ended");
  assert.equal(harness.bridge.status().lastCall.terminalReason, "native_room_destroyed");
  assert.equal(harness.bridge.status().lastCall.remoteEndedConfirmed, false, "room destruction does not assert remote hangup");
  assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);
  assert.equal(terminalSnapshots.length, 1);
  assert.equal(terminalSnapshots[0].activeCall.phase, "ended", "Control's synchronous snapshot sees the terminal call");
  assert.equal(terminalSnapshots[0].activeCall.uncertain, false);
  assert.equal(terminalSnapshots[0].activeCall.remoteEndedConfirmed, false);
  assert.equal(terminalSnapshots[0].activeCall.roomDestroyedConfirmed, true);

  const second = await prepareAudioAccept(harness, invite);
  const secondEnterRelation = "entered-relation-secret-two";
  await harness.host.emitOutput(20004, 0, roomEntry(second.scene, secondEnterRelation, second.peerUid));
  await harness.host.emitOutput(5, second.accepted.id, [0, ""]);
  await harness.host.emitOutput(20011, 0, [second.scene, firstEnterRelation, "stale-room-one"]);
  assert.equal(harness.bridge.status().activeCall.callId, second.callId, "an older enter relation cannot end the next call");
  assert.equal(harness.bridge.status().activeCall.phase, "connected");
  await harness.host.emitOutput(20011, 0, [second.scene, secondEnterRelation, "room-secret-two"]);
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(terminalSnapshots.length, 2);

  const serialized = JSON.stringify({ terminalSnapshots, status: harness.bridge.status() });
  for (const secret of [invite.relationId, firstEnterRelation, secondEnterRelation, "stale-room-one", "room-secret-one", "room-secret-two"]) {
    assert.equal(serialized.includes(secret), false, "native relation and room identifiers never enter status");
  }
  await harness.bridge.stop();
});

test("verified Windows Kernel room end clears only the current entered room and leaves the next same-caller call live", async () => {
  const terminalSnapshots = [];
  const harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    onStatus(event) {
      if (event.kind === "call_terminal") terminalSnapshots.push(event.status);
    },
  });
  const first = await connectAudioCall(harness, {
    scene: PRIVATE_CALL_SCENE,
    peerUid: "u_peer_one",
    relationId: "same-invite-relation",
    enterRelationId: "entered-kernel-room-one",
  });

  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs("stale-room", first.peerUid));
  assert.equal(harness.bridge.status().activeCall.phase, "connected", "a reason-only or wrong-room event cannot end the current call");
  assert.equal(harness.bridge.status().lastCall, null);

  const firstEnd = kernelRoomEndArgs(first.enterRelationId, first.peerUid);
  await harness.provider.emitKernel("OnS2CActionToAVSDK", firstEnd);
  await harness.provider.emitKernel("OnS2CActionToAVSDK", firstEnd);
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.status().lastCall.phase, "ended");
  assert.equal(harness.bridge.status().lastCall.terminalReason, "kernel_current_room_destroyed");
  assert.equal(harness.bridge.status().lastCall.remoteEndedConfirmed, false);
  assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);
  assert.equal(terminalSnapshots.length, 1, "a repeated matching Kernel event is idempotent");
  assert.equal(terminalSnapshots[0].activeCall.phase, "ended", "the terminal journal projection precedes clearing the slot");

  const second = await connectAudioCall(harness, {
    scene: PRIVATE_CALL_SCENE,
    peerUid: first.peerUid,
    relationId: first.relationId,
    enterRelationId: "entered-kernel-room-two",
  });
  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(first.enterRelationId, second.peerUid));
  assert.equal(harness.bridge.status().activeCall.callId, second.callId);
  assert.equal(harness.bridge.status().activeCall.phase, "connected", "the prior room cannot terminate a later call from the same caller");
  await harness.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(second.enterRelationId, second.peerUid));
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(terminalSnapshots.length, 2);

  const statusText = JSON.stringify({ terminalSnapshots, status: harness.bridge.status() });
  for (const secret of [first.enterRelationId, second.enterRelationId, "same-invite-relation", "stale-room"]) {
    assert.equal(statusText.includes(secret), false, "Kernel room identifiers never enter status or terminal snapshots");
  }
  await harness.bridge.stop();
});

test("Windows Kernel end rejects wrong peers, unobserved reasons, malformed lists, and accessor-backed data", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await connectAudioCall(harness);
  const rejectedArgs = [
    ...[0, 2, "1"].map((reason) => kernelRoomEndArgs(current.enterRelationId, current.peerUid, { reason })),
    kernelRoomEndArgs("different-enter-relation", current.peerUid),
    kernelRoomEndArgs(current.enterRelationId, "u_other_peer"),
    [{ destroyReason: 1, relation_id: "unrelated-kernel-relation" }, null],
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: null }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [] }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [kernelRoomInfo(current.enterRelationId, current.peerUid), kernelRoomInfo(current.enterRelationId, current.peerUid)] }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: { 0: kernelRoomInfo(current.enterRelationId, current.peerUid), length: 1 } }),
    [kernelRoomEndArgs(current.enterRelationId, current.peerUid)[0]],
    [...kernelRoomEndArgs(current.enterRelationId, current.peerUid), null],
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [kernelRoomInfo(current.enterRelationId, current.peerUid, { termAction: 1 })] }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [kernelRoomInfo(current.enterRelationId, current.peerUid, { termType: 1 })] }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [kernelRoomInfo(current.enterRelationId, current.peerUid, { bussinessType: 1 })] }),
    kernelRoomEndArgs(current.enterRelationId, current.peerUid, { list: [kernelRoomInfo(current.enterRelationId, current.peerUid, { peerUin: "12" })] }),
  ];

  for (const args of rejectedArgs) {
    await harness.provider.emitKernel("OnS2CActionToAVSDK", args);
    assert.equal(harness.bridge.status().activeCall.phase, "connected");
    assert.equal(harness.bridge.status().lastCall, null);
  }

  let getterReads = 0;
  const accessorArgs = [null, null];
  Object.defineProperty(accessorArgs, "0", { get() { getterReads += 1; return kernelRoomEndArgs(current.enterRelationId, current.peerUid)[0]; } });
  await harness.provider.emitKernel("OnS2CActionToAVSDK", accessorArgs);

  const accessorList = [];
  Object.defineProperty(accessorList, "0", { get() { getterReads += 1; return kernelRoomInfo(current.enterRelationId, current.peerUid); } });
  await harness.provider.emitKernel("OnS2CActionToAVSDK", [{ destroyReason: 1, relation_id: "unrelated-kernel-relation", pushRoomInfoList: accessorList }, null]);

  const accessorRoom = {};
  Object.defineProperties(accessorRoom, {
    roomId: { get() { getterReads += 1; return current.enterRelationId; } },
    peerUid: { get() { getterReads += 1; return current.peerUid; } },
    termAction: { value: 0 },
    termType: { value: 0 },
    bussinessType: { value: 0 },
    peerUin: { value: "1" },
  });
  await harness.provider.emitKernel("OnS2CActionToAVSDK", [{ destroyReason: 1, relation_id: "unrelated-kernel-relation", pushRoomInfoList: [accessorRoom] }, null]);
  assert.equal(getterReads, 0, "event, array, and room fields are read only through own data descriptors");
  assert.equal(harness.bridge.status().activeCall.phase, "connected");
  assert.equal(harness.bridge.status().lastCall, null);
  await harness.bridge.stop();
});

test("Windows Kernel end requires a current typed entry and a verified call on a live controller", async () => {
  const noEntry = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const pending = await prepareAudioAccept(noEntry);
  await noEntry.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs("unentered-room", pending.peerUid));
  assert.equal(noEntry.bridge.status().activeCall.callId, pending.callId);
  assert.notEqual(noEntry.bridge.status().lastCall?.terminalReason, "kernel_current_room_destroyed");
  await noEntry.bridge.stop();

  const untrusted = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  await untrusted.bridge.attach();
  await untrusted.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "invite-only", invite_type: 3, from_uid: "u_unverified_peer" }]);
  await untrusted.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs("fabricated-room", "u_unverified_peer"));
  assert.equal(untrusted.bridge.status().activeCall.phase, "invite_observed");
  assert.notEqual(untrusted.bridge.status().lastCall?.terminalReason, "kernel_current_room_destroyed");
  await untrusted.bridge.stop();

  const disconnectedHost = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const hostCall = await connectAudioCall(disconnectedHost);
  await disconnectedHost.host.setAvailable(false);
  await disconnectedHost.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(hostCall.enterRelationId, hostCall.peerUid));
  assert.equal(disconnectedHost.bridge.status().activeCall.roomDestroyedConfirmed, undefined);
  await disconnectedHost.bridge.stop();

  const disconnectedProvider = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const providerCall = await connectAudioCall(disconnectedProvider);
  await disconnectedProvider.provider.emitStatus(false);
  await disconnectedProvider.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(providerCall.enterRelationId, providerCall.peerUid));
  assert.equal(disconnectedProvider.bridge.status().activeCall.roomDestroyedConfirmed, undefined);
  await disconnectedProvider.bridge.stop();

  const stopping = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const stoppingCall = await connectAudioCall(stopping);
  const stop = stopping.bridge.stop();
  await stopping.provider.emitKernel("OnS2CActionToAVSDK", kernelRoomEndArgs(stoppingCall.enterRelationId, stoppingCall.peerUid));
  await stop;
  assert.equal(stopping.bridge.status().lastCall.roomDestroyedConfirmed, undefined);
});

test("enter-relation terminal projection survives synchronous subscriber re-entry", async () => {
  let harness;
  let reentered = false;
  let projected = null;
  harness = makeHarness({
    audioDeviceTarget: AUDIO_DEVICE_TARGET,
    onStatus(event) {
      if (event.kind !== "call_terminal") return;
      projected = event.status.activeCall;
      if (!reentered) {
        reentered = true;
        void harness.provider.emitStatus(false);
      }
    },
  });
  const current = await prepareAudioAccept(harness);
  const enterRelationId = "entry-for-reentry";
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  await harness.host.emitOutput(20011, 0, [current.scene, enterRelationId, "room"]);

  assert.equal(projected.phase, "ended");
  assert.equal(projected.uncertain, false);
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.status().lastCall.terminalReason, "native_room_destroyed");
  assert.equal(harness.bridge.status().lastCall.roomDestroyedConfirmed, true);
  await harness.bridge.stop();
});

test("room destruction after entry clears pending cmd 5 and late ACKs cannot revive the call", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(harness);
  const enterRelationId = "entry-before-accept-ack";
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  assert.equal(harness.bridge.status().counts.pendingAcks, 1, "the consumed id=0 entry reservation is removed");

  await harness.host.emitOutput(20011, 0, [current.scene, enterRelationId, "native-room"]);
  assert.equal(harness.bridge.status().activeCall, null);
  assert.equal(harness.bridge.status().counts.pendingAcks, 0, "destroy invalidates the cmd 5 reservation");
  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  assert.equal(harness.bridge.status().activeCall, null, "late ACK and entry output cannot recreate the call");
  assert.equal(harness.bridge.status().lastCall.terminalReason, "native_room_destroyed");
  await harness.bridge.stop();
});

test("20011 without a saved enter relation cannot clear the call or cancel device selection", async () => {
  const pending = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(pending);
  await pending.host.emitOutput(20011, 0, [current.scene, current.relationId, "room-before-entry"]);
  assert.equal(pending.bridge.status().activeCall.callId, current.callId);
  assert.equal(pending.bridge.status().activeCall.phase, "accept_posting");
  await pending.bridge.stop();

  const selectionGate = deferred();
  const selecting = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, hostSelect: () => selectionGate.promise });
  const invite = await prepareAudioInvite(selecting);
  const accepting = selecting.bridge.accept(invite.callId);
  await waitFor(() => selecting.bridge.status().activeCall.acceptState === "selecting_devices");
  await selecting.host.emitOutput(20011, 0, [invite.scene, invite.relationId, "room-before-entry"]);
  assert.equal(selecting.bridge.status().activeCall.callId, invite.callId);
  const selection = selecting.calls.find((entry) => entry.kind === "select_devices");
  selectionGate.resolve({ selected: true, id: selection.id, command: 102 });
  const accepted = await accepting;
  assert.equal(accepted.posted, true);
  assert.equal(selecting.calls.some((entry) => entry.kind === "invoke" && entry.command === 5 && entry.id === accepted.id), true);
  assert.equal(selecting.bridge.status().activeCall.phase, "accept_posting");
  await selecting.bridge.stop();
});

test("audio room destruction requires the saved enter relation, exact scene, UID match, and bounded tuples", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(harness);
  const enterRelationId = "strict-enter-relation";
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  const invalidValues = [
    [current.scene, enterRelationId],
    [true, enterRelationId, "room"],
    [0x80000000, enterRelationId, "room"],
    [current.scene, "", "room"],
    [current.scene, "r".repeat(513), "room"],
    [current.scene, enterRelationId, null],
    [current.scene, enterRelationId, "r".repeat(513)],
    [current.scene + 1, enterRelationId, "room"],
    [current.scene, "other-enter-relation", "room"],
  ];
  for (const value of invalidValues) await harness.host.emitOutput(20011, 0, value);
  await harness.host.emitOutput(20011, 1, [current.scene, enterRelationId, "room"]);
  await assert.rejects(
    harness.host.emitOutput(20011, 0, [current.scene, enterRelationId, "room"], 32),
    { code: "stale_host_generation" },
  );
  assert.equal(harness.bridge.status().activeCall.callId, current.callId, "malformed, mismatched, and stale outputs do not finish the call");
  assert.equal(harness.bridge.status().activeCall.phase, "accept_posting");
  await harness.host.emitOutput(20011, 0, [current.scene, enterRelationId, ""]);
  assert.equal(harness.bridge.status().activeCall, null, "a string room_id is type-checked but is not an identity key");
  await harness.bridge.stop();

  const mismatch = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  await mismatch.bridge.attach();
  await mismatch.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = mismatch.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await mismatch.host.emitOutput(1, login.id, [0, ""]);
  await mismatch.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "kernel", invite_type: 3, from_uid: "u_expected" }]);
  const callId = mismatch.bridge.status().activeCall.callId;
  await mismatch.host.emitOutput(20006, 0, matchedInviteCallback(7, "u_other", "invite-relation"));
  await mismatch.host.emitOutput(20004, 0, roomEntry(7, "enter-relation", "u_expected"));
  await mismatch.host.emitOutput(20011, 0, [7, "enter-relation", "room"]);
  assert.equal(mismatch.bridge.status().activeCall.callId, callId);
  assert.equal(mismatch.bridge.status().activeCall.inviteCallbackReady, false, "an unmatched Kernel UID cannot authorize entry or destroy callbacks");
  await mismatch.bridge.stop();
});

test("room destruction cannot resolve uncertain, disconnected, or stopped calls", async () => {
  const uncertain = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(uncertain);
  const enterRelationId = "entry-that-became-uncertain";
  await uncertain.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  await uncertain.provider.emitKernel("OnS2CActionToAVSDK", [{ relation_id: "kernel-relation", destroyReason: 1 }]);
  assert.equal(uncertain.bridge.status().activeCall.phase, "end_observed");
  assert.equal(uncertain.bridge.status().activeCall.uncertain, true);
  await uncertain.host.emitOutput(20011, 0, [current.scene, enterRelationId, "room"]);
  assert.equal(uncertain.bridge.status().activeCall.phase, "end_observed");
  assert.equal(uncertain.bridge.status().lastCall.roomDestroyedConfirmed, undefined);
  await uncertain.bridge.stop();

  const disconnected = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const disconnectedCall = await prepareAudioAccept(disconnected);
  const disconnectedRelation = "entry-before-disconnect";
  await disconnected.host.emitOutput(20004, 0, roomEntry(disconnectedCall.scene, disconnectedRelation, disconnectedCall.peerUid));
  await disconnected.provider.emitStatus(false);
  await assert.rejects(disconnected.host.emitOutput(20011, 0, [disconnectedCall.scene, disconnectedRelation, "room"]), {
    code: "provider_generation_invalidated",
  });
  assert.equal(disconnected.bridge.status().activeCall.uncertain, true);
  assert.notEqual(disconnected.bridge.status().lastCall.terminalReason, "native_room_destroyed");
  await disconnected.bridge.stop();

  const stopping = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const stoppingCall = await prepareAudioAccept(stopping);
  const stoppingRelation = "entry-before-stop";
  await stopping.host.emitOutput(20004, 0, roomEntry(stoppingCall.scene, stoppingRelation, stoppingCall.peerUid));
  await stopping.bridge.stop();
  await stopping.host.emitOutput(20011, 0, [stoppingCall.scene, stoppingRelation, "room"]);
  assert.equal(stopping.bridge.status().lastCall.terminalReason, "bridge_stopped");
  assert.equal(stopping.bridge.status().lastCall.roomDestroyedConfirmed, undefined);
});

test("enter relation history rejects reuse and reaches its capacity before posting another accept", async () => {
  const reused = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const invite = { scene: PRIVATE_CALL_SCENE, peerUid: "u_same_peer", relationId: "same-invite-relation" };
  const first = await prepareAudioAccept(reused, invite);
  const firstEnterRelation = "seen-enter-relation";
  await reused.host.emitOutput(20004, 0, roomEntry(first.scene, firstEnterRelation, first.peerUid));
  await reused.host.emitOutput(5, first.accepted.id, [0, ""]);
  await reused.host.emitOutput(20011, 0, [first.scene, firstEnterRelation, "room-one"]);

  const second = await prepareAudioAccept(reused, invite);
  await reused.host.emitOutput(20004, 0, roomEntry(second.scene, firstEnterRelation, second.peerUid));
  assert.equal(reused.bridge.status().activeCall.callId, second.callId);
  assert.equal(reused.bridge.status().activeCall.phase, "invite_uncertain");
  assert.equal(reused.bridge.status().activeCall.uncertain, true);
  assert.equal(reused.bridge.status().hostInvalidated, true);
  assert.equal(reused.bridge.status().lastFailureCode, "enter_relation_reused_requires_new_host");
  await assert.rejects(reused.host.emitOutput(5, first.accepted.id, [0, ""]), { code: "host_generation_invalidated" });
  await assert.rejects(reused.host.emitOutput(20011, 0, [second.scene, firstEnterRelation, "old-room"]), { code: "host_generation_invalidated" });
  assert.equal(reused.bridge.status().activeCall.phase, "invite_uncertain", "stale ACK and destroy cannot revive or end a reused enter relation");
  await reused.bridge.stop();

  const full = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, callTimeoutMs: 10_000 });
  await full.bridge.attach();
  await full.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = full.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await full.host.emitOutput(1, login.id, [0, ""]);
  for (let index = 0; index < 64; index += 1) {
    const repeated = await prepareAudioAccept(full, { scene: PRIVATE_CALL_SCENE, peerUid: "u_history_peer", relationId: "same-invite-relation" });
    const enterRelationId = `history-enter-${index}`;
    await full.host.emitOutput(20004, 0, roomEntry(repeated.scene, enterRelationId, repeated.peerUid));
    await full.host.emitOutput(5, repeated.accepted.id, [0, ""]);
    assert.equal(full.bridge.status().activeCall.phase, "connected");
    await full.host.emitOutput(20011, 0, [repeated.scene, enterRelationId, `room-${index}`]);
    assert.equal(full.bridge.status().activeCall, null);
  }

  const overLimit = await prepareAudioInvite(full, { scene: PRIVATE_CALL_SCENE, peerUid: "u_history_peer", relationId: "same-invite-relation" });
  const selectsBefore = full.calls.filter((call) => call.kind === "select_devices").length;
  const invokesBefore = full.calls.filter((call) => call.kind === "invoke" && call.command === 5).length;
  await assert.rejects(full.bridge.accept(overLimit.callId), { code: "enter_relation_history_full_requires_new_host" });
  assert.equal(full.bridge.status().activeCall.phase, "invite_uncertain");
  assert.equal(full.bridge.status().hostInvalidated, true);
  assert.equal(full.calls.filter((call) => call.kind === "select_devices").length, selectsBefore, "full history blocks before device selection");
  assert.equal(full.calls.filter((call) => call.kind === "invoke" && call.command === 5).length, invokesBefore, "full history blocks before cmd 5");
  await full.bridge.stop();
});

test("non-audio controller preserves legacy 20011 and 20006 behavior", async () => {
  const harness = makeHarness();
  await harness.bridge.attach();
  await harness.bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "private" });
  const login = harness.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "kernel", invite_type: 3, from_uid: "caller-private-uid" }]);
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, inviteCallback("caller-private-uid"));
  await harness.host.emitOutput(20011, 0, [7, "relation", "room"]);
  assert.equal(harness.bridge.status().activeCall.callId, callId);
  assert.equal(harness.bridge.status().activeCall.phase, "invite_ready");
  await harness.bridge.stop();
});

test("audio enter result requires exact scene, peer, generations, tuple, and accept reservation", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  const current = await prepareAudioAccept(harness);
  const enterRelationId = "fresh-enter-relation-not-invite-relation";

  await harness.host.emitOutput(20004, 0, roomEntry(current.scene + 1, enterRelationId, current.peerUid));
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, "u_other_synthetic_peer"));
  await assert.rejects(
    harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid), 32),
    { code: "stale_host_generation" },
  );
  await harness.host.emitOutput(20004, current.accepted.id, [0, current.scene, enterRelationId, current.peerUid, 0, "", 1]);
  assert.equal(harness.bridge.status().activeCall.connected, false, "mismatched and stale-generation tuples do not connect");
  const malformedValues = [
    roomEntry(current.scene, enterRelationId, current.peerUid).slice(0, 6),
    [0x80000000, current.scene, enterRelationId, current.peerUid, 0, "", 1],
    [0, true, enterRelationId, current.peerUid, 0, "", 1],
    [0, current.scene, 17, current.peerUid, 0, "", 1],
    [0, current.scene, "e".repeat(513), current.peerUid, 0, "", 1],
    [0, current.scene, enterRelationId, 17, 0, "", 1],
    [0, current.scene, enterRelationId, current.peerUid, true, "", 1],
    [0, current.scene, enterRelationId, current.peerUid, 0, 17, 1],
    [0, current.scene, enterRelationId, current.peerUid, 0, "", true],
  ];
  for (const malformed of malformedValues) await harness.host.emitOutput(20004, 0, malformed);
  assert.equal(harness.bridge.status().activeCall.connected, false, "malformed tuple types and arity fail closed");
  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.connected, false, "cmd 5 ACK alone does not connect an audio call");
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, enterRelationId, current.peerUid));
  assert.equal(harness.bridge.status().activeCall.connected, true, "a valid new enter relation is correlated without matching invite relation");

  // A matching result before accept reservation is also uncorrelated.
  const beforeAccept = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET });
  await beforeAccept.bridge.attach();
  await beforeAccept.bridge.login({ selfUid: "synthetic-self", selfUin: "10001", accountPath: "private" });
  const login = beforeAccept.calls.find((call) => call.kind === "invoke" && call.command === 1);
  await beforeAccept.host.emitOutput(1, login.id, [0, ""]);
  await beforeAccept.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "stale-kernel-relation", invite_type: 3, from_uid: current.peerUid }], { generation: 20 });
  await beforeAccept.host.emitOutput(20006, 0, matchedInviteCallback(current.scene, current.peerUid, current.relationId));
  assert.equal(beforeAccept.bridge.status().activeCall, null, "stale connection-generation invite cannot authorize a room result");
  await beforeAccept.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "kernel-relation", invite_type: 3, from_uid: current.peerUid }]);
  await beforeAccept.host.emitOutput(20006, 0, matchedInviteCallback(current.scene, current.peerUid, current.relationId));
  await beforeAccept.host.emitOutput(20004, 0, roomEntry(current.scene, "pre-accept-enter-relation", current.peerUid));
  assert.equal(beforeAccept.bridge.status().activeCall.connected, false);
  const accepted = await beforeAccept.bridge.accept(beforeAccept.bridge.status().activeCall.callId);
  await beforeAccept.host.emitOutput(5, accepted.id, [0, ""]);
  await beforeAccept.host.emitOutput(20004, 0, roomEntry(current.scene, "accepted-enter-relation", current.peerUid));
  assert.equal(beforeAccept.bridge.status().activeCall.connected, true, "invalid early results do not consume the later reservation");

  await harness.bridge.stop();
  await beforeAccept.bridge.stop();
});

test("expired audio accept cannot be revived by a late zero-id room result", async () => {
  const harness = makeHarness({ audioDeviceTarget: AUDIO_DEVICE_TARGET, callTimeoutMs: 300 });
  const current = await prepareAudioAccept(harness);
  await waitFor(() => harness.bridge.status().activeCall.phase === "expired", 1200);
  await harness.host.emitOutput(20004, 0, roomEntry(current.scene, "expired-enter-relation", current.peerUid));
  await harness.host.emitOutput(5, current.accepted.id, [0, ""]);
  assert.equal(harness.bridge.status().activeCall.phase, "expired");
  assert.equal(harness.bridge.status().activeCall.connected, false);
  await harness.bridge.stop();
});

test("opt-in device selection completes before cmd 5 and rechecks call lifecycle after its await", async () => {
  const target = {
    inputDeviceName: "Line 1 (Virtual Audio Cable)",
    outputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
  };
  const selectionGate = deferred();
  const harness = makeHarness({ audioDeviceTarget: target, hostSelect: () => selectionGate.promise });
  await harness.bridge.attach();
  const login = await harness.bridge.login({ selfUid: "uid", selfUin: "uin", accountPath: "private" });
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("onInviteActionToAVSDK", [{ relation_id: 7501, invite_type: 3, from_uid: "peer" }]);
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, inviteCallback("peer"));

  const accepting = harness.bridge.accept(callId);
  await waitFor(() => harness.bridge.getIntegrationSnapshot().call?.acceptState === "selecting_devices");
  assert.equal(harness.calls.some((entry) => entry.kind === "invoke" && entry.command === 5), false);
  const selection = harness.calls.find((entry) => entry.kind === "select_devices");
  assert.deepEqual({ inputDeviceName: selection.inputDeviceName, outputDeviceName: selection.outputDeviceName }, target);
  selectionGate.resolve({ selected: true, id: selection.id, command: 102 });
  const accepted = await accepting;
  assert.equal(accepted.posted, true);
  assert.deepEqual(harness.calls.filter((entry) => ["select_devices", "invoke"].includes(entry.kind) &&
    (entry.kind === "select_devices" || entry.command === 5)).map((entry) => entry.kind), ["select_devices", "invoke"]);
  await harness.bridge.stop();

  const staleGate = deferred();
  const staleHarness = makeHarness({ audioDeviceTarget: target, hostSelect: () => staleGate.promise });
  await staleHarness.bridge.attach();
  const staleLogin = await staleHarness.bridge.login({ selfUid: "uid", selfUin: "uin", accountPath: "private" });
  await staleHarness.host.emitOutput(1, staleLogin.id, [0, ""]);
  await staleHarness.provider.emitKernel("onInviteActionToAVSDK", [{ relation_id: 7502, invite_type: 3, from_uid: "peer" }]);
  const staleCallId = staleHarness.bridge.status().activeCall.callId;
  await staleHarness.host.emitOutput(20006, 0, inviteCallback("peer"));
  const staleAccept = staleHarness.bridge.accept(staleCallId);
  await waitFor(() => staleHarness.bridge.getIntegrationSnapshot().call?.acceptState === "selecting_devices");
  await staleHarness.provider.emitStatus(false);
  staleGate.resolve({ selected: true, id: staleHarness.calls.find((entry) => entry.kind === "select_devices").id, command: 102 });
  await assert.rejects(staleAccept, { code: "provider_generation_invalidated" });
  assert.equal(staleHarness.calls.some((entry) => entry.kind === "invoke" && entry.command === 5), false);
  await staleHarness.bridge.stop();
});

test("accept fence is synchronous and rechecked after device selection", async () => {
  const target = {
    inputDeviceName: "Line 1 (Virtual Audio Cable)",
    outputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
  };
  const selectionGate = deferred();
  const harness = makeHarness({ audioDeviceTarget: target, hostSelect: () => selectionGate.promise });
  await harness.bridge.attach();
  const login = await harness.bridge.login({ selfUid: "uid", selfUin: "uin", accountPath: "private" });
  await harness.host.emitOutput(1, login.id, [0, ""]);
  await harness.provider.emitKernel("onInviteActionToAVSDK", [{ relation_id: 7510, invite_type: 3, from_uid: "peer" }]);
  const callId = harness.bridge.status().activeCall.callId;
  await harness.host.emitOutput(20006, 0, inviteCallback("peer"));

  await assert.rejects(
    harness.bridge.accept(callId, { assertCurrent: () => Promise.resolve(true) }),
    { code: "accept_guard_async" },
  );
  assert.equal(harness.calls.some((entry) => entry.kind === "select_devices"), false);

  let current = true;
  const accepting = harness.bridge.accept(callId, { assertCurrent: () => current });
  await waitFor(() => harness.bridge.getIntegrationSnapshot().call?.acceptState === "selecting_devices");
  current = false;
  selectionGate.resolve({
    selected: true,
    id: harness.calls.find((entry) => entry.kind === "select_devices").id,
    command: 102,
  });
  await assert.rejects(accepting, { code: "call_not_current" });
  assert.equal(harness.calls.some((entry) => entry.kind === "invoke" && entry.command === 5), false);
  await harness.bridge.stop();
});

test("fresh host generation creates a fresh controller for a subsequent call", async () => {
  const first = makeHarness({ connectionGeneration: 21, hostGeneration: 31 });
  await first.bridge.attach();
  await first.bridge.stop();
  const second = makeHarness({ connectionGeneration: 22, hostGeneration: 32 });
  await second.bridge.attach();
  await second.bridge.login({ selfUid: "uid", selfUin: "uin", accountPath: "private" });
  const login = second.calls.find((call) => call.command === 1);
  await second.host.emitOutput(1, login.id, [0, ""]);
  await second.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "new-relation", invite_type: 1, from_uid: "caller" }]);
  await second.host.emitOutput(20006, 0, inviteCallback("caller"));
  assert.equal(second.bridge.status().activeCall.generation, 1);
  assert.equal(second.bridge.status().hostGeneration, 32);
  await second.bridge.stop();
});

test("native ACK must match command, id, call generation, and host generation", async () => {
  const { bridge, provider, host, calls } = makeHarness();
  await bridge.attach();
  await bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" });
  const login = calls.find((call) => call.command === 1);
  await assert.rejects(host.emitOutput(1, login.id, [0, ""], 999), { code: "stale_host_generation" });
  assert.equal(bridge.status().loginState, "pending");
  await host.emitOutput(1, login.id + 100, [0, ""]);
  assert.equal(bridge.status().loginState, "pending");
  await host.emitOutput(1, login.id, [0, ""]);
  assert.equal(bridge.status().loginState, "ready");
  await provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: "r", invite_type: 2, from_uid: "caller" }]);
  const callId = bridge.status().activeCall.callId;
  await host.emitOutput(20006, 0, inviteCallback("different-caller"));
  assert.equal(bridge.status().activeCall.inviteCallbackReady, false);
  await host.emitOutput(20006, 0, inviteCallback("caller"));
  const accept = await bridge.accept(callId);
  await host.emitOutput(5, 0, [0, ""]);
  assert.equal(bridge.status().activeCall.connected, false, "id=0 cmd5 ACK is uncorrelated");
  await host.emitOutput(20004, accept.id, [0, ""]);
  assert.equal(bridge.status().activeCall.connected, false, "room entry cannot precede matched cmd5 ACK");
  await host.emitOutput(5, accept.id, [1, "failed"]);
  assert.equal(bridge.status().activeCall.connected, false);
  assert.equal(bridge.status().activeCall.phase, "failed");
  await host.emitOutput(20004, accept.id, [0, ""]);
  assert.equal(bridge.status().activeCall.phase, "failed", "late room callback cannot revive failed call");
  await bridge.stop();
});

test("ACK can arrive before invoke's posted receipt and does not deadlock 20001", async () => {
  let loginId;
  let hostRef;
  let forwarded = 0;
  let receiptGate;
  const posted = deferred();
  const harness = makeHarness({
    hostInvoke(input, host) {
      hostRef = host;
      if (input.command === 1) {
        loginId = input.id;
        host.emitOutput(1, loginId, [0, ""]);
        return posted.promise;
      }
      return { posted: true, id: input.id, command: input.command };
    },
  });
  harness.provider.forwardAVSDKOutput = async (command, value) => {
    forwarded += 1;
    assert.equal(command, 20001);
    assert.deepEqual(value, [4, "net"]);
    return { ok: true, result: { ok: true, forwarded: true } };
  };
  await harness.bridge.attach();
  const loginPromise = harness.bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" });
  await waitFor(() => harness.bridge.status().loginState === "ready");
  await hostRef.emitOutput(20001, 0, [4, "net"]);
  assert.equal(forwarded, 1);
  assert.equal(harness.bridge.status().loginState, "ready");
  posted.resolve({ posted: true, id: loginId, command: 1 });
  assert.deepEqual(await loginPromise, { posted: true, id: loginId, loginState: "ready" });
  await harness.bridge.stop();
});

test("signal queue is bounded and remains ordered once login ACK arrives", async () => {
  const { bridge, provider, host, calls } = makeHarness({ maxSignalQueue: 2 });
  await bridge.attach();
  await bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" });
  const login = calls.find((call) => call.command === 1);
  for (const [n, payload] of [[1, "one"], [2, "two"], [3, "three"]]) {
    await provider.emitKernel("OnActionToAVSDK", [n, payload], {
      action: { command: 55, value: [n, payload] },
    });
  }
  assert.equal(bridge.status().counts.queuedSignals, 2);
  await host.emitOutput(1, login.id, [0, ""]);
  await waitFor(() => calls.filter((call) => call.kind === "invoke" && call.command === 55).length === 2);
  assert.deepEqual(calls.filter((call) => call.kind === "invoke" && call.command === 55).map((call) => call.params), [[1, "one"], [2, "two"]]);
  await bridge.stop();
});

test("timeout, crash, and provider/host disconnects invalidate or lock the current call", async () => {
  const timeoutCase = makeHarness({ callTimeoutMs: 100 });
  await timeoutCase.bridge.attach();
  await timeoutCase.bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" });
  const timeoutLogin = timeoutCase.calls.find((call) => call.command === 1);
  await timeoutCase.host.emitOutput(1, timeoutLogin.id, [0, ""]);
  await timeoutCase.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: 1, invite_type: 1, from_uid: "caller" }]);
  const callId = timeoutCase.bridge.status().activeCall.callId;
  await new Promise((resolve) => setTimeout(resolve, 130));
  assert.equal(timeoutCase.bridge.status().activeCall.phase, "expired");
  assert.equal(timeoutCase.bridge.status().activeCall.remoteEndedConfirmed, false);
  await assert.rejects(timeoutCase.bridge.accept(callId), { code: "call_not_accepting" });
  await timeoutCase.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: 2, invite_type: 2, from_uid: "other" }]);
  assert.equal(timeoutCase.bridge.status().counts.busyInvites, 1);
  await timeoutCase.bridge.stop();

  const disconnected = makeHarness();
  await disconnected.bridge.attach();
  await disconnected.bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" });
  const login = disconnected.calls.find((call) => call.command === 1);
  await disconnected.host.emitOutput(1, login.id, [0, ""]);
  await disconnected.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: 3, invite_type: 3, from_uid: "caller" }]);
  await disconnected.host.setAvailable(false);
  assert.equal(disconnected.bridge.status().hostInvalidated, true);
  assert.equal(disconnected.bridge.status().loginState, "invalidated");
  await disconnected.host.setAvailable(true);
  await assert.rejects(disconnected.bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" }), { code: "host_generation_invalidated" });
  await disconnected.bridge.stop();

  const crash = makeHarness();
  await crash.bridge.attach();
  await crash.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: 4, invite_type: 4, from_uid: "caller" }]);
  await crash.provider.emitKernel("OnAVSdkCrash", []);
  assert.equal(crash.bridge.status().hostInvalidated, true);
  assert.equal(crash.bridge.status().activeCall.phase, "failed");
  await crash.bridge.stop();
});

test("attach/shutdown race waits for registration and confirms detach", async () => {
  const started = deferred();
  const registration = deferred();
  const harness = makeHarness({ providerAttach: async () => {
    started.resolve();
    const reply = await registration.promise;
    return reply;
  } });
  const attaching = harness.bridge.attach();
  await started.promise;
  let stopped = false;
  const stopping = harness.bridge.stop().then((result) => { stopped = true; return result; });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(stopped, false, "shutdown waits while registration is unresolved");
  registration.resolve({ ok: true, status: { listenerRegistered: true } });
  await assert.rejects(attaching, { code: "bridge_stopping" });
  assert.deepEqual(await stopping, { stopped: true, detachedConfirmed: true });
  assert.equal(harness.calls.filter((call) => call.kind === "detach").length, 1);
  await assert.rejects(harness.bridge.attach(), { code: "bridge_closing" });
});

test("uncertain attach cannot be repeated and stop retries its own detach", async () => {
  let actualListenerRegistered = false;
  const harness = makeHarness({ providerAttach: async () => {
    actualListenerRegistered = true;
    throw Object.assign(new Error("lost attach response"), { code: "request_timeout" });
  } });
  harness.provider.status = () => ({ connected: true, agent: { agent: { listenerRegistered: false } } }); // stale cached false
  harness.provider.detach = async () => {
    harness.calls.push({ kind: "detach" });
    actualListenerRegistered = false;
    return { ok: true, status: { listenerRegistered: false } };
  };
  await assert.rejects(harness.bridge.attach(), { code: "request_timeout" });
  assert.equal(harness.bridge.status().attachUncertain, true);
  assert.equal(harness.bridge.status().attached, false);
  await assert.rejects(harness.bridge.attach(), { code: "listener_attach_state_uncertain" });
  assert.deepEqual(await harness.bridge.stop(), { stopped: true, detachedConfirmed: true });
  assert.equal(actualListenerRegistered, false);
  assert.equal(harness.calls.filter((call) => call.kind === "detach").length, 1);
});

test("failed stop stays permanently closing and a confirmed retry remains truthful", async () => {
  const harness = makeHarness();
  await harness.bridge.attach();
  let detachCount = 0;
  harness.provider.detach = async () => {
    detachCount += 1;
    if (detachCount === 1) throw Object.assign(new Error("detach uncertain"), { code: "listener_removal_failed" });
    return { ok: true, status: { listenerRegistered: false } };
  };
  await assert.rejects(harness.bridge.stop(), { code: "listener_removal_failed" });
  assert.equal(harness.bridge.status().closing, true);
  assert.equal(harness.bridge.status().attached, true);
  await assert.rejects(harness.bridge.attach(), { code: "bridge_closing" });
  await assert.rejects(harness.bridge.login({ selfUid: "u", selfUin: "n", accountPath: "p" }), { code: "bridge_closing" });
  await assert.rejects(harness.bridge.accept("any"), { code: "bridge_closing" });
  assert.deepEqual(await harness.bridge.stop(), { stopped: true, detachedConfirmed: true });
  assert.deepEqual(await harness.bridge.stop(), { stopped: true, detachedConfirmed: true });
  assert.equal(detachCount, 2);
});

test("20001 has a separate ordered bounded queue and overflow invalidates the controller", async () => {
  const gate = deferred();
  const firstStarted = deferred();
  const values = [];
  const ordered = makeHarness({ maxForwardQueue: 3 });
  ordered.provider.forwardAVSDKOutput = async (_command, value) => {
    values.push(value[0]);
    if (value[0] === 1) {
      firstStarted.resolve();
      await gate.promise;
    }
    return { ok: true, result: { ok: true, forwarded: true } };
  };
  await ordered.bridge.attach();
  const first = ordered.host.emitOutput(20001, 0, [1, "a"]);
  await firstStarted.promise;
  const second = ordered.host.emitOutput(20001, 0, [2, "b"]);
  const third = ordered.host.emitOutput(20001, 0, [3, "c"]);
  gate.resolve();
  await Promise.all([first, second, third]);
  assert.deepEqual(values, [1, 2, 3]);
  assert.equal(ordered.bridge.status().counts.outstanding20001, 0);
  await ordered.bridge.stop();

  const overflowGate = deferred();
  const overflowStarted = deferred();
  const overflowValues = [];
  const overflow = makeHarness({ maxForwardQueue: 2 });
  overflow.provider.forwardAVSDKOutput = async (_command, value) => {
    overflowValues.push(value[0]);
    overflowStarted.resolve();
    await overflowGate.promise;
    return { ok: true, result: { ok: true, forwarded: true } };
  };
  await overflow.bridge.attach();
  const p1 = overflow.host.emitOutput(20001, 0, [1, "a"]).catch((error) => error.code);
  await overflowStarted.promise;
  const p2 = overflow.host.emitOutput(20001, 0, [2, "b"]).catch((error) => error.code);
  const p3 = overflow.host.emitOutput(20001, 0, [3, "c"]).catch((error) => error.code);
  await waitFor(() => overflow.bridge.status().controllerInvalidated);
  overflowGate.resolve();
  const outcomes = await Promise.all([p1, p2, p3]);
  assert.equal(overflowValues.length, 1, "queued output is not forwarded after overflow");
  assert.equal(outcomes.includes("forward_queue_overflow"), true);
  assert.equal(overflow.bridge.status().loginState, "invalidated");
  await overflow.bridge.stop();
});

test("stop clears queued 20001 work and no later queue item reaches the provider", async () => {
  const gate = deferred();
  const firstStarted = deferred();
  const values = [];
  const harness = makeHarness({ maxForwardQueue: 4 });
  harness.provider.forwardAVSDKOutput = async (_command, value) => {
    values.push(value[0]);
    if (value[0] === 1) {
      firstStarted.resolve();
      await gate.promise;
    }
    return { ok: true, result: { ok: true, forwarded: true } };
  };
  await harness.bridge.attach();
  const first = harness.host.emitOutput(20001, 0, [1, "a"]).catch(() => {});
  await firstStarted.promise;
  const queued = harness.host.emitOutput(20001, 0, [2, "b"]).catch((error) => error.code);
  const stopping = harness.bridge.stop();
  assert.equal(await queued, "bridge_closing");
  gate.resolve();
  await Promise.all([first, stopping]);
  assert.deepEqual(values, [1]);
});
