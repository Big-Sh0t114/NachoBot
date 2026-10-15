"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { APP_PATH } = require("./official-window-probe.cjs");

const SCHEMA = "qqcall.renderer.script-metadata.v1";
const PROOF_LEVEL = "diagnostic_only";
const APP_RENDERER_BASE_URL = "app://./renderer/index.html";
const APP_RENDERER_URL = `${APP_RENDERER_BASE_URL}#/av`;
const RENDERER_PATH = path.win32.join(APP_PATH, "application.asar", "renderer", "index.html");
const FILE_RENDERER_BASE_URL = pathToFileURL(RENDERER_PATH).href;
const FILE_RENDERER_URL = `${FILE_RENDERER_BASE_URL}#/av`;
const MAX_URL_LENGTH = 2048;
const MAX_WINDOWS = 16;
const MAX_SCRIPTS_PER_WINDOW = 64;
const MAX_SCRIPT_RECORDS = 128;
const MAX_SCRIPT_BYTES = 256 * 1024;
const MAX_TOTAL_SCRIPT_BYTES = 1024 * 1024;
const MAX_SCRIPT_CHARS = MAX_SCRIPT_BYTES;
const MAX_REPORT_BYTES = 128 * 1024;
const MAX_PROBE_MS = 3000;
const REPORT_KEYS = [
  "schema", "proofLevel", "status", "code", "windowCount", "windowsTruncated",
  "exactAVWindowCount", "scriptRecordCount", "windows",
];
const WINDOW_KEYS = [
  "ordinal", "destroyed", "rendererBaseKind", "avFragmentExact", "queryPresent",
  "exactAVRenderer", "status", "scriptCount", "scriptsTruncated", "scripts",
];
const SCRIPT_KEYS = ["ordinal", "sourceKind", "status", "byteCount", "avsdkMarker", "postMessageMarker", "sceneIdMarker"];
const REPORT_CODES = new Set([
  "ok", "process_not_browser", "electron_not_present", "electron_not_builtin", "electron_unavailable",
  "app_unavailable", "app_not_ready", "app_path_mismatch", "browser_window_unavailable",
  "window_enumeration_failed", "window_list_invalid", "renderer_unavailable", "renderer_timed_out",
  "renderer_context_changed", "script_metadata_invalid", "script_metadata_oversized",
]);
const WINDOW_STATUSES = new Set(["not_target", "observed"]);
const RENDERER_BASE_KINDS = new Set(["app", "file", "other", "unavailable"]);
const SCRIPT_KINDS = new Set(["inline", "local_js", "rejected"]);
const SCRIPT_STATUSES = new Set(["observed", "external_unread", "source_rejected"]);

function emptyReport(status, code) {
  return {
    schema: SCHEMA,
    proofLevel: PROOF_LEVEL,
    status,
    code,
    windowCount: 0,
    windowsTruncated: false,
    exactAVWindowCount: 0,
    scriptRecordCount: 0,
    windows: [],
  };
}

function isPlainExactObject(value, keys) {
  try {
    return Boolean(value && typeof value === "object" && !Array.isArray(value) &&
      Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length &&
      keys.every((key) => Object.hasOwn(value, key)));
  } catch {
    return false;
  }
}

function isScriptRecord(value, ordinal) {
  if (!isPlainExactObject(value, SCRIPT_KEYS) || value.ordinal !== ordinal ||
      !SCRIPT_KINDS.has(value.sourceKind) || !SCRIPT_STATUSES.has(value.status) ||
      !(value.byteCount === null || (Number.isInteger(value.byteCount) && value.byteCount >= 0 && value.byteCount <= MAX_SCRIPT_BYTES)) ||
      typeof value.avsdkMarker !== "boolean" || typeof value.postMessageMarker !== "boolean" ||
      typeof value.sceneIdMarker !== "boolean") return false;
  if (value.sourceKind === "inline") return value.status === "observed" && value.byteCount !== null;
  if (value.sourceKind === "local_js") return value.status === "external_unread" && value.byteCount === null &&
    !value.avsdkMarker && !value.postMessageMarker && !value.sceneIdMarker;
  return value.sourceKind === "rejected" && value.status === "source_rejected" && value.byteCount === null &&
    !value.avsdkMarker && !value.postMessageMarker && !value.sceneIdMarker;
}

function isRendererScriptMetadataReport(value) {
  try {
    if (!isPlainExactObject(value, REPORT_KEYS) || value.schema !== SCHEMA || value.proofLevel !== PROOF_LEVEL ||
        !["complete", "rejected"].includes(value.status) || !REPORT_CODES.has(value.code) ||
        (value.status === "complete") !== (value.code === "ok") ||
        !Number.isInteger(value.windowCount) || value.windowCount < 0 || value.windowCount > MAX_WINDOWS ||
        typeof value.windowsTruncated !== "boolean" || (value.windowsTruncated && value.windowCount !== MAX_WINDOWS) ||
        !Number.isInteger(value.exactAVWindowCount) || value.exactAVWindowCount < 0 || value.exactAVWindowCount > value.windowCount ||
        !Number.isInteger(value.scriptRecordCount) || value.scriptRecordCount < 0 || value.scriptRecordCount > MAX_SCRIPT_RECORDS ||
        !Array.isArray(value.windows) || value.windows.length !== value.windowCount || value.windows.length > MAX_WINDOWS) return false;
    if (value.status === "rejected") {
      return value.windowCount === 0 && !value.windowsTruncated && value.exactAVWindowCount === 0 && value.scriptRecordCount === 0;
    }
    let exactCount = 0;
    let recordCount = 0;
    for (let index = 0; index < value.windows.length; index += 1) {
      const window = value.windows[index];
      if (!isPlainExactObject(window, WINDOW_KEYS) || window.ordinal !== index + 1 ||
          !(window.destroyed === null || typeof window.destroyed === "boolean") ||
          !RENDERER_BASE_KINDS.has(window.rendererBaseKind) || typeof window.avFragmentExact !== "boolean" ||
          typeof window.queryPresent !== "boolean" ||
          typeof window.exactAVRenderer !== "boolean" || !WINDOW_STATUSES.has(window.status) ||
          !Number.isInteger(window.scriptCount) || window.scriptCount < 0 || window.scriptCount > MAX_SCRIPTS_PER_WINDOW ||
          typeof window.scriptsTruncated !== "boolean" || (window.scriptsTruncated && window.scriptCount !== MAX_SCRIPTS_PER_WINDOW) ||
          !Array.isArray(window.scripts) || window.scripts.length !== window.scriptCount ||
          window.scripts.length > MAX_SCRIPTS_PER_WINDOW) return false;
      if (window.rendererBaseKind === "unavailable" && (window.avFragmentExact || window.queryPresent)) return false;
      const exactRoute = ["app", "file"].includes(window.rendererBaseKind) && window.avFragmentExact;
      if (window.exactAVRenderer && !exactRoute) return false;
      if (!window.exactAVRenderer) {
        if (window.status !== "not_target" || window.scriptCount !== 0 || window.scriptsTruncated) return false;
      } else {
        exactCount += 1;
        if (window.status !== "observed" || window.destroyed !== false) return false;
      }
      for (let scriptIndex = 0; scriptIndex < window.scripts.length; scriptIndex += 1) {
        if (!isScriptRecord(window.scripts[scriptIndex], scriptIndex + 1)) return false;
      }
      recordCount += window.scriptCount;
    }
    if (recordCount !== value.scriptRecordCount || exactCount !== value.exactAVWindowCount) return false;
    const inlineBytes = value.windows.reduce((sum, window) => sum + window.scripts.reduce((windowSum, script) =>
      windowSum + (script.sourceKind === "inline" ? script.byteCount : 0), 0), 0);
    if (inlineBytes > MAX_TOTAL_SCRIPT_BYTES) return false;
    return Buffer.byteLength(JSON.stringify(value), "utf8") <= MAX_REPORT_BYTES;
  } catch {
    return false;
  }
}

function safeBooleanMethod(object, name) {
  try {
    const method = object?.[name];
    if (typeof method !== "function") return null;
    const result = method.call(object);
    return typeof result === "boolean" ? result : null;
  } catch {
    return null;
  }
}

function safeURL(webContents) {
  try {
    const method = webContents?.getURL;
    if (typeof method !== "function") return null;
    const value = method.call(webContents);
    return typeof value === "string" && value.length <= MAX_URL_LENGTH ? value : null;
  } catch {
    return null;
  }
}

function isExactAVRendererURL(value) {
  if (typeof value !== "string" || value.length > MAX_URL_LENGTH) return false;
  const hashIndex = value.indexOf("#");
  if (hashIndex < 0 || value.slice(hashIndex) !== "#/av") return false;
  const beforeFragment = value.slice(0, hashIndex);
  const queryIndex = beforeFragment.indexOf("?");
  const base = queryIndex < 0 ? beforeFragment : beforeFragment.slice(0, queryIndex);
  return base === APP_RENDERER_BASE_URL || base === FILE_RENDERER_BASE_URL;
}

function classifyRendererURL(value) {
  if (typeof value !== "string" || value.length > MAX_URL_LENGTH) {
    return { rendererBaseKind: "unavailable", avFragmentExact: false, queryPresent: false };
  }
  const suffixIndex = value.search(/[?#]/);
  const base = suffixIndex < 0 ? value : value.slice(0, suffixIndex);
  let rendererBaseKind = "other";
  if (base === APP_RENDERER_BASE_URL) rendererBaseKind = "app";
  else if (base === FILE_RENDERER_BASE_URL) rendererBaseKind = "file";
  const hashIndex = value.indexOf("#");
  return {
    rendererBaseKind,
    avFragmentExact: hashIndex >= 0 && value.slice(hashIndex) === "#/av",
    queryPresent: value.includes("?"),
  };
}

function pinnedAppPath(app) {
  try {
    const method = app?.getAppPath;
    if (typeof method !== "function") return false;
    const value = method.call(app);
    return typeof value === "string" && value.length <= 4096 &&
      path.win32.resolve(value).toLowerCase() === path.win32.resolve(APP_PATH).toLowerCase();
  } catch {
    return false;
  }
}

function matchesCurrentContext(window, webContents, app, expectedURL) {
  if (safeBooleanMethod(window, "isDestroyed") !== false) return false;
  let currentContents;
  try { currentContents = window?.webContents || null; }
  catch { return false; }
  return currentContents === webContents && safeBooleanMethod(webContents, "isDestroyed") === false &&
    safeURL(webContents) === expectedURL && isExactAVRendererURL(expectedURL) && pinnedAppPath(app);
}

function safeErrorReport(code) {
  return emptyReport("rejected", REPORT_CODES.has(code) && code !== "ok" ? code : "script_metadata_invalid");
}

function waitForResult(value, deadline) {
  const pending = Promise.resolve(value);
  pending.then(() => {}, () => {});
  const remaining = deadline - Date.now();
  if (remaining <= 0) return Promise.resolve({ kind: "timed_out" });
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      resolve({ kind: "timed_out" });
    }, remaining);
    pending.then((result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ kind: "resolved", result });
    }, () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ kind: "rejected" });
    });
  });
}

function emptyWindowRecord(ordinal, destroyed = null) {
  return {
    ordinal,
    destroyed,
    rendererBaseKind: "unavailable",
    avFragmentExact: false,
    queryPresent: false,
    exactAVRenderer: false,
    status: "not_target",
    scriptCount: 0,
    scriptsTruncated: false,
    scripts: [],
  };
}

const FIXED_DOM_SCRIPT = `(() => {
  const fail = (code) => ({ kind: "rejected", code });
  try {
    const deadline = __QQCALL_HOST_DEADLINE__;
    const totalByteLimit = __QQCALL_REMAINING_BYTES__;
    if (Date.now() >= deadline) return fail("renderer_timed_out");
    const href = typeof location === "object" ? location.href : null;
    if (typeof href !== "string" || href.length > ${MAX_URL_LENGTH}) return fail("renderer_context_changed");
    const hashIndex = href.indexOf("#");
    if (hashIndex < 0 || href.slice(hashIndex) !== "#/av") return fail("renderer_context_changed");
    const beforeFragment = href.slice(0, hashIndex);
    const queryIndex = beforeFragment.indexOf("?");
    const rendererBase = queryIndex < 0 ? beforeFragment : beforeFragment.slice(0, queryIndex);
    if (rendererBase !== ${JSON.stringify(APP_RENDERER_BASE_URL)} && rendererBase !== ${JSON.stringify(FILE_RENDERER_BASE_URL)}) {
      return fail("renderer_context_changed");
    }
    const elements = document.scripts;
    const total = elements && elements.length;
    if (!Number.isSafeInteger(total) || total < 0) return fail("script_metadata_invalid");
    const limit = Math.min(total, ${MAX_SCRIPTS_PER_WINDOW});
    const scripts = [];
    let totalBytes = 0;
    for (let index = 0; index < limit; index += 1) {
      if (Date.now() >= deadline) return fail("renderer_timed_out");
      const element = elements[index];
      if (!element || typeof element !== "object") return fail("script_metadata_invalid");
      let source;
      try { source = element.getAttribute("src"); }
      catch { return fail("script_metadata_invalid"); }
      if (source === null) {
        let text;
        try { text = element.textContent; }
        catch { return fail("script_metadata_invalid"); }
        if (typeof text !== "string") return fail("script_metadata_invalid");
        if (text.length > ${MAX_SCRIPT_CHARS}) return fail("script_metadata_oversized");
        let bytes;
        try { bytes = new TextEncoder().encode(text).byteLength; }
        catch { return fail("script_metadata_invalid"); }
        if (!Number.isSafeInteger(bytes) || bytes > ${MAX_SCRIPT_BYTES} || totalBytes + bytes > totalByteLimit) {
          return fail("script_metadata_oversized");
        }
        totalBytes += bytes;
        scripts.push({ ordinal: index + 1, sourceKind: "inline", status: "observed", byteCount: bytes,
          avsdkMarker: /avSDK|AVSDK/i.test(text), postMessageMarker: /postMessage/.test(text),
          sceneIdMarker: /scene_id/.test(text) });
      } else if (typeof source === "string" && source.length <= ${MAX_SCRIPT_CHARS}) {
        let localPath = source;
        if (localPath.startsWith("app://./renderer/")) localPath = localPath.slice(17);
        else if (localPath.startsWith("/renderer/")) localPath = localPath.slice(10);
        else if (localPath.startsWith("./")) localPath = localPath.slice(2);
        const segments = localPath.split("/");
        const canonicalAppPrefix = source.startsWith("app://./renderer/");
        const localJavaScript = source.length > 0 && !source.includes("%") && !source.includes("\\\\") &&
          !source.includes("?") && !source.includes("#") && (canonicalAppPrefix || !source.includes(":")) && segments.length > 0 &&
          segments.every((segment) => /^[A-Za-z0-9_-][A-Za-z0-9._-]*$/.test(segment)) &&
          /\\.(?:m?js)$/i.test(segments[segments.length - 1]);
        scripts.push({ ordinal: index + 1, sourceKind: localJavaScript ? "local_js" : "rejected",
          status: localJavaScript ? "external_unread" : "source_rejected", byteCount: null,
          avsdkMarker: false, postMessageMarker: false, sceneIdMarker: false });
      } else {
        return fail(source === null ? "script_metadata_invalid" : "script_metadata_oversized");
      }
      if (scripts.length > ${MAX_SCRIPT_RECORDS}) return fail("script_metadata_oversized");
    }
    return { kind: "ok", scriptCount: scripts.length, scriptsTruncated: total > limit, scripts };
  } catch {
    return fail("script_metadata_invalid");
  }
})()`;

function fixedDomScriptFor(deadline, remainingBytes) {
  if (!Number.isSafeInteger(deadline) || deadline < 0 || !Number.isSafeInteger(remainingBytes) ||
      remainingBytes < 0 || remainingBytes > MAX_TOTAL_SCRIPT_BYTES) return null;
  return FIXED_DOM_SCRIPT
    .replace("__QQCALL_HOST_DEADLINE__", String(deadline))
    .replace("__QQCALL_REMAINING_BYTES__", String(remainingBytes));
}

async function inspectWindow(window, ordinal, app, deadline, remainingBytes) {
  const record = emptyWindowRecord(ordinal);
  record.destroyed = safeBooleanMethod(window, "isDestroyed");
  let webContents;
  try { webContents = window?.webContents || null; }
  catch { return { record, failure: null }; }
  if (!webContents) return { record, failure: null };
  const initialURL = safeURL(webContents);
  Object.assign(record, classifyRendererURL(initialURL));
  if (record.destroyed !== false || safeBooleanMethod(webContents, "isDestroyed") !== false) return { record, failure: null };
  if (!isExactAVRendererURL(initialURL)) return { record, failure: null };
  record.exactAVRenderer = true;
  if (!pinnedAppPath(app) || !matchesCurrentContext(window, webContents, app, initialURL)) {
    return { record: emptyWindowRecord(ordinal), failure: "app_path_mismatch" };
  }
  let executeJavaScript;
  try { executeJavaScript = webContents.executeJavaScript; }
  catch { return { record: emptyWindowRecord(ordinal), failure: "renderer_unavailable" }; }
  if (typeof executeJavaScript !== "function") return { record: emptyWindowRecord(ordinal), failure: "renderer_unavailable" };
  if (Date.now() >= deadline) return { record: emptyWindowRecord(ordinal), failure: "renderer_timed_out" };
  const query = fixedDomScriptFor(deadline, remainingBytes);
  if (!query) return { record: emptyWindowRecord(ordinal), failure: "script_metadata_invalid" };
  let promise;
  try { promise = executeJavaScript.call(webContents, query); }
  catch { return { record: emptyWindowRecord(ordinal), failure: "renderer_unavailable" }; }
  const result = await waitForResult(promise, deadline);
  if (Date.now() >= deadline) return { record: emptyWindowRecord(ordinal), failure: "renderer_timed_out" };
  if (!matchesCurrentContext(window, webContents, app, initialURL)) return { record: emptyWindowRecord(ordinal), failure: "renderer_context_changed" };
  if (result.kind === "timed_out") return { record: emptyWindowRecord(ordinal), failure: "renderer_timed_out" };
  if (result.kind === "rejected") return { record: emptyWindowRecord(ordinal), failure: "renderer_unavailable" };
  const payload = result.result;
  if (isPlainExactObject(payload, ["kind", "code"]) && payload.kind === "rejected" &&
      ["renderer_context_changed", "renderer_timed_out", "script_metadata_invalid", "script_metadata_oversized"].includes(payload.code)) {
    return { record: emptyWindowRecord(ordinal), failure: payload.code };
  }
  if (!isPlainExactObject(payload, ["kind", "scriptCount", "scriptsTruncated", "scripts"]) || payload.kind !== "ok" ||
      !Number.isInteger(payload.scriptCount) || payload.scriptCount < 0 || payload.scriptCount > MAX_SCRIPTS_PER_WINDOW ||
      typeof payload.scriptsTruncated !== "boolean" || (payload.scriptsTruncated && payload.scriptCount !== MAX_SCRIPTS_PER_WINDOW) ||
      !Array.isArray(payload.scripts) || payload.scripts.length !== payload.scriptCount ||
      payload.scripts.some((item, index) => !isScriptRecord(item, index + 1))) {
    return { record: emptyWindowRecord(ordinal), failure: "script_metadata_invalid" };
  }
  record.status = "observed";
  record.scriptCount = payload.scriptCount;
  record.scriptsTruncated = payload.scriptsTruncated;
  record.scripts = payload.scripts;
  const inlineBytes = record.scripts.reduce((sum, script) => sum +
    (script.sourceKind === "inline" ? script.byteCount : 0), 0);
  if (inlineBytes > remainingBytes) return { record: emptyWindowRecord(ordinal), failure: "script_metadata_oversized" };
  return { record, failure: null };
}

async function probeRendererScriptMetadata({ processObject = process, requireFunction = require } = {}) {
  const deadline = Date.now() + MAX_PROBE_MS;
  let processType;
  try { processType = processObject?.type; } catch { processType = null; }
  if (processType !== "browser") return safeErrorReport("process_not_browser");
  let electronVersion;
  try { electronVersion = processObject?.versions?.electron; } catch { electronVersion = null; }
  if (typeof electronVersion !== "string" || electronVersion.length < 1 || electronVersion.length > 64) {
    return safeErrorReport("electron_not_present");
  }
  try {
    if (typeof requireFunction !== "function" || typeof requireFunction.resolve !== "function") return safeErrorReport("electron_unavailable");
    if (requireFunction.resolve("electron") !== "electron") return safeErrorReport("electron_not_builtin");
  } catch {
    return safeErrorReport("electron_unavailable");
  }
  let electron;
  try { electron = requireFunction("electron"); }
  catch { return safeErrorReport("electron_unavailable"); }
  let app;
  let browserWindow;
  let getAllWindows;
  try {
    app = electron?.app;
    browserWindow = electron?.BrowserWindow;
    getAllWindows = browserWindow?.getAllWindows;
  } catch {
    return safeErrorReport("browser_window_unavailable");
  }
  if (!app || typeof app.isReady !== "function" || typeof app.getAppPath !== "function") return safeErrorReport("app_unavailable");
  try { if (app.isReady() !== true) return safeErrorReport("app_not_ready"); }
  catch { return safeErrorReport("app_unavailable"); }
  if (!pinnedAppPath(app)) return safeErrorReport("app_path_mismatch");
  if (typeof browserWindow !== "function" || typeof getAllWindows !== "function") return safeErrorReport("browser_window_unavailable");
  let windows;
  try { windows = getAllWindows.call(browserWindow); }
  catch { return safeErrorReport("window_enumeration_failed"); }
  let total;
  try { total = Array.isArray(windows) ? windows.length : null; }
  catch { total = null; }
  if (!Number.isSafeInteger(total) || total < 0) return safeErrorReport("window_list_invalid");
  const count = Math.min(total, MAX_WINDOWS);
  const inspections = [];
  let totalInlineBytes = 0;
  for (let index = 0; index < count; index += 1) {
    if (Date.now() >= deadline) return safeErrorReport("renderer_timed_out");
    if (totalInlineBytes >= MAX_TOTAL_SCRIPT_BYTES) return safeErrorReport("script_metadata_oversized");
    let window;
    try { window = windows[index]; }
    catch { inspections.push({ record: emptyWindowRecord(index + 1), failure: null }); continue; }
    inspections.push(await inspectWindow(window, index + 1, app, deadline, MAX_TOTAL_SCRIPT_BYTES - totalInlineBytes));
    const failed = inspections.at(-1).failure;
    if (failed) return safeErrorReport(failed);
    totalInlineBytes += inspections.at(-1).record.scripts.reduce((sum, script) => sum +
      (script.sourceKind === "inline" ? script.byteCount : 0), 0);
  }
  const records = inspections.map((item) => item.record);
  const report = {
    schema: SCHEMA,
    proofLevel: PROOF_LEVEL,
    status: "complete",
    code: "ok",
    windowCount: count,
    windowsTruncated: total > count,
    exactAVWindowCount: records.filter((item) => item.exactAVRenderer).length,
    scriptRecordCount: records.reduce((sum, item) => sum + item.scriptCount, 0),
    windows: records,
  };
  return isRendererScriptMetadataReport(report) ? report : safeErrorReport("script_metadata_invalid");
}

module.exports = Object.freeze({
  APP_PATH,
  APP_RENDERER_URL,
  FIXED_DOM_SCRIPT,
  MAX_PROBE_MS,
  MAX_REPORT_BYTES,
  MAX_SCRIPT_BYTES,
  MAX_SCRIPT_CHARS,
  MAX_SCRIPT_RECORDS,
  MAX_SCRIPTS_PER_WINDOW,
  MAX_TOTAL_SCRIPT_BYTES,
  MAX_WINDOWS,
  PROOF_LEVEL,
  SCHEMA,
  isRendererScriptMetadataReport,
  probeRendererScriptMetadata,
});
