"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash, randomBytes } = require("node:crypto");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");
const { selectRemovalContract } = require("../agent/bootstrap.cjs");
const { createBroker } = require("../probe/broker.cjs");
const { createPipeClient } = require("../probe/ipc.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");
const { createControlService } = require("../bridge/control-service.cjs");
const { createHostClient } = require("../bridge/host-client.cjs");
const { PRIVATE_CALL_SCENE } = require("../host/private-end.cjs");
const {
  PRIVATE_INVITE_PROFILE,
  nativeInviteEvidenceFromCallback,
} = require("../shared/private-invite-scope.cjs");

const ACCOUNT_ID = "123456789";
const KERNEL_UID = `u_${"A".repeat(22)}`;
const PEER_UIN = "987654321";
const TARGET_PID = 4455;
const CONNECTION_GENERATION = 9;
const HOST_GENERATION = 7;
const KERNEL_CREATION_TIME_100NS = "134000000000000000";
const CONTRACT_FIXTURE_PATH = path.resolve(
  __dirname,
  "../.runtime/private-scope-repair/after/private-scope-contract.fixture.json",
);
const CONTRACT_SOURCE_PATHS = Object.freeze([
  "agent/bootstrap.cjs",
  "agent/kernel-agent.cjs",
  "probe/broker.cjs",
  "bridge/host-client.cjs",
  "bridge/qqcall-bridge.cjs",
  "bridge/control-service.cjs",
  "shared/private-invite-scope.cjs",
  "tests/private-invite-scope-contract.test.cjs",
]);
try { fs.rmSync(CONTRACT_FIXTURE_PATH, { force: true }); } catch { /* stale generated fixture must not pass Python verification */ }

function testToken() { return randomBytes(32).toString("base64url"); }

async function waitUntil(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail("contract condition did not become true before timeout");
}

async function postJson(port, token, route, payload) {
  const response = await fetch(`http://127.0.0.1:${port}${route}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return { status: response.status, body: await response.json() };
}

function makeHost({ selectAudioDevices } = {}) {
  const listeners = new Set();
  const invocations = [];
  const deviceSelections = [];
  return {
    invocations,
    deviceSelections,
    status: () => ({ available: true, hostGeneration: HOST_GENERATION }),
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async invoke(input) {
      invocations.push(input);
      return { posted: true, id: input.id, command: input.command };
    },
    async selectAudioDevices(input) {
      deviceSelections.push(input);
      if (selectAudioDevices) return selectAudioDevices(input);
      return { selected: true, id: input.id, command: 102 };
    },
    async emitOutput(command, id, value) {
      return Promise.all([...listeners].map((listener) => listener({
        type: "output", command, id, value, hostGeneration: HOST_GENERATION,
      })));
    },
  };
}

test("unknown wrapper hash and config cannot enable a private invite profile", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-scope-hash-"));
  try {
    const modulePath = path.join(root, "wrapper.node");
    fs.writeFileSync(modulePath, Buffer.from("unrecognized wrapper fixture"));
    const result = selectRemovalContract({
      appPath: root,
      wrapperModulePath: modulePath,
      nativeInviteScopeProfile: PRIVATE_INVITE_PROFILE,
    }, modulePath);
    assert.equal(result.mode, "unverified");
    assert.equal(result.verified, false);
    assert.equal(result.nativeInviteScopeProfile, null);

    const mismatched = selectRemovalContract({ appPath: root, wrapperModulePath: `${modulePath}.other` }, modulePath);
    assert.equal(mismatched.verified, false);
    assert.notEqual(mismatched.nativeInviteScopeProfile, PRIVATE_INVITE_PROFILE);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("legacy CLI without a Kernel FILETIME keeps invite events unknown and strips native proof", async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-scope-legacy-"));
  const ipcToken = testToken();
  const events = [];
  const broker = createBroker({
    targetPid: TARGET_PID,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "agent-report"),
    sessionName: "nt_1",
    connectionGeneration: CONNECTION_GENERATION,
    ipcToken,
    nonce: randomBytes(12).toString("hex"),
    onEvent: (op, payload) => events.push({ op, payload }),
  });
  const client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: CONNECTION_GENERATION, ipcToken });
  try {
    await broker.start();
    await client.connect();
    const args = [{ relation_id: PEER_UIN, invite_type: 1, from_uid: "" }];
    const nativeInviteEvidence = nativeInviteEvidenceFromCallback(
      PRIVATE_INVITE_PROFILE,
      "OnInviteActionToAVSDK",
      args,
      1,
      1,
    );
    client.publishEvent("kernel_event", {
      kind: "kernel_event",
      method: "OnInviteActionToAVSDK",
      args,
      connectionGeneration: CONNECTION_GENERATION,
      nativeInviteEvidence,
      nativeInviteProvenance: {
        targetPid: TARGET_PID,
        kernelCreationTime100ns: KERNEL_CREATION_TIME_100NS,
        connectionGeneration: CONNECTION_GENERATION,
        listenerMutationEpoch: 1,
        ipcSequence: 999,
      },
    });
    await waitUntil(() => events.some((entry) => entry.op === "kernel_event"));
    const forwarded = events.find((entry) => entry.op === "kernel_event").payload;
    assert.equal(Object.hasOwn(forwarded, "nativeInviteEvidence"), false);
    assert.equal(Object.hasOwn(forwarded, "nativeInviteProvenance"), false);
    assert.equal(forwarded.args[0].invite_type, 1,
      "old CLI event compatibility leaves the invite available only to the unknown legacy path");
    assert.equal(broker.status().nativeInviteBinding, null);
  } finally {
    await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("type-1 native callback proof crosses authenticated Broker, Bridge, Control, and strict poll projection", async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-scope-contract-"));
  const ipcToken = testToken();
  const controlToken = testToken();
  const brokerEvents = [];
  const wrapperService = {
    listener: null,
    addKernelAVSDKListener(listener) { this.listener = listener; },
    removeKernelAVSDKListener(listener) {
      if (this.listener === listener) this.listener = null;
    },
    setActionFromAVSDK() {},
  };
  const wrapper = {
    NodeIQQNTWrapperSession: {
      getNTWrapperSession: async () => ({ getAVSDKService: async () => wrapperService }),
    },
  };
  const broker = createBroker({
    targetPid: TARGET_PID,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "agent-report"),
    sessionName: "nt_1",
    connectionGeneration: CONNECTION_GENERATION,
    kernelCreationTime100ns: KERNEL_CREATION_TIME_100NS,
    ipcToken,
    nonce: randomBytes(12).toString("hex"),
    onEvent: (op, payload) => brokerEvents.push({ op, payload }),
  });
  const client = createPipeClient({
    pipeName: broker.pipeName,
    connectionGeneration: CONNECTION_GENERATION,
    ipcToken,
  });
  const agentEvents = [];
  const agent = createKernelAgent({
    wrapper,
    connectionGeneration: CONNECTION_GENERATION,
    removalMode: "listener-object",
    nativeInviteScopeProfile: PRIVATE_INVITE_PROFILE,
    emit: (event) => {
      agentEvents.push(event);
      return client.publishEvent("kernel_event", event);
    },
  });
  client.setAgent(agent);
  client.setBootstrapStatus({
    wrapperHashPinned: true,
    processPid: TARGET_PID,
    connectionGeneration: CONNECTION_GENERATION,
    listenerRegistered: false,
  });

  let deviceSelectionStarted;
  const deviceSelectionStartedPromise = new Promise((resolve) => { deviceSelectionStarted = resolve; });
  let releaseDeviceSelection;
  const deviceSelectionGate = new Promise((resolve) => { releaseDeviceSelection = resolve; });
  const host = makeHost({
    selectAudioDevices: (input) => {
      deviceSelectionStarted(input);
      return deviceSelectionGate;
    },
  });
  const bridge = createQQCallBridge({
    provider: broker,
    host,
    connectionGeneration: CONNECTION_GENERATION,
    hostGeneration: HOST_GENERATION,
    kernelPeerUidByUin: { [PEER_UIN]: KERNEL_UID },
    audioDeviceTarget: {
      inputDeviceName: "Controlled fixture input",
      outputDeviceName: "Controlled fixture output",
    },
    callTimeoutMs: 60_000,
  });
  const controlService = createControlService({
    bridge,
    token: controlToken,
    accountId: ACCOUNT_ID,
    port: 0,
  });
  let freshBridge = null;
  let freshControlService = null;

  try {
    await broker.start();
    const brokerConfigPath = path.join(runtimeDir, "broker-config.json");
    await broker.writeConfigFile(brokerConfigPath);
    assert.equal(Object.hasOwn(JSON.parse(fs.readFileSync(brokerConfigPath, "utf8")), "kernelCreationTime100ns"), false,
      "supervisor FILETIME stays Broker-owned and does not enter the injector config schema");
    await client.connect();
    const serviceStatus = await controlService.start();
    await bridge.attach();
    assert.equal(wrapperService.listener !== null, true, "the fixture listener is installed only by explicit Bridge attach");
    assert.equal(broker.status().nativeInviteBinding?.targetPid, TARGET_PID);

    // Reattach the same controlled Agent. A captured old listener callback is
    // explicitly native-origin marked, but its registration sequence is stale.
    const oldListener = wrapperService.listener;
    assert.equal(broker.status().nativeInviteBinding.listenerRegistrationSequence, 1);
    await broker.detach();
    assert.equal(broker.status().nativeInviteBinding, null);
    await broker.attach();
    const currentListener = wrapperService.listener;
    assert.notStrictEqual(currentListener, oldListener);
    assert.equal(broker.status().nativeInviteBinding.listenerRegistrationSequence, 2);

    const staleCallbackEventCount = broker.status().eventCount;
    oldListener.OnInviteActionToAVSDK({
      relation_id: PEER_UIN,
      invite_type: 1,
      from_uid: "",
    }, 55, "stale-command");
    await waitUntil(() => broker.status().eventCount > staleCallbackEventCount && broker.status().lastEvent?.dropped === true);
    assert.equal(bridge.getIntegrationSnapshot().call, null,
      "a stale-origin invite cannot allocate a call after listener replacement");
    assert.equal(host.invocations.length, 0,
      "a stale-origin command-55 payload cannot reach Host");

    // This is a controlled native callback fixture using the exact known C2C
    // shape: invite_type=1, canonical relation UIN, actual empty from_uid.
    currentListener.OnInviteActionToAVSDK({
      relation_id: PEER_UIN,
      invite_type: 1,
      from_uid: "",
    });
    await waitUntil(() => bridge.getIntegrationSnapshot().call?.phase === "invite_observed");
    const callBeforeHost = bridge.getIntegrationSnapshot().call;
    assert.equal(Object.hasOwn(callBeforeHost, "nativeInviteEvidence"), false,
      "native proof is withheld until matching SDK 20006 identity confirmation");

    const inviteCallback = [PRIVATE_CALL_SCENE, KERNEL_UID, "self", 9, PEER_UIN, null, null, null, null, null, true, "tail"];
    await host.emitOutput(20006, 0, inviteCallback);
    const integration = bridge.getIntegrationSnapshot();
    assert.equal(integration.call.inviteCorrelation, "kernel_relation_uid_matched");
    assert.equal(integration.call.nativeInviteEvidence.fromUid, "");
    assert.equal(bridge.status().activeCall.peerUid, undefined, "public status omits identity fields");
    assert.equal(JSON.stringify(bridge.status()).includes(KERNEL_UID), false);

    const polled = await postJson(serviceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeContractClient",
      after: 0,
    });
    assert.equal(polled.status, 200);
    assert.equal(polled.body.accountId, ACCOUNT_ID);
    assert.equal(polled.body.currentCall.inviteCallbackReady, true);
    assert.equal(polled.body.currentCall.inviteCorrelation, "kernel_relation_uid_matched");
    assert.deepEqual(polled.body.currentCall.privateScopeProof && {
      inviteType: polled.body.currentCall.privateScopeProof.inviteType,
      fromUid: polled.body.currentCall.privateScopeProof.fromUid,
      relationId: polled.body.currentCall.privateScopeProof.relationId,
      scopeKind: polled.body.currentCall.privateScopeProof.scopeKind,
    }, {
      inviteType: 1,
      fromUid: "",
      relationId: PEER_UIN,
      scopeKind: "call",
    });

    const confirmedCallId = integration.call.callId;
    const currentAgentEvent = agentEvents.at(-1);
    for (const replaySequence of [currentAgentEvent.nativeInviteEvidence.callbackSequence, 1]) {
      const beforeReplay = broker.status().eventCount;
      client.publishEvent("kernel_event", {
        ...currentAgentEvent,
        nativeInviteEvidence: {
          ...currentAgentEvent.nativeInviteEvidence,
          callbackSequence: replaySequence,
        },
        // These sender-supplied identity values are deliberately wrong; the
        // Broker must reject replay before provenance projection.
        nativeInviteProvenance: {
          targetPid: TARGET_PID + 1,
          kernelCreationTime100ns: "134000000000000001",
          connectionGeneration: CONNECTION_GENERATION + 1,
          listenerMutationEpoch: 999,
          ipcSequence: 999,
        },
      });
      await waitUntil(() => broker.status().eventCount > beforeReplay && broker.status().lastEvent?.dropped === true);
      assert.equal(bridge.getIntegrationSnapshot().call.callId, confirmedCallId,
        "duplicate and out-of-order native evidence cannot replace or mutate the current call");
      assert.equal(host.invocations.length, 0, "replayed proof cannot forward an action to Host");
    }
    const beforeStampedEvent = broker.status().eventCount;
    client.publishEvent("kernel_event", {
      ...currentAgentEvent,
      nativeInviteEvidence: {
        ...currentAgentEvent.nativeInviteEvidence,
        callbackSequence: currentAgentEvent.nativeInviteEvidence.callbackSequence + 1,
      },
      nativeInviteProvenance: {
        targetPid: TARGET_PID + 1,
        kernelCreationTime100ns: "134000000000000001",
        connectionGeneration: CONNECTION_GENERATION + 1,
        listenerMutationEpoch: 999,
        ipcSequence: 999,
      },
    });
    await waitUntil(() => broker.status().eventCount > beforeStampedEvent &&
      brokerEvents.filter((entry) => entry.op === "kernel_event").length >= 2);
    const trustedProjection = brokerEvents.filter((entry) => entry.op === "kernel_event").at(-1).payload.nativeInviteProvenance;
    assert.deepEqual({
      targetPid: trustedProjection.targetPid,
      kernelCreationTime100ns: trustedProjection.kernelCreationTime100ns,
      connectionGeneration: trustedProjection.connectionGeneration,
      listenerMutationEpoch: trustedProjection.listenerMutationEpoch,
    }, {
      targetPid: TARGET_PID,
      kernelCreationTime100ns: KERNEL_CREATION_TIME_100NS,
      connectionGeneration: CONNECTION_GENERATION,
      listenerMutationEpoch: broker.status().nativeInviteBinding.listenerMutationEpoch,
    }, "Broker stamps provenance from its current pinned binding, not the incoming event");
    assert.notEqual(trustedProjection.ipcSequence, 999);
    assert.equal(bridge.getIntegrationSnapshot().call.callId, confirmedCallId,
      "a duplicate relation cannot replace proof on the already active call");

    // Hold the existing proof-backed control accept across device selection.
    // The Broker remains connected/authenticated on the same generic
    // generation while its current bootstrap profile loses the pinned marker.
    const oldBinding = broker.status().nativeInviteBinding;
    const login = await bridge.login({
      selfUid: `u_${"S".repeat(22)}`,
      selfUin: ACCOUNT_ID,
      accountPath: path.join(runtimeDir, "synthetic-account"),
    });
    await host.emitOutput(1, login.id, [0, ""]);
    const callScope = {
      serviceInstance: polled.body.serviceInstance,
      hostGeneration: polled.body.hostGeneration,
      connectionGeneration: polled.body.connectionGeneration,
      callId: polled.body.currentCall.callId,
      callGeneration: polled.body.currentCall.generation,
    };
    const accepting = postJson(serviceStatus.port, controlToken, "/v1/call/command", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeContractClient",
      operation: "accept",
      scope: callScope,
    });
    const selection = await deviceSelectionStartedPromise;
    const unpinnedBootstrapStatus = {
      wrapperHashPinned: false,
      processPid: TARGET_PID,
      connectionGeneration: CONNECTION_GENERATION,
      listenerRegistered: true,
    };
    client.setBootstrapStatus(unpinnedBootstrapStatus);
    client.publishEvent("bootstrap_status", unpinnedBootstrapStatus);
    await waitUntil(() => broker.status().nativeInviteBinding === null &&
      broker.status().connected === true && broker.status().authenticated === true);
    assert.equal(broker.status().connectionGeneration, CONNECTION_GENERATION,
      "native binding revocation does not require a generic provider reconnect");

    const revokedPoll = await postJson(serviceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeContractClient",
      after: 0,
    });
    assert.equal(revokedPoll.status, 200);
    assert.equal(Object.hasOwn(revokedPoll.body.currentCall, "privateScopeProof"), false,
      "current poll withholds the proof as soon as the Broker native binding is revoked");
    assert.equal(JSON.stringify(revokedPoll.body.events).includes("privateScopeProof"), false,
      "historical call snapshots do not replay a retired proof");

    releaseDeviceSelection({ selected: true, id: selection.id, command: 102 });
    const staleAccept = await accepting;
    assert.equal(staleAccept.status, 409);
    assert.deepEqual(staleAccept.body, { ok: false, code: "private_scope_unverified" });
    assert.equal(host.invocations.some((entry) => entry.command === 5), false,
      "revocation during device selection prevents the previously proved call from posting cmd 5");

    // Restore the pinned bootstrap report, then retire this Bridge so its
    // normal owned detach/attach path establishes a new authenticated listener
    // epoch under the same Broker connection generation.
    client.setBootstrapStatus({
      wrapperHashPinned: true,
      processPid: TARGET_PID,
      connectionGeneration: CONNECTION_GENERATION,
      listenerRegistered: true,
    });
    await controlService.stop();
    await bridge.stop();
    assert.equal(Object.hasOwn(bridge.getIntegrationSnapshot().call, "nativeInviteEvidence"), false,
      "reattaching a current listener cannot restore proof on the old same-scope call");

    freshBridge = createQQCallBridge({
      provider: broker,
      host,
      connectionGeneration: CONNECTION_GENERATION,
      hostGeneration: HOST_GENERATION,
      kernelPeerUidByUin: { [PEER_UIN]: KERNEL_UID },
      audioDeviceTarget: {
        inputDeviceName: "Controlled fixture input",
        outputDeviceName: "Controlled fixture output",
      },
      callTimeoutMs: 60_000,
    });
    freshControlService = createControlService({
      bridge: freshBridge,
      token: controlToken,
      accountId: ACCOUNT_ID,
      port: 0,
    });
    const freshServiceStatus = await freshControlService.start();
    await freshBridge.attach();
    const freshLogin = await freshBridge.login({
      selfUid: `u_${"F".repeat(22)}`,
      selfUin: ACCOUNT_ID,
      accountPath: path.join(runtimeDir, "fresh-synthetic-account"),
    });
    await host.emitOutput(1, freshLogin.id, [0, ""]);
    const freshBinding = broker.status().nativeInviteBinding;
    assert.ok(freshBinding);
    assert.equal(freshBinding.connectionGeneration, CONNECTION_GENERATION);
    assert.notEqual(freshBinding.listenerMutationEpoch, oldBinding.listenerMutationEpoch);
    assert.notEqual(freshBinding.listenerRegistrationSequence, oldBinding.listenerRegistrationSequence);
    const freshListener = wrapperService.listener;
    freshListener.OnInviteActionToAVSDK({
      relation_id: PEER_UIN,
      invite_type: 1,
      from_uid: "",
    });
    await waitUntil(() => freshBridge.getIntegrationSnapshot().call?.phase === "invite_observed");
    await host.emitOutput(20006, 0, inviteCallback);
    const freshCall = freshBridge.getIntegrationSnapshot().call;
    assert.equal(freshCall.nativeInviteEvidence.listenerMutationEpoch, freshBinding.listenerMutationEpoch);
    assert.equal(freshCall.nativeInviteEvidence.listenerRegistrationSequence, freshBinding.listenerRegistrationSequence);
    const freshPoll = await postJson(freshServiceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeFreshClient",
      after: 0,
    });
    assert.equal(freshPoll.status, 200);
    assert.equal(freshPoll.body.currentCall.privateScopeProof?.inviteType, 1,
      "a fresh callback on the newly confirmed listener can prove a new call");

    const freshSelectionCount = host.deviceSelections.length;
    const freshAcceptCount = host.invocations.filter((entry) => entry.command === 5).length;
    const freshUnpinnedStatus = {
      wrapperHashPinned: false,
      processPid: TARGET_PID,
      connectionGeneration: CONNECTION_GENERATION,
      listenerRegistered: true,
    };
    client.setBootstrapStatus(freshUnpinnedStatus);
    client.publishEvent("bootstrap_status", freshUnpinnedStatus);
    await waitUntil(() => broker.status().nativeInviteBinding === null &&
      broker.status().connected === true && broker.status().authenticated === true);
    const freshRevokedPoll = await postJson(freshServiceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeFreshClient",
      after: 0,
    });
    assert.equal(Object.hasOwn(freshRevokedPoll.body.currentCall, "privateScopeProof"), false);
    const queuedAfterRevocation = await postJson(freshServiceStatus.port, controlToken, "/v1/call/command", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateScopeFreshClient",
      operation: "accept",
      scope: {
        serviceInstance: freshPoll.body.serviceInstance,
        hostGeneration: freshPoll.body.hostGeneration,
        connectionGeneration: freshPoll.body.connectionGeneration,
        callId: freshPoll.body.currentCall.callId,
        callGeneration: freshPoll.body.currentCall.generation,
      },
    });
    assert.equal(queuedAfterRevocation.status, 409);
    assert.deepEqual(queuedAfterRevocation.body, { ok: false, code: "private_scope_unverified" },
      "a command queued or begun after observed revocation cannot downgrade to legacy manual acceptance");
    assert.equal(host.deviceSelections.length, freshSelectionCount,
      "accept after revocation does not enter device selection");
    assert.equal(host.invocations.filter((entry) => entry.command === 5).length, freshAcceptCount,
      "accept after revocation does not post cmd 5");

    fs.mkdirSync(path.dirname(CONTRACT_FIXTURE_PATH), { recursive: true });
    const sourceSha256 = Object.fromEntries(CONTRACT_SOURCE_PATHS.map((relativePath) => [
      relativePath,
      createHash("sha256").update(fs.readFileSync(path.join(__dirname, "..", relativePath))).digest("hex").toUpperCase(),
    ]));
    fs.writeFileSync(CONTRACT_FIXTURE_PATH, `${JSON.stringify({
      version: 1,
      generatedAtMs: Date.now(),
      sourceSha256,
      poll: polled.body,
    }, null, 2)}\n`, "utf8");
  } finally {
    await freshControlService?.stop().catch(() => {});
    await freshBridge?.stop().catch(() => {});
    await controlService.stop().catch(() => {});
    await bridge.stop().catch(() => {});
    await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("proof-backed cmd 5 is fenced when it reaches the real Host HTTP queue", async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-scope-host-queue-"));
  const ipcToken = testToken();
  const controlToken = testToken();
  const hostToken = testToken();
  const brokerEvents = [];
  const wrapperService = {
    listener: null,
    addKernelAVSDKListener(listener) { this.listener = listener; },
    removeKernelAVSDKListener(listener) {
      if (this.listener === listener) this.listener = null;
    },
    setActionFromAVSDK() {},
  };
  const wrapper = {
    NodeIQQNTWrapperSession: {
      getNTWrapperSession: async () => ({ getAVSDKService: async () => wrapperService }),
    },
  };
  const broker = createBroker({
    targetPid: TARGET_PID,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "agent-report"),
    sessionName: "nt_1",
    connectionGeneration: CONNECTION_GENERATION,
    kernelCreationTime100ns: KERNEL_CREATION_TIME_100NS,
    ipcToken,
    nonce: randomBytes(12).toString("hex"),
    onEvent: (op, payload) => brokerEvents.push({ op, payload }),
  });
  const client = createPipeClient({
    pipeName: broker.pipeName,
    connectionGeneration: CONNECTION_GENERATION,
    ipcToken,
  });
  const agent = createKernelAgent({
    wrapper,
    connectionGeneration: CONNECTION_GENERATION,
    removalMode: "listener-object",
    nativeInviteScopeProfile: PRIVATE_INVITE_PROFILE,
    emit: (event) => client.publishEvent("kernel_event", event),
  });
  client.setAgent(agent);
  const initialBootstrapStatus = {
    wrapperHashPinned: true,
    processPid: TARGET_PID,
    connectionGeneration: CONNECTION_GENERATION,
    listenerRegistered: false,
  };
  client.setBootstrapStatus(initialBootstrapStatus);

  let resolveFirst55Seen;
  let releaseFirst55;
  const first55Seen = new Promise((resolve) => { resolveFirst55Seen = resolve; });
  const first55Gate = new Promise((resolve) => { releaseFirst55 = resolve; });
  const hostInvocations = [];
  const hostFailures = [];
  const fakeHostServer = http.createServer((request, response) => {
    void (async () => {
      if (request.headers.authorization !== `Bearer ${hostToken}`) {
        response.writeHead(401).end();
        request.resume();
        return;
      }
      if (request.method === "GET" && request.url === "/healthz") {
        const body = Buffer.from(JSON.stringify({
          ok: true,
          status: {
            hostGeneration: HOST_GENERATION,
            ready: true,
            pluginFound: true,
            hasPostMessage: true,
            rendererGone: false,
            stopped: false,
            outputFailed: false,
            readinessError: null,
          },
        }));
        response.writeHead(200, { "content-type": "application/json", "content-length": body.length }).end(body);
        return;
      }
      if (request.method !== "POST" || request.url !== "/v1/invoke") {
        response.writeHead(404).end();
        request.resume();
        return;
      }
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      hostInvocations.push(body);
      if (body.command === 55 && body.id === 9001) {
        resolveFirst55Seen();
        await first55Gate;
      }
      const reply = Buffer.from(JSON.stringify({ posted: true, id: body.id, command: body.command }));
      response.writeHead(200, { "content-type": "application/json", "content-length": reply.length }).end(reply);
    })().catch(() => {
      if (!response.destroyed && !response.writableEnded) response.writeHead(500).end();
    });
  });
  await new Promise((resolve, reject) => {
    fakeHostServer.once("error", reject);
    fakeHostServer.listen(0, "127.0.0.1", resolve);
  });
  const hostClient = createHostClient({
    token: hostToken,
    listenPort: fakeHostServer.address().port,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    requestTimeoutMs: 5_000,
    outputTimeoutMs: 1_000,
    healthPollMs: 60_000,
    onFailure: (event) => hostFailures.push(event.code),
  });

  let bridge = null;
  let controlService = null;
  let freshBridge = null;
  let freshControlService = null;
  const inviteCallback = [PRIVATE_CALL_SCENE, KERNEL_UID, "self", 9, PEER_UIN, null, null, null, null, null, true, "tail"];
  async function sendHostOutput(outputPort, command, id, value) {
    const response = await fetch(`http://127.0.0.1:${outputPort}/v1/avsdk/output`, {
      method: "POST",
      headers: { authorization: `Bearer ${hostToken}`, "content-type": "application/json" },
      body: JSON.stringify({ command, id, value, hostGeneration: HOST_GENERATION }),
    });
    return { status: response.status, body: await response.json() };
  }
  async function waitForCallbackReadiness(outputPort) {
    const response = await fetch(`http://127.0.0.1:${outputPort}/v1/callback-ready`, {
      headers: {
        authorization: `Bearer ${hostToken}`,
        "x-qqcall-host-generation": String(HOST_GENERATION),
      },
    });
    assert.equal(response.status, 200, "the authenticated Host callback route is ready before SDK output is posted");
    assert.equal((await response.json()).callbackReady, true);
  }
  async function loginThroughHost(currentBridge, outputPort, accountPath) {
    const login = await currentBridge.login({
      selfUid: `u_${"L".repeat(22)}`,
      selfUin: ACCOUNT_ID,
      accountPath,
    });
    assert.equal(login.posted, true);
    const output = await sendHostOutput(outputPort, 1, login.id, [0, ""]);
    assert.equal(output.status, 202);
    await waitUntil(() => currentBridge.status().loginState === "ready");
  }
  async function proveFreshCall(currentBridge, outputPort) {
    const listener = wrapperService.listener;
    assert.ok(listener);
    listener.OnInviteActionToAVSDK({ relation_id: PEER_UIN, invite_type: 1, from_uid: "" });
    await waitUntil(() => currentBridge.getIntegrationSnapshot().call?.phase === "invite_observed");
    const output = await sendHostOutput(outputPort, 20006, 0, inviteCallback);
    assert.equal(output.status, 202);
    assert.equal(currentBridge.getIntegrationSnapshot().call.inviteCorrelation, "kernel_relation_uid_matched");
  }

  try {
    await broker.start();
    await client.connect();
    await hostClient.start();
    assert.equal(await hostClient.checkHealth(), true, "the fake Host accepts only the authenticated ready health response");

    bridge = createQQCallBridge({
      provider: broker,
      host: hostClient,
      connectionGeneration: CONNECTION_GENERATION,
      hostGeneration: HOST_GENERATION,
      kernelPeerUidByUin: { [PEER_UIN]: KERNEL_UID },
      callTimeoutMs: 60_000,
    });
    controlService = createControlService({ bridge, token: controlToken, accountId: ACCOUNT_ID, port: 0 });
    const serviceStatus = await controlService.start();
    await bridge.attach();
    const hostStatus = hostClient.status();
    assert.equal(hostStatus.available, true);
    await waitForCallbackReadiness(hostStatus.outputPort);
    await loginThroughHost(bridge, hostStatus.outputPort, path.join(runtimeDir, "first-account"));
    await proveFreshCall(bridge, hostStatus.outputPort);

    const polled = await postJson(serviceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateHostQueueClient",
      after: 0,
    });
    assert.equal(polled.status, 200);
    assert.equal(polled.body.currentCall.privateScopeProof?.inviteType, 1);
    const scope = {
      serviceInstance: polled.body.serviceInstance,
      hostGeneration: polled.body.hostGeneration,
      connectionGeneration: polled.body.connectionGeneration,
      callId: polled.body.currentCall.callId,
      callGeneration: polled.body.currentCall.generation,
    };

    const first55 = hostClient.invoke({ command: 55, id: 9001, params: [4, "held-first"], hostGeneration: HOST_GENERATION });
    await first55Seen;
    const accepting = postJson(serviceStatus.port, controlToken, "/v1/call/command", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateHostQueueClient",
      operation: "accept",
      scope,
    });
    await waitUntil(() => hostClient.status().counts.queued === 1);
    assert.equal(hostInvocations.some((entry) => entry.command === 5), false,
      "accepted cmd 5 stays behind the still-pending actual Host HTTP response");
    const after55 = hostClient.invoke({ command: 55, id: 9002, params: [4, "ordinary-after"], hostGeneration: HOST_GENERATION });
    await waitUntil(() => hostClient.status().counts.queued === 2);

    const revokedBootstrapStatus = {
      wrapperHashPinned: false,
      processPid: TARGET_PID,
      connectionGeneration: CONNECTION_GENERATION,
      listenerRegistered: true,
    };
    client.setBootstrapStatus(revokedBootstrapStatus);
    client.publishEvent("bootstrap_status", revokedBootstrapStatus);
    await waitUntil(() => broker.status().nativeInviteBinding === null &&
      broker.status().connected === true && broker.status().authenticated === true);
    releaseFirst55();
    assert.deepEqual(await first55, { posted: true, id: 9001, command: 55 });
    const revokedAccept = await accepting;
    assert.equal(revokedAccept.status, 409);
    assert.deepEqual(revokedAccept.body, { ok: false, code: "private_scope_unverified" });
    assert.deepEqual(await after55, { posted: true, id: 9002, command: 55 },
      "the guard rejection leaves the real FIFO available to the next ordinary item");
    assert.deepEqual(hostInvocations.map(({ command, id }) => ({ command, id })), [
      { command: 1, id: hostInvocations.find((entry) => entry.command === 1).id },
      { command: 55, id: 9001 },
      { command: 55, id: 9002 },
    ]);
    assert.equal(hostInvocations.filter((entry) => entry.command === 5).length, 0,
      "revoked proof is rejected at dequeue before any cmd 5 HTTP POST");
    assert.deepEqual(hostFailures, [], "scope revocation is not reported as a Host transport failure");
    assert.equal(hostClient.status().available, true);
    assert.equal(hostClient.status().counts.queued, 0);
    assert.equal(hostClient.status().counts.queuedBytes, 0);

    await controlService.stop();
    await bridge.stop();
    client.setBootstrapStatus({
      wrapperHashPinned: true,
      processPid: TARGET_PID,
      connectionGeneration: CONNECTION_GENERATION,
      listenerRegistered: true,
    });
    freshBridge = createQQCallBridge({
      provider: broker,
      host: hostClient,
      connectionGeneration: CONNECTION_GENERATION,
      hostGeneration: HOST_GENERATION,
      kernelPeerUidByUin: { [PEER_UIN]: KERNEL_UID },
      callTimeoutMs: 60_000,
    });
    freshControlService = createControlService({ bridge: freshBridge, token: controlToken, accountId: ACCOUNT_ID, port: 0 });
    const freshServiceStatus = await freshControlService.start();
    await freshBridge.attach();
    const freshHostStatus = hostClient.status();
    await waitForCallbackReadiness(freshHostStatus.outputPort);
    await loginThroughHost(freshBridge, freshHostStatus.outputPort, path.join(runtimeDir, "fresh-account"));
    await proveFreshCall(freshBridge, freshHostStatus.outputPort);
    const freshPoll = await postJson(freshServiceStatus.port, controlToken, "/v1/adapter/poll", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateHostQueueFreshClient",
      after: 0,
    });
    assert.equal(freshPoll.status, 200);
    assert.equal(freshPoll.body.currentCall.privateScopeProof?.inviteType, 1);
    const acceptsBefore = hostInvocations.filter((entry) => entry.command === 5).length;
    const positiveAccept = await postJson(freshServiceStatus.port, controlToken, "/v1/call/command", {
      accountId: ACCOUNT_ID,
      clientInstance: "qprivateHostQueueFreshClient",
      operation: "accept",
      scope: {
        serviceInstance: freshPoll.body.serviceInstance,
        hostGeneration: freshPoll.body.hostGeneration,
        connectionGeneration: freshPoll.body.connectionGeneration,
        callId: freshPoll.body.currentCall.callId,
        callGeneration: freshPoll.body.currentCall.generation,
      },
    });
    assert.equal(positiveAccept.status, 200);
    assert.equal(positiveAccept.body.result.posted, true);
    assert.equal(hostInvocations.filter((entry) => entry.command === 5).length, acceptsBefore + 1,
      "a current same-call proof reaches the real Host HTTP endpoint exactly once");
    assert.deepEqual(hostFailures, []);
  } finally {
    releaseFirst55();
    await freshControlService?.stop().catch(() => {});
    await freshBridge?.stop().catch(() => {});
    await controlService?.stop().catch(() => {});
    await bridge?.stop().catch(() => {});
    await hostClient.stop().catch(() => {});
    await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    await new Promise((resolve) => {
      fakeHostServer.closeAllConnections?.();
      fakeHostServer.close(resolve);
    });
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});
