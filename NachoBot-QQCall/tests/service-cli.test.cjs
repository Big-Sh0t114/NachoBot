"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { PassThrough, Writable } = require("node:stream");
const { createBroker } = require("../probe/broker.cjs");
const { createPipeClient } = require("../probe/ipc.cjs");
const { parseArgs, startCli } = require("../bridge/cli.cjs");

const HOST_TOKEN = "service-host-token-value-with-sufficient-private-length-012345";
const CONTROL_TOKEN = "service-control-token-value-with-sufficient-private-length-0123";

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

async function waitFor(predicate, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("service_cli_test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

function sendJson(response, status, value) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, { "content-type": "application/json", "content-length": body.length });
  response.end(body);
}

async function startFakeHost({ hostGeneration = 31, onInvoke = () => {}, readinessGeneration = () => hostGeneration } = {}) {
  const server = http.createServer((request, response) => {
    void (async () => {
      if (request.headers.authorization !== `Bearer ${HOST_TOKEN}`) {
        request.resume();
        sendJson(response, 401, { ok: false, code: "unauthorized" });
        return;
      }
      if (request.method === "GET" && request.url === "/healthz") {
        const currentGeneration = readinessGeneration();
        sendJson(response, 200, { ok: true, status: {
          hostGeneration: currentGeneration,
          ready: currentGeneration === hostGeneration,
          pluginFound: true,
          hasPostMessage: true,
          rendererGone: false,
          stopped: false,
          outputFailed: false,
          readinessError: null,
        } });
        return;
      }
      if (request.method === "POST" && request.url === "/v1/invoke") {
        const payload = JSON.parse(await readBody(request));
        onInvoke(payload);
        sendJson(response, 200, { posted: true, id: payload.id, command: payload.command });
        return;
      }
      request.resume();
      sendJson(response, 404, { ok: false, code: "not_found" });
    })().catch(() => sendJson(response, 500, { ok: false, code: "host_test_failed" }));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    server,
    port: server.address().port,
    hostGeneration,
    async close() {
      const closed = new Promise((resolve) => server.close(resolve));
      server.closeAllConnections?.();
      await closed;
    },
  };
}

function makeDirectory(t, label) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), `qqcall-${label}-`));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function writePrivateFile(filePath, value) {
  fs.writeFileSync(filePath, JSON.stringify(value), { mode: 0o600 });
}

async function cliOptions(directory, { hostPort, hostGeneration = 31, connectionGeneration = 32, serviceMode = true, autoBootstrap = false, loginFile, identitySourceFile, identityOutputFile } = {}) {
  const hostTokenFile = path.join(directory, "host-private.json");
  const controlTokenFile = path.join(directory, "control-private.json");
  writePrivateFile(hostTokenFile, { ipcToken: HOST_TOKEN });
  writePrivateFile(controlTokenFile, { ipcToken: CONTROL_TOKEN });
  const args = [
    "--pid", String(process.pid),
    "--app-path", process.cwd(),
    "--token-file", hostTokenFile,
    "--host-port", String(hostPort),
    "--output-port", String(await reservePort()),
  ];
  args.push(
    "--host-generation", String(hostGeneration),
    "--connection-generation", String(connectionGeneration),
    "--config", path.join(directory, "private-config.json"),
    "--report", path.join(directory, "private-report"),
    "--service-mode",
    "--control-port", "0",
    "--control-token-file", controlTokenFile,
    "--account-uin", "70001",
  );
  if (autoBootstrap) {
    args.push("--auto-bootstrap");
    if (loginFile !== undefined) args.push("--login-file", loginFile);
    if (identitySourceFile !== undefined) args.push("--identity-source-file", identitySourceFile);
    if (identityOutputFile !== undefined) args.push("--identity-output-file", identityOutputFile);
  }
  return parseArgs(args);
}

function makeStreams() {
  const input = new PassThrough();
  let stdout = "";
  let stderr = "";
  const output = new Writable({ write(chunk, _encoding, callback) { stdout += chunk.toString("utf8"); callback(); } });
  const errorOutput = new Writable({ write(chunk, _encoding, callback) { stderr += chunk.toString("utf8"); callback(); } });
  return {
    input,
    output,
    errorOutput,
    get stdout() { return stdout; },
    get stderr() { return stderr; },
  };
}

function fakeAgent({ detachSucceeds = true, counts = {}, accountPath = "C:\\private\\account", getAccountPath } = {}) {
  let listenerRegistered = false;
  return {
    async attach() {
      counts.attach = (counts.attach || 0) + 1;
      listenerRegistered = true;
      return { ok: true, status: { listenerRegistered: true, state: "attached" } };
    },
    async detach() {
      counts.detach = (counts.detach || 0) + 1;
      if (detachSucceeds) listenerRegistered = false;
      return detachSucceeds
        ? { ok: true, status: { listenerRegistered: false, state: "detached" } }
        : { ok: false, code: "listener_removal_failed", status: { listenerRegistered: true, state: "attached" } };
    },
    async getAccountPath({ uin, generation }) {
      counts.getAccountPath = (counts.getAccountPath || 0) + 1;
      return typeof getAccountPath === "function" ? getAccountPath({ uin, generation }) : { ok: true, accountPath };
    },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true, status: { listenerRegistered } }; },
    status() { return { listenerRegistered, state: listenerRegistered ? "attached" : "detached" }; },
  };
}

async function connectAgent(cli, agent) {
  const client = createPipeClient({
    pipeName: cli.broker.pipeName,
    connectionGeneration: cli.broker.status().connectionGeneration,
    ipcToken: HOST_TOKEN,
  });
  client.setAgent(agent);
  await client.connect();
  return client;
}

function bootstrapPayload(cli, overrides = {}) {
  const status = cli.broker.status();
  return {
    sessionFound: true,
    serviceAvailable: true,
    listenerRegistered: false,
    connectionGeneration: status.connectionGeneration,
    processPid: status.targetPid,
    ...overrides,
  };
}

function parseLines(value) {
  return value.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function postHostOutput(port, { id, hostGeneration = 31, command = 1, value = [0, ""] }) {
  const body = Buffer.from(JSON.stringify({ id, command, value, hostGeneration }));
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1",
      port,
      method: "POST",
      path: "/v1/avsdk/output",
      headers: {
        authorization: `Bearer ${HOST_TOKEN}`,
        "content-type": "application/json",
        "content-length": body.length,
      },
    }, (response) => {
      response.resume();
      response.once("end", () => resolve(response.statusCode));
    });
    request.once("error", reject);
    request.end(body);
  });
}

test("service and auto-bootstrap flags are strict and lifetime remains probe-only", () => {
  const base = [
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "host.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
  ];
  assert.equal(parseArgs([...base, "--service-mode"]).serviceMode, true);
  assert.throws(() => parseArgs([...base, "--service-mode", "true"]), { code: "invalid_cli_arguments" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--service-mode"]), { code: "duplicate_cli_option" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--lifetime-ms", "1000"]), { code: "service_mode_lifetime_conflict" });
  assert.throws(() => parseArgs([...base, "--auto-bootstrap"]), { code: "auto_bootstrap_requires_service_mode" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--auto-bootstrap"]), { code: "auto_bootstrap_requires_identity_source" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--login-file", "identity.json"]), { code: "login_file_requires_auto_bootstrap" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--auto-bootstrap", "--login-file", "identity.json"]), { code: "auto_bootstrap_requires_control" });
  assert.throws(() => parseArgs([...base, "--service-mode", "--auto-bootstrap", "--identity-source-file", "onebot.json"]), { code: "identity_source_options_incomplete" });
  assert.throws(() => parseArgs([
    ...base, "--service-mode", "--auto-bootstrap", "--login-file", "identity.json",
    "--identity-source-file", "onebot.json", "--identity-output-file", "generated.json",
  ]), { code: "identity_sources_conflict" });
  assert.equal(parseArgs([
    ...base, "--service-mode", "--auto-bootstrap", "--login-file", "identity.json",
    "--control-port", "0", "--control-token-file", "control.json", "--account-uin", "70001",
  ]).autoBootstrap, true);
  assert.equal(parseArgs([
    ...base, "--service-mode", "--auto-bootstrap", "--identity-source-file", "onebot.json",
    "--identity-output-file", "generated.json", "--control-port", "0", "--control-token-file", "control.json",
    "--account-uin", "70001",
  ]).identityOutputFile, path.resolve("generated.json"));
  assert.throws(() => createBroker({ serviceMode: "true" }), { code: "invalid_service_mode" });
  assert.throws(() => createBroker({ serviceMode: true, maxLifetimeMs: 1000 }), { code: "service_mode_lifetime_conflict" });
});

test("service Broker stays alive across mocked probe lifetime while bounded probe still expires", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const makeBroker = (overrides) => createBroker({
    targetPid: process.pid,
    modulePath: __filename,
    appPath: process.cwd(),
    reportPath: path.join(os.tmpdir(), `qqcall-broker-${process.pid}`),
    connectionGeneration: 55,
    ipcToken: HOST_TOKEN,
    ...overrides,
  });
  const service = makeBroker({ serviceMode: true });
  await service.start();
  t.after(async () => { try { await service.stop(); } catch { /* preserve assertions */ } });
  t.mock.timers.tick(60 * 60 * 1000);
  assert.equal(service.status().stopped, false);
  await service.stop();

  const probe = makeBroker({ maxLifetimeMs: 1000 });
  await probe.start();
  t.after(async () => { try { await probe.stop({ confirmDetach: false }); } catch { /* preserve assertions */ } });
  t.mock.timers.tick(1000);
  assert.equal(probe.status().stopped, true, "bounded probe expiry still stops at its configured deadline");
});

test("service stdin pause and resume preserve the authenticated native binding", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "adapter-recovery");
  const host = await startFakeHost();
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts }));
  t.after(async () => { await cli.close(); await client.close(); });
  await cli.handleCommand("attach");
  assert.equal(cli.bridge.status().attached, true);
  await waitFor(() => cli.host.status().available === true);
  await cli.handleCommand("adapter-pause");
  await cli.handleCommand("adapter-resume unexpected");
  assert.match(streams.stdout, /"command":"adapter-pause","result":\{"adapterAvailable":false\}/);
  assert.match(streams.stdout, /"command":"adapter-resume","code":"invalid_adapter_availability"/);
  await cli.handleCommand("adapter-resume");
  assert.match(streams.stdout, /"command":"adapter-resume","result":\{"adapterAvailable":true\}/);
  assert.equal(cli.bridge.status().attached, true);
  assert.equal(cli.broker.status().stopped, false);
  assert.equal(counts.attach, 1);
  assert.equal(counts.detach || 0, 0);
  await cli.handleCommand("adapter-pause");
  await host.close();
  await cli.handleCommand("adapter-resume");
  assert.match(streams.stdout, /"command":"adapter-resume","code":"adapter_recovery_native_unavailable"/);
});

test("service mode ignores stdin EOF and returns an explicit unconfirmed detach result", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-eof");
  const options = await cliOptions(directory, { hostPort: await reservePort() });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const agent = fakeAgent({ detachSucceeds: false, counts });
  const client = await connectAgent(cli, agent);
  t.after(async () => {
    try { await cli.close(); } catch { /* keep the assertion result */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  streams.input.write("attach\n");
  await waitFor(() => cli.bridge.status().attached === true);
  streams.input.end();
  await waitFor(() => cli.interface.closed === true);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(cli.broker.status().stopped, false, "service mode remains live after stdin EOF");
  assert.equal(cli.bridge.status().attached, true, "stdin EOF did not detach the live listener");
  assert.doesNotMatch(streams.stdout, /stdin_eof_shutdown/);

  const result = await cli.close();
  assert.deepEqual(result, { stopped: true, detachedConfirmed: false, code: "listener_removal_failed" });
  assert.equal(counts.attach, 1);
  assert.equal(counts.detach, 1);
  assert.equal(cli.broker.status().stopped, true, "local service resources close after an unconfirmed detach");
  assert.doesNotMatch(streams.stdout, /detachedConfirmed":true/);
});

test("service quit JSON preserves an unconfirmed detach outcome for the supervisor", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-quit-unconfirmed");
  const options = await cliOptions(directory, { hostPort: await reservePort(), serviceMode: true });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const client = await connectAgent(cli, fakeAgent({ detachSucceeds: false }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  streams.input.write("attach\nquit\n");
  await waitFor(() => cli.broker.status().stopped === true);
  const quit = parseLines(streams.stdout).find((line) => line.command === "quit");
  assert.deepEqual(quit.result, { stopped: true, detachedConfirmed: false, code: "listener_removal_failed" });
  assert.equal(cli.bridge.status().stopped, false, "a failed native detach is never projected as a clean Bridge stop");
  assert.doesNotMatch(streams.stdout, /"command":"quit","result":\{"stopped":true,"detachedConfirmed":true/);
});

test("auto-bootstrap attaches and posts login once, then waits for the matching native login ACK", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-auto-login");
  const identityFile = path.join(directory, "private-login.json");
  const identity = { selfUid: "secret-uid-value", selfUin: "70001", accountPath: "C:\\private\\account" };
  writePrivateFile(identityFile, identity);
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  await waitFor(() => cli.host.status().available === true);
  assert.equal(cli.controlService.status().serviceInstance, parseLines(streams.stdout).find((line) => line.kind === "bridge_ready").controlServiceInstance);
  assert.doesNotMatch(streams.stdout, /private-login\.json|secret-uid-value|70001|private\\account|private-config\.json/);
  client.publishEvent("bootstrap_status", bootstrapPayload(cli, { processPid: process.pid + 1 }));
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(counts.attach || 0, 0, "stale process evidence cannot attach");
  assert.equal(invocations.length, 0, "stale process evidence cannot log in");

  const acceptedBootstrap = bootstrapPayload(cli);
  client.publishEvent("kernel_event", acceptedBootstrap);
  client.publishEvent("bootstrap_status", { ...acceptedBootstrap, serviceAvailable: false });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(counts.attach || 0, 0, "non-bootstrap and unavailable-service evidence cannot attach");
  assert.equal(invocations.length, 0, "non-bootstrap and unavailable-service evidence cannot log in");
  client.publishEvent("bootstrap_status", acceptedBootstrap);
  await waitFor(() => counts.attach === 1 && invocations.length === 1);
  client.publishEvent("bootstrap_status", acceptedBootstrap);
  client.publishEvent("bootstrap_status", acceptedBootstrap);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.equal(counts.attach, 1, "duplicate bootstrap status did not attach twice");
  assert.equal(invocations.length, 1, "duplicate bootstrap status did not post login twice");
  assert.equal(counts.getAccountPath, 1, "auto-login verifies the authenticated account path once before posting");
  assert.equal(invocations[0].command, 1);
  assert.equal(cli.bridge.status().loginState, "pending", "posted login is not treated as ready");

  const outputPort = cli.host.status().outputPort;
  assert.equal(await postHostOutput(outputPort, { id: invocations[0].id }), 202);
  await waitFor(() => cli.bridge.status().loginState === "ready");
  await waitFor(() => parseLines(streams.stdout).some((line) => line.kind === "auto_login_ready"));
  assert.equal(counts.attach, 1);
  assert.equal(invocations.length, 1);
  assert.match(streams.stdout, /"loginState":"ready"/);
  assert.match(streams.stdout, /"accountPathVerified":true/);

  const shutdown = await cli.close();
  assert.deepEqual(shutdown, { stopped: true, detachedConfirmed: true });
  assert.equal(counts.detach, 1);
  await waitFor(() => client.status().connected === false);
});

test("replaced auto-login identity is rejected before listener or login mutation", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-replaced-identity");
  const identityFile = path.join(directory, "private-login.json");
  writePrivateFile(identityFile, { selfUid: "original-private-uid", selfUin: "70001", accountPath: "C:\\private\\account" });
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  writePrivateFile(identityFile, { selfUid: "replacement-private-uid", selfUin: "70001", accountPath: "C:\\private\\account" });
  const correct = bootstrapPayload(cli);
  client.publishEvent("bootstrap_status", { ...correct, connectionGeneration: correct.connectionGeneration + 1 });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(counts.attach || 0, 0, "stale generation cannot mutate the listener");
  client.publishEvent("bootstrap_status", correct);
  await waitFor(() => streams.stderr.includes("identity_file_changed"));
  assert.equal(counts.attach || 0, 0, "replaced identity is rejected before attach");
  assert.equal(invocations.length, 0);
  assert.notEqual(cli.bridge.status().loginState, "ready");
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /original-private-uid|replacement-private-uid|70001|private-login\.json/);
});

test("native account-path mismatch blocks automatic login after confirmed attach", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-account-path-mismatch");
  const identityFile = path.join(directory, "private-login.json");
  writePrivateFile(identityFile, { selfUid: "private-uid", selfUin: "70001", accountPath: "C:\\other\\account" });
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts, accountPath: "C:\\private\\account" }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  await waitFor(() => cli.host.status().available === true);
  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => streams.stderr.includes("auto_login_account_path_mismatch"));
  assert.equal(counts.attach, 1, "the authenticated listener attach was confirmed first");
  assert.equal(counts.getAccountPath, 1);
  assert.equal(invocations.length, 0, "a stale account directory never reaches cmd 1");
  assert.notEqual(cli.bridge.status().loginState, "ready");
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /C:\\other|C:\\private|private-uid|70001/);
});

test("identity replacement or Host generation loss during account-path RPC blocks login", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-account-path-await");
  const identityFile = path.join(directory, "private-login.json");
  writePrivateFile(identityFile, { selfUid: "private-uid", selfUin: "70001", accountPath: "C:\\private\\account" });
  const invocations = [];
  let hostReadinessGeneration = 31;
  let cli;
  const host = await startFakeHost({
    onInvoke: (payload) => invocations.push(payload),
    readinessGeneration: () => hostReadinessGeneration,
  });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts, getAccountPath: async ({ uin, generation }) => {
    assert.equal(uin, "70001");
    assert.equal(generation, options.connectionGeneration);
    writePrivateFile(identityFile, { selfUid: "replacement-private-uid", selfUin: "70001", accountPath: "C:\\private\\account" });
    hostReadinessGeneration = 99;
    await cli.host.checkHealth();
    return { ok: true, accountPath: "C:\\private\\account" };
  } }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  await waitFor(() => cli.host.status().available === true);
  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => streams.stderr.includes("identity_file_changed"));
  assert.equal(counts.attach, 1);
  assert.equal(counts.getAccountPath, 1);
  assert.equal(invocations.length, 0, "revalidated identity change blocks cmd 1");
  assert.notEqual(cli.bridge.status().loginState, "ready");
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /replacement-private-uid|70001|private-login\.json/);
});

test("Host generation invalidation during account-path RPC blocks login after await", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-host-generation-await");
  const identityFile = path.join(directory, "private-login.json");
  writePrivateFile(identityFile, { selfUid: "private-uid", selfUin: "70001", accountPath: "C:\\private\\account" });
  const invocations = [];
  let hostReadinessGeneration = 31;
  let cli;
  const host = await startFakeHost({
    onInvoke: (payload) => invocations.push(payload),
    readinessGeneration: () => hostReadinessGeneration,
  });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts, getAccountPath: async () => {
    hostReadinessGeneration = 99;
    await cli.host.checkHealth();
    return { ok: true, accountPath: "C:\\private\\account" };
  } }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  await waitFor(() => cli.host.status().available === true);
  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => streams.stderr.includes("auto_login_generation_mismatch"));
  assert.equal(counts.attach, 1);
  assert.equal(counts.getAccountPath, 1);
  assert.equal(invocations.length, 0, "stale Host generation never reaches cmd 1");
  assert.notEqual(cli.bridge.status().loginState, "ready");
});

test("first-install identity source resolves the native account path, publishes private identity, then waits for login ACK", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-first-install-identity");
  const identitySourceFile = path.join(directory, "private-onebot.json");
  const identityOutputFile = path.join(directory, "private-login.json");
  const nativeAccountPath = "C:\\Users\\Bot\\Documents\\Tencent Files\\70001";
  writePrivateFile(identitySourceFile, { selfUid: "first-install-private-uid", selfUin: "70001" });
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, {
    hostPort: host.port,
    autoBootstrap: true,
    identitySourceFile,
    identityOutputFile,
  });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts, accountPath: nativeAccountPath }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  await waitFor(() => cli.host.status().available === true);
  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => counts.attach === 1 && invocations.length === 1 && fs.existsSync(identityOutputFile));
  assert.deepEqual(JSON.parse(fs.readFileSync(identityOutputFile, "utf8")), {
    selfUid: "first-install-private-uid",
    selfUin: "70001",
    accountPath: nativeAccountPath,
  });
  assert.equal(counts.getAccountPath, 1);
  assert.equal(invocations[0].command, 1);
  assert.equal(cli.bridge.status().loginState, "pending", "the post receipt is not a login-ready ACK");
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /first-install-private-uid|70001|Tencent Files|private-onebot\.json|private-login\.json/);

  assert.equal(await postHostOutput(cli.host.status().outputPort, { id: invocations[0].id }), 202);
  await waitFor(() => cli.bridge.status().loginState === "ready");
  await waitFor(() => parseLines(streams.stdout).some((line) => line.kind === "auto_login_ready"));
  assert.equal(counts.attach, 1);
  assert.equal(invocations.length, 1);
});

test("first-install bootstrap rejects a replaced identity source before attaching", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-first-install-replaced");
  const identitySourceFile = path.join(directory, "private-onebot.json");
  const identityOutputFile = path.join(directory, "private-login.json");
  writePrivateFile(identitySourceFile, { selfUid: "original-private-uid", selfUin: "70001" });
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, identitySourceFile, identityOutputFile });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  writePrivateFile(identitySourceFile, { selfUid: "replacement-private-uid", selfUin: "70001" });
  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => streams.stderr.includes("identity_file_changed"));
  assert.equal(counts.attach || 0, 0);
  assert.equal(counts.getAccountPath || 0, 0);
  assert.equal(invocations.length, 0);
  assert.equal(fs.existsSync(identityOutputFile), false);
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /original-private-uid|replacement-private-uid|70001|private-onebot\.json/);
});

test("first-install bootstrap requires an available absolute native account path before output or login", {
  skip: process.platform !== "win32" ? "Windows named pipes are required" : false,
}, async (t) => {
  const directory = makeDirectory(t, "service-first-install-no-path");
  const identitySourceFile = path.join(directory, "private-onebot.json");
  const identityOutputFile = path.join(directory, "private-login.json");
  writePrivateFile(identitySourceFile, { selfUid: "private-uid", selfUin: "70001" });
  const invocations = [];
  const host = await startFakeHost({ onInvoke: (payload) => invocations.push(payload) });
  t.after(() => host.close());
  const options = await cliOptions(directory, { hostPort: host.port, autoBootstrap: true, identitySourceFile, identityOutputFile });
  const streams = makeStreams();
  const cli = await startCli(options, streams);
  const counts = {};
  const client = await connectAgent(cli, fakeAgent({ counts, getAccountPath: async () => ({ ok: false, code: "account_path_unavailable" }) }));
  t.after(async () => {
    try { await cli.close(); } catch { /* preserve assertions */ }
    try { await client.close(); } catch { /* close local pipe */ }
  });

  client.publishEvent("bootstrap_status", bootstrapPayload(cli));
  await waitFor(() => streams.stderr.includes("auto_login_account_path_unavailable"));
  assert.equal(counts.attach, 1, "authenticated attach is confirmed before querying the account path");
  assert.equal(counts.getAccountPath, 1);
  assert.equal(invocations.length, 0, "no login command is posted without the native account path");
  assert.equal(fs.existsSync(identityOutputFile), false);
  assert.notEqual(cli.bridge.status().loginState, "ready");
  assert.doesNotMatch(`${streams.stdout}${streams.stderr}`, /private-uid|70001|private-onebot\.json|private-login\.json/);
});

test("first-install identity output must be new and distinct before service startup", async (t) => {
  const directory = makeDirectory(t, "service-first-install-output-exists");
  const identitySourceFile = path.join(directory, "private-onebot.json");
  const identityOutputFile = path.join(directory, "private-login.json");
  writePrivateFile(identitySourceFile, { selfUid: "private-uid", selfUin: "70001" });
  writePrivateFile(identityOutputFile, { sentinel: "preserve-existing-file" });
  const options = await cliOptions(directory, { hostPort: await reservePort(), autoBootstrap: true, identitySourceFile, identityOutputFile });
  const streams = makeStreams();
  await assert.rejects(startCli(options, streams), { code: "identity_output_exists" });
  assert.deepEqual(JSON.parse(fs.readFileSync(identityOutputFile, "utf8")), { sentinel: "preserve-existing-file" });
  assert.equal(streams.stdout, "");
  assert.equal(streams.stderr, "");
});

test("account UIN mismatch rejects auto-bootstrap before local services start", async (t) => {
  const directory = makeDirectory(t, "service-mismatched-uin");
  const identityFile = path.join(directory, "private-login.json");
  writePrivateFile(identityFile, { selfUid: "private-uid", selfUin: "70002", accountPath: "C:\\private\\account" });
  const options = await cliOptions(directory, { hostPort: await reservePort(), autoBootstrap: true, loginFile: identityFile });
  const streams = makeStreams();
  await assert.rejects(startCli(options, streams), { code: "configured_account_mismatch" });
  assert.equal(streams.stdout, "");
  assert.equal(streams.stderr, "");

  const missingOptions = await cliOptions(directory, {
    hostPort: await reservePort(),
    autoBootstrap: true,
    loginFile: path.join(directory, "missing-private-login.json"),
  });
  await assert.rejects(startCli(missingOptions, makeStreams()), { code: "identity_file_unavailable" });

  const partialSourceFile = path.join(directory, "private-onebot.json");
  const partialOutputFile = path.join(directory, "private-full-login.json");
  writePrivateFile(partialSourceFile, { selfUid: "private-uid", selfUin: "70002" });
  const partialOptions = await cliOptions(directory, {
    hostPort: await reservePort(), autoBootstrap: true,
    identitySourceFile: partialSourceFile, identityOutputFile: partialOutputFile,
  });
  await assert.rejects(startCli(partialOptions, makeStreams()), { code: "configured_account_mismatch" });
  assert.equal(fs.existsSync(partialOutputFile), false);
});

test("fatal controller errors are recorded without changing Node's default exit", {
  skip: process.platform !== "win32" ? "the isolated Broker fixture requires Windows named pipes" : false,
}, (t) => {
  const directory = makeDirectory(t, "controller-exit-observer");
  const cliPath = path.resolve(__dirname, "../bridge/cli.cjs");
  const childScript = `
    const fs = require("node:fs");
    const http = require("node:http");
    const net = require("node:net");
    const path = require("node:path");
    const { PassThrough, Writable } = require("node:stream");
    const { parseArgs, startCli } = require(process.argv[1]);
    const directory = process.argv[2];
    const reportBase = process.argv[3];
    const host = http.createServer((request, response) => {
      request.resume();
      if (request.method === "GET" && request.url === "/healthz") {
        const body = Buffer.from(JSON.stringify({ ok: true, status: {
          hostGeneration: 31, ready: true, pluginFound: true, hasPostMessage: true,
          rendererGone: false, stopped: false, outputFailed: false,
        } }));
        response.writeHead(200, { "content-type": "application/json", "content-length": body.length });
        response.end(body);
      } else {
        response.writeHead(404);
        response.end();
      }
    });
    function reservePort() {
      return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
          const port = server.address().port;
          server.close((error) => error ? reject(error) : resolve(port));
        });
      });
    }
    host.listen(0, "127.0.0.1", async () => {
      const hostTokenFile = path.join(directory, "host-token.private.json");
      fs.writeFileSync(hostTokenFile, JSON.stringify({ ipcToken: "private-test-host-token-value-0123456789abcdef" }));
      const options = parseArgs([
        "--pid", String(process.pid), "--app-path", process.cwd(),
        "--token-file", hostTokenFile, "--host-port", String(host.address().port),
        "--output-port", String(await reservePort()), "--host-generation", "31",
        "--connection-generation", "32", "--config", path.join(directory, "config.private.json"),
        "--report", reportBase, "--service-mode",
      ]);
      const sink = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
      await startCli(options, { input: new PassThrough(), output: sink, errorOutput: sink });
      if (process.argv[4] === "fail-write") {
        fs.linkSync = () => { throw new Error("PRIVATE_WRITE_FAILURE_MARKER"); };
      }
      setImmediate(() => {
        const failure = new Error("PRIVATE_CONTROLLER_EXCEPTION_MARKER");
        Object.defineProperty(failure, "code", {
          get() { throw new Error("PRIVATE_CODE_GETTER_MARKER"); },
        });
        throw failure;
      });
    });
  `;
  const reportBase = path.join(directory, "agent-report");
  const result = spawnSync(process.execPath, ["-e", childScript, cliPath, directory, reportBase], {
    encoding: "utf8",
    timeout: 10_000,
    windowsHide: true,
  });
  assert.equal(result.error, undefined, "isolated controller subprocess must exit within its bound");
  assert.equal(result.status, 1, "uncaught exception must retain Node's default fatal exit status");
  assert.match(result.stderr, /PRIVATE_CONTROLLER_EXCEPTION_MARKER/);
  assert.doesNotMatch(result.stderr, /PRIVATE_CODE_GETTER_MARKER/);

  const reportPath = `${reportBase}.controller-exit.private.json`;
  const report = JSON.parse(fs.readFileSync(reportPath, "utf8"));
  assert.deepEqual(Object.keys(report).sort(), [
    "errorClass", "exitCode", "exitedAtMs", "failureCode", "observedAtMs", "origin", "schema",
  ]);
  assert.equal(report.schema, 1);
  assert.equal(report.errorClass, "uncaught_exception");
  assert.equal(report.failureCode, "controller_uncaught_exception");
  assert.equal(report.origin, "uncaughtException");
  assert.equal(report.exitCode, 1);
  assert.ok(report.exitedAtMs >= report.observedAtMs);
  assert.doesNotMatch(JSON.stringify(report), /PRIVATE_CONTROLLER_EXCEPTION_MARKER|PRIVATE_CODE_GETTER_MARKER/);

  const writeFailureDirectory = path.join(directory, "write-failure");
  fs.mkdirSync(writeFailureDirectory);
  const failedReportBase = path.join(writeFailureDirectory, "agent-report");
  const writeFailure = spawnSync(process.execPath, ["-e", childScript, cliPath, directory, failedReportBase, "fail-write"], {
    encoding: "utf8",
    timeout: 10_000,
    windowsHide: true,
  });
  assert.equal(writeFailure.error, undefined);
  assert.equal(writeFailure.status, 1, "diagnostic write failure must not suppress default fatal exit");
  assert.doesNotMatch(writeFailure.stderr, /PRIVATE_WRITE_FAILURE_MARKER/);
  assert.equal(fs.existsSync(`${failedReportBase}.controller-exit.private.json`), false);
});
