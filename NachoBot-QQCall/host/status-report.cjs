"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const PHASES = new Set([
  "initializing",
  "ready",
  "failed",
  "renderer_failure",
  "output_failure",
  "stopping",
  "stopped",
]);
const SAFE_CODE = /^[a-z][a-z0-9_]{1,63}$/;
const RENDERER_GONE_REASONS = new Set([
  "clean-exit",
  "abnormal-exit",
  "killed",
  "crashed",
  "oom",
  "launch-failed",
  "integrity-failure",
  "memory-eviction",
  "unknown",
]);
const MIN_EXIT_CODE = -0x80000000;
const MAX_EXIT_CODE = 0xffffffff;
const PRIVATE_END_TRACE_SUFFIX = ".host.private-end.trace.json";
const PRIVATE_END_TRACE_MAX_BYTES = 64 * 1024;
const PRIVATE_END_TRACE_MAX_EVENTS = 160;
const PRIVATE_END_TRACE_MAX_OUTPUTS = 64;
const PRIVATE_END_TRACE_MAX_TRANSACTIONS = 16;
const PRIVATE_END_TRACE_COMMANDS = new Set([8, 9, 10, 20001, 20011, 20017]);
const PRIVATE_END_TRACE_STAGES = new Set([
  "dispatching",
  "preflight_rejected",
  "renderer_dispatch_started",
  "renderer_posted",
  "post_receipt_invalid",
  "invoke_failed",
  "window_elapsed",
  "host_stopped",
]);
const PRIVATE_END_TRACE_RAW_ID_STATES = new Set(["valid", "missing", "invalid", "unknown"]);
const PRIVATE_END_TRACE_VALUE_TYPES = new Set(["array", "object", "string", "number", "boolean", "null", "undefined"]);
const PRIVATE_END_TRACE_ELEMENT_TYPES = new Set(["array", "object", "string", "number", "boolean", "null", "undefined"]);
const PRIVATE_END_TRACE_ASSOCIATIONS = new Set([
  "request_id_match",
  "id_zero_uncorrelated",
  "multiple_id_matches",
  "no_matching_request_id",
  "temporal_only",
  "multiple_active_windows",
]);

function traceInteger(value, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}

function traceIdentity(input) {
  if (!traceInteger(input?.traceId, { min: 1 }) || !traceInteger(input?.requestId, { min: 1 }) ||
      ![8, 9, 10].includes(input?.command) || !traceInteger(input?.hostGeneration, { min: 1 }) ||
      !traceInteger(input?.targetPid, { min: 1, max: 0x7fffffff })) return null;
  return {
    traceId: input.traceId,
    requestId: input.requestId,
    command: input.command,
    hostGeneration: input.hostGeneration,
    targetPid: input.targetPid,
  };
}

function sanitizePrivateEndTraceEvent(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  if (input.type === "request") {
    const identity = traceIdentity(input);
    if (!identity || !traceInteger(input.atMs) || !PRIVATE_END_TRACE_STAGES.has(input.postedStage) || input.postedStage !== "dispatching") return null;
    let params;
    if (input.command === 8 && input.params?.scene === 1 && input.params?.reason === 0) {
      params = { scene: 1, reason: 0 };
    } else if (input.command === 9 && input.params?.scene === 1 && input.params?.peerUidType === "string" &&
        traceInteger(input.params.peerUidLength, { max: 8192 }) && [1, 2].includes(input.params.rejectReason)) {
      // Retain historical reason-2 diagnostics; this grants no dispatch right.
      params = { scene: 1, peerUidType: "string", peerUidLength: input.params.peerUidLength, rejectReason: input.params.rejectReason };
    } else if (input.command === 10 && input.params?.scene === 1 && input.params?.peerUidType === "string" &&
        traceInteger(input.params.peerUidLength, { min: 1, max: 8192 }) && input.params.closeReason === 0) {
      params = { scene: 1, peerUidType: "string", peerUidLength: input.params.peerUidLength, closeReason: 0 };
    } else return null;
    return {
      type: "request",
      ...identity,
      atMs: input.atMs,
      postedStage: "dispatching",
      correlationEligible: input.correlationEligible !== false,
      windowMs: 5000,
      params,
    };
  }
  if (input.type === "post_stage") {
    const identity = traceIdentity(input);
    if (!identity || !traceInteger(input.atMs) || !PRIVATE_END_TRACE_STAGES.has(input.postedStage) || input.postedStage === "dispatching") return null;
    const event = { type: "post_stage", ...identity, atMs: input.atMs, postedStage: input.postedStage };
    if (typeof input.failureCode === "string" && SAFE_CODE.test(input.failureCode)) event.failureCode = input.failureCode;
    return event;
  }
  if (input.type === "sdk_output") {
    if (!traceInteger(input.atMs) || !PRIVATE_END_TRACE_COMMANDS.has(input.command) ||
        !traceInteger(input.hostGeneration, { min: 1 }) || !traceInteger(input.targetPid, { min: 1, max: 0x7fffffff }) ||
        !traceInteger(input.normalizedId) || !PRIVATE_END_TRACE_RAW_ID_STATES.has(input.rawIdState) ||
        !PRIVATE_END_TRACE_VALUE_TYPES.has(input.valueType) || !PRIVATE_END_TRACE_ASSOCIATIONS.has(input.association) ||
        !traceInteger(input.activeWindowCount, { max: 256 })) return null;
    const event = {
      type: "sdk_output",
      atMs: input.atMs,
      hostGeneration: input.hostGeneration,
      targetPid: input.targetPid,
      command: input.command,
      normalizedId: input.normalizedId,
      rawIdState: input.rawIdState,
      valueType: input.valueType,
      rawValueOmitted: true,
      association: input.association,
      activeWindowCount: input.activeWindowCount,
    };
    if (traceInteger(input.traceId, { min: 1 }) && traceInteger(input.requestId, { min: 1 }) &&
        [8, 9, 10].includes(input.command) && input.association === "request_id_match") {
      event.traceId = input.traceId;
      event.requestId = input.requestId;
    }
    if (traceInteger(input.arrayLength, { max: 1_000_000 })) event.arrayLength = input.arrayLength;
    if (Array.isArray(input.elementTypes)) {
      event.elementTypes = input.elementTypes.slice(0, 8).map((value) =>
        PRIVATE_END_TRACE_ELEMENT_TYPES.has(value) ? value : "undefined");
    }
    if ([8, 9, 10].includes(input.command)) {
      if (traceInteger(input.ackReturnCode, { min: -0x80000000, max: 0x7fffffff })) event.ackReturnCode = input.ackReturnCode;
      if (typeof input.ackSecondEmptyString === "boolean") event.ackSecondEmptyString = input.ackSecondEmptyString;
    }
    if (input.command === 20011 && traceInteger(input.scene, { max: 3 })) event.scene = input.scene;
    if (input.command === 20017) {
      if (traceInteger(input.closeReason, { max: 255 })) event.closeReason = input.closeReason;
      if (traceInteger(input.destroyReason, { max: 255 })) event.destroyReason = input.destroyReason;
      if (["false_boolean", "true_boolean", "zero_integer", "one_integer", "unknown"].includes(input.inRoomValueKind)) {
        event.inRoomValueKind = input.inRoomValueKind;
      }
    }
    return event;
  }
  if (input.type === "window_end") {
    const identity = traceIdentity(input);
    if (!identity || !traceInteger(input.atMs) || !traceInteger(input.elapsedMs, { max: 5000 }) ||
        !["window_elapsed", "window_superseded", "host_stopped"].includes(input.reason) ||
        !Array.isArray(input.commandCounts) || input.commandCounts.length > 32 ||
        !traceInteger(input.otherCommandRecords, { max: 1_000_000 }) ||
        !traceInteger(input.network20001Count, { max: 1_000_000 }) ||
        !traceInteger(input.droppedRelevantRecords, { max: 1_000_000 })) return null;
    const commandCounts = [];
    for (const item of input.commandCounts) {
      if (!item || !traceInteger(item.command, { min: 1 }) || !traceInteger(item.count, { min: 1, max: 1_000_000 })) return null;
      commandCounts.push({ command: item.command, count: item.count });
    }
    return {
      type: "window_end",
      ...identity,
      atMs: input.atMs,
      elapsedMs: input.elapsedMs,
      reason: input.reason,
      commandCounts,
      otherCommandRecords: input.otherCommandRecords,
      network20001Count: input.network20001Count,
      droppedRelevantRecords: input.droppedRelevantRecords,
    };
  }
  return null;
}

function privateEndTraceDocument(input) {
  if (!input || typeof input !== "object" || !Array.isArray(input.events)) return null;
  const events = [];
  for (const rawEvent of input.events) {
    const event = sanitizePrivateEndTraceEvent(rawEvent);
    if (!event) continue;
    events.push(event);
  }

  const outputEventIndexes = [];
  for (let index = 0; index < events.length; index += 1) {
    if (events[index].type === "sdk_output") outputEventIndexes.push(index);
  }
  let droppedOutputCount = traceInteger(input.droppedOutputCount, { max: 1_000_000 }) ? input.droppedOutputCount : 0;
  if (outputEventIndexes.length > PRIVATE_END_TRACE_MAX_OUTPUTS) {
    const retainedOutputIndexes = new Set(outputEventIndexes.slice(-PRIVATE_END_TRACE_MAX_OUTPUTS));
    droppedOutputCount = Math.min(1_000_000, droppedOutputCount + outputEventIndexes.length - retainedOutputIndexes.size);
    for (let index = events.length - 1; index >= 0; index -= 1) {
      if (events[index].type === "sdk_output" && !retainedOutputIndexes.has(index)) events.splice(index, 1);
    }
  }

  const requestTraceIds = [];
  for (const event of events) {
    if (event.type !== "request") continue;
    requestTraceIds.push(event.traceId);
  }
  const retainedTraceIds = new Set(requestTraceIds.slice(-PRIVATE_END_TRACE_MAX_TRANSACTIONS));
  let boundedEvents = events.filter((event) =>
    (event.type === "sdk_output" && !event.traceId) ||
    !traceInteger(event.traceId, { min: 1 }) || retainedTraceIds.has(event.traceId));
  if (boundedEvents.length > PRIVATE_END_TRACE_MAX_EVENTS) boundedEvents = boundedEvents.slice(-PRIVATE_END_TRACE_MAX_EVENTS);
  const document = {
    schema: 1,
    updatedAtMs: traceInteger(input.updatedAtMs) ? input.updatedAtMs : Date.now(),
    droppedEventCount: traceInteger(input.droppedEventCount, { max: 1_000_000 }) ? input.droppedEventCount : 0,
    droppedOutputCount,
    events: boundedEvents,
  };
  let encoded = Buffer.from(`${JSON.stringify(document, null, 2)}\n`, "utf8");
  while (encoded.byteLength > PRIVATE_END_TRACE_MAX_BYTES && document.events.length) {
    const removable = document.events.findIndex((event) => event.type === "sdk_output");
    document.events.splice(removable >= 0 ? removable : 0, 1);
    document.droppedEventCount += 1;
    encoded = Buffer.from(`${JSON.stringify(document, null, 2)}\n`, "utf8");
  }
  return encoded.byteLength <= PRIVATE_END_TRACE_MAX_BYTES ? { document, encoded } : null;
}

async function writeHostPrivateEndTrace(reportPath, input) {
  if (typeof reportPath !== "string" || !path.isAbsolute(reportPath)) return false;
  const safe = privateEndTraceDocument(input);
  if (!safe) return false;
  const target = `${reportPath}${PRIVATE_END_TRACE_SUFFIX}`;
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    await fs.promises.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
    await fs.promises.writeFile(temporary, safe.encoded, { encoding: "utf8", mode: 0o600, flag: "wx" });
    await fs.promises.rename(temporary, target);
    try { await fs.promises.chmod(target, 0o600); } catch { /* Best effort on filesystems without POSIX modes. */ }
    return true;
  } catch {
    try { await fs.promises.rm(temporary, { force: true }); } catch { /* Preserve the previous complete trace. */ }
    return false;
  }
}

function createPrivateEndTraceWriter(reportPath, { writeSnapshot = writeHostPrivateEndTrace } = {}) {
  if (typeof writeSnapshot !== "function") throw new TypeError("private_end_trace_writer_invalid");
  const events = [];
  let droppedEventCount = 0;
  let droppedOutputCount = 0;
  let dirty = false;
  let scheduled = false;
  let inFlight = null;

  function makeDocument() {
    return { schema: 1, updatedAtMs: Date.now(), droppedEventCount, droppedOutputCount, events: [...events] };
  }

  function flush() {
    if (inFlight) return inFlight.then(() => dirty ? flush() : true, () => dirty ? flush() : false);
    if (!dirty) return Promise.resolve(true);
    dirty = false;
    const snapshot = makeDocument();
    inFlight = Promise.resolve().then(() => writeSnapshot(reportPath, snapshot)).then((result) => result === true, () => false).finally(() => {
      inFlight = null;
      if (dirty) scheduleFlush();
    });
    return inFlight;
  }

  function scheduleFlush() {
    if (scheduled) return;
    scheduled = true;
    setImmediate(() => {
      scheduled = false;
      void flush().catch(() => {});
    });
  }

  function observe(input) {
    try {
      const event = sanitizePrivateEndTraceEvent(input);
      if (!event) return;
      if (event.type === "sdk_output") {
        const outputCount = events.reduce((count, item) => count + (item.type === "sdk_output" ? 1 : 0), 0);
        if (outputCount >= PRIVATE_END_TRACE_MAX_OUTPUTS) {
          const oldestOutputIndex = events.findIndex((item) => item.type === "sdk_output");
          if (oldestOutputIndex >= 0) events.splice(oldestOutputIndex, 1);
          droppedOutputCount += 1;
        }
      }
      events.push(event);
      if (events.length > PRIVATE_END_TRACE_MAX_EVENTS) {
        const removable = events.findIndex((item) => item.type === "sdk_output");
        events.splice(removable >= 0 ? removable : 0, 1);
        droppedEventCount += 1;
      }
      if (event.type === "request") {
        const requestIds = [];
        for (const item of events) if (item.type === "request") requestIds.push(item.traceId);
        const expired = new Set(requestIds.slice(0, Math.max(0, requestIds.length - PRIVATE_END_TRACE_MAX_TRANSACTIONS)));
        if (expired.size) {
          for (let index = events.length - 1; index >= 0; index -= 1) {
            if (traceInteger(events[index].traceId, { min: 1 }) && expired.has(events[index].traceId)) {
              events.splice(index, 1);
              droppedEventCount += 1;
            }
          }
        }
      }
      dirty = true;
      scheduleFlush();
    } catch { /* Diagnostic observation never changes Host behavior. */ }
  }

  Object.defineProperty(observe, "flush", { value: flush });
  return observe;
}

function statusDocument({ phase, hostGeneration, targetPid, lastFailureCode = null, ready = false, listening = false, stopped = false } = {}) {
  return Object.freeze({
    phase: PHASES.has(phase) ? phase : "failed",
    hostGeneration: Number.isSafeInteger(hostGeneration) && hostGeneration > 0 ? hostGeneration : null,
    targetPid: Number.isSafeInteger(targetPid) && targetPid > 0 ? targetPid : null,
    lastFailureCode: typeof lastFailureCode === "string" && SAFE_CODE.test(lastFailureCode) ? lastFailureCode : null,
    ready: ready === true,
    listening: listening === true,
    stopped: stopped === true,
  });
}

function writeHostStatus(reportPath, status) {
  if (typeof reportPath !== "string" || !path.isAbsolute(reportPath)) return false;
  const target = `${reportPath}.host.status.json`;
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    fs.writeFileSync(temporary, `${JSON.stringify(statusDocument(status), null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    fs.renameSync(temporary, target);
    try { fs.chmodSync(target, 0o600); } catch { /* Best-effort private mode on platforms without POSIX modes. */ }
    return true;
  } catch {
    try { fs.rmSync(temporary, { force: true }); } catch { /* Reporting must never change Host ownership. */ }
    return false;
  }
}

function positiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function rendererGoneDocument(input = {}) {
  const reason = RENDERER_GONE_REASONS.has(input?.reason) ? input.reason : "unknown";
  const exitCode = Number.isSafeInteger(input?.exitCode) && input.exitCode >= MIN_EXIT_CODE && input.exitCode <= MAX_EXIT_CODE
    ? input.exitCode
    : null;
  return Object.freeze({
    schema: 1,
    observedAtMs: Number.isSafeInteger(input?.observedAtMs) && input.observedAtMs >= 0 ? input.observedAtMs : null,
    hostGeneration: positiveSafeInteger(input?.hostGeneration),
    targetPid: positiveSafeInteger(input?.targetPid),
    rendererPid: positiveSafeInteger(input?.rendererPid),
    reason,
    exitCode,
    shutdownInProgress: input?.shutdownInProgress === true,
    ready: input?.ready === true,
    pluginFound: input?.pluginFound === true,
    hasPostMessage: input?.hasPostMessage === true,
    callbackReady: input?.callbackReady === true,
  });
}

function writeHostCrash(reportPath, input) {
  if (typeof reportPath !== "string" || !path.isAbsolute(reportPath)) return false;
  const target = `${reportPath}.host.crash.json`;
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
    fs.writeFileSync(temporary, `${JSON.stringify(rendererGoneDocument(input), null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    // A same-directory hard link publishes the complete private record atomically
    // without replacing an earlier renderer-gone observation.
    fs.linkSync(temporary, target);
    try { fs.chmodSync(target, 0o600); } catch { /* Best-effort private mode on platforms without POSIX modes. */ }
    return true;
  } catch {
    return false;
  } finally {
    try { fs.rmSync(temporary, { force: true }); } catch { /* Reporting must never change Host ownership. */ }
  }
}

module.exports = {
  createPrivateEndTraceWriter,
  privateEndTraceDocument,
  rendererGoneDocument,
  sanitizePrivateEndTraceEvent,
  statusDocument,
  writeHostCrash,
  writeHostPrivateEndTrace,
  writeHostStatus,
};
