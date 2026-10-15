"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const { createChattingRoomIdQuery, summarizeRoomId } = require("../host/chatting-room-id.cjs");

test("SDK room-ID diagnostic follows empty-parameter command 14 and waits for the exact ACK and post receipt", async (t) => {
  let resolvePost;
  const sent = [];
  const query = createChattingRoomIdQuery({ hostGeneration: 7, sendCommand: (request) => {
    sent.push(request);
    return new Promise((resolve) => { resolvePost = resolve; });
  }});
  t.after(() => query.stop());
  const result = query.query(11);
  await Promise.resolve();
  query.observeOutput({ command: 14, id: 0, hostGeneration: 7, value: [0, "", "wrong"] });
  query.observeOutput({ command: 14, id: 11, hostGeneration: 6, value: [0, "", "wrong"] });
  query.observeOutput({ command: 13, id: 11, hostGeneration: 7, value: [0, "", "wrong"] });
  let completed = false;
  result.then(() => { completed = true; });
  query.observeOutput({ command: 14, id: 11, hostGeneration: 7, value: [0, "", "private-room"] });
  await Promise.resolve();
  assert.equal(completed, false);
  resolvePost({ posted: true, command: 14, id: 11 });
  const value = await result;
  assert.deepEqual(sent, [{ command: 14, id: 11, params: [], hostGeneration: 7 }]);
  assert.equal(value.diagnosticOnly, true);
  assert.equal(value.observation.dataKind, "string");
  assert.equal(value.observation.dataLength, 12);
  assert.equal(JSON.stringify(value).includes("private-room"), false);
  assert.throws(() => query.query(11), { code: "group_local_room_query_id_reused" });
});

test("local empty, missing and error results remain diagnostic and preserve their distinction", () => {
  for (const [value, kind] of [[[0, "", ""], "empty_string"], [[0, "", "0"], "zero_string"],
    [[0, "", 0], "zero_number"], [[0, ""], "missing"], [[17, "private error", 0], "zero_number"]]) {
    const result = summarizeRoomId(value);
    assert.equal(result.diagnosticOnly, true);
    assert.equal(result.dataKind, kind);
    assert.equal(result.returnCode, value[0]);
    assert.equal(JSON.stringify(result).includes("private error"), false);
    assert.equal(result.absenceProved, undefined);
  }
  assert.equal(summarizeRoomId([0, "", { secret: "unknown" }]), null);
  assert.equal(summarizeRoomId([false, "", 0]), null);
});

test("timed-out IDs cannot be reused; their late ACK cannot complete another query", async (t) => {
  const query = createChattingRoomIdQuery({ hostGeneration: 7, timeoutMs: 10,
    sendCommand: async ({ id }) => ({ posted: true, id, command: 14 }) });
  t.after(() => query.stop());
  const timer = setInterval(() => {}, 20);
  t.after(() => clearInterval(timer));
  await assert.rejects(query.query(11), { code: "group_local_room_query_timeout" });
  assert.throws(() => query.query(11), { code: "group_local_room_query_id_reused" });
  const next = query.query(12);
  query.observeOutput({ command: 14, id: 11, hostGeneration: 7, value: [0, "", ""] });
  query.observeOutput({ command: 14, id: 12, hostGeneration: 7, value: [0, "", 42] });
  assert.equal((await next).observation.dataKind, "number");
});

test("failed post, malformed ACK, stop and overlapping requests fail closed", async (t) => {
  const query = createChattingRoomIdQuery({ hostGeneration: 7,
    sendCommand: async ({ id }) => ({ posted: true, id, command: 14 }) });
  t.after(() => query.stop());
  const first = query.query(11);
  assert.throws(() => query.query(12), { code: "group_local_room_query_busy" });
  query.observeOutput({ command: 14, id: 11, hostGeneration: 7, value: [0, "", false] });
  await assert.rejects(first, { code: "group_local_room_response_invalid" });
  const next = query.query(12);
  query.stop();
  await assert.rejects(next, { code: "host_stopped" });
  const failed = createChattingRoomIdQuery({ hostGeneration: 7, sendCommand: async () => ({ posted: false }) });
  t.after(() => failed.stop());
  await assert.rejects(failed.query(11), { code: "group_local_room_query_post_failed" });
});

test("production-generated read IDs remain unique beyond the bounded diagnostic history", async (t) => {
  let query;
  const ids = new Set();
  query = createChattingRoomIdQuery({ hostGeneration: 7, sendCommand: async ({ id }) => {
    assert.equal(ids.has(id), false);
    ids.add(id);
    query.observeOutput({ command: 14, id, hostGeneration: 7, value: [0, "", "0"] });
    return { posted: true, id, command: 14 };
  } });
  t.after(() => query.stop());
  for (let i = 0; i < 4100; i += 1) await query.queryGenerated();
  assert.equal(ids.size, 4100);
  const first = ids.values().next().value;
  assert.throws(() => query.query(first), { code: "group_local_room_query_id_reused" });
  assert.equal(query.ownsOutput({ command: 14, id: first, hostGeneration: 7 }), true);
  assert.equal(query.ownsOutput({ command: 14, id: first, hostGeneration: 8 }), false);
});

test("a generated read accepts only a reduced timeout budget and a late result cannot satisfy the next read", async (t) => {
  const sentIds = [];
  const query = createChattingRoomIdQuery({ hostGeneration: 7, timeoutMs: 100,
    sendCommand: async ({ id }) => { sentIds.push(id); return { posted: true, id, command: 14 }; } });
  t.after(() => query.stop());
  const timer = setInterval(() => {}, 20);
  t.after(() => clearInterval(timer));
  for (const budget of [0, -1, 101, 1.5]) {
    assert.throws(() => query.queryGenerated(budget), { code: "group_local_room_query_budget_invalid" });
  }
  await assert.rejects(query.queryGenerated(5), { code: "group_local_room_query_timeout" });
  const timedOutId = sentIds[0];
  const next = query.queryGenerated(50);
  await Promise.resolve();
  const nextId = sentIds[1];
  query.observeOutput({ command: 14, id: timedOutId, hostGeneration: 7, value: [0, "", "0"] });
  let settled = false;
  next.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  query.observeOutput({ command: 14, id: nextId, hostGeneration: 7, value: [0, "", "765432109"] });
  const response = await next;
  assert.equal(response.id, nextId);
  assert.equal(response.observation.dataKind, "string");
});

test("a delayed post receipt cannot publish an early ACK after the wall-clock deadline", async (t) => {
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  t.after(() => { Date.now = originalNow; });
  let query;
  query = createChattingRoomIdQuery({ hostGeneration: 7, timeoutMs: 100,
    sendCommand: async ({ id }) => {
      query.observeOutput({ command: 14, id, hostGeneration: 7, value: [0, "", "0"] });
      now += 11;
      return { posted: true, id, command: 14 };
    } });
  t.after(() => query.stop());
  await assert.rejects(query.queryGenerated(10), { code: "group_local_room_query_timeout" });
});
