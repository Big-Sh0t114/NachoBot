"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const net = require("node:net");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PassThrough, Writable } = require("node:stream");
const test = require("node:test");
const { createControlService, MAX_BODY_BYTES } = require("../bridge/control-service.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");
const { parseArgs, startCli } = require("../bridge/cli.cjs");
const { PRIVATE_INVITE_PROFILE, PINNED_WRAPPER_SHA256 } = require("../shared/private-invite-scope.cjs");

const TOKEN = Buffer.alloc(32, 0x27).toString("base64url");
const CONTROL_TOKEN = Buffer.alloc(32, 0x38).toString("base64url");
const ACCOUNT_ID = "10001";

async function officialReportFixture() {
  const { createOfficialAVObserver } = require("../agent/official-av-observer.cjs");
  const observer = createOfficialAVObserver({ processObject: { type: "renderer" } });
  const report = await observer.start();
  await observer.stop();
  return report;
}

test("official AV diagnostics require authentication/account/exact input without claiming or renewing a lease", async (t) => {
  const report = await officialReportFixture();
  const calls = [];
  const api = Object.fromEntries(["start", "drain", "stop"].map((key) => [key, async () => { calls.push(key); return report; }]));
  const bridge = makeBridge();
  const {service,port}=await createService(bridge,{officialAVObserver:api,leaseMs:1000});
  t.after(()=>service.stop());
  const path="/v1/diagnostics/official-av/";
  const body={accountId:ACCOUNT_ID};
  assert.equal((await request(port,path+"start",{token:null,body})).status,401);
  assert.equal((await request(port,path+"start",{body:{accountId:"99999"}})).status,409);
  assert.equal((await request(port,path+"start",{body:{...body,nativeConditional:true}})).status,400);
  assert.equal((await request(port,path+"drain",{body})).status,409);
  assert.deepEqual(calls,[]);
  const start=await request(port,path+"start",{body});
  assert.equal(start.status,200);
  assert.equal(start.json.diagnosticOnly,true);
  assert.equal(start.json.report.proofLevel,"diagnostic_only");
  assert.equal(service.status().leaseActive,false);
  assert.equal((await request(port,path+"start",{body})).status,409);
  await request(port,"/v1/adapter/poll",{body:pollBody("owner")});
  await new Promise(resolve=>setTimeout(resolve,600));
  assert.equal((await request(port,path+"drain",{body})).status,200);
  await new Promise(resolve=>setTimeout(resolve,500));
  assert.equal((await request(port,"/v1/adapter/poll",{body:pollBody("next-owner")})).status,200);
  assert.deepEqual(bridge._invoked,{accept:[],hangup:[],reject:[],stop:0});
  assert.equal((await request(port,path+"stop",{body})).status,200);
});

test("official AV diagnostics reject unavailable, malformed and changed-generation reports and clean up", async (t) => {
  const report=await officialReportFixture();
  for (const mode of ["unavailable","malformed","generation"]) {
    const bridge=makeBridge();
    let stops=0;
    const api={start:async()=>{
      if(mode==="generation")bridge.setSnapshot({status:{hostGeneration:52}});
      return mode==="malformed"?{...report,secret:"private-payload"}:report;
    },drain:async()=>report,stop:async()=>{stops+=1;return report;}};
    const {service,port}=await createService(bridge,{officialAVObserver:mode==="unavailable"?null:api});
    t.after(()=>service.stop());
    const response=await request(port,"/v1/diagnostics/official-av/start",{body:{accountId:ACCOUNT_ID}});
    assert.equal(response.status,mode==="malformed"?502:503);
    assert.equal(response.text.includes("private-payload"),false);
    assert.equal(service.status().leaseActive,false);
    assert.equal(stops,mode==="unavailable"?0:1);
  }
});

test("stopping during an official AV installation cleans the late observer and returns no report", async () => {
  const report=await officialReportFixture();
  let finish,invoked=false,stops=0;
  const gate=new Promise(resolve=>{finish=resolve;});
  const api={start:async()=>{invoked=true;await gate;return report;},drain:async()=>report,
    stop:async()=>{stops+=1;return report;}};
  const {service,port}=await createService(makeBridge(),{officialAVObserver:api});
  const pending=request(port,"/v1/diagnostics/official-av/start",{body:{accountId:ACCOUNT_ID}});
  await waitFor(()=>invoked);
  const stopped=service.stop();
  finish();
  const response=await pending;
  assert.equal(response.status,503);
  assert.equal(response.json.code,"service_stopped");
  assert.equal(Object.hasOwn(response.json,"report"),false);
  assert.equal((await stopped).stopped,true);
  assert.ok(stops>=1);
});

test("private diagnostics requires authentication and account, projects only fixed fields and does not claim a lease", async (t) => {
  const bridge = makeBridge();
  bridge.setSnapshot({ status: { activeCall: { ...callSnapshot({ phase: "failed", acceptState: "acknowledged", uncertain: true }),
    token: "synthetic_secret" }, lastCall: { phase: "failed", terminalReason: "native_accept_failed", peerUid: "private_identity" },
    lastFailureCode: "native_accept_failed", counts: { hostOutputs: 12, secret: "synthetic_secret" } } });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const body = { accountId: ACCOUNT_ID };
  assert.equal((await request(port, "/v1/call/diagnostics", { token: null, body })).status, 401);
  assert.equal((await request(port, "/v1/call/diagnostics", { body: { accountId: "20002" } })).status, 409);
  assert.equal((await request(port, "/v1/call/diagnostics", { body: { ...body, clientInstance: "intruder" } })).status, 400);
  const before = service.status();
  const response = await request(port, "/v1/call/diagnostics", { body });
  assert.equal(response.status, 200);
  assert.equal(response.json.diagnosticOnly, true);
  assert.equal(response.json.activeCall.phase, "failed");
  assert.equal(response.json.activeCall.acceptState, "acknowledged");
  assert.equal(response.json.lastCall.terminalReason, "native_accept_failed");
  assert.equal(response.json.lastFailureCode, "native_accept_failed");
  assert.equal(response.json.counts.hostOutputs, 12);
  for (const privateValue of ["synthetic_secret", "private_identity", callSnapshot().callId]) assert.equal(response.text.includes(privateValue), false);
  const after = service.status();
  assert.equal(after.cursor, before.cursor);
  assert.equal(after.leaseActive, false);
  assert.deepEqual(bridge._invoked, { accept: [], hangup: [], reject: [], stop: 0 });
});

test("private diagnostics preserves an existing lease and redacts unknown labels", async (t) => {
  const bridge = makeBridge();
  bridge.setSnapshot({ status: { activeCall: { phase: "secret_phase", acceptState: "secret_state" }, lastFailureCode: "secret_code" } });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  await request(port, "/v1/adapter/poll", { body: pollBody("adapter-owner") });
  const response = await request(port, "/v1/call/diagnostics", { body: { accountId: ACCOUNT_ID } });
  assert.equal(response.status, 200);
  assert.equal(response.json.activeCall.phase, null);
  assert.equal(response.json.lastFailureCode, "unrecognized");
  assert.equal(response.text.includes("secret_"), false);
  assert.equal(service.status().leaseActive, true);
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-other") })).status, 409);
});

test("private diagnostics rejects a changed generation without projecting a mixed snapshot", async (t) => {
  const bridge = makeBridge();
  let reads=0;
  bridge.status=()=>({hostGeneration:51,connectionGeneration:++reads===1?61:62});
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const response=await request(port,"/v1/call/diagnostics",{body:{accountId:ACCOUNT_ID}});
  assert.equal(response.status,503);
  assert.equal(response.json.code,"bridge_status_unavailable");
});

test("group diagnostics requires token and lease and preserves the admission contract", async (t) => {
  const bridge = makeBridge();
  const diagnostic = { groupId: "12345", membership: null,
    invitation: { roomId: "actual-room", totalInvitedMembers: 1 },
    roomEndObservation: null, diagnosticOnly: true, nativeJoinVerified: false };
  let reads = 0;
  bridge.getGroupDiagnostics = (groupId) => { reads += 1; assert.equal(groupId, "12345"); return diagnostic; };
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const body = { accountId: ACCOUNT_ID, clientInstance: "adapter-diag", groupId: "12345" };
  assert.equal((await request(port, "/v1/group/diagnostics", { token: null, body })).status, 401);
  assert.equal((await request(port, "/v1/group/diagnostics", { body })).status, 409);
  assert.equal(reads, 0);
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-diag") })).status, 200);
  const result = await request(port, "/v1/group/diagnostics", { body });
  assert.equal(result.status, 200);
  assert.deepEqual(result.json.diagnostic, diagnostic);
  assert.equal(result.json.diagnosticOnly, true);
  assert.equal(result.json.nativeJoinVerified, false);
  assert.equal(result.json.room, null);
  const admission = await request(port, "/v1/group/status", { body });
  assert.deepEqual(Object.keys(admission.json).sort(), ["version", "accountId", "serviceInstance", "hostGeneration",
    "connectionGeneration", "groupId", "occupied", "nativeJoinVerified", "room"].sort());
  assert.equal((await request(port, "/v1/group/join", { body: { ...body, requesterUserId: "23456", triggerKind: "command" } })).status, 501);
  assert.deepEqual(bridge._invoked.accept, []);
});

test("group diagnostics discards a projection if process generations change during the read", async (t) => {
  const bridge = makeBridge();
  bridge.getGroupDiagnostics = () => { bridge.setSnapshot({ status: { connectionGeneration: 62 } }); return null; };
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  await request(port, "/v1/adapter/poll", { body: pollBody("adapter-diag") });
  const result = await request(port, "/v1/group/diagnostics", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-diag", groupId: "12345" },
  });
  assert.equal(result.status, 503);
  assert.equal(result.json.code, "bridge_status_unavailable");
});

test("group diagnostics reads cached observations across query quarantine without native operations and preserves leased ownership", async (t) => {
  const bridge = makeBridge();
  let queries = 0;
  bridge.getGroupRoomStatus = () => { queries += 1; throw Object.assign(Error("quarantined"), { code: "group_room_query_quarantined" }); };
  bridge.getGroupDiagnostics = () => ({ groupId: "12345", membership: { onlineCount: 0, memberCount: 0 },
    invitation: null, roomEndObservation: null, diagnosticOnly: true, nativeJoinVerified: false });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  await request(port, "/v1/adapter/poll", { body: pollBody("adapter-diag") });
  const before = service.status();
  const body = { accountId: ACCOUNT_ID, clientInstance: "adapter-diag", groupId: "12345" };
  const result = await request(port, "/v1/group/diagnostics", { body });
  assert.equal(result.status, 200);
  assert.equal(result.json.diagnostic.membership.onlineCount, 0);
  assert.equal(result.json.nativeJoinVerified, false);
  assert.equal(result.json.room, null);
  assert.equal(queries, 0);
  assert.equal(service.status().cursor, before.cursor);
  assert.equal(service.status().leaseActive, true);
  assert.deepEqual(bridge._invoked, { accept: [], hangup: [], reject: [], stop: 0 });
  const admission = await request(port, "/v1/group/status", { body });
  assert.equal(admission.status, 409);
  assert.equal(admission.json.code, "group_room_query_quarantined");
  assert.equal(queries, 1, "admission retains its original native query fence");
});

function callSnapshot(overrides = {}) {
  return {
    callId: "7156ab16-c2c3-4f7f-987f-620145dce8df",
    generation: 7,
    peerUid: "private-peer-uid",
    phase: "invite_ready",
    uncertain: false,
    inviteCallbackReady: true,
    inviteCorrelation: "kernel_from_uid_matched",
    acceptState: "not_requested",
    ...overrides,
  };
}

function makeBridge({ call = null, accept, hangup, reject } = {}) {
  let snapshot = {
    status: {
      hostGeneration: 51,
      connectionGeneration: 61,
      activeCall: call ? { callId: call.callId, generation: call.generation, phase: call.phase } : null,
    },
    call: call ? { ...call } : null,
  };
  const handlers = new Set();
  const controlHandlers = new Set();
  const invoked = { accept: [], hangup: [], reject: [], stop: 0 };
  const bridge = {
    status: () => ({ ...snapshot.status }),
    getIntegrationSnapshot: () => ({ status: { ...snapshot.status }, call: snapshot.call ? { ...snapshot.call } : null }),
    subscribeStatus(handler) { handlers.add(handler); return () => handlers.delete(handler); },
    subscribeControlEvents(handler) { controlHandlers.add(handler); return () => controlHandlers.delete(handler); },
    setSnapshot(next, kind = "status") {
      snapshot = {
        status: { ...snapshot.status, ...(next.status || {}) },
        call: Object.prototype.hasOwnProperty.call(next, "call") ? (next.call ? { ...next.call } : null) : snapshot.call,
      };
      if (Object.prototype.hasOwnProperty.call(next, "call") && !Object.prototype.hasOwnProperty.call(next.status || {}, "activeCall")) {
        snapshot.status.activeCall = snapshot.call
          ? { callId: snapshot.call.callId, generation: snapshot.call.generation, phase: snapshot.call.phase }
          : null;
      }
      for (const handler of handlers) handler({ kind, status: bridge.status() });
    },
    emitControlEvent(event) {
      for (const handler of controlHandlers) handler(event);
    },
    async accept(callId, options) {
      invoked.accept.push(callId);
      if (accept) return accept(callId, options);
      return { posted: true, id: 88, callId, callGeneration: snapshot.call?.generation, extra: "must-not-echo" };
    },
    async hangup(callId) {
      invoked.hangup.push(callId);
      if (hangup) return hangup(callId);
      const error = new Error("unsupported"); error.code = "unsupported_unverified_command:hangup"; throw error;
    },
    async reject(callId) {
      invoked.reject.push(callId);
      if (reject) return reject(callId);
      const error = new Error("unsupported"); error.code = "unsupported_unverified_command:reject"; throw error;
    },
    async stop() { invoked.stop += 1; return { stopped: true }; },
    _handlers: handlers,
    _controlHandlers: controlHandlers,
    _invoked: invoked,
  };
  return bridge;
}

async function createService(bridge, options = {}) {
  const service = createControlService({
    bridge,
    token: CONTROL_TOKEN,
    accountId: ACCOUNT_ID,
    port: 0,
    ...options,
  });
  const status = await service.start();
  return { service, port: status.port };
}

function request(port, pathname, { method = "POST", token = CONTROL_TOKEN, body, rawBody, contentType = "application/json" } = {}) {
  return new Promise((resolve, reject) => {
    const bytes = rawBody !== undefined ? Buffer.from(rawBody) : body !== undefined ? Buffer.from(JSON.stringify(body)) : Buffer.alloc(0);
    const headers = {};
    if (token !== null) headers.Authorization = `Bearer ${token}`;
    if (contentType) headers["Content-Type"] = contentType;
    if (method !== "GET") headers["Content-Length"] = bytes.byteLength;
    const req = http.request({ hostname: "127.0.0.1", port, path: pathname, method, headers }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let json = null;
        try { json = JSON.parse(text); } catch { /* assert status and body below */ }
        resolve({ status: response.statusCode, json, text });
      });
    });
    req.once("error", reject);
    req.end(bytes);
  });
}

function pollBody(clientInstance = "adapter-A", after = 0) {
  return { accountId: ACCOUNT_ID, clientInstance, after };
}

test("adapter suspension revokes leases and blocks HTTP controls until trusted resume", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const initial = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(initial.status, 200);
  assert.deepEqual(await service.setAdapterAvailable(false), { adapterAvailable: false });
  assert.equal(service.status().leaseActive, false);
  for (const client of ["adapter-A", "adapter-B"]) {
    const blocked = await request(port, "/v1/adapter/poll", { body: pollBody(client) });
    assert.equal(blocked.status, 409);
    assert.equal(blocked.json.code, "adapter_unavailable");
    const occupied = await request(port, "/v1/call/occupancy", {
      body: { accountId: ACCOUNT_ID, clientInstance: client },
    });
    assert.equal(occupied.json.code, "adapter_unavailable");
  }
  assert.equal((await request(port, "/v1/adapter/resume", { body: pollBody() })).status, 404);
  assert.deepEqual(await service.setAdapterAvailable(true), { adapterAvailable: true });
  const fresh = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") });
  assert.equal(fresh.status, 200);
  assert.equal(fresh.json.serviceInstance, initial.json.serviceInstance);
  assert.equal(fresh.json.hostGeneration, initial.json.hostGeneration);
  assert.equal(fresh.json.connectionGeneration, initial.json.connectionGeneration);
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") })).json.code, "adapter_owned");
  assert.equal(bridge._invoked.stop, 0);
});

test("pause ends only the existing native group scope and never clears occupancy from a quit receipt", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const scope = { kind: "qqcall_group", serviceInstance: service.status().serviceInstance,
    hostGeneration: 51, connectionGeneration: 61, groupId: "20002", roomId: "20002", roomGeneration: 3 };
  bridge.getCurrentGroupScope = () => scope;
  bridge.setSnapshot({ status: { groupCall: { occupied: true } } });
  const quits = [];
  bridge.quitGroup = async (value) => { quits.push(value); return { ok: true, scope: value }; };
  await request(port, "/v1/adapter/poll", { body: pollBody() });
  await service.setAdapterAvailable(false);
  await service.setAdapterAvailable(false);
  assert.deepEqual(quits, [scope]);
  await service.setAdapterAvailable(true);
  await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") });
  const occupied = await request(port, "/v1/call/occupancy", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-B" },
  });
  assert.equal(occupied.json.occupied, true);
  assert.equal(bridge._invoked.stop, 0);
});

test("pause requests scoped private hangup or reject without detaching the native listener", async (t) => {
  for (const phase of ["connected", "invite_ready"]) {
    const call = callSnapshot({ phase });
    const bridge = makeBridge({ call,
      hangup: async () => ({ posted: true }), reject: async () => ({ posted: true }) });
    const { service } = await createService(bridge);
    t.after(() => service.stop());
    await service.setAdapterAvailable(false);
    assert.deepEqual(bridge._invoked[phase === "connected" ? "hangup" : "reject"], [call.callId]);
    assert.equal(bridge._invoked.stop, 0);
  }
});

test("pause fences an awaited accept and queued commands before any late native post", async (t) => {
  let release, started;
  const gate = new Promise((resolve) => { release = resolve; });
  const began = new Promise((resolve) => { started = resolve; });
  let posted = false;
  const bridge = makeBridge({ call: callSnapshot(), accept: async (callId, options) => {
    started(); await gate; options.assertCurrent(); posted = true;
    return { posted: true, id: 88, callId, callGeneration: 7 };
  } });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  const body = { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope: scopeFrom(polled.json) };
  const first = request(port, "/v1/call/command", { body });
  await began;
  const queued = request(port, "/v1/call/command", { body });
  await waitFor(() => service.status().queuedCommands === 2);
  const paused = service.setAdapterAvailable(false);
  assert.equal(service.status().leaseActive, false);
  release();
  assert.equal((await first).json.code, "adapter_unavailable");
  assert.equal((await queued).json.code, "adapter_unavailable");
  assert.equal((await paused).adapterAvailable, false);
  assert.equal(posted, false);
  assert.equal(bridge._invoked.accept.length, 1);
});

function privateBusyEvidence(callerUid, identityCorrelation, relationId, callbackSequence) {
  return {
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    callbackSequence,
    listenerRegistrationSequence: 2,
    inviteType: 1,
    relationId,
    fromUid: identityCorrelation === "kernel_relation_uid_matched" ? "" : callerUid,
    targetPid: 4455,
    kernelCreationTime100ns: "134000000000000000",
    connectionGeneration: 61,
    listenerMutationEpoch: 4,
    ipcSequence: callbackSequence + 10,
    inviteEventId: `123e4567-e89b-42d3-a456-42661417400${callbackSequence}`,
  };
}

function groupStatusBody(clientInstance = "adapter-A", groupId = "20002") {
  return { accountId: ACCOUNT_ID, clientInstance, groupId };
}

test("group status preserves bounded native query failures without claiming room absence", async (t) => {
  let failureCode = "group_room_query_timeout";
  const bridge = makeBridge();
  bridge.getGroupRoomStatus = async () => {
    const error = new Error("private query details must not escape");
    error.code = failureCode;
    throw error;
  };
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  await request(port, "/v1/adapter/poll", { body: pollBody() });
  for (const [code, expectedStatus] of [
    ["group_room_query_timeout", 504], ["group_room_query_quarantined", 409],
    ["group_room_query_busy", 409], ["group_room_query_post_failed", 502],
    ["group_room_metadata_invalid", 502], ["private_untrusted_error", 502],
  ]) {
    failureCode = code;
    const response = await request(port, "/v1/group/status", { body: groupStatusBody() });
    assert.equal(response.status, expectedStatus);
    assert.deepEqual(response.json, { ok: false, code: code === "private_untrusted_error" ? "command_failed" : code });
    assert.equal(response.text.includes("private"), false);
    assert.equal(Object.hasOwn(response.json, "room"), false);
  }
  assert.deepEqual(bridge._invoked.accept, []);
});

function groupJoinBody(clientInstance = "adapter-A", groupId = "20002") {
  return {
    accountId: ACCOUNT_ID,
    clientInstance,
    groupId,
    requesterUserId: "10003",
    triggerKind: "command",
  };
}

function scopeFrom(poll, call = poll.currentCall) {
  return {
    serviceInstance: poll.serviceInstance,
    hostGeneration: poll.hostGeneration,
    connectionGeneration: poll.connectionGeneration,
    callId: call.callId,
    callGeneration: call.generation,
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
    if (Date.now() >= deadline) throw new Error("control_service_test_timeout");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

test("control routes authenticate every request and bound exact JSON input", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());

  const unauthorized = await request(port, "/v1/adapter/poll", { token: null, body: pollBody() });
  assert.equal(unauthorized.status, 401);
  assert.deepEqual(unauthorized.json, { ok: false, code: "unauthorized" });
  const wrongToken = await request(port, "/v1/adapter/release", { token: "x".repeat(CONTROL_TOKEN.length), body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A" } });
  assert.equal(wrongToken.status, 401);

  const oversized = await request(port, "/v1/adapter/poll", { rawBody: `${"x".repeat(MAX_BODY_BYTES + 1)}private-marker` });
  assert.equal(oversized.status, 413);
  assert.deepEqual(oversized.json, { ok: false, code: "body_too_large" });
  assert.equal(oversized.text.includes("private-marker"), false);

  const extraField = await request(port, "/v1/adapter/poll", {
    body: { ...pollBody(), rawargs: "must-not-echo" },
  });
  assert.equal(extraField.status, 400);
  assert.deepEqual(extraField.json, { ok: false, code: "invalid_poll_shape" });
  assert.equal(extraField.text.includes("must-not-echo"), false);
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody(), contentType: "text/plain" })).json.code, "content_type_required");
  assert.equal((await request(port, "/v1/adapter/poll?debug=1", { body: pollBody() })).json.code, "invalid_path");
  const wrongAccount = await request(port, "/v1/adapter/poll", {
    body: { ...pollBody(), accountId: "10002" },
  });
  assert.equal(wrongAccount.status, 409);
  assert.deepEqual(wrongAccount.json, { ok: false, code: "account_mismatch" });
});

test("account IDs and service limits validate without starting a listener", () => {
  const bridge = makeBridge();
  for (const accountId of ["", "0", "010001", "１２３", "1".repeat(21)]) {
    assert.throws(() => createControlService({ bridge, token: CONTROL_TOKEN, accountId }), { code: "invalid_account_id" });
  }
  assert.throws(() => createControlService({ bridge, token: "short", accountId: ACCOUNT_ID }), { code: "invalid_control_token" });
});

test("one client lease owns polling and commands until release or activity expiry", async (t) => {
  const bridge = makeBridge({ call: callSnapshot() });
  const { service, port } = await createService(bridge, { leaseMs: 1000 });
  t.after(() => service.stop());

  const first = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") });
  assert.equal(first.status, 200);
  assert.equal(first.json.version, 1);
  assert.equal(first.json.accountId, ACCOUNT_ID);
  assert.equal(first.json.currentCall.peerUid, "private-peer-uid");
  assert.equal(first.json.events[0].kind, "snapshot");

  const second = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") });
  assert.equal(second.status, 409);
  assert.deepEqual(second.json, { ok: false, code: "adapter_owned" });
  const deniedCommand = await request(port, "/v1/call/command", {
    body: {
      accountId: ACCOUNT_ID,
      clientInstance: "adapter-B",
      operation: "accept",
      scope: scopeFrom(first.json),
    },
  });
  assert.equal(deniedCommand.status, 409);
  assert.deepEqual(deniedCommand.json, { ok: false, code: "adapter_owned" });
  assert.deepEqual(bridge._invoked.accept, []);

  const released = await request(port, "/v1/adapter/release", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A" },
  });
  assert.equal(released.status, 200);
  assert.equal(released.json.released, true);
  assert.equal(bridge._invoked.stop, 0, "release never stops Bridge");
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") })).status, 200);

  await new Promise((resolve) => setTimeout(resolve, 1050));
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") })).status, 200);
});

test("poll journal returns the latest first snapshot and detects expired or future cursors", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge, { journalLimit: 2 });
  t.after(() => service.stop());
  bridge.setSnapshot({}, "attached");
  bridge.setSnapshot({}, "invite_observed");
  bridge.setSnapshot({}, "call_connected");
  for (let index = 0; index < 100; index += 1) bridge.setSnapshot({}, "ack_uncorrelated");

  const first = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A", 0) });
  assert.equal(first.status, 200);
  assert.equal(first.json.events.length, 2);
  assert.deepEqual(first.json.events.map((event) => event.kind), ["invite_observed", "call_connected"]);
  assert.equal(first.json.cursor, 4);

  const expired = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A", 1) });
  assert.equal(expired.status, 409);
  assert.deepEqual(expired.json, { ok: false, code: "event_cursor_expired" });
  const future = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A", 5) });
  assert.equal(future.status, 409);
  assert.deepEqual(future.json, { ok: false, code: "event_cursor_invalid" });
});

test("authenticated poll journals exact private busy notices within the bounded event window", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge, { journalLimit: 2 });
  t.after(() => service.stop());
  const initial = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(initial.status, 200);

  const firstUid = `u_${"A".repeat(22)}`;
  const secondUid = `u_${"C".repeat(22)}`;
  const thirdUid = `u_${"D".repeat(22)}`;
  const firstBusy = { kind: "invite_busy", busyInvite: {
    callerUid: firstUid,
    identityCorrelation: "kernel_from_uid_matched",
    nativeInviteEvidence: privateBusyEvidence(firstUid, "kernel_from_uid_matched", "busy-relation-a", 1),
  } };
  const secondBusy = { kind: "invite_busy", busyInvite: {
    callerUid: secondUid,
    identityCorrelation: "kernel_relation_uid_matched",
    nativeInviteEvidence: privateBusyEvidence(secondUid, "kernel_relation_uid_matched", "10003", 2),
  } };
  const thirdBusy = { kind: "invite_busy", busyInvite: {
    callerUid: thirdUid,
    identityCorrelation: "kernel_from_uid_matched",
    nativeInviteEvidence: privateBusyEvidence(thirdUid, "kernel_from_uid_matched", "busy-relation-d", 3),
  } };
  bridge.emitControlEvent({ kind: "invite_busy", busyInvite: {
    callerUid: `u_${"X".repeat(22)}`,
    identityCorrelation: "kernel_from_uid_matched",
  } });
  bridge.emitControlEvent(firstBusy);
  bridge.emitControlEvent(secondBusy);
  bridge.emitControlEvent(thirdBusy);

  const unauthorized = await request(port, "/v1/adapter/poll", { token: null, body: pollBody() });
  assert.equal(unauthorized.status, 401);
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(polled.status, 200);
  assert.equal(polled.json.events.length, 2, "journal limit also bounds private busy event retention");
  assert.deepEqual(polled.json.events.map((event) => ({
    sequence: event.sequence,
    kind: event.kind,
    call: event.call,
    callerUid: event.busyInvite.callerUid,
    identityCorrelation: event.busyInvite.identityCorrelation,
    proof: {
      inviteType: event.busyInvite.privateScopeProof.inviteType,
      fromUid: event.busyInvite.privateScopeProof.fromUid,
      relationId: event.busyInvite.privateScopeProof.relationId,
      scopeKind: event.busyInvite.privateScopeProof.scopeKind,
    },
  })), [
    {
      sequence: initial.json.cursor + 2,
      kind: "invite_busy",
      call: null,
      callerUid: secondUid,
      identityCorrelation: "kernel_relation_uid_matched",
      proof: { inviteType: 1, fromUid: "", relationId: "10003", scopeKind: "busy" },
    },
    {
      sequence: initial.json.cursor + 3,
      kind: "invite_busy",
      call: null,
      callerUid: thirdUid,
      identityCorrelation: "kernel_from_uid_matched",
      proof: { inviteType: 1, fromUid: thirdUid, relationId: "busy-relation-d", scopeKind: "busy" },
    },
  ]);
  assert.equal(polled.json.events.some((event) => Object.hasOwn(event, "fingerprint")), false);
});

test("group status is lease-bound, exact, and reflects the global Bridge slot; join stays unsupported", async (t) => {
  const bridge = makeBridge({ call: callSnapshot() });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const poll = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(poll.status, 200);

  const status = await request(port, "/v1/group/status", { body: groupStatusBody() });
  assert.equal(status.status, 200);
  assert.deepEqual(status.json, {
    version: 1,
    accountId: ACCOUNT_ID,
    serviceInstance: poll.json.serviceInstance,
    hostGeneration: 51,
    connectionGeneration: 61,
    groupId: "20002",
    occupied: true,
    nativeJoinVerified: false,
    room: null,
  });
  const busyJoin = await request(port, "/v1/group/join", { body: groupJoinBody() });
  assert.equal(busyJoin.status, 409);
  assert.deepEqual(busyJoin.json, { ok: false, code: "call_occupied" });
  assert.deepEqual(bridge._invoked.accept, [], "group intent never invokes private accept");
  assert.deepEqual(bridge._invoked.hangup, []);
  assert.deepEqual(bridge._invoked.reject, []);

  bridge.setSnapshot({ call: null }, "call_cleared");
  const availableStatus = await request(port, "/v1/group/status", { body: groupStatusBody() });
  assert.equal(availableStatus.status, 200);
  assert.equal(availableStatus.json.occupied, false);
  assert.equal(availableStatus.json.nativeJoinVerified, false);
  assert.equal(availableStatus.json.room, null);
  const unsupportedJoin = await request(port, "/v1/group/join", { body: groupJoinBody() });
  assert.equal(unsupportedJoin.status, 501);
  assert.deepEqual(unsupportedJoin.json, { ok: false, code: "unsupported_unverified_group_join" });
});

test("group routes reject malformed IDs, types, shapes, accounts, and unleased clients", async (t) => {
  const bridge = makeBridge();
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  assert.deepEqual((await request(port, "/v1/group/status", { token: null, body: groupStatusBody() })).json,
    { ok: false, code: "unauthorized" });
  const poll = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(poll.status, 200);
  const unleased = await request(port, "/v1/group/status", { body: groupStatusBody("adapter-B") });
  assert.equal(unleased.status, 409);
  assert.deepEqual(unleased.json, { ok: false, code: "adapter_owned" });

  const extraStatus = await request(port, "/v1/group/status", { body: { ...groupStatusBody(), extra: true } });
  assert.deepEqual(extraStatus.json, { ok: false, code: "invalid_group_status_shape" });
  const wrongAccount = await request(port, "/v1/group/status", { body: { ...groupStatusBody(), accountId: "10002" } });
  assert.deepEqual(wrongAccount.json, { ok: false, code: "account_mismatch" });
  for (const groupId of ["", "0", "020002", "1".repeat(21), 20002, null]) {
    const invalid = await request(port, "/v1/group/status", { body: { ...groupStatusBody(), groupId } });
    assert.equal(invalid.status, 400);
    assert.deepEqual(invalid.json, { ok: false, code: "invalid_group_id" });
  }
  for (const [field, value, expected] of [
    ["groupId", "020002", "invalid_group_id"],
    ["requesterUserId", 10003, "invalid_requester_user_id"],
    ["requesterUserId", "010003", "invalid_requester_user_id"],
    ["triggerKind", "voice", "invalid_trigger_kind"],
  ]) {
    const invalid = await request(port, "/v1/group/join", { body: { ...groupJoinBody(), [field]: value } });
    assert.equal(invalid.status, 400);
    assert.deepEqual(invalid.json, { ok: false, code: expected });
  }
  const extraJoin = await request(port, "/v1/group/join", { body: { ...groupJoinBody(), roomId: "untrusted" } });
  assert.deepEqual(extraJoin.json, { ok: false, code: "invalid_group_join_shape" });
});

test("group status and join fail closed when the global Bridge slot projection is missing", async (t) => {
  const bridge = makeBridge();
  bridge.getIntegrationSnapshot = () => ({
    status: { hostGeneration: 51, connectionGeneration: 61 },
    call: null,
  });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody() })).status, 200);
  const status = await request(port, "/v1/group/status", { body: groupStatusBody() });
  assert.equal(status.status, 503);
  assert.deepEqual(status.json, { ok: false, code: "bridge_status_unavailable" });
  const join = await request(port, "/v1/group/join", { body: groupJoinBody() });
  assert.equal(join.status, 503);
  assert.deepEqual(join.json, { ok: false, code: "bridge_status_unavailable" });
  assert.deepEqual(bridge._invoked.accept, []);
  assert.deepEqual(bridge._invoked.hangup, []);
  assert.deepEqual(bridge._invoked.reject, []);
});

test("concurrent group joins observe the active global call slot and never reach Host", async (t) => {
  const providerHandlers = new Set();
  const hostHandlers = new Set();
  const hostInvokes = [];
  const provider = {
    async attach() { return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { return { ok: true, status: { listenerRegistered: false } }; },
    status() { return { connected: true }; },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true }; },
    subscribe(handler) { providerHandlers.add(handler); return () => providerHandlers.delete(handler); },
    async emitInvite() {
      const event = {
        kind: "kernel_event",
        method: "OnInviteActionToAVSDK",
        args: [{ relation_id: "active-invite", invite_type: 3, from_uid: "caller-A" }],
        connectionGeneration: 61,
      };
      await Promise.all([...providerHandlers].map((handler) => handler("kernel_event", event)));
    },
  };
  const host = {
    status() { return { available: true, hostGeneration: 51 }; },
    subscribe(handler) { hostHandlers.add(handler); handler({ type: "availability", available: true, hostGeneration: 51 }); return () => hostHandlers.delete(handler); },
    async invoke(input) { hostInvokes.push(input); return { posted: true, command: input.command, id: input.id }; },
  };
  const bridge = createQQCallBridge({ provider, host, connectionGeneration: 61, hostGeneration: 51 });
  await bridge.attach();
  await provider.emitInvite();
  const occupiedBefore = bridge.status().activeCall;
  const { service, port } = await createService(bridge);
  t.after(async () => { await service.stop(); await bridge.stop(); });
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody() })).status, 200);

  const responses = await Promise.all([
    request(port, "/v1/group/join", { body: groupJoinBody() }),
    request(port, "/v1/group/join", { body: { ...groupJoinBody(), triggerKind: "natural", requesterUserId: "10004" } }),
  ]);
  assert.deepEqual(responses.map((response) => [response.status, response.json.code]), [
    [409, "call_occupied"],
    [409, "call_occupied"],
  ]);
  assert.deepEqual(bridge.status().activeCall, occupiedBefore, "serialized group requests cannot change the slot");
  assert.deepEqual(hostInvokes, [], "no native group command or private accept reaches Host");
});

test("full-scope stale commands never invoke Bridge, including while queued", async (t) => {
  let completeFirst;
  const firstCommand = new Promise((resolve) => { completeFirst = resolve; });
  const bridge = makeBridge({
    call: callSnapshot(),
    accept: async (callId) => {
      if (bridge._invoked.accept.length === 1) return firstCommand;
      return { posted: true, id: 91, callId, callGeneration: 7 };
    },
  });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  const scope = scopeFrom(polled.json);

  bridge.setSnapshot({ status: { hostGeneration: 52 } }, "new_host_generation");
  const stale = await request(port, "/v1/call/command", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope },
  });
  assert.equal(stale.status, 409);
  assert.deepEqual(stale.json, { ok: false, code: "call_scope_stale" });
  assert.deepEqual(bridge._invoked.accept, []);

  bridge.setSnapshot({ status: { hostGeneration: 51 } }, "restored_host_generation");
  const firstReq = request(port, "/v1/call/command", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope },
  });
  await waitFor(() => bridge._invoked.accept.length === 1);
  const queuedReq = request(port, "/v1/call/command", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope },
  });
  await new Promise((resolve) => setImmediate(resolve));
  bridge.setSnapshot({ call: callSnapshot({ generation: 8 }) }, "call_changed_while_queued");
  completeFirst({ posted: true, id: 92, callId: scope.callId, callGeneration: scope.callGeneration });
  const firstResponse = await firstReq;
  assert.equal(firstResponse.status, 200);
  const queuedResponse = await queuedReq;
  assert.equal(queuedResponse.status, 409);
  assert.deepEqual(queuedResponse.json, { ok: false, code: "call_scope_stale" });
  assert.equal(bridge._invoked.accept.length, 1, "queued stale command was rejected before invocation");
});

for (const fails of [false, true]) test(`a completed long command (${fails ? "failure" : "success"}) leaves its owner a fresh response window without renewing diagnostic reads`, async (t) => {
  let finish, fail;
  const delayed = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  const bridge = makeBridge({ call: callSnapshot(), accept: () => delayed });
  const { service, port } = await createService(bridge, { leaseMs: 1000 });
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") });
  const command = request(port, "/v1/call/command", { body: { accountId: ACCOUNT_ID,
    clientInstance: "adapter-A", operation: "accept", scope: scopeFrom(polled.json) } });
  await waitFor(() => bridge._invoked.accept.length === 1);
  await new Promise((resolve) => setTimeout(resolve, 1050));
  const competing = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") });
  assert.equal(competing.json.code, "adapter_owned", "the running operation keeps its exact lease pinned");
  if (fails) fail(Object.assign(new Error("synthetic failure"), { code: "command_failed" }));
  else finish({ posted: true, id: 91, callId: polled.json.currentCall.callId, callGeneration: 7 });
  assert.equal((await command).status, fails ? 502 : 200);
  assert.equal(service.status().leaseActive, true, "the lease must not expire at the instant the response is delivered");
  await request(port, "/v1/call/diagnostics", { body: { accountId: ACCOUNT_ID } });
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") })).status, 409);
  await new Promise((resolve) => setTimeout(resolve, 1050));
  assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-B") })).status, 200,
    "the response grace still expires, and read-only diagnostics never renew it");
});

for (const renew of [false, true]) test(`a queued command whose lease ${renew ? "is renewed" : "expires"} while another command runs ${renew ? "remains executable" : "never invokes Bridge"}`, async (t) => {
  let completeFirst;
  const firstCommand = new Promise((resolve) => { completeFirst = resolve; });
  const bridge = makeBridge({
    call: callSnapshot(),
    accept: async (callId) => {
      if (bridge._invoked.accept.length === 1) return firstCommand;
      return { posted: true, id: 92, callId, callGeneration: 7 };
    },
  });
  const { service, port } = await createService(bridge, { leaseMs: 1000 });
  t.after(() => service.stop());
  const poll = (await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") })).json;
  const body = {
    accountId: ACCOUNT_ID,
    clientInstance: "adapter-A",
    operation: "accept",
    scope: scopeFrom(poll),
  };
  const firstRequest = request(port, "/v1/call/command", { body });
  await waitFor(() => bridge._invoked.accept.length === 1);
  const queuedRequest = request(port, "/v1/call/command", { body });
  await waitFor(() => service.status().queuedCommands === 2);
  await new Promise((resolve) => setTimeout(resolve, 1050));
  if (renew) assert.equal((await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A") })).status, 200);
  completeFirst({ posted: true, id: 91, callId: body.scope.callId, callGeneration: 7 });
  assert.equal((await firstRequest).status, 200);
  const queued = await queuedRequest;
  assert.equal(queued.status, renew ? 200 : 409);
  if (!renew) assert.deepEqual(queued.json, { ok: false, code: "adapter_lease_required" });
  assert.equal(bridge._invoked.accept.length, renew ? 2 : 1,
    "only an ordinary owner renewal can preserve queued work across a long command");
});

test("calls remain unroutable until a correlated invite callback, then accept returns posted receipt only", async (t) => {
  const bridge = makeBridge({ call: callSnapshot({ phase: "invite_observed", inviteCallbackReady: false, inviteCorrelation: "awaiting_20006" }) });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());

  const pending = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(pending.status, 200);
  assert.equal(pending.json.currentCall.phase, "awaiting_20006");
  assert.equal(pending.json.currentCall.inviteCorrelation, "awaiting_20006");
  assert.equal(pending.json.currentCall.inviteCallbackReady, false);
  assert.equal(pending.json.currentCall.peerUid, null);
  const notReady = await request(port, "/v1/call/command", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope: scopeFrom(pending.json) },
  });
  assert.equal(notReady.status, 409);
  assert.deepEqual(notReady.json, { ok: false, code: "invite_callback_not_ready" });
  assert.deepEqual(bridge._invoked.accept, []);

  bridge.setSnapshot({ call: callSnapshot() }, "invite_callback_ready");
  const ready = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A", pending.json.cursor) });
  assert.equal(ready.status, 200);
  assert.equal(ready.json.currentCall.peerUid, "private-peer-uid");
  assert.equal(ready.json.currentCall.inviteCorrelation, "kernel_from_uid_matched");
  const fullScope = scopeFrom(ready.json);
  for (const [overrides, expectedCode] of [
    [{ peerUid: null }, "invite_callback_not_ready"],
    [{ inviteCorrelation: "single_active_invite_uncorrelated" }, "invite_callback_not_ready"],
    [{ uncertain: true }, "call_not_accepting"],
    [{ phase: "connected" }, "call_not_accepting"],
    [{ acceptState: "posted" }, "accept_already_requested"],
  ]) {
    bridge.setSnapshot({ call: callSnapshot(overrides) });
    const denied = await request(port, "/v1/call/command", {
      body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope: fullScope },
    });
    assert.equal(denied.status, 409);
    assert.deepEqual(denied.json, { ok: false, code: expectedCode });
    assert.deepEqual(bridge._invoked.accept, [], "stale or uncorrelated ready metadata never reaches Bridge");
  }
  bridge.setSnapshot({ call: callSnapshot() });
  const accepted = await request(port, "/v1/call/command", {
    body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation: "accept", scope: fullScope },
  });
  assert.equal(accepted.status, 200);
  assert.deepEqual(accepted.json.result, {
    posted: true,
    id: 88,
    callId: ready.json.currentCall.callId,
    callGeneration: ready.json.currentCall.generation,
  });
  assert.equal(accepted.text.includes("must-not-echo"), false);
  assert.equal(accepted.text.includes("private-peer-uid"), false);
  assert.deepEqual(bridge._invoked.accept, [ready.json.currentCall.callId]);

  bridge.setSnapshot({ call: callSnapshot({ inviteCorrelation: "kernel_relation_uid_matched" }) }, "invite_callback_ready");
  const relationMapped = await request(port, "/v1/adapter/poll", {
    body: pollBody("adapter-A", accepted.json.cursor),
  });
  assert.equal(relationMapped.status, 200);
  assert.equal(relationMapped.json.currentCall.inviteCorrelation, "kernel_relation_uid_matched");
  const mappedAcceptance = await request(port, "/v1/call/command", {
    body: {
      accountId: ACCOUNT_ID,
      clientInstance: "adapter-A",
      operation: "accept",
      scope: scopeFrom(relationMapped.json),
    },
  });
  assert.equal(mappedAcceptance.status, 200, "the separately verified relation-to-UID source passes the same call gate");
  assert.equal(bridge._invoked.accept.length, 2);
});

test("control stop fences delayed device selection before cmd 5 and keeps the lease pinned", async (t) => {
  let releaseSelection;
  let selectionStarted;
  const selectionStartedPromise = new Promise((resolve) => { selectionStarted = resolve; });
  const selectionPromise = new Promise((resolve) => { releaseSelection = resolve; });
  const providerHandlers = new Set();
  const hostHandlers = new Set();
  const posted = [];
  const provider = {
    async attach() { return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { return { ok: true, status: { listenerRegistered: false } }; },
    status() { return { connected: true, agent: { agent: { listenerRegistered: true } } }; },
    subscribe(handler) { providerHandlers.add(handler); return () => providerHandlers.delete(handler); },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true }; },
  };
  const host = {
    subscribe(handler) {
      hostHandlers.add(handler);
      handler({ type: "availability", available: true, hostGeneration: 51 });
      return () => hostHandlers.delete(handler);
    },
    async invoke(input) {
      posted.push({ ...input });
      return { posted: true, id: input.id, command: input.command };
    },
    async selectAudioDevices(input) {
      selectionStarted(input);
      return selectionPromise;
    },
  };
  const bridge = createQQCallBridge({
    provider,
    host,
    connectionGeneration: 61,
    hostGeneration: 51,
    audioDeviceTarget: {
      inputDeviceName: "Line 1 (Virtual Audio Cable)",
      outputDeviceName: "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)",
    },
  });
  await bridge.attach();
  t.after(() => bridge.stop());
  const login = await bridge.login({ selfUid: "self", selfUin: ACCOUNT_ID, accountPath: "private" });
  await Promise.all([...hostHandlers].map((handler) => handler({
    type: "output", command: 1, id: login.id, value: [0, ""], hostGeneration: 51,
  })));
  await Promise.all([...providerHandlers].map((handler) => handler("kernel_event", {
    kind: "kernel_event",
    method: "OnInviteActionToAVSDK",
    args: [{ from_uid: "private-peer" }],
    connectionGeneration: 61,
  })));
  await Promise.all([...hostHandlers].map((handler) => handler({
    type: "output",
    command: 20006,
    id: 0,
    value: [1, "private-peer", "self", 2, "room", null, null, null, null, null, true, "extra"],
    hostGeneration: 51,
  })));

  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(polled.status, 200);
  const body = {
    accountId: ACCOUNT_ID,
    clientInstance: "adapter-A",
    operation: "accept",
    scope: scopeFrom(polled.json),
  };
  const accepted = request(port, "/v1/call/command", { body });
  const selection = await selectionStartedPromise;
  const release = await request(port, "/v1/adapter/release", {
    body: { accountId: ACCOUNT_ID, clientInstance: body.clientInstance },
  });
  assert.equal(release.status, 409);
  assert.deepEqual(release.json, { ok: false, code: "adapter_command_in_flight" });

  const stopping = service.stop();
  assert.equal(service.status().stopped, true);
  releaseSelection({ selected: true, id: selection.id, command: 102 });
  const outcome = await accepted;
  const stopResult = await Promise.race([
    stopping,
    new Promise((_, reject) => setTimeout(() => reject(new Error("control stop stalled")), 1000)),
  ]);
  assert.equal(outcome.status, 503);
  assert.deepEqual(outcome.json, { ok: false, code: "service_stopped" });
  assert.equal(stopResult.stopped, true);
  assert.equal(posted.some((entry) => entry.command === 5), false, "stop fence prevents cmd 5 after selection returns");
});

test("control accept keeps its captured private proof current across an awaited selection", async (t) => {
  const peerUid = `u_${"A".repeat(22)}`;
  const nativeInviteEvidence = {
    profile: PRIVATE_INVITE_PROFILE,
    wrapperSha256: PINNED_WRAPPER_SHA256,
    callbackSequence: 1,
    listenerRegistrationSequence: 2,
    inviteType: 1,
    relationId: "10002",
    fromUid: peerUid,
    targetPid: 4455,
    kernelCreationTime100ns: "134000000000000000",
    connectionGeneration: 61,
    listenerMutationEpoch: 4,
    ipcSequence: 12,
    inviteEventId: "123e4567-e89b-42d3-a456-426614174000",
  };
  const initialCall = callSnapshot({ peerUid, nativeInviteEvidence });
  let releaseSelection;
  let selectionStarted;
  const selectionStartedPromise = new Promise((resolve) => { selectionStarted = resolve; });
  const selectionGate = new Promise((resolve) => { releaseSelection = resolve; });
  let nativeAcceptPosted = false;
  const bridge = makeBridge({
    call: initialCall,
    accept: async (callId, options) => {
      options.assertCurrent();
      selectionStarted();
      await selectionGate;
      options.assertCurrent();
      nativeAcceptPosted = true;
      return { posted: true, id: 88, callId, callGeneration: initialCall.generation };
    },
  });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  assert.equal(polled.status, 200);
  assert.equal(polled.json.currentCall.privateScopeProof?.fromUid, peerUid);
  const commandBody = {
    accountId: ACCOUNT_ID,
    clientInstance: "adapter-A",
    operation: "accept",
    scope: scopeFrom(polled.json),
  };
  const accepting = request(port, "/v1/call/command", { body: commandBody });
  await selectionStartedPromise;

  const proofRevokedCall = callSnapshot({ peerUid });
  bridge.setSnapshot({ call: proofRevokedCall }, "invite_callback_ready");
  const currentPoll = await request(port, "/v1/adapter/poll", { body: pollBody("adapter-A", polled.json.cursor) });
  assert.equal(currentPoll.status, 200);
  assert.equal(Object.hasOwn(currentPoll.json.currentCall, "privateScopeProof"), false);
  assert.equal(currentPoll.text.includes("privateScopeProof"), false,
    "poll removes retired proofs from both the current call and historical snapshots");

  releaseSelection();
  const response = await accepting;
  assert.equal(response.status, 409);
  assert.deepEqual(response.json, { ok: false, code: "private_scope_unverified" });
  assert.equal(nativeAcceptPosted, false, "a retired captured proof cannot pass the awaited accept fence");
});

test("control preserves fixed device-selection failures and redacts unknown Bridge codes", async (t) => {
  let failureCode = "audio_device_selection_unverified";
  const bridge = makeBridge({
    call: callSnapshot(),
    accept: async () => {
      const error = new Error("must not be returned");
      error.code = failureCode;
      throw error;
    },
  });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  const body = {
    accountId: ACCOUNT_ID,
    clientInstance: "adapter-A",
    operation: "accept",
    scope: scopeFrom(polled.json),
  };
  for (const [code, status] of [
    ["audio_device_selection_unverified", 501],
    ["audio_device_not_found", 409],
    ["audio_device_map_changed", 409],
    ["audio_device_selection_quarantined", 409],
    ["enter_relation_reused_requires_new_host", 409],
    ["enter_relation_history_full_requires_new_host", 409],
  ]) {
    failureCode = code;
    const response = await request(port, "/v1/call/command", { body });
    assert.equal(response.status, status);
    assert.deepEqual(response.json, { ok: false, code });
    assert.equal(response.text.includes("must not be returned"), false);
  }
  failureCode = "secret-peer-or-stack-marker";
  const redacted = await request(port, "/v1/call/command", { body });
  assert.equal(redacted.status, 502);
  assert.deepEqual(redacted.json, { ok: false, code: "command_failed" });
  assert.equal(redacted.text.includes("secret-peer-or-stack-marker"), false);
});

test("hangup and reject preserve typed unsupported-command codes without retry", async (t) => {
  const bridge = makeBridge({ call: callSnapshot() });
  const { service, port } = await createService(bridge);
  t.after(() => service.stop());
  const polled = await request(port, "/v1/adapter/poll", { body: pollBody() });
  const scope = scopeFrom(polled.json);
  for (const operation of ["hangup", "reject"]) {
    const result = await request(port, "/v1/call/command", {
      body: { accountId: ACCOUNT_ID, clientInstance: "adapter-A", operation, scope },
    });
    assert.equal(result.status, 501);
    assert.deepEqual(result.json, { ok: false, code: `unsupported_unverified_command:${operation}` });
  }
  assert.deepEqual(bridge._invoked.hangup, [scope.callId]);
  assert.deepEqual(bridge._invoked.reject, [scope.callId]);
});

test("failed service startup releases its Bridge subscription and stop stays clean", async () => {
  const bridge = makeBridge();
  const blocker = http.createServer();
  await new Promise((resolve) => blocker.listen(0, "127.0.0.1", resolve));
  const busyPort = blocker.address().port;
  const service = createControlService({ bridge, token: CONTROL_TOKEN, accountId: ACCOUNT_ID, port: busyPort });
  await assert.rejects(service.start(), { code: "control_port_in_use" });
  assert.equal(bridge._handlers.size, 0);
  const stopped = await service.stop();
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.started, false);
  await new Promise((resolve) => blocker.close(resolve));
});

test("Bridge integration snapshot exposes only copied primitive call metadata and status subscriptions", async () => {
  const providerHandlers = new Set();
  const hostHandlers = new Set();
  const provider = {
    async attach() { return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { return { ok: true, status: { listenerRegistered: false } }; },
    status() { return { connected: true }; },
    subscribe(handler) { providerHandlers.add(handler); return () => providerHandlers.delete(handler); },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true }; },
  };
  const host = {
    status() { return { available: true }; },
    subscribe(handler) { hostHandlers.add(handler); return () => hostHandlers.delete(handler); },
    async invoke({ command, id }) {
      for (const handler of hostHandlers) handler({
        type: "output", hostGeneration: 51, command, id, value: [0, ""],
      });
      return { posted: true, command, id };
    },
  };
  const bridge = createQQCallBridge({ provider, host, connectionGeneration: 61, hostGeneration: 51 });
  const observed = [];
  const unsubscribe = bridge.subscribeStatus((event) => observed.push(event.kind));
  const first = bridge.getIntegrationSnapshot();
  assert.equal(first.call, null);
  assert.equal((await bridge.attach()).attached, true);
  await bridge.login({ selfUid: "self", selfUin: "10001", accountPath: "account" });
  for (const handler of providerHandlers) handler("kernel_event", {
    connectionGeneration: 61,
    method: "OnInviteActionToAVSDK",
    args: [{ from_uid: "kernel-peer" }],
  });
  const pending = bridge.getIntegrationSnapshot();
  assert.equal(pending.call.peerUid, "kernel-peer");
  assert.equal(pending.call.inviteCallbackReady, false);
  assert.equal(pending.call.inviteCorrelation, "awaiting_20006");
  const callId = pending.call.callId;
  await Promise.all([...hostHandlers].map((handler) => handler({
    type: "output",
    command: 20006,
    id: 0,
    value: [1, "wrong-peer", "self", 2, "room", null, null, null, null, null, true, "extra"],
    hostGeneration: 51,
  })));
  const mismatch = bridge.getIntegrationSnapshot();
  assert.equal(mismatch.call.callId, callId);
  assert.equal(mismatch.call.peerUid, null, "an unmatched Host callback suppresses peer identity");
  assert.equal(mismatch.call.inviteCallbackReady, false);
  await Promise.all([...hostHandlers].map((handler) => handler({
    type: "output",
    command: 20006,
    id: 0,
    value: [1, "kernel-peer", "self", 2, "room", null, null, null, null, null, true, "extra"],
    hostGeneration: 51,
  })));
  const ready = bridge.getIntegrationSnapshot();
  assert.deepEqual(Object.keys(ready.call).sort(), [
    "acceptState", "callId", "generation", "inviteCallbackReady", "inviteCorrelation", "peerUid", "phase", "uncertain",
  ]);
  assert.equal(ready.call.peerUid, null, "a previously mismatched callback keeps identity suppressed");
  assert.equal(ready.call.inviteCallbackReady, true);
  assert.ok(observed.includes("invite_callback_unmatched"));
  assert.equal(unsubscribe(), true);
  await bridge.stop();
  assert.equal(providerHandlers.size, 0);
  assert.equal(hostHandlers.size, 0);
});

test("CLI keeps control opt-in, requires all flags, and validates canonical QQUIN", () => {
  const base = [
    "--pid", "123", "--app-path", process.cwd(), "--token-file", "host-token.json",
    "--host-port", "6111", "--output-port", "6110", "--host-generation", "9", "--connection-generation", "10",
  ];
  const legacy = parseArgs(base);
  assert.equal(Object.prototype.hasOwnProperty.call(legacy, "controlPort"), false);
  const enabled = parseArgs([...base, "--control-port", "0", "--control-token-file", "control-token", "--account-uin", "1"]);
  assert.equal(enabled.controlPort, 0);
  assert.equal(enabled.accountUin, "1");
  for (const args of [
    [...base, "--control-port", "0"],
    [...base, "--control-port", "0", "--control-token-file", "token"],
    [...base, "--control-port", "-1", "--control-token-file", "token", "--account-uin", ACCOUNT_ID],
    [...base, "--control-port", "0", "--control-token-file", "token", "--account-uin", "010001"],
  ]) assert.throws(() => parseArgs(args));
});

test("CLI starts the opt-in endpoint and EOF closes it without an implicit attach or login", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for this CLI smoke test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-control-cli-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const hostTokenFile = path.join(directory, "host-token.json");
  const controlTokenFile = path.join(directory, "control-token.json");
  fs.writeFileSync(hostTokenFile, JSON.stringify({ ipcToken: TOKEN }));
  fs.writeFileSync(controlTokenFile, JSON.stringify({ ipcToken: CONTROL_TOKEN }));
  const input = new PassThrough();
  let stdout = "";
  let stderr = "";
  const output = new Writable({ write(chunk, _encoding, callback) { stdout += chunk.toString("utf8"); callback(); } });
  const errorOutput = new Writable({ write(chunk, _encoding, callback) { stderr += chunk.toString("utf8"); callback(); } });
  const hostPort = await reservePort();
  let outputPort = await reservePort();
  while (outputPort === hostPort) outputPort = await reservePort();
  const options = parseArgs([
    "--pid", String(process.pid), "--app-path", process.cwd(), "--token-file", hostTokenFile,
    "--host-port", String(hostPort), "--output-port", String(outputPort), "--host-generation", "901",
    "--connection-generation", "902", "--config", path.join(directory, "agent-config.json"),
    "--report", path.join(directory, "agent-report"), "--control-port", "0",
    "--control-token-file", controlTokenFile, "--account-uin", ACCOUNT_ID,
  ]);
  const cli = await startCli(options, { input, output, errorOutput });
  t.after(async () => { if (!cli.bridge.status().stopped) { try { await cli.close(); } catch { /* test cleanup */ } } });
  assert.equal(cli.controlService.status().started, true);
  assert.equal(cli.bridge.status().attached, false);
  assert.equal(cli.bridge.status().loginState, "logged_out");
  assert.match(stdout, /"kind":"bridge_ready".*"controlPort":\d+/);
  assert.doesNotMatch(stdout, new RegExp(`${TOKEN}|${CONTROL_TOKEN}`));

  const health = await request(cli.controlService.status().port, "/v1/adapter/poll", { body: pollBody("adapter-smoke") });
  assert.equal(health.status, 200);
  const identityPath = path.join(directory, "wrong-account.json");
  fs.writeFileSync(identityPath, JSON.stringify({ selfUid: "uid", selfUin: "10002", accountPath: "private" }));
  await cli.handleCommand(`login "${identityPath}"`);
  assert.match(stdout, /"command":"login","code":"configured_account_mismatch"/);
  assert.equal(cli.bridge.status().loginState, "logged_out");

  input.end();
  await waitFor(() => cli.controlService.status().stopped && cli.broker.status().stopped && cli.host.status().stopped);
  assert.deepEqual(await cli.close(), { stopped: true, detachedConfirmed: true });
  assert.equal(cli.bridge.status().attached, false);
  assert.equal(cli.bridge.status().loginState, "logged_out");
  assert.equal(stderr, "");
});

test("CLI control-port startup failure closes its local Host resources", {
  skip: process.platform !== "win32" ? "Windows named pipes are required for this CLI smoke test" : false,
}, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-control-cli-start-failure-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const hostTokenFile = path.join(directory, "host-token.json");
  const controlTokenFile = path.join(directory, "control-token.json");
  fs.writeFileSync(hostTokenFile, JSON.stringify({ ipcToken: TOKEN }));
  fs.writeFileSync(controlTokenFile, JSON.stringify({ ipcToken: CONTROL_TOKEN }));
  const blocker = http.createServer();
  await new Promise((resolve) => blocker.listen(0, "127.0.0.1", resolve));
  const controlPort = blocker.address().port;
  let hostPort = await reservePort();
  let outputPort = await reservePort();
  while (outputPort === hostPort || outputPort === controlPort || hostPort === controlPort) {
    hostPort = await reservePort();
    outputPort = await reservePort();
  }
  const input = new PassThrough();
  const output = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  const errorOutput = new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  const options = parseArgs([
    "--pid", String(process.pid), "--app-path", process.cwd(), "--token-file", hostTokenFile,
    "--host-port", String(hostPort), "--output-port", String(outputPort), "--host-generation", "911",
    "--connection-generation", "912", "--config", path.join(directory, "agent-config.json"),
    "--report", path.join(directory, "agent-report"), "--control-port", String(controlPort),
    "--control-token-file", controlTokenFile, "--account-uin", ACCOUNT_ID,
  ]);
  await assert.rejects(startCli(options, { input, output, errorOutput }), { code: "control_port_in_use" });
  await new Promise((resolve) => blocker.close(resolve));

  const reuse = net.createServer();
  await new Promise((resolve, reject) => {
    reuse.once("error", reject);
    reuse.listen(outputPort, "127.0.0.1", resolve);
  });
  await new Promise((resolve) => reuse.close(resolve));
});
