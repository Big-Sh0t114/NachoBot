"use strict";

const crypto = require("node:crypto");
const COMMAND = 14;
const MAX_CALLBACK_BYTES = 8192;

function codedError(code) {
  return Object.assign(new Error(code), { code });
}

function summarizeRoomId(value) {
  if (!Array.isArray(value) || value.length < 2 || value.length > 3 ||
      !Number.isInteger(value[0]) || value[0] < -0x80000000 || value[0] > 0x7fffffff ||
      typeof value[1] !== "string" || Buffer.byteLength(value[1], "utf8") > MAX_CALLBACK_BYTES) return null;
  const result = { diagnosticOnly: true, returnCode: value[0], dataKind: "missing", dataLength: 0, dataSha256: null, numericRoomId: false };
  if (value.length === 2) return Object.freeze(result);
  let encoded;
  if (typeof value[2] === "string" && Buffer.byteLength(value[2], "utf8") <= MAX_CALLBACK_BYTES) {
    result.dataKind = value[2] === "" ? "empty_string" : value[2] === "0" ? "zero_string" : "string";
    encoded = value[2];
    result.numericRoomId = /^[1-9][0-9]{0,19}$/.test(encoded);
  } else if (Number.isSafeInteger(value[2]) && value[2] >= 0) {
    result.dataKind = value[2] === 0 ? "zero_number" : "number";
    encoded = String(value[2]);
  } else {
    return null;
  }
  result.dataLength = Buffer.byteLength(encoded, "utf8");
  result.dataSha256 = crypto.createHash("sha256").update(encoded, "utf8").digest("hex");
  return Object.freeze(result);
}

function createChattingRoomIdQuery({ hostGeneration, sendCommand, timeoutMs = 5000 }) {
  if (!Number.isSafeInteger(hostGeneration) || hostGeneration <= 0 || typeof sendCommand !== "function" ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000) {
    throw codedError("group_local_room_query_config_invalid");
  }
  let ticket = null;
  let stopped = false;
  const usedIds = new Set();
  const generatedBase = crypto.randomInt(0x40000000, 0x60000000);
  let generatedTop = generatedBase;

  function finish(active, error, value) {
    if (ticket !== active) return;
    ticket = null;
    clearTimeout(active.timer);
    if (error) active.reject(error);
    else active.resolve({ ok: true, diagnosticOnly: true, hostGeneration, id: active.id, observation: value });
  }

  function finishIfReady(active) {
    if (ticket === active && active.posted && active.observation) {
      if (Date.now() >= active.deadlineMs) finish(active, codedError("group_local_room_query_timeout"));
      else finish(active, null, active.observation);
    }
  }

  function beginQuery(id, generated, requestTimeoutMs = timeoutMs) {
    if (stopped) throw codedError("host_stopped");
    if (ticket) throw codedError("group_local_room_query_busy");
    if (!Number.isSafeInteger(requestTimeoutMs) || requestTimeoutMs < 1 || requestTimeoutMs > timeoutMs) {
      throw codedError("group_local_room_query_budget_invalid");
    }
    if (!Number.isSafeInteger(id) || id <= 0) throw codedError("invalid_request_id");
    if (usedIds.has(id) || (!generated && id > generatedBase && id <= generatedTop)) throw codedError("group_local_room_query_id_reused");
    if (!generated) {
      if (usedIds.size >= 4096) throw codedError("group_local_room_query_exhausted");
      usedIds.add(id);
    }
    const result = new Promise((resolve, reject) => {
      const active = { id, resolve, reject, posted: false, observation: null, timer: null,
        deadlineMs: Date.now() + requestTimeoutMs };
      ticket = active;
      active.timer = setTimeout(() => finish(active, codedError("group_local_room_query_timeout")), requestTimeoutMs);
      active.timer.unref?.();
      Promise.resolve().then(() => {
        if (ticket !== active || stopped) throw codedError("host_stopped");
        return sendCommand({ command: COMMAND, id, params: [], hostGeneration }, { timeoutMs: requestTimeoutMs });
      })
        .then((receipt) => {
          if (ticket !== active) return;
          if (receipt?.posted !== true || receipt.id !== id || receipt.command !== COMMAND) {
            finish(active, codedError("group_local_room_query_post_failed"));
            return;
          }
          active.posted = true;
          finishIfReady(active);
        }, () => finish(active, codedError("group_local_room_query_post_failed")));
    });
    result.catch(() => {});
    return result;
  }

  function observeOutput(frame) {
    const active = ticket;
    if (!active || frame.command !== COMMAND || frame.id !== active.id || frame.hostGeneration !== hostGeneration) return;
    const observation = summarizeRoomId(frame.value);
    if (!observation) {
      finish(active, codedError("group_local_room_response_invalid"));
      return;
    }
    active.observation = observation;
    finishIfReady(active);
  }

  function stop() {
    stopped = true;
    if (ticket) finish(ticket, codedError("host_stopped"));
  }
  function queryGenerated(requestTimeoutMs = timeoutMs) {
    do { generatedTop += 1; } while (usedIds.has(generatedTop));
    if (generatedTop > 0x7fffffff) throw codedError("group_local_room_query_exhausted");
    return beginQuery(generatedTop, true, requestTimeoutMs);
  }
  function ownsOutput(frame) {
    return frame.command === COMMAND && frame.hostGeneration === hostGeneration &&
      (usedIds.has(frame.id) || (Number.isSafeInteger(frame.id) && frame.id > generatedBase && frame.id <= generatedTop));
  }
  return Object.freeze({ query: (id) => beginQuery(id, false), queryGenerated, ownsOutput, observeOutput, stop });
}

module.exports = { COMMAND, summarizeRoomId, createChattingRoomIdQuery };
