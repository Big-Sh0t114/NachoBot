"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { isAbsolute, relative, resolve, sep } = path;

const LOOPBACK = "127.0.0.1";
const HOST_CONFIG_SCHEMA = 1;

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function positiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function validToken(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{43,128}$/.test(value);
}

function equalToken(left, right) {
  if (!validToken(left) || !validToken(right)) return false;
  const a = Buffer.from(left, "ascii");
  const b = Buffer.from(right, "ascii");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function absolutePath(value, code) {
  if (typeof value !== "string" || !isAbsolute(value)) throw codedError(code);
  return resolve(value);
}

function samePath(left, right, platform = process.platform) {
  const a = resolve(left);
  const b = resolve(right);
  return platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function isWithinPath(candidate, root, platform = process.platform) {
  const left = resolve(root);
  const right = resolve(candidate);
  const rel = relative(left, right);
  const normalized = platform === "win32" ? rel.toLowerCase() : rel;
  return normalized !== "" && normalized !== ".." && !normalized.startsWith(`..${sep}`) && !isAbsolute(rel);
}

function resolvePluginPath(appPath, platform = process.platform) {
  const resolvedAppPath = absolutePath(appPath, "app_path_invalid");
  let realAppPath;
  try {
    const stats = fs.statSync(resolvedAppPath);
    if (!stats.isDirectory()) throw codedError("app_path_invalid");
    realAppPath = fs.realpathSync(resolvedAppPath);
  } catch (error) {
    if (error?.code === "app_path_invalid") throw error;
    throw codedError("app_path_missing");
  }

  const filename = platform === "win32" ? "AVSDKPlugin.dll" :
    platform === "linux" ? "libAVSDKPlugin.so" : null;
  if (!filename) throw codedError("platform_unsupported");

  const lexicalPath = path.join(realAppPath, "avsdk", filename);
  let resolvedPlugin;
  try {
    const stats = fs.statSync(lexicalPath);
    if (!stats.isFile()) throw codedError("avsdk_plugin_missing");
    resolvedPlugin = fs.realpathSync(lexicalPath);
  } catch (error) {
    if (error?.code === "avsdk_plugin_missing") throw error;
    throw codedError("avsdk_plugin_missing");
  }
  if (!isWithinPath(resolvedPlugin, realAppPath, platform)) throw codedError("avsdk_plugin_outside_app_path");
  return Object.freeze({ appPath: realAppPath, pluginPath: resolvedPlugin });
}

function validateBootstrapConfig(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw codedError("bootstrap_config_missing");
  const config = {
    modulePath: absolutePath(input.modulePath, "bootstrap_module_path_invalid"),
    appPath: absolutePath(input.appPath, "app_path_invalid"),
    reportPath: absolutePath(input.reportPath, "report_path_invalid"),
    pipeName: input.pipeName,
    sessionName: input.sessionName,
    connectionGeneration: input.connectionGeneration,
    ipcToken: input.ipcToken,
    attach: input.attach,
  };
  const pipePrefix = "\\\\.\\pipe\\nachobot-qqcall-";
  if (typeof config.pipeName !== "string" || !config.pipeName.startsWith(pipePrefix) ||
      !/^\d+-[a-f0-9]{16,64}$/i.test(config.pipeName.slice(pipePrefix.length))) {
    throw codedError("pipe_name_invalid");
  }
  if (typeof config.sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(config.sessionName)) {
    throw codedError("session_name_invalid");
  }
  if (!positiveSafeInteger(config.connectionGeneration)) throw codedError("generation_invalid");
  if (!validToken(config.ipcToken)) throw codedError("auth_token_invalid");
  if (config.attach !== false) throw codedError("explicit_attach_required");
  return Object.freeze(config);
}

function validateHostConfig(input, bootstrapConfig, { platform = process.platform, expectedPid = process.pid } = {}) {
  const native = validateBootstrapConfig(bootstrapConfig);
  if (!input || typeof input !== "object" || Array.isArray(input)) throw codedError("host_config_invalid");
  if (input.schema !== HOST_CONFIG_SCHEMA) throw codedError("host_config_schema_unsupported");
  if (!positiveSafeInteger(input.targetPid) || input.targetPid !== expectedPid) throw codedError("host_pid_mismatch");
  if (!samePath(input.modulePath, native.modulePath, platform) || !samePath(native.modulePath, __dirname + path.sep + "bootstrap.cjs", platform)) {
    throw codedError("bootstrap_module_mismatch");
  }
  if (!samePath(input.appPath, native.appPath, platform)) throw codedError("app_path_mismatch");
  if (!samePath(input.reportPath, native.reportPath, platform)) throw codedError("report_path_mismatch");
  if (input.pipeName !== native.pipeName || input.sessionName !== native.sessionName) throw codedError("bootstrap_config_mismatch");
  if (input.connectionGeneration !== native.connectionGeneration ||
      input.hostGeneration !== native.connectionGeneration) throw codedError("generation_mismatch");
  if (!equalToken(input.ipcToken, native.ipcToken)) throw codedError("auth_token_mismatch");
  if (input.listenHost !== LOOPBACK || input.outputHost !== LOOPBACK) throw codedError("loopback_only");
  if (!Number.isInteger(input.listenPort) || input.listenPort < 1 || input.listenPort > 65535) throw codedError("listen_port_invalid");
  if (!Number.isInteger(input.outputPort) || input.outputPort < 1 || input.outputPort > 65535) throw codedError("output_port_invalid");
  if (input.listenPort === input.outputPort) throw codedError("ports_must_differ");
  const profilePath = absolutePath(input.profilePath, "profile_path_invalid");
  let resolved;
  try {
    const stat = fs.statSync(profilePath);
    if (!stat.isDirectory()) throw codedError("profile_path_invalid");
    resolved = fs.realpathSync(profilePath);
  } catch (error) {
    if (error?.code === "profile_path_invalid") throw error;
    throw codedError("profile_path_missing");
  }
  const plugin = resolvePluginPath(native.appPath, platform);
  return Object.freeze({
    schema: HOST_CONFIG_SCHEMA,
    targetPid: input.targetPid,
    modulePath: native.modulePath,
    appPath: plugin.appPath,
    reportPath: native.reportPath,
    pipeName: native.pipeName,
    sessionName: native.sessionName,
    connectionGeneration: native.connectionGeneration,
    hostGeneration: input.hostGeneration,
    ipcToken: native.ipcToken,
    listenHost: LOOPBACK,
    listenPort: input.listenPort,
    outputHost: LOOPBACK,
    outputPort: input.outputPort,
    profilePath: resolved,
    pluginPath: plugin.pluginPath,
  });
}

function buildHostConfigs({
  targetPid,
  appPath,
  reportPath,
  profilePath,
  listenPort,
  outputPort,
  hostGeneration,
  sessionName = "qqcall_av_host",
  modulePath = path.resolve(__dirname, "bootstrap.cjs"),
  platform = process.platform,
  token = crypto.randomBytes(32).toString("base64url"),
  pipeNonce = crypto.randomBytes(16).toString("hex"),
} = {}) {
  if (!positiveSafeInteger(targetPid)) throw codedError("host_pid_invalid");
  if (!positiveSafeInteger(hostGeneration)) throw codedError("host_generation_invalid");
  if (!validToken(token)) throw codedError("auth_token_invalid");
  if (typeof pipeNonce !== "string" || !/^[a-f0-9]{16,64}$/i.test(pipeNonce)) throw codedError("pipe_nonce_invalid");
  if (typeof sessionName !== "string" || !/^[A-Za-z0-9_.-]{1,80}$/.test(sessionName)) throw codedError("session_name_invalid");
  if (!Number.isInteger(listenPort) || listenPort < 1 || listenPort > 65535) throw codedError("listen_port_invalid");
  if (!Number.isInteger(outputPort) || outputPort < 1 || outputPort > 65535) throw codedError("output_port_invalid");
  if (listenPort === outputPort) throw codedError("ports_must_differ");

  const resolvedPaths = resolvePluginPath(appPath, platform);
  const report = absolutePath(reportPath, "report_path_invalid");
  const resolvedProfilePath = absolutePath(profilePath, "profile_path_invalid");
  const resolvedModulePath = absolutePath(modulePath, "bootstrap_module_path_invalid");
  const nativeConfig = Object.freeze({
    modulePath: resolvedModulePath,
    pipeName: `\\\\.\\pipe\\nachobot-qqcall-${targetPid}-${pipeNonce}`,
    appPath: resolvedPaths.appPath,
    sessionName,
    connectionGeneration: hostGeneration,
    ipcToken: token,
    reportPath: report,
    attach: false,
  });
  const hostConfig = Object.freeze({
    schema: HOST_CONFIG_SCHEMA,
    targetPid,
    ...nativeConfig,
    hostGeneration,
    listenHost: LOOPBACK,
    listenPort,
    outputHost: LOOPBACK,
    outputPort,
    profilePath: resolvedProfilePath,
  });

  // Resolve all on-disk inputs while building so a bad manual selection fails before
  // either private config file is written.
  const validated = validateHostConfig(hostConfig, nativeConfig, { platform, expectedPid: targetPid });
  return Object.freeze({ nativeConfig, hostConfig: validated });
}

function readJsonFile(filePath, code = "host_config_read_failed") {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    throw codedError(code);
  }
}

function writePrivateJson(filePath, value) {
  const target = absolutePath(filePath, "config_output_path_invalid");
  const directory = path.dirname(target);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    fs.renameSync(temporary, target);
    try { fs.chmodSync(target, 0o600); } catch { /* Windows ACLs may not map POSIX mode bits. */ }
  } catch {
    try { fs.rmSync(temporary, { force: true }); } catch { /* best effort */ }
    throw codedError("config_write_failed");
  }
  return target;
}

module.exports = {
  HOST_CONFIG_SCHEMA,
  LOOPBACK,
  buildHostConfigs,
  codedError,
  equalToken,
  positiveSafeInteger,
  readJsonFile,
  resolvePluginPath,
  samePath,
  validateBootstrapConfig,
  validateHostConfig,
  validToken,
  writePrivateJson,
};
