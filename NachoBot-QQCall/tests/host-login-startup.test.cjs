"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { createHostTransport } = require("../host/transport.cjs");

const TOKEN = Buffer.alloc(32, 0x4a).toString("base64url");
const HOST_GENERATION = 117;
const LOGIN_UID = `u_${"L".repeat(22)}`;
const LOGIN_UIN = "123456789012";
const LOGIN_PARAMS = [LOGIN_UID, LOGIN_UIN, LOGIN_UIN, path.join(os.tmpdir(), "qqcall-login-test"), ""];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function loginBody(id = 81) {
  return { command: 1, id, params: [...LOGIN_PARAMS], hostGeneration: HOST_GENERATION };
}

function httpPost(port, body) {
  const encoded = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: "/v1/invoke",
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        "Content-Length": encoded.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end(encoded);
  });
}

async function createHarness(options = {}) {
  const readiness = { ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false };
  let transport;
  transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    getReadiness: () => readiness,
    outputSink: async () => ({ ok: true }),
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    ...options,
    getReadiness: options.getReadiness || (() => readiness),
  });
  await transport.start();
  return { transport, port: transport.status().listenPort, readiness };
}

test("SDK info posts once before login, command-57 ACK is ignored, and same-id duplicate stays busy", async (t) => {
  const calls = [];
  let transport;
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params }) => {
      calls.push({ command, id, params: structuredClone(params) });
      if (command === 57) {
        await transport.publishOutput({ command: 57, id, value: [0, ""], hostGeneration: HOST_GENERATION });
      }
      return { posted: true, id, command };
    },
  });
  transport = harness.transport;
  t.after(() => transport.stop());

  const firstLogin = await httpPost(harness.port, loginBody(81));
  assert.equal(firstLogin.status, 200);
  assert.deepEqual(calls, [
    { command: 57, id: 81, params: ["1.0.1168"] },
    { command: 1, id: 81, params: LOGIN_PARAMS },
  ]);
  assert.equal(transport.status().loginAcknowledged, false,
    "a successful command-57 output is not a command-1 login acknowledgment");
  assert.equal(transport.status().sdkInfoStartupState, "ready",
    "ready records the renderer post receipt even though command 1 is not yet acknowledged");
  assert.equal(transport.status().invokeAttempts, 2);
  assert.equal(transport.status().invokePosted, 2,
    "the SDK-info renderer receipt completes its invoke instead of waiting on a native ACK");
  const health = await new Promise((resolve, reject) => {
    const request = http.get({ hostname: "127.0.0.1", port: harness.port, path: "/healthz",
      headers: { Authorization: `Bearer ${TOKEN}` } }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({ status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
    });
    request.once("error", reject);
  });
  assert.equal(health.status, 200);
  assert.equal(health.body.status.sdkInfoStartupState, "ready",
    "health exposes only the bounded startup state enum");

  const sameIdRetryWhileLoginPending = await httpPost(harness.port, loginBody(81));
  assert.equal(sameIdRetryWhileLoginPending.status, 429);
  assert.equal(sameIdRetryWhileLoginPending.body.code, "host_busy");
  assert.equal(calls.length, 2, "a same-id retry cannot duplicate either startup or pending login posts");

  await transport.publishOutput({ command: 1, id: 81, value: [0, ""], hostGeneration: HOST_GENERATION });
  assert.equal(transport.status().loginAcknowledged, true);

  const secondLogin = await httpPost(harness.port, loginBody(82));
  assert.equal(secondLogin.status, 200);
  assert.deepEqual(calls.slice(2), [{ command: 1, id: 82, params: LOGIN_PARAMS }],
    "later safe logins in the same Host generation do not resend command 57");
});

test("concurrent first logins share one SDK-info post and only one dispatches command 1", async (t) => {
  const sdkInfoStarted = deferred();
  const allowSdkInfoReceipt = deferred();
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params }) => {
      calls.push({ command, id, params: structuredClone(params) });
      if (command === 57) {
        sdkInfoStarted.resolve();
        await allowSdkInfoReceipt.promise;
      }
      return { posted: true, id, command };
    },
  });
  t.after(() => harness.transport.stop());

  const first = httpPost(harness.port, loginBody(81));
  await sdkInfoStarted.promise;
  assert.equal(harness.transport.status().sdkInfoStartupState, "in_flight");
  const second = httpPost(harness.port, loginBody(82));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [{ command: 57, id: 81, params: ["1.0.1168"] }],
    "the second login joins the in-flight startup helper instead of posting another command 57");

  allowSdkInfoReceipt.resolve();
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.equal(firstResult.status, 200);
  assert.equal(secondResult.status, 429);
  assert.equal(secondResult.body.code, "host_busy");
  assert.deepEqual(calls, [
    { command: 57, id: 81, params: ["1.0.1168"] },
    { command: 1, id: 81, params: LOGIN_PARAMS },
  ]);
  assert.equal(harness.transport.status().sdkInfoStartupState, "ready");
});

test("a pre-dispatch SDK-info refusal resets startup so a same-id login can safely retry", async (t) => {
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params }) => {
      calls.push({ command, id, params: structuredClone(params) });
      return { posted: true, id, command };
    },
  });
  t.after(() => harness.transport.stop());

  harness.readiness.ready = false;
  const refused = await httpPost(harness.port, loginBody(81));
  assert.equal(refused.status, 503);
  assert.equal(refused.body.code, "host_not_ready");
  assert.equal(harness.transport.status().sdkInfoStartupState, "idle");
  assert.deepEqual(calls, [], "readiness rejection happened before SetSDKInfo dispatch");

  harness.readiness.ready = true;
  const retry = await httpPost(harness.port, loginBody(81));
  assert.equal(retry.status, 200);
  assert.deepEqual(calls.map(({ command }) => command), [57, 1]);
});

test("an uncertain SDK-info post is quarantined and never retried or followed by login", async (t) => {
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params }) => {
      calls.push({ command, id, params: structuredClone(params) });
      if (command === 57) throw Object.assign(new Error("post result unavailable"), { code: "renderer_post_unknown" });
      return { posted: true, id, command };
    },
  });
  t.after(() => harness.transport.stop());

  const first = await httpPost(harness.port, loginBody(81));
  assert.equal(first.status, 502);
  assert.equal(first.body.code, "sdk_info_post_uncertain");
  const sameIdRetry = await httpPost(harness.port, loginBody(81));
  const newIdRetry = await httpPost(harness.port, loginBody(82));
  assert.equal(sameIdRetry.status, 502);
  assert.equal(newIdRetry.status, 502);
  assert.equal(sameIdRetry.body.code, "sdk_info_post_uncertain");
  assert.equal(newIdRetry.body.code, "sdk_info_post_uncertain");
  assert.equal(harness.transport.status().sdkInfoStartupState, "uncertain");
  assert.deepEqual(calls, [{ command: 57, id: 81, params: ["1.0.1168"] }],
    "an uncertain internal post cannot be silently retried or proceed to command 1");
});

test("startup refuses to send SDK info while a private call is active", async (t) => {
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params }) => {
      calls.push({ command, id, params: structuredClone(params) });
      return { posted: true, id, command };
    },
  });
  t.after(() => harness.transport.stop());

  await harness.transport.publishOutput({
    command: 20006,
    id: 0,
    value: [1, `u_${"P".repeat(22)}`, [LOGIN_UID], 0, "private-room", "", "", "", "", "", 1, "private-seq"],
    hostGeneration: HOST_GENERATION,
  });
  const login = await httpPost(harness.port, loginBody(81));
  assert.equal(login.status, 409);
  assert.equal(login.body.code, "private_call_pending");
  assert.deepEqual(calls, [], "SetSDKInfo is not sent while a private call owns the call slot");
});

test("generic invoke cannot call SDK-info command 57", async (t) => {
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id }) => {
      calls.push({ command, id });
      return { posted: true, id, command };
    },
  });
  t.after(() => harness.transport.stop());

  const response = await httpPost(harness.port, {
    command: 57, id: 81, params: ["1.0.1168"], hostGeneration: HOST_GENERATION,
  });
  assert.equal(response.status, 400);
  assert.equal(response.body.code, "command_not_allowed");
  assert.deepEqual(calls, []);
});

test("shutdown during SDK-info posting prevents any late command-1 dispatch", async () => {
  const started = deferred();
  const calls = [];
  const harness = await createHarness({
    invokePlugin: async ({ command, id, params, signal }) => {
      calls.push({ command, id, params: structuredClone(params) });
      if (command === 57) {
        started.resolve();
        return new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(Object.assign(new Error("stopped"), { code: "host_stopped" })), { once: true });
        });
      }
      return { posted: true, id, command };
    },
  });

  const loginPromise = httpPost(harness.port, loginBody(81));
  await started.promise;
  const stopped = await harness.transport.stop();
  const login = await loginPromise;
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.sdkInfoStartupState, "stopped");
  assert.equal(login.status, 503);
  assert.equal(login.body.code, "host_stopped");
  assert.deepEqual(calls, [{ command: 57, id: 81, params: ["1.0.1168"] }]);
});
