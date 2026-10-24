"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const http = require("node:http");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { createHostTransport, LOOPBACK } = require("./transport.cjs");
const { copyGroupRoomQueryObservation } = require("./group-room-info.cjs");
const { codedError, resolvePluginPath, samePath } = require("./config.cjs");
const { rendererGoneDocument } = require("./status-report.cjs");

const MIME_TYPE = "application/x-ppapi-avSDK";
const READY_CHANNEL = "nachobot-qqcall-host:ready";
const OUTPUT_CHANNEL = "nachobot-qqcall-host:output";
const FAILURE_CHANNEL = "nachobot-qqcall-host:failure";
const HOST_HTML = path.resolve(__dirname, "host.html");
const HOST_PRELOAD = path.resolve(__dirname, "preload.cjs");
const READY_TIMEOUT_MS = 15000;
const CALLBACK_READY_PATH = "/v1/callback-ready";
const CALLBACK_READY_BODY_LIMIT = 4096;
const VERIFIED_AUDIO_DEVICE_PLUGIN_SHA256 = "757C48DDC0B5FDFF101C36BF309BF9F79FE8618A64FBF0D059347AD3834A9DBF";
const MAX_PLUGIN_HASH_BYTES = 256 * 1024 * 1024;
const MAX_GROUP_INVITE_LIST_ENTRIES = 1000;
const MAX_GROUP_INVITE_SEQUENCE_STRING_LENGTH = 8192;

function sameOpenedPath(opened, named) {
  if (!named.isFile() || named.isSymbolicLink()) return false;
  if (Number.isFinite(opened.dev) && Number.isFinite(opened.ino) && opened.ino !== 0 &&
      Number.isFinite(named.dev) && Number.isFinite(named.ino) && named.ino !== 0) {
    return opened.dev === named.dev && opened.ino === named.ino;
  }
  return opened.size === named.size && opened.mtimeMs === named.mtimeMs;
}

function sameOpenedState(before, after) {
  return after.isFile() && before.size === after.size && before.mtimeMs === after.mtimeMs &&
    (before.ctimeMs === undefined || after.ctimeMs === undefined || before.ctimeMs === after.ctimeMs);
}

function openReadOnlyNoFollow(filePath) {
  const constants = fs.constants;
  let flags = constants.O_RDONLY;
  if (Number.isInteger(constants.O_NOFOLLOW)) flags |= constants.O_NOFOLLOW;
  if (Number.isInteger(constants.O_NONBLOCK)) flags |= constants.O_NONBLOCK;
  return fs.openSync(filePath, flags);
}

function matchesVerifiedAudioDevicePluginDigest(digest, platform = process.platform) {
  return platform === "win32" && typeof digest === "string" &&
    /^[a-f0-9]{64}$/i.test(digest) && digest.toUpperCase() === VERIFIED_AUDIO_DEVICE_PLUGIN_SHA256;
}

function pluginHasVerifiedAudioDeviceProtocol(pluginPath, platform = process.platform) {
  if (platform !== "win32" || typeof pluginPath !== "string" || !path.isAbsolute(pluginPath)) return false;
  let descriptor;
  try {
    descriptor = openReadOnlyNoFollow(pluginPath);
    const openedStats = fs.fstatSync(descriptor);
    if (!openedStats.isFile() || !Number.isSafeInteger(openedStats.size) ||
        openedStats.size < 0 || openedStats.size > MAX_PLUGIN_HASH_BYTES ||
        !sameOpenedPath(openedStats, fs.lstatSync(pluginPath))) return false;
    const hash = crypto.createHash("sha256");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    let totalBytes = 0;
    let read;
    while ((read = fs.readSync(descriptor, buffer, 0,
      Math.min(buffer.byteLength, MAX_PLUGIN_HASH_BYTES + 1 - totalBytes), totalBytes)) > 0) {
      totalBytes += read;
      if (totalBytes > MAX_PLUGIN_HASH_BYTES) return false;
      hash.update(buffer.subarray(0, read));
    }
    const finishedStats = fs.fstatSync(descriptor);
    if (!sameOpenedState(openedStats, finishedStats) ||
        !sameOpenedPath(finishedStats, fs.lstatSync(pluginPath))) return false;
    return matchesVerifiedAudioDevicePluginDigest(hash.digest("hex"), platform);
  } catch {
    return false;
  } finally {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* A hash failure stays a disabled feature. */ }
    }
  }
}

function pluginSwitchMatches(value, pluginPath, platform = process.platform) {
  if (typeof value !== "string" || !value) return false;
  return value.split(",").some((entry) => {
    const separator = entry.lastIndexOf(";");
    if (separator < 1) return false;
    return entry.slice(separator + 1) === MIME_TYPE && samePath(entry.slice(0, separator), pluginPath, platform);
  });
}

function configureHost({ app, config, platform = process.platform } = {}) {
  if (!app || !app.commandLine) throw codedError("electron_app_unavailable");
  const resolved = resolvePluginPath(config?.appPath, platform);
  const commandLine = app.commandLine;
  const isReady = typeof app.isReady === "function" && app.isReady();
  const hasAllowPlugins = typeof commandLine.hasSwitch === "function" && commandLine.hasSwitch("allow-command-line-plugins");
  const pluginSwitchValue = typeof commandLine.getSwitchValue === "function"
    ? commandLine.getSwitchValue("register-pepper-plugins")
    : "";
  const hasPlugin = pluginSwitchMatches(pluginSwitchValue, resolved.pluginPath, platform);

  if (isReady) {
    if (!hasAllowPlugins || !hasPlugin) throw codedError("unsupported_startup");
    let userDataPath;
    try { userDataPath = app.getPath("userData"); }
    catch { throw codedError("host_profile_unavailable"); }
    if (!samePath(userDataPath, config.profilePath, platform)) throw codedError("host_profile_mismatch");
    return resolved;
  }

  try {
    fs.mkdirSync(config.profilePath, { recursive: true, mode: 0o700 });
    if (!hasAllowPlugins) commandLine.appendSwitch("allow-command-line-plugins");
    if (!hasPlugin) {
      if (pluginSwitchValue) throw codedError("plugin_switch_conflict");
      commandLine.appendSwitch("register-pepper-plugins", `${resolved.pluginPath};${MIME_TYPE}`);
    }
    app.setPath("userData", config.profilePath);
  } catch (error) {
    if (error?.code) throw error;
    throw codedError("host_configuration_failed");
  }
  return resolved;
}

function safeStatusCode(code) {
  return typeof code === "string" && /^[a-z][a-z0-9_]{1,63}$/.test(code) ? code : "host_failure";
}

function copyGroupNativeObservation(value) {
  if (!value || typeof value !== "object") return null;
  const isInt32 = (candidate) => Number.isInteger(candidate) && candidate >= -0x80000000 && candidate <= 0x7fffffff;
  const nativeCommandCounts = Object.create(null);
  for (const [command, count] of Object.entries(value.nativeCommandCounts || {}).slice(0, 16)) {
    if (!/^[1-9][0-9]{0,9}$/.test(command) || Number(command) > 0x7fffffff ||
        !Number.isSafeInteger(count) || count < 0) continue;
    nativeCommandCounts[command] = Math.min(count, 1000);
  }
  const reasonKeys = [
    "invalid_parameters", "permission_denied", "not_in_group", "group_not_found", "network_error",
    "login_required", "unsupported", "room_error", "timeout", "unknown",
  ];
  return {
    startAck4CorrelatedSeen: value.startAck4CorrelatedSeen === true,
    startAck4ReturnCode: isInt32(value.startAck4ReturnCode) ? value.startAck4ReturnCode : null,
    startAck4IdZeroSeen: value.startAck4IdZeroSeen === true,
    startAck4IdZeroUntrustedReturnCode: isInt32(value.startAck4IdZeroUntrustedReturnCode)
      ? value.startAck4IdZeroUntrustedReturnCode : null,
    startAck4OtherIdSeen: value.startAck4OtherIdSeen === true,
    startAck4OtherIdUntrustedReturnCode: isInt32(value.startAck4OtherIdUntrustedReturnCode)
      ? value.startAck4OtherIdUntrustedReturnCode : null,
    groupEnter20004Seen: value.groupEnter20004Seen === true,
    groupEnter20004Matched: value.groupEnter20004Matched === true,
    groupEnter20004ReturnCode: isInt32(value.groupEnter20004ReturnCode) ? value.groupEnter20004ReturnCode : null,
    groupEnter20004Creator: ["true", "false", "unknown"].includes(value.groupEnter20004Creator)
      ? value.groupEnter20004Creator : "unknown",
    error20003Seen: value.error20003Seen === true,
    error20003Matched: value.error20003Matched === true,
    error20003MessageLength: Number.isSafeInteger(value.error20003MessageLength) &&
      value.error20003MessageLength >= 0 && value.error20003MessageLength <= 8192
      ? value.error20003MessageLength : null,
    error20003MessageSha256: typeof value.error20003MessageSha256 === "string" &&
      /^[a-f0-9]{64}$/.test(value.error20003MessageSha256) ? value.error20003MessageSha256 : null,
    error20003ReasonFlags: Object.fromEntries(reasonKeys.map((key) =>
      [key, value.error20003ReasonFlags?.[key] === true])),
    error20003CodeLabel: ["error", "ret", "errno"].includes(value.error20003CodeLabel) ? value.error20003CodeLabel : null,
    error20003Code: isInt32(value.error20003Code) ? value.error20003Code : null,
    timeout20005Observed: value.timeout20005Observed === true,
    nativeCommandCounts,
  };
}

function copyGroupInviteObservation(value) {
  if (!value || typeof value !== "object") return null;
  const subBusinessType = Number.isInteger(value.subBusinessType) &&
    value.subBusinessType >= -0x80000000 && value.subBusinessType <= 0x7fffffff
    ? value.subBusinessType : null;
  return {
    seen: value.seen === true,
    tupleLength: value.seen === true && value.tupleLength === 12 ? 12 : null,
    relationMatchesFreshRoom: value.relationMatchesFreshRoom === true,
    inviterKind: ["uid_pattern", "uin_pattern", "other"].includes(value.inviterKind) ? value.inviterKind : "other",
    inviteListCount: Number.isSafeInteger(value.inviteListCount) && value.inviteListCount >= 0 &&
      value.inviteListCount <= MAX_GROUP_INVITE_LIST_ENTRIES ? value.inviteListCount : null,
    subBusinessType,
    showUiFlagKind: ["boolean", "zero_one", "other"].includes(value.showUiFlagKind) ? value.showUiFlagKind : "other",
    sequenceStringLength: Number.isSafeInteger(value.sequenceStringLength) && value.sequenceStringLength >= 0 &&
      value.sequenceStringLength <= MAX_GROUP_INVITE_SEQUENCE_STRING_LENGTH ? value.sequenceStringLength : null,
  };
}

function callbackListenerReady(responseStatus, payload, expectedGeneration) {
  return responseStatus === 200 && payload?.ok === true && payload?.callbackReady === true &&
    payload?.hostGeneration === expectedGeneration && payload?.started === true &&
    payload?.subscriberReady === true && payload?.stopping === false && payload?.stopped === false;
}

function probeOutputCallback(config, timeoutMs, signal) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    let incoming = null;
    const request = http.request({
      host: "127.0.0.1",
      port: config.outputPort,
      method: "GET",
      path: CALLBACK_READY_PATH,
      headers: {
        authorization: `Bearer ${config.ipcToken}`,
        "x-qqcall-host-generation": String(config.hostGeneration),
        accept: "application/json",
      },
    }, (response) => {
      incoming = response;
      let size = 0;
      const chunks = [];
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > CALLBACK_READY_BODY_LIMIT) {
          finish(false);
          response.destroy();
          request.destroy();
          return;
        }
        chunks.push(chunk);
      });
      response.once("end", () => {
        let payload;
        try { payload = JSON.parse(Buffer.concat(chunks, size).toString("utf8")); }
        catch { finish(false); return; }
        finish(callbackListenerReady(response.statusCode, payload, config.hostGeneration));
      });
      response.once("aborted", () => finish(false));
      response.once("error", () => finish(false));
    });
    const onAbort = () => {
      finish(false);
      request.destroy();
    };
    function finish(ready) {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      if (incoming && !incoming.complete) incoming.destroy();
      resolve(ready);
    }
    timer = setTimeout(() => {
      finish(false);
      request.destroy();
    }, timeoutMs);
    timer.unref?.();
    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener("abort", onAbort, { once: true });
    request.once("error", () => finish(false));
    request.end();
  });
}

function waitForOutputCallback(config, { timeoutMs, requestTimeoutMs, pollIntervalMs, signal }) {
  const deadline = Date.now() + timeoutMs;
  return (async () => {
    while (Date.now() < deadline) {
      if (signal?.aborted) throw codedError("host_stopped");
      const remaining = deadline - Date.now();
      const probeTimeout = Math.max(1, Math.min(requestTimeoutMs, remaining));
      if (await probeOutputCallback(config, probeTimeout, signal)) return true;
      if (signal?.aborted) throw codedError("host_stopped");
      const delayMs = Math.min(pollIntervalMs, Math.max(0, deadline - Date.now()));
      if (delayMs > 0) {
        await new Promise((resolve) => {
          const finishDelay = () => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", onAbort);
            resolve();
          };
          const onAbort = () => finishDelay();
          const timer = setTimeout(finishDelay, delayMs);
          signal?.addEventListener("abort", onAbort, { once: true });
          timer.unref?.();
        });
      }
    }
    if (signal?.aborted) throw codedError("host_stopped");
    throw codedError("output_callback_not_ready");
  })();
}

function createElectronHost({
  electron,
  config,
  onFailure = () => {},
  onStatus = () => {},
  onRendererGone = () => {},
  onPrivateEndTrace = () => {},
  readyTimeoutMs = READY_TIMEOUT_MS,
  callbackReadyTimeoutMs = 45_000,
  callbackReadyRequestTimeoutMs = 1500,
  callbackReadyPollMs = 250,
} = {}) {
  if (!electron?.app || !electron?.BrowserWindow || !electron?.ipcMain) throw codedError("electron_runtime_unavailable");
  if (typeof onFailure !== "function") throw codedError("failure_hook_invalid");
  if (typeof onStatus !== "function") throw codedError("status_hook_invalid");
  if (typeof onRendererGone !== "function") throw codedError("renderer_gone_hook_invalid");
  if (typeof onPrivateEndTrace !== "function") throw codedError("private_end_trace_hook_invalid");
  if (!Number.isSafeInteger(readyTimeoutMs) || readyTimeoutMs < 1000 || readyTimeoutMs > 120000) throw codedError("ready_timeout_invalid");
  if (!Number.isSafeInteger(callbackReadyTimeoutMs) || callbackReadyTimeoutMs < 1000 || callbackReadyTimeoutMs > 60_000) {
    throw codedError("callback_ready_timeout_invalid");
  }
  if (!Number.isSafeInteger(callbackReadyRequestTimeoutMs) || callbackReadyRequestTimeoutMs < 100 || callbackReadyRequestTimeoutMs > 10_000) {
    throw codedError("callback_ready_request_timeout_invalid");
  }
  if (!Number.isSafeInteger(callbackReadyPollMs) || callbackReadyPollMs < 10 || callbackReadyPollMs > 5000) {
    throw codedError("callback_ready_poll_invalid");
  }

  const { app, BrowserWindow, ipcMain } = electron;
  const htmlUrl = pathToFileURL(HOST_HTML).href;
  let window = null;
  let transport = null;
  let startPromise = null;
  let stopPromise = null;
  let internalClose = false;
  let rendererGone = false;
  let rendererGoneCode = null;
  let rendererGoneObserved = false;
  let cachedRendererPid = null;
  let rendererClosed = false;
  let pluginFound = false;
  let hasPostMessage = false;
  let callbackReady = false;
  let callbackWaitController = null;
  let lastFailureCode = null;
  let readinessWaiter = null;

  function signalFailure(code) {
    const safeCode = safeStatusCode(code);
    lastFailureCode = safeCode;
    try { onFailure({ code: safeCode, hostGeneration: config.hostGeneration }); }
    catch { /* Failure observers cannot interrupt Host cleanup. */ }
  }

  function readiness() {
    const ready = pluginFound && hasPostMessage && !rendererGone && !rendererClosed && !stopPromise;
    return {
      ready,
      pluginFound,
      hasPostMessage,
      rendererGone: rendererGone || rendererClosed,
      rendererGoneCode,
    };
  }

  function status() {
    const transportStatus = transport?.status();
    const rawGroupCallState = transportStatus?.groupCallState;
    const groupCallState = ["idle", "starting", "starting_post", "reserved", "quitting", "quarantined"]
      .includes(rawGroupCallState) ? rawGroupCallState : "idle";
    return {
      hostGeneration: config.hostGeneration,
      targetPid: config.targetPid,
      ready: readiness().ready && (transportStatus ? transportStatus.ready : true),
      callbackReady,
      pluginFound,
      hasPostMessage,
      rendererGone: rendererGone || rendererClosed,
      started: Boolean(startPromise),
      stopped: Boolean(stopPromise),
      listening: transportStatus?.listening === true,
      listenHost: LOOPBACK,
      listenPort: transportStatus?.listenPort ?? config.listenPort,
      outputPort: config.outputPort,
      invokeAttempts: transportStatus?.invokeAttempts ?? 0,
      invokePosted: transportStatus?.invokePosted ?? 0,
      outputForwarded: transportStatus?.outputForwarded ?? 0,
      queuedOutputBytes: transportStatus?.queuedOutputBytes ?? 0,
      queuedOutputFrames: transportStatus?.queuedOutputFrames ?? 0,
      outputFailed: transportStatus?.outputFailed === true,
      privateEndProtocolVerified: transportStatus?.privateEndProtocolVerified === true,
      groupCallProtocolAvailable: transportStatus?.groupCallProtocolAvailable === true,
      groupLocalQuitProofAvailable: transportStatus?.groupLocalQuitProofAvailable === true,
      groupLocalQuitWitnessAvailable: transportStatus?.groupLocalQuitWitnessAvailable === true,
      groupLocalQuitObservation: transportStatus?.groupLocalQuitObservation ?? null,
      groupBusyPrivateInvitesObserved: transportStatus?.groupBusyPrivateInvitesObserved ?? 0,
      groupCallReserved: transportStatus?.groupCallReserved === true,
      groupCallState,
      groupCallQuarantined: transportStatus?.groupCallQuarantined === true,
      groupCallNativeEntryVerified: !stopPromise && transportStatus?.groupCallNativeEntryVerified === true,
      groupRoomInfoQueryPending: transportStatus?.groupRoomInfoQueryPending === true,
      groupRoomQueryObservation: copyGroupRoomQueryObservation(transportStatus?.groupRoomQueryObservation),
      groupRoomInfoQueryQuarantined: transportStatus?.groupRoomInfoQueryQuarantined === true,
      groupRoomInfoQueryQuarantineCode: ["group_room_query_timeout", "group_room_query_post_failed", "group_room_metadata_invalid"]
        .includes(transportStatus?.groupRoomInfoQueryQuarantineCode) ? transportStatus.groupRoomInfoQueryQuarantineCode : null,
      groupQuitPosted: transportStatus?.groupQuitPosted === true,
      groupQuitAcknowledged: transportStatus?.groupQuitAcknowledged === true,
      groupQuitTerminalObserved: transportStatus?.groupQuitTerminalObserved === true,
      groupNativeObservation: !stopPromise ? copyGroupNativeObservation(transportStatus?.groupNativeObservation) : null,
      groupInviteObservation: !stopPromise ? copyGroupInviteObservation(transportStatus?.groupInviteObservation) : null,
      lastFailureCode: lastFailureCode || transportStatus?.lastFailureCode || null,
    };
  }

  function notifyStatus(phase) {
    try { onStatus({ phase, status: status() }); }
    catch { /* Status observers are best-effort and cannot change Host ownership. */ }
  }

  function trustedMessage(event) {
    if (!window || event?.sender !== window.webContents) return false;
    const frame = event.senderFrame;
    if (frame) return frame === window.webContents.mainFrame && frame.url === htmlUrl;
    try { return window.webContents.getURL() === htmlUrl; }
    catch { return false; }
  }

  function setReadyState(event, value) {
    if (!trustedMessage(event) || !value || typeof value !== "object") return;
    pluginFound = value.pluginFound === true;
    hasPostMessage = value.hasPostMessage === true;
    if (pluginFound && hasPostMessage) {
      readinessWaiter?.resolve();
      readinessWaiter = null;
    }
  }

  function acceptOutput(event, message) {
    if (!trustedMessage(event) || !message || typeof message !== "object" || Array.isArray(message)) return;
    const command = Number.isSafeInteger(message.command) ? message.command : message.cmd;
    if (!Number.isSafeInteger(command) || command < 1 || !Object.prototype.hasOwnProperty.call(message, "value")) {
      signalFailure("invalid_plugin_output");
      return;
    }
    const rawIdValid = Number.isSafeInteger(message.id) && message.id >= 0;
    const id = rawIdValid ? message.id : 0;
    const rawIdState = !Object.prototype.hasOwnProperty.call(message, "id") ? "missing" : rawIdValid ? "valid" : "invalid";
    void transport?.publishOutput(
      { command, id, value: message.value, hostGeneration: config.hostGeneration },
      { rawIdState },
    ).catch(() => {});
  }

  function reportRendererFailure(event, code) {
    if (!trustedMessage(event)) return;
    signalFailure(code === "renderer_output_send_failed" ? code : "renderer_reported_failure");
  }

  const onReady = (event, value) => setReadyState(event, value);
  const onOutput = (event, message) => acceptOutput(event, message);
  const onRendererFailure = (event, code) => reportRendererFailure(event, code);

  function installIpc() {
    ipcMain.on(READY_CHANNEL, onReady);
    ipcMain.on(OUTPUT_CHANNEL, onOutput);
    ipcMain.on(FAILURE_CHANNEL, onRendererFailure);
  }

  function removeIpc() {
    ipcMain.removeListener(READY_CHANNEL, onReady);
    ipcMain.removeListener(OUTPUT_CHANNEL, onOutput);
    ipcMain.removeListener(FAILURE_CHANNEL, onRendererFailure);
  }

  const onBeforeQuit = () => { void stop("host_stopped"); };
  app.on("before-quit", onBeforeQuit);
  const onPluginProcessGone = (_event, details) => {
    // This dedicated Host embeds one pinned AVSDK Pepper plugin. GPU, utility,
    // broker and other processes do not establish loss of that plugin.
    if (details?.type !== "Pepper Plugin" || !window || stopPromise || internalClose) return;
    pluginFound = false;
    hasPostMessage = false;
    const code = details?.reason === "crashed" ? "avsdk_plugin_crashed" : "avsdk_plugin_unavailable";
    signalFailure(code);
    readinessWaiter?.reject(codedError(code));
    readinessWaiter = null;
    void stop(code);
  };
  app.on("child-process-gone", onPluginProcessGone);

  async function invokePlugin({ command, id, params, deadlineMs, signal }) {
    if (signal?.aborted || Date.now() >= deadlineMs) throw codedError("host_stopped");
    if (!window || window.isDestroyed() || rendererGone || rendererClosed) throw codedError("renderer_crashed");
    if (!readiness().ready) throw codedError("host_not_ready");
    const encoded = (value) => JSON.stringify(value).replace(/[\u2028\u2029]/g, (character) => character === "\u2028" ? "\\u2028" : "\\u2029");
    const script = `window.__qqCallHostInvoke(${encoded(command)},${encoded(id)},${encoded(params)},${encoded(deadlineMs)})`;
    let result;
    try { result = await window.webContents.executeJavaScript(script, true); }
    catch { throw codedError("plugin_post_failed"); }
    if (signal?.aborted) throw codedError("host_stopped");
    if (result?.posted === false && typeof result.code === "string") throw codedError(safeStatusCode(result.code));
    return result;
  }

  async function waitForPluginReady() {
    if (readiness().ready) return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        readinessWaiter = null;
        reject(codedError("plugin_not_ready"));
      }, readyTimeoutMs);
      timer.unref?.();
      readinessWaiter = {
        resolve() { clearTimeout(timer); resolve(); },
        reject(error) { clearTimeout(timer); reject(error); },
      };
    });
    if (!readiness().ready) throw codedError("plugin_not_ready");
  }

  function createWindow() {
    window = new BrowserWindow({
      width: 24,
      height: 24,
      show: false,
      skipTaskbar: true,
      autoHideMenuBar: true,
      webPreferences: {
        preload: HOST_PRELOAD,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        plugins: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
        backgroundThrottling: false,
      },
    });
    const cacheRendererPid = () => {
      try {
        if (typeof window.webContents.getOSProcessId !== "function") return;
        const processId = window.webContents.getOSProcessId();
        if (Number.isSafeInteger(processId) && processId > 0) cachedRendererPid = processId;
      } catch { /* A renderer PID is optional diagnostic context. */ }
    };
    cacheRendererPid();
    window.webContents.on("did-start-navigation", cacheRendererPid);
    window.webContents.on("dom-ready", cacheRendererPid);
    window.webContents.on("did-finish-load", cacheRendererPid);
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event, url) => {
      if (url !== htmlUrl) event.preventDefault();
    });
    window.webContents.on("will-redirect", (event, url) => {
      if (url !== htmlUrl) event.preventDefault();
    });
    window.webContents.on("render-process-gone", (_event, details) => {
      const shutdownInProgress = Boolean(stopPromise || internalClose);
      if (!rendererGoneObserved) {
        rendererGoneObserved = true;
        try {
          const currentStatus = status();
          const record = rendererGoneDocument({
            observedAtMs: Date.now(),
            hostGeneration: config.hostGeneration,
            targetPid: config.targetPid,
            rendererPid: cachedRendererPid,
            reason: details?.reason,
            exitCode: details?.exitCode,
            shutdownInProgress,
            ready: currentStatus.ready,
            pluginFound,
            hasPostMessage,
            callbackReady,
          });
          const result = onRendererGone(record);
          if (result && typeof result.then === "function") void Promise.resolve(result).catch(() => {});
        } catch { /* Diagnostic observers cannot delay or interrupt Host cleanup. */ }
      }
      rendererGone = true;
      pluginFound = false;
      hasPostMessage = false;
      if (shutdownInProgress) {
        rendererGoneCode = lastFailureCode || "host_stopped";
        return;
      }
      rendererGoneCode = details?.reason === "clean-exit" && details?.exitCode === 0
        ? "renderer_closed"
        : "renderer_crashed";
      signalFailure(rendererGoneCode);
      readinessWaiter?.reject(codedError(rendererGoneCode));
      readinessWaiter = null;
      void stop(rendererGoneCode);
    });
    window.on("closed", () => {
      const shutdownInProgress = Boolean(stopPromise || internalClose);
      rendererClosed = true;
      pluginFound = false;
      hasPostMessage = false;
      if (!shutdownInProgress) {
        rendererGoneCode = "renderer_closed";
        signalFailure(rendererGoneCode);
        readinessWaiter?.reject(codedError(rendererGoneCode));
        readinessWaiter = null;
        void stop(rendererGoneCode);
      } else {
        rendererGoneCode = rendererGoneCode || lastFailureCode || "host_stopped";
      }
    });
    installIpc();
    return window;
  }

  async function stop(reason) {
    if (stopPromise) return stopPromise;
    if (reason) lastFailureCode = safeStatusCode(reason);
    stopPromise = Promise.resolve().then(async () => {
      internalClose = true;
      callbackReady = false;
      callbackWaitController?.abort();
      callbackWaitController = null;
      app.removeListener("before-quit", onBeforeQuit);
      app.removeListener("child-process-gone", onPluginProcessGone);
      readinessWaiter?.reject(codedError(lastFailureCode || "host_stopped"));
      readinessWaiter = null;
      removeIpc();
      if (window && !window.isDestroyed()) {
        try { window.destroy(); } catch { /* The renderer may already be gone. */ }
      }
      if (transport) await transport.stop();
      try {
        const pendingTraceWrite = onPrivateEndTrace.flush?.();
        if (pendingTraceWrite && typeof pendingTraceWrite.then === "function") void Promise.resolve(pendingTraceWrite).catch(() => {});
      } catch { /* A diagnostic flush cannot delay or interrupt Host cleanup. */ }
      notifyStatus("stopped");
      return status();
    });
    notifyStatus("stopping");
    return stopPromise;
  }

  async function start() {
    if (startPromise) return startPromise;
    startPromise = (async () => {
      const resolvedPlugin = configureHost({ app, config });
      const verifiedAudioDeviceProtocol = pluginHasVerifiedAudioDeviceProtocol(resolvedPlugin.pluginPath);
      const verifiedGroupCallProtocol = verifiedAudioDeviceProtocol;
      transport = createHostTransport({
        token: config.ipcToken,
        listenPort: config.listenPort,
        outputPort: config.outputPort,
        hostGeneration: config.hostGeneration,
        targetPid: config.targetPid,
        invokePlugin,
        getReadiness: readiness,
        verifiedAudioDeviceProtocol,
        verifiedAudioDeviceSelectionProtocol: verifiedAudioDeviceProtocol,
        verifiedGroupRoomInfoProtocol: verifiedAudioDeviceProtocol,
        verifiedGroupCallProtocol,
        verifiedLocalRoomIdProtocol: verifiedGroupCallProtocol,
        repeatableGroupRoomQueries: verifiedGroupCallProtocol,
        verifiedPrivateEndProtocol: verifiedAudioDeviceProtocol,
        onPrivateEndTrace,
        onFailure(event) { signalFailure(event.code); },
      });
      await transport.start();
      if (stopPromise) throw codedError("host_stopped");
      await app.whenReady();
      if (stopPromise) throw codedError("host_stopped");
      callbackWaitController = new AbortController();
      await waitForOutputCallback(config, {
        timeoutMs: callbackReadyTimeoutMs,
        requestTimeoutMs: callbackReadyRequestTimeoutMs,
        pollIntervalMs: callbackReadyPollMs,
        signal: callbackWaitController.signal,
      });
      if (stopPromise) throw codedError("host_stopped");
      callbackReady = true;
      callbackWaitController = null;
      createWindow();
      try { await window.loadFile(HOST_HTML); }
      catch { throw codedError("host_page_load_failed"); }
      if (stopPromise) throw codedError("host_stopped");
      await waitForPluginReady();
      if (stopPromise) throw codedError("host_stopped");
      notifyStatus("ready");
      return status();
    })();
    try {
      return await startPromise;
    } catch (error) {
      signalFailure(safeStatusCode(error?.code));
      await stop();
      throw codedError(safeStatusCode(error?.code));
    }
  }

  return Object.freeze({ start, stop, status, publishOutput: (message) => transport?.publishOutput(message) });
}

module.exports = {
  createElectronHost,
  configureHost,
  pluginSwitchMatches,
  matchesVerifiedAudioDevicePluginDigest,
  pluginHasVerifiedAudioDeviceProtocol,
};
