"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { buildAudioRouteObserver, buildAudioRouteSetup, parseArgs, readPrivateTokenDetails } = require("../bridge/cli.cjs");

const IPC_TOKEN = Buffer.alloc(32, 0x19).toString("base64url");
const CONTROL_TOKEN = Buffer.alloc(32, 0x2a).toString("base64url");
const HOST_PID = 9772;
const KERNEL_PID = 68784;
const HOST_GENERATION = 105;

function setup(t, { sidecarOverrides = {}, rawToken = false } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-route-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const pythonExecutable = path.join(directory, "python.exe");
  fs.writeFileSync(pythonExecutable, "not executed in this test", { mode: 0o600 });
  const tokenFile = path.join(directory, "host-config-sidecar.json");
  const sidecar = {
    ipcToken: IPC_TOKEN,
    targetPid: HOST_PID,
    hostGeneration: HOST_GENERATION,
    ...sidecarOverrides,
  };
  fs.writeFileSync(tokenFile, rawToken ? IPC_TOKEN : JSON.stringify(sidecar), { mode: 0o600 });
  const controlTokenFile = path.join(directory, "control-token.txt");
  fs.writeFileSync(controlTokenFile, CONTROL_TOKEN, { mode: 0o600 });
  const audioRoutePath = path.join(directory, "audio-route-config.private.json");
  const routeConfig = {
    schema: 1,
    hostGeneration: HOST_GENERATION,
    hostPid: HOST_PID,
    pythonExecutable,
    processes: [
      { role: "host", pid: HOST_PID, creationTime100ns: "133240000000000001" },
      { role: "child", pid: 9773, creationTime100ns: "133240000000000002" },
    ],
    captureRenderName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
    playbackRenderName: "Line 1 (Virtual Audio Cable)",
  };
  fs.writeFileSync(audioRoutePath, JSON.stringify(routeConfig), { mode: 0o600 });
  const baseArgs = [
    "--pid", String(KERNEL_PID), "--app-path", directory, "--token-file", tokenFile,
    "--host-port", "6201", "--output-port", "6202", "--host-generation", String(HOST_GENERATION),
    "--connection-generation", "106", "--config", path.join(directory, "kernel-config.json"),
    "--report", path.join(directory, "kernel-report"), "--control-port", "0",
    "--control-token-file", controlTokenFile, "--account-uin", "10001",
  ];
  return { directory, tokenFile, audioRoutePath, routeConfig, sidecar, baseArgs };
}

test("audio-route option remains opt-in and can only be combined with control flags", (t) => {
  const { baseArgs, audioRoutePath } = setup(t);
  const legacy = parseArgs(baseArgs);
  assert.equal(Object.hasOwn(legacy, "audioRouteConfigPath"), false);
  const enabled = parseArgs([...baseArgs, "--audio-route-config", audioRoutePath]);
  assert.equal(enabled.audioRouteConfigPath, path.resolve(audioRoutePath));
  assert.throws(() => parseArgs([
    "--pid", "1", "--app-path", process.cwd(), "--token-file", "token",
    "--host-port", "6201", "--output-port", "6202", "--host-generation", "105", "--connection-generation", "106",
    "--audio-route-config", audioRoutePath,
  ]), { code: "audio_route_requires_control" });
});

test("route configuration rejects raw token files and mismatched Host sidecar identity", async (t) => {
  const raw = setup(t, { rawToken: true });
  const rawOptions = parseArgs([...raw.baseArgs, "--audio-route-config", raw.audioRoutePath]);
  assert.throws(() => buildAudioRouteObserver({
    audioRouteConfigPath: rawOptions.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(raw.tokenFile),
    token: IPC_TOKEN,
    controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  }), { code: "audio_route_host_sidecar_mismatch" });

  const wrongPid = setup(t, { sidecarOverrides: { targetPid: HOST_PID + 1 } });
  const pidOptions = parseArgs([...wrongPid.baseArgs, "--audio-route-config", wrongPid.audioRoutePath]);
  assert.throws(() => buildAudioRouteObserver({
    audioRouteConfigPath: pidOptions.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(wrongPid.tokenFile), token: IPC_TOKEN, controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  }), { code: "audio_route_host_sidecar_mismatch" });

  const wrongGeneration = setup(t, { sidecarOverrides: { hostGeneration: HOST_GENERATION + 1 } });
  const generationOptions = parseArgs([...wrongGeneration.baseArgs, "--audio-route-config", wrongGeneration.audioRoutePath]);
  assert.throws(() => buildAudioRouteObserver({
    audioRouteConfigPath: generationOptions.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(wrongGeneration.tokenFile), token: IPC_TOKEN, controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  }), { code: "audio_route_host_sidecar_mismatch" });
});

test("Host sidecar validation keeps Kernel PID independent from Host PID", (t) => {
  const fixture = setup(t);
  const options = parseArgs([...fixture.baseArgs, "--audio-route-config", fixture.audioRoutePath]);
  assert.notEqual(options.targetPid, fixture.routeConfig.hostPid);
  const observer = buildAudioRouteObserver({
    audioRouteConfigPath: options.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(fixture.tokenFile),
    token: IPC_TOKEN,
    controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  });
  assert.equal(observer.status().closed, false);
  return observer.stop();
});

test("opt-in route config derives selection target in the required reverse endpoint direction", async (t) => {
  const fixture = setup(t);
  const options = parseArgs([...fixture.baseArgs, "--audio-route-config", fixture.audioRoutePath]);
  const setupResult = buildAudioRouteSetup({
    audioRouteConfigPath: options.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(fixture.tokenFile),
    token: IPC_TOKEN,
    controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  });
  assert.deepEqual(setupResult.audioDeviceTarget, {
    inputDeviceName: fixture.routeConfig.playbackRenderName,
    outputDeviceName: fixture.routeConfig.captureRenderName,
  });
  assert.equal(typeof setupResult.observer, "function");
  assert.deepEqual(buildAudioRouteSetup({}).audioDeviceTarget, null);
  await setupResult.observer.stop();
});

test("route configuration rejects duplicate process identities before startup", (t) => {
  const fixture = setup(t);
  const route = JSON.parse(fs.readFileSync(fixture.audioRoutePath, "utf8"));
  route.processes.push({ ...route.processes[1] });
  fs.writeFileSync(fixture.audioRoutePath, JSON.stringify(route), { mode: 0o600 });
  const options = parseArgs([...fixture.baseArgs, "--audio-route-config", fixture.audioRoutePath]);
  assert.throws(() => buildAudioRouteObserver({
    audioRouteConfigPath: options.audioRouteConfigPath,
    tokenDetails: readPrivateTokenDetails(fixture.tokenFile), token: IPC_TOKEN, controlEnabled: true,
    hostGeneration: HOST_GENERATION,
  }), { code: "audio_route_processes_invalid" });
});
