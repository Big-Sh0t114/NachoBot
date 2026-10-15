"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { GUARD_ENV, installAudioRouteGuard, validateGuardConfig } = require("../host/audio-route-guard.cjs");
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-route-guard-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = { version: 1, qqBuild: "9.9.26-44343", hostGeneration: 128,
    nodeArtifact: path.join(root, "QQCallAudioRouteGuard-v2.node"), guardDllPath: path.join(root, "QQCallAudioRouteGuard-v2.dll"),
    captureEndpointId: "mock-capture", renderEndpointId: "mock-render" };
  for (const [file, hash] of [["nodeArtifact", "nodeSha256"], ["guardDllPath", "dllSha256"]]) {
    fs.writeFileSync(config[file], file);
    config[hash] = crypto.createHash("sha256").update(file).digest("hex");
  }
  const filename = path.join(root, "guard.json");
  fs.writeFileSync(filename, JSON.stringify(config));
  return { config, filename };
}
test("guard is optional; generation and artifact digests bind native installation", (t) => {
  assert.equal(installAudioRouteGuard({ env: {} }), null);
  const { config, filename } = fixture(t);
  let calls = 0;
  const result = installAudioRouteGuard({ env: { [GUARD_ENV]: filename }, generation: 128, platform: "win32", execPath: "owned-QQ",
    loadNative(filename) { assert.equal(filename, config.nodeArtifact); return {
      install(options) { calls++; assert.equal(options.qqImagePath, "owned-QQ"); assert.equal(options.hostGeneration, 128); return { installed: true, failureCode: 0, generation: 128 }; }, status() {},
    }; } });
  assert.deepEqual(result, { installed: true, generation: 128 });
  assert.equal(calls, 1);
  assert.throws(() => validateGuardConfig(config, 129), { code: "audio_guard_config_invalid" });
  fs.writeFileSync(config.guardDllPath, "changed");
  assert.throws(() => installAudioRouteGuard({ env: { [GUARD_ENV]: filename }, generation: 128, platform: "win32", loadNative() { throw new Error("must not load"); } }), { code: "audio_guard_artifact_mismatch" });
});
test("guard refuses unsupported builds, unsafe endpoints, platform and native failures", (t) => {
  const { config, filename } = fixture(t);
  assert.throws(() => validateGuardConfig({ ...config, qqBuild: "future" }, 128), { code: "audio_guard_config_invalid" });
  assert.throws(() => validateGuardConfig({ ...config, renderEndpointId: config.captureEndpointId }, 128), { code: "audio_guard_endpoint_invalid" });
  assert.throws(() => validateGuardConfig({ ...config, extra: true }, 128), { code: "audio_guard_config_invalid" });
  assert.throws(() => installAudioRouteGuard({ env: { [GUARD_ENV]: filename }, generation: 128, platform: "linux" }), { code: "audio_guard_platform_or_path_invalid" });
  assert.throws(() => installAudioRouteGuard({ env: { [GUARD_ENV]: filename }, generation: 128, platform: "win32", loadNative() { return { install() { return { installed: false }; }, status() {} }; } }), { code: "audio_guard_install_failed" });
});
