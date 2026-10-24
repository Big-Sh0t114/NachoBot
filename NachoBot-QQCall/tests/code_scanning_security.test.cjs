"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");

const cli = require("../bridge/cli.cjs");
const audioRoutes = require("../bridge/audio-route-observer.cjs");
const audioGuard = require("../host/audio-route-guard.cjs");
const electronHost = require("../host/electron-host.cjs");
const earlyMain = require("../host/early-main.cjs");
const asarSource = require("../agent/asar-source-probe.cjs");
const asarCandidate = require("../agent/asar-candidate-probe.cjs");
const nativeObserver = require("../agent/native-call-observer.cjs");

function tempDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-security-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function writePrivateFile(filePath, content) {
  fs.writeFileSync(filePath, content, { mode: 0o600 });
}

function codeOf(error) {
  return error && error.code;
}

function digest(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

function makeVirtualFs(contents, { reportedSizes = {} } = {}) {
  let nextDescriptor = 10;
  const open = new Map();
  let closed = 0;
  let largestReadRequest = 0;
  function labelFor(filePath) {
    return asarCandidate.SOURCE_ENTRIES.find((label) =>
      filePath.endsWith(path.join("application.asar", ...label.split("/")))) || null;
  }
  return {
    get closed() { return closed; },
    get largestReadRequest() { return largestReadRequest; },
    openSync(filePath, flags) {
      assert.equal(flags, "r");
      const label = labelFor(filePath);
      if (!label || !contents.has(label)) throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      const descriptor = nextDescriptor++;
      open.set(descriptor, { label, bytes: contents.get(label) });
      return descriptor;
    },
    fstatSync(descriptor) {
      const item = open.get(descriptor);
      assert.ok(item);
      return { size: reportedSizes[item.label] ?? item.bytes.length, isFile: () => true };
    },
    readSync(descriptor, target, targetOffset, length, position) {
      const item = open.get(descriptor);
      assert.ok(item);
      largestReadRequest = Math.max(largestReadRequest, length);
      const offset = Number.isInteger(position) ? position : 0;
      const part = item.bytes.subarray(offset, offset + length);
      part.copy(target, targetOffset);
      return part.length;
    },
    closeSync(descriptor) {
      assert.ok(open.delete(descriptor));
      closed += 1;
    },
  };
}

function candidateContents(html = "<script>ok</script>") {
  return new Map(asarCandidate.SOURCE_ENTRIES.map((label) => [
    label,
    Buffer.from(label === "renderer/index.html" ? html : `safe ${label}`, "utf8"),
  ]));
}

function validRouteConfig(pythonExecutable, hostPid = process.pid) {
  return {
    schema: 1,
    hostGeneration: 3,
    hostPid,
    pythonExecutable,
    processes: [{ role: "host", pid: hostPid, creationTime100ns: "1" }],
    captureRenderName: "QQCall Capture",
    playbackRenderName: "QQCall Playback",
  };
}

test("private CLI readers accept bounded descriptor contents and close each descriptor", (t) => {
  const root = tempDirectory(t);
  const token = "A".repeat(43);
  const tokenPath = path.join(root, "token.txt");
  const identityPath = path.join(root, "identity.json");
  const oneBotPath = path.join(root, "onebot.json");
  const peersPath = path.join(root, "peers.json");
  writePrivateFile(tokenPath, `${token}\n`);
  writePrivateFile(identityPath, JSON.stringify({ selfUid: "uid", selfUin: "12345", accountPath: "C:\\QQ" }));
  writePrivateFile(oneBotPath, JSON.stringify({ selfUid: "uid", selfUin: "12345" }));
  writePrivateFile(peersPath, JSON.stringify({
    accountUin: "12345", targetPid: process.pid, connectionGeneration: 1, peerUidsByUin: {},
  }));

  const originalClose = fs.closeSync;
  let closes = 0;
  fs.closeSync = (descriptor) => { closes += 1; return originalClose(descriptor); };
  try {
    assert.equal(cli.readPrivateToken(tokenPath), token);
    assert.deepEqual(cli.readIdentityFile(identityPath), {
      selfUid: "uid", selfUin: "12345", accountPath: "C:\\QQ",
    });
    assert.deepEqual(cli.readPrivateOneBotIdentityFile(oneBotPath), { selfUid: "uid", selfUin: "12345" });
    assert.equal(Object.keys(cli.readKernelPeerIdentitiesFile(peersPath, {
      accountUin: "12345", targetPid: process.pid, connectionGeneration: 1,
    })).length, 0);
  } finally {
    fs.closeSync = originalClose;
  }
  assert.equal(closes, 4);
});

test("private CLI readers reject oversized inputs and still close the descriptor", (t) => {
  const tokenPath = path.join(tempDirectory(t), "oversized-token.txt");
  writePrivateFile(tokenPath, "A".repeat(64 * 1024 + 1));
  const originalClose = fs.closeSync;
  let closes = 0;
  fs.closeSync = (descriptor) => { closes += 1; return originalClose(descriptor); };
  try {
    assert.throws(() => cli.readPrivateToken(tokenPath), (error) => codeOf(error) === "invalid_token_file");
  } finally {
    fs.closeSync = originalClose;
  }
  assert.equal(closes, 1);
});

test("private CLI readers reject symlinks and detect a path swap after open", (t) => {
  if (process.platform === "win32") return t.skip("symlink creation is not guaranteed on Windows runners");
  const root = tempDirectory(t);
  const token = "B".repeat(43);
  const target = path.join(root, "token.txt");
  const link = path.join(root, "token-link.txt");
  const moved = path.join(root, "token-opened.txt");
  writePrivateFile(target, token);
  fs.symlinkSync(target, link);
  assert.throws(() => cli.readPrivateToken(link), (error) => codeOf(error) === "invalid_token_file");

  const originalOpen = fs.openSync;
  const originalClose = fs.closeSync;
  let closes = 0;
  let swapped = false;
  fs.closeSync = (descriptor) => { closes += 1; return originalClose(descriptor); };
  fs.openSync = (filePath, flags, mode) => {
    const descriptor = originalOpen(filePath, flags, mode);
    if (!swapped && path.resolve(filePath) === path.resolve(target)) {
      swapped = true;
      fs.renameSync(target, moved);
      fs.symlinkSync(moved, target);
    }
    return descriptor;
  };
  try {
    assert.throws(() => cli.readPrivateToken(target), (error) => codeOf(error) === "invalid_token_file");
  } finally {
    fs.openSync = originalOpen;
    fs.closeSync = originalClose;
    if (fs.existsSync(target)) fs.unlinkSync(target);
    if (fs.existsSync(moved)) fs.renameSync(moved, target);
  }
  assert.equal(swapped, true);
  assert.equal(closes, 1);
});

test("audio route config keeps POSIX-private validation on the opened descriptor", (t) => {
  const root = tempDirectory(t);
  const executable = path.join(root, "python.exe");
  const configPath = path.join(root, "route.json");
  writePrivateFile(executable, "not executed by this test");
  writePrivateFile(configPath, JSON.stringify(validRouteConfig(executable)));
  assert.equal(audioRoutes.readAudioRouteConfig(configPath).hostGeneration, 3);
  if (process.platform !== "win32") {
    fs.chmodSync(configPath, 0o644);
    assert.throws(() => audioRoutes.readAudioRouteConfig(configPath),
      (error) => codeOf(error) === "audio_route_config_not_private");
  }
  writePrivateFile(configPath, " ".repeat(64 * 1024 + 1));
  assert.throws(() => audioRoutes.readAudioRouteConfig(configPath),
    (error) => codeOf(error) === "audio_route_config_unavailable");
});

test("audio observer rejects a report path swapped after descriptor open", async (t) => {
  if (process.platform === "win32") return t.skip("symlink creation is not guaranteed on Windows runners");
  const root = tempDirectory(t);
  const executable = path.join(root, "python.exe");
  const configPath = path.join(root, "route.json");
  writePrivateFile(executable, "not executed by this test");
  writePrivateFile(configPath, "{}");
  const config = audioRoutes.validateAudioRouteConfig(validRouteConfig(executable));
  let reportPath;
  let movedReportPath;
  let swapped = false;
  const child = new EventEmitter();
  child.pid = process.pid;
  child.kill = () => true;
  const observer = audioRoutes.createAudioRouteObserver({
    config,
    configPath,
    execFileImpl: (_executable, args, _options, callback) => {
      reportPath = args[args.indexOf("--report") + 1];
      movedReportPath = `${reportPath}.opened`;
      fs.writeFileSync(reportPath, "{}");
      callback(null, JSON.stringify({ status: "unverified", verified: false }), "");
      setImmediate(() => child.emit("close", 0, null));
      return child;
    },
  });

  const originalOpen = fs.openSync;
  const originalClose = fs.closeSync;
  let reportDescriptor = null;
  let reportDescriptorClosed = false;
  fs.openSync = (filePath, flags, mode) => {
    const descriptor = originalOpen(filePath, flags, mode);
    if (!swapped && reportPath && path.resolve(filePath) === path.resolve(reportPath)) {
      swapped = true;
      reportDescriptor = descriptor;
      fs.renameSync(reportPath, movedReportPath);
      fs.symlinkSync(movedReportPath, reportPath);
    }
    return descriptor;
  };
  fs.closeSync = (descriptor) => {
    if (descriptor === reportDescriptor) reportDescriptorClosed = true;
    return originalClose(descriptor);
  };
  try {
    const result = await observer({
      captureRenderName: config.captureRenderName,
      playbackRenderName: config.playbackRenderName,
    });
    assert.deepEqual(result, { verified: false, code: "observer_report_invalid" });
    assert.equal(swapped, true);
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    fs.openSync = originalOpen;
    fs.closeSync = originalClose;
    await observer.stop();
    if (reportPath && fs.existsSync(reportPath)) fs.rmSync(reportPath, { force: true });
    if (movedReportPath && fs.existsSync(movedReportPath)) fs.rmSync(movedReportPath, { force: true });
  }
  assert.equal(reportDescriptorClosed, true);
});

test("audio guard config and artifacts are read through bounded descriptors", (t) => {
  const root = tempDirectory(t);
  const generation = 7;
  const nodeArtifact = path.join(root, "QQCallAudioRouteGuard-v2.node");
  const guardDllPath = path.join(root, "QQCallAudioRouteGuard-v2.dll");
  const configPath = path.join(root, "guard.json");
  const nodeBytes = Buffer.from("node artifact test bytes");
  const dllBytes = Buffer.from("dll artifact test bytes");
  writePrivateFile(nodeArtifact, nodeBytes);
  writePrivateFile(guardDllPath, dllBytes);
  const config = {
    version: 1,
    qqBuild: "9.9.26-44343",
    hostGeneration: generation,
    nodeArtifact,
    nodeSha256: digest(nodeBytes).toLowerCase(),
    guardDllPath,
    dllSha256: digest(dllBytes).toLowerCase(),
    captureEndpointId: "capture-id",
    renderEndpointId: "render-id",
  };
  writePrivateFile(configPath, JSON.stringify(config));
  const result = audioGuard.installAudioRouteGuard({
    env: { [audioGuard.GUARD_ENV]: configPath },
    generation,
    platform: "win32",
    execPath: "C:\\QQ\\QQ.exe",
    loadNative: () => ({
      install: () => ({ installed: true, failureCode: 0, generation }),
      status: () => ({}),
    }),
  });
  assert.deepEqual(result, { installed: true, generation });
  writePrivateFile(configPath, " ".repeat(16 * 1024 + 1));
  assert.throws(() => audioGuard.installAudioRouteGuard({
    env: { [audioGuard.GUARD_ENV]: configPath }, generation, platform: "win32",
  }), (error) => codeOf(error) === "audio_guard_config_unavailable");
});

test("early launch plan reads use the opened file with a hard size cap", (t) => {
  const root = tempDirectory(t);
  const runtimePath = path.join(root, "runtime");
  const planPath = path.join(root, "launch-plan.json");
  const plan = {
    schema: 1,
    appPath: path.join(root, "app"),
    runtimePath,
    reportPath: path.join(runtimePath, "report.json"),
    profilePath: path.join(runtimePath, "profile"),
    listenPort: 1234,
    outputPort: 1235,
    hostGeneration: 1,
    sessionName: "offline-test",
  };
  writePrivateFile(planPath, JSON.stringify(plan));
  assert.equal(earlyMain.readLaunchPlan(planPath).hostGeneration, 1);
  writePrivateFile(planPath, " ".repeat(64 * 1024 + 1));
  assert.throws(() => earlyMain.readLaunchPlan(planPath),
    (error) => codeOf(error) === "launch_plan_invalid");
});

test("earlyloader reads its owned plan through the opened descriptor", (t) => {
  const planPath = path.resolve(__dirname, "..", "launch-plan.json");
  if (fs.existsSync(planPath)) return t.skip("an existing launch plan belongs to the local runtime");
  const oldPlan = process.env.QQCALL_HOST_LAUNCH_PLAN;
  const oldMain = process.env.QQCALL_HOST_MAIN;
  const oldExitCode = process.exitCode;
  const oldConsoleError = console.error;
  delete process.env.QQCALL_HOST_LAUNCH_PLAN;
  delete process.env.QQCALL_HOST_MAIN;
  console.error = () => {};
  let earlyLoader;
  try {
    earlyLoader = require("../host/earlyloader.cjs");
  } finally {
    console.error = oldConsoleError;
    process.exitCode = oldExitCode;
  }

  const writePlan = () => writePrivateFile(planPath, JSON.stringify({ schema: 1, hostGeneration: 23 }));
  try {
    writePlan();
    process.env.QQCALL_HOST_LAUNCH_PLAN = planPath;
    assert.equal(earlyLoader.readOwnedPlanLocation().generation, 23);
    if (process.platform !== "win32") {
      const movedPlanPath = `${planPath}.opened`;
      const originalOpen = fs.openSync;
      let swapped = false;
      fs.openSync = (filePath, flags, mode) => {
        const descriptor = originalOpen(filePath, flags, mode);
        if (!swapped && path.resolve(filePath) === path.resolve(planPath)) {
          swapped = true;
          fs.renameSync(planPath, movedPlanPath);
          fs.symlinkSync(movedPlanPath, planPath);
        }
        return descriptor;
      };
      try {
        assert.equal(earlyLoader.readOwnedPlanLocation(), null);
      } finally {
        fs.openSync = originalOpen;
        if (fs.existsSync(planPath)) fs.unlinkSync(planPath);
        if (fs.existsSync(movedPlanPath)) fs.renameSync(movedPlanPath, planPath);
      }
      assert.equal(swapped, true);
    }
  } finally {
    if (oldPlan === undefined) delete process.env.QQCALL_HOST_LAUNCH_PLAN;
    else process.env.QQCALL_HOST_LAUNCH_PLAN = oldPlan;
    if (oldMain === undefined) delete process.env.QQCALL_HOST_MAIN;
    else process.env.QQCALL_HOST_MAIN = oldMain;
    fs.rmSync(planPath, { force: true });
  }
});

test("candidate ASAR reader uses virtual descriptors and closes them", (t) => {
  const html = [
    "<script>alpha</SCRIPT\t\n bar>",
    "<script>beta</script/>",
    "<script>gamma</script data-x='yes'>",
    "<script>delta</scriptx>tail</script>",
  ].join("\n");
  const virtualFs = makeVirtualFs(candidateContents(html));
  const probe = asarCandidate.createAsarCandidateProbe({ fsModule: virtualFs });
  const report = probe.probeAsarCandidates({ appPath: path.resolve(os.tmpdir(), "qqcall-virtual-app") });
  assert.equal(report.status, "candidate");
  assert.equal(virtualFs.closed, asarCandidate.SOURCE_ENTRIES.length);
  const entry = report.entries.find((item) => item.label === "renderer/index.html");
  assert.deepEqual(entry.content.inlineScripts, [
    "alpha", "beta", "gamma", "delta</scriptx>tail",
  ]);
});

test("candidate ASAR reader rejects oversized descriptor content and closes it", (t) => {
  const contents = candidateContents();
  const first = asarCandidate.SOURCE_ENTRIES[0];
  contents.set(first, Buffer.alloc(asarCandidate.MAX_ENTRY_BYTES + 1, 0x61));
  const virtualFs = makeVirtualFs(contents, { reportedSizes: { [first]: 1 } });
  const report = asarCandidate.createAsarCandidateProbe({ fsModule: virtualFs })
    .probeAsarCandidates({ appPath: path.resolve(os.tmpdir(), "qqcall-virtual-app") });
  assert.equal(report.status, "rejected");
  assert.equal(report.entries[0].code, "source_entry_invalid");
  assert.ok(virtualFs.largestReadRequest <= 64 * 1024);
  assert.equal(virtualFs.closed, asarCandidate.SOURCE_ENTRIES.length);
});

test("source ASAR probe preserves physical hash pins and reports lexical HTML scripts", (t) => {
  const root = tempDirectory(t);
  const archiveBytes = Buffer.from("archive bytes");
  const wrapperBytes = Buffer.from("wrapper bytes");
  const packageBytes = Buffer.from('{"name":"offline-fixture"}\n');
  fs.writeFileSync(path.join(root, "application.asar"), archiveBytes);
  fs.writeFileSync(path.join(root, "wrapper.node"), wrapperBytes);
  fs.writeFileSync(path.join(root, "package.json"), packageBytes);
  const contents = candidateContents([
    "<script>alpha</SCRIPT\t\n bar>",
    "<script>beta</script/>",
    "<script>gamma</script data-x='yes'>",
    "<script>delta</scriptx>tail</script>",
  ].join("\n"));
  const virtualFs = makeVirtualFs(contents);
  const report = asarSource.createAsarSourceProbe({
    fsModule: virtualFs,
    mockPhysicalFsModule: fs,
    pins: { archive: digest(archiveBytes), packageJson: digest(packageBytes), wrapper: digest(wrapperBytes) },
  }).probeAsarSources({ appPath: root });
  assert.equal(report.status, "complete");
  assert.equal(report.archiveSha256, digest(archiveBytes));
  assert.equal(report.packageJsonSha256, digest(packageBytes));
  assert.equal(report.wrapperSha256, digest(wrapperBytes));
  assert.deepEqual(report.entries.at(-1).content.inlineScripts, [
    "alpha", "beta", "gamma", "delta</scriptx>tail",
  ]);
  assert.equal(virtualFs.closed, asarSource.SOURCE_ENTRIES.length);
});

test("source ASAR probe detects a path swap after opening and closes the archive descriptor", (t) => {
  if (process.platform === "win32") return t.skip("symlink creation is not guaranteed on Windows runners");
  const root = tempDirectory(t);
  const archivePath = path.join(root, "application.asar");
  const movedArchivePath = path.join(root, "application.asar.opened");
  const archiveBytes = Buffer.from("archive bytes");
  const wrapperBytes = Buffer.from("wrapper bytes");
  const packageBytes = Buffer.from("{}");
  fs.writeFileSync(archivePath, archiveBytes);
  fs.writeFileSync(path.join(root, "wrapper.node"), wrapperBytes);
  fs.writeFileSync(path.join(root, "package.json"), packageBytes);
  const contents = candidateContents();
  const virtualFs = makeVirtualFs(contents);
  const physicalFs = Object.create(fs);
  const originalOpen = fs.openSync;
  const originalClose = fs.closeSync;
  let swapped = false;
  let closes = 0;
  physicalFs.openSync = (filePath, flags, mode) => {
    const descriptor = originalOpen(filePath, flags, mode);
    if (!swapped && path.resolve(filePath) === path.resolve(archivePath)) {
      swapped = true;
      fs.renameSync(archivePath, movedArchivePath);
      fs.symlinkSync(movedArchivePath, archivePath);
    }
    return descriptor;
  };
  physicalFs.closeSync = (descriptor) => { closes += 1; return originalClose(descriptor); };
  const report = asarSource.createAsarSourceProbe({
    fsModule: virtualFs,
    mockPhysicalFsModule: physicalFs,
    pins: { archive: digest(archiveBytes), packageJson: digest(packageBytes), wrapper: digest(wrapperBytes) },
  }).probeAsarSources({ appPath: root });
  if (fs.existsSync(archivePath)) fs.unlinkSync(archivePath);
  if (fs.existsSync(movedArchivePath)) fs.renameSync(movedArchivePath, archivePath);
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "archive_symlink");
  assert.equal(swapped, true);
  assert.equal(closes, 1);
});

test("native observer expression builders reject hostile dynamic bindings", () => {
  const runId = "a".repeat(32);
  const expression = nativeObserver.buildNativeTargetExpression(runId, 2, 9);
  const condition = nativeObserver.buildNativeConditionalExpression(runId, 9);
  const key = `qqcall.official.av.observer.${runId}.9`;
  assert.ok(expression.includes(JSON.stringify(key)));
  assert.ok(expression.includes(`__own(__s,"ordinal")!==2`));
  assert.ok(condition.includes(JSON.stringify(key)));

  const original = function original() { return "bound original"; };
  let observed = null;
  const state = {
    schema: "qqcall.official.av.observer.page.v1",
    runId,
    ordinal: 2,
    epoch: 9,
    nativeConditional: true,
    active: true,
    original,
    observeNativeAttempt(receiver, args) { observed = { receiver, args: Array.from(args) }; },
  };
  const sandbox = { [Symbol.for(key)]: state };
  vm.createContext(sandbox);
  assert.equal(vm.runInContext(expression, sandbox), original);
  const receiver = { target: true };
  const args = ["native-call", 17];
  sandbox.receiver = receiver;
  sandbox.args = args;
  assert.equal(vm.runInContext(`(function(){return ${condition}}).apply(receiver,args)`, sandbox), false);
  assert.deepEqual(observed, { receiver, args });

  for (const value of [
    `"));globalThis.pwned=true;//`,
    "A".repeat(32),
    "a".repeat(31),
    "a".repeat(33),
    null,
  ]) {
    assert.throws(() => nativeObserver.buildNativeTargetExpression(value, 1, 1), TypeError);
    assert.throws(() => nativeObserver.buildNativeConditionalExpression(value, 1), TypeError);
  }
  for (const epoch of [0, 1_000_001, Number.MAX_SAFE_INTEGER, 1.5, "1"]) {
    assert.throws(() => nativeObserver.buildNativeTargetExpression(runId, 1, epoch), TypeError);
    assert.throws(() => nativeObserver.buildNativeConditionalExpression(runId, epoch), TypeError);
  }
  for (const ordinal of [0, 17, Number.MAX_SAFE_INTEGER, 1.5, "1"]) {
    assert.throws(() => nativeObserver.buildNativeTargetExpression(runId, ordinal, 1), TypeError);
  }
});

test("verified plugin probe rejects a symlink before trusting its bytes", (t) => {
  if (process.platform === "win32") return t.skip("symlink creation is not guaranteed on Windows runners");
  const root = tempDirectory(t);
  const target = path.join(root, "actual.node");
  const link = path.join(root, "alias.node");
  writePrivateFile(target, "not a verified plugin");
  fs.symlinkSync(target, link);
  assert.equal(electronHost.pluginHasVerifiedAudioDeviceProtocol(link, "win32"), false);
});
