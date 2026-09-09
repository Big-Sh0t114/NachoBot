import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { MOCAP_STYLE_FILES, clampMotionQuaternion, motionDuration, sampleCompactMotion, validateCompactMotion } from '../mocap-motion.js';
import { sampleHandGesture } from '../hand-choreography.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const motionDir = path.resolve(here, '../../models/motions/aist');

function loadMotion(file) {
  return JSON.parse(fs.readFileSync(path.join(motionDir, file), 'utf8'));
}

test('all bundled AIST++ motions are valid full-body clips', () => {
  for (const [style, file] of Object.entries(MOCAP_STYLE_FILES)) {
    const clip = loadMotion(file);
    assert.ok(validateCompactMotion(clip), `${style} clip is malformed`);
    assert.ok(motionDuration(clip) >= 7, `${style} clip is unexpectedly short`);
    assert.equal(clip.fps, 60);
  }
});

test('mocap sampling interpolates normalized quaternions and a seamless loop', () => {
  const clip = loadMotion(MOCAP_STYLE_FILES.hiphop);
  for (const time of [0, 0.137, 2.41, motionDuration(clip) - 0.01]) {
    const pose = sampleCompactMotion(clip, time);
    for (const quaternion of Object.values(pose.bones)) {
      assert.ok(quaternion.every(Number.isFinite));
      assert.ok(Math.abs(Math.hypot(...quaternion) - 1) < 0.002);
    }
  }
  const before = sampleCompactMotion(clip, motionDuration(clip) - 0.001).bones.chest;
  const after = sampleCompactMotion(clip, 0.001).bones.chest;
  const dot = Math.abs(before.reduce((sum, value, index) => sum + value * after[index], 0));
  assert.ok(dot > 0.999, `loop seam is discontinuous: ${dot}`);
});

test('fist and pointing gestures drive all finger bones with distinct curls', () => {
  const fist = sampleHandGesture('fist', 'left');
  const point = sampleHandGesture('point', 'left');
  assert.equal(Object.keys(fist).length, 15);
  assert.ok(Math.abs(fist.leftIndexIntermediate.z) > 0.8);
  assert.ok(Math.abs(point.leftIndexIntermediate.z) < 0.05);
  assert.ok(Math.abs(point.leftMiddleIntermediate.z) > 0.8);
});

test('joint safety limits tame incompatible leg and wrist rotations', () => {
  const extreme = [Math.sin(1.2), 0, 0, Math.cos(1.2)];
  const upperLeg = clampMotionQuaternion('leftUpperLeg', extreme);
  const hand = clampMotionQuaternion('rightHand', extreme);
  const angle = (q) => 2 * Math.acos(Math.min(1, Math.abs(q[3])));
  assert.ok(angle(upperLeg) <= 0.961);
  assert.ok(angle(hand) <= 0.901);
});
