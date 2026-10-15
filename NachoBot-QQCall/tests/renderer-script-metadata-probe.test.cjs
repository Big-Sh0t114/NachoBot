"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const vm = require("node:vm");
const { TextEncoder } = require("node:util");
const {
  APP_PATH,
  APP_RENDERER_URL,
  FIXED_DOM_SCRIPT,
  MAX_SCRIPT_BYTES,
  MAX_SCRIPT_CHARS,
  MAX_SCRIPT_RECORDS,
  MAX_SCRIPTS_PER_WINDOW,
  MAX_TOTAL_SCRIPT_BYTES,
  isRendererScriptMetadataReport,
  probeRendererScriptMetadata,
} = require("../agent/renderer-script-metadata-probe.cjs");

const FILE_RENDERER_URL = `${pathToFileURL(path.win32.join(APP_PATH, "application.asar", "renderer", "index.html")).href}#/av`;
const FILE_RENDERER_BASE_URL = FILE_RENDERER_URL.slice(0, FILE_RENDERER_URL.indexOf("#"));
const APP_QUERY_AV_URL = "app://./renderer/index.html?window=sentinel#/av";
const FILE_QUERY_AV_URL = `${FILE_RENDERER_BASE_URL}?window=sentinel#/av`;

function inlineScript(text) {
  return { getAttribute(name) { assert.equal(name, "src"); return null; }, textContent: text };
}

function externalScript(source) {
  return { getAttribute(name) { assert.equal(name, "src"); return source; }, get textContent() { assert.fail("external source text must not be read"); } };
}

function fixedScriptForTest(deadline = Date.now() + 3000, remainingBytes = MAX_TOTAL_SCRIPT_BYTES) {
  return FIXED_DOM_SCRIPT
    .replace("__QQCALL_HOST_DEADLINE__", String(deadline))
    .replace("__QQCALL_REMAINING_BYTES__", String(remainingBytes));
}

function runFixedScript(scripts, { deadline = Date.now() + 3000, remainingBytes = MAX_TOTAL_SCRIPT_BYTES, clock = () => Date.now(), url = APP_RENDERER_URL } = {}) {
  const calls = { fetch: 0, postMessage: 0, createElement: 0, forms: 0, storage: 0 };
  const document = { scripts };
  Object.defineProperty(document, "forms", { get() { calls.forms += 1; throw new Error("private form data"); } });
  Object.defineProperty(document, "createElement", { get() { calls.createElement += 1; throw new Error("DOM mutation forbidden"); } });
  const sandbox = {
    Date: { now: clock },
    document,
    location: { href: url },
    URL,
    TextEncoder,
    fetch() { calls.fetch += 1; throw new Error("network forbidden"); },
    localStorage: { get length() { calls.storage += 1; throw new Error("storage forbidden"); } },
    window: { postMessage() { calls.postMessage += 1; throw new Error("message forbidden"); } },
  };
  const query = fixedScriptForTest(deadline, remainingBytes);
  return { result: JSON.parse(JSON.stringify(vm.runInNewContext(query, sandbox))), calls, query };
}

function makeWindow(url, evaluate, { urlAfter = url } = {}) {
  let urlReads = 0;
  const calls = [];
  const window = {
    isDestroyed() { return false; },
    webContents: {
      isDestroyed() { return false; },
      getURL() { urlReads += 1; return urlReads > 2 ? urlAfter : url; },
      executeJavaScript(script) {
        calls.push(script);
        return evaluate(script);
      },
    },
  };
  return { window, calls };
}

function mockRequire(windows, appPath = APP_PATH) {
  const electron = {
    app: { isReady: () => true, getAppPath: () => appPath },
    BrowserWindow: Object.assign(function BrowserWindow() {}, { getAllWindows: () => windows }),
  };
  const fn = (name) => {
    assert.equal(name, "electron");
    return electron;
  };
  fn.resolve = (name) => {
    assert.equal(name, "electron");
    return "electron";
  };
  return fn;
}

function browserProcess() {
  return { type: "browser", versions: { electron: "test" } };
}

test("the fixed renderer query reports only bounded inline and local/external script metadata", async () => {
  const inlineText = "const privateSourceSentinel = 'INLINE_SOURCE_SECRET'; avSDK.postMessage(scene_id);";
  const { result: rendererResult, calls: forbiddenCalls } = runFixedScript([
    inlineScript(inlineText),
    externalScript("./bundle-private-name.js"),
    externalScript("app://./renderer/section/canonical.js"),
    externalScript("https://private.example/credential.js?token=secret"),
  ]);
  const target = makeWindow(APP_RENDERER_URL, (script) => {
    const deadline = script.match(/const deadline = (\d+);/)?.[1];
    const remaining = script.match(/const totalByteLimit = (\d+);/)?.[1];
    assert.ok(deadline);
    assert.equal(Number(remaining), MAX_TOTAL_SCRIPT_BYTES);
    assert.equal(script, fixedScriptForTest(Number(deadline), Number(remaining)));
    return rendererResult;
  });
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([target.window]),
  });
  assert.equal(report.status, "complete");
  assert.equal(report.exactAVWindowCount, 1);
  assert.equal(report.scriptRecordCount, 4);
  assert.deepEqual(report.windows[0].scripts, [
    { ordinal: 1, sourceKind: "inline", status: "observed", byteCount: Buffer.byteLength(inlineText), avsdkMarker: true, postMessageMarker: true, sceneIdMarker: true },
    { ordinal: 2, sourceKind: "local_js", status: "external_unread", byteCount: null, avsdkMarker: false, postMessageMarker: false, sceneIdMarker: false },
    { ordinal: 3, sourceKind: "local_js", status: "external_unread", byteCount: null, avsdkMarker: false, postMessageMarker: false, sceneIdMarker: false },
    { ordinal: 4, sourceKind: "rejected", status: "source_rejected", byteCount: null, avsdkMarker: false, postMessageMarker: false, sceneIdMarker: false },
  ]);
  const serialized = JSON.stringify(report);
  for (const privateValue of [inlineText, "INLINE_SOURCE_SECRET", "bundle-private-name.js", "private.example", "credential.js", "token=secret"]) {
    assert.equal(serialized.includes(privateValue), false, `report does not contain ${privateValue}`);
  }
  assert.deepEqual(forbiddenCalls, { fetch: 0, postMessage: 0, createElement: 0, forms: 0, storage: 0 });
  assert.equal(target.calls.length, 1);
  assert.equal(isRendererScriptMetadataReport(report), true);
});

test("external JavaScript candidates use a strict renderer-local source grammar", () => {
  const sources = [
    "./subdir/safe.js",
    "/renderer/assets/module.mjs",
    "app://./renderer/js/app.js",
    "../shared.js",
    "./%2e%2e/shared.js",
    ".\\escape.js",
    "./safe.js?token=secret",
    "./safe.js#fragment",
    "app://user:password@./renderer/safe.js",
    "app://./renderer/%2e%2e/safe.js",
    "app://./renderer/../safe.js",
    "app://./renderer/dir\\safe.js",
    "app://./renderer/safe.js?query",
    "app://./renderer/safe.js#fragment",
    "/other/safe.js",
  ];
  const result = runFixedScript(sources.map(externalScript)).result;
  assert.deepEqual(result.scripts.map((script) => script.sourceKind), [
    "local_js", "local_js", "local_js", "rejected", "rejected", "rejected", "rejected", "rejected", "rejected", "rejected",
    "rejected", "rejected", "rejected", "rejected", "rejected",
  ]);
  assert.doesNotMatch(JSON.stringify(result), /shared\.js|%2e|escape|token=secret|fragment|password|\/other/);
});

test("only exact app and canonical file AV URLs are queried; route mismatches are classified without disclosure", async () => {
  const urls = [
    "app://./renderer/index.html#/av",
    FILE_RENDERER_URL,
    APP_QUERY_AV_URL,
    FILE_QUERY_AV_URL,
    "app://./renderer/index.html#/av?room=secret",
    `${FILE_RENDERER_BASE_URL}#/av?room=secret`,
    "app://./renderer/index.html?token=secret#/official/room",
    "app://./renderer/index.html#/official/room",
    "app://localhost/renderer/index.html#/av",
    "app://./renderer/INDEX.html#/av",
    "app://./renderer/../renderer/index.html#/av",
    "https://example.invalid/renderer/index.html#/av",
    "file:///C:/Program%20Files/Tencent/QQNT/renderer/index.html#/av",
    `${FILE_RENDERER_BASE_URL.replace("file:///", "file://localhost/")}#/av`,
    `${FILE_RENDERER_BASE_URL.toLowerCase()}#/av`,
    `${FILE_RENDERER_BASE_URL.replace("index.html", "%2e%2e/renderer/index.html")}#/av`,
  ];
  const targets = urls.map((url, index) => makeWindow(url, () => {
    if (index > 3) assert.fail("route mismatches must not be queried");
    return runFixedScript([], { url }).result;
  }));
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire(targets.map((target) => target.window)),
  });
  assert.equal(report.status, "complete");
  assert.equal(report.exactAVWindowCount, 4);
  assert.deepEqual(targets.map((target) => target.calls.length), [1, 1, 1, 1, ...Array(urls.length - 4).fill(0)]);
  assert.ok(report.windows.slice(4).every((window) => window.exactAVRenderer === false && window.scripts.length === 0));
  assert.deepEqual(report.windows.slice(0, 4).map(({ rendererBaseKind, avFragmentExact, queryPresent }) => ({
    rendererBaseKind, avFragmentExact, queryPresent,
  })), [
    { rendererBaseKind: "app", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "file", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "app", avFragmentExact: true, queryPresent: true },
    { rendererBaseKind: "file", avFragmentExact: true, queryPresent: true },
  ]);
  assert.deepEqual(report.windows.slice(4).map(({ rendererBaseKind, avFragmentExact, queryPresent }) => ({
    rendererBaseKind, avFragmentExact, queryPresent,
  })), [
    { rendererBaseKind: "app", avFragmentExact: false, queryPresent: true },
    { rendererBaseKind: "file", avFragmentExact: false, queryPresent: true },
    { rendererBaseKind: "app", avFragmentExact: false, queryPresent: true },
    { rendererBaseKind: "app", avFragmentExact: false, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
    { rendererBaseKind: "other", avFragmentExact: true, queryPresent: false },
  ]);
  assert.doesNotMatch(JSON.stringify(report), /https:|file:|renderer\/index|official\/room|room=secret|token=secret|window=sentinel|Program%20Files/);
  assert.doesNotMatch(targets.slice(0, 4).flatMap((target) => target.calls).join("\n"), /window=sentinel/);
});

test("windows without a readable URL are marked unavailable with false route flags", async () => {
  const target = makeWindow(null, () => assert.fail("an unavailable URL must not be queried"));
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([target.window]),
  });
  assert.equal(report.status, "complete");
  assert.equal(target.calls.length, 0);
  assert.deepEqual({
    rendererBaseKind: report.windows[0].rendererBaseKind,
    avFragmentExact: report.windows[0].avFragmentExact,
    queryPresent: report.windows[0].queryPresent,
  }, { rendererBaseKind: "unavailable", avFragmentExact: false, queryPresent: false });
});

test("an exact window whose URL or webContents changes after evaluation fails closed", async () => {
  const target = makeWindow(APP_RENDERER_URL, () => ({
    kind: "ok", scriptCount: 0, scriptsTruncated: false, scripts: [],
  }), { urlAfter: "app://./renderer/index.html#/other" });
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([target.window]),
  });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "renderer_context_changed");
  assert.equal(report.windowCount, 0);
  assert.equal(target.calls.length, 1);

  const appToFile = makeWindow(APP_RENDERER_URL, () => ({
    kind: "ok", scriptCount: 0, scriptsTruncated: false, scripts: [],
  }), { urlAfter: FILE_RENDERER_URL });
  const routeChanged = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([appToFile.window]),
  });
  assert.equal(routeChanged.status, "rejected");
  assert.equal(routeChanged.code, "renderer_context_changed");
  assert.equal(appToFile.calls.length, 1);

  const appQueryChanged = makeWindow(APP_QUERY_AV_URL, () => ({
    kind: "ok", scriptCount: 0, scriptsTruncated: false, scripts: [],
  }), { urlAfter: "app://./renderer/index.html?window=changed#/av" });
  const queryChanged = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([appQueryChanged.window]),
  });
  assert.equal(queryChanged.status, "rejected");
  assert.equal(queryChanged.code, "renderer_context_changed");
  assert.equal(appQueryChanged.calls.length, 1);
});

test("the exact canonical file AV route is admitted by the fixed renderer query", () => {
  const result = runFixedScript([inlineScript("avSDK.postMessage(scene_id)")], { url: FILE_RENDERER_URL });
  assert.equal(result.result.kind, "ok");
  assert.equal(result.result.scriptCount, 1);
  assert.ok(result.query.includes(JSON.stringify("app://./renderer/index.html")));
  assert.ok(result.query.includes(JSON.stringify(FILE_RENDERER_BASE_URL)));
  assert.ok(result.query.includes('"#/av"'));
});

test("the fixed renderer query accepts only safe pre-fragment queries and returns no query value", () => {
  for (const url of [APP_QUERY_AV_URL, FILE_QUERY_AV_URL]) {
    const result = runFixedScript([inlineScript("avSDK.postMessage(scene_id)")], { url });
    assert.equal(result.result.kind, "ok");
    assert.equal(result.result.scriptCount, 1);
    assert.doesNotMatch(JSON.stringify(result.result), /window=sentinel|secret/);
  }
  for (const url of [
    "app://./renderer/index.html#/av?window=sentinel",
    `${FILE_RENDERER_BASE_URL}#/av?window=sentinel`,
    "app://./renderer/index.html?window=sentinel",
    `${FILE_RENDERER_BASE_URL}?window=sentinel`,
    "app://localhost/renderer/index.html?window=sentinel#/av",
    `${FILE_RENDERER_BASE_URL.toLowerCase()}?window=sentinel#/av`,
  ]) {
    const result = runFixedScript([inlineScript("private source should not be read")], { url });
    assert.deepEqual(result.result, { kind: "rejected", code: "renderer_context_changed" });
  }
});

test("renderer source size limits apply before encoding and across the whole report", () => {
  const perScript = runFixedScript([inlineScript("x".repeat(MAX_SCRIPT_CHARS + 1))]);
  assert.deepEqual(perScript.result, { kind: "rejected", code: "script_metadata_oversized" });
  const perScriptBytes = runFixedScript([inlineScript("é".repeat(Math.floor(MAX_SCRIPT_BYTES / 2) + 1))]);
  assert.deepEqual(perScriptBytes.result, { kind: "rejected", code: "script_metadata_oversized" });
  const totalText = "x".repeat(Math.floor(MAX_TOTAL_SCRIPT_BYTES / 4));
  const total = runFixedScript(Array.from({ length: 5 }, () => inlineScript(totalText)));
  assert.deepEqual(total.result, { kind: "rejected", code: "script_metadata_oversized" });
});

test("the one-megabyte inline budget is global across windows and forged aggregate replies are rejected", async () => {
  const perWindow = {
    kind: "ok",
    scriptCount: 3,
    scriptsTruncated: false,
    scripts: Array.from({ length: 3 }, (_, ordinal) => ({
      ordinal: ordinal + 1,
      sourceKind: "inline",
      status: "observed",
      byteCount: 200 * 1024,
      avsdkMarker: false,
      postMessageMarker: false,
      sceneIdMarker: false,
    })),
  };
  const targets = [0, 1, 2].map(() => makeWindow(APP_RENDERER_URL, (query) => {
    const remaining = Number(query.match(/const totalByteLimit = (\d+);/)?.[1]);
    assert.ok(remaining > 0 && remaining <= MAX_TOTAL_SCRIPT_BYTES);
    return perWindow;
  }));
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire(targets.map((target) => target.window)),
  });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "script_metadata_oversized");
  assert.ok(targets[0].calls.length + targets[1].calls.length <= 2);
  assert.equal(targets[2].calls.length, 0);

  const forged = {
    schema: "qqcall.renderer.script-metadata.v1",
    proofLevel: "diagnostic_only",
    status: "complete",
    code: "ok",
    windowCount: 2,
    windowsTruncated: false,
    exactAVWindowCount: 2,
    scriptRecordCount: 6,
    windows: [1, 2].map((ordinal) => ({
      ordinal,
      destroyed: false,
      exactAVRenderer: true,
      status: "observed",
      scriptCount: 3,
      scriptsTruncated: false,
      scripts: perWindow.scripts,
    })),
  };
  assert.equal(isRendererScriptMetadataReport(forged), false);
});

test("exhausting the report-wide inline budget stops before the next renderer window", async () => {
  const atBudget = {
    kind: "ok",
    scriptCount: 4,
    scriptsTruncated: false,
    scripts: Array.from({ length: 4 }, (_, ordinal) => ({
      ordinal: ordinal + 1,
      sourceKind: "inline",
      status: "observed",
      byteCount: 128 * 1024,
      avsdkMarker: false,
      postMessageMarker: false,
      sceneIdMarker: false,
    })),
  };
  const targets = [0, 1, 2].map(() => makeWindow(APP_RENDERER_URL, () => atBudget));
  const report = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire(targets.map((target) => target.window)),
  });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "script_metadata_oversized");
  assert.deepEqual(targets.map((target) => target.calls.length), [1, 1, 0]);
});

test("the fixed query uses one absolute host deadline and fails when execution resolves after it", async () => {
  const originalNow = Date.now;
  let clock = 10_000;
  Date.now = () => clock;
  try {
    const target = makeWindow(APP_RENDERER_URL, (query) => {
      assert.equal(Number(query.match(/const deadline = (\d+);/)?.[1]), 10_000 + 3000);
      return new Promise((resolve) => setTimeout(() => {
        clock = 10_000 + 3001;
        resolve({ kind: "ok", scriptCount: 0, scriptsTruncated: false, scripts: [] });
      }, 0));
    });
    const report = await probeRendererScriptMetadata({
      processObject: browserProcess(),
      requireFunction: mockRequire([target.window]),
    });
    assert.equal(report.status, "rejected");
    assert.equal(report.code, "renderer_timed_out");
    assert.equal(target.calls.length, 1);
  } finally {
    Date.now = originalNow;
  }
});

test("the renderer checks its absolute deadline before and during enumeration", () => {
  let scriptReads = 0;
  const scripts = new Proxy([inlineScript("first"), inlineScript("second")], {
    get(target, property, receiver) {
      if (property === "0" || property === "1") scriptReads += 1;
      return Reflect.get(target, property, receiver);
    },
  });
  const expired = runFixedScript(scripts, { deadline: 4, clock: () => 4 });
  assert.deepEqual(expired.result, { kind: "rejected", code: "renderer_timed_out" });
  assert.equal(scriptReads, 0);

  const times = [0, 0, 4];
  const during = runFixedScript(scripts, { deadline: 4, clock: () => times.shift() ?? 4 });
  assert.deepEqual(during.result, { kind: "rejected", code: "renderer_timed_out" });
  assert.equal(scriptReads, 1);
});

test("script, window, report, and result-schema caps fail closed", async () => {
  let highScriptIndexRead = false;
  const scriptElements = new Proxy(Array.from({ length: MAX_SCRIPTS_PER_WINDOW + 1 }, () => externalScript("./local.js")), {
    get(target, property, receiver) {
      if (property === String(MAX_SCRIPTS_PER_WINDOW)) highScriptIndexRead = true;
      return Reflect.get(target, property, receiver);
    },
  });
  const cappedRendererResult = runFixedScript(scriptElements).result;
  assert.equal(cappedRendererResult.scripts.length, MAX_SCRIPTS_PER_WINDOW);
  assert.equal(cappedRendererResult.scriptsTruncated, true);
  assert.equal(highScriptIndexRead, false);
  const target = makeWindow(APP_RENDERER_URL, () => cappedRendererResult);
  const windows = new Proxy([target.window, ...Array.from({ length: 18 }, () => ({ isDestroyed: () => true }))], {
    get(array, property, receiver) {
      if (property === "16") assert.fail("window limit must prevent reading beyond sixteen");
      return Reflect.get(array, property, receiver);
    },
  });
  const capped = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire(windows),
  });
  assert.equal(capped.windowCount, 16);
  assert.equal(capped.windowsTruncated, true);
  assert.equal(capped.scriptRecordCount, MAX_SCRIPTS_PER_WINDOW);
  assert.equal(isRendererScriptMetadataReport(capped), true);
  assert.equal(isRendererScriptMetadataReport({ ...capped, proofLevel: "unknown" }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, sourceText: "forged" }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], url: APP_RENDERER_URL }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], rendererBaseKind: "remote" }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], rendererBaseKind: 4 }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], avFragmentExact: 1 }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], queryPresent: "false" }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], rendererBaseKind: "unavailable", queryPresent: true }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: [{ ...capped.windows[0], url: APP_RENDERER_URL }, ...capped.windows.slice(1)] }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, scriptRecordCount: MAX_SCRIPT_RECORDS + 1 }), false);
  assert.equal(isRendererScriptMetadataReport({ ...capped, windows: Array.from({ length: 17 }, (_, ordinal) => ({ ...capped.windows[0], ordinal })) }), false);
  assert.equal(isRendererScriptMetadataReport({
    ...capped,
    windows: [{ ...capped.windows[0], scripts: [{ ...capped.windows[0].scripts[0], sourceText: "private" }] }, ...capped.windows.slice(1)],
  }), false);
  assert.equal(isRendererScriptMetadataReport({
    ...capped,
    windows: [{ ...capped.windows[0], scripts: [{ ...capped.windows[0].scripts[0], url: "app://./private.js" }] }, ...capped.windows.slice(1)],
  }), false);
});

test("a mismatched pinned app path or non-builtin Electron module is rejected before window access", async () => {
  let windowsRead = 0;
  const appMismatch = await probeRendererScriptMetadata({
    processObject: browserProcess(),
    requireFunction: mockRequire([], "C:\\Other\\QQ\\app"),
  });
  assert.equal(appMismatch.code, "app_path_mismatch");
  const nonBuiltinRequire = mockRequire([]);
  nonBuiltinRequire.resolve = () => "C:\\private\\electron.js";
  const nonBuiltin = await probeRendererScriptMetadata({ processObject: browserProcess(), requireFunction: nonBuiltinRequire });
  assert.equal(nonBuiltin.code, "electron_not_builtin");
  const malformedRequire = (name) => {
    assert.equal(name, "electron");
    return { app: { isReady: () => true, getAppPath: () => APP_PATH }, BrowserWindow: Object.assign(function BrowserWindow() {}, {
      getAllWindows() { windowsRead += 1; throw new Error("private window error"); },
    }) };
  };
  malformedRequire.resolve = () => "electron";
  const failed = await probeRendererScriptMetadata({ processObject: browserProcess(), requireFunction: malformedRequire });
  assert.equal(failed.code, "window_enumeration_failed");
  assert.equal(windowsRead, 1);
  assert.doesNotMatch(JSON.stringify([appMismatch, nonBuiltin, failed]), /Other|private window error|electron\.js/);
});
