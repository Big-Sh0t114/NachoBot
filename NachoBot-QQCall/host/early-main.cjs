"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { buildHostConfigs, codedError, writePrivateJson } = require("./config.cjs");
const { main: startHostMain } = require("./electron-main.cjs");
const { GUARD_ENV, installAudioRouteGuard } = require("./audio-route-guard.cjs");

const PLAN_ENV = "QQCALL_HOST_LAUNCH_PLAN";
const STATE_KEY = Symbol.for("nachobot.qqcall.standalone-host-loader.v1");
const PLAN_KEYS = Object.freeze([
  "schema", "appPath", "runtimePath", "reportPath", "profilePath",
  "listenPort", "outputPort", "hostGeneration", "sessionName",
]);

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

function readBoundedRegularFile(filePath, maxBytes, invalidCode) {
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

function absolutePath(value, code) {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw codedError(code);
  return path.resolve(value);
}

function isWithin(candidate, root) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  const compared = process.platform === "win32" ? relative.toLowerCase() : relative;
  return compared !== "" && compared !== ".." && !compared.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function positivePort(value) {
  return Number.isInteger(value) && value > 0 && value <= 65535;
}

function validateLaunchPlan(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw codedError("launch_plan_invalid");
  const keys = Object.keys(input);
  if (keys.some((key) => !PLAN_KEYS.includes(key) && key !== "audioRouteGuardConfigPath") || PLAN_KEYS.some((key) => !Object.hasOwn(input, key))) {
    throw codedError("launch_plan_fields_invalid");
  }
  if (input.schema !== 1) throw codedError("launch_plan_schema_unsupported");
  const appPath = absolutePath(input.appPath, "app_path_invalid");
  const runtimePath = absolutePath(input.runtimePath, "runtime_path_invalid");
  const reportPath = absolutePath(input.reportPath, "report_path_invalid");
  const profilePath = absolutePath(input.profilePath, "profile_path_invalid");
  if (!isWithin(reportPath, runtimePath) || !isWithin(profilePath, runtimePath)) throw codedError("launch_plan_paths_outside_runtime");
  if (isWithin(runtimePath, appPath) || isWithin(appPath, runtimePath)) throw codedError("launch_runtime_overlaps_app_path");
  if (!positivePort(input.listenPort) || !positivePort(input.outputPort) || input.listenPort === input.outputPort) {
    throw codedError("launch_plan_ports_invalid");
  }
  if (!Number.isSafeInteger(input.hostGeneration) || input.hostGeneration <= 0) throw codedError("host_generation_invalid");
  if (typeof input.sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(input.sessionName)) {
    throw codedError("session_name_invalid");
  }
  const guardPath = Object.hasOwn(input, "audioRouteGuardConfigPath")
    ? absolutePath(input.audioRouteGuardConfigPath, "audio_guard_config_path_invalid") : null;
  if (guardPath && !isWithin(guardPath, runtimePath)) throw codedError("audio_guard_config_outside_runtime");
  return Object.freeze({
    schema: 1,
    appPath,
    runtimePath,
    reportPath,
    profilePath,
    listenPort: input.listenPort,
    outputPort: input.outputPort,
    hostGeneration: input.hostGeneration,
    sessionName: input.sessionName,
    ...(guardPath ? { audioRouteGuardConfigPath: guardPath } : {}),
  });
}

function readLaunchPlan(planPath) {
  const filename = absolutePath(planPath, "launch_plan_path_invalid");
  let source;
  try {
    source = readBoundedRegularFile(filename, 64 * 1024, "launch_plan_invalid").toString("utf8");
  } catch (error) {
    if (typeof error?.code === "string" && /^[a-z][a-z0-9_:-]{1,63}$/.test(error.code)) throw error;
    throw codedError("launch_plan_unavailable");
  }
  let value;
  try { value = JSON.parse(source); }
  catch { throw codedError("launch_plan_invalid"); }
  return validateLaunchPlan(value);
}

function prepareHostConfig(planInput, { pid = process.pid, platform = process.platform } = {}) {
  const plan = validateLaunchPlan(planInput);
  if (!Number.isSafeInteger(pid) || pid <= 0) throw codedError("host_pid_invalid");
  fs.mkdirSync(plan.profilePath, { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.dirname(plan.reportPath), { recursive: true, mode: 0o700 });
  const { nativeConfig, hostConfig } = buildHostConfigs({
    targetPid: pid,
    appPath: plan.appPath,
    reportPath: plan.reportPath,
    profilePath: plan.profilePath,
    listenPort: plan.listenPort,
    outputPort: plan.outputPort,
    hostGeneration: plan.hostGeneration,
    sessionName: plan.sessionName,
    modulePath: path.resolve(__dirname, "bootstrap.cjs"),
    platform,
  });
  const bootstrapConfigPath = `${plan.reportPath}.host.bootstrap.config.json`;
  const sidecarPath = `${plan.reportPath}.host.config.json`;
  writePrivateJson(bootstrapConfigPath, nativeConfig);
  writePrivateJson(sidecarPath, hostConfig);
  return Object.freeze({
    targetPid: pid,
    hostGeneration: hostConfig.hostGeneration,
    listenPort: hostConfig.listenPort,
    outputPort: hostConfig.outputPort,
    profilePath: hostConfig.profilePath,
    bootstrapConfigPath,
    sidecarPath,
  });
}

function launchFromEnvironment({ env = process.env, electron = null, hostMain = startHostMain } = {}) {
  const planPath = env[PLAN_ENV];
  const plan = readLaunchPlan(planPath);
  const registry = globalThis[STATE_KEY];
  if (registry?.active) {
    if (registry.planPath === path.resolve(planPath) && registry.hostGeneration === plan.hostGeneration) return registry.result;
    throw codedError("duplicate_standalone_host_loader");
  }
  const active = { active: true, planPath: path.resolve(planPath), hostGeneration: plan.hostGeneration, result: null };
  globalThis[STATE_KEY] = active;
  try {
    const guardEnv = plan.audioRouteGuardConfigPath ? { ...env, [GUARD_ENV]: plan.audioRouteGuardConfigPath } : env;
    const guard = installAudioRouteGuard({ env: guardEnv, generation: plan.hostGeneration });
    if (plan.audioRouteGuardConfigPath && !guard?.installed) throw codedError("audio_guard_install_failed");
    writePrivateJson(path.join(plan.runtimePath, "audio-guard.status.json"), {
      hostGeneration: plan.hostGeneration, targetPid: process.pid,
      configured: Boolean(plan.audioRouteGuardConfigPath || env[GUARD_ENV]), installed: guard?.installed === true,
    });
    const files = prepareHostConfig(plan);
    const nextEnv = { ...env, QQCALL_HOST_CONFIG: files.sidecarPath };
    active.result = hostMain(electron || require("electron"), nextEnv);
    if (!active.result) {
      active.active = false;
      if (globalThis[STATE_KEY] === active) delete globalThis[STATE_KEY];
      throw codedError("host_startup_failed");
    }
    return active.result;
  } catch (error) {
    active.active = false;
    if (globalThis[STATE_KEY] === active) delete globalThis[STATE_KEY];
    if (typeof error?.code === "string" && /^[a-z][a-z0-9_:-]{1,63}$/.test(error.code)) throw error;
    throw codedError("host_startup_failed");
  }
}

module.exports = { PLAN_ENV, STATE_KEY, isWithin, launchFromEnvironment, prepareHostConfig, readLaunchPlan, validateLaunchPlan };
