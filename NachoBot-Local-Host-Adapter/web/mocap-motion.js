export const MOCAP_STYLE_FILES = Object.freeze({
  cute: 'cute.t2.json',
  energetic: 'energetic.t2.json',
  kpop: 'kpop.t2.json',
  hiphop: 'hiphop.t2.json',
  shuffle: 'shuffle.t2.json',
  elegant: 'elegant.t2.json',
});

const REQUIRED_BONES = Object.freeze([
  'hips', 'spine', 'chest', 'neck', 'head',
  'leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand',
  'rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot',
  'rightUpperLeg', 'rightLowerLeg', 'rightFoot',
]);

const clamp01 = (value) => Math.min(1, Math.max(0, value));
const MOTION_MAX_ANGLE = Object.freeze({
  neck: 0.52, head: 0.62,
  leftShoulder: 0.72, rightShoulder: 0.72,
  leftUpperArm: 2.08, rightUpperArm: 2.08,
  leftLowerArm: 2.15, rightLowerArm: 2.15,
  leftHand: 0.9, rightHand: 0.9,
  leftUpperLeg: 0.96, rightUpperLeg: 0.96,
  leftLowerLeg: 2.15, rightLowerLeg: 2.15,
  leftFoot: 0.68, rightFoot: 0.68,
});

function smoothstep(value) {
  const t = clamp01(value);
  return t * t * (3 - 2 * t);
}

function readQuaternion(values, frame) {
  const offset = frame * 4;
  return [values[offset], values[offset + 1], values[offset + 2], values[offset + 3]];
}

function normalizeQuaternion(q) {
  const length = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
  return q.map((value) => value / length);
}

export function slerpQuaternion(a, b, amount) {
  const t = clamp01(amount);
  let target = b;
  let dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
  if (dot < 0) {
    dot = -dot;
    target = b.map((value) => -value);
  }
  if (dot > 0.9995) {
    return normalizeQuaternion(a.map((value, index) => value + (target[index] - value) * t));
  }
  const theta = Math.acos(Math.min(1, Math.max(-1, dot)));
  const sinTheta = Math.sin(theta);
  const left = Math.sin((1 - t) * theta) / sinTheta;
  const right = Math.sin(t * theta) / sinTheta;
  return a.map((value, index) => value * left + target[index] * right);
}

export function clampMotionQuaternion(name, quaternion) {
  const limit = MOTION_MAX_ANGLE[name];
  const q = normalizeQuaternion(quaternion);
  if (!limit) return q;
  const angle = 2 * Math.acos(Math.min(1, Math.max(-1, Math.abs(q[3]))));
  if (angle <= limit) return q;
  return slerpQuaternion([0, 0, 0, 1], q, limit / angle);
}

function sampleQuaternionTrack(values, frame, frameCount) {
  const first = Math.min(frameCount - 1, Math.max(0, Math.floor(frame)));
  const second = Math.min(frameCount - 1, first + 1);
  return slerpQuaternion(readQuaternion(values, first), readQuaternion(values, second), frame - first);
}

function sampleHips(values, frame, frameCount) {
  const first = Math.min(frameCount - 1, Math.max(0, Math.floor(frame)));
  const second = Math.min(frameCount - 1, first + 1);
  const amount = frame - first;
  return [0, 1, 2].map((axis) => {
    const a = values[first * 3 + axis];
    const b = values[second * 3 + axis];
    return a + (b - a) * amount;
  });
}

export function validateCompactMotion(clip) {
  if (!clip || !Number.isInteger(clip.n) || clip.n < 2 || !Number.isFinite(clip.fps) || clip.fps <= 0) {
    return false;
  }
  if (!Array.isArray(clip.hips) || clip.hips.length < clip.n * 3 || !clip.bones) return false;
  return REQUIRED_BONES.every((name) => Array.isArray(clip.bones[name]) && clip.bones[name].length >= clip.n * 4);
}

export function motionDuration(clip) {
  return validateCompactMotion(clip) ? (clip.n - 1) / clip.fps : 0;
}

export function sampleCompactMotion(clip, elapsedSeconds, seamSeconds = 0.55) {
  if (!validateCompactMotion(clip)) throw new Error('Invalid compact humanoid motion clip');
  const duration = motionDuration(clip);
  const localTime = ((Math.max(0, elapsedSeconds) % duration) + duration) % duration;
  const frame = localTime * clip.fps;
  const blendStart = Math.max(0, duration - Math.min(seamSeconds, duration * 0.2));
  const blend = localTime > blendStart ? smoothstep((localTime - blendStart) / (duration - blendStart)) : 0;
  const restartFrame = Math.max(0, localTime - blendStart) * clip.fps;
  const bones = {};

  for (const [name, values] of Object.entries(clip.bones)) {
    const current = sampleQuaternionTrack(values, frame, clip.n);
    const looped = blend > 0
      ? slerpQuaternion(current, sampleQuaternionTrack(values, restartFrame, clip.n), blend)
      : current;
    bones[name] = clampMotionQuaternion(name, looped);
  }

  const currentHips = sampleHips(clip.hips, frame, clip.n);
  const restartHips = sampleHips(clip.hips, restartFrame, clip.n);
  const hips = currentHips.map((value, axis) => value + (restartHips[axis] - value) * blend);
  return { bones, hips, frame, duration, loopBlend: blend };
}
