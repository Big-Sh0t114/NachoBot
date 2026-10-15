"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { rendererGoneDocument, writeHostCrash } = require("../host/status-report.cjs");

test("renderer-gone document accepts Electron reasons and only bounded allowlisted fields", () => {
  const valid = rendererGoneDocument({
    schema: 99,
    observedAtMs: 1791534600123,
    hostGeneration: 66,
    targetPid: 9540,
    rendererPid: 15640,
    reason: "memory-eviction",
    exitCode: 0xffffffff,
    shutdownInProgress: true,
    ready: true,
    pluginFound: true,
    hasPostMessage: true,
    callbackReady: true,
    token: "must-not-be-copied",
    payload: { private: true },
  });
  assert.deepEqual(valid, {
    schema: 1,
    observedAtMs: 1791534600123,
    hostGeneration: 66,
    targetPid: 9540,
    rendererPid: 15640,
    reason: "memory-eviction",
    exitCode: 0xffffffff,
    shutdownInProgress: true,
    ready: true,
    pluginFound: true,
    hasPostMessage: true,
    callbackReady: true,
  });

  const malformed = rendererGoneDocument({
    observedAtMs: Number.POSITIVE_INFINITY,
    hostGeneration: 0,
    targetPid: -1,
    rendererPid: "15640",
    reason: "arbitrary-reason-with-data",
    exitCode: 0x100000000,
    shutdownInProgress: "true",
    ready: 1,
    pluginFound: false,
    hasPostMessage: true,
    callbackReady: null,
    path: "private-path",
  });
  assert.deepEqual(malformed, {
    schema: 1,
    observedAtMs: null,
    hostGeneration: null,
    targetPid: null,
    rendererPid: null,
    reason: "unknown",
    exitCode: null,
    shutdownInProgress: false,
    ready: false,
    pluginFound: false,
    hasPostMessage: true,
    callbackReady: false,
  });
  assert.equal(Object.hasOwn(malformed, "path"), false);
});

test("private renderer-gone report is first-write-only and requires an absolute report path", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-host-crash-report-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const reportPath = path.join(root, "host-report");
  const target = `${reportPath}.host.crash.json`;
  const first = {
    observedAtMs: 100,
    hostGeneration: 66,
    targetPid: 9540,
    rendererPid: 15640,
    reason: "crashed",
    exitCode: -1073741819,
    shutdownInProgress: false,
    ready: true,
    pluginFound: true,
    hasPostMessage: true,
    callbackReady: true,
  };

  assert.equal(writeHostCrash("relative-report-path", first), false);
  assert.equal(fs.existsSync(target), false);
  assert.equal(writeHostCrash(reportPath, first), true);
  const original = fs.readFileSync(target, "utf8");
  assert.equal(writeHostCrash(reportPath, { ...first, reason: "clean-exit", observedAtMs: 200 }), false);
  assert.equal(fs.readFileSync(target, "utf8"), original);
  assert.deepEqual(JSON.parse(original), rendererGoneDocument(first));
  assert.deepEqual(fs.readdirSync(root), ["host-report.host.crash.json"]);
});
