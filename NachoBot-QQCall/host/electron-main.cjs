"use strict";

const path = require("node:path");
const { createElectronHost } = require("./electron-host.cjs");
const { codedError, readJsonFile, validateHostConfig } = require("./config.cjs");
const { createPrivateEndTraceWriter, writeHostCrash, writeHostStatus } = require("./status-report.cjs");

function nativeConfigFromHostConfig(input) {
  return {
    modulePath: input?.modulePath,
    appPath: input?.appPath,
    reportPath: input?.reportPath,
    pipeName: input?.pipeName,
    sessionName: input?.sessionName,
    connectionGeneration: input?.connectionGeneration,
    ipcToken: input?.ipcToken,
    // The private Host sidecar is emitted only after the native builder validated
    // a non-attaching bootstrap config; preserve that fixed contract here.
    attach: false,
  };
}

function loadEnvironmentConfig(env = process.env) {
  const configPath = env.QQCALL_HOST_CONFIG;
  if (typeof configPath !== "string" || !path.isAbsolute(configPath)) throw codedError("host_config_path_missing");
  const raw = readJsonFile(configPath, "host_config_read_failed");
  return validateHostConfig(raw, nativeConfigFromHostConfig(raw), { expectedPid: process.pid });
}

function safeCode(error) {
  return typeof error?.code === "string" && /^[a-z][a-z0-9_]{1,63}$/.test(error.code)
    ? error.code
    : "host_startup_failed";
}

function failurePhase(code) {
  if (code === "renderer_crashed" || code === "renderer_closed") return "renderer_failure";
  if (code.startsWith("output_") || code === "invalid_plugin_output" || code === "renderer_output_send_failed") return "output_failure";
  return "failed";
}

function writeLifecycleStatus(config, phase, runtimeStatus = null, lastFailureCode = null) {
  return writeHostStatus(config.reportPath, {
    phase,
    hostGeneration: config.hostGeneration,
    targetPid: config.targetPid,
    lastFailureCode: lastFailureCode || runtimeStatus?.lastFailureCode || null,
    ready: runtimeStatus?.ready === true,
    listening: runtimeStatus?.listening === true,
    stopped: runtimeStatus?.stopped === true,
  });
}

function main(electron = require("electron"), env = process.env) {
  let config;
  try { config = loadEnvironmentConfig(env); }
  catch (error) {
    console.error(`QQCALL_HOST_STARTUP_${safeCode(error)}`);
    process.exitCode = 1;
    return null;
  }
  writeLifecycleStatus(config, "initializing");
  const onPrivateEndTrace = createPrivateEndTraceWriter(config.reportPath);
  let runtime;
  try {
    runtime = createElectronHost({
      electron,
      config,
      onFailure({ code }) {
        writeLifecycleStatus(config, failurePhase(code), runtime?.status(), code);
      },
      onStatus({ phase, status }) {
        writeLifecycleStatus(config, phase, status);
      },
      onRendererGone(record) {
        writeHostCrash(config.reportPath, record);
      },
      onPrivateEndTrace,
    });
  }
  catch (error) {
    const code = safeCode(error);
    writeLifecycleStatus(config, "failed", null, code);
    console.error(`QQCALL_HOST_STARTUP_${code}`);
    process.exitCode = 1;
    return null;
  }
  void runtime.start().catch((error) => {
    const code = safeCode(error);
    writeLifecycleStatus(config, "failed", runtime.status(), code);
    console.error(`QQCALL_HOST_STARTUP_${code}`);
    process.exitCode = 1;
  });
  return runtime;
}

if (require.main === module) main();

module.exports = { loadEnvironmentConfig, main, nativeConfigFromHostConfig };
