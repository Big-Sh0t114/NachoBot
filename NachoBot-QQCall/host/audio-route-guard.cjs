"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { codedError } = require("./config.cjs");
const GUARD_ENV = "QQCALL_AUDIO_ROUTE_GUARD_CONFIG";
const KEYS = ["version", "qqBuild", "hostGeneration", "nodeArtifact", "nodeSha256", "guardDllPath", "dllSha256", "captureEndpointId", "renderEndpointId"];

function validateGuardConfig(input, generation) {
  if (!input || typeof input !== "object" || Array.isArray(input) ||
      Object.keys(input).length !== KEYS.length || KEYS.some((key) => !Object.hasOwn(input, key)) ||
      input.version !== 1 || input.qqBuild !== "9.9.26-44343" ||
      !Number.isSafeInteger(generation) || generation <= 0 || input.hostGeneration !== generation) {
    throw codedError("audio_guard_config_invalid");
  }
  for (const field of ["captureEndpointId", "renderEndpointId"]) {
    if (typeof input[field] !== "string" || !input[field] || input[field].length >= 1024 || input[field].includes("\0")) throw codedError("audio_guard_endpoint_invalid");
  }
  if (input.captureEndpointId === input.renderEndpointId) throw codedError("audio_guard_endpoint_invalid");
  for (const field of ["nodeArtifact", "guardDllPath"]) {
    if (typeof input[field] !== "string" || !path.isAbsolute(input[field]) || input[field].includes("\0") || input[field].length >= 1024) throw codedError("audio_guard_artifact_invalid");
  }
  if (path.basename(input.nodeArtifact) !== "QQCallAudioRouteGuard-v2.node" || path.basename(input.guardDllPath) !== "QQCallAudioRouteGuard-v2.dll") throw codedError("audio_guard_artifact_invalid");
  for (const field of ["nodeSha256", "dllSha256"]) if (typeof input[field] !== "string" || !/^[a-f0-9]{64}$/.test(input[field])) throw codedError("audio_guard_digest_invalid");
  return Object.freeze({ ...input });
}

function checkedArtifact(filename, digest) {
  const descriptor = fs.openSync(filename, "r");
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || stat.size < 1 || stat.size > 32 * 1024 * 1024 ||
        crypto.createHash("sha256").update(fs.readFileSync(descriptor)).digest("hex") !== digest) throw codedError("audio_guard_artifact_mismatch");
  } finally { fs.closeSync(descriptor); }
}

function installAudioRouteGuard({ env = process.env, generation, platform = process.platform, execPath = process.execPath, loadNative = require } = {}) {
  const filename = env[GUARD_ENV];
  if (filename === undefined || filename === "") return null;
  if (platform !== "win32" || typeof filename !== "string" || !path.isAbsolute(filename)) throw codedError("audio_guard_platform_or_path_invalid");
  let raw;
  try {
    const stat = fs.statSync(filename);
    if (!stat.isFile() || stat.size > 16 * 1024) throw new Error();
    raw = JSON.parse(fs.readFileSync(filename, "utf8"));
  } catch { throw codedError("audio_guard_config_unavailable"); }
  const config = validateGuardConfig(raw, generation);
  checkedArtifact(config.nodeArtifact, config.nodeSha256);
  checkedArtifact(config.guardDllPath, config.dllSha256);
  const native = loadNative(config.nodeArtifact);
  if (typeof native?.install !== "function" || typeof native?.status !== "function") throw codedError("audio_guard_exports_invalid");
  const result = native.install({ qqImagePath: execPath, qqBuild: config.qqBuild,
    guardDllPath: config.guardDllPath, captureEndpointId: config.captureEndpointId,
    renderEndpointId: config.renderEndpointId, hostGeneration: generation });
  if (result?.installed !== true || result.failureCode !== 0 || result.generation !== generation) throw codedError("audio_guard_install_failed");
  return Object.freeze({ installed: true, generation });
}
module.exports = { GUARD_ENV, validateGuardConfig, installAudioRouteGuard };
