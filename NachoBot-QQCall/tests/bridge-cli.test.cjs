"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { PassThrough, Writable } = require("node:stream");
const { PINNED_HASHES, SOURCE_ENTRIES } = require("../agent/asar-source-probe.cjs");
const { SOURCE_ENTRIES: ASAR_CANDIDATE_ENTRIES, PROOF_LEVEL: ASAR_CANDIDATE_PROOF_LEVEL } = require("../agent/asar-candidate-probe.cjs");
const { PROOF_LEVEL: OFFICIAL_WINDOW_PROOF_LEVEL } = require("../agent/official-window-probe.cjs");
const { PROOF_LEVEL: RENDERER_SCRIPT_METADATA_PROOF_LEVEL } = require("../agent/renderer-script-metadata-probe.cjs");
const { PROOF_LEVEL: OFFICIAL_AV_PROOF_LEVEL } = require("../agent/official-av-observer.cjs");
const { APP_PATH: ASAR_CANDIDATE_APP_PATH } = require("../bridge/asar-keeper-client.cjs");
const { createPipeClient } = require("../probe/ipc.cjs");
const {
  parseArgs,
  parseCommandLine,
  parseIdentityFileArguments,
  readIdentityFile,
  readKernelPeerIdentitiesFile,
  readPrivateOneBotIdentityFile,
  readPrivateToken,
  runRendererAdmissionProcess,
  startTransportBeforeHost,
  startRendererCapabilityProbe,
  startAsarSourceProbe,
  startAsarCandidateProbe,
  startOfficialWindowProbe,
  startRendererScriptMetadataProbe,
  validateRendererScriptMetadataProbeOptions,
  summarizeRendererScriptMetadataProbe,
  startOfficialAVObserver,
  startCli,
  validateOfficialAVObserverOptions,
  validateRendererAdmissionResult,
} = require("../bridge/cli.cjs");

const TOKEN = "cli-private-token-value-that-is-43-characters-long-0123456789";
const FIXED_CAPABILITY_REPORT = Object.freeze({
  schema: 1,
  processType: "unknown",
  electronPresent: false,
  electronBuiltin: false,
  appAvailable: false,
  browserWindowAvailable: false,
  getAllWindowsAvailable: false,
  windowCount: 0,
  windowsTruncated: false,
  windows: [],
});
const ASAR_SOURCE_SENTINEL = "PRIVATE_ASAR_SOURCE_TEXT_SENTINEL";
const ASAR_CANDIDATE_SENTINEL = "PRIVATE_ASAR_CANDIDATE_SOURCE_SENTINEL";
const FIXED_ASAR_SOURCE_REPORT = Object.freeze({
  schema: 1,
  status: "complete",
  code: "ok",
  archiveSha256: PINNED_HASHES.archive,
  packageJsonSha256: PINNED_HASHES.packageJson,
  wrapperSha256: PINNED_HASHES.wrapper,
  entries: SOURCE_ENTRIES.map((label, index) => ({
    label,
    status: "read",
    bytes: index === SOURCE_ENTRIES.length - 1 ? 120 : 64,
    sha256: String(index + 1).repeat(64),
    content: label === "renderer/index.html"
      ? { inlineScripts: [ASAR_SOURCE_SENTINEL], scriptSrcDirectives: ["script-src 'self'"], scriptSources: ["renderer.js"] }
      : `// ${ASAR_SOURCE_SENTINEL} ${label}`,
  })),
});
const FIXED_ASAR_CANDIDATE_REPORT = Object.freeze({
  schema: "qqcall.asar.candidates.v1",
  proofLevel: ASAR_CANDIDATE_PROOF_LEVEL,
  status: "candidate",
  code: "ok",
  entries: ASAR_CANDIDATE_ENTRIES.map((label, index) => ({
    label,
    status: "read",
    bytes: 48 + index,
    sha256: String(index + 1).repeat(64),
    content: label === "renderer/index.html"
      ? { inlineScripts: [ASAR_CANDIDATE_SENTINEL], scriptSrcDirectives: ["script-src 'self'"], scriptSources: ["renderer.js"] }
      : `// ${ASAR_CANDIDATE_SENTINEL} ${label}`,
  })),
});
const FIXED_OFFICIAL_WINDOW_REPORT = Object.freeze({
  schema: "qqcall.official.windows.v1",
  proofLevel: OFFICIAL_WINDOW_PROOF_LEVEL,
  status: "complete",
  code: "ok",
  windowCount: 2,
  windowsTruncated: false,
  windows: [
    {
      ordinal: 1,
      destroyed: false,
      rendererFileMatch: true,
      routePath: null,
      domStatus: "observed",
      embedCount: 1,
      postMessageAvailable: true,
    },
    {
      ordinal: 2,
      destroyed: false,
      rendererFileMatch: false,
      routePath: null,
      domStatus: "not_queried",
      embedCount: null,
      postMessageAvailable: null,
    },
  ],
});
const FIXED_RENDERER_SCRIPT_METADATA_REPORT = Object.freeze({
  schema: "qqcall.renderer.script-metadata.v1",
  proofLevel: RENDERER_SCRIPT_METADATA_PROOF_LEVEL,
  status: "complete",
  code: "ok",
  windowCount: 1,
  windowsTruncated: false,
  exactAVWindowCount: 1,
  scriptRecordCount: 2,
  windows: [{
    ordinal: 1,
    destroyed: false,
    rendererBaseKind: "app",
    avFragmentExact: true,
    queryPresent: false,
    exactAVRenderer: true,
    status: "observed",
    scriptCount: 2,
    scriptsTruncated: false,
    scripts: [
      { ordinal: 1, sourceKind: "inline", status: "observed", byteCount: 87, avsdkMarker: true, postMessageMarker: false, sceneIdMarker: true },
      { ordinal: 2, sourceKind: "local_js", status: "external_unread", byteCount: null, avsdkMarker: false, postMessageMarker: false, sceneIdMarker: false },
    ],
  }],
});

function rendererProbeOptions(directory) {
  const tokenFile = path.join(directory, "renderer-probe-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    probeRendererCapabilities: true,
    targetPid: 123,
    appPath: process.cwd(),
    tokenFile,
    configPath: path.join(directory, "renderer-probe-config.json"),
    connectionGeneration: 991,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function asarSourceProbeOptions(directory) {
  const tokenFile = path.join(directory, "asar-source-probe-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    probeAsarSources: true,
    targetPid: 123,
    appPath: process.cwd(),
    tokenFile,
    configPath: path.join(directory, "asar-source-probe-config.json"),
    connectionGeneration: 992,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function asarCandidateProbeOptions(directory) {
  const tokenFile = path.join(directory, "asar-candidate-probe-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    probeAsarCandidates: true,
    targetPid: 123,
    appPath: ASAR_CANDIDATE_APP_PATH,
    tokenFile,
    configPath: path.join(directory, "asar-candidate-probe-config.json"),
    connectionGeneration: 993,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function officialWindowProbeOptions(directory) {
  const tokenFile = path.join(directory, "official-window-probe-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    probeOfficialWindows: true,
    targetPid: 123,
    appPath: ASAR_CANDIDATE_APP_PATH,
    tokenFile,
    configPath: path.join(directory, "official-window-probe-config.json"),
    connectionGeneration: 994,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function rendererScriptMetadataProbeOptions(directory) {
  const tokenFile = path.join(directory, "renderer-script-metadata-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    probeRendererScriptMetadata: true,
    targetPid: 123,
    appPath: ASAR_CANDIDATE_APP_PATH,
    tokenFile,
    configPath: path.join(directory, "renderer-script-metadata-config.json"),
    connectionGeneration: 996,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function officialAVObserverOptions(directory) {
  const tokenFile = path.join(directory, "official-av-observer-token.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  return {
    observeOfficialAV: true,
    targetPid: 123,
    appPath: ASAR_CANDIDATE_APP_PATH,
    tokenFile,
    configPath: path.join(directory, "official-av-observer-config.json"),
    connectionGeneration: 995,
    kernelCreationTime100ns: "134000000000000000",
    probePythonPath: process.execPath,
  };
}

function fixedOfficialAVReport(status = "active", { includeEvent = false, windowStatus = null, nativeConditionalStatus = null } = {}) {
  const event = {
    eventIndex: 1,
    direction: "outgoing",
    command: 4,
    requestId: { present: true, type: "string", length: 8, ref: "v1" },
    windowOrdinal: 1,
    documentEpoch: 1,
    parameterKind: "array",
    parameterLength: 1,
    parameterTruncated: false,
    parameterItems: [{ type: "string", length: 16, ref: null }],
    command4: {
      available: true,
      sceneId: 3,
      inviteCount: 1,
      subBusinessType: 7,
      inviteOriginal: 1,
      isCreator: true,
      relationId: "v2",
      roomId: "v3",
      selfUid: "v4",
      inviteUidRefs: ["v5"],
      inviteUidCount: 1,
      inviteUidTruncated: false,
    },
  };
  const windows = status === "rejected" ? [] : [{
    ordinal: 1,
    documentEpoch: 1,
    status: windowStatus || (status === "stopped" ? "destroyed" : "armed"),
    embedCount: 1,
    postMessageAvailable: true,
    cleanupComplete: status === "stopped" ? true : null,
    nativeConditionalStatus,
    nativeCallAttempts: 0,
    nativeCallAttemptsSaturated: false,
  }];
  const events = includeEvent ? [event] : [];
  return {
    schema: "qqcall.official.av.observer.v1",
    proofLevel: OFFICIAL_AV_PROOF_LEVEL,
    status,
    code: status === "rejected" ? "process_not_browser" : "ok",
    coverageGap: true,
    comparisonScope: "document_epoch_only",
    outboundAttemptsOnly: true,
    windowsTruncated: false,
    windows,
    events,
    droppedEvents: 0,
    encodedEventBytes: events.length ? Buffer.byteLength(JSON.stringify(events[0])) : 0,
    incomplete: false,
    queueLossPossible: false,
    cleanupComplete: status === "stopped" ? true : null,
  };
}

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("cli_test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

test("CLI parser accepts explicit generations/ports and normalizes unknown commands without echoing text", () => {
  const options = parseArgs([
    "--pid", "123",
    "--app-path", process.cwd(),
    "--token-file", "token.json",
    "--host-port", "6111",
    "--output-port", "6110",
    "--host-generation", "9",
    "--connection-generation", "10",
  ]);
  assert.equal(options.targetPid, 123);
  assert.equal(options.hostPort, 6111);
  assert.equal(options.outputPort, 6110);
  assert.equal(options.hostPid, undefined);
  assert.equal(options.hostGeneration, 9);
  assert.equal(options.connectionGeneration, 10);
  assert.equal(Object.hasOwn(options, "kernelCreationTime100ns"), false,
    "older CLI invocations remain valid but cannot create a private-origin binding");
  assert.equal(Object.hasOwn(options, "maxLifetimeMs"), false, "omission preserves the Broker's default lifetime");
  assert.deepEqual(parseCommandLine(`${TOKEN} never-print-me`), { command: "unknown", argument: "" });
  assert.deepEqual(parseCommandLine('login "C:\\private identity.json"'), { command: "login", argument: "C:\\private identity.json" });
  const identityArgs = 'identity "C:\\private identity.json" "C:\\output identity.json"';
  assert.deepEqual(parseCommandLine(identityArgs), { command: "identity", argument: '"C:\\private identity.json" "C:\\output identity.json"' });
  assert.deepEqual(parseIdentityFileArguments(parseCommandLine(identityArgs).argument), ["C:\\private identity.json", "C:\\output identity.json"]);
  assert.throws(() => parseArgs(["--pid", "1"]), { code: "missing_cli_option" });
  assert.throws(() => parseArgs([
    "--pid", "1", "--app-path", process.cwd(), "--token-file", "token",
    "--host-port", "0", "--output-port", "2", "--host-generation", "1", "--connection-generation", "1",
  ]), { code: "invalid_cli_number" });
  assert.throws(() => parseArgs([
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "token.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
    "--kernel-peer-identities-path", "identities.json",
  ]), { code: "kernel_peer_identities_requires_control" });
  const withKernelCreation = parseArgs([
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "token.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
    "--kernel-creation-time-100ns", "134000000000000000",
  ]);
  assert.equal(withKernelCreation.kernelCreationTime100ns, "134000000000000000");
  for (const value of ["0", "00", "-1", "1e9", "18446744073709551616"]) {
    assert.throws(() => parseArgs([
      "--pid", "123", "--app-path", process.cwd(), "--token-file", "token.json",
      "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
      "--kernel-creation-time-100ns", value,
    ]), { code: "invalid_kernel_creation_time" }, value);
  }
  const mapped = parseArgs([
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "token.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
    "--control-port", "0", "--control-token-file", "control-token.json", "--account-uin", "70001",
    "--kernel-peer-identities-path", "identities.json",
  ]);
  assert.equal(mapped.kernelPeerIdentitiesPath, path.resolve("identities.json"));
});

test("CLI prepares real callbacks before Host start without Kernel attach or login", async () => {
  const order = [];
  const broker = {
    async start() { order.push("broker.start"); return { pipeName: "fixture-pipe" }; },
    async writeConfigFile(value) { assert.equal(value, "fixture-config"); order.push("broker.config"); },
  };
  const bridge = {
    prepareSubscriptions() { order.push("bridge.prepare"); },
    async attach() { assert.fail("startup must not attach"); },
    async login() { assert.fail("startup must not login"); },
  };
  const host = {
    async start() { order.push("host.start"); return { available: false, outputPort: 6110 }; },
  };
  const result = await startTransportBeforeHost({ broker, bridge, host, configPath: "fixture-config" });
  assert.deepEqual(order, ["broker.start", "broker.config", "bridge.prepare", "host.start"]);
  assert.deepEqual(result, {
    brokerStatus: { pipeName: "fixture-pipe" },
    hostStatus: { available: false, outputPort: 6110 },
  });
});

test("CLI rejects a Host PID or generation that differs from private sidecar metadata", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-host-binding-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const sidecar = path.join(directory, "host-sidecar.json");
  fs.writeFileSync(sidecar, JSON.stringify({
    ipcToken: TOKEN, targetPid: 456, hostGeneration: 9, listenPort: 6111, outputPort: 6110,
  }));
  const options = parseArgs([
    "--pid", "123", "--app-path", process.cwd(), "--token-file", sidecar,
    "--host-port", "6111", "--output-port", "6110", "--host-pid", "457",
    "--host-generation", "9", "--connection-generation", "10",
  ]);
  await assert.rejects(startCli(options), { code: "host_sidecar_binding_mismatch" });
});

test("optional Broker lifetime accepts the inclusive bounded range and rejects malformed values", () => {
  const base = [
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "token.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
  ];
  assert.equal(parseArgs([...base, "--lifetime-ms", "1000"]).maxLifetimeMs, 1000);
  assert.equal(parseArgs([...base, "--lifetime-ms", "3600000"]).maxLifetimeMs, 3600000);
  assert.equal(parseArgs([...base, "--lifetime-ms", "123456"]).maxLifetimeMs, 123456);
  for (const value of ["999", "3600001", "1.5", "1e3", "-1000", "", "9007199254740992"]) {
    assert.throws(() => parseArgs([...base, "--lifetime-ms", value]), { code: "invalid_lifetime" }, value);
  }
});

test("programmatic Broker lifetime rejects values outside the bounded integer range", async () => {
  for (const maxLifetimeMs of [null, 999, 3600001, 1.5, "1000", Number.MAX_SAFE_INTEGER]) {
    await assert.rejects(startCli({ maxLifetimeMs }), { code: "invalid_lifetime" });
  }
});

test("CLI forwards a selected bounded lifetime to the Broker", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for the Broker lifetime smoke test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-lifetime-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const tokenFile = path.join(directory, "private-host-sidecar.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  const input = new PassThrough();
  const output = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  const errorOutput = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  const options = parseArgs([
    "--pid", String(process.pid), "--app-path", process.cwd(), "--token-file", tokenFile,
    "--host-port", String(await reservePort()), "--output-port", String(await reservePort()),
    "--host-generation", "913", "--connection-generation", "914", "--lifetime-ms", "1000",
    "--config", path.join(directory, "shim-config.json"), "--report", path.join(directory, "report"),
  ]);
  const cli = await startCli(options, { input, output, errorOutput });
  t.after(async () => { try { await cli.close(); } catch { /* preserve test assertion result */ } });
  await waitFor(() => cli.broker.status().stopped === true, 3000);
  assert.equal(cli.broker.status().stopped, true, "the CLI-selected one-second Broker lifetime took effect");
  assert.equal(cli.bridge.status().attached, false, "the lifetime probe does not attach the Kernel listener");
  await cli.close();
});

test("private OneBot identity accepts explicit selfUin or accountUin without echoing identity", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-onebot-identity-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "onebot.json");
  fs.writeFileSync(file, JSON.stringify({ selfUid: "private-onebot-uid", accountUin: "70001", accessToken: "never-read-out" }));
  assert.deepEqual(readPrivateOneBotIdentityFile(file), { selfUid: "private-onebot-uid", selfUin: "70001" });
  fs.writeFileSync(file, JSON.stringify({ selfUid: "private-onebot-uid", selfUin: "70001", accountUin: "70002" }));
  assert.throws(() => readPrivateOneBotIdentityFile(file), { code: "invalid_onebot_identity_file" });
});

test("Kernel peer identity file is exact, bounded, and bound to account, process, and generation", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-kernel-peer-map-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "peer-identities.json");
  const context = { accountUin: "70001", targetPid: 123, connectionGeneration: 10 };
  const uid = "u_ABCDEFGHIJKLMNOPQRSTUV";
  const valid = {
    accountUin: context.accountUin,
    targetPid: context.targetPid,
    connectionGeneration: context.connectionGeneration,
    peerUidsByUin: { "70002": uid },
  };
  const prototypeKeyMapping = JSON.parse(`{"70002":"${uid}","__proto__":"${uid}"}`);
  fs.writeFileSync(file, JSON.stringify(valid));
  const result = readKernelPeerIdentitiesFile(file, context);
  assert.equal(Object.getPrototypeOf(result), null);
  assert.equal(Object.isFrozen(result), true);
  assert.equal(result["70002"], uid);
  assert.equal(JSON.stringify(result).includes("accountUin"), false);

  for (const [document, expectedCode] of [
    [{ ...valid, accountUin: "70003" }, "kernel_peer_identities_binding_mismatch"],
    [{ ...valid, targetPid: 124 }, "kernel_peer_identities_binding_mismatch"],
    [{ ...valid, connectionGeneration: 11 }, "kernel_peer_identities_binding_mismatch"],
    [{ ...valid, extra: true }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, targetPid: "123" }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, connectionGeneration: true }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, accountUin: "070001" }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, peerUidsByUin: [uid] }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, peerUidsByUin: { "070002": uid } }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, peerUidsByUin: { "70002": "not-a-qq-uid" } }, "invalid_kernel_peer_identities_file"],
    [{ ...valid, peerUidsByUin: prototypeKeyMapping }, "invalid_kernel_peer_identities_file"],
  ]) {
    fs.writeFileSync(file, JSON.stringify(document));
    assert.throws(() => readKernelPeerIdentitiesFile(file, context), { code: expectedCode });
  }

  const tooMany = Object.fromEntries(Array.from({ length: 65 }, (_unused, index) => [String(index + 1), uid]));
  fs.writeFileSync(file, JSON.stringify({ ...valid, peerUidsByUin: tooMany }));
  assert.throws(() => readKernelPeerIdentitiesFile(file, context), { code: "invalid_kernel_peer_identities_file" });
  fs.writeFileSync(file, "{");
  assert.throws(() => readKernelPeerIdentitiesFile(file, context), { code: "invalid_kernel_peer_identities_file" });
  assert.throws(() => readKernelPeerIdentitiesFile(path.join(directory, "missing.json"), context), { code: "kernel_peer_identities_unavailable" });
});

test("identity command obtains accountPath through authenticated private pipe RPC and writes a private login file", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for this CLI protocol test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-identity-command-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const tokenFile = path.join(directory, "host-sidecar.json");
  const onebotFile = path.join(directory, "onebot-private.json");
  const outputFile = path.join(directory, "login-private.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN, ignoredPath: "must-not-leak" }));
  fs.writeFileSync(onebotFile, JSON.stringify({ selfUid: "secret-uid", accountUin: "70001", token: "never-print-this" }));
  const input = new PassThrough();
  let stdout = "";
  let stderr = "";
  const output = new Writable({ write(chunk, _encoding, callback) { stdout += chunk.toString("utf8"); callback(); } });
  const errorOutput = new Writable({ write(chunk, _encoding, callback) { stderr += chunk.toString("utf8"); callback(); } });
  const options = parseArgs([
    "--pid", String(process.pid), "--app-path", process.cwd(), "--token-file", tokenFile,
    "--host-port", String(await reservePort()), "--output-port", String(await reservePort()),
    "--host-generation", "911", "--connection-generation", "912",
    "--config", path.join(directory, "shim-config.json"), "--report", path.join(directory, "report"),
  ]);
  const cli = await startCli(options, { input, output, errorOutput });
  t.after(async () => { try { await cli.close(); } catch { /* preserve test assertion result */ } });
  const client = createPipeClient({ pipeName: cli.broker.pipeName, connectionGeneration: 912, ipcToken: TOKEN });
  await client.connect();
  client.setAgent({
    async getAccountPath({ uin, generation }) {
      assert.equal(uin, "70001");
      assert.equal(generation, 912);
      return { ok: true, accountPath: "C:\\private\\account path" };
    },
    async detach() { return { ok: true, status: { listenerRegistered: false } }; },
    status() { return { listenerRegistered: false }; },
  });
  try {
    await cli.broker.statusRequest();
    await cli.handleCommand(`identity "${onebotFile}" "${outputFile}"`);
    assert.deepEqual(JSON.parse(fs.readFileSync(outputFile, "utf8")), {
      selfUid: "secret-uid", selfUin: "70001", accountPath: "C:\\private\\account path",
    });
    assert.match(stdout, /"command":"identity","result":\{"written":true\}/);
    assert.doesNotMatch(stdout, /secret-uid|70001|account path|never-print-this|must-not-leak|ipcToken/);
    assert.equal(stderr, "");
    await assert.rejects(cli.broker.getAccountPath("070001"), { code: "invalid_account_uin" });
  } finally {
    await cli.close();
    await client.close().catch(() => {});
  }
});

test("private token reader accepts a host sidecar ipcToken without logging or returning other fields", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-private-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const hostSidecar = path.join(directory, "host-sidecar.json");
  fs.writeFileSync(hostSidecar, JSON.stringify({ ipcToken: TOKEN, accountPath: "never-return-this" }));
  assert.equal(readPrivateToken(hostSidecar), TOKEN);
  const identityFile = path.join(directory, "identity.json");
  fs.writeFileSync(identityFile, JSON.stringify({ selfUid: "private-uid", selfUin: "private-uin", accountPath: "private-account-path", ignored: true }));
  assert.deepEqual(readIdentityFile(identityFile), {
    selfUid: "private-uid", selfUin: "private-uin", accountPath: "private-account-path",
  });
  assert.throws(() => readIdentityFile(path.join(directory, "missing.json")), { code: "ENOENT" });
});

test("CLI starts local control only, unknown input is redacted, and quit confirms a clean no-listener shutdown", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for the CLI smoke test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-smoke-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const tokenFile = path.join(directory, "private-host-sidecar.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  const input = new PassThrough();
  let stdout = "";
  let stderr = "";
  const output = new Writable({ write(chunk, _encoding, callback) { stdout += chunk.toString("utf8"); callback(); } });
  const errorOutput = new Writable({ write(chunk, _encoding, callback) { stderr += chunk.toString("utf8"); callback(); } });
  const options = parseArgs([
    "--pid", String(process.pid),
    "--app-path", process.cwd(),
    "--token-file", tokenFile,
    "--host-port", String(await reservePort()),
    "--output-port", String(await reservePort()),
    "--host-generation", "901",
    "--connection-generation", "902",
    "--config", path.join(directory, "nativeconfig.json"),
    "--report", path.join(directory, "agent-report"),
  ]);
  const cli = await startCli(options, { input, output, errorOutput });
  t.after(async () => { if (!cli.bridge.status().stopped) { try { await cli.close(); } catch { /* smoke cleanup */ } } });
  input.write(`${TOKEN} secret-looking-command\nquit\n`);
  await waitFor(() => cli.interface.closed === true);
  await waitFor(() => cli.bridge.status().stopped === true);
  assert.match(stdout, /"kind":"bridge_ready"/);
  assert.match(stdout, /"command":"unknown","code":"unknown_command"/);
  assert.match(stdout, /"command":"quit","result":\{"stopped":true,"detachedConfirmed":true\}/);
  assert.doesNotMatch(stdout, new RegExp(TOKEN));
  assert.doesNotMatch(stdout, /secret-looking-command|private-uid|private-uin|private-account-path/);
  assert.equal(stderr, "");
  assert.deepEqual(await cli.close(), { stopped: true, detachedConfirmed: true });
  assert.deepEqual(await cli.close(), { stopped: true, detachedConfirmed: true });
});

test("EOF detach failure closes local resources but repeated close preserves unconfirmed status", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for the CLI smoke test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-cli-eof-failure-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const tokenFile = path.join(directory, "private-host-sidecar.json");
  fs.writeFileSync(tokenFile, JSON.stringify({ ipcToken: TOKEN }));
  const input = new PassThrough();
  let stdout = "";
  let stderr = "";
  const output = new Writable({ write(chunk, _encoding, callback) { stdout += chunk.toString("utf8"); callback(); } });
  const errorOutput = new Writable({ write(chunk, _encoding, callback) { stderr += chunk.toString("utf8"); callback(); } });
  const options = parseArgs([
    "--pid", String(process.pid),
    "--app-path", process.cwd(),
    "--token-file", tokenFile,
    "--host-port", String(await reservePort()),
    "--output-port", String(await reservePort()),
    "--host-generation", "911",
    "--connection-generation", "912",
    "--config", path.join(directory, "nativeconfig.json"),
    "--report", path.join(directory, "agent-report"),
  ]);
  const signalListenersBefore = {
    sigint: process.listenerCount("SIGINT"),
    sigterm: process.listenerCount("SIGTERM"),
  };
  const cli = await startCli(options, { input, output, errorOutput });
  let detachCalls = 0;
  const fakeAgent = {
    async attach() { return { ok: true, status: { listenerRegistered: true, state: "attached" } }; },
    async detach() {
      detachCalls += 1;
      return { ok: false, code: "listener_removal_failed", status: { listenerRegistered: true, state: "attached" } };
    },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true, status: { listenerRegistered: true } }; },
    status() { return { listenerRegistered: true, state: "attached" }; },
  };
  const pipeClient = createPipeClient({
    pipeName: cli.broker.pipeName,
    connectionGeneration: options.connectionGeneration,
    ipcToken: TOKEN,
  });
  pipeClient.setAgent(fakeAgent);
  await pipeClient.connect();
  t.after(async () => {
    if (!input.destroyed) input.end();
    try { await cli.close(); } catch { /* preserve the test result */ }
    try { await pipeClient.close(); } catch { /* local pipe cleanup */ }
  });

  input.write("attach\n");
  await waitFor(() => cli.bridge.status().attached === true);
  input.end();
  await waitFor(() => cli.broker.status().stopped === true);
  await waitFor(() => cli.host.status().stopped === true);
  await waitFor(() => stderr.includes("shutdown_unconfirmed"));

  assert.equal(detachCalls, 1);
  assert.equal(cli.bridge.status().stopped, false, "failed detach did not make the Bridge clean");
  assert.deepEqual(await cli.close(), { stopped: true, detachedConfirmed: false });
  assert.deepEqual(await cli.close(), { stopped: true, detachedConfirmed: false });
  assert.match(stderr, /"kind":"shutdown_unconfirmed","detachedConfirmed":false,"code":"listener_removal_failed"/);
  assert.equal(cli.broker.status().stopped, true, "the local Broker closed after EOF cleanup");
  assert.equal(cli.host.status().stopped, true, "the local Host client closed after EOF cleanup");
  assert.equal(process.listenerCount("SIGINT"), signalListenersBefore.sigint, "EOF removes the CLI SIGINT handler");
  assert.equal(process.listenerCount("SIGTERM"), signalListenersBefore.sigterm, "EOF removes the CLI SIGTERM handler");
  assert.doesNotMatch(stdout, /detachedConfirmed":true/);
});

test("renderer probe CLI mode is strict and rejects ordinary startup, identity, and service options", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-probe-args-"));
  try {
    const base = [
      "--probe-renderer-capabilities",
      "--pid", "123",
      "--app-path", process.cwd(),
      "--token-file", path.join(directory, "token.json"),
      "--config", path.join(directory, "new-config.json"),
      "--connection-generation", "991",
      "--kernel-creation-time-100ns", "134000000000000000",
      "--probe-python", process.execPath,
    ];
    const parsed = parseArgs(base);
    assert.equal(parsed.probeRendererCapabilities, true);
    assert.equal(parsed.targetPid, 123);
    assert.equal(parsed.connectionGeneration, 991);
    for (const extra of [
      ["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"],
      ["--wrapper", path.join(directory, "wrapper.node")], ["--session", "nt_2"],
      ["--control-port", "0"], ["--login-file", "identity.json"],
      ["--audio-route-config", "route.json"], ["--report", "report"],
    ]) {
      assert.throws(() => parseArgs([...base, ...extra]), { code: "renderer_probe_option_forbidden" });
    }
    assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)),
      { code: "missing_cli_option" });
    const ordinary = [
      "--pid", "123", "--app-path", process.cwd(), "--token-file", "token",
      "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
      "--probe-python", process.execPath,
    ];
    assert.throws(() => parseArgs(ordinary), { code: "probe_python_requires_probe_mode" });
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test("renderer admission parser accepts one LF or CRLF JSON line from a bounded child", async () => {
  const fingerprint = "a".repeat(64);
  const response = { schema: 1, ok: true, code: null, manifestAgeMs: 500, admissionFingerprint: fingerprint };
  for (const ending of ["\n", "\r\n"]) {
    let inputText = "";
    const spawnProcess = (executable, args, spawnOptions) => {
      assert.equal(executable, process.execPath);
      assert.deepEqual(args, ["fixed-helper.py"]);
      assert.equal(spawnOptions.shell, false);
      assert.equal(spawnOptions.windowsHide, true);
      const child = new (require("node:events").EventEmitter)();
      child.stdin = new PassThrough();
      child.stdout = new PassThrough();
      child.kill = () => {};
      child.stdin.on("data", (chunk) => { inputText += chunk.toString("utf8"); });
      child.stdin.once("finish", () => {
        child.stdout.end(`${JSON.stringify(response)}${ending}`, "utf8", () => {
          setImmediate(() => child.emit("close", 0));
        });
      });
      return child;
    };
    const request = { schema: 1, targetPid: 123, fingerprintKey: "b".repeat(64) };
    const result = await runRendererAdmissionProcess(process.execPath, request, {
      spawnProcess,
      helperPath: "fixed-helper.py",
      timeoutMs: 1000,
    });
    assert.deepEqual(result, response);
    assert.deepEqual(JSON.parse(inputText), request);
  }
});

test("renderer admission validation exposes only exact helper fields and fixed error codes", () => {
  assert.throws(() => validateRendererAdmissionResult({ schema: 1, ok: false, code: "target_mismatch" }),
    { code: "renderer_probe_admission_denied" });
  for (const manifestAgeMs of [-5_000, 10_000]) {
    assert.equal(validateRendererAdmissionResult({
      schema: 1, ok: true, code: null, manifestAgeMs, admissionFingerprint: "a".repeat(64),
    }).manifestAgeMs, manifestAgeMs);
  }
  for (const value of [
    { schema: 1, ok: true, code: null, manifestAgeMs: -5_001, admissionFingerprint: "a".repeat(64) },
    { schema: 1, ok: true, code: null, manifestAgeMs: 10_001, admissionFingerprint: "a".repeat(64) },
    { schema: 1, ok: true, code: null, manifestAgeMs: true, admissionFingerprint: "a".repeat(64) },
    { schema: 1, ok: true, code: null, manifestAgeMs: 0.5, admissionFingerprint: "a".repeat(64) },
    { schema: 1, ok: false, code: "account 70001 secret" },
    { schema: 1, ok: false, code: new String("target_mismatch") },
    { schema: 1, ok: true, code: null, manifestAgeMs: 500, admissionFingerprint: "a".repeat(64), extra: "private" },
  ]) {
    assert.throws(() => validateRendererAdmissionResult(value), { code: "renderer_probe_admission_invalid" });
  }
});

function fakeRendererProbeDependencies({ options, directory, runAdmission, probeCalls, outputText }) {
  const steps = [];
  let brokerStarted = false;
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false, "the new injectable config is absent before the Broker starts");
    assert.equal(brokerOptions.maxLifetimeMs, 150_000, "bounded lifetime covers bootstrap and both later admission gates");
    return {
      // The public config getter intentionally redacts the IPC token.
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); return {}; },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => { steps.push("unsubscribe"); };
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async probeRendererCapabilities(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        probeCalls.count += 1;
        steps.push("probe_dispatch");
        return FIXED_CAPABILITY_REPORT;
      },
      async stop() { steps.push("broker_stop"); return { stopped: true }; },
    };
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      steps.push("admission");
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      const count = steps.filter((step) => step === "admission").length;
      if (count === 1) {
        assert.equal(brokerStarted, false, "canonical adapter admission precedes any Broker startup");
        assert.equal(fs.existsSync(options.configPath), false, "admission precedes writing the injectable config");
      } else if (count === 2) {
        assert.equal(brokerStarted, true);
        assert.equal(probeCalls.count, 0, "the second admission runs immediately before dispatch");
      } else if (count === 3) {
        assert.equal(probeCalls.count, 1, "the third admission runs after the response and before persistence");
      }
      return runAdmission(request, count);
    },
    brokerFactory,
    createRunDirectory: () => path.join(directory, "fresh-probe-run"),
  };
  return { dependencies, steps };
}

test("renderer capability CLI admits before startup, checks the same identity three times, and writes only fixed metadata", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-probe-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = rendererProbeOptions(directory);
  const probeCalls = { count: 0 };
  const outputText = { value: "" };
  const fingerprint = "c".repeat(64);
  const keys = [];
  const { dependencies, steps } = fakeRendererProbeDependencies({
    options,
    directory,
    probeCalls,
    outputText,
    runAdmission(request) {
      keys.push(request.fingerprintKey);
      return { schema: 1, ok: true, code: null, manifestAgeMs: 512, admissionFingerprint: fingerprint };
    },
  });
  const result = await startCli(options, { rendererProbeDependencies: dependencies, output: dependencies.output });
  assert.equal(result.probeCompleted, true);
  assert.equal(probeCalls.count, 1);
  assert.equal(keys.length, 3);
  assert.equal(new Set(keys).size, 1, "all gates share one per-run HMAC key");
  assert.ok(steps.indexOf("admission") < steps.indexOf("broker_create"));
  assert.equal(steps.at(-1), "broker_stop");
  const runDirectory = path.join(directory, "fresh-probe-run");
  const saved = fs.readFileSync(path.join(runDirectory, "renderer-capability.private.json"), "utf8");
  assert.equal(JSON.parse(saved).capability.processType, "unknown");
  assert.doesNotMatch(saved, new RegExp(TOKEN));
  assert.doesNotMatch(saved, new RegExp(fingerprint));
  assert.doesNotMatch(saved, new RegExp(keys[0]));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.doesNotMatch(outputText.value, new RegExp(keys[0]));
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1, connectionGeneration: options.connectionGeneration, ipcToken: TOKEN,
  });
});

test("renderer probe rejects initial or changed admission without dispatching or persisting a report", async (t) => {
  for (const failedAt of ["initial", "before_dispatch", "before_persist"]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-probe-gate-"));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = rendererProbeOptions(directory);
    const probeCalls = { count: 0 };
    const outputText = { value: "" };
    const firstFingerprint = "d".repeat(64);
    const changedFingerprint = "e".repeat(64);
    let admissionCalls = 0;
    let brokerCreates = 0;
    const base = fakeRendererProbeDependencies({
      options,
      directory,
      probeCalls,
      outputText,
      runAdmission(_request, count) {
        admissionCalls = count;
        if (failedAt === "initial") return { schema: 1, ok: false, code: "target_mismatch" };
        const changedCount = failedAt === "before_dispatch" ? 2 : 3;
        return {
          schema: 1,
          ok: true,
          code: null,
          manifestAgeMs: 700,
          admissionFingerprint: count === changedCount ? changedFingerprint : firstFingerprint,
        };
      },
    });
    const originalBrokerFactory = base.dependencies.brokerFactory;
    base.dependencies.brokerFactory = (...args) => { brokerCreates += 1; return originalBrokerFactory(...args); };
    if (failedAt === "initial") {
      await assert.rejects(startRendererCapabilityProbe(options, base.dependencies), { code: "renderer_probe_admission_denied" });
      assert.equal(brokerCreates, 0);
      assert.equal(fs.existsSync(options.configPath), false);
      assert.equal(fs.existsSync(path.join(directory, "fresh-probe-run")), false);
      assert.equal(probeCalls.count, 0);
    } else {
      await assert.rejects(startRendererCapabilityProbe(options, base.dependencies), { code: "renderer_probe_admission_changed" });
      assert.equal(brokerCreates, 1);
      assert.equal(admissionCalls, failedAt === "before_dispatch" ? 2 : 3);
      assert.equal(probeCalls.count, failedAt === "before_dispatch" ? 0 : 1);
      assert.equal(fs.existsSync(path.join(directory, "fresh-probe-run", "renderer-capability.private.json")), false);
    }
    assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  }
});

test("ASAR source probe CLI mode is exclusive and rejects service, host, control, and identity options", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-probe-args-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const base = [
    "--probe-asar-sources",
    "--pid", "123",
    "--app-path", process.cwd(),
    "--token-file", path.join(directory, "token.json"),
    "--config", path.join(directory, "new-config.json"),
    "--connection-generation", "992",
    "--kernel-creation-time-100ns", "134000000000000000",
    "--probe-python", process.execPath,
  ];
  const parsed = parseArgs(base);
  assert.equal(parsed.probeAsarSources, true);
  assert.equal(parsed.targetPid, 123);
  assert.equal(parsed.connectionGeneration, 992);
  for (const extra of [
    ["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"],
    ["--wrapper", path.join(directory, "wrapper.node")], ["--session", "nt_2"],
    ["--control-port", "0"], ["--login-file", "identity.json"],
    ["--audio-route-config", "route.json"], ["--report", "report"],
  ]) {
    assert.throws(() => parseArgs([...base, ...extra]), { code: "asar_source_probe_option_forbidden" });
  }
  assert.throws(() => parseArgs([...base, "--probe-renderer-capabilities"]));
  assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)),
    { code: "missing_cli_option" });
});

function fakeAsarSourceProbeDependencies({ options, directory, runAdmission, probeCalls, outputText }) {
  const steps = [];
  let brokerStarted = false;
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false, "the private injector config is absent before admission");
    assert.equal(brokerOptions.maxLifetimeMs, 150_000);
    return {
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); return {}; },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => steps.push("unsubscribe");
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async probeAsarSources(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        probeCalls.count += 1;
        steps.push("probe_dispatch");
        return FIXED_ASAR_SOURCE_REPORT;
      },
      async stop() { steps.push("broker_stop"); return { stopped: true }; },
    };
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      steps.push("admission");
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      const count = steps.filter((step) => step === "admission").length;
      if (count === 1) {
        assert.equal(brokerStarted, false, "canonical adapter admission precedes all broker work");
        assert.equal(fs.existsSync(options.configPath), false);
      } else if (count === 2) {
        assert.equal(brokerStarted, true);
        assert.equal(probeCalls.count, 0, "the second admission precedes source reads");
      } else if (count === 3) {
        assert.equal(probeCalls.count, 1, "the third admission gates report persistence");
      }
      return runAdmission(request, count);
    },
    brokerFactory,
    createRunDirectory: () => path.join(directory, "fresh-asar-source-run"),
  };
  return { dependencies, steps };
}

test("ASAR source CLI performs three stable admissions and keeps source bytes private", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-probe-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = asarSourceProbeOptions(directory);
  const probeCalls = { count: 0 };
  const outputText = { value: "" };
  const fingerprint = "f".repeat(64);
  const keys = [];
  const { dependencies, steps } = fakeAsarSourceProbeDependencies({
    options,
    directory,
    probeCalls,
    outputText,
    runAdmission(request) {
      keys.push(request.fingerprintKey);
      return { schema: 1, ok: true, code: null, manifestAgeMs: 730, admissionFingerprint: fingerprint };
    },
  });
  const result = await startCli(options, { asarSourceProbeDependencies: dependencies, output: dependencies.output });
  assert.equal(result.asarSourceProbeCompleted, true);
  assert.equal(probeCalls.count, 1);
  assert.equal(keys.length, 3);
  assert.equal(new Set(keys).size, 1);
  assert.ok(steps.indexOf("admission") < steps.indexOf("broker_create"));
  assert.equal(steps.at(-1), "broker_stop");
  const runDirectory = path.join(directory, "fresh-asar-source-run");
  const saved = fs.readFileSync(path.join(runDirectory, "asar-sources.private.json"), "utf8");
  assert.match(saved, new RegExp(ASAR_SOURCE_SENTINEL));
  assert.doesNotMatch(saved, new RegExp(TOKEN));
  assert.doesNotMatch(saved, new RegExp(fingerprint));
  assert.doesNotMatch(saved, new RegExp(keys[0]));
  assert.doesNotMatch(outputText.value, new RegExp(ASAR_SOURCE_SENTINEL));
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.doesNotMatch(outputText.value, new RegExp(keys[0]));
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1, connectionGeneration: options.connectionGeneration, ipcToken: TOKEN,
  });
});

test("ASAR source CLI denies changed admission before reading or persisting", async (t) => {
  for (const failedAt of ["initial", "before_dispatch", "before_persist"]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-probe-gate-"));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = asarSourceProbeOptions(directory);
    const probeCalls = { count: 0 };
    const outputText = { value: "" };
    const firstFingerprint = "1".repeat(64);
    const changedFingerprint = "2".repeat(64);
    let admissionCalls = 0;
    let brokerCreates = 0;
    const base = fakeAsarSourceProbeDependencies({
      options,
      directory,
      probeCalls,
      outputText,
      runAdmission(_request, count) {
        admissionCalls = count;
        if (failedAt === "initial") return { schema: 1, ok: false, code: "target_mismatch" };
        const changedCount = failedAt === "before_dispatch" ? 2 : 3;
        return {
          schema: 1,
          ok: true,
          code: null,
          manifestAgeMs: 400,
          admissionFingerprint: count === changedCount ? changedFingerprint : firstFingerprint,
        };
      },
    });
    const originalBrokerFactory = base.dependencies.brokerFactory;
    base.dependencies.brokerFactory = (...args) => { brokerCreates += 1; return originalBrokerFactory(...args); };
    if (failedAt === "initial") {
      await assert.rejects(startAsarSourceProbe(options, base.dependencies), { code: "renderer_probe_admission_denied" });
      assert.equal(brokerCreates, 0);
      assert.equal(fs.existsSync(options.configPath), false);
      assert.equal(fs.existsSync(path.join(directory, "fresh-asar-source-run")), false);
      assert.equal(probeCalls.count, 0);
    } else {
      await assert.rejects(startAsarSourceProbe(options, base.dependencies), { code: "asar_source_probe_admission_changed" });
      assert.equal(brokerCreates, 1);
      assert.equal(admissionCalls, failedAt === "before_dispatch" ? 2 : 3);
      assert.equal(probeCalls.count, failedAt === "before_dispatch" ? 0 : 1);
      assert.equal(fs.existsSync(path.join(directory, "fresh-asar-source-run", "asar-sources.private.json")), false);
    }
    assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
    assert.doesNotMatch(outputText.value, new RegExp(ASAR_SOURCE_SENTINEL));
  }
});

function fixedAsarKeeperFrame(options, state = "final") {
  return {
    schema: "asar.physical.keeper.v1",
    nonce: "9a".repeat(32),
    state,
    keeperPid: 9001,
    keeperCreationTime100ns: "134000000000000010",
    kernelPid: options.targetPid,
    kernelCreationTime100ns: options.kernelCreationTime100ns,
    connectionGeneration: options.connectionGeneration,
    appPath: options.appPath,
    files: [
      { name: "application.asar", nativeVolumeSerial: "12", nativeFileId: "A".repeat(32), canonicalFinalPath: path.win32.join(options.appPath, "application.asar"), size: 1000, lastWriteTime100ns: "134000000000000001", sha256: PINNED_HASHES.archive },
      { name: "package.json", nativeVolumeSerial: "12", nativeFileId: "B".repeat(32), canonicalFinalPath: path.win32.join(options.appPath, "package.json"), size: 100, lastWriteTime100ns: "134000000000000002", sha256: PINNED_HASHES.packageJson },
      { name: "wrapper.node", nativeVolumeSerial: "12", nativeFileId: "C".repeat(32), canonicalFinalPath: path.win32.join(options.appPath, "wrapper.node"), size: 2000, lastWriteTime100ns: "134000000000000003", sha256: PINNED_HASHES.wrapper },
    ],
  };
}

function fakeAsarCandidateProbeDependencies({ options, directory, runAdmission, outputText, keeperFailureAt = null } = {}) {
  const steps = [];
  let brokerStarted = false;
  let keeperStarted = false;
  let keeperFinished = false;
  let probeCount = 0;
  let admissionCount = 0;
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false);
    assert.equal(brokerOptions.maxLifetimeMs, 150_000);
    const broker = {
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => steps.push("unsubscribe");
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async probeAsarCandidates(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        assert.equal(keeperStarted, true, "native identity handles are acquired before the one-shot RPC");
        probeCount += 1;
        steps.push("candidate_rpc");
        return FIXED_ASAR_CANDIDATE_REPORT;
      },
      async stop() { steps.push("broker_stop"); return { stopped: true }; },
    };
    brokerOptions.onEvent("candidate_source", FIXED_ASAR_CANDIDATE_REPORT);
    return broker;
  };
  const keeperFactory = async (keeperOptions) => {
    assert.equal(brokerStarted, true, "keeper starts only after authenticated bootstrap");
    assert.deepEqual(keeperOptions, {
      kernelPid: options.targetPid,
      kernelCreationTime100ns: options.kernelCreationTime100ns,
      connectionGeneration: options.connectionGeneration,
      appPath: options.appPath,
      probePythonPath: options.probePythonPath,
    });
    keeperStarted = true;
    steps.push("keeper_start");
    return {
      async finish() {
        if (keeperFailureAt === "finish") throw Object.assign(new Error("private keeper detail"), { code: "keeper_frame_invalid" });
        keeperFinished = true;
        steps.push("keeper_finish");
        return fixedAsarKeeperFrame(options);
      },
      async cancel() { steps.push("keeper_cancel"); },
    };
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      admissionCount += 1;
      steps.push(`admission_${admissionCount}`);
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      if (admissionCount === 1) {
        assert.equal(brokerStarted, false, "the initial admission precedes broker bootstrap");
        assert.equal(fs.existsSync(options.configPath), false);
      } else if (admissionCount === 2) {
        assert.equal(brokerStarted, true, "the second admission follows bootstrap readiness");
        assert.equal(keeperStarted, false, "keeper start follows the pre-RPC admission");
        assert.equal(probeCount, 0);
      } else if (admissionCount === 3) {
        assert.equal(probeCount, 1);
        assert.equal(keeperFinished, true, "persistence admission follows helper finish and process closure");
      }
      return runAdmission(request, admissionCount);
    },
    brokerFactory,
    keeperFactory,
    createRunDirectory: () => path.join(directory, "fresh-asar-candidate-run"),
  };
  return { dependencies, steps, get admissionCount() { return admissionCount; }, get probeCount() { return probeCount; }, get keeperStarted() { return keeperStarted; } };
}

function fakeOfficialWindowProbeDependencies({ options, directory, runAdmission, outputText } = {}) {
  const steps = [];
  let brokerStarted = false;
  let probeCount = 0;
  let admissionCount = 0;
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false);
    assert.equal(brokerOptions.maxLifetimeMs, 150_000);
    assert.equal(brokerOptions.targetPid, options.targetPid);
    assert.equal(brokerOptions.connectionGeneration, options.connectionGeneration);
    assert.equal(Object.hasOwn(brokerOptions, "controlPort"), false);
    assert.equal(Object.hasOwn(brokerOptions, "hostPort"), false);
    const broker = {
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => steps.push("unsubscribe");
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async probeOfficialWindows(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        probeCount += 1;
        steps.push("official_window_rpc");
        return FIXED_OFFICIAL_WINDOW_REPORT;
      },
      async stop() { steps.push("broker_stop"); return { stopped: true }; },
    };
    brokerOptions.onEvent("private_event", { content: "must_not_be_forwarded" });
    return broker;
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      admissionCount += 1;
      steps.push(`admission_${admissionCount}`);
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      if (admissionCount === 1) {
        assert.equal(brokerStarted, false, "the initial admission precedes broker startup");
        assert.equal(fs.existsSync(options.configPath), false);
      } else if (admissionCount === 2) {
        assert.equal(brokerStarted, true, "the second admission follows authenticated bootstrap readiness");
        assert.equal(probeCount, 0, "the second admission gates RPC dispatch");
      } else if (admissionCount === 3) {
        assert.equal(probeCount, 1, "the last admission gates artifact persistence");
      }
      return runAdmission(request, admissionCount);
    },
    brokerFactory,
    createRunDirectory: () => path.join(directory, "fresh-official-window-run"),
  };
  return { dependencies, steps, get admissionCount() { return admissionCount; }, get probeCount() { return probeCount; } };
}

function fakeRendererScriptMetadataProbeDependencies({ options, directory, runAdmission, outputText, report = FIXED_RENDERER_SCRIPT_METADATA_REPORT } = {}) {
  const steps = [];
  let brokerStarted = false;
  let probeCount = 0;
  let admissionCount = 0;
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false);
    assert.equal(brokerOptions.maxLifetimeMs, 150_000);
    assert.equal(brokerOptions.targetPid, options.targetPid);
    assert.equal(brokerOptions.connectionGeneration, options.connectionGeneration);
    assert.equal(brokerOptions.appPath, ASAR_CANDIDATE_APP_PATH);
    assert.equal(Object.hasOwn(brokerOptions, "controlPort"), false);
    assert.equal(Object.hasOwn(brokerOptions, "hostPort"), false);
    const broker = {
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => steps.push("unsubscribe");
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async probeRendererScriptMetadata(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        probeCount += 1;
        steps.push("renderer_script_metadata_rpc");
        return report;
      },
      async stop() { steps.push("broker_stop"); return { stopped: true }; },
    };
    brokerOptions.onEvent("private_event", { content: "must_not_be_forwarded" });
    return broker;
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      admissionCount += 1;
      steps.push(`admission_${admissionCount}`);
      assert.equal(Object.isFrozen(request), true, "the HMAC binding object stays immutable across admission checks");
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      if (admissionCount === 1) {
        assert.equal(brokerStarted, false, "initial admission precedes broker startup");
        assert.equal(fs.existsSync(options.configPath), false);
      } else if (admissionCount === 2) {
        assert.equal(brokerStarted, true, "dispatch admission follows authenticated bootstrap readiness");
        assert.equal(probeCount, 0, "second admission gates RPC dispatch");
      } else if (admissionCount === 3) {
        assert.equal(probeCount, 1, "final admission gates report persistence");
      }
      return runAdmission(request, admissionCount);
    },
    brokerFactory,
    createRunDirectory: () => path.join(directory, "fresh-renderer-script-metadata-run"),
  };
  return { dependencies, steps, get admissionCount() { return admissionCount; }, get probeCount() { return probeCount; } };
}

function fakeOfficialAVObserverDependencies({ options, directory, runAdmission, outputText } = {}) {
  const steps = [];
  let brokerStarted = false;
  let observerStartCount = 0;
  let observerDrainCount = 0;
  let observerStopCount = 0;
  let observerStartOptions = null;
  let admissionCount = 0;
  let brokerStopped = false;
  const observerWindowStatus = options.officialAVIncomingOnly ? "armed_incoming_only" :
    options.officialAVNativeConditional ? "armed_native_conditional" : "armed";
  const brokerFactory = (brokerOptions) => {
    steps.push("broker_create");
    assert.equal(fs.existsSync(options.configPath), false);
    assert.equal(brokerOptions.maxLifetimeMs, 240000);
    assert.equal(brokerOptions.targetPid, options.targetPid);
    assert.equal(brokerOptions.connectionGeneration, options.connectionGeneration);
    assert.equal(Object.hasOwn(brokerOptions, "controlPort"), false);
    assert.equal(Object.hasOwn(brokerOptions, "hostPort"), false);
    const broker = {
      config: { schema: 1, connectionGeneration: options.connectionGeneration },
      async start() { brokerStarted = true; steps.push("broker_start"); },
      status() { return { authenticated: true }; },
      subscribe(handler) {
        queueMicrotask(() => handler("bootstrap_status", {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        }));
        return () => steps.push("unsubscribe");
      },
      async statusRequest() {
        return { ok: true, bootstrap: {
          bootstrapLoaded: true,
          processPid: options.targetPid,
          connectionGeneration: options.connectionGeneration,
        } };
      },
      async startOfficialAVObserver(timeoutMs, startOptions) {
        assert.equal(timeoutMs, 5000);
        observerStartOptions = startOptions;
        observerStartCount += 1;
        steps.push("observer_start");
        return fixedOfficialAVReport("active", { windowStatus: observerWindowStatus,
          nativeConditionalStatus: options.officialAVNativeConditional ? "breakpoint_set" : null });
      },
      async drainOfficialAVObserver(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        observerDrainCount += 1;
        steps.push("observer_drain");
        return fixedOfficialAVReport("active", { includeEvent: !options.officialAVIncomingOnly,
          windowStatus: observerWindowStatus, nativeConditionalStatus: options.officialAVNativeConditional ? "breakpoint_set" : null });
      },
      async stopOfficialAVObserver(timeoutMs) {
        assert.equal(timeoutMs, 5000);
        observerStopCount += 1;
        steps.push("observer_stop");
        return fixedOfficialAVReport("stopped", { includeEvent: !options.officialAVIncomingOnly,
          windowStatus: options.officialAVIncomingOnly ? "destroyed" : null });
      },
      async stop() { brokerStopped = true; steps.push("broker_stop"); return { stopped: true }; },
    };
    brokerOptions.onEvent("private_event", { content: "must_not_be_forwarded" });
    return broker;
  };
  const output = new Writable({ write(chunk, _encoding, callback) { outputText.value += chunk.toString("utf8"); callback(); } });
  const dependencies = {
    output,
    runAdmission: async (request) => {
      admissionCount += 1;
      steps.push(`admission_${admissionCount}`);
      assert.match(request.fingerprintKey, /^[a-f0-9]{64}$/);
      if (admissionCount === 1) {
        assert.equal(brokerStarted, false, "initial Snow admission precedes Broker startup");
        assert.equal(fs.existsSync(options.configPath), false);
      } else if (admissionCount === 2) {
        assert.equal(brokerStarted, true, "second Snow admission follows bootstrap readiness");
        assert.equal(observerStartCount, 0, "second admission gates the start RPC");
      } else if (admissionCount === 3) {
        assert.equal(observerStopCount, 1, "the last admission follows observer cleanup");
        assert.equal(brokerStopped, false, "the Broker remains alive through the final admission");
      }
      return runAdmission(request, admissionCount);
    },
    brokerFactory,
    createRunDirectory: () => path.join(directory, "fresh-official-av-observer-run"),
    observeForMs: 0,
    sleep: async () => { throw new Error("zero-duration test must not sleep"); },
  };
  return { dependencies, steps, get admissionCount() { return admissionCount; },
    get observerStartCount() { return observerStartCount; }, get observerDrainCount() { return observerDrainCount; },
    get observerStopCount() { return observerStopCount; }, get observerStartOptions() { return observerStartOptions; },
    get brokerStopped() { return brokerStopped; } };
}

test("official AV observer CLI parser accepts only its exclusive seven-value diagnostic form", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-av-observer-args-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const base = [
    "--observe-official-av", "--pid", "123", "--app-path", ASAR_CANDIDATE_APP_PATH,
    "--token-file", path.join(directory, "token.json"), "--config", path.join(directory, "new-config.json"),
    "--connection-generation", "995", "--kernel-creation-time-100ns", "134000000000000000",
    "--probe-python", process.execPath,
  ];
  const parsed = parseArgs(base);
  assert.equal(parsed.observeOfficialAV, true);
  assert.equal(parsed.officialAVIncomingOnly, false);
  assert.equal(parsed.officialAVNativeConditional, false);
  assert.equal(parsed.targetPid, 123);
  assert.equal(parsed.connectionGeneration, 995);
  assert.equal(parseArgs([...base, "--incoming-only"]).officialAVIncomingOnly, true);
  assert.equal(parseArgs([...base, "--native-conditional"]).officialAVNativeConditional, true);
  assert.throws(() => parseArgs([...base, "--incoming-only", "--native-conditional"]),
    { code: "official_av_observer_modes_mutually_exclusive" });
  const options = officialAVObserverOptions(directory);
  assert.doesNotThrow(() => validateOfficialAVObserverOptions(options), "the prior full-mode options shape remains valid without the new optional key");
  assert.doesNotThrow(() => validateOfficialAVObserverOptions({ ...options, officialAVIncomingOnly: false }),
    "an explicit false value also selects full mode");
  assert.doesNotThrow(() => validateOfficialAVObserverOptions({ ...options, officialAVNativeConditional: false }),
    "an explicit false native-mode value also selects full mode");
  assert.throws(() => validateOfficialAVObserverOptions({ ...options, officialAVIncomingOnly: true, officialAVNativeConditional: true }),
    { code: "invalid_official_av_observer_options" });
  assert.throws(() => validateOfficialAVObserverOptions({ ...options, selector: "document.body" }),
    { code: "invalid_official_av_observer_options" });
  const accessorOptions = { ...options };
  Object.defineProperty(accessorOptions, "officialAVIncomingOnly", { enumerable: true, get() { throw new Error("must not be read"); } });
  assert.throws(() => validateOfficialAVObserverOptions(accessorOptions), { code: "invalid_official_av_observer_options" });
  const nativeAccessorOptions = { ...options };
  Object.defineProperty(nativeAccessorOptions, "officialAVNativeConditional", { enumerable: true, get() { throw new Error("must not be read"); } });
  assert.throws(() => validateOfficialAVObserverOptions(nativeAccessorOptions), { code: "invalid_official_av_observer_options" });
  for (const extra of [["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"], ["--control-port", "0"],
    ["--probe-official-windows"], ["--probe-asar-candidates"], ["--audio-route-config", "route.json"]]) {
    assert.throws(() => parseArgs([...base, ...extra]), (error) => /option_forbidden$/.test(error.code));
  }
  assert.throws(() => parseArgs(["--incoming-only"]), { code: "incoming_only_requires_official_av_observer" });
  assert.throws(() => parseArgs(["--native-conditional"]), { code: "native_conditional_requires_official_av_observer" });
  assert.throws(() => parseArgs([...base, "--incoming-only", "--incoming-only"]), { code: "duplicate_cli_option" });
  assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)), { code: "missing_cli_option" });
});

test("official AV observer CLI keeps private capture out of stdout and gates lifecycle with three admissions", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-av-observer-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = officialAVObserverOptions(directory);
  const outputText = { value: "" };
  const fingerprints = [];
  const fingerprint = "abcdeffedcba0123".repeat(4);
  const fake = fakeOfficialAVObserverDependencies({ options, directory, outputText, runAdmission(request) {
    fingerprints.push(request.fingerprintKey);
    assert.equal(request.appPath, ASAR_CANDIDATE_APP_PATH);
    return { schema: 1, ok: true, code: null, manifestAgeMs: 400, admissionFingerprint: fingerprint };
  } });
  const result = await startCli(options, { officialAVObserverDependencies: fake.dependencies, output: fake.dependencies.output });
  assert.equal(result.officialAVObserverCompleted, true);
  assert.equal(result.proofLevel, "diagnostic_only");
  assert.equal(fake.admissionCount, 3);
  assert.deepEqual(fake.observerStartOptions, {}, "the legacy full mode remains the empty start payload");
  assert.equal(fake.observerStartCount, 1);
  assert.deepEqual(fake.observerStartOptions, {});
  assert.equal(fake.observerDrainCount, 1);
  assert.equal(fake.observerStopCount, 1);
  assert.equal(fake.brokerStopped, true);
  assert.equal(new Set(fingerprints).size, 1);
  assert.ok(fake.steps.indexOf("admission_1") < fake.steps.indexOf("broker_create"));
  assert.ok(fake.steps.indexOf("admission_2") < fake.steps.indexOf("observer_start"));
  assert.ok(fake.steps.indexOf("observer_stop") < fake.steps.indexOf("admission_3"));
  assert.equal(fake.steps.at(-1), "broker_stop");
  const artifactPath = path.join(directory, "fresh-official-av-observer-run", "official-av-observer.private.json");
  const artifactText = fs.readFileSync(artifactPath, "utf8");
  const artifact = JSON.parse(artifactText);
  assert.equal(artifact.proofLevel, "diagnostic_only");
  assert.equal(artifact.report.events[0].command, 4);
  assert.equal(artifact.report.report?.proofLevel, undefined);
  assert.doesNotMatch(artifactText, new RegExp(TOKEN));
  assert.doesNotMatch(artifactText, new RegExp(fingerprint));
  assert.doesNotMatch(outputText.value, /request-secret|relation-secret|room-secret|u_[A-Za-z0-9_-]{22}|C:\\Program|private_event|must_not_be_forwarded/);
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.match(outputText.value, /"armedWindowCount":1/);
  assert.match(outputText.value, /"eventCount":1/);
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1, connectionGeneration: options.connectionGeneration, ipcToken: TOKEN,
  });
});

test("official AV incoming-only CLI forwards the fixed mode and publishes aggregate counts only", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-av-incoming-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = officialAVObserverOptions(directory);
  options.officialAVIncomingOnly = true;
  const outputText = { value: "" };
  const fake = fakeOfficialAVObserverDependencies({ options, directory, outputText, runAdmission() {
    return { schema: 1, ok: true, code: null, manifestAgeMs: 300, admissionFingerprint: "a".repeat(64) };
  } });
  await startCli(options, { officialAVObserverDependencies: fake.dependencies, output: fake.dependencies.output });
  assert.deepEqual(fake.observerStartOptions, { incomingOnly: true });
  assert.match(outputText.value, /"armedWindowCount":1/);
  assert.match(outputText.value, /"incomingOnlyWindowCount":1/);
  assert.doesNotMatch(outputText.value, /"events"|outgoing|request-secret|application\.asar/);
  const artifactPath = path.join(directory, "fresh-official-av-observer-run", "official-av-observer.private.json");
  const report = JSON.parse(fs.readFileSync(artifactPath, "utf8")).report;
  assert.equal(report.windows[0].status, "destroyed", "the final private report records cleanup after the live armed summary");
  assert.equal(report.events.length, 0, "the fixture does not manufacture outgoing observations for incoming-only mode");
});

test("official AV native-conditional CLI forwards the fixed mode and publishes aggregate counts only", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-av-native-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = officialAVObserverOptions(directory);
  options.officialAVNativeConditional = true;
  const outputText = { value: "" };
  const fake = fakeOfficialAVObserverDependencies({ options, directory, outputText, runAdmission() {
    return { schema: 1, ok: true, code: null, manifestAgeMs: 300, admissionFingerprint: "a".repeat(64) };
  } });
  await startCli(options, { officialAVObserverDependencies: fake.dependencies, output: fake.dependencies.output });
  assert.deepEqual(fake.observerStartOptions, { nativeConditional: true });
  assert.match(outputText.value, /"armedWindowCount":1/);
  assert.match(outputText.value, /"nativeConditionalWindowCount":1/);
  assert.doesNotMatch(outputText.value, /"events"|outgoing|request-secret|application\.asar/);
  const artifactPath = path.join(directory, "fresh-official-av-observer-run", "official-av-observer.private.json");
  const artifact = JSON.parse(fs.readFileSync(artifactPath, "utf8"));
  assert.equal(artifact.proofLevel, "diagnostic_only");
  assert.equal(artifact.report.windows[0].status, "destroyed");
  assert.equal(artifact.report.events.length, 1, "the private artifact may contain a bounded projected attempt");
  assert.doesNotMatch(JSON.stringify(artifact), /C:\\Program Files|application\.asar|native-request-secret/);
});

test("official AV observer admission changes block dispatch or private publication", async (t) => {
  for (const changedAt of [2, 3]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `qqcall-official-av-admission-${changedAt}-`));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = officialAVObserverOptions(directory);
    const outputText = { value: "" };
    const fake = fakeOfficialAVObserverDependencies({ options, directory, outputText, runAdmission(_request, count) {
      return { schema: 1, ok: true, code: null, manifestAgeMs: 400,
        admissionFingerprint: count === changedAt ? "2".repeat(64) : "1".repeat(64) };
    } });
    await assert.rejects(startOfficialAVObserver(options, fake.dependencies), { code: "official_av_observer_admission_changed" });
    assert.equal(fake.admissionCount, changedAt);
    assert.equal(fake.observerStartCount, changedAt === 2 ? 0 : 1);
    assert.equal(fake.observerStopCount, changedAt === 2 ? 0 : 1);
    assert.equal(fs.existsSync(path.join(directory, "fresh-official-av-observer-run", "official-av-observer.private.json")), false);
    assert.doesNotMatch(outputText.value, /private_event|must_not_be_forwarded/);
  }
});

test("official-window CLI parser is exclusive and requires the same seven bound values", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-window-args-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const base = [
    "--probe-official-windows",
    "--pid", "123",
    "--app-path", ASAR_CANDIDATE_APP_PATH,
    "--token-file", path.join(directory, "token.json"),
    "--config", path.join(directory, "new-config.json"),
    "--connection-generation", "994",
    "--kernel-creation-time-100ns", "134000000000000000",
    "--probe-python", process.execPath,
  ];
  const parsed = parseArgs(base);
  assert.equal(parsed.probeOfficialWindows, true);
  assert.equal(parsed.targetPid, 123);
  assert.equal(parsed.connectionGeneration, 994);
  assert.equal(parsed.appPath, ASAR_CANDIDATE_APP_PATH);
  for (const extra of [
    ["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"],
    ["--wrapper", path.join(directory, "wrapper.node")], ["--control-port", "0"],
    ["--audio-route-config", "route.json"], ["--probe-asar-candidates"],
  ]) {
    assert.throws(() => parseArgs([...base, ...extra]), (error) => /option_forbidden$/.test(error.code));
  }
  assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)), { code: "missing_cli_option" });
});

test("official-window CLI persists only the diagnostic projection and prints counts/status", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-window-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = officialWindowProbeOptions(directory);
  const outputText = { value: "" };
  const fingerprintKeys = [];
  const fingerprint = "abcdeffedcba0123".repeat(4);
  const fake = fakeOfficialWindowProbeDependencies({
    options,
    directory,
    outputText,
    runAdmission(request) {
      fingerprintKeys.push(request.fingerprintKey);
      assert.equal(request.appPath, ASAR_CANDIDATE_APP_PATH);
      return { schema: 1, ok: true, code: null, manifestAgeMs: 420, admissionFingerprint: fingerprint };
    },
  });
  const result = await startCli(options, {
    officialWindowProbeDependencies: fake.dependencies,
    output: fake.dependencies.output,
  });
  assert.equal(result.officialWindowProbeCompleted, true);
  assert.equal(result.proofLevel, "diagnostic_only");
  assert.equal(fake.probeCount, 1);
  assert.equal(fake.admissionCount, 3);
  assert.equal(new Set(fingerprintKeys).size, 1);
  assert.ok(fake.steps.indexOf("admission_1") < fake.steps.indexOf("broker_create"));
  assert.ok(fake.steps.indexOf("admission_2") < fake.steps.indexOf("official_window_rpc"));
  assert.ok(fake.steps.indexOf("official_window_rpc") < fake.steps.indexOf("admission_3"));
  assert.equal(fake.steps.at(-1), "broker_stop");

  const artifactPath = path.join(directory, "fresh-official-window-run", "official-windows.private.json");
  const savedText = fs.readFileSync(artifactPath, "utf8");
  const saved = JSON.parse(savedText);
  assert.equal(saved.proofLevel, "diagnostic_only");
  assert.equal(saved.report.windows[0].routePath, null);
  assert.equal(saved.report.windows[0].postMessageAvailable, true);
  assert.doesNotMatch(savedText, new RegExp(TOKEN));
  assert.doesNotMatch(savedText, new RegExp(fingerprint));
  assert.doesNotMatch(outputText.value, /official\/group|C:\\Program|private_event|must_not_be_forwarded/);
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.match(outputText.value, /"status":"complete"/);
  assert.match(outputText.value, /"rendererMatchCount":1/);
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1,
    connectionGeneration: options.connectionGeneration,
    ipcToken: TOKEN,
  });
});

test("official-window admission changes prevent dispatch or artifact publication", async (t) => {
  for (const changedAt of [2, 3]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `qqcall-official-window-admission-${changedAt}-`));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = officialWindowProbeOptions(directory);
    const outputText = { value: "" };
    const firstFingerprint = "1".repeat(64);
    const changedFingerprint = "2".repeat(64);
    const fake = fakeOfficialWindowProbeDependencies({
      options,
      directory,
      outputText,
      runAdmission(_request, count) {
        return {
          schema: 1,
          ok: true,
          code: null,
          manifestAgeMs: 400,
          admissionFingerprint: count === changedAt ? changedFingerprint : firstFingerprint,
        };
      },
    });
    await assert.rejects(startOfficialWindowProbe(options, fake.dependencies), { code: "official_window_probe_admission_changed" });
    assert.equal(fake.admissionCount, changedAt);
    assert.equal(fake.probeCount, changedAt === 2 ? 0 : 1);
    assert.equal(fs.existsSync(path.join(directory, "fresh-official-window-run", "official-windows.private.json")), false);
    assert.doesNotMatch(outputText.value, /official\/group|C:\\Program|must_not_be_forwarded/);
  }
});

test("official-window private report is create-only", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-official-window-overwrite-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = officialWindowProbeOptions(directory);
  const runDirectory = path.join(directory, "fresh-official-window-run");
  fs.mkdirSync(runDirectory, { recursive: true });
  const artifactPath = path.join(runDirectory, "official-windows.private.json");
  fs.writeFileSync(artifactPath, "KEEP-EXISTING-MARKER");
  const outputText = { value: "" };
  const fake = fakeOfficialWindowProbeDependencies({
    options,
    directory,
    outputText,
    runAdmission() { return { schema: 1, ok: true, code: null, manifestAgeMs: 300, admissionFingerprint: "6".repeat(64) }; },
  });
  await assert.rejects(startOfficialWindowProbe(options, {
    ...fake.dependencies,
    createRunDirectory: () => runDirectory,
  }), { code: "official_window_probe_report_write_failed" });
  assert.equal(fs.readFileSync(artifactPath, "utf8"), "KEEP-EXISTING-MARKER");
  assert.equal(fake.probeCount, 1);
  assert.doesNotMatch(outputText.value, /official\/group|C:\\Program/);
});

test("ASAR candidate CLI parser is exclusive and requires exactly the seven candidate probe values", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-candidate-args-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const base = [
    "--probe-asar-candidates",
    "--pid", "123",
    "--app-path", ASAR_CANDIDATE_APP_PATH,
    "--token-file", path.join(directory, "token.json"),
    "--config", path.join(directory, "new-config.json"),
    "--connection-generation", "993",
    "--kernel-creation-time-100ns", "134000000000000000",
    "--probe-python", process.execPath,
  ];
  const parsed = parseArgs(base);
  assert.equal(parsed.probeAsarCandidates, true);
  assert.equal(parsed.targetPid, 123);
  assert.equal(parsed.connectionGeneration, 993);
  assert.equal(parsed.appPath, ASAR_CANDIDATE_APP_PATH);
  for (const extra of [
    ["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"],
    ["--wrapper", path.join(directory, "wrapper.node")], ["--session", "nt_2"],
    ["--control-port", "0"], ["--login-file", "identity.json"],
    ["--audio-route-config", "route.json"], ["--report", "report"],
    ["--probe-asar-sources"], ["--probe-renderer-capabilities"],
  ]) {
    assert.throws(() => parseArgs([...base, ...extra]), (error) => /option_forbidden$/.test(error.code));
  }
  assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)), { code: "missing_cli_option" });
});

test("ASAR candidate CLI persists redacted source plus bound private keeper evidence while printing metadata only", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-candidate-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = asarCandidateProbeOptions(directory);
  const outputText = { value: "" };
  const fingerprint = "abcdeffedcba0123".repeat(4);
  const fingerprintKeys = [];
  const fake = fakeAsarCandidateProbeDependencies({
    options,
    directory,
    outputText,
    runAdmission(request, count) {
      fingerprintKeys.push(request.fingerprintKey);
      assert.equal(request.appPath, ASAR_CANDIDATE_APP_PATH);
      return { schema: 1, ok: true, code: null, manifestAgeMs: 420, admissionFingerprint: fingerprint };
    },
  });
  const result = await startCli(options, { asarCandidateProbeDependencies: fake.dependencies, output: fake.dependencies.output });
  assert.equal(result.asarCandidateProbeCompleted, true);
  assert.equal(result.proofLevel, ASAR_CANDIDATE_PROOF_LEVEL);
  assert.equal(fake.probeCount, 1);
  assert.equal(fake.admissionCount, 3);
  assert.equal(new Set(fingerprintKeys).size, 1);
  assert.ok(fake.steps.indexOf("admission_1") < fake.steps.indexOf("broker_create"));
  assert.ok(fake.steps.indexOf("admission_2") < fake.steps.indexOf("keeper_start"));
  assert.ok(fake.steps.indexOf("keeper_start") < fake.steps.indexOf("candidate_rpc"));
  assert.ok(fake.steps.indexOf("candidate_rpc") < fake.steps.indexOf("keeper_finish"));
  assert.ok(fake.steps.indexOf("keeper_finish") < fake.steps.indexOf("admission_3"));
  assert.equal(fake.steps.at(-1), "broker_stop");

  const reportPath = path.join(directory, "fresh-asar-candidate-run", "asar-candidates.private.json");
  const savedText = fs.readFileSync(reportPath, "utf8");
  const saved = JSON.parse(savedText);
  assert.equal(saved.proofLevel, "candidate_unverified");
  assert.equal(saved.candidateReport.entries[0].content.includes(ASAR_CANDIDATE_SENTINEL), true);
  assert.equal(saved.physicalKeeper.state, "final");
  assert.equal(saved.physicalKeeper.files[0].nativeFileId, "A".repeat(32));
  assert.equal(saved.physicalKeeper.files[0].canonicalFinalPath, path.win32.join(options.appPath, "application.asar"));
  assert.doesNotMatch(savedText, new RegExp(TOKEN));
  assert.doesNotMatch(savedText, new RegExp(fingerprint));
  assert.doesNotMatch(savedText, new RegExp(fingerprintKeys[0]));
  assert.doesNotMatch(outputText.value, /PRIVATE_ASAR_CANDIDATE_SOURCE_SENTINEL|C:\\Program|nativeFileId|application\.asar|keeperCreationTime100ns/);
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprintKeys[0]));
  assert.match(outputText.value, /candidate_unverified/);
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1,
    connectionGeneration: options.connectionGeneration,
    ipcToken: TOKEN,
  });
});

test("ASAR candidate admission change or keeper failure prevents private source artifact creation", async (t) => {
  for (const failedAt of ["initial", "before_rpc", "before_persist", "keeper_finish"]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `qqcall-asar-candidate-${failedAt}-`));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = asarCandidateProbeOptions(directory);
    const outputText = { value: "" };
    const firstFingerprint = "1".repeat(64);
    const changedFingerprint = "2".repeat(64);
    const fake = fakeAsarCandidateProbeDependencies({
      options,
      directory,
      outputText,
      keeperFailureAt: failedAt === "keeper_finish" ? "finish" : null,
      runAdmission(_request, count) {
        const changedCount = failedAt === "before_rpc" ? 2 : failedAt === "before_persist" ? 3 : 0;
        if (failedAt === "initial") return { schema: 1, ok: false, code: "target_mismatch" };
        return {
          schema: 1,
          ok: true,
          code: null,
          manifestAgeMs: 400,
          admissionFingerprint: count === changedCount ? changedFingerprint : firstFingerprint,
        };
      },
    });
    if (failedAt === "initial") {
      await assert.rejects(startAsarCandidateProbe(options, fake.dependencies), { code: "renderer_probe_admission_denied" });
      assert.equal(fake.admissionCount, 1);
      assert.equal(fs.existsSync(path.join(directory, "fresh-asar-candidate-run")), false);
    } else {
      await assert.rejects(startAsarCandidateProbe(options, fake.dependencies), failedAt === "keeper_finish"
        ? { code: "keeper_frame_invalid" }
        : { code: "asar_candidate_probe_admission_changed" });
      assert.equal(fake.admissionCount, failedAt === "before_rpc" ? 2 : failedAt === "keeper_finish" ? 2 : 3);
      assert.equal(fake.probeCount, failedAt === "before_rpc" ? 0 : 1);
      assert.equal(fs.existsSync(path.join(directory, "fresh-asar-candidate-run", "asar-candidates.private.json")), false);
    }
    assert.doesNotMatch(outputText.value, new RegExp(ASAR_CANDIDATE_SENTINEL));
    assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  }
});

test("ASAR candidate report publication refuses overwrite after source and keeper validation", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-candidate-overwrite-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = asarCandidateProbeOptions(directory);
  const runDirectory = path.join(directory, "fresh-asar-candidate-run");
  fs.mkdirSync(runDirectory, { recursive: true });
  const reportPath = path.join(runDirectory, "asar-candidates.private.json");
  fs.writeFileSync(reportPath, "KEEP-EXISTING-PRIVATE-MARKER");
  const outputText = { value: "" };
  const fake = fakeAsarCandidateProbeDependencies({
    options,
    directory,
    outputText,
    runAdmission() { return { schema: 1, ok: true, code: null, manifestAgeMs: 300, admissionFingerprint: "6".repeat(64) }; },
  });
  await assert.rejects(startAsarCandidateProbe(options, {
    ...fake.dependencies,
    createRunDirectory: () => runDirectory,
  }), { code: "asar_candidate_probe_report_write_failed" });
  assert.equal(fs.readFileSync(reportPath, "utf8"), "KEEP-EXISTING-PRIVATE-MARKER");
  assert.equal(fake.probeCount, 1);
  assert.doesNotMatch(outputText.value, new RegExp(ASAR_CANDIDATE_SENTINEL));
});

test("renderer script metadata CLI parser accepts only its exclusive seven-value diagnostic form", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-script-metadata-args-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const base = [
    "--probe-renderer-script-metadata", "--pid", "123", "--app-path", ASAR_CANDIDATE_APP_PATH,
    "--token-file", path.join(directory, "token.json"), "--config", path.join(directory, "new-config.json"),
    "--connection-generation", "996", "--kernel-creation-time-100ns", "134000000000000000",
    "--probe-python", process.execPath,
  ];
  const parsed = parseArgs(base);
  assert.equal(parsed.probeRendererScriptMetadata, true);
  assert.equal(parsed.targetPid, 123);
  assert.equal(parsed.connectionGeneration, 996);
  assert.equal(parsed.appPath, ASAR_CANDIDATE_APP_PATH);
  for (const extra of [
    ["--service-mode"], ["--auto-bootstrap"], ["--host-port", "6111"],
    ["--wrapper", path.join(directory, "wrapper.node")], ["--control-port", "0"],
    ["--audio-route-config", "route.json"], ["--probe-official-windows"],
  ]) {
    assert.throws(() => parseArgs([...base, ...extra]), (error) => /option_forbidden$/.test(error.code));
  }
  assert.throws(() => parseArgs(base.filter((value) => value !== "--probe-python" && value !== process.execPath)), { code: "missing_cli_option" });
  assert.throws(() => validateRendererScriptMetadataProbeOptions({ ...parsed, extra: "sourceText" }), { code: "invalid_renderer_script_metadata_probe_options" });
  assert.throws(() => validateRendererScriptMetadataProbeOptions({ ...parsed, appPath: "C:\\Other\\QQ\\app" }), { code: "invalid_renderer_script_metadata_probe_options" });
  const accessor = { ...parsed };
  Object.defineProperty(accessor, "tokenFile", { enumerable: true, get() { assert.fail("probe options must not invoke accessors"); } });
  assert.throws(() => validateRendererScriptMetadataProbeOptions(accessor), { code: "invalid_renderer_script_metadata_probe_options" });
});

test("renderer script metadata CLI persists a fixed metadata report and prints aggregates only", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-script-metadata-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = rendererScriptMetadataProbeOptions(directory);
  const outputText = { value: "" };
  const fingerprints = [];
  const fingerprint = "abcdeffedcba0123".repeat(4);
  const fake = fakeRendererScriptMetadataProbeDependencies({
    options,
    directory,
    outputText,
    runAdmission(request) {
      fingerprints.push(request.fingerprintKey);
      assert.equal(request.targetPid, options.targetPid);
      assert.equal(request.targetFiletime, options.kernelCreationTime100ns);
      assert.equal(request.appPath, ASAR_CANDIDATE_APP_PATH);
      assert.equal(request.brokerGeneration, options.connectionGeneration);
      return { schema: 1, ok: true, code: null, manifestAgeMs: 420, admissionFingerprint: fingerprint };
    },
  });
  const result = await startCli(options, {
    rendererScriptMetadataProbeDependencies: fake.dependencies,
    output: fake.dependencies.output,
  });
  assert.equal(result.rendererScriptMetadataProbeCompleted, true);
  assert.equal(result.proofLevel, "diagnostic_only");
  assert.equal(fake.probeCount, 1);
  assert.equal(fake.admissionCount, 3);
  assert.equal(new Set(fingerprints).size, 1);
  assert.ok(fake.steps.indexOf("admission_1") < fake.steps.indexOf("broker_create"));
  assert.ok(fake.steps.indexOf("admission_2") < fake.steps.indexOf("renderer_script_metadata_rpc"));
  assert.ok(fake.steps.indexOf("renderer_script_metadata_rpc") < fake.steps.indexOf("admission_3"));
  assert.equal(fake.steps.at(-1), "broker_stop");

  const artifactPath = path.join(directory, "fresh-renderer-script-metadata-run", "renderer-script-metadata.private.json");
  const savedText = fs.readFileSync(artifactPath, "utf8");
  const saved = JSON.parse(savedText);
  assert.equal(saved.proofLevel, "diagnostic_only");
  assert.equal(saved.admissionStable, true);
  assert.equal(saved.report.exactAVWindowCount, 1);
  assert.equal(saved.report.windows[0].scripts[0].byteCount, 87);
  assert.doesNotMatch(savedText, new RegExp(TOKEN));
  assert.doesNotMatch(savedText, new RegExp(fingerprint));
  assert.doesNotMatch(savedText, /sourceText|INLINE_SOURCE_SECRET|private\.js|app:\/\/|https:\/\//);
  assert.doesNotMatch(outputText.value, /sourceKind|sourceText|inline-source|app:\/\/|https:\/\/|private_event|must_not_be_forwarded/);
  assert.doesNotMatch(outputText.value, new RegExp(TOKEN));
  assert.doesNotMatch(outputText.value, new RegExp(fingerprint));
  assert.match(outputText.value, /"status":"complete"/);
  assert.match(outputText.value, /"exactAVWindowCount":1/);
  assert.match(outputText.value, /"localJavaScriptCount":1/);
  assert.match(outputText.value, /"avsdkMarkerCount":1/);
  assert.deepEqual(JSON.parse(fs.readFileSync(options.configPath, "utf8")), {
    schema: 1,
    connectionGeneration: options.connectionGeneration,
    ipcToken: TOKEN,
  });
  assert.deepEqual(summarizeRendererScriptMetadataProbe(FIXED_RENDERER_SCRIPT_METADATA_REPORT), {
    proofLevel: "diagnostic_only",
    windowCount: 1,
    windowsTruncated: false,
    exactAVWindowCount: 1,
    scriptRecordCount: 2,
    inlineCount: 1,
    localJavaScriptCount: 1,
    rejectedSourceCount: 0,
    avsdkMarkerCount: 1,
    postMessageMarkerCount: 0,
    sceneIdMarkerCount: 1,
  });
});

test("changed renderer metadata admission blocks dispatch or private report publication", async (t) => {
  for (const changedAt of [2, 3]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `qqcall-renderer-script-metadata-admission-${changedAt}-`));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    const options = rendererScriptMetadataProbeOptions(directory);
    const outputText = { value: "" };
    const firstFingerprint = "1".repeat(64);
    const fake = fakeRendererScriptMetadataProbeDependencies({
      options,
      directory,
      outputText,
      runAdmission(_request, count) {
        return {
          schema: 1,
          ok: true,
          code: null,
          manifestAgeMs: 400,
          admissionFingerprint: count === changedAt ? "2".repeat(64) : firstFingerprint,
        };
      },
    });
    await assert.rejects(startRendererScriptMetadataProbe(options, fake.dependencies), { code: "renderer_script_metadata_probe_admission_changed" });
    assert.equal(fake.admissionCount, changedAt);
    assert.equal(fake.probeCount, changedAt === 2 ? 0 : 1);
    assert.equal(fs.existsSync(path.join(directory, "fresh-renderer-script-metadata-run", "renderer-script-metadata.private.json")), false);
    assert.doesNotMatch(outputText.value, /sourceText|app:\/\/|https:\/\/|private_event/);
  }
});

test("renderer metadata CLI rejects an unknown IPC proof before persistence", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-renderer-script-metadata-forged-proof-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = rendererScriptMetadataProbeOptions(directory);
  const outputText = { value: "" };
  const fake = fakeRendererScriptMetadataProbeDependencies({
    options,
    directory,
    outputText,
    report: { ...FIXED_RENDERER_SCRIPT_METADATA_REPORT, proofLevel: "nativeJoinVerified" },
    runAdmission() { return { schema: 1, ok: true, code: null, manifestAgeMs: 400, admissionFingerprint: "5".repeat(64) }; },
  });
  await assert.rejects(startRendererScriptMetadataProbe(options, fake.dependencies), { code: "renderer_script_metadata_probe_invalid_result" });
  assert.equal(fake.admissionCount, 2);
  assert.equal(fake.probeCount, 1);
  assert.equal(fs.existsSync(path.join(directory, "fresh-renderer-script-metadata-run", "renderer-script-metadata.private.json")), false);
  assert.doesNotMatch(outputText.value, /nativeJoinVerified|sourceText|app:\/\//);
});
