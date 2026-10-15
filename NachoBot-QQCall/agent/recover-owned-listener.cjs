"use strict";

// Fixed one-shot recovery for this exact wrapper.node build. This module never
// loads a wrapper or registers a listener; it only locates and removes the
// listener owned by the active QQCall Agent.
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const BOOTSTRAP_KEY = Symbol.for("nachobot.qqcall.bootstrap.v1");
const RECOVERY_KEY = Symbol.for("nachobot.qqcall.recovery.v1");
const PINNED_WRAPPER_SHA256 = "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2";
const PIPE_PREFIX = "\\\\.\\pipe\\nachobot-qqcall-";
const RECOVERY_FUNCTION = `async function (listener) {
  await this.removeKernelAVSDKListener(listener);
  return true;
}`;

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function isAbsolutePath(value) {
  return typeof value === "string" && path.isAbsolute(value);
}

function samePath(left, right) {
  if (!isAbsolutePath(left) || !isAbsolutePath(right)) return false;
  const a = path.resolve(left).replace(/[\\/]+$/, "");
  const b = path.resolve(right).replace(/[\\/]+$/, "");
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function validToken(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(value);
}

function sameToken(left, right) {
  if (!validToken(left) || !validToken(right)) return false;
  const a = Buffer.from(left, "ascii");
  const b = Buffer.from(right, "ascii");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function validateInput(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw codedError("recovery_config_missing");
  if (!samePath(input.modulePath, __filename)) throw codedError("recovery_module_path_mismatch");
  if (!isAbsolutePath(input.appPath)) throw codedError("app_path_invalid");
  if (!isAbsolutePath(input.wrapperModulePath)) throw codedError("wrapper_path_invalid");
  if (!isAbsolutePath(input.reportPath)) throw codedError("report_path_invalid");
  if (typeof input.pipeName !== "string" || !input.pipeName.startsWith(PIPE_PREFIX) || !/^\\\\\.\\pipe\\nachobot-qqcall-\d+-[a-f0-9]{16,64}$/i.test(input.pipeName)) {
    throw codedError("pipe_name_invalid");
  }
  const targetPid = Number(input.pipeName.slice(PIPE_PREFIX.length).split("-")[0]);
  if (!Number.isSafeInteger(targetPid) || targetPid !== process.pid) throw codedError("target_pid_mismatch");
  if (!Number.isSafeInteger(input.connectionGeneration) || input.connectionGeneration < 1) throw codedError("generation_invalid");
  if (typeof input.sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(input.sessionName)) throw codedError("session_name_invalid");
  if (!validToken(input.ipcToken)) throw codedError("auth_token_invalid");
  return input;
}

function sameRun(original, input) {
  return original &&
    original.connectionGeneration === input.connectionGeneration &&
    original.pipeName === input.pipeName &&
    original.sessionName === input.sessionName &&
    samePath(original.appPath, input.appPath) &&
    samePath(original.wrapperModulePath, input.wrapperModulePath) &&
    samePath(input.reportPath, `${original.reportPath}-recovery`) &&
    sameToken(original.ipcToken, input.ipcToken);
}

function wrapperDigest(modulePath) {
  let bytes;
  try { bytes = fs.readFileSync(modulePath); }
  catch { throw codedError("wrapper_read_failed"); }
  return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

function reportBase(input) {
  return {
    schema: 1,
    recoveryLoaded: true,
    processPid: process.pid,
    connectionGeneration: input.connectionGeneration,
    registryMatched: false,
    wrapperHashMatched: false,
    listenerRegistered: false,
    listenerIdAvailable: null,
    agentReferenceFound: false,
    listenerReferenceFound: false,
    serviceReferenceFound: false,
    nativeRemovalCallAttempted: false,
    nativeRemovalCallReturned: null,
    vendorInternalRemovalVerified: false,
    originalAgentStateSynchronized: false,
    originalClientCloseAttempted: false,
    originalClientCloseCompleted: false,
    retainedOwnedReferences: false,
    inspectorAvailable: null,
    inspectorRequireCode: null,
    lastFailureCode: null,
  };
}

async function writeReport(input, report) {
  const target = `${input.reportPath}.recovery.json`;
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`;
  try {
    await fs.promises.mkdir(path.dirname(target), { recursive: true });
    await fs.promises.writeFile(temporary, `${JSON.stringify(report, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    await fs.promises.rename(temporary, target);
  } catch {
    try { await fs.promises.rm(temporary, { force: true }); } catch { /* best effort */ }
    report.lastFailureCode = report.lastFailureCode || "recovery_report_write_failed";
  }
}

function post(session, method, params, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(codedError("inspector_request_timeout"));
    }, timeoutMs);
    timer.unref?.();
    session.post(method, params, (error, result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(codedError("inspector_protocol_failed"));
      else resolve(result);
    });
  });
}

async function getProperties(session, objectId) {
  return post(session, "Runtime.getProperties", {
    objectId,
    ownProperties: true,
    accessorPropertiesOnly: false,
    generatePreview: false,
  });
}

async function findUniqueScopeBinding(session, functionObjectId, bindingName) {
  const details = await getProperties(session, functionObjectId);
  const scopesProperty = details.internalProperties?.find((property) => property.name === "[[Scopes]]");
  const scopesObjectId = scopesProperty?.value?.objectId;
  if (!scopesObjectId) return { count: 0, value: null };
  const scopesList = await getProperties(session, scopesObjectId);
  const scopeObjects = (scopesList.result || [])
    .filter((property) => /^\d+$/.test(property.name))
    .map((property) => property.value)
    .filter((remote) => remote?.objectId);
  const matches = [];
  for (const scope of scopeObjects) {
    const properties = await getProperties(session, scope.objectId);
    for (const property of properties.result || []) {
      if (property.name === bindingName) matches.push(property.value);
    }
  }
  return { count: matches.length, value: matches.length === 1 ? matches[0] : null };
}

async function findOwnProperty(session, objectId, name) {
  const properties = await getProperties(session, objectId);
  return properties.result?.find((property) => property.name === name)?.value || null;
}

function recoveryResult(report) {
  return Object.freeze({ ...report });
}

async function recover(inputValue) {
  let input;
  let report;
  let session = null;
  let reportWriteAllowed = false;
  let retainedForCleanup = null;
  try {
    input = validateInput(inputValue);
    report = reportBase(input);

    if (wrapperDigest(input.wrapperModulePath) !== PINNED_WRAPPER_SHA256) throw codedError("unsupported_wrapper_hash");
    report.wrapperHashMatched = true;

    const currentRegistry = globalThis[BOOTSTRAP_KEY] || null;
    const previousRecovery = globalThis[RECOVERY_KEY] || null;
    const registry = currentRegistry || previousRecovery?.registry || null;
    if (!registry || !registry.config || !sameRun(registry.config, input)) throw codedError("bootstrap_run_mismatch");
    reportWriteAllowed = true;
    if (currentRegistry && previousRecovery?.registry && previousRecovery.registry !== currentRegistry) {
      throw codedError("recovery_namespace_occupied");
    }
    const controller = registry.controller || previousRecovery?.controller || null;
    if (!controller || typeof controller.status !== "function" || !controller.client || typeof controller.client.close !== "function") {
      throw codedError("owned_controller_unavailable");
    }
    const originalStatus = controller.status();
    const agentStatus = originalStatus?.agent;
    if (originalStatus?.processPid !== process.pid || originalStatus?.connectionGeneration !== input.connectionGeneration) {
      throw codedError("controller_runtime_mismatch");
    }
    if (agentStatus?.listenerRegistered !== true || agentStatus?.listenerIdAvailable !== false ||
        agentStatus?.serviceAvailable !== true || agentStatus?.capabilities?.removeKernelAVSDKListener !== true) {
      throw codedError("owned_listener_state_mismatch");
    }
    if (agentStatus.state !== "attached_without_removal_handle" ||
        agentStatus.capabilities?.removeHandleAvailable !== false ||
        (agentStatus.listenerRemovalMode !== undefined && agentStatus.listenerRemovalMode !== "returned-id")) {
      throw codedError("owned_listener_not_legacy_contract");
    }
    report.registryMatched = true;
    report.listenerRegistered = true;
    report.listenerIdAvailable = false;

    // Retain only references rooted in our exact bootstrap registry. This
    // survives the old Agent deleting its registry key after pipe shutdown.
    let retained = previousRecovery;
    if (retained && retained.registry !== registry) throw codedError("recovery_namespace_occupied");
    if (retained?.inFlight === true) {
      report.lastFailureCode = "recovery_in_progress";
      return recoveryResult(report);
    }
    if (!retained) {
      retained = {
        schema: 1,
        registry,
        controller,
        agent: null,
        listener: null,
        avsdkService: null,
        nativeRemovalCallReturned: false,
        nativeRemovalCallAttempted: false,
        clientCloseCompleted: false,
        inFlight: false,
      };
      Object.defineProperty(globalThis, RECOVERY_KEY, { value: retained, configurable: true, enumerable: false, writable: false });
    }
    report.retainedOwnedReferences = true;
    if (retained.nativeRemovalCallAttempted === true && retained.nativeRemovalCallReturned !== true) {
      report.nativeRemovalCallAttempted = true;
      report.nativeRemovalCallReturned = false;
      report.lastFailureCode = "native_removal_outcome_unknown";
      await writeReport(input, report);
      return recoveryResult(report);
    }
    retained.inFlight = true;
    retainedForCleanup = retained;

    if (retained.nativeRemovalCallReturned === true) {
      report.agentReferenceFound = Boolean(retained.agent);
      report.listenerReferenceFound = Boolean(retained.listener);
      report.serviceReferenceFound = Boolean(retained.avsdkService);
      report.nativeRemovalCallAttempted = true;
      report.nativeRemovalCallReturned = true;
      report.originalClientCloseAttempted = !retained.clientCloseCompleted;
      if (!retained.clientCloseCompleted) {
        try {
          await retained.controller.client.close();
          retained.clientCloseCompleted = true;
          report.originalClientCloseCompleted = true;
        } catch {
          report.lastFailureCode = "original_client_close_failed";
        }
      } else {
        report.originalClientCloseCompleted = true;
      }
      await writeReport(input, report);
      return recoveryResult(report);
    }

    // Some embedded Electron/Node builds omit the inspector builtin. Load it
    // only after validating and strongly retaining our exact owned registry;
    // absence must leave both the listener and client untouched for retry.
    let inspector;
    try {
      inspector = require("node:inspector");
      if (!inspector || typeof inspector.Session !== "function") throw codedError("inspector_module_unavailable");
      report.inspectorAvailable = true;
    } catch (error) {
      const allowedRequireCodes = new Set([
        "MODULE_NOT_FOUND",
        "ERR_UNKNOWN_BUILTIN_MODULE",
        "ERR_UNKNOWN_MODULE_SPECIFIER",
      ]);
      report.inspectorAvailable = false;
      report.inspectorRequireCode = allowedRequireCodes.has(error?.code) ? error.code : null;
      report.lastFailureCode = "inspector_module_unavailable";
      await writeReport(input, report);
      return recoveryResult(report);
    }

    session = new inspector.Session();
    session.connect();
    await post(session, "Runtime.enable", {});
    const attachRemote = await post(session, "Runtime.evaluate", {
      expression: `globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")]?.controller?.attach`,
      returnByValue: false,
      silent: true,
      generatePreview: false,
    });
    const attachFunctionId = attachRemote.result?.objectId;
    if (!attachFunctionId || attachRemote.result?.type !== "function") throw codedError("owned_attach_function_missing");

    const agentBinding = await findUniqueScopeBinding(session, attachFunctionId, "agent");
    if (agentBinding.count !== 1 || !agentBinding.value?.objectId || agentBinding.value.type !== "object") {
      throw codedError("owned_agent_scope_missing_or_ambiguous");
    }
    const detachBinding = await findOwnProperty(session, agentBinding.value.objectId, "detach");
    if (!detachBinding?.objectId || detachBinding.type !== "function") throw codedError("owned_detach_function_missing");
    const listenerBinding = await findUniqueScopeBinding(session, detachBinding.objectId, "listener");
    if (listenerBinding.count !== 1 || !listenerBinding.value?.objectId || listenerBinding.value.type !== "object") {
      throw codedError("owned_listener_scope_missing_or_ambiguous");
    }
    const serviceBinding = await findUniqueScopeBinding(session, detachBinding.objectId, "avsdkService");
    if (serviceBinding.count !== 1 || !serviceBinding.value?.objectId || serviceBinding.value.type !== "object") {
      throw codedError("owned_service_scope_missing_or_ambiguous");
    }

    report.agentReferenceFound = true;
    report.listenerReferenceFound = true;
    report.serviceReferenceFound = true;

    // Retain the exact values found in the original closures, without exposing
    // Inspector object IDs outside this in-process session.
    const globalRemote = await post(session, "Runtime.evaluate", {
      expression: "globalThis",
      returnByValue: false,
      silent: true,
      generatePreview: false,
    });
    const retainedResult = await post(session, "Runtime.callFunctionOn", {
      objectId: globalRemote.result?.objectId,
      functionDeclaration: `function (agent, listener, avsdkService) {
        const slot = this[Symbol.for("nachobot.qqcall.recovery.v1")];
        if (!slot) return false;
        slot.agent = agent;
        slot.listener = listener;
        slot.avsdkService = avsdkService;
        return true;
      }`,
      arguments: [
        { objectId: agentBinding.value.objectId },
        { objectId: listenerBinding.value.objectId },
        { objectId: serviceBinding.value.objectId },
      ],
      returnByValue: true,
      silent: true,
    });
    if (retainedResult.result?.value !== true) throw codedError("owned_reference_retain_failed");
    if (!retained.agent || !retained.listener || !retained.avsdkService) throw codedError("owned_reference_retain_failed");

    report.nativeRemovalCallAttempted = true;
    retained.nativeRemovalCallAttempted = true;
    const removeResult = await post(session, "Runtime.callFunctionOn", {
      objectId: serviceBinding.value.objectId,
      functionDeclaration: RECOVERY_FUNCTION,
      arguments: [{ objectId: listenerBinding.value.objectId }],
      awaitPromise: true,
      returnByValue: true,
      silent: true,
    }, 5000);
    if (removeResult.exceptionDetails || removeResult.result?.value !== true) {
      report.nativeRemovalCallAttempted = true;
      report.nativeRemovalCallReturned = false;
      retained.nativeRemovalCallReturned = false;
      report.lastFailureCode = "native_remove_call_failed";
      await writeReport(input, report);
      return recoveryResult(report);
    }

    report.nativeRemovalCallReturned = true;
    retained.nativeRemovalCallReturned = true;
    report.originalAgentStateSynchronized = false;
    report.originalClientCloseAttempted = true;
    try {
      await controller.client.close();
      report.originalClientCloseCompleted = true;
      retained.clientCloseCompleted = true;
    } catch {
      report.lastFailureCode = "original_client_close_failed";
    }
    await writeReport(input, report);
    return recoveryResult(report);
  } catch (error) {
    if (!report && inputValue && typeof inputValue === "object" && isAbsolutePath(inputValue.reportPath)) {
      input = inputValue;
      report = reportBase(input);
    }
    if (report) {
      if (retainedForCleanup?.nativeRemovalCallAttempted === true && retainedForCleanup.nativeRemovalCallReturned !== true) {
        report.nativeRemovalCallAttempted = true;
        report.nativeRemovalCallReturned = false;
      }
      report.lastFailureCode = /^[a-z0-9_]{1,80}$/i.test(error?.code || "") ? error.code : "recovery_failed";
      if (input && reportWriteAllowed) await writeReport(input, report);
      return recoveryResult(report);
    }
    return Object.freeze({ schema: 1, recoveryLoaded: true, processPid: process.pid, lastFailureCode: /^[a-z0-9_]{1,80}$/i.test(error?.code || "") ? error.code : "recovery_failed" });
  } finally {
    if (retainedForCleanup) retainedForCleanup.inFlight = false;
    try { session?.disconnect(); } catch { /* Inspector session is local and disposable */ }
  }
}

module.exports = Object.freeze({ bootstrap: recover, recover, pinnedWrapperSha256: PINNED_WRAPPER_SHA256 });
