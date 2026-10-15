"use strict";

const path = require("node:path");
const { fileURLToPath, pathToFileURL } = require("node:url");

const SCHEMA = "qqcall.official.windows.v1";
const PROOF_LEVEL = "diagnostic_only";
const APP_PATH = String.raw`C:\Program Files\Tencent\QQNT\versions\9.9.26-44343\resources\app`;
const RENDERER_PATH = path.win32.join(APP_PATH, "application.asar", "renderer", "index.html");
const APP_RENDERER_URL = "app://./renderer/index.html";
const MAX_WINDOWS = 16;
const MAX_PROBE_MS = 3000;
const MAX_URL_LENGTH = 8192;
const FIXED_DOM_SCRIPT = "(() => { const embeds = document.querySelectorAll('embed[type=\"application/x-ppapi-avSDK\"]'); return { embedCount: Math.min(embeds.length, 5), postMessageAvailable: embeds.length === 1 && typeof embeds[0].postMessage === 'function' }; })()";
const REPORT_KEYS = ["schema", "proofLevel", "status", "code", "windowCount", "windowsTruncated", "windows"];
const WINDOW_KEYS = ["ordinal", "destroyed", "rendererFileMatch", "routePath", "domStatus", "embedCount", "postMessageAvailable"];
const REPORT_CODES = new Set([
  "ok", "process_not_browser", "electron_not_present", "electron_not_builtin",
  "electron_unavailable", "browser_window_unavailable", "window_enumeration_failed",
  "window_list_invalid",
]);
const DOM_STATUSES = new Set([
  "not_queried", "observed", "ambiguous_embeds", "invalid_embed_count",
  "invalid_result", "unavailable", "timed_out", "stale_context",
]);

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function emptyReport(status, code) {
  return {
    schema: SCHEMA,
    proofLevel: PROOF_LEVEL,
    status,
    code,
    windowCount: 0,
    windowsTruncated: false,
    windows: [],
  };
}

function triStateMethod(object, key) {
  try {
    const method = object?.[key];
    if (typeof method !== "function") return null;
    const result = method.call(object);
    return typeof result === "boolean" ? result : null;
  } catch {
    return null;
  }
}

function safeGetURL(webContents) {
  try {
    const method = webContents?.getURL;
    if (typeof method !== "function") return null;
    const value = method.call(webContents);
    return typeof value === "string" && value.length <= MAX_URL_LENGTH ? value : null;
  } catch {
    return null;
  }
}

function safeRoutePath(value) {
  if (typeof value !== "string") return null;
  const route = value.split(/[?#]/, 1)[0];
  return route === "/" ? "/" : null;
}

function exactRendererRoute(urlValue) {
  if (typeof urlValue !== "string" || urlValue.length > MAX_URL_LENGTH) return { matches: false, routePath: null };
  try {
    // The installed QQ build also serves this exact renderer through Electron's
    // app scheme. Compare its literal base before query/hash; never persist either.
    const suffixIndex = urlValue.search(/[?#]/);
    const urlBase = suffixIndex < 0 ? urlValue : urlValue.slice(0, suffixIndex);
    if (urlBase === APP_RENDERER_URL) {
      const hashIndex = urlValue.indexOf("#");
      const fragmentRoute = hashIndex < 0 ? "" : urlValue.slice(hashIndex + 1);
      return { matches: true, routePath: safeRoutePath(fragmentRoute) };
    }

    // Retain the canonical fixed file URL check before decoding. This rejects
    // path aliases and encoded traversal while still allowing a route/query suffix.
    const expectedFileUrl = pathToFileURL(RENDERER_PATH).href;
    if (urlBase.toLowerCase() !== expectedFileUrl.toLowerCase()) return { matches: false, routePath: null };

    const parsed = new URL(urlValue);
    if (parsed.protocol !== "file:") return { matches: false, routePath: null };
    const decodedPath = fileURLToPath(parsed);
    if (path.win32.resolve(decodedPath).toLowerCase() !== path.win32.resolve(RENDERER_PATH).toLowerCase()) {
      return { matches: false, routePath: null };
    }
    const fragmentRoute = parsed.hash.startsWith("#") ? parsed.hash.slice(1) : "";
    return { matches: true, routePath: safeRoutePath(fragmentRoute) };
  } catch {
    return { matches: false, routePath: null };
  }
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

function isSafeRoute(value) {
  return value === null || value === "/";
}

function isOfficialWindowReport(value) {
  try {
    if (!isPlainExactObject(value, REPORT_KEYS) || value.schema !== SCHEMA || value.proofLevel !== PROOF_LEVEL ||
        !["complete", "rejected"].includes(value.status) || !REPORT_CODES.has(value.code) ||
        (value.status === "complete") !== (value.code === "ok") ||
        !Number.isInteger(value.windowCount) || value.windowCount < 0 || value.windowCount > MAX_WINDOWS ||
        typeof value.windowsTruncated !== "boolean" || !Array.isArray(value.windows) ||
        value.windows.length !== value.windowCount || (value.windowsTruncated && value.windowCount !== MAX_WINDOWS)) return false;
    if (value.status === "rejected" && (value.windowCount !== 0 || value.windowsTruncated)) return false;
    return value.windows.every((window, index) => {
      if (!isPlainExactObject(window, WINDOW_KEYS) || window.ordinal !== index + 1 ||
          !(window.destroyed === null || typeof window.destroyed === "boolean") ||
          typeof window.rendererFileMatch !== "boolean" || !isSafeRoute(window.routePath) ||
          !DOM_STATUSES.has(window.domStatus) ||
          !(window.embedCount === null || (Number.isInteger(window.embedCount) && window.embedCount >= 0 && window.embedCount <= 4)) ||
          !(window.postMessageAvailable === null || typeof window.postMessageAvailable === "boolean")) return false;
      if (!window.rendererFileMatch) {
        return window.routePath === null && window.domStatus === "not_queried" &&
          window.embedCount === null && window.postMessageAvailable === null;
      }
      if (window.domStatus === "observed") {
        return window.embedCount !== null && typeof window.postMessageAvailable === "boolean" &&
          (window.embedCount === 1 || window.postMessageAvailable === false);
      }
      if (window.domStatus === "ambiguous_embeds") {
        return window.embedCount >= 2 && window.postMessageAvailable === false;
      }
      if (window.domStatus === "invalid_embed_count") {
        return window.embedCount === null && window.postMessageAvailable === null;
      }
      return window.embedCount === null && window.postMessageAvailable === null;
    });
  } catch {
    return false;
  }
}

function waitForResult(value, deadline) {
  const pending = Promise.resolve(value);
  // Keep a late Electron promise observed if the bounded race expires.
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

function matchesCurrentContext(window, webContents, initialUrl) {
  if (triStateMethod(window, "isDestroyed") !== false) return false;
  let currentWebContents;
  try { currentWebContents = window?.webContents || null; }
  catch { return false; }
  if (currentWebContents !== webContents || triStateMethod(webContents, "isDestroyed") !== false) return false;
  return safeGetURL(webContents) === initialUrl;
}

function emptyWindowRecord(ordinal, destroyed = null) {
  return {
    ordinal,
    destroyed,
    rendererFileMatch: false,
    routePath: null,
    domStatus: "not_queried",
    embedCount: null,
    postMessageAvailable: null,
  };
}

async function inspectWindow(window, ordinal, deadline) {
  const record = emptyWindowRecord(ordinal);
  try { return await inspectWindowInternal(window, ordinal, deadline, record); }
  catch {
    record.domStatus = record.rendererFileMatch ? "unavailable" : "not_queried";
    record.embedCount = null;
    record.postMessageAvailable = null;
    return record;
  }
}

async function inspectWindowInternal(window, ordinal, deadline, record) {
  const initialDestroyed = triStateMethod(window, "isDestroyed");
  record.destroyed = initialDestroyed;
  if (initialDestroyed !== false) return record;

  let webContents;
  try { webContents = window?.webContents || null; }
  catch { return record; }
  if (!webContents) return record;
  const contentsDestroyed = triStateMethod(webContents, "isDestroyed");
  if (contentsDestroyed !== false) return record;
  const initialUrl = safeGetURL(webContents);
  if (initialUrl === null) return record;
  const match = exactRendererRoute(initialUrl);
  if (!match.matches) return record;

  record.rendererFileMatch = true;
  record.routePath = match.routePath;
  let executeJavaScript;
  try { executeJavaScript = webContents.executeJavaScript; }
  catch {
    record.domStatus = "unavailable";
    return record;
  }
  if (typeof executeJavaScript !== "function") {
    record.domStatus = "unavailable";
    return record;
  }
  if (!matchesCurrentContext(window, webContents, initialUrl)) {
    record.destroyed = triStateMethod(window, "isDestroyed");
    record.domStatus = "stale_context";
    return record;
  }
  if (Date.now() >= deadline) {
    record.destroyed = triStateMethod(window, "isDestroyed");
    record.domStatus = "timed_out";
    return record;
  }

  let scriptPromise;
  try { scriptPromise = executeJavaScript.call(webContents, FIXED_DOM_SCRIPT); }
  catch {
    record.domStatus = matchesCurrentContext(window, webContents, initialUrl) ? "unavailable" : "stale_context";
    record.destroyed = triStateMethod(window, "isDestroyed");
    return record;
  }
  const result = await waitForResult(scriptPromise, deadline);
  const contextStable = matchesCurrentContext(window, webContents, initialUrl);
  record.destroyed = triStateMethod(window, "isDestroyed");
  if (!contextStable) {
    record.domStatus = "stale_context";
    return record;
  }
  if (result.kind === "timed_out") {
    record.domStatus = "timed_out";
    return record;
  }
  if (result.kind === "rejected") {
    record.domStatus = "unavailable";
    return record;
  }
  if (result.kind !== "resolved" || !isPlainExactObject(result.result, ["embedCount", "postMessageAvailable"]) ||
      !Number.isInteger(result.result.embedCount) || result.result.embedCount < 0 ||
      typeof result.result.postMessageAvailable !== "boolean") {
    record.domStatus = "invalid_result";
    return record;
  }
  if (result.result.embedCount > 4) {
    record.domStatus = "invalid_embed_count";
    return record;
  }
  record.embedCount = result.result.embedCount;
  if (result.result.embedCount > 1) {
    record.domStatus = "ambiguous_embeds";
    record.postMessageAvailable = false;
    return record;
  }
  record.domStatus = "observed";
  record.postMessageAvailable = result.result.embedCount === 1 && result.result.postMessageAvailable;
  return record;
}

async function probeOfficialWindows({
  processObject = process,
  requireFunction = require,
  timeoutMs = MAX_PROBE_MS,
} = {}) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1) timeoutMs = MAX_PROBE_MS;
  timeoutMs = Math.min(timeoutMs, MAX_PROBE_MS);
  // The deadline bounds awaited DOM results; synchronous Electron getters and
  // an already submitted renderer script cannot be preempted from this process.
  const deadline = Date.now() + timeoutMs;
  let processType;
  try { processType = processObject?.type; } catch { processType = null; }
  if (processType !== "browser") return emptyReport("rejected", "process_not_browser");
  let electronVersion;
  try { electronVersion = processObject?.versions?.electron; } catch { electronVersion = null; }
  if (typeof electronVersion !== "string" || electronVersion.length < 1 || electronVersion.length > 64) {
    return emptyReport("rejected", "electron_not_present");
  }

  try {
    if (typeof requireFunction !== "function" || typeof requireFunction.resolve !== "function") {
      return emptyReport("rejected", "electron_unavailable");
    }
    if (requireFunction.resolve("electron") !== "electron") return emptyReport("rejected", "electron_not_builtin");
  } catch {
    return emptyReport("rejected", "electron_unavailable");
  }

  let electron;
  try { electron = requireFunction("electron"); }
  catch { return emptyReport("rejected", "electron_unavailable"); }
  let browserWindow;
  let getAllWindows;
  try {
    browserWindow = electron?.BrowserWindow;
    getAllWindows = browserWindow?.getAllWindows;
  }
  catch { return emptyReport("rejected", "browser_window_unavailable"); }
  if (typeof browserWindow !== "function" || typeof getAllWindows !== "function") {
    return emptyReport("rejected", "browser_window_unavailable");
  }

  let windows;
  try { windows = getAllWindows.call(browserWindow); }
  catch { return emptyReport("rejected", "window_enumeration_failed"); }
  let total;
  try { total = Array.isArray(windows) ? windows.length : null; }
  catch { total = null; }
  if (!Number.isSafeInteger(total) || total < 0) return emptyReport("rejected", "window_list_invalid");

  const count = Math.min(total, MAX_WINDOWS);
  const inspections = [];
  for (let index = 0; index < count; index += 1) {
    let window;
    try { window = windows[index]; }
    catch { inspections.push(Promise.resolve(emptyWindowRecord(index + 1))); continue; }
    inspections.push(inspectWindow(window, index + 1, deadline));
  }
  const report = {
    schema: SCHEMA,
    proofLevel: PROOF_LEVEL,
    status: "complete",
    code: "ok",
    windowCount: count,
    windowsTruncated: total > count,
    windows: await Promise.all(inspections),
  };
  return isOfficialWindowReport(report) ? report : emptyReport("rejected", "window_list_invalid");
}

module.exports = Object.freeze({
  APP_PATH,
  SCHEMA,
  PROOF_LEVEL,
  MAX_WINDOWS,
  MAX_PROBE_MS,
  FIXED_DOM_SCRIPT,
  exactRendererRoute,
  isOfficialWindowReport,
  probeOfficialWindows,
});
