'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const buildDir = path.resolve(__dirname, '../../audio_route/build');
const guard = require(path.join(buildDir, 'QQCallAudioRouteGuard-v1.node'));
assert.equal(typeof guard.install, 'function');
assert.equal(typeof guard.status, 'function');
const initialStatus = guard.status();
assert.equal(initialStatus.installed, false);

const options = {
  qqImagePath: path.join(process.env.WINDIR || 'C:\\Windows', 'System32', 'cmd.exe'),
  guardDllPath: path.join(buildDir, 'QQCallAudioRouteGuard-v1.dll'),
  qqBuild: '9.9.26-44343',
  captureEndpointId: 'opaque-capture-mock-target',
  renderEndpointId: 'opaque-render-mock-target',
};

const invalidGenerations = [undefined, null, '5', true, {}, 1n, 0, -1, 1.5, Infinity, NaN, 2 ** 53];
for (const generation of invalidGenerations) {
  const candidate = { ...options };
  if (generation !== undefined) candidate.hostGeneration = generation;
  const result = guard.install(candidate);
  assert.equal(result.installed, false);
  assert.equal(result.failureCode, 1001);
  assert.equal(result.generation, 0);
}

for (const generation of [1, Number.MAX_SAFE_INTEGER]) {
  const result = guard.install({ ...options, hostGeneration: generation });
  // cmd.exe exists but is not the image of this Node process, so native
  // installation stops before any process hook is created or any child scan.
  assert.equal(result.installed, false);
  assert.equal(result.failureCode, 1005);
  assert.equal(result.generation, generation);
}

console.log('audio_route_node_api_probe=passed side_effects=none hooks_installed=false');
