"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  GROUP_SCENE,
  GROUP_START_COMMAND,
  GROUP_QUIT_COMMAND,
  GROUP_ENTER_OUTPUT_COMMAND,
  GROUP_TERMINAL_OUTPUT_COMMAND,
  NATIVE_GROUP_SOURCE,
  buildStartParams,
  parseExistingRoomQueryResult,
  parseGroupEnterCallback,
  parseGroupTerminalCallback,
  createGroupCallDriver,
} = require("../host/group-call.cjs");

const HOST_GENERATION = 501;
const CONNECTION_GENERATION = 702;
const SERVICE_INSTANCE = "group-driver-test-1";
const GROUP_ID = "22345678901";
const SELF_UID = `u_${"L".repeat(22)}`;
const START_REQUEST = Object.freeze({ id: 41, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

async function flush() {
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
}

function roomQueryResponse(request, { userNum = 2, userList = [{ uid: "one", accountType: 1 }] } = {}) {
  return {
    ok: true,
    hostGeneration: request.hostGeneration,
    id: request.id,
    groupId: request.groupId,
    diagnosticOnly: true,
    nativeJoinVerified: false,
    roomInfo: {
      sceneId: GROUP_SCENE,
      relationId: request.groupId,
      userNum,
      userList,
    },
  };
}

function enterFrame(overrides = {}) {
  return {
    command: GROUP_ENTER_OUTPUT_COMMAND,
    hostGeneration: HOST_GENERATION,
    value: [0, GROUP_SCENE, GROUP_ID, "u_peer", 0, "", 17],
    ...overrides,
  };
}

function terminalFrame(overrides = {}) {
  return {
    command: GROUP_TERMINAL_OUTPUT_COMMAND,
    hostGeneration: HOST_GENERATION,
    value: [GROUP_SCENE, GROUP_ID, "opaque-sdk-room-reference"],
    ...overrides,
  };
}

function fakeClock(startAt = 1000) {
  let now = startAt;
  let nextId = 0;
  const timers = new Map();
  return {
    clock: () => now,
    schedule(callback, delay) {
      const id = ++nextId;
      timers.set(id, { callback, due: now + delay });
      return id;
    },
    cancel(id) { timers.delete(id); },
    advance(ms, runTimers = true) {
      now += ms;
      if (!runTimers) return;
      for (const [id, timer] of [...timers]) {
        if (timer.due > now) continue;
        timers.delete(id);
        timer.callback();
      }
    },
    pending: () => timers.size,
  };
}

function makeDriver(overrides = {}) {
  const commands = [];
  const queries = [];
  const statusEvents = [];
  const defaults = {
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    serviceInstance: SERVICE_INSTANCE,
    selfUid: SELF_UID,
    queryRoomInfo: async (request) => {
      queries.push(request);
      return roomQueryResponse(request);
    },
    sendCommand: async (request) => {
      commands.push(request);
      return { posted: true, id: request.id, command: request.command };
    },
    slotFree: () => true,
    currentContext: () => true,
    confirmNoMembership: async () => true,
    confirmRemoteNoMembership: async () => true,
    onStatus: (record) => statusEvents.push(record),
  };
  const options = { ...defaults, ...overrides };
  const driver = createGroupCallDriver(options);
  return { driver, commands, queries, statusEvents, options };
}

async function beginStart(driver, request = START_REQUEST) {
  const started = driver.start(request);
  await flush();
  return { started };
}

async function enter(driver, startPromise, frame = enterFrame()) {
  driver.onOutput(frame);
  await flush();
  return startPromise;
}

async function makeJoined(overrides = {}) {
  const harness = makeDriver(overrides);
  const { started: startPromise } = await beginStart(harness.driver);
  assert.equal(harness.driver.status().state, "awaiting_entry");
  const result = await enter(harness.driver, startPromise);
  assert.equal(result.ok, true);
  return { ...harness, scope: result.scope, startResult: result };
}

test("constructor fails closed on missing dependencies and invalid generations", () => {
  assert.throws(() => createGroupCallDriver(), { code: "invalid_host_generation" });
  assert.throws(() => createGroupCallDriver({
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    serviceInstance: SERVICE_INSTANCE,
    selfUid: SELF_UID,
  }), { code: "group_room_query_required" });
  assert.throws(() => createGroupCallDriver({
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    serviceInstance: SERVICE_INSTANCE,
    selfUid: SELF_UID,
    queryRoomInfo() {}, sendCommand() {}, slotFree() {}, currentContext() {},
    startTtlMs: 10_001,
  }), { code: "group_start_ttl_invalid" });
  assert.throws(() => createGroupCallDriver({
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    serviceInstance: SERVICE_INSTANCE,
    selfUid: "u_short",
    queryRoomInfo() {}, sendCommand() {}, slotFree() {}, currentContext() {},
  }), { code: "invalid_self_uid" });
  assert.throws(() => createGroupCallDriver({
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    serviceInstance: SERVICE_INSTANCE,
    selfUid: SELF_UID,
    queryRoomInfo() {}, sendCommand() {}, slotFree() {}, currentContext() {},
    allocateRequestId: 7,
  }), { code: "group_request_id_allocator_invalid" });
});

test("start builder emits exactly the observed JSON-string parameter and pins constructor UID", () => {
  const params = buildStartParams({ groupId: GROUP_ID, selfUid: SELF_UID });
  assert.equal(params.length, 1);
  assert.equal(typeof params[0], "string");
  assert.deepEqual(JSON.parse(params[0]), {
    scene_id: 3,
    self_uid: SELF_UID,
    invite_count: 0,
    invite_uids: [],
    relation_id: GROUP_ID,
    invite_original: 3,
    sub_business_type: 1,
    invite_reason: 0,
  });
  assert.deepEqual(Object.keys(JSON.parse(params[0])), [
    "scene_id", "self_uid", "invite_count", "invite_uids", "relation_id",
    "invite_original", "sub_business_type", "invite_reason",
  ]);
  assert.throws(() => buildStartParams({ groupId: "022345678901", selfUid: SELF_UID }), { code: "invalid_group_id" });
  assert.throws(() => buildStartParams({ groupId: GROUP_ID, selfUid: "" }), { code: "invalid_self_uid" });
  for (const invalidUid of ["u_short", `u_${"L".repeat(21)}`, `u_${"L".repeat(23)}`, `x_${"L".repeat(22)}`]) {
    assert.throws(() => buildStartParams({ groupId: GROUP_ID, selfUid: invalidUid }), { code: "invalid_self_uid" });
  }
});

test("query parser requires the diagnostic-only Host response and positive existing room", () => {
  const request = { id: 8, hostGeneration: HOST_GENERATION, groupId: GROUP_ID };
  assert.deepEqual(parseExistingRoomQueryResult(roomQueryResponse(request), request), { valid: true, existing: true });
  assert.deepEqual(parseExistingRoomQueryResult(roomQueryResponse(request, {
    userNum: 8,
    userList: [{ uid: "u_partial", accountType: 0 }],
  }), request), { valid: true, existing: true }, "Host may return a partial member list");
  assert.deepEqual(parseExistingRoomQueryResult(roomQueryResponse(request, { userNum: 0, userList: [] }), request), {
    valid: true,
    existing: false,
  });
  assert.deepEqual(parseExistingRoomQueryResult({ ...roomQueryResponse(request), nativeJoinVerified: true }, request), {
    valid: false,
    existing: false,
  });
  const accessorResponse = roomQueryResponse(request);
  Object.defineProperty(accessorResponse, "nativeJoinVerified", { enumerable: true, get() { throw new Error("must not run"); } });
  assert.deepEqual(parseExistingRoomQueryResult(accessorResponse, request), { valid: false, existing: false });
  const invalidRows = [
    [{ uid: "", accountType: 0 }],
    [{ uid: "u_missing_type" }],
    [{ uid: "u_bad_type", accountType: "1" }],
    [{ uid: "u_large_type", accountType: 65_536 }],
    [{ uid: "u_non_ascii_ñ", accountType: 1 }],
    [{ uid: "u_duplicate", accountType: 1 }, { uid: "u_duplicate", accountType: 2 }],
    [{ uid: "u_extra", accountType: 1, unexpected: true }],
  ];
  for (const userList of invalidRows) {
    assert.deepEqual(parseExistingRoomQueryResult(roomQueryResponse(request, { userList }), request), {
      valid: false,
      existing: false,
    });
  }
});

test("callback parsers enforce seven-value entry contract and preserve the opaque terminal ID", () => {
  assert.deepEqual(parseGroupEnterCallback([0, 3, GROUP_ID, "u_peer", 0, "", 11], GROUP_ID), {
    matched: true,
    relationId: GROUP_ID,
    unexpectedCreator: false,
  });
  assert.deepEqual(parseGroupEnterCallback([0, 3, GROUP_ID, "", false, "", 11], GROUP_ID), {
    matched: true,
    relationId: GROUP_ID,
    unexpectedCreator: false,
  }, "scene-3 group callbacks may have no peer UID");
  assert.deepEqual(parseGroupEnterCallback([0, 3, GROUP_ID, "u_peer", 1, "", 11], GROUP_ID), {
    matched: false,
    unexpectedCreator: true,
    relationId: GROUP_ID,
  });
  assert.equal(parseGroupEnterCallback([0, 2, GROUP_ID, "u_peer", 0, "", 11], GROUP_ID).matched, false);
  assert.equal(parseGroupEnterCallback([0, 3, GROUP_ID, "u_peer", false, "", Infinity], GROUP_ID).matched, false);
  assert.deepEqual(parseGroupTerminalCallback([3, GROUP_ID, "sdk-opaque-room"], GROUP_ID), {
    matched: true,
    sceneId: 3,
    relationId: GROUP_ID,
  });
});

test("start reserves before query, creates a fresh query ID, and posts exact cmd 4 JSON once", async () => {
  const { driver, commands, queries } = makeDriver();
  const { started: startPromise } = await beginStart(driver, { id: 1, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });
  assert.deepEqual(queries, [{ id: 2, hostGeneration: HOST_GENERATION, groupId: GROUP_ID }]);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].command, GROUP_START_COMMAND);
  assert.equal(commands[0].id, 1);
  assert.equal(commands[0].hostGeneration, HOST_GENERATION);
  assert.deepEqual(commands[0].params, buildStartParams({ groupId: GROUP_ID, selfUid: SELF_UID }));
  assert.equal(JSON.parse(commands[0].params[0]).self_uid, SELF_UID);
  assert.deepEqual(Object.keys(JSON.parse(commands[0].params[0])), [
    "scene_id", "self_uid", "invite_count", "invite_uids", "relation_id",
    "invite_original", "sub_business_type", "invite_reason",
  ]);
  let settled = false;
  startPromise.then(() => { settled = true; });
  await flush();
  assert.equal(settled, false, "ACK 4 is not entry confirmation");
  assert.equal(driver.status().state, "awaiting_entry");
  const result = await enter(driver, startPromise);
  assert.deepEqual(result, {
    ok: true,
    source: NATIVE_GROUP_SOURCE,
    scope: {
      kind: "qqcall_group",
      serviceInstance: SERVICE_INSTANCE,
      hostGeneration: HOST_GENERATION,
      connectionGeneration: CONNECTION_GENERATION,
      groupId: GROUP_ID,
      roomId: GROUP_ID,
      roomGeneration: 1,
    },
  });
  assert.deepEqual(Object.keys(result.scope), [
    "kind", "serviceInstance", "hostGeneration", "connectionGeneration", "groupId", "roomId", "roomGeneration",
  ]);
});

test("request accessors, noncanonical IDs, stale context, and occupied slots never query or post", async () => {
  let accessorReads = 0;
  const accessorRequest = { id: 5, hostGeneration: HOST_GENERATION };
  Object.defineProperty(accessorRequest, "groupId", {
    enumerable: true,
    get() { accessorReads += 1; return GROUP_ID; },
  });
  const first = makeDriver();
  assert.throws(() => first.driver.start(accessorRequest), { code: "invalid_group_start_shape" });
  assert.equal(accessorReads, 0);
  assert.throws(() => first.driver.start({ id: 5, hostGeneration: HOST_GENERATION, groupId: "012345" }), {
    code: "invalid_group_id",
  });
  assert.equal(first.queries.length, 0);
  assert.equal(first.commands.length, 0);

  const stale = makeDriver({ currentContext: () => false });
  assert.deepEqual(await stale.driver.start(START_REQUEST), { ok: false, code: "stale_context", state: "idle" });
  assert.equal(stale.queries.length, 0);
  assert.equal(stale.commands.length, 0);

  const occupied = makeDriver({ slotFree: () => false });
  assert.deepEqual(await occupied.driver.start(START_REQUEST), { ok: false, code: "slot_occupied", state: "idle" });
  assert.equal(occupied.queries.length, 0);
  assert.equal(occupied.commands.length, 0);
});

test("zero-room, malformed-query, query-error, and query-timeout outcomes never post cmd 4", async () => {
  for (const queryRoomInfo of [
    async (request) => roomQueryResponse(request, { userNum: 0, userList: [] }),
    async (request) => ({ ...roomQueryResponse(request), nativeJoinVerified: true }),
    async () => { throw new Error("private detail stays private"); },
  ]) {
    const harness = makeDriver({ queryRoomInfo });
    const result = await harness.driver.start(START_REQUEST);
    assert.equal(result.ok, false);
    assert.equal(harness.commands.length, 0);
    assert.equal(harness.driver.status().occupied, false);
  }

  const pendingQuery = deferred();
  const clock = fakeClock();
  let queryRequest = null;
  const harness = makeDriver({
    startTtlMs: 50,
    clock: clock.clock,
    schedule: clock.schedule,
    cancel: clock.cancel,
    queryRoomInfo: (request) => { queryRequest = request; return pendingQuery.promise; },
  });
  const resultPromise = harness.driver.start(START_REQUEST);
  await flush();
  clock.advance(51);
  assert.deepEqual(await resultPromise, { ok: false, code: "preflight_timeout", state: "idle" });
  pendingQuery.resolve(roomQueryResponse(queryRequest));
  await flush();
  assert.equal(harness.commands.length, 0);
  assert.equal(harness.driver.status().occupied, false);
});

test("generated query IDs use the shared allocator, skip the start ID, and fail closed on bad allocation", async (t) => {
  const allocated = [];
  const harness = makeDriver({
    allocateRequestId: () => {
      const id = [START_REQUEST.id, 90][allocated.length];
      allocated.push(id);
      return id;
    },
  });
  const startPromise = harness.driver.start(START_REQUEST);
  await flush();
  assert.equal(harness.driver.status().state, "awaiting_entry", "preflight remains pending until native entry");
  assert.equal(harness.queries.length, 1);
  assert.equal(harness.queries[0].id, 90, "an allocator collision is retried once");
  assert.deepEqual(allocated, [START_REQUEST.id, 90]);
  assert.equal(harness.commands.length, 1);
  harness.driver.stop();
  assert.equal((await startPromise).state, "quarantined");

  const cases = [
    { name: "zero", allocateRequestId: () => 0 },
    { name: "unsafe integer", allocateRequestId: () => Number.MAX_SAFE_INTEGER + 1 },
    { name: "throw", allocateRequestId: () => { throw new Error("private allocator failure"); } },
    { name: "repeated excluded ID", allocateRequestId: () => START_REQUEST.id },
  ];
  for (const item of cases) {
    await t.test(item.name, async () => {
      let allocationCalls = 0;
      const invalid = makeDriver({
        allocateRequestId: () => { allocationCalls += 1; return item.allocateRequestId(); },
      });
      assert.deepEqual(await invalid.driver.start(START_REQUEST), {
        ok: false,
        code: "query_id_unavailable",
        state: "idle",
      });
      assert.equal(allocationCalls, item.name === "repeated excluded ID" ? 2 : 1);
      assert.equal(invalid.queries.length, 0, "a bad generated ID never reaches cmd 13");
      assert.equal(invalid.commands.length, 0, "a bad generated ID never reaches cmd 4");
      assert.equal(invalid.driver.status().occupied, false);
    });
  }
});

test("unexpected creator cleanup uses the shared allocator and quarantines on bad cleanup ID", async () => {
  const ids = [90, 0];
  const harness = makeDriver({ allocateRequestId: () => ids.shift() });
  const { started } = await beginStart(harness.driver);
  harness.driver.onOutput(enterFrame({ value: [0, 3, GROUP_ID, "u_peer", 1, "", 17] }));
  await flush();
  assert.deepEqual(await started, { ok: false, code: "unexpected_creator", state: "quarantined" });
  assert.equal(harness.queries[0].id, 90);
  assert.deepEqual(harness.commands.map(({ command }) => command), [GROUP_START_COMMAND]);
  assert.equal(harness.driver.status().occupied, true);
  assert.equal(harness.driver.status().state, "quarantined");
});

test("concurrent start calls reserve a single global slot before awaiting the room query", async () => {
  const query = deferred();
  const queryRequests = [];
  const harness = makeDriver({ queryRoomInfo: async (request) => { queryRequests.push(request); return query.promise; } });
  const first = harness.driver.start(START_REQUEST);
  await flush();
  const second = await harness.driver.start({ id: 42, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });
  assert.deepEqual(second, { ok: false, code: "slot_occupied", state: "preflight" });
  assert.equal(queryRequests.length, 1);
  query.resolve(roomQueryResponse(queryRequests[0]));
  await flush();
  assert.equal(harness.commands.length, 1);
  harness.driver.stop();
  assert.equal((await first).state, "quarantined");
});

test("room query context is rechecked after await before any native join command", async () => {
  const query = deferred();
  let current = true;
  let queryRequest = null;
  const harness = makeDriver({
    currentContext: () => current,
    queryRoomInfo: (request) => { queryRequest = request; return query.promise; },
  });
  const startPromise = harness.driver.start(START_REQUEST);
  await flush();
  current = false;
  query.resolve(roomQueryResponse(queryRequest));
  assert.deepEqual(await startPromise, { ok: false, code: "stale_context", state: "idle" });
  assert.equal(harness.commands.length, 0);
  assert.equal(harness.driver.status().occupied, false);
});

test("other-call slot is rechecked after query and before cmd 4, with clean pre-post refusal", async () => {
  const query = deferred();
  let queryRequest = null;
  let free = true;
  const afterQuery = makeDriver({
    slotFree: () => free,
    queryRoomInfo: (request) => { queryRequest = request; return query.promise; },
  });
  const queryStart = afterQuery.driver.start(START_REQUEST);
  await flush();
  free = false;
  query.resolve(roomQueryResponse(queryRequest));
  assert.deepEqual(await queryStart, { ok: false, code: "slot_occupied", state: "idle" });
  assert.equal(afterQuery.commands.length, 0);
  assert.equal(afterQuery.driver.status().occupied, false);

  let checks = 0;
  const beforePost = makeDriver({ slotFree: () => ++checks !== 3 });
  assert.deepEqual(await beforePost.driver.start(START_REQUEST), {
    ok: false,
    code: "slot_occupied",
    state: "idle",
  });
  assert.equal(checks, 3, "start, post-query, and immediate pre-post checks ran");
  assert.equal(beforePost.commands.length, 0);
});

test("other-call slot conflict after cmd 4 receipt quarantines a possibly posted join", async () => {
  const receipt = deferred();
  let free = true;
  const harness = makeDriver({
    slotFree: () => free,
    sendCommand: (request) => {
      harness.commands.push(request);
      return receipt.promise;
    },
  });
  const started = harness.driver.start(START_REQUEST);
  await flush();
  assert.equal(harness.commands.length, 1);
  free = false;
  receipt.resolve({ posted: true, id: START_REQUEST.id, command: GROUP_START_COMMAND });
  assert.deepEqual(await started, { ok: false, code: "other_call_slot_occupied", state: "quarantined" });
  assert.equal(harness.driver.status().occupied, true);
  assert.equal(harness.driver.onOutput(enterFrame()), false);
  assert.equal(harness.driver.status().roomGeneration, 0);
});

test("other-call slot conflict at 20004 prevents entry activation and holds the slot", async () => {
  let checks = 0;
  const harness = makeDriver({ slotFree: () => ++checks !== 5 });
  const { started } = await beginStart(harness.driver);
  assert.equal(checks, 4, "initial, post-query, pre-post, and post-receipt checks ran");
  assert.equal(harness.driver.onOutput(enterFrame({ value: [0, 3, GROUP_ID, "", 0, "", 17] })), false);
  assert.deepEqual(await started, { ok: false, code: "other_call_slot_occupied", state: "quarantined" });
  assert.equal(harness.driver.status().roomGeneration, 0);
});

test("empty peer UID is accepted as group entry without becoming scope identity", async () => {
  const harness = makeDriver();
  const { started } = await beginStart(harness.driver);
  const result = await enter(harness.driver, started, enterFrame({ value: [0, 3, GROUP_ID, "", false, "", 17] }));
  assert.equal(result.ok, true);
  assert.equal(result.scope.roomId, GROUP_ID);
  assert.equal(Object.hasOwn(result.scope, "peerUid"), false);
});

test("context revoked while StartCall receipt is pending quarantines before accepting callbacks", async () => {
  const receipt = deferred();
  let current = true;
  const harness = makeDriver({
    currentContext: () => current,
    sendCommand: (request) => {
      harness.commands.push(request);
      return receipt.promise;
    },
  });
  const started = harness.driver.start(START_REQUEST);
  await flush();
  assert.equal(harness.commands.length, 1);
  current = false;
  receipt.resolve({ posted: true, id: START_REQUEST.id, command: GROUP_START_COMMAND });
  assert.deepEqual(await started, { ok: false, code: "stale_context", state: "quarantined" });
  assert.equal(harness.driver.onOutput(enterFrame()), false);
  assert.equal(harness.driver.status().state, "quarantined");
});

test("wrong, malformed, foreign, and late 20004 callbacks never activate the slot", async () => {
  const wrongFrames = [
    enterFrame({ value: [0, 2, GROUP_ID, "u_peer", 0, "", 17] }),
    enterFrame({ value: [0, 3, "9988776655", "u_peer", 0, "", 17] }),
    enterFrame({ value: [1, 3, GROUP_ID, "u_peer", 0, "", 17] }),
    enterFrame({ value: [0, 3, GROUP_ID, "u_peer", "false", "", 17] }),
    enterFrame({ value: [0, 3, GROUP_ID, "u_peer", 0, "", 17, "extra"] }),
    enterFrame({ hostGeneration: HOST_GENERATION + 1 }),
  ];
  for (const frame of wrongFrames) {
    const harness = makeDriver();
    const { started } = await beginStart(harness.driver);
    harness.driver.onOutput(frame);
    await flush();
    assert.equal(harness.driver.status().state, "awaiting_entry");
    harness.driver.stop();
    assert.equal((await started).ok, false);
  }

  const clock = fakeClock();
  const harness = makeDriver({ startTtlMs: 20, clock: clock.clock, schedule: clock.schedule, cancel: clock.cancel });
  const { started } = await beginStart(harness.driver);
  clock.advance(21);
  assert.deepEqual(await started, { ok: false, code: "start_timeout", state: "quarantined" });
  assert.equal(harness.driver.onOutput(enterFrame()), false);
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.driver.status().roomGeneration, 0);
});

test("cold native entry has its own bounded join budget and does not lengthen quit", async () => {
  const clock = fakeClock();
  const harness = makeDriver({ startTtlMs: 20, joinTtlMs: 60, clock: clock.clock, schedule: clock.schedule, cancel: clock.cancel });
  const { started } = await beginStart(harness.driver);
  clock.advance(40);
  assert.equal(harness.driver.status().state, "awaiting_entry");
  assert.equal(harness.driver.onOutput(enterFrame()), true);
  const joined = await started;
  assert.equal(joined.ok, true);
  const quit = harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: joined.scope });
  await flush();
  clock.advance(21);
  assert.equal((await quit).ok, true); // Posting is acknowledged before terminal proof.
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.statusEvents.at(-1).code, "quit_timeout");
});

test("late matching entry before the deadline timer runs still cancels the owned call", async () => {
  const clock = fakeClock();
  const harness = makeDriver({ startTtlMs: 20, clock: clock.clock, schedule: clock.schedule, cancel: clock.cancel });
  const { started } = await beginStart(harness.driver);
  clock.advance(21, false);
  assert.equal(harness.driver.onOutput(enterFrame()), false);
  assert.equal((await started).code, "start_timeout");
  await flush();
  assert.equal(harness.commands.filter(c => c.command === 8).length, 1);
  assert.equal(harness.driver.currentScope(), null);
});

test("late matching entry cancels only the owned StartCall once without granting scope", async () => {
  const clock = fakeClock();
  const harness = makeDriver({ startTtlMs: 20, clock: clock.clock, schedule: clock.schedule, cancel: clock.cancel });
  const { started } = await beginStart(harness.driver);
  clock.advance(21);
  assert.equal((await started).code, "start_timeout");
  assert.equal(harness.driver.onOutput(enterFrame({ value: [0, 3, "999999", "u_peer", 0, "", 17] })), false);
  assert.equal(harness.commands.filter(c => c.command === 8).length, 0);
  assert.equal(harness.driver.onOutput(enterFrame()), false);
  harness.driver.onOutput(enterFrame());
  await flush();
  const quits = harness.commands.filter(c => c.command === 8);
  assert.equal(quits.length, 1);
  assert.deepEqual(quits[0].params, [3, 0]);
  assert.equal(harness.driver.currentScope(), null);
  assert.equal(harness.driver.status().occupied, true);
  assert.equal(harness.driver.status().state, "quarantined");
});

test("late callback from expired context cannot issue cleanup against a new connection", async () => {
  const clock = fakeClock();
  let current = true;
  const harness = makeDriver({ startTtlMs: 20, clock: clock.clock, schedule: clock.schedule,
    cancel: clock.cancel, currentContext: () => current });
  const { started } = await beginStart(harness.driver);
  clock.advance(21); await started; current = false;
  harness.driver.onOutput(enterFrame()); await flush();
  assert.equal(harness.commands.filter(c => c.command === 8).length, 0);
});

test("unexpected creator callback quarantines and immediately schedules only owned cmd 8 [3,0]", async () => {
  const harness = makeDriver();
  const { started } = await beginStart(harness.driver);
  harness.driver.onOutput(enterFrame({ value: [0, 3, GROUP_ID, "u_peer", 1, "", 17] }));
  await flush();
  assert.deepEqual(await started, { ok: false, code: "unexpected_creator", state: "quarantined" });
  assert.deepEqual(harness.commands.map(({ command, params }) => ({ command, params })), [
    { command: GROUP_START_COMMAND, params: buildStartParams({ groupId: GROUP_ID, selfUid: SELF_UID }) },
    { command: GROUP_QUIT_COMMAND, params: [3, 0] },
  ]);
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.driver.status().roomGeneration, 0);
});

test("uncertain StartCall posting keeps the slot quarantined instead of reporting failure as clean", async () => {
  const commands = [];
  const post = deferred();
  const harness = makeDriver({
    sendCommand: (request) => { commands.push(request); return post.promise; },
  });
  const started = harness.driver.start(START_REQUEST);
  await flush();
  assert.equal(harness.driver.status().state, "posting_start");
  harness.driver.stop();
  assert.equal((await started).state, "quarantined");
  post.resolve({ posted: true, id: START_REQUEST.id, command: GROUP_START_COMMAND });
  await flush();
  assert.equal(commands.length, 1);
  assert.equal(harness.driver.status().state, "quarantined");
});

test("explicit cmd 4 refusal before posting releases the reserved slot", async () => {
  const harness = makeDriver({
    sendCommand: async (request) => {
      harness.commands.push(request);
      return { posted: false, id: request.id, command: request.command };
    },
  });
  const result = await harness.driver.start(START_REQUEST);
  assert.deepEqual(result, { ok: false, code: "start_refused", state: "idle" });
  assert.equal(harness.commands.length, 1);
  assert.equal(harness.driver.status().occupied, false);
});

test("entry result scope uses the native scene-3 relation as roomId and keeps groupId distinct", async () => {
  const harness = makeDriver();
  const { started } = await beginStart(harness.driver);
  const result = await enter(harness.driver, started, enterFrame({
    value: [0, 3, GROUP_ID, "u_peer", false, "ok", 0],
  }));
  assert.equal(result.source, "native_scene3_relation");
  assert.equal(result.scope.groupId, GROUP_ID);
  assert.equal(result.scope.roomId, GROUP_ID);
  assert.notEqual(result.scope.groupId, undefined);
  assert.equal(result.scope.roomGeneration, 1);
  const diagnosticText = JSON.stringify({ status: harness.driver.status(), events: harness.statusEvents });
  assert.equal(diagnosticText.includes(SELF_UID), false);
  assert.equal(diagnosticText.includes(GROUP_ID), false);
  assert.equal(diagnosticText.includes("u_peer"), false);
});

test("currentScope returns a frozen exact clone only while joined", async () => {
  const harness = makeDriver();
  const { started } = await beginStart(harness.driver);
  assert.equal(harness.driver.currentScope(), null, "preflight and awaiting-entry have no active scope");
  const result = await enter(harness.driver, started);
  const snapshot = harness.driver.currentScope();
  assert.deepEqual(snapshot, result.scope);
  assert.notStrictEqual(snapshot, result.scope);
  assert.equal(Object.isFrozen(snapshot), true);
  assert.deepEqual(Object.keys(snapshot), [
    "kind", "serviceInstance", "hostGeneration", "connectionGeneration", "groupId", "roomId", "roomGeneration",
  ]);
  assert.throws(() => { snapshot.roomId = "9988776655"; }, TypeError);
  assert.deepEqual(harness.driver.currentScope(), result.scope, "mutating the returned snapshot cannot affect ownership");

  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: result.scope });
  assert.equal(harness.driver.currentScope(), null, "ending scopes are not exposed as current");

  const quarantined = await makeJoined({ confirmNoMembership: async () => false });
  await quarantined.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: quarantined.scope });
  quarantined.driver.onOutput(terminalFrame());
  await flush();
  assert.equal(quarantined.driver.status().state, "quarantined");
  assert.equal(quarantined.driver.currentScope(), null);

  const stopped = await makeJoined();
  stopped.driver.stop();
  assert.equal(stopped.driver.currentScope(), null, "stopped drivers never expose a current lease");
});

test("quit validates the exact seven-field scope and sends only cmd 8 [3,0]", async () => {
  const harness = await makeJoined();
  const badScope = { ...harness.scope, roomGeneration: harness.scope.roomGeneration + 1 };
  assert.deepEqual(await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: badScope }), {
    ok: false,
    code: "group_scope_mismatch",
    state: "joined",
  });
  assert.equal(harness.commands.length, 1);

  const result = await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  assert.deepEqual(result, {
    ok: true,
    state: "ending",
    ended: false,
    source: NATIVE_GROUP_SOURCE,
    scope: harness.scope,
  });
  assert.deepEqual(harness.commands.slice(1).map(({ command, id, params, hostGeneration }) => ({
    command, id, params, hostGeneration,
  })), [{ command: 8, id: 52, params: [3, 0], hostGeneration: HOST_GENERATION }]);
  assert.equal(harness.driver.status().state, "ending");
});

test("cmd 8 post receipt does not release; private and foreign terminal callbacks remain no-ops", async () => {
  const calls = [];
  const harness = await makeJoined({ confirmNoMembership: async (request) => { calls.push(request); return true; } });
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  assert.equal(harness.driver.status().state, "ending");
  assert.equal(calls.length, 0, "a Host cmd 8 post receipt is not native terminal evidence");
  const beforeForeign = harness.driver.status();
  harness.driver.onOutput(terminalFrame({ value: [1, GROUP_ID, "private-room"] }));
  harness.driver.onOutput(terminalFrame({ value: [3, "9988776655", "foreign-room"] }));
  await flush();
  assert.deepEqual(harness.driver.status(), beforeForeign);
  assert.equal(harness.driver.status().state, "ending");
  assert.equal(calls.length, 0);
});

test("matching 20011 uses independent exact-true proof; opaque SDK room ID is never compared", async () => {
  const calls = [];
  const harness = await makeJoined({
    confirmNoMembership: async (request) => { calls.push(request); return true; },
  });
  const quitResult = await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  assert.equal(quitResult.ended, false);
  harness.driver.onOutput(terminalFrame({ value: [3, GROUP_ID, "different-opaque-sdk-id"] }));
  await flush();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    scope: harness.scope,
    terminalSequence: 1,
    quitRequestId: 52,
  });
  assert.deepEqual(harness.driver.status(), {
    occupied: false,
    state: "idle",
    code: "membership_absence_confirmed",
    stopped: false,
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    roomGeneration: 1,
    terminalSequence: 1,
  });
});

test("matched remote terminal immediately revokes current scope and releases only after exact absence proof", async () => {
  const proof = deferred();
  const calls = [];
  const harness = await makeJoined({
    confirmRemoteNoMembership: (request) => { calls.push(request); return proof.promise; },
  });
  assert.equal(harness.driver.currentScope().roomId, GROUP_ID);
  assert.equal(harness.driver.onOutput(terminalFrame()), true);
  assert.equal(harness.driver.status().state, "ending_remote");
  assert.equal(harness.driver.status().occupied, true);
  assert.equal(harness.driver.currentScope(), null);
  assert.equal(harness.commands.some((request) => request.command === GROUP_QUIT_COMMAND), false,
    "remote end never fabricates a local cmd 8");
  await flush();
  assert.deepEqual(calls, [{ scope: harness.scope, terminalSequence: 1 }]);

  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().state, "idle");
  assert.equal(harness.driver.status().occupied, false);
});

test("duplicate and unrelated terminal callbacks leave the remote absence ticket intact", async () => {
  const proof = deferred();
  let confirmations = 0;
  const harness = await makeJoined({
    confirmRemoteNoMembership: () => { confirmations += 1; return proof.promise; },
  });
  harness.driver.onOutput(terminalFrame());
  await flush();
  assert.equal(confirmations, 1);
  const pendingStatus = harness.driver.status();
  assert.equal(harness.driver.onOutput(terminalFrame({ value: [3, GROUP_ID, "duplicate-terminal"] })), true);
  assert.equal(harness.driver.onOutput(terminalFrame({ value: [3, "9988776655", "foreign-terminal"] })), false);
  assert.equal(harness.driver.onOutput(terminalFrame({ value: [1, GROUP_ID, "private-terminal"] })), false);
  assert.deepEqual(harness.driver.status(), pendingStatus);
  proof.resolve(true);
  await flush();
  assert.equal(confirmations, 1);
  assert.equal(harness.driver.status().state, "idle");
  assert.equal(harness.driver.status().occupied, false);
  assert.equal(harness.driver.currentScope(), null);
});

test("remote terminal without the optional membership confirmer stays quarantined and occupied", async () => {
  const harness = await makeJoined({ confirmRemoteNoMembership: undefined });
  harness.driver.onOutput(terminalFrame());
  await flush();
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.driver.status().occupied, true);
  assert.equal(harness.driver.currentScope(), null);
  assert.equal(harness.commands.some((request) => request.command === GROUP_QUIT_COMMAND), false);
});

test("scoped leave retries remote-end confirmation without posting Quit against a destroyed room", async () => {
  let absent = false;
  const harness = await makeJoined({ confirmRemoteNoMembership: async () => absent });
  harness.driver.onOutput(terminalFrame());
  await flush();
  assert.equal(harness.driver.status().state, "quarantined");
  const foreign = await harness.driver.quit({ id: 51, hostGeneration: HOST_GENERATION,
    scope: { ...harness.scope, roomGeneration: harness.scope.roomGeneration + 1 } });
  assert.equal(foreign.ok, false);
  assert.equal(harness.driver.status().occupied, true);
  absent = true;
  const result = await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  assert.equal(result.ok, true);
  assert.equal(result.ended, true);
  assert.equal(harness.driver.status().occupied, false);
  assert.equal(harness.commands.some((request) => request.command === GROUP_QUIT_COMMAND), false);
  const next = harness.driver.start({ ...START_REQUEST, id: 90 });
  await flush();
  harness.driver.onOutput(enterFrame());
  assert.equal((await next).ok, true);
});

test("a timed-out remote proof cannot poison a new scoped recovery ticket", async () => {
  const timers = fakeClock();
  const first = deferred();
  const retry = deferred();
  let checks = 0;
  const harness = await makeJoined({ clock: timers.clock, schedule: timers.schedule, cancel: timers.cancel,
    startTtlMs: 100, confirmRemoteNoMembership: () => ++checks === 1 ? first.promise : retry.promise });
  harness.driver.onOutput(terminalFrame());
  await flush();
  timers.advance(100);
  const recovery = harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  first.resolve(true);
  await flush();
  assert.equal(harness.driver.status().state, "ending_remote");
  assert.equal(harness.driver.status().occupied, true);
  retry.resolve(true);
  assert.equal((await recovery).ok, true);
  assert.equal(harness.driver.status().occupied, false);
});

test("remote absence proof timeout is bounded to ten seconds and keeps the global slot occupied", async () => {
  const clock = fakeClock();
  const proof = deferred();
  const harness = await makeJoined({
    startTtlMs: 10_000,
    clock: clock.clock,
    schedule: clock.schedule,
    cancel: clock.cancel,
    confirmNoMembership: () => proof.promise,
  });
  harness.driver.onOutput(terminalFrame());
  assert.equal(harness.driver.status().state, "ending_remote");
  assert.equal(clock.pending(), 1);
  clock.advance(10_000);
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.driver.status().code, "membership_confirmation_timeout");
  assert.equal(harness.driver.status().occupied, true);
  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().occupied, true, "late confirmation cannot release after the bounded deadline");
});

test("missing, false, malformed, and rejected membership proof keep the slot quarantined", async () => {
  const cases = [
    { name: "missing", override: { confirmNoMembership: undefined } },
    { name: "false", override: { confirmNoMembership: async () => false } },
    { name: "malformed", override: { confirmNoMembership: async () => ({ ok: true, member: false }) } },
    { name: "rejected", override: { confirmNoMembership: async () => { throw new Error("private"); } } },
  ];
  for (const item of cases) {
    const harness = await makeJoined(item.override);
    await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
    harness.driver.onOutput(terminalFrame());
    await flush();
    assert.equal(harness.driver.status().state, "quarantined", item.name);
    assert.equal(harness.driver.status().occupied, true, item.name);
  }
});

test("proof completion after context revocation cannot release the slot", async () => {
  const proof = deferred();
  let current = true;
  const harness = await makeJoined({
    currentContext: () => current,
    confirmNoMembership: () => proof.promise,
  });
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  harness.driver.onOutput(terminalFrame());
  await flush();
  current = false;
  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.driver.status().occupied, true);
});

test("duplicate matching terminal leaves an owned-quit proof ticket current", async () => {
  const proof = deferred();
  const harness = await makeJoined({ confirmNoMembership: () => proof.promise });
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  harness.driver.onOutput(terminalFrame());
  await flush();
  harness.driver.onOutput(terminalFrame({ value: [3, GROUP_ID, "newer-matching-terminal"] }));
  assert.equal(harness.driver.status().terminalSequence, 1);
  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().terminalSequence, 1);
  assert.equal(harness.driver.status().state, "idle");
  assert.equal(harness.driver.status().occupied, false);
});

test("private and foreign 20011 outputs during membership proof are no-ops", async () => {
  const proof = deferred();
  const harness = await makeJoined({ confirmNoMembership: () => proof.promise });
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  harness.driver.onOutput(terminalFrame());
  await flush();
  const pendingStatus = harness.driver.status();
  assert.equal(pendingStatus.terminalSequence, 1);

  assert.equal(harness.driver.onOutput(terminalFrame({ value: [1, GROUP_ID, "private-room"] })), false);
  assert.equal(harness.driver.onOutput(terminalFrame({ value: [3, "9988776655", "foreign-room"] })), false);
  assert.deepEqual(harness.driver.status(), pendingStatus);

  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().occupied, false);
  assert.equal(harness.driver.status().state, "idle");
  assert.equal(harness.driver.status().terminalSequence, 1);
});

test("matching 20011 plus exact native absence proof may end before cmd 8 post receipt", async () => {
  const quitReceipt = deferred();
  let confirmationCount = 0;
  const harness = makeDriver({
    sendCommand: (request) => {
      harness.commands.push(request);
      if (request.command === GROUP_QUIT_COMMAND) return quitReceipt.promise;
      return Promise.resolve({ posted: true, id: request.id, command: request.command });
    },
    confirmNoMembership: async () => { confirmationCount += 1; return true; },
  });
  const { started } = await beginStart(harness.driver);
  const joined = await enter(harness.driver, started);
  assert.equal(joined.ok, true);
  const quitPromise = harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: joined.scope });
  await flush();
  assert.equal(harness.driver.status().state, "ending");
  assert.equal(confirmationCount, 0);

  harness.driver.onOutput(terminalFrame());
  await flush();
  assert.equal(confirmationCount, 1);
  assert.deepEqual(await quitPromise, {
    ok: true,
    state: "idle",
    ended: true,
    source: NATIVE_GROUP_SOURCE,
    scope: joined.scope,
  });
  assert.equal(harness.driver.status().occupied, false);
  quitReceipt.resolve({ posted: true, id: 52, command: GROUP_QUIT_COMMAND });
  await flush();
  assert.equal(harness.driver.status().occupied, false);
});

test("stop revokes an in-flight membership proof and never sends network cleanup", async () => {
  const proof = deferred();
  const harness = await makeJoined({ confirmNoMembership: () => proof.promise });
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  harness.driver.onOutput(terminalFrame());
  await flush();
  const countBeforeStop = harness.commands.length;
  assert.equal(harness.driver.stop().state, "quarantined");
  proof.resolve(true);
  await flush();
  assert.equal(harness.driver.status().state, "quarantined");
  assert.equal(harness.commands.length, countBeforeStop);
});

test("invalid quit request accessors and private command alternatives never reach SDK", async () => {
  const harness = await makeJoined();
  let reads = 0;
  const request = { id: 52, scope: harness.scope };
  Object.defineProperty(request, "hostGeneration", { enumerable: true, get() { reads += 1; return HOST_GENERATION; } });
  assert.throws(() => harness.driver.quit(request), { code: "invalid_group_quit_shape" });
  assert.equal(reads, 0);
  assert.equal(harness.commands.length, 1);
  await harness.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: harness.scope });
  assert.deepEqual(harness.commands.map(({ command }) => command), [4, 8]);
  assert.equal(harness.commands.some(({ command }) => command === 5 || command === 9 || command === 10), false);
});

test("explicit cmd 8 refusal before posting restores joined state; uncertain quit outcome quarantines", async () => {
  const refused = await makeJoined({
    sendCommand: async (request) => ({ posted: request.command !== GROUP_QUIT_COMMAND, id: request.id, command: request.command }),
  });
  const refusal = await refused.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: refused.scope });
  assert.deepEqual(refusal, { ok: false, code: "quit_refused", state: "joined" });
  assert.equal(refused.driver.status().state, "joined");

  const uncertain = await makeJoined({
    sendCommand: async (request) => {
      if (request.command === GROUP_QUIT_COMMAND) throw new Error("private");
      return { posted: true, id: request.id, command: request.command };
    },
  });
  const outcome = await uncertain.driver.quit({ id: 52, hostGeneration: HOST_GENERATION, scope: uncertain.scope });
  assert.deepEqual(outcome, { ok: false, code: "quit_outcome_unknown", state: "quarantined" });
  assert.equal(uncertain.driver.status().occupied, true);
});

test("owned timers are cleared on clean preflight refusal and stop does not close shared dependencies", async () => {
  const timers = fakeClock();
  let closes = 0;
  const harness = makeDriver({
    startTtlMs: 100,
    clock: timers.clock,
    schedule: timers.schedule,
    cancel: timers.cancel,
    close() { closes += 1; },
    queryRoomInfo: async (request) => roomQueryResponse(request, { userNum: 0, userList: [] }),
  });
  const result = await harness.driver.start(START_REQUEST);
  assert.equal(result.code, "room_not_existing");
  assert.equal(timers.pending(), 0);
  harness.driver.stop();
  assert.equal(closes, 0);

  const posted = fakeClock();
  const inCall = makeDriver({ startTtlMs: 100, clock: posted.clock, schedule: posted.schedule, cancel: posted.cancel });
  const { started } = await beginStart(inCall.driver);
  assert.equal(posted.pending(), 1);
  inCall.driver.stop();
  assert.equal(posted.pending(), 0);
  assert.equal((await started).state, "quarantined");
  assert.equal(inCall.commands.length, 1, "stop performs no additional SDK command");
});
