"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");
const { bootstrap, removalModeForDigest } = require("../agent/bootstrap.cjs");
const { SOURCE_ENTRIES: ASAR_CANDIDATE_ENTRIES, PROOF_LEVEL: ASAR_CANDIDATE_PROOF_LEVEL } = require("../agent/asar-candidate-probe.cjs");
const { PROOF_LEVEL: OFFICIAL_WINDOW_PROOF_LEVEL } = require("../agent/official-window-probe.cjs");
const { PROOF_LEVEL: RENDERER_SCRIPT_METADATA_PROOF_LEVEL } = require("../agent/renderer-script-metadata-probe.cjs");
const { PROOF_LEVEL: OFFICIAL_AV_PROOF_LEVEL } = require("../agent/official-av-observer.cjs");
const { createBroker } = require("../probe/broker.cjs");
const {
  MAX_FRAME_BYTES,
  PIPE_PREFIX,
  FrameDecoder,
  createBoundedWriter,
  createPipeClient,
  encodeFrame,
  makeEnvelope,
  validateEnvelope,
} = require("../probe/ipc.cjs");

function testToken() { return randomBytes(32).toString("base64url"); }

const FIXED_ASAR_SOURCE_REPORT = Object.freeze({
  schema: 1,
  status: "rejected",
  code: "archive_hash_mismatch",
  archiveSha256: null,
  packageJsonSha256: null,
  wrapperSha256: null,
  entries: [],
});
const FIXED_ASAR_CANDIDATE_REPORT = Object.freeze({
  schema: "qqcall.asar.candidates.v1",
  proofLevel: ASAR_CANDIDATE_PROOF_LEVEL,
  status: "candidate",
  code: "ok",
  entries: ASAR_CANDIDATE_ENTRIES.map((label, index) => ({
    label,
    status: "read",
    bytes: 24 + index,
    sha256: String(index + 1).repeat(64),
    content: label === "renderer/index.html"
      ? { inlineScripts: ["fixed-candidate-source"], scriptSrcDirectives: [], scriptSources: [] }
      : `fixed-candidate-source-${label}`,
  })),
});
const FIXED_OFFICIAL_WINDOW_REPORT = Object.freeze({
  schema: "qqcall.official.windows.v1",
  proofLevel: OFFICIAL_WINDOW_PROOF_LEVEL,
  status: "complete",
  code: "ok",
  windowCount: 1,
  windowsTruncated: false,
  windows: [{
    ordinal: 1,
    destroyed: false,
    rendererFileMatch: true,
    routePath: null,
    domStatus: "observed",
    embedCount: 1,
    postMessageAvailable: true,
  }],
});
const FIXED_RENDERER_SCRIPT_METADATA_REPORT = Object.freeze({
  schema: "qqcall.renderer.script-metadata.v1",
  proofLevel: RENDERER_SCRIPT_METADATA_PROOF_LEVEL,
  status: "complete",
  code: "ok",
  windowCount: 0,
  windowsTruncated: false,
  exactAVWindowCount: 0,
  scriptRecordCount: 0,
  windows: [],
});
function fixedOfficialAVReport(status = "active") {
  return {
    schema: "qqcall.official.av.observer.v1",
    proofLevel: OFFICIAL_AV_PROOF_LEVEL,
    status,
    code: "ok",
    coverageGap: true,
    comparisonScope: "document_epoch_only",
    outboundAttemptsOnly: true,
    windowsTruncated: false,
    windows: [],
    events: [],
    droppedEvents: 0,
    encodedEventBytes: 0,
    incomplete: false,
    queueLossPossible: false,
    cleanupComplete: status === "stopped" ? true : null,
  };
}

test("wrapper removal contract is selected only by the pinned SHA-256", () => {
  assert.equal(removalModeForDigest("29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2"), "listener-object");
  assert.equal(removalModeForDigest("29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2 "), "unverified");
  assert.equal(removalModeForDigest("00".repeat(32)), "unverified");
});

async function waitUntil(predicate, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail("condition did not become true before timeout");
}

test("frame decoder rejects malformed and oversized frames, including oversized remainder after a valid frame", () => {
  let malformedCode;
  const malformed = new FrameDecoder({ maxFrameBytes: 16, onError: (error) => { malformedCode = error.code; } });
  malformed.push(Buffer.from("{broken}\n", "utf8"));
  assert.equal(malformedCode, "malformed_json");

  let oversizeCode;
  const oversized = new FrameDecoder({ maxFrameBytes: 8, onError: (error) => { oversizeCode = error.code; } });
  oversized.push(Buffer.from("123456789", "utf8"));
  assert.equal(oversizeCode, "frame_too_large");

  const messages = [];
  let remainderCode;
  const remainder = new FrameDecoder({ maxFrameBytes: 8, onMessage: (message) => messages.push(message), onError: (error) => { remainderCode = error.code; } });
  remainder.push(Buffer.from("{\"a\":1}\n123456789", "utf8"));
  assert.deepEqual(messages, [{ a: 1 }]);
  assert.equal(remainderCode, "frame_too_large");
  assert.throws(() => encodeFrame({ value: "x".repeat(MAX_FRAME_BYTES) }), { code: "frame_too_large" });
});

test("envelope validation rejects stale generation, wrong token, and out-of-order sequence", () => {
  const token = testToken();
  const message = makeEnvelope("request", 1, 4, "r1", "status", {}, token);
  assert.equal(validateEnvelope(message, { expectedSequence: 1, generation: 4, token, direction: "broker-in" }).op, "status");
  assert.throws(() => validateEnvelope(message, { expectedSequence: 1, generation: 5, token }), { code: "stale_generation" });
  assert.throws(() => validateEnvelope(message, { expectedSequence: 1, generation: 4, token: testToken() }), { code: "unauthorized" });
  assert.throws(() => validateEnvelope(message, { expectedSequence: 2, generation: 4, token }), { code: "out_of_order_sequence" });
});

test("bounded writer caps queued bytes when a pipe is backpressured", () => {
  class FakeSocket extends EventEmitter {
    destroyed = false;
    write() { return false; }
  }
  const socket = new FakeSocket();
  const writer = createBoundedWriter(socket, { maxQueuedBytes: 4 });
  writer.write(Buffer.from("one"));
  writer.write(Buffer.from("1234"));
  assert.throws(() => writer.write(Buffer.from("x")), { code: "backpressure_limit" });
  writer.close();
});

test("attach timeout and failed disconnect cleanup cannot reuse a stale clean report", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-broker-attach-uncertain-"));
  const reportPath = path.join(runtimeDir, "report");
  const generation = 51;
  const ipcToken = testToken();
  fs.writeFileSync(`${reportPath}.agent.json`, JSON.stringify({
    processPid: process.pid,
    connectionGeneration: generation,
    listenerRegistered: false,
    lastFailureCode: null,
  }));
  let actualAdds = 0;
  let removeAttempts = 0;
  let resolveAttachEntered;
  let resolveDisconnectCleanup;
  const attachEntered = new Promise((resolve) => { resolveAttachEntered = resolve; });
  const disconnectCleanup = new Promise((resolve) => { resolveDisconnectCleanup = resolve; });
  const fakeAgent = {
    status() { return { listenerRegistered: actualAdds > removeAttempts }; },
    attach() {
      actualAdds += 1;
      resolveAttachEntered();
      return new Promise(() => {}); // Simulate add succeeding while its ACK is lost.
    },
    async detach() {
      removeAttempts += 1;
      return { ok: false, code: "listener_removal_failed", status: { listenerRegistered: true } };
    },
    forwardAVSDKOutput() { return { ok: false, code: "agent_not_attached" }; },
  };
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath,
    connectionGeneration: generation,
    ipcToken,
    maxLifetimeMs: 30_000,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: generation, ipcToken });
    await client.connect();
    client.setAgent(fakeAgent);
    client.onDisconnect(async () => {
      try { resolveDisconnectCleanup(await fakeAgent.detach()); }
      catch (error) { resolveDisconnectCleanup({ ok: false, code: error.code }); }
    });
    await broker.statusRequest(); // Establish an old, clean status before attach.
    const attachRequest = broker.attach(25);
    await assert.rejects(attachRequest, { code: "request_timeout" });
    await attachEntered;
    assert.equal(actualAdds, 1);

    await client.close();
    assert.equal((await disconnectCleanup).ok, false);
    assert.equal(removeAttempts, 1);
    await assert.rejects(broker.stop({ confirmDetach: true }), { code: "detach_unconfirmed_agent_disconnected" });
    const forced = await broker.stop({ confirmDetach: false });
    const repeatedForced = await broker.stop({ confirmDetach: false });
    assert.equal(forced.detachedConfirmed, false);
    assert.equal(repeatedForced.detachedConfirmed, false, "a second force stop cannot infer clean from a flat or stale status");
    await assert.rejects(broker.detachAndStop(), { code: "detach_unconfirmed" });
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("flat attached status stays unconfirmed across repeated force stop", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-broker-flat-status-"));
  const ipcToken = testToken();
  let adds = 0;
  const fakeAgent = {
    status() { return { listenerRegistered: adds > 0 }; },
    async attach() { adds += 1; return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { return { ok: false, code: "listener_removal_failed", status: { listenerRegistered: true } }; },
    forwardAVSDKOutput() { return { ok: false, code: "agent_not_attached" }; },
  };
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: 52,
    ipcToken,
    maxLifetimeMs: 30_000,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: 52, ipcToken });
    await client.connect();
    client.setAgent(fakeAgent);
    await broker.attach();
    await waitUntil(() => broker.status().agent?.listenerRegistered === true);
    assert.equal(broker.status().agent.listenerRegistered, true, "the latest bootstrap event is flat");
    assert.equal((await broker.stop({ confirmDetach: false })).detachedConfirmed, false);
    assert.equal((await broker.stop({ confirmDetach: false })).detachedConfirmed, false);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("shutdown claims closing before detach awaits and rejects attach", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-broker-shutdown-race-"));
  const ipcToken = testToken();
  let attachCalls = 0;
  let detachCalls = 0;
  let resolveDetach;
  const detachResult = new Promise((resolve) => { resolveDetach = resolve; });
  const fakeAgent = {
    status() { return { listenerRegistered: false }; },
    async attach() { attachCalls += 1; return { ok: true, status: { listenerRegistered: true } }; },
    detach() {
      detachCalls += 1;
      return detachResult;
    },
    forwardAVSDKOutput() { return { ok: false, code: "agent_not_attached" }; },
  };
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: 53,
    ipcToken,
    maxLifetimeMs: 30_000,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: 53, ipcToken });
    await client.connect();
    client.setAgent(fakeAgent);
    const closing = broker.detachAndStop();
    await waitUntil(() => detachCalls === 1);
    const competingStop = await broker.stop({ confirmDetach: false });
    assert.equal(competingStop.stopped, false);
    assert.equal(competingStop.detachedConfirmed, false);
    assert.equal(detachCalls, 1, "a competing stop cannot race the owner or claim shutdown");
    await assert.rejects(broker.attach(), { code: "broker_closing" });
    assert.equal(attachCalls, 0);
    resolveDetach({ ok: true, status: { listenerRegistered: false } });
    const closed = await closing;
    assert.equal(closed.stopped, true);
    assert.equal(closed.detachedConfirmed, true);
    assert.equal(attachCalls, 0);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("named-pipe broker completes handshake, status, attach, detach, reattach, and disconnect cleanup", { skip: process.platform !== "win32" }, async () => {
  const reportBase = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-")), "agent");
  const token = testToken();
  let listenerAdds = 0;
  const listeners = [];
  const removed = [];
  const calls = [];
  const service = {
    addKernelAVSDKListener(listener) { listenerAdds += 1; listeners.push(listener); },
    removeKernelAVSDKListener(listener) { removed.push(listener); },
    setActionFromAVSDK(...args) { calls.push(args); },
  };
  const agent = createKernelAgent({
    connectionGeneration: 17,
    removalMode: "listener-object",
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });
  await agent.probe();
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: reportBase,
    connectionGeneration: 17,
    ipcToken: token,
    maxLifetimeMs: 30_000,
  });
  let client;
  let cleanupDone;
  let cleanupResolve;
  const cleanupSignal = new Promise((resolve) => { cleanupResolve = resolve; });
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: 17, ipcToken: token });
    await client.connect();
    client.setAgent(agent);
    client.onDisconnect(async () => {
      cleanupDone = await agent.detach();
      cleanupResolve();
    });

    const initial = await broker.statusRequest();
    assert.equal(initial.bootstrap.bootstrapLoaded, true);
    assert.equal(initial.agent.listenerRegistered, false);
    assert.equal((await broker.attach()).result.status.listenerRegistered, true);
    assert.equal((await broker.forwardAVSDKOutput(20001, [7, "private"])).result.forwarded, true);
    assert.deepEqual(calls, [[7, "private"]]);
    assert.equal((await broker.detach()).result.status.listenerRegistered, false);
    assert.equal((await broker.attach()).result.status.listenerRegistered, true);
    assert.equal(listenerAdds, 2);

    await client.close();
    await cleanupSignal;
    assert.equal(cleanupDone.ok, true);
    assert.equal(agent.status().listenerRegistered, false);
    assert.strictEqual(removed[0], listeners[0]);
    assert.strictEqual(removed[1], listeners[1]);
    assert.equal(removed.length, 2);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(path.dirname(reportBase), { recursive: true, force: true });
  }
});

test("renderer capability RPC requires empty payload, authenticated bootstrap identity, and dispatches once", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-renderer-probe-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 927;
  const token = testToken();
  let probeCalls = 0;
  const report = {
    schema: 1,
    processType: "browser",
    electronPresent: true,
    electronBuiltin: false,
    appAvailable: false,
    browserWindowAvailable: false,
    getAllWindowsAvailable: false,
    windowCount: 0,
    windowsTruncated: false,
    windows: [],
  };
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeRendererCapabilities() { probeCalls += 1; return report; },
    });
    await client.connect();

    client.setBootstrapStatus({
      bootstrapLoaded: true,
      wrapperFound: true,
      wrapperHashPinned: true,
      listenerRegistered: false,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    client.publishEvent("bootstrap_status", {
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    assert.deepEqual(await broker.probeRendererCapabilities(), report, "a fixed negative capability report is a valid result");
    assert.equal(probeCalls, 1);
    await assert.rejects(broker.probeRendererCapabilities(), { code: "renderer_probe_already_dispatched" });
    assert.equal(probeCalls, 1);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("ASAR source RPC accepts only an empty authenticated request and one fixed report per generation", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-asar-source-probe-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 932;
  const token = testToken();
  let probeCalls = 0;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeAsarSources() { probeCalls += 1; return FIXED_ASAR_SOURCE_REPORT; },
    });
    await client.connect();
    client.setBootstrapStatus({
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    client.publishEvent("bootstrap_status", {
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    assert.deepEqual(await broker.probeAsarSources(), FIXED_ASAR_SOURCE_REPORT);
    assert.equal(probeCalls, 1);
    await assert.rejects(broker.probeAsarSources(), { code: "asar_source_probe_already_dispatched" });
    assert.equal(probeCalls, 1);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("ASAR candidate RPC is authenticated, empty, one-shot, and returns only a validated candidate report", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-asar-candidate-probe-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 933;
  const token = testToken();
  let probeCalls = 0;
  const publishedEvents = [];
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
    onEvent(op, payload) { publishedEvents.push({ op, payload }); },
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeAsarCandidates() { probeCalls += 1; return FIXED_ASAR_CANDIDATE_REPORT; },
    });
    await client.connect();
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.probeAsarCandidates(), FIXED_ASAR_CANDIDATE_REPORT);
    assert.equal(probeCalls, 1);
    await assert.rejects(broker.probeAsarCandidates(), { code: "asar_candidate_probe_already_dispatched" });
    assert.equal(probeCalls, 1);
    assert.equal(MAX_FRAME_BYTES, 1024 * 1024, "the existing wire maximum stays fixed");
    assert.equal(publishedEvents.some((event) => JSON.stringify(event).includes("fixed-candidate-source")), false);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("official-window RPC waits for the bound bootstrap, accepts an empty request once, and keeps reports off events", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-official-window-probe-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 935;
  const token = testToken();
  let probeCalls = 0;
  const publishedEvents = [];
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
    onEvent(op, payload) { publishedEvents.push({ op, payload }); },
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeOfficialWindows() { probeCalls += 1; return FIXED_OFFICIAL_WINDOW_REPORT; },
    });
    await client.connect();
    await assert.rejects(broker.probeOfficialWindows(), { code: "official_window_probe_not_ready" });
    assert.equal(probeCalls, 0, "readiness failure does not spend the one-shot dispatch");
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.probeOfficialWindows(), FIXED_OFFICIAL_WINDOW_REPORT);
    assert.equal(probeCalls, 1);
    await assert.rejects(broker.probeOfficialWindows(), { code: "official_window_probe_already_dispatched" });
    assert.equal(probeCalls, 1);
    assert.equal(publishedEvents.some((event) => JSON.stringify(event).includes("official/group")), false);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("official-window IPC rejects selectors, stale PID/generation, and duplicate dispatch with fixed codes", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 936;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  let client;
  let socket;
  let serverSequence = 0;
  let probeCalls = 0;
  const replies = new Map();
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type === "response") {
        const waiter = replies.get(message.requestId);
        if (waiter) { replies.delete(message.requestId); waiter(message); }
      }
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onProbeOfficialWindows() { probeCalls += 1; return FIXED_OFFICIAL_WINDOW_REPORT; },
  });
  await client.connect();
  const send = async (id, payload) => {
    const reply = new Promise((resolve) => replies.set(id, resolve));
    socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, id, "probeOfficialWindows", payload, token)));
    return reply;
  };
  const malformed = await send("official-malformed", { selector: "document.body" });
  assert.equal(malformed.payload.code, "invalid_official_window_probe_payload");
  assert.equal(probeCalls, 0);
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid + 1, connectionGeneration: generation });
  const wrongPid = await send("official-wrong-pid", {});
  assert.equal(wrongPid.payload.code, "official_window_probe_not_ready");
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation + 1 });
  const wrongGeneration = await send("official-wrong-generation", {});
  assert.equal(wrongGeneration.payload.code, "official_window_probe_not_ready");
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
  const valid = await send("official-valid", {});
  assert.equal(valid.payload.ok, true);
  assert.deepEqual(valid.payload.report, FIXED_OFFICIAL_WINDOW_REPORT);
  const duplicate = await send("official-duplicate", {});
  assert.equal(duplicate.payload.code, "official_window_probe_already_dispatched");
  assert.equal(probeCalls, 1);
  assert.equal(JSON.stringify(valid).includes("application.asar"), false);
});

test("official AV observer RPC is empty-payload, authenticated, generation-bound, one-shot, and idempotently stopped", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-official-av-observer-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 937;
  const token = testToken();
  const publishedEvents = [];
  const counts = { start: 0, drain: 0, stop: 0 };
  let receivedStartOptions = null;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
    onEvent(op, payload) { publishedEvents.push({ op, payload }); },
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onStartOfficialAVObserver(options) { receivedStartOptions = options; counts.start += 1; return fixedOfficialAVReport("active"); },
      onDrainOfficialAVObserver() { counts.drain += 1; return fixedOfficialAVReport("active"); },
      onStopOfficialAVObserver() { counts.stop += 1; return fixedOfficialAVReport("stopped"); },
    });
    await client.connect();
    for (const invalid of [{ incomingOnly: false }, { incomingOnly: true, selector: "document.body" }]) {
      await assert.rejects(broker.startOfficialAVObserver(5000, invalid), { code: "invalid_official_av_observer_options" });
    }
    const accessorOption = {};
    Object.defineProperty(accessorOption, "incomingOnly", { enumerable: true, get() { throw new Error("must not be invoked"); } });
    await assert.rejects(broker.startOfficialAVObserver(5000, accessorOption), { code: "invalid_official_av_observer_options" });
    await assert.rejects(broker.startOfficialAVObserver(), { code: "official_av_observer_not_ready" });
    assert.equal(counts.start, 0, "readiness failure does not spend the observer start");
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.startOfficialAVObserver(), fixedOfficialAVReport("active"));
    assert.equal(counts.start, 1);
    assert.deepEqual(receivedStartOptions, {}, "the legacy empty start payload stays the default mode");
    await assert.rejects(broker.startOfficialAVObserver(), { code: "official_av_observer_already_started" });
    assert.deepEqual(await broker.drainOfficialAVObserver(), fixedOfficialAVReport("active"));
    assert.equal(counts.drain, 1);
    const stopped = await broker.stopOfficialAVObserver();
    assert.deepEqual(stopped, fixedOfficialAVReport("stopped"));
    assert.deepEqual(await broker.stopOfficialAVObserver(), stopped);
    assert.equal(counts.stop, 1, "the broker caches the final idempotent stop result");
    await assert.rejects(broker.drainOfficialAVObserver(), { code: "official_av_observer_not_active" });
    assert.equal(publishedEvents.some((event) => JSON.stringify(event).includes("events")), false,
      "private observer reports never enter the event stream");
    assert.equal(MAX_FRAME_BYTES, 1024 * 1024, "the existing IPC wire maximum remains unchanged");
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("private invitation lifecycle IPC admits only exact bound requests and fixed completion results", { skip: process.platform !== "win32" }, async (t) => {
  const { PRIVATE_INVITE_PROFILE, PINNED_WRAPPER_SHA256 } = require("../shared/private-invite-scope.cjs");
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-invite-lifecycle-rpc-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 947, token = testToken();
  const peer = `u_${"P".repeat(22)}`;
  const body = { operation: "reject", evidence: { profile: PRIVATE_INVITE_PROFILE, wrapperSha256: PINNED_WRAPPER_SHA256,
    callbackSequence: 1, listenerRegistrationSequence: 1, inviteType: 1, relationId: "2146014839", fromUid: "" },
    peerUid: peer, selfUid: `u_${"S".repeat(22)}`, selfUin: "2955610491", inviteParams: [1, peer, [peer], 1, "0", false, "0"] };
  const broker = createBroker({ targetPid: process.pid, modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."), reportPath: path.join(runtimeDir, "report"), connectionGeneration: generation, ipcToken: token });
  let client, calls = 0, rawResult = false;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: generation, ipcToken: token,
      onPrivateInviteLifecycle(request) {
        calls += 1; assert.deepEqual(request, body);
        return { confirmed: true, code: "official_private_invite_document_ended", ...(rawResult ? { secret: "forbidden" } : {}) };
      } });
    await client.connect();
    await assert.rejects(broker.privateInviteLifecycle({ ...body, selector: "document.body" }), { code: "invalid_private_invite_lifecycle_request" });
    await assert.rejects(broker.privateInviteLifecycle(body), { code: "official_private_invite_unavailable" });
    assert.equal(calls, 0);
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.privateInviteLifecycle(body), { confirmed: true, code: "official_private_invite_document_ended" });
    rawResult = true;
    await assert.rejects(broker.privateInviteLifecycle(body), { code: "official_private_invite_invalid_result" });
    assert.equal(calls, 2);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("broker forwards the exact incoming-only mode through the authenticated observer RPC", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-official-av-incoming-rpc-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 938;
  const token = testToken();
  let receivedOptions = null;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onStartOfficialAVObserver(options) { receivedOptions = options; return fixedOfficialAVReport("active"); },
      onDrainOfficialAVObserver() { return fixedOfficialAVReport("active"); },
      onStopOfficialAVObserver() { return fixedOfficialAVReport("stopped"); },
    });
    await client.connect();
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.startOfficialAVObserver(5000, { incomingOnly: true }), fixedOfficialAVReport("active"));
    assert.deepEqual(receivedOptions, { incomingOnly: true });
    await broker.stopOfficialAVObserver();
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("broker forwards the exact native-conditional mode through the authenticated observer RPC", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-official-av-native-rpc-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 940;
  const token = testToken();
  let receivedOptions = null;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onStartOfficialAVObserver(options) { receivedOptions = options; return fixedOfficialAVReport("active"); },
      onDrainOfficialAVObserver() { return fixedOfficialAVReport("active"); },
      onStopOfficialAVObserver() { return fixedOfficialAVReport("stopped"); },
    });
    await client.connect();
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.startOfficialAVObserver(5000, { nativeConditional: true }), fixedOfficialAVReport("active"));
    assert.deepEqual(receivedOptions, { nativeConditional: true });
    await broker.stopOfficialAVObserver();
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("official AV IPC rejects false and unknown start selectors before one-shot dispatch", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 939;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  let client;
  let socket;
  let serverSequence = 0;
  let startCalls = 0;
  let receivedOptions = null;
  const replies = new Map();
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type === "response") replies.get(message.requestId)?.(message);
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onStartOfficialAVObserver(options) { startCalls += 1; receivedOptions = options; return fixedOfficialAVReport("active"); },
    onDrainOfficialAVObserver() { return fixedOfficialAVReport("active"); },
    onStopOfficialAVObserver() { return fixedOfficialAVReport("stopped"); },
  });
  await client.connect();
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
  const send = async (id, payload) => {
    const response = new Promise((resolve) => replies.set(id, resolve));
    socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, id, "startOfficialAVObserver", payload, token)));
    return response;
  };
  assert.equal((await send("observer-false", { incomingOnly: false })).payload.code, "invalid_official_av_observer_start_payload");
  assert.equal((await send("observer-extra", { incomingOnly: true, source: "raw" })).payload.code, "invalid_official_av_observer_start_payload");
  assert.equal((await send("observer-native-false", { nativeConditional: false })).payload.code, "invalid_official_av_observer_start_payload");
  assert.equal((await send("observer-native-extra", { nativeConditional: true, selector: "document.body" })).payload.code,
    "invalid_official_av_observer_start_payload");
  assert.equal((await send("observer-two-modes", { incomingOnly: true, nativeConditional: true })).payload.code,
    "invalid_official_av_observer_start_payload");
  assert.equal(startCalls, 0);
  const started = await send("observer-incoming", { incomingOnly: true });
  assert.equal(started.payload.ok, true);
  assert.deepEqual(receivedOptions, { incomingOnly: true });
  assert.equal(startCalls, 1);
  assert.equal((await send("observer-duplicate", { incomingOnly: true })).payload.code, "official_av_observer_already_started");
  assert.equal(startCalls, 1);
});

test("candidate IPC rejects nonempty selectors and oversize reports with fixed response codes", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 934;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  let client;
  let socket;
  let serverSequence = 0;
  let probeCalls = 0;
  const replies = new Map();
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type === "response") {
        const waiter = replies.get(message.requestId);
        if (waiter) { replies.delete(message.requestId); waiter(message); }
      }
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onProbeAsarCandidates() {
      probeCalls += 1;
      const oversized = structuredClone(FIXED_ASAR_CANDIDATE_REPORT);
      oversized.entries[0].content = "x".repeat(768 * 1024);
      return oversized;
    },
  });
  await client.connect();
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
  const send = async (id, payload) => {
    const reply = new Promise((resolve) => replies.set(id, resolve));
    socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, id, "probeAsarCandidates", payload, token)));
    return reply;
  };
  const malformed = await send("malformed-asar-candidates", { path: "private-source.js" });
  assert.equal(malformed.payload.code, "invalid_asar_candidate_probe_payload");
  assert.equal(probeCalls, 0);
  const oversize = await send("oversize-asar-candidates", {});
  assert.equal(oversize.payload.code, "asar_candidate_probe_invalid_result");
  assert.equal(probeCalls, 1);
  assert.equal(JSON.stringify(oversize).includes("x".repeat(100)), false, "oversize private source is never echoed");
});

test("pipe client rejects nonempty renderer probe payload and returns only a fixed capability report", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 929;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  const report = {
    schema: 1,
    processType: "browser",
    electronPresent: true,
    electronBuiltin: false,
    appAvailable: false,
    browserWindowAvailable: false,
    getAllWindowsAvailable: false,
    windowCount: 0,
    windowsTruncated: false,
    windows: [],
  };
  let probeCalls = 0;
  let client;
  let socket;
  let serverSequence = 0;
  let clientResponseSequence = 0;
  let malformedResolve;
  let validResolve;
  const malformedResponse = new Promise((resolve) => { malformedResolve = resolve; });
  const validResponse = new Promise((resolve) => { validResolve = resolve; });
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type !== "response") return;
      clientResponseSequence = message.seq;
      if (message.requestId === "malformed-probe") malformedResolve(message);
      if (message.requestId === "valid-probe") validResolve(message);
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onProbeRendererCapabilities() { probeCalls += 1; return report; },
  });
  await client.connect();
  socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, "malformed-probe", "probeRendererCapabilities", { path: "secret" }, token)));
  const malformed = await malformedResponse;
  assert.equal(malformed.payload.code, "invalid_renderer_probe_payload");
  assert.equal(probeCalls, 0);
  socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, "valid-probe", "probeRendererCapabilities", {}, token)));
  const valid = await validResponse;
  assert.equal(valid.payload.ok, true);
  assert.deepEqual(valid.payload.report, report);
  assert.equal(probeCalls, 1);
  assert.equal(clientResponseSequence, 3, "responses remain on the authenticated monotone IPC sequence");
});

test("pipe client rejects all ASAR probe selectors and validates its private report before responding", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 933;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  let probeCalls = 0;
  let client;
  let socket;
  let serverSequence = 0;
  let malformedResolve;
  let validResolve;
  const malformedResponse = new Promise((resolve) => { malformedResolve = resolve; });
  const validResponse = new Promise((resolve) => { validResolve = resolve; });
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type !== "response") return;
      if (message.requestId === "malformed-asar-probe") malformedResolve(message);
      if (message.requestId === "valid-asar-probe") validResolve(message);
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onProbeAsarSources() { probeCalls += 1; return FIXED_ASAR_SOURCE_REPORT; },
  });
  await client.connect();
  socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, "malformed-asar-probe", "probeAsarSources", { path: "private-source.js" }, token)));
  const malformed = await malformedResponse;
  assert.equal(malformed.payload.code, "invalid_asar_source_probe_payload");
  assert.equal(probeCalls, 0);
  socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, "valid-asar-probe", "probeAsarSources", {}, token)));
  const valid = await validResponse;
  assert.equal(valid.payload.ok, true);
  assert.deepEqual(valid.payload.report, FIXED_ASAR_SOURCE_REPORT);
  assert.equal(probeCalls, 1);
});

test("renderer capability RPC refuses a bootstrap PID or generation mismatch before dispatch", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-renderer-probe-binding-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 928;
  const token = testToken();
  let probeCalls = 0;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeRendererCapabilities() { probeCalls += 1; return {}; },
    });
    await client.connect();
    client.setBootstrapStatus({
      bootstrapLoaded: true,
      wrapperFound: true,
      wrapperHashPinned: true,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    client.publishEvent("bootstrap_status", {
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    });
    client.setBootstrapStatus({
      bootstrapLoaded: true,
      processPid: process.pid + 1,
      connectionGeneration: generation,
    });
    await assert.rejects(broker.probeRendererCapabilities(), { code: "renderer_probe_not_ready" });
    assert.equal(probeCalls, 0);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("simultaneous renderer capability requests reserve the single dispatch after status readiness", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-renderer-probe-race-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 931;
  const token = testToken();
  const report = {
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
  };
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let socket;
  let agentSequence = 0;
  let handshakeResolve;
  const handshakeResponse = new Promise((resolve) => { handshakeResolve = resolve; });
  const statusRequests = [];
  let probeRequests = 0;
  const decoder = new FrameDecoder({ onMessage(message) {
    if (message.type === "response" && message.op === "handshake") {
      handshakeResolve(message);
      return;
    }
    if (message.type !== "request") return;
    if (message.op === "status") {
      statusRequests.push(message);
      if (statusRequests.length === 2) {
        const bootstrapStatus = {
          bootstrapLoaded: true,
          processPid: process.pid,
          connectionGeneration: generation,
        };
        const responses = statusRequests.map((request) => encodeFrame(makeEnvelope(
          "response", ++agentSequence, generation, request.requestId, "status",
          { ok: true, code: null, bootstrap: bootstrapStatus, agent: null }, token,
        )));
        socket.write(Buffer.concat(responses));
      }
      return;
    }
    if (message.op === "probeRendererCapabilities") {
      probeRequests += 1;
      socket.write(encodeFrame(makeEnvelope(
        "response", ++agentSequence, generation, message.requestId, "probeRendererCapabilities",
        { ok: true, code: null, report }, token,
      )));
    }
  } });
  try {
    await broker.start();
    socket = net.createConnection(broker.pipeName);
    socket.on("data", (chunk) => decoder.push(chunk));
    await new Promise((resolve, reject) => {
      socket.once("connect", resolve);
      socket.once("error", reject);
    });
    socket.write(encodeFrame(makeEnvelope("request", ++agentSequence, generation, "probe-race-handshake", "handshake",
      { agentProtocol: 1 }, token)));
    const handshake = await handshakeResponse;
    assert.equal(handshake.payload.ok, true);
    socket.write(encodeFrame(makeEnvelope("event", ++agentSequence, generation, null, "bootstrap_status", {
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    }, token)));
    await waitUntil(() => broker.status().authenticated && broker.status().eventCount === 1);

    const outcomes = await Promise.allSettled([
      broker.probeRendererCapabilities(2000),
      broker.probeRendererCapabilities(2000),
    ]);
    assert.equal(statusRequests.length, 2, "both callers were held at the same asynchronous status gate");
    assert.equal(probeRequests, 1, "only the caller that atomically reserved the generation dispatches");
    assert.equal(outcomes.filter((entry) => entry.status === "fulfilled").length, 1);
    const rejected = outcomes.find((entry) => entry.status === "rejected");
    assert.equal(rejected.reason.code, "renderer_probe_already_dispatched");
  } finally {
    if (socket && !socket.destroyed) socket.destroy();
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("simultaneous ASAR source requests reserve one dispatch after the asynchronous readiness gate", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-asar-source-probe-race-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 934;
  const token = testToken();
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
  });
  let socket;
  let agentSequence = 0;
  let handshakeResolve;
  const handshakeResponse = new Promise((resolve) => { handshakeResolve = resolve; });
  const statusRequests = [];
  let probeRequests = 0;
  const decoder = new FrameDecoder({ onMessage(message) {
    if (message.type === "response" && message.op === "handshake") {
      handshakeResolve(message);
      return;
    }
    if (message.type !== "request") return;
    if (message.op === "status") {
      statusRequests.push(message);
      if (statusRequests.length === 2) {
        const bootstrapStatus = {
          bootstrapLoaded: true,
          processPid: process.pid,
          connectionGeneration: generation,
        };
        const responses = statusRequests.map((request) => encodeFrame(makeEnvelope(
          "response", ++agentSequence, generation, request.requestId, "status",
          { ok: true, code: null, bootstrap: bootstrapStatus, agent: null }, token,
        )));
        socket.write(Buffer.concat(responses));
      }
      return;
    }
    if (message.op === "probeAsarSources") {
      probeRequests += 1;
      socket.write(encodeFrame(makeEnvelope(
        "response", ++agentSequence, generation, message.requestId, "probeAsarSources",
        { ok: true, code: null, report: FIXED_ASAR_SOURCE_REPORT }, token,
      )));
    }
  } });
  try {
    await broker.start();
    socket = net.createConnection(broker.pipeName);
    socket.on("data", (chunk) => decoder.push(chunk));
    await new Promise((resolve, reject) => {
      socket.once("connect", resolve);
      socket.once("error", reject);
    });
    socket.write(encodeFrame(makeEnvelope("request", ++agentSequence, generation, "asar-probe-race-handshake", "handshake",
      { agentProtocol: 1 }, token)));
    const handshake = await handshakeResponse;
    assert.equal(handshake.payload.ok, true);
    socket.write(encodeFrame(makeEnvelope("event", ++agentSequence, generation, null, "bootstrap_status", {
      bootstrapLoaded: true,
      processPid: process.pid,
      connectionGeneration: generation,
    }, token)));
    await waitUntil(() => broker.status().authenticated && broker.status().eventCount === 1);

    const outcomes = await Promise.allSettled([
      broker.probeAsarSources(2000),
      broker.probeAsarSources(2000),
    ]);
    assert.equal(statusRequests.length, 2);
    assert.equal(probeRequests, 1, "only the first ready caller reserves and sends the source read");
    assert.equal(outcomes.filter((entry) => entry.status === "fulfilled").length, 1);
    const rejected = outcomes.find((entry) => entry.status === "rejected");
    assert.equal(rejected.reason.code, "asar_source_probe_already_dispatched");
  } finally {
    if (socket && !socket.destroyed) socket.destroy();
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("bootstrap rejects renderer and ASAR probes until the wrapper attempt settles", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-renderer-bootstrap-gate-"));
  const generation = 930;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  const fakeWrapperPath = path.join(runtimeDir, "cached-wrapper.node");
  let resolveSession;
  let sessionLookupStarted = false;
  const delayedSession = new Promise((resolve) => { resolveSession = resolve; });
  const fakeWrapperModule = {
    id: fakeWrapperPath,
    filename: fakeWrapperPath,
    loaded: true,
    exports: { NodeIQQNTWrapperSession: {
      getNTWrapperSession() { sessionLookupStarted = true; return delayedSession; },
    } },
  };
  require.cache[fakeWrapperPath] = fakeWrapperModule;

  let socket = null;
  let serverSequence = 0;
  const responseResolvers = new Map();
  let bootstrapStatusResolve;
  const bootstrapStatusPromise = new Promise((resolve) => { bootstrapStatusResolve = resolve; });
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type === "event" && message.op === "bootstrap_status") {
        bootstrapStatusResolve(message.payload);
        return;
      }
      if (message.type === "response") responseResolvers.get(message.requestId)?.(message.payload);
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    resolveSession?.(null);
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
    delete require.cache[fakeWrapperPath];
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  });

  const config = {
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: runtimeDir,
    wrapperModulePath: fakeWrapperPath,
    reportPath: path.join(runtimeDir, "report"),
    sessionName: "nt_1",
    connectionGeneration: generation,
    ipcToken: token,
    pipeName,
  };
  let controller;
  const bootstrapPromise = bootstrap(config);
  bootstrapPromise.catch(() => {});
  try {
    await waitUntil(() => sessionLookupStarted);
    await waitUntil(() => Boolean(socket && !socket.destroyed));
    const sendProbe = (requestId, op = "probeRendererCapabilities") => new Promise((resolve) => {
      responseResolvers.set(requestId, resolve);
      socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, requestId,
        op, {}, token)));
    });
    const premature = await sendProbe("premature-capability-probe");
    assert.equal(premature.ok, false);
    assert.equal(premature.code, "renderer_probe_failed", "the in-flight wrapper attempt stays private behind a fixed error");
    assert.equal(JSON.stringify(premature).includes("session"), false);

    const prematureAsar = await sendProbe("premature-asar-source-probe", "probeAsarSources");
    assert.equal(prematureAsar.ok, false);
    assert.equal(prematureAsar.code, "asar_source_probe_failed", "the in-flight wrapper attempt blocks the source read behind a fixed error");
    assert.equal(JSON.stringify(prematureAsar).includes("session"), false);

    const prematureCandidate = await sendProbe("premature-asar-candidate-probe", "probeAsarCandidates");
    assert.equal(prematureCandidate.ok, false);
    assert.equal(prematureCandidate.code, "asar_candidate_probe_not_ready", "the candidate RPC keeps its dispatch unspent until bootstrap readiness");
    assert.equal(JSON.stringify(prematureCandidate).includes("session"), false);

    resolveSession(null);
    const settledStatus = await bootstrapStatusPromise;
    assert.equal(settledStatus.bootstrapLoaded, true);
    assert.equal(settledStatus.processPid, process.pid);
    assert.equal(settledStatus.connectionGeneration, generation);
    assert.equal(settledStatus.lastFailureCode, "session_not_found");
    controller = await bootstrapPromise;

    const settled = await sendProbe("settled-capability-probe");
    assert.equal(settled.ok, true, "a fixed negative capability result is available after the wrapper attempt settles");
    assert.equal(settled.report.processType, "unknown");
    assert.equal(settled.report.electronBuiltin, false);
    assert.equal(JSON.stringify(settled).includes(token), false);

    const settledAsar = await sendProbe("settled-asar-source-probe", "probeAsarSources");
    assert.equal(settledAsar.ok, true, "a fixed negative ASAR result is available after the wrapper attempt settles");
    assert.equal(settledAsar.report.status, "rejected");
    assert.equal(settledAsar.report.code, "physical_fs_unavailable");
    assert.equal(JSON.stringify(settledAsar).includes(token), false);

    const settledCandidate = await sendProbe("settled-asar-candidate-probe", "probeAsarCandidates");
    assert.equal(settledCandidate.ok, true, "fixed candidate diagnostics are available after bootstrap settles");
    assert.equal(settledCandidate.report.proofLevel, ASAR_CANDIDATE_PROOF_LEVEL);
    assert.equal(settledCandidate.report.status, "rejected", "the test app tree has no virtual ASAR entries");
    assert.equal(JSON.stringify(settledCandidate).includes(token), false);
    const duplicateCandidate = await sendProbe("duplicate-asar-candidate-probe", "probeAsarCandidates");
    assert.equal(duplicateCandidate.ok, false);
    assert.equal(duplicateCandidate.code, "asar_candidate_probe_already_dispatched");
  } finally {
    if (controller) await controller.close().catch(() => {});
  }
});

test("bootstrap reserves a single in-flight run and reports capability-only status", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-bootstrap-"));
  const reportBase = path.join(runtimeDir, "report");
  const modulePath = path.resolve(__dirname, "../agent/bootstrap.cjs");
  const appPath = path.resolve(__dirname, "..");
  const fakeWrapperPath = path.join(runtimeDir, "cached-wrapper.node");
  let adds = 0;
  let removes = 0;
  const service = {
    addKernelAVSDKListener() { adds += 1; return `id-${adds}`; },
    removeKernelAVSDKListener() { removes += 1; },
    setActionFromAVSDK() {},
  };
  const wrapperModule = { filename: fakeWrapperPath, exports: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } } };
  require.cache[fakeWrapperPath] = wrapperModule;
  const broker = createBroker({
    targetPid: process.pid,
    modulePath,
    appPath,
    reportPath: reportBase,
    connectionGeneration: 21,
    ipcToken: testToken(),
    maxLifetimeMs: 30_000,
  });
  let controller;
  try {
    await broker.start();
    const configPath = path.join(runtimeDir, "config.json");
    await broker.writeConfigFile(configPath);
    const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
    const initializing = bootstrap(config);
    await assert.rejects(bootstrap(config), { code: "bootstrap_initializing" });
    controller = await initializing;
    const persisted = JSON.parse(fs.readFileSync(`${reportBase}.agent.json`, "utf8"));
    assert.equal(persisted.ipc.connected, true);
    assert.equal(persisted.ipc.handshaken, true);
    assert.equal(persisted.ipc.connectionGeneration, 21);
    assert.equal(persisted.listenerRegistered, false);
    assert.equal(persisted.agent.listenerRegistered, false);
    assert.equal((await bootstrap(config)), controller, "same active config returns the existing controller");
    const differentRun = { ...config, connectionGeneration: 22 };
    await assert.rejects(bootstrap(differentRun), { code: "duplicate_bootstrap_active" });

    const initial = await broker.statusRequest();
    assert.equal(initial.bootstrap.wrapperFound, true);
    assert.equal(initial.bootstrap.sessionFound, true);
    assert.equal(initial.bootstrap.serviceAvailable, true);
    assert.equal(initial.bootstrap.wrapperHashPinned, false);
    assert.equal(initial.bootstrap.listenerRemovalMode, "unverified");
    assert.equal(initial.bootstrap.listenerRegistered, false);
    assert.equal(initial.bootstrap.processPid, process.pid);
    assert.equal(typeof initial.bootstrap.nodeVersion, "string");
    assert.equal(adds, 0, "bootstrap must not attach a listener automatically");

    await assert.rejects(broker.attach(), { code: "listener_removal_contract_unverified" });
    assert.equal(adds, 0, "unknown wrapper hash must not attach by guessing the remove argument");
    assert.equal(removes, 0);
    assert.equal(JSON.parse(fs.readFileSync(`${reportBase}.agent.json`, "utf8")).listenerRegistered, false);
    const stopped = await broker.detachAndStop();
    assert.equal(stopped.detachedConfirmed, true);
    assert.equal((await controller.close()).ok, true);
  } finally {
    if (controller) await controller.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    delete require.cache[fakeWrapperPath];
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("bootstrap keeps attach unavailable during probe and detaches before rejecting an initialization disconnect", { skip: process.platform !== "win32" }, async () => {
  for (const sendAttach of [true, false]) {
    const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-probe-disconnect-"));
    const reportBase = path.join(runtimeDir, "report");
    const fakeWrapperPath = path.join(runtimeDir, "cached-wrapper.node");
    const generation = sendAttach ? 31 : 32;
    let resolveSession;
    let sessionRequested;
    const sessionRequest = new Promise((resolve) => { sessionRequested = resolve; });
    const activeListenerIds = new Set();
    const removed = [];
    let adds = 0;
    const service = {
      addKernelAVSDKListener() {
        const id = `probe-disconnect-${++adds}`;
        activeListenerIds.add(id);
        return id;
      },
      removeKernelAVSDKListener(id) {
        removed.push(id);
        activeListenerIds.delete(id);
      },
      setActionFromAVSDK() {},
    };
    const wrapperModule = {
      filename: fakeWrapperPath,
      exports: {
        NodeIQQNTWrapperSession: {
          getNTWrapperSession() {
            sessionRequested();
            return new Promise((resolve) => { resolveSession = resolve; });
          },
        },
      },
    };
    require.cache[fakeWrapperPath] = wrapperModule;
    const broker = createBroker({
      targetPid: process.pid,
      modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
      appPath: path.resolve(__dirname, ".."),
      reportPath: reportBase,
      connectionGeneration: generation,
      ipcToken: testToken(),
      maxLifetimeMs: 30_000,
    });
    let initializing;
    try {
      await broker.start();
      const configPath = path.join(runtimeDir, "config.json");
      await broker.writeConfigFile(configPath);
      initializing = bootstrap(JSON.parse(fs.readFileSync(configPath, "utf8")));
      await sessionRequest;

      if (sendAttach) await assert.rejects(broker.attach(), { code: "agent_unavailable" });
      await broker.stop({ confirmDetach: false });
      await waitUntil(() => {
        try {
          return JSON.parse(fs.readFileSync(`${reportBase}.agent.json`, "utf8")).lastFailureCode === "pipe_disconnected";
        } catch { return false; }
      });

      // Resolve the host-session lookup only after the authenticated pipe is gone.
      resolveSession({ getAVSDKService: () => service });
      await assert.rejects(initializing, { code: "pipe_disconnected" });
      assert.equal(activeListenerIds.size, 0, "no listener may survive initialization disconnect");
      assert.equal(removed.length, adds, "every listener added before disconnect must be removed by its returned ID");
      assert.equal(globalThis[Symbol.for("nachobot.qqcall.bootstrap.v1")], undefined, "failed initialization must release the duplicate-bootstrap guard");
      const persisted = JSON.parse(fs.readFileSync(`${reportBase}.agent.json`, "utf8"));
      assert.equal(persisted.ipc.connected, false);
      assert.equal(persisted.ipc.handshaken, false);
      assert.equal(persisted.ipc.connectionGeneration, generation);
      assert.equal(persisted.listenerRegistered, false);
    } finally {
      if (resolveSession) resolveSession({ getAVSDKService: () => service });
      await initializing?.catch(() => {});
      await broker.stop({ confirmDetach: false }).catch(() => {});
      delete require.cache[fakeWrapperPath];
      fs.rmSync(runtimeDir, { recursive: true, force: true });
    }
  }
});

test("pipe client replays an early disconnect to a late cleanup handler", { skip: process.platform !== "win32" }, async () => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-late-disconnect-"));
  const ipcToken = testToken();
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: 41,
    ipcToken,
    maxLifetimeMs: 30_000,
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: 41, ipcToken });
    await client.connect();
    await broker.stop({ confirmDetach: false });
    await waitUntil(() => !client.status().connected);
    let resolveNotification;
    const notification = new Promise((resolve) => { resolveNotification = resolve; });
    client.onDisconnect((code) => resolveNotification(code));
    assert.equal(await notification, "pipe_disconnected");
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
    fs.rmSync(runtimeDir, { recursive: true, force: true });
  }
});

test("renderer script metadata RPC is generation-bound, authenticated, empty-payload, one-shot, and diagnostic-only", { skip: process.platform !== "win32" }, async (t) => {
  const runtimeDir = fs.mkdtempSync(path.join(os.tmpdir(), "nachobot-qqcall-renderer-script-metadata-rpc-"));
  t.after(() => fs.rmSync(runtimeDir, { recursive: true, force: true }));
  const generation = 998;
  const token = testToken();
  let probeCalls = 0;
  const publishedEvents = [];
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtimeDir, "report"),
    connectionGeneration: generation,
    ipcToken: token,
    onEvent(op, payload) { publishedEvents.push({ op, payload }); },
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({
      pipeName: broker.pipeName,
      connectionGeneration: generation,
      ipcToken: token,
      onProbeRendererScriptMetadata() { probeCalls += 1; return FIXED_RENDERER_SCRIPT_METADATA_REPORT; },
    });
    await client.connect();
    await assert.rejects(broker.probeRendererScriptMetadata(), { code: "renderer_script_metadata_probe_not_ready" });
    assert.equal(probeCalls, 0, "failed readiness does not spend the one-shot dispatch");
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid + 1, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid + 1, connectionGeneration: generation });
    await assert.rejects(broker.probeRendererScriptMetadata(), { code: "renderer_script_metadata_probe_not_ready" });
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation + 1 });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation + 1 });
    await assert.rejects(broker.probeRendererScriptMetadata(), { code: "renderer_script_metadata_probe_not_ready" });
    client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    client.publishEvent("bootstrap_status", { bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
    assert.deepEqual(await broker.probeRendererScriptMetadata(), FIXED_RENDERER_SCRIPT_METADATA_REPORT);
    assert.equal(probeCalls, 1);
    await assert.rejects(broker.probeRendererScriptMetadata(), { code: "renderer_script_metadata_probe_already_dispatched" });
    assert.equal(probeCalls, 1);
    assert.equal(publishedEvents.some((event) => JSON.stringify(event).includes("script-metadata")), false);
    assert.equal(publishedEvents.some((event) => JSON.stringify(event).includes("sourceText")), false);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});

test("renderer script metadata IPC rejects selectors and stale bindings, then rejects an unknown proof shape", { skip: process.platform !== "win32" }, async (t) => {
  const generation = 999;
  const token = testToken();
  const pipeName = `${PIPE_PREFIX}${process.pid}-${randomBytes(8).toString("hex")}`;
  let client;
  let socket;
  let serverSequence = 0;
  let probeCalls = 0;
  const replies = new Map();
  const server = net.createServer((connection) => {
    socket = connection;
    const decoder = new FrameDecoder({ onMessage(message) {
      if (message.type === "request" && message.op === "handshake") {
        socket.write(encodeFrame(makeEnvelope("response", ++serverSequence, generation, message.requestId, "handshake", {
          ok: true, schema: 1, brokerProtocol: 1,
        }, token)));
        return;
      }
      if (message.type === "response") {
        const waiter = replies.get(message.requestId);
        if (waiter) { replies.delete(message.requestId); waiter(message); }
      }
    } });
    connection.on("data", (chunk) => decoder.push(chunk));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipeName, resolve);
  });
  t.after(async () => {
    if (client) await client.close().catch(() => {});
    if (socket && !socket.destroyed) socket.destroy();
    await new Promise((resolve) => server.close(() => resolve()));
  });
  client = createPipeClient({
    pipeName,
    connectionGeneration: generation,
    ipcToken: token,
    onProbeRendererScriptMetadata() {
      probeCalls += 1;
      return { ...FIXED_RENDERER_SCRIPT_METADATA_REPORT, proofLevel: "nativeJoinVerified" };
    },
  });
  await client.connect();
  const send = async (id, payload) => {
    const reply = new Promise((resolve) => replies.set(id, resolve));
    socket.write(encodeFrame(makeEnvelope("request", ++serverSequence, generation, id, "probeRendererScriptMetadata", payload, token)));
    return reply;
  };
  const malformed = await send("script-metadata-malformed", { sourceText: "private" });
  assert.equal(malformed.payload.code, "invalid_renderer_script_metadata_probe_payload");
  assert.equal(probeCalls, 0);
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid + 1, connectionGeneration: generation });
  const wrongPid = await send("script-metadata-wrong-pid", {});
  assert.equal(wrongPid.payload.code, "renderer_script_metadata_probe_not_ready");
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation + 1 });
  const wrongGeneration = await send("script-metadata-wrong-generation", {});
  assert.equal(wrongGeneration.payload.code, "renderer_script_metadata_probe_not_ready");
  client.setBootstrapStatus({ bootstrapLoaded: true, processPid: process.pid, connectionGeneration: generation });
  const unknownProof = await send("script-metadata-unknown-proof", {});
  assert.equal(unknownProof.payload.code, "renderer_script_metadata_probe_invalid_result");
  assert.doesNotMatch(JSON.stringify(unknownProof), /nativeJoinVerified|sourceText|private/);
  const duplicate = await send("script-metadata-duplicate", {});
  assert.equal(duplicate.payload.code, "renderer_script_metadata_probe_already_dispatched");
  assert.equal(probeCalls, 1);
});
