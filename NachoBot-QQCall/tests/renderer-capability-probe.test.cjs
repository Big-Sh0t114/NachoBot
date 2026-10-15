"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { isRendererCapabilityReport, probeRendererCapabilities } = require("../agent/renderer-capability-probe.cjs");

function mockRequire(electronValue, resolved = "electron") {
  const calls = [];
  const fn = (name) => { calls.push(["load", name]); return electronValue; };
  fn.resolve = (name) => { calls.push(["resolve", name]); return resolved; };
  fn.calls = calls;
  return fn;
}

test("renderer capability probe reads only bounded Electron main-window metadata", () => {
  const windows = Array.from({ length: 19 }, (_, index) => ({
    id: `private-window-${index}`,
    isDestroyed: () => index === 4,
    webContents: {
      isDestroyed: () => false,
      getType() { throw new Error("must_not_call_getType"); },
      executeJavaScript() { throw new Error("must_not_call_executeJavaScript"); },
      debugger: { attach() { throw new Error("must_not_attach_debugger"); } },
      getURL() { throw new Error("must_not_read_url"); },
    },
    getTitle() { throw new Error("must_not_read_title"); },
  }));
  const requireFunction = mockRequire({
    app: { isReady: () => true },
    BrowserWindow: Object.assign(function BrowserWindow() {}, { getAllWindows: () => windows }),
  });
  const report = probeRendererCapabilities({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction,
  });
  assert.equal(report.windowCount, 16);
  assert.equal(report.windowsTruncated, true);
  assert.equal(report.windows[4].destroyed, true);
  assert.equal(report.windows[0].getTypeAvailable, true);
  assert.equal(report.windows[0].executeJavaScriptAvailable, true);
  assert.equal(report.windows[0].debuggerAvailable, true);
  assert.deepEqual(requireFunction.calls, [["resolve", "electron"], ["load", "electron"]]);
  assert.equal(isRendererCapabilityReport(report), true);
  assert.equal(JSON.stringify(report).includes("private-window"), false);
  assert.equal(JSON.stringify(report).includes("must_not_"), false);
});

test("non-browser or non-Electron processes never resolve or load the Electron module", () => {
  for (const processObject of [
    { type: "renderer", versions: { electron: "test" } },
    { type: "browser", versions: { node: "test" } },
    { type: "surprise", versions: { electron: "test" } },
  ]) {
    const requireFunction = mockRequire({});
    const report = probeRendererCapabilities({ processObject, requireFunction });
    assert.equal(report.windowCount, 0);
    assert.deepEqual(requireFunction.calls, []);
    assert.equal(isRendererCapabilityReport(report), true);
  }
});

test("a non-bare Electron resolution is rejected before module load", () => {
  const requireFunction = mockRequire({}, "C:\\private\\node_modules\\electron\\index.js");
  const report = probeRendererCapabilities({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction,
  });
  assert.equal(report.electronBuiltin, false);
  assert.deepEqual(requireFunction.calls, [["resolve", "electron"]]);
  assert.equal(isRendererCapabilityReport(report), true);
});

test("malformed Electron capabilities degrade to a fixed negative report without raw errors", () => {
  const requireFunction = mockRequire({
    app: null,
    BrowserWindow: Object.assign(function BrowserWindow() {}, { getAllWindows: () => { throw new Error("sensitive path"); } }),
  });
  const report = probeRendererCapabilities({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction,
  });
  assert.equal(report.electronBuiltin, true);
  assert.equal(report.getAllWindowsAvailable, true);
  assert.equal(report.windowCount, 0);
  assert.equal(JSON.stringify(report).includes("sensitive"), false);
  assert.equal(isRendererCapabilityReport(report), true);
  assert.equal(isRendererCapabilityReport({ ...report, extra: "no" }), false);
});

test("hostile Electron metadata accessors and malformed report proxies fail closed", () => {
  const windows = new Proxy([{}], {
    get(target, property, receiver) {
      if (property === "0") throw new Error("private accessor text");
      return Reflect.get(target, property, receiver);
    },
  });
  const requireFunction = mockRequire({
    app: { isReady() {} },
    BrowserWindow: Object.assign(function BrowserWindow() {}, { getAllWindows: () => windows }),
  });
  const report = probeRendererCapabilities({
    processObject: { type: "browser", versions: { electron: "test" } },
    requireFunction,
  });
  assert.equal(report.windowCount, 1);
  assert.equal(report.windows[0].webContentsAvailable, false);
  assert.equal(isRendererCapabilityReport(report), true);
  assert.doesNotMatch(JSON.stringify(report), /private accessor text/);

  const revoked = Proxy.revocable({}, {});
  revoked.revoke();
  assert.equal(isRendererCapabilityReport(revoked.proxy), false);
});
