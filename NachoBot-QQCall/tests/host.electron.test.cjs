"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");
const { createElectronHost } = require("../host/electron-host.cjs");
const { buildHostConfigs } = require("../host/config.cjs");
const { writeHostCrash } = require("../host/status-report.cjs");

const TOKEN = Buffer.alloc(32, 0x62).toString("base64url");
const pluginFilename = process.platform === "win32" ? "AVSDKPlugin.dll" : process.platform === "linux" ? "libAVSDKPlugin.so" : null;
const READY_CHANNEL = "nachobot-qqcall-host:ready";
const OUTPUT_CHANNEL = "nachobot-qqcall-host:output";
const HOST_HTML = path.resolve(__dirname, "../host/host.html");
const HOST_URL = pathToFileURL(HOST_HTML).href;

function createElectronMock(config, { getOSProcessId = () => 43210 } = {}) {
  const app = new EventEmitter();
  const switches = new Map();
  let userDataPath = "";
  app.commandLine = {
    hasSwitch: (name) => switches.has(name),
    getSwitchValue: (name) => switches.get(name) || "",
    appendSwitch: (name, value) => switches.set(name, value ?? ""),
  };
  app.isReady = () => false;
  app.setPath = (name, value) => { if (name === "userData") userDataPath = value; };
  app.getPath = (name) => name === "userData" ? userDataPath : "";
  app.whenReady = () => Promise.resolve();

  const ipcMain = new EventEmitter();
  let windowRef = null;
  class BrowserWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: HOST_URL };
      this.webContents.getOSProcessId = getOSProcessId;
      this.webContents.setWindowOpenHandler = (handler) => { this.windowOpenHandler = handler; };
      this.webContents.getURL = () => HOST_URL;
      this.webContents.executeJavaScript = async (script, userGesture) => {
        this.lastScript = script;
        this.lastUserGesture = userGesture;
        const match = script.match(/^window\.__qqCallHostInvoke\((\d+),(\d+),/);
        if (!match) throw new Error("unexpected host script");
        return { posted: true, command: Number(match[1]), id: Number(match[2]) };
      };
      this.loadFile = async (filename) => {
        assert.equal(filename, HOST_HTML);
        this.webContents.emit("did-start-navigation", {}, HOST_URL, false, true);
        this.webContents.emit("dom-ready");
        this.webContents.emit("did-finish-load");
        const event = { sender: this.webContents, senderFrame: this.webContents.mainFrame };
        ipcMain.emit(READY_CHANNEL, event, { pluginFound: true, hasPostMessage: true });
      };
      this.isDestroyed = () => this.destroyed;
      this.destroy = () => {
        if (this.destroyed) return;
        this.destroyed = true;
        this.emit("closed");
      };
      windowRef = this;
    }
  }
  return { electron: { app, BrowserWindow, ipcMain }, app, ipcMain, window: () => windowRef, switches, userDataPath: () => userDataPath, config };
}

function makeConfig(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-host-electron-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const appPath = path.join(root, "qq-app");
  const profilePath = path.join(root, "profile");
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(appPath, "avsdk", pluginFilename || "plugin.stub"), "mock plugin");
  fs.mkdirSync(profilePath, { recursive: true });
  const configs = buildHostConfigs({
    targetPid: process.pid,
    appPath,
    reportPath: path.join(root, "host-report"),
    profilePath,
    listenPort: 6111,
    outputPort: 6110,
    hostGeneration: 31,
    token: TOKEN,
    pipeNonce: "0123456789abcdef0123456789abcdef",
  });
  return { root, config: { ...configs.hostConfig, listenPort: 0 } };
}

async function startRendererTestHost(t, { onRendererGone = () => {}, onFailure = () => {}, getOSProcessId = () => 43210 } = {}) {
  const { config: baseConfig } = makeConfig(t);
  const callbackServer = http.createServer((request, response) => {
    if (request.method === "GET" && request.url === "/v1/callback-ready") {
      return jsonReply(response, 200, {
        ok: true,
        callbackReady: true,
        hostGeneration: baseConfig.hostGeneration,
        started: true,
        subscriberReady: true,
        stopping: false,
        stopped: false,
      });
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => callbackServer.listen(0, "127.0.0.1", resolve));
  const config = { ...baseConfig, outputPort: callbackServer.address().port };
  const mock = createElectronMock(config, { getOSProcessId });
  const host = createElectronHost({
    electron: mock.electron,
    config,
    onRendererGone,
    onFailure,
    readyTimeoutMs: 1000,
    callbackReadyTimeoutMs: 1000,
    callbackReadyRequestTimeoutMs: 300,
    callbackReadyPollMs: 20,
  });
  t.after(async () => {
    await host.stop().catch(() => {});
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });
  const started = await host.start();
  assert.equal(started.ready, true);
  return { config, host, mock };
}

function readCrashReport(config) {
  return fs.readFileSync(`${config.reportPath}.host.crash.json`, "utf8");
}

function postJson(port, pathname, body) {
  return new Promise((resolve, reject) => {
    const bytes = Buffer.from(JSON.stringify(body));
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
        "Content-Length": bytes.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
    });
    request.once("error", reject);
    request.end(bytes);
  });
}

function getJson(port, pathname) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method: "GET",
      headers: { Authorization: `Bearer ${TOKEN}` },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end();
  });
}

function jsonReply(response, status, body) {
  const encoded = Buffer.from(JSON.stringify(body));
  response.writeHead(status, { "content-type": "application/json", "content-length": encoded.length });
  response.end(encoded);
}

async function waitFor(predicate, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

test("Electron host trusts only its local renderer and closes transport on renderer crash", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const { config } = makeConfig(t);
  let outputResolve;
  const outputReceived = new Promise((resolve) => { outputResolve = resolve; });
  const callbackReadinessRequests = [];
  const controller = http.createServer((request, response) => {
    if (request.method === "GET" && request.url === "/v1/callback-ready") {
      callbackReadinessRequests.push({
        authorization: request.headers.authorization,
        hostGeneration: request.headers["x-qqcall-host-generation"],
      });
      jsonReply(response, 200, {
        ok: true,
        callbackReady: true,
        hostGeneration: config.hostGeneration,
        started: true,
        subscriberReady: true,
        stopping: false,
        stopped: false,
      });
      return;
    }
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      outputResolve({
        authorization: request.headers.authorization,
        path: request.url,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      });
      response.writeHead(200);
      response.end("ok");
    });
  });
  await new Promise((resolve) => controller.listen(0, "127.0.0.1", resolve));
  const { port: outputPort } = controller.address();
  const mock = createElectronMock({ ...config, outputPort });
  const host = createElectronHost({ electron: mock.electron, config: { ...config, outputPort }, readyTimeoutMs: 1000 });
  t.after(async () => {
    await host.stop().catch(() => {});
    await new Promise((resolve) => controller.close(resolve));
  });

  const started = await host.start();
  assert.equal(started.ready, true);
  assert.equal(started.pluginFound, true);
  assert.equal(started.hasPostMessage, true);
  assert.equal(started.callbackReady, true);
  assert.deepEqual({
    reserved: started.groupCallReserved,
    state: started.groupCallState,
    quarantined: started.groupCallQuarantined,
    entryVerified: started.groupCallNativeEntryVerified,
  }, { reserved: false, state: "idle", quarantined: false, entryVerified: false });
  assert.equal("groupId" in started, false);
  assert.equal("startRequestId" in started, false);
  assert.equal("nativeEntryProof" in started, false);
  const health = await getJson(started.listenPort, "/healthz");
  assert.equal(health.status, 200);
  assert.deepEqual({
    reserved: health.body.status.groupCallReserved,
    state: health.body.status.groupCallState,
    quarantined: health.body.status.groupCallQuarantined,
    entryVerified: health.body.status.groupCallNativeEntryVerified,
  }, { reserved: false, state: "idle", quarantined: false, entryVerified: false });
  assert.deepEqual(callbackReadinessRequests[0], {
    authorization: `Bearer ${TOKEN}`,
    hostGeneration: String(config.hostGeneration),
  });
  assert.equal(mock.window().options.show, false);
  assert.equal(mock.window().options.webPreferences.plugins, true);
  assert.equal(mock.userDataPath(), config.profilePath);
  assert.deepEqual(mock.window().windowOpenHandler(), { action: "deny" });

  const posted = await postJson(started.listenPort, "/v1/invoke", {
    command: 1,
    id: 901,
    params: ["uid", "uin", "uin", "account", ""],
    hostGeneration: config.hostGeneration,
  });
  assert.equal(posted.status, 200);
  assert.deepEqual(posted.body, { posted: true, id: 901, command: 1 });
  assert.equal(mock.window().lastUserGesture, true);
  assert.match(mock.window().lastScript, /^window\.__qqCallHostInvoke\(1,901,/);

  mock.ipcMain.emit(OUTPUT_CHANNEL, { sender: new EventEmitter(), senderFrame: { url: "https://example.invalid" } }, {
    command: 20006,
    id: 0,
    value: "untrusted",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.status().ready, true);

  mock.ipcMain.emit(OUTPUT_CHANNEL, {
    sender: mock.window().webContents,
    senderFrame: mock.window().webContents.mainFrame,
  }, { command: 20006, id: 0, value: ["event"] });
  const output = await outputReceived;
  assert.equal(output.authorization, `Bearer ${TOKEN}`);
  assert.equal(output.path, "/v1/avsdk/output");
  assert.deepEqual(output.body, { command: 20006, id: 0, value: ["event"], hostGeneration: config.hostGeneration });

  mock.window().webContents.emit("render-process-gone", {}, { reason: "crashed" });
  const stopped = await host.stop();
  assert.equal(stopped.ready, false);
  assert.equal(stopped.rendererGone, true);
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.listening, false);
  assert.equal(stopped.groupCallNativeEntryVerified, false);
  assert.equal(stopped.groupCallState, "idle");
  assert.equal(mock.window().isDestroyed(), true);
  assert.equal(mock.ipcMain.listenerCount(READY_CHANNEL), 0);
  assert.equal(mock.ipcMain.listenerCount(OUTPUT_CHANNEL), 0);
  assert.equal(mock.app.listenerCount("before-quit"), 0);
});

test("Electron keeps audio-device query disabled for an unpinned resolved plugin regardless of config flags", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const { config } = makeConfig(t);
  let callbackPort;
  const controller = http.createServer((request, response) => {
    if (request.method === "GET" && request.url === "/v1/callback-ready") {
      jsonReply(response, 200, {
        ok: true,
        callbackReady: true,
        hostGeneration: config.hostGeneration,
        started: true,
        subscriberReady: true,
        stopping: false,
        stopped: false,
      });
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => controller.listen(0, "127.0.0.1", resolve));
  callbackPort = controller.address().port;
  const mock = createElectronMock(config);
  const host = createElectronHost({
    electron: mock.electron,
    config: {
      ...config,
      outputPort: callbackPort,
      verifiedAudioDeviceProtocol: true,
      verifiedAudioDeviceSelectionProtocol: true,
    },
    readyTimeoutMs: 1000,
  });
  t.after(async () => {
    await host.stop().catch(() => {});
    await new Promise((resolve) => controller.close(resolve));
  });

  const started = await host.start();
  const response = await postJson(started.listenPort, "/v1/audio/devices", {
    id: 903,
    hostGeneration: config.hostGeneration,
  });
  assert.equal(response.status, 501);
  assert.deepEqual(response.body, { ok: false, code: "audio_device_protocol_unverified" });
  const selectionResponse = await postJson(started.listenPort, "/v1/audio/select", {
    id: 904,
    hostGeneration: config.hostGeneration,
    inputDeviceName: "private-input-name",
    outputDeviceName: "private-output-name",
  });
  assert.equal(selectionResponse.status, 501);
  assert.deepEqual(selectionResponse.body, { ok: false, code: "audio_device_selection_unverified" });
  assert.equal(started.privateEndProtocolVerified, false);
  const hangupResponse = await postJson(started.listenPort, "/v1/invoke", {
    command: 8,
    id: 908,
    params: [1, 0],
    hostGeneration: config.hostGeneration,
  });
  assert.equal(hangupResponse.status, 501);
  assert.deepEqual(hangupResponse.body, { ok: false, code: "private_end_protocol_unverified" });
  const rejectResponse = await postJson(started.listenPort, "/v1/invoke", {
    command: 9,
    id: 909,
    params: [1, `u_${"P".repeat(22)}`, 1],
    hostGeneration: config.hostGeneration,
  });
  assert.equal(rejectResponse.status, 501);
  assert.deepEqual(rejectResponse.body, { ok: false, code: "private_end_protocol_unverified" });
  assert.equal(mock.window().lastScript, undefined, "a caller config flag cannot authorize a PPAPI post");
});

test("Electron Host waits for matching authenticated Bridge readiness before creating its PPAPI window", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const { config } = makeConfig(t);
  let callbackReady = true;
  let responseGeneration = config.hostGeneration + 1;
  let readinessRequests = 0;
  const callbackServer = http.createServer((request, response) => {
    if (request.method !== "GET" || request.url !== "/v1/callback-ready") {
      jsonReply(response, 404, { ok: false, code: "not_found" });
      return;
    }
    readinessRequests += 1;
    const matches = request.headers.authorization === `Bearer ${TOKEN}` &&
      request.headers["x-qqcall-host-generation"] === String(config.hostGeneration);
    const ready = callbackReady && matches && responseGeneration === config.hostGeneration;
    jsonReply(response, ready ? 200 : 503, {
      ok: ready,
      callbackReady: ready,
      hostGeneration: responseGeneration,
      started: true,
      subscriberReady: true,
      stopping: false,
      stopped: false,
      ...(!ready ? { code: "callback_not_ready" } : {}),
    });
  });
  await new Promise((resolve) => callbackServer.listen(0, "127.0.0.1", resolve));
  const outputPort = callbackServer.address().port;
  const mock = createElectronMock(config);
  const host = createElectronHost({
    electron: mock.electron,
    config: { ...config, outputPort },
    callbackReadyTimeoutMs: 1500,
    callbackReadyRequestTimeoutMs: 300,
    callbackReadyPollMs: 20,
    readyTimeoutMs: 1000,
  });
  t.after(async () => {
    await host.stop().catch(() => {});
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });

  const startPromise = host.start();
  await waitFor(() => host.status().listening && readinessRequests > 0);
  assert.equal(mock.window(), null, "wrong generation must not instantiate an embed");
  assert.equal(host.status().ready, false);
  callbackReady = true;
  assert.equal(mock.window(), null, "a true but mismatched generation is still not sufficient");
  responseGeneration = config.hostGeneration;
  const started = await startPromise;
  assert.equal(readinessRequests >= 2, true);
  assert.equal(mock.window() !== null, true);
  assert.equal(started.callbackReady, true);
  assert.equal(started.ready, true);
});

test("callback readiness timeout and stop during the barrier never create a PPAPI window", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const { config } = makeConfig(t);
  const callbackServer = http.createServer((_request, response) => {
    jsonReply(response, 503, {
      ok: false,
      callbackReady: false,
      hostGeneration: config.hostGeneration,
      started: true,
      subscriberReady: false,
      stopping: false,
      stopped: false,
      code: "callback_not_ready",
    });
  });
  await new Promise((resolve) => callbackServer.listen(0, "127.0.0.1", resolve));
  const outputPort = callbackServer.address().port;
  const mock = createElectronMock(config);
  const host = createElectronHost({
    electron: mock.electron,
    config: { ...config, outputPort },
    callbackReadyTimeoutMs: 1000,
    callbackReadyRequestTimeoutMs: 200,
    callbackReadyPollMs: 20,
    readyTimeoutMs: 1000,
  });
  t.after(async () => {
    await host.stop().catch(() => {});
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });

  await assert.rejects(host.start(), { code: "output_callback_not_ready" });
  assert.equal(mock.window(), null);
  assert.equal(host.status().ready, false);
  assert.equal(host.status().listening, false);
  assert.equal(host.status().lastFailureCode, "output_callback_not_ready");
});

test("stopping the Host during callback readiness aborts the wait and cleans listeners", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const { config } = makeConfig(t);
  const callbackServer = http.createServer((_request, response) => {
    jsonReply(response, 503, { ok: false, callbackReady: false, hostGeneration: config.hostGeneration });
  });
  await new Promise((resolve) => callbackServer.listen(0, "127.0.0.1", resolve));
  const outputPort = callbackServer.address().port;
  const mock = createElectronMock(config);
  const host = createElectronHost({
    electron: mock.electron,
    config: { ...config, outputPort },
    callbackReadyTimeoutMs: 5000,
    callbackReadyRequestTimeoutMs: 200,
    callbackReadyPollMs: 20,
    readyTimeoutMs: 1000,
  });
  t.after(async () => {
    await host.stop().catch(() => {});
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });

  const startPromise = host.start();
  await waitFor(() => host.status().listening);
  const stopped = await host.stop();
  await assert.rejects(startPromise, { code: "host_stopped" });
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.listening, false);
  assert.equal(stopped.callbackReady, false);
  assert.equal(mock.window(), null);
});

test("Pepper plugin loss invalidates readiness even while the page still has postMessage", async (t) => {
  const failures = [];
  const runtime = await startRendererTestHost(t, { onFailure(event) { failures.push(event.code); } });
  assert.equal(runtime.host.status().ready, true);
  for (const type of ["GPU", "Utility", "Pepper Plugin Broker", "Unknown"]) {
    runtime.mock.app.emit("child-process-gone", {}, { type, reason: "crashed", name: TOKEN });
    assert.equal(runtime.host.status().ready, true);
  }
  runtime.mock.app.emit("child-process-gone", {}, { type: "Pepper Plugin", reason: "crashed", name: TOKEN, exitCode: -1 });
  assert.equal(runtime.host.status().ready, false);
  assert.equal(runtime.host.status().pluginFound, false);
  await runtime.host.stop();
  assert.deepEqual(failures, ["avsdk_plugin_crashed"]);
  assert.equal(runtime.mock.app.listenerCount("child-process-gone"), 0);
  assert.equal(runtime.mock.window().isDestroyed(), true);
});

test("renderer-gone capture sanitizes actual details once before cleanup and retains its first record", async (t) => {
  let pidReads = 0;
  const observations = [];
  const failures = [];
  let config;
  const runtime = await startRendererTestHost(t, {
    getOSProcessId() {
      pidReads += 1;
      return pidReads === 1 ? 0 : 43210;
    },
    onRendererGone(record) {
      observations.push(record);
      writeHostCrash(config.reportPath, record);
      throw new Error(`observer failure ${TOKEN}`);
    },
    onFailure(event) { failures.push(event.code); },
  });
  config = runtime.config;
  const readsAfterReady = pidReads;
  assert.ok(readsAfterReady > 1, "refresh the cached PID after the renderer begins loading");
  runtime.mock.window().webContents.getOSProcessId = () => {
    pidReads += 1;
    return 99999;
  };

  runtime.mock.window().webContents.emit("render-process-gone", { ignoredEventValue: TOKEN }, {
    reason: "crashed",
    exitCode: 3221225477,
    payload: TOKEN,
    path: runtime.config.reportPath,
  });
  const firstRecord = readCrashReport(runtime.config);
  const parsed = JSON.parse(firstRecord);
  assert.equal(parsed.schema, 1);
  assert.equal(Number.isSafeInteger(parsed.observedAtMs), true);
  assert.equal(parsed.hostGeneration, runtime.config.hostGeneration);
  assert.equal(parsed.targetPid, runtime.config.targetPid);
  assert.equal(parsed.rendererPid, 43210);
  assert.equal(parsed.reason, "crashed");
  assert.equal(parsed.exitCode, 3221225477);
  assert.equal(parsed.shutdownInProgress, false);
  assert.equal(parsed.ready, true);
  assert.equal(parsed.pluginFound, true);
  assert.equal(parsed.hasPostMessage, true);
  assert.equal(parsed.callbackReady, true);
  assert.deepEqual(Object.keys(parsed).sort(), [
    "callbackReady", "exitCode", "hasPostMessage", "hostGeneration", "observedAtMs", "pluginFound",
    "ready", "reason", "rendererPid", "schema", "shutdownInProgress", "targetPid",
  ]);
  assert.equal(firstRecord.includes(TOKEN), false);
  assert.equal(firstRecord.includes(runtime.config.reportPath), false);
  assert.equal(pidReads, readsAfterReady, "use the cached PID after the renderer is gone");
  assert.equal(observations.length, 1);
  assert.deepEqual(observations[0], parsed);

  runtime.mock.window().webContents.emit("render-process-gone", {}, {
    reason: "junk-reason",
    exitCode: "not-an-integer",
    payload: "must-not-replace-first-record",
  });
  await runtime.host.stop();
  assert.equal(observations.length, 1);
  assert.equal(readCrashReport(runtime.config), firstRecord, "duplicate notifications and normal stop preserve the first diagnostic");
  assert.equal(runtime.host.status().stopped, true, "an observer exception does not block cleanup");
  assert.equal(runtime.host.status().lastFailureCode, "renderer_crashed");
  assert.deepEqual(failures, ["renderer_crashed"]);
});

test("an unexpected clean renderer exit is unavailable as renderer_closed", async (t) => {
  const failures = [];
  let captured = null;
  let config;
  const runtime = await startRendererTestHost(t, {
    onRendererGone(record) {
      captured = record;
      writeHostCrash(config.reportPath, record);
    },
    onFailure(event) { failures.push(event.code); },
  });
  config = runtime.config;

  runtime.mock.window().webContents.emit("render-process-gone", {}, { reason: "clean-exit", exitCode: 0 });
  await runtime.host.stop();

  assert.ok(captured);
  assert.equal(captured.reason, "clean-exit");
  assert.equal(captured.exitCode, 0);
  assert.equal(captured.shutdownInProgress, false);
  assert.equal(runtime.host.status().ready, false);
  assert.equal(runtime.host.status().rendererGone, true);
  assert.equal(runtime.host.status().lastFailureCode, "renderer_closed");
  assert.deepEqual(failures, ["renderer_closed"]);
});

test("renderer-gone capture marks a renderer event that races with explicit Host shutdown", async (t) => {
  let config;
  let captured = null;
  const failures = [];
  const runtime = await startRendererTestHost(t, {
    onRendererGone(record) {
      captured = record;
      writeHostCrash(config.reportPath, record);
    },
    onFailure(event) { failures.push(event.code); },
  });
  config = runtime.config;

  const stopping = runtime.host.stop("output_forward_timeout");
  runtime.mock.window().webContents.emit("render-process-gone", {}, { reason: "clean-exit", exitCode: 0 });
  runtime.mock.window().emit("closed");
  await stopping;

  assert.ok(captured);
  assert.equal(captured.reason, "clean-exit");
  assert.equal(captured.exitCode, 0);
  assert.equal(captured.shutdownInProgress, true);
  assert.equal(JSON.parse(readCrashReport(runtime.config)).shutdownInProgress, true);
  assert.equal(runtime.host.status().stopped, true);
  assert.equal(runtime.host.status().lastFailureCode, "output_forward_timeout");
  assert.deepEqual(failures, []);
});
