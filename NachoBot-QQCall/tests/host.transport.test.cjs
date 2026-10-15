"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createHostClient } = require("../bridge/host-client.cjs");
const { createHostTransport, MAX_FRAME_BYTES } = require("../host/transport.cjs");

const TOKEN = Buffer.alloc(32, 0x51).toString("base64url");
const HOST_GENERATION = 17;

function readyState(overrides = {}) {
  return { ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false, ...overrides };
}

async function createTransport(options = {}) {
  const mutableReadiness = { value: readyState() };
  const transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    getReadiness: () => mutableReadiness.value,
    ...options,
  });
  await transport.start();
  return { transport, mutableReadiness, port: transport.status().listenPort };
}

async function httpRequest(port, pathname, { method = "GET", body, token = TOKEN, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        let json = null;
        try { json = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
        catch { /* The test reports the HTTP status if a response is not JSON. */ }
        resolve({ status: response.statusCode, json });
      });
    });
    request.once("error", reject);
    if (body !== undefined) request.end(body);
    else request.end();
  });
}

function invokeBody({ command = 1, id = 81, params = ["uid", "uin", "uin", "account", ""], hostGeneration = HOST_GENERATION } = {}) {
  return JSON.stringify({ command, id, params, hostGeneration });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test("health and invoke require Bearer auth; readiness is authenticated metadata", async (t) => {
  const mutable = { value: readyState({ ready: false, pluginFound: false, hasPostMessage: false }) };
  const { transport, port } = await createTransport({ getReadiness: () => mutable.value });
  t.after(() => transport.stop());

  const noAuth = await httpRequest(port, "/healthz", { token: null });
  assert.equal(noAuth.status, 401);
  assert.equal(noAuth.json.code, "unauthorized");

  const notReady = await httpRequest(port, "/healthz");
  assert.equal(notReady.status, 503);
  assert.equal(notReady.json.ok, false);
  assert.equal(notReady.json.status.ready, false);

  const blockedInvoke = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(blockedInvoke.status, 503);
  assert.equal(blockedInvoke.json.code, "host_not_ready");

  mutable.value = readyState();
  const ready = await httpRequest(port, "/healthz");
  assert.equal(ready.status, 200);
  assert.deepEqual(ready.json.status.pluginFound, true);
  assert.deepEqual(ready.json.status.hasPostMessage, true);
});

test("transport validates optional Host PID metadata", async () => {
  const base = {
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    invokePlugin: async () => ({ posted: true }),
  };
  for (const targetPid of [0, -1, 1.5, 0x80000000, Number.MAX_SAFE_INTEGER]) {
    assert.throws(() => createHostTransport({ ...base, targetPid }), { code: "invalid_host_pid" });
  }
});

test("HostClient readiness requires the transport's configured Host PID", async (t) => {
  const targetPid = 456;
  const missing = await createTransport();
  const missingClient = createHostClient({
    token: TOKEN,
    listenPort: missing.port,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    targetPid,
    healthPollMs: 10_000,
  });
  t.after(async () => {
    await missingClient.stop();
    await missing.transport.stop();
  });
  await missingClient.start();
  assert.equal(await missingClient.checkHealth(), false, "health without the pinned PID is unavailable");

  const matching = await createTransport({ targetPid });
  const matchingClient = createHostClient({
    token: TOKEN,
    listenPort: matching.port,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    targetPid,
    healthPollMs: 10_000,
  });
  t.after(async () => {
    await matchingClient.stop();
    await matching.transport.stop();
  });
  matching.mutableReadiness.value = readyState({ targetPid: targetPid + 1 });
  const matchingHealth = await httpRequest(matching.port, "/healthz");
  assert.equal(matchingHealth.status, 200);
  assert.equal(matchingHealth.json.status.targetPid, targetPid,
    "the transport reports its configured PID rather than an observer's arbitrary value");
  await matchingClient.start();
  assert.equal(await matchingClient.checkHealth(), true, "matching authenticated Host PID makes the client available");

  const mismatchedClient = createHostClient({
    token: TOKEN,
    listenPort: matching.port,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    targetPid: targetPid + 1,
    healthPollMs: 10_000,
  });
  t.after(() => mismatchedClient.stop());
  await mismatchedClient.start();
  assert.equal(await mismatchedClient.checkHealth(), false, "a different pinned Host PID stays unavailable");
});

test("strict command signatures, controller ids, and Host generation are enforced", async (t) => {
  const calls = [];
  const { transport, port } = await createTransport({
    invokePlugin: async (request) => {
      calls.push(request);
      return { posted: true, id: request.id, command: request.command };
    },
  });
  t.after(() => transport.stop());

  const deniedCommand = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 3 }),
  });
  assert.equal(deniedCommand.status, 400);
  assert.equal(deniedCommand.json.code, "command_not_allowed");

  const badLogin = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ params: ["uid", "uin"] }),
  });
  assert.equal(badLogin.status, 400);
  assert.equal(badLogin.json.code, "invalid_params");

  const stale = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ hostGeneration: HOST_GENERATION + 1 }),
  });
  assert.equal(stale.status, 409);
  assert.equal(stale.json.code, "generation_mismatch");

  const badId = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ id: 0 }),
  });
  assert.equal(badId.status, 400);
  assert.equal(badId.json.code, "invalid_request_id");

  const acceptParams = [41, "invite-id", ["invite-id"], 9, "room", true, "payload"];
  const accept = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 5, id: 987654321, params: acceptParams }),
  });
  assert.equal(accept.status, 200);
  assert.deepEqual(accept.json, { posted: true, id: 987654321, command: 5 });

  const action = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 55, id: 44, params: [31, "private action"] }),
  });
  assert.equal(action.status, 200);
  assert.deepEqual(action.json, { posted: true, id: 44, command: 55 });
  assert.deepEqual(calls.map((call) => [call.command, call.id]), [[5, 987654321], [55, 44]]);
  assert.equal(calls[0].hostGeneration, HOST_GENERATION);
});

test("private end commands stay typed-unsupported unless the pinned protocol capability and exact signatures are present", async (t) => {
  const deniedCalls = [];
  const unverified = await createTransport({
    invokePlugin: async (request) => { deniedCalls.push(request); return { posted: true, id: request.id, command: request.command }; },
  });
  t.after(() => unverified.transport.stop());
  const privatePeerUid = `u_${"P".repeat(22)}`;
  const denied = await httpRequest(unverified.port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 8, id: 8801, params: [1, 0] }),
  });
  assert.equal(denied.status, 501);
  assert.equal(denied.json.code, "private_end_protocol_unverified");
  assert.equal(unverified.transport.status().privateEndProtocolVerified, false);
  assert.deepEqual(deniedCalls, []);
  const deniedClose = await httpRequest(unverified.port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 10, id: 8806, params: [1, privatePeerUid, 0] }),
  });
  assert.equal(deniedClose.status, 501);
  assert.equal(deniedClose.json.code, "private_end_protocol_unverified");
  assert.deepEqual(deniedCalls, [], "a valid Close candidate remains blocked without the pinned protocol capability");

  const calls = [];
  const verified = await createTransport({
    verifiedPrivateEndProtocol: true,
    invokePlugin: async (request) => {
      calls.push({ command: request.command, id: request.id, params: request.params });
      return { posted: true, id: request.id, command: request.command };
    },
  });
  t.after(() => verified.transport.stop());
  assert.equal(verified.transport.status().privateEndProtocolVerified, true);

  for (const [command, id, params] of [
    [8, 8802, [1, 0]],
    [9, 9901, [1, privatePeerUid, 1]],
    [10, 10001, [1, privatePeerUid, 0]],
  ]) {
    const response = await httpRequest(verified.port, "/v1/invoke", {
      method: "POST",
      body: invokeBody({ command, id, params }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(response.json, { posted: true, id, command });
  }
  for (const [command, id, params] of [
    [8, 8803, [1, "enter-relation-must-not-be-sent"]],
    [8, 8804, [2, 0]],
    [8, 8805, [1, 0, "extra"]],
    [9, 9902, [1, "not-a-QQ-UID", 1]],
    [9, 9903, [1, privatePeerUid, 2]],
    [9, 9904, [1, privatePeerUid, 1, "extra"]],
    [10, 10002, [3, privatePeerUid, 0]],
    [10, 10003, [1, "not-a-QQ-UID", 0]],
    [10, 10004, [1, privatePeerUid, 1]],
    [10, 10005, [1, privatePeerUid, 0, "extra"]],
  ]) {
    const response = await httpRequest(verified.port, "/v1/invoke", {
      method: "POST",
      body: invokeBody({ command, id, params }),
    });
    assert.equal(response.status, 400);
    assert.equal(response.json.code, "invalid_params");
  }
  const groupReject = await httpRequest(verified.port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 3, id: 10006, params: [1, privatePeerUid, 0] }),
  });
  assert.equal(groupReject.status, 400);
  assert.equal(groupReject.json.code, "command_not_allowed", "group command 3 is not enabled by private Close support");
  assert.deepEqual(calls, [
    { command: 8, id: 8802, params: [1, 0] },
    { command: 9, id: 9901, params: [1, privatePeerUid, 1] },
    { command: 10, id: 10001, params: [1, privatePeerUid, 0] },
  ]);
});

test("renderer exit classification rejects invokes and keeps health unavailable with typed codes", async (t) => {
  const mutable = { value: readyState({ ready: false }) };
  const { transport, port } = await createTransport({ getReadiness: () => mutable.value });
  t.after(() => transport.stop());

  const notReady = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(notReady.status, 503);
  assert.equal(notReady.json.code, "host_not_ready");

  mutable.value = readyState({ ready: false, rendererGone: true, rendererGoneCode: "renderer_crashed" });
  const crashed = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(crashed.status, 503);
  assert.equal(crashed.json.code, "renderer_crashed");
  const crashedHealth = await httpRequest(port, "/healthz");
  assert.equal(crashedHealth.status, 503);
  assert.equal(crashedHealth.json.status.ready, false);
  assert.equal(crashedHealth.json.status.lastFailureCode, "renderer_crashed");

  mutable.value = readyState({ ready: false, rendererGone: true, rendererGoneCode: "renderer_closed" });
  const closed = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(closed.status, 503);
  assert.equal(closed.json.code, "renderer_closed");
  const closedHealth = await httpRequest(port, "/healthz");
  assert.equal(closedHealth.status, 503);
  assert.equal(closedHealth.json.status.ready, false);
  assert.equal(closedHealth.json.status.lastFailureCode, "renderer_closed");
});

test("the real HTTP reader rejects a chunked body one byte over 1 MiB and remains healthy", async (t) => {
  const invokeCalls = [];
  const { transport, port } = await createTransport({
    invokePlugin: async ({ command, id }) => {
      invokeCalls.push({ command, id });
      return { posted: true, id, command };
    },
  });
  t.after(() => transport.stop());
  const oversizedPrefix = Buffer.alloc(MAX_FRAME_BYTES, 0x61);
  const response = await new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: "/v1/invoke",
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        "Transfer-Encoding": "chunked",
      },
    }, (incoming) => {
      const chunks = [];
      incoming.on("data", (chunk) => chunks.push(chunk));
      incoming.on("end", () => {
        let json = null;
        try { json = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
        catch { /* assertions below expose an unexpected response */ }
        resolve({ status: incoming.statusCode, json });
      });
    });
    request.once("error", reject);
    const flushed = request.write(oversizedPrefix);
    const endWithOverflowByte = () => request.end(Buffer.from([0x61]));
    if (flushed) endWithOverflowByte();
    else request.once("drain", endWithOverflowByte);
  });
  assert.equal(response.status, 413);
  assert.equal(response.json.code, "body_too_large");
  assert.deepEqual(invokeCalls, [], "overflow is rejected by the streaming body reader before plugin invocation");

  const health = await httpRequest(port, "/healthz");
  assert.equal(health.status, 200);
  const subsequent = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(subsequent.status, 200);
  assert.deepEqual(invokeCalls, [{ command: 57, id: 81 }, { command: 1, id: 81 }]);
});

test("the real HTTP reader rejects oversized Content-Length from headers without a body upload", async (t) => {
  const invokeCalls = [];
  const { transport, port } = await createTransport({
    invokePlugin: async ({ command, id }) => {
      invokeCalls.push({ command, id });
      return { posted: true, id, command };
    },
  });
  t.after(() => transport.stop());

  const response = await new Promise((resolve, reject) => {
    const chunks = [];
    const socket = net.createConnection({ host: "127.0.0.1", port });
    socket.once("error", reject);
    socket.on("data", (chunk) => chunks.push(chunk));
    socket.once("end", () => {
      const bytes = Buffer.concat(chunks);
      const separator = bytes.indexOf(Buffer.from("\r\n\r\n"));
      assert.notEqual(separator, -1, "HTTP rejection includes response headers");
      const head = bytes.subarray(0, separator).toString("ascii");
      const status = Number(head.match(/^HTTP\/1\.1 (\d{3})/)?.[1]);
      const body = bytes.subarray(separator + 4).toString("utf8");
      resolve({ status, json: JSON.parse(body) });
    });
    socket.once("connect", () => {
      socket.write([
        "POST /v1/invoke HTTP/1.1",
        "Host: 127.0.0.1",
        `Authorization: Bearer ${TOKEN}`,
        "Content-Type: application/json",
        `Content-Length: ${MAX_FRAME_BYTES + 1}`,
        "Connection: close",
        "",
        "",
      ].join("\r\n"));
    });
  });

  assert.equal(response.status, 413);
  assert.equal(response.json.code, "body_too_large");
  assert.deepEqual(invokeCalls, [], "declared overflow is rejected before plugin invocation");
});

test("output POSTs keep order and a bounded overflow fails Host readiness", async (t) => {
  const firstSink = deferred();
  const seen = [];
  let firstStarted = false;
  const { transport, port } = await createTransport({
    maxOutputQueueBytes: 140,
    outputSink: async (frame) => {
      seen.push(frame);
      if (!firstStarted) {
        firstStarted = true;
        await firstSink.promise;
      }
    },
  });
  t.after(() => transport.stop());

  const firstFrame = { command: 20006, id: 0, value: "a", hostGeneration: HOST_GENERATION };
  const first = transport.publishOutput(firstFrame);
  const firstOutcome = first.then(() => "forwarded", (error) => error.code);
  await new Promise((resolve) => setImmediate(resolve));
  const second = transport.publishOutput({ command: 20004, id: 0, value: "b", hostGeneration: HOST_GENERATION });
  const secondOutcome = second.then(() => "forwarded", (error) => error.code);
  const overflow = await transport.publishOutput({ command: 20001, id: 0, value: "c", hostGeneration: HOST_GENERATION }).then(
    () => null,
    (error) => error.code,
  );
  assert.equal(overflow, "output_queue_full");
  firstSink.resolve();
  assert.equal(await firstOutcome, "forwarded");
  assert.equal(await secondOutcome, "output_forward_failed");
  assert.deepEqual(seen.map((frame) => frame.command), [20006]);
  const health = await httpRequest(port, "/healthz");
  assert.equal(health.status, 503);
  assert.equal(health.json.status.outputFailed, true);
  assert.equal(health.json.status.lastFailureCode, "output_queue_full");
});

test("output events are serialized and forwarded through the injected sink", async (t) => {
  const gate = deferred();
  const seen = [];
  const { transport } = await createTransport({
    outputSink: async (frame) => {
      seen.push(frame);
      if (seen.length === 1) await gate.promise;
    },
  });
  t.after(() => transport.stop());

  const first = transport.publishOutput({ command: 20006, id: 0, value: ["invite"], hostGeneration: HOST_GENERATION });
  await new Promise((resolve) => setImmediate(resolve));
  const second = transport.publishOutput({ command: 20004, id: 0, value: [0, ""], hostGeneration: HOST_GENERATION });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(seen.map((frame) => frame.command), [20006]);
  gate.resolve();
  await Promise.all([first, second]);
  assert.deepEqual(seen.map((frame) => frame.command), [20006, 20004]);
  assert.equal(seen[0].hostGeneration, HOST_GENERATION);
});

test("the default output sink POSTs an authenticated generation-bound frame over loopback", async (t) => {
  let received = null;
  const controller = http.createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      received = {
        authorization: request.headers.authorization,
        path: request.url,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      };
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: true }));
    });
  });
  await new Promise((resolve) => controller.listen(0, "127.0.0.1", resolve));
  const outputPort = controller.address().port;
  const { transport } = await createTransport({ outputPort });
  t.after(async () => {
    await transport.stop();
    await new Promise((resolve) => controller.close(resolve));
  });

  const frame = { command: 20004, id: 0, value: [0, ""] };
  await transport.publishOutput(frame);
  assert.equal(received.authorization, `Bearer ${TOKEN}`);
  assert.equal(received.path, "/v1/avsdk/output");
  assert.deepEqual(received.body, { ...frame, hostGeneration: HOST_GENERATION });
});

test("output forwarding times out once and marks the Host unavailable", async (t) => {
  let attempts = 0;
  const { transport, port } = await createTransport({
    outputTimeoutMs: 25,
    outputSink: async () => { attempts += 1; return new Promise(() => {}); },
  });
  t.after(() => transport.stop());

  await assert.rejects(
    transport.publishOutput({ command: 20006, id: 0, value: "payload", hostGeneration: HOST_GENERATION }),
    { code: "output_forward_timeout" },
  );
  assert.equal(attempts, 1);
  assert.equal(transport.status().outputFailed, true);
  const health = await httpRequest(port, "/healthz");
  assert.equal(health.status, 503);
  assert.equal(health.json.status.lastFailureCode, "output_forward_timeout");
  await assert.rejects(
    transport.publishOutput({ command: 20004, id: 0, value: [0, ""], hostGeneration: HOST_GENERATION }),
    { code: "output_forward_failed" },
  );
  assert.equal(attempts, 1);
});

test("a stalled loopback controller response has a stable output timeout code", async (t) => {
  let accepted = 0;
  const controller = http.createServer((request) => {
    accepted += 1;
    request.resume();
    // Deliberately leave the response pending until Host timeout closes the socket.
  });
  await new Promise((resolve) => controller.listen(0, "127.0.0.1", resolve));
  const outputPort = controller.address().port;
  const { transport, port } = await createTransport({ outputPort, outputTimeoutMs: 35 });
  t.after(async () => {
    await transport.stop();
    await new Promise((resolve) => controller.close(resolve));
  });

  await assert.rejects(
    transport.publishOutput({ command: 20006, id: 0, value: "pending", hostGeneration: HOST_GENERATION }),
    { code: "output_forward_timeout" },
  );
  assert.equal(accepted, 1);
  const health = await httpRequest(port, "/healthz");
  assert.equal(health.status, 503);
  assert.equal(health.json.status.outputFailed, true);
  assert.equal(health.json.status.lastFailureCode, "output_forward_timeout");
});

test("an SDK-info startup timeout is uncertain and never dispatches command 1", async (t) => {
  let attempts = 0;
  const calls = [];
  const { transport, port } = await createTransport({
    requestTimeoutMs: 25,
    invokePlugin: async ({ command }) => {
      attempts += 1;
      calls.push(command);
      return new Promise(() => {});
    },
  });
  t.after(() => transport.stop());

  const response = await httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  assert.equal(response.status, 502);
  assert.equal(response.json.code, "sdk_info_post_uncertain");
  assert.equal(attempts, 1);
  assert.deepEqual(calls, [57], "the uncertain startup post is never followed by command 1");
  assert.equal(transport.status().invokeAttempts, 1);
  assert.equal(transport.status().lastFailureCode, "invoke_timeout");
});

test("concurrent invokes are bounded and excess requests receive 429", async (t) => {
  const pending = deferred();
  const started = deferred();
  const { transport, port } = await createTransport({
    maxConcurrentInvokes: 1,
    invokePlugin: async ({ command, id }) => {
      started.resolve();
      await pending.promise;
      return { posted: true, id, command };
    },
  });
  t.after(() => transport.stop());

  const firstRequest = httpRequest(port, "/v1/invoke", {
    method: "POST", body: invokeBody({ command: 55, params: [1, "first"] }),
  });
  await started.promise;
  const secondResponse = await httpRequest(port, "/v1/invoke", {
    method: "POST",
    body: invokeBody({ command: 55, id: 82, params: [1, "second"] }),
  });
  assert.equal(secondResponse.status, 429);
  assert.equal(secondResponse.json.code, "host_busy");
  pending.resolve();
  const firstResponse = await firstRequest;
  assert.equal(firstResponse.status, 200);
});

test("shutdown aborts an in-flight invoke and output writer and closes HTTP", async () => {
  const invokeStarted = deferred();
  const outputStarted = deferred();
  const { transport, port } = await createTransport({
    invokePlugin: async ({ signal }) => {
      invokeStarted.resolve();
      return new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("stopped"), { code: "host_stopped" })), { once: true });
      });
    },
    outputSink: async (_frame, { signal }) => {
      outputStarted.resolve();
      return new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("stopped"), { code: "host_stopped" })), { once: true });
      });
    },
  });

  const invokeRequest = httpRequest(port, "/v1/invoke", { method: "POST", body: invokeBody() });
  await invokeStarted.promise;
  const outputPromise = transport.publishOutput({ command: 20006, id: 0, value: "invite", hostGeneration: HOST_GENERATION });
  const outputOutcome = outputPromise.then(() => "forwarded", (error) => error.code);
  await outputStarted.promise;
  const stopped = await transport.stop();
  const invokeResponse = await invokeRequest;
  assert.equal(await outputOutcome, "host_stopped");
  assert.equal(invokeResponse.status, 503);
  assert.equal(invokeResponse.json.code, "host_stopped");
  assert.equal(stopped.stopped, true);
  await assert.rejects(httpRequest(port, "/healthz"));
});

test("renderer invoke emits only the verified PPAPI message shape", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "host", "host.html"), "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/i)?.[1];
  assert.ok(script);
  const posted = [];
  const plugin = {
    postMessage(message) { posted.push(message); },
    addEventListener() {},
  };
  const window = { addEventListener() {}, setTimeout, clearTimeout };
  const document = { getElementById: (id) => id === "avsdk" ? plugin : null };
  vm.runInNewContext(script, { window, document, Date, Number, Boolean });

  const params = ["uid", "uin", "uin", "account", ""];
  const receipt = window.__qqCallHostInvoke(1, 99, params, Date.now() + 1000);
  const hangupParams = [1, 0];
  const hangupReceipt = window.__qqCallHostInvoke(8, 101, hangupParams, Date.now() + 1000);
  const rejectParams = [1, `u_${"P".repeat(22)}`, 1];
  const rejectReceipt = window.__qqCallHostInvoke(9, 102, rejectParams, Date.now() + 1000);
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [
    { cmd: 1, id: 99, param: params },
    { cmd: 8, id: 101, param: hangupParams },
    { cmd: 9, id: 102, param: rejectParams },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(receipt)), { posted: true, id: 99, command: 1 });
  assert.deepEqual(JSON.parse(JSON.stringify(hangupReceipt)), { posted: true, id: 101, command: 8 });
  assert.deepEqual(JSON.parse(JSON.stringify(rejectReceipt)), { posted: true, id: 102, command: 9 });
  const late = window.__qqCallHostInvoke(1, 100, params, Date.now() - 1);
  assert.deepEqual(JSON.parse(JSON.stringify(late)), { posted: false, code: "invoke_deadline_elapsed" });
  assert.equal(posted.length, 3);
});
