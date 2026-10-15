"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

test("standalone Host launcher dry-run builds an owned early-loader package and private profile without launching QQ", {
  skip: process.platform !== "win32" ? "PowerShell and the Windows development loader are required" : false,
}, (t) => {
  const projectRoot = path.resolve(__dirname, "..");
  const runtimeBase = path.join(projectRoot, ".runtime", `launcher-test-${process.pid}-${Date.now()}`);
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-launch-fixture-"));
  const shellRoot = path.join(fixtureRoot, "NapCat.Shell");
  const appPath = path.join(fixtureRoot, "resources", "app");
  const qqExe = path.join(fixtureRoot, "QQ.exe");
  fs.mkdirSync(shellRoot, { recursive: true });
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(shellRoot, "NapCatWinBootMain.exe"), "fixture launcher");
  fs.writeFileSync(path.join(shellRoot, "NapCatWinBootHook.dll"), "fixture hook");
  fs.writeFileSync(qqExe, "fixture QQ executable");
  fs.writeFileSync(path.join(appPath, "avsdk", "AVSDKPlugin.dll"), "fixture plugin");
  fs.writeFileSync(path.join(shellRoot, "qqnt.json"), JSON.stringify({
    name: "qq-chat",
    verHash: "2c9d3f6c",
    version: "9.9.22-40990",
    linuxVersion: "3.2.20-40990",
    linuxVerHash: "ec800879",
    private: true,
    description: "QQ",
    productName: "QQ",
    sideEffects: true,
    main: "./loadNapCat.js",
    buildVersion: "40990",
    isPureShell: true,
    isByteCodeShell: true,
    platform: "win32",
    eleArch: "x64",
  }));
  t.after(() => {
    fs.rmSync(runtimeBase, { recursive: true, force: true });
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  });

  const script = path.join(projectRoot, "scripts", "launch-host.ps1");
  const result = spawnSync("powershell.exe", [
    "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
    "-QQExePath", qqExe,
    "-AppPath", appPath,
    "-NapCatShellRoot", shellRoot,
    "-RuntimeDirectory", runtimeBase,
    "-ListenPort", "6111",
    "-OutputPort", "6110",
    "-HostGeneration", "73",
    "-DryRun",
  ], { encoding: "utf8", timeout: 15000, windowsHide: true });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 0, result.stderr);
  const response = JSON.parse(result.stdout.trim());
  assert.equal(response.ok, true);
  assert.equal(response.dryRun, true);
  assert.equal(response.processLaunched, false);
  assert.equal(response.hostGeneration, 73);
  assert.equal(response.listenPort, 6111);
  assert.equal(response.outputPort, 6110);
  assert.doesNotMatch(result.stdout + result.stderr, /ipcToken|accessToken|secret/i);

  const packageManifest = JSON.parse(fs.readFileSync(response.packagePath, "utf8"));
  assert.equal(packageManifest.name, "qq-chat");
  assert.equal(packageManifest.version, "9.9.22-40990");
  assert.equal(packageManifest.verHash, "2c9d3f6c");
  assert.equal(packageManifest.main, "./loadNapCat.js");
  assert.equal(path.basename(response.loaderPath), "earlyloader.js", "the Hook loads our script through NAPCAT_LOAD_PATH");
  assert.equal(fs.existsSync(response.profilePath), true);
  const plan = JSON.parse(fs.readFileSync(response.launchPlanPath, "utf8"));
  assert.deepEqual(Object.keys(plan).sort(), [
    "appPath", "hostGeneration", "listenPort", "outputPort", "profilePath", "reportPath", "runtimePath", "schema", "sessionName",
  ]);
  assert.equal(plan.hostGeneration, 73);
  assert.doesNotMatch(JSON.stringify(plan), /token|secret/i);
  assert.match(fs.readFileSync(response.loaderPath, "utf8"), /loadEarlyHost/);
});

test("copied early-loader records bounded startup and failure evidence in its owned run directory", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-early-loader-"));
  const runtime = path.join(root, "runtime");
  const packageRoot = path.join(runtime, "package");
  fs.mkdirSync(packageRoot, { recursive: true });
  const loaderPath = path.join(packageRoot, "earlyloader.cjs");
  fs.copyFileSync(path.resolve(__dirname, "../host/earlyloader.cjs"), loaderPath);
  const planPath = path.join(runtime, "launch-plan.json");
  fs.writeFileSync(planPath, JSON.stringify({ schema: 1, hostGeneration: 81 }));
  const successMain = path.join(runtime, "host-main.cjs");
  fs.writeFileSync(successMain, "module.exports = { launchFromEnvironment() { return { started: true }; } };\n");
  const failureMain = path.join(runtime, "host-main-fail.cjs");
  fs.writeFileSync(failureMain, "module.exports = { launchFromEnvironment() { const e = new Error('opaque'); e.code = 'host_main_test_failure'; throw e; } };\n");
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));

  const run = (main) => spawnSync(process.execPath, [loaderPath], {
    encoding: "utf8",
    timeout: 5000,
    windowsHide: true,
    env: { ...process.env, QQCALL_HOST_LAUNCH_PLAN: planPath, QQCALL_HOST_MAIN: main },
  });
  const success = run(successMain);
  assert.equal(success.status, 0, success.stderr);
  const statusPath = path.join(runtime, "earlyloader.status.json");
  const loaded = JSON.parse(fs.readFileSync(statusPath, "utf8"));
  assert.deepEqual(loaded, {
    schema: 1,
    phase: "host_main_loaded",
    processPid: loaded.processPid,
    hostGeneration: 81,
    lastFailureCode: null,
  });
  assert.equal(Number.isSafeInteger(loaded.processPid), true);

  const failure = run(failureMain);
  assert.equal(failure.status, 1);
  assert.match(failure.stderr, /QQCALL_EARLY_HOST_host_main_test_failure/);
  const failed = JSON.parse(fs.readFileSync(statusPath, "utf8"));
  assert.equal(failed.phase, "failed");
  assert.equal(failed.lastFailureCode, "host_main_test_failure");
  assert.equal(failed.hostGeneration, 81);
  assert.doesNotMatch(fs.readFileSync(statusPath, "utf8"), /opaque|token|secret|appPath/i);
});
