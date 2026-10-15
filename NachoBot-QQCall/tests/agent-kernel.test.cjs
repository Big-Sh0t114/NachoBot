"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createKernelAgent } = require("../agent/kernel-agent.cjs");
const { PRIVATE_INVITE_PROFILE } = require("../shared/private-invite-scope.cjs");

test("capability probe uses only an existing session and does not register or initialize anything", async () => {
  let lookups = 0;
  const wrapper = {
    NodeIQQNTWrapperSession: {
      getNTWrapperSession(name) {
        lookups += 1;
        assert.equal(name, "nt_1");
        return null;
      },
      createNTWrapperSession() { assert.fail("must not create a session"); },
    },
  };
  const agent = createKernelAgent({ wrapper, connectionGeneration: 4 });
  const result = await agent.probe();
  assert.equal(result.ok, false);
  assert.equal(result.code, "session_not_found");
  assert.equal(agent.status().sessionFound, false);
  assert.equal(agent.status().listenerRegistered, false);
  assert.equal(lookups, 1);
});

test("missing wrapper accessor and null service fail with redacted capability codes", async () => {
  const missing = createKernelAgent({ wrapper: {} });
  assert.equal((await missing.probe()).code, "wrapper_session_accessor_missing");
  const noService = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => null }) } },
  });
  assert.equal((await noService.probe()).code, "avsdk_service_unavailable");
  const statusJson = JSON.stringify(noService.status());
  assert.doesNotMatch(statusJson, /token|uin|uid|login params/i);
});

test("native null sentinels fail closed before service lookup or listener registration", async () => {
  let serviceLookups = 0;
  let adds = 0;
  const sessionNull = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ isNull: () => true, getAVSDKService: () => { serviceLookups += 1; } }) } },
  });
  assert.equal((await sessionNull.probe()).code, "session_native_null");
  assert.equal(serviceLookups, 0);

  const serviceNull = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => ({ isNull: () => true, addKernelAVSDKListener() { adds += 1; } }) }) } },
  });
  assert.equal((await serviceNull.attach()).code, "service_native_null");
  assert.equal(adds, 0);
  assert.equal(serviceNull.status().listenerRegistered, false);
});

test("listener attaches only on request, detaches by returned listenerId, and can reattach", async () => {
  const removals = [];
  let adds = 0;
  const service = {
    addKernelAVSDKListener(listener) {
      adds += 1;
      return `listener-${adds}`;
    },
    removeKernelAVSDKListener(id) { removals.push(id); },
    setActionFromAVSDK() {},
  };
  const agent = createKernelAgent({
    removalMode: "returned-id",
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });
  assert.equal((await agent.probe()).ok, true);
  assert.equal(adds, 0, "probe must not register a listener");
  const simultaneous = await Promise.all([agent.attach(), agent.attach()]);
  assert.equal(simultaneous.every((result) => result.ok), true);
  assert.equal(adds, 1, "concurrent attach must register once");
  assert.equal(agent.status().listenerRegistered, true);
  assert.equal((await agent.detach()).ok, true);
  assert.deepEqual(removals, ["listener-1"]);
  assert.equal(agent.status().listenerRegistered, false);
  assert.equal((await agent.attach()).ok, true);
  assert.equal(adds, 2, "reattach uses the same verified service without session creation");
  assert.equal((await agent.detach()).ok, true);
  assert.deepEqual(removals, ["listener-1", "listener-2"]);
});

test("pinned listener-object contract accepts void add and removes the same object through reattach cycles", async () => {
  const added = [];
  const removed = [];
  const service = {
    addKernelAVSDKListener(listener) { added.push(listener); },
    removeKernelAVSDKListener(listener) { removed.push(listener); },
    setActionFromAVSDK() {},
  };
  const agent = createKernelAgent({
    removalMode: "listener-object",
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });

  assert.equal((await agent.attach()).ok, true);
  assert.equal(agent.status().listenerIdAvailable, false);
  assert.equal(agent.status().listenerRegistered, true);
  assert.equal((await agent.detach()).ok, true);
  assert.strictEqual(removed[0], added[0]);
  assert.equal((await agent.attach()).ok, true);
  assert.equal((await agent.detach()).ok, true);
  assert.strictEqual(removed[1], added[1]);
  assert.equal(added.length, 2);
  assert.equal(removed.length, 2);
});

test("unknown removal contract blocks attach before registering a listener", async () => {
  let adds = 0;
  const agent = createKernelAgent({
    removalMode: "unverified",
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => ({
      addKernelAVSDKListener() { adds += 1; },
      removeKernelAVSDKListener() {},
      setActionFromAVSDK() {},
    }) }) } },
  });
  assert.equal((await agent.probe()).ok, true);
  assert.equal((await agent.attach()).code, "listener_removal_contract_unverified");
  assert.equal(adds, 0);
  assert.equal(agent.status().listenerRegistered, false);
});

test("private invite evidence is type-1 only and remains bound to its original listener registration", async () => {
  const events = [];
  let activeListener = null;
  const service = {
    addKernelAVSDKListener(listener) { activeListener = listener; },
    removeKernelAVSDKListener(listener) { if (activeListener === listener) activeListener = null; },
    setActionFromAVSDK() {},
  };
  const agent = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
    connectionGeneration: 9,
    removalMode: "listener-object",
    nativeInviteScopeProfile: PRIVATE_INVITE_PROFILE,
    emit: (event) => events.push(event),
  });
  assert.equal((await agent.attach()).ok, true);
  const firstListener = activeListener;
  firstListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 1, from_uid: "" });
  firstListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 2, from_uid: "u_ABCDEFGHIJKLMNOPQRSTUV" });
  firstListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 3, from_uid: "u_ABCDEFGHIJKLMNOPQRSTUV" });
  firstListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 1, from_uid: null });
  assert.equal(events[0].nativeInviteEvidence.inviteType, 1);
  assert.equal(events[0].nativeInviteEvidence.fromUid, "");
  assert.equal(events[0].nativeInviteEvidence.listenerRegistrationSequence, 1);
  assert.equal(events.slice(1).every((event) => !Object.hasOwn(event, "nativeInviteEvidence")), true);

  assert.equal((await agent.detach()).ok, true);
  assert.equal((await agent.attach()).ok, true);
  const secondListener = activeListener;
  assert.notStrictEqual(secondListener, firstListener);
  // A delayed callback from the removed native listener keeps sequence 1;
  // Broker confirmation for the new listener will reject it.
  firstListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 1, from_uid: "" });
  secondListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 1, from_uid: "" });
  assert.equal(events[4].nativeInviteEvidence.listenerRegistrationSequence, 1);
  assert.equal(events[5].nativeInviteEvidence.listenerRegistrationSequence, 2);
  assert.equal(agent.status().callbackDiagnostics.knownCallbacks.onInviteActionToAVSDK.count, 6,
    "diagnostics continue to observe callbacks from stale listener registrations without changing their evidence binding");
  assert.equal((await agent.detach()).ok, true);

  const unclassifiedEvents = [];
  let unclassifiedListener;
  const unclassified = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => ({
      addKernelAVSDKListener(listener) { unclassifiedListener = listener; return "listener-id"; },
      removeKernelAVSDKListener() {},
      setActionFromAVSDK() {},
    }) }) } },
    removalMode: "returned-id",
    nativeInviteScopeProfile: "unknown-profile",
    emit: (event) => unclassifiedEvents.push(event),
  });
  assert.equal((await unclassified.attach()).ok, true);
  unclassifiedListener.OnInviteActionToAVSDK({ relation_id: "123456789", invite_type: 1, from_uid: "" });
  assert.equal(Object.hasOwn(unclassifiedEvents[0], "nativeInviteEvidence"), false);
  await unclassified.detach();
});

test("20001 is forwarded with the verified inverse mapping and stale generations are rejected", async () => {
  const calls = [];
  const service = {
    addKernelAVSDKListener: () => "listener-id",
    removeKernelAVSDKListener() {},
    setActionFromAVSDK: (...args) => calls.push(args),
  };
  const agent = createKernelAgent({
    connectionGeneration: 9,
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });
  assert.equal((await agent.probe()).ok, true);
  assert.equal((await agent.forwardAVSDKOutput({ command: 20001, value: [8, "secret action"] })).code, "agent_not_attached");
  assert.equal((await agent.attach()).ok, true);
  const stale = await agent.forwardAVSDKOutput({ command: 20001, value: [8, "secret action"], generation: 8 });
  assert.equal(stale.code, "stale_generation");
  assert.deepEqual(calls, []);
  const forwarded = await agent.forwardAVSDKOutput({ command: 20001, value: [8, "secret action"], generation: 9 });
  assert.equal(forwarded.ok, true);
  assert.deepEqual(calls, [[8, "secret action"]]);
  assert.equal((await agent.forwardAVSDKOutput({ command: 5, value: [] })).code, "unsupported_avsdk_output_command");
  await agent.detach();
});

test("kernel events preserve binary bytes privately and map upstream callbacks by exact argument positions", async () => {
  const events = [];
  let listener;
  const service = {
    addKernelAVSDKListener(value) { listener = value; return "listener-1"; },
    removeKernelAVSDKListener() {},
    setActionFromAVSDK() {},
  };
  const agent = createKernelAgent({
    connectionGeneration: 2,
    emit: (event) => events.push(event),
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });
  await agent.probe();
  assert.equal(agent.status().listenerRegistered, false);
  await agent.attach();
  listener.onInviteActionToAVSDK(1, 31, "private invite blob", Buffer.from([0, 1, 255]));
  listener.onActionToAVSDK(42, "private action blob", new Uint8Array([9, 8]));
  assert.equal(events.length, 2);
  assert.deepEqual(events[0].action, { command: 55, value: [31, "private invite blob"] });
  assert.deepEqual(events[1].action, { command: 55, value: [42, "private action blob"] });
  assert.deepEqual(events[0].args[3], { $qqcall: "binary", encoding: "base64", length: 3, data: "AAH/" });
  assert.deepEqual(events[1].args[2], { $qqcall: "binary", encoding: "base64", length: 2, data: "CQg=" });
  const status = JSON.stringify(agent.status());
  assert.doesNotMatch(status, /private invite blob|private action blob|AAH\/|CQg=/);
  assert.equal(agent.status().lastKernelEvent.arguments.type, "array");
  await agent.detach();
});

test("callback diagnostics aggregate fixed names, redact arguments, and retain unmapped group callback counts", async () => {
  let listener;
  const service = {
    addKernelAVSDKListener(value) { listener = value; return "listener-1"; },
    removeKernelAVSDKListener() {},
    setActionFromAVSDK() {},
  };
  const agent = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => service }) } },
  });
  assert.equal((await agent.attach()).ok, true);

  listener.ONGROUPAUDIOMEMNUMCHANGE("u_UID_SECRET_token_123456", { uin: "987654321", nested: { bearer: "secret" } });
  listener.onGroupVideoActionToAVSDK({ uid: "u_UID_SECRET_token_123456" });
  listener.OnGroupVideoServerPushToAVSDK(Buffer.from("private-payload"));
  listener.OnGroupVideoInviteMemberUpdate({ uid: "u_UID_SECRET_token_123456", invitees: ["private-child"] });
  listener.onUnknown_callback_u_UID_SECRET_token({ token: "private-token", payload: ["private-child"] });

  const status = agent.status();
  const diagnostics = status.callbackDiagnostics;
  assert.equal(diagnostics.unknownCallbackCount, 1);
  assert.equal(diagnostics.knownCallbacks.onGroupAudioMemNumChange.count, 1);
  assert.equal(diagnostics.knownCallbacks.onGroupVideoMemNumPushInfo.count, 0);
  assert.equal(diagnostics.knownCallbacks.onGroupVideoInviteMemberUpdate.count, 1);
  assert.equal(diagnostics.knownCallbacks.onGroupAudioMemNumChange.lastArgumentCount, 2);
  assert.deepEqual(diagnostics.knownCallbacks.onGroupAudioMemNumChange.lastArguments, [
    { type: "string", length: 25, lengthCapped: false },
    { type: "object" },
  ]);
  for (const name of ["onGroupVideoActionToAVSDK", "onGroupVideoServerPushToAVSDK"]) {
    const callback = diagnostics.knownCallbacks[name];
    assert.equal(callback.count, 1, name);
    assert.equal(callback.unmappedCount, 1, name);
    assert.equal(callback.mappedCount, 0, name);
    assert.equal(callback.lastActionMapped, false, name);
  }
  const diagnosticJson = JSON.stringify(diagnostics);
  for (const secret of ["u_UID_SECRET_token_123456", "987654321", "private-payload", "private-token", "onUnknown_callback_u_UID_SECRET_token", "bearer", "nested", "payload"]) {
    assert.equal(diagnosticJson.includes(secret), false, `callback diagnostics must omit ${secret}`);
  }

  diagnostics.knownCallbacks.onGroupAudioMemNumChange.lastArguments[0].length = 0;
  diagnostics.knownCallbacks.onGroupAudioMemNumChange.count = 0;
  assert.equal(agent.status().callbackDiagnostics.knownCallbacks.onGroupAudioMemNumChange.count, 1,
    "returned diagnostic projections must not mutate retained counters");
  assert.equal((await agent.detach()).ok, true);
});

test("membership shape diagnostics retain only fixed field kinds without invoking getters or changing forwarding", async () => {
  let listener;
  let getterReads = 0;
  const events = [];
  const agent = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => ({
      addKernelAVSDKListener(value) { listener = value; return "listener-1"; },
      removeKernelAVSDKListener() {},
      setActionFromAVSDK() {},
    }) }) } },
    emit: (event) => events.push(event),
  });
  assert.equal((await agent.attach()).ok, true);
  assert.equal(agent.status().callbackDiagnostics.knownCallbacks.onGroupAudioMemNumChange.lastMembershipShape, null);
  const info = { group_code: "private-group", onlineCount: 2, memberUins: ["private-member"], businessType: 3,
    secretArbitraryKey: "private-token" };
  Object.defineProperty(info, "isNativeTimeout", { enumerable: true, get() { getterReads += 1; throw Error("must not read"); } });
  listener.onGroupAudioMemNumChange({ notifyInfo: info });
  const snapshot = agent.status().callbackDiagnostics.knownCallbacks.onGroupAudioMemNumChange;
  const shape = snapshot.lastMembershipShape;
  assert.equal(shape.argumentKind, "object");
  assert.equal(shape.notifyInfoKind, "object");
  assert.equal(shape.rootGroupCodeKind, "absent");
  assert.equal(shape.notifyGroupCodeKind, "string");
  assert.equal(shape.notifyCamelGroupCodeKind, "absent");
  assert.equal(shape.notifyOnlineCountKind, "number");
  assert.equal(shape.notifyMemberUinsKind, "array");
  assert.equal(shape.notifyBusinessTypeKind, "number");
  assert.equal(shape.notifyNativeTimeoutKind, "accessor");
  assert.equal(Object.keys(shape).length, 14);
  assert.doesNotMatch(JSON.stringify(snapshot), /private-group|private-member|secretArbitraryKey|private-token/);
  assert.equal(getterReads, 0);
  assert.equal(events[0].method, "onGroupAudioMemNumChange");
  assert.equal(events[0].args[0].notifyInfo.group_code, "private-group");
  assert.deepEqual(events[0].args[0].notifyInfo.isNativeTimeout, { $qqcall: "accessor" });
  shape.notifyGroupCodeKind = "corrupted";
  assert.equal(agent.status().callbackDiagnostics.knownCallbacks.onGroupAudioMemNumChange.lastMembershipShape.notifyGroupCodeKind, "string");
  const accessor = {};
  Object.defineProperty(accessor, "notifyInfo", { enumerable: true, get() { getterReads += 1; throw Error("must not read"); } });
  listener.onGroupVideoMemNumPushInfo(accessor);
  const videoShape = agent.status().callbackDiagnostics.knownCallbacks.onGroupVideoMemNumPushInfo.lastMembershipShape;
  assert.equal(videoShape.notifyInfoKind, "accessor");
  assert.equal(videoShape.notifyOnlineCountKind, "absent");
  assert.equal(getterReads, 0);
  assert.equal((await agent.detach()).ok, true);
});

test("callback diagnostics bound argument and binary summaries and saturate aggregate counts", async () => {
  let listener;
  const agent = createKernelAgent({
    wrapper: { NodeIQQNTWrapperSession: { getNTWrapperSession: () => ({ getAVSDKService: () => ({
      addKernelAVSDKListener(value) { listener = value; return "listener-1"; },
      removeKernelAVSDKListener() {},
      setActionFromAVSDK() {},
    }) }) } },
  });
  assert.equal((await agent.attach()).ok, true);

  const oversizedBinary = new Uint8Array(1_000_001);
  const oversizedString = "s".repeat(1_000_001);
  listener.onGroupVideoMemNumPushInfo(oversizedBinary, oversizedString, {}, {}, {}, {}, {}, {}, "extra-1", "extra-2");
  const first = agent.status().callbackDiagnostics.knownCallbacks.onGroupVideoMemNumPushInfo;
  assert.equal(first.count, 1);
  assert.equal(first.lastArgumentCount, 10);
  assert.equal(first.lastArguments.length, 8);
  assert.deepEqual(first.lastArguments[0], { type: "binary", length: 1_000_000, lengthCapped: true });
  assert.deepEqual(first.lastArguments[1], { type: "string", length: 1_000_000, lengthCapped: true });
  assert.equal(first.omittedArgumentCount, 2);
  assert.equal(JSON.stringify(first).includes("extra-1"), false);

  const unknown = listener.onAnotherUnknown;
  for (let i = 0; i < 1_000_001; i += 1) unknown();
  assert.equal(agent.status().callbackDiagnostics.unknownCallbackCount, 1_000_000);
  assert.equal((await agent.detach()).ok, true);
});
