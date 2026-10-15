"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { createOfficialPrivateInviteController, validRequest, rendererOperation } = require("../agent/official-private-invite.cjs");
const { PRIVATE_INVITE_PROFILE, PINNED_WRAPPER_SHA256 } = require("../shared/private-invite-scope.cjs");
const PEER = `u_${"P".repeat(22)}`, SELF = `u_${"S".repeat(22)}`;
const AV = "app://./renderer/index.html?webcontentsid=4#/av";
const proof = (sequence = 1) => ({ profile: PRIVATE_INVITE_PROFILE, wrapperSha256: PINNED_WRAPPER_SHA256,
  callbackSequence: sequence, listenerRegistrationSequence: 1, inviteType: 1, relationId: "2146014839", fromUid: "" });
const params = [1, PEER, [PEER], 1, "0", false, "0"];
const request = (operation = "reject", sequence = 1) => ({ operation, evidence: proof(sequence), peerUid: PEER,
  selfUid: SELF, selfUin: "2955610491", inviteParams: structuredClone(params) });
const invite = (peer = PEER, targets = [SELF]) => ({ cmd: 20006, id: 0,
  param: [1, peer, targets, 1, "0", 0, "0", 0, 0, 0, 0, "0"] });
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

function runtime({ existing = false, onReject = null, missingAction = false, installGate = null, rejectGate = null,
  labels = ["邀请你语音通话", "接听", "挂断"] } = {}) {
  const app = new EventEmitter(), windows = [];
  let bindingCurrent = true, clicks = 0;
  const makeWindow = () => {
    const window = new EventEmitter(), contents = new EventEmitter(), embed = new EventEmitter();
    let destroyed = false, visible = true, minimized = false;
    embed.postMessage = () => { throw new Error("must never call the SDK method"); };
    embed.addEventListener = embed.on.bind(embed); embed.removeEventListener = embed.removeListener.bind(embed);
    const page = new EventEmitter();
    page.location = { href: AV };
    page.addEventListener = page.on.bind(page); page.removeEventListener = page.removeListener.bind(page);
    const context = vm.createContext({ window: page, Date, setTimeout, clearTimeout,
      document: { querySelectorAll(selector) {
        if (selector.startsWith("embed")) return [embed];
        if (selector === "body *") return labels.map(label => {
          const leaf = { children: [], textContent: label,
            getBoundingClientRect: () => ({ width: 20, height: 20 }), click() {} };
          if (label !== "挂断") return leaf;
          const icon = { getBoundingClientRect: () => ({ width: 40, height: 40 }) };
          const target = { click() {
            if (label !== "挂断") throw new Error("wrong lifecycle button");
            clicks += 1;
            if (onReject) onReject({ window, contents, page });
            else { destroyed = true; window.emit("closed"); }
          } };
          const component = { parentElement: null, getBoundingClientRect: () => ({ width: 40, height: 64 }),
            contains: item => [leaf, target, icon].includes(item), querySelectorAll: () => missingAction ? [] : [icon] };
          icon.parentElement = target; target.parentElement = component; leaf.parentElement = component;
          return leaf;
        });
        return [];
      } }, getComputedStyle: () => ({ display: "block", visibility: "visible" }) });
    contents.getURL = () => AV;
    contents.isLoadingMainFrame = () => false;
    contents.isDestroyed = () => destroyed;
    contents.executeJavaScript = async code => {
      if (installGate && code.includes(')("install",')) { window.test.installDispatched = true; await installGate.promise; }
      if (rejectGate && code.includes(')("reject",')) { window.test.rejectDispatched = true; await rejectGate.promise; }
      return structuredClone(vm.runInContext(code, context));
    };
    window.webContents = contents; window.isDestroyed = () => destroyed;
    window.isVisible = () => visible; window.isMinimized = () => minimized;
    window.test = { embed, page, context, get destroyed() { return destroyed; },
      hide() { visible = false; window.emit("hide"); }, show() { visible = true; window.emit("show"); },
      minimize() { minimized = true; window.emit("hide"); } };
    windows.push(window);
    return window;
  };
  const initial = existing ? makeWindow() : null;
  const requireFunction = () => ({ app, BrowserWindow: { getAllWindows: () => windows } });
  requireFunction.resolve = () => "electron";
  const controller = createOfficialPrivateInviteController({ processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction, isBindingCurrent: () => bindingCurrent, pollMs: 5 });
  const add = () => { const window = makeWindow(); app.emit("browser-window-created", {}, window); return window; };
  return { controller, initial, add, get clicks() { return clicks; }, invalidate: () => { bindingCurrent = false; } };
}
async function armed(window) {
  for (let i = 0; i < 40; i += 1) { if (window.test.embed.listenerCount("message")) return; await wait(5); }
  throw new Error("listener not installed");
}
const dispatch = (window, value) => window.test.embed.emit("message", { data: JSON.stringify(value) });

test("only an exact live invitation invokes the official receiving lifecycle and confirms its document ended", async (t) => {
  const rt = runtime(); t.after(() => rt.controller.stop());
  assert.equal(rt.controller.start(), true);
  rt.controller.nativeInvite(proof());
  const window = rt.add(); await armed(window);
  dispatch(window, invite()); await wait(15);
  assert.deepEqual(await rt.controller.lifecycle(request()), { confirmed: true, code: "official_private_invite_document_ended" });
  assert.equal(rt.clicks, 1);
  assert.equal(rt.controller.status().windows, 0, "closed windows do not exhaust the lifetime cap");
  assert.equal((await rt.controller.lifecycle(request())).confirmed, false, "the invitation is consumed once");
});

test("reusable receiving closure needs a post-action Reject ACK, hide transition and unchanged scope", async (t) => {
  for (const scenario of ["confirmed", "hide_only", "ack_only", "failed_ack", "malformed_ack", "nonempty_error", "duplicate_ack",
    "new_invite", "entry", "user_input", "reshown", "minimized", "already_hidden", "early_ack"]) {
    await t.test(scenario, async (t) => {
      const rt = runtime({ onReject: ({ window }) => {
        if (scenario !== "hide_only") dispatch(window, { cmd: 9, param: [scenario === "failed_ack" ? 1 : 0,
          scenario === "malformed_ack" ? null : scenario === "nonempty_error" ? "unexpected" : ""] });
        if (scenario === "duplicate_ack") dispatch(window, { cmd: 9, param: [0, ""] });
        if (scenario !== "ack_only") window.test.hide();
        if (scenario === "new_invite") { rt.controller.nativeInvite(proof(2)); dispatch(window, invite()); }
        if (scenario === "entry") dispatch(window, { cmd: 20004, param: [0] });
        if (scenario === "user_input") window.test.page.emit("pointerdown", { isTrusted: true });
        if (scenario === "reshown") window.test.show();
        if (scenario === "minimized") window.test.minimize();
      } });
      t.after(() => rt.controller.stop()); rt.controller.start(); rt.controller.nativeInvite(proof());
      const window = rt.add(); await armed(window); dispatch(window, invite()); await wait(15);
      if (scenario === "already_hidden") window.test.hide();
      if (scenario === "early_ack") dispatch(window, { cmd: 9, param: [0, ""] });
      const result = await rt.controller.lifecycle(request());
      assert.equal(result.confirmed, scenario === "confirmed");
      if (scenario === "confirmed") {
        assert.equal(result.code, "official_private_invite_receiving_closed");
        const observation = rt.controller.status().lastObservation;
        assert.equal(observation.documentEnded, false);
        assert.equal(observation.receivingClosed, true);
        assert.equal(observation.officialRejectAcknowledged, true);
        // The same official window can safely receive a later, new invitation.
        for (let i = 0; i < 30 && rt.controller.status().armedWindows !== 1; i += 1) await wait(5);
        rt.controller.nativeInvite(proof(2)); window.test.show();
        dispatch(window, invite()); await wait(15);
        assert.equal((await rt.controller.lifecycle(request("reject", 2))).confirmed, true);
        assert.equal(rt.clicks, 2);
      } else if (["already_hidden", "early_ack"].includes(scenario)) assert.equal(rt.clicks, 0);
    });
  }
});

test("scope, DOM, interaction and native-state negatives never invoke hangup", async (t) => {
  for (const scenario of ["existing_av", "new_invite", "entry", "accept_ack", "input", "binding_changed", "wrong_peer",
    "wrong_self", "wrong_evidence", "duplicate_window", "connected_ui", "ambiguous_button", "navigation", "renderer_route_changed", "missing_action"]) {
    await t.test(scenario, async (t) => {
      const rt = runtime({ existing: scenario === "existing_av", missingAction: scenario === "missing_action",
        labels: scenario === "connected_ui" ? ["挂断"] : scenario === "ambiguous_button" ? ["邀请你语音通话", "接听", "挂断", "挂断"] : undefined });
      t.after(() => rt.controller.stop()); rt.controller.start(); rt.controller.nativeInvite(proof());
      const window = rt.initial || rt.add();
      if (scenario !== "existing_av") {
        await armed(window); dispatch(window, invite());
        if (scenario === "new_invite") { rt.controller.nativeInvite(proof(2)); dispatch(window, invite()); }
        if (scenario === "entry") dispatch(window, { cmd: 20004, param: [0] });
        if (scenario === "accept_ack") dispatch(window, { cmd: 5, param: [0, ""] });
        if (scenario === "input") window.test.page.emit("pointerdown", { isTrusted: true });
        if (scenario === "navigation") window.webContents.emit("did-start-navigation", {}, AV, false, true);
        if (scenario === "renderer_route_changed") window.test.page.location.href = "https://unrelated.invalid/#/av";
        if (scenario === "duplicate_window") { const other = rt.add(); await armed(other); dispatch(other, invite()); }
      }
      if (scenario === "binding_changed") rt.invalidate();
      const body = request();
      if (scenario === "wrong_peer") { body.peerUid = `u_${"Q".repeat(22)}`; body.inviteParams[1] = body.peerUid; body.inviteParams[2] = [body.peerUid]; }
      if (scenario === "wrong_self") body.selfUid = `u_${"Q".repeat(22)}`;
      if (scenario === "wrong_evidence") body.evidence.callbackSequence += 10;
      await wait(20);
      assert.equal((await rt.controller.lifecycle(body)).confirmed, false);
      assert.equal(rt.clicks, 0);
    });
  }
});

test("accepted scopes retire without UI actions and a fresh document can serve the next invite", async (t) => {
  const rt = runtime(); t.after(() => rt.controller.stop()); rt.controller.start(); rt.controller.nativeInvite(proof());
  const first = rt.add(); await armed(first); dispatch(first, invite());
  assert.equal((await rt.controller.lifecycle(request("accepted"))).confirmed, true);
  assert.equal(rt.clicks, 0);
  rt.controller.nativeInvite(proof(2));
  const second = rt.add(); await armed(second); dispatch(second, invite()); await wait(15);
  assert.equal((await rt.controller.lifecycle(request("reject", 2))).confirmed, true);
  assert.equal(rt.clicks, 1);
});

test("a failed official lifecycle never claims completion and stop removes owned listeners", async (t) => {
  const rt = runtime({ onReject() {} }); t.after(() => rt.controller.stop());
  rt.controller.start(); rt.controller.nativeInvite(proof());
  const window = rt.add(); await armed(window); dispatch(window, invite()); await wait(15);
  assert.equal((await rt.controller.lifecycle(request())).confirmed, false);
  assert.equal(rt.clicks, 1);
  await rt.controller.stop();
  assert.equal(window.test.embed.listenerCount("message"), 0);
  assert.equal((await rt.controller.lifecycle(request())).confirmed, false);
});

test("in-place document reuse removes the old capture before a new invitation is armed", async (t) => {
  const rt = runtime(); t.after(() => rt.controller.stop()); rt.controller.start(); rt.controller.nativeInvite(proof());
  const window = rt.add(); await armed(window); dispatch(window, invite());
  assert.equal((await rt.controller.lifecycle(request("accepted"))).confirmed, true);
  window.webContents.emit("did-start-navigation", {}, AV, true, true);
  window.webContents.emit("did-navigate-in-page", {}, AV, true);
  rt.controller.nativeInvite(proof(2));
  await wait(25); await armed(window); dispatch(window, invite()); await wait(15);
  assert.equal(window.test.embed.listenerCount("message"), 1);
  assert.equal((await rt.controller.lifecycle(request("reject", 2))).confirmed, true);
  assert.equal(rt.clicks, 1);
});

test("an invite between paired in-page navigation notifications survives the completion notification", async (t) => {
  const rt = runtime(); t.after(() => rt.controller.stop()); rt.controller.start(); rt.controller.nativeInvite(proof());
  const window = rt.add(); await armed(window); dispatch(window, invite());
  assert.equal((await rt.controller.lifecycle(request("accepted"))).confirmed, true);
  window.webContents.emit("did-start-navigation", {}, AV, true, true);
  rt.controller.nativeInvite(proof(2)); await armed(window);
  dispatch(window, invite()); await wait(15);
  window.webContents.emit("did-navigate-in-page", {}, AV, true);
  await wait(20);
  assert.equal((await rt.controller.lifecycle(request("reject", 2))).confirmed, true);
  assert.equal(rt.clicks, 1);
});

test("stop tombstones a delayed installation so it cannot resurrect a listener", async () => {
  let resolve;
  const gate = { promise: new Promise(done => { resolve = done; }) };
  const rt = runtime({ installGate: gate }); rt.controller.start(); rt.controller.nativeInvite(proof());
  const window = rt.add();
  assert.equal(window.test.installDispatched, true);
  await rt.controller.stop(); resolve(); await wait(30);
  assert.equal(window.test.embed.listenerCount("message"), 0);
  assert.equal(rt.clicks, 0);
});

test("an issued renderer action cannot run after the operation deadline even while reads renew its lease", async (t) => {
  let resolve;
  const gate = { promise: new Promise(done => { resolve = done; }) };
  const rt = runtime({ rejectGate: gate }); t.after(() => { resolve(); return rt.controller.stop(); });
  rt.controller.start(); rt.controller.nativeInvite(proof());
  const window = rt.add(); await armed(window); dispatch(window, invite()); await wait(15);
  const started = Date.now();
  assert.equal((await rt.controller.lifecycle(request())).confirmed, false);
  assert.equal(window.test.rejectDispatched, true);
  await wait(Math.max(0, 2600 - (Date.now() - started)));
  resolve(); await wait(30);
  assert.equal(rt.clicks, 0);
});

test("request validation rejects extras and getters without reading them", () => {
  assert.equal(validRequest(request()), true);
  assert.equal(validRequest({ ...request(), selector: "arbitrary" }), false);
  let reads = 0;
  const body = request();
  Object.defineProperty(body, "peerUid", { enumerable: true, get() { reads += 1; return PEER; } });
  assert.equal(validRequest(body), false); assert.equal(reads, 0);
  assert.equal(typeof rendererOperation, "function");
});
