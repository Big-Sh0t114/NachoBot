"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  PLAN_ENV,
  STATE_KEY,
  launchFromEnvironment,
  prepareHostConfig,
  validateLaunchPlan,
} = require("../host/early-main.cjs");
const { nativeConfigFromHostConfig } = require("../host/electron-main.cjs");
const { validateHostConfig } = require("../host/config.cjs");

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-early-host-"));
  const appPath = path.join(root, "resources", "app");
  const runtimePath = path.join(root, "runtime");
  const profilePath = path.join(runtimePath, "profile");
  const reportPath = path.join(runtimePath, "host");
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(appPath, "avsdk", "AVSDKPlugin.dll"), "fixture-plugin");
  fs.mkdirSync(runtimePath, { recursive: true });
  const plan = {
    schema: 1,
    appPath,
    runtimePath,
    reportPath,
    profilePath,
    listenPort: 6111,
    outputPort: 6110,
    hostGeneration: 51,
    sessionName: "qqcall_av_host",
  };
  const planPath = path.join(runtimePath, "launch-plan.json");
  fs.writeFileSync(planPath, JSON.stringify(plan));
  t.after(() => {
    delete globalThis[STATE_KEY];
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, appPath, runtimePath, profilePath, reportPath, planPath, plan };
}

test("early Host launch plan is strict and generates private PID-bound config before main startup", (t) => {
  const value = fixture(t);
  assert.deepEqual(validateLaunchPlan(value.plan), value.plan);
  assert.throws(() => validateLaunchPlan({ ...value.plan, ipcToken: "must-not-be-supplied" }), { code: "launch_plan_fields_invalid" });
  assert.throws(() => validateLaunchPlan({ ...value.plan, profilePath: path.join(value.appPath, "profile") }), { code: "launch_plan_paths_outside_runtime" });
  assert.throws(() => validateLaunchPlan({ ...value.plan, audioRouteGuardConfigPath: path.join(value.appPath, "guard.json") }), { code: "audio_guard_config_outside_runtime" });

  const generated = prepareHostConfig(value.plan, { pid: 23456 });
  assert.equal(generated.targetPid, 23456);
  assert.equal(generated.hostGeneration, value.plan.hostGeneration);
  assert.equal(fs.existsSync(value.profilePath), true);
  const config = JSON.parse(fs.readFileSync(generated.sidecarPath, "utf8"));
  assert.equal(config.targetPid, 23456);
  assert.equal(config.appPath, fs.realpathSync(value.appPath));
  assert.equal(config.listenPort, 6111);
  assert.equal(config.outputPort, 6110);
  assert.equal(JSON.parse(fs.readFileSync(generated.bootstrapConfigPath, "utf8")).attach, false);
  assert.match(config.ipcToken, /^[A-Za-z0-9_-]{43,128}$/);
  assert.equal(validateHostConfig(config, nativeConfigFromHostConfig(config), { expectedPid: 23456 }).targetPid, 23456);
  assert.doesNotMatch(fs.readFileSync(value.planPath, "utf8"), /ipcToken|secret/i);
});

test("a required guard plan fails before Host main when its private configuration is unavailable", (t) => {
  const value = fixture(t);
  fs.writeFileSync(value.planPath, JSON.stringify({ ...value.plan,
    audioRouteGuardConfigPath: path.join(value.runtimePath, "missing-guard.json") }));
  let calls = 0;
  assert.throws(() => launchFromEnvironment({ env: { [PLAN_ENV]: value.planPath }, electron: {},
    hostMain() { calls++; return {}; } }), { code: "audio_guard_config_unavailable" });
  assert.equal(calls, 0);
  assert.equal(fs.existsSync(`${value.reportPath}.host.config.json`), false);
});

test("early loader supplies generated sidecar to Host main once and guards duplicate loads", (t) => {
  const value = fixture(t);
  let calls = 0;
  const env = { [PLAN_ENV]: value.planPath, preserved: "value" };
  const expectedResult = { hostStarted: true };
  const hostMain = (_electron, nextEnv) => {
    calls += 1;
    assert.equal(nextEnv.preserved, "value");
    assert.equal(path.isAbsolute(nextEnv.QQCALL_HOST_CONFIG), true);
    assert.equal(JSON.parse(fs.readFileSync(nextEnv.QQCALL_HOST_CONFIG, "utf8")).targetPid, process.pid);
    return expectedResult;
  };
  const first = launchFromEnvironment({ env, electron: {}, hostMain });
  const second = launchFromEnvironment({ env, electron: {}, hostMain });
  assert.strictEqual(first, expectedResult);
  assert.strictEqual(second, first);
  assert.equal(calls, 1);
});
