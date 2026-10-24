"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder } = require("node:util");

const PINNED_HASHES = Object.freeze({
  archive: "9C9959CFA351E8597C008C944DD482F826A24967ABA607F56350B06AC56200E2",
  packageJson: "15557B5A5A49A9C866D21048316DB50723642E13067CA12AFB36AF35ED3ECDC9",
  wrapper: "29DE02C5BEC028643FC5D597E269F15BE8BC75F7EA1620E53B50C6FF900288F2",
});

const SOURCE_ENTRIES = Object.freeze([
  "app_launcher/index.js",
  "app_launcher/launcher.js",
  "background.js",
  "preload.js",
  "preload_chatwin.js",
  "renderer/index.html",
]);
const MAX_ARCHIVE_BYTES = 64 * 1024 * 1024;
const MAX_WRAPPER_BYTES = 256 * 1024 * 1024;
const MAX_MANIFEST_BYTES = 256 * 1024;
const MAX_ENTRY_BYTES = 256 * 1024;
const MAX_TOTAL_SOURCE_BYTES = 1024 * 1024;
const MAX_REPORT_BYTES = 768 * 1024;
const HASH = /^[A-F0-9]{64}$/;
const ARCHIVE_STAGE_CODES = new Set([
  "archive_lstat_failed", "archive_not_file", "archive_symlink", "archive_open_failed",
  "archive_fstat_before_failed", "archive_fstat_not_file", "archive_size_invalid", "archive_size_exceeded",
  "archive_open_size_mismatch", "archive_open_mtime_mismatch", "archive_open_dev_mismatch", "archive_open_ino_mismatch",
  "archive_read_failed", "archive_read_count_invalid", "archive_fstat_after_failed", "archive_after_stat_changed",
  "archive_hash_failed", "archive_open_dev_mismatch_fd_hash_matches", "archive_open_dev_mismatch_fd_hash_mismatch",
  "archive_open_dev_mismatch_fd_unverified",
]);
const PREFIX_CODES = new Set([
  "app_path_invalid", "physical_fs_unavailable", "original_fs_unverified",
  "archive_unavailable", "archive_hash_mismatch", "wrapper_unavailable",
  "wrapper_hash_mismatch", "package_manifest_unavailable", "package_manifest_hash_mismatch",
  "wire_budget_exceeded", ...ARCHIVE_STAGE_CODES,
]);
const ENTRY_CODES = new Set([
  "source_entry_unavailable", "source_entry_invalid", "source_entry_oversize", "source_entry_not_utf8",
  "source_total_limit_exceeded", "wire_budget_exceeded",
]);
const CREDENTIAL_ASSIGNMENT = /((?:["'`])?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|ipc[_-]?token|auth(?:entication)?[_-]?token|bearer[_-]?token|token|client[_-]?secret|secret|password|passwd|credential|authorization|cookie|api[_-]?key)(?:["'`])?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)/gi;
const BEARER_LITERAL = /\bBearer\s+[A-Za-z0-9._~+\/-]{12,}={0,2}/gi;
const URL_CREDENTIAL = /([?&](?:access_token|refresh_token|token|secret|password|api[_-]?key|authorization)=)[^&#\s"'`]+/gi;
const CSP_NONCE = /(\bnonce-)[A-Za-z0-9+/_=-]+/gi;
const ARCHIVE_STAGE_ERRORS = new WeakSet();

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function archiveStageError(code) {
  if (!ARCHIVE_STAGE_CODES.has(code)) return codedError("archive_unavailable");
  const error = codedError(code);
  ARCHIVE_STAGE_ERRORS.add(error);
  return error;
}

function isBrandedArchiveStageError(error) {
  return ARCHIVE_STAGE_ERRORS.has(error) && ARCHIVE_STAGE_CODES.has(error.code);
}

function sha256(bytes, cryptoModule = crypto) {
  return cryptoModule.createHash("sha256").update(bytes).digest("hex").toUpperCase();
}

function statMatches(left, right) {
  return left.size === right.size && left.mtimeMs === right.mtimeMs &&
    (left.dev === undefined || right.dev === undefined || left.dev === right.dev) &&
    (left.ino === undefined || right.ino === undefined || left.ino === right.ino);
}

function archiveOpenStatMismatches(left, right) {
  const mismatches = [];
  if (left.size !== right.size) mismatches.push("archive_open_size_mismatch");
  if (left.mtimeMs !== right.mtimeMs) mismatches.push("archive_open_mtime_mismatch");
  if (left.dev !== undefined && right.dev !== undefined && left.dev !== right.dev) mismatches.push("archive_open_dev_mismatch");
  if (left.ino !== undefined && right.ino !== undefined && left.ino !== right.ino) mismatches.push("archive_open_ino_mismatch");
  return mismatches;
}

function archiveDevMismatchFdHashStatus(fsModule, descriptor, before, maxBytes, cryptoModule, expectedHash) {
  try {
    if (!Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes) {
      return "archive_open_dev_mismatch_fd_unverified";
    }
    const digest = cryptoModule.createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let offset = 0;
    while (offset < before.size) {
      const requested = Math.min(buffer.length, before.size - offset);
      const count = fsModule.readSync(descriptor, buffer, 0, requested, offset);
      if (!Number.isInteger(count) || count <= 0 || count > requested) {
        return "archive_open_dev_mismatch_fd_unverified";
      }
      digest.update(buffer.subarray(0, count));
      offset += count;
    }
    const after = fsModule.fstatSync(descriptor);
    if (!after.isFile() || !statMatches(before, after)) return "archive_open_dev_mismatch_fd_unverified";
    return digest.digest("hex").toUpperCase() === expectedHash
      ? "archive_open_dev_mismatch_fd_hash_matches"
      : "archive_open_dev_mismatch_fd_hash_mismatch";
  } catch {
    return "archive_open_dev_mismatch_fd_unverified";
  }
}

function openPhysicalReadOnly(fsModule, filePath) {
  const constants = fsModule.constants || fs.constants;
  let flags = constants.O_RDONLY;
  if (Number.isInteger(constants.O_NOFOLLOW)) flags |= constants.O_NOFOLLOW;
  if (Number.isInteger(constants.O_NONBLOCK)) flags |= constants.O_NONBLOCK;
  return fsModule.openSync(filePath, flags);
}

function classifyPhysicalOpenFailure(fsModule, filePath, failure) {
  let linkInfo;
  try { linkInfo = fsModule.lstatSync(filePath); }
  catch { throw failure("archive_lstat_failed"); }
  if (linkInfo.isSymbolicLink()) throw failure("archive_symlink");
  if (!linkInfo.isFile()) throw failure("archive_not_file");
  throw failure("archive_open_failed");
}

function readBoundedPhysicalFile(fsModule, filePath, maxBytes, cryptoModule, keepBytes = false, archiveDiagnostics = false, archiveExpectedHash) {
  let descriptor = null;
  const failure = (code) => archiveDiagnostics ? archiveStageError(code) : codedError("file_unavailable");
  try {
    try { descriptor = openPhysicalReadOnly(fsModule, filePath); }
    catch { classifyPhysicalOpenFailure(fsModule, filePath, failure); }

    let before;
    try { before = fsModule.fstatSync(descriptor); }
    catch { throw failure("archive_fstat_before_failed"); }
    if (!before.isFile()) throw failure("archive_not_file");
    if (!Number.isSafeInteger(before.size) || before.size < 0) throw failure("archive_size_invalid");
    if (before.size > maxBytes) throw failure("archive_size_exceeded");
    let linkInfo;
    try { linkInfo = fsModule.lstatSync(filePath); }
    catch { throw failure("archive_lstat_failed"); }
    if (linkInfo.isSymbolicLink()) throw failure("archive_symlink");
    if (!linkInfo.isFile()) throw failure("archive_not_file");
    if (archiveDiagnostics) {
      const mismatches = archiveOpenStatMismatches(linkInfo, before);
      if (mismatches.length === 1 && mismatches[0] === "archive_open_dev_mismatch") {
        throw failure(archiveDevMismatchFdHashStatus(fsModule, descriptor, before, maxBytes, cryptoModule, archiveExpectedHash));
      }
      if (mismatches.length > 0) throw failure(mismatches[0]);
    } else if (!statMatches(linkInfo, before)) throw codedError("file_unavailable");

    let digest;
    try { digest = cryptoModule.createHash("sha256"); }
    catch { throw failure("archive_hash_failed"); }
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    const chunks = keepBytes ? [] : null;
    let offset = 0;
    while (offset < before.size) {
      const requested = Math.min(buffer.length, before.size - offset);
      let count;
      try { count = fsModule.readSync(descriptor, buffer, 0, requested, offset); }
      catch { throw failure("archive_read_failed"); }
      if (!Number.isInteger(count) || count <= 0 || count > requested) throw failure("archive_read_count_invalid");
      const chunk = buffer.subarray(0, count);
      try { digest.update(chunk); }
      catch { throw failure("archive_hash_failed"); }
      if (chunks) chunks.push(Buffer.from(chunk));
      offset += count;
    }
    let after;
    try { after = fsModule.fstatSync(descriptor); }
    catch { throw failure("archive_fstat_after_failed"); }
    let finalPathInfo;
    try { finalPathInfo = fsModule.lstatSync(filePath); }
    catch { throw failure("archive_after_stat_changed"); }
    if (finalPathInfo.isSymbolicLink() || !statMatches(before, after) || !statMatches(after, finalPathInfo)) {
      throw failure("archive_after_stat_changed");
    }
    let sha256;
    try { sha256 = digest.digest("hex").toUpperCase(); }
    catch { throw failure("archive_hash_failed"); }
    return {
      bytes: offset,
      sha256,
      contents: chunks ? Buffer.concat(chunks, offset) : undefined,
    };
  } catch (error) {
    if (archiveDiagnostics && isBrandedArchiveStageError(error)) throw error;
    throw codedError("file_unavailable");
  } finally {
    if (descriptor !== null) {
      try { fsModule.closeSync(descriptor); } catch { /* A completed read remains metadata-only. */ }
    }
  }
}

function hashBoundedFile(fsModule, filePath, maxBytes, cryptoModule, archiveDiagnostics = false, archiveExpectedHash) {
  return readBoundedPhysicalFile(fsModule, filePath, maxBytes, cryptoModule, false, archiveDiagnostics, archiveExpectedHash);
}

function readBoundedVirtualFile(fsModule, filePath, maxBytes) {
  let descriptor;
  try {
    descriptor = fsModule.openSync(filePath, "r");
    const before = fsModule.fstatSync(descriptor);
    if (!before.isFile() || !Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes) {
      throw codedError(before.size > maxBytes ? "source_entry_oversize" : "source_entry_invalid");
    }
    // ASAR descriptors read through Electron's virtual filesystem. The stat
    // size and decoded bytes are bounded independently because ASAR metadata
    // may describe stored bytes differently from the bytes returned by read.
    const chunks = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes + 1));
    let offset = 0;
    while (offset <= maxBytes) {
      const requested = Math.min(buffer.length, maxBytes + 1 - offset);
      if (requested <= 0) break;
      const count = fsModule.readSync(descriptor, buffer, 0, requested, offset);
      if (count === 0) break;
      offset += count;
      if (offset > maxBytes) throw codedError("source_entry_invalid");
      chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    const after = fsModule.fstatSync(descriptor);
    if (!after.isFile() || before.size !== after.size) throw codedError("source_entry_invalid");
    return Buffer.concat(chunks, offset);
  } catch (error) {
    if (error?.code === "source_entry_oversize" || error?.code === "source_entry_invalid") throw error;
    throw codedError("source_entry_unavailable");
  } finally {
    if (descriptor !== undefined) {
      try { fsModule.closeSync(descriptor); } catch { /* preserve the original read result or failure */ }
    }
  }
}

function isPhysicalFsApi(value) {
  return Boolean(value && ["lstatSync", "openSync", "fstatSync", "readSync", "closeSync"]
    .every((method) => typeof value[method] === "function"));
}

function loadOriginalFs() {
  let resolved;
  try { resolved = require.resolve("original-fs"); }
  catch { throw codedError("physical_fs_unavailable"); }
  if (resolved !== "original-fs") throw codedError("original_fs_unverified");

  let physicalFs;
  try { physicalFs = require("original-fs"); }
  catch { throw codedError("physical_fs_unavailable"); }
  if (!isPhysicalFsApi(physicalFs)) throw codedError("original_fs_unverified");
  return physicalFs;
}

function decodeStrictUtf8(bytes) {
  try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
  catch { throw codedError("source_entry_not_utf8"); }
}

function redactCredentialLiterals(text) {
  return text
    .replace(CREDENTIAL_ASSIGNMENT, (_match, prefix) => `${prefix}"<redacted>"`)
    .replace(URL_CREDENTIAL, (_match, prefix) => `${prefix}<redacted>`)
    .replace(CSP_NONCE, "$1<redacted>")
    .replace(BEARER_LITERAL, "Bearer <redacted>");
}

function attributeValue(attributes, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\s)${escaped}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, "i").exec(attributes);
  return match ? (match[1] ?? match[2] ?? match[3] ?? "") : null;
}

function extractHtmlScripts(text) {
  const inlineScripts = [];
  const scriptSrcDirectives = [];
  const scriptSources = [];
  const metaTags = text.match(/<meta\b[^>]*>/gi) || [];
  for (const tag of metaTags) {
    const httpEquiv = attributeValue(tag.slice(5, -1), "http-equiv");
    if (typeof httpEquiv !== "string" || httpEquiv.trim().toLowerCase() !== "content-security-policy") continue;
    const content = attributeValue(tag.slice(5, -1), "content");
    if (typeof content !== "string") continue;
    for (const directive of content.split(";")) {
      if (/^\s*script-src(?:\s|$)/i.test(directive)) {
        const safeDirective = redactCredentialLiterals(directive.trim());
        if (Buffer.byteLength(safeDirective, "utf8") > 16 * 1024) throw codedError("source_entry_invalid");
        scriptSrcDirectives.push(safeDirective);
      }
    }
  }
  const scriptTags = /<script\b([^>]*)>([\s\S]*?)<\/script(?=[\t\n\f\r />])[^>]*>/gi;
  let match;
  while ((match = scriptTags.exec(text)) !== null) {
    const src = attributeValue(match[1], "src");
    if (src !== null) {
      if (Buffer.byteLength(src, "utf8") > 2048) throw codedError("source_entry_invalid");
      scriptSources.push(redactCredentialLiterals(src));
      if (scriptSources.length > 256) throw codedError("source_entry_invalid");
      continue;
    }
    inlineScripts.push(redactCredentialLiterals(match[2]));
    if (inlineScripts.length > 256) throw codedError("source_entry_invalid");
  }
  return { inlineScripts, scriptSrcDirectives, scriptSources };
}

function makeRejectedReport(code) {
  return {
    schema: 1,
    status: "rejected",
    code,
    archiveSha256: null,
    packageJsonSha256: null,
    wrapperSha256: null,
    entries: [],
  };
}

function makeEntryFailure(label, code) {
  return { label, status: "unavailable", code };
}

function isExactObject(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key)));
}

function contentIsRedacted(content) {
  if (typeof content === "string") return redactCredentialLiterals(content) === content;
  if (!isExactObject(content, ["inlineScripts", "scriptSrcDirectives", "scriptSources"])) return false;
  return content.inlineScripts.every((text) => redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= MAX_ENTRY_BYTES) &&
    content.scriptSrcDirectives.every((text) => redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= 16 * 1024) &&
    content.scriptSources.every((text) => redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= 2048);
}

function isAsarSourceProbeReport(value, pins = PINNED_HASHES) {
  try {
    const reportKeys = ["schema", "status", "code", "archiveSha256", "packageJsonSha256", "wrapperSha256", "entries"];
    if (!isExactObject(value, reportKeys) || value.schema !== 1 || !Array.isArray(value.entries)) return false;
    if (value.status === "rejected") {
      if (!PREFIX_CODES.has(value.code) || value.archiveSha256 !== null || value.packageJsonSha256 !== null ||
          value.wrapperSha256 !== null || value.entries.length !== 0) return false;
    } else if (value.status === "complete") {
      if (value.code !== "ok" || value.archiveSha256 !== pins.archive ||
          value.packageJsonSha256 !== pins.packageJson || value.wrapperSha256 !== pins.wrapper ||
          value.entries.length !== SOURCE_ENTRIES.length) return false;
      let totalSourceBytes = 0;
      for (let index = 0; index < SOURCE_ENTRIES.length; index += 1) {
        const entry = value.entries[index];
        if (!entry || entry.label !== SOURCE_ENTRIES[index]) return false;
        if (entry.status === "read") {
          const keys = ["label", "status", "bytes", "sha256", "content"];
          if (!isExactObject(entry, keys) || !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > MAX_ENTRY_BYTES ||
              typeof entry.sha256 !== "string" || !HASH.test(entry.sha256)) return false;
          if (entry.label === "renderer/index.html") {
            if (!isExactObject(entry.content, ["inlineScripts", "scriptSrcDirectives", "scriptSources"]) || !Array.isArray(entry.content.inlineScripts) ||
                !Array.isArray(entry.content.scriptSrcDirectives) || !Array.isArray(entry.content.scriptSources) ||
                entry.content.inlineScripts.length > 256 || entry.content.scriptSrcDirectives.length > 64 || entry.content.scriptSources.length > 256 ||
                !entry.content.inlineScripts.every((item) => typeof item === "string") ||
                !entry.content.scriptSrcDirectives.every((item) => typeof item === "string") ||
                !entry.content.scriptSources.every((item) => typeof item === "string")) return false;
          } else if (typeof entry.content !== "string") return false;
          if (!contentIsRedacted(entry.content)) return false;
          totalSourceBytes += entry.bytes;
        } else if (entry.status === "unavailable") {
          if (!isExactObject(entry, ["label", "status", "code"]) || !ENTRY_CODES.has(entry.code)) return false;
        } else if (entry.status === "source_omitted") {
          if (!isExactObject(entry, ["label", "status", "code", "bytes", "sha256"]) || entry.code !== "wire_budget_exceeded" ||
              !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > MAX_ENTRY_BYTES ||
              typeof entry.sha256 !== "string" || !HASH.test(entry.sha256)) return false;
          totalSourceBytes += entry.bytes;
        } else return false;
      }
      if (totalSourceBytes > MAX_TOTAL_SOURCE_BYTES) return false;
    } else return false;
    return Buffer.byteLength(JSON.stringify(value), "utf8") <= MAX_REPORT_BYTES;
  } catch {
    return false;
  }
}

function createAsarSourceProbe({
  fsModule = fs,
  mockPhysicalFsModule,
  cryptoModule = crypto,
  pathModule = path,
  pins = PINNED_HASHES,
} = {}) {
  function probeAsarSources({ appPath } = {}) {
    if (typeof appPath !== "string" || !pathModule.isAbsolute(appPath) || appPath.includes("\0")) {
      return makeRejectedReport("app_path_invalid");
    }
    let physicalFsModule;
    try {
      physicalFsModule = mockPhysicalFsModule === undefined ? loadOriginalFs() : mockPhysicalFsModule;
      if (!isPhysicalFsApi(physicalFsModule)) throw codedError("original_fs_unverified");
    } catch (error) {
      const code = error?.code === "original_fs_unverified" ? "original_fs_unverified" : "physical_fs_unavailable";
      return makeRejectedReport(code);
    }
    const archivePath = pathModule.join(appPath, "application.asar");
    const wrapperPath = pathModule.join(appPath, "wrapper.node");
    let archiveDigest;
    try { archiveDigest = hashBoundedFile(physicalFsModule, archivePath, MAX_ARCHIVE_BYTES, cryptoModule, true, pins.archive); }
    catch (error) { return makeRejectedReport(isBrandedArchiveStageError(error) ? error.code : "archive_unavailable"); }
    if (archiveDigest.sha256 !== pins.archive) return makeRejectedReport("archive_hash_mismatch");
    let wrapperDigest;
    try { wrapperDigest = hashBoundedFile(physicalFsModule, wrapperPath, MAX_WRAPPER_BYTES, cryptoModule); }
    catch { return makeRejectedReport("wrapper_unavailable"); }
    if (wrapperDigest.sha256 !== pins.wrapper) return makeRejectedReport("wrapper_hash_mismatch");

    const packagePath = pathModule.join(appPath, "package.json");
    let packageRead;
    try { packageRead = readBoundedPhysicalFile(physicalFsModule, packagePath, MAX_MANIFEST_BYTES, cryptoModule, true); }
    catch { return makeRejectedReport("package_manifest_unavailable"); }
    if (packageRead.sha256 !== pins.packageJson) return makeRejectedReport("package_manifest_hash_mismatch");

    const report = {
      schema: 1,
      status: "complete",
      code: "ok",
      archiveSha256: pins.archive,
      packageJsonSha256: pins.packageJson,
      wrapperSha256: pins.wrapper,
      entries: [],
    };
    let totalSourceBytes = 0;
    for (const label of SOURCE_ENTRIES) {
      if (totalSourceBytes >= MAX_TOTAL_SOURCE_BYTES) {
        report.entries.push(makeEntryFailure(label, "source_total_limit_exceeded"));
        continue;
      }
      let bytes;
      try {
        const sourcePath = pathModule.join(appPath, "application.asar", ...label.split("/"));
        const remainingBudget = MAX_TOTAL_SOURCE_BYTES - totalSourceBytes;
        bytes = readBoundedVirtualFile(fsModule, sourcePath, Math.min(MAX_ENTRY_BYTES, remainingBudget));
      } catch (error) {
        const aggregateLimit = error?.code === "source_entry_oversize" && MAX_TOTAL_SOURCE_BYTES - totalSourceBytes < MAX_ENTRY_BYTES;
        const code = aggregateLimit ? "source_total_limit_exceeded" : ENTRY_CODES.has(error?.code) ? error.code : "source_entry_unavailable";
        if (aggregateLimit) totalSourceBytes = MAX_TOTAL_SOURCE_BYTES;
        report.entries.push(makeEntryFailure(label, code));
        continue;
      }
      if (totalSourceBytes + bytes.length > MAX_TOTAL_SOURCE_BYTES) {
        report.entries.push(makeEntryFailure(label, "source_total_limit_exceeded"));
        totalSourceBytes = MAX_TOTAL_SOURCE_BYTES;
        continue;
      }
      totalSourceBytes += bytes.length;
      const digest = sha256(bytes, cryptoModule);
      let text;
      try { text = decodeStrictUtf8(bytes); }
      catch { report.entries.push(makeEntryFailure(label, "source_entry_not_utf8")); continue; }
      let content;
      try {
        content = label === "renderer/index.html"
          ? extractHtmlScripts(text)
          : redactCredentialLiterals(text);
      } catch {
        report.entries.push(makeEntryFailure(label, "source_entry_invalid"));
        continue;
      }
      report.entries.push({ label, status: "read", bytes: bytes.length, sha256: digest, content });
    }

    while (Buffer.byteLength(JSON.stringify(report), "utf8") > MAX_REPORT_BYTES) {
      let index = report.entries.length - 1;
      while (index >= 0 && report.entries[index].status !== "read") index -= 1;
      if (index < 0) return makeRejectedReport("wire_budget_exceeded");
      const entry = report.entries[index];
      report.entries[index] = {
        label: entry.label,
        status: "source_omitted",
        code: "wire_budget_exceeded",
        bytes: entry.bytes,
        sha256: entry.sha256,
      };
    }
    return report;
  }

  return Object.freeze({ probeAsarSources });
}

const defaultProbe = createAsarSourceProbe();

module.exports = Object.freeze({
  PINNED_HASHES,
  SOURCE_ENTRIES,
  createAsarSourceProbe,
  isAsarSourceProbeReport,
  probeAsarSources: defaultProbe.probeAsarSources,
});
