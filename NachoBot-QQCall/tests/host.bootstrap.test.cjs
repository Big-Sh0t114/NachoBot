"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");
const { buildHostConfigs, writePrivateJson } = require("../host/config.cjs");
const { bootstrap } = require("../host/bootstrap.cjs");

const pluginFilename = process.platform === "win32" ? "AVSDKPlugin.dll" : process.platform === "linux" ? "libAVSDKPlugin.so" : null;
const TOKEN = Buffer.alloc(32, 0x67).toString("base64url");
const STATE_KEY = Symbol.for("nachobot.qqcall.avsdk-host.v1");

function fixture({ listenPort = 6111, outputPort = 6110 } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-host-bootstrap-test-"));
  const appPath = path.join(root, "qq-app");
  const profilePath = path.join(root, "host-profile");
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(appPath, "avsdk", pluginFilename || "plugin.stub"), "mock plugin");
  fs.mkdirSync(profilePath, { recursive: true });
  const reportPath = path.join(root, "reports", "host");
  const config = buildHostConfigs({
    targetPid: process.pid,
    appPath,
    reportPath,
    profilePath,
    listenPort,
    outputPort,
    hostGeneration: 29,
    token: TOKEN,
    pipeNonce: "abcdef0123456789abcdef0123456789",
  });
  writePrivateJson(`${reportPath}.host.config.json`, config.hostConfig);
  return { root, ...config };
}

function readReport(selected) {
  return JSON.parse(fs.readFileSync(`${selected.nativeConfig.reportPath}.host.status.json`, "utf8"));
}

function readCrashReport(selected) {
  return JSON.parse(fs.readFileSync(`${selected.nativeConfig.reportPath}.host.crash.json`, "utf8"));
}

function assertSanitizedReport(selected, report) {
  assert.deepEqual(Object.keys(report).sort(), [
    "hostGeneration", "lastFailureCode", "listening", "phase", "ready", "stopped", "targetPid",
  ]);
  assert.equal(JSON.stringify(report).includes(TOKEN), false);
  assert.equal(JSON.stringify(report).includes(selected.root), false);
}

function assertSanitizedCrashReport(selected, report) {
  assert.deepEqual(Object.keys(report).sort(), [
    "callbackReady", "exitCode", "hasPostMessage", "hostGeneration", "observedAtMs", "pluginFound",
    "ready", "reason", "rendererPid", "schema", "shutdownInProgress", "targetPid",
  ]);
  assert.equal(JSON.stringify(report).includes(TOKEN), false);
  assert.equal(JSON.stringify(report).includes(selected.root), false);
}

async function startCallbackServer(port, hostGeneration) {
  const server = http.createServer((request, response) => {
    const encoded = (status, body) => {
      const bytes = Buffer.from(JSON.stringify(body));
      response.writeHead(status, { "content-type": "application/json", "content-length": bytes.length });
      response.end(bytes);
    };
    if (request.method === "GET" && request.url === "/v1/callback-ready") {
      if (request.headers.authorization !== `Bearer ${TOKEN}`) return encoded(401, { ok: false, code: "unauthorized" });
      if (request.headers["x-qqcall-host-generation"] !== String(hostGeneration)) return encoded(409, { ok: false, code: "generation_mismatch" });
      return encoded(200, {
        ok: true,
        callbackReady: true,
        hostGeneration,
        started: true,
        subscriberReady: true,
        stopping: false,
        stopped: false,
      });
    }
    if (request.method === "POST" && request.url === "/v1/avsdk/output") {
      request.resume();
      return request.once("end", () => encoded(503, { ok: false, code: "test_output_rejected" }));
    }
    request.resume();
    return request.once("end", () => encoded(404, { ok: false, code: "not_found" }));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return server;
}

function createReadyElectron() {
  const app = new EventEmitter();
  const switches = new Map();
  let profilePath = "";
  app.commandLine = {
    hasSwitch: (name) => switches.has(name),
    getSwitchValue: (name) => switches.get(name) || "",
    appendSwitch: (name, value) => switches.set(name, value ?? ""),
  };
  app.isReady = () => false;
  app.setPath = (name, value) => { if (name === "userData") profilePath = value; };
  app.getPath = (name) => name === "userData" ? profilePath : "";
  app.whenReady = () => Promise.resolve();
  const ipcMain = new EventEmitter();
  const hostHtml = path.resolve(__dirname, "../host/host.html");
  const hostUrl = pathToFileURL(hostHtml).href;
  let windowRef = null;
  class BrowserWindow extends EventEmitter {
    constructor() {
      super();
      this.destroyed = false;
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = { url: hostUrl };
      this.webContents.getOSProcessId = () => 92345;
      this.webContents.setWindowOpenHandler = () => {};
      this.webContents.getURL = () => hostUrl;
      this.webContents.executeJavaScript = async () => ({ posted: true });
      windowRef = this;
    }
    async loadFile(filename) {
      assert.equal(filename, hostHtml);
      ipcMain.emit("nachobot-qqcall-host:ready", {
        sender: this.webContents,
        senderFrame: this.webContents.mainFrame,
      }, { pluginFound: true, hasPostMessage: true });
    }
    isDestroyed() { return this.destroyed; }
    destroy() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  return { app, ipcMain, BrowserWindow, window: () => windowRef };
}

async function reservePort(excludedPort = null) {
  while (true) {
    const server = http.createServer();
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = server.address().port;
    await new Promise((resolve) => server.close(resolve));
    if (port !== excludedPort && port !== 6110) return port;
  }
}

async function readHostHealth(port, token) {
  return new Promise((resolve, reject) => {
    const request = http.get({
      hostname: "127.0.0.1",
      port,
      path: "/healthz",
      headers: { authorization: `Bearer ${token}` },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => {
        try {
          resolve({ status: response.statusCode, json: JSON.parse(Buffer.concat(chunks).toString("utf8")) });
        } catch (error) { reject(error); }
      });
    });
    request.once("error", reject);
  });
}

test("constructor failure keeps its typed code in a sanitized Host status report", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const selected = fixture();
  t.after(() => fs.rmSync(selected.root, { recursive: true, force: true }));
  delete globalThis[STATE_KEY];

  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "electron") return "C:\\fake\\electron.exe";
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    await assert.rejects(bootstrap(selected.nativeConfig), { code: "electron_runtime_unavailable" });
  } finally {
    Module._load = originalLoad;
  }

  assert.equal(globalThis[STATE_KEY], undefined);
  const report = readReport(selected);
  assert.deepEqual(report, {
    phase: "failed",
    hostGeneration: 29,
    targetPid: process.pid,
    lastFailureCode: "electron_runtime_unavailable",
    ready: false,
    listening: false,
    stopped: false,
  });
  assertSanitizedReport(selected, report);
});

test("a status report write failure does not retain bootstrap ownership", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const selected = fixture();
  t.after(() => fs.rmSync(selected.root, { recursive: true, force: true }));
  delete globalThis[STATE_KEY];
  const statusPath = `${selected.nativeConfig.reportPath}.host.status.json`;
  fs.mkdirSync(statusPath);

  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "electron") return "C:\\fake\\electron.exe";
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    await assert.rejects(bootstrap(selected.nativeConfig), { code: "electron_runtime_unavailable" });
  } finally {
    Module._load = originalLoad;
  }

  assert.equal(globalThis[STATE_KEY], undefined);
  assert.equal(fs.statSync(statusPath).isDirectory(), true);
});

test("bootstrap writes sanitized ready, output-failure, renderer-failure and stopped lifecycle reports", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const listenPort = await reservePort();
  const outputPort = await reservePort(listenPort);
  const selected = fixture({ listenPort, outputPort });
  t.after(() => fs.rmSync(selected.root, { recursive: true, force: true }));
  const callbackServer = await startCallbackServer(outputPort, selected.hostConfig.hostGeneration);
  t.after(async () => {
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });
  delete globalThis[STATE_KEY];
  const electron = createReadyElectron();
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "electron") return electron;
    return originalLoad.call(this, request, parent, isMain);
  };

  let controller;
  try {
    controller = await bootstrap(selected.nativeConfig);
    const ready = readReport(selected);
    assert.deepEqual(ready, {
      phase: "ready",
      hostGeneration: 29,
      targetPid: process.pid,
      lastFailureCode: null,
      ready: true,
      listening: true,
      stopped: false,
    });
    assertSanitizedReport(selected, ready);

    const hostHealth = await readHostHealth(selected.hostConfig.listenPort, TOKEN);
    assert.equal(hostHealth.status, 200);
    assert.equal(hostHealth.json.ok, true);
    assert.equal(hostHealth.json.status.hostGeneration, selected.hostConfig.hostGeneration);
    assert.equal(hostHealth.json.status.targetPid, selected.hostConfig.targetPid,
      "Electron Host health must carry its immutable, validated config PID through transport");

    electron.ipcMain.emit("nachobot-qqcall-host:output", {
      sender: electron.window().webContents,
      senderFrame: electron.window().webContents.mainFrame,
    }, { command: 20006, id: 0, value: ["not-logged"] });
    for (let attempt = 0; attempt < 40 && readReport(selected).phase !== "output_failure"; attempt += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    const outputFailure = readReport(selected);
    assert.deepEqual(outputFailure, {
      phase: "output_failure",
      hostGeneration: 29,
      targetPid: process.pid,
      lastFailureCode: "output_forward_failed",
      ready: false,
      listening: true,
      stopped: false,
    });
    assertSanitizedReport(selected, outputFailure);

    electron.window().webContents.emit("render-process-gone", {}, { reason: "crashed", exitCode: 3221225477, privatePayload: TOKEN });
    const crash = readCrashReport(selected);
    assert.equal(crash.schema, 1);
    assert.equal(Number.isSafeInteger(crash.observedAtMs), true);
    assert.equal(crash.hostGeneration, 29);
    assert.equal(crash.targetPid, process.pid);
    assert.equal(crash.rendererPid, 92345);
    assert.equal(crash.reason, "crashed");
    assert.equal(crash.exitCode, 3221225477);
    assert.equal(crash.shutdownInProgress, false);
    assert.equal(crash.callbackReady, true);
    assertSanitizedCrashReport(selected, crash);
    await controller.stop();
    const stopped = readReport(selected);
    assert.deepEqual(stopped, {
      phase: "stopped",
      hostGeneration: 29,
      targetPid: process.pid,
      lastFailureCode: "renderer_crashed",
      ready: false,
      listening: false,
      stopped: true,
    });
    assertSanitizedReport(selected, stopped);
  } finally {
    Module._load = originalLoad;
    if (controller) await controller.stop().catch(() => {});
    delete globalThis[STATE_KEY];
  }
});

test("Electron main entry writes ready and stopped lifecycle reports", async (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const listenPort = await reservePort();
  const outputPort = await reservePort(listenPort);
  const selected = fixture({ listenPort, outputPort });
  t.after(() => fs.rmSync(selected.root, { recursive: true, force: true }));
  const callbackServer = await startCallbackServer(outputPort, selected.hostConfig.hostGeneration);
  t.after(async () => {
    const closing = new Promise((resolve) => callbackServer.close(resolve));
    callbackServer.closeAllConnections?.();
    await closing;
  });
  const { main } = require("../host/electron-main.cjs");
  const electron = createReadyElectron();
  const runtime = main(electron, { QQCALL_HOST_CONFIG: `${selected.nativeConfig.reportPath}.host.config.json` });
  assert.ok(runtime);

  try {
    for (let attempt = 0; attempt < 20 && !runtime.status().ready; attempt += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    const ready = readReport(selected);
    assert.equal(ready.phase, "ready");
    assert.equal(ready.ready, true);
    assert.equal(ready.listening, true);
    assertSanitizedReport(selected, ready);

    electron.window().webContents.emit("render-process-gone", {}, { reason: "abnormal-exit", exitCode: -7 });
    const crash = readCrashReport(selected);
    assert.equal(crash.hostGeneration, 29);
    assert.equal(crash.targetPid, process.pid);
    assert.equal(crash.rendererPid, 92345);
    assert.equal(crash.reason, "abnormal-exit");
    assert.equal(crash.exitCode, -7);
    assert.equal(crash.shutdownInProgress, false);
    assertSanitizedCrashReport(selected, crash);

    await runtime.stop();
    const stopped = readReport(selected);
    assert.equal(stopped.phase, "stopped");
    assert.equal(stopped.ready, false);
    assert.equal(stopped.listening, false);
    assert.equal(stopped.stopped, true);
    assertSanitizedReport(selected, stopped);
  } finally {
    await runtime.stop().catch(() => {});
  }
});
