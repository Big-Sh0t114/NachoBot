"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const test = require("node:test");
const { ALLOWED_COMMANDS, createHostTransport } = require("../host/transport.cjs");
const { createGroupRoomInfoQuery, parseGroupRoomInfoOutput, copyGroupRoomQueryObservation } = require("../host/group-room-info.cjs");

const TOKEN = Buffer.alloc(32, 0x47).toString("base64url");
const HOST_GENERATION = 91;
const GROUP_ID = "123456789";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function createTransport(options = {}) {
  const mutable = {
    readiness: { ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false },
    transport: null,
    outputs: [],
    failures: [],
  };
  const transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    verifiedGroupRoomInfoProtocol: true,
    groupRoomInfoTimeoutMs: 1000,
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    getReadiness: () => mutable.readiness,
    outputSink: async (frame) => { mutable.outputs.push(frame); return { ok: true }; },
    onFailure: (failure) => mutable.failures.push(failure),
    ...options,
  });
  mutable.transport = transport;
  await transport.start();
  return { transport, port: transport.status().listenPort, mutable };
}

function postJson(port, pathname, body, { token = TOKEN, contentType = "application/json" } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = Buffer.from(JSON.stringify(body));
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method: "POST",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        "Content-Type": contentType,
        "Content-Length": bytes.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end(bytes);
  });
}

function postRaw(port, pathname, raw, { token = TOKEN, contentType = "application/json" } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = Buffer.from(raw);
    const request = http.request({
      hostname: "127.0.0.1",
      port,
      path: pathname,
      method: "POST",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        "Content-Type": contentType,
        "Content-Length": bytes.byteLength,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end(bytes);
  });
}

function requestBody(overrides = {}) {
  return { id: 921, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, ...overrides };
}

test("paired native ACK and explicit empty error settle negatively without poisoning later queries", async () => {
  for (const errorFirst of [false, true]) {
    const query = createGroupRoomInfoQuery({ hostGeneration: HOST_GENERATION, capability: true, repeatable: true,
      sendCommand: async (request) => ({ posted: true, id: request.id, command: request.command }) });
    const failed = query.query(requestBody());
    const ack = { command: 13, id: 921, hostGeneration: HOST_GENERATION, value: [0, ""] };
    const error = { command: 20010, id: 0, hostGeneration: HOST_GENERATION, value: ["", -1] };
    query.observeOutput(errorFirst ? error : ack);
    query.observeOutput(errorFirst ? ack : error);
    await assert.rejects(failed, { code: "group_room_unavailable" });
    assert.equal(query.status().pending, false);
    assert.equal(query.status().quarantined, false);
    const next = query.query(requestBody({ id: 922 }));
    query.observeOutput(roomOutput());
    assert.equal((await next).nativeJoinVerified, false, "a query result is still only diagnostic metadata");
    query.stop();
  }
});

function roomRecord(groupId = GROUP_ID, overrides = {}) {
  return {
    scene_id: 3,
    relation_id: groupId,
    user_num: 2,
    user_list: [
      { uid: "u_A1b2C3", account_type: 1 },
      { uid: "u_D4e5F6", account_type: 1 },
    ],
    ...overrides,
  };
}

function roomOutput(groupId = GROUP_ID, overrides = {}, { command = 20010, id = 0, status = 0 } = {}) {
  return {
    command,
    id,
    value: [JSON.stringify(roomRecord(groupId, overrides)), status],
    hostGeneration: HOST_GENERATION,
  };
}

test("unverified group-room capability refuses without posting cmd 13", async (t) => {
  let calls = 0;
  const { transport, port } = await createTransport({
    verifiedGroupRoomInfoProtocol: false,
    invokePlugin: async () => { calls += 1; return { posted: true, id: 921, command: 13 }; },
  });
  t.after(() => transport.stop());

  const response = await postJson(port, "/v1/group/room-info", requestBody());
  assert.equal(response.status, 501);
  assert.equal(response.body.code, "group_room_info_protocol_unverified");
  assert.equal(calls, 0);
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
});

test("group-room route requires bearer, strict JSON content type, exact body and current generation", async (t) => {
  let calls = 0;
  const { transport, port } = await createTransport({
    invokePlugin: async ({ command, id }) => { calls += 1; return { posted: true, id, command }; },
  });
  t.after(() => transport.stop());

  const unauthorized = await postJson(port, "/v1/group/room-info", requestBody(), { token: null });
  assert.equal(unauthorized.status, 401);
  assert.equal(unauthorized.body.code, "unauthorized");

  const contentType = await postJson(port, "/v1/group/room-info", requestBody(), { contentType: "text/plain" });
  assert.equal(contentType.status, 400);
  assert.equal(contentType.body.code, "content_type_required");

  const invalidJson = await postRaw(port, "/v1/group/room-info", "{not-json");
  assert.equal(invalidJson.status, 400);
  assert.equal(invalidJson.body.code, "invalid_json");

  const extra = await postJson(port, "/v1/group/room-info", { ...requestBody(), params: [3, GROUP_ID] });
  assert.equal(extra.status, 400);
  assert.equal(extra.body.code, "invalid_request_shape");

  const stale = await postJson(port, "/v1/group/room-info", requestBody({ hostGeneration: HOST_GENERATION + 1 }));
  assert.equal(stale.status, 409);
  assert.equal(stale.body.code, "generation_mismatch");

  for (const id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, "921"]) {
    const invalid = await postJson(port, "/v1/group/room-info", requestBody({ id }));
    assert.equal(invalid.status, 400);
    assert.equal(invalid.body.code, "invalid_request_id");
  }

  for (const groupId of ["", "0", "012345", "123 456", 123456, "9".repeat(21)]) {
    const invalid = await postJson(port, "/v1/group/room-info", requestBody({ groupId }));
    assert.equal(invalid.status, 400);
    assert.equal(invalid.body.code, "invalid_group_id");
  }
  assert.equal(calls, 0);
});

test("query helper independently validates malformed input before consuming its one-shot", async () => {
  let sends = 0;
  const query = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    sendCommand: async ({ command, id }) => {
      sends += 1;
      return { posted: true, command, id };
    },
    timeoutMs: 1000,
  });

  assert.throws(() => query.query(requestBody({ id: 0 })), { code: "invalid_request_id" });
  assert.throws(() => query.query({ ...requestBody(), unexpected: true }), { code: "invalid_request_shape" });
  assert.equal(query.status().used, false);
  assert.equal(sends, 0);

  const result = query.query(requestBody());
  query.observeOutput(roomOutput());
  assert.equal((await result).diagnosticOnly, true);
  assert.equal(sends, 1);
});

test("repeatable query mode serializes successful fresh requests without retaining request history", async () => {
  const sent = [];
  const query = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    repeatable: true,
    sendCommand: async ({ command, id, params }) => {
      sent.push({ command, id, params });
      return { posted: true, command, id };
    },
    timeoutMs: 1000,
  });

  const first = query.query(requestBody());
  assert.throws(() => query.query(requestBody({ id: 922 })), { code: "group_room_query_busy" });
  query.observeOutput(roomOutput());
  assert.equal((await first).id, 921);

  const second = query.query(requestBody({ id: 922 }));
  query.observeOutput(roomOutput());
  assert.equal((await second).id, 922);
  const third = query.query(requestBody({ id: 923 }));
  query.observeOutput(roomOutput());
  assert.equal((await third).id, 923);
  assert.deepEqual(sent, [
    { command: 13, id: 921, params: [3, GROUP_ID] },
    { command: 13, id: 922, params: [3, GROUP_ID] },
    { command: 13, id: 923, params: [3, GROUP_ID] },
  ]);
  assert.deepEqual(query.status(), { used: true, pending: false, stopped: false, repeatable: true, quarantined: false, quarantineCode: null });
});

test("repeatable mode quarantines timeout, malformed matching output, and uncertain posts", async () => {
  const timeoutQuery = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    repeatable: true,
    sendCommand: async ({ command, id }) => ({ posted: true, command, id }),
    timeoutMs: 10,
  });
  await assert.rejects(timeoutQuery.query(requestBody()), { code: "group_room_query_timeout" });
  assert.equal(timeoutQuery.status().quarantined, true);
  assert.equal(timeoutQuery.status().quarantineCode, "group_room_query_timeout");
  assert.throws(() => timeoutQuery.query(requestBody({ id: 922 })), { code: "group_room_query_quarantined" });

  const malformedQuery = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    repeatable: true,
    sendCommand: async ({ command, id }) => ({ posted: true, command, id }),
    timeoutMs: 1000,
  });
  const malformed = malformedQuery.query(requestBody());
  malformedQuery.observeOutput(roomOutput(GROUP_ID, { ...roomRecord(GROUP_ID), user_num: -1 }));
  await assert.rejects(malformed, { code: "group_room_metadata_invalid" });
  assert.equal(malformedQuery.status().quarantined, true);
  assert.equal(malformedQuery.status().quarantineCode, "group_room_metadata_invalid");
  assert.throws(() => malformedQuery.query(requestBody({ id: 922 })), { code: "group_room_query_quarantined" });

  const uncertainQuery = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    repeatable: true,
    sendCommand: async () => { throw Object.assign(new Error("post state unclear"), { code: "network_failed" }); },
    timeoutMs: 1000,
  });
  await assert.rejects(uncertainQuery.query(requestBody()), { code: "group_room_query_post_failed" });
  assert.equal(uncertainQuery.status().quarantined, true);
  assert.equal(uncertainQuery.status().quarantineCode, "group_room_query_post_failed");
  assert.throws(() => uncertainQuery.query(requestBody({ id: 922 })), { code: "group_room_query_quarantined" });
});

test("repeatable pre-post refusal releases the singleflight ticket without quarantining", async () => {
  let attempts = 0;
  const query = createGroupRoomInfoQuery({
    hostGeneration: HOST_GENERATION,
    capability: true,
    repeatable: true,
    sendCommand: async () => {
      attempts += 1;
      throw Object.assign(new Error("renderer unavailable"), { code: "host_not_ready" });
    },
    timeoutMs: 1000,
  });
  await assert.rejects(query.query(requestBody()), { code: "host_not_ready" });
  assert.equal(query.status().pending, false);
  assert.equal(query.status().quarantined, false);
  await assert.rejects(query.query(requestBody({ id: 922 })), { code: "host_not_ready" });
  assert.equal(attempts, 2);
});

test("native success value is a JSON string plus int32 zero and projects only the two serialized user fields", () => {
  const record = roomRecord(GROUP_ID, {
    user_num: 3,
  });
  const parsed = parseGroupRoomInfoOutput([JSON.stringify(record), 0], GROUP_ID);
  assert.deepEqual(parsed, {
    matched: true,
    roomInfo: {
      sceneId: 3,
      relationId: GROUP_ID,
      userNum: 3,
      userList: [
        { uid: "u_A1b2C3", accountType: 1 },
        { uid: "u_D4e5F6", accountType: 1 },
      ],
    },
  });
  assert.deepEqual(parseGroupRoomInfoOutput([JSON.stringify(record)], GROUP_ID), { matched: false });
  const oldUserSchema = roomRecord(GROUP_ID, {
    user_list: [
      { uid: "u_A1b2C3", account_type: 1, is_mic_off_by_admin: false },
      { uid: "u_D4e5F6", account_type: 1 },
    ],
  });
  assert.throws(() => parseGroupRoomInfoOutput([JSON.stringify(oldUserSchema), 0], GROUP_ID), {
    code: "group_room_metadata_invalid",
  });
});

test("invalid status shapes, native empty/error pairs and unmatched records stay unmatched", () => {
  const validJson = JSON.stringify(roomRecord());
  for (const value of [
    [validJson],
    [validJson, false],
    [validJson, "0"],
    [validJson, 0.5],
    [validJson, NaN],
    [validJson, Infinity],
    [validJson, null],
    [validJson, 0x80000000],
    [validJson, -0x80000001],
    [validJson, 1],
    [validJson, -1],
    [validJson, -0x80000000],
    ["", 0],
    ["", 9],
    [validJson, 0, "extra"],
    [null, 0],
  ]) {
    assert.deepEqual(parseGroupRoomInfoOutput(value, GROUP_ID), { matched: false });
  }
  assert.deepEqual(parseGroupRoomInfoOutput([JSON.stringify(roomRecord("987654321")), 0], GROUP_ID), { matched: false });
  assert.deepEqual(parseGroupRoomInfoOutput([JSON.stringify(roomRecord(GROUP_ID, { scene_id: 2 })), 0], GROUP_ID), { matched: false });
});

test("cmd 13 posts exact scene/group params; output cmd 13 is not the 20010 result", async (t) => {
  const commandStarted = deferred();
  const allowAck = deferred();
  const posted = [];
  const { transport, port, mutable } = await createTransport({
    invokePlugin: async (request) => {
      posted.push({ command: request.command, id: request.id, params: request.params, hostGeneration: request.hostGeneration });
      commandStarted.resolve();
      await mutable.transport.publishOutput(roomOutput(GROUP_ID, {}, { command: 13 }));
      await mutable.transport.publishOutput(roomOutput(GROUP_ID, {}, { command: 20011 }));
      await mutable.transport.publishOutput(roomOutput("987654321"));
      await mutable.transport.publishOutput({ command: 20010, id: 0, value: ["{bad-json", 0], hostGeneration: HOST_GENERATION });
      await mutable.transport.publishOutput(roomOutput(GROUP_ID, { scene_id: 2 }));
      await allowAck.promise;
      return { posted: true, id: request.id, command: request.command };
    },
  });
  t.after(() => transport.stop());

  const responsePromise = postJson(port, "/v1/group/room-info", requestBody());
  await commandStarted.promise;
  const premature = await Promise.race([
    responsePromise.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 30)),
  ]);
  assert.equal(premature, false, "ACK-less and unrelated callbacks must not settle the room query");
  await mutable.transport.publishOutput(roomOutput());
  allowAck.resolve();

  const response = await responsePromise;
  assert.equal(response.status, 200);
  assert.deepEqual(posted, [{ command: 13, id: 921, params: [3, GROUP_ID], hostGeneration: HOST_GENERATION }]);
  assert.deepEqual(response.body, {
    ok: true,
    hostGeneration: HOST_GENERATION,
    id: 921,
    groupId: GROUP_ID,
    diagnosticOnly: true,
    nativeJoinVerified: false,
    roomInfo: {
      sceneId: 3,
      relationId: GROUP_ID,
      userNum: 2,
      userList: [
        { uid: "u_A1b2C3", accountType: 1 },
        { uid: "u_D4e5F6", accountType: 1 },
      ],
    },
  });
  assert.equal(Object.hasOwn(response.body, "roomId"), false);
  assert.equal(Object.hasOwn(response.body, "live"), false);
  assert.equal(Object.hasOwn(response.body, "join"), false);
  assert.deepEqual(mutable.outputs.map((frame) => frame.command), [13, 20011, 20010, 20010, 20010, 20010]);
  assert.equal(transport.status().outputFailed, false);
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
});

test("cmd 12 receipt cannot confirm cmd 13 even when a matching 20010 result arrived", async (t) => {
  const posted = [];
  const { transport, port, mutable } = await createTransport({
    invokePlugin: async (request) => {
      posted.push({ command: request.command, id: request.id, params: request.params });
      await mutable.transport.publishOutput(roomOutput(GROUP_ID, {}, { command: 13 }));
      await mutable.transport.publishOutput(roomOutput());
      return { posted: true, id: request.id, command: 12 };
    },
  });
  t.after(() => transport.stop());

  const response = await postJson(port, "/v1/group/room-info", requestBody());
  assert.equal(response.status, 502);
  assert.equal(response.body.code, "group_room_query_post_failed");
  assert.deepEqual(posted, [{ command: 13, id: 921, params: [3, GROUP_ID] }]);
  assert.deepEqual(mutable.outputs.map((frame) => frame.command), [13, 20010]);
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
  const retry = await postJson(port, "/v1/group/room-info", requestBody({ id: 922 }));
  assert.equal(retry.status, 409);
  assert.equal(retry.body.code, "group_room_query_exhausted");
  assert.equal(posted.length, 1);
});

test("SDK post ACK without 20010 times out and permanently exhausts this Host generation", async (t) => {
  let calls = 0;
  const { transport, port } = await createTransport({
    groupRoomInfoTimeoutMs: 60,
    invokePlugin: async ({ command, id }) => { calls += 1; return { posted: true, id, command }; },
  });
  t.after(() => transport.stop());

  const first = await postJson(port, "/v1/group/room-info", requestBody());
  assert.equal(first.status, 504);
  assert.equal(first.body.code, "group_room_query_timeout");
  await transport.publishOutput(roomOutput());
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
  const second = await postJson(port, "/v1/group/room-info", requestBody({ id: 922 }));
  assert.equal(second.status, 409);
  assert.equal(second.body.code, "group_room_query_exhausted");
  assert.equal(calls, 1);
});

test("native empty/error pairs and invalid statuses remain pending until timeout", async (t) => {
  const validJson = JSON.stringify(roomRecord());
  const malformedOutputs = [
    { command: 20010, id: 0, value: ["", 0], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: ["", 17], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: [validJson, false], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: [validJson, "0"], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: [validJson, 1.5], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: [validJson, 0x80000000], hostGeneration: HOST_GENERATION },
    { command: 20010, id: 0, value: [validJson], hostGeneration: HOST_GENERATION },
    roomOutput("987654321"),
    roomOutput(GROUP_ID, { scene_id: 2 }),
  ];
  const { transport, port, mutable } = await createTransport({
    groupRoomInfoTimeoutMs: 50,
    invokePlugin: async (request) => {
      for (const frame of malformedOutputs) await mutable.transport.publishOutput(frame);
      return { posted: true, id: request.id, command: request.command };
    },
  });
  t.after(() => transport.stop());

  const response = await postJson(port, "/v1/group/room-info", requestBody());
  assert.equal(response.status, 504);
  assert.equal(response.body.code, "group_room_query_timeout");
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
  assert.equal(mutable.outputs.length, malformedOutputs.length);
  const observation = transport.status().groupRoomQueryObservation;
  assert.equal(observation.diagnosticOnly, true);
  assert.equal(observation.commandPosted, true);
  assert.equal(observation.outputCount, malformedOutputs.length);
  assert.equal(observation.emptySuccessOutputs, 1);
  assert.equal(observation.emptyErrorOutputs, 1);
  assert.equal(observation.lastEmptyStatusCode, 17);
  assert.equal(observation.matchingOutputs, 0);
  assert.equal(observation.otherUnmatchedOutputs, malformedOutputs.length - 2);
});

test("query observations copy only bounded counts and status without invoking getters", () => {
  const value = { outputCount: 2_000_000, emptyErrorOutputs: 1, lastEmptyStatusCode: -17,
    rawPayload: "private callback", groupId: "private identity", commandPosted: true };
  Object.defineProperty(value, "matchingOutputs", { get() { throw new Error("getter invoked"); } });
  const observation = copyGroupRoomQueryObservation(value);
  assert.equal(observation.outputCount, 1_000_000);
  assert.equal(observation.matchingOutputs, 0);
  assert.equal(observation.lastEmptyStatusCode, -17);
  assert.equal(observation.rawPayload, undefined);
  assert.equal(observation.groupId, undefined);
  assert.equal(Object.isFrozen(observation), true);
});

test("query ACK diagnostics do not complete a room query or lift timeout quarantine", async () => {
  let timeout;
  const query = createGroupRoomInfoQuery({ hostGeneration: HOST_GENERATION, capability: true, repeatable: true,
    sendCommand: async ({ command, id }) => ({ posted: true, command, id }),
    schedule: (callback) => { timeout = callback; return { unref() {} }; }, cancel() {} });
  const result = query.query(requestBody());
  await Promise.resolve(); await Promise.resolve();
  query.observeOutput({ command: 13, id: 0, value: [91, "private error detail"] });
  query.observeOutput({ command: 13, id: 921, value: [0, "private ack detail"] });
  const observation = query.observation();
  assert.equal(observation.queryAckOutputs, 2);
  assert.equal(observation.matchingQueryAckOutputs, 1);
  assert.equal(observation.lastQueryAckMatched, true);
  assert.equal(observation.lastQueryAckFirstInteger, 0);
  assert.equal(observation.lastQueryAckValueKind, "array");
  assert.equal(observation.lastQueryAckArrayLength, 2);
  assert.equal(observation.outputCount, 0, "room result counter excludes command acknowledgements");
  assert.equal(JSON.stringify(observation).includes("private"), false);
  assert.equal(query.status().pending, true);
  timeout();
  await assert.rejects(result, { code: "group_room_query_timeout" });
  query.observeOutput({ command: 13, id: 921, value: [0, ""] });
  assert.equal(query.observation().queryAckOutputs, 2, "late ACK cannot mutate the terminal snapshot");
  assert.equal(query.status().quarantined, true);
  assert.throws(() => query.query(requestBody({ id: 922 })), { code: "group_room_query_quarantined" });
  query.stop();
});

test("query ACK diagnostics preserve result correlation and sanitize copied values", async () => {
  const query = createGroupRoomInfoQuery({ hostGeneration: HOST_GENERATION, capability: true, repeatable: true,
    sendCommand: async ({ command, id }) => ({ posted: true, command, id }) });
  const first = query.query(requestBody());
  await Promise.resolve(); await Promise.resolve();
  query.observeOutput({ command: 13, id: 921, value: [0x80000000, "private"] });
  assert.equal(query.observation().lastQueryAckFirstInteger, null);
  query.observeOutput(roomOutput(GROUP_ID));
  await first;
  const second = query.query(requestBody({ id: 922 }));
  await Promise.resolve(); await Promise.resolve();
  query.observeOutput({ command: 13, id: 921, value: [-7, "private"] });
  assert.equal(query.observation().matchingQueryAckOutputs, 0);
  assert.equal(query.observation().lastQueryAckMatched, false);
  query.observeOutput(roomOutput(GROUP_ID));
  await second;
  const input = { queryAckOutputs: 2_000_000, lastQueryAckFirstInteger: 0x80000000,
    lastQueryAckArrayLength: 99999, lastQueryAckValueKind: "private", lastQueryAckMatched: "true" };
  Object.defineProperty(input, "matchingQueryAckOutputs", { get() { throw Error("getter invoked"); } });
  const copied = copyGroupRoomQueryObservation(input);
  assert.equal(copied.queryAckOutputs, 1_000_000);
  assert.equal(copied.matchingQueryAckOutputs, 0);
  assert.equal(copied.lastQueryAckFirstInteger, null);
  assert.equal(copied.lastQueryAckArrayLength, 8192);
  assert.equal(copied.lastQueryAckValueKind, null);
  assert.equal(copied.lastQueryAckMatched, false);
  query.stop();
});

test("matching native root with malformed metadata fails closed and consumes the one-shot", async (t) => {
  const invalidRecords = [
    { user_num: -1 },
    { user_num: 1001 },
    { user_num: 0, user_list: Array.from({ length: 1001 }, (_, index) => ({ uid: `u${index}`, account_type: 1 })) },
    { user_list: [
      { uid: "bad uid", account_type: 1 },
      { uid: "u_D4e5F6", account_type: 1 },
    ] },
    { user_list: [
      { uid: "u_A1b2C3", account_type: 1 },
      { uid: "u_A1b2C3", account_type: 1 },
    ] },
    { user_list: [
      { uid: "u_A1b2C3", account_type: 1, is_mic_off_by_admin: false },
      { uid: "u_D4e5F6", account_type: 1 },
    ] },
    { extra: true },
  ];
  for (const invalidRecord of invalidRecords) {
    let calls = 0;
    const { transport, port, mutable } = await createTransport({
      invokePlugin: async (request) => {
        calls += 1;
        await mutable.transport.publishOutput(roomOutput(GROUP_ID, invalidRecord));
        return { posted: true, id: request.id, command: request.command };
      },
    });
    const first = await postJson(port, "/v1/group/room-info", requestBody());
    assert.equal(first.status, 502);
    assert.equal(first.body.code, "group_room_metadata_invalid");
    const second = await postJson(port, "/v1/group/room-info", requestBody({ id: 922 }));
    assert.equal(second.status, 409);
    assert.equal(second.body.code, "group_room_query_exhausted");
    assert.equal(calls, 1);
    assert.equal(transport.status().outputFailed, false);
    await transport.stop();
  }
});

test("failed command post consumes the one-shot without marking the Host failed", async (t) => {
  let failures = 0;
  const { transport, port, mutable } = await createTransport({
    onFailure: () => { failures += 1; },
    invokePlugin: async () => { throw Object.assign(new Error("private detail"), { code: "plugin_post_failed" }); },
  });
  t.after(() => transport.stop());

  const first = await postJson(port, "/v1/group/room-info", requestBody());
  assert.equal(first.status, 502);
  assert.equal(first.body.code, "group_room_query_post_failed");
  const second = await postJson(port, "/v1/group/room-info", requestBody({ id: 922 }));
  assert.equal(second.body.code, "group_room_query_exhausted");
  assert.equal(mutable.failures.length, 0);
  assert.equal(failures, 0);
});

test("concurrent requests post at most once and stop rejects and clears pending query", async (t) => {
  const started = deferred();
  const neverAck = deferred();
  let calls = 0;
  const { transport, port, mutable } = await createTransport({
    invokePlugin: async ({ command, id, signal }) => {
      calls += 1;
      started.resolve();
      await new Promise((resolve, reject) => {
        signal.addEventListener("abort", () => reject(Object.assign(new Error("stopped"), { code: "host_stopped" })), { once: true });
        neverAck.promise.then(resolve, reject);
      });
      return { posted: true, id, command };
    },
  });
  const firstPromise = postJson(port, "/v1/group/room-info", requestBody());
  await started.promise;
  const second = await postJson(port, "/v1/group/room-info", requestBody({ id: 922 }));
  assert.equal(second.status, 409);
  assert.equal(second.body.code, "group_room_query_exhausted");
  assert.equal(calls, 1);
  assert.equal(transport.status().groupRoomInfoQueryPending, true);

  await transport.stop();
  const first = await firstPromise;
  assert.equal(first.status, 503);
  assert.equal(first.body.code, "host_stopped");
  assert.equal(transport.status().groupRoomInfoQueryPending, false);
  assert.equal(mutable.failures.length, 0);
});

test("diagnostic cmds 12 and 13 stay outside generic invoke allowlist", async (t) => {
  let calls = 0;
  const { transport, port } = await createTransport({
    invokePlugin: async ({ command, id }) => { calls += 1; return { posted: true, id, command }; },
  });
  t.after(() => transport.stop());

  assert.deepEqual([...ALLOWED_COMMANDS].sort((a, b) => a - b), [1, 5, 55]);
  for (const command of [12, 13]) {
    const response = await postJson(port, "/v1/invoke", {
      command,
      id: 921,
      params: [3, GROUP_ID],
      hostGeneration: HOST_GENERATION,
    });
    assert.equal(response.status, 400);
    assert.equal(response.body.code, "command_not_allowed");
  }
  assert.equal(calls, 0);
});
