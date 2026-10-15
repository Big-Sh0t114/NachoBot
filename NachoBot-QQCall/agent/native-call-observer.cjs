"use strict";

const MAX_SETUP_MS = 3000;
const MAX_BREAKPOINT_ID_LENGTH = 256;
const OWNED_OBJECT_GROUP_PREFIX = "qqcall-official-av-native-conditional";
const STATE_SCHEMA = "qqcall.official.av.observer.page.v1";
const STATE_SYMBOL_PREFIX = "qqcall.official.av.observer.";

const OUTCOMES = new Set([
  "not_started", "breakpoint_set", "debugger_unavailable", "debugger_already_attached",
  "attach_failed", "attach_protocol_unsupported", "attach_no_target", "attach_target_retained",
  "attach_not_connected", "attach_state_unknown", "debugger_enable_failed", "pause_policy_failed", "skip_all_pauses_unsupported",
  "target_unavailable", "breakpoint_rejected", "timed_out", "unexpected_pause", "debugger_detached",
  "cleanup_incomplete",
]);

function classifyAttachError(error) {
  let message;
  try {
    if (!error || (typeof error !== "object" && typeof error !== "function")) return "attach_failed";
    const descriptor = Object.getOwnPropertyDescriptor(error, "message");
    if (!descriptor || !Object.hasOwn(descriptor, "value") || typeof descriptor.value !== "string") return "attach_failed";
    message = descriptor.value;
  } catch { return "attach_failed"; }
  if (message === "Requested protocol version is not supported") return "attach_protocol_unsupported";
  if (message === "No target available") return "attach_no_target";
  if (message === "Debugger is already attached to the target") return "attach_target_retained";
  return "attach_failed";
}

function stateSymbolKey(runId, epoch) {
  if (typeof runId !== "string" || !/^[a-f0-9]{32}$/.test(runId) ||
      !Number.isSafeInteger(epoch) || epoch < 1 || epoch > 1000000) throw new TypeError("invalid observer state binding");
  return `${STATE_SYMBOL_PREFIX}${runId}.${epoch}`;
}

function buildNativeTargetExpression(runId, ordinal, epoch) {
  const key = stateSymbolKey(runId, epoch);
  if (!Number.isSafeInteger(ordinal) || ordinal < 1 || ordinal > 16) throw new TypeError("invalid observer window binding");
  return `(()=>{try{const __d=Object.getOwnPropertyDescriptor(globalThis,Symbol.for(${JSON.stringify(key)}));if(!__d||!Object.hasOwn(__d,"value"))return null;const __s=__d.value;const __own=(o,k)=>{try{const d=Object.getOwnPropertyDescriptor(o,k);return d&&Object.hasOwn(d,"value")?d.value:void 0}catch{return void 0}};if(__own(__s,"schema")!==${JSON.stringify(STATE_SCHEMA)}||__own(__s,"runId")!==${JSON.stringify(runId)}||__own(__s,"ordinal")!==${ordinal}||__own(__s,"epoch")!==${epoch}||__own(__s,"nativeConditional")!==true||__own(__s,"active")!==true)return null;const __f=__own(__s,"original");return typeof __f==="function"?__f:null}catch{return null}})()`;
}

function buildNativeConditionalExpression(runId, epoch) {
  const key = stateSymbolKey(runId, epoch);
  return `(()=>{try{const __d=Object.getOwnPropertyDescriptor(globalThis,Symbol.for(${JSON.stringify(key)}));const __s=__d&&Object.hasOwn(__d,"value")?__d.value:null;const __f=__s&&Object.getOwnPropertyDescriptor(__s,"observeNativeAttempt");if(__f&&Object.hasOwn(__f,"value")&&typeof __f.value==="function")__f.value(this,arguments)}catch(_){}return false})()`;
}

function safeStatus(value) {
  return OUTCOMES.has(value) ? value : "cleanup_incomplete";
}

function createNativeCallObserver({
  debuggerApi,
  runId,
  ordinal,
  epoch,
  now = Date.now,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  timeoutMs = MAX_SETUP_MS,
  onAbort = null,
} = {}) {
  const key = stateSymbolKey(runId, epoch);
  const objectGroup = `${OWNED_OBJECT_GROUP_PREFIX}-${runId}-${ordinal}-${epoch}`;
  const targetExpression = buildNativeTargetExpression(runId, ordinal, epoch);
  const condition = buildNativeConditionalExpression(runId, epoch);
  timeoutMs = Number.isSafeInteger(timeoutMs) ? Math.min(Math.max(timeoutMs, 1), MAX_SETUP_MS) : MAX_SETUP_MS;

  let outcome = "not_started";
  let attachedByUs = false;
  let ownershipLost = false;
  let detachingByUs = false;
  let listenerInstalled = false;
  let debuggerEnabledAttempted = false;
  let objectGroupUsed = false;
  let breakpointId = null;
  let breakpointInstalled = false;
  let cleanupComplete = null;
  let pausedEvents = 0;
  let abortRequested = false;
  let started = false;
  let stopping = false;
  let finalized = false;
  let destroyedTarget = false;
  let ownerToken = null;
  let startPromise = null;
  let stopPromise = null;
  let finalSnapshot = null;
  let setupDeadline = 0;
  const pendingCommandResults = new Set();

  const snapshot = () => finalSnapshot || Object.freeze({
    status: safeStatus(outcome),
    breakpointSet: breakpointInstalled,
    pausedEvents: Number.isSafeInteger(pausedEvents) ? Math.min(pausedEvents, 1000000) : 0,
    cleanupComplete: typeof cleanupComplete === "boolean" ? cleanupComplete : null,
  });

  function detachListeners() {
    if (!listenerInstalled) return true;
    let ok = true;
    try { debuggerApi.removeListener("detach", onDetach); } catch { ok = false; }
    try { debuggerApi.removeListener("message", onMessage); } catch { ok = false; }
    listenerInstalled = false;
    return ok;
  }

  function loseOwnership(code = "debugger_detached") {
    if (ownershipLost) return;
    ownershipLost = true;
    attachedByUs = false;
    if (outcome === "breakpoint_set" || debuggerEnabledAttempted || objectGroupUsed || breakpointId) {
      cleanupComplete = false;
    }
    if (!detachingByUs) outcome = code;
    detachListeners();
  }

  function isOwned(token = ownerToken) {
    if (!token || token !== ownerToken || !attachedByUs || ownershipLost || finalized || destroyedTarget) return false;
    let attached = false;
    try { attached = debuggerApi.isAttached() === true; } catch { attached = false; }
    if (!attached) loseOwnership();
    return attached && attachedByUs && !ownershipLost && token === ownerToken;
  }

  function sendBounded(method, params, deadline, token, lateKind = null) {
    if (!isOwned(token)) return Promise.resolve({ kind: "ownership_lost" });
    const remaining = deadline - now();
    if (remaining <= 0) return Promise.resolve({ kind: "timed_out" });
    let raw;
    try { raw = Promise.resolve(debuggerApi.sendCommand(method, params)); }
    catch { return Promise.resolve({ kind: "rejected" }); }
    let timedOut = false;
    const tracked = raw.then((value) => {
      if (!timedOut) return;
      if (finalized) { cleanupComplete = false; return; }
      if (lateKind === "evaluate") objectGroupUsed = true;
      if (lateKind === "breakpoint") {
        const id = validBreakpointResult(value);
        if (id) breakpointId = id;
        else cleanupComplete = false;
      }
    }, () => {
      if (timedOut) cleanupComplete = false;
    });
    pendingCommandResults.add(tracked);
    tracked.then(() => pendingCommandResults.delete(tracked), () => pendingCommandResults.delete(tracked));
    return new Promise((resolve) => {
      let settled = false;
      const timer = setTimeoutFn(() => {
        if (settled) return;
        settled = true;
        timedOut = true;
        if (["Debugger.removeBreakpoint", "Runtime.releaseObjectGroup", "Debugger.disable"].includes(method)) cleanupComplete = false;
        resolve({ kind: "timed_out" });
      }, remaining);
      raw.then((value) => {
        if (settled) return;
        settled = true;
        clearTimeoutFn(timer);
        resolve(isOwned(token) ? { kind: "resolved", value } : { kind: "ownership_lost" });
      }, () => {
        if (settled) return;
        settled = true;
        clearTimeoutFn(timer);
        resolve(isOwned(token) ? { kind: "rejected" } : { kind: "ownership_lost" });
      });
    });
  }

  function onDetach() {
    if (detachingByUs) {
      attachedByUs = false;
      ownershipLost = true;
      return;
    }
    loseOwnership("debugger_detached");
  }

  function onMessage(_event, method) {
    if (method !== "Debugger.paused" || !isOwned()) return;
    pausedEvents = Math.min(1000000, pausedEvents + 1);
    abortRequested = true;
    outcome = "unexpected_pause";
    const token = ownerToken;
    try { if (typeof onAbort === "function") onAbort(); } catch { /* parent receives only the fixed failure status */ }
    const resumeDeadline = now() + Math.min(timeoutMs, 250);
    void sendBounded("Debugger.resume", {}, resumeDeadline, token).then((result) => {
      if (result.kind !== "resolved") cleanupComplete = false;
    }).finally(() => {
      void stop({ deadline: now() + timeoutMs });
    });
  }

  function installListeners() {
    if (!debuggerApi || typeof debuggerApi.on !== "function" || typeof debuggerApi.removeListener !== "function") return false;
    try {
      debuggerApi.on("detach", onDetach);
      debuggerApi.on("message", onMessage);
      listenerInstalled = true;
      return true;
    } catch {
      try { debuggerApi.removeListener("detach", onDetach); } catch {}
      try { debuggerApi.removeListener("message", onMessage); } catch {}
      return false;
    }
  }

  function validBreakpointResult(value) {
    const id = value && typeof value.breakpointId === "string" ? value.breakpointId : null;
    return id && id.length > 0 && id.length <= MAX_BREAKPOINT_ID_LENGTH ? id : null;
  }

  async function performStart() {
    started = true;
    setupDeadline = now() + timeoutMs;
    let alreadyAttached;
    try { alreadyAttached = debuggerApi?.isAttached?.(); }
    catch { outcome = "debugger_unavailable"; cleanupComplete = true; return snapshot(); }
    if (!debuggerApi || typeof debuggerApi.isAttached !== "function" || typeof debuggerApi.attach !== "function" ||
        typeof debuggerApi.detach !== "function" || typeof debuggerApi.sendCommand !== "function" ||
        typeof alreadyAttached !== "boolean") {
      outcome = "debugger_unavailable";
      cleanupComplete = true;
      return snapshot();
    }
    if (alreadyAttached !== false) {
      outcome = "debugger_already_attached";
      cleanupComplete = true;
      return snapshot();
    }
    if (!installListeners()) {
      outcome = "debugger_unavailable";
      cleanupComplete = true;
      return snapshot();
    }
    try { debuggerApi.attach("1.3"); }
    catch (error) {
      outcome = classifyAttachError(error);
      cleanupComplete = true;
      detachListeners();
      return snapshot();
    }
    let attachedNow;
    try { attachedNow = debuggerApi.isAttached(); }
    catch {
      outcome = "attach_state_unknown";
      cleanupComplete = false;
      detachListeners();
      return snapshot();
    }
    if (attachedNow !== true) {
      outcome = "attach_not_connected";
      cleanupComplete = false;
      detachListeners();
      return snapshot();
    }
    attachedByUs = true;
    ownerToken = Object.freeze({ ownedAttachment: true });
    const token = ownerToken;

    debuggerEnabledAttempted = true;
    let result = await sendBounded("Debugger.enable", {}, setupDeadline, token);
    if (result.kind !== "resolved") {
      outcome = result.kind === "timed_out" ? "timed_out" : result.kind === "ownership_lost" ? "debugger_detached" : "debugger_enable_failed";
      return snapshot();
    }
    if (abortRequested || stopping || !isOwned(token)) { if (!abortRequested) outcome = "debugger_detached"; return snapshot(); }

    result = await sendBounded("Debugger.setPauseOnExceptions", { state: "none" }, setupDeadline, token);
    if (result.kind !== "resolved") {
      outcome = result.kind === "timed_out" ? "timed_out" : result.kind === "ownership_lost" ? "debugger_detached" : "pause_policy_failed";
      return snapshot();
    }
    if (abortRequested || stopping || !isOwned(token)) { if (!abortRequested) outcome = "debugger_detached"; return snapshot(); }

    result = await sendBounded("Debugger.setSkipAllPauses", { skip: true }, setupDeadline, token);
    if (result.kind !== "resolved") {
      outcome = result.kind === "timed_out" ? "timed_out" : result.kind === "ownership_lost" ? "debugger_detached" : "skip_all_pauses_unsupported";
      return snapshot();
    }
    if (abortRequested || stopping || !isOwned(token)) { if (!abortRequested) outcome = "debugger_detached"; return snapshot(); }

    objectGroupUsed = true;
    result = await sendBounded("Runtime.evaluate", {
      expression: targetExpression,
      objectGroup,
      returnByValue: false,
      awaitPromise: false,
    }, setupDeadline, token, "evaluate");
    const remote = result.kind === "resolved" ? result.value?.result : null;
    const objectId = remote && remote.type === "function" && typeof remote.objectId === "string" &&
      remote.objectId.length > 0 && remote.objectId.length <= 4096 ? remote.objectId : null;
    if (!objectId) {
      outcome = result.kind === "timed_out" ? "timed_out" : result.kind === "ownership_lost" ? "debugger_detached" : "target_unavailable";
      return snapshot();
    }
    if (abortRequested || stopping || !isOwned(token)) { if (!abortRequested) outcome = "debugger_detached"; return snapshot(); }

    result = await sendBounded("Debugger.setBreakpointOnFunctionCall", {
      objectId,
      condition,
    }, setupDeadline, token, "breakpoint");
    const candidateId = result.kind === "resolved" ? validBreakpointResult(result.value) : null;
    if (!candidateId) {
      outcome = result.kind === "timed_out" ? "timed_out" : result.kind === "ownership_lost" ? "debugger_detached" : "breakpoint_rejected";
      return snapshot();
    }
    breakpointId = candidateId;
    if (abortRequested || stopping || !isOwned(token)) { if (!abortRequested) outcome = "debugger_detached"; return snapshot(); }
    outcome = "breakpoint_set";
    breakpointInstalled = true;
    cleanupComplete = null;
    return snapshot();
  }

  function start() {
    if (startPromise) return startPromise;
    if (destroyedTarget) return Promise.resolve(snapshot());
    if (started) return Promise.resolve(snapshot());
    startPromise = performStart().catch(() => {
      if (outcome === "not_started") outcome = "debugger_unavailable";
      cleanupComplete = false;
      return snapshot();
    });
    startPromise.then(() => {}, () => {});
    return startPromise;
  }

  async function teardownOwned(deadline) {
    const token = ownerToken;
    if (!token || !isOwned(token)) {
      if (debuggerEnabledAttempted || objectGroupUsed || breakpointId) cleanupComplete = false;
      return false;
    }
    let ok = true;
    const runCleanupCommand = async (method, params) => {
      if (!isOwned(token)) { ok = false; return; }
      const result = await sendBounded(method, params, deadline, token);
      if (result.kind !== "resolved") ok = false;
    };
    if (breakpointId) {
      const id = breakpointId;
      breakpointId = null;
      await runCleanupCommand("Debugger.removeBreakpoint", { breakpointId: id });
    }
    if (objectGroupUsed) {
      objectGroupUsed = false;
      await runCleanupCommand("Runtime.releaseObjectGroup", { objectGroup });
    }
    if (debuggerEnabledAttempted) {
      debuggerEnabledAttempted = false;
      await runCleanupCommand("Debugger.disable", {});
    }
    if (isOwned(token)) {
      // Keep the detach listener until our detach completes. Removing it can run
      // user-controlled EventEmitter hooks that transfer this debugger to DevTools.
      try { debuggerApi.removeListener("message", onMessage); } catch { ok = false; }
      if (!isOwned(token)) {
        ok = false;
      } else {
        detachingByUs = true;
        try { debuggerApi.detach(); } catch { ok = false; }
        detachingByUs = false;
        ownershipLost = true;
        attachedByUs = false;
      }
      if (!detachListeners()) ok = false;
    } else ok = false;
    return ok;
  }

  function invalidate() {
    // Navigation invalidation disables capture but preserves the owned attachment
    // token so stop() can still remove only this observer's resources.
    abortRequested = true;
    if (outcome === "breakpoint_set") outcome = "debugger_detached";
    return stop();
  }

  function stop({ deadline = now() + timeoutMs, destroyed = false } = {}) {
    if (destroyed) {
      const hadOwnedResources = attachedByUs || debuggerEnabledAttempted || objectGroupUsed || breakpointId || listenerInstalled ||
        pendingCommandResults.size > 0;
      stopping = true;
      abortRequested = true;
      destroyedTarget = true;
      ownershipLost = true;
      attachedByUs = false;
      ownerToken = null;
      if (hadOwnedResources) cleanupComplete = false;
      if (outcome === "breakpoint_set") outcome = "debugger_detached";
      detachListeners();
      finalized = true;
      if (cleanupComplete === null) cleanupComplete = !hadOwnedResources;
      finalSnapshot = Object.freeze({
        status: safeStatus(outcome),
        breakpointSet: breakpointInstalled,
        pausedEvents: Number.isSafeInteger(pausedEvents) ? Math.min(pausedEvents, 1000000) : 0,
        cleanupComplete: typeof cleanupComplete === "boolean" ? cleanupComplete : null,
      });
      if (!stopPromise) stopPromise = Promise.resolve(finalSnapshot);
      return stopPromise;
    }
    if (stopPromise) return stopPromise;
    stopping = true;
    stopPromise = (async () => {
      if (startPromise) {
        const remaining = Math.max(0, deadline - now());
        if (remaining > 0) {
          let timer;
          await Promise.race([
            startPromise,
            new Promise((resolve) => { timer = setTimeoutFn(resolve, remaining); }),
          ]);
          if (timer) clearTimeoutFn(timer);
        }
      }
    let pendingComplete = true;
      if (pendingCommandResults.size > 0) {
        const remaining = Math.max(0, deadline - now());
        if (remaining <= 0) pendingComplete = false;
        else {
          let timer;
          const pending = Promise.allSettled([...pendingCommandResults]);
          const completed = await Promise.race([
            pending.then(() => true),
            new Promise((resolve) => { timer = setTimeoutFn(() => resolve(false), remaining); }),
          ]);
          if (timer) clearTimeoutFn(timer);
          pendingComplete = completed === true && pendingCommandResults.size === 0;
        }
      }
      if (!pendingComplete || now() >= deadline) cleanupComplete = false;
      const token = ownerToken;
      const stillOwned = token && isOwned(token);
      if (stillOwned) {
        const ok = await teardownOwned(deadline);
        cleanupComplete = ok && pendingComplete && cleanupComplete !== false;
      } else {
        if (debuggerEnabledAttempted || objectGroupUsed || breakpointId || attachedByUs) cleanupComplete = false;
        else if (cleanupComplete === null) cleanupComplete = true;
      }
      const listenerOk = detachListeners();
      if (!listenerOk) cleanupComplete = false;
      if (cleanupComplete === null) cleanupComplete = true;
      if (!cleanupComplete && outcome === "breakpoint_set") outcome = "cleanup_incomplete";
      finalized = true;
      finalSnapshot = Object.freeze({
        status: safeStatus(outcome),
        breakpointSet: breakpointInstalled,
        pausedEvents: Number.isSafeInteger(pausedEvents) ? Math.min(pausedEvents, 1000000) : 0,
        cleanupComplete: typeof cleanupComplete === "boolean" ? cleanupComplete : null,
      });
      return finalSnapshot;
    })().catch(() => {
      cleanupComplete = false;
      outcome = "cleanup_incomplete";
      finalized = true;
      detachListeners();
      finalSnapshot = Object.freeze({
        status: safeStatus(outcome), breakpointSet: breakpointInstalled,
        pausedEvents: Number.isSafeInteger(pausedEvents) ? Math.min(pausedEvents, 1000000) : 0,
        cleanupComplete: false,
      });
      return finalSnapshot;
    });
    return stopPromise;
  }

  return Object.freeze({
    start,
    stop,
    invalidate,
    get status() { return snapshot(); },
    get stateSymbolKey() { return key; },
  });
}

module.exports = Object.freeze({
  MAX_SETUP_MS,
  OUTCOMES,
  stateSymbolKey,
  buildNativeTargetExpression,
  buildNativeConditionalExpression,
  createNativeCallObserver,
});
