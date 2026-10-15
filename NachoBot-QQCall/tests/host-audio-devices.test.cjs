"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  createHostTransport,
  validateAudioDevicesBody,
  validateInvokeBody,
} = require("../host/transport.cjs");
const {
  matchesVerifiedAudioDevicePluginDigest,
  pluginHasVerifiedAudioDeviceProtocol,
} = require("../host/electron-host.cjs");

const TOKEN = Buffer.alloc(32, 0x67).toString("base64url");
const HOST_GENERATION = 71;
const PINNED_PLUGIN_SHA256 = "757C48DDC0B5FDFF101C36BF309BF9F79FE8618A64FBF0D059347AD3834A9DBF";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function createTransport(options = {}) {
  const mutable = { readiness: { ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false } };
  const transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    getReadiness: () => mutable.readiness,
    ...options,
  });
  mutable.transport = transport;
  await transport.start();
  return { transport, port: transport.status().listenPort, mutable };
}

function postJson(port, pathname, body, { token = TOKEN } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = Buffer.from(JSON.stringify(body));
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method: "POST",
      headers: {
        ...(token ? { Authorization: "Bearer " + token } : {}),
        "Content-Type": "application/json",
        "Content-Length": bytes.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end(bytes);
  });
}

function requestBody(overrides = {}) {
  return { id: 713, hostGeneration: HOST_GENERATION, ...overrides };
}

test("audio-device protocol is enabled only by the pinned Windows DLL digest", async (t) => {
  assert.equal(matchesVerifiedAudioDevicePluginDigest(PINNED_PLUGIN_SHA256, "win32"), true);
  assert.equal(matchesVerifiedAudioDevicePluginDigest(PINNED_PLUGIN_SHA256.toLowerCase(), "win32"), true);
  assert.equal(matchesVerifiedAudioDevicePluginDigest(PINNED_PLUGIN_SHA256, "linux"), false);
  assert.equal(matchesVerifiedAudioDevicePluginDigest("0".repeat(64), "win32"), false);
  assert.equal(matchesVerifiedAudioDevicePluginDigest("not-a-digest", "win32"), false);

  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-audio-device-digest-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const pluginPath = path.join(directory, "AVSDKPlugin.dll");
  fs.writeFileSync(pluginPath, "test plugin bytes");
  const actualDigest = crypto.createHash("sha256").update(fs.readFileSync(pluginPath)).digest("hex");
  assert.equal(matchesVerifiedAudioDevicePluginDigest(actualDigest, "win32"), false);
  assert.equal(await pluginHasVerifiedAudioDeviceProtocol(pluginPath, "win32"), false);
  assert.equal(await pluginHasVerifiedAudioDeviceProtocol(pluginPath, "linux"), false);
  assert.equal(await pluginHasVerifiedAudioDeviceProtocol(path.join(directory, "missing.dll"), "win32"), false);
});

test("audio-device HTTP query requires auth, exact shape, safe ID, and current Host generation", async (t) => {
  let invokeCount = 0;
  const { transport, port } = await createTransport({
    verifiedAudioDeviceProtocol: true,
    invokePlugin: async ({ command, id }) => {
      invokeCount += 1;
      return { posted: true, id, command };
    },
  });
  t.after(() => transport.stop());

  const unauthorized = await postJson(port, "/v1/audio/devices", requestBody(), { token: null });
  assert.equal(unauthorized.status, 401);
  assert.equal(unauthorized.body.code, "unauthorized");

  const extra = await postJson(port, "/v1/audio/devices", { ...requestBody(), command: 103 });
  assert.equal(extra.status, 400);
  assert.equal(extra.body.code, "invalid_request_shape");

  const stale = await postJson(port, "/v1/audio/devices", requestBody({ hostGeneration: HOST_GENERATION + 1 }));
  assert.equal(stale.status, 409);
  assert.equal(stale.body.code, "generation_mismatch");

  for (const id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, "713"]) {
    const invalid = await postJson(port, "/v1/audio/devices", requestBody({ id }));
    assert.equal(invalid.status, 400);
    assert.equal(invalid.body.code, "invalid_request_id");
  }
  assert.equal(invokeCount, 0);
});

test("verified audio-device query posts only cmd 103 with empty params and returns only posted receipt", async (t) => {
  const posted = [];
  const { transport, port, mutable } = await createTransport({
    verifiedAudioDeviceProtocol: true,
    outputSink: async () => ({ ok: true }),
    invokePlugin: async (request) => {
      posted.push(request);
      if (request.command === 103) void mutable.transport.publishOutput({
        command: 20061,
        id: 0,
        value: [
          Array.from({ length: 18 }, (_, index) => index + 1),
          Array.from({ length: 18 }, (_, index) => `Output ${index + 1}`),
          Array.from({ length: 18 }, (_, index) => `{OUT-${index + 1}}`),
          Array.from({ length: 15 }, (_, index) => index + 1),
          Array.from({ length: 15 }, (_, index) => `Input ${index + 1}`),
          Array.from({ length: 15 }, (_, index) => `{IN-${index + 1}}`),
          0, 0, 10, 8,
        ],
        hostGeneration: HOST_GENERATION,
      });
      return { posted: true, id: request.id, command: request.command };
    },
  });
  t.after(() => transport.stop());

  const response = await postJson(port, "/v1/audio/devices", requestBody());
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, { posted: true, id: 713, command: 103 });
  assert.deepEqual(posted.map(({ command, id, params, hostGeneration }) => ({ command, id, params, hostGeneration })), [
    { command: 103, id: 713, params: [], hostGeneration: HOST_GENERATION },
  ]);
  assert.equal(transport.status().invokePosted, 1);
});

test("unverified DLL digest and non-Windows capability return 501 without posting", async (t) => {
  let calls = 0;
  for (const verifiedAudioDeviceProtocol of [false]) {
    const { transport, port } = await createTransport({
      verifiedAudioDeviceProtocol,
      invokePlugin: async () => { calls += 1; return { posted: true, id: 713, command: 103 }; },
    });
    const response = await postJson(port, "/v1/audio/devices", requestBody());
    assert.equal(response.status, 501);
    assert.deepEqual(response.body, { ok: false, code: "audio_device_protocol_unverified" });
    assert.equal(calls, 0);
    await transport.stop();
  }
  assert.equal(matchesVerifiedAudioDevicePluginDigest(PINNED_PLUGIN_SHA256, "linux"), false);
});

test("generic invoke remains closed to command 103", async (t) => {
  let calls = 0;
  const { transport, port } = await createTransport({
    verifiedAudioDeviceProtocol: true,
    invokePlugin: async () => { calls += 1; return { posted: true, id: 713, command: 103 }; },
  });
  t.after(() => transport.stop());
  const response = await postJson(port, "/v1/invoke", {
    command: 103,
    id: 713,
    params: [],
    hostGeneration: HOST_GENERATION,
  });
  assert.equal(response.status, 400);
  assert.equal(response.body.code, "command_not_allowed");
  assert.throws(() => validateInvokeBody({ command: 103, id: 713, params: [], hostGeneration: HOST_GENERATION }, HOST_GENERATION), {
    code: "command_not_allowed",
  });
  assert.equal(calls, 0);
});

test("named audio-device validation is separate from generic signatures", () => {
  assert.deepEqual(validateAudioDevicesBody(requestBody(), HOST_GENERATION), requestBody());
  assert.throws(() => validateAudioDevicesBody({ ...requestBody(), params: [] }, HOST_GENERATION), {
    code: "invalid_request_shape",
  });
});

test("audio-device queries share readiness, timeout, and concurrency guards", async (t) => {
  const pending = deferred();
  const started = deferred();
  let calls = 0;
  const { transport, port, mutable } = await createTransport({
    verifiedAudioDeviceProtocol: true,
    requestTimeoutMs: 30,
    maxConcurrentInvokes: 1,
    outputSink: async () => ({ ok: true }),
    invokePlugin: async ({ command, id, signal }) => {
      calls += 1;
      started.resolve();
      if (id === 714) {
        await pending.promise;
        void mutable.transport.publishOutput({
          command: 20061, id: 0,
          value: [
            Array.from({ length: 18 }, (_, index) => index + 1),
            Array.from({ length: 18 }, (_, index) => `Output ${index + 1}`),
            Array.from({ length: 18 }, (_, index) => `{OUT-${index + 1}}`),
            Array.from({ length: 15 }, (_, index) => index + 1),
            Array.from({ length: 15 }, (_, index) => `Input ${index + 1}`),
            Array.from({ length: 15 }, (_, index) => `{IN-${index + 1}}`),
            0, 0, 10, 8,
          ],
          hostGeneration: HOST_GENERATION,
        });
        return { posted: true, id, command };
      }
      return new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("deadline"), { code: "invoke_timeout" })), { once: true });
      });
    },
  });
  t.after(() => transport.stop());

  mutable.readiness.ready = false;
  const notReady = await postJson(port, "/v1/audio/devices", requestBody());
  assert.equal(notReady.status, 503);
  assert.equal(notReady.body.code, "host_not_ready");
  assert.equal(calls, 0);

  mutable.readiness.ready = true;
  const firstRequest = postJson(port, "/v1/audio/devices", requestBody({ id: 714 }));
  await started.promise;
  const second = await postJson(port, "/v1/audio/devices", requestBody({ id: 715 }));
  assert.equal(second.status, 409);
  assert.equal(second.body.code, "audio_device_query_busy");
  pending.resolve();
  const first = await firstRequest;
  assert.equal(first.status, 200);

  const timedOut = await postJson(port, "/v1/audio/devices", requestBody({ id: 716 }));
  assert.equal(timedOut.status, 504);
  assert.equal(timedOut.body.code, "invoke_timeout");
  assert.equal(calls, 2, "timeout does not retry the SDK post");
  assert.equal(transport.status().activeInvokes, 0);
});

test("shutdown aborts a pending audio-device query and closes its HTTP listener", async () => {
  const started = deferred();
  let calls = 0;
  const { transport, port } = await createTransport({
    verifiedAudioDeviceProtocol: true,
    invokePlugin: async ({ signal }) => {
      calls += 1;
      started.resolve();
      return new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("stopped"), { code: "host_stopped" })), { once: true });
      });
    },
  });
  const request = postJson(port, "/v1/audio/devices", requestBody());
  await started.promise;
  const stopped = await transport.stop();
  const response = await request;
  assert.equal(response.status, 503);
  assert.equal(response.body.code, "host_stopped");
  assert.equal(stopped.stopped, true);
  assert.equal(calls, 1);
});
