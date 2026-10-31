"use strict";

const path = require("node:path");
const { types } = require("node:util");

const TYPES = new Set(["Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "URIError", "EvalError", "AggregateError"]);
const CODES = new Set(["EPIPE", "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "ENOTCONN", "EBADF", "ENOMEM", "ERR_STREAM_DESTROYED", "ERR_STREAM_WRITE_AFTER_END", "ERR_UNHANDLED_ERROR"]);
const FILES = ["bridge/cli.cjs", "bridge/host-client.cjs", "bridge/control-service.cjs", "bridge/qqcall-bridge.cjs", "bridge/audio-route-observer.cjs", "probe/broker.cjs"];
const LOCATIONS = FILES.map((file) => [file, path.resolve(__dirname, "..", file).replaceAll("\\", "/")]);

function ownValue(value, key) {
  return Object.getOwnPropertyDescriptor(value, key)?.value;
}

// Never persist messages, arbitrary codes, function names, or absolute paths.
function summarizeException(error) {
  const result = { type: "unknown", code: "unknown", locations: [] };
  try {
    if (!types.isNativeError(error)) return result;
    const name = ownValue(error, "name") ?? ownValue(Object.getPrototypeOf(error), "name");
    result.type = TYPES.has(name) ? name : "Error";
    const code = ownValue(error, "code");
    if (CODES.has(code)) result.code = code;
    const descriptor = Object.getOwnPropertyDescriptor(error, "stack");
    // Recent V8 versions expose stack through a native accessor, even after assignment.
    const stack = descriptor && "value" in descriptor ? descriptor.value
      : typeof descriptor?.get === "function" && /\{\s*\[native code\]\s*\}/.test(Function.prototype.toString.call(descriptor.get))
        ? descriptor.get.call(error) : undefined;
    if (typeof stack !== "string" || stack.length > 65536) return result;
    for (const line of stack.split("\n").slice(1, 65)) {
      if (!/^\s+at /.test(line)) continue;
      const normalized = line.replaceAll("\\", "/");
      for (const [file, absolute] of LOCATIONS) {
        const marker = normalized.lastIndexOf(absolute + ":");
        if (marker < 0 || ![" ", "("].includes(normalized[marker - 1])) continue;
        const suffix = normalized.slice(marker + absolute.length);
        const match = /^:([1-9][0-9]{0,6}):([1-9][0-9]{0,6})\)?\s*$/.exec(suffix);
        if (match) result.locations.push({ file, line: Number(match[1]), column: Number(match[2]) });
        if (result.locations.length === 8) return result;
      }
    }
  } catch { /* Diagnostics must not interfere with fatal exception handling. */ }
  return result;
}

module.exports = { summarizeException };
