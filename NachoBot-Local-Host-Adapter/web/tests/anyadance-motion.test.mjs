import assert from 'node:assert/strict';
import test from 'node:test';

import {
  anyadanceMotionDuration,
  sampleAnyaDanceMotion,
  validateAnyaDanceMotion,
} from '../anyadance-motion.js';

const JOINTS = [
  'pelvis', 'head', 'left_shoulder', 'right_shoulder', 'left_elbow', 'right_elbow',
  'left_wrist', 'right_wrist', 'left_ankle', 'right_ankle', 'left_toe', 'right_toe',
];

function pose(x, y, z, angle = 0) {
  return { p: [x, y, z], q: [0, Math.sin(angle / 2), 0, Math.cos(angle / 2)] };
}

function fixture() {
  const rest = Object.fromEntries(JOINTS.map((name, index) => [name, pose(index % 2, 1 + index / 20, 0)]));
  const frames = [0, 1, 2].map((index) => ({
    t: index / 60,
    j: Object.fromEntries(JOINTS.map((name, jointIndex) => [name, pose(jointIndex % 2 + index * 0.01, 1 + jointIndex / 20, 0, index * 0.1)])),
    fl: [index / 2, 0, 0, 0, 0],
    fr: [0, index / 2, 0, 0, 0],
  }));
  return { format: 'anyadance_mmd_solved', version: 1, fps: 60, rest, frames };
}

test('accepts AnyaDance solved motion and samples every required joint', () => {
  const clip = fixture();
  assert.ok(validateAnyaDanceMotion(clip));
  const sample = sampleAnyaDanceMotion(clip, 0.01);
  assert.equal(Object.keys(sample.joints).length, JOINTS.length);
  assert.ok(sample.joints.left_wrist.q.every(Number.isFinite));
  assert.ok(sample.leftFingers[0] > 0);
});

test('loops with quaternion and position seam blending', () => {
  const clip = fixture();
  const duration = anyadanceMotionDuration(clip);
  const sample = sampleAnyaDanceMotion(clip, duration - 0.001);
  assert.ok(sample.loopBlend > 0);
  assert.ok(sample.joints.pelvis.p.every(Number.isFinite));
  assert.ok(Math.abs(Math.hypot(...sample.joints.head.q) - 1) < 0.001);
});
