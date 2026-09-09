import { slerpQuaternion } from './mocap-motion.js';

// Blender + MMD Tools produces world-space joints; the browser stage retargets
// those joints onto the loaded VRM instead of applying source-model rotations
// directly to a different avatar.
export const ANYADANCE_STYLE_FILES = Object.freeze({
  cute: 'cute.json',
  energetic: 'energetic.json',
  kpop: 'kpop.json',
  hiphop: 'hiphop.json',
  shuffle: 'shuffle.json',
  elegant: 'elegant.json',
});

export const ANYADANCE_JOINTS = Object.freeze([
  'pelvis', 'head',
  'left_shoulder', 'right_shoulder',
  'left_elbow', 'right_elbow',
  'left_wrist', 'right_wrist',
  'left_ankle', 'right_ankle',
  'left_toe', 'right_toe',
]);

function finiteVector(values, length) {
  return Array.isArray(values) && values.length >= length && values.slice(0, length).every(Number.isFinite);
}

function validPose(pose) {
  return pose && finiteVector(pose.p, 3) && finiteVector(pose.q, 4)
    && Math.hypot(...pose.q.slice(0, 4)) > 1e-5;
}

export function validateAnyaDanceMotion(clip) {
  if (!clip || clip.format !== 'anyadance_mmd_solved' || clip.version !== 1) return false;
  if (!Number.isFinite(clip.fps) || clip.fps <= 0 || !Array.isArray(clip.frames) || clip.frames.length < 2) return false;
  if (!clip.rest || !ANYADANCE_JOINTS.every((name) => validPose(clip.rest[name]))) return false;
  return clip.frames.every((frame) => frame && ANYADANCE_JOINTS.every((name) => validPose(frame.j?.[name])));
}

export function anyadanceMotionDuration(clip) {
  return validateAnyaDanceMotion(clip) ? (clip.frames.length - 1) / clip.fps : 0;
}

function frameIndex(clip, elapsedSeconds) {
  const duration = anyadanceMotionDuration(clip);
  const localTime = ((Math.max(0, elapsedSeconds) % duration) + duration) % duration;
  const frame = localTime * clip.fps;
  const first = Math.min(clip.frames.length - 1, Math.max(0, Math.floor(frame)));
  const second = Math.min(clip.frames.length - 1, first + 1);
  return { duration, localTime, frame, first, second, amount: frame - first };
}

function interpolatePose(a, b, amount) {
  const q = slerpQuaternion(a.q, b.q, amount);
  const p = [0, 1, 2].map((axis) => a.p[axis] + (b.p[axis] - a.p[axis]) * amount);
  return { p, q };
}

function interpolateCurls(a, b, amount) {
  if (!Array.isArray(a) && !Array.isArray(b)) return null;
  const left = Array.isArray(a) ? a : b;
  const right = Array.isArray(b) ? b : a;
  return left.map((value, index) => value + (right[index] - value) * amount);
}

function sampleFrame(clip, frame) {
  const first = Math.min(clip.frames.length - 1, Math.max(0, Math.floor(frame)));
  const second = Math.min(clip.frames.length - 1, first + 1);
  const amount = frame - first;
  const a = clip.frames[first];
  const b = clip.frames[second];
  const joints = Object.fromEntries(
    ANYADANCE_JOINTS.map((name) => [name, interpolatePose(a.j[name], b.j[name], amount)]),
  );
  return {
    joints,
    leftFingers: interpolateCurls(a.fl, b.fl, amount),
    rightFingers: interpolateCurls(a.fr, b.fr, amount),
  };
}

export function sampleAnyaDanceMotion(clip, elapsedSeconds, seamSeconds = 0.55) {
  if (!validateAnyaDanceMotion(clip)) throw new Error('Invalid AnyaDance solved motion clip');
  const timing = frameIndex(clip, elapsedSeconds);
  const start = Math.max(0, timing.duration - Math.min(seamSeconds, timing.duration * 0.2));
  const blend = timing.localTime > start
    ? Math.min(1, Math.max(0, (timing.localTime - start) / (timing.duration - start)))
    : 0;
  const current = sampleFrame(clip, timing.frame);
  if (blend <= 0) return { ...current, ...timing, loopBlend: 0 };

  const restart = sampleFrame(clip, (timing.localTime - start) * clip.fps);
  const joints = Object.fromEntries(
    ANYADANCE_JOINTS.map((name) => {
      const a = current.joints[name];
      const b = restart.joints[name];
      return [name, { p: a.p.map((value, axis) => value + (b.p[axis] - value) * blend), q: slerpQuaternion(a.q, b.q, blend) }];
    }),
  );
  return {
    ...timing,
    joints,
    leftFingers: restart.leftFingers || current.leftFingers,
    rightFingers: restart.rightFingers || current.rightFingers,
    loopBlend: blend,
  };
}
