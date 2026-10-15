"use strict";

const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const path = require("node:path");
const { TextDecoder } = require("node:util");

const SCHEMA = "asar.physical.keeper.v1";
const KEEPER_HELPER_PATH = path.resolve(__dirname, "../probe/asar-physical-keeper.py");
const REPOSITORY_ROOT = path.resolve(__dirname, "..");
const APP_PATH = String.raw`C:\Program Files\Tencent\QQNT\versions\9.9.26-44343\resources\app`;
const FILE_PINS = Object.freeze([
  Object.freeze({ name: "application.asar", maxBytes: 64 * 1024 * 1024, sha256: "9C9959CFA351E8597C008C944DD482F826A24967ABA607F56350B06AC56200E2" }),
  Object.freeze({ name: "package.json", maxBytes: 64 * 1024, sha256: "15557B5A5A49A9C866D21048316DB50723642E13067CA12AFB36AF35ED3ECDC9" }),
  Object.freeze({ name: "wrapper.node", maxBytes: 128 * 1024 * 1024, sha256: "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2" }),
]);
const HELPER_REJECTION_CODES = new Set([
  "input_too_large", "input_eof", "input_timeout", "input_read_failed", "input_invalid",
  "input_schema_rejected", "finish_schema_rejected", "unknown_operation", "nonce_invalid",
  "nonce_mismatch", "binding_invalid", "app_path_rejected", "process_query_unsupported",
  "process_identity_mismatch", "process_identity_changed",
  "native_query_unsupported", "component_open_failed", "component_reparse_rejected",
  "component_type_rejected", "canonical_path_mismatch", "file_type_rejected",
  "file_size_invalid", "file_size_exceeded", "file_metadata_changed", "file_hash_unavailable",
  "file_hash_mismatch", "output_too_large", "output_write_failed", "runtime_deadline_exceeded",
  "resource_close_failed", "internal_error",
]);
const CLIENT_REJECTION_CODES = new Set([
  "keeper_options_invalid", "keeper_spawn_failed", "keeper_child_invalid", "keeper_timeout",
  "keeper_output_too_large", "keeper_output_invalid", "keeper_output_eof", "keeper_unexpected_output",
  "keeper_process_exited", "keeper_process_exit_invalid", "keeper_input_failed",
  "keeper_frame_invalid", "keeper_rejected", "keeper_interpreter_spawn_failed",
  "keeper_interpreter_child_invalid", "keeper_interpreter_timeout", "keeper_interpreter_output_invalid",
  "keeper_interpreter_resolve_failed",
]);
const MAX_INPUT_LINE_BYTES = 4096;
const MAX_OUTPUT_LINE_BYTES = 64 * 1024;
const MAX_RUNTIME_MS = 15_000;
const PYTHON_RESOLVER_MAX_RUNTIME_MS = 5_000;
const PYTHON_RESOLVER_MAX_OUTPUT_BYTES = 4096;
const CHILD_CLEANUP_WAIT_MS = 500;
const PYTHON_RESOLVER_SCRIPT = "import json,os,sys; p=getattr(sys,'_base_executable',None) or sys.executable; print(json.dumps({'executable':os.path.abspath(p)},separators=(',',':')))";
const FILETIME = /^(0|[1-9][0-9]{0,19})$/;
const NONCE = /^[A-Fa-f0-9]{64}$/;
const SHA256 = /^[A-F0-9]{64}$/;
const FILE_ID = /^[A-F0-9]{32}$/;

function codedError(code) {
  const error = new Error(CLIENT_REJECTION_CODES.has(code) || HELPER_REJECTION_CODES.has(code) ? code : "keeper_frame_invalid");
  error.code = CLIENT_REJECTION_CODES.has(code) || HELPER_REJECTION_CODES.has(code) ? code : "keeper_frame_invalid";
  return error;
}

function isPlainExactObject(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key)));
}

function scanJsonNoDuplicateKeys(text) {
  let index = 0;
  const whitespace = () => { while (index < text.length && /[\x20\t\r\n]/.test(text[index])) index += 1; };
  function stringToken() {
    const start = index;
    if (text[index] !== '"') throw codedError("keeper_output_invalid");
    index += 1;
    while (index < text.length) {
      const char = text[index++];
      if (char === '"') {
        try { return JSON.parse(text.slice(start, index)); }
        catch { throw codedError("keeper_output_invalid"); }
      }
      if (char === "\\") {
        if (index >= text.length) throw codedError("keeper_output_invalid");
        index += 1;
      } else if (char.charCodeAt(0) < 0x20) throw codedError("keeper_output_invalid");
    }
    throw codedError("keeper_output_invalid");
  }
  function value(depth) {
    if (depth > 32) throw codedError("keeper_output_invalid");
    whitespace();
    const char = text[index];
    if (char === '"') { stringToken(); return; }
    if (char === "{") {
      index += 1;
      whitespace();
      const keys = new Set();
      if (text[index] === "}") { index += 1; return; }
      while (index < text.length) {
        whitespace();
        const key = stringToken();
        if (keys.has(key)) throw codedError("keeper_output_invalid");
        keys.add(key);
        whitespace();
        if (text[index++] !== ":") throw codedError("keeper_output_invalid");
        value(depth + 1);
        whitespace();
        if (text[index] === "}") { index += 1; return; }
        if (text[index++] !== ",") throw codedError("keeper_output_invalid");
      }
      throw codedError("keeper_output_invalid");
    }
    if (char === "[") {
      index += 1;
      whitespace();
      if (text[index] === "]") { index += 1; return; }
      while (index < text.length) {
        value(depth + 1);
        whitespace();
        if (text[index] === "]") { index += 1; return; }
        if (text[index++] !== ",") throw codedError("keeper_output_invalid");
      }
      throw codedError("keeper_output_invalid");
    }
    const start = index;
    while (index < text.length && !/[\x20\t\r\n,\]}]/.test(text[index])) index += 1;
    if (index === start) throw codedError("keeper_output_invalid");
  }
  value(0);
  whitespace();
  if (index !== text.length) throw codedError("keeper_output_invalid");
}

function parseOutputLine(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length < 2 || bytes.length > MAX_OUTPUT_LINE_BYTES || bytes[bytes.length - 1] !== 0x0a) {
    throw codedError("keeper_output_invalid");
  }
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, -1)); }
  catch { throw codedError("keeper_output_invalid"); }
  if (text.endsWith("\r")) text = text.slice(0, -1);
  if (text.includes("\r") || text.includes("\n")) throw codedError("keeper_output_invalid");
  scanJsonNoDuplicateKeys(text);
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) {
      throw codedError("keeper_output_invalid");
    }
    return value;
  } catch (error) {
    throw codedError(error?.code === "keeper_output_invalid" ? error.code : "keeper_output_invalid");
  }
}

function validateRequest(options) {
  const keys = ["kernelPid", "kernelCreationTime100ns", "connectionGeneration", "appPath", "probePythonPath"];
  if (!isPlainExactObject(options, keys) || !Number.isSafeInteger(options.kernelPid) || options.kernelPid < 1 || options.kernelPid > 0xffffffff ||
      typeof options.kernelCreationTime100ns !== "string" || !FILETIME.test(options.kernelCreationTime100ns) ||
      BigInt(options.kernelCreationTime100ns) > 0xffffffffffffffffn ||
      !Number.isSafeInteger(options.connectionGeneration) || options.connectionGeneration < 1 ||
      typeof options.appPath !== "string" || options.appPath !== APP_PATH ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath)) {
    throw codedError("keeper_options_invalid");
  }
}

function rejectHelperFrame(frame) {
  if (!isPlainExactObject(frame, ["schema", "state", "code"]) || frame.schema !== SCHEMA ||
      frame.state !== "rejected" || typeof frame.code !== "string" || !HELPER_REJECTION_CODES.has(frame.code)) {
    throw codedError("keeper_frame_invalid");
  }
  throw codedError(frame.code);
}

function validateReadyFrame(frame, expected, childPid) {
  if (frame?.state === "rejected") return rejectHelperFrame(frame);
  const keys = ["schema", "nonce", "state", "keeperPid", "keeperCreationTime100ns", "kernelPid", "kernelCreationTime100ns", "connectionGeneration", "appPath", "files"];
  if (!isPlainExactObject(frame, keys) || frame.schema !== SCHEMA || frame.state !== "ready" || frame.nonce !== expected.nonce ||
      !Number.isSafeInteger(childPid) || childPid < 1 || frame.keeperPid !== childPid ||
      typeof frame.keeperCreationTime100ns !== "string" || !FILETIME.test(frame.keeperCreationTime100ns) ||
      BigInt(frame.keeperCreationTime100ns) > 0xffffffffffffffffn ||
      frame.kernelPid !== expected.kernelPid || frame.kernelCreationTime100ns !== expected.kernelCreationTime100ns ||
      frame.connectionGeneration !== expected.connectionGeneration || frame.appPath !== expected.appPath ||
      !Array.isArray(frame.files) || frame.files.length !== FILE_PINS.length) throw codedError("keeper_frame_invalid");
  for (let index = 0; index < FILE_PINS.length; index += 1) {
    const pin = FILE_PINS[index];
    const file = frame.files[index];
    const expectedFinalPath = path.win32.join(APP_PATH, pin.name);
    if (!isPlainExactObject(file, ["name", "nativeVolumeSerial", "nativeFileId", "canonicalFinalPath", "size", "lastWriteTime100ns", "sha256"]) ||
        file.name !== pin.name || typeof file.nativeVolumeSerial !== "string" || !/^(0|[1-9][0-9]{0,19})$/.test(file.nativeVolumeSerial) ||
        BigInt(file.nativeVolumeSerial) > 0xffffffffffffffffn || typeof file.nativeFileId !== "string" || !FILE_ID.test(file.nativeFileId) ||
        typeof file.canonicalFinalPath !== "string" || file.canonicalFinalPath.length > 32768 || file.canonicalFinalPath.includes("\0") ||
        path.win32.normalize(file.canonicalFinalPath).toLowerCase() !== path.win32.normalize(expectedFinalPath).toLowerCase() ||
        !Number.isSafeInteger(file.size) || file.size < 0 || file.size > pin.maxBytes ||
        typeof file.lastWriteTime100ns !== "string" || !FILETIME.test(file.lastWriteTime100ns) ||
        BigInt(file.lastWriteTime100ns) > 0xffffffffffffffffn || typeof file.sha256 !== "string" || file.sha256 !== pin.sha256 || !SHA256.test(file.sha256)) {
      throw codedError("keeper_frame_invalid");
    }
  }
  return frame;
}

function sameBoundFrames(ready, final) {
  if (final?.state === "rejected") return rejectHelperFrame(final);
  const keys = ["schema", "nonce", "state", "keeperPid", "keeperCreationTime100ns", "kernelPid", "kernelCreationTime100ns", "connectionGeneration", "appPath", "files"];
  if (!isPlainExactObject(final, keys) || final.schema !== SCHEMA || final.state !== "final" ||
      final.nonce !== ready.nonce || final.keeperPid !== ready.keeperPid ||
      final.keeperCreationTime100ns !== ready.keeperCreationTime100ns || final.kernelPid !== ready.kernelPid ||
      final.kernelCreationTime100ns !== ready.kernelCreationTime100ns || final.connectionGeneration !== ready.connectionGeneration ||
      final.appPath !== ready.appPath || !Array.isArray(final.files) || final.files.length !== ready.files.length) {
    throw codedError("keeper_frame_invalid");
  }
  for (let index = 0; index < ready.files.length; index += 1) {
    const left = ready.files[index];
    const right = final.files[index];
    if (!isPlainExactObject(right, ["name", "nativeVolumeSerial", "nativeFileId", "canonicalFinalPath", "size", "lastWriteTime100ns", "sha256"]) ||
        Object.keys(left).some((key) => left[key] !== right[key])) throw codedError("keeper_frame_invalid");
  }
  return final;
}

function createDeferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function watchChildClose(child) {
  const closed = createDeferred();
  const state = { closed: false, info: null, onClose: null, promise: closed.promise };
  if (child && typeof child.on === "function") {
    child.on("close", (exitCode, signal) => {
      if (state.closed) return;
      state.closed = true;
      state.info = { exitCode, signal };
      closed.resolve(state.info);
      state.onClose?.(state.info);
    });
  }
  return state;
}

async function cleanupSpawnedChild(child, closeState, waitMs = CHILD_CLEANUP_WAIT_MS, terminate = true) {
  if (!child) return;
  try { child.stdin?.end?.(); } catch { /* bounded cleanup */ }
  try { child.stdout?.resume?.(); } catch { /* drain private output */ }
  try { child.stderr?.resume?.(); } catch { /* drain private output */ }
  if (!closeState?.closed) {
    if (terminate) {
      try { child.kill?.(); } catch { /* bounded cleanup */ }
    }
    if (closeState?.promise) {
      let timer;
      await Promise.race([
        closeState.promise,
        new Promise((resolve) => { timer = setTimeout(resolve, waitMs); timer.unref?.(); }),
      ]);
      if (timer) clearTimeout(timer);
    }
  }
  for (const stream of [child.stdin, child.stdout, child.stderr]) {
    try { if (stream && !stream.destroyed) stream.destroy(); } catch { /* bounded cleanup */ }
  }
}

function parsePythonExecutableOutput(bytes) {
  try {
    if (!Buffer.isBuffer(bytes) || bytes.length < 2 || bytes.length > PYTHON_RESOLVER_MAX_OUTPUT_BYTES || bytes[bytes.length - 1] !== 0x0a) {
      throw codedError("keeper_interpreter_output_invalid");
    }
    let text = new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, -1));
    if (text.endsWith("\r")) text = text.slice(0, -1);
    if (text.includes("\n") || text.includes("\r")) throw codedError("keeper_interpreter_output_invalid");
    scanJsonNoDuplicateKeys(text);
    const value = JSON.parse(text);
    if (!isPlainExactObject(value, ["executable"]) || typeof value.executable !== "string" ||
        value.executable.length > 32768 || /[\u0000-\u001f\u007f]/.test(value.executable) || !path.isAbsolute(value.executable)) {
      throw codedError("keeper_interpreter_output_invalid");
    }
    return value.executable;
  } catch {
    throw codedError("keeper_interpreter_output_invalid");
  }
}

async function resolveBasePythonExecutable(venvPythonPath, {
  spawnProcess = spawn,
  timeoutMs = PYTHON_RESOLVER_MAX_RUNTIME_MS,
  cleanupWaitMs = CHILD_CLEANUP_WAIT_MS,
} = {}) {
  if (typeof venvPythonPath !== "string" || !path.isAbsolute(venvPythonPath) || venvPythonPath.includes("\0") ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > PYTHON_RESOLVER_MAX_RUNTIME_MS ||
      !Number.isSafeInteger(cleanupWaitMs) || cleanupWaitMs < 1 || cleanupWaitMs > CHILD_CLEANUP_WAIT_MS) {
    throw codedError("keeper_options_invalid");
  }

  let childErrorObserved = false;
  let onChildErrorObserved = null;
  let child;
  try {
    child = spawnProcess(venvPythonPath, ["-I", "-c", PYTHON_RESOLVER_SCRIPT], {
      cwd: REPOSITORY_ROOT,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch {
    throw codedError("keeper_interpreter_spawn_failed");
  }
  if (child && typeof child.on === "function") {
    child.on("error", () => {
      childErrorObserved = true;
      onChildErrorObserved?.();
    });
  }

  let settled = false;
  let failureCode = null;
  let stdout = Buffer.alloc(0);
  let stderrBytes = 0;
  let timeout = null;
  const closeState = watchChildClose(child);
  let resolveResult;
  let rejectResult;
  const result = new Promise((resolve, reject) => { resolveResult = resolve; rejectResult = reject; });
  result.catch(() => {});
  const kill = () => { try { child?.kill?.(); } catch { /* bounded cleanup */ } };
  const fail = (code) => {
    if (settled) return;
    settled = true;
    failureCode = code;
    if (timeout) clearTimeout(timeout);
    rejectResult(codedError(code));
    if (!closeState.closed) kill();
  };
  onChildErrorObserved = () => fail("keeper_interpreter_spawn_failed");
  if (childErrorObserved) onChildErrorObserved();
  const validChild = Boolean(child && typeof child.on === "function" && child.stdout && typeof child.stdout.on === "function" &&
    child.stderr && typeof child.stderr.on === "function" && typeof child.kill === "function");
  if (!validChild) fail(childErrorObserved ? "keeper_interpreter_spawn_failed" : "keeper_interpreter_child_invalid");

  if (validChild && !failureCode) {
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (stdout.length + bytes.length > PYTHON_RESOLVER_MAX_OUTPUT_BYTES) {
        fail("keeper_interpreter_output_invalid");
        return;
      }
      stdout = Buffer.concat([stdout, bytes]);
    });
    child.stderr.on("data", (chunk) => {
      if (settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      stderrBytes += bytes.length;
      if (stderrBytes > PYTHON_RESOLVER_MAX_OUTPUT_BYTES) fail("keeper_interpreter_output_invalid");
    });
    child.stdout.on("error", () => fail("keeper_interpreter_output_invalid"));
    child.stderr.on("error", () => fail("keeper_interpreter_output_invalid"));
    closeState.onClose = ({ exitCode, signal }) => {
      if (settled) return;
      if (exitCode !== 0 || signal !== null) {
        fail("keeper_interpreter_resolve_failed");
        return;
      }
      try {
        const executable = parsePythonExecutableOutput(stdout);
        settled = true;
        if (timeout) clearTimeout(timeout);
        resolveResult(executable);
      } catch {
        fail("keeper_interpreter_output_invalid");
      }
    };
    if (closeState.info) closeState.onClose(closeState.info);
    if (!settled) {
      timeout = setTimeout(() => fail("keeper_interpreter_timeout"), timeoutMs);
      timeout.unref?.();
    }
  }

  try {
    return await result;
  } catch (error) {
    await cleanupSpawnedChild(child, closeState, cleanupWaitMs, false);
    throw codedError(failureCode || (CLIENT_REJECTION_CODES.has(error?.code) ? error.code : "keeper_interpreter_resolve_failed"));
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

async function startAsarPhysicalKeeper(options, {
  spawnProcess = spawn,
  resolverSpawnProcess = spawnProcess,
  resolverTimeoutMs = PYTHON_RESOLVER_MAX_RUNTIME_MS,
  resolverCleanupWaitMs = CHILD_CLEANUP_WAIT_MS,
  randomBytesFn = randomBytes,
  now = Date.now,
  runtimeMs = MAX_RUNTIME_MS,
  terminateWaitMs = 1000,
} = {}) {
  validateRequest(options);
  if (!Number.isSafeInteger(runtimeMs) || runtimeMs < 1 || runtimeMs > MAX_RUNTIME_MS ||
      !Number.isSafeInteger(terminateWaitMs) || terminateWaitMs < 1 || terminateWaitMs > 1000) throw codedError("keeper_options_invalid");
  const nonce = randomBytesFn(32).toString("hex");
  if (!NONCE.test(nonce)) throw codedError("keeper_options_invalid");
  const request = {
    schema: SCHEMA,
    nonce,
    kernelPid: options.kernelPid,
    kernelCreationTime100ns: options.kernelCreationTime100ns,
    connectionGeneration: options.connectionGeneration,
    appPath: options.appPath,
  };
  const requestBytes = Buffer.from(`${JSON.stringify(request)}\n`, "utf8");
  if (requestBytes.length > MAX_INPUT_LINE_BYTES) throw codedError("keeper_input_failed");

  const basePythonPath = await resolveBasePythonExecutable(options.probePythonPath, {
    spawnProcess: resolverSpawnProcess,
    timeoutMs: resolverTimeoutMs,
    cleanupWaitMs: resolverCleanupWaitMs,
  });

  let child;
  try {
    child = spawnProcess(basePythonPath, [KEEPER_HELPER_PATH, "--stdio"], {
      cwd: REPOSITORY_ROOT,
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch {
    throw codedError("keeper_spawn_failed");
  }
  let spawnErrorObserved = false;
  let onSpawnErrorObserved = null;
  if (child && typeof child.on === "function") {
    child.on("error", () => {
      spawnErrorObserved = true;
      onSpawnErrorObserved?.();
    });
  }
  const closeState = watchChildClose(child);
  if (!child || !Number.isSafeInteger(child.pid) || child.pid < 1 || !child.stdin || !child.stdout || !child.stderr ||
      typeof child.kill !== "function" || typeof child.stdin.write !== "function" || typeof child.stdout.on !== "function") {
    await cleanupSpawnedChild(child, closeState, terminateWaitMs);
    throw codedError(spawnErrorObserved ? "keeper_spawn_failed" : "keeper_child_invalid");
  }

  const childPid = child.pid;
  const deadline = now() + runtimeMs;
  const closed = createDeferred();
  let closeInfo = closeState.info;
  let phase = "initial_wait";
  let frameWaiter = null;
  let buffer = Buffer.alloc(0);
  let fatalCode = null;
  let terminationPromise = null;
  let finished = false;
  let killRequested = false;

  const setFatal = (code) => {
    if (fatalCode) return;
    fatalCode = code;
    phase = "failed";
    if (frameWaiter) {
      const waiter = frameWaiter;
      frameWaiter = null;
      clearTimeout(waiter.timer);
      waiter.fail(codedError(code));
    }
    if (!closeInfo) killChild();
  };

  const killChild = () => {
    if (killRequested) return;
    killRequested = true;
    try { child.kill(); } catch { /* cleanup is bounded and never forwards process errors */ }
  };

  onSpawnErrorObserved = () => setFatal("keeper_spawn_failed");
  closeState.onClose = ({ exitCode, signal }) => {
    closeInfo = { exitCode, signal };
    closed.resolve(closeInfo);
    if (phase === "initial_wait" || phase === "final_wait") setFatal("keeper_output_eof");
    else if (phase === "holding") setFatal("keeper_process_exited");
  };
  if (spawnErrorObserved) onSpawnErrorObserved();
  if (closeState.info) closeState.onClose(closeState.info);

  const remainingMs = () => Math.max(0, deadline - now());

  const frameFor = (waitPhase, receivedPhase) => new Promise((resolve, reject) => {
    if (fatalCode) { reject(codedError(fatalCode)); return; }
    const remaining = remainingMs();
    if (remaining <= 0) { setFatal("keeper_timeout"); reject(codedError("keeper_timeout")); return; }
    phase = waitPhase;
    const timer = setTimeout(() => setFatal("keeper_timeout"), remaining);
    timer.unref?.();
    frameWaiter = {
      timer,
      complete(value) {
        if (frameWaiter !== this) return;
        frameWaiter = null;
        clearTimeout(timer);
        phase = receivedPhase;
        resolve(value);
      },
      fail(error) {
        if (frameWaiter === this) frameWaiter = null;
        clearTimeout(timer);
        reject(error);
      },
    };
  });

  const writeLine = (value) => new Promise((resolve, reject) => {
    if (fatalCode || closeInfo) { reject(codedError(fatalCode || "keeper_process_exited")); return; }
    const bytes = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
    if (bytes.length > MAX_INPUT_LINE_BYTES || remainingMs() <= 0) { reject(codedError("keeper_input_failed")); return; }
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(codedError("keeper_timeout"));
      setFatal("keeper_timeout");
    }, remainingMs());
    timer.unref?.();
    try {
      child.stdin.write(bytes, (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error || fatalCode) reject(codedError(fatalCode || "keeper_input_failed"));
        else resolve();
      });
    } catch {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(codedError("keeper_input_failed"));
      }
    }
  });

  child.stdout.on("data", (chunk) => {
    if (fatalCode) return;
    if (phase !== "initial_wait" && phase !== "final_wait") { setFatal("keeper_unexpected_output"); return; }
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    if (buffer.length + bytes.length > MAX_OUTPUT_LINE_BYTES) { setFatal("keeper_output_too_large"); return; }
    buffer = Buffer.concat([buffer, bytes]);
    const newline = buffer.indexOf(0x0a);
    if (newline < 0) return;
    if (newline !== buffer.length - 1) { setFatal("keeper_unexpected_output"); return; }
    const currentPhase = phase;
    const waiter = frameWaiter;
    if (!waiter || (currentPhase !== "initial_wait" && currentPhase !== "final_wait")) { setFatal("keeper_unexpected_output"); return; }
    let frame;
    try { frame = parseOutputLine(buffer); }
    catch { setFatal("keeper_output_invalid"); return; }
    buffer = Buffer.alloc(0);
    waiter.complete(frame);
  });
  child.stdout.on("end", () => {
    if (!closeInfo && !fatalCode && phase !== "final_received") setFatal("keeper_output_eof");
  });
  child.stdout.on("error", () => setFatal("keeper_output_eof"));
  child.stderr.on("data", () => { /* Drain private helper stderr without forwarding its contents. */ });
  child.stderr.on("error", () => { /* stderr is diagnostic-free and non-authoritative. */ });
  child.stdin.on?.("error", () => setFatal("keeper_input_failed"));

  const terminate = () => {
    if (terminationPromise) return terminationPromise;
    terminationPromise = (async () => {
      if (child.stdin && !child.stdin.destroyed) {
        try { child.stdin.end(); } catch { /* process cleanup */ }
      }
      try { child.stdout.resume?.(); } catch { /* continue draining */ }
      try { child.stderr.resume?.(); } catch { /* continue draining */ }
      if (!closeInfo) killChild();
      if (!closeInfo) {
        let timer;
        await Promise.race([
          closed.promise,
          new Promise((resolve) => { timer = setTimeout(resolve, terminateWaitMs); timer.unref?.(); }),
        ]);
        if (timer) clearTimeout(timer);
      }
      if (child.stdout && !child.stdout.destroyed) child.stdout.destroy();
      if (child.stderr && !child.stderr.destroyed) child.stderr.destroy();
      if (child.stdin && !child.stdin.destroyed) child.stdin.destroy();
    })();
    return terminationPromise;
  };

  let readyPromise = null;
  try {
    readyPromise = frameFor("initial_wait", "initial_received");
    // Output may fail before the stdin write callback settles. Attach a
    // rejection handler now, while preserving the rejection for the await.
    readyPromise.catch(() => {});
    await writeLine(request);
    const ready = validateReadyFrame(await readyPromise, { ...request }, childPid);
    if (fatalCode || closeInfo) throw codedError(fatalCode || "keeper_process_exited");
    phase = "holding";
    const session = Object.freeze({
      childPid,
      nonce,
      readyFrame: ready,
      async finish() {
        if (finished) throw codedError("keeper_frame_invalid");
        if (fatalCode || closeInfo || phase !== "holding") throw codedError(fatalCode || "keeper_process_exited");
        finished = true;
        let finalPromise = null;
        try {
          const finishRequest = { op: "finish", nonce };
          finalPromise = frameFor("final_wait", "final_received");
          // Keep early output/EOF failures observed during a delayed write.
          finalPromise.catch(() => {});
          await writeLine(finishRequest);
          const final = sameBoundFrames(ready, await finalPromise);
          if (fatalCode) throw codedError(fatalCode);
          const remaining = remainingMs();
          if (remaining <= 0) throw codedError("keeper_timeout");
          let timer;
          const closeResult = await Promise.race([
            closed.promise,
            new Promise((_, reject) => { timer = setTimeout(() => reject(codedError("keeper_timeout")), remaining); timer.unref?.(); }),
          ]).finally(() => { if (timer) clearTimeout(timer); });
          if (fatalCode) throw codedError(fatalCode);
          if (closeResult.exitCode !== 0 || closeResult.signal !== null) throw codedError("keeper_process_exit_invalid");
          if (buffer.length !== 0) throw codedError("keeper_unexpected_output");
          phase = "finished";
          return final;
        } catch (error) {
          const failureCode = CLIENT_REJECTION_CODES.has(error?.code) || HELPER_REJECTION_CODES.has(error?.code)
            ? error.code : "keeper_frame_invalid";
          setFatal(failureCode);
          await finalPromise?.catch(() => {});
          await terminate();
          throw codedError(fatalCode || "keeper_frame_invalid");
        }
      },
      async cancel() {
        if (phase !== "finished") {
          if (!fatalCode) setFatal("keeper_process_exited");
          await terminate();
        }
      },
    });
    return session;
  } catch (error) {
    const failureCode = CLIENT_REJECTION_CODES.has(error?.code) || HELPER_REJECTION_CODES.has(error?.code)
      ? error.code : "keeper_frame_invalid";
    setFatal(failureCode);
    await readyPromise?.catch(() => {});
    await terminate();
    throw codedError(fatalCode || "keeper_frame_invalid");
  }
}

module.exports = Object.freeze({
  SCHEMA,
  APP_PATH,
  KEEPER_HELPER_PATH,
  FILE_PINS,
  HELPER_REJECTION_CODES,
  MAX_INPUT_LINE_BYTES,
  MAX_OUTPUT_LINE_BYTES,
  MAX_RUNTIME_MS,
  parseOutputLine,
  validateReadyFrame,
  sameBoundFrames,
  startAsarPhysicalKeeper,
});
