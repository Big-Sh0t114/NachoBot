"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { createHostClient, hostReady } = require("../bridge/host-client.cjs");

const TOKEN = "local-test-token-that-is-long-enough-0123456789";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function waitFor(predicate, timeoutMs = 1500) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function startFakeHost(handler, hostGeneration = 72) {
  const server = http.createServer((request, response) => {
    void (async () => {
      const raw = request.method === "POST" ? await readBody(request) : "";
      const record = { method: request.method, url: request.url, authorization: request.headers.authorization, raw };
      await handler(request, response, record);
    })().catch(() => {
      if (!response.destroyed && !response.writableEnded) response.writeHead(500).end();
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return { server, port: server.address().port, hostGeneration };
}

function reply(response, status, value) {
  const bytes = Buffer.from(JSON.stringify(value));
  response.writeHead(status, { "content-type": "application/json", "content-length": bytes.length });
  response.end(bytes);
}

function readyBody(hostGeneration, overrides = {}) {
  return {
    ok: true,
    status: {
      hostGeneration,
      ready: true,
      pluginFound: true,
      hasPostMessage: true,
      rendererGone: false,
      stopped: false,
      outputFailed: false,
      readinessError: null,
      ...overrides,
    },
  };
}

test("private reject absence validates exact correlated receipts and current scope before and after the read", async (t) => {
  let mutation = null;
  let requests = 0;
  const fake = await startFakeHost(async (request, response, record) => {
    if (request.method === "GET") return reply(response, 200, readyBody(72));
    requests += 1;
    assert.equal(record.url, "/v1/private/reject-absence");
    assert.equal(record.authorization, `Bearer ${TOKEN}`);
    const result = { ...JSON.parse(record.raw), ok: true, confirmed: true, source: "native_reject_ack_and_sdk_room_absence" };
    if (mutation) mutation(result);
    reply(response, 200, result);
  });
  t.after(() => new Promise(resolve => fake.server.close(resolve)));
  const client = createHostClient({ token: TOKEN, listenPort: fake.port, outputPort: 0, hostGeneration: 72, healthPollMs: 10_000 });
  t.after(() => client.stop());
  await client.start();
  await client.checkHealth();
  const input = { id: 91, hostGeneration: 72, peerUid: `u_${"P".repeat(22)}`, rejectRequestId: 90 };
  assert.equal((await client.confirmPrivateRejectAbsence(input, { assertCurrent: () => true })).confirmed, true);
  mutation = result => { result.source = "native_reject_ack_and_no_room_admission"; };
  assert.equal((await client.confirmPrivateRejectAbsence(input, { assertCurrent: () => true })).confirmed, true);
  for (const key of ["id", "hostGeneration", "peerUid", "rejectRequestId", "source", "confirmed"]) {
    mutation = result => { result[key] = "wrong"; };
    await assert.rejects(client.confirmPrivateRejectAbsence(input, { assertCurrent: () => true }), { code: "private_reject_proof_invalid" });
  }
  mutation = null;
  let checks = 0;
  await assert.rejects(client.confirmPrivateRejectAbsence(input, { assertCurrent: () => ++checks === 1 }), { code: "private_end_scope_unverified" });
  assert.equal(checks, 2);
  const before = requests;
  await assert.rejects(client.confirmPrivateRejectAbsence(input, { assertCurrent: () => false }), { code: "private_end_scope_unverified" });
  assert.equal(requests, before);
});

test("private entered-room absence binds its exact entry and rejects changed scope receipts", async (t) => {
  let mutation = null;
  const fake = await startFakeHost(async (request, response, record) => {
    if (request.method === "GET") return reply(response, 200, readyBody(72));
    assert.equal(record.url, "/v1/private/ended-room-absence");
    assert.equal(record.authorization, `Bearer ${TOKEN}`);
    const result = { ...JSON.parse(record.raw), ok: true, confirmed: true, source: "native_entered_private_room_absence" };
    if (mutation) mutation(result);
    reply(response, 200, result);
  });
  t.after(() => new Promise(resolve => fake.server.close(resolve)));
  const client = createHostClient({ token: TOKEN, listenPort: fake.port, outputPort: 0, hostGeneration: 72, healthPollMs: 10_000 });
  t.after(() => client.stop());
  await client.start();
  await client.checkHealth();
  const input = { id: 99, hostGeneration: 72, peerUid: `u_${"P".repeat(22)}`, enterRelationId: "entered-room" };
  assert.equal((await client.confirmPrivateEndedRoomAbsence(input, { assertCurrent: () => true })).confirmed, true);
  for (const key of ["id", "hostGeneration", "peerUid", "enterRelationId", "source", "confirmed"]) {
    mutation = result => { result[key] = "wrong"; };
    await assert.rejects(client.confirmPrivateEndedRoomAbsence(input, { assertCurrent: () => true }), { code: "private_room_end_proof_invalid" });
  }
  mutation = null;
  let checks = 0;
  await assert.rejects(client.confirmPrivateEndedRoomAbsence(input, { assertCurrent: () => ++checks === 1 }), { code: "private_end_scope_unverified" });
});

test("typed local quit proof validates exact receipts and rechecks the dispatch guard", async (t) => {
  let mutation = null;
  let requests = 0;
  const fake = await startFakeHost(async (request, response, record) => {
    if (request.method === "GET") return reply(response, 200, readyBody(72, {
      groupCallProtocolAvailable: true, groupLocalQuitProofAvailable: true }));
    requests += 1;
    const body = JSON.parse(record.raw);
    assert.equal(record.url, "/v1/group/quit-absence");
    assert.equal(record.authorization, `Bearer ${TOKEN}`);
    const result = { ...body, ok: true, confirmed: true, source: "native_sdk_room_id_readback" };
    if (mutation) mutation(result);
    reply(response, 200, result);
  });
  t.after(() => new Promise((resolve) => fake.server.close(resolve)));
  const client = createHostClient({ token: TOKEN, listenPort: fake.port, outputPort: 0, hostGeneration: 72, healthPollMs: 10_000 });
  t.after(() => client.stop());
  await client.start();
  await client.checkHealth();
  const input = { id: 91, hostGeneration: 72, groupId: "20002", startRequestId: 89, quitRequestId: 90 };
  assert.equal((await client.confirmGroupQuitAbsence(input, { assertCurrent: () => true })).confirmed, true);
  for (const field of ["id", "hostGeneration", "groupId", "startRequestId", "quitRequestId", "source"]) {
    mutation = (result) => { result[field] = "wrong"; };
    await assert.rejects(client.confirmGroupQuitAbsence(input, { assertCurrent: () => true }), { code: "group_quit_proof_invalid" });
  }
  mutation = (result) => { result.extra = "unexpected"; };
  await assert.rejects(client.confirmGroupQuitAbsence(input, { assertCurrent: () => true }), { code: "group_quit_proof_invalid" });
  mutation = null;
  let checks = 0;
  await assert.rejects(client.confirmGroupQuitAbsence(input, { assertCurrent: () => ++checks === 1 }), { code: "host_invoke_guard_rejected" });
  assert.equal(checks, 2);
  const before = requests;
  await assert.rejects(client.confirmGroupQuitAbsence(input, { assertCurrent: () => false }), { code: "host_invoke_guard_rejected" });
  assert.equal(requests, before);
});

function makeClient(fake, overrides = {}) {
  return createHostClient({
    token: TOKEN,
    listenPort: fake.port,
    outputPort: 0,
    hostGeneration: fake.hostGeneration,
    requestTimeoutMs: 1000,
    outputTimeoutMs: 150,
    healthPollMs: 10_000,
    bodyTimeoutMs: 200,
    ...overrides,
  });
}

function requestJson(port, pathname, { method = "POST", token = TOKEN, body, rawBody, headers = {} } = {}) {
  const bytes = rawBody !== undefined ? Buffer.from(rawBody) : body === undefined ? null : Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1",
      port,
      method,
      path: pathname,
      headers: {
        ...(token === null ? {} : { authorization: `Bearer ${token}` }),
        ...(bytes ? { "content-length": bytes.length, "content-type": "application/json" } : {}),
        ...headers,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        raw: Buffer.concat(chunks).toString("utf8"),
      }));
    });
    request.once("error", reject);
    if (bytes) request.write(bytes);
    request.end();
  });
}

test("hostReady requires explicit ready, exact generation, and clean plugin/renderer state", () => {
  assert.equal(hostReady(readyBody(72), 72), true);
  assert.equal(hostReady(readyBody(72, { targetPid: 456 }), 72, 456), true);
  assert.equal(hostReady(readyBody(72, { targetPid: 457 }), 72, 456), false, "health must name the pinned Host PID");
  assert.equal(hostReady(readyBody(72), 72, 456), false, "health without a PID is not accepted when one is pinned");
  assert.equal(hostReady({ status: { hostGeneration: 72, pluginFound: true, hasPostMessage: true } }, 72), false);
  assert.equal(hostReady(readyBody(73), 72), false);
  assert.equal(hostReady(readyBody(72, { ready: false }), 72), false);
  assert.equal(hostReady(readyBody(72, { outputFailed: true }), 72), false);
  assert.equal(hostReady(readyBody(72, { readinessError: "plugin_missing" }), 72), false);
  assert.equal(hostReady(readyBody(72, { stopped: true }), 72), false);
  assert.equal(hostReady(readyBody(72, { rendererGone: true }), 72), false);
});

test("callback listener starts before Host health and advertises only a real subscriber", async (t) => {
  const fake = await startFakeHost((_request, response) => reply(response, 503, readyBody(72, { ready: false, targetPid: 456 })), 72);
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { targetPid: 456 });
  client.subscribe(() => {});
  const started = await client.start();
  assert.equal(started.callbackReady, true);
  assert.equal(started.available, false, "Host health remains independent of callback-listener readiness");
  const callback = await requestJson(started.outputPort, "/v1/callback-ready", {
    method: "GET",
    headers: { "x-qqcall-host-generation": "72" },
  });
  assert.equal(callback.status, 200);
  assert.deepEqual(JSON.parse(callback.raw), {
    ok: true,
    callbackReady: true,
    hostGeneration: 72,
    started: true,
    subscriberReady: true,
    stopping: false,
    stopped: false,
  });
  await client.stop();
});

test("client binds callback on loopback ephemeral port and accepts only matching ready Host generation", async (t) => {
  let readiness = readyBody(72, { ready: false });
  let healthRequests = 0;
  const fake = await startFakeHost((request, response, record) => {
    assert.equal(record.authorization, `Bearer ${TOKEN}`);
    if (request.url === "/healthz") {
      healthRequests += 1;
      reply(response, readiness.ok ? 200 : 503, readiness);
      return;
    }
    reply(response, 404, { code: "not_found" });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });

  const client = makeClient(fake);
  t.after(() => client.stop());
  const initial = await client.start();
  assert.equal(initial.outputPort > 0, true);
  assert.equal(await client.checkHealth(), false);
  readiness = readyBody(72);
  assert.equal(await client.checkHealth(), true);
  assert.equal(healthRequests >= 2, true);
  assert.equal(client.status().available, true);
});

test("authenticated callback readiness requires the exact Host generation and an active output subscriber", async (t) => {
  const fake = await startFakeHost((_request, response) => reply(response, 404, { code: "not_found" }));
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake);
  t.after(() => client.stop());
  const started = await client.start();
  const endpoint = (token = TOKEN, generation = 72) => requestJson(started.outputPort, "/v1/callback-ready", {
    method: "GET",
    token,
    headers: { "x-qqcall-host-generation": String(generation) },
  });

  assert.equal(client.status().callbackReady, false);
  assert.equal((await endpoint(null)).status, 401, "readiness requires the private callback token");
  const wrongGeneration = await endpoint(TOKEN, 73);
  assert.equal(wrongGeneration.status, 409);
  assert.equal(JSON.parse(wrongGeneration.raw).code, "generation_mismatch");
  const withoutSubscriber = await endpoint();
  assert.equal(withoutSubscriber.status, 503);
  assert.equal(JSON.parse(withoutSubscriber.raw).subscriberReady, false);

  const unsubscribe = client.subscribe(() => {});
  assert.equal(client.status().callbackReady, true);
  const ready = await endpoint();
  assert.equal(ready.status, 200);
  assert.equal(JSON.parse(ready.raw).callbackReady, true);
  assert.equal(JSON.parse(ready.raw).hostGeneration, 72);
  assert.equal(unsubscribe(), true);
  const afterUnsubscribe = await endpoint();
  assert.equal(afterUnsubscribe.status, 503);
  assert.equal(JSON.parse(afterUnsubscribe.raw).subscriberReady, false);
});

test("invoke requests are bounded FIFO, preserve IDs, and accept only matching posted receipts", async (t) => {
  const gate = deferred();
  const firstSeen = deferred();
  const invocations = [];
  const fake = await startFakeHost(async (_request, response, record) => {
    if (record.url === "/healthz") {
      reply(response, 200, readyBody(72));
      return;
    }
    const body = JSON.parse(record.raw);
    invocations.push(body);
    if (body.id === 101) {
      firstSeen.resolve();
      await gate.promise;
    }
    reply(response, 200, { posted: true, id: body.id, command: body.command });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake);
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);

  const first = client.invoke({ command: 1, id: 101, params: ["a", "b", "b", "p", ""], hostGeneration: 72 });
  await firstSeen.promise;
  const second = client.invoke({ command: 55, id: 102, params: [4, "x"], hostGeneration: 72 });
  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), [
    { posted: true, id: 101, command: 1 },
    { posted: true, id: 102, command: 55 },
  ]);
  assert.deepEqual(invocations.map((item) => item.id), [101, 102]);
  assert.deepEqual(invocations.map((item) => item.hostGeneration), [72, 72]);
});

test("queued invoke guard runs at actual HTTP dispatch and rejects only the stale item", async (t) => {
  const gate = deferred();
  const firstSeen = deferred();
  const invokeBodies = [];
  const failures = [];
  const fake = await startFakeHost(async (_request, response, record) => {
    if (record.url === "/healthz") {
      reply(response, 200, readyBody(72));
      return;
    }
    const body = JSON.parse(record.raw);
    invokeBodies.push(body);
    if (body.id === 901) {
      firstSeen.resolve();
      await gate.promise;
    }
    reply(response, 200, { posted: true, id: body.id, command: body.command });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { onFailure: (event) => failures.push(event.code) });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);

  const first = client.invoke({ command: 55, id: 901, params: [4, "held"], hostGeneration: 72 });
  await firstSeen.promise;
  let proofCurrent = true;
  const scoped = client.invoke(
    { command: 5, id: 902, params: ["opaque"], hostGeneration: 72 },
    { assertCurrent: () => {
      if (!proofCurrent) {
        const error = new Error("private scope expired");
        error.code = "private_scope_unverified";
        throw error;
      }
      return true;
    } },
  );
  const ordinaryAfter = client.invoke({ command: 55, id: 903, params: [4, "continues"], hostGeneration: 72 });
  await waitFor(() => client.status().counts.queued === 2);

  proofCurrent = false;
  gate.resolve();
  assert.deepEqual(await first, { posted: true, id: 901, command: 55 });
  await assert.rejects(scoped, { code: "private_scope_unverified" });
  assert.deepEqual(await ordinaryAfter, { posted: true, id: 903, command: 55 });
  assert.deepEqual(invokeBodies.map((body) => ({ id: body.id, command: body.command })), [
    { id: 901, command: 55 },
    { id: 903, command: 55 },
  ]);
  assert.equal(client.status().available, true);
  assert.equal(client.status().counts.queued, 0);
  assert.equal(client.status().counts.queuedBytes, 0);
  assert.deepEqual(failures, []);
});

test("invoke dispatch guards must be synchronous booleans and do not report Host failures", async (t) => {
  const invokeBodies = [];
  const failures = [];
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") reply(response, 200, readyBody(72));
    else {
      invokeBodies.push(JSON.parse(record.raw));
      const body = JSON.parse(record.raw);
      reply(response, 200, { posted: true, id: body.id, command: body.command });
    }
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { onFailure: (event) => failures.push(event.code) });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);

  await assert.rejects(client.invoke(
    { command: 5, id: 911, params: [], hostGeneration: 72 },
    { assertCurrent: () => false },
  ), { code: "host_invoke_guard_rejected" });
  await assert.rejects(client.invoke(
    { command: 5, id: 912, params: [], hostGeneration: 72 },
    { assertCurrent: () => Promise.reject(new Error("consumed guard rejection")) },
  ), { code: "host_invoke_guard_async" });
  await assert.rejects(client.invoke(
    { command: 5, id: 913, params: [], hostGeneration: 72 },
    { assertCurrent: () => true, extra: "invalid" },
  ), { code: "invalid_invoke_guard" });
  assert.deepEqual(invokeBodies, []);
  assert.equal(client.status().available, true);
  assert.equal(client.status().counts.queued, 0);
  assert.equal(client.status().counts.queuedBytes, 0);
  assert.deepEqual(failures, []);
});

test("named device selection uses only the dedicated authenticated route and generic invoke stays closed", async (t) => {
  const requests = [];
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") {
      reply(response, 200, readyBody(72));
      return;
    }
    requests.push(record);
    const body = JSON.parse(record.raw);
    reply(response, 200, { selected: true, id: body.id, command: 102 });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake);
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);

  const selection = await client.selectAudioDevices({
    id: 813,
    hostGeneration: 72,
    inputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
    outputDeviceName: "Line 1 (Virtual Audio Cable)",
  });
  assert.deepEqual(selection, { selected: true, id: 813, command: 102 });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/v1/audio/select");
  assert.equal(requests[0].authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(JSON.parse(requests[0].raw), {
    id: 813,
    hostGeneration: 72,
    inputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
    outputDeviceName: "Line 1 (Virtual Audio Cable)",
  });
  for (const command of [102, 103]) {
    await assert.rejects(client.invoke({ command, id: 814, params: [], hostGeneration: 72 }), { code: "invalid_invoke" });
  }
  assert.equal(requests.length, 1);
});

test("HostClient forwards only the private-end candidates to transport and preserves its typed capability refusal", async (t) => {
  const bodies = [];
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") {
      reply(response, 200, readyBody(72));
      return;
    }
    const body = JSON.parse(record.raw);
    bodies.push(body);
    reply(response, 501, { ok: false, code: "private_end_protocol_unverified" });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake);
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);

  const peerUid = `u_${"P".repeat(22)}`;
  await assert.rejects(client.invoke({ command: 8, id: 801, params: [1, 0], hostGeneration: 72 }), {
    code: "private_end_protocol_unverified",
  });
  await assert.rejects(client.invoke({ command: 9, id: 802, params: [1, peerUid, 1], hostGeneration: 72 }), {
    code: "private_end_protocol_unverified",
  });
  await assert.rejects(client.invoke({ command: 10, id: 803, params: [1, peerUid, 0], hostGeneration: 72 }), {
    code: "private_end_protocol_unverified",
  });
  await assert.rejects(client.invoke({ command: 3, id: 804, params: [1, peerUid, 0], hostGeneration: 72 }), {
    code: "invalid_invoke",
  });
  assert.deepEqual(bodies, [
    { command: 8, id: 801, params: [1, 0], hostGeneration: 72 },
    { command: 9, id: 802, params: [1, peerUid, 1], hostGeneration: 72 },
    { command: 10, id: 803, params: [1, peerUid, 0], hostGeneration: 72 },
  ]);
  assert.equal(client.status().available, true, "a typed protocol refusal does not invalidate Host readiness");
});

test("named device selection preserves typed host refusal and does not retry", async (t) => {
  let selections = 0;
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") {
      reply(response, 200, readyBody(72));
      return;
    }
    selections += 1;
    reply(response, 409, { ok: false, code: "audio_device_selection_mismatch" });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake);
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  await assert.rejects(client.selectAudioDevices({
    id: 815, hostGeneration: 72, inputDeviceName: "in", outputDeviceName: "out",
  }), { code: "audio_device_selection_mismatch" });
  assert.equal(selections, 1);
});

test("authenticated output accepts event id zero, awaits observers, and rejects malformed/stale/oversized bodies", async (t) => {
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") reply(response, 200, readyBody(72));
    else reply(response, 404, { code: "not_found" });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { maxOutputBytes: 1024 });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  const events = [];
  let acknowledgement = "received_stored";
  client.subscribe((event) => {
    if (event.type !== "output") return undefined;
    events.push(event);
    return acknowledgement === "received_stored"
      ? { receivedStored: true, receivedBeforeAttach: true, sequence: events.length }
      : { forwarded: true };
  });
  const outputPort = client.status().outputPort;

  const accepted = await requestJson(outputPort, "/v1/avsdk/output", {
    body: { command: 20001, id: 0, value: ["binary-or-payload"], hostGeneration: 72 },
  });
  assert.equal(accepted.status, 202);
  assert.deepEqual(JSON.parse(accepted.raw), {
    ok: true,
    accepted: true,
    receipt: { kind: "received_stored", sequence: 1 },
  });
  assert.equal(events.length, 1);
  assert.deepEqual(events[0], {
    type: "output", command: 20001, id: 0, value: ["binary-or-payload"], hostGeneration: 72,
  });

  acknowledgement = "kernel_forwarded";
  const forwarded = await requestJson(outputPort, "/v1/avsdk/output", {
    body: { command: 20001, id: 0, value: ["ordered-payload"], hostGeneration: 72 },
  });
  assert.equal(forwarded.status, 202);
  assert.deepEqual(JSON.parse(forwarded.raw).receipt, { kind: "kernel_forwarded" });

  assert.equal((await requestJson(outputPort, "/v1/avsdk/output", { token: "wrong", body: {} })).status, 401);
  assert.equal((await requestJson(outputPort, "/v1/avsdk/output", { rawBody: "{" })).status, 400);
  assert.equal((await requestJson(outputPort, "/v1/avsdk/output", {
    body: { command: 20006, id: 0, value: [], hostGeneration: 73 },
  })).status, 409);
  assert.equal((await requestJson(outputPort, "/v1/avsdk/output", { rawBody: "x".repeat(1025) })).status, 413);
  assert.equal(events.length, 2);
});

test("observer rejection and output timeout are typed HTTP failures without escaping the callback", async (t) => {
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") reply(response, 200, readyBody(72));
    else reply(response, 404, {});
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { outputTimeoutMs: 100 });
  t.after(() => client.stop());
  await client.start();
  await client.checkHealth();
  const remove = client.subscribe((event) => {
    if (event.type === "output") return Promise.reject(new Error("sensitive observer detail"));
  });
  const failed = await requestJson(client.status().outputPort, "/v1/avsdk/output", {
    body: { command: 20006, id: 0, value: ["opaque"], hostGeneration: 72 },
  });
  assert.equal(failed.status, 503);
  assert.match(failed.raw, /output_observer_failed/);
  remove();

  let release;
  client.subscribe((event) => event.type === "output" ? new Promise((resolve) => { release = resolve; }) : undefined);
  const timedOut = await requestJson(client.status().outputPort, "/v1/avsdk/output", {
    body: { command: 20006, id: 0, value: [], hostGeneration: 72 },
  });
  assert.equal(timedOut.status, 504);
  assert.match(timedOut.raw, /output_timeout/);
  release?.();
});

test("bad Host JSON and non-2xx invoke responses are reported as safe rejections", async (t) => {
  let malformed = false;
  let failInvoke = true;
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url === "/healthz") {
      if (malformed) {
        response.writeHead(200, { "content-type": "application/json" }).end("{");
      } else reply(response, 200, readyBody(72));
      return;
    }
    if (failInvoke) reply(response, 409, { code: "generation_mismatch" });
    else reply(response, 200, { posted: true, id: 1, command: 1 });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const failures = [];
  const client = makeClient(fake, { onFailure: (event) => failures.push(event.code) });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  await assert.rejects(client.invoke({ command: 1, id: 1, params: ["a", "b", "b", "p", ""], hostGeneration: 72 }), {
    code: "generation_mismatch",
  });
  failInvoke = false;
  malformed = true;
  assert.equal(await client.checkHealth(), false);
  assert.equal(client.status().lastFailureCode, "invalid_host_response");
  assert.equal(failures.includes("generation_mismatch"), true);
});

test("a business refusal preserves availability but a later HTTP Host failure revokes it", async (t) => {
  let healthy = true;
  let posts = 0;
  const failures = [];
  const availability = [];
  const fake = await startFakeHost((_request, response, record) => {
    assert.equal(record.authorization, `Bearer ${TOKEN}`);
    if (record.url === "/healthz") {
      if (healthy) return reply(response, 200, readyBody(72, { groupCallProtocolAvailable: true }));
      return reply(response, 503, { ok: false, code: "output_forward_failed", status: {
        hostGeneration: 72, ready: false, outputFailed: true } });
    }
    posts += 1;
    const body = JSON.parse(record.raw);
    reply(response, 409, { ok: false, code: "group_audio_busy", id: body.id, command: 4, posted: false });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { onFailure: (event) => failures.push(event.code) });
  t.after(() => client.stop());
  client.subscribe((event) => {
    if (event.type === "availability") availability.push(event.available);
  });
  await client.start();
  assert.equal(await client.checkHealth(), true);
  assert.deepEqual(await client.startGroup({ id: 23, hostGeneration: 72, groupId: "722852338" }),
    { posted: false, id: 23, command: 4 });
  assert.equal(client.status().available, true);
  assert.deepEqual(failures, ["group_start_refused"]);
  healthy = false;
  assert.equal(await client.checkHealth(), false);
  assert.equal(client.status().available, false);
  assert.equal(client.status().lastFailureCode, "output_forward_failed");
  assert.deepEqual(availability, [false, true, false]);
  await assert.rejects(client.startGroup({ id: 24, hostGeneration: 72, groupId: "722852338" }),
    { code: "host_unavailable" });
  assert.equal(posts, 1, "the failed Host receives no subsequent group command");
});

test("the HTTP query envelope allows the Host's 10s SDK timeout to return its typed rejection", async (t) => {
  const querySeen = deferred();
  const failures = [];
  const fake = await startFakeHost(async (_request, response, record) => {
    if (record.url === "/healthz") return reply(response, 200, readyBody(72, { groupCallProtocolAvailable: true }));
    querySeen.resolve();
    await new Promise((resolve) => setTimeout(resolve, 10_000));
    reply(response, 504, { ok: false, code: "group_room_query_timeout" });
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { onFailure: (event) => failures.push(event) });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  const rejected = assert.rejects(client.queryGroupRoomInfo({ id: 31, hostGeneration: 72, groupId: "722852338" }),
    { code: "group_room_query_timeout" });
  await querySeen.promise;
  assert.equal(await client.checkHealth(), true, "the Host health endpoint responds while the SDK query waits");
  await rejected;
  assert.equal(client.status().available, true);
  assert.deepEqual(failures, [{ code: "group_room_query_timeout", hostGeneration: 72, operation: "group_room_query" }]);
});

test("an invoke HTTP deadline rejects that operation while authenticated health remains authoritative", async (t) => {
  let healthy = true;
  const failures = [];
  const fake = await startFakeHost((_request, response, record) => {
    if (record.url !== "/healthz" || !healthy) return;
    reply(response, 200, readyBody(72));
  });
  t.after(() => {
    const closing = new Promise((resolve) => fake.server.close(resolve));
    fake.server.closeAllConnections?.();
    return closing;
  });
  const client = makeClient(fake, { requestTimeoutMs: 100, onFailure: (event) => failures.push(event) });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  await assert.rejects(client.invoke({ command: 55, id: 32, params: [], hostGeneration: 72 }), { code: "host_request_timeout" });
  assert.equal(client.status().available, true);
  assert.deepEqual(failures[0], { code: "host_request_timeout", hostGeneration: 72, operation: "invoke" });
  healthy = false;
  assert.equal(await client.checkHealth(), false);
  assert.equal(client.status().available, false);
  assert.deepEqual(failures.at(-1), { code: "host_request_timeout", hostGeneration: 72, operation: "health" });
});

test("stop aborts concurrent health and invoke requests and bounds partial callback bodies", async (t) => {
  const requestSeen = [];
  const gate = deferred();
  let healthCount = 0;
  const fake = await startFakeHost(async (_request, response, record) => {
    if (record.url === "/healthz" && healthCount++ === 0) {
        reply(response, 200, readyBody(72));
        return;
    }
    requestSeen.push(record.url);
    await gate.promise;
    if (!response.destroyed && !response.writableEnded) reply(response, 200, readyBody(72));
  });
  t.after(() => {
    gate.resolve();
    return new Promise((resolve) => fake.server.close(resolve));
  });
  const client = makeClient(fake, { requestTimeoutMs: 2000, bodyTimeoutMs: 250 });
  await client.start();
  assert.equal(await client.checkHealth(), true);

  const health = client.checkHealth();
  const invoke = client.invoke({ command: 1, id: 9, params: ["a", "b", "b", "p", ""], hostGeneration: 72 });
  await waitFor(() => requestSeen.includes("/healthz") && requestSeen.includes("/v1/invoke"));

  const partial = http.request({
    host: "127.0.0.1",
    port: client.status().outputPort,
    method: "POST",
    path: "/v1/avsdk/output",
    headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", "content-length": "100" },
  });
  partial.on("error", () => {});
  partial.write("{");
  await new Promise((resolve) => setTimeout(resolve, 15));
  await client.stop();
  gate.resolve();
  assert.equal(await health, false);
  await assert.rejects(invoke, { code: "host_client_stopped" });
  assert.equal(client.status().stopped, true);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(partial.destroyed || partial.destroyed === undefined, true);
});
