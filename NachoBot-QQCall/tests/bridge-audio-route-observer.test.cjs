"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  OBSERVER_TIMEOUT_MS,
  SCRIPT_PATH,
  createAudioRouteObserver,
  routeRequest,
  validateAudioRouteConfig,
  validateHelperReport,
  validateHostTokenSidecar,
} = require("../bridge/audio-route-observer.cjs");

const HOST_PID = 4101;
const HOST_GENERATION = 89;
const CONFIG = Object.freeze({
  schema: 1,
  hostGeneration: HOST_GENERATION,
  hostPid: HOST_PID,
  pythonExecutable: "",
  processes: [
    { role: "host", pid: HOST_PID, creationTime100ns: "133240000000000001" },
    { role: "child", pid: 4102, creationTime100ns: "133240000000000002" },
  ],
  captureRenderName: "Voicemeeter In 5",
  playbackRenderName: "Line 1",
});

function setup(t, { execFileImpl, timeoutMs, now, diagnosticWriteImpl, configOverrides = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-route-observer-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pythonExecutable = path.join(root, "python.exe");
  fs.writeFileSync(pythonExecutable, "test-only executable placeholder", { mode: 0o600 });
  const configPath = path.join(root, "audio-route-config.json");
  fs.writeFileSync(configPath, "{}", { mode: 0o600 });
  const config = validateAudioRouteConfig({ ...CONFIG, pythonExecutable, ...configOverrides });
  const observer = createAudioRouteObserver({ config, configPath, execFileImpl, timeoutMs, now, diagnosticWriteImpl });
  return { root, config, configPath, observer };
}

function requestBody(config, overrides = {}) {
  return {
    accountId: "10001",
    clientInstance: "adapter-1",
    scope: {
      serviceInstance: "12345678-1234-4234-8234-123456789012",
      hostGeneration: config.hostGeneration,
      connectionGeneration: 55,
      callId: "7156ab16-c2c3-4f7f-987f-620145dce8df",
      callGeneration: 7,
    },
    captureRenderName: config.captureRenderName,
    playbackRenderName: config.playbackRenderName,
    ...overrides,
  };
}

function helperReport(config, overrides = {}) {
  const now = overrides.observedAtMs ?? Date.now();
  const session = (process, hash) => ({
    role: process.role,
    pid: process.pid,
    processGeneration: process.creationTime100ns,
    processStartTimeUtc: "2026-10-05T01:00:00Z",
    sessionGenerationSha256: hash,
    firstObservedUtc: new Date(now).toISOString(),
    state: "active",
    processAttribution: "single",
  });
  const base = {
    schema: 1,
    source: "windows_active_sessions",
    observedAtMs: now,
    verified: true,
    hostGeneration: config.hostGeneration,
    hostPid: config.hostPid,
    routes: [
      {
        key: "host_input",
        direction: "capture",
        endpointName: config.playbackRenderName,
        endpointIdSha256: "a".repeat(64),
        active: true,
        sessions: [session(config.processes[0], "c".repeat(64))],
      },
      {
        key: "host_output",
        direction: "render",
        endpointName: config.captureRenderName,
        endpointIdSha256: "b".repeat(64),
        active: true,
        sessions: [session(config.processes[1], "d".repeat(64))],
      },
    ],
    errors: [],
  };
  return { ...base, ...overrides };
}

function diagnosticRoute(key, overrides = {}) {
  const isInput = key === "host_input";
  return {
    key,
    endpointMatches: 1,
    endpointStatus: "unique",
    endpointIdPresent: true,
    endpointError: null,
    sessionCount: 1,
    stateCounts: { active: 1, inactive: 0, expired: 0, unknown: 0 },
    attributionCounts: { single: 1, shared: 0, unavailable: 0 },
    targetCounts: { host: isInput ? 1 : 0, child: isInput ? 0 : 1, pinnedLoopback: 0, foreign: 0, unclassified: 0 },
    identityQueried: 1,
    identityAvailable: 1,
    identityMissing: 0,
    identityNotQueried: 0,
    qualifyingSessions: 1,
    active: true,
    ...overrides,
  };
}

function helperRouteDiagnostics({ observedAtMs = Date.now(), hostInput = {}, hostOutput = {} } = {}) {
  return {
    schema: 1,
    observedAtMs,
    processSnapshot: "stable",
    staleProcessCount: 0,
    routes: [
      diagnosticRoute("host_input", hostInput),
      diagnosticRoute("host_output", hostOutput),
    ],
  };
}

function negativeHelperReport(config, observedAtMs = Date.now(), diagnosticOverrides = {}) {
  const report = helperReport(config, {
    verified: false,
    observedAtMs,
    errors: ["host_input_not_active"],
  });
  const inactiveInput = diagnosticRoute("host_input", {
    sessionCount: 1,
    stateCounts: { active: 0, inactive: 1, expired: 0, unknown: 0 },
    identityQueried: 0,
    identityAvailable: 0,
    identityNotQueried: 1,
    qualifyingSessions: 0,
    active: false,
  });
  report.routeDiagnostics = helperRouteDiagnostics({
    observedAtMs,
    hostInput: { ...inactiveInput, ...(diagnosticOverrides.hostInput || {}) },
    hostOutput: diagnosticOverrides.hostOutput || {},
    ...(diagnosticOverrides.topLevel || {}),
  });
  if (Object.hasOwn(diagnosticOverrides, "replace")) report.routeDiagnostics = diagnosticOverrides.replace;
  return report;
}

function fakeExecFile(reportFactory, calls = []) {
  return (executable, args, options, callback) => {
    calls.push({ executable, args, options });
    const requestPath = args[args.indexOf("--request-file") + 1];
    const reportPath = args[args.indexOf("--report") + 1];
    const child = new EventEmitter();
    child.killCalls = [];
    child.kill = (signal) => { child.killCalls.push(signal || "default"); return true; };
    queueMicrotask(() => {
      try {
        const input = JSON.parse(fs.readFileSync(requestPath, "utf8"));
        const report = reportFactory(input);
        fs.writeFileSync(reportPath, JSON.stringify(report), { mode: 0o600 });
        callback(null, JSON.stringify({ status: report.verified ? "verified" : "unverified", verified: report.verified }), "");
        child.emit("close", 0, null);
      } catch (error) {
        callback(error, "", "");
        child.emit("close", 1, null);
      }
    });
    return child;
  };
}

async function waitFor(predicate, timeoutMs = 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  return predicate();
}

test("opt-in child discovery binds new session identities to the pinned Host generation", (t) => {
  const { config } = setup(t, { configOverrides: { discoverChildren: true } });
  assert.equal(routeRequest(config).discoverChildren, true);
  const report = helperReport(config);
  const child = { role: "child", pid: 4110, creationTime100ns: "133240000000000010" };
  report.discoveredProcesses = [child];
  for (const route of report.routes) Object.assign(route.sessions[0], { role: "child", pid: child.pid, processGeneration: child.creationTime100ns });
  assert.equal(validateHelperReport(report, config).verified, true);
  assert.equal(validateHelperReport(report, { ...config, discoverChildren: undefined }).verified, false);
  for (const invalid of [{ ...child, role: "host" }, { ...child, pid: HOST_PID },
      { ...child, creationTime100ns: "133240000000000000" }, { ...child, creationTime100ns: "0" }]) {
    assert.equal(validateHelperReport({ ...report, discoveredProcesses: [invalid] }, config).verified, false);
  }
  assert.equal(validateHelperReport({ ...report, discoveredProcesses: [child, child] }, config).verified, false);
  assert.equal(validateHelperReport({ ...report, discoveredProcesses: [] }, config).verified, false);
  assert.throws(() => validateAudioRouteConfig({ ...config, discoverChildren: false }), { code: "audio_route_config_invalid" });
});

test("fixed helper request pins Host generation, explicit process identities and the two selected routes", (t) => {
  const { config } = setup(t);
  assert.deepEqual(routeRequest(config), {
    schema: 1,
    hostGeneration: HOST_GENERATION,
    hostPid: HOST_PID,
    processes: [
      { role: "host", pid: HOST_PID, creationTime100ns: "133240000000000001" },
      { role: "child", pid: 4102, creationTime100ns: "133240000000000002" },
    ],
    expectations: [
      { key: "host_input", direction: "capture", endpointName: "Line 1" },
      { key: "host_output", direction: "render", endpointName: "Voicemeeter In 5" },
    ],
  });
  assert.equal(SCRIPT_PATH.endsWith(path.join("audio", "windows_routes.py")), true);
});

test("config rejects arbitrary paths, duplicate identities, wrong Host identity, and non-canonical FILETIME", (t) => {
  const { config } = setup(t);
  assert.throws(() => validateAudioRouteConfig({ ...config, pythonExecutable: "python.exe" }), { code: "audio_route_python_invalid" });
  assert.throws(() => validateAudioRouteConfig({ ...config, hostPid: HOST_PID + 2 }), { code: "audio_route_processes_invalid" });
  assert.throws(() => validateAudioRouteConfig({
    ...config,
    processes: [...config.processes, { ...config.processes[1], role: "child" }],
  }), { code: "audio_route_processes_invalid" });
  assert.throws(() => validateAudioRouteConfig({
    ...config,
    processes: [{ ...config.processes[0], creationTime100ns: "0133240000000000001" }, config.processes[1]],
  }), { code: "audio_route_processes_invalid" });
});

test("optional loopback reader identities are exact, disjoint config pins forwarded only to the helper", (t) => {
  const { config } = setup(t);
  const loopbackReaders = [{ pid: 4103, creationTime100ns: "133240000000000003" }];
  const configured = validateAudioRouteConfig({ ...config, loopbackReaders });
  assert.deepEqual(configured.loopbackReaders, loopbackReaders);
  assert.deepEqual(routeRequest(configured).loopbackReaders, loopbackReaders);
  assert.deepEqual(routeRequest(config).loopbackReaders, undefined);

  for (const readers of [
    [],
    [{ pid: 4103, creationTime100ns: "133240000000000003" }, { pid: 4103, creationTime100ns: "133240000000000003" }],
    [{ pid: HOST_PID, creationTime100ns: config.processes[0].creationTime100ns }],
    [{ pid: config.processes[1].pid, creationTime100ns: config.processes[1].creationTime100ns }],
    [{ pid: true, creationTime100ns: "133240000000000003" }],
    [{ pid: 4103, creationTime100ns: "0133240000000000003" }],
    [{ pid: 4103, creationTime100ns: "133240000000000003", role: "child" }],
    Array.from({ length: 17 }, (_, index) => ({
      pid: 5000 + index,
      creationTime100ns: String(133240000000000100n + BigInt(index)),
    })),
  ]) {
    assert.throws(() => validateAudioRouteConfig({ ...config, loopbackReaders: readers }), {
      code: "audio_route_processes_invalid",
    });
  }
});

test("a loopback reader can never satisfy a Host route in a helper report", (t) => {
  const { config: base } = setup(t);
  const reader = { pid: 4103, creationTime100ns: "133240000000000003" };
  const config = validateAudioRouteConfig({ ...base, loopbackReaders: [reader] });
  const report = helperReport(config);
  const output = report.routes.find((route) => route.key === "host_output");
  output.sessions = [{
    role: "child",
    pid: reader.pid,
    processGeneration: reader.creationTime100ns,
    processStartTimeUtc: "2026-10-05T01:00:00Z",
    sessionGenerationSha256: "d".repeat(64),
    firstObservedUtc: new Date(report.observedAtMs).toISOString(),
    state: "active",
    processAttribution: "single",
  }];
  assert.deepEqual(validateHelperReport(report, config), { verified: false, code: "host_route_unverified" });
});

test("audio Host sidecar must bind the explicit CLI PID, Host generation and IPC token", () => {
  const sidecar = { ipcToken: "private-ipc-token", targetPid: HOST_PID, hostGeneration: HOST_GENERATION };
  assert.equal(validateHostTokenSidecar(sidecar, {
    token: "private-ipc-token", targetPid: HOST_PID, hostPid: HOST_PID, hostGeneration: HOST_GENERATION,
  }), true);
  for (const overrides of [
    { targetPid: HOST_PID + 1 },
    { hostGeneration: HOST_GENERATION + 1 },
    { ipcToken: "different-token" },
  ]) assert.throws(() => validateHostTokenSidecar({ ...sidecar, ...overrides }, {
    token: "private-ipc-token", targetPid: HOST_PID, hostPid: HOST_PID, hostGeneration: HOST_GENERATION,
  }), { code: "audio_route_host_sidecar_mismatch" });
});

test("helper report must prove two unique active sessions for exact identities and distinct endpoints", (t) => {
  const { config } = setup(t);
  const report = helperReport(config);
  const proof = validateHelperReport(report, config);
  assert.equal(proof.verified, true);
  assert.deepEqual(proof.hostInput, {
    deviceName: "Line 1", flow: "capture", state: "active", endpointIdHash: "a".repeat(64), processId: HOST_PID,
  });
  assert.deepEqual(proof.hostOutput, {
    deviceName: "Voicemeeter In 5", flow: "render", state: "active", endpointIdHash: "b".repeat(64), processId: 4102,
  });
  assert.equal(validateHelperReport(helperReport(config, { hostGeneration: HOST_GENERATION + 1 }), config).verified, false);
  assert.equal(validateHelperReport(helperReport(config, { errors: ["shared_session_unverified"] }), config).verified, false);
  assert.equal(validateHelperReport(helperReport(config, {
    routes: helperReport(config).routes.map((route) => route.key === "host_input" ? { ...route, sessions: [] } : route),
  }), config).verified, false);
  assert.equal(validateHelperReport(helperReport(config, {
    routes: helperReport(config).routes.map((route) => ({ ...route, endpointIdSha256: "a".repeat(64) })),
  }), config).verified, false);
  assert.equal(validateHelperReport(helperReport(config, {
    routes: helperReport(config).routes.map((route) => route.key === "host_output" ? {
      ...route, sessions: [{ ...route.sessions[0], processAttribution: "shared" }],
    } : route),
  }), config).code, "shared_session_unverified");
});

test("optional route diagnostics never change positive or negative public proof results", (t) => {
  const { config } = setup(t);
  const positive = helperReport(config);
  const baseline = validateHelperReport(positive, config);
  const validOptional = { ...positive, routeDiagnostics: helperRouteDiagnostics({ observedAtMs: positive.observedAtMs }) };
  const malformedOptional = { ...positive, routeDiagnostics: { schema: 1, unexpected: "private sentinel" } };
  assert.deepEqual(validateHelperReport(validOptional, config), baseline);
  assert.deepEqual(validateHelperReport(malformedOptional, config), baseline);

  const negative = negativeHelperReport(config);
  const negativeBaseline = validateHelperReport(negative, config);
  assert.deepEqual(validateHelperReport(negative, config), negativeBaseline);
  assert.deepEqual(validateHelperReport({ ...negative, routeDiagnostics: { malformed: true } }, config), negativeBaseline);
});

test("observer invokes only the fixed helper, validates report, and removes its owned request/report", async (t) => {
  let calls = [];
  const { config, configPath, observer, root } = setup(t, {
    execFileImpl: fakeExecFile(() => helperReport(config), calls),
  });
  const proof = await observer(requestBody(config));
  assert.equal(proof.verified, true);
  assert.equal(proof.source, "windows_active_sessions");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].executable, config.pythonExecutable);
  assert.deepEqual(calls[0].args.slice(0, 2), ["-I", SCRIPT_PATH]);
  assert.equal(calls[0].options.shell, false);
  assert.equal(calls[0].options.timeout <= OBSERVER_TIMEOUT_MS, true);
  assert.equal(calls[0].options.maxBuffer <= 64 * 1024, true);
  assert.equal(calls[0].options.windowsHide, true);
  assert.equal(calls[0].args.includes("--include-route-diagnostics"), true);
  const runtimeDirectory = path.join(path.dirname(configPath), "QQCall.runtime");
  assert.equal(calls[0].options.cwd, runtimeDirectory);
  assert.deepEqual(fs.readdirSync(runtimeDirectory), []);
  assert.equal(fs.existsSync(root), true);
  await observer.stop();
});

test("only a valid fresh negative diagnostic is written asynchronously to its generation directory", async (t) => {
  let config;
  const { observer, configPath, root } = setup(t, {
    execFileImpl: fakeExecFile(() => negativeHelperReport(config)),
  });
  config = validateAudioRouteConfig({ ...CONFIG, pythonExecutable: path.join(root, "python.exe") });
  const result = await observer(requestBody(config));
  assert.deepEqual(result, { verified: false, code: "host_input_not_active" });
  const runtimeDirectory = path.join(path.dirname(configPath), "QQCall.runtime");
  const diagnosticsPath = path.join(runtimeDirectory, "audio-route-negative-diagnostics.private.json");
  assert.equal(await waitFor(() => fs.existsSync(diagnosticsPath)), true);
  const saved = JSON.parse(fs.readFileSync(diagnosticsPath, "utf8"));
  assert.equal(saved.schema, 1);
  assert.equal(saved.failureCode, "host_input_not_active");
  assert.equal(saved.routeDiagnostics.routes[0].stateCounts.inactive, 1);
  assert.equal(saved.routeDiagnostics.routes[0].identityNotQueried, 1);
  const encoded = JSON.stringify(saved);
  for (const privateValue of ["Line 1", "Voicemeeter", "4101", "endpointIdSha256", "sessionGenerationSha256", "private sentinel"]) {
    assert.equal(encoded.includes(privateValue), false, `persisted diagnostic leaked ${privateValue}`);
  }
  await observer.stop();
});

test("malformed and stale diagnostics are ignored without changing fixed negative code", async (t) => {
  const now = 50_000;
  const cases = [
    ["wrong relationships", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { sessionCount: 2 } })],
    ["extra key", () => ({ ...helperRouteDiagnostics({ observedAtMs: now }), secret: "private sentinel" })],
    ["future counter", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { sessionCount: Number.MAX_SAFE_INTEGER + 1 } })],
    ["boolean counter", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { sessionCount: true } })],
    ["fractional counter", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { sessionCount: 1.5 } })],
    ["negative counter", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { sessionCount: -1 } })],
    ["untrusted endpoint error", () => helperRouteDiagnostics({ observedAtMs: now, hostInput: { endpointError: "private sentinel" } })],
    ["stale snapshot", () => helperRouteDiagnostics({ observedAtMs: now - 2501 })],
  ];
  for (const [name, diagnosticFactory] of cases) {
    let config;
    const { observer, configPath } = setup(t, {
      now: () => now,
      execFileImpl: fakeExecFile(() => {
        const report = negativeHelperReport(config, now);
        report.routeDiagnostics = diagnosticFactory(config);
        return report;
      }),
    });
    config = validateAudioRouteConfig({ ...CONFIG, pythonExecutable: path.join(path.dirname(configPath), "python.exe") });
    const result = await observer(requestBody(config));
    assert.deepEqual(result, { verified: false, code: "host_input_not_active" }, name);
    await new Promise((resolve) => setTimeout(resolve, 10));
    const diagnosticsPath = path.join(path.dirname(configPath), "QQCall.runtime", "audio-route-negative-diagnostics.private.json");
    assert.equal(fs.existsSync(diagnosticsPath), false, name);
    await observer.stop();
  }
});

test("diagnostic persistence failure and a stalled writer never delay or change a route result", async (t) => {
  let config;
  const failedWrite = setup(t, {
    diagnosticWriteImpl: async () => { throw new Error("private writer sentinel"); },
    execFileImpl: fakeExecFile(() => negativeHelperReport(config)),
  });
  config = failedWrite.config;
  const failedResult = await failedWrite.observer(requestBody(config));
  assert.deepEqual(failedResult, { verified: false, code: "host_input_not_active" });
  await failedWrite.observer.stop();

  let now = 70_000;
  let releaseFirst;
  let markStarted;
  const started = new Promise((resolve) => { markStarted = resolve; });
  const blocked = new Promise((resolve) => { releaseFirst = resolve; });
  let activeWriters = 0;
  let maxActiveWriters = 0;
  const writes = [];
  const diagnosticWriteImpl = async (_directory, value) => {
    activeWriters += 1;
    maxActiveWriters = Math.max(maxActiveWriters, activeWriters);
    writes.push(value);
    if (writes.length === 1) markStarted();
    if (writes.length === 1) await blocked;
    activeWriters -= 1;
  };
  let latestConfig;
  const coalesced = setup(t, {
    now: () => now,
    diagnosticWriteImpl,
    execFileImpl: fakeExecFile(() => negativeHelperReport(latestConfig, now)),
  });
  latestConfig = coalesced.config;
  assert.deepEqual(await coalesced.observer(requestBody(latestConfig)), { verified: false, code: "host_input_not_active" });
  await started;
  now += 10;
  assert.deepEqual(await coalesced.observer(requestBody(latestConfig)), { verified: false, code: "host_input_not_active" });
  now += 10;
  assert.deepEqual(await coalesced.observer(requestBody(latestConfig)), { verified: false, code: "host_input_not_active" });
  assert.equal(writes.length, 1, "pending diagnostics are coalesced while one write is active");
  releaseFirst();
  assert.equal(await waitFor(() => writes.length === 2), true);
  assert.equal(writes[1].observedAtMs, now, "the pending slot keeps only the latest negative snapshot");
  assert.equal(maxActiveWriters, 1);
  assert.equal(await waitFor(() => activeWriters === 0), true);
  await coalesced.observer.stop();
});

test("diagnostic output rejects an existing symlink and never follows its target", async (t) => {
  let config;
  let symlinkError = null;
  let linkPath;
  let targetPath;
  const { observer, configPath } = setup(t, {
    execFileImpl: (executable, args, options, callback) => {
      linkPath = path.join(options.cwd, "audio-route-negative-diagnostics.private.json");
      targetPath = path.join(path.dirname(configPath), "outside-diagnostic-target.txt");
      fs.writeFileSync(targetPath, "unchanged", "utf8");
      try { fs.symlinkSync(targetPath, linkPath, "file"); }
      catch (error) { symlinkError = error; }
      return fakeExecFile(() => negativeHelperReport(config))(executable, args, options, callback);
    },
  });
  config = validateAudioRouteConfig({ ...CONFIG, pythonExecutable: path.join(path.dirname(configPath), "python.exe") });
  const result = await observer(requestBody(config));
  assert.deepEqual(result, { verified: false, code: "host_input_not_active" });
  if (symlinkError) {
    t.skip(`symlink creation is unavailable on this host (${symlinkError.code || "error"})`);
    return;
  }
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(fs.lstatSync(linkPath).isSymbolicLink(), true);
  assert.equal(fs.readFileSync(targetPath, "utf8"), "unchanged");
  await observer.stop();
});

test("observer forwards pinned loopback reader identities through its private helper request", async (t) => {
  const loopbackReaders = [{ pid: 4103, creationTime100ns: "133240000000000003" }];
  let configured;
  let forwarded;
  const calls = [];
  const { config, observer } = setup(t, {
    configOverrides: { loopbackReaders },
    execFileImpl: fakeExecFile((request) => {
      forwarded = request.loopbackReaders;
      return helperReport(configured);
    }, calls),
  });
  configured = config;
  const proof = await observer(requestBody(config));
  assert.equal(proof.verified, true);
  assert.deepEqual(forwarded, loopbackReaders);
  assert.equal(calls.length, 1);
  await observer.stop();
});

test("negative, stale, shared and endpoint-mismatched helper reports cannot make the observer ready", async (t) => {
  const cases = [
    { name: "negative helper", factory: (config) => helperReport(config, { verified: false, errors: ["host_input_not_active"] }), expected: "host_input_not_active" },
    { name: "wrong process generation", factory: (config) => {
      const report = helperReport(config);
      report.routes[0].sessions[0].processGeneration = "133240000000000009";
      return report;
    }, expected: "observer_report_invalid" },
    { name: "wrong endpoint", factory: (config) => {
      const report = helperReport(config);
      report.routes[0].endpointName = "Default Microphone";
      return report;
    }, expected: "observer_report_invalid" },
  ];
  for (const item of cases) {
    const { config, observer } = setup(t, { execFileImpl: fakeExecFile(item.factory) });
    const result = await observer(requestBody(config));
    assert.equal(result.verified, false, item.name);
    assert.equal(result.code, item.expected, item.name);
    await observer.stop();
  }
});

test("verified result cache requires both recent completion and fresh native observation", async (t) => {
  let now = 20_000;
  let calls = [];
  const { config, observer } = setup(t, {
    now: () => now,
    execFileImpl: fakeExecFile(() => helperReport(config, { observedAtMs: now }), calls),
  });
  const first = await observer(requestBody(config));
  const second = await observer(requestBody(config));
  assert.equal(first.verified, true);
  assert.equal(second.verified, true);
  assert.equal(calls.length, 1);
  now += 251;
  const third = await observer(requestBody(config));
  assert.equal(third.verified, true);
  assert.equal(third.observedAtMs, now, "a young completion cache cannot restamp an aged native proof");
  assert.equal(calls.length, 2);
  assert.equal((await observer(requestBody(config))).verified, true);
  assert.equal(calls.length, 2, "the newly observed proof may be reused while it remains fresh");
  now += 1001;
  const fourth = await observer(requestBody(config));
  assert.equal(fourth.verified, true);
  assert.equal(calls.length, 3);
  const wrongName = await observer(requestBody(config, { playbackRenderName: "Default" }));
  assert.deepEqual(wrongName, { verified: false, code: "endpoint_not_found" });
  await observer.stop();
});

test("timed out child is killed and quarantined until close without stacking observations", async (t) => {
  let child;
  const { config, observer } = setup(t, {
    timeoutMs: 25,
    execFileImpl: (_executable, _args, _options, _callback) => {
      child = new EventEmitter();
      child.killCalls = [];
      child.kill = (signal) => { child.killCalls.push(signal || "default"); return true; };
      return child;
    },
  });
  const first = await observer(requestBody(config));
  assert.deepEqual(first, { verified: false, code: "observer_timeout" });
  assert.ok(child.killCalls.length >= 1);
  assert.deepEqual(await observer(requestBody(config)), { verified: false, code: "observer_busy" });
  child.emit("close", null, "SIGTERM");
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(await observer(requestBody(config)), { verified: false, code: "observer_timeout" });
  child.emit("close", null, "SIGTERM");
  await new Promise((resolve) => setImmediate(resolve));
  await observer.stop();
});
