"use strict";

const fs = require("node:fs");
const path = require("node:path");

const PLAN_ENV = "QQCALL_HOST_LAUNCH_PLAN";
const MAIN_ENV = "QQCALL_HOST_MAIN";

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

function readBoundedRegularFile(filePath, maxBytes) {
  let descriptor;
  try {
    const constants = fs.constants;
    let flags = constants.O_RDONLY;
    if (Number.isInteger(constants.O_NOFOLLOW)) flags |= constants.O_NOFOLLOW;
    if (Number.isInteger(constants.O_NONBLOCK)) flags |= constants.O_NONBLOCK;
    descriptor = fs.openSync(filePath, flags);
    const before = fs.fstatSync(descriptor);
    if (!before.isFile() || !Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes ||
        !sameOpenedPath(before, fs.lstatSync(filePath))) throw new Error("plan_file_invalid");
    const chunks = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes + 1));
    let offset = 0;
    while (offset <= maxBytes) {
      const requested = Math.min(buffer.length, maxBytes + 1 - offset);
      if (requested <= 0) break;
      const count = fs.readSync(descriptor, buffer, 0, requested, offset);
      if (count === 0) break;
      offset += count;
      if (offset > maxBytes) throw new Error("plan_file_invalid");
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    const after = fs.fstatSync(descriptor);
    if (!sameOpenedState(before, after) || !sameOpenedPath(after, fs.lstatSync(filePath))) {
      throw new Error("plan_file_invalid");
    }
    return Buffer.concat(chunks, offset);
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* best-effort status probe cleanup */ }
    }
  }
}

function safeCode(error) {
  return typeof error?.code === "string" && /^[a-z][a-z0-9_:-]{1,63}$/.test(error.code)
    ? error.code : "early_host_load_failed";
}

function readOwnedPlanLocation() {
  const filename = process.env[PLAN_ENV];
  if (typeof filename !== "string" || !path.isAbsolute(filename)) return null;
  const planPath = path.resolve(filename);
  const expected = path.resolve(__dirname, "..", "launch-plan.json");
  if (planPath.toLowerCase() !== expected.toLowerCase()) return null;
  try {
    const plan = JSON.parse(readBoundedRegularFile(planPath, 64 * 1024).toString("utf8"));
    const generation = plan && Number.isSafeInteger(plan.hostGeneration) && plan.hostGeneration > 0
      ? plan.hostGeneration : null;
    return { runRoot: path.dirname(planPath), generation };
  } catch { return null; }
}

function writeStatus(location, phase, code = null) {
  if (!location) return;
  const filename = path.join(location.runRoot, "earlyloader.status.json");
  const temp = `${filename}.${process.pid}.tmp`;
  const status = {
    schema: 1,
    phase,
    processPid: process.pid,
    hostGeneration: location.generation,
    lastFailureCode: code,
  };
  try {
    fs.writeFileSync(temp, `${JSON.stringify(status)}\n`, { encoding: "utf8", mode: 0o600 });
    fs.renameSync(temp, filename);
  } catch {
    try { fs.rmSync(temp, { force: true }); } catch { /* best-effort diagnostic only */ }
  }
}

function loadEarlyHost() {
  const location = readOwnedPlanLocation();
  writeStatus(location, "loader_loaded");
  const mainPath = process.env[MAIN_ENV];
  if (typeof mainPath !== "string" || !path.isAbsolute(mainPath)) {
    const error = new Error("early_main_path_invalid");
    error.code = "early_main_path_invalid";
    throw error;
  }
  const entry = require(mainPath);
  if (!entry || typeof entry.launchFromEnvironment !== "function") {
    const error = new Error("early_main_export_missing");
    error.code = "early_main_export_missing";
    throw error;
  }
  writeStatus(location, "host_main_starting");
  return entry.launchFromEnvironment();
}

const ownedLocation = readOwnedPlanLocation();
try {
  loadEarlyHost();
  writeStatus(ownedLocation, "host_main_loaded");
} catch (error) {
  const code = safeCode(error);
  writeStatus(ownedLocation, "failed", code);
  console.error(`QQCALL_EARLY_HOST_${code}`);
  process.exitCode = 1;
}

module.exports = { loadEarlyHost, readOwnedPlanLocation, safeCode, writeStatus };
