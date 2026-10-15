"use strict";

const MAX_DEVICE_COUNT = 256;
const AUDIO_DEVICE_INFO_COMMAND = 20061;
const MAX_DEVICE_NAME_BYTES = 512;
const MAX_GUID_BYTES = 256;

function codedError(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function validDeviceText(value, maxBytes) {
  return typeof value === "string" && value.length > 0 &&
    Buffer.byteLength(value, "utf8") <= maxBytes && !/[\u0000-\u001f\u007f]/.test(value);
}

function parseDeviceRows(ids, names, guids, direction) {
  if (!Array.isArray(ids) || !Array.isArray(names) || !Array.isArray(guids) ||
      ids.length < 1 || ids.length > MAX_DEVICE_COUNT || names.length !== ids.length || guids.length !== ids.length) {
    throw codedError("audio_device_metadata_invalid");
  }
  const seenIds = new Set();
  const seenGuids = new Set();
  const rows = ids.map((id, index) => {
    const name = names[index];
    const guid = guids[index];
    const normalizedGuid = typeof guid === "string" ? guid.toLowerCase() : "";
    if (!Number.isSafeInteger(id) || id !== index + 1 || !validDeviceText(name, MAX_DEVICE_NAME_BYTES) ||
        !validDeviceText(guid, MAX_GUID_BYTES) || seenIds.has(id) || seenGuids.has(normalizedGuid)) {
      throw codedError("audio_device_metadata_invalid");
    }
    seenIds.add(id);
    seenGuids.add(normalizedGuid);
    return Object.freeze({ direction, index: index + 1, id, name, guid });
  });
  return Object.freeze(rows);
}

function parseAudioDeviceSnapshot(value) {
  if (!Array.isArray(value) || value.length !== 10) throw codedError("audio_device_metadata_invalid");
  const output = parseDeviceRows(value[0], value[1], value[2], "output");
  const input = parseDeviceRows(value[3], value[4], value[5], "input");
  const tail = value.slice(6);
  if (tail.some((item) => !Number.isSafeInteger(item))) throw codedError("audio_device_metadata_invalid");
  const outputIds = new Set(output.map((device) => device.id));
  const inputIds = new Set(input.map((device) => device.id));
  if ((tail[0] !== 0 && !outputIds.has(tail[0])) || (tail[1] !== 0 && !inputIds.has(tail[1])) ||
      (tail[2] !== 0 && !outputIds.has(tail[2])) || (tail[3] !== 0 && !inputIds.has(tail[3]))) {
    throw codedError("audio_device_metadata_invalid");
  }
  return Object.freeze({
    output,
    input,
  // Keep the verified current-output/current-input/default-output/default-input
  // values in memory only; callers never expose vendor metadata over HTTP.
    tail: Object.freeze(tail),
  });
}

function resolveUniqueDevice(snapshot, direction, name) {
  if (!snapshot || (direction !== "input" && direction !== "output") || !validDeviceText(name, MAX_DEVICE_NAME_BYTES)) {
    throw codedError("audio_device_name_invalid");
  }
  const matches = snapshot[direction].filter((row) => row.name === name);
  if (matches.length !== 1) throw codedError(matches.length ? "audio_device_name_ambiguous" : "audio_device_not_found");
  const device = matches[0];
  if (!Number.isSafeInteger(device.id) || device.id <= 0 || !device.guid) throw codedError("audio_device_not_selectable");
  return device;
}

function sameDeviceMappings(before, after) {
  if (!before || !after) return false;
  for (const direction of ["input", "output"]) {
    const left = before[direction];
    const right = after[direction];
    if (left.length !== right.length) return false;
    const rightById = new Map(right.map((device) => [device.id, device]));
    for (const device of left) {
      const candidate = rightById.get(device.id);
      if (!candidate || candidate.name !== device.name || candidate.guid !== device.guid) return false;
    }
  }
  return true;
}

module.exports = {
  AUDIO_DEVICE_INFO_COMMAND,
  MAX_DEVICE_COUNT,
  MAX_DEVICE_NAME_BYTES,
  parseAudioDeviceSnapshot,
  resolveUniqueDevice,
  sameDeviceMappings,
};
