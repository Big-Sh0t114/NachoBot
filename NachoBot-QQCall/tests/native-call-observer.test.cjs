"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const {
  buildNativeConditionalExpression,
  buildNativeTargetExpression,
  createNativeCallObserver,
  stateSymbolKey,
} = require("../agent/native-call-observer.cjs");

const RUN_ID = "0123456789abcdef0123456789abcdef";
const ORDINAL = 2;
const EPOCH = 3;

class FakeDebugger extends EventEmitter {
  constructor({ initiallyAttached = false, command = null, attachThrows = false } = {}) {
    super();
    this.attached = initiallyAttached;
    this.attachThrows = attachThrows;
    this.attachCalls = 0;
    this.detachCalls = 0;
    this.commands = [];
    this.command = command;
  }

  isAttached() { return this.attached; }

  attach(version) {
    this.attachCalls += 1;
    assert.equal(version, "1.3");
    if (this.attachThrows) throw new Error("private-test-sentinel");
    this.attached = true;
  }

  detach() {
    this.detachCalls += 1;
    if (!this.attached) throw new Error("unexpected-detach");
    this.attached = false;
    this.emit("detach", {}, "target_closed");
  }

  sendCommand(method, params) {
    this.commands.push({ method, params });
    if (this.command) return this.command(method, params, this);
    if (method === "Runtime.evaluate") return Promise.resolve({ result: { type: "function", objectId: "owned-object-1" } });
    if (method === "Debugger.setBreakpointOnFunctionCall") return Promise.resolve({ breakpointId: "owned-breakpoint-1" });
    return Promise.resolve({});
  }
}

function create(debuggerApi, options = {}) {
  return createNativeCallObserver({
    debuggerApi,
    runId: RUN_ID,
    ordinal: ORDINAL,
    epoch: EPOCH,
    timeoutMs: 100,
    ...options,
  });
}

test("an already attached debugger is left untouched", async () => {
  const debuggerApi = new FakeDebugger({ initiallyAttached: true });
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "debugger_already_attached");
  assert.equal(started.breakpointSet, false);
  assert.equal(started.cleanupComplete, true);
  assert.equal(debuggerApi.attachCalls, 0);
  assert.equal(debuggerApi.commands.length, 0);
  assert.equal(debuggerApi.listenerCount("message"), 0);
  assert.equal((await observer.stop()).cleanupComplete, true);
  assert.equal(debuggerApi.detachCalls, 0);
});

test("attach failure does not send commands or detach an unowned target", async () => {
  const debuggerApi = new FakeDebugger({ attachThrows: true });
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "attach_failed");
  assert.equal(debuggerApi.commands.length, 0);
  assert.equal(debuggerApi.detachCalls, 0);
  assert.equal((await observer.stop()).cleanupComplete, true);
});

test("known Electron attach messages map to fixed statuses without cleanup actions", async () => {
  const cases = [
    ["Requested protocol version is not supported", "attach_protocol_unsupported"],
    ["No target available", "attach_no_target"],
    ["Debugger is already attached to the target", "attach_target_retained"],
  ];
  for (const [message, expected] of cases) {
    const debuggerApi = new FakeDebugger();
    debuggerApi.attach = function (version) {
      this.attachCalls += 1;
      assert.equal(version, "1.3");
      throw new Error(message);
    };
    const observer = create(debuggerApi);
    const started = await observer.start();
    assert.equal(started.status, expected);
    assert.equal(started.cleanupComplete, true);
    assert.equal(JSON.stringify(started).includes(message), false);
    assert.equal(debuggerApi.commands.length, 0);
    assert.equal(debuggerApi.detachCalls, 0);
    assert.equal(debuggerApi.listenerCount("message"), 0);
    assert.equal((await observer.stop()).cleanupComplete, true);
  }
});

test("unknown attach errors are redacted and an accessor message is never read", async () => {
  const secret = "private-attach-error-sentinel";
  const debuggerApi = new FakeDebugger();
  debuggerApi.attach = function () { throw new Error(secret); };
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "attach_failed");
  assert.equal(JSON.stringify(started).includes(secret), false);

  let getterReads = 0;
  const accessorError = new Error();
  Object.defineProperty(accessorError, "message", {
    configurable: true,
    get() { getterReads += 1; return "Requested protocol version is not supported"; },
  });
  const accessorDebugger = new FakeDebugger();
  accessorDebugger.attach = function () { throw accessorError; };
  const accessorObserver = create(accessorDebugger);
  const accessorStarted = await accessorObserver.start();
  assert.equal(accessorStarted.status, "attach_failed");
  assert.equal(getterReads, 0);
  assert.equal(JSON.stringify(accessorStarted).includes("Requested protocol version"), false);
  assert.equal(accessorDebugger.detachCalls, 0);
});

test("returned attach without a confirmed connection remains cleanup-unknown", async () => {
  const debuggerApi = new FakeDebugger();
  debuggerApi.attach = function (version) {
    this.attachCalls += 1;
    assert.equal(version, "1.3");
    // Simulate Electron returning without a confirmed debugger connection.
  };
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "attach_not_connected");
  assert.equal(started.cleanupComplete, false);
  const stopped = await observer.stop();
  assert.equal(stopped.status, "attach_not_connected");
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.detachCalls, 0);
  assert.equal(debuggerApi.commands.length, 0);
});

test("returned attach followed by an isAttached exception stays unknown and is not detached", async () => {
  const debuggerApi = new FakeDebugger();
  let attachedQueries = 0;
  debuggerApi.isAttached = function () {
    attachedQueries += 1;
    if (attachedQueries === 1) return false;
    throw new Error("raw-is-attached-error-sentinel");
  };
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "attach_state_unknown");
  assert.equal(started.cleanupComplete, false);
  assert.equal(JSON.stringify(started).includes("raw-is-attached-error-sentinel"), false);
  const stopped = await observer.stop();
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.detachCalls, 0);
  assert.equal(debuggerApi.commands.length, 0);
});

test("skip-all-pauses failure rejects the mode and cleans only its attachment", async () => {
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Debugger.setSkipAllPauses") return Promise.reject(new Error("private-test-sentinel"));
    return method === "Runtime.evaluate" ? { result: { type: "function", objectId: "owned-object-1" } } : {};
  } });
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "skip_all_pauses_unsupported");
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall"), false);
  const stopped = await observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(debuggerApi.detachCalls, 1);
  assert.deepEqual(debuggerApi.commands.filter((entry) => entry.method === "Debugger.setSkipAllPauses").map((entry) => entry.params), [{ skip: true }]);
});

test("no exact state function object rejects setup without claiming a breakpoint", async () => {
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Runtime.evaluate") return { result: { type: "object", objectId: "wrong-type" } };
    return {};
  } });
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "target_unavailable");
  assert.equal(started.breakpointSet, false);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall"), false);
  assert.equal((await observer.stop()).cleanupComplete, true);
});

test("a set breakpoint with zero callbacks reports setup only and remains a coverage gap", async () => {
  const debuggerApi = new FakeDebugger();
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "breakpoint_set");
  assert.equal(started.breakpointSet, true);
  assert.equal(started.pausedEvents, 0);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.setSkipAllPauses" && entry.params.skip === true), true);
  const evaluation = debuggerApi.commands.find((entry) => entry.method === "Runtime.evaluate").params;
  const expression = evaluation.expression;
  const expectedObjectGroup = `qqcall-official-av-native-conditional-${RUN_ID}-${ORDINAL}-${EPOCH}`;
  assert.equal(evaluation.objectGroup, expectedObjectGroup);
  const condition = debuggerApi.commands.find((entry) => entry.method === "Debugger.setBreakpointOnFunctionCall").params.condition;
  assert.equal(expression, buildNativeTargetExpression(RUN_ID, ORDINAL, EPOCH));
  assert.equal(condition, buildNativeConditionalExpression(RUN_ID, EPOCH));
  const stopped = await observer.stop();
  assert.equal(stopped.breakpointSet, true);
  assert.equal(stopped.cleanupComplete, true);
  assert.deepEqual(debuggerApi.commands.filter((entry) => entry.method === "Debugger.removeBreakpoint").map((entry) => entry.params), [
    { breakpointId: "owned-breakpoint-1" },
  ]);
  assert.deepEqual(debuggerApi.commands.filter((entry) => entry.method === "Runtime.releaseObjectGroup").map((entry) => entry.params), [
    { objectGroup: expectedObjectGroup },
  ]);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("fixed target and condition bind only to the exact epoch state and project receiver plus arguments", () => {
  const context = vm.createContext({});
  const key = stateSymbolKey(RUN_ID, EPOCH);
  const embed = vm.runInContext("({})", context);
  const original = vm.runInContext("(function(){return 'original'})", context);
  const seen = [];
  const state = {
    schema: "qqcall.official.av.observer.page.v1",
    runId: RUN_ID,
    ordinal: ORDINAL,
    epoch: EPOCH,
    nativeConditional: true,
    active: true,
    original,
    observeNativeAttempt(receiver, args) {
      const length = Object.getOwnPropertyDescriptor(args, "length");
      const first = Object.getOwnPropertyDescriptor(args, "0");
      const second = Object.getOwnPropertyDescriptor(args, "1");
      seen.push({
        receiverMatches: receiver === embed,
        argc: length.value,
        firstType: typeof first.value,
        secondType: typeof second.value,
      });
    },
  };
  Object.defineProperty(context, Symbol.for(key), { configurable: true, enumerable: false, value: state });
  context.expectedOriginal = original;
  context.targetExpression = buildNativeTargetExpression(RUN_ID, ORDINAL, EPOCH);
  context.conditionExpression = buildNativeConditionalExpression(RUN_ID, EPOCH);
  assert.equal(vm.runInContext("eval(targetExpression) === expectedOriginal", context), true);
  assert.equal(vm.runInContext(`eval(${JSON.stringify(buildNativeTargetExpression(RUN_ID, ORDINAL + 1, EPOCH))})`, context), null);
  const invokeCondition = vm.runInContext("(function(){return eval(conditionExpression)})", context);
  const falseResult = invokeCondition.call(embed, { cmd: 4 }, "extra-marker");
  assert.equal(falseResult, false, "condition remains false even when the observer runs");
  assert.deepEqual(seen, [{ receiverMatches: true, argc: 2, firstType: "object", secondType: "string" }]);
  assert.equal(state.original, original);
});

test("unexpected pause is immediately resumed and aborts setup before target evaluation", async () => {
  let debuggerApi;
  debuggerApi = new FakeDebugger({ command(method, _params, current) {
    if (method === "Debugger.setSkipAllPauses") {
      queueMicrotask(() => current.emit("message", {}, "Debugger.paused", { reason: "not-retained" }));
      return Promise.resolve({});
    }
    return {};
  } });
  const observer = create(debuggerApi);
  const started = await observer.start();
  assert.equal(started.status, "unexpected_pause");
  assert.equal(started.pausedEvents, 1);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.resume"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Runtime.evaluate"), false);
  const stopped = await observer.stop();
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.disable"), true);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("an unexpected pause after arming schedules bounded observer teardown immediately", async () => {
  let aborts = 0;
  const debuggerApi = new FakeDebugger();
  const observer = create(debuggerApi, { onAbort() { aborts += 1; } });
  assert.equal((await observer.start()).status, "breakpoint_set");
  debuggerApi.emit("message", {}, "Debugger.paused", { reason: "not-retained" });
  const stopped = await observer.stop();
  assert.equal(aborts, 1);
  assert.equal(stopped.status, "unexpected_pause");
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.resume"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.removeBreakpoint"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Runtime.releaseObjectGroup"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.disable"), true);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("a never-settling resume command still enters bounded teardown", async () => {
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Debugger.resume") return new Promise(() => {});
    if (method === "Runtime.evaluate") return { result: { type: "function", objectId: "owned-object" } };
    if (method === "Debugger.setBreakpointOnFunctionCall") return { breakpointId: "owned-breakpoint" };
    return {};
  } });
  const observer = create(debuggerApi, { timeoutMs: 80 });
  assert.equal((await observer.start()).status, "breakpoint_set");
  const startedAt = Date.now();
  debuggerApi.emit("message", {}, "Debugger.paused", { reason: "not-retained" });
  const stopped = await observer.stop();
  assert.ok(Date.now() - startedAt < 700, "resume timeout is bounded and teardown cannot hang");
  assert.equal(stopped.status, "unexpected_pause");
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("foreign debugger takeover permanently revokes ownership and receives no cleanup command", async () => {
  const debuggerApi = new FakeDebugger();
  const observer = create(debuggerApi);
  assert.equal((await observer.start()).status, "breakpoint_set");
  debuggerApi.attached = false;
  debuggerApi.emit("detach", {}, "devtools_closed");
  debuggerApi.attached = true;
  const countAtTakeover = debuggerApi.commands.length;
  const stopped = await observer.stop();
  assert.equal(stopped.status, "debugger_detached");
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.commands.length, countAtTakeover);
  assert.equal(debuggerApi.detachCalls, 0);
});

test("navigation invalidation cleans the still-owned debugger attachment", async () => {
  const debuggerApi = new FakeDebugger();
  const observer = create(debuggerApi);
  assert.equal((await observer.start()).status, "breakpoint_set");
  const invalidated = await observer.invalidate();
  assert.equal(invalidated.cleanupComplete, true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.removeBreakpoint"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Runtime.releaseObjectGroup"), true);
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Debugger.disable"), true);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("removing a listener that triggers foreign detach never cleans up the replacement", async () => {
  const debuggerApi = new FakeDebugger();
  const observer = create(debuggerApi);
  assert.equal((await observer.start()).status, "breakpoint_set");
  const originalRemove = debuggerApi.removeListener.bind(debuggerApi);
  let takeover = true;
  debuggerApi.removeListener = function (eventName, listener) {
    const result = originalRemove(eventName, listener);
    if (eventName === "message" && takeover) {
      takeover = false;
      this.attached = false;
      this.emit("detach", {}, "foreign_takeover");
      this.attached = true;
    }
    return result;
  };
  const stopped = await observer.stop();
  assert.equal(stopped.status, "debugger_detached");
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.detachCalls, 0);
  assert.equal(takeover, false);
  assert.equal(debuggerApi.listenerCount("detach"), 0);
});

test("late Runtime.evaluate result is released on the same owned attachment", async () => {
  let resolveEvaluation;
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Runtime.evaluate") return new Promise((resolve) => { resolveEvaluation = resolve; });
    return {};
  } });
  const observer = create(debuggerApi, { timeoutMs: 15 });
  const started = await observer.start();
  assert.equal(started.status, "timed_out");
  setTimeout(() => resolveEvaluation({ result: { type: "function", objectId: "late-owned-object" } }), 5);
  const stopped = await observer.stop({ deadline: Date.now() + 250 });
  assert.equal(debuggerApi.commands.some((entry) => entry.method === "Runtime.releaseObjectGroup"), true);
  assert.equal(stopped.cleanupComplete, true);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("late breakpoint result is removed before the owned debugger is detached", async () => {
  let resolveBreakpoint;
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Debugger.setBreakpointOnFunctionCall") return new Promise((resolve) => { resolveBreakpoint = resolve; });
    return method === "Runtime.evaluate" ? { result: { type: "function", objectId: "owned-object" } } : {};
  } });
  const observer = create(debuggerApi, { timeoutMs: 15 });
  assert.equal((await observer.start()).status, "timed_out");
  setTimeout(() => resolveBreakpoint({ breakpointId: "late-breakpoint" }), 5);
  const stopped = await observer.stop({ deadline: Date.now() + 250 });
  assert.deepEqual(debuggerApi.commands.filter((entry) => entry.method === "Debugger.removeBreakpoint").map((entry) => entry.params), [
    { breakpointId: "late-breakpoint" },
  ]);
  assert.equal(stopped.cleanupComplete, true);
});

test("breakpoint removal failure is reported as cleanup incomplete", async () => {
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Debugger.removeBreakpoint") return Promise.reject(new Error("private-test-sentinel"));
    return method === "Runtime.evaluate" ? { result: { type: "function", objectId: "owned-object" } } :
      method === "Debugger.setBreakpointOnFunctionCall" ? { breakpointId: "owned-breakpoint" } : {};
  } });
  const observer = create(debuggerApi);
  await observer.start();
  const stopped = await observer.stop();
  assert.equal(stopped.status, "cleanup_incomplete");
  assert.equal(stopped.cleanupComplete, false);
  assert.equal(debuggerApi.detachCalls, 1);
});

test("destruction revokes a pending stop before its next command or detach", async () => {
  let resolveDisable;
  const debuggerApi = new FakeDebugger({ command(method) {
    if (method === "Debugger.disable") return new Promise((resolve) => { resolveDisable = resolve; });
    return method === "Runtime.evaluate" ? { result: { type: "function", objectId: "owned-object" } } :
      method === "Debugger.setBreakpointOnFunctionCall" ? { breakpointId: "owned-breakpoint" } : {};
  } });
  const observer = create(debuggerApi, { timeoutMs: 500 });
  assert.equal((await observer.start()).status, "breakpoint_set");

  const pendingStop = observer.stop({ deadline: Date.now() + 500 });
  while (!resolveDisable) await new Promise((resolve) => setImmediate(resolve));
  const commandsAtPendingDisable = debuggerApi.commands.length;

  const destroyedStop = observer.stop({ destroyed: true });
  assert.equal(observer.status.cleanupComplete, false, "destroyed target cleanup remains explicitly unknown");
  resolveDisable({});
  await Promise.all([pendingStop, destroyedStop]);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(debuggerApi.commands.length, commandsAtPendingDisable, "destroyed target cannot authorize later teardown commands");
  assert.equal(debuggerApi.detachCalls, 0, "destroyed/replacement target is never detached by the stale owner");
  assert.equal(observer.status.cleanupComplete, false);
});
