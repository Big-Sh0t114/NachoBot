"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const path = require("node:path");
const {
  APP_PATH,
  FILE_PINS,
  KEEPER_HELPER_PATH,
  SCHEMA,
  startAsarPhysicalKeeper,
} = require("../bridge/asar-keeper-client.cjs");

const CREATION_TIME = "134000000000000000";
const GENERATION = 77;
const KERNEL_PID = 12345;
const BASE_PYTHON_PATH = String.raw`C:\Python312\python.exe`;

function options(overrides = {}) {
  return {
    kernelPid: KERNEL_PID,
    kernelCreationTime100ns: CREATION_TIME,
    connectionGeneration: GENERATION,
    appPath: APP_PATH,
    probePythonPath: process.execPath,
    ...overrides,
  };
}

function fileRecords(overrides = {}) {
  return FILE_PINS.map((pin, index) => ({
    name: pin.name,
    nativeVolumeSerial: "12",
    nativeFileId: (index + 1).toString(16).toUpperCase().padStart(32, "0"),
    canonicalFinalPath: path.win32.join(APP_PATH, pin.name),
    size: 128 + index,
    lastWriteTime100ns: "134000000000000001",
    sha256: pin.sha256,
    ...(overrides[index] || {}),
  }));
}

function frame(state, nonce, overrides = {}) {
  return {
    schema: SCHEMA,
    nonce,
    state,
    keeperPid: 9001,
    keeperCreationTime100ns: "134000000000000010",
    kernelPid: KERNEL_PID,
    kernelCreationTime100ns: CREATION_TIME,
    connectionGeneration: GENERATION,
    appPath: APP_PATH,
    files: fileRecords(),
    ...overrides,
  };
}

class FakeKeeperChild extends EventEmitter {
  constructor({ ready = "valid", final = "valid", closeCode = 0, finalDelayMs = 1,
    initialWriteCallbackDelayMs = 0, finishWriteCallbackDelayMs = 0,
    readyFailureBeforeWriteCallback = null, finalFailureBeforeWriteCallback = null } = {}) {
    super();
    this.pid = 9001;
    this.killed = 0;
    this.closed = false;
    this.closeCode = closeCode;
    this.finalMode = final;
    this.finalDelayMs = finalDelayMs;
    this.initialWriteCallbackDelayMs = initialWriteCallbackDelayMs;
    this.finishWriteCallbackDelayMs = finishWriteCallbackDelayMs;
    this.readyFailureBeforeWriteCallback = readyFailureBeforeWriteCallback;
    this.finalFailureBeforeWriteCallback = finalFailureBeforeWriteCallback;
    this.sent = [];
    this.stderrText = "";
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.stderr.on("data", (chunk) => { this.stderrText += chunk.toString("utf8"); });
    this.stdin = new Writable({
      write: (chunk, _encoding, callback) => {
        const text = chunk.toString("utf8");
        this.sent.push(text);
        let request;
        try { request = JSON.parse(text); }
        catch { callback(new Error("fake_input_invalid")); return; }
        const isFinish = Object.hasOwn(request, "op");
        const earlyFailure = isFinish ? this.finalFailureBeforeWriteCallback : this.readyFailureBeforeWriteCallback;
        const callbackDelay = isFinish ? this.finishWriteCallbackDelayMs : this.initialWriteCallbackDelayMs;
        if (earlyFailure) {
          queueMicrotask(() => {
            if (this.closed) return;
            if (earlyFailure === "eof") this.close(null, "SIGTERM");
            else if (earlyFailure === "oversize") this.stdout.write(Buffer.alloc(64 * 1024 + 1, 0x61));
            else this.stdout.write("not-json\n");
          });
          setTimeout(() => callback(), callbackDelay);
          return;
        }
        if (callbackDelay > 0) setTimeout(() => callback(), callbackDelay);
        else callback();
        queueMicrotask(() => {
          if (this.closed) return;
          if (!Object.hasOwn(request, "op")) {
            if (ready === "silent") return;
            if (ready === "eof") { this.close(null, "SIGTERM"); return; }
            if (ready === "oversize") { this.stdout.write(Buffer.alloc(64 * 1024 + 1, 0x61)); return; }
            if (ready === "extra") { this.stdout.write(`${JSON.stringify(frame("ready", request.nonce))}\nextra`); return; }
            if (ready === "duplicate") {
              this.stdout.write(`{"schema":"${SCHEMA}","schema":"${SCHEMA}","state":"rejected","code":"internal_error"}\n`);
              return;
            }
            if (ready === "reject") {
              this.stdout.write(`${JSON.stringify({ schema: SCHEMA, state: "rejected", code: "component_reparse_rejected" })}\n`);
              return;
            }
            const response = frame("ready", request.nonce);
            if (ready === "bad_nonce") response.nonce = "f".repeat(64);
            if (ready === "bad_pid") response.keeperPid += 1;
            if (ready === "bad_file_id") response.files[0].nativeFileId = "not-a-file-id";
            this.stdout.write(`${JSON.stringify(response)}\n`);
            return;
          }
          if (request.op !== "finish") return;
          if (this.finalMode === "eof") { this.close(null, "SIGTERM"); return; }
          if (this.finalMode === "silent") return;
          const response = frame("final", request.nonce);
          if (this.finalMode === "changed_file") response.files[1].nativeFileId = "A".repeat(32);
          if (this.finalMode === "changed_keeper") response.keeperCreationTime100ns = "134000000000000011";
          if (this.finalMode === "wrong_exit") this.closeCode = 2;
          setTimeout(() => {
            if (this.closed) return;
            this.stdout.write(`${JSON.stringify(response)}\n`);
            setTimeout(() => this.close(this.closeCode, null), this.finalDelayMs);
          }, 0);
        });
      },
    });
  }

  close(code, signal) {
    if (this.closed) return;
    this.closed = true;
    this.stdout.end();
    this.stderr.end();
    this.emit("close", code, signal);
  }

  kill() {
    this.killed += 1;
    queueMicrotask(() => this.close(null, "SIGTERM"));
    return true;
  }
}

class FakePythonResolverChild extends EventEmitter {
  constructor({ mode = "valid" } = {}) {
    super();
    this.pid = 7331;
    this.killed = 0;
    this.closed = false;
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    queueMicrotask(() => {
      if (mode === "timeout") return;
      if (mode === "async_enoent") {
        this.errorListenersAtEmit = this.listenerCount("error");
        if (this.errorListenersAtEmit > 0) this.emit("error", Object.assign(new Error("private spawn detail"), { code: "ENOENT" }));
        else this.unobservedError = true;
        this.close(-2, null);
        return;
      }
      let output = `${JSON.stringify({ executable: BASE_PYTHON_PATH })}\n`;
      if (mode === "crlf") output = output.replace("\n", "\r\n");
      if (mode === "malformed") output = "not-json\n";
      if (mode === "extra") output += `${JSON.stringify({ executable: BASE_PYTHON_PATH })}\n`;
      if (mode === "oversize") output = "x".repeat(4097);
      if (mode !== "eof") this.stdout.write(Buffer.from(output, "utf8"));
      if (mode === "stderr_oversize") this.stderr.write(Buffer.alloc(4097, 0x78));
      this.stdout.end();
      this.stderr.end();
      setImmediate(() => this.close(mode === "nonzero" ? 1 : 0, null));
    });
  }

  close(exitCode, signal) {
    if (this.closed) return;
    this.closed = true;
    if (!this.stdout.destroyed) this.stdout.end();
    if (!this.stderr.destroyed) this.stderr.end();
    this.emit("close", exitCode, signal);
  }

  kill() {
    this.killed += 1;
    setImmediate(() => this.close(null, "SIGTERM"));
    return true;
  }
}

function fakeSpawn(childOptions = {}, resolverOptions = {}) {
  const state = { calls: [], child: null };
  const spawnProcess = (command, args, spawnOptions) => {
    state.calls.push({ command, args, spawnOptions });
    state.child = new FakeKeeperChild(childOptions);
    return state.child;
  };
  const resolver = fakeResolverSpawn(resolverOptions);
  return { state, spawnProcess, resolverSpawnProcess: resolver.spawnProcess, resolverState: resolver.state };
}

function fakeResolverSpawn({ mode = "valid" } = {}) {
  const state = { calls: [], child: null };
  const spawnProcess = (command, args, spawnOptions) => {
    state.calls.push({ command, args, spawnOptions });
    state.child = new FakePythonResolverChild({ mode });
    return state.child;
  };
  return { state, spawnProcess };
}

function fakeAsyncEnoentHelperChild() {
  const child = new EventEmitter();
  child.pid = undefined;
  child.killed = 0;
  child.closed = false;
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => {
    child.killed += 1;
    queueMicrotask(() => child.close(-2, null));
    return false;
  };
  child.close = (exitCode, signal) => {
    if (child.closed) return;
    child.closed = true;
    child.stdout.end();
    child.stderr.end();
    child.emit("close", exitCode, signal);
  };
  queueMicrotask(() => {
    child.errorListenersAtEmit = child.listenerCount("error");
    if (child.errorListenersAtEmit > 0) child.emit("error", Object.assign(new Error("private spawn detail"), { code: "ENOENT" }));
    else child.unobservedError = true;
    child.close(-2, null);
  });
  return child;
}

function keeperDependencies(fake, overrides = {}) {
  return {
    spawnProcess: fake.spawnProcess,
    resolverSpawnProcess: fake.resolverSpawnProcess,
    randomBytesFn: deterministicNonce,
    ...overrides,
  };
}

function deterministicNonce(size) {
  return Buffer.alloc(size, 0xab);
}

test("keeper client binds initial request, ready/final metadata, same PID and pins, and waits for exit 0", async () => {
  const fake = fakeSpawn({}, { mode: "crlf" });
  const session = await startAsarPhysicalKeeper(options(), keeperDependencies(fake));
  assert.equal(session.childPid, 9001);
  assert.equal(session.nonce, "ab".repeat(32));
  assert.equal(session.readyFrame.state, "ready");
  assert.equal(fake.state.calls.length, 1);
  assert.equal(fake.resolverState.calls.length, 1);
  assert.equal(fake.resolverState.calls[0].command, process.execPath);
  assert.deepEqual(fake.resolverState.calls[0].args.slice(0, 2), ["-I", "-c"]);
  assert.ok(fake.resolverState.calls[0].args[2].includes("_base_executable"));
  assert.equal(fake.resolverState.calls[0].spawnOptions.shell, false);
  assert.equal(fake.resolverState.calls[0].spawnOptions.stdio[0], "ignore");
  const spawnCall = fake.state.calls[0];
  assert.equal(spawnCall.command, BASE_PYTHON_PATH);
  assert.deepEqual(spawnCall.args, [KEEPER_HELPER_PATH, "--stdio"]);
  assert.equal(spawnCall.spawnOptions.shell, false);
  assert.equal(spawnCall.spawnOptions.windowsHide, true);
  assert.equal(spawnCall.spawnOptions.cwd, path.resolve(__dirname, ".."));
  assert.equal(fake.state.child.stderrText, "");
  const initial = JSON.parse(fake.state.child.sent[0]);
  assert.deepEqual(Object.keys(initial).sort(), ["appPath", "connectionGeneration", "kernelCreationTime100ns", "kernelPid", "nonce", "schema"].sort());
  assert.deepEqual(initial, {
    schema: SCHEMA,
    nonce: session.nonce,
    kernelPid: KERNEL_PID,
    kernelCreationTime100ns: CREATION_TIME,
    connectionGeneration: GENERATION,
    appPath: APP_PATH,
  });
  const final = await session.finish();
  assert.equal(final.state, "final");
  assert.deepEqual(final.files, session.readyFrame.files);
  assert.deepEqual(JSON.parse(fake.state.child.sent[1]), { op: "finish", nonce: session.nonce });
  assert.equal(fake.state.child.closed, true);
  await session.cancel();
  assert.equal(fake.state.child.killed, 0);
});

test("Python resolver rejects bounded malformed output and cleans its child without spawning the keeper", async () => {
  const cases = [
    ["malformed", "keeper_interpreter_output_invalid"],
    ["extra", "keeper_interpreter_output_invalid"],
    ["oversize", "keeper_interpreter_output_invalid"],
    ["eof", "keeper_interpreter_output_invalid"],
    ["stderr_oversize", "keeper_interpreter_output_invalid"],
    ["nonzero", "keeper_interpreter_resolve_failed"],
    ["async_enoent", "keeper_interpreter_spawn_failed"],
  ];
  for (const [mode, code] of cases) {
    const fake = fakeSpawn();
    const resolver = fakeResolverSpawn({ mode });
    await assert.rejects(startAsarPhysicalKeeper(options(), {
      spawnProcess: fake.spawnProcess,
      resolverSpawnProcess: resolver.spawnProcess,
      randomBytesFn: deterministicNonce,
    }), { code });
    assert.equal(fake.state.calls.length, 0, `${mode} must not spawn the physical keeper`);
    assert.ok(resolver.state.child.closed || resolver.state.child.killed > 0, `${mode} helper process is cleaned up`);
    assert.equal(resolver.state.child.unobservedError, undefined);
    if (mode === "async_enoent") assert.ok(resolver.state.child.errorListenersAtEmit > 0);
  }

  const timeoutFake = fakeSpawn();
  const timeoutResolver = fakeResolverSpawn({ mode: "timeout" });
  await assert.rejects(startAsarPhysicalKeeper(options(), {
    spawnProcess: timeoutFake.spawnProcess,
    resolverSpawnProcess: timeoutResolver.spawnProcess,
    resolverTimeoutMs: 20,
    randomBytesFn: deterministicNonce,
  }), { code: "keeper_interpreter_timeout" });
  assert.equal(timeoutFake.state.calls.length, 0);
  assert.equal(timeoutResolver.state.child.killed, 1);
  assert.equal(timeoutResolver.state.child.closed, true);
});

test("keeper spawn observes async ENOENT before rejecting a child with no PID", async () => {
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error?.code || "unclassified");
  process.on("unhandledRejection", onUnhandled);
  const fake = fakeSpawn();
  const resolver = fakeResolverSpawn();
  let invalidChild;
  const spawnProcess = () => {
    invalidChild = fakeAsyncEnoentHelperChild();
    return invalidChild;
  };
  try {
    await assert.rejects(startAsarPhysicalKeeper(options(), {
      spawnProcess,
      resolverSpawnProcess: resolver.spawnProcess,
      randomBytesFn: deterministicNonce,
    }), { code: "keeper_spawn_failed" });
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(invalidChild.errorListenersAtEmit > 0);
    assert.equal(invalidChild.unobservedError, undefined);
    assert.equal(invalidChild.closed, true);
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("keeper client converts helper rejection to its fixed code and terminates that child", async () => {
  const fake = fakeSpawn({ ready: "reject" });
  await assert.rejects(startAsarPhysicalKeeper(options(), keeperDependencies(fake)),
    { code: "component_reparse_rejected" });
  assert.equal(fake.state.child.killed, 1);
  assert.equal(fake.state.calls.length, 1);
});

test("keeper client rejects ready/final PID, nonce, identity, and pin mismatches without restarting", async () => {
  for (const ready of ["bad_nonce", "bad_pid", "bad_file_id"]) {
    const fake = fakeSpawn({ ready });
    await assert.rejects(startAsarPhysicalKeeper(options(), keeperDependencies(fake)),
      { code: "keeper_frame_invalid" });
    assert.equal(fake.state.calls.length, 1);
    assert.equal(fake.state.child.killed, 1);
  }
  for (const final of ["changed_file", "changed_keeper"]) {
    const fake = fakeSpawn({ final, finalDelayMs: 20 });
    const session = await startAsarPhysicalKeeper(options(), keeperDependencies(fake));
    await assert.rejects(session.finish(), { code: "keeper_frame_invalid" });
    assert.equal(fake.state.calls.length, 1, "a changed keeper is never restarted or substituted");
    assert.ok(fake.state.child.killed >= 1);
  }
});

test("keeper client rejects EOF, extra frames, oversize, and duplicate-key JSON with no raw details", async () => {
  for (const ready of ["eof", "extra", "oversize", "duplicate"]) {
    const fake = fakeSpawn({ ready });
    await assert.rejects(startAsarPhysicalKeeper(options(), keeperDependencies(fake)), (error) => {
      assert.match(error.code, /^keeper_/);
      assert.doesNotMatch(error.code, /C:\\|private|token/i);
      return true;
    });
    assert.equal(fake.state.calls.length, 1);
    assert.equal(fake.state.child.killed, ready === "eof" ? 0 : 1);
  }
});

test("keeper client enforces the bounded lifetime, finish EOF, and exit status", async () => {
  const timeoutFake = fakeSpawn({ ready: "silent" });
  const timeoutResolver = fakeResolverSpawn();
  await assert.rejects(startAsarPhysicalKeeper(options(), {
    spawnProcess: timeoutFake.spawnProcess,
    resolverSpawnProcess: timeoutResolver.spawnProcess,
    randomBytesFn: deterministicNonce,
    runtimeMs: 20,
  }), { code: "keeper_timeout" });
  assert.equal(timeoutFake.state.child.killed, 1);

  for (const final of ["eof", "wrong_exit"]) {
    const fake = fakeSpawn({ final });
    const session = await startAsarPhysicalKeeper(options(), keeperDependencies(fake));
    await assert.rejects(session.finish(), final === "eof" ? { code: "keeper_output_eof" } : { code: "keeper_process_exit_invalid" });
    assert.equal(fake.state.calls.length, 1);
    assert.equal(fake.state.child.killed, 0, "an already-exited helper is not restarted or signaled again");
  }
});

test("keeper client observes early output failures before delayed stdin callbacks", async () => {
  const unhandled = [];
  const onUnhandled = (error) => unhandled.push(error?.code || "unclassified");
  process.on("unhandledRejection", onUnhandled);
  try {
    const initialFake = fakeSpawn({
      initialWriteCallbackDelayMs: 40,
      readyFailureBeforeWriteCallback: "malformed",
    });
    await assert.rejects(startAsarPhysicalKeeper(options(), keeperDependencies(initialFake)), { code: "keeper_output_invalid" });
    assert.equal(initialFake.state.child.killed, 1);
    assert.equal(initialFake.state.child.closed, true);

    const finalFake = fakeSpawn({
      finishWriteCallbackDelayMs: 40,
      finalFailureBeforeWriteCallback: "eof",
    });
    const session = await startAsarPhysicalKeeper(options(), keeperDependencies(finalFake));
    await assert.rejects(session.finish(), { code: "keeper_output_eof" });
    assert.equal(finalFake.state.child.closed, true);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("keeper client rejects malformed arguments before spawning", async () => {
  const fake = fakeSpawn();
  await assert.rejects(startAsarPhysicalKeeper(options({ kernelPid: true }), { spawnProcess: fake.spawnProcess }), { code: "keeper_options_invalid" });
  await assert.rejects(startAsarPhysicalKeeper(options({ appPath: "C:\\unexpected\\app" }), { spawnProcess: fake.spawnProcess }), { code: "keeper_options_invalid" });
  assert.equal(fake.state.calls.length, 0);
});
