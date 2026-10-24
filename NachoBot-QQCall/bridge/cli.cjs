"use strict";

const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { spawn } = require("node:child_process");
const { createHash, randomBytes, randomUUID } = require("node:crypto");
const { createBroker, summarizeBootstrapStatus, summarizeEvent } = require("../probe/broker.cjs");
const { isRendererCapabilityReport } = require("../agent/renderer-capability-probe.cjs");
const { isAsarSourceProbeReport } = require("../agent/asar-source-probe.cjs");
const { isAsarCandidateReport, PROOF_LEVEL: ASAR_CANDIDATE_PROOF_LEVEL } = require("../agent/asar-candidate-probe.cjs");
const { isOfficialWindowReport, PROOF_LEVEL: OFFICIAL_WINDOW_PROOF_LEVEL } = require("../agent/official-window-probe.cjs");
const { isRendererScriptMetadataReport, PROOF_LEVEL: RENDERER_SCRIPT_METADATA_PROOF_LEVEL } = require("../agent/renderer-script-metadata-probe.cjs");
const { isObserverReport, PROOF_LEVEL: OFFICIAL_AV_PROOF_LEVEL } = require("../agent/official-av-observer.cjs");
const { APP_PATH: ASAR_PHYSICAL_APP_PATH, startAsarPhysicalKeeper } = require("./asar-keeper-client.cjs");
const { createHostClient } = require("./host-client.cjs");
const { createQQCallBridge } = require("./qqcall-bridge.cjs");
const { createControlService } = require("./control-service.cjs");
const { writePrivateJson } = require("../host/config.cjs");
const { canonicalFiletime } = require("../shared/private-invite-scope.cjs");
const {
  createPersistentAudioRouteObserver,
  readAudioRouteConfig,
  validateHostTokenSidecar,
} = require("./audio-route-observer.cjs");

const MODULE_PATH = path.resolve(__dirname, "../agent/bootstrap.cjs");
const RENDERER_ADMISSION_PATH = path.resolve(__dirname, "../service/renderer_probe_admission.py");
const CANONICAL_UIN = /^[1-9][0-9]{0,19}$/;
const QQ_UID = /^u_[A-Za-z0-9_-]{22}$/;
const RENDERER_ADMISSION_CODES = new Set([
  "input_invalid", "config_unavailable", "config_invalid", "config_changed", "admission_disabled",
  "adapter_mismatch", "manifest_unavailable", "manifest_invalid", "manifest_stale", "account_mismatch",
  "binding_unavailable", "binding_changed", "target_mismatch", "internal_error",
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

function openReadOnlyNoFollow(filePath) {
  const constants = fs.constants;
  let flags = constants.O_RDONLY;
  if (Number.isInteger(constants.O_NOFOLLOW)) flags |= constants.O_NOFOLLOW;
  if (Number.isInteger(constants.O_NONBLOCK)) flags |= constants.O_NONBLOCK;
  return fs.openSync(filePath, flags);
}

function readBoundedRegularFile(filePath, maxBytes, invalidCode) {
  let descriptor;
  try {
    try { descriptor = openReadOnlyNoFollow(filePath); }
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
    const named = fs.lstatSync(filePath);
    if (!sameOpenedPath(before, named)) throw codedError(invalidCode);

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
      try { fs.closeSync(descriptor); } catch { /* keep the original read result or failure */ }
    }
  }
}

let controllerExitReportTarget = null;
let pendingControllerExitEvidence = null;
let controllerExitObserverInstalled = false;

function validatedControllerExitReportTarget(reportPath) {
  try {
    if (typeof reportPath !== "string" || !path.isAbsolute(reportPath) || reportPath.length > 32_000 || reportPath.includes("\0")) {
      return null;
    }
    const base = path.resolve(reportPath);
    const parent = path.dirname(base);
    const parentInfo = fs.lstatSync(parent);
    if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) return null;
    return `${base}.controller-exit.private.json`;
  } catch {
    return null;
  }
}

function writeControllerExitEvidenceIfAbsent(target, evidence) {
  let temporary = null;
  try {
    const parent = path.dirname(target);
    const parentInfo = fs.lstatSync(parent);
    if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) return;
    if (fs.existsSync(target)) return;
    temporary = path.join(parent, `.qqcall-controller-exit-${process.pid}-${randomUUID()}.tmp`);
    fs.writeFileSync(temporary, `${JSON.stringify(evidence)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    try {
      fs.linkSync(temporary, target);
    } catch { return; }
    try { fs.chmodSync(target, 0o600); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
  } catch {
    // Exit diagnostics are best-effort and must not replace Node's default exit behavior.
  } finally {
    if (temporary) {
      try { fs.rmSync(temporary, { force: true }); } catch { /* best effort */ }
    }
  }
}

function installControllerExitObserver(reportPath) {
  controllerExitReportTarget = validatedControllerExitReportTarget(reportPath);
  pendingControllerExitEvidence = null;
  if (!controllerExitObserverInstalled) {
    try {
      process.on("uncaughtExceptionMonitor", (_error, origin) => {
        try {
          if (!controllerExitReportTarget || pendingControllerExitEvidence) return;
          const observedAtMs = Date.now();
          if (!Number.isSafeInteger(observedAtMs) || observedAtMs < 0) return;
          pendingControllerExitEvidence = {
            schema: 1,
            errorClass: "uncaught_exception",
            failureCode: "controller_uncaught_exception",
            origin: origin === "uncaughtException" || origin === "unhandledRejection" ? origin : "uncaughtException",
            observedAtMs,
          };
        } catch { /* The monitor must never interfere with Node's default exception handling. */ }
      });
      process.on("exit", (exitCode) => {
        try {
          const pending = pendingControllerExitEvidence;
          const target = controllerExitReportTarget;
          if (!pending || !target) return;
          const exitedAtMs = Date.now();
          if (!Number.isSafeInteger(exitedAtMs) || exitedAtMs < 0) return;
          writeControllerExitEvidenceIfAbsent(target, {
            ...pending,
            exitedAtMs,
            exitCode: Number.isInteger(exitCode) && exitCode >= 0 && exitCode <= 255 ? exitCode : null,
          });
        } catch { /* Exit observers are best-effort only. */ }
      });
      controllerExitObserverInstalled = true;
    } catch {
      controllerExitReportTarget = null;
    }
  }
  const target = controllerExitReportTarget;
  return () => {
    if (controllerExitReportTarget === target && pendingControllerExitEvidence === null) {
      controllerExitReportTarget = null;
    }
  };
}

function parseArgs(argv) {
  const values = new Map();
  const flags = new Set();
  const flagOptions = new Set(["service-mode", "auto-bootstrap", "probe-renderer-capabilities", "probe-asar-sources", "probe-asar-candidates", "probe-official-windows", "probe-renderer-script-metadata", "observe-official-av", "incoming-only", "native-conditional"]);
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (name === "--help") return { help: true };
    if (!name.startsWith("--")) throw codedError("invalid_cli_arguments");
    const key = name.slice(2);
    if (flagOptions.has(key)) {
      if (values.has(key) || flags.has(key)) throw codedError("duplicate_cli_option");
      flags.add(key);
      continue;
    }
    if (index + 1 >= argv.length || argv[index + 1].startsWith("--")) throw codedError("invalid_cli_arguments");
    if (values.has(key) || flags.has(key)) throw codedError("duplicate_cli_option");
    values.set(key, argv[++index]);
  }
  const allowed = new Set([
    "pid", "app-path", "wrapper", "session", "token-file", "config", "report",
    "host-port", "output-port", "host-pid", "host-generation", "connection-generation",
    "control-port", "control-token-file", "account-uin", "audio-route-config", "kernel-peer-identities-path", "lifetime-ms", "login-file",
    "identity-source-file", "identity-output-file", "kernel-creation-time-100ns",
    "probe-python",
  ]);
  const rendererProbe = flags.has("probe-renderer-capabilities");
  if (rendererProbe) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("renderer_probe_option_forbidden");
    for (const key of flags) if (key !== "probe-renderer-capabilities") throw codedError("renderer_probe_option_forbidden");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("renderer_probe_config_path_must_be_absolute");
    return {
      help: false,
      probeRendererCapabilities: true,
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("probe-asar-sources")) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("asar_source_probe_option_forbidden");
    for (const key of flags) if (key !== "probe-asar-sources") throw codedError("asar_source_probe_option_forbidden");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("asar_source_probe_config_path_must_be_absolute");
    return {
      help: false,
      probeAsarSources: true,
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("probe-asar-candidates")) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("asar_candidate_probe_option_forbidden");
    for (const key of flags) if (key !== "probe-asar-candidates") throw codedError("asar_candidate_probe_option_forbidden");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("asar_candidate_probe_config_path_must_be_absolute");
    return {
      help: false,
      probeAsarCandidates: true,
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("probe-official-windows")) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("official_window_probe_option_forbidden");
    for (const key of flags) if (key !== "probe-official-windows") throw codedError("official_window_probe_option_forbidden");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("official_window_probe_config_path_must_be_absolute");
    return {
      help: false,
      probeOfficialWindows: true,
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("probe-renderer-script-metadata")) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("renderer_script_metadata_probe_option_forbidden");
    for (const key of flags) if (key !== "probe-renderer-script-metadata") throw codedError("renderer_script_metadata_probe_option_forbidden");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("renderer_script_metadata_probe_config_path_must_be_absolute");
    return {
      help: false,
      probeRendererScriptMetadata: true,
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("observe-official-av")) {
    const probeValues = new Set(["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]);
    for (const key of values.keys()) if (!probeValues.has(key)) throw codedError("official_av_observer_option_forbidden");
    for (const key of flags) if (key !== "observe-official-av" && key !== "incoming-only" && key !== "native-conditional") throw codedError("official_av_observer_option_forbidden");
    if (flags.has("incoming-only") && flags.has("native-conditional")) throw codedError("official_av_observer_modes_mutually_exclusive");
    for (const key of ["pid", "app-path", "token-file", "config", "connection-generation", "kernel-creation-time-100ns", "probe-python"]) {
      if (!values.has(key)) throw codedError("missing_cli_option");
    }
    const pidText = values.get("pid");
    const generationText = values.get("connection-generation");
    if (!/^[1-9][0-9]*$/.test(pidText) || !/^[1-9][0-9]*$/.test(generationText)) throw codedError("invalid_cli_number");
    const targetPid = Number(pidText);
    const connectionGeneration = Number(generationText);
    if (!Number.isSafeInteger(targetPid) || targetPid > 0x7fffffff || !Number.isSafeInteger(connectionGeneration)) {
      throw codedError("invalid_cli_number");
    }
    const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
    if (!canonicalFiletime(kernelCreationTime100ns)) throw codedError("invalid_kernel_creation_time");
    const probePythonPath = values.get("probe-python");
    if (!path.isAbsolute(probePythonPath)) throw codedError("probe_python_path_must_be_absolute");
    const configPath = values.get("config");
    if (!path.isAbsolute(configPath)) throw codedError("official_av_observer_config_path_must_be_absolute");
    return {
      help: false,
      observeOfficialAV: true,
      officialAVIncomingOnly: flags.has("incoming-only"),
      officialAVNativeConditional: flags.has("native-conditional"),
      targetPid,
      appPath: path.resolve(values.get("app-path")),
      tokenFile: path.resolve(values.get("token-file")),
      configPath: path.resolve(configPath),
      connectionGeneration,
      kernelCreationTime100ns,
      probePythonPath: path.resolve(probePythonPath),
    };
  }
  if (flags.has("incoming-only")) throw codedError("incoming_only_requires_official_av_observer");
  if (flags.has("native-conditional")) throw codedError("native_conditional_requires_official_av_observer");
  if (values.has("probe-python")) throw codedError("probe_python_requires_probe_mode");
  for (const key of values.keys()) if (!allowed.has(key)) throw codedError("unknown_cli_option");
  for (const key of ["pid", "app-path", "token-file", "host-port", "output-port", "host-generation", "connection-generation"]) {
    if (!values.has(key)) throw codedError("missing_cli_option");
  }
  const numeric = (key) => {
    const value = Number(values.get(key));
    if (!Number.isSafeInteger(value) || value <= 0) throw codedError("invalid_cli_number");
    return value;
  };
  const pid = numeric("pid");
  const hostPort = numeric("host-port");
  const outputPort = numeric("output-port");
  const hostPid = values.has("host-pid") ? numeric("host-pid") : undefined;
  const hostGeneration = numeric("host-generation");
  const connectionGeneration = numeric("connection-generation");
  if (pid > 0x7fffffff || (hostPid !== undefined && hostPid > 0x7fffffff) || hostPort > 65535 || outputPort > 65535) throw codedError("invalid_cli_number");
  const kernelCreationTime100ns = values.get("kernel-creation-time-100ns");
  if (kernelCreationTime100ns !== undefined && !canonicalFiletime(kernelCreationTime100ns)) {
    throw codedError("invalid_kernel_creation_time");
  }
  let maxLifetimeMs;
  if (values.has("lifetime-ms")) {
    const lifetimeText = values.get("lifetime-ms");
    if (!/^(0|[1-9][0-9]*)$/.test(lifetimeText)) throw codedError("invalid_lifetime");
    maxLifetimeMs = Number(lifetimeText);
    if (!Number.isSafeInteger(maxLifetimeMs) || maxLifetimeMs < 1000 || maxLifetimeMs > 60 * 60 * 1000) {
      throw codedError("invalid_lifetime");
    }
  }
  const serviceMode = flags.has("service-mode");
  const autoBootstrap = flags.has("auto-bootstrap");
  const hasIdentitySource = values.has("identity-source-file");
  const hasIdentityOutput = values.has("identity-output-file");
  const hasIdentityPair = hasIdentitySource && hasIdentityOutput;
  if (autoBootstrap && !serviceMode) throw codedError("auto_bootstrap_requires_service_mode");
  if (serviceMode && values.has("lifetime-ms")) throw codedError("service_mode_lifetime_conflict");
  if (values.has("login-file") && (!serviceMode || !autoBootstrap)) throw codedError("login_file_requires_auto_bootstrap");
  if (hasIdentitySource !== hasIdentityOutput) throw codedError("identity_source_options_incomplete");
  if ((hasIdentitySource || hasIdentityOutput) && (!serviceMode || !autoBootstrap)) {
    throw codedError("identity_source_requires_auto_bootstrap");
  }
  if (values.has("login-file") && (hasIdentitySource || hasIdentityOutput)) throw codedError("identity_sources_conflict");
  if (autoBootstrap && !values.has("login-file") && !hasIdentityPair) throw codedError("auto_bootstrap_requires_identity_source");
  const controlOptionKeys = ["control-port", "control-token-file", "account-uin"];
  const controlOptionsPresent = controlOptionKeys.filter((key) => values.has(key)).length;
  if (controlOptionsPresent !== 0 && controlOptionsPresent !== controlOptionKeys.length) {
    throw codedError("control_options_incomplete");
  }
  if (autoBootstrap && controlOptionsPresent !== controlOptionKeys.length) throw codedError("auto_bootstrap_requires_control");
  let control = null;
  if (controlOptionsPresent) {
    const controlPortText = values.get("control-port");
    if (!/^(0|[1-9][0-9]{0,4})$/.test(controlPortText)) throw codedError("invalid_control_port");
    const controlPort = Number(controlPortText);
    if (!Number.isInteger(controlPort) || controlPort > 65535) throw codedError("invalid_control_port");
    const accountUin = values.get("account-uin");
    if (!/^[1-9][0-9]{0,19}$/.test(accountUin)) throw codedError("invalid_account_uin");
    control = {
      controlPort,
      controlTokenFile: path.resolve(values.get("control-token-file")),
      accountUin,
    };
  }
  if (values.has("audio-route-config") && !controlOptionsPresent) throw codedError("audio_route_requires_control");
  if (values.has("kernel-peer-identities-path") && !controlOptionsPresent) throw codedError("kernel_peer_identities_requires_control");
  return {
    help: false,
    serviceMode,
    autoBootstrap,
    targetPid: pid,
    appPath: path.resolve(values.get("app-path")),
    ...(values.has("wrapper") ? { wrapperModulePath: path.resolve(values.get("wrapper")) } : {}),
    sessionName: values.get("session") || "nt_1",
    tokenFile: path.resolve(values.get("token-file")),
    configPath: path.resolve(values.get("config") || path.join(__dirname, "../probe/.runtime", `agent-${pid}.config.json`)),
    reportPath: path.resolve(values.get("report") || path.join(__dirname, "../probe/.runtime", `agent-${pid}`)),
    hostPort,
    outputPort,
    ...(hostPid !== undefined ? { hostPid } : {}),
    hostGeneration,
    connectionGeneration,
    ...(kernelCreationTime100ns !== undefined ? { kernelCreationTime100ns } : {}),
    ...(maxLifetimeMs !== undefined ? { maxLifetimeMs } : {}),
    ...(values.has("login-file") ? { loginFile: path.resolve(values.get("login-file")) } : {}),
    ...(hasIdentitySource ? { identitySourceFile: path.resolve(values.get("identity-source-file")) } : {}),
    ...(hasIdentityOutput ? { identityOutputFile: path.resolve(values.get("identity-output-file")) } : {}),
    ...(values.has("audio-route-config") ? { audioRouteConfigPath: path.resolve(values.get("audio-route-config")) } : {}),
    ...(values.has("kernel-peer-identities-path") ? { kernelPeerIdentitiesPath: path.resolve(values.get("kernel-peer-identities-path")) } : {}),
    ...(control || {}),
  };
}

function readPrivateTokenDetails(filePath) {
  let raw;
  try {
    raw = readBoundedRegularFile(filePath, 64 * 1024, "invalid_token_file").toString("utf8").trim();
  } catch (error) {
    if (error.code && /^[a-z0-9_:-]{1,64}$/i.test(error.code)) throw error;
    throw codedError("token_file_unavailable");
  }
  let token = raw;
  let document = null;
  if (raw.startsWith("{")) {
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch { throw codedError("invalid_token_file"); }
    document = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
    token = document?.ipcToken;
  }
  if (!/^[A-Za-z0-9_-]{43,128}$/.test(token)) throw codedError("invalid_private_token");
  return { token, document };
}

function readPrivateToken(filePath) {
  return readPrivateTokenDetails(filePath).token;
}

function buildAudioRouteObserver({ audioRouteConfigPath, tokenDetails, token, controlEnabled, hostGeneration } = {}) {
  return buildAudioRouteSetup({ audioRouteConfigPath, tokenDetails, token, controlEnabled, hostGeneration }).observer;
}

function buildAudioRouteSetup({ audioRouteConfigPath, tokenDetails, token, controlEnabled, hostGeneration } = {}) {
  if (audioRouteConfigPath === undefined) return { observer: null, audioDeviceTarget: null };
  if (!controlEnabled) throw codedError("audio_route_requires_control");
  const routeConfig = readAudioRouteConfig(audioRouteConfigPath);
  validateHostTokenSidecar(tokenDetails?.document, {
    token,
    targetPid: routeConfig.hostPid,
    hostGeneration,
    hostPid: routeConfig.hostPid,
  });
  if (routeConfig.hostGeneration !== hostGeneration) throw codedError("audio_route_host_sidecar_mismatch");
  return {
    observer: createPersistentAudioRouteObserver({ config: routeConfig, configPath: audioRouteConfigPath }),
    audioDeviceTarget: Object.freeze({
      inputDeviceName: routeConfig.playbackRenderName,
      outputDeviceName: routeConfig.captureRenderName,
    }),
  };
}

function readIdentityFileSnapshot(filePath) {
  let text;
  try {
    text = readBoundedRegularFile(filePath, 64 * 1024, "invalid_identity_file").toString("utf8");
  } catch (error) {
    if (error.code && /^[a-z0-9_:-]{1,64}$/i.test(error.code)) throw error;
    throw codedError("identity_file_unavailable");
  }
  if (Buffer.byteLength(text, "utf8") > 64 * 1024) throw codedError("invalid_identity_file");
  let identity;
  try { identity = JSON.parse(text); }
  catch { throw codedError("invalid_identity_file"); }
  if (!identity || typeof identity !== "object" || Array.isArray(identity) ||
      typeof identity.selfUid !== "string" || !identity.selfUid || identity.selfUid.length > 512 ||
      typeof identity.selfUin !== "string" || !identity.selfUin || identity.selfUin.length > 128 ||
      typeof identity.accountPath !== "string" || !identity.accountPath || identity.accountPath.length > 32768) {
    throw codedError("invalid_identity_file");
  }
  return {
    identity: { selfUid: identity.selfUid, selfUin: identity.selfUin, accountPath: identity.accountPath },
    fingerprint: createHash("sha256").update(text, "utf8").digest("hex"),
  };
}

function readIdentityFile(filePath) {
  return readIdentityFileSnapshot(filePath).identity;
}

function readPrivateOneBotIdentitySnapshot(filePath) {
  let text;
  try {
    text = readBoundedRegularFile(filePath, 64 * 1024, "invalid_onebot_identity_file").toString("utf8");
  } catch (error) {
    if (error.code && /^[a-z0-9_:-]{1,64}$/i.test(error.code)) throw error;
    throw codedError("onebot_identity_file_unavailable");
  }
  let value;
  try { value = JSON.parse(text); }
  catch { throw codedError("invalid_onebot_identity_file"); }
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      typeof value.selfUid !== "string" || value.selfUid.length < 1 || value.selfUid.length > 512) {
    throw codedError("invalid_onebot_identity_file");
  }
  const selfUin = value.selfUin ?? value.accountUin;
  if (typeof selfUin !== "string" || !/^[1-9][0-9]{0,19}$/.test(selfUin) || !Number.isSafeInteger(Number(selfUin)) ||
      (value.selfUin !== undefined && value.accountUin !== undefined && value.selfUin !== value.accountUin)) {
    throw codedError("invalid_onebot_identity_file");
  }
  return {
    identity: { selfUid: value.selfUid, selfUin },
    fingerprint: createHash("sha256").update(text, "utf8").digest("hex"),
  };
}

function readPrivateOneBotIdentityFile(filePath) {
  return readPrivateOneBotIdentitySnapshot(filePath).identity;
}

function samePrivatePath(left, right) {
  return path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
}

function writePrivateJsonIfAbsent(filePath, value) {
  const target = path.resolve(filePath);
  const temporary = path.join(path.dirname(target), `.qqcall-identity-${randomUUID()}.tmp`);
  try {
    writePrivateJson(temporary, value);
    try {
      // A same-directory hard link publishes the already-private file atomically
      // and fails if another writer created the destination after preflight.
      fs.linkSync(temporary, target);
    } catch (error) {
      if (error?.code === "EEXIST") throw codedError("identity_output_exists");
      throw codedError("identity_output_write_failed");
    }
    try { fs.chmodSync(target, 0o600); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch (error) {
    if (error?.code === "identity_output_exists" || error?.code === "identity_output_write_failed") throw error;
    throw codedError("identity_output_write_failed");
  } finally {
    try { fs.rmSync(temporary, { force: true }); } catch { /* best-effort removal of the private staging link */ }
  }
}

function ensurePrivateRealDirectory(directory, code) {
  const target = path.resolve(directory);
  try {
    fs.mkdirSync(target, { recursive: true, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError(code);
    }
    return target;
  } catch {
    throw codedError(code);
  }
}

function createFreshRendererProbeDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "renderer_probe_report_directory_unavailable");
  const target = path.join(runtimeRoot, `renderer-capability-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError("renderer_probe_report_directory_unavailable");
    }
    return target;
  } catch {
    throw codedError("renderer_probe_report_directory_unavailable");
  }
}

function createFreshAsarSourceProbeDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "asar_source_probe_report_directory_unavailable");
  const target = path.join(runtimeRoot, `asar-source-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw codedError("asar_source_probe_report_directory_unavailable");
    try { fs.chmodSync(target, 0o700); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch {
    throw codedError("asar_source_probe_report_directory_unavailable");
  }
}

function createFreshAsarCandidateProbeDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "asar_candidate_probe_report_directory_unavailable");
  const target = path.join(runtimeRoot, `asar-candidate-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError("asar_candidate_probe_report_directory_unavailable");
    }
    try { fs.chmodSync(target, 0o700); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch {
    throw codedError("asar_candidate_probe_report_directory_unavailable");
  }
}

function createFreshOfficialWindowProbeDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "official_window_probe_report_directory_unavailable");
  const target = path.join(runtimeRoot, `official-window-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError("official_window_probe_report_directory_unavailable");
    }
    try { fs.chmodSync(target, 0o700); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch {
    throw codedError("official_window_probe_report_directory_unavailable");
  }
}

function createFreshRendererScriptMetadataProbeDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "renderer_script_metadata_probe_report_directory_unavailable");
  const target = path.join(runtimeRoot, `renderer-script-metadata-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError("renderer_script_metadata_probe_report_directory_unavailable");
    }
    try { fs.chmodSync(target, 0o700); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch {
    throw codedError("renderer_script_metadata_probe_report_directory_unavailable");
  }
}

function createFreshOfficialAVObserverDirectory() {
  const runtimeRoot = ensurePrivateRealDirectory(path.resolve(__dirname, "../probe/.runtime"), "official_av_observer_report_directory_unavailable");
  const target = path.join(runtimeRoot, `official-av-observer-${randomUUID()}`);
  try {
    fs.mkdirSync(target, { recursive: false, mode: 0o700 });
    const stat = fs.lstatSync(target);
    if (!stat.isDirectory() || stat.isSymbolicLink() || !samePrivatePath(fs.realpathSync(target), target)) {
      throw codedError("official_av_observer_report_directory_unavailable");
    }
    try { fs.chmodSync(target, 0o700); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
    return target;
  } catch {
    throw codedError("official_av_observer_report_directory_unavailable");
  }
}

function validateRendererAdmissionResult(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || value.schema !== 1 || typeof value.ok !== "boolean") {
    throw codedError("renderer_probe_admission_invalid");
  }
  const keys = Object.keys(value).sort();
  if (value.ok === true) {
    if (keys.join(",") !== ["admissionFingerprint", "code", "manifestAgeMs", "ok", "schema"].sort().join(",") ||
        value.code !== null || !Number.isSafeInteger(value.manifestAgeMs) || value.manifestAgeMs < -5_000 || value.manifestAgeMs > 10_000 ||
        typeof value.admissionFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(value.admissionFingerprint)) {
      throw codedError("renderer_probe_admission_invalid");
    }
    return value;
  }
  if (keys.join(",") !== ["code", "ok", "schema"].sort().join(",") ||
      typeof value.code !== "string" || !RENDERER_ADMISSION_CODES.has(value.code)) {
    throw codedError("renderer_probe_admission_invalid");
  }
  throw codedError("renderer_probe_admission_denied");
}

function runRendererAdmissionProcess(pythonPath, request, { spawnProcess = spawn, helperPath = RENDERER_ADMISSION_PATH, timeoutMs = 40_000 } = {}) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnProcess(pythonPath, [helperPath], {
        cwd: path.resolve(__dirname, ".."),
        shell: false,
        windowsHide: true,
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      reject(codedError("renderer_probe_admission_unavailable"));
      return;
    }
    let settled = false;
    let stdout = Buffer.alloc(0);
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { child.kill(); } catch { /* fixed timeout result remains authoritative */ }
      reject(codedError("renderer_probe_admission_timeout"));
    }, timeoutMs);
    timer.unref?.();
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (stdout.byteLength + bytes.byteLength > 64 * 1024) {
        settled = true;
        clearTimeout(timer);
        try { child.kill(); } catch { /* fixed output-limit result remains authoritative */ }
        reject(codedError("renderer_probe_admission_invalid"));
        return;
      }
      stdout = Buffer.concat([stdout, bytes]);
    });
    child.on("error", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(codedError("renderer_probe_admission_unavailable"));
    });
    child.on("close", (exitCode) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      let parsed;
      try {
        const text = stdout.toString("utf8");
        const lineEnding = text.endsWith("\r\n") ? "\r\n" : text.endsWith("\n") ? "\n" : null;
        if (!lineEnding) {
          throw codedError("renderer_probe_admission_invalid");
        }
        const jsonText = text.slice(0, -lineEnding.length);
        if (jsonText.includes("\r") || jsonText.includes("\n")) throw codedError("renderer_probe_admission_invalid");
        parsed = JSON.parse(jsonText);
      } catch {
        reject(codedError("renderer_probe_admission_invalid"));
        return;
      }
      try {
        const result = validateRendererAdmissionResult(parsed);
        if (exitCode !== 0) throw codedError("renderer_probe_admission_invalid");
        resolve(result);
      } catch (error) {
        if (parsed?.ok === false && error?.code === "renderer_probe_admission_denied" && exitCode === 2) resolve(parsed);
        else reject(error?.code ? error : codedError("renderer_probe_admission_invalid"));
      }
    });
    try { child.stdin.end(`${JSON.stringify(request)}\n`, "utf8"); }
    catch {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        try { child.kill(); } catch { /* fixed input failure result remains authoritative */ }
        reject(codedError("renderer_probe_admission_unavailable"));
      }
    }
  });
}

function validateRendererProbeOptions(options) {
  const allowed = new Set([
    "help", "probeRendererCapabilities", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath",
  ]);
  if (!options || typeof options !== "object" || Object.keys(options).some((key) => !allowed.has(key)) ||
      options.probeRendererCapabilities !== true || !Number.isSafeInteger(options.targetPid) ||
      options.targetPid < 1 || options.targetPid > 0x7fffffff || !Number.isSafeInteger(options.connectionGeneration) ||
      options.connectionGeneration < 1 || typeof options.appPath !== "string" || !path.isAbsolute(options.appPath) ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) {
    throw codedError("invalid_renderer_probe_options");
  }
}

function waitForRendererProbeReadiness(broker, options, { timeoutMs = 45_000 } = {}) {
  if (typeof broker?.subscribe !== "function") throw codedError("renderer_probe_bootstrap_unavailable");
  let settled = false;
  let checking = false;
  let timer = null;
  let unsubscribe = null;
  let resolvePromise;
  let rejectPromise;
  const promise = new Promise((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  promise.catch(() => {});
  const finish = (error = null) => {
    if (settled) return;
    settled = true;
    if (timer) clearTimeout(timer);
    timer = null;
    try { unsubscribe?.(); } catch { /* subscription cleanup cannot change the readiness result */ }
    unsubscribe = null;
    if (error) rejectPromise(error);
    else resolvePromise(true);
  };
  timer = setTimeout(() => finish(codedError("renderer_probe_bootstrap_timeout")), timeoutMs);
  try {
    unsubscribe = broker.subscribe((op, payload) => {
      if (settled || checking || op !== "bootstrap_status" || payload?.bootstrapLoaded !== true) return;
      if (payload.processPid !== options.targetPid || payload.connectionGeneration !== options.connectionGeneration) {
        finish(codedError("renderer_probe_not_ready"));
        return;
      }
      if (broker.status()?.authenticated !== true) return;
      checking = true;
      Promise.resolve(broker.statusRequest(1000)).then((reply) => {
        const bootstrap = reply?.bootstrap;
        if (reply?.ok === true && broker.status()?.authenticated === true &&
            bootstrap?.bootstrapLoaded === true && bootstrap?.processPid === options.targetPid &&
            bootstrap?.connectionGeneration === options.connectionGeneration) finish();
        else finish(codedError("renderer_probe_not_ready"));
      }).catch(() => finish(codedError("renderer_probe_not_ready")));
    });
  } catch {
    finish(codedError("renderer_probe_bootstrap_unavailable"));
  }
  return {
    promise,
    cancel() {
      if (!settled) {
        settled = true;
        if (timer) clearTimeout(timer);
        timer = null;
        try { unsubscribe?.(); } catch { /* best effort */ }
        unsubscribe = null;
        resolvePromise(false);
      }
    },
  };
}

async function startRendererCapabilityProbe(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  createRunDirectory = createFreshRendererProbeDirectory,
} = {}) {
  validateRendererProbeOptions(options);
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("renderer_probe_token_unavailable"); }
  const fingerprintKey = randomBytes(32).toString("hex");
  const admissionRequest = {
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey,
  };
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint) throw codedError("renderer_probe_admission_changed");
    return admission;
  };

  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "renderer_probe_report_directory_unavailable"); }
  catch { throw codedError("renderer_probe_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "renderer_probe_config_path_unavailable"); }
  catch { throw codedError("renderer_probe_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("renderer_probe_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "renderer_probe_config_exists" ? error : codedError("renderer_probe_config_exists");
  }

  let broker = null;
  let readinessWaiter = null;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      // Readiness can take 45s and admission is bounded at 40s before and
      // after dispatch; retain enough bounded Broker lifetime for both gates.
      maxLifetimeMs: 150_000,
      ipcToken: tokenDetails.token,
      onEvent() { /* capability-only diagnostics do not forward live events */ },
      onStatus() { /* avoid high-frequency console diagnostics in probe mode */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try {
      // Broker.config is the intentionally redacted public view. Reinsert the
      // token only into this private, no-overwrite injection config.
      writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token });
    }
    catch { throw codedError("renderer_probe_config_write_failed"); }
    printLine(output, { kind: "renderer_probe_ready", configWritten: true });

    if (!(await readinessWaiter.promise)) throw codedError("renderer_probe_bootstrap_timeout");
    const beforeDispatch = await confirmAdmission();
    if (beforeDispatch.manifestAgeMs > 10_000) throw codedError("renderer_probe_admission_changed");
    const capability = await broker.probeRendererCapabilities(5000);
    const beforePersist = await confirmAdmission();
    if (!isRendererCapabilityReport(capability)) throw codedError("renderer_probe_invalid_result");
    const safeReport = {
      schema: 1,
      result: "complete",
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      capability,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "renderer-capability.private.json"), safeReport); }
    catch { throw codedError("renderer_probe_report_write_failed"); }
    printLine(output, { kind: "renderer_capability_probe", result: "complete", admissionStable: true, capability });
    return { probeCompleted: true, capability };
  } finally {
    readinessWaiter?.cancel();
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* bounded capability probe has no attach and must still return its fixed result */ }
    }
  }
}

function validateAsarSourceProbeOptions(options) {
  const allowed = new Set([
    "help", "probeAsarSources", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath",
  ]);
  if (!options || typeof options !== "object" || Object.keys(options).some((key) => !allowed.has(key)) ||
      options.probeAsarSources !== true || !Number.isSafeInteger(options.targetPid) ||
      options.targetPid < 1 || options.targetPid > 0x7fffffff || !Number.isSafeInteger(options.connectionGeneration) ||
      options.connectionGeneration < 1 || typeof options.appPath !== "string" || !path.isAbsolute(options.appPath) ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) {
    throw codedError("invalid_asar_source_probe_options");
  }
}

function summarizeAsarSourceProbe(report) {
  return {
    status: report.status,
    code: report.code,
    archiveSha256: report.archiveSha256,
    packageJsonSha256: report.packageJsonSha256,
    wrapperSha256: report.wrapperSha256,
    entries: report.entries.map((entry) => entry.status === "read" || entry.status === "source_omitted"
      ? { label: entry.label, status: entry.status, bytes: entry.bytes, sha256: entry.sha256, ...(entry.status === "source_omitted" ? { code: entry.code } : {}) }
      : { label: entry.label, status: entry.status, code: entry.code }),
  };
}

async function startAsarSourceProbe(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  createRunDirectory = createFreshAsarSourceProbeDirectory,
} = {}) {
  validateAsarSourceProbeOptions(options);
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("asar_source_probe_token_unavailable"); }
  const fingerprintKey = randomBytes(32).toString("hex");
  const admissionRequest = {
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey,
  };
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint) throw codedError("asar_source_probe_admission_changed");
    return admission;
  };

  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "asar_source_probe_report_directory_unavailable"); }
  catch { throw codedError("asar_source_probe_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "asar_source_probe_config_path_unavailable"); }
  catch { throw codedError("asar_source_probe_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("asar_source_probe_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "asar_source_probe_config_exists" ? error : codedError("asar_source_probe_config_exists");
  }

  let broker = null;
  let readinessWaiter = null;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      maxLifetimeMs: 150_000,
      ipcToken: tokenDetails.token,
      onEvent() { /* Private source bytes never enter public event processing. */ },
      onStatus() { /* Avoid forwarding probe status to CLI output. */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try {
      // The public Broker config redacts its token. Add it only to this fresh,
      // private, no-overwrite injector configuration from the private token file.
      writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token });
    } catch { throw codedError("asar_source_probe_config_write_failed"); }
    printLine(output, { kind: "asar_source_probe_ready", configWritten: true });

    if (!(await readinessWaiter.promise)) throw codedError("asar_source_probe_bootstrap_timeout");
    const beforeRead = await confirmAdmission();
    if (beforeRead.manifestAgeMs > 10_000) throw codedError("asar_source_probe_admission_changed");
    const report = await broker.probeAsarSources(5000);
    const beforePersist = await confirmAdmission();
    if (!isAsarSourceProbeReport(report)) throw codedError("asar_source_probe_invalid_result");
    const privateReport = {
      schema: 1,
      result: report.status,
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      report,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "asar-sources.private.json"), privateReport); }
    catch { throw codedError("asar_source_probe_report_write_failed"); }
    const summary = summarizeAsarSourceProbe(report);
    printLine(output, { kind: "asar_source_probe", result: report.status, code: report.code, ...summary });
    return { asarSourceProbeCompleted: true, report };
  } finally {
    readinessWaiter?.cancel();
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* Capability-only diagnostic cleanup remains bounded and local. */ }
    }
  }
}

function validateAsarCandidateProbeOptions(options) {
  const allowed = new Set([
    "help", "probeAsarCandidates", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath",
  ]);
  if (!options || typeof options !== "object" || Object.keys(options).some((key) => !allowed.has(key)) ||
      options.probeAsarCandidates !== true || !Number.isSafeInteger(options.targetPid) ||
      options.targetPid < 1 || options.targetPid > 0x7fffffff || !Number.isSafeInteger(options.connectionGeneration) ||
      options.connectionGeneration < 1 || typeof options.appPath !== "string" || options.appPath !== ASAR_PHYSICAL_APP_PATH ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) {
    throw codedError("invalid_asar_candidate_probe_options");
  }
}

function summarizeAsarCandidateProbe(report) {
  return {
    proofLevel: ASAR_CANDIDATE_PROOF_LEVEL,
    entryCount: report.entries.length,
    readEntryCount: report.entries.filter((entry) => entry.status === "read").length,
    omittedEntryCount: report.entries.filter((entry) => entry.status === "source_omitted").length,
    sourceBytes: report.entries.reduce((total, entry) => total + (Number.isSafeInteger(entry.bytes) ? entry.bytes : 0), 0),
  };
}

async function startAsarCandidateProbe(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  keeperFactory = startAsarPhysicalKeeper,
  createRunDirectory = createFreshAsarCandidateProbeDirectory,
} = {}) {
  validateAsarCandidateProbeOptions(options);
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("asar_candidate_probe_token_unavailable"); }
  const fingerprintKey = randomBytes(32).toString("hex");
  const admissionRequest = {
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey,
  };
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint) throw codedError("asar_candidate_probe_admission_changed");
    return admission;
  };

  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "asar_candidate_probe_report_directory_unavailable"); }
  catch { throw codedError("asar_candidate_probe_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "asar_candidate_probe_config_path_unavailable"); }
  catch { throw codedError("asar_candidate_probe_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("asar_candidate_probe_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "asar_candidate_probe_config_exists" ? error : codedError("asar_candidate_probe_config_exists");
  }

  let broker = null;
  let readinessWaiter = null;
  let keeper = null;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      maxLifetimeMs: 150_000,
      ipcToken: tokenDetails.token,
      onEvent() { /* Private candidate source and keeper evidence never enter public events. */ },
      onStatus() { /* Candidate diagnostics emit only their final fixed summary. */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try {
      writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token });
    } catch { throw codedError("asar_candidate_probe_config_write_failed"); }
    printLine(output, { kind: "asar_candidate_probe_ready", configWritten: true });

    if (!(await readinessWaiter.promise)) throw codedError("asar_candidate_probe_bootstrap_timeout");
    const beforeRead = await confirmAdmission();
    if (beforeRead.manifestAgeMs > 10_000) throw codedError("asar_candidate_probe_admission_changed");

    // Start the native keeper only after the explicitly authenticated bootstrap
    // is ready. It owns fixed physical handles only during the empty RPC.
    keeper = await keeperFactory({
      kernelPid: options.targetPid,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      connectionGeneration: options.connectionGeneration,
      appPath: options.appPath,
      probePythonPath: options.probePythonPath,
    });
    const report = await broker.probeAsarCandidates(5000);
    if (!isAsarCandidateReport(report)) throw codedError("asar_candidate_probe_invalid_result");

    const finalKeeperFrame = await keeper.finish();
    const beforePersist = await confirmAdmission();
    if (beforePersist.manifestAgeMs > 10_000) throw codedError("asar_candidate_probe_admission_changed");
    const privateReport = {
      schema: "qqcall.asar.candidates.private.v1",
      proofLevel: ASAR_CANDIDATE_PROOF_LEVEL,
      status: report.status,
      code: report.code,
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      candidateReport: report,
      physicalKeeper: finalKeeperFrame,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "asar-candidates.private.json"), privateReport); }
    catch { throw codedError("asar_candidate_probe_report_write_failed"); }
    const summary = summarizeAsarCandidateProbe(report);
    printLine(output, { kind: "asar_candidate_probe", status: report.status, code: report.code, ...summary });
    return { asarCandidateProbeCompleted: true, proofLevel: ASAR_CANDIDATE_PROOF_LEVEL, report };
  } finally {
    readinessWaiter?.cancel();
    if (keeper) {
      try { await keeper.cancel(); }
      catch { /* Child ownership remains bounded; failure details are never forwarded. */ }
    }
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* The isolated candidate path has no attach and cleanup is bounded. */ }
    }
  }
}

function validateOfficialWindowProbeOptions(options) {
  const allowed = new Set([
    "help", "probeOfficialWindows", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath",
  ]);
  if (!options || typeof options !== "object" || Object.keys(options).some((key) => !allowed.has(key)) ||
      options.probeOfficialWindows !== true || !Number.isSafeInteger(options.targetPid) ||
      options.targetPid < 1 || options.targetPid > 0x7fffffff || !Number.isSafeInteger(options.connectionGeneration) ||
      options.connectionGeneration < 1 || typeof options.appPath !== "string" || options.appPath !== ASAR_PHYSICAL_APP_PATH ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) {
    throw codedError("invalid_official_window_probe_options");
  }
}

function summarizeOfficialWindowProbe(report) {
  return {
    proofLevel: OFFICIAL_WINDOW_PROOF_LEVEL,
    windowCount: report.windowCount,
    windowsTruncated: report.windowsTruncated,
    rendererMatchCount: report.windows.filter((window) => window.rendererFileMatch).length,
    domObservedCount: report.windows.filter((window) => window.domStatus === "observed").length,
    embedMethodCount: report.windows.filter((window) => window.postMessageAvailable === true).length,
  };
}

async function startOfficialWindowProbe(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  createRunDirectory = createFreshOfficialWindowProbeDirectory,
} = {}) {
  validateOfficialWindowProbeOptions(options);
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("official_window_probe_token_unavailable"); }

  const fingerprintKey = randomBytes(32).toString("hex");
  const admissionRequest = {
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey,
  };
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint) throw codedError("official_window_probe_admission_changed");
    return admission;
  };

  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "official_window_probe_report_directory_unavailable"); }
  catch { throw codedError("official_window_probe_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "official_window_probe_config_path_unavailable"); }
  catch { throw codedError("official_window_probe_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("official_window_probe_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "official_window_probe_config_exists"
      ? error : codedError("official_window_probe_config_exists");
  }

  let broker = null;
  let readinessWaiter = null;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      maxLifetimeMs: 150_000,
      ipcToken: tokenDetails.token,
      onEvent() { /* Window metadata remains in its private diagnostic artifact. */ },
      onStatus() { /* Emit only the final fixed summary. */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try { writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token }); }
    catch { throw codedError("official_window_probe_config_write_failed"); }
    printLine(output, { kind: "official_window_probe_ready", status: "ready" });

    if (!(await readinessWaiter.promise)) throw codedError("official_window_probe_bootstrap_timeout");
    const beforeDispatch = await confirmAdmission();
    if (beforeDispatch.manifestAgeMs > 10_000) throw codedError("official_window_probe_admission_changed");
    const report = await broker.probeOfficialWindows(5000);
    if (!isOfficialWindowReport(report)) throw codedError("official_window_probe_invalid_result");

    const beforePersist = await confirmAdmission();
    if (beforePersist.manifestAgeMs > 10_000) throw codedError("official_window_probe_admission_changed");
    const privateReport = {
      schema: "qqcall.official.windows.private.v1",
      proofLevel: OFFICIAL_WINDOW_PROOF_LEVEL,
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      report,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "official-windows.private.json"), privateReport); }
    catch { throw codedError("official_window_probe_report_write_failed"); }
    const summary = summarizeOfficialWindowProbe(report);
    printLine(output, {
      kind: "official_window_probe",
      status: report.status,
      code: report.code,
      ...summary,
    });
    return { officialWindowProbeCompleted: true, proofLevel: OFFICIAL_WINDOW_PROOF_LEVEL, report };
  } finally {
    readinessWaiter?.cancel();
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* This diagnostic path never attaches and keeps cleanup bounded. */ }
    }
  }
}

function validateRendererScriptMetadataProbeOptions(options) {
  const allowed = new Set([
    "help", "probeRendererScriptMetadata", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath",
  ]);
  let validOwnDataShape = false;
  try {
    if (options && typeof options === "object" && !Array.isArray(options) && Object.getPrototypeOf(options) === Object.prototype) {
      validOwnDataShape = Reflect.ownKeys(options).every((key) => typeof key === "string" && allowed.has(key) &&
        Object.hasOwn(Object.getOwnPropertyDescriptor(options, key) || {}, "value"));
    }
  } catch { validOwnDataShape = false; }
  if (!validOwnDataShape || options.probeRendererScriptMetadata !== true || !Number.isSafeInteger(options.targetPid) ||
      options.targetPid < 1 || options.targetPid > 0x7fffffff || !Number.isSafeInteger(options.connectionGeneration) ||
      options.connectionGeneration < 1 || typeof options.appPath !== "string" || options.appPath !== ASAR_PHYSICAL_APP_PATH ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) {
    throw codedError("invalid_renderer_script_metadata_probe_options");
  }
}

function summarizeRendererScriptMetadataProbe(report) {
  const scripts = report.windows.flatMap((window) => window.scripts);
  return {
    proofLevel: RENDERER_SCRIPT_METADATA_PROOF_LEVEL,
    windowCount: report.windowCount,
    windowsTruncated: report.windowsTruncated,
    exactAVWindowCount: report.exactAVWindowCount,
    scriptRecordCount: report.scriptRecordCount,
    inlineCount: scripts.filter((script) => script.sourceKind === "inline").length,
    localJavaScriptCount: scripts.filter((script) => script.sourceKind === "local_js").length,
    rejectedSourceCount: scripts.filter((script) => script.sourceKind === "rejected").length,
    avsdkMarkerCount: scripts.filter((script) => script.avsdkMarker).length,
    postMessageMarkerCount: scripts.filter((script) => script.postMessageMarker).length,
    sceneIdMarkerCount: scripts.filter((script) => script.sceneIdMarker).length,
  };
}

async function startRendererScriptMetadataProbe(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  createRunDirectory = createFreshRendererScriptMetadataProbeDirectory,
} = {}) {
  validateRendererScriptMetadataProbeOptions(options);
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("renderer_script_metadata_probe_token_unavailable"); }

  const admissionRequest = Object.freeze({
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey: randomBytes(32).toString("hex"),
  });
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint) {
      throw codedError("renderer_script_metadata_probe_admission_changed");
    }
    return admission;
  };

  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "renderer_script_metadata_probe_report_directory_unavailable"); }
  catch { throw codedError("renderer_script_metadata_probe_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "renderer_script_metadata_probe_config_path_unavailable"); }
  catch { throw codedError("renderer_script_metadata_probe_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("renderer_script_metadata_probe_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "renderer_script_metadata_probe_config_exists"
      ? error : codedError("renderer_script_metadata_probe_config_exists");
  }

  let broker = null;
  let readinessWaiter = null;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      maxLifetimeMs: 150_000,
      ipcToken: tokenDetails.token,
      onEvent() { /* Metadata remains in its private diagnostic artifact. */ },
      onStatus() { /* Emit only the final fixed aggregate. */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try { writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token }); }
    catch { throw codedError("renderer_script_metadata_probe_config_write_failed"); }
    printLine(output, { kind: "renderer_script_metadata_probe_ready", status: "ready" });

    if (!(await readinessWaiter.promise)) throw codedError("renderer_script_metadata_probe_bootstrap_timeout");
    const beforeDispatch = await confirmAdmission();
    if (beforeDispatch.manifestAgeMs > 10_000) throw codedError("renderer_script_metadata_probe_admission_changed");
    const report = await broker.probeRendererScriptMetadata(5000);
    if (!isRendererScriptMetadataReport(report)) throw codedError("renderer_script_metadata_probe_invalid_result");

    const beforePersist = await confirmAdmission();
    if (beforePersist.manifestAgeMs > 10_000) throw codedError("renderer_script_metadata_probe_admission_changed");
    const privateReport = {
      schema: "qqcall.renderer.script-metadata.private.v1",
      proofLevel: RENDERER_SCRIPT_METADATA_PROOF_LEVEL,
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      report,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "renderer-script-metadata.private.json"), privateReport); }
    catch { throw codedError("renderer_script_metadata_probe_report_write_failed"); }
    printLine(output, {
      kind: "renderer_script_metadata_probe",
      status: report.status,
      code: report.code,
      ...summarizeRendererScriptMetadataProbe(report),
    });
    return { rendererScriptMetadataProbeCompleted: true, proofLevel: RENDERER_SCRIPT_METADATA_PROOF_LEVEL, report };
  } finally {
    readinessWaiter?.cancel();
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* This diagnostic path never attaches and keeps cleanup bounded. */ }
    }
  }
}

function validateOfficialAVObserverOptions(options) {
  const allowed = new Set(["help", "observeOfficialAV", "targetPid", "appPath", "tokenFile", "configPath",
    "connectionGeneration", "kernelCreationTime100ns", "probePythonPath", "officialAVIncomingOnly", "officialAVNativeConditional"]);
  let validOwnDataShape = false;
  try {
    if (options && typeof options === "object" && !Array.isArray(options) && Object.getPrototypeOf(options) === Object.prototype) {
      const keys = Reflect.ownKeys(options);
      validOwnDataShape = keys.every((key) => typeof key === "string" && allowed.has(key) &&
        Object.hasOwn(Object.getOwnPropertyDescriptor(options, key) || {}, "value"));
    }
  } catch { validOwnDataShape = false; }
  const hasIncomingOnly = validOwnDataShape && Object.hasOwn(options, "officialAVIncomingOnly");
  const hasNativeConditional = validOwnDataShape && Object.hasOwn(options, "officialAVNativeConditional");
  const incomingOnly = hasIncomingOnly && Object.getOwnPropertyDescriptor(options, "officialAVIncomingOnly").value === true;
  const nativeConditional = hasNativeConditional && Object.getOwnPropertyDescriptor(options, "officialAVNativeConditional").value === true;
  if (!validOwnDataShape || options.observeOfficialAV !== true ||
      (hasIncomingOnly && typeof Object.getOwnPropertyDescriptor(options, "officialAVIncomingOnly")?.value !== "boolean") ||
      (hasNativeConditional && typeof Object.getOwnPropertyDescriptor(options, "officialAVNativeConditional")?.value !== "boolean") ||
      (incomingOnly && nativeConditional) ||
      !Number.isSafeInteger(options.targetPid) || options.targetPid < 1 || options.targetPid > 0x7fffffff ||
      !Number.isSafeInteger(options.connectionGeneration) || options.connectionGeneration < 1 ||
      typeof options.appPath !== "string" || options.appPath !== ASAR_PHYSICAL_APP_PATH ||
      typeof options.tokenFile !== "string" || !path.isAbsolute(options.tokenFile) ||
      typeof options.configPath !== "string" || !path.isAbsolute(options.configPath) ||
      typeof options.probePythonPath !== "string" || !path.isAbsolute(options.probePythonPath) ||
      !canonicalFiletime(options.kernelCreationTime100ns)) throw codedError("invalid_official_av_observer_options");
}

function summarizeOfficialAVObserver(report) {
  return {
    status: report.status,
    code: report.code,
    windowCount: report.windows.length,
    armedWindowCount: report.windows.filter((window) => window.status === "armed" || window.status === "armed_incoming_only" || window.status === "armed_native_conditional").length,
    incomingOnlyWindowCount: report.windows.filter((window) => window.status === "armed_incoming_only").length,
    nativeConditionalWindowCount: report.windows.filter((window) => window.status === "armed_native_conditional").length,
    nativeCallAttemptCount: report.windows.reduce((total, window) => Math.min(1000000, total + window.nativeCallAttempts), 0),
    eventCount: report.events.length,
    droppedEvents: report.droppedEvents,
    incomplete: report.incomplete,
    coverageGap: true,
    cleanupComplete: report.cleanupComplete,
  };
}

async function startOfficialAVObserver(options, {
  output = process.stdout,
  runAdmission = (request) => runRendererAdmissionProcess(options.probePythonPath, request),
  brokerFactory = createBroker,
  createRunDirectory = createFreshOfficialAVObserverDirectory,
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  now = Date.now,
  observeForMs = 180000,
} = {}) {
  validateOfficialAVObserverOptions(options);
  const incomingOnly = Object.getOwnPropertyDescriptor(options, "officialAVIncomingOnly")?.value === true;
  const nativeConditional = Object.getOwnPropertyDescriptor(options, "officialAVNativeConditional")?.value === true;
  observeForMs = Number.isFinite(observeForMs) ? Math.min(180000, Math.max(0, observeForMs)) : 180000;
  let tokenDetails;
  try { tokenDetails = readPrivateTokenDetails(options.tokenFile); }
  catch { throw codedError("official_av_observer_token_unavailable"); }
  const fingerprintKey = randomBytes(32).toString("hex");
  const admissionRequest = {
    schema: 1,
    targetPid: options.targetPid,
    targetFiletime: options.kernelCreationTime100ns,
    appPath: options.appPath,
    brokerGeneration: options.connectionGeneration,
    fingerprintKey,
  };
  const firstAdmission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
  const expectedFingerprint = firstAdmission.admissionFingerprint;
  const confirmAdmission = async () => {
    const admission = validateRendererAdmissionResult(await runAdmission(admissionRequest));
    if (admission.admissionFingerprint !== expectedFingerprint || admission.manifestAgeMs > 10000) {
      throw codedError("official_av_observer_admission_changed");
    }
    return admission;
  };
  let runDirectory;
  try { runDirectory = ensurePrivateRealDirectory(createRunDirectory(), "official_av_observer_report_directory_unavailable"); }
  catch { throw codedError("official_av_observer_report_directory_unavailable"); }
  const reportBase = path.join(runDirectory, "agent");
  try { ensurePrivateRealDirectory(path.dirname(options.configPath), "official_av_observer_config_path_unavailable"); }
  catch { throw codedError("official_av_observer_config_path_unavailable"); }
  try {
    fs.lstatSync(options.configPath);
    throw codedError("official_av_observer_config_exists");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error?.code === "official_av_observer_config_exists"
      ? error : codedError("official_av_observer_config_exists");
  }
  let broker = null;
  let readinessWaiter = null;
  let observerStarted = false;
  let observerStopped = false;
  try {
    broker = brokerFactory({
      targetPid: options.targetPid,
      modulePath: MODULE_PATH,
      appPath: options.appPath,
      reportPath: reportBase,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      sessionName: "nt_1",
      connectionGeneration: options.connectionGeneration,
      maxLifetimeMs: 240000,
      ipcToken: tokenDetails.token,
      onEvent() { /* Renderer data is private and never enters the public event stream. */ },
      onStatus() { /* Only aggregate state is printed by this finite CLI. */ },
    });
    await broker.start();
    readinessWaiter = waitForRendererProbeReadiness(broker, options);
    try { writePrivateJsonIfAbsent(options.configPath, { ...broker.config, ipcToken: tokenDetails.token }); }
    catch { throw codedError("official_av_observer_config_write_failed"); }
    printLine(output, { kind: "official_av_observer_ready", status: "ready" });
    if (!(await readinessWaiter.promise)) throw codedError("official_av_observer_bootstrap_timeout");
    await confirmAdmission();
    const startReport = await broker.startOfficialAVObserver(5000,
      incomingOnly ? { incomingOnly: true } : nativeConditional ? { nativeConditional: true } : {});
    if (!isObserverReport(startReport)) throw codedError("official_av_observer_invalid_result");
    observerStarted = true;
    let latestReport = startReport;
    let latestSummary = summarizeOfficialAVObserver(latestReport);
    let previousSummaryText = JSON.stringify(latestSummary);
    printLine(output, { kind: "official_av_observer_started", ...latestSummary });
    const deadline = now() + observeForMs;
    while (startReport.status === "active" && latestReport.status === "active" && now() < deadline) {
      const remaining = deadline - now();
      await sleep(Math.min(2000, Math.max(1, remaining)));
      if (now() >= deadline) break;
      latestReport = await broker.drainOfficialAVObserver(5000);
      if (!isObserverReport(latestReport)) throw codedError("official_av_observer_invalid_result");
      latestSummary = summarizeOfficialAVObserver(latestReport);
      const summaryText = JSON.stringify(latestSummary);
      if (summaryText !== previousSummaryText) {
        printLine(output, { kind: "official_av_observer_update", ...latestSummary });
        previousSummaryText = summaryText;
      }
    }
    if (latestReport.status === "active") {
      latestReport = await broker.drainOfficialAVObserver(5000);
      if (!isObserverReport(latestReport)) throw codedError("official_av_observer_invalid_result");
    }
    const finalReport = await broker.stopOfficialAVObserver(5000);
    observerStopped = true;
    if (!isObserverReport(finalReport) || !["stopped", "expired"].includes(finalReport.status)) {
      throw codedError("official_av_observer_invalid_result");
    }
    const beforePersist = await confirmAdmission();
    const privateReport = {
      schema: "qqcall.official.av.observer.private.v1",
      proofLevel: OFFICIAL_AV_PROOF_LEVEL,
      admissionStable: true,
      manifestAgeMs: beforePersist.manifestAgeMs,
      startReport,
      report: finalReport,
    };
    try { writePrivateJsonIfAbsent(path.join(runDirectory, "official-av-observer.private.json"), privateReport); }
    catch { throw codedError("official_av_observer_report_write_failed"); }
    const finalSummary = summarizeOfficialAVObserver(finalReport);
    if (JSON.stringify(finalSummary) !== previousSummaryText) {
      printLine(output, { kind: "official_av_observer_complete", ...finalSummary });
    }
    return { officialAVObserverCompleted: true, proofLevel: OFFICIAL_AV_PROOF_LEVEL, report: finalReport };
  } finally {
    readinessWaiter?.cancel();
    if (broker && observerStarted && !observerStopped) {
      try { await broker.stopOfficialAVObserver(5000); }
      catch { /* disconnect cleanup is bounded and diagnostic-only */ }
    }
    if (broker) {
      try { await broker.stop({ confirmDetach: false }); }
      catch { /* Observer mode never attaches; retain only its bounded private report. */ }
    }
  }
}

function readKernelPeerIdentitiesFile(filePath, { accountUin, targetPid, connectionGeneration } = {}) {
  if (typeof filePath !== "string" || !filePath ||
      typeof accountUin !== "string" || !CANONICAL_UIN.test(accountUin) ||
      !Number.isSafeInteger(targetPid) || targetPid <= 0 || targetPid > 0x7fffffff ||
      !Number.isSafeInteger(connectionGeneration) || connectionGeneration <= 0) {
    throw codedError("invalid_kernel_peer_identities_binding");
  }
  let text;
  try {
    text = readBoundedRegularFile(filePath, 32 * 1024, "invalid_kernel_peer_identities_file").toString("utf8");
  } catch (error) {
    if (error.code === "invalid_kernel_peer_identities_file") throw error;
    throw codedError("kernel_peer_identities_unavailable");
  }
  let value;
  try { value = JSON.parse(text); }
  catch { throw codedError("invalid_kernel_peer_identities_file"); }
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype ||
      Reflect.ownKeys(value).length !== 4 ||
      !["accountUin", "targetPid", "connectionGeneration", "peerUidsByUin"].every((key) => Object.prototype.hasOwnProperty.call(value, key)) ||
      typeof value.accountUin !== "string" || !CANONICAL_UIN.test(value.accountUin) ||
      !Number.isSafeInteger(value.targetPid) || value.targetPid <= 0 || value.targetPid > 0x7fffffff ||
      !Number.isSafeInteger(value.connectionGeneration) || value.connectionGeneration <= 0) {
    throw codedError("invalid_kernel_peer_identities_file");
  }
  if (value.accountUin !== accountUin || value.targetPid !== targetPid || value.connectionGeneration !== connectionGeneration) {
    throw codedError("kernel_peer_identities_binding_mismatch");
  }
  const mappings = value.peerUidsByUin;
  if (!mappings || typeof mappings !== "object" || Array.isArray(mappings) || Object.getPrototypeOf(mappings) !== Object.prototype) {
    throw codedError("invalid_kernel_peer_identities_file");
  }
  let keys;
  try { keys = Reflect.ownKeys(mappings); }
  catch { throw codedError("invalid_kernel_peer_identities_file"); }
  if (keys.length > 64) throw codedError("invalid_kernel_peer_identities_file");
  const copy = Object.create(null);
  for (const key of keys) {
    if (typeof key !== "string" || key === "__proto__" || key === "prototype" || key === "constructor" ||
        !CANONICAL_UIN.test(key) || typeof mappings[key] !== "string" || !QQ_UID.test(mappings[key])) {
      throw codedError("invalid_kernel_peer_identities_file");
    }
    copy[key] = mappings[key];
  }
  return Object.freeze(copy);
}

function safeStatus(broker, host, bridge) {
  const brokerStatus = broker.status();
  const latest = brokerStatus.agent && typeof brokerStatus.agent === "object" ? brokerStatus.agent : {};
  const report = latest.result && typeof latest.result === "object" ? latest.result : latest;
  const bootstrap = report.bootstrap && typeof report.bootstrap === "object" ? report.bootstrap : latest.bootstrap && typeof latest.bootstrap === "object" ? latest.bootstrap : report;
  const agent = report.agent && typeof report.agent === "object" ? report.agent : latest.agent && typeof latest.agent === "object" ? latest.agent : {};
  const bootstrapSummary = summarizeBootstrapStatus(bootstrap);
  return {
    broker: {
      endpointReady: brokerStatus.endpointReady,
      connected: brokerStatus.connected,
      authenticated: brokerStatus.authenticated,
      targetPid: brokerStatus.targetPid,
      connectionGeneration: brokerStatus.connectionGeneration,
      eventCount: brokerStatus.eventCount,
      rejectedConnections: brokerStatus.rejectedConnections,
      lastFailureCode: brokerStatus.lastFailureCode,
      stopped: brokerStatus.stopped,
    },
    agent: {
      ...bootstrapSummary,
      listenerRegistered: typeof agent.listenerRegistered === "boolean" ? agent.listenerRegistered :
        typeof bootstrap.listenerRegistered === "boolean" ? bootstrap.listenerRegistered : null,
      state: typeof agent.state === "string" ? agent.state : null,
      lastFailureCode: typeof agent.lastFailureCode === "string" ? agent.lastFailureCode : bootstrapSummary.lastFailureCode || null,
    },
    host: host.status(),
    bridge: bridge.status(),
  };
}

function parseCommandLine(line) {
  const value = typeof line === "string" ? line.trim() : "";
  if (!value) return { command: "", argument: "" };
  const separator = value.search(/\s/);
  const requested = (separator < 0 ? value : value.slice(0, separator)).toLowerCase();
  const known = new Set(["status", "attach", "identity", "login", "accept", "quit", "adapter-pause", "adapter-resume"]);
  if (!known.has(requested)) return { command: "unknown", argument: "" };
  const rawArgument = separator < 0 ? "" : value.slice(separator).trim();
  const argument = requested === "identity" ? rawArgument :
    rawArgument.replace(/^(?:"([\s\S]*)"|'([\s\S]*)')$/, (_match, doubleQuoted, singleQuoted) => doubleQuoted ?? singleQuoted);
  return { command: requested, argument };
}

function parseIdentityFileArguments(value) {
  const source = typeof value === "string" ? value : "";
  const tokens = [];
  let index = 0;
  while (index < source.length) {
    while (/\s/.test(source[index] || "")) index += 1;
    if (index >= source.length) break;
    const quote = source[index] === '"' || source[index] === "'" ? source[index++] : null;
    let token = "";
    if (quote) {
      while (index < source.length && source[index] !== quote) token += source[index++];
      if (source[index] !== quote) throw codedError("identity_file_arguments_invalid");
      index += 1;
      if (index < source.length && !/\s/.test(source[index])) throw codedError("identity_file_arguments_invalid");
    } else {
      while (index < source.length && !/\s/.test(source[index])) token += source[index++];
    }
    if (!token) throw codedError("identity_file_arguments_invalid");
    tokens.push(token);
    if (tokens.length > 2) throw codedError("identity_file_arguments_invalid");
  }
  if (tokens.length !== 2) throw codedError("identity_file_arguments_invalid");
  return tokens;
}

function printLine(stream, value) {
  stream.write(`${JSON.stringify(value)}\n`);
}

function printUsage(stream = process.stdout) {
  stream.write([
    "QQCall standalone Bridge controller (starts no QQ process and performs no injection)",
    "Required: --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "          --host-port <1..65535> --output-port <1..65535>",
    "          --host-generation <n> --connection-generation <n>",
    "Optional: --wrapper <wrapper.node> --session nt_1 --config <privateConfigPath> --report <reportBase>",
    "          --lifetime-ms <1000..3600000> (bounded Broker lifetime; default 600000; probe mode only)",
    "Diagnostic only: --probe-renderer-capabilities --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython>",
    "Diagnostic only: --probe-asar-sources --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython>",
    "Diagnostic only: --probe-asar-candidates --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython>",
    "Diagnostic only: --probe-official-windows --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython>",
    "Diagnostic only: --probe-renderer-script-metadata --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython>",
    "Diagnostic only: --observe-official-av --pid <qqPid> --app-path <qqAppPath> --token-file <privateTokenFile>",
    "                --config <newPrivateConfigPath> --connection-generation <n> --kernel-creation-time-100ns <filetime> --probe-python <absolutePython> [--incoming-only | --native-conditional]",
    "          --service-mode (ignore stdin EOF and disable the Broker lifetime timer)",
    "          --auto-bootstrap --login-file <privateIdentityJSON> OR --identity-source-file <privateOneBotJSON> --identity-output-file <newPrivateIdentityJSON>",
    "          (auto-bootstrap requires service mode and complete adapter control; identity input modes are mutually exclusive)",
    "Opt-in adapter control: --control-port <0..65535> --control-token-file <separatePrivateTokenFile> --account-uin <numericQQUIN>",
    "Optional route proof: --audio-route-config <privateSchema1JSON> (requires adapter control and the matching Host config sidecar)",
    "Optional invite identity map: --kernel-peer-identities-path <privateBoundJSON> (requires adapter control)",
    "Commands: status | attach | identity <privateOneBotIdentityFile> <outputPrivateLoginFile> | login <privateIdentityFile> | accept <opaqueCallId> | quit",
    "The config file contains the Host IPC token. Keep it private and pass it to the separately controlled Shim loader.",
    "Adapter control starts no QQ process and performs no injection; auto-bootstrap is opt-in and generation/auth gated.",
    "",
  ].join("\n"));
}

async function startTransportBeforeHost({ broker, bridge, host, configPath, onBrokerStarted = () => {} } = {}) {
  const brokerStatus = await broker.start();
  onBrokerStarted();
  await broker.writeConfigFile(configPath);
  bridge.prepareSubscriptions();
  const hostStatus = await host.start();
  return { brokerStatus, hostStatus };
}

async function startCli(options, { input = process.stdin, output = process.stdout, errorOutput = process.stderr, rendererProbeDependencies = {}, asarSourceProbeDependencies = {}, asarCandidateProbeDependencies = {}, officialWindowProbeDependencies = {}, rendererScriptMetadataProbeDependencies = {}, officialAVObserverDependencies = {} } = {}) {
  if (options?.probeRendererCapabilities === true) {
    return startRendererCapabilityProbe(options, { output, ...rendererProbeDependencies });
  }
  if (options?.probeAsarSources === true) {
    return startAsarSourceProbe(options, { output, ...asarSourceProbeDependencies });
  }
  if (options?.probeAsarCandidates === true) {
    return startAsarCandidateProbe(options, { output, ...asarCandidateProbeDependencies });
  }
  if (options?.probeOfficialWindows === true) {
    return startOfficialWindowProbe(options, { output, ...officialWindowProbeDependencies });
  }
  if (options?.probeRendererScriptMetadata === true) {
    return startRendererScriptMetadataProbe(options, { output, ...rendererScriptMetadataProbeDependencies });
  }
  if (options?.observeOfficialAV === true) {
    return startOfficialAVObserver(options, { output, ...officialAVObserverDependencies });
  }
  const serviceMode = options.serviceMode === undefined ? false : options.serviceMode;
  const autoBootstrap = options.autoBootstrap === undefined ? false : options.autoBootstrap;
  if (typeof serviceMode !== "boolean") throw codedError("invalid_service_mode");
  if (typeof autoBootstrap !== "boolean") throw codedError("invalid_auto_bootstrap");
  if (autoBootstrap && !serviceMode) throw codedError("auto_bootstrap_requires_service_mode");
  if (serviceMode && options.maxLifetimeMs !== undefined) throw codedError("service_mode_lifetime_conflict");
  if (options.loginFile !== undefined && (!serviceMode || !autoBootstrap)) throw codedError("login_file_requires_auto_bootstrap");
  const hasIdentitySource = options.identitySourceFile !== undefined && options.identitySourceFile !== null;
  const hasIdentityOutput = options.identityOutputFile !== undefined && options.identityOutputFile !== null;
  const hasIdentityPair = hasIdentitySource && hasIdentityOutput;
  if (hasIdentitySource !== hasIdentityOutput) throw codedError("identity_source_options_incomplete");
  if ((hasIdentitySource || hasIdentityOutput) && (!serviceMode || !autoBootstrap)) {
    throw codedError("identity_source_requires_auto_bootstrap");
  }
  if (options.loginFile !== undefined && (hasIdentitySource || hasIdentityOutput)) throw codedError("identity_sources_conflict");
  if (autoBootstrap && typeof options.loginFile !== "string" && !hasIdentityPair) {
    throw codedError("auto_bootstrap_requires_identity_source");
  }
  if (hasIdentityPair && (typeof options.identitySourceFile !== "string" || !options.identitySourceFile ||
      typeof options.identityOutputFile !== "string" || !options.identityOutputFile)) {
    throw codedError("identity_source_options_incomplete");
  }
  if (options.maxLifetimeMs !== undefined &&
      (!Number.isSafeInteger(options.maxLifetimeMs) || options.maxLifetimeMs < 1000 || options.maxLifetimeMs > 60 * 60 * 1000)) {
    throw codedError("invalid_lifetime");
  }
  if (options.hostPid !== undefined &&
      (!Number.isSafeInteger(options.hostPid) || options.hostPid < 1 || options.hostPid > 0x7fffffff)) {
    throw codedError("invalid_host_pid");
  }
  const tokenDetails = readPrivateTokenDetails(options.tokenFile);
  const token = tokenDetails.token;
  if (options.hostPid !== undefined) {
    const metadata = tokenDetails.document;
    if (!metadata || metadata.ipcToken !== token || metadata.targetPid !== options.hostPid ||
        metadata.hostGeneration !== options.hostGeneration || metadata.listenPort !== options.hostPort ||
        metadata.outputPort !== options.outputPort) {
      throw codedError("host_sidecar_binding_mismatch");
    }
  }
  const controlValues = [options.controlPort, options.controlTokenFile, options.accountUin];
  const controlPresent = controlValues.filter((value) => value !== undefined && value !== null).length;
  if (controlPresent !== 0 && controlPresent !== controlValues.length) throw codedError("control_options_incomplete");
  if (autoBootstrap && controlPresent !== controlValues.length) throw codedError("auto_bootstrap_requires_control");
  let controlToken = null;
  if (controlPresent) {
    if (!Number.isInteger(options.controlPort) || options.controlPort < 0 || options.controlPort > 65535) throw codedError("invalid_control_port");
    if (typeof options.accountUin !== "string" || !/^[1-9][0-9]{0,19}$/.test(options.accountUin)) throw codedError("invalid_account_uin");
    controlToken = readPrivateToken(options.controlTokenFile);
    if (controlToken === token) throw codedError("control_token_must_be_distinct");
  }
  let autoLoginSnapshot = null;
  const autoIdentitySourceKind = hasIdentityPair ? "onebot" : options.loginFile !== undefined ? "full" : null;
  if (autoBootstrap) {
    if (autoIdentitySourceKind === "full") {
      if (typeof options.loginFile !== "string" || !options.loginFile) throw codedError("auto_bootstrap_requires_identity_source");
      try { autoLoginSnapshot = readIdentityFileSnapshot(options.loginFile); }
      catch (error) {
        if (error?.code === "invalid_identity_file") throw error;
        throw codedError("identity_file_unavailable");
      }
    } else {
      const outputPath = path.resolve(options.identityOutputFile);
      if (samePrivatePath(options.identitySourceFile, outputPath)) throw codedError("identity_output_matches_input");
      if (fs.existsSync(outputPath)) throw codedError("identity_output_exists");
      try { autoLoginSnapshot = readPrivateOneBotIdentitySnapshot(options.identitySourceFile); }
      catch (error) {
        if (error?.code === "invalid_onebot_identity_file") throw error;
        throw codedError("identity_file_unavailable");
      }
    }
    if (autoLoginSnapshot.identity.selfUin !== options.accountUin) throw codedError("configured_account_mismatch");
  }
  if (options.kernelPeerIdentitiesPath !== undefined && !controlPresent) throw codedError("kernel_peer_identities_requires_control");
  const kernelPeerUidByUin = options.kernelPeerIdentitiesPath !== undefined
    ? readKernelPeerIdentitiesFile(options.kernelPeerIdentitiesPath, {
      accountUin: options.accountUin,
      targetPid: options.targetPid,
      connectionGeneration: options.connectionGeneration,
    })
    : undefined;
  const audioRouteSetup = buildAudioRouteSetup({
    audioRouteConfigPath: options.audioRouteConfigPath,
    tokenDetails,
    token,
    controlEnabled: controlPresent === controlValues.length,
    hostGeneration: options.hostGeneration,
  });
  const audioRouteObserver = audioRouteSetup.observer;
  const broker = createBroker({
    targetPid: options.targetPid,
    modulePath: MODULE_PATH,
    appPath: options.appPath,
    wrapperModulePath: options.wrapperModulePath,
    reportPath: options.reportPath,
    ...(options.kernelCreationTime100ns !== undefined ? { kernelCreationTime100ns: options.kernelCreationTime100ns } : {}),
    sessionName: options.sessionName,
    connectionGeneration: options.connectionGeneration,
    serviceMode,
    ...(options.maxLifetimeMs !== undefined ? { maxLifetimeMs: options.maxLifetimeMs } : {}),
    ipcToken: token,
    onEvent(op, payload) {
      const summary = op === "bootstrap_status" ? summarizeBootstrapStatus(payload) : summarizeEvent(op, payload);
      printLine(output, { kind: "agent_event", event: summary });
    },
    onStatus(status) {
      printLine(output, {
        kind: "broker_status",
        status: {
          endpointReady: status.endpointReady,
          connected: status.connected,
          authenticated: status.authenticated,
          eventCount: status.eventCount,
          rejectedConnections: status.rejectedConnections,
          lastFailureCode: status.lastFailureCode,
          stopped: status.stopped,
        },
      });
    },
  });
  const host = createHostClient({
    token,
    listenPort: options.hostPort,
    outputPort: options.outputPort,
    hostGeneration: options.hostGeneration,
    ...(options.hostPid !== undefined ? { targetPid: options.hostPid } : {}),
    onFailure: (event) => printLine(output, { kind: "host_failure", code: event.code,
      ...(["health", "output_callback", "group_room_query", "group_start", "group_quit", "audio_device_selection", "invoke"]
        .includes(event.operation) ? { operation: event.operation } : {}) }),
  });
  const bridge = createQQCallBridge({
    officialPrivateInviteLifecycle: (body) => broker.privateInviteLifecycle(body),
    provider: broker,
    host,
    connectionGeneration: options.connectionGeneration,
    hostGeneration: options.hostGeneration,
    ...(audioRouteSetup.audioDeviceTarget ? { audioDeviceTarget: audioRouteSetup.audioDeviceTarget } : {}),
    ...(kernelPeerUidByUin ? { kernelPeerUidByUin } : {}),
    onStatus: (event) => printLine(output, { kind: "bridge_status", event: event.kind, status: event.status }),
  });
  const controlService = controlPresent ? createControlService({
    bridge,
    token: controlToken,
    accountId: options.accountUin,
    port: options.controlPort,
    audioRouteObserver,
    officialAVObserver: {
      // QQ's native method cannot be wrapped and its debugger attachment is
      // unconfirmed on the pinned build. Service diagnostics stay passive.
      start: () => broker.startOfficialAVObserver(5000, { incomingOnly: true }),
      drain: () => broker.drainOfficialAVObserver(5000),
      stop: () => broker.stopOfficialAVObserver(5000),
    },
  }) : null;

  let brokerStarted = false;
  let hostStarted = false;
  let interfaceHandle = null;
  let commandTail = Promise.resolve();
  let closed = false;
  let eofHandling = false;
  let shutdownResult = null;
  let shutdownOperation = null;
  let signalHandlers = null;
  let autoBootstrapUnsubscribe = null;
  let autoBootstrapEvidence = null;
  let autoBootstrapScheduled = false;
  let runtimeReady = false;
  const configPath = options.configPath;
  const clearControllerExitObserver = installControllerExitObserver(options.reportPath);

  function exactAutoBootstrapEvidence(payload) {
    return payload && typeof payload === "object" && !Array.isArray(payload) &&
      payload.sessionFound === true && payload.serviceAvailable === true &&
      payload.connectionGeneration === options.connectionGeneration &&
      payload.processPid === options.targetPid;
  }

  function brokerIsCurrent() {
    const current = broker.status();
    return current.connected === true && current.authenticated === true &&
      current.targetPid === options.targetPid && current.connectionGeneration === options.connectionGeneration;
  }

  function currentBootstrapPayload() {
    let agent = broker.status().agent;
    if (!agent || typeof agent !== "object" || Array.isArray(agent)) return null;
    if (agent.bootstrap && typeof agent.bootstrap === "object" && !Array.isArray(agent.bootstrap)) agent = agent.bootstrap;
    else if (agent.result?.bootstrap && typeof agent.result.bootstrap === "object" && !Array.isArray(agent.result.bootstrap)) {
      agent = agent.result.bootstrap;
    }
    return agent;
  }

  function autoAttachEvidenceIsCurrent() {
    return exactAutoBootstrapEvidence(autoBootstrapEvidence) &&
      exactAutoBootstrapEvidence(currentBootstrapPayload()) && brokerIsCurrent();
  }

  function revalidateAutoLoginIdentity() {
    if (!autoLoginSnapshot || !autoBootstrap) throw codedError("identity_file_unavailable");
    let current;
    try {
      current = autoIdentitySourceKind === "full"
        ? readIdentityFileSnapshot(options.loginFile)
        : readPrivateOneBotIdentitySnapshot(options.identitySourceFile);
    }
    catch { throw codedError("identity_file_unavailable"); }
    if (current.fingerprint !== autoLoginSnapshot.fingerprint) throw codedError("identity_file_changed");
    if (current.identity.selfUin !== options.accountUin || current.identity.selfUin !== autoLoginSnapshot.identity.selfUin) {
      throw codedError("configured_account_mismatch");
    }
    return current.identity;
  }

  function autoLoginGateIsCurrent() {
    const kernel = broker.status();
    const bridgeState = bridge.status();
    const hostState = host.status();
    return brokerIsCurrent() && bridgeState.attached === true &&
      bridgeState.connectionGeneration === options.connectionGeneration &&
      bridgeState.hostGeneration === options.hostGeneration &&
      hostState.available === true && hostState.hostGeneration === options.hostGeneration &&
      kernel.targetPid === options.targetPid && kernel.connectionGeneration === options.connectionGeneration;
  }

  function waitForAutoLoginReady(timeoutMs = 15_000) {
    if (bridge.status().loginState === "ready") return Promise.resolve(true);
    return new Promise((resolve, reject) => {
      let settled = false;
      let unsubscribe = null;
      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        unsubscribe?.();
        if (error) reject(error);
        else resolve(true);
      };
      const timer = setTimeout(() => finish(codedError("login_ack_timeout")), timeoutMs);
      timer.unref?.();
      unsubscribe = bridge.subscribeStatus((event) => {
        const state = event?.status?.loginState;
        if (state === "ready") finish();
        else if (["failed", "invalidated", "logged_out"].includes(state)) finish(codedError("native_login_failed"));
      });
      const state = bridge.status().loginState;
      if (state === "ready") finish();
      else if (["failed", "invalidated", "logged_out"].includes(state)) finish(codedError("native_login_failed"));
    });
  }

  function reportAutoBootstrapFailure(error) {
    const code = typeof error?.code === "string" && /^[A-Za-z0-9_:-]{1,64}$/.test(error.code)
      ? error.code : "auto_bootstrap_failed";
    printLine(errorOutput, { kind: "auto_bootstrap_failed", code });
  }

  function maybeStartAutoBootstrap() {
    if (!autoBootstrap || !runtimeReady || closed || shutdownOperation || autoBootstrapScheduled || !autoAttachEvidenceIsCurrent()) return;
    autoBootstrapScheduled = true;
    const run = async () => {
      if (closed || !autoAttachEvidenceIsCurrent()) throw codedError("bootstrap_evidence_stale");
      revalidateAutoLoginIdentity();
      await handleCommand("attach");
      const attached = bridge.status();
      if (attached.attached !== true || attached.connectionGeneration !== options.connectionGeneration ||
          attached.hostGeneration !== options.hostGeneration || !brokerIsCurrent()) {
        throw codedError("listener_attach_unconfirmed");
      }
      // Read the file again after confirmed attachment so replacement during
      // the serialized attach cannot silently change the account identity.
      let identity = revalidateAutoLoginIdentity();
      if (!autoLoginGateIsCurrent()) throw codedError("auto_login_generation_mismatch");
      let accountPath;
      try { accountPath = await broker.getAccountPath(identity.selfUin); }
      catch { throw codedError("auto_login_account_path_unavailable"); }
      if (!path.isAbsolute(accountPath)) throw codedError("auto_login_account_path_unavailable");
      if (autoIdentitySourceKind === "full" && (!path.isAbsolute(identity.accountPath) ||
          !samePrivatePath(identity.accountPath, accountPath))) {
        throw codedError("auto_login_account_path_mismatch");
      }
      identity = revalidateAutoLoginIdentity();
      if (!autoLoginGateIsCurrent()) throw codedError("auto_login_generation_mismatch");
      if (autoIdentitySourceKind === "onebot") {
        writePrivateJsonIfAbsent(options.identityOutputFile, {
          selfUid: identity.selfUid,
          selfUin: identity.selfUin,
          accountPath,
        });
        identity = { selfUid: identity.selfUid, selfUin: identity.selfUin, accountPath };
      }
      if (!autoLoginGateIsCurrent()) throw codedError("auto_login_generation_mismatch");
      printLine(output, { kind: "auto_login_identity_verified", result: { accountPathVerified: true } });
      const loginState = bridge.status().loginState;
      if (loginState === "ready") {
        printLine(output, { kind: "auto_login_ready", result: { posted: false, loginState: "ready" } });
        return;
      }
      if (loginState !== "logged_out") throw codedError("login_already_pending");
      let loginResult;
      try { loginResult = await bridge.login(identity); }
      finally {
        identity.selfUid = "";
        identity.selfUin = "";
        identity.accountPath = "";
      }
      if (loginResult?.posted !== true) throw codedError("login_post_receipt_invalid");
      await waitForAutoLoginReady();
      if (!autoLoginGateIsCurrent() || bridge.status().loginState !== "ready") throw codedError("auto_login_generation_mismatch");
      printLine(output, { kind: "auto_login_ready", result: { posted: true, loginState: "ready" } });
    };
    enqueueSerialized(run).catch(reportAutoBootstrapFailure);
  }

  function onBrokerEvent(op, payload) {
    if (!autoBootstrap || op !== "bootstrap_status") return;
    autoBootstrapEvidence = payload;
    maybeStartAutoBootstrap();
  }

  function removeSignalHandlers() {
    if (!signalHandlers) return;
    process.removeListener("SIGINT", signalHandlers.onSignal);
    process.removeListener("SIGTERM", signalHandlers.onSignal);
    signalHandlers = null;
  }

  async function performCleanShutdown() {
    if (shutdownResult) return shutdownResult;
    if (controlService) await controlService.stop();
    const bridgeStop = await bridge.stop();
    if (bridgeStop?.stopped !== true || bridgeStop?.detachedConfirmed !== true) throw codedError("detach_unconfirmed");
    const brokerStop = await broker.stop({ confirmDetach: false });
    if (brokerStop?.stopped !== true || brokerStop?.detachedConfirmed !== true) throw codedError("broker_detach_unconfirmed");
    await host.stop();
    shutdownResult = { stopped: true, detachedConfirmed: true };
    closed = true;
    removeSignalHandlers();
    interfaceHandle?.close();
    return shutdownResult;
  }

  async function performServiceShutdown() {
    if (shutdownResult) return shutdownResult;
    let failureCode = null;
    if (controlService) {
      try { await controlService.stop(); }
      catch (error) { failureCode ||= error?.code || "control_stop_failed"; }
    }
    let bridgeStop = null;
    try { bridgeStop = await bridge.stop(); }
    catch (error) { failureCode ||= error?.code || "detach_unconfirmed"; }
    let brokerStop = null;
    try { brokerStop = await broker.stop({ confirmDetach: false }); }
    catch (error) { failureCode ||= error?.code || "broker_stop_failed"; }
    try { await host.stop(); }
    catch (error) { failureCode ||= error?.code || "host_stop_failed"; }
    const detachedConfirmed = bridgeStop?.stopped === true && bridgeStop?.detachedConfirmed === true &&
      brokerStop?.stopped === true && brokerStop?.detachedConfirmed === true;
    shutdownResult = {
      stopped: true,
      detachedConfirmed,
      ...(!detachedConfirmed && failureCode
        ? { code: /^[A-Za-z0-9_:-]{1,64}$/.test(failureCode) ? failureCode : "detach_unconfirmed" }
        : {}),
    };
    closed = true;
    autoBootstrapUnsubscribe?.();
    autoBootstrapUnsubscribe = null;
    removeSignalHandlers();
    interfaceHandle?.close();
    return shutdownResult;
  }

  function trackShutdown(operation) {
    const tracked = Promise.resolve(operation).finally(() => {
      if (shutdownOperation === tracked) shutdownOperation = null;
      if (shutdownResult) clearControllerExitObserver();
    });
    shutdownOperation = tracked;
    return tracked;
  }

  function closeCleanly() {
    if (shutdownResult) return Promise.resolve(shutdownResult);
    if (shutdownOperation) return shutdownOperation;
    return trackShutdown(serviceMode ? performServiceShutdown() : performCleanShutdown());
  }

  function enqueueSerialized(operation) {
    const task = commandTail.then(operation);
    commandTail = task.catch(() => {});
    return task;
  }

  function queueCommand(line) {
    return enqueueSerialized(() => handleCommand(line));
  }

  async function reportEOFShutdownFailure(error) {
    const code = typeof error?.code === "string" && /^[A-Za-z0-9_:-]{1,64}$/.test(error.code)
      ? error.code : "detach_unconfirmed";
    printLine(errorOutput, { kind: "shutdown_unconfirmed", detachedConfirmed: false, code });
    removeSignalHandlers();
    if (controlService) {
      try { await controlService.stop(); } catch { /* do not leave the opt-in adapter endpoint open */ }
    }
    try { await host.stop(); } catch { /* EOF must not leave a control listener running */ }
    try { await broker.stop({ confirmDetach: false }); } catch { /* preserve the explicit unconfirmed result */ }
    closed = true;
    shutdownResult = { stopped: true, detachedConfirmed: false };
    return shutdownResult;
  }

  function closeFromEOF() {
    if (shutdownResult) return Promise.resolve(shutdownResult);
    const previous = shutdownOperation;
    if (previous) {
      return trackShutdown((async () => {
        try { return await previous; }
        catch (error) { return reportEOFShutdownFailure(error); }
      })());
    }
    return trackShutdown((async () => {
      try { return await performCleanShutdown(); }
      catch (error) { return reportEOFShutdownFailure(error); }
    })());
  }

  async function handleCommand(line) {
    const { command, argument } = parseCommandLine(line);
    if (!command) return;
    try {
      if (command === "status") {
        try { await broker.statusRequest(); } catch { /* show current redacted status with a stale/failed broker state */ }
        await host.checkHealth();
        printLine(output, { command, status: safeStatus(broker, host, bridge) });
      } else if (command === "adapter-pause" || command === "adapter-resume") {
        if (!serviceMode || !controlService || argument) throw codedError("invalid_adapter_availability");
        if (command === "adapter-resume") {
          await broker.statusRequest();
          await host.checkHealth();
          const current = safeStatus(broker, host, bridge);
          if (current.broker.connected !== true || current.broker.authenticated !== true ||
              current.agent.listenerRegistered !== true || current.host.available !== true ||
              current.bridge.attached !== true || current.bridge.providerInvalidated === true ||
              current.bridge.controllerInvalidated === true || current.bridge.hostInvalidated === true ||
              current.bridge.closing === true || current.bridge.stopped === true) {
            throw codedError("adapter_recovery_native_unavailable");
          }
        }
        const result = await controlService.setAdapterAvailable(command === "adapter-resume");
        printLine(output, { command, result });
      } else if (command === "attach") {
        const result = await bridge.attach();
        printLine(output, { command, result: { attached: result.attached === true, status: safeStatus(broker, host, bridge) } });
      } else if (command === "identity") {
        const [inputPath, outputPathText] = parseIdentityFileArguments(argument);
        const outputPath = path.resolve(outputPathText);
        if (path.resolve(inputPath).toLowerCase() === outputPath.toLowerCase()) throw codedError("identity_output_matches_input");
        if (fs.existsSync(outputPath)) throw codedError("identity_output_exists");
        let identity = readPrivateOneBotIdentityFile(inputPath);
        let accountPath = null;
        try {
          if (controlPresent && identity.selfUin !== options.accountUin) throw codedError("configured_account_mismatch");
          accountPath = await broker.getAccountPath(identity.selfUin);
          if (typeof accountPath !== "string" || !accountPath || accountPath.length > 32768 || accountPath.includes("\0")) {
            throw codedError("account_path_unavailable");
          }
          writePrivateJson(outputPath, { selfUid: identity.selfUid, selfUin: identity.selfUin, accountPath });
          printLine(output, { command, result: { written: true } });
        } finally {
          identity = null;
          accountPath = null;
        }
      } else if (command === "login") {
        if (!argument) throw codedError("identity_file_required");
        let identity = readIdentityFile(argument);
        try {
          if (controlPresent && identity.selfUin !== options.accountUin) throw codedError("configured_account_mismatch");
          const result = await bridge.login(identity);
          printLine(output, { command, result: { posted: result.posted === true, id: result.id, loginState: result.loginState } });
        } finally {
          identity = null;
        }
      } else if (command === "accept") {
        if (!argument || argument.length > 128) throw codedError("call_id_required");
        const result = await bridge.accept(argument);
        printLine(output, { command, result: { posted: result.posted === true, id: result.id, callId: result.callId, callGeneration: result.callGeneration } });
      } else if (command === "quit") {
        const result = await closeCleanly();
        printLine(output, { command, result });
      } else {
        printLine(output, { command: "unknown", code: "unknown_command", accepted: ["status", "attach", "login", "accept", "quit"] });
      }
    } catch (error) {
      const code = typeof error?.code === "string" && /^[A-Za-z0-9_:-]{1,64}$/.test(error.code)
        ? error.code : "command_failed";
      printLine(output, { command, code });
    }
  }

  async function cleanupBeforeReturningError() {
    if (controlService) {
      try { await controlService.stop(); } catch { /* preserve startup failure classification */ }
    }
    try { await host.stop(); } catch { /* preserve startup failure classification */ }
    if (brokerStarted) {
      try { await broker.stop({ confirmDetach: false }); } catch { /* no attach occurred */ }
    }
    autoBootstrapUnsubscribe?.();
    autoBootstrapUnsubscribe = null;
  }

  try {
    autoBootstrapUnsubscribe = autoBootstrap ? broker.subscribe(onBrokerEvent) : null;
    const { brokerStatus: started, hostStatus } = await startTransportBeforeHost({
      broker, bridge, host, configPath,
      onBrokerStarted() { brokerStarted = true; },
    });
    hostStarted = true;
    const controlStatus = controlService ? await controlService.start() : null;
    const controlServiceInstance = controlService?.status()?.serviceInstance;
    interfaceHandle = readline.createInterface({ input, crlfDelay: Infinity });
    interfaceHandle.on("line", (line) => {
      queueCommand(line).catch(() => {});
    });
    const onSignal = () => {
      queueCommand("quit").catch(() => {});
    };
    signalHandlers = { onSignal };
    process.once("SIGINT", onSignal);
    process.once("SIGTERM", onSignal);
    interfaceHandle.once("close", () => {
      if (serviceMode || closed || eofHandling) return;
      eofHandling = true;
      commandTail = commandTail.then(async () => {
        const result = await closeFromEOF();
        if (result.detachedConfirmed) printLine(output, { kind: "stdin_eof_shutdown", result });
      }).catch(() => {});
    });
    printLine(output, {
      kind: "bridge_ready",
      targetPid: options.targetPid,
      connectionGeneration: options.connectionGeneration,
      hostGeneration: options.hostGeneration,
      ...(serviceMode ? { serviceMode: true } : {}),
      ...(autoBootstrap ? { autoBootstrap: true } : {}),
      brokerReady: true,
      hostAvailable: hostStatus.available,
      hostPort: options.hostPort,
      outputPort: hostStatus.outputPort,
      ...(controlStatus ? { controlPort: controlStatus.port } : {}),
      ...(controlStatus && typeof controlServiceInstance === "string" ? { controlServiceInstance } : {}),
      ...(controlStatus && audioRouteObserver ? { audioRouteProofEnabled: true } : {}),
      configWritten: true,
      ...(!serviceMode ? { configPath } : {}),
      pipeConfigured: Boolean(started.pipeName),
      commands: ["status", "attach", "identity <privateOneBotIdentityFile> <outputPrivateLoginFile>", "login <privateIdentityFile>", "accept <opaqueCallId>", "quit"],
    });
    runtimeReady = true;
    maybeStartAutoBootstrap();
    return {
      broker,
      host,
      bridge,
      controlService,
      interface: interfaceHandle,
      close: closeCleanly,
      handleCommand,
      serviceMode,
      autoBootstrap,
      get commandTail() { return commandTail; },
    };
  } catch (error) {
    if (hostStarted || !closed) await cleanupBeforeReturningError();
    clearControllerExitObserver();
    throw error;
  }
}

if (require.main === module) {
  let options;
  try { options = parseArgs(process.argv.slice(2)); }
  catch (error) {
    process.stderr.write(`QQCall Bridge CLI failed: ${error.code || "invalid_cli_arguments"}\n`);
    process.exitCode = 2;
  }
  if (options?.help) printUsage();
  else if (options) {
    void startCli(options).catch((error) => {
      const code = typeof error?.code === "string" && /^[A-Za-z0-9_:-]{1,64}$/.test(error.code)
        ? error.code : "startup_failed";
      process.stderr.write(`QQCall Bridge CLI failed: ${code}\n`);
      process.exitCode = 1;
    });
  }
}

module.exports = {
  MODULE_PATH,
  parseArgs,
  parseCommandLine,
  parseIdentityFileArguments,
  readPrivateToken,
  readPrivateTokenDetails,
  buildAudioRouteObserver,
  buildAudioRouteSetup,
  readIdentityFile,
  readPrivateOneBotIdentityFile,
  readPrivateOneBotIdentitySnapshot,
  readKernelPeerIdentitiesFile,
  safeStatus,
  startRendererCapabilityProbe,
  startAsarSourceProbe,
  validateAsarSourceProbeOptions,
  summarizeAsarSourceProbe,
  startAsarCandidateProbe,
  validateAsarCandidateProbeOptions,
  summarizeAsarCandidateProbe,
  startOfficialWindowProbe,
  validateOfficialWindowProbeOptions,
  summarizeOfficialWindowProbe,
  startRendererScriptMetadataProbe,
  validateRendererScriptMetadataProbeOptions,
  summarizeRendererScriptMetadataProbe,
  startOfficialAVObserver,
  validateOfficialAVObserverOptions,
  summarizeOfficialAVObserver,
  runRendererAdmissionProcess,
  validateRendererAdmissionResult,
  startTransportBeforeHost,
  startCli,
};
