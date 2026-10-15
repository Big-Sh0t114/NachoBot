"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const test = require("node:test");
const { createControlService } = require("../bridge/control-service.cjs");
const { createHostClient } = require("../bridge/host-client.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");
const {
  PRIVATE_INVITE_PROFILE,
  PINNED_WRAPPER_SHA256,
  nativeInviteEvidenceFromCallback,
} = require("../shared/private-invite-scope.cjs");

const HOST_TOKEN = "local-group-test-token-that-is-long-enough-0123456789";
const CONTROL_TOKEN = Buffer.alloc(32, 0x61).toString("base64url");
const ACCOUNT_ID = "10001";
const CLIENT = "group-adapter-1";
const REQUESTER = "23456";
const HOST_GENERATION = 51;
const CONNECTION_GENERATION = 61;
const SELF_UID = `u_${"S".repeat(22)}`;
const PEER_UID = `u_${"P".repeat(22)}`;
const AUDIO_DEVICE_TARGET = Object.freeze({
  inputDeviceName: "Virtual Capture",
  outputDeviceName: "Virtual Playback",
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function waitFor(predicate, timeoutMs = 1800) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("test_wait_timeout");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function request(port, route, body, token = CONTROL_TOKEN) {
  const bytes = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: "127.0.0.1",
      port,
      path: route,
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "Content-Length": bytes.length,
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        try { resolve({ status: response.statusCode, json: JSON.parse(text) }); }
        catch { reject(new Error("invalid_control_response")); }
      });
    });
    req.once("error", reject);
    req.end(bytes);
  });
}

function groupJoinBody(groupId = "20002", clientInstance = CLIENT) {
  return { accountId: ACCOUNT_ID, clientInstance, groupId, requesterUserId: REQUESTER, triggerKind: "command" };
}

function makeRoomResult(input, users = [{ uid: PEER_UID, accountType: 1 }, { uid: "23457", accountType: 1 }], userNum = users.length) {
  return {
    ok: true,
    hostGeneration: HOST_GENERATION,
    id: input.id,
    groupId: input.groupId,
    diagnosticOnly: true,
    nativeJoinVerified: false,
    roomInfo: { sceneId: 3, relationId: input.groupId, userNum, userList: users.map((user) => ({ ...user })) },
  };
}

function routeProof(captureRenderName, playbackRenderName) {
  return {
    verified: true,
    source: "windows_active_sessions",
    hostPid: 4101,
    observedAtMs: Date.now(),
    hostInput: {
      deviceName: playbackRenderName,
      flow: "capture",
      state: "active",
      endpointIdHash: "a".repeat(64),
      processId: 4102,
    },
    hostOutput: {
      deviceName: captureRenderName,
      flow: "render",
      state: "active",
      endpointIdHash: "b".repeat(64),
      processId: 4101,
    },
  };
}

function makeRig({
  queryRoomInfo,
  startGroup,
  quitGroup,
  localQuitProof = null,
  localRemoteProof = null,
  localClearProof = null,
  selectAudioDevices: selectAudioDevicesBehavior,
  audioDeviceTarget = AUDIO_DEVICE_TARGET,
  audioRouteObserver = null,
  trustedNativeInviteProvider = false,
  selfUid = SELF_UID,
  selfUin = ACCOUNT_ID,
  now = Date.now,
} = {}) {
  const providerListeners = new Set();
  const hostListeners = new Set();
  const genericInvokes = [];
  const forwards = [];
  const roomQueries = [];
  const startCalls = [];
  const quitCalls = [];
  const audioSelections = [];
  let listenerRegistered = false;
  let providerConnected = true;
  let providerAuthenticated = true;
  let hostAvailable = true;
  let roomUsers = [{ uid: PEER_UID, accountType: 1 }, { uid: "23457", accountType: 1 }];
  let roomUserNum = 2;
  let callbackSequence = 0;
  let ipcSequence = 0;
  const nativeInviteBinding = trustedNativeInviteProvider ? Object.freeze({
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    targetPid: 4455,
    kernelCreationTime100ns: "134000000000000000",
    connectionGeneration: CONNECTION_GENERATION,
    listenerMutationEpoch: 2,
    listenerRegistrationSequence: 1,
  }) : null;

  const provider = {
    async attach() {
      listenerRegistered = true;
      return { ok: true, status: { listenerRegistered: true } };
    },
    async detach() {
      listenerRegistered = false;
      return { ok: true, status: { listenerRegistered: false } };
    },
    status() {
      return {
        connected: providerConnected,
        authenticated: providerAuthenticated,
        connectionGeneration: CONNECTION_GENERATION,
        agent: { agent: { listenerRegistered } },
        ...(listenerRegistered && nativeInviteBinding ? { nativeInviteBinding } : {}),
      };
    },
    async forwardAVSDKOutput(command, value) {
      forwards.push({ command, value });
      return { ok: true, result: { ok: true, forwarded: true } };
    },
    subscribe(handler) { providerListeners.add(handler); return () => providerListeners.delete(handler); },
    async emitKernel(method, args, { provenNative = false } = {}) {
      const payload = { kind: "kernel_event", method, args, connectionGeneration: CONNECTION_GENERATION };
      if (provenNative && nativeInviteBinding && listenerRegistered) {
        callbackSequence += 1;
        const evidence = nativeInviteEvidenceFromCallback(
          PRIVATE_INVITE_PROFILE,
          method,
          args,
          callbackSequence,
          nativeInviteBinding.listenerRegistrationSequence,
        );
        if (evidence) {
          ipcSequence += 1;
          payload.nativeInviteEvidence = evidence;
          payload.nativeInviteProvenance = {
            targetPid: nativeInviteBinding.targetPid,
            kernelCreationTime100ns: nativeInviteBinding.kernelCreationTime100ns,
            connectionGeneration: CONNECTION_GENERATION,
            listenerMutationEpoch: nativeInviteBinding.listenerMutationEpoch,
            ipcSequence,
          };
        }
      }
      await Promise.all([...providerListeners].map((handler) => handler("kernel_event", payload)));
    },
    async emitLoss() {
      providerConnected = false;
      providerAuthenticated = false;
      await Promise.all([...providerListeners].map((handler) => handler("broker_status", {
        connected: false,
        authenticated: false,
        connectionGeneration: CONNECTION_GENERATION,
      })));
    },
  };

  const host = {
    status: () => ({ available: hostAvailable, groupCallProtocolAvailable: true, hostGeneration: HOST_GENERATION,
      groupLocalQuitProofAvailable: [localQuitProof, localRemoteProof, localClearProof].some((value) => typeof value === "function") }),
    subscribe(handler) {
      hostListeners.add(handler);
      handler({ type: "availability", available: hostAvailable, hostGeneration: HOST_GENERATION });
      return () => hostListeners.delete(handler);
    },
    async invoke(input) {
      genericInvokes.push({ ...input });
      return { posted: true, id: input.id, command: input.command };
    },
    async selectAudioDevices(input) {
      audioSelections.push({ ...input });
      if (selectAudioDevicesBehavior) return selectAudioDevicesBehavior(input, audioSelections.length);
      return { selected: true, id: input.id, command: 102 };
    },
    async queryGroupRoomInfo(input, options) {
      roomQueries.push({ ...input });
      if (options?.assertCurrent && options.assertCurrent() !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
      if (queryRoomInfo) return queryRoomInfo(input, roomQueries.length, { users: roomUsers.map((user) => ({ ...user })), userNum: roomUserNum });
      return makeRoomResult(input, roomUsers, roomUserNum);
    },
    async startGroup(input, options) {
      startCalls.push({ ...input });
      if (options?.assertCurrent && options.assertCurrent() !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
      if (startGroup) return startGroup(input, options);
      return { posted: true, id: input.id, command: 4 };
    },
    async quitGroup(input, options) {
      quitCalls.push({ ...input });
      if (options?.assertCurrent && options.assertCurrent() !== true) throw Object.assign(new Error("host_invoke_guard_rejected"), { code: "host_invoke_guard_rejected" });
      if (quitGroup) return quitGroup(input, options);
      return { posted: true, id: input.id, command: 8 };
    },
    async confirmGroupQuitAbsence(input, options) {
      if (options?.assertCurrent && options.assertCurrent() !== true) throw new Error("host_invoke_guard_rejected");
      return localQuitProof(input);
    },
    async confirmGroupRemoteAbsence(input, options) {
      if (options?.assertCurrent && options.assertCurrent() !== true) throw new Error("host_invoke_guard_rejected");
      return localRemoteProof(input);
    },
    async clearGroupRoomAbsence(input, options) {
      if (options?.assertCurrent && options.assertCurrent() !== true) throw new Error("host_invoke_guard_rejected");
      return localClearProof(input);
    },
    async emitOutput(command, id, value, generation = HOST_GENERATION) {
      return Promise.all([...hostListeners].map((handler) => handler({
        type: "output", command, id, value, hostGeneration: generation,
      })));
    },
    async emitAvailability(available) {
      hostAvailable = available;
      return Promise.all([...hostListeners].map((handler) => handler({
        type: "availability", available, hostGeneration: HOST_GENERATION,
      })));
    },
  };

  const bridge = createQQCallBridge({
    provider,
    host,
    audioDeviceTarget,
    connectionGeneration: CONNECTION_GENERATION,
    hostGeneration: HOST_GENERATION,
    now,
  });
  const service = createControlService({
    bridge,
    token: CONTROL_TOKEN,
    accountId: ACCOUNT_ID,
    port: 0,
    ...(audioRouteObserver ? { audioRouteObserver } : {}),
  });

  return {
    bridge,
    service,
    provider,
    host,
    genericInvokes,
    forwards,
    roomQueries,
    startCalls,
    quitCalls,
    audioSelections,
    setMembers(users, userNum = users.length) { roomUsers = users.map((user) => ({ ...user })); roomUserNum = userNum; },
  };
}

async function startRig(t, options = {}) {
  const rig = makeRig(options);
  const status = await rig.service.start();
  t.after(async () => {
    await rig.service.stop();
    try { await rig.bridge.stop(); } catch { /* preserve the assertion result */ }
  });
  await rig.bridge.attach();
  const login = await rig.bridge.login({ selfUid: options.selfUid || SELF_UID, selfUin: options.selfUin || ACCOUNT_ID, accountPath: "opaque-account-path" });
  await rig.host.emitOutput(1, login.id, [0, ""]);
  assert.equal(rig.bridge.status().loginState, "ready");
  await request(status.port, "/v1/adapter/poll", { accountId: ACCOUNT_ID, clientInstance: CLIENT, after: 0 });
  return { ...rig, port: status.port };
}

async function joinThroughControl(rig, groupId = "20002") {
  const before = rig.startCalls.length;
  const pending = request(rig.port, "/v1/group/join", groupJoinBody(groupId));
  await waitFor(() => rig.startCalls.length > before);
  await enterOutput(rig, groupId);
  return pending;
}

async function enterOutput(rig, groupId = "20002", generation = HOST_GENERATION) {
  return rig.host.emitOutput(20004, 0, [0, 3, groupId, "", 0, "", 1], generation);
}

test("fresh complete native zero-members refuses before querying and new activity restores the same Bridge", async (t) => {
  let clock = 10000;
  const rig = await startRig(t, { now: () => clock });
  const body = { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" };
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [{
    group_code: "20002", onlineCount: 0, memberUins: [], isNativeTimeout: true,
  }]);
  const empty = await request(rig.port, "/v1/group/status", body);
  assert.equal(empty.status, 409);
  assert.equal(empty.json.code, "group_room_observed_empty");
  assert.equal(rig.roomQueries.length, 0);
  assert.equal(rig.startCalls.length, 0);
  assert.equal(rig.quitCalls.length, 0);
  clock += 1;
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [{
    group_code: "20002", onlineCount: 1, memberUins: [REQUESTER], isNativeTimeout: false,
  }]);
  const restored = await request(rig.port, "/v1/group/status", body);
  assert.equal(restored.status, 200);
  assert.equal(restored.json.nativeJoinVerified, true);
  assert.equal(rig.roomQueries.length, 1, "positive activity still requires a fresh native room query");
});

test("empty-room hint cannot replace missing, partial, stale, foreign or superseded state", async (t) => {
  for (const scenario of ["missing", "partial", "contradictory", "stale", "clock_backwards", "foreign", "new_invitation_update", "new_native_invite"]) {
    await t.test(scenario, async (t) => {
      let clock = Date.now();
      const rig = await startRig(t, { now: () => clock });
      if (scenario !== "missing") {
        const info = { group_code: scenario === "foreign" ? "20003" : "20002", onlineCount: 0,
          ...(scenario === "partial" ? {} : { memberUins: scenario === "contradictory" ? [REQUESTER] : [] }), isNativeTimeout: true };
        await rig.provider.emitKernel("onGroupAudioMemNumChange", [info]);
      }
      if (scenario === "stale") clock += 5000;
      if (scenario === "clock_backwards") clock -= 1;
      if (scenario === "new_native_invite") {
        clock += 1;
        assert.equal((await inviteGroup(rig)).length, 1);
      }
      if (scenario === "new_invitation_update") {
        clock += 1;
        await rig.provider.emitKernel("onGroupVideoInviteMemberUpdate", [{
          groupId: "20002", roomId: "native-room", totalInvitedMembers: "1", seq: "1", join: [SELF_UID], quit: [],
        }]);
      }
      const result = await rig.bridge.getGroupRoomStatus("20002");
      assert.equal(result.ready, true);
      assert.equal(rig.roomQueries.length, 1);
    });
  }
});

test("zero-member hints cannot release an occupied group or revoke its existing scope", async (t) => {
  const rig = await startRig(t);
  const joined = await joinThroughControl(rig);
  const scope = joined.json.scope;
  const queryCount = rig.roomQueries.length;
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [{
    group_code: "20002", onlineCount: 0, memberUins: [], isNativeTimeout: true,
  }]);
  const result = await rig.bridge.getGroupRoomStatus("20002");
  assert.equal(result.occupied, true);
  assert.equal(rig.roomQueries.length, queryCount + 1);
  assert.deepEqual(rig.bridge.getCurrentGroupScope(), scope);
  assert.equal(rig.quitCalls.length, 0);
});

async function groupRoute(rig, scope, { capture = "Virtual Capture", playback = "Virtual Playback" } = {}) {
  return request(rig.port, "/v1/audio/route", {
    accountId: ACCOUNT_ID,
    clientInstance: CLIENT,
    scope,
    captureRenderName: capture,
    playbackRenderName: playback,
  });
}

async function inviteGroup(rig, { groupId = "20002", targets = [SELF_UID], seq = "invitation-1", callerUid = PEER_UID, kernelUid = PEER_UID } = {}) {
  await rig.provider.emitKernel("OnInviteActionToAVSDK", [{ relation_id: groupId, invite_type: 3, from_uid: kernelUid }]);
  await rig.host.emitOutput(20006, 0, [3, callerUid, targets, 1, groupId, null, null, null, null, null, true, seq]);
  const response = await request(rig.port, "/v1/adapter/poll", { accountId: ACCOUNT_ID, clientInstance: CLIENT, after: 0 });
  return response.json.events.filter((event) => event.kind === "group_invite");
}

test("native invitation addressed to self journals once and joins an existing room without a command", async (t) => {
  const rig = await startRig(t);
  const events = await inviteGroup(rig);
  assert.equal(events.length, 1);
  assert.equal(events[0].call, null);
  assert.equal(events[0].groupInvite.groupId, "20002");
  assert.equal(rig.startCalls.length, 0, "Bridge waits for adapter ACL admission");
  const body = { ...groupJoinBody(), triggerKind: "invite", inviteId: events[0].groupInvite.inviteId };
  const wrong = await request(rig.port, "/v1/group/join", { ...body, groupId: "20003" });
  assert.equal(wrong.json.code, "group_invite_stale");
  const joining = request(rig.port, "/v1/group/join", body);
  await waitFor(() => rig.startCalls.length === 1);
  await enterOutput(rig);
  assert.equal((await joining).json.joined, true);
  assert.equal((await inviteGroup(rig)).length, 1, "occupied and duplicate invitations create no new ticket");
});

test("foreign targets, mismatched caller, missing Kernel invite and expired tickets never join", async (t) => {
  for (const values of [{ targets: [PEER_UID] }, { callerUid: SELF_UID }]) {
    const rig = await startRig(t);
    assert.equal((await inviteGroup(rig, values)).length, 0);
    assert.equal(rig.startCalls.length, 0);
  }
  const orphan = await startRig(t);
  await orphan.host.emitOutput(20006, 0, [3, PEER_UID, [SELF_UID], 1, "20002", null, null, null, null, null, true, "orphan"]);
  const poll = await request(orphan.port, "/v1/adapter/poll", { accountId: ACCOUNT_ID, clientInstance: CLIENT, after: 0 });
  assert.equal(poll.json.events.some((event) => event.kind === "group_invite"), false);
  let at = Date.now();
  const expired = await startRig(t, { now: () => at });
  const [event] = await inviteGroup(expired);
  at += 30_000;
  const result = await request(expired.port, "/v1/group/join", {
    ...groupJoinBody(), triggerKind: "invite", inviteId: event.groupInvite.inviteId,
  });
  assert.equal(result.json.code, "group_invite_stale");
  assert.equal(expired.startCalls.length, 0);
});

test("Kernel group relation with empty caller UID uses only the correlated native inviter UID", async (t) => {
  const rig = await startRig(t);
  const [event] = await inviteGroup(rig, { kernelUid: "" });
  assert.equal(event.groupInvite.callerUid, PEER_UID);
  const joining = request(rig.port, "/v1/group/join", { ...groupJoinBody(), triggerKind: "invite", inviteId: event.groupInvite.inviteId });
  await waitFor(() => rig.startCalls.length === 1);
  await enterOutput(rig);
  assert.equal((await joining).json.joined, true);
});

test("invitation expiring during room preflight is consumed and cannot post Start", async (t) => {
  let at = Date.now();
  const gate = deferred();
  const rig = await startRig(t, { now: () => at, queryRoomInfo: () => gate.promise });
  const [event] = await inviteGroup(rig);
  const body = { ...groupJoinBody(), triggerKind: "invite", inviteId: event.groupInvite.inviteId };
  const joining = request(rig.port, "/v1/group/join", body);
  await waitFor(() => rig.roomQueries.length === 1);
  at += 30_000;
  gate.resolve(makeRoomResult(rig.roomQueries[0]));
  assert.notEqual((await joining).status, 200);
  assert.equal(rig.startCalls.length, 0);
  assert.equal((await request(rig.port, "/v1/group/join", body)).json.code, "group_invite_stale");
  assert.equal((await inviteGroup(rig)).length, 1, "replayed callback cannot mint another invitation");
});

test("idle public admission uses fresh room proof without joining or installing an audio scope", async (t) => {
  const rig = await startRig(t);
  const body = { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" };
  const first = await request(rig.port, "/v1/group/status", body);
  const second = await request(rig.port, "/v1/group/status", body);
  assert.equal(first.status, 200);
  assert.equal(first.json.nativeJoinVerified, true);
  assert.equal(first.json.occupied, false);
  assert.equal(first.json.room.roomId, "20002");
  assert.equal(second.json.room.roomGeneration, first.json.room.roomGeneration);
  assert.equal(rig.roomQueries.length, 2, "each preflight queries native room membership afresh");
  assert.equal(rig.startCalls.length, 0);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
  assert.equal(rig.bridge.status().groupCall.occupied, false);
});

test("empty room and generation loss cannot produce public admission proof", async (t) => {
  const empty = await startRig(t);
  empty.setMembers([], 0);
  const body = { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" };
  const response = await request(empty.port, "/v1/group/status", body);
  assert.equal(response.json.nativeJoinVerified, false);
  assert.equal(response.json.room, null);
  const pendingRoom = deferred();
  const lost = await startRig(t, { queryRoomInfo: () => pendingRoom.promise });
  const pending = request(lost.port, "/v1/group/status", body);
  await waitFor(() => lost.roomQueries.length === 1);
  await lost.host.emitAvailability(false);
  pendingRoom.resolve(makeRoomResult(lost.roomQueries[0]));
  const rejected = await pending;
  assert.notEqual(rejected.status, 200);
  assert.equal(lost.startCalls.length, 0);
});

test("a call occupying the slot during a room query cancels its admission proof", async (t) => {
  const pendingRoom = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 1 ? pendingRoom.promise : makeRoomResult(input),
  });
  const pending = request(rig.port, "/v1/group/status", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002",
  });
  await waitFor(() => rig.roomQueries.length === 1);
  const joined = await joinThroughControl(rig);
  assert.equal(joined.json.joined, true);
  pendingRoom.resolve(makeRoomResult(rig.roomQueries[0]));
  const status = await pending;
  assert.equal(status.status, 200);
  assert.equal(status.json.occupied, true);
  assert.equal(status.json.nativeJoinVerified, false);
  assert.equal(status.json.room, null);
});

test("typed Host group APIs validate capability, IDs, generation, guards, and exact receipts", async (t) => {
  const received = [];
  let negativeShape = "exact";
  const server = http.createServer((req, res) => {
    void (async () => {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null;
      received.push({ method: req.method, url: req.url, authorization: req.headers.authorization, body });
      const send = (status, value) => {
        const bytes = Buffer.from(JSON.stringify(value));
        res.writeHead(status, { "Content-Type": "application/json", "Content-Length": bytes.length });
        res.end(bytes);
      };
      if (req.url === "/healthz") {
        send(200, { ok: true, status: {
          ready: true, hostGeneration: HOST_GENERATION, pluginFound: true, hasPostMessage: true,
          rendererGone: false, stopped: false, outputFailed: false, groupCallProtocolAvailable: true,
        } });
      } else if (req.url === "/v1/group/room-info") {
        send(200, makeRoomResult(body));
      } else if (req.url === "/v1/group/start") {
        if (body.groupId === "20003") {
          if (negativeShape === "disconnect") { res.destroy(); return; }
          const refusal = { ok: false, code: "group_room_not_existing", id: body.id, command: 4, posted: false };
          if (negativeShape === "extra") refusal.extra = true;
          if (negativeShape === "missing") delete refusal.id;
          if (negativeShape === "wrong_id") refusal.id += 1;
          if (negativeShape === "wrong_command") refusal.command = 8;
          send(409, refusal);
        } else send(200, { posted: true, id: body.id, command: 4 });
      } else if (req.url === "/v1/group/quit") {
        if (body.groupId === "20003") {
          const refusal = { ok: false, code: "group_quit_refused", id: body.id, command: 8, posted: false };
          if (negativeShape === "extra") refusal.extra = true;
          if (negativeShape === "missing") delete refusal.id;
          if (negativeShape === "wrong_id") refusal.id += 1;
          if (negativeShape === "wrong_command") refusal.command = 4;
          send(409, refusal);
        } else send(200, { posted: true, id: body.id, command: 8 });
      } else send(404, { code: "not_found" });
    })().catch(() => res.destroy());
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const client = createHostClient({
    token: HOST_TOKEN,
    listenPort: server.address().port,
    outputPort: 0,
    hostGeneration: HOST_GENERATION,
    healthPollMs: 10_000,
  });
  t.after(() => client.stop());
  await client.start();
  assert.equal(await client.checkHealth(), true);
  assert.equal(client.status().groupCallProtocolAvailable, true);

  let guardCalls = 0;
  const guard = { assertCurrent: () => { guardCalls += 1; return true; } };
  const room = await client.queryGroupRoomInfo({ id: 301, hostGeneration: HOST_GENERATION, groupId: "20002" }, guard);
  assert.equal(room.roomInfo.relationId, "20002");
  assert.deepEqual(await client.startGroup({ id: 302, hostGeneration: HOST_GENERATION, groupId: "20002" }, guard), {
    posted: true, id: 302, command: 4,
  });
  assert.deepEqual(await client.quitGroup({ id: 303, hostGeneration: HOST_GENERATION, groupId: "20002", startRequestId: 302 }, guard), {
    posted: true, id: 303, command: 8,
  });
  assert.equal(guardCalls, 3);
  assert.deepEqual(received.filter((entry) => entry.url.startsWith("/v1/group/")).map((entry) => entry.url), [
    "/v1/group/room-info", "/v1/group/start", "/v1/group/quit",
  ]);
  assert.ok(received.filter((entry) => entry.url.startsWith("/v1/group/")).every((entry) => entry.authorization === `Bearer ${HOST_TOKEN}`));
  assert.deepEqual(received.find((entry) => entry.url === "/v1/group/quit").body, {
    id: 303, hostGeneration: HOST_GENERATION, groupId: "20002", startRequestId: 302,
  });
  await assert.rejects(client.invoke({ command: 4, id: 309, params: [], hostGeneration: HOST_GENERATION }), {
    code: "invalid_invoke",
  });
  await assert.rejects(client.invoke({ command: 13, id: 310, params: [], hostGeneration: HOST_GENERATION }), {
    code: "invalid_invoke",
  });
  const refused = await client.startGroup({ id: 306, hostGeneration: HOST_GENERATION, groupId: "20003" });
  assert.deepEqual(refused, { posted: false, id: 306, command: 4 }, "only the exact correlated Host refusal becomes a clean refusal receipt");
  for (const shape of ["extra", "missing", "wrong_id", "wrong_command"]) {
    negativeShape = shape;
    await assert.rejects(client.startGroup({ id: 307, hostGeneration: HOST_GENERATION, groupId: "20003" }), {
      code: "group_room_not_existing",
    }, `${shape} negative receipt is not promoted`);
  }
  negativeShape = "exact";
  assert.deepEqual(await client.quitGroup({
    id: 311, hostGeneration: HOST_GENERATION, groupId: "20003", startRequestId: 302,
  }), { posted: false, id: 311, command: 8 });
  for (const shape of ["extra", "missing", "wrong_id", "wrong_command"]) {
    negativeShape = shape;
    await assert.rejects(client.quitGroup({
      id: 312, hostGeneration: HOST_GENERATION, groupId: "20003", startRequestId: 302,
    }), { code: "group_quit_refused" }, `quit ${shape} negative receipt is not promoted`);
  }
  await assert.rejects(client.queryGroupRoomInfo({ id: 304, hostGeneration: HOST_GENERATION + 1, groupId: "20002" }), {
    code: "invalid_group_room_query",
  });
  await assert.rejects(client.startGroup({ id: 305, hostGeneration: HOST_GENERATION, groupId: "020002" }), {
    code: "invalid_group_start",
  });
  negativeShape = "disconnect";
  await assert.rejects(client.startGroup({ id: 308, hostGeneration: HOST_GENERATION, groupId: "20003" }), {
    code: "host_disconnected",
  }, "ambiguous network loss stays an exception");
});

test("group preflight selects exact configured devices while holding the global slot", async (t) => {
  const selectionGate = deferred();
  const rig = await startRig(t, { selectAudioDevices: () => selectionGate.promise });
  const loginId = rig.genericInvokes[0].id;
  const pendingJoin = request(rig.port, "/v1/group/join", groupJoinBody("20002"));
  await waitFor(() => rig.audioSelections.length === 1);
  assert.deepEqual(rig.audioSelections[0], {
    id: rig.audioSelections[0].id,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: AUDIO_DEVICE_TARGET.inputDeviceName,
    outputDeviceName: AUDIO_DEVICE_TARGET.outputDeviceName,
  });
  assert.equal(rig.roomQueries.length, 0, "room info waits for the exact device-selection receipt");
  assert.equal(rig.startCalls.length, 0);
  assert.equal(rig.bridge.status().groupCall.occupied, true, "selection holds the group reservation");
  await assert.rejects(rig.bridge.joinGroup("20003"), { code: "call_occupied" },
    "a concurrent group start remains blocked during selection");

  selectionGate.resolve({ selected: true, id: rig.audioSelections[0].id, command: 102 });
  await waitFor(() => rig.roomQueries.length === 1 && rig.startCalls.length === 1);
  await enterOutput(rig);
  const joined = await pendingJoin;
  assert.equal(joined.status, 200);
  const allIds = [loginId, rig.audioSelections[0].id, rig.roomQueries[0].id, rig.startCalls[0].id];
  assert.equal(new Set(allIds).size, allIds.length, "selection, query, login, and Start use distinct global IDs");
});

test("missing audio target or failed selection stops group preflight before room-info and Start", async (t) => {
  const cases = [
    { name: "missing target", options: { audioDeviceTarget: null }, selectionCount: 0 },
    { name: "selection rejected", options: { selectAudioDevices: async () => { throw new Error("selection failed"); } }, selectionCount: 1 },
    { name: "selection receipt mismatch", options: { selectAudioDevices: async (input) => ({ selected: true, id: input.id + 1, command: 102 }) }, selectionCount: 1 },
  ];
  for (const item of cases) {
    await t.test(item.name, async (t2) => {
      const rig = await startRig(t2, item.options);
      const result = await request(rig.port, "/v1/group/join", groupJoinBody("20002"));
      assert.notEqual(result.status, 200);
      assert.equal(rig.audioSelections.length, item.selectionCount);
      assert.equal(rig.roomQueries.length, 0);
      assert.equal(rig.startCalls.length, 0);
      assert.equal(rig.bridge.status().groupCall.occupied, false);
      assert.equal(rig.genericInvokes.some((entry) => [4, 13, 8].includes(entry.command)), false);
    });
  }
});

test("context loss during group audio selection prevents a late room query or Start", async (t) => {
  const selectionGate = deferred();
  const rig = await startRig(t, { selectAudioDevices: () => selectionGate.promise });
  const pendingJoin = request(rig.port, "/v1/group/join", groupJoinBody("20002"));
  await waitFor(() => rig.audioSelections.length === 1);
  await rig.provider.emitLoss();
  selectionGate.resolve({ selected: true, id: rig.audioSelections[0].id, command: 102 });
  const result = await pendingJoin;
  assert.notEqual(result.status, 200);
  assert.equal(rig.roomQueries.length, 0);
  assert.equal(rig.startCalls.length, 0);
  assert.equal(rig.bridge.status().groupCall.occupied, false);
});

test("trusted private invite during group device selection preserves busy handling and aborts preflight", async (t) => {
  const selectionGate = deferred();
  const rig = await startRig(t, {
    trustedNativeInviteProvider: true,
    selectAudioDevices: () => selectionGate.promise,
  });
  const controlEvents = [];
  rig.bridge.subscribeControlEvents((event) => controlEvents.push(event));
  const pendingJoin = request(rig.port, "/v1/group/join", groupJoinBody("20002"));
  await waitFor(() => rig.audioSelections.length === 1);
  await rig.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "98765", invite_type: 1, from_uid: PEER_UID,
  }], { provenNative: true });
  assert.equal(rig.bridge.getIntegrationSnapshot().call, null, "the invite cannot steal the reserved slot");
  assert.equal(controlEvents.length, 1);
  assert.equal(controlEvents[0].kind, "invite_busy");
  selectionGate.resolve({ selected: true, id: rig.audioSelections[0].id, command: 102 });
  const result = await pendingJoin;
  assert.notEqual(result.status, 200);
  assert.equal(rig.roomQueries.length, 0);
  assert.equal(rig.startCalls.length, 0);
  assert.equal(rig.bridge.status().groupCall.occupied, false);
  assert.equal(rig.bridge.status().counts.busyInvites, 1);
});

test("group reservation blocks private invites, ignores foreign callbacks, and requires native entry beyond command ACK", async (t) => {
  const queryGate = deferred();
  const startGate = deferred();
  const rig = await startRig(t, {
    trustedNativeInviteProvider: true,
    queryRoomInfo: (input, index) => index === 1 ? queryGate.promise : makeRoomResult(input),
    startGroup: (input) => startGate.promise,
  });
  const occupancyBody = { accountId: ACCOUNT_ID, clientInstance: CLIENT };
  const initiallyFree = await request(rig.port, "/v1/call/occupancy", occupancyBody);
  assert.deepEqual(Object.keys(initiallyFree.json).sort(), [
    "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration", "occupied", "kind",
  ].sort());
  assert.equal(initiallyFree.json.occupied, false);
  assert.equal(initiallyFree.json.kind, null);
  const controlEvents = [];
  rig.bridge.subscribeControlEvents((event) => controlEvents.push(event));
  const joinPromise = joinThroughControl(rig);
  await waitFor(() => rig.roomQueries.length === 1);
  const reserved = await request(rig.port, "/v1/call/occupancy", occupancyBody);
  assert.equal(reserved.json.occupied, true, "preflight reservation is part of global group occupancy");
  assert.equal(reserved.json.kind, "group");
  await rig.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "98765", invite_type: 1, from_uid: PEER_UID,
  }], { provenNative: true });
  assert.equal(rig.bridge.getIntegrationSnapshot().call, null, "private invite cannot occupy the reserved group slot");
  assert.equal(rig.bridge.status().counts.busyInvites, 1);
  assert.equal(controlEvents.length, 1, "the existing proof-carrying busy event remains available");
  assert.equal(controlEvents[0].kind, "invite_busy");
  assert.equal(controlEvents[0].busyInvite.callerUid, PEER_UID);

  queryGate.resolve(makeRoomResult({ id: rig.roomQueries[0].id, groupId: "20002" }));
  await waitFor(() => rig.startCalls.length === 1);
  const startId = rig.startCalls[0].id;
  assert.deepEqual(rig.genericInvokes.map((entry) => entry.command), [1], "group 4/13/8 use typed APIs only");
  await rig.host.emitOutput(4, startId, [0, ""]);
  await enterOutput(rig, "99999");
  await rig.host.emitOutput(20004, 1, [0, 3, "20002", "", 0, "", 1]);
  await assert.rejects(rig.host.emitOutput(20004, 0, [0, 3, "20002", "", 0, "", 1], HOST_GENERATION + 1), {
    code: "stale_host_generation",
  });
  assert.equal(rig.bridge.getCurrentGroupScope(), null, "an ACK or foreign scene callback is not native entry proof");

  await enterOutput(rig);
  assert.equal(rig.bridge.getCurrentGroupScope(), null, "entry callback arriving before the Host post receipt is only a candidate");
  startGate.resolve({ posted: true, id: startId, command: 4 });
  const joined = await joinPromise;
  assert.equal(joined.status, 200);
  assert.equal(joined.json.joined, true);
  assert.equal(joined.json.source, "native_scene3_relation");
  assert.deepEqual(Object.keys(joined.json).sort(), [
    "version", "accountId", "serviceInstance", "hostGeneration", "connectionGeneration", "joined", "scope",
    "source", "requesterUserId",
  ].sort());
  const scope = joined.json.scope;
  assert.deepEqual(rig.bridge.getCurrentGroupScope(), scope);
  await rig.host.emitOutput(20011, 1, [3, "20002", "foreign-rtc-room"]);
  assert.deepEqual(rig.bridge.getCurrentGroupScope(), scope, "positive-id command replies cannot suspend a group scope");
  await rig.host.emitOutput(20011, 0, [3, "99999", "foreign-rtc-room"]);
  assert.deepEqual(rig.bridge.getCurrentGroupScope(), scope, "foreign terminal relation does not suspend the joined scope");
  await rig.host.emitOutput(20006, 0, [3, PEER_UID, SELF_UID, 1, "private-relation", null, null, null, null, null, true, ""]);
  assert.equal(rig.bridge.status().activeCall, null, "private invite callback remains a no-op while group occupancy is held");

  const active = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(active.status, 200);
  assert.equal(active.json.active, true);
  assert.equal(active.json.requesterUserId, REQUESTER);
  const forged = await request(rig.port, "/v1/group/current", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: { ...scope, roomGeneration: scope.roomGeneration + 1 },
  });
  assert.equal(forged.json.active, false, "cached join payload cannot substitute for exact live scope");
  const staleGeneration = await request(rig.port, "/v1/group/current", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: { ...scope, hostGeneration: HOST_GENERATION + 1 },
  });
  assert.equal(staleGeneration.json.active, false);

  const forwarded = await rig.host.emitOutput(20001, 0, [17, "kernel-signal"]);
  assert.equal(rig.forwards.length, 1);
  assert.deepEqual(rig.forwards[0], { command: 20001, value: [17, "kernel-signal"] });
  assert.ok(forwarded.length > 0, "20001 still reaches the Kernel forwarder during group occupancy");
  assert.deepEqual(rig.genericInvokes.map((entry) => entry.command), [1]);
  const occupied = await request(rig.port, "/v1/call/occupancy", occupancyBody);
  assert.equal(occupied.json.occupied, true);
  assert.equal(occupied.json.kind, "group");
  const admission = await request(rig.port, "/v1/group/status", { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" });
  assert.equal(admission.json.occupied, true);
  assert.equal(admission.json.nativeJoinVerified, false);
  assert.equal(admission.json.room, null);
});

test("control quit allows fresh native absence to settle before the adapter's next read", async (t) => {
  const absence = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 2 ? absence.promise : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  let settled = false;
  const pending = request(rig.port, "/v1/group/quit", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope,
  }).then((value) => { settled = true; return value; });
  await waitFor(() => rig.quitCalls.length === 1);
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.roomQueries.length === 2);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(settled, false, "post receipt must allow the in-flight native absence read to finish");
  absence.resolve(makeRoomResult(rig.roomQueries[1]));
  const response = await pending;
  assert.equal(response.status, 200);
  assert.equal(response.json.operation, "group_quit");
  assert.equal(rig.bridge.status().groupCall.occupied, false);
});

test("control quit settling window cannot survive provider identity loss", async (t) => {
  const absence = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 2 ? absence.promise : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const pending = request(rig.port, "/v1/group/quit", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope,
  });
  await waitFor(() => rig.quitCalls.length === 1);
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.roomQueries.length === 2);
  await rig.provider.emitLoss();
  absence.resolve(makeRoomResult(rig.roomQueries[1]));
  const response = await pending;
  assert.notEqual(response.status, 200);
  assert.equal(rig.bridge.status().groupCall.occupied, true, "late old-context absence cannot release ownership");
});

test("quit receipt and command ACK hold the slot until a fresh complete membership query proves absence", async (t) => {
  const absenceQuery = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 2 ? absenceQuery.promise : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const scope = joined.json.scope;
  const quit = await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(quit.status, 200);
  assert.equal(quit.json.operation, "group_quit");
  assert.equal(quit.json.result.state, "ending");
  assert.equal(rig.bridge.status().groupCall.occupied, true, "Host post receipt is not a release proof");
  assert.equal(rig.quitCalls.length, 1);
  assert.equal(rig.quitCalls[0].groupId, "20002");
  assert.equal(rig.quitCalls[0].startRequestId, rig.startCalls[0].id);

  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.roomQueries.length === 2);
  assert.equal(rig.bridge.status().groupCall.occupied, true, "group command ACK 8 alone cannot clear occupancy");
  const held = await request(rig.port, "/v1/call/occupancy", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(held.json.occupied, true);
  assert.equal(held.json.kind, "group");
  await enterOutput(rig, "20002");
  assert.equal(rig.bridge.status().groupCall.occupied, true, "a repeated entry callback cannot end a group room");

  rig.setMembers([{ uid: PEER_UID, accountType: 1 }, { uid: "23457", accountType: 1 }], 2);
  const queriesBeforeTerminal = rig.roomQueries.length;
  await rig.host.emitOutput(20011, 0, [3, "20002", "opaque-rtc-room-id"]);
  absenceQuery.resolve(makeRoomResult(rig.roomQueries[1]));
  await waitFor(() => rig.bridge.status().groupCall.occupied === false);
  assert.equal(rig.roomQueries.length, queriesBeforeTerminal, "the matching late terminal cannot duplicate the ACK's fresh absence query");
  assert.deepEqual(rig.genericInvokes.map((entry) => entry.command), [1], "group query/start/quit never use generic invoke");
  const current = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(current.json.active, false);
  assert.equal(current.json.scope, null);
  const admission = await request(rig.port, "/v1/group/status", { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" });
  assert.equal(admission.json.nativeJoinVerified, true, "fresh native membership permits an idle admission preflight");
  assert.equal(admission.json.room.groupId, "20002");
  assert.equal(admission.json.room.live, true);
  const idle = await request(rig.port, "/v1/call/occupancy", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(idle.json.occupied, false);
  assert.equal(idle.json.kind, null);
});

test("correlated successful Quit8 plus complete absence releases the slot without a terminal callback", async (t) => {
  const rig = await startRig(t);
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope });
  const before = rig.roomQueries.length;
  await rig.host.emitOutput(8, 0, [0, ""]);
  await rig.host.emitOutput(8, rig.quitCalls[0].id + 100, [0, ""]);
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [1, ""]);
  assert.equal(rig.roomQueries.length, before);
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.bridge.status().groupCall.occupied === false);
  assert.equal(rig.roomQueries.length, before + 1);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
});

test("owned SDK readback releases the Bridge slot without querying a vanished server room", async (t) => {
  const proofs = [];
  const rig = await startRig(t, { localQuitProof: (input) => {
    proofs.push(input);
    return { ...input, ok: true, confirmed: true, source: "native_sdk_room_id_readback" };
  }});
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope });
  assert.equal(proofs.length, 0, "a post receipt does not request native absence proof");
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => !rig.bridge.status().groupCall.occupied);
  assert.equal(proofs.length, 1);
  assert.equal(proofs[0].startRequestId, rig.startCalls[0].id);
  assert.equal(proofs[0].quitRequestId, rig.quitCalls[0].id);
  assert.equal(rig.roomQueries.length, 1, "no membership query is sent after owned local absence");
  assert.equal((await joinThroughControl(rig)).status, 200);
});

test("unconfirmed local readback keeps the complete native membership fallback", async (t) => {
  const rig = await startRig(t, { localQuitProof: (input) => ({ ...input, ok: true, confirmed: false, source: "native_sdk_room_id_readback" }) });
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope });
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => !rig.bridge.status().groupCall.occupied);
  assert.equal(rig.roomQueries.length, 2);
});

test("provider loss during local readback prevents old proof from releasing the Bridge slot", async (t) => {
  const pending = deferred();
  let proofInput;
  const rig = await startRig(t, { localQuitProof: (input) => { proofInput = input; return pending.promise; } });
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope });
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => Boolean(proofInput));
  await rig.provider.emitLoss();
  pending.resolve({ ...proofInput, ok: true, confirmed: true, source: "native_sdk_room_id_readback" });
  await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.roomQueries.length, 1);
});

test("owned Quit retries a complete lagging roster with a new ID and then frees the slot", async (t) => {
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 2
      ? makeRoomResult(input, [{uid: SELF_UID, accountType: 1}]) : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", {accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope});
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.bridge.status().groupCall.occupied === false);
  assert.equal(rig.roomQueries.length, 3);
  assert.equal(new Set(rig.roomQueries.map(q => q.id)).size, 3);
  assert.equal(rig.quitCalls.length, 1);
  assert.equal((await joinThroughControl(rig)).status, 200);
});

test("owned Quit roster retries remain bounded when self never leaves", async (t) => {
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index > 1
      ? makeRoomResult(input, [{uid: SELF_UID, accountType: 1}]) : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", {accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope});
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
  assert.equal(rig.roomQueries.length, 4);
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.quitCalls.length, 1);
});

test("provider loss during the owned Quit roster settling delay prevents another query", async (t) => {
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index > 1
      ? makeRoomResult(input, [{uid: SELF_UID, accountType: 1}]) : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await request(rig.port, "/v1/group/quit", {accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope});
  await rig.host.emitOutput(8, rig.quitCalls[0].id, [0, ""]);
  await waitFor(() => rig.roomQueries.length === 2);
  await rig.provider.emitLoss();
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.equal(rig.roomQueries.length, 2);
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.quitCalls.length, 1);
});

test("group query IDs stay on the Bridge controller sequence across sequential join, quit, and join", async (t) => {
  const rig = await startRig(t);
  const loginId = rig.genericInvokes[0].id;

  const first = await joinThroughControl(rig, "20002");
  const firstScope = first.json.scope;
  const firstStartId = rig.startCalls[0].id;
  const firstQueryId = rig.roomQueries[0].id;
  assert.equal(rig.audioSelections.length, 1, "each group preflight selects configured devices once");
  assert.deepEqual(rig.audioSelections[0], {
    id: rig.audioSelections[0].id,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: AUDIO_DEVICE_TARGET.inputDeviceName,
    outputDeviceName: AUDIO_DEVICE_TARGET.outputDeviceName,
  });
  assert.notEqual(rig.audioSelections[0].id, firstQueryId);
  const quit = await request(rig.port, "/v1/group/quit", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: firstScope,
  });
  assert.equal(quit.status, 200);
  const quitId = rig.quitCalls[0].id;
  rig.setMembers([{ uid: PEER_UID, accountType: 1 }, { uid: "23457", accountType: 1 }], 2);
  await rig.host.emitOutput(20011, 0, [3, "20002", "opaque-first-room"]);
  await waitFor(() => rig.bridge.status().groupCall.occupied === false);
  const absenceQueryId = rig.roomQueries[1].id;
  assert.equal(rig.audioSelections.length, 1, "fresh native absence query does not reselect devices");

  const second = await joinThroughControl(rig, "20003");
  assert.equal(second.status, 200);
  const secondStartId = rig.startCalls[1].id;
  const secondQueryId = rig.roomQueries[2].id;
  assert.equal(rig.audioSelections.length, 2, "the next group preflight selects again exactly once");
  assert.deepEqual(rig.audioSelections[1], {
    id: rig.audioSelections[1].id,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: AUDIO_DEVICE_TARGET.inputDeviceName,
    outputDeviceName: AUDIO_DEVICE_TARGET.outputDeviceName,
  });
  const queryIds = rig.roomQueries.map((item) => item.id);
  const otherControllerIds = [loginId, firstStartId, quitId, secondStartId];
  assert.equal(new Set(queryIds).size, queryIds.length, "no native cmd 13 query ID is reused");
  assert.equal(new Set([...queryIds, ...otherControllerIds]).size, queryIds.length + otherControllerIds.length,
    "Bridge-issued group queries use the shared controller sequence");
  assert.deepEqual(queryIds, [firstQueryId, absenceQueryId, secondQueryId]);
  assert.equal(new Set([...queryIds, ...otherControllerIds, ...rig.audioSelections.map((item) => item.id)]).size,
    queryIds.length + otherControllerIds.length + rig.audioSelections.length,
    "audio selection, room query, login, start, and quit share one unique ID sequence");
  assert.deepEqual(rig.genericInvokes.map((item) => item.command), [1]);
});

test("unexpected-creator cleanup receives a fresh Bridge-allocated request ID", async (t) => {
  const startReceipt = deferred();
  const rig = await startRig(t, { startGroup: () => startReceipt.promise });
  const loginId = rig.genericInvokes[0].id;
  const pendingJoin = request(rig.port, "/v1/group/join", groupJoinBody("20002"));
  await waitFor(() => rig.startCalls.length === 1);
  const startId = rig.startCalls[0].id;
  const queryId = rig.roomQueries[0].id;
  await rig.host.emitOutput(20004, 0, [0, 3, "20002", "", 1, "", 17]);
  const result = await pendingJoin;
  assert.equal(result.status, 409);
  assert.equal(result.json.code, "unexpected_creator");
  assert.equal(rig.quitCalls.length, 1, "unexpected creator triggers only the scoped safe cmd 8 path");
  const safeQuitId = rig.quitCalls[0].id;
  assert.equal(rig.quitCalls[0].startRequestId, startId);
  assert.equal(new Set([loginId, startId, queryId, safeQuitId]).size, 4,
    "query and unexpected-creator cleanup IDs do not reuse controller IDs");
  assert.equal(rig.bridge.status().groupCall.state, "quarantined");
  assert.equal(rig.startCalls.length, 1, "an early creator callback cannot synthesize a second Start4");
  startReceipt.resolve({ posted: true, id: startId, command: 4 });
  for (let index = 0; index < 10; index += 1) await Promise.resolve();
  assert.equal(rig.bridge.status().groupCall.state, "quarantined", "late Start4 receipt cannot undo creator cleanup");
  assert.equal(rig.genericInvokes.some((item) => [4, 8, 13].includes(item.command)), false);
});

test("correlated Host pre-dispatch refusal releases only the reserved group slot", async (t) => {
  const rig = await startRig(t, {
    startGroup: (input) => ({ posted: false, id: input.id, command: 4 }),
  });
  const refused = await request(rig.port, "/v1/group/join", groupJoinBody());
  assert.equal(refused.status, 409);
  assert.equal(refused.json.code, "start_refused");
  assert.equal(rig.bridge.status().groupCall.occupied, false);
  const occupancy = await request(rig.port, "/v1/call/occupancy", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(occupancy.json.occupied, false);
  assert.equal(rig.genericInvokes.some((entry) => [4, 13, 8].includes(entry.command)), false);
});

test("leased occupancy identifies a real private call without exposing its identity or scope", async (t) => {
  const rig = await startRig(t, { audioDeviceTarget: null });
  await rig.provider.emitKernel("OnInviteActionToAVSDK", [{
    relation_id: "private-relation", invite_type: 3, from_uid: PEER_UID,
  }]);
  assert.ok(rig.bridge.status().activeCall);
  const result = await request(rig.port, "/v1/call/occupancy", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(result.status, 200);
  assert.deepEqual(result.json, {
    version: 1,
    accountId: ACCOUNT_ID,
    serviceInstance: rig.service.status().serviceInstance,
    hostGeneration: HOST_GENERATION,
    connectionGeneration: CONNECTION_GENERATION,
    occupied: true,
    kind: "private",
  });
  assert.equal(JSON.stringify(result.json).includes(PEER_UID), false);
  assert.equal(JSON.stringify(result.json).includes("private-relation"), false);
  const extra = await request(rig.port, "/v1/call/occupancy", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: { callId: "untrusted" },
  });
  assert.equal(extra.status, 400);
});

test("partial lists, unknown member IDs, and the trusted self UIN quarantine membership release", async (t) => {
  const cases = [
    { name: "partial list", users: [{ uid: PEER_UID, accountType: 1 }], userNum: 2 },
    { name: "self UIN still present", users: [{ uid: PEER_UID, accountType: 1 }, { uid: ACCOUNT_ID, accountType: 1 }], userNum: 2 },
    { name: "unknown UID format", users: [{ uid: PEER_UID, accountType: 1 }, { uid: "opaque-member", accountType: 1 }], userNum: 2 },
  ];
  for (const item of cases) {
    await t.test(item.name, async (t2) => {
      const rig = await startRig(t2);
      const joined = await joinThroughControl(rig);
      await enterOutput(rig);
      const scope = joined.json.scope;
      await request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
      rig.setMembers(item.users, item.userNum);
      await rig.host.emitOutput(20011, 0, [3, "20002", "opaque-rtc-room-id"]);
      await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
      assert.equal(rig.bridge.status().groupCall.occupied, true, "uncertain absence evidence never frees the global slot");
      assert.equal(rig.bridge.getCurrentGroupScope(), null);
    });
  }
});

test("provider loss quarantines a joined group and matched remote terminal suspends current/audio proof", async (t) => {
  let observerCalls = 0;
  const rig = await startRig(t, {
    audioRouteObserver: async (input) => {
      observerCalls += 1;
      return routeProof(input.captureRenderName, input.playbackRenderName);
    },
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const scope = joined.json.scope;
  rig.setMembers([{ uid: PEER_UID, accountType: 1 }, { uid: ACCOUNT_ID, accountType: 1 }], 2);
  const route = await groupRoute(rig, scope);
  assert.equal(route.status, 200);
  assert.equal(route.json.verified, true);
  assert.deepEqual(route.json.scope, scope);
  assert.deepEqual(Object.keys(route.json).sort(), [
    "version", "accountId", "scope", "verified", "source", "hostPid", "observedAtMs", "hostInput", "hostOutput",
  ].sort());

  const injected = await groupRoute(rig, { ...scope, attackerField: true });
  assert.equal(injected.status, 400);
  const stale = await groupRoute(rig, { ...scope, groupId: "20003" });
  assert.equal(stale.status, 409);
  assert.equal(observerCalls, 1, "bad scopes are rejected before the Windows session observer runs");

  await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
  assert.equal(rig.bridge.getCurrentGroupScope(), null, "a matched remote terminal revokes current-scope and audio proof");
  await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
  assert.equal(rig.bridge.status().groupCall.occupied, true, "the fresh query found the trusted self UIN still present");
  const current = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(current.json.active, false);
  const revokedRoute = await groupRoute(rig, scope);
  assert.equal(revokedRoute.status, 409);
  assert.equal(observerCalls, 1, "revoked terminal scope cannot reach the observer");
});

test("group route timeout after a matched terminal cannot return a positive proof", async (t) => {
  const observerStarted = deferred();
  const rig = await startRig(t, {
    audioRouteObserver: async ({ signal, captureRenderName, playbackRenderName }) => {
      observerStarted.resolve();
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
      return routeProof(captureRenderName, playbackRenderName);
    },
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const scope = joined.json.scope;
  rig.setMembers([{ uid: PEER_UID, accountType: 1 }, { uid: ACCOUNT_ID, accountType: 1 }], 2);
  const pendingRoute = groupRoute(rig, scope);
  await observerStarted.promise;
  await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
  const result = await pendingRoute;
  assert.equal(result.status, 409, "scope revalidation defeats the late observer result");
  assert.notEqual(result.json.verified, true);
  assert.equal(rig.bridge.status().groupCall.occupied, true);
});

test("admin native zero-member callback clears the owned room without requiring SDK20011 and scoped leave retries", async (t) => {
  const clearCalls = [];
  let absent = false;
  const rig = await startRig(t, {
    localClearProof: async (input) => {
      clearCalls.push(input);
      return { ok: true, ...input, confirmed: absent, source: "native_group_clear_absence" };
    },
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const event = { group_code: "20002", onlineCount: 0, memberUins: [], businessType: 3, isNativeTimeout: false };
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [{ ...event, group_code: "99999" }]);
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [{ ...event, onlineCount: 1 }]);
  assert.equal(clearCalls.length, 0);
  assert.equal(rig.bridge.status().groupCall.state, "joined");
  await rig.provider.emitKernel("onGroupAudioMemNumChange", [event]);
  await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
  assert.equal(clearCalls.length, 1);
  assert.equal(rig.bridge.status().groupCall.occupied, true, "member-count callback alone never frees the slot");
  absent = true;
  const left = await request(rig.port, "/v1/group/quit", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: joined.json.scope,
  });
  assert.equal(left.status, 200);
  assert.equal(left.json.result.ended, true);
  assert.equal(rig.bridge.status().groupCall.occupied, false);
  assert.equal(rig.quitCalls.length, 0);
  assert.equal((await joinThroughControl(rig, "20003")).status, 200);
});

test("admin remote end uses entered-room SDK absence without waiting for a missing member callback", async (t) => {
  const rig = await startRig(t, {
    localRemoteProof: async (input) => ({ ok: true, ...input, confirmed: true,
      source: "native_entered_group_room_absence" }),
  });
  await joinThroughControl(rig);
  await enterOutput(rig);
  const queries = rig.roomQueries.length;
  await rig.host.emitOutput(20011, 0, [3, "20002", "admin-ended-room"]);
  await waitFor(() => !rig.bridge.status().groupCall.occupied);
  assert.equal(rig.bridge.status().groupCall.state, "idle");
  assert.equal(rig.roomQueries.length, queries);
  assert.equal(rig.quitCalls.length, 0);
  assert.equal((await joinThroughControl(rig, "20003")).status, 200);
});

test("remote terminal holds occupancy while scoped leave waits for proof, then admits the next call", async (t) => {
  const absenceQuery = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 3 ? absenceQuery.promise : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const scope = joined.json.scope;
  const diagnostic = await rig.bridge.getGroupRoomStatus("20002");
  assert.equal(diagnostic.ok, true);
  assert.equal(rig.bridge.status().groupCall.state, "joined", "a room query by itself is not a terminal event");
  await rig.host.emitOutput(8, 999, [0, ""]);
  assert.equal(rig.bridge.status().groupCall.state, "joined", "an unrelated Quit8 reply cannot end the group");

  const queriesBeforeTerminal = rig.roomQueries.length;
  await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
  await waitFor(() => rig.roomQueries.length === queriesBeforeTerminal + 1);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
  assert.equal(rig.bridge.status().groupCall.state, "ending_remote");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.quitCalls.length, 0, "a remote terminal never posts a fabricated Quit8");
  const current = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(current.json.active, false);

  // The accepted terminal owns one proof ticket. A duplicate for that same
  // group, a foreign relation, private-scene terminal, or callback with a
  // nonzero request id must not stale it or start another room query.
  await rig.host.emitOutput(20011, 0, [3, "20002", "duplicate-remote-terminal"]);
  await rig.host.emitOutput(20011, 0, [3, "99999", "foreign-remote-terminal"]);
  await rig.host.emitOutput(20011, 0, [1, "20002", "private-terminal"]);
  await rig.host.emitOutput(20011, 77, [3, "20002", "request-scoped-terminal"]);
  assert.equal(rig.bridge.status().groupCall.state, "ending_remote");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.roomQueries.length, queriesBeforeTerminal + 1, "duplicate and unrelated callbacks do not start another proof query");

  const quitPending = request(rig.port, "/v1/group/quit", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(rig.quitCalls.length, 0);

  absenceQuery.resolve(makeRoomResult(rig.roomQueries.at(-1)));
  const quit = await quitPending;
  assert.equal(quit.status, 200);
  assert.equal(quit.json.result.ended, true);
  const stale = await request(rig.port, "/v1/group/quit", {
    accountId: ACCOUNT_ID, clientInstance: CLIENT, scope: { ...scope, roomGeneration: scope.roomGeneration + 1 },
  });
  assert.equal(stale.status, 409);
  await waitFor(() => rig.bridge.status().groupCall.occupied === false);
  assert.equal(rig.bridge.status().groupCall.state, "idle");
  assert.equal(rig.roomQueries.length, queriesBeforeTerminal + 1, "the terminal starts exactly one fresh proof query");
  const second = await joinThroughControl(rig, "20003");
  assert.equal(second.status, 200, "a complete absence proof releases the shared group slot for the next call");
  await enterOutput(rig, "20003");
  assert.equal(rig.bridge.getCurrentGroupScope().groupId, "20003");
});

test("remote terminal membership proof fails closed for partial, self-present, and unrecognized lists", async (t) => {
  const cases = [
    { name: "partial", users: [{ uid: PEER_UID, accountType: 1 }], userNum: 2 },
    { name: "self UID present", users: [{ uid: PEER_UID, accountType: 1 }, { uid: SELF_UID, accountType: 1 }], userNum: 2 },
    { name: "self UIN present", users: [{ uid: PEER_UID, accountType: 1 }, { uid: ACCOUNT_ID, accountType: 1 }], userNum: 2 },
    { name: "unknown member identity", users: [{ uid: PEER_UID, accountType: 1 }, { uid: "opaque-member", accountType: 1 }], userNum: 2 },
  ];
  for (const item of cases) {
    await t.test(item.name, async (t2) => {
      const rig = await startRig(t2);
      const joined = await joinThroughControl(rig);
      await enterOutput(rig);
      rig.setMembers(item.users, item.userNum);
      await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
      await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
      assert.equal(rig.bridge.status().groupCall.occupied, true);
      assert.equal(rig.bridge.getCurrentGroupScope(), null);
      assert.equal(rig.roomQueries.length, item.name.includes("present") ? 4 : 2,
        "only a complete self-present roster gets bounded freshness retries; invalid evidence fails immediately");
      assert.equal(rig.quitCalls.length, 0);
      assert.equal(joined.json.joined, true);
    });
  }
});

test("remote absence query error or malformed receipt keeps occupancy quarantined", async (t) => {
  const cases = [
    {
      name: "query error",
      queryRoomInfo: async (input, index) => {
        if (index === 2) throw new Error("query unavailable");
        return makeRoomResult(input);
      },
    },
    {
      name: "malformed receipt",
      queryRoomInfo: async (input, index) => index === 2 ? { ok: true } : makeRoomResult(input),
    },
  ];
  for (const item of cases) {
    await t.test(item.name, async (t2) => {
      const rig = await startRig(t2, { queryRoomInfo: item.queryRoomInfo });
      await joinThroughControl(rig);
      await enterOutput(rig);
      await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
      await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
      assert.equal(rig.bridge.status().groupCall.occupied, true);
      assert.equal(rig.bridge.getCurrentGroupScope(), null);
      assert.equal(rig.roomQueries.length, 2, "a failed proof attempt is not retried against a quarantined query");
    });
  }
});

test("late absence proof after generation loss cannot release remote-end occupancy", async (t) => {
  const absenceQuery = deferred();
  const rig = await startRig(t, {
    queryRoomInfo: (input, index) => index === 2 ? absenceQuery.promise : makeRoomResult(input),
  });
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  await rig.host.emitOutput(20011, 0, [3, "20002", "remote-terminal-room"]);
  await waitFor(() => rig.roomQueries.length === 2);
  await rig.host.emitAvailability(false);
  absenceQuery.resolve(makeRoomResult(rig.roomQueries[1]));
  await waitFor(() => rig.bridge.status().groupCall.state === "quarantined");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
  assert.equal(joined.json.joined, true);
});

test("provider loss stops the group driver without clearing uncertain occupancy", async (t) => {
  const rig = await startRig(t);
  const joined = await joinThroughControl(rig);
  await enterOutput(rig);
  const scope = joined.json.scope;
  await rig.provider.emitLoss();
  assert.equal(rig.bridge.status().groupCall.state, "quarantined");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
  const current = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(current.json.active, false);
  const status = await request(rig.port, "/v1/group/status", { accountId: ACCOUNT_ID, clientInstance: CLIENT, groupId: "20002" });
  assert.equal(status.json.occupied, true, "quarantined group stays globally busy");
});

test("Host loss stops the group driver without clearing uncertain occupancy", async (t) => {
  const rig = await startRig(t);
  const joined = await joinThroughControl(rig);
  const scope = joined.json.scope;
  await rig.host.emitAvailability(false);
  assert.equal(rig.bridge.status().groupCall.state, "quarantined");
  assert.equal(rig.bridge.status().groupCall.occupied, true);
  assert.equal(rig.bridge.getCurrentGroupScope(), null);
  const current = await request(rig.port, "/v1/group/current", { accountId: ACCOUNT_ID, clientInstance: CLIENT, scope });
  assert.equal(current.json.active, false);
  const occupancy = await request(rig.port, "/v1/call/occupancy", { accountId: ACCOUNT_ID, clientInstance: CLIENT });
  assert.equal(occupancy.json.occupied, true);
  assert.equal(occupancy.json.kind, "group");
});
