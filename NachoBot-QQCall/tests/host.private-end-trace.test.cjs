"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const http = require("node:http");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildHostConfigs, writePrivateJson } = require("../host/config.cjs");
const { createHostTransport } = require("../host/transport.cjs");
const { createPrivateEndTraceWriter, sanitizePrivateEndTraceEvent } = require("../host/status-report.cjs");

const TOKEN = Buffer.alloc(32, 0x73).toString("base64url");
const HOST_GENERATION = 73;

async function createTransport(t, options = {}) {
  let transport;
  transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    targetPid: process.pid,
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    outputSink: async () => {},
    verifiedPrivateEndProtocol: true,
    ...options,
  });
  await transport.start();
  t.after(async () => { await transport.stop().catch(() => {}); });
  return { transport, port: transport.status().listenPort };
}

function postInvoke(port, { command, id, params, hostGeneration = HOST_GENERATION }) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: "/v1/invoke",
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        let body;
        try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
        catch { return reject(new Error("invalid test response")); }
        resolve({ status: response.statusCode, body });
      });
    });
    request.once("error", reject);
    request.end(JSON.stringify({ command, id, params, hostGeneration }));
  });
}

function privateTraceFixture(root) {
  const appPath = path.join(root, "qq-app");
  const profilePath = path.join(root, "profile");
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(appPath, "avsdk", process.platform === "win32" ? "AVSDKPlugin.dll" : "libAVSDKPlugin.so"), "test plugin");
  fs.mkdirSync(profilePath, { recursive: true });
  const reportPath = path.join(root, "reports", "host");
  const configs = buildHostConfigs({
    targetPid: process.pid,
    appPath,
    reportPath,
    profilePath,
    listenPort: 6111,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    token: TOKEN,
    pipeNonce: "0123456789abcdef0123456789abcdef",
  });
  writePrivateJson(`${reportPath}.host.config.json`, configs.hostConfig);
  return { ...configs, reportPath };
}

function loadEntrypointWithHostMock(entryPath, electronStub = {}) {
  const resolvedEntry = require.resolve(entryPath);
  const resolvedHost = require.resolve("../host/electron-host.cjs");
  const oldEntry = require.cache[resolvedEntry];
  delete require.cache[resolvedEntry];
  const originalLoad = Module._load;
  let capturedOptions = null;
  const runtime = {
    start: async () => ({ ready: true, listening: true }),
    status: () => ({ ready: true, listening: true, stopped: false }),
    stop: async () => ({ ready: false, listening: false, stopped: true }),
  };
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "./electron-host.cjs" && require.resolve(path.resolve(path.dirname(parent.filename), request)) === resolvedHost) {
      return {
        createElectronHost(options) { capturedOptions = options; return runtime; },
        configureHost() {},
      };
    }
    if (request === "electron") return electronStub;
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    const loaded = require(entryPath);
    let restored = false;
    return {
      loaded,
      runtime,
      getOptions: () => capturedOptions,
      restore() {
        if (restored) return;
        restored = true;
        Module._load = originalLoad;
        if (oldEntry) require.cache[resolvedEntry] = oldEntry;
        else delete require.cache[resolvedEntry];
      },
    };
  } catch (error) {
    Module._load = originalLoad;
    if (oldEntry) require.cache[resolvedEntry] = oldEntry;
    else delete require.cache[resolvedEntry];
    throw error;
  }
}

test("private-end trace redacts payloads, separates ID-zero ACKs, and keeps pre-receipt outputs ordered", async (t) => {
  const events = [];
  let transport;
  const setup = await createTransport(t, {
    onPrivateEndTrace(event) { events.push(event); },
    invokePlugin: async ({ command, id }) => {
      await transport.publishOutput({ command: 8, id: 0, value: [0, ""] }, { rawIdState: "missing" });
      await transport.publishOutput({ command: 8, id, value: [7, "native-error-private-marker"] }, { rawIdState: "valid" });
      await transport.publishOutput({ command: 20001, id: 0, value: [3, "uid-private-marker"] }, { rawIdState: "missing" });
      await transport.publishOutput({ command: 20011, id: 0, value: [1, "relation-private-marker", "room-private-marker"] }, { rawIdState: "missing" });
      return { posted: true, id, command };
    },
  });
  transport = setup.transport;

  const result = await postInvoke(setup.port, { command: 8, id: 901, params: [1, 0] });
  assert.equal(result.status, 200);
  assert.deepEqual(result.body, { posted: true, id: 901, command: 8 });
  await transport.stop();

  const request = events.find((event) => event.type === "request");
  assert.deepEqual(request.params, { scene: 1, reason: 0 });
  assert.equal(request.hostGeneration, HOST_GENERATION);
  assert.equal(request.targetPid, process.pid);
  const outputs = events.filter((event) => event.type === "sdk_output");
  assert.equal(outputs.length, 4);
  assert.equal(outputs[0].association, "id_zero_uncorrelated");
  assert.equal(outputs[0].normalizedId, 0);
  assert.equal(outputs[0].rawIdState, "missing");
  assert.equal(outputs[0].ackReturnCode, 0);
  assert.equal(outputs[0].ackSecondEmptyString, true);
  assert.equal(outputs[1].association, "request_id_match");
  assert.equal(outputs[1].requestId, 901);
  assert.equal(outputs[1].ackReturnCode, 7);
  assert.equal(outputs[1].ackSecondEmptyString, false);
  assert.equal(outputs[2].association, "temporal_only");
  assert.equal(outputs[3].association, "temporal_only");
  assert.equal(outputs[3].scene, 1);
  assert.equal(JSON.stringify(events).includes("native-error-private-marker"), false);
  assert.equal(JSON.stringify(events).includes("uid-private-marker"), false);
  assert.equal(JSON.stringify(events).includes("relation-private-marker"), false);
  assert.equal(JSON.stringify(events).includes("room-private-marker"), false);
  const postedIndex = events.findIndex((event) => event.type === "post_stage" && event.postedStage === "renderer_posted");
  assert.ok(events.findIndex((event) => event.type === "sdk_output") < postedIndex,
    "SDK output arriving before the renderer post receipt remains ordered before that receipt");
  const ended = events.find((event) => event.type === "window_end");
  assert.equal(ended.reason, "host_stopped");
  assert.equal(ended.network20001Count, 1);
  assert.deepEqual(ended.commandCounts, [
    { command: 8, count: 2 },
    { command: 20001, count: 1 },
    { command: 20011, count: 1 },
  ]);
});

test("private Close trace keeps only peer UID length and correlates the numeric ACK", async (t) => {
  const events = [];
  const peerUid = `u_${"C".repeat(22)}`;
  const setup = await createTransport(t, {
    onPrivateEndTrace(event) { events.push(event); },
    invokePlugin: async ({ command, id }) => {
      await setup.transport.publishOutput({ command, id, value: [0, ""] }, { rawIdState: "valid" });
      return { posted: true, id, command };
    },
  });
  const result = await postInvoke(setup.port, { command: 10, id: 1007, params: [1, peerUid, 0] });
  assert.equal(result.status, 200);
  await setup.transport.stop();

  const request = events.find((event) => event.type === "request");
  assert.deepEqual(request.params, { scene: 1, peerUidType: "string", peerUidLength: Buffer.byteLength(peerUid), closeReason: 0 });
  const sanitizedRequest = sanitizePrivateEndTraceEvent(request);
  assert.deepEqual(sanitizedRequest.params, request.params, "the persisted report accepts only the redacted Close request shape");
  const ack = events.find((event) => event.type === "sdk_output");
  assert.equal(ack.command, 10);
  assert.equal(ack.association, "request_id_match");
  assert.equal(ack.requestId, 1007);
  assert.equal(ack.ackReturnCode, 0);
  assert.equal(ack.ackSecondEmptyString, true);
  const sanitizedAck = sanitizePrivateEndTraceEvent(ack);
  assert.equal(sanitizedAck.requestId, 1007);
  assert.equal(sanitizedAck.ackReturnCode, 0);
  assert.equal(JSON.stringify(events).includes(peerUid), false, "the trace never stores the raw caller UID");
});

test("private-end trace does not bind duplicate request IDs across active windows", async (t) => {
  const events = [];
  let transport;
  let calls = 0;
  const setup = await createTransport(t, {
    onPrivateEndTrace(event) { events.push(event); },
    invokePlugin: async ({ command, id }) => {
      calls += 1;
      if (calls === 2) {
        await transport.publishOutput({ command: 8, id, value: [0, ""] }, { rawIdState: "valid" });
      }
      return { posted: true, id, command };
    },
  });
  transport = setup.transport;
  await postInvoke(setup.port, { command: 8, id: 902, params: [1, 0] });
  await postInvoke(setup.port, { command: 8, id: 902, params: [1, 0] });
  const output = events.find((event) => event.type === "sdk_output");
  assert.equal(output.association, "multiple_id_matches");
  assert.equal(Object.hasOwn(output, "requestId"), false);
  const requests = events.filter((event) => event.type === "request");
  assert.equal(requests[0].correlationEligible, true);
  assert.equal(requests[1].correlationEligible, false);
});

test("private-end trace expires after five seconds and counts non-terminal SDK commands", { timeout: 9000 }, async (t) => {
  const events = [];
  const setup = await createTransport(t, { onPrivateEndTrace(event) { events.push(event); } });
  await postInvoke(setup.port, { command: 8, id: 903, params: [1, 0] });
  await setup.transport.publishOutput({ command: 55, id: 13, value: [1, "outgoing string omitted"] });
  await new Promise((resolve) => setTimeout(resolve, 5200));
  const ended = events.find((event) => event.type === "window_end");
  assert.equal(ended.reason, "window_elapsed");
  assert.equal(ended.elapsedMs, 5000);
  assert.deepEqual(ended.commandCounts, [{ command: 55, count: 1 }]);
  assert.equal(ended.network20001Count, 0);
  const outputCount = events.filter((event) => event.type === "sdk_output").length;
  await setup.transport.publishOutput({ command: 20011, id: 0, value: [1, "stale"] });
  assert.equal(events.filter((event) => event.type === "sdk_output").length, outputCount,
    "output outside the five-second window is ignored");
});

test("private-end trace enforces expiry before a delayed timer callback", async (t) => {
  const events = [];
  const setup = await createTransport(t, { onPrivateEndTrace(event) { events.push(event); } });
  await postInvoke(setup.port, { command: 8, id: 905, params: [1, 0] });
  const realNow = Date.now;
  try {
    Date.now = () => realNow() + 6000;
    await setup.transport.publishOutput({ command: 8, id: 905, value: [0, ""] });
  } finally {
    Date.now = realNow;
  }
  assert.equal(events.some((event) => event.type === "sdk_output"), false,
    "a late SDK frame cannot enter an expired trace even if the event-loop timer was delayed");
  assert.equal(events.find((event) => event.type === "window_end").reason, "window_elapsed");
});

test("private-end trace output limit is per window, so a later transaction still records SDK evidence", async (t) => {
  const events = [];
  const setup = await createTransport(t, { onPrivateEndTrace(event) { events.push(event); } });
  const first = await postInvoke(setup.port, { command: 8, id: 910, params: [1, 0] });
  assert.equal(first.status, 200);
  for (let index = 0; index < 65; index += 1) {
    await setup.transport.publishOutput({ command: 20011, id: 0, value: [1, ""] });
  }

  const second = await postInvoke(setup.port, { command: 8, id: 911, params: [1, 0] });
  assert.equal(second.status, 200);
  await setup.transport.publishOutput({ command: 8, id: 911, value: [0, ""] });

  const outputs = events.filter((event) => event.type === "sdk_output");
  assert.equal(outputs.length, 65);
  assert.equal(outputs.at(-1).association, "request_id_match");
  assert.equal(outputs.at(-1).requestId, 911);
  assert.equal(outputs.at(-1).ackReturnCode, 0);
  await setup.transport.stop();
  const firstEnd = events.find((event) => event.type === "window_end" && event.requestId === 910);
  const secondEnd = events.find((event) => event.type === "window_end" && event.requestId === 911);
  assert.equal(firstEnd.droppedRelevantRecords, 2);
  assert.equal(secondEnd.droppedRelevantRecords, 0);
});

test("private-end trace supersedes the oldest of 16 windows without changing invoke or ACK handling", async (t) => {
  const events = [];
  let invokeCount = 0;
  let transport;
  const setup = await createTransport(t, {
    onPrivateEndTrace(event) { events.push(event); },
    invokePlugin: async ({ command, id }) => {
      invokeCount += 1;
      await transport.publishOutput({ command, id, value: [0, ""] }, { rawIdState: "valid" });
      return { posted: true, id, command };
    },
  });
  transport = setup.transport;

  const results = [];
  for (let index = 0; index < 17; index += 1) {
    results.push(await postInvoke(setup.port, { command: 8, id: 1200 + index, params: [1, 0] }));
  }

  assert.equal(results.every((result) => result.status === 200 && result.body.posted === true), true);
  assert.equal(invokeCount, 17);
  assert.equal(transport.status().invokePosted, 17);
  const superseded = events.find((event) => event.type === "window_end" && event.requestId === 1200);
  assert.equal(superseded.reason, "window_superseded");
  assert.ok(sanitizePrivateEndTraceEvent(superseded), "the private report redactor accepts supersession reasons");
  const outputs = events.filter((event) => event.type === "sdk_output");
  assert.equal(outputs.length, 17);
  for (let index = 0; index < 17; index += 1) {
    const output = outputs.find((event) => event.requestId === 1200 + index);
    assert.equal(output.association, "request_id_match");
    assert.equal(output.ackReturnCode, 0);
  }
  assert.equal(events.filter((event) => event.type === "request").length, 17);
});

test("private-end trace writer is bounded, redacted, replaceable, and atomic-failure-safe", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-end-trace-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "report");
  const target = `${reportPath}.host.private-end.trace.json`;
  const uid = `u_${"S".repeat(22)}`;
  const writer = createPrivateEndTraceWriter(reportPath);
  writer({
    type: "request", traceId: 1, requestId: 990, command: 9, hostGeneration: HOST_GENERATION,
    targetPid: process.pid, atMs: Date.now(), postedStage: "dispatching",
    params: { scene: 1, peerUidType: "string", peerUidLength: Buffer.byteLength(uid), rejectReason: 2, peerUid: uid },
  });
  writer({ type: "post_stage", traceId: 1, requestId: 990, command: 9, hostGeneration: HOST_GENERATION, targetPid: process.pid, atMs: Date.now(), postedStage: "renderer_posted" });
  for (let index = 0; index < 100; index += 1) {
    writer({
      type: "sdk_output", atMs: Date.now() + index, hostGeneration: HOST_GENERATION, targetPid: process.pid,
      command: 20011, normalizedId: index, rawIdState: "missing",
      valueType: "array", arrayLength: 3, elementTypes: ["number", "string", "string"], scene: 1,
      association: "temporal_only", activeWindowCount: 1, secretValue: "large-output-private-marker".repeat(100),
    });
  }
  assert.equal(await writer.flush(), true);
  const bytes = fs.readFileSync(target);
  assert.ok(bytes.byteLength <= 64 * 1024);
  const document = JSON.parse(bytes.toString("utf8"));
  assert.equal(document.events.filter((event) => event.type === "sdk_output").length, 64);
  assert.equal(document.droppedOutputCount, 36);
  const outputs = document.events.filter((event) => event.type === "sdk_output");
  assert.equal(outputs[0].normalizedId, 36);
  assert.equal(outputs.at(-1).normalizedId, 99);
  assert.equal(bytes.toString("utf8").includes(uid), false);
  assert.equal(bytes.toString("utf8").includes("large-output-private-marker"), false);
  assert.equal(document.events[0].params.peerUidLength, Buffer.byteLength(uid));
  writer({
    type: "post_stage", traceId: 1, requestId: 990, command: 9, hostGeneration: HOST_GENERATION,
    targetPid: process.pid, atMs: Date.now(), postedStage: "host_stopped",
  });
  assert.equal(await writer.flush(), true);
  const replacement = fs.readFileSync(target);
  assert.notDeepEqual(replacement, bytes, "a later snapshot atomically replaces the earlier complete file");
  assert.equal(JSON.parse(replacement.toString("utf8")).events.some((event) => event.postedStage === "host_stopped"), true);
  const previous = fs.readFileSync(target);

  const failingWriter = createPrivateEndTraceWriter(reportPath, { writeSnapshot: async () => false });
  failingWriter({ type: "post_stage", traceId: 1, requestId: 990, command: 9, hostGeneration: HOST_GENERATION, targetPid: process.pid, atMs: Date.now(), postedStage: "host_stopped" });
  assert.equal(await failingWriter.flush(), false);
  assert.deepEqual(fs.readFileSync(target), previous, "failed writes leave the last complete trace intact");
});

test("chat-closed diagnostics retain only bounded reason fields and grant no correlation", () => {
  const event = sanitizePrivateEndTraceEvent({
    type: "sdk_output", atMs: Date.now(), hostGeneration: HOST_GENERATION, targetPid: process.pid,
    command: 20017, normalizedId: 0, rawIdState: "valid", valueType: "array", arrayLength: 6,
    elementTypes: ["number", "number", "boolean", "number", "string", "string"],
    association: "temporal_only", activeWindowCount: 1, closeReason: 3, destroyReason: 0,
    inRoomValueKind: "false_boolean", url: "private-url-marker", errMsg: "private-error-marker",
    value: [3, 0, false, 123, "private-url-marker", "private-error-marker"],
  });
  assert.equal(event.closeReason, 3);
  assert.equal(event.destroyReason, 0);
  assert.equal(event.inRoomValueKind, "false_boolean");
  assert.equal(event.association, "temporal_only");
  assert.equal(event.rawValueOmitted, true);
  assert.equal(JSON.stringify(event).includes("private-"), false);
  assert.equal(Object.hasOwn(event, "traceId"), false);
});

test("transport records chat-closed shape without changing the forwarded SDK frame", async (t) => {
  const events = [], forwarded = [];
  const setup = await createTransport(t, {
    onPrivateEndTrace(event) { events.push(event); },
    outputSink: async (frame) => { forwarded.push(frame); },
  });
  const response = await postInvoke(setup.port, { command: 9, id: 919, params: [1, `u_${"P".repeat(22)}`, 1] });
  assert.equal(response.status, 200);
  const value = [3, 0, false, 123, "private-url-marker", "private-error-marker"];
  await setup.transport.publishOutput({ command: 20017, id: 0, value });
  const observed = events.find((event) => event.command === 20017 && event.type === "sdk_output");
  assert.equal(observed.closeReason, 3);
  assert.equal(observed.destroyReason, 0);
  assert.equal(observed.inRoomValueKind, "false_boolean");
  assert.equal(observed.association, "temporal_only");
  assert.equal(JSON.stringify(observed).includes("private-"), false);
  assert.equal(forwarded.length, 1);
  assert.deepEqual(forwarded[0].value, value);
});

test("transport stop preserves a private-end window for the private writer", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-end-stop-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "host-report");
  const writer = createPrivateEndTraceWriter(reportPath);
  const setup = await createTransport(t, { onPrivateEndTrace: writer });
  await postInvoke(setup.port, { command: 8, id: 906, params: [1, 0] });
  await setup.transport.stop();
  assert.equal(await writer.flush(), true);
  const document = JSON.parse(fs.readFileSync(`${reportPath}.host.private-end.trace.json`, "utf8"));
  assert.equal(document.events.some((event) => event.type === "window_end" && event.reason === "host_stopped"), true);
});

test("throwing trace observers do not affect invoke or output acceptance", async (t) => {
  const setup = await createTransport(t, {
    onPrivateEndTrace() { throw new Error("diagnostic failure"); },
  });
  const result = await postInvoke(setup.port, { command: 8, id: 904, params: [1, 0] });
  assert.equal(result.status, 200);
  assert.equal(result.body.posted, true);
  await setup.transport.publishOutput({ command: 8, id: 904, value: [0, ""] });
  assert.equal(setup.transport.status().invokePosted, 1);
});

test("bootstrap and Electron main wire private trace observers to their generation report path", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-private-end-entrypoint-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const native = privateTraceFixture(root);
  const privateEvent = {
    type: "request", traceId: 1, requestId: 991, command: 8, hostGeneration: HOST_GENERATION,
    targetPid: process.pid, atMs: Date.now(), postedStage: "dispatching", params: { scene: 1, reason: 0 },
  };

  const stateKey = Symbol.for("nachobot.qqcall.avsdk-host.v1");
  delete globalThis[stateKey];
  let bootstrapController;
  const bootstrapMock = loadEntrypointWithHostMock("../host/bootstrap.cjs", { app: new EventEmitter() });
  try {
    bootstrapController = await bootstrapMock.loaded.bootstrap(native.nativeConfig);
    const options = bootstrapMock.getOptions();
    assert.equal(typeof options.onPrivateEndTrace, "function");
    options.onPrivateEndTrace(privateEvent);
    assert.equal(await options.onPrivateEndTrace.flush(), true);
    assert.equal(fs.existsSync(`${native.reportPath}.host.private-end.trace.json`), true);
    await bootstrapController.stop();
  } finally {
    bootstrapMock.restore();
    if (bootstrapController) await bootstrapController.stop().catch(() => {});
    delete globalThis[stateKey];
  }

  fs.rmSync(`${native.reportPath}.host.private-end.trace.json`, { force: true });
  const mainMock = loadEntrypointWithHostMock("../host/electron-main.cjs");
  try {
    const runtime = mainMock.loaded.main({ app: new EventEmitter() }, { QQCALL_HOST_CONFIG: `${native.reportPath}.host.config.json` });
    assert.ok(runtime);
    await new Promise((resolve) => setImmediate(resolve));
    const options = mainMock.getOptions();
    assert.equal(typeof options.onPrivateEndTrace, "function");
    options.onPrivateEndTrace(privateEvent);
    assert.equal(await options.onPrivateEndTrace.flush(), true);
    assert.equal(fs.existsSync(`${native.reportPath}.host.private-end.trace.json`), true);
    await runtime.stop();
  } finally {
    mainMock.restore();
  }
});
