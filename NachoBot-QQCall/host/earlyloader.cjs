"use strict";

const fs = require("node:fs");
const path = require("node:path");

const PLAN_ENV = "QQCALL_HOST_LAUNCH_PLAN";
const MAIN_ENV = "QQCALL_HOST_MAIN";

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
    const stat = fs.statSync(planPath);
    if (!stat.isFile() || stat.size > 64 * 1024) return null;
    const plan = JSON.parse(fs.readFileSync(planPath, "utf8"));
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
