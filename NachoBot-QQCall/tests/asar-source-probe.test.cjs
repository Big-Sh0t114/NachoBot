"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {
  PINNED_HASHES,
  SOURCE_ENTRIES,
  createAsarSourceProbe,
  isAsarSourceProbeReport,
} = require("../agent/asar-source-probe.cjs");

function digest(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

function fixture({ sources = {}, packageJson = Buffer.from('{"name":"fixture"}'), archive = Buffer.from("archive"), wrapper = Buffer.from("wrapper") } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-asar-source-probe-"));
  const archivePath = path.join(root, "application.asar");
  const wrapperPath = path.join(root, "wrapper.node");
  const packagePath = path.join(root, "package.json");
  const virtualRoot = path.join(root, "virtual-asar");
  fs.writeFileSync(archivePath, archive);
  fs.writeFileSync(wrapperPath, wrapper);
  fs.writeFileSync(packagePath, packageJson);
  fs.mkdirSync(virtualRoot, { recursive: true });
  for (const label of SOURCE_ENTRIES) {
    const filePath = path.join(virtualRoot, ...label.split("/"));
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, sources[label] ?? "// harmless fixture\n", typeof (sources[label] ?? "// harmless fixture\n") === "string" ? "utf8" : undefined);
  }
  const virtualPath = (filePath) => {
    const prefix = `${archivePath}${path.sep}`;
    return filePath.startsWith(prefix) ? path.join(virtualRoot, filePath.slice(prefix.length)) : filePath;
  };
  const virtualFs = {
    ...fs,
    lstatSync: (filePath) => fs.lstatSync(filePath),
    openSync: (filePath, flags) => fs.openSync(filePath, flags),
    fstatSync: (descriptor) => fs.fstatSync(descriptor),
    readSync: (...args) => fs.readSync(...args),
    closeSync: (descriptor) => fs.closeSync(descriptor),
    statSync: (filePath) => fs.statSync(virtualPath(filePath)),
    readFileSync: (filePath, ...args) => fs.readFileSync(virtualPath(filePath), ...args),
  };
  return {
    root,
    archivePath,
    wrapperPath,
    packagePath,
    virtualRoot,
    virtualFs,
    pins: { archive: digest(archive), packageJson: digest(packageJson), wrapper: digest(wrapper) },
  };
}

function cleanup(fx) {
  fs.rmSync(fx.root, { recursive: true, force: true });
}

function probeFixture(fx, options = {}) {
  return createAsarSourceProbe({
    fsModule: options.fsModule ?? fx.virtualFs,
    mockPhysicalFsModule: options.mockPhysicalFsModule ?? fs,
    cryptoModule: options.cryptoModule ?? crypto,
    pins: options.pins ?? fx.pins,
  });
}

function nativeErrorWithCode(code) {
  const error = new Error("native physical I/O failure");
  error.code = code;
  return error;
}

function makeStat(base, overrides = {}) {
  return {
    ...base,
    ...overrides,
    isFile: overrides.isFile ?? (() => true),
    isSymbolicLink: overrides.isSymbolicLink ?? (() => false),
  };
}

function archiveStageFs(fx, stage) {
  let archiveDescriptor = null;
  let fstatCalls = 0;
  const calls = [];
  const opened = [];
  const closed = [];
  const physicalFs = {
    ...fs,
    lstatSync(filePath, ...args) {
      if (filePath !== fx.archivePath) return fs.lstatSync(filePath, ...args);
      calls.push(["lstat", filePath]);
      if (stage === "archive_lstat_failed") throw nativeErrorWithCode("archive_read_failed");
      const stat = fs.lstatSync(filePath, ...args);
      if (stage === "archive_not_file") return makeStat(stat, { isFile: () => false });
      if (stage === "archive_symlink") return makeStat(stat, { isFile: () => false, isSymbolicLink: () => true });
      if (stage === "archive_open_ino_mismatch") return makeStat(stat, { ino: 1 });
      if (stage === "archive_open_dev_and_ino_mismatch") return makeStat(stat, { dev: 1, ino: 1 });
      if (stage === "archive_open_dev_mismatch" || stage.startsWith("archive_open_dev_mismatch_fd_")) {
        return makeStat(stat, { dev: 1 });
      }
      if (stage === "unknown") return null;
      return stat;
    },
    openSync(filePath, ...args) {
      if (filePath !== fx.archivePath) return fs.openSync(filePath, ...args);
      calls.push(["open", filePath]);
      if (stage === "archive_open_failed") throw nativeErrorWithCode("archive_hash_failed");
      archiveDescriptor = fs.openSync(filePath, ...args);
      opened.push(archiveDescriptor);
      return archiveDescriptor;
    },
    fstatSync(descriptor, ...args) {
      if (descriptor !== archiveDescriptor) return fs.fstatSync(descriptor, ...args);
      fstatCalls += 1;
      calls.push(["fstat", descriptor, fstatCalls]);
      if (fstatCalls === 1 && stage === "archive_fstat_before_failed") throw nativeErrorWithCode("archive_hash_failed");
      if (fstatCalls === 2 && stage === "archive_fstat_after_failed") throw nativeErrorWithCode("archive_read_failed");
      const stat = fs.fstatSync(descriptor, ...args);
      if (fstatCalls === 1 && stage === "archive_fstat_not_file") return makeStat(stat, { isFile: () => false });
      if (fstatCalls === 1 && stage === "archive_size_invalid") return makeStat(stat, { size: Number.NaN });
      if (fstatCalls === 1 && stage === "archive_size_exceeded") return makeStat(stat, { size: 64 * 1024 * 1024 + 1 });
      if (fstatCalls === 1 && stage === "archive_open_size_mismatch") return makeStat(stat, { size: stat.size + 1 });
      if (fstatCalls === 1 && stage === "archive_open_mtime_mismatch") return makeStat(stat, { mtimeMs: stat.mtimeMs + 1 });
      if (stage === "archive_open_dev_and_ino_mismatch") return makeStat(stat, { dev: 2, ino: 2 });
      if (stage === "archive_open_dev_mismatch" || stage.startsWith("archive_open_dev_mismatch_fd_")) {
        if (fstatCalls === 2 && stage === "archive_open_dev_mismatch_fd_poststat_changed") {
          return makeStat(stat, { dev: 2, mtimeMs: stat.mtimeMs + 1 });
        }
        return makeStat(stat, { dev: 2 });
      }
      if (stage === "archive_open_ino_mismatch") return makeStat(stat, { ino: 2 });
      if (fstatCalls === 2 && stage === "archive_after_stat_changed") return makeStat(stat, { mtimeMs: stat.mtimeMs + 1 });
      return stat;
    },
    readSync(descriptor, ...args) {
      if (descriptor !== archiveDescriptor) return fs.readSync(descriptor, ...args);
      calls.push(["read", descriptor]);
      if (stage === "archive_open_dev_mismatch_fd_read_failed") {
        throw nativeErrorWithCode("archive_open_dev_mismatch_fd_hash_matches");
      }
      if (stage === "archive_read_failed") throw nativeErrorWithCode("archive_hash_failed");
      if (stage === "archive_read_count_invalid") return 0;
      return fs.readSync(descriptor, ...args);
    },
    closeSync(descriptor, ...args) {
      if (descriptor === archiveDescriptor) closed.push(descriptor);
      return fs.closeSync(descriptor, ...args);
    },
  };
  return { physicalFs, calls, opened, closed };
}

test("ASAR source probe pins exact installed inputs and a fixed entry allowlist", () => {
  assert.deepEqual(PINNED_HASHES, {
    archive: "9C9959CFA351E8597C008C944DD482F826A24967ABA607F56350B06AC56200E2",
    packageJson: "15557B5A5A49A9C866D21048316DB50723642E13067CA12AFB36AF35ED3ECDC9",
    wrapper: "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2",
  });
  assert.deepEqual(SOURCE_ENTRIES, [
    "app_launcher/index.js", "app_launcher/launcher.js", "background.js", "preload.js", "preload_chatwin.js", "renderer/index.html",
  ]);
});

test("ASAR source reader verifies archive, physical package and wrapper before fixed UTF-8 entries", (t) => {
  const secret = "ASAR-TEST-CREDENTIAL-SECRET";
  const fx = fixture({ sources: {
    "app_launcher/index.js": `const token = "${secret}"; module.exports = token;`,
    "app_launcher/launcher.js": `const headers = { Authorization: "Bearer ${secret}xxxxxxxx" };`,
    "renderer/index.html": `<html><head><meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'nonce-${secret}'"></head><body>STATIC_BODY_${secret}<script>const apiKey = '${secret}';</script><script src="external.js?token=${secret}">EXTERNAL_BODY_${secret}</script></body></html>`,
  } });
  t.after(() => cleanup(fx));
  let packageRead = false;
  let sourceReads = 0;
  const physicalFsSpy = {
    ...fs,
    openSync(filePath, ...args) {
      if (filePath === fx.packagePath) packageRead = true;
      return fs.openSync(filePath, ...args);
    },
  };
  const fsSpy = {
    ...fx.virtualFs,
    readFileSync(filePath, ...args) {
      if (SOURCE_ENTRIES.some((entry) => filePath.endsWith(entry.replaceAll("/", path.sep)))) {
        sourceReads += 1;
        assert.equal(packageRead, true, "manifest hash is checked before source entries are accessed");
      }
      return fx.virtualFs.readFileSync(filePath, ...args);
    },
  };
  const report = probeFixture(fx, { fsModule: fsSpy, mockPhysicalFsModule: physicalFsSpy }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.status, "complete");
  assert.equal(sourceReads, SOURCE_ENTRIES.length);
  assert.equal(isAsarSourceProbeReport(report, fx.pins), true);
  const encoded = JSON.stringify(report);
  assert.doesNotMatch(encoded, new RegExp(secret));
  assert.doesNotMatch(encoded, /STATIC_BODY_|EXTERNAL_BODY_/);
  const html = report.entries.find((entry) => entry.label === "renderer/index.html");
  assert.deepEqual(html.content.inlineScripts, [`const apiKey = "<redacted>";`]);
  assert.deepEqual(html.content.scriptSrcDirectives, [`script-src 'self' 'nonce-<redacted>'`]);
  assert.deepEqual(html.content.scriptSources, [`external.js?token=<redacted>`]);
  assert.match(report.entries[0].content, /"<redacted>"/);
  assert.match(report.entries[1].content, /Authorization: "<redacted>"/);
  assert.equal(report.entries[0].sha256, digest(Buffer.from(`const token = "${secret}"; module.exports = token;`)));
  assert.equal(isAsarSourceProbeReport({ ...report, unexpected: "private" }, fx.pins), false);
  const unredacted = structuredClone(report);
  unredacted.entries[0].content = `const token = "${secret}";`;
  assert.equal(isAsarSourceProbeReport(unredacted, fx.pins), false);
});

test("virtual ASAR directory uses node:fs for decoded entries while original-fs hashes physical files", (t) => {
  const decodedSource = Buffer.alloc(59, 0x78);
  const fx = fixture({ sources: { "app_launcher/index.js": decodedSource } });
  t.after(() => cleanup(fx));
  const virtualPhysicalTouches = [];
  const virtualSourceReads = [];
  const archiveEntryPrefix = fx.archivePath + path.sep;
  const virtualFs = {
    ...fx.virtualFs,
    lstatSync(filePath) {
      virtualPhysicalTouches.push(["lstat", filePath]);
      if (filePath === fx.archivePath) {
        return { size: 0, isFile: () => false, isDirectory: () => true, isSymbolicLink: () => false };
      }
      throw new Error("virtual node:fs has no physical files");
    },
    openSync(filePath) {
      virtualPhysicalTouches.push(["open", filePath]);
      throw new Error("virtual node:fs cannot open the raw archive");
    },
    statSync(filePath) {
      if (filePath === fx.archivePath) {
        return { size: 0, isFile: () => false, isDirectory: () => true };
      }
      const stats = fx.virtualFs.statSync(filePath);
      if (filePath === path.join(fx.archivePath, "app_launcher", "index.js")) {
        return { ...stats, size: 80, isFile: () => true, isSymbolicLink: () => false };
      }
      return stats;
    },
    readFileSync(filePath, ...args) {
      virtualSourceReads.push(filePath);
      assert.ok(filePath.startsWith(archiveEntryPrefix), "node:fs reads only virtual source entries");
      return fx.virtualFs.readFileSync(filePath, ...args);
    },
  };
  const physicalPaths = [];
  const originalFsMock = {
    ...fs,
    lstatSync(filePath, ...args) { physicalPaths.push(["lstat", filePath]); return fs.lstatSync(filePath, ...args); },
    openSync(filePath, ...args) { physicalPaths.push(["open", filePath]); return fs.openSync(filePath, ...args); },
  };
  const noAsarBefore = Object.getOwnPropertyDescriptor(process, "noAsar");

  const report = probeFixture(fx, { fsModule: virtualFs, mockPhysicalFsModule: originalFsMock })
    .probeAsarSources({ appPath: fx.root });

  assert.equal(report.status, "complete");
  assert.equal(report.archiveSha256, fx.pins.archive);
  assert.equal(report.packageJsonSha256, fx.pins.packageJson);
  assert.equal(report.wrapperSha256, fx.pins.wrapper);
  assert.equal(report.entries[0].bytes, 59, "decoded bytes have their own entry bound and count");
  assert.equal(report.entries[0].content, decodedSource.toString("utf8"));
  assert.deepEqual(virtualPhysicalTouches, [], "physical archive hashing never uses patched node:fs");
  assert.equal(virtualSourceReads.length, SOURCE_ENTRIES.length, "virtual entries still use node:fs decoding");
  assert.deepEqual(physicalPaths.filter(([operation]) => operation === "open").map(([, filePath]) => filePath), [
    fx.archivePath, fx.wrapperPath, fx.packagePath,
  ]);
  assert.equal(physicalPaths.some(([, filePath]) => filePath.startsWith(archiveEntryPrefix)), false);
  assert.deepEqual(Object.getOwnPropertyDescriptor(process, "noAsar"), noAsarBefore);
});

test("default probe fails closed before file reads when original-fs is unavailable or unverified", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  const noAsarBefore = Object.getOwnPropertyDescriptor(process, "noAsar");
  const physicalMethods = ["lstatSync", "openSync", "fstatSync", "readSync", "closeSync"];
  const originalMethods = new Map(physicalMethods.map((name) => [name, fs[name]]));
  let physicalCalls = 0;
  for (const name of physicalMethods) {
    fs[name] = function (...args) {
      physicalCalls += 1;
      return originalMethods.get(name).apply(fs, args);
    };
  }
  let report;
  try {
    report = require("../agent/asar-source-probe.cjs").probeAsarSources({ appPath: fx.root });
  } finally {
    for (const name of physicalMethods) fs[name] = originalMethods.get(name);
  }

  assert.equal(report.status, "rejected");
  assert.ok(["physical_fs_unavailable", "original_fs_unverified"].includes(report.code));
  assert.equal(physicalCalls, 0, "an absent or unverified builtin is rejected before physical reads");
  assert.equal(isAsarSourceProbeReport(report), true);
  assert.deepEqual(Object.getOwnPropertyDescriptor(process, "noAsar"), noAsarBefore);
});

test("original-fs npm path is rejected without loading the package and resolution stays lazy", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  const nodePathRoot = fs.mkdtempSync(path.join(os.tmpdir(), "qqcall-original-fs-package-"));
  t.after(() => fs.rmSync(nodePathRoot, { recursive: true, force: true }));
  const packageDirectory = path.join(nodePathRoot, "original-fs");
  fs.mkdirSync(packageDirectory, { recursive: true });
  const sentinelPath = path.join(nodePathRoot, "loaded.txt");
  fs.writeFileSync(path.join(packageDirectory, "index.js"),
    'require("node:fs").writeFileSync(process.env.ORIGINAL_FS_SENTINEL, "loaded"); module.exports = require("node:fs");\n', "utf8");

  const childSource = [
    'const Module = require("node:module");',
    'const originalResolveFilename = Module._resolveFilename;',
    "let originalFsResolutions = 0;",
    "Module._resolveFilename = function(request, ...args) {",
    '  if (request === "original-fs") originalFsResolutions += 1;',
    "  return originalResolveFilename.call(this, request, ...args);",
    "};",
    "const probe = require(process.env.ASAR_PROBE_MODULE);",
    "const createdAtResolutionCount = originalFsResolutions;",
    "const report = probe.probeAsarSources({ appPath: process.env.ASAR_APP_PATH });",
    "process.stdout.write(JSON.stringify({ createdAtResolutionCount, originalFsResolutions, report }));",
  ].join("\n");
  const child = spawnSync(process.execPath, ["-e", childSource], {
    encoding: "utf8",
    timeout: 10000,
    env: {
      ...process.env,
      NODE_PATH: nodePathRoot,
      ASAR_PROBE_MODULE: require.resolve("../agent/asar-source-probe.cjs"),
      ASAR_APP_PATH: fx.root,
      ORIGINAL_FS_SENTINEL: sentinelPath,
    },
  });

  assert.equal(child.status, 0, child.stderr);
  const result = JSON.parse(child.stdout);
  assert.equal(result.createdAtResolutionCount, 0, "creating the probe does not resolve original-fs");
  assert.equal(result.originalFsResolutions, 1, "one explicit probe performs one builtin resolution");
  assert.equal(result.report.status, "rejected");
  assert.equal(result.report.code, "original_fs_unverified");
  assert.equal(isAsarSourceProbeReport(result.report), true);
  assert.equal(fs.existsSync(sentinelPath), false, "an npm fallback path is rejected before require() loads it");
  assert.equal(child.stdout.includes(nodePathRoot), false);
  assert.equal(child.stdout.includes(fx.root), false);
});

test("archive physical-I/O stages use only branded fixed codes and always close opened descriptors", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  const stages = [
    ["archive_lstat_failed", "archive_lstat_failed"], ["archive_not_file", "archive_not_file"],
    ["archive_symlink", "archive_symlink"], ["archive_open_failed", "archive_open_failed"],
    ["archive_fstat_before_failed", "archive_fstat_before_failed"], ["archive_fstat_not_file", "archive_fstat_not_file"],
    ["archive_size_invalid", "archive_size_invalid"], ["archive_size_exceeded", "archive_size_exceeded"],
    ["archive_open_size_mismatch", "archive_open_size_mismatch"], ["archive_open_mtime_mismatch", "archive_open_mtime_mismatch"],
    ["archive_open_dev_mismatch", "archive_open_dev_mismatch_fd_hash_matches"], ["archive_open_ino_mismatch", "archive_open_ino_mismatch"],
    ["archive_read_failed", "archive_read_failed"], ["archive_read_count_invalid", "archive_read_count_invalid"],
    ["archive_fstat_after_failed", "archive_fstat_after_failed"], ["archive_after_stat_changed", "archive_after_stat_changed"],
    ["archive_hash_failed", "archive_hash_failed"], ["unknown", "archive_unavailable"],
  ];
  const hashExpectedStages = new Set([
    "archive_open_dev_mismatch", "archive_read_failed", "archive_read_count_invalid",
    "archive_fstat_after_failed", "archive_after_stat_changed", "archive_hash_failed",
  ]);

  for (const [stage, expectedCode] of stages) {
    const stageFs = archiveStageFs(fx, stage);
    let virtualReads = 0;
    const virtualFs = {
      ...fx.virtualFs,
      statSync(...args) { virtualReads += 1; return fx.virtualFs.statSync(...args); },
      readFileSync(...args) { virtualReads += 1; return fx.virtualFs.readFileSync(...args); },
    };
    let hashCalls = 0;
    const cryptoModule = {
      ...crypto,
      createHash(...args) {
        hashCalls += 1;
        if (stage === "archive_hash_failed") throw nativeErrorWithCode("archive_open_dev_mismatch_fd_hash_matches");
        return crypto.createHash(...args);
      },
    };
    const report = probeFixture(fx, {
      fsModule: virtualFs,
      mockPhysicalFsModule: stageFs.physicalFs,
      cryptoModule,
    }).probeAsarSources({ appPath: fx.root });

    assert.equal(report.status, "rejected", stage);
    assert.equal(report.code, expectedCode, stage);
    assert.deepEqual(Object.keys(report), [
      "schema", "status", "code", "archiveSha256", "packageJsonSha256", "wrapperSha256", "entries",
    ], stage);
    assert.equal(isAsarSourceProbeReport(report), true, stage);
    assert.equal(virtualReads, 0, `${stage} stops before virtual source reads`);
    assert.equal(hashCalls, hashExpectedStages.has(stage) ? 1 : 0,
      `${stage} runs only the permitted hash attempt`);
    assert.ok(stageFs.calls
      .filter(([operation]) => operation === "lstat" || operation === "open")
      .every(([, filePath]) => filePath === fx.archivePath), stage);
    assert.equal(stageFs.closed.length, stageFs.opened.length, `${stage} closes each opened descriptor`);
    for (const descriptor of stageFs.opened) {
      assert.throws(() => fs.fstatSync(descriptor), { code: "EBADF" }, `${stage} leaves no open descriptor`);
    }
    assert.equal(JSON.stringify(report).includes(fx.root), false, `${stage} hides the physical path`);
    assert.equal(JSON.stringify(report).includes("native physical I/O failure"), false, `${stage} hides native messages`);
  }
  assert.equal(stages.length, 18);
});

test("sole archive device mismatch hashes the opened descriptor as a bounded diagnostic and still rejects", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  const cases = [
    { stage: "archive_open_dev_mismatch", pins: fx.pins, expectedCode: "archive_open_dev_mismatch_fd_hash_matches" },
    { stage: "archive_open_dev_mismatch", pins: { ...fx.pins, archive: "A".repeat(64) }, expectedCode: "archive_open_dev_mismatch_fd_hash_mismatch" },
    { stage: "archive_open_dev_mismatch_fd_read_failed", pins: fx.pins, expectedCode: "archive_open_dev_mismatch_fd_unverified" },
    { stage: "archive_open_dev_mismatch_fd_poststat_changed", pins: fx.pins, expectedCode: "archive_open_dev_mismatch_fd_unverified" },
    { stage: "archive_open_dev_mismatch_fd_hash_failed", pins: fx.pins, expectedCode: "archive_open_dev_mismatch_fd_unverified" },
    { stage: "archive_open_dev_and_ino_mismatch", pins: fx.pins, expectedCode: "archive_open_dev_mismatch", expectedHashCalls: 0 },
  ];

  for (const { stage, pins, expectedCode, expectedHashCalls = 1 } of cases) {
    const stageFs = archiveStageFs(fx, stage);
    let virtualReads = 0;
    let hashCalls = 0;
    const virtualFs = {
      ...fx.virtualFs,
      statSync(...args) { virtualReads += 1; return fx.virtualFs.statSync(...args); },
      readFileSync(...args) { virtualReads += 1; return fx.virtualFs.readFileSync(...args); },
    };
    const cryptoModule = {
      ...crypto,
      createHash(...args) {
        hashCalls += 1;
        if (stage === "archive_open_dev_mismatch_fd_hash_failed") {
          throw nativeErrorWithCode("archive_open_dev_mismatch_fd_hash_mismatch");
        }
        return crypto.createHash(...args);
      },
    };
    const report = probeFixture(fx, {
      fsModule: virtualFs,
      mockPhysicalFsModule: stageFs.physicalFs,
      cryptoModule,
      pins,
    }).probeAsarSources({ appPath: fx.root });

    assert.equal(report.status, "rejected", stage);
    assert.equal(report.code, expectedCode, stage);
    assert.deepEqual(Object.keys(report), [
      "schema", "status", "code", "archiveSha256", "packageJsonSha256", "wrapperSha256", "entries",
    ], stage);
    assert.equal(report.archiveSha256, null, stage);
    assert.equal(report.packageJsonSha256, null, stage);
    assert.equal(report.wrapperSha256, null, stage);
    assert.deepEqual(report.entries, [], stage);
    assert.equal(isAsarSourceProbeReport(report, pins), true, stage);
    assert.equal(hashCalls, expectedHashCalls, `${stage} hash-attempt count`);
    assert.equal(virtualReads, 0, `${stage} never proceeds to virtual source reads`);
    assert.equal(stageFs.opened.length, 1, `${stage} opened the archive descriptor`);
    assert.deepEqual(stageFs.closed, stageFs.opened, `${stage} closes the same descriptor`);
    assert.throws(() => fs.fstatSync(stageFs.opened[0]), { code: "EBADF" }, `${stage} leaves the descriptor closed`);
  }
});

test("physical wrapper and package errors keep their existing fixed codes", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  for (const [failedPath, expectedCode] of [
    [fx.wrapperPath, "wrapper_unavailable"],
    [fx.packagePath, "package_manifest_unavailable"],
  ]) {
    const physicalFs = {
      ...fs,
      lstatSync(filePath, ...args) {
        if (filePath === failedPath) throw nativeErrorWithCode("archive_read_failed");
        return fs.lstatSync(filePath, ...args);
      },
    };
    const report = probeFixture(fx, { mockPhysicalFsModule: physicalFs }).probeAsarSources({ appPath: fx.root });
    assert.equal(report.status, "rejected");
    assert.equal(report.code, expectedCode);
    assert.equal(isAsarSourceProbeReport(report), true);
  }
});

test("ASAR hash mismatch fails before manifest or source reads", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  let virtualReads = 0;
  const fsSpy = {
    ...fx.virtualFs,
    statSync(filePath) { virtualReads += 1; return fx.virtualFs.statSync(filePath); },
    readFileSync(filePath, ...args) { virtualReads += 1; return fx.virtualFs.readFileSync(filePath, ...args); },
  };
  const wrongPins = { ...fx.pins, archive: "A".repeat(64) };
  const report = probeFixture(fx, { fsModule: fsSpy, pins: wrongPins }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.status, "rejected");
  assert.equal(report.code, "archive_hash_mismatch");
  assert.equal(virtualReads, 0);
  assert.equal(isAsarSourceProbeReport(report), true);
});

test("wrapper hash mismatch also blocks the virtual manifest and all source entries", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  let virtualReads = 0;
  const fsSpy = {
    ...fx.virtualFs,
    statSync(filePath) { virtualReads += 1; return fx.virtualFs.statSync(filePath); },
    readFileSync(filePath, ...args) { virtualReads += 1; return fx.virtualFs.readFileSync(filePath, ...args); },
  };
  const wrongPins = { ...fx.pins, wrapper: "C".repeat(64) };
  const report = probeFixture(fx, { fsModule: fsSpy, pins: wrongPins }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.code, "wrapper_hash_mismatch");
  assert.equal(virtualReads, 0);
});

test("package mismatch blocks all source reads and only fixed entry errors are retained", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  let sourceReads = 0;
  const fsSpy = {
    ...fx.virtualFs,
    statSync(filePath) {
      if (SOURCE_ENTRIES.some((entry) => filePath.endsWith(entry.replaceAll("/", path.sep)))) sourceReads += 1;
      return fx.virtualFs.statSync(filePath);
    },
  };
  const wrongPins = { ...fx.pins, packageJson: "B".repeat(64) };
  const report = probeFixture(fx, { fsModule: fsSpy, pins: wrongPins }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.code, "package_manifest_hash_mismatch");
  assert.equal(sourceReads, 0);
  assert.equal(isAsarSourceProbeReport(report), true);
  const malformed = { ...report, code: "C:\\secret\\private-token" };
  assert.equal(isAsarSourceProbeReport(malformed), false);
});

test("package manifest is read from the physical admitted application root, never virtual ASAR", (t) => {
  const fx = fixture();
  t.after(() => cleanup(fx));
  const physicalPaths = [];
  const physicalFsSpy = {
    ...fs,
    lstatSync(filePath, ...args) { physicalPaths.push(filePath); return fs.lstatSync(filePath, ...args); },
    openSync(filePath, ...args) { physicalPaths.push(filePath); return fs.openSync(filePath, ...args); },
  };
  const virtualPaths = [];
  const fsSpy = {
    ...fx.virtualFs,
    statSync(filePath, ...args) { virtualPaths.push(filePath); return fx.virtualFs.statSync(filePath, ...args); },
    readFileSync(filePath, ...args) { virtualPaths.push(filePath); return fx.virtualFs.readFileSync(filePath, ...args); },
  };
  const report = probeFixture(fx, { fsModule: fsSpy, mockPhysicalFsModule: physicalFsSpy }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.status, "complete");
  assert.ok(physicalPaths.includes(fx.packagePath));
  assert.ok(!physicalPaths.some((filePath) => filePath === path.join(fx.archivePath, "package.json")));
  assert.ok(!virtualPaths.includes(fx.packagePath));
});

test("ASAR virtual reads allow decoded length to differ from stored stat size while bounding returned bytes", (t) => {
  const source = Buffer.alloc(59, 0x78);
  const fx = fixture({ sources: { "app_launcher/index.js": source } });
  t.after(() => cleanup(fx));
  const target = path.join(fx.archivePath, "app_launcher", "index.js");
  const fsSpy = {
    ...fx.virtualFs,
    statSync(filePath) {
      const stats = fx.virtualFs.statSync(filePath);
      if (filePath !== target) return stats;
      return { ...stats, size: 80, isFile: () => true, isSymbolicLink: () => false };
    },
  };
  const report = probeFixture(fx, { fsModule: fsSpy }).probeAsarSources({ appPath: fx.root });
  assert.equal(report.status, "complete");
  assert.equal(report.entries[0].bytes, 59);
  assert.equal(report.entries[0].content, source.toString("utf8"));
  assert.equal(isAsarSourceProbeReport(report, fx.pins), true);
});

test("source bounds, strict UTF-8 and serialized 768 KiB budget fail closed without chunks", (t) => {
  const oversized = Buffer.alloc(256 * 1024 + 1, 0x78);
  const invalidUtf8 = Buffer.from([0xc3, 0x28]);
  const fx = fixture({ sources: {
    "app_launcher/index.js": oversized,
    "app_launcher/launcher.js": invalidUtf8,
    background: "unused",
  } });
  t.after(() => cleanup(fx));
  const report = probeFixture(fx).probeAsarSources({ appPath: fx.root });
  assert.equal(report.entries[0].code, "source_entry_oversize");
  assert.equal(report.entries[1].code, "source_entry_not_utf8");
  assert.equal(isAsarSourceProbeReport(report, fx.pins), true);

  const largeSources = Object.fromEntries(SOURCE_ENTRIES.map((label) => [label, "x".repeat(200 * 1024)]));
  const largeFx = fixture({ sources: largeSources });
  t.after(() => cleanup(largeFx));
  const largeReport = probeFixture(largeFx).probeAsarSources({ appPath: largeFx.root });
  assert.equal(Buffer.byteLength(JSON.stringify(largeReport), "utf8") <= 768 * 1024, true);
  assert.ok(largeReport.entries.some((entry) => entry.code === "wire_budget_exceeded"));
  assert.equal(isAsarSourceProbeReport(largeReport, largeFx.pins), true);
});

test("aggregate source read budget stops before exceeding one MiB", (t) => {
  const sources = Object.fromEntries(SOURCE_ENTRIES.map((label) => [label, "x".repeat(200 * 1024)]));
  const fx = fixture({ sources });
  t.after(() => cleanup(fx));
  let sourceReads = 0;
  const fsSpy = {
    ...fx.virtualFs,
    readFileSync(filePath, ...args) {
      if (SOURCE_ENTRIES.some((entry) => filePath.endsWith(entry.replaceAll("/", path.sep)))) sourceReads += 1;
      return fx.virtualFs.readFileSync(filePath, ...args);
    },
  };
  const report = probeFixture(fx, { fsModule: fsSpy }).probeAsarSources({ appPath: fx.root });
  assert.equal(sourceReads, 5);
  assert.equal(report.entries[5].code, "source_total_limit_exceeded");
  assert.equal(isAsarSourceProbeReport(report, fx.pins), true);
});
