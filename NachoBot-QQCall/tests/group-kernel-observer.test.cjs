"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createGroupKernelObserver, MAX_TRACKED_GROUPS } = require("../bridge/group-kernel-observer.cjs");
const { createQQCallBridge } = require("../bridge/qqcall-bridge.cjs");

function kernelEvent(method, notifyInfo, connectionGeneration = 7) {
  return {
    connectionGeneration,
    method,
    args: [method.toLowerCase() === "ongroupaudiomemnumchange" ? notifyInfo : { notifyInfo }],
  };
}

function kernelInviteEvent(info, connectionGeneration = 7) {
  return {
    connectionGeneration,
    method: "onGroupVideoInviteMemberUpdate",
    args: [info],
  };
}

function makeObserver(now = () => 1_000) {
  return createGroupKernelObserver({ connectionGeneration: 7, now });
}

function audioMembership(groupCode = "1234567890", overrides = {}) {
  return {
    group_code: groupCode,
    onlineCount: 2,
    memberUins: ["private-uin-one", "private-uin-two"],
    businessType: 3,
    isNativeTimeout: false,
    ...overrides,
  };
}

test("captured audio callback uses direct fields; renderer notifyInfo wrapping is not the native contract", () => {
  const observer = makeObserver();
  const direct = { connectionGeneration: 7, method: "onGroupAudioMemNumChange", args: [audioMembership()] };
  assert.equal(observer.observe(direct), true);
  assert.equal(observer.getGroupDiagnostics("1234567890").membership.onlineCount, 2);
  const wrapped = { connectionGeneration: 7, method: "onGroupAudioMemNumChange", args: [{ notifyInfo: audioMembership("456") }] };
  assert.equal(observer.observe(wrapped), false);
  assert.equal(observer.getGroupDiagnostics("456"), null);
  assert.equal(observer.observe({ ...direct, args: [audioMembership("789", { isNativeTimeout: "false" })] }), false);
  assert.equal(observer.getGroupDiagnostics("789"), null);
});

function invitation(groupId = "1234567890", overrides = {}) {
  return {
    groupId,
    roomId: "actual-native-room-id",
    totalInvitedMembers: "1",
    seq: "1",
    eventType: 2,
    join: ["private-uid-one", "private-uid-two"],
    quit: ["private-uid-three"],
    ...overrides,
  };
}

test("group snapshots separate membership, invitation and timeout-end observations without exposing identities", () => {
  let now = 1_000;
  const observer = makeObserver(() => now);

  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership())), true);
  assert.equal(observer.observe(kernelInviteEvent(invitation())), true);
  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership("1234567890", {
    onlineCount: 0,
    memberUins: [],
    isNativeTimeout: true,
  }))), true);

  const snapshot = observer.getGroupDiagnostics("1234567890");
  assert.deepEqual(snapshot, {
    groupId: "1234567890",
    membership: {
      source: "group_audio_member_count",
      onlineCount: 0,
      memberCount: 0,
      businessType: 3,
      nativeTimeout: true,
      observedAtMs: now,
    },
    invitation: {
      source: "group_video_invite_member_update",
      roomId: "actual-native-room-id",
      totalInvitedMembers: 1,
      seq: "1",
      joinCount: 2,
      quitCount: 1,
      observedAtMs: now,
    },
    roomEndObservation: {
      source: "native_timeout_zero_members",
      nativeTimeout: true,
      onlineCount: 0,
      observedAtMs: now,
    },
    diagnosticOnly: true,
    nativeJoinVerified: false,
  });
  assert.equal(JSON.stringify(snapshot).includes("private-uin"), false);
  assert.equal(JSON.stringify(snapshot).includes("private-uid"), false);
  assert.equal(Object.isFrozen(snapshot), true);
  assert.equal(Object.isFrozen(snapshot.membership), true);
  assert.equal(Object.hasOwn(snapshot, "active"), false);
  assert.equal(Object.hasOwn(snapshot, "live"), false);
  assert.equal(Object.hasOwn(snapshot, "roomId"), false, "room ID appears only when an invitation callback supplied it");

  const invitationOnly = makeObserver(() => now);
  invitationOnly.observe(kernelInviteEvent(invitation("777")));
  const invited = invitationOnly.getGroupDiagnostics("777");
  assert.equal(invited.membership, null);
  assert.equal(invited.roomEndObservation, null);
  assert.equal(invited.nativeJoinVerified, false, "an invitation callback is not room-membership or join proof");
});

test("only exact connection generation and getter-free own data are observed", () => {
  const observer = makeObserver();
  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership(), 8)), false);
  let reads = 0;
  const event = {
    method: "onGroupAudioMemNumChange",
    args: [audioMembership("1234567890", { onlineCount: 3 })],
  };
  Object.defineProperty(event, "connectionGeneration", {
    enumerable: true,
    get() { reads += 1; throw new Error("must not run"); },
  });
  assert.equal(observer.observe(event), false);
  assert.equal(reads, 0);

  const info = audioMembership();
  Object.defineProperty(info, "onlineCount", {
    enumerable: true,
    get() { reads += 1; throw new Error("must not run"); },
  });
  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", info)), false);
  assert.equal(reads, 0);
  assert.equal(observer.getGroupDiagnostics("1234567890"), null);
});

test("captured direct invitation callback accepts bounded decimal-string counts", () => {
  const observer = makeObserver();
  const capturedShape = {
    groupId: "1234567890",
    roomId: "actual-native-room-id",
    totalInvitedMembers: "1",
    seq: "1",
    eventType: 2,
    join: ["private-uid-one"],
    quit: [],
  };
  assert.equal(observer.observe(kernelInviteEvent(capturedShape)), true);
  assert.deepEqual(observer.getGroupDiagnostics("1234567890").invitation, {
    source: "group_video_invite_member_update",
    roomId: "actual-native-room-id",
    totalInvitedMembers: 1,
    seq: "1",
    joinCount: 1,
    quitCount: 0,
    observedAtMs: 1_000,
  });
});

test("unknown and oversized shapes are ignored without persisting a partial record", () => {
  const observer = makeObserver();
  assert.equal(observer.observe(kernelEvent("OnAnUnknownGroupCallback", invitation())), false);
  assert.equal(observer.observe(kernelInviteEvent(invitation("123", {
    roomId: "r".repeat(513),
  }))), false);
  assert.equal(observer.observe(kernelInviteEvent(invitation("000123"))), false);
  assert.equal(observer.observe(kernelInviteEvent(invitation("456", { totalInvitedMembers: "1000001" }))), false);
  assert.equal(observer.observe(kernelInviteEvent(invitation("789", { seq: "1x" }))), false);
  assert.equal(observer.observe(kernelEvent("onGroupVideoInviteMemberUpdate", invitation())), false,
    "the observed invite shape has direct args[0] fields, not a notifyInfo wrapper");
  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership("123", {
    onlineCount: 1_000_001,
  }))), false);
  assert.equal(observer.getGroupDiagnostics("123"), null);
  assert.equal(observer.status().trackedGroups, 0);
});

test("category TTL expires at 30 seconds and does not let a new invitation refresh membership", () => {
  let now = 5_000;
  const observer = makeObserver(() => now);
  observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership()));
  now += 29_999;
  observer.observe(kernelInviteEvent(invitation()));
  let snapshot = observer.getGroupDiagnostics("1234567890");
  assert.ok(snapshot.membership);
  assert.ok(snapshot.invitation);

  now = 35_000;
  snapshot = observer.getGroupDiagnostics("1234567890");
  assert.equal(snapshot.membership, null, "membership expired from its own timestamp");
  assert.ok(snapshot.invitation, "fresh invitation remains within its own TTL");

  now = 65_000;
  assert.equal(observer.getGroupDiagnostics("1234567890"), null);
});

test("observer bounds tracked groups to 32 and stop clears then disables collection", () => {
  let now = 1_000;
  const observer = makeObserver(() => now);
  for (let index = 1; index <= MAX_TRACKED_GROUPS + 1; index += 1) {
    assert.equal(observer.observe(kernelEvent("onGroupVideoMemNumPushInfo", {
      group_code: String(index), onlineCount: index,
    })), true);
    now += 1;
  }
  assert.equal(observer.status().trackedGroups, MAX_TRACKED_GROUPS);
  assert.equal(observer.getGroupDiagnostics("1"), null, "oldest group is evicted at capacity");
  assert.ok(observer.getGroupDiagnostics(String(MAX_TRACKED_GROUPS + 1)));
  observer.stop();
  assert.deepEqual(observer.status(), {
    trackedGroups: 0,
    membershipEvents: 0,
    invitationEvents: 0,
    roomEndObservations: 0,
  });
  assert.equal(observer.observe(kernelEvent("onGroupAudioMemNumChange", audioMembership())), false);
  assert.equal(observer.getGroupDiagnostics("1234567890"), null);
});

function makeBridgeHarness() {
  const providerListeners = new Set();
  const hostListeners = new Set();
  let providerConnected = true;
  let attached = false;
  const provider = {
    status: () => ({ connected: providerConnected, authenticated: providerConnected, connectionGeneration: 7 }),
    subscribe(handler) { providerListeners.add(handler); return () => providerListeners.delete(handler); },
    async attach() { attached = true; return { ok: true, status: { listenerRegistered: true } }; },
    async detach() { attached = false; return { ok: true, status: { listenerRegistered: false } }; },
    async forwardAVSDKOutput() { return { ok: true, forwarded: true }; },
  };
  const host = {
    status: () => ({ available: true, hostGeneration: 9 }),
    subscribe(handler) { hostListeners.add(handler); handler({ type: "availability", available: true, hostGeneration: 9 }); return () => hostListeners.delete(handler); },
    async invoke(input) { return { posted: true, id: input.id, command: input.command }; },
  };
  const bridge = createQQCallBridge({ provider, host, connectionGeneration: 7, hostGeneration: 9, now: () => 100 });
  return {
    bridge,
    async emitKernel(method, info, generation = 7) {
      const event = method.toLowerCase() === "ongroupvideoinvitememberupdate"
        ? kernelInviteEvent(info, generation)
        : kernelEvent(method, info, generation);
      await Promise.all([...providerListeners].map((listener) => listener("kernel_event", event)));
    },
    async loseProvider() {
      providerConnected = false;
      await Promise.all([...providerListeners].map((listener) => listener("broker_status", {
        connected: false, authenticated: false, connectionGeneration: 7,
      })));
    },
    async loseHost() {
      await Promise.all([...hostListeners].map((listener) => listener({
        type: "availability", available: false, hostGeneration: 9,
      })));
    },
    async invalidateController() {
      await Promise.all([...hostListeners].map((listener) => listener({
        type: "output",
        hostGeneration: 9,
        output: { command: 20001, id: 1, value: [], hostGeneration: 8 },
      })));
    },
    isAttached: () => attached,
  };
}

test("Bridge exposes private snapshots, status only counts, rejects stale generations and clears on disconnect/stop", async (t) => {
  const providerLoss = makeBridgeHarness();
  await providerLoss.bridge.attach();
  assert.equal(providerLoss.isAttached(), true);
  await providerLoss.emitKernel("onGroupVideoInviteMemberUpdate", invitation());
  await providerLoss.emitKernel("onGroupAudioMemNumChange", audioMembership(), 8);
  assert.ok(providerLoss.bridge.getGroupDiagnostics("1234567890"));
  const serializedStatus = JSON.stringify(providerLoss.bridge.status());
  assert.match(serializedStatus, /groupDiagnosticCounts/);
  assert.doesNotMatch(serializedStatus, /1234567890|private-uid|actual-native-room-id/);
  await providerLoss.loseProvider();
  assert.equal(providerLoss.bridge.getGroupDiagnostics("1234567890"), null);
  await providerLoss.emitKernel("onGroupVideoInviteMemberUpdate", invitation(), 7);
  assert.equal(providerLoss.bridge.getGroupDiagnostics("1234567890"), null,
    "late same-generation callbacks cannot refill diagnostics after provider invalidation");
  assert.equal(providerLoss.bridge.status().counts.groupDiagnosticCounts.trackedGroups, 0);
  await providerLoss.bridge.stop();

  const hostLoss = makeBridgeHarness();
  await hostLoss.bridge.attach();
  await hostLoss.emitKernel("onGroupAudioMemNumChange", audioMembership());
  assert.ok(hostLoss.bridge.getGroupDiagnostics("1234567890"));
  await hostLoss.loseHost();
  assert.equal(hostLoss.bridge.getGroupDiagnostics("1234567890"), null);
  await hostLoss.emitKernel("onGroupAudioMemNumChange", audioMembership(), 7);
  assert.equal(hostLoss.bridge.getGroupDiagnostics("1234567890"), null,
    "late same-generation callbacks cannot refill diagnostics after Host invalidation");
  await hostLoss.bridge.stop();

  const controllerLoss = makeBridgeHarness();
  await controllerLoss.bridge.attach();
  await controllerLoss.emitKernel("onGroupAudioMemNumChange", audioMembership());
  assert.ok(controllerLoss.bridge.getGroupDiagnostics("1234567890"));
  await assert.rejects(controllerLoss.invalidateController(), { code: "stale_host_generation" });
  assert.equal(controllerLoss.bridge.status().controllerInvalidated, true);
  assert.equal(controllerLoss.bridge.getGroupDiagnostics("1234567890"), null);
  await controllerLoss.emitKernel("onGroupAudioMemNumChange", audioMembership(), 7);
  assert.equal(controllerLoss.bridge.getGroupDiagnostics("1234567890"), null,
    "late same-generation callbacks cannot refill diagnostics after controller invalidation");
  await controllerLoss.bridge.stop();

  const stopped = makeBridgeHarness();
  await stopped.bridge.attach();
  await stopped.emitKernel("onGroupAudioMemNumChange", audioMembership());
  assert.ok(stopped.bridge.getGroupDiagnostics("1234567890"));
  await stopped.bridge.stop();
  assert.equal(stopped.bridge.getGroupDiagnostics("1234567890"), null);
  assert.equal(stopped.bridge.status().counts.groupDiagnosticCounts.trackedGroups, 0);
});
