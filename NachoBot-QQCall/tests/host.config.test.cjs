"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildHostConfigs, validateHostConfig } = require("../host/config.cjs");
const { configureHost, pluginSwitchMatches } = require("../host/electron-host.cjs");
const { buildAndWrite } = require("../scripts/host-config.cjs");

const platform = process.platform;
const pluginFilename = platform === "win32" ? "AVSDKPlugin.dll" : platform === "linux" ? "libAVSDKPlugin.so" : null;
const TOKEN = Buffer.alloc(32, 0x73).toString("base64url");

function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-host-test-"));
  const appPath = path.join(root, "qq-resources-app");
  const profilePath = path.join(root, "host-profile");
  fs.mkdirSync(path.join(appPath, "avsdk"), { recursive: true });
  fs.writeFileSync(path.join(appPath, "avsdk", pluginFilename || "plugin.stub"), "mock plugin");
  fs.mkdirSync(profilePath, { recursive: true });
  return { root, appPath, profilePath, reportPath: path.join(root, "report", "host") };
}

function fixtureConfig(fixture, overrides = {}) {
  return buildHostConfigs({
    targetPid: process.pid,
    appPath: fixture.appPath,
    reportPath: fixture.reportPath,
    profilePath: fixture.profilePath,
    listenPort: 6111,
    outputPort: 6110,
    hostGeneration: 23,
    token: TOKEN,
    pipeNonce: "1234567890abcdef1234567890abcdef",
    platform,
    ...overrides,
  });
}

test("host config builder creates matching native and private sidecar contracts", (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const fixture = makeFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const { nativeConfig, hostConfig } = fixtureConfig(fixture);

  assert.equal(nativeConfig.modulePath, path.resolve(__dirname, "..", "host", "bootstrap.cjs"));
  assert.equal(nativeConfig.connectionGeneration, 23);
  assert.equal(nativeConfig.attach, false);
  assert.equal(hostConfig.hostGeneration, nativeConfig.connectionGeneration);
  assert.equal(hostConfig.connectionGeneration, nativeConfig.connectionGeneration);
  assert.equal(hostConfig.ipcToken, nativeConfig.ipcToken);
  assert.equal(hostConfig.listenHost, "127.0.0.1");
  assert.equal(hostConfig.outputHost, "127.0.0.1");
  assert.ok(hostConfig.pluginPath.endsWith(path.join("avsdk", pluginFilename)));
  assert.deepEqual(validateHostConfig(hostConfig, nativeConfig, { expectedPid: process.pid }), hostConfig);

  assert.throws(() => validateHostConfig({ ...hostConfig, hostGeneration: 24 }, nativeConfig, { expectedPid: process.pid }), { code: "generation_mismatch" });
  assert.throws(() => validateHostConfig({ ...hostConfig, ipcToken: Buffer.alloc(32, 0x74).toString("base64url") }, nativeConfig, { expectedPid: process.pid }), { code: "auth_token_mismatch" });
  assert.throws(() => validateHostConfig({ ...hostConfig, outputHost: "0.0.0.0" }, nativeConfig, { expectedPid: process.pid }), { code: "loopback_only" });
  assert.throws(() => validateHostConfig(hostConfig, nativeConfig, { expectedPid: process.pid + 1 }), { code: "host_pid_mismatch" });
});

test("PPAPI startup flags are installed early and late startup checks the exact plugin path", (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const fixture = makeFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const { hostConfig } = fixtureConfig(fixture);
  const switches = new Map();
  const paths = new Map();
  const earlyApp = {
    commandLine: {
      hasSwitch: (name) => switches.has(name),
      getSwitchValue: (name) => switches.get(name) || "",
      appendSwitch: (name, value) => switches.set(name, value ?? ""),
    },
    isReady: () => false,
    setPath: (name, value) => paths.set(name, value),
  };

  const resolved = configureHost({ app: earlyApp, config: hostConfig, platform });
  assert.equal(resolved.pluginPath, hostConfig.pluginPath);
  assert.equal(switches.get("allow-command-line-plugins"), "");
  assert.equal(switches.get("register-pepper-plugins"), `${hostConfig.pluginPath};application/x-ppapi-avSDK`);
  assert.equal(paths.get("userData"), hostConfig.profilePath);

  const lateApp = {
    commandLine: {
      hasSwitch: (name) => name === "allow-command-line-plugins",
      getSwitchValue: (name) => name === "register-pepper-plugins" ? `${hostConfig.pluginPath};application/x-ppapi-avSDK` : "",
    },
    isReady: () => true,
    getPath: (name) => name === "userData" ? hostConfig.profilePath : "",
  };
  assert.equal(configureHost({ app: lateApp, config: hostConfig, platform }).pluginPath, hostConfig.pluginPath);
  assert.equal(pluginSwitchMatches(`${hostConfig.pluginPath};application/x-ppapi-avSDK`, hostConfig.pluginPath, platform), true);
  assert.throws(() => configureHost({
    app: { ...lateApp, commandLine: { ...lateApp.commandLine, getSwitchValue: () => "C:\\other\\AVSDKPlugin.dll;application/x-ppapi-avSDK" } },
    config: hostConfig,
    platform,
  }), { code: "unsupported_startup" });
});

test("host-config CLI writes both config files without printing the token", (t) => {
  if (!pluginFilename) return t.skip("PPAPI AVSDK paths are defined only for Windows and Linux");
  const fixture = makeFixture();
  t.after(() => fs.rmSync(fixture.root, { recursive: true, force: true }));
  const values = new Map([
    ["--pid", String(process.pid)],
    ["--app-path", fixture.appPath],
    ["--report", fixture.reportPath],
    ["--profile-path", fixture.profilePath],
    ["--listen-port", "6111"],
    ["--output-port", "6110"],
    ["--generation", "23"],
    ["--config-out", path.join(fixture.root, "native-config.json")],
  ]);
  const result = buildAndWrite(values);
  const nativeConfig = JSON.parse(fs.readFileSync(result.configPath, "utf8"));
  const hostConfig = JSON.parse(fs.readFileSync(result.sidecarPath, "utf8"));
  assert.equal(nativeConfig.modulePath, path.resolve(__dirname, "..", "host", "bootstrap.cjs"));
  assert.equal(hostConfig.ipcToken, nativeConfig.ipcToken);
  assert.equal(hostConfig.hostGeneration, nativeConfig.connectionGeneration);
  assert.equal(JSON.stringify(result).includes(nativeConfig.ipcToken), false);
});
