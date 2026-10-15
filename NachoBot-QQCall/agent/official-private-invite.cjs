"use strict";

const { randomBytes } = require("node:crypto");
const { exactAVRoute } = require("./official-av-observer.cjs");
const { PRIVATE_INVITE_PROFILE, PINNED_WRAPPER_SHA256 } = require("../shared/private-invite-scope.cjs");

const UID = /^u_[A-Za-z0-9_-]{22}$/;
const EVIDENCE_KEYS = ["profile", "wrapperSha256", "callbackSequence", "listenerRegistrationSequence", "inviteType", "relationId", "fromUid"];
function own(value, key) {
  try { const d = Object.getOwnPropertyDescriptor(value, key); return d && Object.hasOwn(d, "value") ? d.value : undefined; }
  catch { return undefined; }
}
function exact(value, keys) {
  try { return Boolean(value && Object.getPrototypeOf(value) === Object.prototype &&
    Reflect.ownKeys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key) &&
      Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"))); } catch { return false; }
}
function dense(value, length) {
  try { return Array.isArray(value) && own(value, "length") === length && Reflect.ownKeys(value).length === length + 1 &&
    Array.from({ length }, (_, i) => Object.getOwnPropertyDescriptor(value, String(i))).every(d => d && Object.hasOwn(d, "value")); }
  catch { return false; }
}
function validEvidence(value) {
  return exact(value, EVIDENCE_KEYS) && value.profile === PRIVATE_INVITE_PROFILE && value.wrapperSha256 === PINNED_WRAPPER_SHA256 &&
    value.inviteType === 1 && Number.isSafeInteger(value.callbackSequence) && value.callbackSequence > 0 &&
    Number.isSafeInteger(value.listenerRegistrationSequence) && value.listenerRegistrationSequence > 0 &&
    typeof value.relationId === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value.relationId) &&
    (value.fromUid === "" || typeof value.fromUid === "string" && UID.test(value.fromUid));
}
function sameEvidence(left, right) {
  return validEvidence(left) && validEvidence(right) && EVIDENCE_KEYS.every(key => left[key] === right[key]);
}
function isLifecycleResult(value) {
  if (!exact(value, ["confirmed", "code"]) || typeof value.confirmed !== "boolean") return false;
  if (value.confirmed) return ["official_private_invite_document_ended", "official_private_invite_receiving_closed", "private_invite_lifecycle_retired"].includes(value.code);
  return ["invalid_private_invite_lifecycle_request", "private_invite_lifecycle_stale", "private_invite_lifecycle_busy",
    "private_invite_window_unproven", "official_private_invite_end_unconfirmed", "official_private_invite_unavailable"].includes(value.code);
}
function validRequest(body) {
  if (!exact(body, ["operation", "evidence", "peerUid", "selfUid", "selfUin", "inviteParams"]) ||
      !["reject", "accepted"].includes(body.operation) || !validEvidence(body.evidence) ||
      typeof body.peerUid !== "string" || !UID.test(body.peerUid) || typeof body.selfUid !== "string" || !UID.test(body.selfUid) ||
      typeof body.selfUin !== "string" || !/^[1-9][0-9]{0,19}$/.test(body.selfUin) ||
      body.peerUid === body.selfUid || body.evidence.fromUid && body.evidence.fromUid !== body.peerUid ||
      !dense(body.inviteParams, 7)) return false;
  const p = body.inviteParams;
  return p[0] === 1 && p[1] === body.peerUid && dense(p[2], 1) && p[2][0] === body.peerUid &&
    Number.isInteger(p[3]) && p[3] >= -2147483648 && p[3] <= 2147483647 &&
    typeof p[4] === "string" && p[4].length > 0 && p[4].length <= 512 &&
    typeof p[5] === "boolean" && typeof p[6] === "string" && p[6].length <= 512;
}

// A separate production listener: diagnostic reports never authorize this path.
// All captured identifiers remain transient inside the Agent and renderer.
function rendererOperation(op, key, epoch, deadline, expected) {
  const read = (object, name) => {
    try { const d = Object.getOwnPropertyDescriptor(object, name); return d && Object.hasOwn(d, "value") ? d.value : undefined; }
    catch { return undefined; }
  };
  const array = (value, n) => {
    try { return Array.isArray(value) && read(value, "length") === n && Reflect.ownKeys(value).length === n + 1 &&
      Array.from({ length: n }, (_, i) => Object.getOwnPropertyDescriptor(value, String(i))).every(d => d && Object.hasOwn(d, "value")); }
    catch { return false; }
  };
  const uid = value => typeof value === "string" && /^u_[A-Za-z0-9_-]{22}$/.test(value);
  let state = read(window, key);
  const cancelledKey = `${key}_cancelled`;
  let canonical = false;
  try {
    const href = window.location.href, base = href.split(/[?#]/, 1)[0];
    canonical = href.slice(href.indexOf("#") + 1) === "/av" && (base === "app://./renderer/index.html" ||
      base.toLowerCase() === "file:///c:/program%20files/tencent/qqnt/versions/9.9.26-44343/resources/app/application.asar/renderer/index.html");
  } catch { /* a changed/unknown renderer can only clean its own old state */ }
  if (op === "stop") {
    if (canonical || state) {
      try { Object.defineProperty(window, cancelledKey, { configurable: true, value: Math.max(read(window, cancelledKey) || 0, epoch) }); } catch { /* short lease remains */ }
      if (state?.epoch <= epoch) state.cleanup();
    }
    return null;
  }
  if (!canonical) return false;
  if (op === "install") {
    if (Date.now() >= deadline || read(window, cancelledKey) >= epoch) return false;
    if (state) {
      if (state.epoch < epoch && typeof state.cleanup === "function") state.cleanup();
      else return false;
    }
    const embeds = document.querySelectorAll('embed[type="application/x-ppapi-avSDK"]');
    if (embeds.length !== 1 || typeof embeds[0].postMessage !== "function") return false;
    const embed = embeds[0];
    state = { epoch, revision: 0, invite: null, blocked: false, interacted: false, used: false,
      deadline, timer: null, alive: true, rejectAckCount: 0, rejectAckSucceeded: false };
    const incoming = event => {
      if (!state.alive || Date.now() >= state.deadline) { state.cleanup(); return; }
      let value;
      try { value = event.data; } catch { state.blocked = true; return; }
      if (typeof value === "string") {
        if (value.length > 8192) return;
        try { value = JSON.parse(value); } catch { return; }
      }
      const c1 = read(value, "cmd"), c2 = read(value, "command");
      if (c1 !== undefined && c2 !== undefined && c1 !== c2) { state.blocked = true; return; }
      const rawCommand = c1 === undefined ? c2 : c1;
      const command = typeof rawCommand === "string" && /^[0-9]{1,6}$/.test(rawCommand) ? Number(rawCommand) : rawCommand;
      if (command === 9 && state.used) {
        const p1 = read(value, "param"), p2 = read(value, "value");
        const raw = p1 === undefined ? p2 : p1;
        state.rejectAckCount += 1;
        if (p1 !== undefined && p2 !== undefined && p1 !== p2 || state.rejectAckCount !== 1 ||
            !array(raw, 2) || read(raw, "0") !== 0 || read(raw, "1") !== "") {
          state.blocked = true; return;
        }
        state.rejectAckSucceeded = true;
        return;
      }
      if ([4, 5, 8, 9, 20004, 20011, 20017].includes(command)) { state.blocked = true; return; }
      if (command !== 20006) return;
      state.revision += 1;
      const p1 = read(value, "param"), p2 = read(value, "value");
      if (p1 !== undefined && p2 !== undefined && p1 !== p2) { state.blocked = true; return; }
      const raw = p1 === undefined ? p2 : p1;
      if (!array(raw, 12)) { state.blocked = true; return; }
      const p = Array.from({ length: 12 }, (_, i) => read(raw, String(i)));
      const targetLength = read(p[2], "length");
      const targets = Number.isSafeInteger(targetLength) && targetLength >= 1 && targetLength <= 16 && array(p[2], targetLength)
        ? Array.from({ length: targetLength }, (_, i) => read(p[2], String(i))) : null;
      if (state.revision !== 1 || p[0] !== 1 || !uid(p[1]) || !targets ||
          !targets.every(value => uid(value) || typeof value === "string" && /^[1-9][0-9]{0,19}$/.test(value)) ||
          !Number.isInteger(p[3]) || p[3] < -2147483648 || p[3] > 2147483647 ||
          typeof p[4] !== "string" || !p[4] || p[4].length > 512 ||
          ![true, false, 0, 1].includes(p[10]) || typeof p[11] !== "string" || p[11].length > 512) {
        state.blocked = true; return;
      }
      state.invite = { params: [1, p[1], [p[1]], p[3], p[4], Boolean(p[10]), p[11]], targets };
    };
    const input = event => { if (event.isTrusted === true) state.interacted = true; };
    const pagehide = () => state.cleanup();
    state.cleanup = () => {
      state.alive = false;
      try { clearTimeout(state.timer); } catch { /* disabled state remains inert */ }
      try { embed.removeEventListener("message", incoming); } catch { /* never break QQ's other listeners */ }
      for (const name of ["pointerdown", "mousedown", "touchstart", "keydown"]) {
        try { window.removeEventListener(name, input, true); } catch { /* owned listener is inert */ }
      }
      try { window.removeEventListener("pagehide", pagehide); } catch { /* owned listener is inert */ }
      try { if (read(window, key) === state) delete window[key]; } catch { /* do not delete replacement ownership */ }
      state.invite = null;
    };
    state.refresh = value => {
      clearTimeout(state.timer); state.deadline = value;
      state.timer = setTimeout(() => state.cleanup(), Math.max(1, value - Date.now()));
    };
    try {
      Object.defineProperty(window, key, { configurable: true, value: state });
      embed.addEventListener("message", incoming);
      for (const name of ["pointerdown", "mousedown", "touchstart", "keydown"]) window.addEventListener(name, input, true);
      window.addEventListener("pagehide", pagehide, { once: true });
      state.refresh(deadline);
      return true;
    } catch { try { state.cleanup(); } catch { /* the finite renderer lease remains the fallback */ } return false; }
  }
  if (!state || state.epoch !== epoch || !state.alive || Date.now() >= state.deadline) return null;
  if (op === "read") {
    state.refresh(deadline);
    return { revision: state.revision, invite: state.invite, blocked: state.blocked, interacted: state.interacted, used: state.used,
      rejectAckCount: state.rejectAckCount, rejectAckSucceeded: state.rejectAckSucceeded };
  }
  if (op !== "reject" || !expected || !Number.isSafeInteger(expected.deadline) || Date.now() >= expected.deadline ||
      state.used || state.blocked || state.interacted || state.revision !== 1 ||
      JSON.stringify(state.invite) !== expected.invite) return false;
  // Use the pinned QQ receiving UI's own lifecycle, including its exitAv path.
  // Distinct visible leaf labels avoid selecting a connected call's hangup.
  const elements = document.querySelectorAll("body *");
  if (elements.length > 4096) return false;
  const labels = new Map([["邀请你语音通话", []], ["接听", []], ["挂断", []]]);
  for (const element of elements) {
    if (element.children.length !== 0) continue;
    const text = element.textContent?.trim();
    if (!labels.has(text)) continue;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility === "visible") labels.get(text).push(element);
  }
  if ([...labels.values()].some(list => list.length !== 1)) return { issued: false, code: "receiving_controls_unproven",
    counts: [...labels.values()].map(list => list.length) };
  const hangupLabel = labels.get("挂断")[0], acceptLabel = labels.get("接听")[0];
  let action = typeof hangupLabel.closest === "function" ? hangupLabel.closest('button,[role="button"]') : null;
  if (action?.contains(acceptLabel)) action = null;
  // QQ places the label below the icon, outside its click handler. Resolve
  // that icon inside the smallest single-action component, not a screen point.
  for (let node = hangupLabel.parentElement, depth = 0; !action && node && depth < 4; node = node.parentElement, depth += 1) {
    if (node.contains(acceptLabel) || node.contains(labels.get("邀请你语音通话")[0])) break;
    const rect = node.getBoundingClientRect();
    if (rect.width > 160 || rect.height > 160) break;
    const icons = node.querySelectorAll("svg");
    if (icons.length !== 1) continue;
    const iconRect = icons[0].getBoundingClientRect();
    if (iconRect.width <= 0 || iconRect.height <= 0) continue;
    let target = icons[0].parentElement;
    while (target && node.contains(target) && typeof target.click !== "function") target = target.parentElement;
    if (target && node.contains(target) && target !== hangupLabel && typeof target.click === "function") action = target;
  }
  if (!action || typeof action.click !== "function") return { issued: false, code: "receiving_action_unproven", counts: [1, 1, 1] };
  state.used = true;
  action.click();
  return { issued: true, code: "clicked", counts: [1, 1, 1] };
}

function createOfficialPrivateInviteController({ processObject = process, requireFunction = require,
  isBindingCurrent = () => false, now = Date.now, runId = randomBytes(16).toString("hex"),
  pollMs = 100, operationTimeoutMs = 750 } = {}) {
  if (!/^[a-f0-9]{32}$/.test(runId)) throw new TypeError("invalid lifecycle run id");
  let started = false, stopped = false, available = false, busy = false, timer = null, electron = null;
  let current = null;
  let lastObservation = null;
  const navigationCounts = { navigationStarts: 0, inPageNavigations: 0, pairedInPageNavigations: 0 };
  const records = new Map();
  const key = `__qqcall_private_invite_${runId}`;
  const live = record => {
    try { return !record.window.isDestroyed() && !record.contents.isDestroyed() && record.window.webContents === record.contents; }
    catch { return false; }
  };
  const url = record => { try { const value = record.contents.getURL(); return typeof value === "string" && value.length < 8192 ? value : ""; } catch { return ""; } };
  const stable = (record, epoch) => live(record) && record.epoch === epoch && exactAVRoute(url(record));
  const scopeCurrent = scope => available && !stopped && scope && current === scope && !scope.retired && isBindingCurrent(scope.evidence) === true;
  const script = (op, record, expected = null) => `(${rendererOperation.toString()})(${JSON.stringify(op)},${JSON.stringify(key)},${record.epoch},${now() + 2000},${JSON.stringify(expected)})`;
  async function execute(record, op, expected = null) {
    const epoch = record.epoch;
    let timeout;
    try {
      const pending = Promise.resolve(record.contents.executeJavaScript(script(op, record, expected)));
      const result = await Promise.race([pending, new Promise(resolve => { timeout = setTimeout(() => resolve(undefined), operationTimeoutMs); })]);
      return stable(record, epoch) ? result : undefined;
    } catch { return undefined; }
    finally { clearTimeout(timeout); }
  }
  function reset(record) {
    if (live(record) && exactAVRoute(url(record))) {
      try { Promise.resolve(record.contents.executeJavaScript(script("stop", record))).catch(() => {}); } catch { /* renderer lease remains */ }
    }
    record.epoch += 1; record.armed = false; record.snapshot = null; record.scope = null; record.eligible = true;
  }
  function add(window, newlyCreated) {
    if (records.has(window)) return;
    if (records.size >= 32) { available = false; return; }
    const contents = window?.webContents;
    if (!contents || typeof contents.on !== "function" || typeof contents.executeJavaScript !== "function") return;
    const record = { window, contents, epoch: 1, armed: false, snapshot: null, scope: null, pending: false, eligible: newlyCreated,
      hides: 0, shows: 0 };
    if (!exactAVRoute(url(record))) record.eligible = true;
    let pendingInPage = null;
    const navigation = (_event, targetURL, inPlace, isMainFrame) => {
      if (isMainFrame !== true) return;
      navigationCounts.navigationStarts = Math.min(65535, navigationCounts.navigationStarts + 1);
      reset(record);
      pendingInPage = inPlace === true && exactAVRoute(targetURL) ? { targetURL, epoch: record.epoch } : null;
      // Hash navigation reuses the live document. Install at its start rather
      // than waiting for the next timer or throwing away the intervening invite.
      if (pendingInPage) void poll(record);
    };
    const inPage = (_event, targetURL, isMainFrame) => {
      if (isMainFrame !== true) return;
      navigationCounts.inPageNavigations = Math.min(65535, navigationCounts.inPageNavigations + 1);
      const paired = pendingInPage && pendingInPage.epoch === record.epoch && pendingInPage.targetURL === targetURL;
      if (paired) navigationCounts.pairedInPageNavigations = Math.min(65535, navigationCounts.pairedInPageNavigations + 1);
      pendingInPage = null;
      if (!paired) reset(record);
      void poll(record);
    };
    record.navigation = navigation; record.inPage = inPage;
    contents.on("did-start-navigation", navigation); contents.on("did-navigate-in-page", inPage);
    record.hide = () => { record.hides += 1; };
    record.show = () => { record.shows += 1; };
    window.on("hide", record.hide); window.on("show", record.show);
    record.closed = () => {
      contents.removeListener("did-start-navigation", navigation); contents.removeListener("did-navigate-in-page", inPage);
      window.removeListener("hide", record.hide); window.removeListener("show", record.show);
      records.delete(window); record.snapshot = null;
    };
    window.once("closed", record.closed);
    records.set(window, record);
  }
  async function poll(record) {
    if (!available || stopped || record.pending || !record.eligible || !live(record) || !exactAVRoute(url(record))) return;
    try { if (record.contents.isLoadingMainFrame() !== false) return; } catch { return; }
    record.pending = true;
    const epoch = record.epoch;
    try {
      if (!record.armed) {
        // Capture the scope before the renderer await. A newer Kernel invite
        // must never re-label an earlier document's already delivered invite.
        record.scope = current && !current.retired ? current : null;
        record.installing = true;
        const result = await execute(record, "install");
        record.installing = false;
        if (stopped || !stable(record, epoch)) { void execute({ ...record, epoch }, "stop"); return; }
        if (result !== true) return;
        record.armed = true;
      }
      const scope = record.scope;
      const result = await execute(record, "read");
      if (stopped || !stable(record, epoch) || scope !== record.scope) return;
      if (!exact(result, ["revision", "invite", "blocked", "interacted", "used", "rejectAckCount", "rejectAckSucceeded"]) ||
          !Number.isSafeInteger(result.revision) || result.revision < 0 ||
          !Number.isSafeInteger(result.rejectAckCount) || result.rejectAckCount < 0 ||
          [result.blocked, result.interacted, result.used, result.rejectAckSucceeded].some(value => typeof value !== "boolean")) {
        record.snapshot = null; return;
      }
      record.snapshot = { value: result, observedAt: now() };
    } finally { record.pending = false; }
  }
  function nativeInvite(evidence) {
    if (!validEvidence(evidence)) { current = null; return; }
    current = { evidence: Object.freeze({ ...evidence }), retired: false };
    for (const record of records.values()) if ((record.armed || record.installing) && !record.scope &&
      (record.installing || record.snapshot?.value.revision === 0)) record.scope = current;
  }
  function start() {
    if (started || stopped) return available;
    started = true;
    try {
      if (processObject?.type !== "browser" || !processObject?.versions?.electron || requireFunction.resolve("electron") !== "electron") return false;
      electron = requireFunction("electron");
      if (typeof electron.app?.on !== "function" || typeof electron.BrowserWindow?.getAllWindows !== "function") return false;
      const windows = electron.BrowserWindow.getAllWindows();
      if (!Array.isArray(windows) || windows.length > 32) return false;
      for (const window of windows) add(window, false);
      available = true;
      electron.app.on("browser-window-created", onCreated);
      timer = setInterval(() => { for (const record of records.values()) void poll(record); }, pollMs);
      timer.unref?.();
      return true;
    } catch { return false; }
  }
  const onCreated = (_event, window) => { if (!stopped && available) { add(window, true); const record = records.get(window); if (record) void poll(record); } };
  async function lifecycle(body) {
    if (!validRequest(body)) return { confirmed: false, code: "invalid_private_invite_lifecycle_request" };
    const scope = current;
    if (!scopeCurrent(scope) || !sameEvidence(body.evidence, scope.evidence)) return { confirmed: false, code: "private_invite_lifecycle_stale" };
    if (body.operation === "accepted") { scope.retired = true; return { confirmed: true, code: "private_invite_lifecycle_retired" }; }
    if (busy) return { confirmed: false, code: "private_invite_lifecycle_busy" };
    busy = true;
    const deadline = now() + 2500;
    try {
      for (const record of records.values()) {
        if (now() >= deadline) return { confirmed: false, code: "official_private_invite_end_unconfirmed" };
        for (let i = 0; record.pending && i < 5; i += 1) await new Promise(resolve => setTimeout(resolve, 20));
        await poll(record);
      }
      const candidates = [...records.values()].filter(record => {
        const snapshot = record.snapshot;
        const value = snapshot?.value;
        return stable(record, record.epoch) && record.scope === scope && snapshot && now() >= snapshot.observedAt && now() - snapshot.observedAt <= 500 &&
          value.revision === 1 && !value.blocked && !value.interacted && !value.used &&
          exact(value.invite, ["params", "targets"]) && Array.isArray(value.invite.targets) &&
          value.invite.targets.length <= 16 && (value.invite.targets.includes(body.selfUid) || value.invite.targets.includes(body.selfUin)) &&
          JSON.stringify(value.invite.params) === JSON.stringify(body.inviteParams);
      });
      lastObservation = { scopeWindows: [...records.values()].filter(record => record.scope === scope).length,
        capturedWindows: [...records.values()].filter(record => record.snapshot?.value.revision === 1).length,
        candidates: candidates.length, actionCode: null, labelCounts: null, documentEnded: false,
        receivingClosed: false, hiddenAfterAction: false, officialRejectAcknowledged: false };
      if (candidates.length !== 1 || !scopeCurrent(scope) || now() >= deadline) return { confirmed: false, code: "private_invite_window_unproven" };
      const record = candidates[0], epoch = record.epoch;
      const inviteJSON = JSON.stringify(record.snapshot.value.invite);
      const hides = record.hides, shows = record.shows;
      let wasVisible = false;
      try { wasVisible = record.window.isVisible() === true && record.window.isMinimized() === false; } catch { /* unknown visibility cannot authorize an action */ }
      if (!wasVisible) return { confirmed: false, code: "private_invite_window_unproven" };
      // Dispatch only after the final synchronous main-process scope check.
      if (!scopeCurrent(scope) || !stable(record, epoch)) return { confirmed: false, code: "private_invite_lifecycle_stale" };
      const operation = execute(record, "reject", { invite: inviteJSON, deadline });
      const issuedAt = now();
      const clicked = await operation;
      if (exact(clicked, ["issued", "code", "counts"]) && typeof clicked.issued === "boolean" &&
          ["clicked", "receiving_controls_unproven", "receiving_action_unproven"].includes(clicked.code) && dense(clicked.counts, 3) &&
          clicked.counts.every(value => Number.isInteger(value) && value >= 0 && value <= 4096)) {
        lastObservation.actionCode = clicked.code; lastObservation.labelCounts = clicked.counts.slice();
      }
      for (let i = 0; i < 20 && now() - issuedAt < 1500; i += 1) {
        if (!scopeCurrent(scope)) return { confirmed: false, code: "private_invite_lifecycle_stale" };
        if (now() >= deadline) break;
        if (!live(record) || record.epoch !== epoch || !exactAVRoute(url(record))) {
          scope.retired = true;
          lastObservation.documentEnded = true;
          return { confirmed: true, code: "official_private_invite_document_ended" };
        }
        if (clicked?.issued !== true) break;
        // The pinned CallStore.hangup hides a reusable AV document. Hidden
        // alone is insufficient: require the post-action official Reject ACK,
        // one actual hide transition, and unchanged renderer/native scope.
        await poll(record);
        if (!scopeCurrent(scope) || !stable(record, epoch) || record.scope !== scope) return { confirmed: false, code: "private_invite_lifecycle_stale" };
        const terminal = record.snapshot;
        const value = terminal?.value;
        let hidden = false;
        try { hidden = wasVisible && record.hides === hides + 1 && record.shows === shows &&
          record.window.isVisible() === false && record.window.isMinimized() === false; } catch { /* unknown visibility cannot complete */ }
        lastObservation.hiddenAfterAction = hidden;
        lastObservation.officialRejectAcknowledged = value?.rejectAckCount === 1 && value.rejectAckSucceeded === true;
        if (value?.blocked || value?.interacted || value?.revision !== 1) break;
        if (hidden && terminal && now() >= terminal.observedAt && now() - terminal.observedAt <= 500 &&
            value.revision === 1 && value.used === true && !value.blocked && !value.interacted &&
            value.rejectAckCount === 1 && value.rejectAckSucceeded === true &&
            JSON.stringify(value.invite) === inviteJSON && now() < deadline) {
          scope.retired = true;
          lastObservation.receivingClosed = true;
          // QQ reuses this hidden document. Rearm only after proven completion;
          // a new native invitation must acquire the fresh listener epoch.
          reset(record);
          return { confirmed: true, code: "official_private_invite_receiving_closed" };
        }
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      return { confirmed: false, code: "official_private_invite_end_unconfirmed" };
    } finally { busy = false; }
  }
  async function stop() {
    if (stopped) return;
    stopped = true; available = false; current = null; clearInterval(timer);
    try { electron?.app?.removeListener("browser-window-created", onCreated); } catch { /* resources expire in the renderer */ }
    await Promise.all([...records.values()].map(async record => {
      record.contents.removeListener("did-start-navigation", record.navigation);
      record.contents.removeListener("did-navigate-in-page", record.inPage);
      record.window.removeListener("closed", record.closed);
      record.window.removeListener("hide", record.hide); record.window.removeListener("show", record.show);
      await execute(record, "stop");
      record.snapshot = null; record.scope = null;
    }));
    records.clear();
  }
  return Object.freeze({ start, stop, nativeInvite, lifecycle, status: () => ({ available: available && !stopped,
    ...navigationCounts,
    windows: records.size, armedWindows: [...records.values()].filter(record => record.armed).length,
    capturedWindows: [...records.values()].filter(record => record.snapshot?.value.revision === 1).length,
    blockedWindows: [...records.values()].filter(record => record.snapshot?.value.blocked || record.snapshot?.value.interacted).length,
    lastObservation: lastObservation ? { ...lastObservation, labelCounts: lastObservation.labelCounts?.slice() || null } : null }) });
}

module.exports = { createOfficialPrivateInviteController, validRequest, validEvidence, sameEvidence, isLifecycleResult, rendererOperation };
