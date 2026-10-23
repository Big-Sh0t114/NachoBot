"use strict";

const { execFile, spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MAX_CONFIG_BYTES = 64 * 1024;
const MAX_REPORT_BYTES = 64 * 1024;
const MAX_STDIO_BYTES = 64 * 1024;
const MAX_ROUTE_DIAGNOSTICS_BYTES = 64 * 1024;
const OBSERVER_TIMEOUT_MS = 2000;
const VERIFIED_CACHE_MS = 1000;
const MAX_LOOPBACK_READERS = 16;
const MAX_ROUTE_SESSIONS = 4096;
const MAX_ENDPOINT_MATCHES = 256;
const MAX_ROUTE_PROCESSES = 128;
const ROUTE_DIAGNOSTICS_FILE = "audio-route-negative-diagnostics.private.json";
const SCRIPT_PATH = path.resolve(__dirname, "../audio/windows_routes.py");
const MAX_FILETIME = 0xffffffffffffffffn;
const HELPER_ERRORS = new Set([
  "request_invalid", "platform_unsupported", "observer_busy", "observer_timeout",
  "com_initialization_failed", "observer_unavailable", "target_process_unavailable",
  "target_process_stale", "endpoint_not_found", "endpoint_ambiguous",
  "endpoint_metadata_unavailable", "session_enumeration_failed",
  "session_attribution_unavailable", "session_state_unavailable",
  "session_identity_unavailable", "shared_session_unverified", "routes_not_distinct",
  "host_input_not_active", "host_output_not_active", "endpoint_session_conflict",
]);

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function sameOpenedPath(opened, named) {
  if (!named.isFile() || named.isSymbolicLink()) return false;
  if (Number.isFinite(opened.dev) && Number.isFinite(opened.ino) && opened.ino !== 0 &&
      Number.isFinite(named.dev) && Number.isFinite(named.ino) && named.ino !== 0) {
    return opened.dev === named.dev && opened.ino === named.ino;
  }
  return opened.size === named.size && opened.mtimeMs === named.mtimeMs;
}

function sameOpenedState(before, after) {
  return after.isFile() && before.size === after.size && before.mtimeMs === after.mtimeMs &&
    (before.ctimeMs === undefined || after.ctimeMs === undefined || before.ctimeMs === after.ctimeMs);
}

function readBoundedRegularFile(filePath, maxBytes, invalidCode, permissionCode = null) {
  let descriptor;
  try {
    const constants = fs.constants;
    let flags = constants.O_RDONLY;
    if (Number.isInteger(constants.O_NOFOLLOW)) flags |= constants.O_NOFOLLOW;
    if (Number.isInteger(constants.O_NONBLOCK)) flags |= constants.O_NONBLOCK;
    try { descriptor = fs.openSync(filePath, flags); }
    catch (error) {
      try {
        const named = fs.lstatSync(filePath);
        if (!named.isFile() || named.isSymbolicLink()) throw codedError(invalidCode);
      } catch (inspectionError) {
        if (inspectionError?.code === invalidCode) throw inspectionError;
      }
      throw error;
    }

    const before = fs.fstatSync(descriptor);
    if (!before.isFile() || !Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes) {
      throw codedError(invalidCode);
    }
    if (permissionCode && process.platform !== "win32" && (before.mode & 0o077) !== 0) {
      throw codedError(permissionCode);
    }
    if (!sameOpenedPath(before, fs.lstatSync(filePath))) throw codedError(invalidCode);

    const chunks = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes + 1));
    let offset = 0;
    while (offset <= maxBytes) {
      const requested = Math.min(buffer.length, maxBytes + 1 - offset);
      if (requested <= 0) break;
      const count = fs.readSync(descriptor, buffer, 0, requested, offset);
      if (count === 0) break;
      offset += count;
      if (offset > maxBytes) throw codedError(invalidCode);
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    const after = fs.fstatSync(descriptor);
    if (!sameOpenedState(before, after) || !sameOpenedPath(after, fs.lstatSync(filePath))) {
      throw codedError(invalidCode);
    }
    return Buffer.concat(chunks, offset);
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* preserve the original read result or failure */ }
    }
  }
}

function ownKeysExactly(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function positiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function validPid(value) {
  return Number.isSafeInteger(value) && value > 0 && value <= 0xffffffff;
}

function validEndpointName(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 256 &&
    value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value);
}

function readPrivateJson(filePath, code) {
  let parsed;
  try {
    const resolved = path.resolve(filePath);
    const bytes = readBoundedRegularFile(resolved, MAX_CONFIG_BYTES, code, "audio_route_config_not_private");
    parsed = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    if (error?.code === code || error?.code === "audio_route_config_not_private") throw error;
    throw codedError(code);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw codedError(code);
  return parsed;
}

function validateAudioRouteConfig(value) {
  const baseFields = [
    "schema", "hostGeneration", "hostPid", "pythonExecutable", "processes",
    "captureRenderName", "playbackRenderName",
  ];
  const hasLoopbackReaders = Boolean(value && typeof value === "object" && !Array.isArray(value) &&
    Object.hasOwn(value, "loopbackReaders"));
  const hasDiscovery = Boolean(value && typeof value === "object" && Object.hasOwn(value, "discoverChildren"));
  const fields = [...baseFields, ...(hasLoopbackReaders ? ["loopbackReaders"] : []), ...(hasDiscovery ? ["discoverChildren"] : [])];
  if ((!ownKeysExactly(value, fields)) || (hasDiscovery && value.discoverChildren !== true) ||
      value.schema !== 1 || !positiveSafeInteger(value.hostGeneration) || !validPid(value.hostPid)) {
    throw codedError("audio_route_config_invalid");
  }
  if (typeof value.pythonExecutable !== "string" || !path.isAbsolute(value.pythonExecutable) ||
      path.extname(value.pythonExecutable).toLowerCase() !== ".exe") throw codedError("audio_route_python_invalid");
  let pythonExecutable;
  try {
    const stat = fs.statSync(value.pythonExecutable);
    if (!stat.isFile()) throw codedError("audio_route_python_invalid");
    pythonExecutable = fs.realpathSync(value.pythonExecutable);
  } catch (error) {
    if (error?.code === "audio_route_python_invalid") throw error;
    throw codedError("audio_route_python_invalid");
  }
  if (!validEndpointName(value.captureRenderName) || !validEndpointName(value.playbackRenderName) ||
      value.captureRenderName === value.playbackRenderName) throw codedError("audio_route_endpoint_invalid");
  if (!Array.isArray(value.processes) || value.processes.length < 1 || value.processes.length > 128) {
    throw codedError("audio_route_processes_invalid");
  }
  const pids = new Set();
  let hostCount = 0;
  const processes = value.processes.map((entry) => {
    if (!ownKeysExactly(entry, ["role", "pid", "creationTime100ns"]) ||
        !["host", "child"].includes(entry.role) || !validPid(entry.pid) || pids.has(entry.pid) ||
        typeof entry.creationTime100ns !== "string" || !/^(?:[1-9][0-9]{0,19})$/.test(entry.creationTime100ns)) {
      throw codedError("audio_route_processes_invalid");
    }
    let filetime;
    try { filetime = BigInt(entry.creationTime100ns); }
    catch { throw codedError("audio_route_processes_invalid"); }
    if (filetime <= 0n || filetime > MAX_FILETIME) throw codedError("audio_route_processes_invalid");
    pids.add(entry.pid);
    if (entry.role === "host") {
      hostCount += 1;
      if (entry.pid !== value.hostPid) throw codedError("audio_route_processes_invalid");
    }
    return Object.freeze({ role: entry.role, pid: entry.pid, creationTime100ns: entry.creationTime100ns });
  });
  if (hostCount !== 1) throw codedError("audio_route_processes_invalid");
  const loopbackReaders = hasLoopbackReaders ? value.loopbackReaders : undefined;
  if (hasLoopbackReaders && (!Array.isArray(loopbackReaders) ||
      loopbackReaders.length < 1 || loopbackReaders.length > MAX_LOOPBACK_READERS ||
      processes.length + loopbackReaders.length > 128)) {
    throw codedError("audio_route_processes_invalid");
  }
  const readers = [];
  if (hasLoopbackReaders) {
    for (const entry of loopbackReaders) {
      if (!ownKeysExactly(entry, ["pid", "creationTime100ns"]) || !validPid(entry.pid) || pids.has(entry.pid) ||
          typeof entry.creationTime100ns !== "string" || !/^(?:[1-9][0-9]{0,19})$/.test(entry.creationTime100ns)) {
        throw codedError("audio_route_processes_invalid");
      }
      let filetime;
      try { filetime = BigInt(entry.creationTime100ns); }
      catch { throw codedError("audio_route_processes_invalid"); }
      if (filetime <= 0n || filetime > MAX_FILETIME) throw codedError("audio_route_processes_invalid");
      pids.add(entry.pid);
      readers.push(Object.freeze({ pid: entry.pid, creationTime100ns: entry.creationTime100ns }));
    }
  }
  const config = {
    schema: 1,
    hostGeneration: value.hostGeneration,
    hostPid: value.hostPid,
    pythonExecutable,
    processes: Object.freeze(processes),
    captureRenderName: value.captureRenderName,
    playbackRenderName: value.playbackRenderName,
  };
  if (hasLoopbackReaders) config.loopbackReaders = Object.freeze(readers);
  if (hasDiscovery) config.discoverChildren = true;
  return Object.freeze(config);
}

function readAudioRouteConfig(filePath) {
  return validateAudioRouteConfig(readPrivateJson(filePath, "audio_route_config_unavailable"));
}

function validateHostTokenSidecar(value, { token, targetPid, hostGeneration, hostPid } = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      typeof value.ipcToken !== "string" || value.ipcToken !== token ||
      !validPid(value.targetPid) || value.targetPid !== targetPid || value.targetPid !== hostPid ||
      !positiveSafeInteger(value.hostGeneration) || value.hostGeneration !== hostGeneration) {
    throw codedError("audio_route_host_sidecar_mismatch");
  }
  return true;
}

function safeFailureCode(value) {
  return typeof value === "string" && (HELPER_ERRORS.has(value) || [
    "observer_report_invalid", "observer_report_missing", "observer_aborted",
    "audio_route_request_write_failed", "audio_route_runtime_unavailable",
    "audio_route_runtime_path_invalid", "audio_route_runtime_not_private",
  ].includes(value)) ? value : "host_route_unverified";
}

function safeCount(value, maximum) {
  return Number.isSafeInteger(value) && value >= 0 && value <= maximum;
}

function safeCountMap(value, keys, maximumTotal) {
  if (!ownKeysExactly(value, keys) || !keys.every((key) => safeCount(value[key], maximumTotal))) return null;
  const total = keys.reduce((sum, key) => sum + value[key], 0);
  return total <= maximumTotal ? { ...value } : null;
}

function diagnosticsRouteIsUnobserved(route) {
  return route.endpointMatches === 0 && route.endpointStatus === "not_observed" &&
    route.endpointIdPresent === false && route.endpointError === null && route.sessionCount === 0 &&
    Object.values(route.stateCounts).every((value) => value === 0) &&
    Object.values(route.attributionCounts).every((value) => value === 0) &&
    Object.values(route.targetCounts).every((value) => value === 0) &&
    route.identityQueried === 0 && route.identityAvailable === 0 && route.identityMissing === 0 &&
    route.identityNotQueried === 0 && route.qualifyingSessions === 0 && route.active === false;
}

function validateRouteDiagnostics(value, report, config, nowMs = Date.now()) {
  try {
    if (!report || typeof report !== "object" || !Number.isSafeInteger(nowMs) ||
        !ownKeysExactly(value, ["schema", "observedAtMs", "processSnapshot", "staleProcessCount", "routes"]) ||
        value.schema !== 1 || !Number.isSafeInteger(value.observedAtMs) ||
        value.observedAtMs !== report.observedAtMs ||
        !["stable", "stale", "invalid_target_set"].includes(value.processSnapshot) ||
        !safeCount(value.staleProcessCount, MAX_ROUTE_PROCESSES) || !Array.isArray(value.routes) ||
        value.routes.length !== 2) return null;
    const ageMs = nowMs - value.observedAtMs;
    if (ageMs < -250 || ageMs > 2500) return null;
    if ((value.processSnapshot === "stable" && value.staleProcessCount !== 0) ||
        (value.processSnapshot === "stale" && value.staleProcessCount === 0) ||
        (value.processSnapshot === "invalid_target_set" && value.staleProcessCount !== 0)) return null;

    const expectedKeys = ["host_input", "host_output"];
    const expectedStatuses = ["not_observed", "not_found", "ambiguous", "unique", "metadata_error"];
    const expectedErrors = ["host", "child", "pinnedLoopback", "foreign", "unclassified"];
    const normalizedRoutes = [];
    for (let index = 0; index < value.routes.length; index += 1) {
      const route = value.routes[index];
      if (!ownKeysExactly(route, [
        "key", "endpointMatches", "endpointStatus", "endpointIdPresent", "endpointError", "sessionCount",
        "stateCounts", "attributionCounts", "targetCounts", "identityQueried", "identityAvailable",
        "identityMissing", "identityNotQueried", "qualifyingSessions", "active",
      ]) || route.key !== expectedKeys[index] || !safeCount(route.endpointMatches, MAX_ENDPOINT_MATCHES) ||
          !expectedStatuses.includes(route.endpointStatus) || typeof route.endpointIdPresent !== "boolean" ||
          !(route.endpointError === null || HELPER_ERRORS.has(route.endpointError)) ||
          !safeCount(route.sessionCount, MAX_ROUTE_SESSIONS) || typeof route.active !== "boolean") return null;

      const states = safeCountMap(route.stateCounts, ["active", "inactive", "expired", "unknown"], MAX_ROUTE_SESSIONS);
      const attributions = safeCountMap(route.attributionCounts, ["single", "shared", "unavailable"], MAX_ROUTE_SESSIONS);
      const targets = safeCountMap(route.targetCounts, expectedErrors, MAX_ROUTE_SESSIONS);
      if (!states || !attributions || !targets ||
          Object.values(states).reduce((sum, count) => sum + count, 0) !== route.sessionCount ||
          Object.values(attributions).reduce((sum, count) => sum + count, 0) !== route.sessionCount ||
          Object.values(targets).reduce((sum, count) => sum + count, 0) !== route.sessionCount ||
          !safeCount(route.identityQueried, MAX_ROUTE_SESSIONS) ||
          !safeCount(route.identityAvailable, MAX_ROUTE_SESSIONS) ||
          !safeCount(route.identityMissing, MAX_ROUTE_SESSIONS) ||
          !safeCount(route.identityNotQueried, MAX_ROUTE_SESSIONS) ||
          !safeCount(route.qualifyingSessions, MAX_ROUTE_SESSIONS) ||
          route.identityAvailable + route.identityMissing !== route.identityQueried ||
          route.identityQueried + route.identityNotQueried !== route.sessionCount ||
          route.qualifyingSessions > states.active || route.qualifyingSessions > attributions.single ||
          route.qualifyingSessions > targets.host + targets.child ||
          route.qualifyingSessions > route.identityAvailable) return null;

      if (value.processSnapshot !== "stable") {
        if (!diagnosticsRouteIsUnobserved({ ...route, stateCounts: states, attributionCounts: attributions, targetCounts: targets })) return null;
      } else if (route.endpointStatus === "not_observed") {
        return null;
      }
      if ((route.endpointStatus === "not_found" &&
            (route.endpointMatches !== 0 || route.endpointIdPresent || route.endpointError !== null || route.sessionCount !== 0)) ||
          (route.endpointStatus === "ambiguous" &&
            (route.endpointMatches < 2 || route.endpointIdPresent || route.endpointError !== null || route.sessionCount !== 0)) ||
          (route.endpointStatus === "unique" &&
            (route.endpointMatches !== 1 || !route.endpointIdPresent || route.endpointError !== null)) ||
          (route.endpointStatus === "metadata_error" &&
            (route.endpointMatches !== 1 || (route.endpointIdPresent && route.endpointError === null)))) return null;
      const expectedActive = route.endpointStatus === "unique" && route.qualifyingSessions > 0;
      if (route.active !== expectedActive) return null;

      normalizedRoutes.push({
        key: route.key,
        endpointMatches: route.endpointMatches,
        endpointStatus: route.endpointStatus,
        endpointIdPresent: route.endpointIdPresent,
        endpointError: route.endpointError,
        sessionCount: route.sessionCount,
        stateCounts: states,
        attributionCounts: attributions,
        targetCounts: targets,
        identityQueried: route.identityQueried,
        identityAvailable: route.identityAvailable,
        identityMissing: route.identityMissing,
        identityNotQueried: route.identityNotQueried,
        qualifyingSessions: route.qualifyingSessions,
        active: route.active,
      });
    }
    const normalized = {
      schema: 1,
      observedAtMs: value.observedAtMs,
      processSnapshot: value.processSnapshot,
      staleProcessCount: value.staleProcessCount,
      routes: normalizedRoutes,
    };
    if (Buffer.byteLength(JSON.stringify(normalized), "utf8") > MAX_ROUTE_DIAGNOSTICS_BYTES) return null;
    return normalized;
  } catch {
    return null;
  }
}

function sameCanonicalPath(left, right) {
  const normalizedLeft = path.normalize(path.resolve(left));
  const normalizedRight = path.normalize(path.resolve(right));
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

function assertPrivateRuntimeDirectory(directory) {
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || !sameCanonicalPath(fs.realpathSync(directory), directory)) {
    throw codedError("audio_route_runtime_path_invalid");
  }
  if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) {
    throw codedError("audio_route_runtime_not_private");
  }
}

function assertSafeDiagnosticTarget(targetPath) {
  try {
    const stat = fs.lstatSync(targetPath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_ROUTE_DIAGNOSTICS_BYTES) {
      throw codedError("audio_route_runtime_path_invalid");
    }
    if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) {
      throw codedError("audio_route_runtime_not_private");
    }
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw error;
  }
}

async function writePrivateRouteDiagnostics(directory, value) {
  const targetPath = path.join(directory, ROUTE_DIAGNOSTICS_FILE);
  let temporaryPath;
  let handle;
  try {
    assertPrivateRuntimeDirectory(directory);
    assertSafeDiagnosticTarget(targetPath);
    const encoded = `${JSON.stringify(value)}\n`;
    if (Buffer.byteLength(encoded, "utf8") > MAX_ROUTE_DIAGNOSTICS_BYTES) return;
    temporaryPath = path.join(directory, `.route-diagnostics-${crypto.randomUUID()}.tmp`);
    handle = await fs.promises.open(temporaryPath, "wx", 0o600);
    await handle.writeFile(encoded, "utf8");
    await handle.sync();
    await handle.close();
    handle = null;
    assertPrivateRuntimeDirectory(directory);
    assertSafeDiagnosticTarget(targetPath);
    await fs.promises.rename(temporaryPath, targetPath);
    temporaryPath = null;
    assertPrivateRuntimeDirectory(directory);
    assertSafeDiagnosticTarget(targetPath);
  } catch {
    // Diagnostics persistence is best effort and never participates in routing.
  } finally {
    if (handle) {
      try { await handle.close(); } catch { /* best-effort descriptor cleanup */ }
    }
    if (temporaryPath) {
      try { await fs.promises.unlink(temporaryPath); } catch { /* best-effort private temp cleanup */ }
    }
  }
}

function createLatestDiagnosticsWriter(directory, writeImpl = writePrivateRouteDiagnostics) {
  let running = false;
  let hasPending = false;
  let pendingValue;

  function start() {
    if (running || !hasPending) return;
    running = true;
    setImmediate(() => {
      const drain = async () => {
        try {
          while (hasPending) {
            const current = pendingValue;
            pendingValue = undefined;
            hasPending = false;
            try { await writeImpl(directory, current); } catch { /* best-effort diagnostics only */ }
          }
        } finally {
          running = false;
          if (hasPending) start();
        }
      };
      void drain().catch(() => {
        running = false;
        if (hasPending) start();
      });
    });
  }

  return (value) => {
    pendingValue = value;
    hasPending = true;
    start();
  };
}

function routeRequest(config) {
  const request = {
    schema: 1,
    hostGeneration: config.hostGeneration,
    hostPid: config.hostPid,
    processes: config.processes.map((entry) => ({ ...entry })),
    expectations: [
      { key: "host_input", direction: "capture", endpointName: config.playbackRenderName },
      { key: "host_output", direction: "render", endpointName: config.captureRenderName },
    ],
  };
  if (config.loopbackReaders !== undefined) {
    request.loopbackReaders = config.loopbackReaders.map((entry) => ({ ...entry }));
  }
  if (config.discoverChildren === true) request.discoverChildren = true;
  return request;
}

function validateHelperReport(report, config, nowMs = Date.now()) {
  const fields = ["schema", "source", "observedAtMs", "verified", "hostGeneration", "hostPid", "routes", "errors"];
  const dynamic = config.discoverChildren === true && report && Object.hasOwn(report, "discoveredProcesses");
  const hasRouteDiagnostics = Boolean(report && Object.hasOwn(report, "routeDiagnostics"));
  if (!ownKeysExactly(report, [...fields, ...(dynamic ? ["discoveredProcesses"] : []), ...(hasRouteDiagnostics ? ["routeDiagnostics"] : [])]) ||
      report.schema !== 1 || report.source !== "windows_active_sessions" ||
      report.hostGeneration !== config.hostGeneration || report.hostPid !== config.hostPid ||
      !Number.isSafeInteger(report.observedAtMs) || typeof report.verified !== "boolean" ||
      !Array.isArray(report.routes) || !Array.isArray(report.errors)) {
    return { verified: false, code: "observer_report_invalid" };
  }
  if (!report.verified) {
    const known = report.errors.find((code) => HELPER_ERRORS.has(code));
    return { verified: false, code: safeFailureCode(known) };
  }
  const ageMs = nowMs - report.observedAtMs;
  if (ageMs < -250 || ageMs > 2500 || report.errors.length !== 0 || report.routes.length !== 2) {
    return { verified: false, code: "observer_report_invalid" };
  }
  const processes = [...config.processes];
  if (config.discoverChildren === true) {
    if (!dynamic || !Array.isArray(report.discoveredProcesses) ||
        report.discoveredProcesses.length + processes.length + (config.loopbackReaders?.length || 0) > 128) return { verified: false, code: "observer_report_invalid" };
    const pids = new Set([...processes, ...(config.loopbackReaders || [])].map((entry) => entry.pid));
    const root = processes.find((entry) => entry.role === "host");
    for (const entry of report.discoveredProcesses) {
      if (!ownKeysExactly(entry, ["role", "pid", "creationTime100ns"]) || entry.role !== "child" ||
          !validPid(entry.pid) || pids.has(entry.pid) || typeof entry.creationTime100ns !== "string" ||
          !/^[1-9][0-9]{0,19}$/.test(entry.creationTime100ns)) return { verified: false, code: "observer_report_invalid" };
      const time = BigInt(entry.creationTime100ns);
      if (time < BigInt(root.creationTime100ns) || time > MAX_FILETIME) return { verified: false, code: "observer_report_invalid" };
      pids.add(entry.pid); processes.push(entry);
    }
  }

  const expected = new Map([
    ["host_input", { direction: "capture", endpointName: config.playbackRenderName, flow: "capture" }],
    ["host_output", { direction: "render", endpointName: config.captureRenderName, flow: "render" }],
  ]);
  const extracted = new Map();
  for (const route of report.routes) {
    if (!ownKeysExactly(route, ["key", "direction", "endpointName", "endpointIdSha256", "active", "sessions"])) {
      return { verified: false, code: "observer_report_invalid" };
    }
    const spec = expected.get(route.key);
    if (!spec || extracted.has(route.key) || route.direction !== spec.direction ||
        route.endpointName !== spec.endpointName || route.active !== true ||
        typeof route.endpointIdSha256 !== "string" || !/^[a-f0-9]{64}$/i.test(route.endpointIdSha256) ||
        !Array.isArray(route.sessions) || route.sessions.length !== 1) {
      return { verified: false, code: "host_route_unverified" };
    }
    const session = route.sessions[0];
    if (!ownKeysExactly(session, [
      "role", "pid", "processGeneration", "processStartTimeUtc", "sessionGenerationSha256",
      "firstObservedUtc", "state", "processAttribution",
    ])) return { verified: false, code: "observer_report_invalid" };
    const process = processes.find((entry) => entry.pid === session.pid && entry.role === session.role);
    if (!process || session.processGeneration !== process.creationTime100ns || session.state !== "active" ||
        session.processAttribution !== "single" ||
        typeof session.sessionGenerationSha256 !== "string" || !/^[a-f0-9]{64}$/i.test(session.sessionGenerationSha256) ||
        !validIsoUtc(session.processStartTimeUtc) || !validIsoUtc(session.firstObservedUtc)) {
      return { verified: false, code: session.processAttribution === "shared" ? "shared_session_unverified" : "host_route_unverified" };
    }
    extracted.set(route.key, {
      deviceName: route.endpointName,
      flow: spec.flow,
      state: "active",
      endpointIdHash: route.endpointIdSha256.toLowerCase(),
      processId: session.pid,
    });
  }
  const hostInput = extracted.get("host_input");
  const hostOutput = extracted.get("host_output");
  if (!hostInput || !hostOutput || hostInput.endpointIdHash === hostOutput.endpointIdHash) {
    return { verified: false, code: "routes_not_distinct" };
  }
  return {
    verified: true,
    source: "windows_active_sessions",
    hostPid: config.hostPid,
    observedAtMs: report.observedAtMs,
    hostInput,
    hostOutput,
  };
}

function validIsoUtc(value) {
  return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value));
}

function cloneResult(value) {
  return value.verified === true ? {
    verified: true,
    source: value.source,
    hostPid: value.hostPid,
    observedAtMs: value.observedAtMs,
    hostInput: { ...value.hostInput },
    hostOutput: { ...value.hostOutput },
  } : { verified: false, code: value.code };
}

function runtimePathFor(configFilePath) {
  if (typeof configFilePath !== "string" || !path.isAbsolute(configFilePath)) throw codedError("audio_route_config_path_invalid");
  let parent;
  try { parent = fs.realpathSync(path.dirname(configFilePath)); }
  catch { throw codedError("audio_route_config_path_invalid"); }
  return path.join(parent, "QQCall.runtime");
}

function ensureRuntimeDirectory(directory) {
  try {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const stat = fs.lstatSync(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw codedError("audio_route_runtime_path_invalid");
    if (process.platform !== "win32" && (stat.mode & 0o077) !== 0) throw codedError("audio_route_runtime_not_private");
  } catch (error) {
    if (["audio_route_runtime_path_invalid", "audio_route_runtime_not_private"].includes(error?.code)) throw error;
    throw codedError("audio_route_runtime_unavailable");
  }
}

function writeOwnedRequest(filePath, value) {
  let descriptor;
  try {
    descriptor = fs.openSync(filePath, "wx", 0o600);
    fs.writeFileSync(descriptor, `${JSON.stringify(value)}\n`, "utf8");
    fs.fsyncSync(descriptor);
  } catch {
    throw codedError("audio_route_request_write_failed");
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* owned descriptor cleanup */ }
    }
  }
}

function readOwnedReport(filePath) {
  try {
    const bytes = readBoundedRegularFile(filePath, MAX_REPORT_BYTES, "observer_report_invalid");
    return JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    if (error?.code === "observer_report_invalid") throw error;
    throw codedError("observer_report_missing");
  }
}

function removeOwnedFile(filePath) {
  try { fs.unlinkSync(filePath); } catch { /* only unique observer-owned files are removed */ }
}

function sanitizedPythonEnvironment() {
  const environment = {};
  for (const key of ["SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"]) {
    if (typeof process.env[key] === "string") environment[key] = process.env[key];
  }
  environment.PYTHONNOUSERSITE = "1";
  environment.PYTHONDONTWRITEBYTECODE = "1";
  return environment;
}

function createAudioRouteObserver({
  config: inputConfig,
  configPath,
  execFileImpl = execFile,
  timeoutMs = OBSERVER_TIMEOUT_MS,
  now = Date.now,
  diagnosticWriteImpl,
} = {}) {
  if (typeof execFileImpl !== "function") throw codedError("audio_route_runner_invalid");
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > OBSERVER_TIMEOUT_MS) throw codedError("audio_route_timeout_invalid");
  const config = validateAudioRouteConfig(inputConfig);
  let runtimeDirectory;
  try { runtimeDirectory = runtimePathFor(configPath); }
  catch { throw codedError("audio_route_config_path_invalid"); }
  if (!fs.existsSync(SCRIPT_PATH) || !fs.statSync(SCRIPT_PATH).isFile()) throw codedError("audio_route_helper_missing");

  let activeFlight = null;
  let cachedProof = null;
  let closed = false;
  let stopPromise = null;
  const enqueueDiagnostics = createLatestDiagnosticsWriter(
    runtimeDirectory,
    typeof diagnosticWriteImpl === "function" ? diagnosticWriteImpl : writePrivateRouteDiagnostics,
  );

  function finishFlight(flight) {
    if (flight.finished) return;
    flight.finished = true;
    clearTimeout(flight.timer);
    clearTimeout(flight.forceKillTimer);
    flight.signal?.removeEventListener("abort", flight.onAbort);
    removeOwnedFile(flight.requestPath);
    removeOwnedFile(flight.reportPath);
    if (validPid(flight.child?.pid)) {
      removeOwnedFile(path.join(path.dirname(flight.reportPath), `.${path.basename(flight.reportPath)}.${flight.child.pid}.tmp`));
    }
    if (activeFlight === flight) activeFlight = null;
    flight.closeResolve();
  }

  function killFlight(flight) {
    if (!flight.child || flight.closeSeen) return;
    try { flight.child.kill(); } catch { /* termination is best effort; quarantine remains until close */ }
    flight.forceKillTimer = setTimeout(() => {
      if (flight.closeSeen) return;
      try { flight.child?.kill("SIGKILL"); } catch { /* do not replace the fixed failure code */ }
    }, 100);
    flight.forceKillTimer.unref?.();
  }

  function settleFlight(flight, value) {
    if (flight.resultSettled) return;
    flight.resultSettled = true;
    flight.resolveResult(cloneResult(value));
  }

  async function observe(request = {}) {
    if (closed) return { verified: false, code: "host_route_unverified" };
    if (!request || typeof request !== "object" || Array.isArray(request) ||
        request.captureRenderName !== config.captureRenderName ||
        request.playbackRenderName !== config.playbackRenderName) {
      return { verified: false, code: "endpoint_not_found" };
    }
    const cacheNow = now();
    const cacheAge = cachedProof ? cacheNow - cachedProof.cachedAt : Number.POSITIVE_INFINITY;
    const proofAge = cachedProof ? cacheNow - cachedProof.proof.observedAtMs : Number.POSITIVE_INFINITY;
    if (cachedProof && cacheAge >= -250 && cacheAge <= VERIFIED_CACHE_MS &&
        proofAge >= -250 && proofAge <= 250) return cloneResult(cachedProof.proof);
    if (activeFlight) return { verified: false, code: "observer_busy" };

    try { ensureRuntimeDirectory(runtimeDirectory); }
    catch (error) { return { verified: false, code: safeFailureCode(error?.code) }; }
    const id = crypto.randomUUID();
    const requestPath = path.join(runtimeDirectory, `${id}.request.json`);
    const reportPath = path.join(runtimeDirectory, `${id}.report.json`);
    let closeResolve;
    const actualClosePromise = new Promise((resolve) => { closeResolve = resolve; });
    const flight = {
      id,
      requestPath,
      reportPath,
      child: null,
      signal: request.signal && typeof request.signal.addEventListener === "function" ? request.signal : null,
      timer: null,
      forceKillTimer: null,
      callbackSeen: false,
      closeSeen: false,
      finished: false,
      resultSettled: false,
      resolveResult: null,
      closePromise: actualClosePromise,
      closeResolve,
      onAbort: null,
    };
    const resultPromise = new Promise((resolve) => { flight.resolveResult = resolve; });
    activeFlight = flight;
    flight.onAbort = () => {
      killFlight(flight);
      settleFlight(flight, { verified: false, code: "observer_aborted" });
    };
    if (flight.signal?.aborted) {
      activeFlight = null;
      return { verified: false, code: "observer_aborted" };
    }
    flight.signal?.addEventListener("abort", flight.onAbort, { once: true });
    flight.timer = setTimeout(() => {
      killFlight(flight);
      settleFlight(flight, { verified: false, code: "observer_timeout" });
    }, timeoutMs);
    flight.timer.unref?.();

    let requestWritten = false;
    try {
      writeOwnedRequest(requestPath, routeRequest(config));
      requestWritten = true;
      const args = ["-I", SCRIPT_PATH, "--request-file", requestPath, "--report", reportPath, "--include-route-diagnostics"];
      const child = execFileImpl(config.pythonExecutable, args, {
        cwd: runtimeDirectory,
        env: sanitizedPythonEnvironment(),
        encoding: "utf8",
        maxBuffer: MAX_STDIO_BYTES,
        timeout: timeoutMs,
        windowsHide: true,
        shell: false,
      }, (error, stdout, _stderr) => {
        flight.callbackSeen = true;
        if (flight.resultSettled) return;
        try {
          if (error || typeof stdout !== "string" || Buffer.byteLength(stdout, "utf8") > MAX_STDIO_BYTES) {
            const code = error?.code === "ETIMEDOUT" || error?.killed ? "observer_timeout" : "observer_unavailable";
            settleFlight(flight, { verified: false, code });
            return;
          }
          let summary;
          try { summary = JSON.parse(stdout.trim()); } catch { throw codedError("observer_report_invalid"); }
          if (!ownKeysExactly(summary, ["status", "verified"]) || typeof summary.verified !== "boolean" ||
              summary.status !== (summary.verified ? "verified" : "unverified")) throw codedError("observer_report_invalid");
          const report = readOwnedReport(reportPath);
          const extracted = validateHelperReport(report, config, now());
          if (summary.verified !== (extracted.verified === true) || report.verified !== summary.verified) {
            throw codedError("observer_report_invalid");
          }
          const routeDiagnostics = validateRouteDiagnostics(report.routeDiagnostics, report, config, now());
          if (extracted.verified) {
            cachedProof = { cachedAt: now(), proof: extracted };
            settleFlight(flight, extracted);
            return;
          }
          settleFlight(flight, extracted);
          if (routeDiagnostics && HELPER_ERRORS.has(extracted.code)) {
            try {
              enqueueDiagnostics({
                schema: 1,
                observedAtMs: report.observedAtMs,
                failureCode: extracted.code,
                routeDiagnostics,
              });
            } catch { /* private diagnostics never affect the route result */ }
          }
        } catch (error) {
          settleFlight(flight, { verified: false, code: safeFailureCode(error?.code) });
        }
      });
      flight.child = child;
      if (!child || typeof child.once !== "function" || typeof child.kill !== "function") {
        throw codedError("audio_route_runner_invalid");
      }
      child.once("close", () => {
        flight.closeSeen = true;
        if (!flight.callbackSeen && !flight.resultSettled) {
          settleFlight(flight, { verified: false, code: "observer_unavailable" });
        }
        finishFlight(flight);
      });
      if (flight.signal?.aborted) flight.onAbort();
    } catch (error) {
      if (flight.child) {
        killFlight(flight);
        settleFlight(flight, { verified: false, code: safeFailureCode(error?.code) });
      } else {
        if (requestWritten) removeOwnedFile(requestPath);
        finishFlight(flight);
        settleFlight(flight, { verified: false, code: safeFailureCode(error?.code) });
      }
    }

    const result = await resultPromise;
    // The helper's result may time out before it exits. Keep the single-flight
    // quarantine and its private files until execFile reports child close.
    if (flight.closeSeen && !flight.finished) finishFlight(flight);
    return result;
  }

  async function stop() {
    if (stopPromise) return stopPromise;
    closed = true;
    stopPromise = (async () => {
      const flight = activeFlight;
      if (!flight) return { stopped: true, quarantined: false };
      flight.onAbort();
      const close = await Promise.race([
        flight.closePromise.then(() => true),
        new Promise((resolve) => {
          const timer = setTimeout(() => resolve(false), OBSERVER_TIMEOUT_MS + 250);
          timer.unref?.();
        }),
      ]);
      return { stopped: true, quarantined: close !== true };
    })();
    return stopPromise;
  }

  const observer = async (request) => observe(request);
  observer.stop = stop;
  observer.status = () => ({ closed, inFlight: Boolean(activeFlight), quarantined: Boolean(activeFlight?.resultSettled) });
  return observer;
}

// Reuse an isolated helper, not an old proof. Each request still performs the
// complete PID/creation-time and active-session observation. Inherited pipes
// avoid process startup and synchronous request-file flush on every refresh.
function createPersistentAudioRouteObserver({
  config: inputConfig, configPath, spawnImpl = spawn,
  timeoutMs = OBSERVER_TIMEOUT_MS, now = Date.now, diagnosticWriteImpl,
} = {}) {
  const config = validateAudioRouteConfig(inputConfig);
  const directory = runtimePathFor(configPath);
  if (typeof spawnImpl !== "function") throw codedError("audio_route_runner_invalid");
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > OBSERVER_TIMEOUT_MS) {
    throw codedError("audio_route_timeout_invalid");
  }
  if (!fs.existsSync(SCRIPT_PATH) || !fs.statSync(SCRIPT_PATH).isFile()) throw codedError("audio_route_helper_missing");
  const diagnostics = createLatestDiagnosticsWriter(directory,
    typeof diagnosticWriteImpl === "function" ? diagnosticWriteImpl : writePrivateRouteDiagnostics);
  let worker = null;
  let flight = null;
  let closed = false;
  let stopping = null;

  function settle(value) {
    const current = flight;
    if (!current) return;
    flight = null;
    clearTimeout(current.timer);
    current.signal?.removeEventListener("abort", current.abort);
    current.resolve(cloneResult(value));
  }

  function retire(current, code) {
    if (!current || current.closed) return;
    current.retiring = true;
    if (worker === current) settle({ verified: false, code });
    if (current.killTimer) return;
    try { current.child.kill(); } catch { /* quarantine until actual close */ }
    current.killTimer = setTimeout(() => {
      if (!current.closed) {
        try { current.child.kill("SIGKILL"); } catch { /* retain quarantine */ }
      }
    }, 100);
    current.killTimer.unref?.();
  }

  function start() {
    ensureRuntimeDirectory(directory);
    const child = spawnImpl(config.pythonExecutable, ["-I", "-u", SCRIPT_PATH, "--stdio"], {
      cwd: directory, env: sanitizedPythonEnvironment(), windowsHide: true,
      shell: false, stdio: ["pipe", "pipe", "pipe"],
    });
    if (!child || typeof child.once !== "function" || typeof child.kill !== "function" ||
        !child.stdin || !child.stdout || !child.stderr) throw codedError("audio_route_runner_invalid");
    let resolveClose;
    const current = { child, closed: false, retiring: false, buffer: Buffer.alloc(0),
      killTimer: null, close: new Promise(resolve => { resolveClose = resolve; }) };
    worker = current;
    child.once("close", () => {
      current.closed = true;
      clearTimeout(current.killTimer);
      if (worker === current) {
        settle({ verified: false, code: "observer_unavailable" });
        worker = null;
      }
      resolveClose();
    });
    const broken = () => retire(current, "observer_unavailable");
    child.once("error", broken);
    child.stdin.on("error", broken);
    child.stdout.on("error", broken);
    child.stderr.on("error", broken);
    // Drain native diagnostic stderr without logging device paths or allocating
    // an unbounded transcript. Only the validated stdout envelope is accepted.
    child.stderr.on("data", () => {});
    child.stdout.on("data", chunk => {
      if (current.retiring || worker !== current) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (current.buffer.length + bytes.length > MAX_REPORT_BYTES) {
        retire(current, "observer_report_invalid"); return;
      }
      current.buffer = Buffer.concat([current.buffer, bytes]);
      const end = current.buffer.indexOf(10);
      if (end < 0) return;
      try {
        // Single-flight protocol: extra lines and unsolicited/late replies are
        // protocol faults, never a proof for a subsequent call.
        if (end !== current.buffer.length - 1 || !flight) throw codedError("observer_report_invalid");
        const envelope = JSON.parse(current.buffer.subarray(0, end).toString("utf8"));
        current.buffer = Buffer.alloc(0);
        if (!ownKeysExactly(envelope, ["id", "report"]) || envelope.id !== flight.id) {
          throw codedError("observer_report_invalid");
        }
        const report = envelope.report;
        const result = validateHelperReport(report, config, now());
        const routeDiagnostics = validateRouteDiagnostics(report.routeDiagnostics, report, config, now());
        settle(result);
        if (!result.verified && routeDiagnostics && HELPER_ERRORS.has(result.code)) {
          diagnostics({ schema: 1, observedAtMs: report.observedAtMs, failureCode: result.code, routeDiagnostics });
        }
        if (["observer_timeout", "observer_busy"].includes(result.code)) retire(current, result.code);
      } catch (error) { retire(current, safeFailureCode(error?.code)); }
    });
    return current;
  }

  const observer = async (request = {}) => {
    if (closed) return { verified: false, code: "host_route_unverified" };
    if (!request || request.captureRenderName !== config.captureRenderName ||
        request.playbackRenderName !== config.playbackRenderName) return { verified: false, code: "endpoint_not_found" };
    if (flight || worker?.retiring) return { verified: false, code: "observer_busy" };
    const signal = request.signal && typeof request.signal.addEventListener === "function" ? request.signal : null;
    if (signal?.aborted) return { verified: false, code: "observer_aborted" };
    let current;
    try { current = worker || start(); }
    catch (error) { return { verified: false, code: safeFailureCode(error?.code) }; }
    return new Promise(resolve => {
      const id = crypto.randomBytes(16).toString("hex");
      const abort = () => retire(current, "observer_aborted");
      const timer = setTimeout(() => retire(current, "observer_timeout"), timeoutMs);
      flight = { id, resolve, signal, abort, timer };
      signal?.addEventListener("abort", abort, { once: true });
      try {
        const wire = JSON.stringify({ id, request: routeRequest(config) }) + "\n";
        if (Buffer.byteLength(wire) > MAX_STDIO_BYTES) throw codedError("request_invalid");
        current.child.stdin.write(wire, error => { if (error) retire(current, "observer_unavailable"); });
      } catch (error) { retire(current, safeFailureCode(error?.code)); }
    });
  };
  observer.stop = () => {
    if (stopping) return stopping;
    closed = true;
    stopping = (async () => {
      const current = worker;
      if (!current) return { stopped: true, quarantined: false };
      retire(current, "observer_aborted");
      let timer;
      const finished = await Promise.race([current.close.then(() => true),
        new Promise(resolve => { timer = setTimeout(() => resolve(false), OBSERVER_TIMEOUT_MS + 250); })]);
      clearTimeout(timer);
      return { stopped: true, quarantined: finished !== true };
    })();
    return stopping;
  };
  observer.status = () => ({ closed, inFlight: Boolean(flight), quarantined: Boolean(worker?.retiring) });
  return observer;
}

module.exports = {
  HELPER_ERRORS,
  OBSERVER_TIMEOUT_MS,
  SCRIPT_PATH,
  VERIFIED_CACHE_MS,
  createAudioRouteObserver,
  createPersistentAudioRouteObserver,
  readAudioRouteConfig,
  routeRequest,
  validateAudioRouteConfig,
  validateHelperReport,
  validateHostTokenSidecar,
};
