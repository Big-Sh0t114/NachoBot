"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const test = require("node:test");
const { createControlService } = require("../bridge/control-service.cjs");

const TOKEN = Buffer.alloc(32, 0x47).toString("base64url");
const ACCOUNT_ID = "10001";
const CLIENT = "audio-adapter-1";

function call(overrides = {}) {
  return {
    callId: "7156ab16-c2c3-4f7f-987f-620145dce8df",
    generation: 7,
    peerUid: "private-peer-uid",
    phase: "connected",
    uncertain: false,
    inviteCallbackReady: true,
    inviteCorrelation: "kernel_from_uid_matched",
    acceptState: "acknowledged",
    ...overrides,
  };
}

function makeBridge(initialCall = call()) {
  let currentCall = initialCall ? { ...initialCall } : null;
  let status = { hostGeneration: 51, connectionGeneration: 61 };
  const subscribers = new Set();
  const controlSubscribers = new Set();
  const invoked = { hangup: [] };
  return {
    status: () => ({ ...status }),
    getIntegrationSnapshot: () => ({ status: { ...status }, call: currentCall ? { ...currentCall } : null }),
    subscribeStatus(handler) { subscribers.add(handler); return () => subscribers.delete(handler); },
    subscribeControlEvents(handler) { controlSubscribers.add(handler); return () => controlSubscribers.delete(handler); },
    setCall(next) { currentCall = next ? { ...next } : null; for (const handler of subscribers) handler({ kind: "call_changed" }); },
    async accept(callId) { return { posted: true, id: 91, callId, callGeneration: currentCall?.generation }; },
    async hangup(callId) { invoked.hangup.push(callId); return { posted: true, id: 92, callId, callGeneration: currentCall?.generation }; },
    async reject(callId) { return { posted: true, id: 93, callId, callGeneration: currentCall?.generation }; },
    _invoked: invoked,
  };
}

function request(port, route, body, { token = TOKEN } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = Buffer.from(JSON.stringify(body));
    const req = http.request({
      hostname: "127.0.0.1",
      port,
      path: route,
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "Content-Length": bytes.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        try { resolve({ status: response.statusCode, json: JSON.parse(text) }); }
        catch { reject(new Error("invalid_control_response")); }
      });
    });
    req.once("error", reject);
    req.end(bytes);
  });
}

function scopeFrom(poll, currentCall = poll.currentCall) {
  return {
    serviceInstance: poll.serviceInstance,
    hostGeneration: poll.hostGeneration,
    connectionGeneration: poll.connectionGeneration,
    callId: currentCall.callId,
    callGeneration: currentCall.generation,
  };
}

function routeBody(poll, overrides = {}) {
  return {
    accountId: ACCOUNT_ID,
    clientInstance: CLIENT,
    scope: scopeFrom(poll),
    captureRenderName: "Voicemeeter In 5",
    playbackRenderName: "Line 1",
    ...overrides,
  };
}

function routeProof() {
  return {
    verified: true,
    source: "windows_active_sessions",
    hostPid: 4101,
    observedAtMs: Date.now(),
    hostInput: {
      deviceName: "Line 1", flow: "capture", state: "active",
      endpointIdHash: "a".repeat(64), processId: 4102,
    },
    hostOutput: {
      deviceName: "Voicemeeter In 5", flow: "render", state: "active",
      endpointIdHash: "b".repeat(64), processId: 4101,
    },
  };
}

async function startService(t, bridge, audioRouteObserver) {
  const service = createControlService({ bridge, token: TOKEN, accountId: ACCOUNT_ID, port: 0, audioRouteObserver });
  const status = await service.start();
  t.after(() => service.stop());
  return { service, port: status.port };
}

async function acquire(servicePort, clientInstance = CLIENT) {
  const result = await request(servicePort, "/v1/adapter/poll", {
    accountId: ACCOUNT_ID, clientInstance, after: 0,
  });
  assert.equal(result.status, 200);
  return result.json;
}

test("audio route endpoint authenticates, validates account/lease/full scope and returns unavailable without an observer", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await startService(t, bridge);
  const poll = await acquire(port);
  const body = routeBody(poll);

  const unauthorized = await request(port, "/v1/audio/route", body, { token: "x".repeat(TOKEN.length) });
  assert.equal(unauthorized.status, 401);
  assert.deepEqual(unauthorized.json, { ok: false, code: "unauthorized" });

  const wrongAccount = await request(port, "/v1/audio/route", { ...body, accountId: "10002" });
  assert.equal(wrongAccount.status, 409);
  assert.deepEqual(wrongAccount.json, { ok: false, code: "account_mismatch" });
  const noLease = await request(port, "/v1/audio/route", { ...body, clientInstance: "adapter-2" });
  assert.equal(noLease.status, 409);
  assert.deepEqual(noLease.json, { ok: false, code: "adapter_owned" });

  const stale = await request(port, "/v1/audio/route", {
    ...body, scope: { ...body.scope, callGeneration: body.scope.callGeneration + 1 },
  });
  assert.equal(stale.status, 409);
  assert.deepEqual(stale.json, { ok: false, code: "call_scope_stale" });
  const invalidShape = await request(port, "/v1/audio/route", { ...body, ignored: "must-not-echo" });
  assert.equal(invalidShape.status, 400);
  assert.deepEqual(invalidShape.json, { ok: false, code: "invalid_audio_route_shape" });

  const unavailable = await request(port, "/v1/audio/route", body);
  assert.equal(unavailable.status, 200);
  assert.deepEqual(unavailable.json, {
    version: 1,
    accountId: ACCOUNT_ID,
    scope: body.scope,
    verified: false,
    code: "host_route_unverified",
  });
  assert.deepEqual(bridge._invoked.hangup, []);

  bridge.setCall(call({ phase: "invite_ready" }));
  const notConnected = await request(port, "/v1/audio/route", body);
  assert.equal(notConnected.status, 409);
  assert.deepEqual(notConnected.json, { ok: false, code: "call_not_connected" });
  assert.equal(service.status().leaseActive, true);
});

test("observer proof is scope-bound; route observation does not block call commands and pins lease against release", async (t) => {
  const bridge = makeBridge();
  let startedResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  let finishObservation;
  const observer = async (requestContext) => {
    assert.equal(requestContext.accountId, ACCOUNT_ID);
    assert.equal(requestContext.clientInstance, CLIENT);
    assert.deepEqual(requestContext.scope.callId, call().callId);
    assert.equal(requestContext.captureRenderName, "Voicemeeter In 5");
    assert.equal(requestContext.playbackRenderName, "Line 1");
    startedResolve();
    return new Promise((resolve) => { finishObservation = resolve; });
  };
  const { service, port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const body = routeBody(poll);
  const routePromise = request(port, "/v1/audio/route", body);
  await started;

  const release = await request(port, "/v1/adapter/release", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(release.status, 409);
  assert.deepEqual(release.json, { ok: false, code: "adapter_command_in_flight" });

  const command = await request(port, "/v1/call/command", {
    accountId: ACCOUNT_ID,
    clientInstance: CLIENT,
    operation: "hangup",
    scope: body.scope,
  });
  assert.equal(command.status, 200, "audio observation is independent of the serialized command lane");
  assert.equal(bridge._invoked.hangup.length, 1);

  const proof = routeProof();
  finishObservation(proof);
  const verified = await routePromise;
  assert.equal(verified.status, 200);
  assert.deepEqual(verified.json, {
    version: 1,
    accountId: ACCOUNT_ID,
    scope: body.scope,
    ...proof,
  });
  assert.equal(verified.json.verified, true);
  assert.equal(verified.json.hostInput.processId, 4102);
  assert.equal(service.status().leaseActive, true);
});

test("control watchdog allows a successful observer result after two seconds", async (t) => {
  const bridge = makeBridge();
  const observer = async () => {
    await new Promise((resolve) => setTimeout(resolve, 2050));
    return routeProof();
  };
  const { port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const response = await request(port, "/v1/audio/route", routeBody(poll));
  assert.equal(response.status, 200);
  assert.equal(response.json.verified, true);
  assert.equal(response.json.hostInput.processId, 4102);
});

test("scope change while observation is pending discards the late proof", async (t) => {
  const bridge = makeBridge();
  let startedResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  let finishObservation;
  const observer = async () => {
    startedResolve();
    return new Promise((resolve) => { finishObservation = resolve; });
  };
  const { port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const body = routeBody(poll);
  const routePromise = request(port, "/v1/audio/route", body);
  await started;
  bridge.setCall(call({ callId: "a8c3d202-7dc0-4d59-90f5-99b25e221177", generation: 8 }));
  finishObservation(routeProof());
  const stale = await routePromise;
  assert.equal(stale.status, 409);
  assert.deepEqual(stale.json, { ok: false, code: "call_scope_stale" });
});

test("observer timeout returns a negative proof but retains the lease pin until late work settles", async (t) => {
  const bridge = makeBridge();
  let startedResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  let finishObservation;
  const observer = async ({ signal }) => {
    startedResolve();
    assert.equal(signal.aborted, false);
    return new Promise((resolve) => { finishObservation = resolve; });
  };
  const { port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const body = routeBody(poll);
  const routePromise = request(port, "/v1/audio/route", body);
  await started;
  const timedOut = await routePromise;
  assert.equal(timedOut.status, 200);
  assert.deepEqual(timedOut.json, {
    version: 1, accountId: ACCOUNT_ID, scope: body.scope, verified: false, code: "observer_timeout",
  });
  const blockedRelease = await request(port, "/v1/adapter/release", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(blockedRelease.status, 409);
  assert.deepEqual(blockedRelease.json, { ok: false, code: "adapter_command_in_flight" });

  finishObservation(routeProof());
  const deadline = Date.now() + 1500;
  let released;
  while (!released && Date.now() < deadline) {
    const result = await request(port, "/v1/adapter/release", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
    if (result.status === 200) released = result;
    else assert.deepEqual(result.json, { ok: false, code: "adapter_command_in_flight" });
    if (!released) await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.ok(released, "late observer work eventually releases its inflight lease pin");
  assert.deepEqual(released.json, {
    version: 1, accountId: ACCOUNT_ID, serviceInstance: poll.serviceInstance, released: true,
  });
});

test("adapter pause aborts old audio observations and fences late proofs across resume", async (t) => {
  const bridge = makeBridge();
  let startedResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  let aborted = false;
  const observer = async ({ signal }) => {
    startedResolve();
    return new Promise((resolve) => signal.addEventListener("abort", () => {
      aborted = true;
      resolve(routeProof());
    }, { once: true }));
  };
  const { service, port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const pending = request(port, "/v1/audio/route", routeBody(poll));
  await started;
  await service.setAdapterAvailable(false);
  const old = await pending;
  assert.equal(aborted, true);
  assert.equal(old.status, 409);
  assert.equal(old.json.code, "adapter_unavailable");
  assert.equal(Object.hasOwn(old.json, "verified"), false);
  await service.setAdapterAvailable(true);
  const fresh = await acquire(port);
  assert.equal(fresh.serviceInstance, poll.serviceInstance);
  assert.equal(fresh.hostGeneration, poll.hostGeneration);
});

test("service stop aborts a late observer and never sends its proof to the adapter", async (t) => {
  const bridge = makeBridge();
  let startedResolve;
  const started = new Promise((resolve) => { startedResolve = resolve; });
  let observerStopped = false;
  const observer = async ({ signal }) => {
    startedResolve();
    return new Promise((resolve) => {
      signal.addEventListener("abort", () => resolve(routeProof()), { once: true });
    });
  };
  observer.stop = async () => { observerStopped = true; };
  const { service, port } = await startService(t, bridge, observer);
  const poll = await acquire(port);
  const routePromise = request(port, "/v1/audio/route", routeBody(poll));
  await started;
  await service.stop();
  const stopped = await routePromise;
  assert.equal(observerStopped, true);
  assert.equal(stopped.status, 503);
  assert.deepEqual(stopped.json, { ok: false, code: "service_stopped" });
});
