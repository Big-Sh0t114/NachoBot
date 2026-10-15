"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const path = require("node:path");
const {
  PROOF_LEVEL,
  SOURCE_ENTRIES,
  MAX_ENTRY_BYTES,
  MAX_REPORT_BYTES,
  createAsarCandidateProbe,
  isAsarCandidateReport,
} = require("../agent/asar-candidate-probe.cjs");

function createFixture({ contents, throwOnRead = new Map(), reportedSizes = new Map() } = {}) {
  const root = path.resolve("C:/Program Files/Tencent/QQNT/versions/9.9.26-44343/resources/app");
  const calls = [];
  const files = new Map();
  for (const label of SOURCE_ENTRIES) {
    const content = contents?.get(label) ?? (label === "renderer/index.html" ? "<html></html>" : `// fixed source ${label}`);
    files.set(path.join(root, "application.asar", ...label.split("/")), Buffer.isBuffer(content) ? content : Buffer.from(content, "utf8"));
  }
  const fsModule = {
    statSync(filePath) {
      calls.push(["stat", filePath]);
      const bytes = files.get(filePath);
      if (!bytes) { const error = new Error("private path not found"); error.code = "ENOENT"; throw error; }
      return { isFile: () => true, size: reportedSizes.get(filePath) ?? bytes.length, mtimeMs: 100 };
    },
    readFileSync(filePath) {
      calls.push(["read", filePath]);
      if (throwOnRead.has(filePath)) throw throwOnRead.get(filePath);
      const bytes = files.get(filePath);
      if (!bytes) { const error = new Error("private path not found"); error.code = "ENOENT"; throw error; }
      return bytes;
    },
  };
  return { root, calls, files, fsModule, probe: createAsarCandidateProbe({ fsModule }) };
}

function secretFreeReport(report) {
  return JSON.stringify(report);
}

test("candidate probe reads only the fixed six Electron virtual paths and returns redacted unverified source", () => {
  const credentials = "ASAR-CANDIDATE-SECRET-DO-NOT-LEAK";
  const contents = new Map([
    ["app_launcher/index.js", `const token = "${credentials}"; const header = "Bearer abcdefghijklmnop";`],
    ["app_launcher/launcher.js", `const endpoint = "https://host/?token=${credentials}";`],
    ["background.js", "// background source"],
    ["preload.js", "// preload source"],
    ["preload_chatwin.js", "// chatwin source"],
    ["renderer/index.html", `<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'nonce-${credentials}'"><script src="bundle.js?token=${credentials}"></script><script>const apiKey = "${credentials}";</script><p>${credentials}</p>`],
  ]);
  const fixture = createFixture({ contents });
  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  assert.equal(report.status, "candidate");
  assert.equal(report.code, "ok");
  assert.equal(report.proofLevel, PROOF_LEVEL);
  assert.equal(isAsarCandidateReport(report), true);
  assert.deepEqual(report.entries.map((entry) => entry.label), SOURCE_ENTRIES);
  assert.equal(report.entries.at(-1).content.inlineScripts.length, 1);
  assert.equal(report.entries.at(-1).content.scriptSources.length, 1);
  assert.match(report.entries.at(-1).content.scriptSrcDirectives[0], /nonce-<redacted>/);
  assert.doesNotMatch(secretFreeReport(report), new RegExp(credentials));
  assert.doesNotMatch(secretFreeReport(report), /abcdefghijklmnop/);
  const expectedPaths = SOURCE_ENTRIES.map((label) => path.join(fixture.root, "application.asar", ...label.split("/")));
  assert.deepEqual([...new Set(fixture.calls.map((call) => call[1]))], expectedPaths);
  assert.equal(fixture.calls.filter(([op]) => op === "read").length, SOURCE_ENTRIES.length);
  assert.ok(fixture.calls.filter(([op]) => op === "stat").length === SOURCE_ENTRIES.length * 2);
});

test("candidate report validator rejects raw credentials, extra paths, bad proof levels, and oversized reports", () => {
  const fixture = createFixture();
  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  const unredacted = structuredClone(report);
  unredacted.entries[0].content = 'const token = "still-secret";';
  assert.equal(isAsarCandidateReport(unredacted), false);
  assert.equal(isAsarCandidateReport({ ...report, proofLevel: "verified" }), false);
  assert.equal(isAsarCandidateReport({ ...report, unexpected: "field" }), false);
  const malformedEntry = structuredClone(report);
  malformedEntry.entries[0].label = "../../package.json";
  assert.equal(isAsarCandidateReport(malformedEntry), false);
  const malformedHtml = structuredClone(report);
  malformedHtml.entries.at(-1).content = { inlineScripts: [], scriptSrcDirectives: [], scriptSources: [], html: "not allowed" };
  assert.equal(isAsarCandidateReport(malformedHtml), false);
  const oversized = structuredClone(report);
  oversized.entries[0].content = "x".repeat(MAX_ENTRY_BYTES + 1);
  assert.equal(isAsarCandidateReport(oversized), false);
  assert.ok(Buffer.byteLength(JSON.stringify(report), "utf8") <= MAX_REPORT_BYTES);
});

test("candidate probe maps malformed or unknown filesystem failures to fixed redacted codes", () => {
  const fixture = createFixture();
  const target = path.join(fixture.root, "application.asar", "background.js");
  const privateFailure = new Error(`disk failure at ${target} with body ASAR-RAW-ERROR`);
  privateFailure.code = "EACCES";
  fixture.probe = createAsarCandidateProbe({ fsModule: {
    ...fixture.fsModule,
    readFileSync(filePath) {
      if (filePath === target) throw privateFailure;
      return fixture.fsModule.readFileSync(filePath);
    },
  } });
  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "source_entry_unavailable");
  assert.equal(report.entries[2].code, "source_entry_unavailable");
  assert.equal(isAsarCandidateReport(report), true);
  assert.doesNotMatch(JSON.stringify(report), /EACCES|disk failure|ASAR-RAW-ERROR|C:\\Program/);
});

test("candidate probe rejects invalid UTF-8 and enforces entry and aggregate bounds before returning source", () => {
  const invalidUtf8 = createFixture({ contents: new Map([[SOURCE_ENTRIES[0], Buffer.from([0xff, 0xfe])]]) });
  const invalidReport = invalidUtf8.probe.probeAsarCandidates({ appPath: invalidUtf8.root });
  assert.equal(invalidReport.status, "rejected");
  assert.equal(invalidReport.code, "source_entry_not_utf8");
  assert.equal(isAsarCandidateReport(invalidReport), true);

  const oversized = createFixture();
  const oversizedPath = path.join(oversized.root, "application.asar", ...SOURCE_ENTRIES[0].split("/"));
  oversized.fsModule.statSync = (filePath) => ({ isFile: () => true, size: filePath === oversizedPath ? MAX_ENTRY_BYTES + 1 : 1, mtimeMs: 100 });
  const oversizedReport = oversized.probe.probeAsarCandidates({ appPath: oversized.root });
  assert.equal(oversizedReport.entries[0].code, "source_entry_oversize");
  assert.equal(oversized.calls.some(([op, filePath]) => op === "read" && filePath === oversizedPath), false);

  const full = new Map(SOURCE_ENTRIES.map((label) => [label, label === "renderer/index.html" ? "" : "z".repeat(MAX_ENTRY_BYTES)]));
  const aggregate = createFixture({ contents: full });
  const aggregateReport = aggregate.probe.probeAsarCandidates({ appPath: aggregate.root });
  assert.equal(aggregateReport.status, "rejected");
  assert.ok(aggregateReport.entries.some((entry) => entry.code === "source_total_limit_exceeded"));
  assert.equal(isAsarCandidateReport(aggregateReport), true);
  assert.ok(Buffer.byteLength(JSON.stringify(aggregateReport), "utf8") <= MAX_REPORT_BYTES);
});

test("candidate probe checks virtual stat size before read and validates returned bytes after read", () => {
  const fixture = createFixture();
  const target = path.join(fixture.root, "application.asar", ...SOURCE_ENTRIES[0].split("/"));
  const oversizedReturnedBuffer = Buffer.alloc(MAX_ENTRY_BYTES + 1, 0x61);
  const originalRead = fixture.fsModule.readFileSync;
  fixture.fsModule.statSync = (filePath) => {
    fixture.calls.push(["stat", filePath]);
    return { isFile: () => true, size: 1, mtimeMs: 100 };
  };
  fixture.fsModule.readFileSync = (filePath) => {
    fixture.calls.push(["read", filePath]);
    return filePath === target ? oversizedReturnedBuffer : originalRead(filePath);
  };

  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  assert.equal(report.entries[0].code, "source_entry_invalid");
  assert.equal(fixture.calls.some(([op, filePath]) => op === "read" && filePath === target), true,
    "returned-byte validation occurs after readFileSync has allocated the result");
  assert.equal(isAsarCandidateReport(report), true);
});

test("candidate probe bounds the JSON wire report by omitting tail source with fixed metadata", () => {
  const contents = new Map(SOURCE_ENTRIES.map((label) => [label, label === "renderer/index.html" ? "" : "\\".repeat(180 * 1024)]));
  const fixture = createFixture({ contents });
  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "wire_budget_exceeded");
  assert.ok(report.entries.some((entry) => entry.status === "source_omitted"));
  assert.equal(isAsarCandidateReport(report), true);
  assert.ok(Buffer.byteLength(JSON.stringify(report), "utf8") <= MAX_REPORT_BYTES);
});

test("candidate probe validates the app path before invoking its virtual filesystem", () => {
  const fixture = createFixture();
  const report = fixture.probe.probeAsarCandidates({ appPath: "relative-path" });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "app_path_invalid");
  assert.equal(fixture.calls.length, 0);
  assert.equal(isAsarCandidateReport(report), true);
});

test("source hashes describe bounded original virtual bytes while exposed content stays redacted", () => {
  const raw = Buffer.from('const password = "candidate-secret";', "utf8");
  const fixture = createFixture({ contents: new Map([[SOURCE_ENTRIES[0], raw]]) });
  const report = fixture.probe.probeAsarCandidates({ appPath: fixture.root });
  assert.equal(report.entries[0].bytes, raw.length);
  assert.equal(report.entries[0].sha256, crypto.createHash("sha256").update(raw).digest("hex").toUpperCase());
  assert.doesNotMatch(report.entries[0].content, /candidate-secret/);
});
