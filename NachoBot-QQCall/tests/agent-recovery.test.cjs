"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");

function token() { return crypto.randomBytes(32).toString("base64url"); }

function waitFor(predicate, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = () => {
      if (predicate()) return resolve();
      if (Date.now() >= deadline) return reject(new Error("fixture condition timed out"));
      setTimeout(check, 5);
    };
    check();
  });
}

function makeRecoveryFixture(runtimeDir, { agentStatus, scopeDelayMs = 0, removeDelayMs = 0, closeFailureCount = 0, shortenRemoveTimeout = false } = {}) {
  const reportBase = path.join(runtimeDir, "report");
  const wrapperPath = path.join(runtimeDir, "mock-wrapper.node");
  const recoveryPath = path.join(runtimeDir, "recovery.cjs");
  const inspectorPath = path.join(runtimeDir, "inspector-stub.cjs");
  const wrapperBytes = Buffer.from("isolated recovery contract fixture");
  fs.writeFileSync(wrapperPath, wrapperBytes);
  const mockHash = crypto.createHash("sha256").update(wrapperBytes).digest("hex").toUpperCase();
  const fixtureKey = "nachobot.qqcall.test.inspector.fixture.v1";
  const fixtureState = { removeCalls: 0, removeArguments: [], closeCalls: 0, scopeDelayMs, removeDelayMs };
  const inspectorSource = `
"use strict";
const FIXTURE_KEY = Symbol.for(${JSON.stringify(fixtureKey)});
class Session {
  connect() {}
  disconnect() {}
  post(method, params, callback) {
    const fixture = globalThis[FIXTURE_KEY];
    let result = {};
    if (method === "Runtime.evaluate") {
      result = params.expression === "globalThis"
        ? { result: { type: "object", objectId: "global" } }
        : { result: { type: "function", objectId: "owned-attach" } };
    } else if (method === "Runtime.getProperties") {
      const table = {
        "owned-attach": { internalProperties: [{ name: "[[Scopes]]", value: { objectId: "scopes-attach" } }] },
        "scopes-attach": { result: [{ name: "0", value: { objectId: "scope-attach" } }] },
        "scope-attach": { result: [{ name: "agent", value: { type: "object", objectId: "owned-agent" } }] },
        "owned-agent": { result: [{ name: "detach", value: { type: "function", objectId: "owned-detach" } }] },
        "owned-detach": { internalProperties: [{ name: "[[Scopes]]", value: { objectId: "scopes-detach" } }] },
        "scopes-detach": { result: [{ name: "0", value: { objectId: "scope-detach" } }] },
        "scope-detach": { result: [
          { name: "listener", value: { type: "object", objectId: "owned-listener" } },
          { name: "avsdkService", value: { type: "object", objectId: "owned-service" } },
        ] },
      };
      result = table[params.objectId] || { result: [] };
    } else if (method === "Runtime.callFunctionOn" && params.objectId === "global") {
      const retained = globalThis[Symbol.for("nachobot.qqcall.recovery.v1")];
      retained.agent = { owned: true };
      retained.listener = fixture.listener;
      retained.avsdkService = fixture.service;
      result = { result: { value: true } };
    } else if (method === "Runtime.callFunctionOn" && params.objectId === "owned-service") {
      fixture.removeCalls += 1;
      fixture.removeArguments.push(params.arguments?.[0]?.objectId);
      fixture.service.removeKernelAVSDKListener(fixture.listener);
      result = { result: { value: true } };
    }
    const delay = method === "Runtime.callFunctionOn" && params.objectId === "owned-service"
      ? fixture.removeDelayMs
      : method === "Runtime.getProperties" ? fixture.scopeDelayMs : 0;
    setTimeout(() => callback(null, result), delay);
  }
}
module.exports = { Session };
`;
  fs.writeFileSync(inspectorPath, inspectorSource);
  const sourcePath = path.resolve(__dirname, "../agent/recover-owned-listener.cjs");
  const expectedHash = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";
  let recoverySource = fs.readFileSync(sourcePath, "utf8");
  recoverySource = recoverySource.replace(expectedHash, mockHash);
  recoverySource = recoverySource.replace('inspector = require("node:inspector");', `inspector = require(${JSON.stringify(inspectorPath)});`);
  if (shortenRemoveTimeout) recoverySource = recoverySource.replace("}, 5000);", "}, 25);");
  fs.writeFileSync(recoveryPath, recoverySource);
  const recovery = require(recoveryPath);
  const listener = { fixtureOwned: true };
  const service = { removeKernelAVSDKListener(value) { assert.strictEqual(value, listener); } };
  fixtureState.listener = listener;
  fixtureState.service = service;
  const originalConfig = {
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    wrapperModulePath: wrapperPath,
    reportPath: reportBase,
    pipeName: `\\\\.\\pipe\\nachobot-qqcall-${process.pid}-1123456789abcdef`,
    sessionName: "nt_1",
    connectionGeneration: 119,
    ipcToken: token(),
  };
  const ownedAgentStatus = agentStatus || {
    state: "attached_without_removal_handle",
    listenerRegistered: true,
    listenerIdAvailable: false,
    listenerRemovalMode: "returned-id",
    serviceAvailable: true,
    capabilities: { removeKernelAVSDKListener: true, removeHandleAvailable: false },
  };
  let closeFailuresLeft = closeFailureCount;
  const registry = {
    active: true,
    config: originalConfig,
    controller: {
      attach() {},
      status() { return { processPid: process.pid, connectionGeneration: originalConfig.connectionGeneration, agent: ownedAgentStatus }; },
      client: { async close() { fixtureState.closeCalls += 1; if (closeFailuresLeft-- > 0) throw new Error("private close detail"); } },
    },
  };
  globalThis[Symbol.for(fixtureKey)] = fixtureState;
  globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")] = registry;
  const recoveryConfig = { ...originalConfig, modulePath: recoveryPath, reportPath: `${reportBase}-recovery` };
  return { recovery, recoveryConfig, registry, fixtureState, listener, service, recoveryPath, inspectorPath, wrapperPath, fixtureKey };
}

function clearRecoveryFixture(fixture) {
  delete globalThis[Symbol.for(fixture.fixtureKey)];
  delete globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")];
  delete globalThis[Symbol.for("nachobot.qqcall.recovery.v1")];
  for (const file of [fixture.recoveryPath, fixture.inspectorPath, fixture.wrapperPath]) delete require.cache[file];
}

test("owned-listener recovery is pinned, retains only the matching run, and removes the exact listener once", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-recovery-test-"));
  const reportBase = path.join(runtimeDir, "report");
  const wrapperPath = path.join(runtimeDir, "mock-wrapper.node");
  const recoveryPath = path.join(runtimeDir, "recover-owned-listener.cjs");
  const expectedHash = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";
  const wrapperBytes = Buffer.from("isolated mock wrapper build");
  fs.writeFileSync(wrapperPath, wrapperBytes);
  const mockHash = crypto.createHash("sha256").update(wrapperBytes).digest("hex").toUpperCase();
  const recoverySource = fs.readFileSync(path.resolve(__dirname, "../agent/recover-owned-listener.cjs"), "utf8");
  assert.equal(recoverySource.split(expectedHash).length, 2, "production module must have one immutable wrapper pin");
  fs.writeFileSync(recoveryPath, recoverySource.replace(expectedHash, mockHash));
  const recovery = require(recoveryPath);
  const wrapperModule = { exports: {} };
  let registeredListener = null;
  const removedListeners = [];
  const service = {
    addKernelAVSDKListener(listener) { registeredListener = listener; return undefined; },
    removeKernelAVSDKListener(listener) { removedListeners.push(listener); },
    setActionFromAVSDK() {},
  };
  wrapperModule.exports.NodeIQQNTWrapperSession = {
    getNTWrapperSession() { return { getAVSDKService: () => service }; },
  };
  require.cache[wrapperPath] = wrapperModule;
  const originalConfig = {
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    wrapperModulePath: wrapperPath,
    reportPath: reportBase,
    pipeName: `\\\\.\\pipe\\nachobot-qqcall-${process.pid}-0123456789abcdef`,
    sessionName: "nt_1",
    connectionGeneration: 115,
    ipcToken: token(),
  };
  const agent = createKernelAgent({
    removalMode: "returned-id",
    connectionGeneration: originalConfig.connectionGeneration,
    wrapper: wrapperModule.exports,
  });
  let closeCalls = 0;
  const controller = {
    attach() { return agent.attach(); },
    status() { return { processPid: process.pid, connectionGeneration: originalConfig.connectionGeneration, agent: agent.status() }; },
    client: { async close() { closeCalls += 1; if (closeCalls === 1) throw new Error("private close detail"); } },
  };
  const registry = { active: true, config: originalConfig, controller };
  try {
    await agent.probe();
    const attached = await agent.attach();
    assert.equal(attached.code, "listener_id_unavailable");
    assert.equal(attached.status.listenerRegistered, true);
    assert.equal(attached.status.listenerIdAvailable, false);
    globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")] = registry;

    const recoveryConfig = { ...originalConfig, modulePath: recoveryPath, reportPath: `${originalConfig.reportPath}-recovery` };

    const foreignRun = await recovery.bootstrap({ ...recoveryConfig, connectionGeneration: 116 });
    assert.equal(foreignRun.lastFailureCode, "bootstrap_run_mismatch");
    assert.equal(globalThis[Symbol.for("nachobot.qqcall.recovery.v1")], undefined, "foreign run must be rejected before retaining references");
    assert.equal(fs.existsSync(`${recoveryConfig.reportPath}.recovery.json`), false, "invalid run must not leave a report artifact");

    const recoverySourceMissingInspector = recoverySource.replace(
      'inspector = require("node:inspector");',
      'throw Object.assign(new Error("sensitive module path"), { code: "MODULE_NOT_FOUND" });',
    );
    const missingInspectorPath = path.join(runtimeDir, "recover-without-inspector.cjs");
    fs.writeFileSync(missingInspectorPath, recoverySourceMissingInspector.replace(expectedHash, mockHash));
    const missingInspector = require(missingInspectorPath);
    const unavailable = await missingInspector.bootstrap({ ...recoveryConfig, modulePath: missingInspectorPath });
    assert.equal(unavailable.lastFailureCode, "inspector_module_unavailable");
    assert.equal(unavailable.inspectorAvailable, false);
    assert.equal(unavailable.inspectorRequireCode, "MODULE_NOT_FOUND");
    assert.equal(unavailable.nativeRemovalCallAttempted, false);
    assert.equal(unavailable.originalClientCloseAttempted, false);
    assert.equal(removedListeners.length, 0);
    assert.equal(closeCalls, 0);
    assert.equal(globalThis[Symbol.for("nachobot.qqcall.recovery.v1")].registry, registry, "the exact original run remains retained for retry");

    const result = await recovery.bootstrap(recoveryConfig);
    assert.equal(result.nativeRemovalCallAttempted, true);
    assert.equal(result.nativeRemovalCallReturned, true);
    assert.equal(result.vendorInternalRemovalVerified, false, "a no-throw JS API return does not prove internal vendor state");
    assert.equal(result.originalAgentStateSynchronized, false);
    assert.equal(result.originalClientCloseCompleted, false, "a close rejection remains observable and retryable");
    assert.equal(result.retainedOwnedReferences, true);
    assert.equal(removedListeners.length, 1);
    assert.strictEqual(removedListeners[0], registeredListener, "remove receives the exact original listener object");

    const retried = await recovery.bootstrap(recoveryConfig);
    assert.equal(retried.nativeRemovalCallReturned, true);
    assert.equal(retried.originalClientCloseCompleted, true);
    assert.equal(removedListeners.length, 1, "a retry after a successful removal never calls remove twice");
    assert.equal(closeCalls, 2, "a failed close is retried once");

    const retained = globalThis[Symbol.for("nachobot.qqcall.recovery.v1")];
    assert.strictEqual(retained.registry, globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")]);
    assert.strictEqual(retained.controller, controller);
    assert.strictEqual(retained.listener, registeredListener);
    assert.strictEqual(retained.avsdkService, service);
    assert.equal(controller.status().agent.listenerRegistered, true, "recovery deliberately does not falsify original Agent state");

    const report = JSON.parse(fs.readFileSync(`${recoveryConfig.reportPath}.recovery.json`, "utf8"));
    assert.equal(report.nativeRemovalCallReturned, true);
    assert.equal(report.vendorInternalRemovalVerified, false);
    assert.equal(JSON.stringify(report).includes("objectId"), false);
  } finally {
    delete globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")];
    delete globalThis[Symbol.for("nachobot.qqcall.recovery.v1")];
    delete require.cache[wrapperPath];
    delete require.cache[recoveryPath];
    delete require.cache[path.join(runtimeDir, "recover-without-inspector.cjs")];
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("owned-listener recovery retains only the exact registry and refuses ambiguous scope bindings", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-recovery-scope-test-"));
  const reportBase = path.join(runtimeDir, "report");
  const wrapperPath = path.join(runtimeDir, "mock-wrapper.node");
  const recoveryPath = path.join(runtimeDir, "recover-owned-listener.cjs");
  const expectedHash = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";
  const wrapperBytes = Buffer.from("isolated mock wrapper build for scope miss");
  fs.writeFileSync(wrapperPath, wrapperBytes);
  const mockHash = crypto.createHash("sha256").update(wrapperBytes).digest("hex").toUpperCase();
  const source = fs.readFileSync(path.resolve(__dirname, "../agent/recover-owned-listener.cjs"), "utf8");
  fs.writeFileSync(recoveryPath, source.replace(expectedHash, mockHash));
  const recovery = require(recoveryPath);
  const fakeRegistry = {
    active: true,
    config: {
      appPath: path.resolve(__dirname, ".."),
      wrapperModulePath: wrapperPath,
      reportPath: reportBase,
      pipeName: `\\\\.\\pipe\\nachobot-qqcall-${process.pid}-0123456789abcdef`,
      sessionName: "nt_1",
      connectionGeneration: 118,
      ipcToken: token(),
    },
    controller: {
      attach() { return undefined; },
      status() {
        return {
          processPid: process.pid,
          connectionGeneration: 118,
          agent: {
            state: "attached_without_removal_handle",
            listenerRegistered: true,
            listenerIdAvailable: false,
            listenerRemovalMode: "returned-id",
            serviceAvailable: true,
            capabilities: { removeKernelAVSDKListener: true, removeHandleAvailable: false },
          },
        };
      },
      client: { async close() {} },
    },
  };
  try {
    globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")] = fakeRegistry;
    const recoveryConfig = { ...fakeRegistry.config, modulePath: recoveryPath, reportPath: `${fakeRegistry.config.reportPath}-recovery` };
    const result = await recovery.bootstrap(recoveryConfig);
    assert.equal(result.lastFailureCode, "owned_agent_scope_missing_or_ambiguous");
    assert.equal(result.retainedOwnedReferences, true, "validated owned controller is retained before scope traversal");
    assert.strictEqual(globalThis[Symbol.for("nachobot.qqcall.recovery.v1")].registry, fakeRegistry);
    assert.equal(result.nativeRemovalCallAttempted, false);
  } finally {
    delete globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")];
    delete globalThis[Symbol.for("nachobot.qqcall.recovery.v1")];
    delete require.cache[recoveryPath];
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("recovery rejects a normal object-contract Agent before retain, remove, or close", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-recovery-object-contract-"));
  const fixture = makeRecoveryFixture(runtimeDir, {
    agentStatus: {
      state: "attached",
      listenerRegistered: true,
      listenerIdAvailable: false,
      listenerRemovalMode: "listener-object",
      serviceAvailable: true,
      capabilities: { removeKernelAVSDKListener: true, removeHandleAvailable: true },
    },
  });
  try {
    const result = await fixture.recovery.bootstrap(fixture.recoveryConfig);
    assert.equal(result.lastFailureCode, "owned_listener_not_legacy_contract");
    assert.equal(result.retainedOwnedReferences, false);
    assert.equal(fixture.fixtureState.removeCalls, 0);
    assert.equal(fixture.fixtureState.closeCalls, 0);
    assert.equal(globalThis[Symbol.for("nachobot.qqcall.recovery.v1")], undefined);
  } finally {
    clearRecoveryFixture(fixture);
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("concurrent recovery calls reserve one scope walk and one native remove", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-recovery-concurrent-"));
  const fixture = makeRecoveryFixture(runtimeDir, { scopeDelayMs: 20, removeDelayMs: 20 });
  try {
    const firstPromise = fixture.recovery.bootstrap(fixture.recoveryConfig);
    const secondPromise = fixture.recovery.bootstrap(fixture.recoveryConfig);
    const [first, second] = await Promise.all([firstPromise, secondPromise]);
    assert.equal(first.nativeRemovalCallReturned, true);
    assert.equal(second.lastFailureCode, "recovery_in_progress");
    assert.equal(fixture.fixtureState.removeCalls, 1);
    assert.deepEqual(fixture.fixtureState.removeArguments, ["owned-listener"]);
    assert.equal(fixture.fixtureState.closeCalls, 1);
  } finally {
    clearRecoveryFixture(fixture);
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("a timed out native removal becomes outcome-unknown and is never retried", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-recovery-timeout-"));
  const fixture = makeRecoveryFixture(runtimeDir, { removeDelayMs: 90, shortenRemoveTimeout: true });
  try {
    const first = await fixture.recovery.bootstrap(fixture.recoveryConfig);
    assert.equal(first.lastFailureCode, "inspector_request_timeout");
    assert.equal(first.nativeRemovalCallAttempted, true);
    assert.equal(first.nativeRemovalCallReturned, false);
    assert.equal(fixture.fixtureState.removeCalls, 1);

    await new Promise((resolve) => setTimeout(resolve, 120));
    const retried = await fixture.recovery.bootstrap(fixture.recoveryConfig);
    assert.equal(retried.lastFailureCode, "native_removal_outcome_unknown");
    assert.equal(retried.nativeRemovalCallAttempted, true);
    assert.equal(retried.nativeRemovalCallReturned, false);
    assert.equal(fixture.fixtureState.removeCalls, 1, "an uncertain API call must not be repeated");
    assert.equal(fixture.fixtureState.closeCalls, 0, "client stays open until a normal API return is observed");
  } finally {
    clearRecoveryFixture(fixture);
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});
