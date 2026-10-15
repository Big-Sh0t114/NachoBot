"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { createBroker } = require("../probe/broker.cjs");
const { createPipeClient } = require("../probe/ipc.cjs");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");
const { createHostTransport } = require("../host/transport.cjs");
const { createHostClient } = require("../bridge/host-client.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");
const { createControlService } = require("../bridge/control-service.cjs");
const { PRIVATE_CALL_SCENE } = require("../host/private-end.cjs");

const TOKEN = "bridge-integration-token-0123456789abcdef0123456789AB";
const CONNECTION_GENERATION = 41;
const HOST_GENERATION = 73;
const CONTROL_TOKEN = Buffer.alloc(32, 0x37).toString("base64url");
const CONTROL_ACCOUNT_ID = "10001";

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("integration_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

function controlPoll(port) {
  const body = Buffer.from(JSON.stringify({ accountId: CONTROL_ACCOUNT_ID, clientInstance: "room-destroy-test", after: 0 }));
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: "/v1/adapter/poll",
      method: "POST",
      headers: {
        Authorization: `Bearer ${CONTROL_TOKEN}`,
        "Content-Type": "application/json",
        "Content-Length": body.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        try { resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }); }
        catch (error) { reject(error); }
      });
    });
    request.once("error", reject);
    request.end(body);
  });
}

test("Control poll journals an ended room-destroyed call before clearing currentCall", async (t) => {
  const providerHandlers = new Set();
  const hostHandlers = new Set();
  const provider = {
    async attach() { return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { return { ok: true, status: { listenerRegistered: false } }; },
    status() { return { connected: true, agent: { agent: { listenerRegistered: true } } }; },
    async forwardAVSDKOutput() { return { ok: true, result: { ok: true, forwarded: true } }; },
    subscribe(handler) { providerHandlers.add(handler); return () => providerHandlers.delete(handler); },
    async emitInvite(peerUid) {
      const event = { connectionGeneration: 61, method: "OnInviteActionToAVSDK", args: [{ relation_id: "kernel-private", invite_type: 3, from_uid: peerUid }] };
      await Promise.all([...providerHandlers].map((handler) => handler("kernel_event", event)));
    },
  };
  const host = {
    status() { return { available: true, hostGeneration: 51 }; },
    subscribe(handler) {
      hostHandlers.add(handler);
      handler({ type: "availability", available: true, hostGeneration: 51 });
      return () => hostHandlers.delete(handler);
    },
    async invoke(input) { return { posted: true, id: input.id, command: input.command }; },
    async selectAudioDevices(input) { return { selected: true, id: input.id, command: 102 }; },
    async emitOutput(command, id, value, hostGeneration = 51) {
      await Promise.all([...hostHandlers].map((handler) => handler({ type: "output", command, id, value, hostGeneration })));
    },
  };
  const bridge = createQQCallBridge({
    provider,
    host,
    connectionGeneration: 61,
    hostGeneration: 51,
    audioDeviceTarget: {
      inputDeviceName: "Line 1 (Virtual Audio Cable)",
      outputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
    },
  });
  const service = createControlService({ bridge, token: CONTROL_TOKEN, accountId: CONTROL_ACCOUNT_ID, port: 0 });
  t.after(async () => {
    await service.stop();
    try { await bridge.stop(); } catch { /* cleanup after assertions */ }
  });

  const started = await service.start();
  await bridge.attach();
  const login = await bridge.login({ selfUid: "u_private_self", selfUin: "10001", accountPath: "private-account-path" });
  await host.emitOutput(1, login.id, [0, ""]);
  await provider.emitInvite("u_private_peer");
  await host.emitOutput(20006, 0, [PRIVATE_CALL_SCENE, "u_private_peer", ["u_private_peer"], 3, "relation-private", null, null, null, null, null, true, "private-extra"]);
  const invite = bridge.getIntegrationSnapshot().call;
  const accepted = await bridge.accept(invite.callId);
  const enterRelationId = "enter-relation-private";
  await host.emitOutput(20004, 0, [0, PRIVATE_CALL_SCENE, enterRelationId, "u_private_peer", 0, "", 1]);
  await host.emitOutput(5, accepted.id, [0, ""]);
  await host.emitOutput(20011, 0, [PRIVATE_CALL_SCENE, enterRelationId, "room-private"]);

  const poll = await controlPoll(started.port);
  assert.equal(poll.status, 200);
  assert.equal(poll.body.currentCall, null, "the current-call slot is released in the same poll snapshot");
  const terminal = poll.body.events.find((event) => event.kind === "call_terminal");
  assert.ok(terminal);
  assert.equal(terminal.call.phase, "ended");
  assert.equal(terminal.call.uncertain, false);
  assert.equal(terminal.call.peerUid, "u_private_peer");
  const serialized = JSON.stringify(poll.body);
  for (const secret of ["relation-private", enterRelationId, "room-private", "kernel-private", "u_private_self", "private-account-path"]) {
    assert.equal(serialized.includes(secret), false, `Control poll excludes ${secret}`);
  }
});

test("Windows named-pipe Agent and real localhost Host transport complete explicit login/accept/detach", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for this integration test" : false,
}, async (t) => {
  const added = [];
  const removed = [];
  const forwardedActions = [];
  let transport;
  let bridge;
  let client;
  let broker;
  let hostClient;
  let agent;
  let transportStarted = false;
  let brokerStarted = false;

  const service = {
    addKernelAVSDKListener(listener) {
      added.push(listener);
      return undefined; // The pinned contract removes this original object.
    },
    removeKernelAVSDKListener(listener) {
      removed.push(listener);
      return undefined;
    },
    setActionFromAVSDK(type, value) {
      forwardedActions.push([type, value]);
    },
  };
  const wrapper = {
    NodeIQQNTWrapperSession: {
      getNTWrapperSession(name) {
        assert.equal(name, "nt_1");
        return { getAVSDKService: () => service };
      },
    },
  };

  t.after(async () => {
    if (bridge) {
      try { await bridge.stop(); } catch { /* keep this cleanup best-effort after the assertion */ }
    }
    if (broker && brokerStarted) {
      try { await broker.stop({ confirmDetach: false }); } catch { /* local pipe cleanup */ }
    }
    if (client) {
      try { await client.close(); } catch { /* local pipe cleanup */ }
    }
    if (transport && transportStarted) {
      try { await transport.stop(); } catch { /* local HTTP cleanup */ }
    }
    if (hostClient) {
      try { await hostClient.stop(); } catch { /* local HTTP cleanup */ }
    }
  });

  const hostPort = await reservePort();
  const reportPath = path.join(__dirname, ".runtime", `bridge-integration-${process.pid}`);
  broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath,
    connectionGeneration: CONNECTION_GENERATION,
    ipcToken: TOKEN,
    maxLifetimeMs: 60_000,
  });
  await broker.start();
  brokerStarted = true;

  const readiness = { wrapperFound: true, sessionFound: true, serviceAvailable: true, listenerRegistered: false };
  agent = createKernelAgent({
    wrapper,
    connectionGeneration: CONNECTION_GENERATION,
    removalMode: "listener-object",
    emit: (event) => client.publishEvent("kernel_event", event),
  });
  client = createPipeClient({
    pipeName: broker.pipeName,
    connectionGeneration: CONNECTION_GENERATION,
    ipcToken: TOKEN,
  });
  client.setAgent(agent);
  await client.connect();
  assert.equal((await agent.probe()).ok, true);
  client.setBootstrapStatus({ ...readiness, connectionGeneration: CONNECTION_GENERATION });
  client.publishEvent("bootstrap_status", { ...readiness, connectionGeneration: CONNECTION_GENERATION });

  hostClient = createHostClient({
    token: TOKEN,
    listenPort: hostPort,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    requestTimeoutMs: 3000,
    outputTimeoutMs: 3000,
    healthPollMs: 10_000,
  });
  await hostClient.start();

  const postedCommands = [];
  transport = createHostTransport({
    token: TOKEN,
    listenPort: hostPort,
    outputPort: hostClient.status().outputPort,
    hostGeneration: HOST_GENERATION,
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    async invokePlugin(message) {
      postedCommands.push({ command: message.command, id: message.id, params: message.params });
      if (message.command === 1) {
        await transport.publishOutput({ command: 20001, id: 0, value: [17, "pre-login-output"], hostGeneration: HOST_GENERATION });
        await transport.publishOutput({ command: 1, id: message.id, value: [0, ""], hostGeneration: HOST_GENERATION });
      } else if (message.command === 5) {
        await transport.publishOutput({ command: 20004, id: message.id, value: [0, "room-entry-observed"], hostGeneration: HOST_GENERATION });
        await transport.publishOutput({ command: 5, id: message.id, value: [0, ""], hostGeneration: HOST_GENERATION });
      }
      return { posted: true, id: message.id, command: message.command };
    },
  });
  await transport.start();
  transportStarted = true;
  let hostReady = false;
  for (let attempt = 0; attempt < 20 && !hostReady; attempt += 1) {
    hostReady = await hostClient.checkHealth();
    if (!hostReady) await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(hostReady, true);
  bridge = createQQCallBridge({
    provider: broker,
    host: hostClient,
    connectionGeneration: CONNECTION_GENERATION,
    hostGeneration: HOST_GENERATION,
    callTimeoutMs: 5000,
  });

  const attached = await bridge.attach();
  assert.equal(attached.attached, true);
  assert.equal(added.length, 1);
  const kernelListener = added[0];
  const login = await bridge.login({ selfUid: "mock-uid", selfUin: "10001", accountPath: "C:\\mock-account" });
  assert.equal(login.posted, true);
  assert.deepEqual(forwardedActions, [[17, "pre-login-output"]], "20001 is forwarded even while login is awaiting its ACK");
  assert.equal(bridge.status().loginState, "ready");

  await kernelListener.OnInviteActionToAVSDK({ relation_id: "relation-a", invite_type: 1, from_uid: "mock-caller" }, 1, "invite");
  await kernelListener.OnActionToAVSDK(29, "kernel-signal");
  await waitFor(() => postedCommands.some((message) => message.command === 55 && message.params[0] === 29));
  assert.equal(postedCommands.some((message) => message.command === 55 && message.params[0] === 29), true);

  const invite = [1, "mock-caller", "mock-self", 2, "opaque-room", null, null, null, null, null, true, "opaque-extra"];
  await transport.publishOutput({ command: 20006, id: 0, value: invite, hostGeneration: HOST_GENERATION });
  await waitFor(() => bridge.status().activeCall?.phase === "invite_ready");
  assert.equal(bridge.status().activeCall.inviteCorrelation, "kernel_from_uid_matched");
  const callId = bridge.status().activeCall.callId;
  const accepted = await bridge.accept(callId);
  assert.equal(accepted.posted, true);
  await waitFor(() => bridge.status().activeCall?.phase === "connected");
  assert.deepEqual(bridge.status().activeCall, {
    callId,
    generation: bridge.status().activeCall.generation,
    phase: "connected",
    uncertain: false,
    inviteCallbackReady: true,
    inviteCorrelation: "kernel_from_uid_matched",
    acceptState: "connected",
    connected: true,
    remoteEndedConfirmed: false,
    observedDestroyReason: false,
  });
  assert.equal(postedCommands.some((message) => message.command === 5 && message.params[2][0] === "mock-caller"), true);

  const stopped = await bridge.stop();
  assert.deepEqual(stopped, { stopped: true, detachedConfirmed: true });
  assert.equal(removed.length, 1);
  assert.equal(removed[0], kernelListener, "detach passes the exact listener object registered earlier");
  const brokerStopped = await broker.stop({ confirmDetach: false });
  assert.equal(brokerStopped.detachedConfirmed, true);
  brokerStarted = false;
});
