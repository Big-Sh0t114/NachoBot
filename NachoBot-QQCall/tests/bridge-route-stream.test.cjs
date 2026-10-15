"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createPersistentAudioRouteObserver } = require("../bridge/audio-route-observer.cjs");

function setup(t, { respond, timeoutMs = 100, autoClose = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-route-stream-"));
  const executable = path.join(root, "python.exe");
  fs.writeFileSync(executable, "test placeholder");
  const config = { schema: 1, hostGeneration: 10, hostPid: 100, pythonExecutable: executable,
    processes: [{ role: "host", pid: 100, creationTime100ns: "133240000000000001" }],
    captureRenderName: "test-render", playbackRenderName: "test-capture" };
  const calls = [];
  const observer = createPersistentAudioRouteObserver({ config, configPath: path.join(root, "config.json"), timeoutMs,
    spawnImpl(exe, args, options) {
      const child = new EventEmitter();
      child.stdin = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
      child.requests = []; child.kills = 0;
      child.kill = () => { child.kills++; if (autoClose) queueMicrotask(() => child.emit("close", 0)); return true; };
      child.stdin.write = (wire, callback) => {
        const request = JSON.parse(wire); child.requests.push(request);
        callback?.();
        if (respond) queueMicrotask(() => respond(child, request, config));
      };
      calls.push({ child, exe, args, options });
      return child;
    },
  });
  t.after(async () => { for (const call of calls) call.child.emit("close", 0); await observer.stop(); fs.rmSync(root, { recursive: true, force: true }); });
  return { observer, calls, config, request: { captureRenderName: config.captureRenderName, playbackRenderName: config.playbackRenderName } };
}

function report(config, overrides = {}) {
  const at = Date.now();
  const session = { role: "host", pid: 100, processGeneration: config.processes[0].creationTime100ns,
    processStartTimeUtc: "2026-10-05T01:00:00Z", firstObservedUtc: new Date(at).toISOString(),
    state: "active", processAttribution: "single", sessionGenerationSha256: "c".repeat(64) };
  return { schema: 1, source: "windows_active_sessions", observedAtMs: at, verified: true,
    hostGeneration: config.hostGeneration, hostPid: config.hostPid, errors: [],
    routes: [
      { key: "host_input", direction: "capture", endpointName: config.playbackRenderName,
        endpointIdSha256: "a".repeat(64), active: true, sessions: [session] },
      { key: "host_output", direction: "render", endpointName: config.captureRenderName,
        endpointIdSha256: "b".repeat(64), active: true, sessions: [{ ...session, sessionGenerationSha256: "d".repeat(64) }] },
    ], ...overrides };
}
function send(child, envelope) { child.stdout.emit("data", Buffer.from(JSON.stringify(envelope) + "\n")); }

test("multiple fresh observations reuse exactly one hidden isolated helper", async t => {
  const { observer, calls, request } = setup(t, { respond: (child, input, config) => send(child, { id: input.id, report: report(config) }) });
  assert.equal((await observer(request)).verified, true);
  assert.equal((await observer(request)).verified, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].child.requests.length, 2);
  assert.notEqual(calls[0].child.requests[0].id, calls[0].child.requests[1].id);
  assert.deepEqual(calls[0].args.slice(0, 2), ["-I", "-u"]);
  assert.equal(calls[0].args.at(-1), "--stdio");
  assert.equal(calls[0].options.windowsHide, true);
  assert.equal(calls[0].options.shell, false);
  assert.equal(calls[0].options.env.PYTHONNOUSERSITE, "1");
  assert.deepEqual(await observer.stop(), { stopped: true, quarantined: false });
  assert.equal((await observer(request)).verified, false);
});

test("fragmented response accepted, but stale proof and wrong Host rejected", async t => {
  let override = {};
  const { observer, request } = setup(t, { respond: (child, input, config) => {
    const wire = JSON.stringify({ id: input.id, report: report(config, override) }) + "\n";
    child.stdout.emit("data", Buffer.from(wire.slice(0, 40)));
    child.stdout.emit("data", Buffer.from(wire.slice(40)));
  } });
  assert.equal((await observer(request)).verified, true);
  override = { observedAtMs: Date.now() - 5000 };
  assert.equal((await observer(request)).verified, false);
  override = { hostPid: 101 };
  assert.equal((await observer(request)).verified, false);
});

test("single-flight and timeout quarantine last until actual process close", async t => {
  const { observer, request, calls } = setup(t, { timeoutMs: 15, autoClose: false });
  const first = observer(request);
  assert.equal((await observer(request)).code, "observer_busy");
  assert.equal((await first).code, "observer_timeout");
  assert.equal((await observer(request)).code, "observer_busy");
  assert.equal(calls.length, 1);
  calls[0].child.emit("close", 0);
  const second = observer(request);
  assert.equal(calls.length, 2);
  calls[1].child.emit("close", 1);
  assert.equal((await second).code, "observer_unavailable");
});

test("abort kills only owned helper and rejects late replies", async t => {
  const { observer, request, calls, config } = setup(t, { autoClose: false });
  const controller = new AbortController();
  const first = observer({ ...request, signal: controller.signal });
  controller.abort();
  assert.equal((await first).code, "observer_aborted");
  send(calls[0].child, { id: calls[0].child.requests[0].id, report: report(config) });
  assert.equal((await observer(request)).code, "observer_busy");
  assert.equal(calls[0].child.kills, 1);
});

for (const mode of ["wrong-id", "extra-line", "oversized", "invalid-json"]) {
  test(`protocol fault ${mode} cannot grant proof`, async t => {
    const { observer, request, calls } = setup(t, { respond: (child, input, config) => {
      const wire = JSON.stringify({ id: mode === "wrong-id" ? "0".repeat(32) : input.id, report: report(config) }) + "\n";
      child.stdout.emit("data", Buffer.from(mode === "oversized" ? "x".repeat(65537) :
        mode === "invalid-json" ? "?\n" : mode === "extra-line" ? wire + wire : wire));
    } });
    assert.equal((await observer(request)).verified, false);
    assert.equal(calls[0].child.kills, 1);
  });
}

test("endpoint mismatch and pre-aborted request do not launch a helper", async t => {
  const { observer, request, calls } = setup(t);
  assert.equal((await observer({ ...request, captureRenderName: "other" })).code, "endpoint_not_found");
  assert.equal((await observer({ ...request, signal: AbortSignal.abort() })).code, "observer_aborted");
  assert.equal(calls.length, 0);
});

test("stream failure releases pending request without trusting partial output", async t => {
  const { observer, request, calls } = setup(t);
  const pending = observer(request);
  calls[0].child.stdout.emit("data", Buffer.from('{"id":'));
  calls[0].child.stdin.emit("error", new Error("private diagnostic must not escape"));
  assert.deepEqual(await pending, { verified: false, code: "observer_unavailable" });
});
