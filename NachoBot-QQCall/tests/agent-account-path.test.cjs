"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");
const { createBroker } = require("../probe/broker.cjs");
const { createPipeClient } = require("../probe/ipc.cjs");

const ACCOUNT_PATH = "C:\\private\\QQ account data";
const TOKEN = randomBytes(32).toString("base64url");

test("account path uses an existing wrapper session, validates canonical UIN, and never enters status", async () => {
  const calls = [];
  let sessionLookups = 0;
  let serviceLookups = 0;
  let sessionCreates = 0;
  const session = {
    getAccountPath(uin) { calls.push(uin); return ACCOUNT_PATH; },
    // AVSDK availability is unrelated to this private identity lookup.
    getAVSDKService() { serviceLookups += 1; return null; },
  };
  const agent = createKernelAgent({
    connectionGeneration: 12,
    wrapper: { NodeIQQNTWrapperSession: {
      getNTWrapperSession() { sessionLookups += 1; return session; },
      createNTWrapperSession() { sessionCreates += 1; throw new Error("must_not_create"); },
    } },
  });

  const result = await agent.getAccountPath({ uin: "10023", generation: 12 });
  assert.deepEqual(result, { ok: true, accountPath: ACCOUNT_PATH });
  assert.equal(sessionLookups, 1);
  assert.deepEqual(calls, [10023]);
  assert.equal(agent.status().listenerRegistered, false);
  assert.equal(agent.status().accountPathAvailable, true);
  assert.equal(serviceLookups, 0, "private identity lookup must not probe AVSDK service");
  assert.doesNotMatch(JSON.stringify(agent.status()), /private|QQ account data|10023/);

  for (const uin of ["0", "010023", "+10023", "9007199254740992", 10023, "1".repeat(20)]) {
    assert.equal((await agent.getAccountPath({ uin, generation: 12 })).code, "invalid_account_uin");
  }
  assert.equal((await agent.getAccountPath({ uin: "10023", generation: 11 })).code, "stale_generation");
  assert.deepEqual(calls, [10023], "invalid or stale lookups must not reach the native wrapper");
  assert.equal((await agent.attach()).code, "avsdk_service_unavailable");
  assert.equal(sessionCreates, 0, "account-path lookup must never create a session");
  assert.equal(sessionLookups, 1, "the session is retrieved once and reused");
  assert.equal(serviceLookups, 1, "AVSDK service is looked up only for explicit attach");
});

test("empty or absent session account path uses only the existing wrapper user-data root", async (t) => {
  const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-existing-userdata-"));
  t.after(() => fs.rmSync(runtimeRoot, { recursive: true, force: true }));
  let sessionLookups = 0;
  let sessionCreates = 0;
  let utilCalls = 0;
  let serviceLookups = 0;
  const session = {
    getAccountPath() { return ""; },
    getAVSDKService() { serviceLookups += 1; return null; },
  };
  const wrapperUtil = {
    marker: "receiver-bound",
    getNTUserDataInfoConfig() {
      utilCalls += 1;
      assert.equal(this, wrapperUtil, "the native utility method keeps its wrapper receiver");
      assert.equal(this.marker, "receiver-bound");
      return runtimeRoot;
    },
  };
  const agent = createKernelAgent({
    connectionGeneration: 24,
    wrapper: {
      NodeIQQNTWrapperSession: {
        getNTWrapperSession() { sessionLookups += 1; return session; },
        createNTWrapperSession() { sessionCreates += 1; throw new Error("must_not_create"); },
      },
      NodeQQNTWrapperUtil: wrapperUtil,
    },
  });

  const result = await agent.getAccountPath({ uin: "70001", generation: 24 });
  assert.deepEqual(result, { ok: true, accountPath: runtimeRoot });
  assert.equal(sessionLookups, 1);
  assert.equal(sessionCreates, 0);
  assert.equal(utilCalls, 1);
  assert.equal(serviceLookups, 0, "the readonly root fallback does not probe AVSDK service");
  assert.equal(agent.status().accountPathAvailable, true);
  assert.equal(agent.status().listenerRegistered, false);
  assert.equal(JSON.stringify(agent.status()).includes(runtimeRoot), false);
  assert.equal(JSON.stringify(agent.status()).includes("70001"), false);
});

test("null, undefined, empty and absent primary getters alone permit the root fallback", async (t) => {
  const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-existing-userdata-empty-"));
  t.after(() => fs.rmSync(runtimeRoot, { recursive: true, force: true }));
  for (const primaryValue of [null, undefined, "", "absent"]) {
    let utilCalls = 0;
    const session = {
      ...(primaryValue === "absent" ? {} : { getAccountPath() { return primaryValue; } }),
      getAVSDKService() { throw new Error("not_used"); },
    };
    const wrapperUtil = {
      getNTUserDataInfoConfig() {
        utilCalls += 1;
        assert.equal(this, wrapperUtil);
        return runtimeRoot;
      },
    };
    const agent = createKernelAgent({
      connectionGeneration: 25,
      wrapper: {
        NodeIQQNTWrapperSession: { getNTWrapperSession: () => session },
        NodeQQNTWrapperUtil: wrapperUtil,
      },
    });
    assert.deepEqual(await agent.getAccountPath({ uin: "70002", generation: 25 }), { ok: true, accountPath: runtimeRoot });
    assert.equal(utilCalls, 1);
  }
});

test("bad or throwing primary account path never falls through to the wrapper root", async () => {
  const badValues = [17, false, {}, "relative\\account", "\\rooted", " ", `C:\\${"x".repeat(32768)}`, "C:\\bad\0path"];
  for (const badValue of badValues) {
    let utilCalls = 0;
    const agent = createKernelAgent({
      connectionGeneration: 26,
      wrapper: {
        NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAccountPath: () => badValue }) },
        NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { utilCalls += 1; return "C:\\"; } },
      },
    });
    assert.deepEqual(await agent.getAccountPath({ uin: "70003", generation: 26 }), {
      ok: false, code: "account_path_unavailable",
    });
    assert.equal(utilCalls, 0);
  }

  let malformedUtilCalls = 0;
  const malformedGetter = createKernelAgent({
    connectionGeneration: 26,
    wrapper: {
      NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAccountPath: null }) },
      NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { malformedUtilCalls += 1; return "C:\\"; } },
    },
  });
  assert.deepEqual(await malformedGetter.getAccountPath({ uin: "70003", generation: 26 }), {
    ok: false, code: "account_path_accessor_unreadable",
  });
  assert.equal(malformedUtilCalls, 0);

  let utilCalls = 0;
  const throwing = createKernelAgent({
    connectionGeneration: 27,
    wrapper: {
      NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAccountPath() { throw new Error("private failure"); } }) },
      NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { utilCalls += 1; return "C:\\"; } },
    },
  });
  assert.deepEqual(await throwing.getAccountPath({ uin: "70004", generation: 27 }), {
    ok: false, code: "account_path_lookup_failed",
  });
  assert.equal(utilCalls, 0);
});

test("wrapper root fallback fails closed for absent, throwing, relative, missing, and non-directory roots", async (t) => {
  const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-root-errors-"));
  t.after(() => fs.rmSync(runtimeRoot, { recursive: true, force: true }));
  const regularFile = path.join(runtimeRoot, "not-a-directory");
  fs.writeFileSync(regularFile, "private");
  const cases = [
    { wrapper: {}, expected: "account_path_root_accessor_missing" },
    { wrapper: { NodeQQNTWrapperUtil: null }, expected: "account_path_root_accessor_missing" },
    { wrapper: { NodeQQNTWrapperUtil: {} }, expected: "account_path_root_accessor_missing" },
    { wrapper: { NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { throw new Error("do not echo"); } } }, expected: "account_path_root_lookup_failed" },
    ...[
      null, undefined, "", "relative\\root", "\\rooted", "C:\\missing\\directory", regularFile,
      `C:\\${"z".repeat(32768)}`, "C:\\bad\0root", 1, false, {},
      Promise.resolve(runtimeRoot),
    ].map((root) => ({
      wrapper: { NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { return root; } } },
      expected: "account_path_root_invalid",
    })),
  ];
  for (const item of cases) {
    const agent = createKernelAgent({
      connectionGeneration: 28,
      wrapper: {
        NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAccountPath: () => undefined }) },
        ...item.wrapper,
      },
    });
    const result = await agent.getAccountPath({ uin: "70005", generation: 28 });
    assert.deepEqual(result, { ok: false, code: item.expected });
    assert.doesNotMatch(JSON.stringify(result), /do not echo|directory|70005|C:/);
  }
});

test("generation and UIN validation happen before session or root utility access", async () => {
  let sessionLookups = 0;
  let utilCalls = 0;
  const agent = createKernelAgent({
    connectionGeneration: 29,
    wrapper: {
      NodeIQQNTWrapperSession: { getNTWrapperSession() { sessionLookups += 1; return { getAccountPath: () => "" }; } },
      NodeQQNTWrapperUtil: { getNTUserDataInfoConfig() { utilCalls += 1; return "C:\\"; } },
    },
  });
  assert.deepEqual(await agent.getAccountPath({ uin: "70006", generation: 28 }), { ok: false, code: "stale_generation" });
  assert.deepEqual(await agent.getAccountPath({ uin: "070006", generation: 29 }), { ok: false, code: "invalid_account_uin" });
  assert.equal(sessionLookups, 0);
  assert.equal(utilCalls, 0);
});

test("authenticated named-pipe getAccountPath is a private response, not a status or event", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for this roundtrip" : false,
}, async (t) => {
  const runtime = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-account-path-"));
  t.after(() => fs.rmSync(runtime, { recursive: true, force: true }));
  let nativeCalls = 0;
  const events = [];
  const session = {
    getAccountPath(uin) {
      nativeCalls += 1;
      assert.equal(uin, 70001);
      return ACCOUNT_PATH;
    },
    getAVSDKService() { return null; },
  };
  const agent = createKernelAgent({
    connectionGeneration: 44,
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => session } },
  });
  const broker = createBroker({
    targetPid: process.pid,
    modulePath: path.resolve(__dirname, "../agent/bootstrap.cjs"),
    appPath: path.resolve(__dirname, ".."),
    reportPath: path.join(runtime, "agent"),
    connectionGeneration: 44,
    ipcToken: TOKEN,
    maxLifetimeMs: 30_000,
    onEvent(op, payload) { events.push({ op, payload }); },
  });
  let client;
  try {
    await broker.start();
    client = createPipeClient({ pipeName: broker.pipeName, connectionGeneration: 44, ipcToken: TOKEN });
    await client.connect();
    client.setAgent(agent);
    const initialEventCount = events.length;
    assert.equal(await broker.getAccountPath("70001"), ACCOUNT_PATH);
    assert.equal(nativeCalls, 1);
    const status = await broker.statusRequest();
    assert.equal(status.agent.listenerRegistered, false);
    assert.equal(status.agent.accountPathAvailable, true);
    assert.doesNotMatch(JSON.stringify(status), /private|QQ account data|70001/);
    assert.equal(events.length, initialEventCount, "the private path response must not be broadcast as an event");
    await assert.rejects(broker.getAccountPath("070001"), { code: "invalid_account_uin" });
    assert.equal(nativeCalls, 1);
  } finally {
    if (client) await client.close().catch(() => {});
    await broker.stop({ confirmDetach: false }).catch(() => {});
  }
});
