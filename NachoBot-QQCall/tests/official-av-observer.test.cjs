"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { buildNativeConditionalExpression, buildNativeTargetExpression } = require("../agent/native-call-observer.cjs");
const {
  MAX_EVENT_BYTES,
  MAX_REPORT_BYTES,
  createOfficialAVObserver,
  exactAVRoute,
  isObserverReport,
} = require("../agent/official-av-observer.cjs");

const AV_URL = "app://./renderer/index.html?webcontentsid=5&processGroupNS=createBlankWindow1#/av";

test("official rejection observations preserve SDK calls and expose only verified reason enums", async (t) => {
  const runtime = makeRuntime();
  t.after(() => runtime.observer.stop());
  await runtime.observer.start();
  for (const reason of [1, 3, 777777]) {
    runtime.embed.postMessage(vm.runInContext(`({cmd:9,id:"private-request",param:[1,"private-peer-uid",${reason}]})`,runtime.context));
  }
  runtime.embed.postMessage(vm.runInContext(`({cmd:10,id:"private-close",param:[1,"private-peer-uid",0]})`,runtime.context));
  runtime.context.__lengthReads=0;
  runtime.embed.postMessage(vm.runInContext(`({cmd:9,param:new Proxy([1,"private-peer-uid",3],{
    get(target,key,receiver){if(key==="length"){__lengthReads+=1;throw new Error("must not read length");}
      return Reflect.get(target,key,receiver);}})})`,runtime.context));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report),true);
  assert.equal(runtime.embed.calls.length,5,"every original SDK call is forwarded exactly once");
  assert.deepEqual(report.events.map(x=>x.command),[9,9,9,10,9]);
  assert.equal(runtime.context.__lengthReads,0);
  assert.deepEqual(report.events[0].privateReject,{sceneId:1,rejectReason:1});
  assert.deepEqual(report.events[1].privateReject,{sceneId:1,rejectReason:3});
  assert.equal(report.events[2].privateReject,undefined);
  for (const secret of ["private-peer-uid","private-request","private-close","777777"]) {
    assert.equal(JSON.stringify(report).includes(secret),false);
  }
  const invalid = structuredClone(report);
  invalid.events[0].privateReject.rejectReason=777777;
  assert.equal(isObserverReport(invalid),false);
});

test("incoming-only observation captures typed Reject ACKs without SDK interception or raw error text", async (t) => {
  const runtime = makeRuntime({ incomingOnly: true });
  t.after(() => runtime.observer.stop());
  Object.defineProperty(runtime.embed, "postMessage", { configurable: false, writable: false, value: runtime.original });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  await runtime.observer.start();
  for (const result of [0, 17, 0.5, true]) runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({ cmd: 9, id: "private-ack-id", param: [result, "sensitive-native-error"] }),
  }));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, 4);
  assert.deepEqual(report.events.slice(0, 2).map(event => event.rejectAck), [{ returnCode: 0 }, { returnCode: 17 }]);
  assert.ok(report.events.every(event => event.direction === "incoming" && event.command === 9));
  assert.equal(report.events[2].rejectAck, undefined);
  assert.equal(report.events[3].rejectAck, undefined);
  assert.equal(runtime.embed.calls.length, 0);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  assert.equal(JSON.stringify(report).includes("sensitive-native-error"), false);
  assert.equal(JSON.stringify(report).includes("private-ack-id"), false);
  for (const mutate of [
    event => { event.rejectAck.rawError = "forbidden"; },
    event => { event.rejectAck.returnCode = true; },
    event => { event.rejectAck.returnCode = 2147483648; },
    event => { event.direction = "outgoing"; },
    event => { event.command = 20001; },
  ]) {
    const invalid = structuredClone(report);
    mutate(invalid.events[0]);
    assert.equal(isObserverReport(invalid), false);
  }
  let reads = 0;
  const accessor = structuredClone(report);
  Object.defineProperty(accessor.events[0].rejectAck, "returnCode", { enumerable: true, get() { reads += 1; return 0; } });
  assert.equal(isObserverReport(accessor), false);
  assert.equal(reads, 0);
});

function makeRuntime({ url = AV_URL, embedCount = 1, execute = null, maxLeaseMs = 10000, windowCount = 1,
  probeTimeoutMs, observerNow, setTimeoutFn = setTimeout, clearTimeoutFn = clearTimeout, incomingOnly = false,
  nativeConditional = false, debuggerCommand = null, debuggerInitiallyAttached = false, mainFrameLoading = false,
  runId = "0123456789abcdef0123456789abcdef" } = {}) {
  const clock = { now: Date.now() };
  const pageTimers = [];
  const sentinel = Object.freeze({ ok: true });
  const runtimes = [];
  for (let ordinal = 0; ordinal < windowCount; ordinal += 1) {
    let currentEmbedCount = embedCount;
    const listeners = new Map();
    const embed = {
      calls: [],
      _listeners: new Map(),
      addEventListener(name, callback) {
        const values = this._listeners.get(name) || new Set();
        values.add(callback);
        this._listeners.set(name, values);
      },
      removeEventListener(name, callback) { this._listeners.get(name)?.delete(callback); },
      dispatch(name, event) { for (const callback of this._listeners.get(name) || []) callback.call(this, event); },
    };
    const original = function (...args) {
      assert.equal(this, embed, "postMessage preserves its receiver");
      this.calls.push(args);
      if (args[0] === "throw-sentinel") throw sentinel;
      return sentinel;
    };
    Object.defineProperty(embed, "postMessage", { configurable: !nativeConditional, enumerable: true,
      writable: !nativeConditional, value: original });
    const context = vm.createContext({
      document: { querySelectorAll(selector) {
        assert.equal(selector, 'embed[type="application/x-ppapi-avSDK"]');
        return Array.from({ length: currentEmbedCount }, () => embed);
      } },
      Date: { now: () => clock.now },
      setTimeout(callback, delay) {
        const timer = { callback, delay, cancelled: false };
        pageTimers.push(timer);
        return timer;
      },
      clearTimeout(timer) { if (timer) timer.cancelled = true; },
    });
    context.window = context;
    context.addEventListener = (name, callback) => {
      const values = listeners.get(name) || new Set();
      values.add(callback);
      listeners.set(name, values);
    };
    context.removeEventListener = (name, callback) => listeners.get(name)?.delete(callback);
    const webContents = new EventEmitter();
    webContents.destroyed = false;
    webContents.url = url;
    webContents.isDestroyed = () => webContents.destroyed;
    let currentMainFrameLoading = mainFrameLoading;
    webContents.isLoadingMainFrame = () => currentMainFrameLoading;
    webContents.getURL = () => webContents.url;
    webContents.executeCount = 0;
    webContents.executeJavaScript = (script) => {
      webContents.executeCount += 1;
      if (execute) return execute(script, context, embed);
      const result = vm.runInContext(script, context);
      return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
    };
    const debuggerApi = new EventEmitter();
    debuggerApi.attached = debuggerInitiallyAttached;
    debuggerApi.commands = [];
    debuggerApi.detachCalls = 0;
    debuggerApi.isAttached = () => debuggerApi.attached;
    debuggerApi.attach = () => { debuggerApi.attached = true; };
    debuggerApi.detach = () => {
      debuggerApi.detachCalls += 1;
      debuggerApi.attached = false;
      debuggerApi.emit("detach", {}, "target_closed");
    };
    debuggerApi.sendCommand = (method, params) => {
      debuggerApi.commands.push({ method, params });
      if (debuggerCommand) return debuggerCommand(method, params, debuggerApi, context);
      if (method === "Runtime.evaluate") return Promise.resolve({ result: { type: "function", objectId: `owned-${ordinal}` } });
      if (method === "Debugger.setBreakpointOnFunctionCall") return Promise.resolve({ breakpointId: `breakpoint-${ordinal}` });
      return Promise.resolve({});
    };
    webContents.debugger = debuggerApi;
    const window = new EventEmitter();
    window.destroyed = false;
    window.webContents = webContents;
    window.isDestroyed = () => window.destroyed;
    runtimes.push({ window, webContents, embed, original, context, listeners, debuggerApi,
      setEmbedCount(count) { currentEmbedCount = count; },
      setMainFrameLoading(loading) { currentMainFrameLoading = loading; } });
  }
  const app = new EventEmitter();
  const BrowserWindow = Object.assign(function BrowserWindow() {}, { getAllWindows: () => runtimes.map((entry) => entry.window) });
  const electron = { app, BrowserWindow };
  const requireFunction = (name) => name === "electron" ? electron : (() => { throw new Error("unexpected module"); })();
  requireFunction.resolve = (name) => name === "electron" ? "electron" : "not-electron";
  const observer = createOfficialAVObserver({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction,
    now: observerNow || (() => clock.now),
    setTimeoutFn,
    clearTimeoutFn,
    maxLeaseMs,
    incomingOnly,
    nativeConditional,
    runId,
    ...(probeTimeoutMs === undefined ? {} : { probeTimeoutMs }),
  });
  return { ...runtimes[0], observer, app, BrowserWindow, sentinel, clock, pageTimers, windows: runtimes };
}

function pageState(context) {
  for (const symbol of Object.getOwnPropertySymbols(context)) {
    const value = context[symbol];
    if (value?.schema === "qqcall.official.av.observer.page.v1") return value;
  }
  return null;
}

function runRendererFixture(script, context) {
  const result = vm.runInContext(script, context);
  return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
}

async function waitForWindowStatus(observer, statuses, timeoutMs = 1500) {
  const accepted = new Set(Array.isArray(statuses) ? statuses : [statuses]);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const window = observer.status.windows[0];
    if (window && accepted.has(window.status)) return window;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return observer.status.windows[0];
}

test("native stop tears down its debugger while a renderer drain remains held", async () => {
  let releaseDrain;
  const runtime = makeRuntime({ nativeConditional: true, probeTimeoutMs: 50, observerNow: Date.now,
    execute(script, context) {
      if (script.includes("function rendererDrain")) {
        return new Promise((resolve) => { releaseDrain = () => resolve({}); });
      }
      return runRendererFixture(script, context);
    },
  });
  assert.equal((await runtime.observer.start()).windows[0].status, "armed_native_conditional");
  const heldDrain = runtime.observer.drain();
  assert.equal(typeof releaseDrain, "function");
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false, "held renderer work is still reported incomplete");
  assert.equal(runtime.debuggerApi.attached, false);
  assert.equal(runtime.debuggerApi.detachCalls, 1);
  assert.equal(runtime.debuggerApi.commands.some((entry) => entry.method === "Debugger.removeBreakpoint"), true);
  const frozen = JSON.stringify(stopped);
  releaseDrain();
  await heldDrain;
  assert.equal(JSON.stringify(runtime.observer.status), frozen);
});

test("native stop tears down its debugger while navigation renderer cleanup is held", async () => {
  let releaseCleanup;
  const runtime = makeRuntime({ nativeConditional: true, probeTimeoutMs: 50, observerNow: Date.now,
    execute(script, context) {
      if (script.includes("function rendererCleanup")) {
        return new Promise((resolve) => { releaseCleanup = () => resolve({}); });
      }
      return runRendererFixture(script, context);
    },
  });
  await runtime.observer.start();
  runtime.webContents.emit("did-start-navigation", {}, AV_URL, false, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof releaseCleanup, "function");
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(runtime.debuggerApi.attached, false);
  assert.equal(runtime.debuggerApi.detachCalls, 1);
  releaseCleanup();
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(runtime.observer.status, stopped);
});

test("native cleanup uncertainty survives repeated no-state cleanup and a new document", async () => {
  const runtime = makeRuntime({ nativeConditional: true, observerNow: Date.now,
    debuggerCommand(method) {
      if (method === "Debugger.setSkipAllPauses" || method === "Debugger.disable") {
        return Promise.reject(new Error("native-cleanup-test-only"));
      }
      return Promise.resolve({});
    },
  });
  assert.equal((await runtime.observer.start()).windows[0].cleanupComplete, false);
  runtime.webContents.emit("did-start-navigation", {}, AV_URL, false, true);
  runtime.webContents.emit("dom-ready");
  await new Promise((resolve) => setImmediate(resolve));
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.windows[0].cleanupComplete, false);
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(stopped.incomplete, true);
});

test("native counters and saturation survive repeated same-epoch no-state cleanup", async () => {
  const runtime = makeRuntime({ nativeConditional: true });
  await runtime.observer.start();
  const state = pageState(runtime.context);
  state.nativeCallAttempts = 1000000;
  state.nativeCallAttemptsSaturated = true;
  assert.equal((await runtime.observer.drain()).windows[0].nativeCallAttempts, 1000000);
  runtime.debuggerApi.emit("message", {}, "Debugger.paused", {});
  await new Promise((resolve) => setImmediate(resolve));
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.windows[0].nativeCallAttempts, 1000000);
  assert.equal(stopped.windows[0].nativeCallAttemptsSaturated, true);
  assert.equal(stopped.cleanupComplete, true);
});

test("old native cleanup counters cannot populate a new document epoch", async () => {
  let releaseCleanup;
  let holdCleanup = true;
  const runtime = makeRuntime({ nativeConditional: true, observerNow: Date.now,
    execute(script, context) {
      if (holdCleanup && script.includes("function rendererCleanup")) {
        const response = JSON.parse(JSON.stringify(vm.runInContext(script, context)));
        response.nativeCallAttempts = 1000000;
        response.nativeCallAttemptsSaturated = true;
        return new Promise((resolve) => { releaseCleanup = () => resolve(response); });
      }
      return runRendererFixture(script, context);
    },
  });
  await runtime.observer.start();
  runtime.webContents.emit("did-start-navigation", {}, AV_URL, false, true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof releaseCleanup, "function");
  holdCleanup = false;
  releaseCleanup();
  await new Promise((resolve) => setImmediate(resolve));
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.windows[0].documentEpoch, 2);
  assert.equal(stopped.windows[0].nativeCallAttempts, 0);
  assert.equal(stopped.windows[0].nativeCallAttemptsSaturated, false);
});

test("native stop detaches its owned debugger when breakpoint setup finishes too late", async () => {
  let releaseBreakpoint;
  const runtime = makeRuntime({ nativeConditional: true, probeTimeoutMs: 50, observerNow: Date.now,
    debuggerCommand(method) {
      if (method === "Debugger.setBreakpointOnFunctionCall") {
        return new Promise((resolve) => { releaseBreakpoint = () => resolve({ breakpointId: "late-owned-breakpoint" }); });
      }
      return method === "Runtime.evaluate" ? { result: { type: "function", objectId: "owned-object" } } : {};
    },
  });
  const pendingStart = runtime.observer.start();
  while (!releaseBreakpoint) await new Promise((resolve) => setImmediate(resolve));
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false, "a pending setup result cannot prove cleanup");
  assert.equal(runtime.debuggerApi.attached, false);
  assert.equal(runtime.debuggerApi.detachCalls, 1);
  const frozen = JSON.stringify(stopped);
  const commandsAtStop = runtime.debuggerApi.commands.length;
  releaseBreakpoint();
  await pendingStart;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runtime.debuggerApi.commands.length, commandsAtStop);
  assert.equal(JSON.stringify(runtime.observer.status), frozen);
});

function makeInstallStageFailure(stage, { failPartialCleanup = false, afterDefineMutation = false, listenerRegistersBeforeThrow = false,
  timerSchedulesBeforeThrow = false } = {}) {
  const sentinel = `raw-install-error-${stage}`;
  let setupFault = false;
  let installCalls = 0;
  const runtime = makeRuntime({ execute(script, context, embed) {
    if (script.includes("function rendererInstall")) {
      installCalls += 1;
    }
    if (script.includes("function rendererInstall") && !setupFault) {
      setupFault = true;
      if (stage === "install_embed_method_failed" || stage === "install_state_publish_failed" || failPartialCleanup) {
        const globalTarget = vm.runInContext("globalThis", context);
        let setupDefineFailureUsed = false;
        context.__observerTestDefine = vm.runInContext("Object.defineProperty", context);
        context.__observerTestAfterDefineMutation = afterDefineMutation;
        context.__observerTestDefineFault = (target, key, descriptor) => {
          if (!setupDefineFailureUsed && stage === "install_embed_method_failed" && target === embed && key === "postMessage") {
            setupDefineFailureUsed = true;
            return true;
          }
          if (!setupDefineFailureUsed && stage === "install_state_publish_failed" && target === globalTarget && typeof key === "symbol" &&
              Symbol.keyFor(key)?.startsWith("qqcall.official.av.observer.") && !Symbol.keyFor(key)?.includes(".cancelled.")) {
            setupDefineFailureUsed = true;
            return true;
          }
          if (failPartialCleanup && target === embed && key === "postMessage" && descriptor?.value === runtime.original) return true;
          return false;
        };
        vm.runInContext(`Object.defineProperty = function (target, key, descriptor) {
          if (globalThis.__observerTestDefineFault(target, key, descriptor)) {
            if (globalThis.__observerTestAfterDefineMutation) {
              Reflect.apply(globalThis.__observerTestDefine, Object, [target, key, descriptor]);
            }
            throw new Error(${JSON.stringify(sentinel)});
          }
          return Reflect.apply(globalThis.__observerTestDefine, Object, [target, key, descriptor]);
        }`, context);
      } else if (stage === "install_embed_listener_failed") {
        if (listenerRegistersBeforeThrow) {
          const originalAdd = embed.addEventListener;
          embed.addEventListener = function (...args) {
            Reflect.apply(originalAdd, this, args);
            throw new Error(sentinel);
          };
        } else embed.addEventListener = () => { throw new Error(sentinel); };
      } else if (stage === "install_pagehide_listener_failed") {
        context.addEventListener = () => { throw new Error(sentinel); };
      } else if (stage === "install_lease_timer_failed") {
        const originalSetTimeout = context.setTimeout;
        let failed = false;
        context.__observerTestScheduledTimerHandles = [];
        context.setTimeout = (...args) => {
          if (!failed) {
            failed = true;
            if (timerSchedulesBeforeThrow) {
              const handle = Reflect.apply(originalSetTimeout, context, args);
              context.__observerTestScheduledTimerHandles.push(handle);
            }
            throw new Error(sentinel);
          }
          return originalSetTimeout(...args);
        };
      }
      if (failPartialCleanup) embed.removeEventListener = () => { throw new Error(sentinel); };
    }
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  return { ...runtime, installSentinel: sentinel, installCalls: () => installCalls };
}

async function prepareUnconfirmedInstall() {
  const runtime = makeInstallStageFailure("install_state_publish_failed", { failPartialCleanup: true });
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].cleanupComplete, false);
  runtime.context.__observerTestDefineFault = () => false;
  runtime.embed.removeEventListener = function (name, callback) { this._listeners.get(name)?.delete(callback); };
  return runtime;
}

async function completeInPlaceAVNavigation(runtime, navigation) {
  const url = `app://./renderer/index.html?webcontentsid=5&processGroupNS=createBlankWindow1&observer_nav=${navigation}#/av`;
  runtime.webContents.emit("did-start-navigation", {}, url, true, true);
  runtime.webContents.url = url;
  runtime.webContents.emit("did-navigate-in-page", {}, url, true, 42, 99);
  await new Promise((resolve) => setTimeout(resolve, 40));
  return url;
}

async function assertCleanInstallStageFailure(stage) {
  const runtime = makeInstallStageFailure(stage);
  const report = await runtime.observer.start();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.windows[0].status, stage);
  assert.equal(report.windows[0].embedCount, 1);
  assert.equal(report.windows[0].postMessageAvailable, true, "the original method was validated before setup failed");
  assert.equal(report.windows[0].cleanupComplete, true);
  assert.equal(runtime.installCalls(), 1, "the failed setup is not retried in this epoch");
  assert.equal(runtime.embed.postMessage, runtime.original, "a failed install restores the validated original method");
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal([...runtime.listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal(pageState(runtime.context), null);
  assert.equal(runtime.embed.postMessage("after-failed-install"), runtime.sentinel);
  assert.equal(runtime.embed.calls.length, 1, "the original method still runs once and returns unchanged");
  const thrown = (() => { try { runtime.embed.postMessage("throw-sentinel"); } catch (error) { return error; } })();
  assert.equal(thrown, runtime.sentinel, "the original thrown object is preserved after failed setup");
  assert.equal(runtime.embed.calls.length, 2, "each caller invocation reaches the original method once");
  assert.equal(JSON.stringify(report).includes(runtime.installSentinel), false);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(stopped.windows[0].cleanupComplete, true);
}

test("install diagnostics identify wrapper definition failure without claiming a missing method", async () => {
  await assertCleanInstallStageFailure("install_embed_method_failed");
});

test("install diagnostics identify embed listener failure and remove acquired wrapper", async () => {
  await assertCleanInstallStageFailure("install_embed_listener_failed");
});

test("a listener that registers its own callback before throwing is still cleaned up", async () => {
  const runtime = makeInstallStageFailure("install_embed_listener_failed", { listenerRegistersBeforeThrow: true });
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_embed_listener_failed");
  assert.equal(report.windows[0].cleanupComplete, true);
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal(runtime.embed.postMessage, runtime.original);
  assert.equal(JSON.stringify(report).includes(runtime.installSentinel), false);
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("a wrapper property mutation followed by a throw is restored by ownership", async () => {
  const runtime = makeInstallStageFailure("install_embed_method_failed", { afterDefineMutation: true });
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_embed_method_failed");
  assert.equal(report.windows[0].postMessageAvailable, true);
  assert.equal(report.windows[0].cleanupComplete, true);
  assert.equal(runtime.embed.postMessage, runtime.original);
  assert.equal(JSON.stringify(report).includes(runtime.installSentinel), false);
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("install diagnostics identify pagehide listener failure and remove prior resources", async () => {
  await assertCleanInstallStageFailure("install_pagehide_listener_failed");
});

test("install diagnostics identify state publication failure and remove prior resources", async () => {
  await assertCleanInstallStageFailure("install_state_publish_failed");
});

test("lease timer throw-before-acquisition is conservatively reported as incomplete", async () => {
  const runtime = makeInstallStageFailure("install_lease_timer_failed");
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_lease_timer_failed");
  assert.equal(report.windows[0].postMessageAvailable, true);
  assert.equal(report.windows[0].cleanupComplete, false);
  assert.equal(runtime.context.__observerTestScheduledTimerHandles.length, 0);
  assert.equal(runtime.pageTimers.length, 0);
  assert.equal(runtime.embed.postMessage, runtime.original);
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal([...runtime.listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal(pageState(runtime.context), null);
  assert.equal(JSON.stringify(report).includes(runtime.installSentinel), false);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(stopped.windows[0].cleanupComplete, false);
});

test("timer scheduled before throw stays unconfirmed and blocks in-place reinstallation", async () => {
  const runtime = makeInstallStageFailure("install_lease_timer_failed", { timerSchedulesBeforeThrow: true });
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_lease_timer_failed");
  assert.equal(report.windows[0].cleanupComplete, false);
  assert.equal(JSON.stringify(report).includes(runtime.installSentinel), false);
  assert.equal(runtime.embed.postMessage, runtime.original, "known owned wrapper is restored");
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false,
    "known owned embed listener is removed");
  assert.equal([...runtime.listeners.values()].some((listeners) => listeners.size > 0), false,
    "known owned pagehide listener is removed");
  assert.equal(pageState(runtime.context), null);
  const unknownHandle = runtime.context.__observerTestScheduledTimerHandles[0];
  assert.ok(unknownHandle);
  assert.equal(runtime.pageTimers.length, 1);
  assert.equal(unknownHandle, runtime.pageTimers[0]);
  assert.equal(unknownHandle.cancelled, false, "cleanup does not invent a handle to cancel");

  await completeInPlaceAVNavigation(runtime, 1);
  let current = runtime.observer.status.windows[0];
  assert.equal(current.documentEpoch, 2);
  assert.equal(current.status, "cleanup_incomplete");
  assert.equal(current.cleanupComplete, false);
  await completeInPlaceAVNavigation(runtime, 2);
  current = runtime.observer.status.windows[0];
  assert.equal(current.documentEpoch, 3);
  assert.equal(current.status, "cleanup_incomplete");
  assert.equal(runtime.installCalls(), 1);
  assert.equal(runtime.embed.postMessage, runtime.original);
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal(unknownHandle.cancelled, false);

  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(stopped.windows[0].cleanupComplete, false);
  assert.equal(runtime.embed.postMessage, runtime.original);
  assert.equal([...runtime.embed._listeners.values()].some((listeners) => listeners.size > 0), false);
  assert.equal(unknownHandle.cancelled, false);
});

test("partial install cleanup failure stays false after same-epoch no-state cleanup", async () => {
  const runtime = makeInstallStageFailure("install_state_publish_failed", { failPartialCleanup: true });
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_state_publish_failed");
  assert.equal(report.windows[0].postMessageAvailable, true);
  assert.equal(report.windows[0].cleanupComplete, false);
  assert.equal(runtime.embed._listeners.get("message")?.size, 1, "failed listener removal is reported as incomplete");
  const retainedWrapper = runtime.embed.postMessage;
  assert.notEqual(retainedWrapper, runtime.original, "failed restoration never silently claims success");
  assert.equal(runtime.embed.postMessage("pass-through-after-cleanup-failure"), runtime.sentinel);
  assert.equal(runtime.embed.calls.length, 1, "inactive wrapper preserves one original call and its return");
  const thrown = (() => { try { runtime.embed.postMessage("throw-sentinel"); } catch (error) { return error; } })();
  assert.equal(thrown, runtime.sentinel, "the incomplete wrapper still preserves the original thrown object");
  assert.equal(runtime.embed.calls.length, 2);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false, "a later no-state response cannot erase the initial failure");
  assert.equal(stopped.windows[0].status, "install_state_publish_failed");
  assert.equal(stopped.windows[0].cleanupComplete, false);
  assert.equal(runtime.embed.postMessage, retainedWrapper, "cleanup does not replace a method it could not safely restore");
  assert.equal(JSON.stringify(stopped).includes(runtime.installSentinel), false);
});

test("unconfirmed resources remain blocked across successive in-place logical epochs", async () => {
  const runtime = await prepareUnconfirmedInstall();
  const retainedWrapper = runtime.embed.postMessage;
  assert.equal(runtime.embed._listeners.get("message")?.size, 1);

  await completeInPlaceAVNavigation(runtime, 1);
  let current = runtime.observer.status.windows[0];
  assert.equal(current.documentEpoch, 2);
  assert.equal(current.status, "cleanup_incomplete");
  assert.equal(current.cleanupComplete, false);

  await completeInPlaceAVNavigation(runtime, 2);
  current = runtime.observer.status.windows[0];
  assert.equal(current.documentEpoch, 3);
  assert.equal(current.status, "cleanup_incomplete", "a second in-place epoch cannot retire document-owned uncertainty");
  assert.equal(current.cleanupComplete, false);
  assert.equal(runtime.installCalls(), 1, "the dirty document is never wrapped again");
  assert.equal(runtime.embed.postMessage, retainedWrapper);
  assert.equal(runtime.embed._listeners.get("message")?.size, 1);

  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(stopped.windows[0].cleanupComplete, false);
  assert.equal(stopped.windows[0].status, "cleanup_incomplete");
  assert.equal(runtime.embed.postMessage, retainedWrapper);
  assert.equal(runtime.embed._listeners.get("message")?.size, 1);
});

test("one in-place navigation cannot turn unconfirmed install cleanup into success at stop", async () => {
  const runtime = await prepareUnconfirmedInstall();
  const retainedWrapper = runtime.embed.postMessage;
  await completeInPlaceAVNavigation(runtime, 1);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(stopped.windows[0].cleanupComplete, false);
  assert.equal(stopped.windows[0].status, "cleanup_incomplete");
  assert.equal(runtime.installCalls(), 1);
  assert.equal(runtime.embed.postMessage, retainedWrapper);
  assert.equal(runtime.embed._listeners.get("message")?.size, 1);
});

test("renderer destruction explicitly ends ownership of failed install resources", async () => {
  const runtime = makeInstallStageFailure("install_state_publish_failed", { failPartialCleanup: true });
  await runtime.observer.start();
  runtime.window.destroyed = true;
  runtime.webContents.destroyed = true;
  runtime.webContents.emit("destroyed");
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.windows[0].status, "destroyed");
  assert.equal(stopped.windows[0].cleanupComplete, true);
  assert.equal(stopped.cleanupComplete, true);
});

test("only a completed full-document replacement retires prior-epoch install cleanup state", async () => {
  const runtime = makeInstallStageFailure("install_state_publish_failed", { failPartialCleanup: true });
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].cleanupComplete, false);
  // The browser discards the old document realm and its DOM-owned resources at full navigation.
  runtime.embed.postMessage = runtime.original;
  for (const listeners of runtime.embed._listeners.values()) listeners.clear();
  runtime.webContents.emit("did-start-navigation", {}, "app://./renderer/index.html#/main/message", false, true);
  runtime.webContents.url = "app://./renderer/index.html#/main/message";
  runtime.webContents.emit("dom-ready");
  const deadline = Date.now() + 500;
  while (runtime.observer.status.windows[0].status !== "not_av" && Date.now() < deadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const current = runtime.observer.status;
  assert.equal(current.windows[0].documentEpoch, 2);
  assert.equal(current.windows[0].status, "not_av");
  assert.equal(current.windows[0].cleanupComplete, null, "the old epoch's cleanup result is not attributed to the replacement document");
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("completed full-document replacement uses a fresh renderer and can safely arm it", async () => {
  const runtime = await prepareUnconfirmedInstall();
  const oldContext = runtime.context;
  const oldWrapper = runtime.embed.postMessage;
  const replacementUrl = "app://./renderer/index.html?webcontentsid=6&processGroupNS=createBlankWindow1#/av";
  const replacement = makeRuntime({ url: replacementUrl });
  const oldExecute = runtime.webContents.executeJavaScript;
  let replacementDocumentActive = false;
  runtime.webContents.executeJavaScript = (script) => replacementDocumentActive
    ? replacement.webContents.executeJavaScript(script)
    : oldExecute(script);

  runtime.webContents.emit("did-start-navigation", {}, replacementUrl, false, true);
  runtime.webContents.url = replacementUrl;
  replacementDocumentActive = true;
  runtime.webContents.emit("dom-ready");

  const deadline = Date.now() + 500;
  while (runtime.observer.status.windows[0].status !== "armed" && Date.now() < deadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const current = runtime.observer.status.windows[0];
  assert.equal(current.documentEpoch, 2);
  assert.equal(current.status, "armed");
  assert.notEqual(replacement.context, oldContext);
  assert.notEqual(replacement.embed.postMessage, replacement.original, "only the replacement document receives a new wrapper");
  assert.equal(runtime.embed.postMessage, oldWrapper, "the discarded realm is not rewritten to manufacture cleanup success");

  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(replacement.embed.postMessage, replacement.original);
});

function domMessage(data) {
  const prototype = Object.defineProperty({}, "data", { get() { return this.payload; } });
  const event = Object.create(prototype);
  Object.defineProperty(event, "payload", { configurable: false, enumerable: false, value: data });
  return event;
}

test("native MessageEvent.data prototype accessor is projected without invoking a page bridge", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  const message = new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20004, id: "native-reply-secret", param: ["private-callback-text"] }),
  });
  assert.equal(Object.hasOwn(message, "data"), false);
  assert.equal(typeof Object.getOwnPropertyDescriptor(Object.getPrototypeOf(message), "data")?.get, "function");
  runtime.embed.dispatch("message", message);
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].command, 20004);
  assert.equal(report.events[0].requestId.type, "string");
  assert.equal(JSON.stringify(report).includes("native-reply-secret"), false);
  assert.equal(JSON.stringify(report).includes("private-callback-text"), false);
  await runtime.observer.stop();
});

test("expired incoming callbacks check the absolute lease before touching prototype data", async () => {
  const runtime = makeRuntime({ maxLeaseMs: 1000 });
  await runtime.observer.start();
  const state = pageState(runtime.context);
  let dataReads = 0;
  const message = Object.create({ get data() { dataReads += 1; return JSON.stringify({ cmd: 20004, param: ["late-private-text"] }); } });
  runtime.clock.now = Date.now() + 2000;
  runtime.embed.dispatch("message", message);
  assert.equal(dataReads, 0);
  assert.equal(runtime.embed.postMessage, runtime.original);
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 0);
  assert.equal(JSON.stringify(report).includes("late-private-text"), false);
  assert.equal(state.active, false);
});

test("outgoing records are explicitly attempts even when the original SDK method throws", async () => {
  const runtime = makeRuntime();
  const thrown = Object.freeze({ marker: "same-throw" });
  runtime.embed.postMessage = function (...args) {
    assert.equal(this, runtime.embed);
    assert.equal(args[0].cmd, 7);
    throw thrown;
  };
  await runtime.observer.start();
  const command = vm.runInContext(`({cmd:7,id:"attempt-id-secret",param:["private-data"]})`, runtime.context);
  let caught;
  try { runtime.embed.postMessage(command); } catch (error) { caught = error; }
  assert.equal(caught, thrown);
  const report = await runtime.observer.drain();
  assert.equal(report.outboundAttemptsOnly, true);
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].command, 7);
  assert.equal(JSON.stringify(report).includes("attempt-id-secret"), false);
  await runtime.observer.stop();
});

test("Native command 1 and 57 startup parameters are reduced to redacted comparisons and lengths", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  const selfUid = `u_${"UID_SECRET".repeat(2)}`;
  const selfUin = "987654321012";
  const dbPath = `C:\\Users\\profile-secret\\QQ\\account\\${selfUin}`;
  const machineId = "machine-secret-value";
  const login = vm.runInContext(`({cmd:1,id:"login-request-secret",param:${JSON.stringify([
    selfUid, selfUin, selfUin, dbPath, machineId,
  ])}})`, runtime.context);
  const sdkInfo = vm.runInContext(`({cmd:57,id:"sdk-info-request-secret",param:["1.0.1168"]})`, runtime.context);
  const unrecognizedSDKInfo = vm.runInContext(`({cmd:57,id:"unknown-version-id",param:["private-version-token"]})`, runtime.context);
  assert.equal(runtime.embed.postMessage(login), runtime.sentinel);
  assert.equal(runtime.embed.postMessage(sdkInfo), runtime.sentinel);
  assert.equal(runtime.embed.postMessage(unrecognizedSDKInfo), runtime.sentinel);

  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.deepEqual(report.events.map((event) => event.command), [1, 57, 57]);
  assert.deepEqual(report.events[0].loginStartup, {
    tinyIdKind: "nonempty_string",
    tinyIdEqualsSelfUin: true,
    tinyIdEqualsSelfUid: false,
    dbPathKind: "string",
    dbPathLength: dbPath.length,
    dbPathIsAbsolute: true,
    dbPathEndsSelfUin: true,
    machineIdKind: "string",
    machineIdLength: machineId.length,
  });
  assert.deepEqual(report.events[1].sdkInfoStartup, {
    kind: "string", length: "1.0.1168".length, exactKnownVersionMatches1_0_1168: true,
  });
  assert.deepEqual(report.events[2].sdkInfoStartup, {
    kind: "string", length: "private-version-token".length, exactKnownVersionMatches1_0_1168: false,
  });
  const serialized = JSON.stringify(report);
  for (const secret of [selfUid, selfUin, "profile-secret", machineId, "login-request-secret", "sdk-info-request-secret",
    "unknown-version-id", "1.0.1168", "private-version-token"]) {
    assert.equal(serialized.includes(secret), false, `startup diagnostics omit ${secret}`);
  }
  await runtime.observer.stop();
});

test("Native login startup classifies undefined, null, zero, strings, and other tiny-id values without retaining them", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.context.__selfUid = `u_${"self-secret".repeat(2)}`;
  runtime.context.__selfUin = "135791357913";
  runtime.context.__dbPath = `C:\\QQ\\account\\${runtime.context.__selfUin}`;
  runtime.context.__machineId = "machine-secret";
  const cases = [
    ["undefined", "undefined", false, false],
    ["null", "null", false, false],
    ["0", "number_zero", false, false],
    ["17", "number_other", false, false],
    ['""', "empty_string", false, false],
    ['"0"', "zero_string", false, false],
    ['"x"', "nonempty_string", false, false],
    ['"tiny-secret"', "nonempty_string", false, false],
    ["__selfUin", "nonempty_string", true, false],
    ["__selfUid", "nonempty_string", false, true],
    ['({ marker: "object-secret" })', "other", false, false],
  ];
  for (let index = 0; index < cases.length; index += 1) {
    const [expression] = cases[index];
    const command = vm.runInContext(`({cmd:1,id:"case-${index}",param:[__selfUid,__selfUin,${expression},__dbPath,__machineId]})`, runtime.context);
    runtime.embed.postMessage(command);
  }
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, cases.length);
  const zeroStringWithWrongLength = structuredClone(report);
  zeroStringWithWrongLength.events[5].parameterItems[2].length = 0;
  assert.equal(isObserverReport(zeroStringWithWrongLength), false,
    "the zero-string classification is only valid for a one-character string descriptor");
  for (const [index, [unused, kind, equalsUin, equalsUid]] of cases.entries()) {
    const event = report.events[index];
    assert.equal(event.loginStartup.tinyIdKind, kind);
    assert.equal(event.loginStartup.tinyIdEqualsSelfUin, equalsUin);
    assert.equal(event.loginStartup.tinyIdEqualsSelfUid, equalsUid);
  }
  assert.equal(report.events[0].parameterItems[2].type, "undefined");
  assert.equal(report.events[1].parameterItems[2].type, "null");
  const serialized = JSON.stringify(report);
  for (const secret of ["self-secret", "135791357913", "machine-secret", "tiny-secret", "object-secret"]) {
    assert.equal(serialized.includes(secret), false);
  }
  await runtime.observer.stop();
});

test("malformed or pre-arm login and SDK-info traffic never creates a startup projection", async () => {
  const runtime = makeRuntime();
  const beforeArm = vm.runInContext(`({cmd:1,id:"pre-arm-login",param:["u_prearm","123456789012","123456789012","C:\\\\prearm\\\\path",""]})`, runtime.context);
  runtime.embed.postMessage(beforeArm);
  await runtime.observer.start();

  runtime.context.__uid = "u_accessor_secret";
  runtime.context.__uin = "123456789012";
  runtime.context.__path = "C:\\Users\\private\\account\\123456789012";
  runtime.context.__machine = "machine-private";
  runtime.embed.postMessage(vm.runInContext(`({cmd:1,id:"short-login",param:[__uid,__uin,__uin,__path]})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:1,id:"sparse-login",param:[__uid,__uin,,__path,__machine]})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`(() => { const param=[__uid,__uin,__uin,__path,__machine]; Object.defineProperty(param,"2",{get(){return "accessor-secret";}}); return {cmd:1,id:"accessor-login",param}; })()`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:1,id:"absent-login-param"})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:57,id:"absent-sdk-info"})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:57,id:"extra-sdk-info",param:["1.0.1168","extra-secret"]})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:57,id:"nonstring-sdk-info",param:[321]})`, runtime.context));
  runtime.embed.postMessage(1);
  runtime.embed.postMessage(1, undefined);

  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.deepEqual(report.events.map((event) => event.command), [1, 1, 1, 1, 57, 57, 57, 1, 1],
    "a login before arming is absent, not inferred from a later command-57 event");
  assert.equal(report.events.every((event) => event.loginStartup === undefined && event.sdkInfoStartup === undefined), true);
  assert.deepEqual(report.events[1].parameterItems[2], { type: "absent" });
  assert.deepEqual(report.events[2].parameterItems[2], { type: "unknown" });
  assert.equal(report.events[3].parameterKind, "absent");
  assert.equal(report.events[4].parameterKind, "absent");
  assert.equal(report.events[7].parameterKind, "absent", "one postMessage argument stays absent");
  assert.deepEqual(report.events[8].parameterItems, [{ type: "undefined" }],
    "an explicitly present undefined second argument stays distinct from absence");
  const serialized = JSON.stringify(report);
  for (const secret of ["accessor-secret", "extra-secret", "private\\account", "machine-private", "pre-arm-login"]) {
    assert.equal(serialized.includes(secret), false);
  }
  await runtime.observer.stop();
});

test("startup projections reject malformed schema fields and do not retain accessor or secret extras", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  const login = vm.runInContext(`({cmd:1,id:"request-safe",param:["u_secret_uid","123456789012","123456789012","C:\\\\Users\\\\private\\\\123456789012","machine_secret"]})`, runtime.context);
  runtime.embed.postMessage(login);
  runtime.embed.postMessage(vm.runInContext(`({cmd:57,id:"sdk-info-safe",param:["1.0.1168"]})`, runtime.context));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);

  const extraField = structuredClone(report);
  extraField.events[0].loginStartup.rawSecret = "raw-credential-secret";
  assert.equal(isObserverReport(extraField), false);

  const invalidEnum = structuredClone(report);
  invalidEnum.events[0].loginStartup.tinyIdKind = "123456789012";
  assert.equal(isObserverReport(invalidEnum), false);

  const invalidCommand = structuredClone(report);
  invalidCommand.events[0].command = 5;
  assert.equal(isObserverReport(invalidCommand), false);

  const invalidKnownVersionLength = structuredClone(report);
  invalidKnownVersionLength.events[1].sdkInfoStartup.length = 7;
  assert.equal(isObserverReport(invalidKnownVersionLength), false,
    "the known SDK version flag requires the exact source-backed eight-character length");

  let getterReads = 0;
  const accessorField = structuredClone(report);
  Object.defineProperty(accessorField.events[0].loginStartup, "tinyIdKind", {
    configurable: true, enumerable: true, get() { getterReads += 1; return "raw-secret"; },
  });
  assert.equal(isObserverReport(accessorField), false);
  assert.equal(getterReads, 0);
  assert.equal(JSON.stringify(report).includes("raw-credential-secret"), false);
  assert.equal(JSON.stringify(report).includes("123456789012"), false);
  await runtime.observer.stop();
});

test("expired postMessage self-cleans before capture while preserving the original call", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  assert.notEqual(runtime.embed.postMessage, runtime.original);
  runtime.context.__hostDateNow = Date.now;
  vm.runInContext("Date.now = () => __hostDateNow() + 60000", runtime.context);
  const command = vm.runInContext(`({cmd:5,id:"after-lease-secret",param:["late-private-value"]})`, runtime.context);
  assert.equal(runtime.embed.postMessage(command), runtime.sentinel);
  vm.runInContext("Date.now = __hostDateNow; delete __hostDateNow", runtime.context);
  assert.equal(runtime.embed.calls.length, 1, "the SDK method still runs exactly once");
  assert.equal(runtime.embed.postMessage, runtime.original, "the expired wrapper restores its own descriptor synchronously");
  assert.equal(pageState(runtime.context), null);
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 0);
  assert.equal(JSON.stringify(report).includes("after-lease-secret"), false);
  await runtime.observer.stop();
});

test("observer wraps only the unique AV embed transparently and projects allowlisted IDs without retaining payload text", async () => {
  const runtime = makeRuntime();
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(started.status, "active");
  assert.equal(started.coverageGap, true);
  assert.equal(started.outboundAttemptsOnly, true);
  assert.equal(started.windows[0].status, "armed");
  assert.equal(started.windows[0].postMessageAvailable, true);

  const command = vm.runInContext(`({cmd:4,id:"request-secret-987654",param:[JSON.stringify({scene_id:3,invite_count:2,sub_business_type:7,invite_original:1,is_creator:true,relation_id:"relation-secret",room_id:"room-secret",self_uid:"uid-secret",invite_uids:["invite-one","invite-two"],unknown:"chat-secret"}),"private-token-secret"]})`, runtime.context);
  assert.equal(runtime.embed.postMessage(command), runtime.sentinel);
  assert.equal(runtime.embed.calls[0][0], command);
  const thrown = (() => { try { runtime.embed.postMessage("throw-sentinel"); } catch (error) { return error; } })();
  assert.equal(thrown, runtime.sentinel, "the same thrown object reaches the caller");

  const incoming = vm.runInContext(`({cmd:20004,id:"reply-secret-123",param:["callback-private-text",17]})`, runtime.context);
  runtime.embed.dispatch("message", domMessage(incoming));
  runtime.embed.dispatch("message", domMessage(JSON.stringify({ cmd: 20006, id: "reply-secret-456", param: ["plain-json-private-text"] })));

  const state = pageState(runtime.context);
  assert.ok(state);
  assert.equal([...state.refs.keys()].some((value) => /chat-secret|private-token-secret|callback-private-text|plain-json-private-text/.test(value)), false,
    "untyped strings and unknown fields never enter the opaque ID map");
  assert.equal([...state.refs.keys()].some((value) => /request-secret|relation-secret|room-secret|uid-secret|invite-one|reply-secret/.test(value)), true,
    "only known request and identity fields are held in the document-local map");

  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, 3, JSON.stringify(report.events));
  const outgoing = report.events[0];
  assert.equal(outgoing.direction, "outgoing");
  assert.equal(outgoing.command, 4);
  assert.equal(outgoing.documentEpoch, 1);
  assert.equal(outgoing.requestId.type, "string");
  assert.match(outgoing.requestId.ref, /^v[1-9]/);
  assert.equal(outgoing.command4.sceneId, 3);
  assert.equal(outgoing.command4.inviteCount, 2);
  assert.equal(outgoing.command4.isCreator, true);
  assert.equal(outgoing.command4.inviteUidRefs.length, 2);
  assert.equal(report.events[1].command, 20004);
  assert.equal(report.events[1].parameterItems[0].type, "string");
  assert.equal(report.events[1].parameterItems[0].ref, null);
  const serialized = JSON.stringify(report);
  for (const secret of ["request-secret", "relation-secret", "room-secret", "uid-secret", "invite-one", "chat-secret", "private-token", "callback-private", "plain-json-private"]) {
    assert.equal(serialized.includes(secret), false, `private value ${secret} is absent`);
  }
  assert.equal(serialized.includes("#/av"), false);
  assert.equal(report.comparisonScope, "document_epoch_only");

  const stopped = await runtime.observer.stop();
  assert.equal(stopped.status, "stopped");
  assert.equal(stopped.cleanupComplete, true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  assert.equal(await runtime.observer.stop(), stopped, "stop is idempotent");
});

test("incoming-only mode observes responses without mutating a nonconfigurable native method", async () => {
  const runtime = makeRuntime({ incomingOnly: true });
  Object.defineProperty(runtime.embed, "postMessage", { configurable: false, enumerable: true, writable: false, value: runtime.original });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].status, "armed_incoming_only");
  assert.equal(started.coverageGap, true);
  assert.equal(isObserverReport(started), true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);

  const command = vm.runInContext(`({cmd:4,id:"outgoing-secret",param:["private-outgoing"]})`, runtime.context);
  assert.equal(runtime.embed.postMessage(command), runtime.sentinel);
  assert.equal(runtime.embed.calls.length, 1, "the original method runs exactly once");
  assert.equal(runtime.embed.calls[0][0], command, "the original receives the identical first argument");
  const thrown = (() => { try { runtime.embed.postMessage("throw-sentinel"); } catch (error) { return error; } })();
  assert.equal(thrown, runtime.sentinel, "the same thrown object is preserved");
  assert.equal(runtime.embed.calls[1][0], "throw-sentinel");
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);

  const incoming = new MessageEvent("message", { data: JSON.stringify({ cmd: 20004, id: "reply-private", param: ["private-callback"] }) });
  runtime.embed.dispatch("message", incoming);
  const drained = await runtime.observer.drain();
  assert.equal(drained.events.length, 1);
  assert.equal(drained.events[0].direction, "incoming");
  assert.equal(drained.events[0].command, 20004);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  for (const secret of ["outgoing-secret", "private-outgoing", "reply-private", "private-callback"]) {
    assert.equal(JSON.stringify(drained).includes(secret), false);
  }

  await completeInPlaceAVNavigation(runtime, 11);
  assert.equal(runtime.observer.status.windows[0].status, "armed_incoming_only");
  runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20006, id: "second-reply-private", param: [17] }),
  }));
  const afterNavigation = await runtime.observer.drain();
  assert.equal(afterNavigation.events.length, 2);
  assert.ok(afterNavigation.events.every((event) => event.direction === "incoming"));
  assert.equal(afterNavigation.events[1].command, 20006);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);

  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
});

test("native conditional mode observes bounded call attempts and incoming 20004 without changing the native method", async () => {
  const runtime = makeRuntime({ nativeConditional: true });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(isObserverReport(started), true);
  assert.equal(started.windows[0].status, "armed_native_conditional");
  assert.equal(started.windows[0].nativeConditionalStatus, "breakpoint_set");
  assert.equal(started.coverageGap, true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  const target = runtime.debuggerApi.commands.find((entry) => entry.method === "Runtime.evaluate");
  const breakpoint = runtime.debuggerApi.commands.find((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall");
  assert.equal(target.params.expression,
    buildNativeTargetExpression("0123456789abcdef0123456789abcdef", 1, 1));
  assert.equal(breakpoint.params.condition,
    buildNativeConditionalExpression("0123456789abcdef0123456789abcdef", 1));
  assert.equal(runtime.debuggerApi.commands.find((entry) => entry.method === "Debugger.setSkipAllPauses").params.skip, true);

  const outgoing = vm.runInContext(`({cmd:4,id:"native-request-secret",param:[JSON.stringify({scene_id:3,invite_count:1,
    relation_id:"native-relation-secret",room_id:"native-room-secret",self_uid:"native-self-secret"})]})`, runtime.context);
  const condition = vm.runInContext(`(function(){return ${breakpoint.params.condition}})`, runtime.context);
  assert.equal(Reflect.apply(condition, runtime.embed, [4, [outgoing.param[0]]]), false);
  assert.equal(runtime.embed.postMessage(outgoing, outgoing.param), runtime.sentinel);
  assert.equal(runtime.embed.calls.length, 1, "the unmodified native method still runs only when called by the test");
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);

  let getterReads = 0;
  vm.runInContext(`globalThis.__observerTestGetterReads=0;globalThis.__observerTestGetterCommand={cmd:4,param:[]};
    Object.defineProperty(globalThis.__observerTestGetterCommand.param,"0",{configurable:true,get(){globalThis.__observerTestGetterReads+=1;return "must-not-read"}})`, runtime.context);
  const getterCommand = runtime.context.__observerTestGetterCommand;
  assert.equal(Reflect.apply(condition, runtime.embed, [getterCommand]), false);
  getterReads = runtime.context.__observerTestGetterReads;
  assert.equal(getterReads, 0, "native argument projection uses own data descriptors only");

  const incomingMessage = new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20004, id: "native-incoming-secret", param: ["private-callback"] }),
  });
  assert.equal(pageState(runtime.context).active, true);
  runtime.embed.dispatch("message", incomingMessage);
  assert.equal(pageState(runtime.context).active, true, "the renderer lease is still active after the incoming message");
  assert.equal(runtime.embed._listeners.get("message")?.size, 1);
  assert.equal(pageState(runtime.context).events.length, 3, JSON.stringify({ dropped: pageState(runtime.context).dropped, incomplete: pageState(runtime.context).incomplete }));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.windows[0].nativeCallAttempts, 2);
  assert.equal(report.events.length, 3);
  assert.equal(report.events[0].direction, "outgoing");
  assert.equal(report.events[0].command, 4);
  assert.equal(report.events[0].command4.sceneId, 3);
  assert.equal(report.events[1].command4.available, false, "an accessor at param[0] is ignored");
  assert.equal(report.events[2].direction, "incoming");
  assert.equal(report.events[2].command, 20004);
  for (const secret of ["native-request-secret", "native-relation-secret", "native-room-secret", "native-self-secret", "must-not-read", "native-incoming-secret"]) {
    assert.equal(JSON.stringify(report).includes(secret), false);
  }
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  assert.equal(runtime.debuggerApi.detachCalls, 1);
});

test("an unexpected pause disables renderer capture and automatically tears down the owned observer", async () => {
  const runtime = makeRuntime({ nativeConditional: true });
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].nativeConditionalStatus, "breakpoint_set");
  const state = pageState(runtime.context);
  assert.equal(state.captureEnabled, true);
  runtime.debuggerApi.emit("message", {}, "Debugger.paused", { reason: "not-retained" });
  assert.equal(state.captureEnabled, false, "the fixed page lease is disabled before async cleanup completes");
  const beforeAttempts = state.nativeCallAttempts;
  const breakpoint = runtime.debuggerApi.commands.find((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall");
  const condition = vm.runInContext(`(function(){return ${breakpoint.params.condition}})`, runtime.context);
  assert.equal(Reflect.apply(condition, runtime.embed, [4, []]), false);
  assert.equal(state.nativeCallAttempts, beforeAttempts, "stale conditions become no-ops after abort");
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(runtime.debuggerApi.detachCalls, 1);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), {
    configurable: false, enumerable: true, writable: false, value: runtime.original,
  });
});

test("native attempt cap short-circuits before a proxied argument can run traps", async () => {
  const runtime = makeRuntime({ nativeConditional: true });
  await runtime.observer.start();
  const state = pageState(runtime.context);
  state.nativeCallAttempts = 1000000;
  vm.runInContext(`globalThis.__observerTestProxyTrapReads=0;globalThis.__observerTestArgument=new Proxy({}, {
    getPrototypeOf(){globalThis.__observerTestProxyTrapReads++;throw new Error("must-not-run")},
    ownKeys(){globalThis.__observerTestProxyTrapReads++;throw new Error("must-not-run")},
    getOwnPropertyDescriptor(){globalThis.__observerTestProxyTrapReads++;throw new Error("must-not-run")},
  })`, runtime.context);
  const breakpoint = runtime.debuggerApi.commands.find((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall");
  const condition = vm.runInContext(`(function(){return ${breakpoint.params.condition}})`, runtime.context);
  assert.equal(Reflect.apply(condition, runtime.embed, [runtime.context.__observerTestArgument]), false);
  assert.equal(runtime.context.__observerTestProxyTrapReads, 0);
  assert.equal(state.nativeCallAttempts, 1000000);
  assert.equal(state.nativeCallAttemptsSaturated, true);
  assert.equal(state.events.length, 0);
  await runtime.observer.stop();
});

test("native conditional mode leaves an already attached debugger untouched and rejects setup", async () => {
  const runtime = makeRuntime({ nativeConditional: true, debuggerInitiallyAttached: true });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].status, "unavailable");
  assert.equal(started.windows[0].nativeConditionalStatus, "debugger_already_attached");
  assert.equal(runtime.debuggerApi.commands.length, 0);
  assert.equal(runtime.debuggerApi.detachCalls, 0);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(runtime.debuggerApi.detachCalls, 0);
});

test("native attach failures are projected only through the fixed official allowlist", async () => {
  const cases = [
    ["Requested protocol version is not supported", "attach_protocol_unsupported"],
    ["No target available", "attach_no_target"],
    ["Debugger is already attached to the target", "attach_target_retained"],
    ["unknown-private-attach-secret", "attach_failed"],
  ];
  for (const [message, expected] of cases) {
    const runtime = makeRuntime({ nativeConditional: true });
    runtime.debuggerApi.attach = () => { throw new Error(message); };
    const started = await runtime.observer.start();
    assert.equal(isObserverReport(started), true);
    assert.equal(started.windows[0].nativeConditionalStatus, expected);
    assert.equal(started.windows[0].status, "unavailable");
    assert.equal(JSON.stringify(started).includes(message), false);
    const forgedStatus = JSON.parse(JSON.stringify(started));
    forgedStatus.windows[0].nativeConditionalStatus = "attach_raw_secret";
    assert.equal(isObserverReport(forgedStatus), false);
    assert.equal(runtime.debuggerApi.commands.length, 0);
    assert.equal(runtime.debuggerApi.detachCalls, 0);
    assert.equal((await runtime.observer.stop()).windows[0].nativeConditionalStatus, expected);
  }
});

test("native attach status classification never evaluates a message accessor", async () => {
  let getterReads = 0;
  const error = new Error();
  Object.defineProperty(error, "message", {
    configurable: true,
    get() { getterReads += 1; return "Requested protocol version is not supported"; },
  });
  const runtime = makeRuntime({ nativeConditional: true });
  runtime.debuggerApi.attach = () => { throw error; };
  const started = await runtime.observer.start();
  assert.equal(isObserverReport(started), true);
  assert.equal(started.windows[0].nativeConditionalStatus, "attach_failed");
  assert.equal(getterReads, 0);
  assert.equal(JSON.stringify(started).includes("Requested protocol version"), false);
  assert.equal(runtime.debuggerApi.detachCalls, 0);
});

test("native attach return without a confirmed connection preserves cleanup uncertainty", async () => {
  const cases = [
    ["attach_not_connected", (debuggerApi) => {
      debuggerApi.attach = () => {};
    }],
    ["attach_state_unknown", (debuggerApi) => {
      let queries = 0;
      debuggerApi.isAttached = () => {
        queries += 1;
        if (queries === 1) return false;
        throw new Error("raw-state-query-sentinel");
      };
    }],
  ];
  for (const [expected, configure] of cases) {
    const runtime = makeRuntime({ nativeConditional: true });
    configure(runtime.debuggerApi);
    const started = await runtime.observer.start();
    assert.equal(isObserverReport(started), true);
    assert.equal(started.windows[0].nativeConditionalStatus, expected);
    assert.equal(started.windows[0].cleanupComplete, false);
    assert.equal(started.cleanupComplete, null, "the aggregate cleanup result is unresolved until stop completes");
    assert.equal(started.incomplete, true);
    assert.equal(JSON.stringify(started).includes("raw-state-query-sentinel"), false);
    const stopped = await runtime.observer.stop();
    assert.equal(isObserverReport(stopped), true);
    assert.equal(stopped.windows[0].nativeConditionalStatus, expected);
    assert.equal(stopped.windows[0].cleanupComplete, false);
    assert.equal(stopped.cleanupComplete, false);
    assert.equal(runtime.debuggerApi.detachCalls, 0);
    assert.equal(runtime.debuggerApi.commands.length, 0);
  }
});

test("incoming 20004 enter-room projection is bounded, source-shaped, and document-scoped", async () => {
  const runtime = makeRuntime({ incomingOnly: true });
  Object.defineProperty(runtime.embed, "postMessage", { configurable: false, enumerable: true, writable: false, value: runtime.original });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  await runtime.observer.start();
  const relation = "relation-ref-private-001";
  const peer = "peer-uid-private-002";
  const dispatchResult = (creator, id) => runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20004, id, param: [0, 3, relation, peer, creator, "private-error-message", 7] }),
  }));
  dispatchResult(true, "private-request-id-1");
  let report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, 1);
  assert.deepEqual(report.events[0].enterRoom, {
    ret: 0,
    scene: 3,
    relationId: { ref: "v1", length: relation.length },
    peerUid: { ref: "v2", length: peer.length },
    isCreator: true,
    sdkProvider: 7,
  });
  for (const privateValue of [relation, peer, "private-error-message", "private-request-id-1"]) {
    assert.equal(JSON.stringify(report).includes(privateValue), false);
  }
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);

  await completeInPlaceAVNavigation(runtime, 73);
  dispatchResult(1, "private-request-id-2");
  report = await runtime.observer.drain();
  assert.equal(report.events.length, 2);
  assert.equal(report.events[1].documentEpoch, 2);
  assert.equal(report.events[1].enterRoom.scene, 3);
  assert.equal(report.events[1].enterRoom.isCreator, 1, "the native 0/1 scalar keeps its numeric type");
  assert.deepEqual(report.events[1].enterRoom.relationId, { ref: "v1", length: relation.length },
    "reference allocation resets with the renderer document epoch");
  assert.deepEqual(report.events[1].enterRoom.peerUid, { ref: "v2", length: peer.length });
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor,
    "incoming-only projection never modifies the native listener method");
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("incoming 20004 preserves empty callback IDs and binds non-null projections to the tuple", async () => {
  const runtime = makeRuntime({ incomingOnly: true });
  await runtime.observer.start();
  runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({
      cmd: 20004,
      id: "private-empty-request-id",
      param: [0, 3, "group-relation", "", false, "", 7],
    }),
  }));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, 1);
  assert.deepEqual(report.events[0].enterRoom, {
    ret: 0,
    scene: 3,
    relationId: { ref: "v1", length: "group-relation".length },
    peerUid: { ref: null, length: 0 },
    isCreator: false,
    sdkProvider: 7,
  });
  assert.equal(JSON.stringify(report).includes("group-relation"), false);
  assert.equal(JSON.stringify(report).includes("private-empty-request-id"), false);

  const wrongDirection = structuredClone(report);
  wrongDirection.events[0].direction = "outgoing";
  wrongDirection.events[0].command = 4;
  assert.equal(isObserverReport(wrongDirection), false);

  const wrongCommand = structuredClone(report);
  wrongCommand.events[0].command = 20006;
  assert.equal(isObserverReport(wrongCommand), false);

  const wrongTupleLength = structuredClone(report);
  wrongTupleLength.events[0].parameterLength = 6;
  wrongTupleLength.events[0].parameterItems = wrongTupleLength.events[0].parameterItems.slice(0, 6);
  assert.equal(isObserverReport(wrongTupleLength), false);

  const emptyWithRef = structuredClone(report);
  emptyWithRef.events[0].enterRoom.peerUid.ref = "v2";
  assert.equal(isObserverReport(emptyWithRef), false);

  const nonemptyWithoutRef = structuredClone(report);
  nonemptyWithoutRef.events[0].enterRoom.relationId.ref = null;
  assert.equal(isObserverReport(nonemptyWithoutRef), false);
  await runtime.observer.stop();
});

test("malformed, oversized, nonfinite, accessor, and trapping or revoked Proxy tuples project null", async () => {
  const runtime = makeRuntime({ incomingOnly: true });
  await runtime.observer.start();
  const valid = [0, 3, "relation-private", "peer-private", true, "private-error", 7];
  const malformed = [
    valid.slice(0, 6),
    [2147483648, 3, "relation-private", "peer-private", true, "private-error", 7],
    [0, 1000001, "relation-private", "peer-private", true, "private-error", 7],
    [0, 3, "r".repeat(129), "peer-private", true, "private-error", 7],
    [0, 3, "relation-private", "peer-private", 2, "private-error", 7],
    [0, 3, "relation-private", "peer-private", true, "private-error", Number.MAX_SAFE_INTEGER + 1],
  ];
  for (const [index, param] of malformed.entries()) {
    runtime.embed.dispatch("message", new MessageEvent("message", {
      data: JSON.stringify({ cmd: 20004, id: `private-malformed-${index}`, param }),
    }));
  }
  runtime.context.__enterRoomGetterReads = 0;
  const accessorEvent = vm.runInContext(`(() => {
    const param = [0, 3, "relation-private", "peer-private", true, "private-error", 7];
    Object.defineProperty(param, "2", { configurable: true, get() { globalThis.__enterRoomGetterReads += 1; return "getter-secret"; } });
    return { cmd: 20004, id: "private-accessor-id", param };
  })()`, runtime.context);
  runtime.embed.dispatch("message", domMessage(accessorEvent));
  const proxyEvents = vm.runInContext(`(() => {
    const trapped = new Proxy([0, 3, "relation-private", "peer-private", true, "private-error", 7], {
      ownKeys() { throw new Error("private proxy trap"); },
    });
    const revokedPair = Proxy.revocable([0, 3, "relation-private", "peer-private", true, "private-error", 7], {});
    revokedPair.revoke();
    return [
      { cmd: 20004, id: "private-proxy-id", param: trapped },
      { cmd: 20004, id: "private-revoked-proxy-id", param: revokedPair.proxy },
    ];
  })()`, runtime.context);
  for (const event of proxyEvents) runtime.embed.dispatch("message", domMessage(event));
  const nonfiniteAndOversized = vm.runInContext(`[
    { cmd: 20004, id: "private-nan-id", param: [NaN, 3, "relation-private", "peer-private", true, "private-error", 7] },
    { cmd: 20004, id: "private-infinity-id", param: [0, 3, "relation-private", "peer-private", true, "private-error", Infinity] },
    { cmd: 20004, id: "private-oversized-error-id", param: [0, 3, "relation-private", "peer-private", true, "e".repeat(8193), 7] },
  ]`, runtime.context);
  for (const event of nonfiniteAndOversized) runtime.embed.dispatch("message", domMessage(event));
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, malformed.length + 6);
  assert.ok(report.events.every((event) => event.enterRoom === null));
  assert.equal(runtime.context.__enterRoomGetterReads, 0, "tuple accessor getters are not invoked");
  for (const secret of ["relation-private", "peer-private", "private-error", "getter-secret", "private-proxy-id", "e".repeat(100)]) {
    assert.equal(JSON.stringify(report).includes(secret), false);
  }
  await runtime.observer.stop();
});

test("enter-room projection is incoming 20004 only and legacy event reports remain valid", async () => {
  const runtime = makeRuntime({ incomingOnly: true });
  await runtime.observer.start();
  const tuple = [0, 3, "relation-private", "peer-private", true, "private-error", 7];
  runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20001, id: "private-other-command", param: tuple }),
  }));
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].enterRoom, null, "other incoming commands receive the explicit null projection");

  const legacy = structuredClone(report);
  delete legacy.events[0].enterRoom;
  assert.equal(isObserverReport(legacy), true, "older v1 events without enterRoom remain valid");

  let getterReads = 0;
  const forged = structuredClone(report);
  Object.defineProperty(forged.events[0], "enterRoom", {
    enumerable: true,
    configurable: true,
    get() { getterReads += 1; return null; },
  });
  assert.equal(isObserverReport(forged), false);
  assert.equal(getterReads, 0, "main-side validation rejects accessors without invoking them");
  await runtime.observer.stop();
});

test("full mode still fails closed when the native method cannot be redefined", async () => {
  const runtime = makeRuntime();
  Object.defineProperty(runtime.embed, "postMessage", { configurable: false, enumerable: true, writable: false, value: runtime.original });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const report = await runtime.observer.start();
  assert.equal(report.windows[0].status, "install_embed_method_failed");
  assert.equal(report.windows[0].postMessageAvailable, true);
  assert.equal(report.windows[0].cleanupComplete, true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  assert.equal(runtime.embed.postMessage("full-mode-failed-closed"), runtime.sentinel);
  assert.equal(runtime.embed.calls.length, 1);
  await runtime.observer.stop();
});

test("incoming-only lease expiry removes only its own listener and leaves the native method untouched", async () => {
  const runtime = makeRuntime({ incomingOnly: true, maxLeaseMs: 100 });
  Object.defineProperty(runtime.embed, "postMessage", { configurable: false, enumerable: true, writable: false, value: runtime.original });
  const originalDescriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].status, "armed_incoming_only");
  await new Promise((resolve) => setTimeout(resolve, 180));
  assert.equal(runtime.observer.status.status, "expired");
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), originalDescriptor);
  assert.equal(runtime.embed._listeners.get("message")?.size || 0, 0);
});

test("fixed incoming and outgoing command allowlists ignore unknown aliases and unsupported commands", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  const unknown = vm.runInContext(`({cmd:5,command:7,param:["must-not-capture"],selector:"document.body"})`, runtime.context);
  runtime.embed.postMessage(unknown);
  runtime.embed.postMessage(vm.runInContext(`({cmd:6,param:["not-allowlisted"]})`, runtime.context));
  runtime.embed.dispatch("message", domMessage(vm.runInContext(`({command:20001,value:[17,"callback-text"],extra:"secret"})`, runtime.context)));
  runtime.embed.dispatch("message", domMessage(vm.runInContext(`({cmd:20002,param:["not-allowlisted"]})`, runtime.context)));
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].direction, "incoming");
  assert.equal(report.events[0].command, 20001);
  assert.equal(report.events[0].parameterItems[0].type, "number");
  assert.equal(report.events[0].parameterItems[1].ref, null);
  assert.equal(JSON.stringify(report).includes("callback-text"), false);
  await runtime.observer.stop();
});

test("object and absent parameters validate with the producer's fixed descriptor shapes", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:5,id:"object-param-id",param:{unknown:"do-not-retain"}})`, runtime.context));
  runtime.embed.postMessage(vm.runInContext(`({cmd:13,id:"absent-param-id"})`, runtime.context));
  const report = await runtime.observer.drain();
  assert.equal(isObserverReport(report), true);
  assert.equal(report.events.length, 2, JSON.stringify(report));
  assert.equal(report.events[0].parameterKind, "object");
  assert.deepEqual(report.events[0].parameterItems, [{ type: "object" }]);
  assert.equal(report.events[1].parameterKind, "absent");
  assert.deepEqual(report.events[1].parameterItems, []);
  assert.equal(JSON.stringify(report).includes("do-not-retain"), false);
  await runtime.observer.stop();
});

test("forged per-type descriptor extras are rejected and never copied into retained reports", async () => {
  for (const malformed of [
    { type: "string", length: 1, ref: null, value: "raw-secret" },
    { type: "boolean", value: true, ref: "raw-secret" },
  ]) {
    const runtime = makeRuntime();
    await runtime.observer.start();
    runtime.embed.postMessage(vm.runInContext(`({cmd:5,param:["x"]})`, runtime.context));
    const state = pageState(runtime.context);
    const cleanEvent = structuredClone(state.events[0]);
    state.events[0].parameterItems[0] = malformed;
    const report = await runtime.observer.drain();
    assert.equal(report.events.length, 0);
    assert.equal(report.droppedEvents, 1);
    assert.equal(report.incomplete, true);
    assert.equal(isObserverReport(report), true);
    assert.equal(JSON.stringify(report).includes("raw-secret"), false);
    const forgedReport = structuredClone(report);
    forgedReport.events = [cleanEvent];
    forgedReport.droppedEvents = 0;
    forgedReport.incomplete = false;
    forgedReport.encodedEventBytes = Buffer.byteLength(JSON.stringify(forgedReport.events), "utf8");
    assert.equal(isObserverReport(forgedReport), true);
    forgedReport.events[0].parameterItems[0] = malformed;
    assert.equal(isObserverReport(forgedReport), false, "exact per-type data keys reject raw fields before retention");
    await runtime.observer.stop();
  }
});

test("normal stop flushes queued projected events before renderer cleanup", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:7,id:"queued-id",param:["private-queued-text"]})`, runtime.context));
  const report = await runtime.observer.stop();
  assert.equal(report.status, "stopped");
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].command, 7);
  assert.equal(report.droppedEvents, 0);
  assert.equal(report.incomplete, false);
  assert.equal(report.queueLossPossible, false);
  assert.equal(isObserverReport(report), true);
  assert.equal(JSON.stringify(report).includes("private-queued-text"), false);
});

test("normal stop owns a held drain before cleanup and freezes the completed snapshot", async () => {
  let releaseDrain;
  let cleanupCalls = 0;
  const runtime = makeRuntime({ probeTimeoutMs: 500, observerNow: Date.now, execute(script, context) {
    if (script.includes("function rendererDrain")) {
      const result = JSON.parse(JSON.stringify(vm.runInContext(script, context)));
      return new Promise((resolve) => { releaseDrain = () => resolve(result); });
    }
    if (script.includes("function rendererCleanup")) cleanupCalls += 1;
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:7,id:"held-drain-id",param:["private-held-drain"]})`, runtime.context));

  const drainPromise = runtime.observer.drain();
  assert.equal(typeof releaseDrain, "function", "renderer drain has synchronously taken ownership of its queue");
  const stopPromise = runtime.observer.stop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cleanupCalls, 0, "stop waits for the in-flight drain before renderer cleanup");

  releaseDrain();
  const report = await stopPromise;
  const drainReport = await drainPromise;
  assert.equal(report.status, "stopped");
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].command, 7);
  assert.equal(report.queueLossPossible, false);
  assert.equal(report.incomplete, false);
  assert.equal(isObserverReport(report), true);
  assert.equal(JSON.stringify(report).includes("private-held-drain"), false);
  assert.equal(drainReport.events.length, 1);

  const frozenJson = JSON.stringify(report);
  assert.strictEqual(await runtime.observer.stop(), report, "repeated stop returns the cached terminal report");
  assert.strictEqual(runtime.observer.status, report, "status reads the same terminal snapshot");
  assert.equal(JSON.stringify(report), frozenJson);
});

test("lease autoexpiry waits for an in-flight drain before flushing and finalizing", async () => {
  let releaseDrain;
  let cleanupCalls = 0;
  let cleanupCountAtRelease = null;
  const leaseMs = 60;
  const runtime = makeRuntime({ maxLeaseMs: leaseMs, probeTimeoutMs: 500, observerNow: Date.now, execute(script, context) {
    if (script.includes("function rendererDrain")) {
      const result = JSON.parse(JSON.stringify(vm.runInContext(script, context)));
      return new Promise((resolve) => { releaseDrain = () => resolve(result); });
    }
    if (script.includes("function rendererCleanup")) cleanupCalls += 1;
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:5,id:"expiry-held-id",param:["private-expiry-held"]})`, runtime.context));
  const drainPromise = runtime.observer.drain();
  assert.equal(typeof releaseDrain, "function");

  await new Promise((resolve) => setTimeout(resolve, leaseMs + 30));
  assert.equal(cleanupCalls, 0, "autoexpiry remains behind the in-flight drain");
  cleanupCountAtRelease = cleanupCalls;
  releaseDrain();
  const settleDeadline = Date.now() + 1000;
  while (runtime.observer.status.status !== "expired" && Date.now() < settleDeadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const report = runtime.observer.status;
  await drainPromise;
  assert.equal(cleanupCountAtRelease, 0);
  assert.equal(report.status, "expired");
  assert.equal(cleanupCalls, 1);
  assert.equal(report.events.length, 1);
  assert.equal(report.queueLossPossible, false);
  assert.equal(report.incomplete, false);
  assert.equal(isObserverReport(report), true);
  assert.strictEqual(await runtime.observer.stop(), report);
});

test("timed-out pending drain declares possible loss and late completion cannot change final report", async () => {
  let releaseDrain;
  const timers = [];
  const runtime = makeRuntime({ maxLeaseMs: 5000, probeTimeoutMs: 500, observerNow: Date.now,
    setTimeoutFn(callback, delay) {
      const timer = { callback, delay, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimeoutFn(timer) { if (timer) timer.cancelled = true; },
    execute(script, context) {
    if (script.includes("function rendererDrain")) {
      const result = JSON.parse(JSON.stringify(vm.runInContext(script, context)));
      return new Promise((resolve) => { releaseDrain = () => resolve(result); });
    }
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:7,id:"timeout-held-id",param:["private-timeout-held"]})`, runtime.context));
  const drainPromise = runtime.observer.drain();
  assert.equal(typeof releaseDrain, "function");

  const stopPromise = runtime.observer.stop();
  while (timers.filter((timer) => !timer.cancelled).length < 2) await new Promise((resolve) => setImmediate(resolve));
  const stopDrainWait = timers.at(-1);
  assert.ok(stopDrainWait.delay > 0);
  stopDrainWait.callback();
  const report = await stopPromise;
  assert.equal(report.status, "stopped");
  assert.equal(report.events.length, 0, "the renderer already spliced the held batch before the stop wait expired");
  assert.equal(report.queueLossPossible, true, "an in-flight drain timeout cannot imply an empty queue");
  assert.equal(report.incomplete, true);
  assert.equal(isObserverReport(report), true);
  const finalJson = JSON.stringify(report);

  releaseDrain();
  await drainPromise;
  assert.strictEqual(runtime.observer.status, report);
  assert.strictEqual(await runtime.observer.stop(), report);
  assert.equal(JSON.stringify(report), finalJson, "late renderer completion cannot mutate the cached terminal report");
});

test("renderer lease expiry reports queued loss in the final drain instead of implying completeness", async () => {
  const runtime = makeRuntime({ maxLeaseMs: 1000 });
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:7,id:"expiry-id",param:["private-expiry-text"]})`, runtime.context));
  const leaseTimer = runtime.pageTimers.find((entry) => entry.delay >= 1000);
  assert.ok(leaseTimer);
  runtime.clock.now = Date.now() + 2000;
  leaseTimer.callback();
  const report = await runtime.observer.drain();
  assert.equal(report.status, "expired");
  assert.equal(report.events.length, 0);
  assert.equal(report.droppedEvents, 1);
  assert.equal(report.incomplete, true);
  assert.equal(report.queueLossPossible, false, "the renderer counted the known pending event exactly");
  assert.equal(isObserverReport(report), true);
});

test("the global event byte cap keeps accepted indices continuous when later records fit", async () => {
  const runtime = makeRuntime({ windowCount: 5 });
  await runtime.observer.start();
  const [first, , , , last] = runtime.windows;
  first.embed.postMessage(vm.runInContext(`({cmd:5,id:"large-source-id",param:Array(16).fill("${"x".repeat(8192)}")})`, first.context));
  last.embed.postMessage(vm.runInContext(`({cmd:7,id:"small-source-id"})`, last.context));
  const largeSeed = structuredClone(pageState(first.context).events[0]);
  const smallSeed = structuredClone(pageState(last.context).events[0]);
  for (const runtimeWindow of runtime.windows.slice(0, 4)) {
    const state = pageState(runtimeWindow.context);
    state.events = Array.from({ length: 70 }, (_, index) => ({
      ...structuredClone(largeSeed), eventIndex: index + 1, windowOrdinal: runtimeWindow.window === first.window ? 1 : runtime.windows.indexOf(runtimeWindow) + 1,
    }));
    state.totalBytes = Buffer.byteLength(JSON.stringify(state.events[0]), "utf8");
  }
  const lastState = pageState(last.context);
  lastState.events = [{ ...smallSeed, windowOrdinal: 5 }];
  lastState.totalBytes = Buffer.byteLength(JSON.stringify(lastState.events[0]), "utf8");

  const report = await runtime.observer.drain();
  assert.equal(report.incomplete, true);
  assert.ok(report.droppedEvents > 0);
  assert.equal(isObserverReport(report), true);
  assert.ok(report.events.length > 0);
  assert.deepEqual(report.events.map((event) => event.eventIndex), Array.from({ length: report.events.length }, (_, index) => index + 1));
  assert.equal(report.events.at(-1).command, 7, "a later small event can fit without inheriting a rejected index");
  await runtime.observer.stop();
});

test("destroying an armed renderer marks its unflushed queue as possibly lost", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:5,param:["private-pending"]})`, runtime.context));
  runtime.webContents.emit("destroyed");
  const report = await runtime.observer.stop();
  assert.equal(report.windows[0].status, "destroyed");
  assert.equal(report.queueLossPossible, true);
  assert.equal(report.incomplete, true);
  assert.equal(isObserverReport(report), true);
});

test("canonical AV route is exact; external, aliased, and ambiguous windows never execute renderer code", async () => {
  assert.equal(exactAVRoute(AV_URL), true);
  assert.equal(exactAVRoute("app://evil/renderer/index.html#/av"), false);
  assert.equal(exactAVRoute("app://./renderer/index.html/extra#/av"), false);
  assert.equal(exactAVRoute("app://./renderer/index.html?query#/main/message"), false);
  let externalExecutions = 0;
  const external = makeRuntime({ url: "https://example.invalid/renderer/index.html#/av", execute() { externalExecutions += 1; } });
  const externalReport = await external.observer.start();
  assert.equal(externalReport.windows[0].status, "not_av");
  assert.equal(externalExecutions, 0);
  await external.observer.stop();

  const ambiguous = makeRuntime({ embedCount: 2 });
  const ambiguousReport = await ambiguous.observer.start();
  assert.equal(ambiguousReport.windows[0].status, "ambiguous_embed");
  assert.equal(ambiguousReport.windows[0].postMessageAvailable, false);
  await ambiguous.observer.stop();

  const bridgeOnly = makeRuntime();
  bridgeOnly.context.native = { postMessage() { throw new Error("must not be called"); } };
  delete bridgeOnly.embed.postMessage;
  const bridgeReport = await bridgeOnly.observer.start();
  assert.equal(bridgeReport.windows[0].embedCount, 1);
  assert.equal(bridgeReport.windows[0].postMessageAvailable, false,
    "window.native is not evidence for the selected AV embed method");
  await bridgeOnly.observer.stop();
});

test("renderer navigation advances the document epoch and stale captures do not cross epochs", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:5,id:"same-id",param:["old-secret"]})`, runtime.context));
  const oldEpoch = 1;
  runtime.webContents.url = "app://./renderer/index.html?webcontentsid=5#/main/message";
  runtime.webContents.emit("did-start-navigation", {}, runtime.webContents.url, true, true);
  runtime.webContents.url = AV_URL;
  runtime.webContents.emit("dom-ready");
  const report = await runtime.observer.drain();
  assert.equal(report.windows[0].documentEpoch, 2);
  assert.equal(report.events.length, 0, "old-document pending data is not compared or attributed to the new epoch");
  assert.equal(report.incomplete, true);
  assert.ok(oldEpoch !== report.windows[0].documentEpoch);
  await runtime.observer.stop();
});

test("in-place route completion rearms only the exact main-frame AV epoch and captures its incoming event", async () => {
  const rootUrl = "app://./renderer/index.html?webcontentsid=5&processGroupNS=createBlankWindow1#/main/message";
  const runtime = makeRuntime({ url: rootUrl, embedCount: 0 });
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].status, "not_av");
  assert.equal(runtime.webContents.executeCount, 0, "the root route does not evaluate the observer script");

  runtime.webContents.emit("did-start-navigation", {}, rootUrl, true, true);
  runtime.webContents.url = AV_URL;
  runtime.webContents.emit("did-navigate-in-page", {}, AV_URL, true, 42, 99);
  await new Promise((resolve) => setTimeout(resolve, 80));
  runtime.setEmbedCount(1);
  const armDeadline = Date.now() + 1000;
  while (runtime.observer.status.windows[0].status !== "armed" && Date.now() < armDeadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(runtime.observer.status.windows[0].status, "armed");
  assert.equal(runtime.observer.status.windows[0].documentEpoch, 2, "did-navigate-in-page completes the epoch without incrementing it again");

  runtime.embed.dispatch("message", domMessage(JSON.stringify({ cmd: 20004, id: "mainframe-reply-secret", param: ["private-callback"] })));
  const report = await runtime.observer.drain();
  assert.equal(report.events.length, 1);
  assert.equal(report.events[0].command, 20004);
  assert.equal(report.events[0].documentEpoch, 2);
  assert.equal(JSON.stringify(report).includes("mainframe-reply-secret"), false);
  assert.equal(JSON.stringify(report).includes("private-callback"), false);

  const attemptsAfterArm = runtime.webContents.executeCount;
  runtime.webContents.emit("did-navigate-in-page", {}, AV_URL, true, 42, 99);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runtime.webContents.executeCount, attemptsAfterArm, "a duplicate completion cannot install twice in one epoch");
  await runtime.observer.stop();
  assert.equal(runtime.webContents.listenerCount("did-navigate-in-page"), 0);
});

test("in-place completion ignores subframes and never evaluates a noncanonical origin or route", async () => {
  const rootUrl = "app://./renderer/index.html?webcontentsid=5#/main/message";
  const subframe = makeRuntime({ url: rootUrl });
  await subframe.observer.start();
  subframe.webContents.emit("did-start-navigation", {}, rootUrl, true, true);
  subframe.webContents.url = AV_URL;
  subframe.webContents.emit("did-navigate-in-page", {}, AV_URL, false, 42, 99);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(subframe.webContents.executeCount, 0);
  assert.equal(subframe.observer.status.windows[0].documentEpoch, 2);
  assert.equal(subframe.observer.status.windows[0].status, "stale_context");
  await subframe.observer.stop();

  for (const url of ["https://example.invalid/#/av", "app://./renderer/index.html?webcontentsid=5#/main/message"]) {
    const runtime = makeRuntime({ url: rootUrl });
    await runtime.observer.start();
    runtime.webContents.emit("did-start-navigation", {}, rootUrl, true, true);
    runtime.webContents.url = url;
    runtime.webContents.emit("did-navigate-in-page", {}, url, true, 42, 99);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(runtime.webContents.executeCount, 0, `must not evaluate for ${url}`);
    assert.equal(runtime.observer.status.windows[0].status, "not_av");
    await runtime.observer.stop();
  }
});

test("bounded AV polling is cancelled by stop and cannot install after lease expiry", async () => {
  const stopped = makeRuntime({ embedCount: 0 });
  let installerCalls = 0;
  let activeInstallers = 0;
  let maxActiveInstallers = 0;
  const originalExecute = stopped.webContents.executeJavaScript;
  stopped.webContents.executeJavaScript = (script) => {
    if (!script.includes("function rendererInstall")) return originalExecute(script);
    installerCalls += 1;
    activeInstallers += 1;
    maxActiveInstallers = Math.max(maxActiveInstallers, activeInstallers);
    return Promise.resolve(originalExecute(script)).finally(() => { activeInstallers -= 1; });
  };
  const starting = stopped.observer.start();
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.ok(installerCalls >= 1, "the bounded AV poll checks for a delayed embed");
  assert.equal(maxActiveInstallers, 1, "only one renderer install executes at a time for the epoch");
  const callsAtStop = installerCalls;
  const stoppedReport = await stopped.observer.stop();
  await starting;
  stopped.setEmbedCount(1);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(installerCalls, callsAtStop, "stop cancels the retry timer");
  assert.equal(stopped.embed.postMessage, stopped.original);
  assert.equal(stoppedReport.cleanupComplete, true);

  const expired = makeRuntime({ embedCount: 0, maxLeaseMs: 100 });
  let expiredInstallerCalls = 0;
  const expiredExecute = expired.webContents.executeJavaScript;
  expired.webContents.executeJavaScript = (script) => {
    if (script.includes("function rendererInstall")) expiredInstallerCalls += 1;
    return expiredExecute(script);
  };
  const expiredStart = await expired.observer.start();
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(expired.observer.status.status, "expired");
  const finalCount = expiredInstallerCalls;
  expired.setEmbedCount(1);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(expiredInstallerCalls, finalCount, "lease expiry ends all polling");
  assert.equal(expired.embed.postMessage, expired.original);
  assert.notEqual(expiredStart.windows[0].status, "armed");
});

test("metadata route discovery arms an initially non-AV canonical renderer after it reaches exact AV", async () => {
  const loadingUrl = "app://./renderer/index.html?webcontentsid=5&processGroupNS=createBlankWindow1#/loading";
  const runtime = makeRuntime({ url: loadingUrl, nativeConditional: true });
  const descriptor = Object.getOwnPropertyDescriptor(runtime.embed, "postMessage");
  const started = await runtime.observer.start();
  assert.equal(started.windows[0].status, "not_av");
  assert.equal(runtime.webContents.executeCount, 0, "the initial non-AV route is never evaluated");
  assert.equal(runtime.debuggerApi.commands.length, 0, "native debugger setup waits for the exact AV route");
  runtime.webContents.emit("dom-ready");

  runtime.webContents.url = AV_URL;
  const armed = await waitForWindowStatus(runtime.observer, "armed_native_conditional");
  assert.equal(armed.status, "armed_native_conditional");
  assert.equal(armed.nativeConditionalStatus, "breakpoint_set");
  assert.equal(runtime.webContents.executeCount, 1);
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), descriptor);

  const breakpoint = runtime.debuggerApi.commands.find((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall");
  const condition = vm.runInContext(`(function(){return ${breakpoint.params.condition}})`, runtime.context);
  const outgoing = vm.runInContext(`({cmd:5,id:"delayed-route-secret",param:[]})`, runtime.context);
  assert.equal(Reflect.apply(condition, runtime.embed, [5, outgoing]), false);
  runtime.embed.dispatch("message", new MessageEvent("message", {
    data: JSON.stringify({ cmd: 20004, id: "delayed-route-reply-secret", param: ["private-reply"] }),
  }));
  const drained = await runtime.observer.drain();
  assert.deepEqual(drained.events.map((event) => [event.direction, event.command]), [["outgoing", 5], ["incoming", 20004]]);
  assert.equal(drained.windows[0].nativeCallAttempts, 1);
  for (const secret of ["delayed-route-secret", "delayed-route-reply-secret", "private-reply"]) {
    assert.equal(JSON.stringify(drained).includes(secret), false);
  }
  assert.deepEqual(Object.getOwnPropertyDescriptor(runtime.embed, "postMessage"), descriptor);
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("route discovery waits for main-frame readiness and never starts while navigation is pending", async () => {
  const loadingUrl = "app://./renderer/index.html?webcontentsid=5#/loading";
  const loading = makeRuntime({ url: loadingUrl, nativeConditional: true });
  await loading.observer.start();
  loading.setMainFrameLoading(true);
  loading.webContents.url = AV_URL;
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(loading.webContents.executeCount, 0, "exact route is not evaluated while the main frame is loading");
  assert.equal(loading.observer.status.windows[0].status, "not_av");
  loading.setMainFrameLoading(false);
  assert.equal((await waitForWindowStatus(loading.observer, "armed_native_conditional")).status, "armed_native_conditional");
  await loading.observer.stop();

  const pending = makeRuntime({ url: loadingUrl, nativeConditional: true });
  await pending.observer.start();
  pending.webContents.emit("did-start-navigation", {}, loadingUrl, true, true);
  pending.webContents.url = AV_URL;
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(pending.webContents.executeCount, 0, "an unfinished main-frame navigation cannot be polled into an install");
  assert.equal(pending.observer.status.windows[0].status, "stale_context");
  await pending.observer.stop();
});

test("route discovery is only eligible from a canonical renderer and cancels on stop, expiry, or destruction", async () => {
  const foreign = makeRuntime({ url: "https://example.invalid/#/loading", nativeConditional: true });
  await foreign.observer.start();
  foreign.webContents.url = AV_URL;
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(foreign.webContents.executeCount, 0, "a foreign initial origin never gains route-poll eligibility");
  assert.equal(foreign.observer.status.windows[0].status, "not_av");
  await foreign.observer.stop();

  const stopped = makeRuntime({ url: "app://./renderer/index.html#/loading", nativeConditional: true });
  await stopped.observer.start();
  stopped.webContents.url = AV_URL;
  const stopReport = await stopped.observer.stop();
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(stopped.webContents.executeCount, 0, "stop cancels the pending metadata timer");
  assert.equal(stopReport.cleanupComplete, true);

  const expired = makeRuntime({ url: "app://./renderer/index.html#/loading", nativeConditional: true, maxLeaseMs: 100 });
  await expired.observer.start();
  const expiryDeadline = Date.now() + 500;
  while (expired.observer.status.status !== "expired" && Date.now() < expiryDeadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(expired.observer.status.status, "expired");
  expired.webContents.url = AV_URL;
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(expired.webContents.executeCount, 0, "lease expiry prevents a late route install");

  const destroyed = makeRuntime({ url: "app://./renderer/index.html#/loading", nativeConditional: true });
  await destroyed.observer.start();
  destroyed.webContents.url = AV_URL;
  destroyed.webContents.destroyed = true;
  destroyed.webContents.emit("destroyed");
  await new Promise((resolve) => setTimeout(resolve, 320));
  assert.equal(destroyed.webContents.executeCount, 0, "destroyed contents cannot be evaluated after timer cancellation");
  assert.equal(destroyed.observer.status.windows[0].status, "destroyed");
  await destroyed.observer.stop();
});

test("in-place navigation without a completion event stays an explicit coverage gap", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  runtime.webContents.url = "app://./renderer/index.html?webcontentsid=5#/main/message";
  runtime.webContents.emit("did-start-navigation", {}, runtime.webContents.url, true, true);
  const report = await runtime.observer.drain();
  assert.equal(report.coverageGap, true);
  assert.equal(report.incomplete, true);
  assert.equal(report.windows[0].status, "stale_context");
  assert.equal(report.windows[0].documentEpoch, 2);
  await runtime.observer.stop();
});

test("pending navigation cleanup completes before rearming and cannot overwrite a newer epoch", async () => {
  let releaseCleanup;
  let releaseDrain;
  let holdFirstCleanup = true;
  const installEpochs = [];
  const runtime = makeRuntime({ execute(script, context) {
    if (script.includes("function rendererInstall")) {
      const match = script.match(/\)\("[a-f0-9]{32}",\d+,(\d+),/);
      installEpochs.push(Number(match?.[1]));
    }
    if (script.includes("function rendererCleanup") && holdFirstCleanup) {
      holdFirstCleanup = false;
      return new Promise((resolve) => { releaseCleanup = () => {
        const result = vm.runInContext(script, context);
        resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
      }; });
    }
    if (script.includes("function rendererDrain")) {
      const result = vm.runInContext(script, context);
      return new Promise((resolve) => { releaseDrain = () => resolve(JSON.parse(JSON.stringify(result))); });
    }
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  await runtime.observer.start();
  runtime.embed.postMessage(vm.runInContext(`({cmd:5,id:"old-epoch-secret",param:["old-private-value"]})`, runtime.context));
  const oldDrainPromise = runtime.observer.drain();
  while (!releaseDrain) await new Promise((resolve) => setImmediate(resolve));

  runtime.webContents.url = "app://./renderer/index.html?webcontentsid=5#/main/message";
  runtime.webContents.emit("did-start-navigation", {}, runtime.webContents.url, true, true);
  runtime.webContents.url = AV_URL;
  runtime.webContents.emit("dom-ready");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(installEpochs.length, 1, "the next epoch waits for cleanup of the previous wrapper");

  releaseCleanup();
  const armDeadline = Date.now() + 1000;
  while (!installEpochs.includes(2) && Date.now() < armDeadline) await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(installEpochs, [1, 2]);
  assert.equal(Object.getOwnPropertySymbols(runtime.context).filter((symbol) =>
    Symbol.keyFor(symbol)?.startsWith("qqcall.official.av.observer.cancelled.")).length, 1,
  "navigation reuses one run-scoped cancellation marker for this renderer context");
  releaseDrain();
  const lateReport = await oldDrainPromise;
  assert.equal(lateReport.windows[0].documentEpoch, 2);
  assert.equal(lateReport.windows[0].status, "armed", "late old-epoch drain cannot overwrite the new armed state");
  assert.equal(lateReport.events.length, 0);
  assert.equal(lateReport.incomplete, true);
  assert.equal((await runtime.observer.stop()).cleanupComplete, true);
});

test("lease stop tombstones a delayed install and expiry is checked inside the renderer", async () => {
  let heldInstall;
  let contextRef;
  const runtime = makeRuntime({ execute(script, context, embed) {
    contextRef = context;
    if (script.includes("function rendererInstall")) return new Promise((resolve) => { heldInstall = () => {
      const result = vm.runInContext(script, context);
      resolve(JSON.parse(JSON.stringify(result)));
    }; });
    const result = vm.runInContext(script, context);
    return Promise.resolve(result === undefined ? undefined : JSON.parse(JSON.stringify(result)));
  } });
  const startPending = runtime.observer.start();
  while (!heldInstall) await new Promise((resolve) => setImmediate(resolve));
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.status, "stopped");
  heldInstall();
  await startPending;
  assert.equal(runtime.embed.postMessage, runtime.original, "a rejected delayed installer never replaces the SDK method");
  assert.equal(pageState(contextRef), null);

  const expired = makeRuntime({ execute: async (script, context) => {
    if (script.includes("function rendererInstall")) await new Promise((resolve) => setTimeout(resolve, 10));
    const result = vm.runInContext(script, context);
    return result === undefined ? undefined : JSON.parse(JSON.stringify(result));
  } });
  const expiredObserver = createOfficialAVObserver({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction: Object.assign((name) => name === "electron" ? { app: expired.app, BrowserWindow: Object.assign(function BrowserWindow() {}, { getAllWindows: () => [expired.window] }) } : null,
      { resolve: () => "electron" }),
    maxLeaseMs: 2,
  });
  const expiredReport = await expiredObserver.start();
  assert.equal(expired.embed.postMessage, expired.original, "the original method is unchanged when installation runs after expiry");
  await expiredObserver.stop();
  assert.equal(expiredReport.windows[0].status === "armed", false);
});

test("replacement ownership is preserved and cumulative event bytes stay below the report cap", async () => {
  const runtime = makeRuntime();
  await runtime.observer.start();
  for (let index = 0; index < 220; index += 1) {
    const payload = vm.runInContext(`({cmd:5,param:Array(16).fill("${"x".repeat(8000)}")})`, runtime.context);
    runtime.embed.postMessage(payload);
  }
  const externalReplacement = function replacement() { return "keep"; };
  runtime.embed.postMessage = externalReplacement;
  const report = await runtime.observer.drain();
  assert.ok(report.events.length <= 256);
  assert.ok(report.encodedEventBytes <= MAX_EVENT_BYTES);
  assert.ok(Buffer.byteLength(JSON.stringify(report), "utf8") <= MAX_REPORT_BYTES);
  assert.equal(report.incomplete, true);
  const stopped = await runtime.observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(runtime.embed.postMessage, externalReplacement, "cleanup never overwrites a later owner");
});

test("observer report validator rejects forged raw event fields and oversized frames", async () => {
  const runtime = makeRuntime();
  const report = await runtime.observer.start();
  const forged = structuredClone(report);
  forged.rawPayload = "token-secret";
  assert.equal(isObserverReport(forged), false);
  const tooLarge = structuredClone(report);
  tooLarge.events = Array.from({ length: 257 }, () => ({}));
  assert.equal(isObserverReport(tooLarge), false);
  await runtime.observer.stop();
});
