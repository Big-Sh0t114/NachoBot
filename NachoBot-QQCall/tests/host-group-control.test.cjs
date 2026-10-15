"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const http = require("node:http");
const test = require("node:test");
const { createHostTransport } = require("../host/transport.cjs");

const TOKEN = Buffer.alloc(32, 0x63).toString("base64url");
const HOST_GENERATION = 107;
const GROUP_ID = "123456789";
const SELF_UID = `u_${"S".repeat(22)}`;
const OTHER_UID = `u_${"O".repeat(22)}`;
const SELF_UIN = "123456789012";
const INPUT_NAME = "Line 1 (Virtual Audio Cable)";
const OUTPUT_NAME = "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)";

function defer() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function defaultDeviceOutput(tail, { inputDeviceName = INPUT_NAME, outputDeviceName = OUTPUT_NAME } = {}) {
  const outputIds = Array.from({ length: 18 }, (_, index) => index + 1);
  const inputIds = Array.from({ length: 15 }, (_, index) => index + 1);
  const outputNames = outputIds.map((id) => id === 3 ? outputDeviceName : `Output ${id}`);
  const inputNames = inputIds.map((id) => id === 13 ? inputDeviceName : `Input ${id}`);
  return [
    outputIds, outputNames, outputIds.map((id) => `{OUTPUT-${id}}`),
    inputIds, inputNames, inputIds.map((id) => `{INPUT-${id}}`),
    ...(tail || [0, 0, 10, 8]),
  ];
}

function roomRecord(groupId = GROUP_ID, users = [{ uid: OTHER_UID, account_type: 1 }], userNum = users.length) {
  return { scene_id: 3, relation_id: groupId, user_num: userNum, user_list: users };
}

function roomOutput(groupId, record) {
  return {
    command: 20010,
    id: 0,
    value: [JSON.stringify(record), 0],
    hostGeneration: HOST_GENERATION,
  };
}

function groupInviteValue({
  scene = 3,
  inviter = SELF_UID,
  inviteList = [OTHER_UID],
  subBusinessType = 1,
  relationId = GROUP_ID,
  showUi = false,
  sequence = "group-sequence",
} = {}) {
  return [scene, inviter, inviteList, subBusinessType, relationId, "", "", "", "", "", showUi, sequence];
}

function postJson(port, route, body, { token = TOKEN, contentType = "application/json" } = {}) {
  const encoded = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1",
      port,
      path: route,
      method: "POST",
      headers: {
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        "content-type": contentType,
        "content-length": encoded.length,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end(encoded);
  });
}

function getJson(port, route, { token = TOKEN } = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1",
      port,
      path: route,
      method: "GET",
      headers: { ...(token ? { authorization: `Bearer ${token}` } : {}) },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({
        status: response.statusCode,
        body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      }));
    });
    request.once("error", reject);
    request.end();
  });
}

function startBody(overrides = {}) {
  return { id: 401, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, ...overrides };
}

function quitBody(overrides = {}) {
  return { id: 402, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401, ...overrides };
}

function acceptBody(overrides = {}) {
  return { id: 431, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, ...overrides };
}

async function createHarness(options = {}) {
  const calls = [];
  const outputFrames = [];
  let transport;
  let deviceQueryCount = 0;
  let roomQueryCount = 0;
  const settings = {
    verifiedGroupCallProtocol: true,
    verifiedGroupRoomInfoProtocol: true,
    verifiedAudioDeviceProtocol: true,
    verifiedAudioDeviceSelectionProtocol: true,
    groupRoomInfoTimeoutMs: 600,
    requestTimeoutMs: 500,
    autoLoginAck: true,
    autoDeviceOutput: true,
    inputDeviceName: INPUT_NAME,
    outputDeviceName: OUTPUT_NAME,
    roomInfoForQuery: () => roomRecord(),
    suppressRoomInfo: false,
    beforeInvoke: async () => undefined,
    ...options,
  };

  async function emit(frame) {
    queueMicrotask(() => { void transport.publishOutput(frame); });
  }

  transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    outputSink: async (frame) => { outputFrames.push(frame); return { ok: true }; },
    verifiedGroupCallProtocol: settings.verifiedGroupCallProtocol,
    verifiedLocalRoomIdProtocol: settings.verifiedLocalRoomIdProtocol === true,
    verifiedPrivateEndProtocol: settings.verifiedPrivateEndProtocol === true,
    verifiedGroupRoomInfoProtocol: settings.verifiedGroupRoomInfoProtocol,
    verifiedAudioDeviceProtocol: settings.verifiedAudioDeviceProtocol,
    verifiedAudioDeviceSelectionProtocol: settings.verifiedAudioDeviceSelectionProtocol,
    repeatableGroupRoomQueries: settings.repeatableGroupRoomQueries ?? settings.verifiedGroupCallProtocol,
    groupRoomInfoTimeoutMs: settings.groupRoomInfoTimeoutMs,
    requestTimeoutMs: settings.requestTimeoutMs,
    invokePlugin: async (request) => {
      calls.push({ command: request.command, id: request.id, params: structuredClone(request.params) });
      const plan = await settings.beforeInvoke(request, { transport, calls, emit });

      if (request.command === 1 && settings.autoLoginAck) {
        await emit({ command: 1, id: request.id, value: [0, ""], hostGeneration: HOST_GENERATION });
      } else if (request.command === 103 && settings.autoDeviceOutput) {
        deviceQueryCount += 1;
        await emit({
          command: 20061,
          id: 0,
          value: defaultDeviceOutput(deviceQueryCount === 1 ? [0, 0, 10, 8] : [3, 13, 10, 8], settings),
          hostGeneration: HOST_GENERATION,
        });
      } else if (request.command === 13 && !settings.suppressRoomInfo) {
        roomQueryCount += 1;
        const record = settings.roomInfoForQuery(request, roomQueryCount);
        if (record) await emit(roomOutput(request.params[1], record));
      }

      if (plan?.error) throw plan.error;
      if (plan?.receipt) return plan.receipt;
      return { posted: true, id: request.id, command: request.command };
    },
  });
  await transport.start();
  return { transport, port: transport.status().listenPort, calls, outputFrames, settings };
}

async function loginAndSelect(harness, {
  inputName = harness.settings.inputDeviceName,
  outputName = harness.settings.outputDeviceName,
  tinyId = SELF_UIN,
} = {}) {
  const login = await postJson(harness.port, "/v1/invoke", {
    command: 1,
    id: 300,
    hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, tinyId, "C:\\QQ\\account", ""],
  });
  assert.equal(login.status, 200);
  assert.equal(harness.transport.status().loginAcknowledged, true);
  const selection = await postJson(harness.port, "/v1/audio/select", {
    id: 301,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: inputName,
    outputDeviceName: outputName,
  });
  assert.equal(selection.status, 200);
  assert.equal(selection.body.selected, true);
}

async function startOwnedGroup(harness, overrides = {}) {
  await loginAndSelect(harness);
  const body = startBody(overrides);
  const response = await postJson(harness.port, "/v1/group/start", body);
  return { body, response };
}

test("native control diagnostics retain only bounded ACK facts and never release private ownership", async (t) => {
  const harness = await createHarness({ beforeInvoke: async (request, { emit }) => {
    if (request.command === 55) await emit({ command:55,id:request.id,hostGeneration:HOST_GENERATION,value:[17,"synthetic_secret"] });
  } });
  t.after(()=>harness.transport.stop());
  await loginAndSelect(harness);
  await harness.transport.publishOutput({command:20006,id:0,hostGeneration:HOST_GENERATION,
    value:groupInviteValue({scene:1,inviter:OTHER_UID,relationId:"pending-private"})});
  assert.equal((await postJson(harness.port,"/v1/invoke",{id:780,hostGeneration:HOST_GENERATION,command:55,params:[1,"{}"]})).status,200);
  const observed=harness.transport.status().nativeControlObservations;
  assert.equal(observed.signalAck.requestMatched,true);
  assert.equal(observed.signalAck.firstInteger,17);
  assert.equal(observed.signalAck.returnCode,null,"unverified signal and end callback layouts do not label their first item a return code");
  assert.equal(JSON.stringify(observed).includes("synthetic_secret"),false);
  observed.signalAck.firstInteger=999;
  assert.equal(harness.transport.status().nativeControlObservations.signalAck.firstInteger,17);
  await harness.transport.publishOutput({command:55,id:781,hostGeneration:HOST_GENERATION,value:[23,"private_text"]});
  assert.equal(harness.transport.status().nativeControlObservations.signalAck.requestMatched,false);
  await harness.transport.publishOutput({command:20004,id:0,hostGeneration:HOST_GENERATION,value:[19,1,"private_relation",OTHER_UID]});
  assert.equal(harness.transport.status().nativeControlObservations.entry.returnCode,19);
  const before = Date.now();
  for (const [command, key] of [[20003, "error"], [20005, "timeout"], [20006, "invite"]]) {
    await harness.transport.publishOutput({command,id:0,hostGeneration:HOST_GENERATION,
      value:[1,OTHER_UID,"error=-42 sensitive native error text"]});
    const diagnostic=harness.transport.status().nativeControlObservations[key];
    assert.equal(diagnostic.firstInteger,1);
    assert.equal(diagnostic.returnCode,null,"a scene or undocumented first value is not a return code");
    assert.equal(diagnostic.requestMatched,false);
    assert.ok(diagnostic.lastObservedAtMs >= before && diagnostic.lastObservedAtMs <= Date.now());
    if (command === 20003) {
      assert.equal(diagnostic.errorCode,-42);
      assert.equal(diagnostic.errorCodeLabel,"error");
    }
  }
  assert.equal(JSON.stringify(harness.transport.status().nativeControlObservations).includes(OTHER_UID),false);
  assert.equal(JSON.stringify(harness.transport.status().nativeControlObservations).includes("sensitive native error text"),false);
  assert.equal((await postJson(harness.port,"/v1/group/start",startBody())).body.code,"group_private_call_pending");
});

async function makeReadbackHarness(options = {}) {
  let localRoom = "765432109";
  let readIndex = 0;
  const harness = await createHarness({ verifiedLocalRoomIdProtocol: true,
    beforeInvoke: async (request, context) => {
      if (request.command === 14) {
        await options.beforeRoomRead?.(request, context);
        if (!options.noReadAck) await context.emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION,
          value: [options.readReturnCode ?? 0, "", options.roomReadSequence?.[readIndex++] ?? localRoom] });
      }
      if (request.command === 8) {
        localRoom = options.roomAfterQuit ?? "0";
        await context.emit({ command: 8, id: options.wrongAckId ? request.id + 1 : request.id,
          hostGeneration: HOST_GENERATION, value: [options.quitReturnCode ?? 0, ""] });
      }
      if (request.command === 11 && options.clearRoom) {
        assert.deepEqual(request.params, [3, GROUP_ID]);
        localRoom = options.roomAfterClear ?? "0";
        await context.emit({ command: 11, id: request.id, hostGeneration: HOST_GENERATION, value: [0, ""] });
      }
    } });
  await startOwnedGroup(harness);
  if (!options.noEntry) await harness.transport.publishOutput({ command: 20004, id: 0,
    hostGeneration: HOST_GENERATION, value: [0, 3, GROUP_ID, "", 0, "", 1] });
  return harness;
}

function quitProofBody(overrides = {}) {
  return { id: 701, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
    startRequestId: 401, quitRequestId: 402, ...overrides };
}

test("owned zero-member cleanup mirrors official ClearRoom11 and releases only after SDK zero", async (t) => {
  const harness = await makeReadbackHarness({ clearRoom: true });
  t.after(() => harness.transport.stop());
  const body = { id: 755, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401 };
  assert.notEqual((await postJson(harness.port, "/v1/group/clear-absence", { ...body, startRequestId: 999 })).status, 200);
  assert.equal(harness.calls.filter((request) => request.command === 11).length, 0);
  const proof = await postJson(harness.port, "/v1/group/clear-absence", body);
  assert.equal(proof.status, 200);
  assert.equal(proof.body.source, "native_group_clear_absence");
  assert.equal(proof.body.confirmed, true);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.calls.filter((request) => request.command === 11).length, 1);
  assert.equal((await postJson(harness.port, "/v1/group/start", startBody({ id: 756 }))).status, 200);
});

test("ClearRoom post or ACK with a nonzero SDK room cannot release; retry does not repost cmd11", async (t) => {
  const harness = await makeReadbackHarness({ clearRoom: true, roomAfterClear: "99887766" });
  t.after(() => harness.transport.stop());
  const body = { id: 755, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401 };
  for (let attempt = 0; attempt < 2; attempt += 1) {
    assert.equal((await postJson(harness.port, "/v1/group/clear-absence", { ...body, id: 755 + attempt })).body.confirmed, false);
    assert.equal(harness.transport.status().groupCallReserved, true);
  }
  assert.equal(harness.calls.filter((request) => request.command === 11).length, 1);
});

test("a changed native entry while clear readback is pending cannot release or reuse the old clear proof", async (t) => {
  let changed = false;
  const harness = await makeReadbackHarness({ clearRoom: true,
    beforeRoomRead: async (_request, { transport }) => {
      if (changed) return;
      changed = true;
      await transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
        value: [0, 3, GROUP_ID, "", 0, "", 1] });
    } });
  t.after(() => harness.transport.stop());
  const body = { id: 755, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401 };
  assert.notEqual((await postJson(harness.port, "/v1/group/clear-absence", body)).status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.notEqual((await postJson(harness.port, "/v1/group/clear-absence", { ...body, id: 756 })).status, 200);
  assert.equal(harness.calls.filter((request) => request.command === 11).length, 1);
});

test("admin remote room destruction plus fresh SDK zero releases the owned Host slot without a member query", async (t) => {
  const harness = await makeReadbackHarness({ roomReadSequence: ["0"] });
  t.after(() => harness.transport.stop());
  const body = { id: 750, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401 };
  assert.notEqual((await postJson(harness.port, "/v1/group/remote-absence", body)).status, 200,
    "entered room without matching terminal cannot be cleared");
  await harness.transport.publishOutput({ command: 20011, id: 0, hostGeneration: HOST_GENERATION,
    value: [3, GROUP_ID, "remote-admin-ended"] });
  assert.equal(harness.transport.status().groupCallReserved, true, "terminal alone cannot clear ownership");
  const foreign = await postJson(harness.port, "/v1/group/remote-absence", { ...body, startRequestId: 999 });
  assert.notEqual(foreign.status, 200);
  const queries = harness.calls.filter((request) => request.command === 13).length;
  const proof = await postJson(harness.port, "/v1/group/remote-absence", body);
  assert.equal(proof.status, 200);
  assert.equal(proof.body.confirmed, true);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.calls.filter((request) => request.command === 13).length, queries);
  assert.equal(harness.calls.some((request) => request.command === 8), false);
  assert.equal((await postJson(harness.port, "/v1/group/start", startBody({ id: 751 }))).status, 200);
});

test("remote absence rejects foreign terminal and nonzero SDK room IDs", async (t) => {
  const harness = await makeReadbackHarness();
  t.after(() => harness.transport.stop());
  const body = { id: 750, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: 401 };
  await harness.transport.publishOutput({ command: 20011, id: 0, hostGeneration: HOST_GENERATION,
    value: [3, "998877", "foreign-room"] });
  assert.notEqual((await postJson(harness.port, "/v1/group/remote-absence", body)).status, 200);
  await harness.transport.publishOutput({ command: 20011, id: 0, hostGeneration: HOST_GENERATION,
    value: [3, GROUP_ID, "matching-room"] });
  assert.equal((await postJson(harness.port, "/v1/group/remote-absence", body)).body.confirmed, false);
  assert.equal(harness.transport.status().groupCallReserved, true);
});

test("SDK witnessed owned Quit frees the last-member slot without a server-member callback", async (t) => {
  const harness = await makeReadbackHarness();
  t.after(() => harness.transport.stop());
  assert.equal((await postJson(harness.port, "/v1/group/quit", quitBody())).status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "ACK alone is insufficient");
  const diagnostic = await postJson(harness.port, "/v1/group/current-room-id", { id: 700, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });
  assert.equal(diagnostic.body.observation.dataKind, "zero_string");
  assert.equal(harness.transport.status().groupCallReserved, true, "diagnostic zero is still insufficient");
  for (const changed of [{ hostGeneration: HOST_GENERATION + 1 }, { groupId: "987654321" }, { startRequestId: 400 }, { quitRequestId: 403 }]) {
    assert.notEqual((await postJson(harness.port, "/v1/group/quit-absence", quitProofBody(changed))).status, 200);
    assert.equal(harness.transport.status().groupCallReserved, true);
  }
  const memberCalls = harness.calls.filter((r) => r.command === 13).length;
  const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(proof.status, 200);
  assert.equal(proof.body.confirmed, true);
  assert.equal(proof.body.source, "native_sdk_room_id_readback");
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.calls.filter((r) => r.command === 13).length, memberCalls);
  assert.equal(harness.outputFrames.some((r) => r.command === 14), false, "Host-local ACKs have no Bridge waiter");
  assert.notEqual((await postJson(harness.port, "/v1/group/quit-absence", quitProofBody())).status, 200, "late proof cannot target a new slot");
});

test("a busy private invitation cannot replace an entered group or prevent its witnessed Quit", async (t) => {
  const harness = await makeReadbackHarness();
  t.after(() => harness.transport.stop());
  await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
    value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "busy-private-invite" }) });
  assert.equal(harness.transport.status().groupBusyPrivateInvitesObserved, 1);
  const privateAccept = await postJson(harness.port, "/v1/invoke", { command: 5, id: 501,
    hostGeneration: HOST_GENERATION, params: [0, "caller", ["caller"], 0, "", false, ""] });
  assert.equal(privateAccept.body.code, "group_call_reserved");
  assert.equal(harness.transport.status().groupCallReserved, true);
  const queriesBeforeQuit = harness.calls.filter((request) => request.command === 13).length;
  assert.equal((await postJson(harness.port, "/v1/group/quit", quitBody())).status, 200);
  assert.equal(harness.transport.status().groupLocalQuitWitnessAvailable, true);
  const absent = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(absent.body.confirmed, true);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.calls.filter((request) => request.command === 13).length, queriesBeforeQuit);
  assert.equal(harness.calls.filter((request) => request.command === 5).length, 0);
});

test("busy private invites during the group witness or absence read preserve the same owned room proof", async (t) => {
  for (const inviteRead of [1, 2]) {
    await t.test(`read ${inviteRead}`, async (t) => {
      let reads = 0;
      const harness = await makeReadbackHarness({ beforeRoomRead: async (_request, { transport }) => {
        if (++reads === inviteRead) await transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
          value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "busy-during-read" }) });
      } });
      t.after(() => harness.transport.stop());
      await postJson(harness.port, "/v1/group/quit", quitBody());
      const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
      assert.equal(proof.body.confirmed, true);
      assert.equal(harness.transport.status().groupBusyPrivateInvitesObserved, 1);
      assert.equal(harness.transport.status().groupCallReserved, false);
    });
  }
});

test("a private invitation before proven group entry and an actual private entry still fence group readback", async (t) => {
  for (const scenario of ["unentered_group", "private_entry_after_busy_invite"]) {
    await t.test(scenario, async (t) => {
      const harness = await makeReadbackHarness({ noEntry: scenario === "unentered_group" });
      t.after(() => harness.transport.stop());
      await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
        value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "private-invite" }) });
      if (scenario !== "unentered_group") await harness.transport.publishOutput({ command: 20004, id: 0,
        hostGeneration: HOST_GENERATION, value: [0, 1, "private-entry", OTHER_UID, 0, "", 1] });
      assert.equal(harness.transport.status().groupBusyPrivateInvitesObserved, scenario === "unentered_group" ? 0 : 1);
      await postJson(harness.port, "/v1/group/quit", quitBody());
      const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
      assert.equal(proof.body.confirmed, false);
      assert.equal(harness.transport.status().groupCallReserved, true);
      assert.equal(harness.calls.filter((request) => request.command === 14).length, 0);
    });
  }
});

test("readback cannot free occupancy with missing entry/witness, unsuccessful ACK, or ambiguous no-room values", async (t) => {
  for (const options of [{ noEntry: true }, { readReturnCode: 17 }, { quitReturnCode: 17 }, { wrongAckId: true },
    { roomAfterQuit: 0 }, { roomAfterQuit: "" }, { roomAfterQuit: "765432109" }]) {
    await t.test(JSON.stringify(options), async (t) => {
      const harness = await makeReadbackHarness(options);
      t.after(() => harness.transport.stop());
      assert.equal((await postJson(harness.port, "/v1/group/quit", quitBody())).status, 200);
      const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
      assert.equal(proof.body.confirmed, false);
      assert.equal(harness.transport.status().groupCallReserved, true);
    });
  }
});

test("an owned Quit may settle from its witnessed room to zero without resending Quit or querying vanished members", async (t) => {
  const harness = await makeReadbackHarness({ roomReadSequence: ["765432109", "765432109", "0"] });
  t.after(() => harness.transport.stop());
  await postJson(harness.port, "/v1/group/quit", quitBody());
  const memberQueries = harness.calls.filter((r) => r.command === 13).length;
  const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(proof.body.confirmed, true);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.transport.status().groupLocalQuitObservation.readAttempts, 2);
  assert.equal(harness.calls.filter((r) => r.command === 8).length, 1);
  assert.equal(harness.calls.filter((r) => r.command === 13).length, memberQueries);
  const reads = harness.calls.filter((r) => r.command === 14);
  assert.equal(reads.length, 3);
  assert.equal(new Set(reads.map((r) => r.id)).size, reads.length);
});

test("a different physical room never starts a settle retry, and the old room can wait only three reads", async (t) => {
  for (const foreign of [true, false]) {
    await t.test(foreign ? "foreign room" : "witnessed room remains", async (t) => {
      const harness = await makeReadbackHarness({ roomReadSequence: foreign
        ? ["765432109", "987654321", "0"] : ["765432109", "765432109", "765432109", "765432109", "0"] });
      t.after(() => harness.transport.stop());
      await postJson(harness.port, "/v1/group/quit", quitBody());
      const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
      assert.equal(proof.body.confirmed, false);
      assert.equal(harness.transport.status().groupCallReserved, true);
      assert.equal(harness.transport.status().groupLocalQuitObservation.readAttempts, foreign ? 1 : 3);
      assert.equal(harness.calls.filter((r) => r.command === 8).length, 1);
      assert.equal(harness.calls.filter((r) => r.command === 14).length, foreign ? 2 : 4);
    });
  }
});

test("a context change on a settle read cannot release even when that read returns zero", async (t) => {
  let reads = 0;
  const harness = await makeReadbackHarness({ roomReadSequence: ["765432109", "765432109", "0"],
    beforeRoomRead: async (request, { transport }) => {
      if (++reads === 3) await transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
        value: [0, 3, "987654321", "", 0, "", 1] });
    } });
  t.after(() => harness.transport.stop());
  await postJson(harness.port, "/v1/group/quit", quitBody());
  const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(proof.body.code, "group_local_room_context_changed");
  assert.equal(harness.transport.status().groupCallReserved, true);
});

test("a missing optional witness ACK still posts Quit within its original HTTP budget without authorizing local release", async (t) => {
  const harness = await makeReadbackHarness({ noReadAck: true });
  t.after(() => harness.transport.stop());
  const startedAt = Date.now();
  const quit = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(quit.status, 200);
  assert.ok(Date.now() - startedAt < 3500);
  assert.equal(harness.calls.filter((r) => r.command === 8).length, 1);
  assert.equal(harness.transport.status().groupLocalQuitWitnessAvailable, false);
  const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(proof.body.confirmed, false);
  assert.equal(harness.transport.status().groupCallReserved, true);
});

test("a new native entry while the no-room readback is in flight invalidates the old witness", async (t) => {
  let reads = 0;
  const harness = await makeReadbackHarness({ beforeRoomRead: async (request, { transport }) => {
    if (++reads === 2) await transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
      value: [0, 3, "987654321", "", 0, "", 1] });
  } });
  t.after(() => harness.transport.stop());
  await postJson(harness.port, "/v1/group/quit", quitBody());
  const proof = await postJson(harness.port, "/v1/group/quit-absence", quitProofBody());
  assert.equal(proof.body.code, "group_local_room_context_changed");
  assert.equal(harness.transport.status().groupCallReserved, true);
});

test("typed local-room diagnostic is authenticated, login/generation scoped, and cannot release a group", async (t) => {
  const harness = await createHarness({ beforeInvoke: async (request, { emit }) => {
    if (request.command === 14) {
      await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION, value: [0, "", 0] });
    }
  }});
  t.after(() => harness.transport.stop());
  const body = { id: 501, hostGeneration: HOST_GENERATION, groupId: GROUP_ID };
  const route = "/v1/group/current-room-id";
  assert.equal((await postJson(harness.port, route, body, { token: "" })).status, 401);
  assert.equal((await postJson(harness.port, route, body)).body.code, "group_login_unverified");
  await loginAndSelect(harness);
  assert.equal((await postJson(harness.port, route, { ...body, hostGeneration: HOST_GENERATION + 1 })).body.code, "generation_mismatch");
  assert.equal((await postJson(harness.port, route, { ...body, unexpected: true })).body.code, "invalid_request_shape");
  const idle = await postJson(harness.port, route, body);
  assert.equal(idle.status, 200);
  assert.equal(idle.body.observation.dataKind, "zero_number");
  assert.equal(idle.body.diagnosticOnly, true);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal((await postJson(harness.port, "/v1/group/start", startBody())).status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true);
  const occupied = await postJson(harness.port, route, { ...body, id: 502 });
  assert.equal(occupied.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "empty diagnostic is not absence proof");
  assert.equal((await postJson(harness.port, route, { ...body, id: 503, groupId: "987654321" })).body.code, "group_call_unowned");
  const generic = await postJson(harness.port, "/v1/invoke", { id: 504, hostGeneration: HOST_GENERATION, command: 14, params: [] });
  assert.notEqual(generic.status, 200);
});

test("private room readback is authenticated, peer-bound, diagnostic only and retains the private barrier", async (t) => {
  const harness = await createHarness({ verifiedLocalRoomIdProtocol: true, verifiedPrivateEndProtocol: true,
    beforeInvoke: async (request, { emit }) => {
      if (request.command === 14) await emit({ command: 14, id: request.id,
        hostGeneration: HOST_GENERATION, value: [0, "", "0"] });
    } });
  t.after(() => harness.transport.stop());
  const route = "/v1/private/current-room-id";
  const body = { id: 701, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID };
  assert.equal((await postJson(harness.port, route, body, { token: "" })).status, 401);
  assert.equal((await postJson(harness.port, route, body)).body.code, "private_local_room_unowned");
  await loginAndSelect(harness);
  await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
    value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "private-invite" }) });
  assert.equal((await postJson(harness.port, route, body)).body.code, "private_local_room_unowned", "never query an uninitialized unentered SDK room");
  await harness.transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
    value: [0, 1, "private-enter", OTHER_UID, 0, "", 1] });
  assert.equal((await postJson(harness.port, route, { ...body, peerUid: SELF_UID })).body.code, "private_local_room_unowned");
  assert.equal((await postJson(harness.port, route, { ...body, hostGeneration: HOST_GENERATION + 1 })).body.code, "generation_mismatch");
  assert.equal((await postJson(harness.port, route, { ...body, extra: true })).status, 400);
  assert.equal(harness.calls.some(({ command }) => command === 14), false);
  const result = await postJson(harness.port, route, body);
  assert.equal(result.status, 200);
  assert.equal(result.body.id, body.id);
  assert.equal(result.body.diagnosticOnly, true);
  assert.equal(result.body.observation.dataKind, "zero_string");
  assert.equal(result.body.confirmed, undefined);
  assert.equal((await postJson(harness.port, "/v1/group/start", startBody())).body.code, "group_private_call_pending");
});

test("private readback refuses unverified protocol and a call that changes during the SDK read", async (t) => {
  for (const verified of [false, true]) {
    const harness = await createHarness({ verifiedLocalRoomIdProtocol: verified, verifiedPrivateEndProtocol: true,
      beforeInvoke: async (request, { transport, emit }) => {
        if (request.command !== 14) return;
        await transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
          value: groupInviteValue({ scene: 1, inviter: SELF_UID, relationId: "foreign-invite" }) });
        await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION, value: [0, "", "0"] });
      } });
    t.after(() => harness.transport.stop());
    await loginAndSelect(harness);
    await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
      value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "private-invite" }) });
    await harness.transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
      value: [0, 1, "private-enter", OTHER_UID, 0, "", 1] });
    const response = await postJson(harness.port, "/v1/private/current-room-id",
      { id: 711, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID });
    assert.equal(response.body.code, verified ? "private_local_room_context_changed" : "private_end_protocol_unverified");
    assert.equal(harness.calls.filter(({ command }) => command === 14).length, verified ? 1 : 0);
  }
});

test("only a posted matching reject ACK plus fresh string-zero SDK absence releases an unentered invitation", async (t) => {
  for (const scenario of ["confirmed", "initialized_without_diagnostic", "uninitialized", "lagging", "different_room", "unexpected_string", "new_invite_between_reads", "foreign_ack", "failed_ack", "number_zero", "missing", "active_room", "accepted", "entered_during_read", "reused_id"]) {
    let initialPositiveRead = true;
    let rejectReads = 0;
    const harness = await createHarness({ verifiedLocalRoomIdProtocol: true, verifiedPrivateEndProtocol: true,
      beforeInvoke: async (request, { transport, emit }) => {
        if (request.command === 9) await emit({ command: 9, id: scenario === "foreign_ack" ? request.id + 1 : request.id,
          hostGeneration: HOST_GENERATION, value: scenario === "failed_ack" ? [17, ""] : [0, ""] });
        if (request.command === 14) {
          if (initialPositiveRead) {
            initialPositiveRead = false;
            await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION, value: [0, "", "123456789"] });
            return;
          }
          if (scenario === "entered_during_read") await transport.publishOutput({ command: 20004, id: 0,
            hostGeneration: HOST_GENERATION, value: [0, 1, "new-enter-relation", OTHER_UID, 0, "", 1] });
          rejectReads += 1;
          if (scenario === "new_invite_between_reads" && rejectReads === 2) await transport.publishOutput({
            command: 20006, id: 0, hostGeneration: HOST_GENERATION,
            value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "later-invite" }) });
          const value = scenario === "number_zero" ? [0, "", 0] : scenario === "missing" ? [0, ""] :
            scenario === "unexpected_string" ? [0, "", "unknown-room"] :
            ["active_room", "different_room", "new_invite_between_reads"].includes(scenario) || scenario === "lagging" && rejectReads === 1
              ? [0, "", scenario === "different_room" && rejectReads > 1 ? "987654321" : "123456789"] : [0, "", "0"];
          await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION, value });
        }
      } });
    t.after(() => harness.transport.stop());
    await loginAndSelect(harness);
    if (scenario !== "uninitialized") {
      await harness.transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
        value: [0, 1, "initial-room", OTHER_UID, 0, "", 1] });
      if (scenario === "initialized_without_diagnostic") initialPositiveRead = false;
      else assert.equal((await postJson(harness.port, "/v1/private/current-room-id",
        { id: 718, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID })).status, 200);
      await harness.transport.publishOutput({ command: 20011, id: 0, hostGeneration: HOST_GENERATION,
        value: [1, "initial-room", "initial-sdk-room"] });
    }
    await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
      value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "private-invite" }) });
    if (scenario === "accepted") assert.equal((await postJson(harness.port, "/v1/invoke",
      { id: 719, hostGeneration: HOST_GENERATION, command: 5, params: [1, OTHER_UID, [SELF_UID], 1, "", false, ""] })).status, 200);
    const reject = { id: 720, hostGeneration: HOST_GENERATION, command: 9, params: [1, OTHER_UID, 1] };
    assert.equal((await postJson(harness.port, "/v1/invoke", reject)).status, 200);
    if (scenario === "reused_id") assert.equal((await postJson(harness.port, "/v1/invoke", reject)).status, 200);
    const proof = await postJson(harness.port, "/v1/private/reject-absence",
      { id: 721, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID, rejectRequestId: 720 });
    if (["entered_during_read", "new_invite_between_reads"].includes(scenario)) {
      assert.equal(proof.body.code, "private_local_room_context_changed");
    } else {
      assert.equal(proof.status, 200, scenario);
      assert.equal(proof.body.confirmed, ["confirmed", "initialized_without_diagnostic", "uninitialized", "lagging"].includes(scenario), scenario);
      if (scenario === "uninitialized") {
        assert.equal(proof.body.source, "native_reject_ack_and_no_room_admission");
        assert.equal(harness.calls.some(({ command }) => command === 14), false);
      }
    }
    const group = await postJson(harness.port, "/v1/group/start", startBody());
    const observation = harness.transport.status().privateRejectObservation;
    assert.equal(observation.outcome, scenario === "uninitialized" ? "confirmed_without_room_admission" :
      ["confirmed", "initialized_without_diagnostic", "lagging"].includes(scenario) ? "confirmed" :
      ["entered_during_read", "new_invite_between_reads"].includes(scenario) ? "context_changed" :
      ["number_zero", "missing", "active_room", "different_room", "unexpected_string"].includes(scenario) ? "readback_not_absent" : "prerequisites_unavailable");
    assert.equal(JSON.stringify(observation).includes(OTHER_UID), false);
    if (["lagging", "different_room", "new_invite_between_reads"].includes(scenario)) assert.equal(rejectReads, 2);
    if (scenario === "active_room") assert.equal(rejectReads, 3);
    if (scenario === "unexpected_string") assert.equal(rejectReads, 1);
    if (["confirmed", "initialized_without_diagnostic", "uninitialized", "lagging"].includes(scenario)) assert.equal(group.status, 200);
    else assert.equal(group.body.code, "group_private_call_pending", scenario);
  }
});

test("a confirmed owned group Quit initializes only fresh same-login private rejection readback", async (t) => {
  for (const scenario of ["confirmed", "active_room", "new_invite", "login_changed", "foreign_reject_ack"]) {
    await t.test(scenario, async (t) => {
      let localRoom = "765432109";
      let privateStage = false;
      let privateReads = 0;
      const harness = await createHarness({ verifiedLocalRoomIdProtocol: true, verifiedPrivateEndProtocol: true,
        beforeInvoke: async (request, { emit, transport }) => {
          if (request.command === 8) {
            localRoom = "0";
            await emit({ command: 8, id: request.id, hostGeneration: HOST_GENERATION, value: [0, ""] });
          }
          if (request.command === 9) await emit({ command: 9,
            id: request.id + (scenario === "foreign_reject_ack" ? 1 : 0),
            hostGeneration: HOST_GENERATION, value: [0, ""] });
          if (request.command === 14) {
            if (privateStage) {
              privateReads += 1;
              if (scenario === "new_invite") await transport.publishOutput({ command: 20006, id: 0,
                hostGeneration: HOST_GENERATION,
                value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "newer-invite" }) });
            }
            await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION,
              value: [0, "", localRoom] });
          }
        } });
      t.after(() => harness.transport.stop());
      assert.equal((await startOwnedGroup(harness)).response.status, 200);
      await harness.transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
        value: [0, 3, GROUP_ID, "", 0, "", 1] });
      assert.equal((await postJson(harness.port, "/v1/group/quit", quitBody())).status, 200);
      assert.equal((await postJson(harness.port, "/v1/group/quit-absence", quitProofBody())).body.confirmed, true);
      if (scenario === "login_changed") await loginAndSelect(harness);
      privateStage = true;
      if (scenario === "active_room") localRoom = "987654321";
      await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
        value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "private-after-group" }) });
      assert.equal((await postJson(harness.port, "/v1/invoke", { command: 9, id: 760,
        hostGeneration: HOST_GENERATION, params: [1, OTHER_UID, 1] })).status, 200);
      const proof = await postJson(harness.port, "/v1/private/reject-absence",
        { id: 761, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID, rejectRequestId: 760 });
      if (scenario === "new_invite") assert.equal(proof.body.code, "private_local_room_context_changed");
      else {
        assert.equal(proof.status, 200);
        assert.equal(proof.body.confirmed, scenario === "confirmed");
        assert.equal(proof.body.source, "native_reject_ack_and_sdk_room_absence");
      }
      assert.equal(privateReads, scenario === "active_room" ? 3 :
        ["confirmed", "new_invite"].includes(scenario) ? 1 : 0);
      const group = await postJson(harness.port, "/v1/group/start", startBody());
      if (scenario === "confirmed") assert.equal(group.status, 200);
      else assert.equal(group.body.code, "group_private_call_pending");
    });
  }
});

test("cold rejection proof stays closed after any possible admission or unknown entry, even with no saved room", async (t) => {
  for (const scenario of ["failed_accept_without_invite", "unknown_entry", "foreign_ack", "failed_ack", "reused_id"]) {
    const harness = await createHarness({ verifiedLocalRoomIdProtocol: true, verifiedPrivateEndProtocol: true,
      beforeInvoke: async (request, { emit }) => {
        if (request.command === 5) throw new Error("possible native post, no room callback");
        if (request.command === 9) await emit({ command: 9, id: request.id + (scenario === "foreign_ack" ? 1 : 0),
          hostGeneration: HOST_GENERATION, value: scenario === "failed_ack" ? [17, ""] : [0, ""] });
      } });
    t.after(() => harness.transport.stop());
    await loginAndSelect(harness);
    if (scenario === "failed_accept_without_invite") await postJson(harness.port, "/v1/invoke",
      { command: 5, id: 750, hostGeneration: HOST_GENERATION, params: [1, OTHER_UID, [SELF_UID], 1, "", false, ""] });
    if (scenario === "unknown_entry") await harness.transport.publishOutput({ command: 20004, id: 0,
      hostGeneration: HOST_GENERATION, value: ["unknown entry shape"] });
    await harness.transport.publishOutput({ command: 20006, id: 0, hostGeneration: HOST_GENERATION,
      value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "cold-private-invite" }) });
    const reject = { command: 9, id: 751, hostGeneration: HOST_GENERATION, params: [1, OTHER_UID, 1] };
    await postJson(harness.port, "/v1/invoke", reject);
    if (scenario === "reused_id") await postJson(harness.port, "/v1/invoke", reject);
    const result = await postJson(harness.port, "/v1/private/reject-absence",
      { id: 752, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID, rejectRequestId: 751 });
    assert.equal(result.body.confirmed, false, scenario);
    assert.equal(harness.calls.some(({ command }) => command === 14), false);
  }
});

test("entered private-room cleanup requires the exact entry and new SDK absence, and fences new invites", async (t) => {
  for (const scenario of ["confirmed", "lagging", "different_room", "foreign_entry", "active_room", "new_invite_during_read"]) {
    let reads = 0;
    const harness = await createHarness({ verifiedLocalRoomIdProtocol: true, verifiedPrivateEndProtocol: true,
      beforeInvoke: async (request, { transport, emit }) => {
        if (request.command !== 14) return;
        reads += 1;
        if (scenario === "new_invite_during_read") await transport.publishOutput({ command: 20006, id: 0,
          hostGeneration: HOST_GENERATION, value: groupInviteValue({ scene: 1, inviter: OTHER_UID, relationId: "later-invite" }) });
        await emit({ command: 14, id: request.id, hostGeneration: HOST_GENERATION,
          value: [0, "", scenario === "active_room" || scenario === "lagging" && reads < 3 || scenario === "different_room"
            ? scenario === "different_room" && reads > 1 ? "987654321" : "123456789" : "0"] });
      } });
    t.after(() => harness.transport.stop());
    await loginAndSelect(harness);
    await harness.transport.publishOutput({ command: 20004, id: 0, hostGeneration: HOST_GENERATION,
      value: [0, 1, "private-enter", OTHER_UID, 0, "", 1] });
    const result = await postJson(harness.port, "/v1/private/ended-room-absence",
      { id: 740, hostGeneration: HOST_GENERATION, peerUid: OTHER_UID,
        enterRelationId: scenario === "foreign_entry" ? "wrong-entry" : "private-enter" });
    if (scenario === "new_invite_during_read") assert.equal(result.body.code, "private_local_room_context_changed");
    else assert.equal(result.body.confirmed, ["confirmed", "lagging"].includes(scenario));
    assert.equal(harness.calls.filter(({ command }) => command === 14).length,
      scenario === "foreign_entry" ? 0 : ["lagging", "active_room"].includes(scenario) ? 3 : scenario === "different_room" ? 2 : 1);
    const group = await postJson(harness.port, "/v1/group/start", startBody());
    if (["confirmed", "lagging"].includes(scenario)) assert.equal(group.status, 200);
    else assert.equal(group.body.code, "group_private_call_pending");
  }
});

async function publishFreshGroupInvite(harness, {
  queryId = 430,
  groupId = GROUP_ID,
  value = groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID], showUi: true }),
} = {}) {
  const query = await postJson(harness.port, "/v1/group/room-info", {
    id: queryId, hostGeneration: HOST_GENERATION, groupId,
  });
  assert.equal(query.status, 200);
  await harness.transport.publishOutput({
    command: 20006,
    id: 0,
    value,
    hostGeneration: HOST_GENERATION,
  });
  return query;
}

test("group controls require bearer JSON, exact generation-bound bodies, and leave generic 4/8/13 closed", async (t) => {
  const harness = await createHarness({ verifiedGroupCallProtocol: false });
  t.after(() => harness.transport.stop());

  assert.equal(harness.transport.status().groupCallProtocolAvailable, false);
  assert.equal(harness.transport.status().nativeJoinVerified, undefined);
  const unauthorized = await postJson(harness.port, "/v1/group/start", startBody(), { token: null });
  assert.equal(unauthorized.status, 401);
  const acceptUnauthorized = await postJson(harness.port, "/v1/group/accept", acceptBody(), { token: null });
  assert.equal(acceptUnauthorized.status, 401);
  const wrongType = await postJson(harness.port, "/v1/group/start", startBody(), { contentType: "text/plain" });
  assert.equal(wrongType.status, 400);
  assert.equal(wrongType.body.code, "content_type_required");
  const acceptWrongType = await postJson(harness.port, "/v1/group/accept", acceptBody(), { contentType: "text/plain" });
  assert.equal(acceptWrongType.status, 400);
  assert.equal(acceptWrongType.body.code, "content_type_required");
  for (const [route, body] of [
    ["/v1/group/start", { ...startBody(), uid: SELF_UID }],
    ["/v1/group/accept", { ...acceptBody(), inviterUid: OTHER_UID }],
    ["/v1/group/quit", { ...quitBody(), params: [3, 0] }],
  ]) {
    const extra = await postJson(harness.port, route, body);
    assert.equal(extra.status, 400);
    assert.equal(extra.body.code, "invalid_request_shape");
  }
  const stale = await postJson(harness.port, "/v1/group/start", startBody({ hostGeneration: HOST_GENERATION + 1 }));
  assert.equal(stale.status, 409);
  assert.deepEqual(stale.body, { ok: false, code: "generation_mismatch" });
  const unverified = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(unverified.status, 501);
  assert.deepEqual(unverified.body, {
    ok: false, code: "group_call_protocol_unverified", id: 401, command: 4, posted: false,
  });
  const acceptUnverified = await postJson(harness.port, "/v1/group/accept", acceptBody());
  assert.equal(acceptUnverified.status, 501);
  assert.deepEqual(acceptUnverified.body, {
    ok: false, code: "group_call_protocol_unverified", id: 431, command: 5, posted: false,
  });

  for (const command of [4, 13]) {
    const generic = await postJson(harness.port, "/v1/invoke", {
      command, id: command + 500, hostGeneration: HOST_GENERATION, params: [3, GROUP_ID],
    });
    assert.equal(generic.status, 400);
    assert.equal(generic.body.code, "command_not_allowed");
  }
  const genericQuit = await postJson(harness.port, "/v1/invoke", {
    command: 8, id: 508, hostGeneration: HOST_GENERATION, params: [3, 0],
  });
  assert.equal(genericQuit.status, 400);
  assert.equal(genericQuit.body.code, "invalid_params");
  assert.deepEqual(harness.calls, []);
});

test("Start4 requires own acknowledged login and a completed verified device selection", async (t) => {
  const noLogin = await createHarness();
  t.after(() => noLogin.transport.stop());
  const beforeLogin = await postJson(noLogin.port, "/v1/group/start", startBody());
  assert.equal(beforeLogin.status, 409);
  assert.deepEqual(beforeLogin.body, {
    ok: false, code: "group_login_unverified", id: 401, command: 4, posted: false,
  });
  assert.deepEqual(noLogin.calls, []);

  const noAck = await createHarness({ autoLoginAck: false });
  t.after(() => noAck.transport.stop());
  const login = await postJson(noAck.port, "/v1/invoke", {
    command: 1, id: 300, hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""],
  });
  assert.equal(login.status, 200);
  assert.equal(noAck.transport.status().loginAcknowledged, false);
  const noAckStart = await postJson(noAck.port, "/v1/group/start", startBody());
  assert.equal(noAckStart.status, 409);
  assert.equal(noAckStart.body.code, "group_login_unverified");
  assert.deepEqual(noAck.calls.map((call) => call.command), [57, 1]);

  const noSelection = await createHarness();
  t.after(() => noSelection.transport.stop());
  const okLogin = await postJson(noSelection.port, "/v1/invoke", {
    command: 1, id: 300, hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""],
  });
  assert.equal(okLogin.status, 200);
  const noSelectionStart = await postJson(noSelection.port, "/v1/group/start", startBody());
  assert.equal(noSelectionStart.status, 409);
  assert.equal(noSelectionStart.body.code, "group_audio_device_selection_required");
  assert.deepEqual(noSelection.calls.map((call) => call.command), [57, 1]);
});

test("empty Native TinyId profile establishes trusted identity for targeted group invitations", async (t) => {
  const harness = await createHarness({ roomInfoForQuery: (request) => roomRecord(request.params[1]) });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness, { tinyId: "" });
  assert.equal(harness.transport.status().loginAcknowledged, true);

  await publishFreshGroupInvite(harness, {
    value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] }),
  });
  const accepted = await postJson(harness.port, "/v1/group/accept", acceptBody());
  assert.equal(accepted.status, 200);
  assert.deepEqual(accepted.body, { posted: true, id: 431, command: 5 });
  assert.equal(harness.calls.filter((call) => call.command === 5).length, 1,
    "the Host's trusted self UID from the empty-TinyId login matches the callback target");
});

test("arbitrary third login identity remains untrusted", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  const login = await postJson(harness.port, "/v1/invoke", {
    command: 1,
    id: 300,
    hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, OTHER_UID, "C:\\QQ\\account", ""],
  });
  assert.equal(login.status, 200, "the Host can post the Native login request without trusting an unknown TinyId");
  assert.equal(harness.transport.status().loginAcknowledged, false,
    "a successful command receipt and ACK cannot turn an arbitrary third field into trusted identity");

  const start = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(start.status, 409);
  assert.equal(start.body.code, "group_login_unverified");
  assert.deepEqual(harness.calls.map((call) => call.command), [57, 1]);
});

test("new login and failed audio selection clear the previously confirmed virtual route", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const relogin = await postJson(harness.port, "/v1/invoke", {
    command: 1, id: 302, hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""],
  });
  assert.equal(relogin.status, 200);
  let response = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(response.status, 409);
  assert.equal(response.body.code, "group_audio_device_selection_required");
  assert.equal(harness.calls.filter((call) => call.command === 13 || call.command === 4).length, 0);

  const reselection = await postJson(harness.port, "/v1/audio/select", {
    id: 303,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: INPUT_NAME,
    outputDeviceName: OUTPUT_NAME,
  });
  assert.equal(reselection.status, 200);
  const failedSelection = await postJson(harness.port, "/v1/audio/select", {
    id: 304,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: "Missing Microphone",
    outputDeviceName: OUTPUT_NAME,
  });
  assert.equal(failedSelection.status, 409);
  assert.equal(failedSelection.body.code, "audio_device_not_found");
  response = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(response.status, 409);
  assert.equal(response.body.code, "group_audio_device_selection_required");
  assert.equal(harness.calls.filter((call) => call.command === 13 || call.command === 4).length, 0);
});

test("authenticated Start4 performs a fresh room check and posts only the controlled scene-3 payload", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const response = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, { posted: true, id: 401, command: 4 });
  assert.deepEqual(harness.calls.map(({ command, id, params }) => ({ command, id, params })), [
    { command: 57, id: 300, params: ["1.0.1168"] },
    { command: 1, id: 300, params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""] },
    { command: 103, id: 301, params: [] },
    { command: 102, id: 301, params: [13, 3] },
    { command: 103, id: 301, params: [] },
    { command: 13, id: 401, params: [3, GROUP_ID] },
    { command: 4, id: 401, params: [JSON.stringify({
      scene_id: 3,
      self_uid: SELF_UID,
      invite_count: 0,
      invite_uids: [],
      relation_id: GROUP_ID,
      invite_original: 3,
      sub_business_type: 1,
      invite_reason: 0,
      use_ntrtc_dsp: false,
      c2c_extend_params: {},
      opensdk_enter_room_params: {},
    })] },
  ]);
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallState, "reserved");
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, "a posted Start4 receipt is not a native entry callback");
  assert.equal(harness.transport.status().nativeJoinVerified, undefined);
});

test("Host health exposes only the current id-zero native group-entry proof", async (t) => {
  const queryStarted = defer();
  const queryMayReturn = defer();
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 13 && request.id === 401) {
        queryStarted.resolve();
        await queryMayReturn.promise;
      }
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const starting = postJson(harness.port, "/v1/group/start", startBody());
  await queryStarted.promise;
  assert.deepEqual({
    reserved: harness.transport.status().groupCallReserved,
    state: harness.transport.status().groupCallState,
    quarantined: harness.transport.status().groupCallQuarantined,
    entryVerified: harness.transport.status().groupCallNativeEntryVerified,
  }, { reserved: true, state: "starting", quarantined: false, entryVerified: false });

  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, "a callback before Start4 dispatch is not owned");
  queryMayReturn.resolve();
  const started = await starting;
  assert.equal(started.status, 200);
  assert.equal(started.body.command, 4);
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, "the Start4 post receipt is not native entry evidence");

  const unrelatedFrames = [
    { command: 20004, id: 1, value: [0, 3, GROUP_ID, "", 0, "", 1] },
    { command: 20004, id: 0, value: [0, 3, "987654321", "", 0, "", 1] },
    { command: 20004, id: 0, value: [0, 1, GROUP_ID, "", 0, "", 1] },
    { command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 1, "", 1] },
    { command: 4, id: 401, value: [0, ""] },
  ];
  for (const frame of unrelatedFrames) {
    await harness.transport.publishOutput({ ...frame, hostGeneration: HOST_GENERATION });
    assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, `unrelated command ${frame.command} cannot verify entry`);
  }
  await assert.rejects(harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION + 1,
  }), { code: "generation_mismatch" });
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, "a stale generation cannot verify entry");

  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, true);
  const health = await getJson(harness.port, "/healthz");
  assert.equal(health.status, 200);
  assert.deepEqual({
    reserved: health.body.status.groupCallReserved,
    state: health.body.status.groupCallState,
    quarantined: health.body.status.groupCallQuarantined,
    entryVerified: health.body.status.groupCallNativeEntryVerified,
  }, { reserved: true, state: "reserved", quarantined: false, entryVerified: true });
  assert.equal("groupId" in health.body.status, false);
  assert.equal("startRequestId" in health.body.status, false);
  assert.equal("nativeEntryProof" in health.body.status, false);
  assert.equal("nativeJoinVerified" in health.body.status, false, "entry observability does not promote room-query verification");
  assert.equal(health.body.status.groupNativeObservation.startAck4CorrelatedSeen, true);
  assert.equal(health.body.status.groupNativeObservation.startAck4ReturnCode, 0);
  assert.equal(health.body.status.groupNativeObservation.groupEnter20004Seen, true);
  assert.equal(health.body.status.groupNativeObservation.groupEnter20004Matched, true);
  assert.equal(health.body.status.groupNativeObservation.groupEnter20004ReturnCode, 0);
  assert.equal(health.body.status.groupNativeObservation.groupEnter20004Creator, "false");
  assert.equal(health.body.status.groupNativeObservation.nativeCommandCounts["4"], 1);
  assert.equal(health.body.status.groupNativeObservation.nativeCommandCounts["20004"], 5);

  const quit = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(quit.status, 200);
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, GROUP_ID, "owned-quit-terminal"], hostGeneration: HOST_GENERATION,
  });
  const absence = await postJson(harness.port, "/v1/group/room-info", {
    id: 415, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(absence.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.transport.status().groupCallState, "idle");
  assert.equal(harness.transport.status().groupCallQuarantined, false);
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false, "clearing the reservation clears its proof");
  assert.equal(harness.transport.status().groupNativeObservation, null, "clearing the reservation drops its diagnostics");
});

test("group native observations are dispatch-scoped, bounded, sanitized, and diagnostic only", async (t) => {
  const rawError = `not in group; ret=-67; uid=${SELF_UID}; uin=${SELF_UIN}; bearer=Bearer ${TOKEN}`;
  const preDispatchOutput = defer();
  const harness = await createHarness({
    beforeInvoke: async (request, { transport }) => {
      if (request.command === 13 && request.id === 401) {
        await transport.publishOutput({
          command: 4, id: 401, value: [0, ""], hostGeneration: HOST_GENERATION,
        });
        await transport.publishOutput({
          command: 20004, id: 0, value: [0, 3, GROUP_ID, SELF_UID, 0, "", 1], hostGeneration: HOST_GENERATION,
        });
        await transport.publishOutput({
          command: 20003, id: 0, value: [3, GROUP_ID, rawError], hostGeneration: HOST_GENERATION,
        });
        await transport.publishOutput({
          command: 20005, id: 0, value: [rawError], hostGeneration: HOST_GENERATION,
        });
        preDispatchOutput.resolve();
      }
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const started = await postJson(harness.port, "/v1/group/start", startBody());
  await preDispatchOutput.promise;
  assert.equal(started.status, 200);
  assert.deepEqual(harness.transport.status().groupNativeObservation, {
    startAck4CorrelatedSeen: false,
    startAck4ReturnCode: null,
    startAck4IdZeroSeen: false,
    startAck4IdZeroUntrustedReturnCode: null,
    startAck4OtherIdSeen: false,
    startAck4OtherIdUntrustedReturnCode: null,
    groupEnter20004Seen: false,
    groupEnter20004Matched: false,
    groupEnter20004ReturnCode: null,
    groupEnter20004Creator: "unknown",
    error20003Seen: false,
    error20003Matched: false,
    error20003MessageLength: null,
    error20003MessageSha256: null,
    error20003ReasonFlags: {
      invalid_parameters: false, permission_denied: false, not_in_group: false, group_not_found: false,
      network_error: false, login_required: false, unsupported: false, room_error: false, timeout: false, unknown: false,
    },
    error20003CodeLabel: null,
    error20003Code: null,
    timeout20005Observed: false,
    nativeCommandCounts: Object.create(null),
  }, "outputs during the fresh room query precede Start4 dispatch and are ignored");

  await harness.transport.publishOutput({ command: 4, id: 401, value: [0], hostGeneration: HOST_GENERATION });
  assert.deepEqual({
    seen: harness.transport.status().groupNativeObservation.startAck4CorrelatedSeen,
    returnCode: harness.transport.status().groupNativeObservation.startAck4ReturnCode,
  }, { seen: true, returnCode: null }, "a malformed ACK is observed but cannot supply a native result code");
  await harness.transport.publishOutput({ command: 4, id: 0, value: [-2, ""], hostGeneration: HOST_GENERATION });
  await harness.transport.publishOutput({ command: 4, id: 999, value: [7, "diagnostic only"], hostGeneration: HOST_GENERATION });
  await harness.transport.publishOutput({ command: 4, id: 401, value: [-17, "native rejection"], hostGeneration: HOST_GENERATION });
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 1, GROUP_ID, SELF_UID, 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, "987654321", SELF_UID, 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20004, id: 1, value: [0, 3, GROUP_ID, SELF_UID, 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [3, "987654321", rawError], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [1, GROUP_ID, rawError], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 401, value: [3, GROUP_ID, rawError], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [3, GROUP_ID, `${rawError} ${"x".repeat(8193)}`], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [3, GROUP_ID], hostGeneration: HOST_GENERATION,
  });
  assert.deepEqual({
    seen: harness.transport.status().groupNativeObservation.groupEnter20004Seen,
    matched: harness.transport.status().groupNativeObservation.groupEnter20004Matched,
    returnCode: harness.transport.status().groupNativeObservation.groupEnter20004ReturnCode,
    creator: harness.transport.status().groupNativeObservation.groupEnter20004Creator,
    entryVerified: harness.transport.status().groupCallNativeEntryVerified,
  }, { seen: false, matched: false, returnCode: null, creator: "unknown", entryVerified: false },
  "private, foreign, request-scoped, and malformed callbacks do not become group results");
  assert.equal(harness.transport.status().groupNativeObservation.error20003Seen, false,
    "pre-dispatch, private-scene, foreign-group, wrong-ID, malformed, and oversized errors are ignored");
  assert.equal(harness.transport.status().groupNativeObservation.timeout20005Observed, false,
    "a timeout before Start4 dispatch is ignored");
  await harness.transport.publishOutput({
    command: 20005, id: 992, value: [rawError], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [-9, 3, GROUP_ID, SELF_UID, 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [3, GROUP_ID, rawError], hostGeneration: HOST_GENERATION,
  });

  let observation = harness.transport.status().groupNativeObservation;
  assert.deepEqual({
    seen: observation.error20003Seen,
    matched: observation.error20003Matched,
    messageLength: observation.error20003MessageLength,
    messageSha256: observation.error20003MessageSha256,
    reasonFlags: observation.error20003ReasonFlags,
    codeLabel: observation.error20003CodeLabel,
    code: observation.error20003Code,
    timeoutObserved: observation.timeout20005Observed,
  }, {
    seen: true,
    matched: true,
    messageLength: Buffer.byteLength(rawError, "utf8"),
    messageSha256: crypto.createHash("sha256").update(rawError, "utf8").digest("hex"),
    reasonFlags: {
      invalid_parameters: false, permission_denied: false, not_in_group: true, group_not_found: false,
      network_error: false, login_required: false, unsupported: false, room_error: false, timeout: false, unknown: false,
    },
    codeLabel: "ret",
    code: -67,
    timeoutObserved: true,
  });
  assert.deepEqual({
    correlatedSeen: observation.startAck4CorrelatedSeen,
    correlatedCode: observation.startAck4ReturnCode,
    idZeroSeen: observation.startAck4IdZeroSeen,
    idZeroCode: observation.startAck4IdZeroUntrustedReturnCode,
    otherSeen: observation.startAck4OtherIdSeen,
    otherCode: observation.startAck4OtherIdUntrustedReturnCode,
  }, {
    correlatedSeen: true,
    correlatedCode: -17,
    idZeroSeen: true,
    idZeroCode: -2,
    otherSeen: true,
    otherCode: 7,
  });
  assert.deepEqual({
    seen: observation.groupEnter20004Seen,
    matched: observation.groupEnter20004Matched,
    returnCode: observation.groupEnter20004ReturnCode,
    creator: observation.groupEnter20004Creator,
  }, { seen: true, matched: false, returnCode: -9, creator: "false" },
  "private-scene, foreign-group, request-id, and malformed callbacks do not assert group entry");
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false,
    "a correlated nonzero Start4 return is visible only as diagnostics");
  assert.equal(harness.transport.status().groupCallReserved, true,
    "diagnostic ACKs cannot release the native reservation");
  assert.equal("nativeJoinVerified" in harness.transport.status(), false);
  assert.equal(JSON.stringify(observation).includes(rawError), false);
  assert.equal(JSON.stringify(observation).includes(SELF_UID), false);
  assert.equal(JSON.stringify(observation).includes(SELF_UIN), false);
  assert.equal(JSON.stringify(observation).includes(TOKEN), false,
    "the original error text, IDs, and bearer-like value never enter health status");

  for (let index = 0; index < 1002; index += 1) {
    await harness.transport.publishOutput({ command: 699, id: 0, value: ["ignored"], hostGeneration: HOST_GENERATION });
  }
  for (let index = 0; index < 24; index += 1) {
    await harness.transport.publishOutput({ command: 30000 + index, id: 0, value: [SELF_UID], hostGeneration: HOST_GENERATION });
  }
  observation = harness.transport.status().groupNativeObservation;
  assert.equal(Object.keys(observation.nativeCommandCounts).length, 16);
  assert.equal(observation.nativeCommandCounts["699"], 1000, "each command counter is capped");
  assert.equal(Object.keys(observation.nativeCommandCounts).every((key) => /^[1-9][0-9]*$/.test(key)), true);
  assert.equal(JSON.stringify(observation).includes(SELF_UID), false, "native values and identifiers are never projected");
  const detached = harness.transport.status();
  detached.groupNativeObservation.nativeCommandCounts["699"] = 1;
  detached.groupNativeObservation.groupEnter20004Seen = false;
  detached.groupNativeObservation.error20003ReasonFlags.not_in_group = false;
  assert.equal(harness.transport.status().groupNativeObservation.nativeCommandCounts["699"], 1000);
  assert.equal(harness.transport.status().groupNativeObservation.groupEnter20004Seen, true,
    "status projections do not expose mutable reservation state");
  assert.equal(harness.transport.status().groupNativeObservation.error20003ReasonFlags.not_in_group, true);

  assert.equal((await getJson(harness.port, "/healthz", { token: null })).status, 401);
  const health = await getJson(harness.port, "/healthz");
  assert.equal(health.status, 200);
  assert.equal(health.body.status.groupNativeObservation.startAck4ReturnCode, -17);
  assert.equal(health.body.status.groupNativeObservation.groupEnter20004ReturnCode, -9);
  assert.equal(health.body.status.groupNativeObservation.error20003MessageSha256,
    crypto.createHash("sha256").update(rawError, "utf8").digest("hex"));
  const serializedHealth = JSON.stringify(health.body.status.groupNativeObservation);
  assert.equal(serializedHealth.includes(rawError), false);
  assert.equal(serializedHealth.includes(SELF_UID), false);
  assert.equal(serializedHealth.includes(SELF_UIN), false);
  assert.equal(serializedHealth.includes(TOKEN), false);

  assert.equal((await postJson(harness.port, "/v1/group/quit", quitBody())).status, 200);
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, GROUP_ID, "owned-quit-terminal"], hostGeneration: HOST_GENERATION,
  });
  const absence = await postJson(harness.port, "/v1/group/room-info", {
    id: 415, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(absence.status, 200);
  assert.equal(harness.transport.status().groupNativeObservation, null,
    "successful release drops all observations for that reservation");

  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [1, GROUP_ID, "private-terminal"], hostGeneration: HOST_GENERATION,
  });
  const nextStart = await postJson(harness.port, "/v1/group/start", startBody({ id: 416 }));
  assert.equal(nextStart.status, 200);
  assert.deepEqual(harness.transport.status().groupNativeObservation.nativeCommandCounts, Object.create(null),
    "a new reservation starts with empty diagnostics");
  assert.equal(harness.transport.status().groupNativeObservation.error20003Seen, false,
    "a new reservation clears the prior error callback");
  assert.equal(harness.transport.status().groupNativeObservation.timeout20005Observed, false,
    "a new reservation clears the temporal timeout observation");
  await harness.transport.publishOutput({
    command: 20003, id: 0, value: [3, GROUP_ID, "权限不足; errno=2147483648"], hostGeneration: HOST_GENERATION,
  });
  observation = harness.transport.status().groupNativeObservation;
  assert.equal(observation.error20003ReasonFlags.permission_denied, true,
    "fixed reason flags recognize bounded Chinese SDK error text");
  assert.equal(observation.error20003CodeLabel, null);
  assert.equal(observation.error20003Code, null, "out-of-int32 labeled codes are discarded");
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false);
  await harness.transport.stop();
  assert.equal(harness.transport.status().groupNativeObservation, null, "stop drops diagnostic state");
});

test("stopping a Host generation hides its stale native group-entry proof", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, true);
  const stopped = await harness.transport.stop();
  assert.equal(stopped.groupCallReserved, true, "uncertain native occupancy stays held through stop");
  assert.equal(stopped.groupCallNativeEntryVerified, false, "entry evidence belongs to the stopped Host generation");
  assert.equal(stopped.groupNativeObservation, null, "diagnostic observations are discarded on stop");
});

test("scene-3 invite diagnostics require a fresh positive full-room query and expose no callback identifiers", async (t) => {
  const harness = await createHarness({
    roomInfoForQuery: (request) => roomRecord(request.params[1]),
  });
  t.after(() => harness.transport.stop());

  const firstQuery = await postJson(harness.port, "/v1/group/room-info", {
    id: 430, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(firstQuery.status, 200);
  assert.equal(firstQuery.body.nativeJoinVerified, false);
  assert.equal(firstQuery.body.roomInfo.userNum, firstQuery.body.roomInfo.userList.length);
  assert.equal(harness.transport.status().groupInviteObservation, null);
  assert.equal(harness.calls.filter((call) => call.command === 13).length, 1);
  assert.equal(harness.calls.some((call) => [4, 5, 8].includes(call.command)), false,
    "room observation does not dispatch Start4, accept, or quit");

  await harness.transport.publishOutput({
    command: 20006,
    id: 0,
    value: groupInviteValue({
      inviter: SELF_UIN,
      inviteList: [SELF_UID, OTHER_UID, TOKEN],
      relationId: "987654321",
      showUi: 1,
      sequence: `sequence ${SELF_UIN} ${TOKEN}`,
    }),
    hostGeneration: HOST_GENERATION,
  });
  const foreignInvite = harness.transport.status().groupInviteObservation;
  assert.deepEqual(foreignInvite, {
    seen: true,
    tupleLength: 12,
    relationMatchesFreshRoom: false,
    inviterKind: "uin_pattern",
    inviteListCount: 3,
    subBusinessType: 1,
    showUiFlagKind: "zero_one",
    sequenceStringLength: Buffer.byteLength(`sequence ${SELF_UIN} ${TOKEN}`, "utf8"),
  }, "a valid scene-3 tuple from another group is recorded only as a redacted non-match");

  const unchanged = structuredClone(foreignInvite);
  const sparseValue = groupInviteValue();
  delete sparseValue[5];
  const invalidFrames = [
    { command: 20006, id: 0, value: groupInviteValue({ scene: 1 }) },
    { command: 20006, id: 44, value: groupInviteValue() },
    { command: 20006, id: 0, value: sparseValue },
    { command: 20006, id: 0, value: groupInviteValue().slice(0, 11) },
    { command: 20006, id: 0, value: groupInviteValue({ inviteList: Array(1001).fill("opaque") }) },
    { command: 20006, id: 0, value: groupInviteValue({ sequence: "s".repeat(8193) }) },
    { command: 20006, id: 0, value: groupInviteValue({ relationId: "not-a-group" }) },
    { command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1] },
  ];
  for (const frame of invalidFrames) {
    await harness.transport.publishOutput({ ...frame, hostGeneration: HOST_GENERATION });
    assert.deepEqual(harness.transport.status().groupInviteObservation, unchanged,
      `non-group invite output ${frame.command}/${frame.id} cannot replace the observation`);
  }

  const secondGroupId = "987654321";
  const secondQuery = await postJson(harness.port, "/v1/group/room-info", {
    id: 431, hostGeneration: HOST_GENERATION, groupId: secondGroupId,
  });
  assert.equal(secondQuery.status, 200);
  await harness.transport.publishOutput({
    command: 20006, id: 0,
    value: groupInviteValue({ inviter: "unclassified-inviter", relationId: GROUP_ID, subBusinessType: "bad" }),
    hostGeneration: HOST_GENERATION,
  });
  assert.deepEqual(harness.transport.status().groupInviteObservation, {
    seen: true,
    tupleLength: 12,
    relationMatchesFreshRoom: false,
    inviterKind: "other",
    inviteListCount: 1,
    subBusinessType: null,
    showUiFlagKind: "boolean",
    sequenceStringLength: Buffer.byteLength("group-sequence", "utf8"),
  }, "an older queried room does not match after the last successful room query changes");

  const secretSequence = `sdk sequence ${SELF_UID} ${SELF_UIN} ${TOKEN}`;
  await harness.transport.publishOutput({
    command: 20006, id: 0,
    value: groupInviteValue({ inviter: SELF_UID, inviteList: [SELF_UIN, TOKEN], relationId: secondGroupId, sequence: secretSequence }),
    hostGeneration: HOST_GENERATION,
  });
  const matchingInvite = harness.transport.status().groupInviteObservation;
  assert.deepEqual(matchingInvite, {
    seen: true,
    tupleLength: 12,
    relationMatchesFreshRoom: true,
    inviterKind: "uid_pattern",
    inviteListCount: 2,
    subBusinessType: 1,
    showUiFlagKind: "boolean",
    sequenceStringLength: Buffer.byteLength(secretSequence, "utf8"),
  });

  const detached = harness.transport.status();
  detached.groupInviteObservation.relationMatchesFreshRoom = false;
  detached.groupInviteObservation.inviteListCount = 1000;
  assert.deepEqual(harness.transport.status().groupInviteObservation, matchingInvite,
    "Host status returns a detached allowlisted projection");

  const realNow = Date.now;
  const staleNow = realNow() + 30_001;
  Date.now = () => staleNow;
  try {
    await harness.transport.publishOutput({
      command: 20006, id: 0,
      value: groupInviteValue({ relationId: secondGroupId }),
      hostGeneration: HOST_GENERATION,
    });
  } finally {
    Date.now = realNow;
  }
  assert.equal(harness.transport.status().groupInviteObservation.relationMatchesFreshRoom, false,
    "a room response older than the 30-second correlation window is not fresh evidence");

  const health = await getJson(harness.port, "/healthz");
  assert.equal(health.status, 200);
  const serializedHealth = JSON.stringify(health.body.status.groupInviteObservation);
  for (const secret of [GROUP_ID, secondGroupId, SELF_UID, OTHER_UID, SELF_UIN, TOKEN, secretSequence]) {
    assert.equal(serializedHealth.includes(secret), false, `health never exposes ${secret === TOKEN ? "the bearer token" : "raw invite data"}`);
  }
  health.body.status.groupInviteObservation.inviterKind = "mutated";
  assert.equal((await getJson(harness.port, "/healthz")).body.status.groupInviteObservation.inviterKind, "uid_pattern",
    "health responses cannot mutate the Host's observation");

  const status = harness.transport.status();
  assert.equal(status.groupCallReserved, false);
  assert.equal(status.groupCallNativeEntryVerified, false);
  assert.equal("nativeJoinVerified" in status, false);
  assert.equal(harness.calls.some((call) => [4, 5, 8].includes(call.command)), false,
    "invite diagnostics never invoke a native group action");

  const stopped = await harness.transport.stop();
  assert.equal(stopped.groupInviteObservation, null, "stop clears invite and room-correlation diagnostics");
});

test("typed group accept consumes only a fresh targeted scene-3 invite and posts the source-shaped command 5 tuple", async (t) => {
  const harness = await createHarness({ roomInfoForQuery: (request) => roomRecord(request.params[1]) });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const missing = await postJson(harness.port, "/v1/group/accept", acceptBody());
  assert.equal(missing.status, 409);
  assert.equal(missing.body.code, "group_invite_unavailable");
  assert.equal(harness.calls.some((call) => call.command === 5), false);

  const firstQuery = await postJson(harness.port, "/v1/group/room-info", {
    id: 430, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(firstQuery.status, 200);
  const malformed = groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] });
  malformed[2] = ["not-a-qq-identity"];
  const invalidFrames = [
    { id: 0, value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID], relationId: "987654321" }) },
    { id: 33, value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] }) },
    { id: 0, value: malformed },
    { id: 0, value: groupInviteValue({ inviter: OTHER_UID, inviteList: Array(1001).fill(SELF_UID) }) },
    { id: 0, value: groupInviteValue({ inviter: OTHER_UID, inviteList: [OTHER_UID] }) },
    { id: 0, value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID], subBusinessType: 2 }) },
  ];
  for (const frame of invalidFrames) {
    await harness.transport.publishOutput({ command: 20006, ...frame, hostGeneration: HOST_GENERATION });
  }
  const afterInvalid = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 432 }));
  assert.equal(afterInvalid.status, 409);
  assert.equal(afterInvalid.body.code, "group_invite_unavailable");
  assert.equal(harness.calls.some((call) => call.command === 5), false,
    "foreign, private-id, malformed, oversized, wrong-target, and non-audio invites cannot authorize accept");

  await harness.transport.publishOutput({
    command: 20006, id: 0,
    value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID], sequence: "first-invite-seq" }),
    hostGeneration: HOST_GENERATION,
  });
  const wrongGroup = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 435, groupId: "987654321" }));
  assert.equal(wrongGroup.status, 409);
  assert.equal(wrongGroup.body.code, "group_invite_group_mismatch");
  assert.equal(harness.calls.some((call) => call.command === 5), false);
  await harness.transport.publishOutput({
    command: 20006, id: 0,
    value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID], sequence: "ambiguous-second-seq" }),
    hostGeneration: HOST_GENERATION,
  });
  const ambiguous = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 436 }));
  assert.equal(ambiguous.status, 409);
  assert.equal(ambiguous.body.code, "group_invite_unavailable",
    "multiple matching native invitations invalidate the single-use candidate");

  const rearmedQuery = await postJson(harness.port, "/v1/group/room-info", {
    id: 437, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(rearmedQuery.status, 200);
  await harness.transport.publishOutput({
    command: 20006,
    id: 0,
    value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UIN], subBusinessType: 1, showUi: true }),
    hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.calls.some((call) => [4, 5, 8].includes(call.command)), false,
    "a genuine incoming invitation remains passive until the typed route is called");
  const accepted = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 431 }));
  assert.equal(accepted.status, 200);
  assert.deepEqual(accepted.body, { posted: true, id: 431, command: 5 });
  assert.deepEqual(harness.calls.filter((call) => call.command === 5), [{
    command: 5,
    id: 431,
    params: [3, SELF_UID, [OTHER_UID], 1, GROUP_ID, false, ""],
  }]);
  assert.equal(harness.calls.filter((call) => call.command === 13).length, 3,
    "accept performs a new typed native room query after reserving ownership");
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false,
    "an HTTP command-5 receipt does not prove native room entry");
  assert.equal("nativeJoinVerified" in harness.transport.status(), false);
  const health = await getJson(harness.port, "/healthz");
  const serializedHealth = JSON.stringify(health.body.status);
  assert.equal(serializedHealth.includes(OTHER_UID), false);
  assert.equal(serializedHealth.includes("ambiguous-second-seq"), false,
    "health never exposes captured inviter or invitation sequence data");

  const genericPrivateAccept = await postJson(harness.port, "/v1/invoke", {
    command: 5, id: 432, hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(genericPrivateAccept.status, 409);
  assert.equal(genericPrivateAccept.body.code, "group_call_reserved");
  const duplicate = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 433 }));
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.body.code, "group_start_busy");
  assert.equal(harness.calls.filter((call) => call.command === 5).length, 1,
    "the ticket cannot be consumed twice or routed through generic command 5");
});

test("typed group accept rejects private invitations, expired tickets, changed identity, and missing audio selection", async (t) => {
  const noLoginHarness = await createHarness();
  t.after(() => noLoginHarness.transport.stop());
  const noLogin = await postJson(noLoginHarness.port, "/v1/group/accept", acceptBody());
  assert.equal(noLogin.status, 409);
  assert.equal(noLogin.body.code, "group_login_unverified");
  assert.equal(noLoginHarness.calls.some((call) => call.command === 5), false);

  const privateHarness = await createHarness();
  t.after(() => privateHarness.transport.stop());
  await loginAndSelect(privateHarness);
  await privateHarness.transport.publishOutput({
    command: 20006,
    id: 0,
    value: [1, OTHER_UID, "self-u-id", 1, "private-relation", null, null, null, null, null, 1, "private-tail"],
    hostGeneration: HOST_GENERATION,
  });
  const privateAttempt = await postJson(privateHarness.port, "/v1/group/accept", acceptBody());
  assert.equal(privateAttempt.status, 409);
  assert.equal(privateAttempt.body.code, "group_private_call_pending");
  assert.equal(privateHarness.calls.some((call) => call.command === 5), false,
    "a private scene-1 callback never falls back to typed group acceptance");

  const staleQueryHarness = await createHarness();
  t.after(() => staleQueryHarness.transport.stop());
  await loginAndSelect(staleQueryHarness);
  const staleQuery = await postJson(staleQueryHarness.port, "/v1/group/room-info", {
    id: 429, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(staleQuery.status, 200);
  const realNow = Date.now;
  Date.now = () => realNow() + 30_001;
  try {
    await staleQueryHarness.transport.publishOutput({
      command: 20006, id: 0,
      value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] }),
      hostGeneration: HOST_GENERATION,
    });
  } finally {
    Date.now = realNow;
  }
  const staleQueryAccept = await postJson(staleQueryHarness.port, "/v1/group/accept", acceptBody());
  assert.equal(staleQueryAccept.status, 409);
  assert.equal(staleQueryAccept.body.code, "group_invite_unavailable");
  assert.equal(staleQueryHarness.calls.some((call) => call.command === 5), false,
    "an invite arriving after its room-query witness expires cannot create a ticket");

  const expiredHarness = await createHarness();
  t.after(() => expiredHarness.transport.stop());
  await loginAndSelect(expiredHarness);
  await publishFreshGroupInvite(expiredHarness);
  const realNowBeforeInviteExpiry = Date.now;
  Date.now = () => realNowBeforeInviteExpiry() + 30_001;
  try {
    const expired = await postJson(expiredHarness.port, "/v1/group/accept", acceptBody());
    assert.equal(expired.status, 409);
    assert.equal(expired.body.code, "group_invite_stale");
  } finally {
    Date.now = realNowBeforeInviteExpiry;
  }
  assert.equal(expiredHarness.calls.some((call) => call.command === 5), false);

  const identityHarness = await createHarness();
  t.after(() => identityHarness.transport.stop());
  await loginAndSelect(identityHarness);
  await publishFreshGroupInvite(identityHarness);
  const changedUid = `u_${"N".repeat(22)}`;
  const changedUin = "223456789012";
  const changedLogin = await postJson(identityHarness.port, "/v1/invoke", {
    command: 1, id: 440, hostGeneration: HOST_GENERATION,
    params: [changedUid, changedUin, changedUin, "C:\\QQ\\new-account", ""],
  });
  assert.equal(changedLogin.status, 200);
  const changedSelection = await postJson(identityHarness.port, "/v1/audio/select", {
    id: 441, hostGeneration: HOST_GENERATION,
    inputDeviceName: INPUT_NAME, outputDeviceName: OUTPUT_NAME,
  });
  assert.equal(changedSelection.status, 200);
  const afterIdentityChange = await postJson(identityHarness.port, "/v1/group/accept", acceptBody({ id: 442 }));
  assert.equal(afterIdentityChange.status, 409);
  assert.equal(afterIdentityChange.body.code, "group_invite_unavailable");
  assert.equal(identityHarness.calls.some((call) => call.command === 5), false,
    "a ticket from a previous trusted login is not reusable");

  const noSelectionHarness = await createHarness();
  t.after(() => noSelectionHarness.transport.stop());
  const login = await postJson(noSelectionHarness.port, "/v1/invoke", {
    command: 1, id: 450, hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""],
  });
  assert.equal(login.status, 200);
  const room = await postJson(noSelectionHarness.port, "/v1/group/room-info", {
    id: 451, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(room.status, 200);
  await noSelectionHarness.transport.publishOutput({
    command: 20006, id: 0,
    value: groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] }),
    hostGeneration: HOST_GENERATION,
  });
  const noSelection = await postJson(noSelectionHarness.port, "/v1/group/accept", acceptBody({ id: 452 }));
  assert.equal(noSelection.status, 409);
  assert.equal(noSelection.body.code, "group_audio_device_selection_required");
  assert.equal(noSelectionHarness.calls.some((call) => call.command === 5), false);
});

test("typed group accept reserves synchronously and requires a new positive full native room result", async (t) => {
  const queryEntered = defer();
  const queryMayReturn = defer();
  const harness = await createHarness({
    roomInfoForQuery: (request, queryNumber) => queryNumber === 1
      ? roomRecord(request.params[1])
      : roomRecord(request.params[1], [{ uid: OTHER_UID, account_type: 1 }], 2),
    beforeInvoke: async (request) => {
      if (request.command === 13 && request.id === 431) {
        queryEntered.resolve();
        await queryMayReturn.promise;
      }
    },
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);
  await publishFreshGroupInvite(harness);

  const accepting = postJson(harness.port, "/v1/group/accept", acceptBody({ id: 431 }));
  await queryEntered.promise;
  assert.equal(harness.transport.status().groupCallReserved, true,
    "the reservation exists while the fresh room query is pending");
  assert.equal(harness.transport.status().groupCallState, "starting");
  const privateFallback = await postJson(harness.port, "/v1/invoke", {
    command: 5, id: 432, hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(privateFallback.status, 409);
  assert.equal(privateFallback.body.code, "group_call_reserved");
  const concurrentStart = await postJson(harness.port, "/v1/group/start", startBody({ id: 433 }));
  assert.equal(concurrentStart.status, 409);
  assert.equal(concurrentStart.body.code, "group_start_busy");
  const duplicate = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 434 }));
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.body.code, "group_start_busy");
  const selectionDuringAccept = await postJson(harness.port, "/v1/audio/select", {
    id: 438, hostGeneration: HOST_GENERATION,
    inputDeviceName: INPUT_NAME, outputDeviceName: OUTPUT_NAME,
  });
  assert.equal(selectionDuringAccept.status, 409);
  assert.equal(selectionDuringAccept.body.code, "group_call_reserved");
  const loginDuringAccept = await postJson(harness.port, "/v1/invoke", {
    command: 1, id: 439, hostGeneration: HOST_GENERATION,
    params: [SELF_UID, SELF_UIN, SELF_UIN, "C:\\QQ\\account", ""],
  });
  assert.equal(loginDuringAccept.status, 409);
  assert.equal(loginDuringAccept.body.code, "group_call_reserved");
  assert.equal(harness.calls.some((call) => call.command === 5), false);

  queryMayReturn.resolve();
  const incomplete = await accepting;
  assert.equal(incomplete.status, 502);
  assert.equal(incomplete.body.code, "group_room_metadata_invalid");
  assert.equal(harness.calls.some((call) => call.command === 5), false,
    "an incomplete full-room response cannot dispatch native accept");
  assert.equal(harness.transport.status().groupCallReserved, false,
    "pre-dispatch refusal releases only its temporary reservation");
  const duplicateAfterRefusal = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 435 }));
  assert.equal(duplicateAfterRefusal.status, 409);
  assert.equal(duplicateAfterRefusal.body.code, "group_invite_unavailable",
    "a failed fresh-room recheck invalidates the ticket rather than reviving it");
});

test("accepted invite entry and owned Quit8 use the accept request id and fresh native absence", async (t) => {
  const harness = await createHarness({
    roomInfoForQuery: (request) => roomRecord(request.params[1], [{ uid: OTHER_UID, account_type: 1 }]),
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);
  const inviteValue = groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] });
  await publishFreshGroupInvite(harness, { value: inviteValue });

  const accepted = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 431 }));
  assert.equal(accepted.status, 200);
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, false);
  await harness.transport.publishOutput({
    command: 20004, id: 0,
    value: [0, 3, GROUP_ID, SELF_UID, 0, "", 1],
    hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallNativeEntryVerified, true,
    "only the matched native non-creator scene-3 result verifies an accepted entry");

  const wrongOwner = await postJson(harness.port, "/v1/group/quit", quitBody({ id: 440, startRequestId: 999 }));
  assert.equal(wrongOwner.status, 409);
  assert.equal(wrongOwner.body.code, "group_call_unowned");
  assert.equal(harness.calls.some((call) => call.command === 8), false);

  const quit = await postJson(harness.port, "/v1/group/quit", quitBody({ id: 441, startRequestId: 431 }));
  assert.equal(quit.status, 200);
  assert.deepEqual(harness.calls.filter((call) => call.command === 8), [
    { command: 8, id: 441, params: [3, 0] },
  ]);
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, GROUP_ID, "accepted-room-left"], hostGeneration: HOST_GENERATION,
  });
  const absent = await postJson(harness.port, "/v1/group/room-info", {
    id: 442, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(absent.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false,
    "the accepted reservation releases only after its owned terminal and fresh full absence query");

  await harness.transport.publishOutput({
    command: 20006, id: 0, value: inviteValue, hostGeneration: HOST_GENERATION,
  });
  const replay = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 443 }));
  assert.equal(replay.status, 409);
  assert.equal(replay.body.code, "group_invite_unavailable");
  assert.equal(harness.calls.filter((call) => call.command === 5).length, 1,
    "the consumed native invitation fingerprint cannot authorize a later room generation");

  const realNow = Date.now;
  Date.now = () => realNow() + 30_001;
  try {
    const refreshedRoom = await postJson(harness.port, "/v1/group/room-info", {
      id: 444, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
    });
    assert.equal(refreshedRoom.status, 200);
    await harness.transport.publishOutput({
      command: 20006, id: 0, value: inviteValue, hostGeneration: HOST_GENERATION,
    });
    const delayedReplay = await postJson(harness.port, "/v1/group/accept", acceptBody({ id: 445 }));
    assert.equal(delayedReplay.status, 409);
    assert.equal(delayedReplay.body.code, "group_invite_unavailable");
  } finally {
    Date.now = realNow;
  }
  assert.equal(harness.calls.filter((call) => call.command === 5).length, 1,
    "the generation-scoped replay fence survives ticket expiry and a new positive room query");
});

test("creator callbacks and uncertain command-5 posts never verify or revive a group acceptance", async (t) => {
  const creatorHarness = await createHarness({
    roomInfoForQuery: (request) => roomRecord(request.params[1], [{ uid: OTHER_UID, account_type: 1 }]),
  });
  t.after(() => creatorHarness.transport.stop());
  await loginAndSelect(creatorHarness);
  await publishFreshGroupInvite(creatorHarness);
  const accepted = await postJson(creatorHarness.port, "/v1/group/accept", acceptBody({ id: 431 }));
  assert.equal(accepted.status, 200);
  for (const value of [
    [1, 3, GROUP_ID, SELF_UID, 0, "", 1],
    [0, 3, "987654321", SELF_UID, 0, "", 1],
  ]) {
    await creatorHarness.transport.publishOutput({ command: 20004, id: 0, value, hostGeneration: HOST_GENERATION });
    assert.equal(creatorHarness.transport.status().groupCallNativeEntryVerified, false,
      "failed or foreign native entry results do not prove acceptance");
  }
  await creatorHarness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, OTHER_UID, 1, "", 1], hostGeneration: HOST_GENERATION,
  });
  assert.equal(creatorHarness.transport.status().groupCallNativeEntryVerified, false,
    "a creator callback is not ordinary accepted membership");
  assert.equal(creatorHarness.transport.status().groupCallReserved, true);
  const creatorQuit = await postJson(creatorHarness.port, "/v1/group/quit", quitBody({ id: 440, startRequestId: 431 }));
  assert.equal(creatorQuit.status, 200, "the owned accept reservation can still use its typed Quit8 cleanup");

  const uncertainHarness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 5) return { error: Object.assign(new Error("post outcome unknown"), { code: "network_failed" }) };
      return undefined;
    },
  });
  t.after(() => uncertainHarness.transport.stop());
  await loginAndSelect(uncertainHarness);
  const inviteValue = groupInviteValue({ inviter: OTHER_UID, inviteList: [SELF_UID] });
  await publishFreshGroupInvite(uncertainHarness, { value: inviteValue });
  const uncertain = await postJson(uncertainHarness.port, "/v1/group/accept", acceptBody({ id: 431 }));
  assert.equal(uncertain.status, 502);
  assert.equal(uncertain.body.code, "plugin_post_failed");
  assert.equal(uncertainHarness.transport.status().groupCallQuarantined, true);
  await uncertainHarness.transport.publishOutput({
    command: 20006, id: 0, value: inviteValue, hostGeneration: HOST_GENERATION,
  });
  const retry = await postJson(uncertainHarness.port, "/v1/group/accept", acceptBody({ id: 432 }));
  assert.equal(retry.status, 409);
  assert.equal(retry.body.code, "group_start_busy");
  assert.equal(uncertainHarness.calls.filter((call) => call.command === 5).length, 1,
    "uncertain command-5 post consumes the invitation and quarantines its ownership");
});

test("group Start4 refuses physical, mixed, or duplicate endpoint names before cmd 13", async (t) => {
  for (const [label, inputDeviceName, outputDeviceName] of [
    ["physical", "Microphone (Realtek Audio)", "Speakers (Realtek Audio)"],
    ["mixed", "Steam Streaming Microphone", "Speakers (Realtek Audio)"],
    ["duplicate", "Steam Streaming Microphone", "Steam Streaming Microphone"],
  ]) {
    const harness = await createHarness({ inputDeviceName, outputDeviceName });
    t.after(() => harness.transport.stop());
    await loginAndSelect(harness);

    const response = await postJson(harness.port, "/v1/group/start", startBody());
    assert.equal(response.status, 409, label);
    assert.equal(response.body.code, "group_audio_device_route_not_virtual", label);
    assert.deepEqual(harness.calls.filter((call) => call.command === 13 || call.command === 4), [], label);
    assert.equal(harness.transport.status().groupCallReserved, false, label);
    await harness.transport.stop();
  }
});

test("Steam and VB-Audio virtual endpoint names satisfy the group route gate", async (t) => {
  const harness = await createHarness({
    inputDeviceName: "Steam Streaming Microphone",
    outputDeviceName: "VB-Audio Virtual Cable",
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const response = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, { posted: true, id: 401, command: 4 });
  assert.deepEqual(harness.calls.filter((call) => call.command === 13 || call.command === 4).map((call) => call.command), [13, 4]);
});

test("an empty room is refused before Start4 and releases the reservation", async (t) => {
  const harness = await createHarness({ roomInfoForQuery: () => roomRecord(GROUP_ID, [], 0) });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const response = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(response.status, 409);
  assert.deepEqual(response.body, { ok: false, code: "group_room_not_existing", id: 401, command: 4, posted: false });
  assert.deepEqual(harness.calls.map((call) => call.command), [57, 1, 103, 102, 103, 13]);
  assert.equal(harness.transport.status().groupCallReserved, false);
});

test("group reservation is synchronous across the room check and blocks cmd 5 and duplicate starts", async (t) => {
  const harness = await createHarness({ suppressRoomInfo: true });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const inFlightStart = postJson(harness.port, "/v1/group/start", startBody());
  while (!harness.calls.some((call) => call.command === 13)) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.transport.status().groupCallState, "starting");
  const privateAccept = await postJson(harness.port, "/v1/invoke", {
    command: 5, id: 501, hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(privateAccept.status, 409);
  assert.equal(privateAccept.body.code, "group_call_reserved");
  const duplicate = await postJson(harness.port, "/v1/group/start", startBody({ id: 402 }));
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.body.code, "group_start_busy");
  assert.equal(harness.calls.filter((call) => call.command === 4).length, 0);

  await harness.transport.publishOutput(roomOutput(GROUP_ID, roomRecord()));
  const started = await inFlightStart;
  assert.equal(started.status, 200);
  assert.equal(harness.calls.filter((call) => call.command === 4).length, 1);
});

test("possible Start4 post failure quarantines owned group state and prevents retry", async (t) => {
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 4) return { error: Object.assign(new Error("network outcome unknown"), { code: "network_failed" }) };
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const uncertain = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(uncertain.status, 502);
  assert.deepEqual(uncertain.body, { ok: false, code: "plugin_post_failed" });
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallQuarantined, true);
  const retry = await postJson(harness.port, "/v1/group/start", startBody({ id: 403 }));
  assert.equal(retry.status, 409);
  const accept = await postJson(harness.port, "/v1/invoke", {
    command: 5, id: 503, hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(accept.status, 409);
  assert.equal(harness.calls.filter((call) => call.command === 4).length, 1);
});

test("possible Quit8 post failure quarantines ownership without a pre-dispatch receipt", async (t) => {
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 8) return { error: Object.assign(new Error("network outcome unknown"), { code: "network_failed" }) };
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);

  const uncertain = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(uncertain.status, 502);
  assert.deepEqual(uncertain.body, { ok: false, code: "plugin_post_failed" });
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallQuarantined, true);
  assert.deepEqual(harness.calls.filter((call) => call.command === 8).map((call) => call.params), [[3, 0]]);
});

test("owned Quit8 success needs fresh complete absence without requiring a terminal callback", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  const started = await startOwnedGroup(harness);
  assert.equal(started.response.status, 200);

  const wrongOwner = await postJson(harness.port, "/v1/group/quit", quitBody({ startRequestId: 999 }));
  assert.equal(wrongOwner.status, 409);
  assert.deepEqual(wrongOwner.body, { ok: false, code: "group_call_unowned", id: 402, command: 8, posted: false });
  assert.equal(harness.calls.filter((call) => call.command === 8).length, 0);

  // A terminal that predates this Quit is stale for this ownership proof.
  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "left"], hostGeneration: HOST_GENERATION });
  const quit = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(quit.status, 200);
  assert.deepEqual(quit.body, { posted: true, id: 402, command: 8 });
  assert.deepEqual(harness.calls.at(-1), { command: 8, id: 402, params: [3, 0] });
  const duplicateQuit = await postJson(harness.port, "/v1/group/quit", quitBody({ id: 403 }));
  assert.equal(duplicateQuit.status, 409);
  assert.equal(harness.calls.filter((call) => call.command === 8).length, 1);

  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, SELF_UID, 1, "", 1], hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallReserved, true, "an entry callback is not Host membership proof");
  assert.equal(harness.transport.status().groupCallState, "quitting");
  const genericAccept = await postJson(harness.port, "/v1/invoke", {
    command: 5, id: 504, hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(genericAccept.status, 409);
  assert.equal(genericAccept.body.code, "group_call_reserved");

  const staleTerminalCheck = await postJson(harness.port, "/v1/group/room-info", {
    id: 404, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(staleTerminalCheck.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "a terminal from before Quit cannot release ownership");

  // A post-Quit terminal still does not release until the caller's fresh cmd 13 query.
  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "left"], hostGeneration: HOST_GENERATION });
  assert.equal(harness.transport.status().groupCallReserved, true);
  const membership = await postJson(harness.port, "/v1/group/room-info", {
    id: 405, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(membership.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.deepEqual(harness.calls.at(-1), { command: 13, id: 405, params: [3, GROUP_ID] });
});

test("owned Quit proof survives a lagging self-present roster until a new absence query", async (t) => {
  const harness = await createHarness({
    roomInfoForQuery: (_request, number) => number === 2
      ? roomRecord(GROUP_ID, [{ uid: SELF_UID, account_type: 1 }], 1)
      : roomRecord(),
  });
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);
  await harness.transport.publishOutput({command: 20004, id: 0,
    value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION});
  await postJson(harness.port, "/v1/group/quit", quitBody());
  await harness.transport.publishOutput({command: 8, id: 402, value: [0, ""], hostGeneration: HOST_GENERATION});
  await postJson(harness.port, "/v1/group/room-info", {id: 404, hostGeneration: HOST_GENERATION, groupId: GROUP_ID});
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallState, "quitting");
  const duplicate = await postJson(harness.port, "/v1/group/quit", quitBody({id: 405}));
  assert.equal(duplicate.status, 409);
  await postJson(harness.port, "/v1/group/room-info", {id: 406, hostGeneration: HOST_GENERATION, groupId: GROUP_ID});
  assert.equal(harness.transport.status().groupCallReserved, false);
  assert.equal(harness.calls.filter(c => c.command === 8).length, 1);
});

test("Host accepts fast owned Quit success only with fresh absence", async (t) => {
  const harness = await createHarness({ beforeInvoke: async (request, { transport }) => {
    if (request.command === 8) await transport.publishOutput({
      command: 8, id: request.id, value: [0, ""], hostGeneration: HOST_GENERATION,
    });
  } });
  t.after(() => harness.transport.stop());
  const start = await startOwnedGroup(harness);
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  const result = await postJson(harness.port, "/v1/group/quit", {
    id: 505, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: start.body.id,
  });
  assert.equal(result.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "fast ACK alone does not release");
  const absence = await postJson(harness.port, "/v1/group/room-info", {
    id: 506, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(absence.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false);
});

test("foreign and failed Quit ACKs cannot authorize native absence release", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  const start = await startOwnedGroup(harness);
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await postJson(harness.port, "/v1/group/quit", {
    id: 507, hostGeneration: HOST_GENERATION, groupId: GROUP_ID, startRequestId: start.body.id,
  });
  await harness.transport.publishOutput({ command: 8, id: 0, value: [0, ""], hostGeneration: HOST_GENERATION });
  await harness.transport.publishOutput({ command: 8, id: 508, value: [0, ""], hostGeneration: HOST_GENERATION });
  await harness.transport.publishOutput({ command: 8, id: 507, value: [1, ""], hostGeneration: HOST_GENERATION });
  await postJson(harness.port, "/v1/group/room-info", { id: 509, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });
  assert.equal(harness.transport.status().groupCallReserved, true);
  await harness.transport.publishOutput({ command: 8, id: 507, value: [0, ""], hostGeneration: HOST_GENERATION });
  await postJson(harness.port, "/v1/group/room-info", { id: 510, hostGeneration: HOST_GENERATION, groupId: GROUP_ID });
  assert.equal(harness.transport.status().groupCallReserved, false);
});

test("remote terminal requires a matched native entry and one fresh full absence query before Host releases", async (t) => {
  let users = [{ uid: OTHER_UID, account_type: 1 }];
  let userNum = 1;
  const harness = await createHarness({
    roomInfoForQuery: (request) => roomRecord(request.params[1], users, userNum),
  });
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);

  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "before-entry"], hostGeneration: HOST_GENERATION });
  let checked = await postJson(harness.port, "/v1/group/room-info", {
    id: 410, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(checked.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "terminal without native entry cannot clear ownership");

  await harness.transport.publishOutput({
    command: 20004, id: 1, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "positive-entry-id"], hostGeneration: HOST_GENERATION });
  checked = await postJson(harness.port, "/v1/group/room-info", {
    id: 411, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(checked.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "positive-id entry output is foreign to the native callback gate");

  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({ command: 20011, id: 1, value: [3, GROUP_ID, "positive-terminal-id"], hostGeneration: HOST_GENERATION });
  checked = await postJson(harness.port, "/v1/group/room-info", {
    id: 412, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(checked.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true, "positive-id terminal output cannot authorize release");

  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "matched-remote-end"], hostGeneration: HOST_GENERATION });
  checked = await postJson(harness.port, "/v1/group/room-info", {
    id: 413, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  assert.equal(checked.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false, "fresh complete recognized self-absence releases the matching reservation");
  assert.deepEqual(harness.calls.filter((call) => call.command === 13).map((call) => call.id), [401, 410, 411, 412, 413]);
});

test("duplicate and unrelated callbacks do not stale a deferred remote absence query", async (t) => {
  const queryStarted = defer();
  const queryMayReturn = defer();
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 13 && request.id === 414) {
        queryStarted.resolve();
        await queryMayReturn.promise;
      }
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);
  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, GROUP_ID, "matched-remote-end"], hostGeneration: HOST_GENERATION,
  });

  const pending = postJson(harness.port, "/v1/group/room-info", {
    id: 414, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  await queryStarted.promise;
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, GROUP_ID, "duplicate-remote-end"], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [3, "987654321", "foreign-relation"], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20011, id: 0, value: [1, GROUP_ID, "private-scene"], hostGeneration: HOST_GENERATION,
  });
  await harness.transport.publishOutput({
    command: 20011, id: 77, value: [3, GROUP_ID, "request-scoped-terminal"], hostGeneration: HOST_GENERATION,
  });
  assert.equal(harness.transport.status().groupCallReserved, true, "callbacks cannot release while the query is pending");

  queryMayReturn.resolve();
  const result = await pending;
  assert.equal(result.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, false, "the original matched proof remains current");
  assert.deepEqual(harness.calls.filter((call) => call.command === 13).map((call) => call.id), [401, 414]);
});

test("remote terminal Host release rejects self-present, partial, and unknown member lists", async (t) => {
  for (const [label, members, count] of [
    ["self UID", [{ uid: OTHER_UID, account_type: 1 }, { uid: SELF_UID, account_type: 1 }], 2],
    ["self UIN", [{ uid: OTHER_UID, account_type: 1 }, { uid: SELF_UIN, account_type: 2 }], 2],
    ["partial list", [{ uid: OTHER_UID, account_type: 1 }], 2],
    ["unknown identity", [{ uid: "opaque_sdk_member", account_type: 1 }], 1],
  ]) {
    const harness = await createHarness({ roomInfoForQuery: (request) => roomRecord(request.params[1], members, count) });
    t.after(() => harness.transport.stop());
    await startOwnedGroup(harness);
    await harness.transport.publishOutput({
      command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 0, "", 1], hostGeneration: HOST_GENERATION,
    });
    await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "remote-end"], hostGeneration: HOST_GENERATION });
    const checked = await postJson(harness.port, "/v1/group/room-info", {
      id: 420, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
    });
    assert.equal(checked.status, 200, label);
    assert.equal(harness.transport.status().groupCallReserved, true, `${label} cannot prove absence`);
  }
});

test("early unexpected creator may use owned Quit8 while Start4 receipt is pending", async (t) => {
  const startEntered = defer();
  const startReceipt = defer();
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 4) {
        startEntered.resolve();
        await startReceipt.promise;
      }
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);

  const starting = postJson(harness.port, "/v1/group/start", startBody());
  await startEntered.promise;
  assert.equal(harness.transport.status().groupCallState, "starting_post");
  const unrelatedQuit = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(unrelatedQuit.status, 409, "starting_post still refuses Quit8 before owned creator-entry evidence");
  assert.equal(harness.calls.some((call) => call.command === 8), false);

  await harness.transport.publishOutput({
    command: 20004, id: 0, value: [0, 3, GROUP_ID, "", 1, "", 17], hostGeneration: HOST_GENERATION,
  });
  const cleanup = await postJson(harness.port, "/v1/group/quit", quitBody());
  assert.equal(cleanup.status, 200);
  assert.deepEqual(harness.calls.filter((call) => call.command === 8).map((call) => call.params), [[3, 0]]);
  assert.equal(harness.transport.status().groupCallState, "quitting");

  startReceipt.resolve();
  const started = await starting;
  assert.equal(started.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true);
  assert.equal(harness.transport.status().groupCallState, "quitting", "late Start4 receipt cannot overwrite in-flight cleanup state");
});

test("Quit absence check holds ownership for either trusted identifier, unknown IDs, and partial user lists", async (t) => {
  for (const [label, users, userNum] of [
    ["own UID", [{ uid: SELF_UID, account_type: 1 }], 1],
    ["own UIN", [{ uid: SELF_UIN, account_type: 2 }], 1],
    ["unknown representation", [{ uid: "opaque_sdk_member", account_type: 1 }], 1],
    ["partial list", [{ uid: OTHER_UID, account_type: 1 }], 2],
  ]) {
    let postQuitQueries = 0;
    const harness = await createHarness({
      roomInfoForQuery: (_request, queryNumber) => {
        if (queryNumber === 1) return roomRecord();
        postQuitQueries += 1;
        return roomRecord(GROUP_ID, users, userNum);
      },
    });
    t.after(() => harness.transport.stop());
    await startOwnedGroup(harness);
    const quit = await postJson(harness.port, "/v1/group/quit", quitBody());
    assert.equal(quit.status, 200, label);
    await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "left"], hostGeneration: HOST_GENERATION });
    const checked = await postJson(harness.port, "/v1/group/room-info", {
      id: 406, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
    });
    assert.equal(checked.status, 200, label);
    assert.equal(postQuitQueries, 1, label);
    assert.equal(harness.transport.status().groupCallReserved, true, `${label} cannot prove absence`);
    await harness.transport.stop();
  }
});

test("fresh room query started before matching Quit terminal cannot clear later", async (t) => {
  const queryStarted = defer();
  const queryMayReturn = defer();
  const harness = await createHarness({
    beforeInvoke: async (request) => {
      if (request.command === 13 && request.id === 407) {
        queryStarted.resolve();
        await queryMayReturn.promise;
      }
      return undefined;
    },
  });
  t.after(() => harness.transport.stop());
  await startOwnedGroup(harness);
  await postJson(harness.port, "/v1/group/quit", quitBody());

  const pendingQuery = postJson(harness.port, "/v1/group/room-info", {
    id: 407, hostGeneration: HOST_GENERATION, groupId: GROUP_ID,
  });
  await queryStarted.promise;
  await harness.transport.publishOutput({ command: 20011, id: 0, value: [3, GROUP_ID, "left"], hostGeneration: HOST_GENERATION });
  queryMayReturn.resolve();
  await harness.transport.publishOutput(roomOutput(GROUP_ID, roomRecord()));
  const response = await pendingQuery;
  assert.equal(response.status, 200);
  assert.equal(harness.transport.status().groupCallReserved, true);
});

test("private scene-1 invite keeps the Host's conservative pending blocker without changing private accept", async (t) => {
  const harness = await createHarness();
  t.after(() => harness.transport.stop());
  await loginAndSelect(harness);
  const privatePeer = `u_${"P".repeat(22)}`;
  await harness.transport.publishOutput({
    command: 20006,
    id: 0,
    value: [1, privatePeer, "self-u-id", 9, "private-relation", null, null, null, null, null, 1, "private-tail"],
    hostGeneration: HOST_GENERATION,
  });

  const blockedGroup = await postJson(harness.port, "/v1/group/start", startBody());
  assert.equal(blockedGroup.status, 409);
  assert.equal(blockedGroup.body.code, "group_private_call_pending");
  assert.equal(harness.calls.filter((call) => call.command === 13 || call.command === 4).length, 0);

  const accepted = await postJson(harness.port, "/v1/invoke", {
    command: 5,
    id: 506,
    hostGeneration: HOST_GENERATION,
    params: [0, "caller", ["caller"], 0, "", false, ""],
  });
  assert.equal(accepted.status, 200);
  assert.deepEqual(accepted.body, { posted: true, id: 506, command: 5 });
  assert.equal(harness.calls.at(-1).command, 5);
});
