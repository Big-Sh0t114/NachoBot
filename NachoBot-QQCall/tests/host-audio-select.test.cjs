"use strict";

const assert = require("node:assert/strict");
const http = require("node:http");
const test = require("node:test");
const { createHostTransport } = require("../host/transport.cjs");
const {
  parseAudioDeviceSnapshot,
  resolveUniqueDevice,
  sameDeviceMappings,
} = require("../host/audio-devices.cjs");
const {
  validateAudioDeviceSelectionBody,
  validateAudioDeviceTail,
} = require("../host/transport.cjs");

const HOST_GENERATION = 81;
const INPUT_NAME = "Line 1 (Virtual Audio Cable)";
const OUTPUT_NAME = "Voicemeeter In 5 (VB-Audio Voicemeeter VAIO)";
const TOKEN = Buffer.alloc(32, 0x72).toString("base64url");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function sampleValue({ duplicateInputName = false, inputTargetId = 13, outputTargetId = 3,
  outputCount = 18, inputCount = 15, tail } = {}) {
  const outputIds = Array.from({ length: outputCount }, (_, index) => index + 1);
  const inputIds = Array.from({ length: inputCount }, (_, index) => index + 1);
  const outputNames = outputIds.map((id) => id === outputTargetId ? OUTPUT_NAME : `Output ${id}`);
  const inputNames = inputIds.map((id) => id === inputTargetId ? INPUT_NAME : `Input ${id}`);
  if (duplicateInputName) inputNames[0] = INPUT_NAME;
  const outputGuids = outputIds.map((id) => `{OUTPUT-${id}}`);
  const inputGuids = inputIds.map((id) => `{INPUT-${id}}`);
  return [outputIds, outputNames, outputGuids, inputIds, inputNames, inputGuids, ...(tail || [0, 0, 10, 8])];
}

async function createSelectionTransport(options = {}) {
  let transport;
  transport = createHostTransport({
    token: TOKEN,
    listenPort: 0,
    outputPort: 6110,
    hostGeneration: HOST_GENERATION,
    getReadiness: () => ({ ready: true, pluginFound: true, hasPostMessage: true, rendererGone: false }),
    outputSink: async () => ({ ok: true }),
    invokePlugin: async ({ command, id }) => ({ posted: true, id, command }),
    verifiedAudioDeviceProtocol: true,
    verifiedAudioDeviceSelectionProtocol: true,
    ...options,
  });
  await transport.start();
  return { transport, port: transport.status().listenPort };
}

function postJson(port, route, body, token = TOKEN) {
  const encoded = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1", port, method: "POST", path: route,
      headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), "content-type": "application/json", "content-length": encoded.length },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.once("end", () => resolve({ status: response.statusCode, body: JSON.parse(Buffer.concat(chunks).toString("utf8")) }));
    });
    request.once("error", reject);
    request.end(encoded);
  });
}

function selectionBody(overrides = {}) {
  return {
    id: 91,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: INPUT_NAME,
    outputDeviceName: OUTPUT_NAME,
    ...overrides,
  };
}

test("20061 parser accepts the observed device counts and resolves the verified selected/default tail", () => {
  const parsed = parseAudioDeviceSnapshot(sampleValue());
  const input = resolveUniqueDevice(parsed, "input", INPUT_NAME);
  const output = resolveUniqueDevice(parsed, "output", OUTPUT_NAME);
  assert.equal(parsed.output.length, 18);
  assert.equal(parsed.input.length, 15);
  assert.deepEqual({ id: input.id, index: input.index }, { id: 13, index: 13 });
  assert.deepEqual({ id: output.id, index: output.index }, { id: 3, index: 3 });
  assert.deepEqual(parsed.tail, [0, 0, 10, 8]);
  assert.deepEqual(validateAudioDeviceTail(parsed), {
    currentOutput: 0, currentInput: 0, defaultOutput: 10, defaultInput: 8,
  });
  assert.equal(Object.isFrozen(parsed), true);
  assert.equal(Object.isFrozen(input), true);
});

test("20061 parser accepts bounded dynamic counts rather than assuming this machine's enumeration size", () => {
  const small = parseAudioDeviceSnapshot(sampleValue({
    outputCount: 2,
    inputCount: 1,
    outputTargetId: 2,
    inputTargetId: 1,
    tail: [0, 0, 2, 1],
  }));
  assert.equal(small.output.length, 2);
  assert.equal(small.input.length, 1);
  assert.equal(resolveUniqueDevice(small, "output", OUTPUT_NAME).id, 2);
  assert.equal(resolveUniqueDevice(small, "input", INPUT_NAME).id, 1);
});

test("device resolution is direction-specific, exact, unique, and rejects default ID zero", () => {
  const snapshot = parseAudioDeviceSnapshot(sampleValue());
  assert.throws(() => resolveUniqueDevice(snapshot, "output", INPUT_NAME), { code: "audio_device_not_found" });
  assert.throws(() => resolveUniqueDevice(snapshot, "input", `${INPUT_NAME} `), { code: "audio_device_not_found" });
  assert.throws(() => resolveUniqueDevice(snapshot, "input", "Missing Device"), { code: "audio_device_not_found" });
  const duplicate = parseAudioDeviceSnapshot(sampleValue({ duplicateInputName: true }));
  assert.throws(() => resolveUniqueDevice(duplicate, "input", INPUT_NAME), { code: "audio_device_name_ambiguous" });
  const zeroIdSnapshot = {
    ...snapshot,
    input: snapshot.input.map((device) => device.name === INPUT_NAME ? { ...device, id: 0 } : device),
  };
  assert.throws(() => resolveUniqueDevice(zeroIdSnapshot, "input", INPUT_NAME), { code: "audio_device_not_selectable" });
});

test("metadata arrays and tails are strict and changed ID-to-GUID mappings are detected", () => {
  const valid = sampleValue();
  for (const invalid of [
    [],
    [...valid.slice(0, 6), 0, 0, 10],
    [...valid.slice(0, 6), 0, 0, 999, 8],
    [valid[0].slice(1), ...valid.slice(1)],
    [valid[0], valid[1], valid[2].slice(1), ...valid.slice(3)],
    [valid[0], [...valid[1].slice(0, 1), "Bad\nName", ...valid[1].slice(2)], ...valid.slice(2)],
    [valid[0], valid[1], [valid[2][0], valid[2][0], ...valid[2].slice(2)], ...valid.slice(3)],
  ]) {
    assert.throws(() => parseAudioDeviceSnapshot(invalid), { code: "audio_device_metadata_invalid" });
  }
  const before = parseAudioDeviceSnapshot(sampleValue());
  const changed = sampleValue();
  changed[2] = [...changed[2]];
  changed[2][2] = "{OUTPUT-OTHER}";
  assert.equal(sameDeviceMappings(before, parseAudioDeviceSnapshot(changed)), false);
  const reordered = sampleValue();
  for (const [idsIndex, namesIndex, guidsIndex] of [[0, 1, 2], [3, 4, 5]]) {
    reordered[idsIndex] = [...reordered[idsIndex]];
    reordered[namesIndex] = [...reordered[namesIndex]].reverse();
    reordered[guidsIndex] = [...reordered[guidsIndex]].reverse();
  }
  assert.equal(sameDeviceMappings(before, parseAudioDeviceSnapshot(reordered)), false);
  const hotplugged = parseAudioDeviceSnapshot(sampleValue({ outputCount: 17 }));
  assert.equal(sameDeviceMappings(before, hotplugged), false);
  const remapped = sampleValue();
  remapped[0] = [...remapped[0]];
  remapped[0][2] = 4;
  assert.throws(() => parseAudioDeviceSnapshot(remapped), { code: "audio_device_metadata_invalid" });
});

test("selection request requires exact keys, current generation, and bounded exact device names", () => {
  const body = {
    id: 19,
    hostGeneration: HOST_GENERATION,
    inputDeviceName: INPUT_NAME,
    outputDeviceName: OUTPUT_NAME,
  };
  assert.deepEqual(validateAudioDeviceSelectionBody(body, HOST_GENERATION), body);
  for (const invalid of [
    { ...body, extra: true },
    { ...body, hostGeneration: HOST_GENERATION + 1 },
    { ...body, id: 0 },
    { ...body, inputDeviceName: "" },
    { ...body, outputDeviceName: OUTPUT_NAME + " " },
    { ...body, inputDeviceName: "Line\u0000 1" },
  ]) {
    assert.throws(() => validateAudioDeviceSelectionBody(invalid, HOST_GENERATION));
  }
});

test("authenticated selector queries fresh metadata, sets input then output IDs, and verifies the readback", async (t) => {
  const posts = [];
  let queryCount = 0;
  let transport;
  const before = sampleValue({ tail: [0, 0, 10, 8] });
  const after = sampleValue({ tail: [3, 13, 10, 8] });
  const created = await createSelectionTransport({
    invokePlugin: async ({ command, id, params }) => {
      posts.push({ command, id, params });
      if (command === 103) {
        queryCount += 1;
        const value = queryCount === 1 ? before : after;
        queueMicrotask(() => void transport.publishOutput({ command: 20061, id: 0, value, hostGeneration: HOST_GENERATION }));
      }
      return { posted: true, id, command };
    },
  });
  transport = created.transport;
  t.after(() => transport.stop());

  const response = await postJson(created.port, "/v1/audio/select", selectionBody());
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, { selected: true, id: 91, command: 102 });
  assert.deepEqual(posts.map(({ command, params }) => ({ command, params })), [
    { command: 103, params: [] },
    { command: 102, params: [13, 3] },
    { command: 103, params: [] },
  ]);
  assert.equal(JSON.stringify(response.body).includes("GUID"), false);
  assert.equal(transport.status().audioDeviceSelectionInFlight, false);
  assert.equal(transport.status().audioDeviceSelectionQuarantined, false);
});

test("selector is authenticated, generation-bound, gated by pinned protocol proof, and generic commands stay closed", async (t) => {
  let posts = 0;
  const { transport, port } = await createSelectionTransport({
    verifiedAudioDeviceSelectionProtocol: false,
    invokePlugin: async ({ command, id }) => { posts += 1; return { posted: true, id, command }; },
  });
  t.after(() => transport.stop());
  assert.equal((await postJson(port, "/v1/audio/select", selectionBody(), null)).status, 401);
  const extra = await postJson(port, "/v1/audio/select", { ...selectionBody(), params: [] });
  assert.equal(extra.status, 400);
  const stale = await postJson(port, "/v1/audio/select", selectionBody({ hostGeneration: HOST_GENERATION + 1 }));
  assert.equal(stale.status, 409);
  const unverified = await postJson(port, "/v1/audio/select", selectionBody());
  assert.equal(unverified.status, 501);
  assert.equal(unverified.body.code, "audio_device_selection_unverified");
  for (const command of [102, 103]) {
    const denied = await postJson(port, "/v1/invoke", { command, id: 92, params: [], hostGeneration: HOST_GENERATION });
    assert.equal(denied.status, 400);
    assert.equal(denied.body.code, "command_not_allowed");
  }
  assert.equal(posts, 0);
});

test("selector fails closed on ambiguous desired names and never posts command 102", async (t) => {
  const posts = [];
  let transport;
  const created = await createSelectionTransport({
    invokePlugin: async ({ command, id }) => {
      posts.push(command);
      if (command === 103) queueMicrotask(() => void transport.publishOutput({
        command: 20061, id: 0, value: sampleValue({ duplicateInputName: true }), hostGeneration: HOST_GENERATION,
      }));
      return { posted: true, id, command };
    },
  });
  transport = created.transport;
  t.after(() => transport.stop());
  const response = await postJson(created.port, "/v1/audio/select", selectionBody());
  assert.equal(response.status, 409);
  assert.equal(response.body.code, "audio_device_name_ambiguous");
  assert.deepEqual(posts, [103]);
});

test("malformed 20061 metadata rejects selection before the setter", async (t) => {
  const posts = [];
  let transport;
  const created = await createSelectionTransport({
    invokePlugin: async ({ command, id }) => {
      posts.push(command);
      if (command === 103) {
        const malformed = sampleValue();
        malformed[0][2] = 4;
        queueMicrotask(() => void transport.publishOutput({ command: 20061, id: 0, value: malformed, hostGeneration: HOST_GENERATION }));
      }
      return { posted: true, id, command };
    },
  });
  transport = created.transport;
  t.after(() => transport.stop());
  const response = await postJson(created.port, "/v1/audio/select", selectionBody());
  assert.equal(response.status, 409);
  assert.equal(response.body.code, "audio_device_metadata_invalid");
  assert.deepEqual(posts, [103]);
});

test("selection cannot be enabled without the same verified device-query capability", async (t) => {
  let posts = 0;
  const { transport, port } = await createSelectionTransport({
    verifiedAudioDeviceProtocol: false,
    verifiedAudioDeviceSelectionProtocol: true,
    invokePlugin: async ({ command, id }) => { posts += 1; return { posted: true, id, command }; },
  });
  t.after(() => transport.stop());
  const response = await postJson(port, "/v1/audio/select", selectionBody());
  assert.equal(response.status, 501);
  assert.equal(response.body.code, "audio_device_selection_unverified");
  assert.equal(posts, 0);
});

test("selector rejects a changed ID/GUID map after cmd 102 without claiming selected", async (t) => {
  const posts = [];
  let queryCount = 0;
  let transport;
  const created = await createSelectionTransport({
    invokePlugin: async ({ command, id }) => {
      posts.push(command);
      if (command === 103) {
        queryCount += 1;
        const value = sampleValue({ tail: queryCount === 1 ? [0, 0, 10, 8] : [3, 13, 10, 8] });
        if (queryCount === 2) value[2][2] = "{HOTPLUGGED-GUID}";
        queueMicrotask(() => void transport.publishOutput({ command: 20061, id: 0, value, hostGeneration: HOST_GENERATION }));
      }
      return { posted: true, id, command };
    },
  });
  transport = created.transport;
  t.after(() => transport.stop());
  const response = await postJson(created.port, "/v1/audio/select", selectionBody());
  assert.equal(response.status, 409);
  assert.equal(response.body.code, "audio_device_map_changed");
  assert.deepEqual(posts, [103, 102, 103]);
});

test("stop during fresh-query wait returns unavailable and never starts command 102", async () => {
  const queryPosted = deferred();
  const posts = [];
  const { transport, port } = await createSelectionTransport({
    audioDeviceSelectionTimeoutMs: 1000,
    invokePlugin: async ({ command, id }) => {
      posts.push(command);
      if (command === 103) queryPosted.resolve();
      return { posted: true, id, command };
    },
  });
  const request = postJson(port, "/v1/audio/select", selectionBody());
  await queryPosted.promise;
  await transport.stop();
  const response = await request;
  assert.equal(response.status, 503);
  assert.equal(response.body.code, "host_stopped");
  assert.deepEqual(posts, [103]);
});

test("selector singleflight blocks accept but permits cmd 55 and does not use ACK-only selection", async (t) => {
  const selectionStarted = deferred();
  const posts = [];
  let queryCount = 0;
  let transport;
  const created = await createSelectionTransport({
    audioDeviceSelectionTimeoutMs: 500,
    invokePlugin: async ({ command, id, params }) => {
      posts.push({ command, id, params });
      if (command === 103) {
        queryCount += 1;
        if (queryCount === 1) selectionStarted.resolve();
        return { posted: true, id, command };
      }
      return { posted: true, id, command };
    },
  });
  transport = created.transport;
  t.after(() => transport.stop());

  const selectionRequest = postJson(created.port, "/v1/audio/select", selectionBody());
  await selectionStarted.promise;
  const accept = await postJson(created.port, "/v1/invoke", {
    command: 5, id: 92, params: [0, "caller", ["caller"], 0, "", false, ""], hostGeneration: HOST_GENERATION,
  });
  assert.equal(accept.status, 409);
  assert.equal(accept.body.code, "audio_device_selection_busy");
  const duplicate = await postJson(created.port, "/v1/audio/select", selectionBody({ id: 93 }));
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.body.code, "audio_device_selection_busy");
  const signal = await postJson(created.port, "/v1/invoke", {
    command: 55, id: 94, params: [1, "signal"], hostGeneration: HOST_GENERATION,
  });
  assert.equal(signal.status, 200);
  assert.deepEqual(signal.body, { posted: true, id: 94, command: 55 });

  void transport.publishOutput({ command: 20061, id: 0, value: sampleValue(), hostGeneration: HOST_GENERATION });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(posts.some((entry) => entry.command === 102), true);
  assert.equal(posts.filter((entry) => entry.command === 5).length, 0);
  // The setter receipt cannot produce selected:true without the second fresh 20061.
  const timeoutResponse = await selectionRequest;
  assert.equal(timeoutResponse.status, 504);
  assert.equal(timeoutResponse.body.code, "audio_device_selection_timeout");
  assert.equal(transport.status().audioDeviceSelectionQuarantined, true);
  assert.equal(posts.filter((entry) => entry.command === 102).length, 1);
});
