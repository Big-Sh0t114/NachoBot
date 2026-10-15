"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { pathToFileURL } = require("node:url");
const {
  APP_PATH,
  FIXED_DOM_SCRIPT,
  PROOF_LEVEL,
  exactRendererRoute,
  isOfficialWindowReport,
  probeOfficialWindows,
} = require("../agent/official-window-probe.cjs");

const RENDERER_URL = pathToFileURL(`${APP_PATH}\\application.asar\\renderer\\index.html`).href;
const APP_RENDERER_URL = "app://./renderer/index.html";

function electronRequire(windows, { resolveAs = "electron" } = {}) {
  const calls = [];
  const BrowserWindow = Object.assign(function BrowserWindow() {}, {
    getAllWindows() { calls.push("getAllWindows"); return windows; },
  });
  const fn = (name) => {
    calls.push(`load:${name}`);
    return { BrowserWindow };
  };
  fn.resolve = (name) => {
    calls.push(`resolve:${name}`);
    return resolveAs;
  };
  fn.calls = calls;
  return fn;
}

function mockWindow(url, executeJavaScript, { destroyed = false } = {}) {
  let currentUrl = url;
  let contentsDestroyed = false;
  const calls = [];
  const webContents = {
    isDestroyed() { return contentsDestroyed; },
    getURL() { calls.push("getURL"); return currentUrl; },
    executeJavaScript(script) {
      calls.push("executeJavaScript");
      assert.equal(script, FIXED_DOM_SCRIPT);
      return executeJavaScript(script, {
        setURL(value) { currentUrl = value; },
        destroyContents() { contentsDestroyed = true; },
      });
    },
  };
  const window = {
    webContents,
    isDestroyed() { return destroyed; },
  };
  return { window, webContents, calls, setURL(value) { currentUrl = value; }, destroyContents() { contentsDestroyed = true; } };
}

function executeFixedDom({ embeds = [], nativePostMessage = false } = {}) {
  return (script) => {
    const native = nativePostMessage ? { postMessage() {} } : undefined;
    return JSON.parse(JSON.stringify(vm.runInNewContext(script, {
      document: {
        querySelectorAll(selector) {
          assert.equal(selector, 'embed[type="application/x-ppapi-avSDK"]');
          return embeds;
        },
      },
      window: { native },
    })));
  };
}

function browserProcess() {
  return { type: "browser", versions: { electron: "test" } };
}

test("official-window probe loads Electron only in browser with the exact builtin resolution", async () => {
  for (const processObject of [{ type: "renderer" }, { type: "utility" }, { type: "unknown" }]) {
    const requireFunction = electronRequire([]);
    const report = await probeOfficialWindows({ processObject, requireFunction });
    assert.equal(report.status, "rejected");
    assert.equal(report.code, "process_not_browser");
    assert.deepEqual(requireFunction.calls, []);
  }

  const nonBuiltin = electronRequire([], { resolveAs: "C:\\private\\node_modules\\electron\\index.js" });
  const report = await probeOfficialWindows({ processObject: browserProcess(), requireFunction: nonBuiltin });
  assert.equal(report.code, "electron_not_builtin");
  assert.deepEqual(nonBuiltin.calls, ["resolve:electron"]);
  assert.equal(isOfficialWindowReport(report), true);

  const noVersion = electronRequire([]);
  const missingElectron = await probeOfficialWindows({ processObject: { type: "browser" }, requireFunction: noVersion });
  assert.equal(missingElectron.code, "electron_not_present");
  assert.deepEqual(noVersion.calls, []);
});

test("external, wrong-root, and encoded-traversal file URLs never execute the DOM script", async () => {
  const urls = [
    "https://example.invalid/renderer/index.html#/official/group",
    RENDERER_URL.replace("/application.asar/renderer/index.html", "/other.asar/renderer/index.html"),
    `${APP_PATH.replaceAll("\\", "/")}/application.asar/renderer/%2e%2e/renderer/index.html#/official/group`,
  ];
  const windows = urls.map((url) => mockWindow(url, () => {
    assert.fail("external or noncanonical renderer URL must not execute JavaScript");
  }));
  const report = await probeOfficialWindows({ processObject: browserProcess(), requireFunction: electronRequire(windows.map(({ window }) => window)) });
  assert.equal(report.status, "complete");
  assert.equal(report.windowCount, urls.length);
  assert.ok(report.windows.every((record) => record.rendererFileMatch === false && record.domStatus === "not_queried"));
  assert.ok(windows.every(({ calls }) => !calls.includes("executeJavaScript")));
  assert.doesNotMatch(JSON.stringify(report), /example|resources|renderer\/index|%2e|official\/group/i);
});

test("only the exact QQ app renderer origin is admitted before query and hash", async () => {
  const aliases = [
    "app://other/renderer/index.html#/main/message",
    "app://./other/index.html#/main/message",
    "APP://./renderer/index.html#/main/message",
    "app://./Renderer/index.html#/main/message",
    "app://./renderer/%69ndex.html#/main/message",
    "app://./renderer/../renderer/index.html#/main/message",
    "app://./renderer/index.html/alias#/main/message",
    "app://./renderer/index.html.evil#/main/message",
    "app://user@./renderer/index.html#/main/message",
    "app://.:1234/renderer/index.html#/main/message",
  ];
  const windows = aliases.map((url) => mockWindow(url, () => {
    assert.fail("noncanonical app renderer URL must not execute JavaScript");
  }));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire(windows.map(({ window }) => window)),
  });
  assert.equal(report.status, "complete");
  assert.ok(report.windows.every((record) => record.rendererFileMatch === false && record.domStatus === "not_queried"));
  assert.ok(windows.every(({ calls }) => !calls.includes("executeJavaScript")));
  assert.equal(isOfficialWindowReport(report), true);
});

test("the observed QQ app renderer is queried while its URL and route remain private", async () => {
  const actualUrl = `${APP_RENDERER_URL}?webcontentsid=2&processGroupNS=createBlankWindow1#/main/message?uid=2955610491`;
  const actualAvUrl = `${APP_RENDERER_URL}?webcontentsid=5&processGroupNS=createBlankWindow1#/av`;
  assert.deepEqual(exactRendererRoute(actualUrl), { matches: true, routePath: null });
  assert.deepEqual(exactRendererRoute(actualAvUrl), { matches: true, routePath: null });
  assert.deepEqual(exactRendererRoute(`${APP_RENDERER_URL}?webcontentsid=2#/?account=51836`), {
    matches: true,
    routePath: "/",
  });

  const actual = mockWindow(actualUrl, executeFixedDom({ embeds: [{ postMessage() {} }] }));
  const actualAv = mockWindow(actualAvUrl, executeFixedDom({ embeds: [{ postMessage() {} }] }));
  const root = mockWindow(`${APP_RENDERER_URL}?webcontentsid=2#/`, executeFixedDom({ embeds: [{ postMessage() {} }] }));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([actual.window, actualAv.window, root.window]),
  });
  assert.equal(report.windows[0].rendererFileMatch, true);
  assert.equal(report.windows[0].routePath, null);
  assert.equal(report.windows[0].domStatus, "observed");
  assert.equal(report.windows[0].embedCount, 1);
  assert.equal(report.windows[0].postMessageAvailable, true);
  assert.equal(report.windows[1].rendererFileMatch, true);
  assert.equal(report.windows[1].routePath, null);
  assert.equal(report.windows[1].domStatus, "observed");
  assert.equal(report.windows[1].embedCount, 1);
  assert.equal(report.windows[1].postMessageAvailable, true);
  assert.equal(report.windows[2].rendererFileMatch, true);
  assert.equal(report.windows[2].routePath, "/");
  assert.equal(isOfficialWindowReport(report), true);
  assert.doesNotMatch(JSON.stringify(report), /webcontentsid|processGroupNS|createBlankWindow|main\/message|2955610491|51836|\/av/);
});

test("only the root route survives projection; arbitrary, private, traversal, and encoded routes are omitted", () => {
  assert.deepEqual(exactRendererRoute(`${RENDERER_URL}#/?account=51836`), {
    matches: true,
    routePath: "/",
  });
  for (const route of [
    "/group/u_ABCDEFG",
    "/group/private-message-text",
    "/../group",
    "/%2e%2e/group",
  ]) {
    assert.deepEqual(exactRendererRoute(`${RENDERER_URL}#${route}`), { matches: true, routePath: null });
  }
  assert.deepEqual(exactRendererRoute("https://example.invalid/renderer/index.html#/official/group"), {
    matches: false,
    routePath: null,
  });
});

test("unknown canonical renderer routes remain queryable without persisting their content", async () => {
  const routeValues = [
    "/group/u_ABCDEFG",
    "/group/private-message-text",
    "/../group",
    "/%2e%2e/group",
    "/?account=51836",
  ];
  const windows = routeValues.map((route) => mockWindow(`${RENDERER_URL}#${route}`, executeFixedDom({ embeds: [{ postMessage() {} }] })));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire(windows.map(({ window }) => window)),
  });
  assert.deepEqual(report.windows.map((record) => record.routePath), [null, null, null, null, "/"]);
  assert.ok(report.windows.every((record) => record.rendererFileMatch && record.domStatus === "observed" && record.postMessageAvailable));
  const persisted = JSON.stringify(report);
  assert.doesNotMatch(persisted, /u_ABCDEFG|private-message-text|51836|%2e%2e/);
});

test("the single fixed read-only script requires exactly one PPAPI embed's own postMessage", async () => {
  assert.match(FIXED_DOM_SCRIPT, /document\.querySelectorAll\('embed\[type=\\?"application\/x-ppapi-avSDK\\?"\]'\)/);
  assert.match(FIXED_DOM_SCRIPT, /embeds\.length === 1/);
  assert.match(FIXED_DOM_SCRIPT, /embeds\[0\]\.postMessage/);
  assert.doesNotMatch(FIXED_DOM_SCRIPT, /window\.native|\.postMessage\s*\(/);

  const hasOwnMethod = mockWindow(`${RENDERER_URL}#/official/call`, executeFixedDom({ embeds: [{ postMessage() {} }] }));
  const globalOnly = mockWindow(`${RENDERER_URL}#/official/peer`, executeFixedDom({ embeds: [{}], nativePostMessage: true }));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([hasOwnMethod.window, globalOnly.window]),
  });
  assert.equal(report.proofLevel, PROOF_LEVEL);
  assert.equal(report.status, "complete");
  assert.equal(report.windows[0].domStatus, "observed");
  assert.equal(report.windows[0].embedCount, 1);
  assert.equal(report.windows[0].postMessageAvailable, true, "an embed method is observed without a global native bridge");
  assert.equal(report.windows[1].domStatus, "observed");
  assert.equal(report.windows[1].postMessageAvailable, false, "window.native.postMessage does not stand in for the embed method");
  assert.equal(isOfficialWindowReport(report), true);
});

test("multiple embeds stay ambiguous, and more than four embeds become bounded invalid metadata", async () => {
  const multiple = mockWindow(`${RENDERER_URL}#/official/group`, executeFixedDom({
    embeds: [{ postMessage() {} }, { postMessage() {} }],
  }));
  const tooMany = mockWindow(`${RENDERER_URL}#/official/room`, executeFixedDom({ embeds: Array.from({ length: 9 }, () => ({ postMessage() {} })) }));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([multiple.window, tooMany.window]),
  });
  assert.equal(report.windows[0].domStatus, "ambiguous_embeds");
  assert.equal(report.windows[0].embedCount, 2);
  assert.equal(report.windows[0].postMessageAvailable, false);
  assert.equal(report.windows[1].domStatus, "invalid_embed_count");
  assert.equal(report.windows[1].embedCount, null);
  assert.equal(report.windows[1].postMessageAvailable, null);
  assert.equal(JSON.stringify(report).includes("9"), false, "the raw over-limit count is not persisted");
  assert.equal(isOfficialWindowReport(report), true);
});

test("destroyed and stale windows cannot contribute DOM results", async () => {
  const destroyed = mockWindow(`${RENDERER_URL}#/official/group`, () => assert.fail("destroyed window was queried"), { destroyed: true });
  const stale = mockWindow(`${APP_RENDERER_URL}?webcontentsid=2#/main/message`, (_script, context) => {
    context.setURL(`${APP_RENDERER_URL}?webcontentsid=3#/main/message?uid=2955610491`);
    return { embedCount: 1, postMessageAvailable: true };
  });
  let resolveAfterReplacement;
  const replaced = mockWindow(`${RENDERER_URL}#/official/call`, () => new Promise((resolve) => { resolveAfterReplacement = resolve; }));
  const replacementContents = { isDestroyed: () => false, getURL: () => `${RENDERER_URL}#/official/call` };
  const probing = probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([destroyed.window, stale.window, replaced.window]),
  });
  replaced.window.webContents = replacementContents;
  resolveAfterReplacement({ embedCount: 1, postMessageAvailable: true });
  const report = await probing;
  assert.equal(report.windows[0].destroyed, true);
  assert.equal(report.windows[0].domStatus, "not_queried");
  assert.equal(report.windows[1].rendererFileMatch, true);
  assert.equal(report.windows[1].domStatus, "stale_context");
  assert.equal(report.windows[1].embedCount, null);
  assert.equal(report.windows[1].postMessageAvailable, null);
  assert.equal(report.windows[1].routePath, null);
  assert.equal(report.windows[2].domStatus, "stale_context");
  assert.equal(report.windows[2].embedCount, null);
  assert.equal(report.windows[2].postMessageAvailable, null);
  assert.doesNotMatch(JSON.stringify(report), /webcontentsid|main\/message|2955610491/);
});

test("the total DOM deadline is bounded and late Electron promise rejection remains observed", async () => {
  const late = mockWindow(`${RENDERER_URL}#/official/group`, () => new Promise((_resolve, reject) => {
    setTimeout(() => reject(new Error("private late renderer error")), 30);
  }));
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([late.window]),
    timeoutMs: 5,
  });
  assert.equal(report.windows[0].domStatus, "timed_out");
  assert.equal(report.windows[0].embedCount, null);
  assert.equal(JSON.stringify(report).includes("private late renderer error"), false);
  await new Promise((resolve) => setTimeout(resolve, 40));

  const rejected = mockWindow(`${RENDERER_URL}#/official/room`, () => Promise.reject(new Error("private renderer error")));
  const rejectedReport = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([rejected.window]),
  });
  assert.equal(rejectedReport.windows[0].domStatus, "unavailable");
  assert.doesNotMatch(JSON.stringify(rejectedReport), /private renderer error/);
});

test("an expired deadline prevents starting another renderer evaluation", async () => {
  const slow = mockWindow(`${RENDERER_URL}#/official/group`, () => {
    assert.fail("no new DOM evaluation starts after the shared deadline");
  });
  slow.webContents.getURL = () => {
    const until = Date.now() + 8;
    while (Date.now() < until) { /* Simulate a synchronous Electron getter that cannot be preempted. */ }
    return RENDERER_URL;
  };
  const report = await probeOfficialWindows({
    processObject: browserProcess(),
    requireFunction: electronRequire([slow.window]),
    timeoutMs: 5,
  });
  assert.equal(slow.calls.includes("executeJavaScript"), false);
  assert.equal(report.windows[0].domStatus, "timed_out");
});

test("window enumeration is capped at sixteen and report validation rejects unsafe shapes", async () => {
  let highIndexRead = false;
  const windows = new Proxy(Array.from({ length: 19 }, () => null), {
    get(target, property, receiver) {
      if (/^(1[6-9]|[2-9][0-9]+)$/.test(String(property))) highIndexRead = true;
      return Reflect.get(target, property, receiver);
    },
  });
  const report = await probeOfficialWindows({ processObject: browserProcess(), requireFunction: electronRequire(windows) });
  assert.equal(report.windowCount, 16);
  assert.equal(report.windowsTruncated, true);
  assert.equal(highIndexRead, false);
  assert.equal(isOfficialWindowReport(report), true);
  assert.equal(isOfficialWindowReport({ ...report, extra: "not allowed" }), false);
  for (const unsafeRoute of ["/group/u_ABCDEFG", "/group/private-message-text", "/group/123", "/../group", "/%2e%2e/group"]) {
    assert.equal(isOfficialWindowReport({
      ...report,
      windows: [{ ...report.windows[0], routePath: unsafeRoute }, ...report.windows.slice(1)],
    }), false, `forged report route ${unsafeRoute} is rejected`);
  }
  assert.equal(isOfficialWindowReport({
    ...report,
    windows: [{ ...report.windows[0], rendererFileMatch: true, routePath: "/", domStatus: "not_queried" }, ...report.windows.slice(1)],
  }), true);
});
