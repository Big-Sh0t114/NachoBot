"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { TextDecoder } = require("node:util");

const SCHEMA = "qqcall.asar.candidates.v1";
const PROOF_LEVEL = "candidate_unverified";
const SOURCE_ENTRIES = Object.freeze([
  "app_launcher/index.js",
  "app_launcher/launcher.js",
  "background.js",
  "preload.js",
  "preload_chatwin.js",
  "renderer/index.html",
]);
const MAX_ENTRY_BYTES = 256 * 1024;
const MAX_TOTAL_SOURCE_BYTES = 1024 * 1024;
const MAX_REPORT_BYTES = 768 * 1024;
const HASH = /^[A-F0-9]{64}$/;
const CANDIDATE_CODES = new Set([
  "ok", "app_path_invalid", "virtual_fs_unavailable", "source_entry_unavailable",
  "source_entry_invalid", "source_entry_oversize", "source_entry_not_utf8",
  "source_total_limit_exceeded", "wire_budget_exceeded",
]);
const ENTRY_CODES = new Set([
  "app_path_invalid", "virtual_fs_unavailable", "source_entry_unavailable",
  "source_entry_invalid", "source_entry_oversize", "source_entry_not_utf8",
  "source_total_limit_exceeded", "wire_budget_exceeded",
]);

// Keep the established source redaction patterns local to this diagnostic path;
// the original, now-failing physical-source probe remains byte-for-byte intact.
const CREDENTIAL_ASSIGNMENT = /((?:["'`])?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|ipc[_-]?token|auth(?:entication)?[_-]?token|bearer[_-]?token|token|client[_-]?secret|secret|password|passwd|credential|authorization|cookie|api[_-]?key)(?:["'`])?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)/gi;
const BEARER_LITERAL = /\bBearer\s+[A-Za-z0-9._~+\/-]{12,}={0,2}/gi;
const URL_CREDENTIAL = /([?&](?:access_token|refresh_token|token|secret|password|api[_-]?key|authorization)=)[^&#\s"'`]+/gi;
const CSP_NONCE = /(\bnonce-)[A-Za-z0-9+/_=-]+/gi;

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function isExactObject(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key)));
}

function sha256(bytes, cryptoModule = crypto) {
  return cryptoModule.createHash("sha256").update(bytes).digest("hex").toUpperCase();
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
        if (scriptSrcDirectives.length > 64) throw codedError("source_entry_invalid");
      }
    }
  }
  const scriptTags = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
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

function readBoundedVirtualFile(fsModule, filePath, maxBytes) {
  try {
    const before = fsModule.statSync(filePath);
    if (!before.isFile() || !Number.isSafeInteger(before.size) || before.size < 0 || before.size > maxBytes) {
      throw codedError(before.size > maxBytes ? "source_entry_oversize" : "source_entry_invalid");
    }
    const bytes = fsModule.readFileSync(filePath);
    // Independently bound the bytes returned by the virtual filesystem. The
    // returned-length check follows readFileSync's allocation, so it validates
    // the result size but is not a hard allocation cap.
    if (!Buffer.isBuffer(bytes) || bytes.length > maxBytes) throw codedError("source_entry_invalid");
    const after = fsModule.statSync(filePath);
    if (!after.isFile() || before.size !== after.size || before.mtimeMs !== after.mtimeMs) {
      throw codedError("source_entry_invalid");
    }
    return bytes;
  } catch (error) {
    if (ENTRY_CODES.has(error?.code) && error.code !== "app_path_invalid" && error.code !== "virtual_fs_unavailable") throw error;
    throw codedError("source_entry_unavailable");
  }
}

function makeUnavailableEntries(code) {
  return SOURCE_ENTRIES.map((label) => ({ label, status: "unavailable", code }));
}

function rejectedReport(code) {
  const fixedCode = CANDIDATE_CODES.has(code) && code !== "ok" ? code : "source_entry_unavailable";
  return { schema: SCHEMA, proofLevel: PROOF_LEVEL, status: "rejected", code: fixedCode, entries: makeUnavailableEntries(fixedCode) };
}

function contentIsRedacted(content) {
  if (typeof content === "string") return redactCredentialLiterals(content) === content && Buffer.byteLength(content, "utf8") <= MAX_ENTRY_BYTES;
  if (!isExactObject(content, ["inlineScripts", "scriptSrcDirectives", "scriptSources"]) ||
      !Array.isArray(content.inlineScripts) || !Array.isArray(content.scriptSrcDirectives) || !Array.isArray(content.scriptSources) ||
      content.inlineScripts.length > 256 || content.scriptSrcDirectives.length > 64 || content.scriptSources.length > 256) return false;
  return content.inlineScripts.every((text) => typeof text === "string" && redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= MAX_ENTRY_BYTES) &&
    content.scriptSrcDirectives.every((text) => typeof text === "string" && redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= 16 * 1024) &&
    content.scriptSources.every((text) => typeof text === "string" && redactCredentialLiterals(text) === text && Buffer.byteLength(text, "utf8") <= 2048);
}

function isAsarCandidateReport(value) {
  try {
    if (!isExactObject(value, ["schema", "proofLevel", "status", "code", "entries"]) ||
        value.schema !== SCHEMA || value.proofLevel !== PROOF_LEVEL || !CANDIDATE_CODES.has(value.code) ||
        !["candidate", "rejected"].includes(value.status) || !Array.isArray(value.entries) ||
        value.entries.length !== SOURCE_ENTRIES.length) return false;
    let sourceBytes = 0;
    let allRead = true;
    for (let index = 0; index < SOURCE_ENTRIES.length; index += 1) {
      const entry = value.entries[index];
      if (!entry || entry.label !== SOURCE_ENTRIES[index]) return false;
      if (entry.status === "read") {
        if (!isExactObject(entry, ["label", "status", "bytes", "sha256", "content"]) ||
            !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > MAX_ENTRY_BYTES ||
            typeof entry.sha256 !== "string" || !HASH.test(entry.sha256) || !contentIsRedacted(entry.content)) return false;
        if (entry.label === "renderer/index.html") {
          if (!isExactObject(entry.content, ["inlineScripts", "scriptSrcDirectives", "scriptSources"])) return false;
        } else if (typeof entry.content !== "string") return false;
        sourceBytes += entry.bytes;
      } else if (entry.status === "source_omitted") {
        allRead = false;
        if (!isExactObject(entry, ["label", "status", "code", "bytes", "sha256"]) || entry.code !== "wire_budget_exceeded" ||
            !Number.isSafeInteger(entry.bytes) || entry.bytes < 0 || entry.bytes > MAX_ENTRY_BYTES ||
            typeof entry.sha256 !== "string" || !HASH.test(entry.sha256)) return false;
        sourceBytes += entry.bytes;
      } else if (entry.status === "unavailable") {
        allRead = false;
        if (!isExactObject(entry, ["label", "status", "code"]) || !ENTRY_CODES.has(entry.code)) return false;
      } else return false;
    }
    if (sourceBytes > MAX_TOTAL_SOURCE_BYTES) return false;
    if (value.status === "candidate" && (!allRead || value.code !== "ok")) return false;
    if (value.status === "rejected" && value.code === "ok") return false;
    return Buffer.byteLength(JSON.stringify(value), "utf8") <= MAX_REPORT_BYTES;
  } catch {
    return false;
  }
}

function createAsarCandidateProbe({ fsModule = fs, cryptoModule = crypto, pathModule = path } = {}) {
  function probeAsarCandidates({ appPath } = {}) {
    if (typeof appPath !== "string" || !pathModule.isAbsolute(appPath) || appPath.includes("\0")) {
      return rejectedReport("app_path_invalid");
    }
    if (!fsModule || typeof fsModule.statSync !== "function" || typeof fsModule.readFileSync !== "function") {
      return rejectedReport("virtual_fs_unavailable");
    }

    const report = { schema: SCHEMA, proofLevel: PROOF_LEVEL, status: "candidate", code: "ok", entries: [] };
    let totalSourceBytes = 0;
    let firstFailure = null;
    for (const label of SOURCE_ENTRIES) {
      const remaining = MAX_TOTAL_SOURCE_BYTES - totalSourceBytes;
      if (remaining <= 0) {
        const code = "source_total_limit_exceeded";
        report.entries.push({ label, status: "unavailable", code });
        firstFailure ||= code;
        continue;
      }
      let bytes;
      try {
        const virtualPath = pathModule.join(appPath, "application.asar", ...label.split("/"));
        bytes = readBoundedVirtualFile(fsModule, virtualPath, Math.min(MAX_ENTRY_BYTES, remaining));
      } catch (error) {
        const aggregateLimit = error?.code === "source_entry_oversize" && remaining < MAX_ENTRY_BYTES;
        const code = aggregateLimit ? "source_total_limit_exceeded" : ENTRY_CODES.has(error?.code) ? error.code : "source_entry_unavailable";
        report.entries.push({ label, status: "unavailable", code });
        firstFailure ||= code;
        if (aggregateLimit) totalSourceBytes = MAX_TOTAL_SOURCE_BYTES;
        continue;
      }
      if (totalSourceBytes + bytes.length > MAX_TOTAL_SOURCE_BYTES) {
        const code = "source_total_limit_exceeded";
        report.entries.push({ label, status: "unavailable", code });
        firstFailure ||= code;
        totalSourceBytes = MAX_TOTAL_SOURCE_BYTES;
        continue;
      }
      totalSourceBytes += bytes.length;
      let content;
      try {
        const text = decodeStrictUtf8(bytes);
        content = label === "renderer/index.html" ? extractHtmlScripts(text) : redactCredentialLiterals(text);
      } catch (error) {
        const code = ENTRY_CODES.has(error?.code) ? error.code : "source_entry_invalid";
        report.entries.push({ label, status: "unavailable", code });
        firstFailure ||= code;
        continue;
      }
      report.entries.push({ label, status: "read", bytes: bytes.length, sha256: sha256(bytes, cryptoModule), content });
    }

    if (firstFailure) {
      report.status = "rejected";
      report.code = firstFailure;
    }
    while (Buffer.byteLength(JSON.stringify(report), "utf8") > MAX_REPORT_BYTES) {
      let index = report.entries.length - 1;
      while (index >= 0 && report.entries[index].status !== "read") index -= 1;
      if (index < 0) return rejectedReport("wire_budget_exceeded");
      const entry = report.entries[index];
      report.entries[index] = {
        label: entry.label,
        status: "source_omitted",
        code: "wire_budget_exceeded",
        bytes: entry.bytes,
        sha256: entry.sha256,
      };
      report.status = "rejected";
      report.code = "wire_budget_exceeded";
    }
    return isAsarCandidateReport(report) ? report : rejectedReport("source_entry_invalid");
  }

  return Object.freeze({ probeAsarCandidates });
}

const defaultProbe = createAsarCandidateProbe();

module.exports = Object.freeze({
  SCHEMA,
  PROOF_LEVEL,
  SOURCE_ENTRIES,
  MAX_ENTRY_BYTES,
  MAX_TOTAL_SOURCE_BYTES,
  MAX_REPORT_BYTES,
  createAsarCandidateProbe,
  isAsarCandidateReport,
  probeAsarCandidates: defaultProbe.probeAsarCandidates,
});
