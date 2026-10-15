"use strict";

const { createElectronHost, configureHost } = require("./electron-host.cjs");
const {
  codedError,
  equalToken,
  readJsonFile,
  samePath,
  validateBootstrapConfig,
  validateHostConfig,
} = require("./config.cjs");
const { createPrivateEndTraceWriter, writeHostCrash, writeHostStatus } = require("./status-report.cjs");

const STATE_KEY = Symbol.for("nachobot.qqcall.avsdk-host.v1");

function sameRun(left, right) {
  return left.targetPid === right.targetPid &&
    left.hostGeneration === right.hostGeneration &&
    left.connectionGeneration === right.connectionGeneration &&
    left.pipeName === right.pipeName &&
    samePath(left.modulePath, right.modulePath) &&
    samePath(left.appPath, right.appPath) &&
    samePath(left.reportPath, right.reportPath) &&
    equalToken(left.ipcToken, right.ipcToken);
}

function configure(input) {
  const nativeConfig = validateBootstrapConfig(input || globalThis.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__);
  let hostConfig;
  try {
    const sidecarPath = `${nativeConfig.reportPath}.host.config.json`;
    const document = readJsonFile(sidecarPath, "host_config_read_failed");
    hostConfig = validateHostConfig(document, nativeConfig, { expectedPid: process.pid });
  } catch (error) {
    const code = safeCode(error, "host_config_invalid");
    writeLifecycleStatus(nativeConfig, "failed", null, code);
    throw codedError(code);
  }
  writeLifecycleStatus(nativeConfig, "initializing");
  let electron;
  try { electron = require("electron"); }
  catch {
    writeLifecycleStatus(nativeConfig, "failed", null, "electron_runtime_unavailable");
    throw codedError("electron_runtime_unavailable");
  }
  try { return configureHost({ app: electron.app, config: hostConfig }); }
  catch (error) {
    const code = safeCode(error);
    writeLifecycleStatus(nativeConfig, failurePhase(code), null, code);
    throw codedError(code);
  }
}

function safeCode(error, fallback = "host_startup_failed") {
  return typeof error?.code === "string" && /^[a-z][a-z0-9_]{1,63}$/.test(error.code)
    ? error.code
    : fallback;
}

function failurePhase(code) {
  if (code === "renderer_crashed" || code === "renderer_closed") return "renderer_failure";
  if (code.startsWith("output_") || code === "invalid_plugin_output" || code === "renderer_output_send_failed") return "output_failure";
  return "failed";
}

function writeLifecycleStatus(nativeConfig, phase, runtimeStatus = null, lastFailureCode = null) {
  return writeHostStatus(nativeConfig.reportPath, {
    phase,
    hostGeneration: nativeConfig.connectionGeneration,
    targetPid: process.pid,
    lastFailureCode: lastFailureCode || runtimeStatus?.lastFailureCode || null,
    ready: runtimeStatus?.ready === true,
    listening: runtimeStatus?.listening === true,
    stopped: runtimeStatus?.stopped === true,
  });
}

async function bootstrap(input) {
  const nativeConfig = validateBootstrapConfig(input || globalThis.__NACHOBOT_QQCALL_BOOTSTRAP_CONFIG__);
  let hostConfig;
  try {
    const sidecarPath = `${nativeConfig.reportPath}.host.config.json`;
    const document = readJsonFile(sidecarPath, "host_config_read_failed");
    hostConfig = validateHostConfig(document, nativeConfig, { expectedPid: process.pid });
  } catch (error) {
    const code = safeCode(error, "host_config_invalid");
    writeLifecycleStatus(nativeConfig, "failed", null, code);
    throw codedError(code);
  }
  const active = globalThis[STATE_KEY];
  if (active?.active) {
    if (!sameRun(active.hostConfig, hostConfig)) throw codedError("duplicate_host_bootstrap_active");
    if (active.initializing) throw codedError("host_bootstrap_initializing");
    if (active.controller?.status().stopped) throw codedError("host_already_stopped");
    return active.controller;
  }

  const registry = {
    active: true,
    initializing: true,
    nativeConfig,
    hostConfig,
    controller: null,
  };
  globalThis[STATE_KEY] = registry;
  writeLifecycleStatus(nativeConfig, "initializing");
  let runtime = null;
  const onPrivateEndTrace = createPrivateEndTraceWriter(nativeConfig.reportPath);
  try {
    let electron;
    try { electron = require("electron"); }
    catch { throw codedError("electron_runtime_unavailable"); }
    runtime = createElectronHost({
      electron,
      config: hostConfig,
      onFailure({ code }) {
        writeLifecycleStatus(nativeConfig, failurePhase(code), runtime?.status(), code);
      },
      onStatus({ phase, status }) {
        writeLifecycleStatus(nativeConfig, phase, status);
      },
      onRendererGone(record) {
        writeHostCrash(nativeConfig.reportPath, record);
      },
      onPrivateEndTrace,
    });
    await runtime.start();
    const controller = Object.freeze({
      status: runtime.status,
      publishOutput: runtime.publishOutput,
      async stop() {
        const result = await runtime.stop();
        registry.active = false;
        if (globalThis[STATE_KEY] === registry) delete globalThis[STATE_KEY];
        return result;
      },
    });
    registry.initializing = false;
    registry.controller = controller;
    return controller;
  } catch (error) {
    const code = safeCode(error);
    if (runtime) {
      try { await runtime.stop(); } catch { /* Best effort cleanup keeps the surfaced error typed. */ }
    }
    registry.active = false;
    if (globalThis[STATE_KEY] === registry) delete globalThis[STATE_KEY];
    writeLifecycleStatus(nativeConfig, "failed", runtime?.status(), code);
    throw codedError(code);
  }
}

module.exports = { bootstrap, configure };
