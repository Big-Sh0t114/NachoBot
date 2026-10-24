"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { codedError } = require("./config.cjs");
const GUARD_ENV = "QQCALL_AUDIO_ROUTE_GUARD_CONFIG";
const KEYS = ["version", "qqBuild", "hostGeneration", "nodeArtifact", "nodeSha256", "guardDllPath", "dllSha256", "captureEndpointId", "renderEndpointId"];

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

function readBoundedRegularFile(filePath, maxBytes) {
  let descriptor;
  try {
    descriptor = openReadOnlyNoFollow(filePath);
    const before = fs.fstatSync(descriptor);
    if (!before.isFile() || !Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes ||
        !sameOpenedPath(before, fs.lstatSync(filePath))) throw codedError("audio_guard_config_unavailable");
    const chunks = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes + 1));
    let offset = 0;
    while (offset <= maxBytes) {
      const requested = Math.min(buffer.length, maxBytes + 1 - offset);
      if (requested <= 0) break;
      const count = fs.readSync(descriptor, buffer, 0, requested, offset);
      if (count === 0) break;
      offset += count;
      if (offset > maxBytes) throw codedError("audio_guard_config_unavailable");
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    const after = fs.fstatSync(descriptor);
    if (!sameOpenedState(before, after) || !sameOpenedPath(after, fs.lstatSync(filePath))) {
      throw codedError("audio_guard_config_unavailable");
    }
    return Buffer.concat(chunks, offset);
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* preserve the original read result or failure */ }
    }
  }
}

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
  const descriptor = openReadOnlyNoFollow(filename);
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || !Number.isSafeInteger(stat.size) || stat.size < 1 || stat.size > 32 * 1024 * 1024 ||
        !sameOpenedPath(stat, fs.lstatSync(filename))) throw codedError("audio_guard_artifact_mismatch");
    const hash = crypto.createHash("sha256");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let total = 0;
    let count;
    while ((count = fs.readSync(descriptor, buffer, 0,
      Math.min(buffer.byteLength, 32 * 1024 * 1024 + 1 - total), total)) > 0) {
      total += count;
      if (total > 32 * 1024 * 1024) throw codedError("audio_guard_artifact_mismatch");
      hash.update(buffer.subarray(0, count));
    }
    const after = fs.fstatSync(descriptor);
    if (!sameOpenedState(stat, after) || !sameOpenedPath(after, fs.lstatSync(filename)) || hash.digest("hex") !== digest) {
      throw codedError("audio_guard_artifact_mismatch");
    }
  } finally { fs.closeSync(descriptor); }
}

function installAudioRouteGuard({ env = process.env, generation, platform = process.platform, execPath = process.execPath, loadNative = require } = {}) {
  const filename = env[GUARD_ENV];
  if (filename === undefined || filename === "") return null;
  if (platform !== "win32" || typeof filename !== "string" || !path.isAbsolute(filename)) throw codedError("audio_guard_platform_or_path_invalid");
  let raw;
  try {
    raw = JSON.parse(readBoundedRegularFile(filename, 16 * 1024).toString("utf8"));
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
