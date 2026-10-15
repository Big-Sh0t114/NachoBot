"use strict";

const PROCESS_TYPES = new Set(["browser", "renderer", "utility"]);
const REPORT_KEYS = [
  "schema", "processType", "electronPresent", "electronBuiltin", "appAvailable",
  "browserWindowAvailable", "getAllWindowsAvailable", "windowCount", "windowsTruncated", "windows",
];
const WINDOW_KEYS = [
  "ordinal", "routeClass", "destroyed", "webContentsAvailable", "webContentsDestroyed",
  "getTypeAvailable", "executeJavaScriptAvailable", "debuggerAvailable",
];

function processTypeOf(value) {
  try {
    const type = value?.type;
    return PROCESS_TYPES.has(type) ? type : "unknown";
  } catch {
    return "unknown";
  }
}

function electronPresentOf(value) {
  try {
    return typeof value?.versions?.electron === "string" && value.versions.electron.length > 0 && value.versions.electron.length <= 64;
  } catch {
    return false;
  }
}

function emptyReport(processType, electronPresent, electronBuiltin = false) {
  return {
    schema: 1,
    processType,
    electronPresent,
    electronBuiltin,
    appAvailable: false,
    browserWindowAvailable: false,
    getAllWindowsAvailable: false,
    windowCount: 0,
    windowsTruncated: false,
    windows: [],
  };
}

function isBooleanOrNull(value) {
  return value === null || typeof value === "boolean";
}

function isRendererCapabilityReport(value) {
  try {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    if (Object.keys(value).length !== REPORT_KEYS.length || REPORT_KEYS.some((key) => !Object.hasOwn(value, key))) return false;
    if (value.schema !== 1 || !["browser", "renderer", "utility", "unknown"].includes(value.processType) ||
        typeof value.electronPresent !== "boolean" || typeof value.electronBuiltin !== "boolean" ||
        typeof value.appAvailable !== "boolean" || typeof value.browserWindowAvailable !== "boolean" ||
        typeof value.getAllWindowsAvailable !== "boolean" || !Number.isInteger(value.windowCount) ||
        value.windowCount < 0 || value.windowCount > 16 || typeof value.windowsTruncated !== "boolean" ||
        !Array.isArray(value.windows) || value.windows.length !== value.windowCount || value.windows.length > 16) return false;
    if (!value.electronPresent || value.processType !== "browser") {
      if (value.electronBuiltin || value.appAvailable || value.browserWindowAvailable || value.getAllWindowsAvailable ||
          value.windowCount !== 0 || value.windowsTruncated) return false;
    }
    if (!value.electronBuiltin && (value.appAvailable || value.browserWindowAvailable || value.getAllWindowsAvailable || value.windowCount !== 0)) return false;
    if (!value.browserWindowAvailable && (value.getAllWindowsAvailable || value.windowCount !== 0 || value.windowsTruncated)) return false;
    if (!value.getAllWindowsAvailable && (value.windowCount !== 0 || value.windowsTruncated)) return false;
    if (value.windowsTruncated && value.windowCount !== 16) return false;
    if (value.windowCount > 0 && (!value.electronBuiltin || !value.getAllWindowsAvailable)) return false;
    return value.windows.every((window, index) => {
      if (!window || typeof window !== "object" || Array.isArray(window) ||
          Object.keys(window).length !== WINDOW_KEYS.length || WINDOW_KEYS.some((key) => !Object.hasOwn(window, key))) return false;
      return window.ordinal === index + 1 && window.routeClass === "unknown" &&
        isBooleanOrNull(window.destroyed) && typeof window.webContentsAvailable === "boolean" &&
        isBooleanOrNull(window.webContentsDestroyed) && typeof window.getTypeAvailable === "boolean" &&
        typeof window.executeJavaScriptAvailable === "boolean" && typeof window.debuggerAvailable === "boolean" &&
        (window.webContentsAvailable || (window.webContentsDestroyed === null && !window.getTypeAvailable &&
          !window.executeJavaScriptAvailable && !window.debuggerAvailable));
    });
  } catch {
    return false;
  }
}

function safeMethodBoolean(object, key) {
  try {
    const method = object?.[key];
    return typeof method === "function" ? method.call(object) === true : null;
  } catch {
    return null;
  }
}

function probeRendererCapabilities({ processObject = process, requireFunction = require } = {}) {
  const processType = processTypeOf(processObject);
  const electronPresent = electronPresentOf(processObject);
  const report = emptyReport(processType, electronPresent);
  if (processType !== "browser" || !electronPresent || typeof requireFunction !== "function") return report;

  let resolved;
  try {
    if (typeof requireFunction.resolve !== "function") return report;
    resolved = requireFunction.resolve("electron");
  } catch {
    return report;
  }
  if (resolved !== "electron") return report;

  report.electronBuiltin = true;
  let electron;
  try { electron = requireFunction("electron"); }
  catch { return report; }
  try { report.appAvailable = Boolean(electron?.app && typeof electron.app.isReady === "function"); }
  catch { report.appAvailable = false; }
  let browserWindow = null;
  try {
    browserWindow = electron?.BrowserWindow;
    report.browserWindowAvailable = typeof browserWindow === "function";
    report.getAllWindowsAvailable = typeof browserWindow?.getAllWindows === "function";
  } catch {
    report.browserWindowAvailable = false;
    report.getAllWindowsAvailable = false;
  }
  if (!report.getAllWindowsAvailable) return report;

  let windows;
  try { windows = browserWindow.getAllWindows(); }
  catch { return report; }
  let isWindowArray = false;
  let total = 0;
  try {
    isWindowArray = Array.isArray(windows);
    total = windows.length;
  } catch {
    return report;
  }
  if (!isWindowArray || !Number.isSafeInteger(total) || total < 0) return report;
  const count = Math.min(total, 16);
  report.windowCount = count;
  report.windowsTruncated = total > count;
  for (let index = 0; index < count; index += 1) {
    let window = null;
    try { window = windows[index]; } catch { /* a hostile accessor stays a fixed negative record */ }
    let webContents = null;
    try { webContents = window?.webContents || null; } catch { webContents = null; }
    let getTypeAvailable = false;
    let executeJavaScriptAvailable = false;
    let debuggerAvailable = false;
    if (webContents) {
      try { getTypeAvailable = typeof webContents.getType === "function"; } catch { /* retain fixed negative capability */ }
      try { executeJavaScriptAvailable = typeof webContents.executeJavaScript === "function"; } catch { /* retain fixed negative capability */ }
      try { debuggerAvailable = Boolean(webContents.debugger && typeof webContents.debugger.attach === "function"); } catch { /* retain fixed negative capability */ }
    }
    report.windows.push({
      ordinal: index + 1,
      routeClass: "unknown",
      destroyed: safeMethodBoolean(window, "isDestroyed"),
      webContentsAvailable: Boolean(webContents),
      webContentsDestroyed: webContents ? safeMethodBoolean(webContents, "isDestroyed") : null,
      getTypeAvailable,
      executeJavaScriptAvailable,
      debuggerAvailable,
    });
  }
  return report;
}

module.exports = { isRendererCapabilityReport, probeRendererCapabilities };
