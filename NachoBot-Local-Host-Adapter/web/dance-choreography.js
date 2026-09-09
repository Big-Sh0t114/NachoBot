export const DANCE_STYLE_META = Object.freeze({
  idle: { label: '自然待机', bpm: 54, phrases: ['呼吸待机'] },
  cute: { label: '软萌甜舞', bpm: 112, phrases: ['踏步挥手', '双手应援', '俏皮转身'] },
  energetic: { label: '元气爵士', bpm: 132, phrases: ['开合律动', '高抬腿', '热力冲刺'] },
  kpop: { label: 'K-pop 编舞', bpm: 124, phrases: ['侧步手势', '身体波浪', '舞台定点'] },
  hiphop: { label: '嘻哈 Groove', bpm: 98, phrases: ['低位律动', '肩胸击拍', '左右 Punch'] },
  shuffle: { label: '曳步舞', bpm: 138, phrases: ['Running Man', 'T-Step', '交叉换步'] },
  elegant: { label: '优雅爵士', bpm: 92, phrases: ['缓慢侧步', '手臂弧线', '轻盈旋转'] },
  gesture: { label: '动作组合秀', bpm: 90, phrases: ['握拳蓄力', '双手合掌', '扶腿下沉', '展开谢幕'] },
  random: { label: '自动串舞', bpm: 120, phrases: ['自动串舞'] },
});

export const DANCE_STYLE_IDS = Object.freeze(Object.keys(DANCE_STYLE_META));
export const MOTION_STYLE_IDS = Object.freeze(
  DANCE_STYLE_IDS.filter((style) => !['idle', 'random'].includes(style)),
);

const RANDOM_SEQUENCE = Object.freeze(['cute', 'kpop', 'hiphop', 'gesture', 'shuffle', 'energetic', 'elegant']);
const TAU = Math.PI * 2;

function clamp(value, min = -1, max = 1) {
  return Math.min(max, Math.max(min, value));
}

function smoothstep(edge0, edge1, value) {
  const t = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return t * t * (3 - 2 * t);
}

function createPose(style, phrase, bpm) {
  return { style, phrase, bpm, hips: { x: 0, y: 0, z: 0 }, bones: {}, ik: null };
}

function bone(pose, name, x = 0, y = 0, z = 0) {
  pose.bones[name] = { x, y, z };
}

function addBone(pose, name, x = 0, y = 0, z = 0) {
  const current = pose.bones[name] || { x: 0, y: 0, z: 0 };
  pose.bones[name] = { x: current.x + x, y: current.y + y, z: current.z + z };
}

function blendBone(pose, name, target, amount) {
  const current = pose.bones[name] || { x: 0, y: 0, z: 0 };
  pose.bones[name] = {
    x: current.x + (target.x - current.x) * amount,
    y: current.y + (target.y - current.y) * amount,
    z: current.z + (target.z - current.z) * amount,
  };
}

function armRestAmount(clock) {
  const beatInPhrase = clock.beat % 8;
  const settle = smoothstep(5.35, 6.15, beatInPhrase);
  const resume = 1 - smoothstep(7.35, 7.95, beatInPhrase);
  return settle * resume;
}

function applyArmRest(pose, clock) {
  // Every eight-beat phrase contains a deliberate recovery beat.  The arms
  // travel back beside the torso instead of remaining in a permanent T pose;
  // legs and hips keep a small groove so the character still feels alive.
  const amount = armRestAmount(clock);
  if (amount <= 0) return pose;
  blendBone(pose, 'leftShoulder', { x: 0, y: 0, z: 0.015 }, amount);
  blendBone(pose, 'rightShoulder', { x: 0, y: 0, z: -0.015 }, amount);
  blendBone(pose, 'leftUpperArm', { x: 0.035, y: 0.045, z: -1.12 }, amount);
  blendBone(pose, 'rightUpperArm', { x: -0.035, y: -0.045, z: 1.12 }, amount);
  blendBone(pose, 'leftLowerArm', { x: 0.1, y: 0.02, z: -0.06 }, amount);
  blendBone(pose, 'rightLowerArm', { x: -0.1, y: -0.02, z: 0.06 }, amount);
  blendBone(pose, 'leftHand', { x: 0, y: 0.035, z: 0 }, amount);
  blendBone(pose, 'rightHand', { x: 0, y: -0.035, z: 0 }, amount);
  return pose;
}

function danceClock(style, elapsedSeconds) {
  const meta = DANCE_STYLE_META[style] || DANCE_STYLE_META.idle;
  const beat = Math.max(0, elapsedSeconds) * meta.bpm / 60;
  const beatFraction = beat - Math.floor(beat);
  const alternating = Math.sin(Math.PI * beat);
  const side = Math.sin(Math.PI * beat / 2);
  const wide = Math.sin(Math.PI * beat / 4);
  const groove = Math.sin(TAU * beatFraction);
  const bounce = Math.sin(Math.PI * beatFraction) ** 2;
  const leftLift = Math.max(0, alternating);
  const rightLift = Math.max(0, -alternating);
  const phrase = Math.floor(beat / 8) % Math.max(1, meta.phrases.length);
  return { meta, beat, alternating, side, wide, groove, bounce, leftLift, rightLift, phrase };
}

export function resolveDanceStyle(style, elapsedSeconds = 0) {
  if (style !== 'random') return DANCE_STYLE_META[style] ? style : 'idle';
  return RANDOM_SEQUENCE[Math.floor(Math.max(0, elapsedSeconds) / 12) % RANDOM_SEQUENCE.length];
}

export function describeDance(style, elapsedSeconds = 0) {
  const resolvedStyle = resolveDanceStyle(style, elapsedSeconds);
  const clock = danceClock(resolvedStyle, elapsedSeconds);
  const { meta, phrase } = clock;
  return {
    requestedStyle: DANCE_STYLE_META[style] ? style : 'idle',
    style: resolvedStyle,
    label: meta.label,
    phrase: resolvedStyle !== 'gesture' && armRestAmount(clock) > 0.72 ? '收臂换气' : meta.phrases[phrase],
  };
}

function sampleIdle(time) {
  const pose = createPose('idle', 0, DANCE_STYLE_META.idle.bpm);
  const breath = (Math.sin(time * 1.65) + 1) * 0.5;
  const look = Math.sin(time * 0.37);
  pose.hips.y = breath * 0.004;
  bone(pose, 'spine', -0.012 * breath, 0, 0);
  bone(pose, 'chest', 0.018 * breath, 0.014 * look, 0);
  bone(pose, 'neck', 0, -0.018 * look, 0);
  bone(pose, 'head', 0.012 * Math.sin(time * 0.53), -0.025 * look, 0.009 * Math.sin(time * 0.31));
  bone(pose, 'leftShoulder', 0, 0, 0.015);
  bone(pose, 'rightShoulder', 0, 0, -0.015);
  bone(pose, 'leftUpperArm', 0.025, 0.035, -1.08);
  bone(pose, 'rightUpperArm', -0.025, -0.035, 1.08);
  bone(pose, 'leftLowerArm', 0.08, 0.01, -0.05);
  bone(pose, 'rightLowerArm', -0.08, -0.01, 0.05);
  return pose;
}

function sampleCute(time) {
  const c = danceClock('cute', time);
  const pose = createPose('cute', c.phrase, c.meta.bpm);
  pose.hips.x = c.side * 0.055;
  pose.hips.y = c.bounce * 0.028;
  pose.hips.z = Math.abs(c.side) * -0.012;
  bone(pose, 'hips', 0.015 * c.groove, c.side * 0.12, -c.side * 0.055);
  bone(pose, 'spine', -0.025 * c.bounce, -c.side * 0.065, c.side * 0.025);
  bone(pose, 'chest', 0.04 * c.bounce, -c.side * 0.1, c.side * 0.04);
  bone(pose, 'leftUpperLeg', c.alternating * 0.19, 0, 0.045 + c.side * 0.035);
  bone(pose, 'rightUpperLeg', -c.alternating * 0.19, 0, -0.045 + c.side * 0.035);
  bone(pose, 'leftLowerLeg', c.rightLift * 0.28, 0, 0);
  bone(pose, 'rightLowerLeg', c.leftLift * 0.28, 0, 0);
  bone(pose, 'leftFoot', -c.rightLift * 0.12, 0, 0.025 * c.side);
  bone(pose, 'rightFoot', -c.leftLift * 0.12, 0, 0.025 * c.side);
  bone(pose, 'neck', 0.02 * c.bounce, -c.side * 0.045, -c.side * 0.035);
  bone(pose, 'head', -0.025 * c.bounce, -c.side * 0.075, -c.side * 0.075);

  if (c.phrase === 0) {
    bone(pose, 'leftUpperArm', 0.18 + c.leftLift * 0.32, -0.08, 0.48 + c.side * 0.13);
    bone(pose, 'rightUpperArm', -0.12 - c.rightLift * 0.25, 0.08, -0.38 + c.side * 0.1);
    bone(pose, 'leftLowerArm', 0.55 + 0.2 * c.groove, 0, 0.24);
    bone(pose, 'rightLowerArm', -0.48 - 0.17 * c.groove, 0, -0.2);
    bone(pose, 'leftHand', 0, 0.25 * c.groove, 0.16 * c.groove);
    bone(pose, 'rightHand', 0, -0.25 * c.groove, -0.16 * c.groove);
  } else if (c.phrase === 1) {
    const cheer = 0.74 + 0.13 * c.groove;
    bone(pose, 'leftUpperArm', -0.15 + 0.1 * c.side, 0, cheer);
    bone(pose, 'rightUpperArm', 0.15 + 0.1 * c.side, 0, -cheer);
    bone(pose, 'leftLowerArm', 0.3 + 0.22 * c.bounce, 0, 0.18);
    bone(pose, 'rightLowerArm', -0.3 - 0.22 * c.bounce, 0, -0.18);
  } else {
    bone(pose, 'leftUpperArm', 0.28 + 0.18 * c.groove, -0.16 * c.side, 0.32);
    bone(pose, 'rightUpperArm', -0.28 - 0.18 * c.groove, -0.16 * c.side, -0.32);
    bone(pose, 'leftLowerArm', 0.7, 0.12 * c.side, 0.34);
    bone(pose, 'rightLowerArm', -0.7, 0.12 * c.side, -0.34);
    addBone(pose, 'hips', 0, c.wide * 0.18, 0);
  }
  return applyArmRest(pose, c);
}

function sampleEnergetic(time) {
  const c = danceClock('energetic', time);
  const pose = createPose('energetic', c.phrase, c.meta.bpm);
  pose.hips.x = c.side * 0.075;
  pose.hips.y = c.bounce * 0.075;
  pose.hips.z = -0.025 * c.bounce;
  bone(pose, 'hips', -0.03 * c.bounce, c.side * 0.18, -c.side * 0.065);
  bone(pose, 'spine', -0.1 * c.bounce, -c.side * 0.1, c.side * 0.04);
  bone(pose, 'chest', 0.14 * c.bounce, -c.side * 0.15, c.side * 0.08);
  bone(pose, 'leftUpperLeg', c.alternating * 0.42, 0, 0.075);
  bone(pose, 'rightUpperLeg', -c.alternating * 0.42, 0, -0.075);
  bone(pose, 'leftLowerLeg', c.rightLift * 0.58, 0, 0);
  bone(pose, 'rightLowerLeg', c.leftLift * 0.58, 0, 0);
  bone(pose, 'leftFoot', -c.rightLift * 0.22, 0, 0);
  bone(pose, 'rightFoot', -c.leftLift * 0.22, 0, 0);
  if (c.phrase === 0) {
    bone(pose, 'leftUpperArm', 0.28 + c.alternating * 0.32, 0, 0.7);
    bone(pose, 'rightUpperArm', -0.28 - c.alternating * 0.32, 0, -0.7);
    bone(pose, 'leftLowerArm', 0.5 + c.leftLift * 0.35, 0, 0.12);
    bone(pose, 'rightLowerArm', -0.5 - c.rightLift * 0.35, 0, -0.12);
  } else if (c.phrase === 1) {
    bone(pose, 'leftUpperArm', -0.42 + c.side * 0.45, -0.12, 0.5);
    bone(pose, 'rightUpperArm', 0.42 + c.side * 0.45, 0.12, -0.5);
    bone(pose, 'leftLowerArm', 0.22 + c.bounce * 0.5, 0, 0.25);
    bone(pose, 'rightLowerArm', -0.22 - c.bounce * 0.5, 0, -0.25);
  } else {
    const punch = clamp(c.groove);
    bone(pose, 'leftUpperArm', 0.15 + punch * 0.55, -0.1, 0.42);
    bone(pose, 'rightUpperArm', -0.15 - punch * 0.55, 0.1, -0.42);
    bone(pose, 'leftLowerArm', 0.72 - c.leftLift * 0.45, 0, 0.08);
    bone(pose, 'rightLowerArm', -0.72 + c.rightLift * 0.45, 0, -0.08);
  }
  bone(pose, 'neck', 0.025 * c.groove, -0.05 * c.side, 0);
  bone(pose, 'head', -0.04 * c.bounce, -0.08 * c.side, -0.03 * c.side);
  return applyArmRest(pose, c);
}

function sampleKpop(time) {
  const c = danceClock('kpop', time);
  const pose = createPose('kpop', c.phrase, c.meta.bpm);
  const bodyWave = Math.sin(TAU * (c.beat / 4));
  pose.hips.x = c.side * 0.085;
  pose.hips.y = c.bounce * 0.035;
  pose.hips.z = bodyWave * 0.018;
  bone(pose, 'hips', bodyWave * 0.09, c.side * 0.2, -c.side * 0.09);
  bone(pose, 'spine', -bodyWave * 0.13, -c.side * 0.12, c.side * 0.055);
  bone(pose, 'chest', bodyWave * 0.18, -c.side * 0.18, c.side * 0.09);
  bone(pose, 'upperChest', bodyWave * 0.09, c.side * 0.08, -c.side * 0.03);
  bone(pose, 'leftUpperLeg', c.alternating * 0.27, -0.03 * c.side, 0.08 + c.side * 0.04);
  bone(pose, 'rightUpperLeg', -c.alternating * 0.27, 0.03 * c.side, -0.08 + c.side * 0.04);
  bone(pose, 'leftLowerLeg', c.rightLift * 0.38, 0, 0);
  bone(pose, 'rightLowerLeg', c.leftLift * 0.38, 0, 0);
  if (c.phrase === 0) {
    bone(pose, 'leftUpperArm', 0.18 + c.side * 0.28, -0.18, 0.45 + c.alternating * 0.22);
    bone(pose, 'rightUpperArm', -0.18 + c.side * 0.28, 0.18, -0.45 + c.alternating * 0.22);
    bone(pose, 'leftLowerArm', 0.66 + c.groove * 0.18, 0.2, 0.2);
    bone(pose, 'rightLowerArm', -0.66 - c.groove * 0.18, -0.2, -0.2);
  } else if (c.phrase === 1) {
    bone(pose, 'leftUpperArm', -0.2 + bodyWave * 0.3, -0.08, 0.74);
    bone(pose, 'rightUpperArm', 0.2 - bodyWave * 0.3, 0.08, -0.74);
    bone(pose, 'leftLowerArm', 0.45 + c.bounce * 0.3, 0, 0.28);
    bone(pose, 'rightLowerArm', -0.45 - c.bounce * 0.3, 0, -0.28);
  } else {
    const point = c.alternating >= 0 ? 1 : -1;
    bone(pose, 'leftUpperArm', point > 0 ? -0.48 : 0.36, -0.2, point > 0 ? 0.82 : 0.3);
    bone(pose, 'rightUpperArm', point < 0 ? 0.48 : -0.36, 0.2, point < 0 ? -0.82 : -0.3);
    bone(pose, 'leftLowerArm', 0.34, 0.15, 0.12);
    bone(pose, 'rightLowerArm', -0.34, -0.15, -0.12);
  }
  bone(pose, 'neck', -bodyWave * 0.035, -c.side * 0.055, c.side * 0.02);
  bone(pose, 'head', bodyWave * 0.05, -c.side * 0.1, -c.side * 0.055);
  return applyArmRest(pose, c);
}

function sampleHiphop(time) {
  const c = danceClock('hiphop', time);
  const pose = createPose('hiphop', c.phrase, c.meta.bpm);
  const halfBeat = Math.sin(Math.PI * c.beat * 2);
  pose.hips.x = c.side * 0.07;
  pose.hips.y = -0.07 + c.bounce * 0.045;
  pose.hips.z = -0.035 + c.groove * 0.012;
  bone(pose, 'hips', -0.1 + c.bounce * 0.06, c.side * 0.16, -c.side * 0.09);
  bone(pose, 'spine', 0.1 - c.bounce * 0.12, -c.side * 0.08, halfBeat * 0.035);
  bone(pose, 'chest', -0.12 + c.bounce * 0.2, c.side * 0.14, -halfBeat * 0.06);
  bone(pose, 'leftShoulder', 0, 0, 0.08 + c.alternating * 0.08);
  bone(pose, 'rightShoulder', 0, 0, -0.08 + c.alternating * 0.08);
  bone(pose, 'leftUpperLeg', 0.12 + c.alternating * 0.2, 0, 0.12);
  bone(pose, 'rightUpperLeg', 0.12 - c.alternating * 0.2, 0, -0.12);
  bone(pose, 'leftLowerLeg', 0.18 + c.rightLift * 0.35, 0, 0);
  bone(pose, 'rightLowerLeg', 0.18 + c.leftLift * 0.35, 0, 0);
  if (c.phrase === 0) {
    bone(pose, 'leftUpperArm', 0.34 + halfBeat * 0.18, -0.18, 0.48);
    bone(pose, 'rightUpperArm', -0.34 - halfBeat * 0.18, 0.18, -0.48);
    bone(pose, 'leftLowerArm', 0.78 - c.leftLift * 0.28, 0, 0.2);
    bone(pose, 'rightLowerArm', -0.78 + c.rightLift * 0.28, 0, -0.2);
  } else if (c.phrase === 1) {
    bone(pose, 'leftUpperArm', 0.1 + c.alternating * 0.48, -0.3, 0.56);
    bone(pose, 'rightUpperArm', -0.1 + c.alternating * 0.48, 0.3, -0.56);
    bone(pose, 'leftLowerArm', 0.62, 0.2 * c.side, 0.08);
    bone(pose, 'rightLowerArm', -0.62, 0.2 * c.side, -0.08);
  } else {
    const punch = Math.sin(Math.PI * c.beat);
    bone(pose, 'leftUpperArm', 0.5 * Math.max(0, punch), -0.24, 0.38);
    bone(pose, 'rightUpperArm', -0.5 * Math.max(0, -punch), 0.24, -0.38);
    bone(pose, 'leftLowerArm', 0.25 + 0.55 * c.rightLift, 0, 0.1);
    bone(pose, 'rightLowerArm', -0.25 - 0.55 * c.leftLift, 0, -0.1);
  }
  bone(pose, 'neck', c.groove * 0.04, -c.side * 0.06, 0);
  bone(pose, 'head', -c.groove * 0.07, -c.side * 0.09, -c.side * 0.035);
  return applyArmRest(pose, c);
}

function sampleShuffle(time) {
  const c = danceClock('shuffle', time);
  const pose = createPose('shuffle', c.phrase, c.meta.bpm);
  const fastSide = Math.sin(Math.PI * c.beat);
  const leftStep = Math.max(0, fastSide);
  const rightStep = Math.max(0, -fastSide);
  pose.hips.x = c.side * 0.09;
  pose.hips.y = c.bounce * 0.065;
  pose.hips.z = -0.03 * c.bounce;
  bone(pose, 'hips', -0.035, c.wide * 0.11, -c.side * 0.055);
  bone(pose, 'spine', -0.04 * c.bounce, -c.side * 0.055, c.side * 0.035);
  bone(pose, 'chest', 0.07 * c.bounce, -c.side * 0.08, -c.side * 0.05);
  bone(pose, 'leftUpperLeg', -leftStep * 0.58 + rightStep * 0.22, 0, 0.06 + c.side * 0.05);
  bone(pose, 'rightUpperLeg', -rightStep * 0.58 + leftStep * 0.22, 0, -0.06 + c.side * 0.05);
  bone(pose, 'leftLowerLeg', leftStep * 0.78 + rightStep * 0.16, 0, 0);
  bone(pose, 'rightLowerLeg', rightStep * 0.78 + leftStep * 0.16, 0, 0);
  bone(pose, 'leftFoot', -leftStep * 0.35, c.side * 0.12, 0.04);
  bone(pose, 'rightFoot', -rightStep * 0.35, -c.side * 0.12, -0.04);
  const armDrive = c.phrase === 1 ? c.groove : c.alternating;
  bone(pose, 'leftUpperArm', 0.24 + armDrive * 0.3, -0.12, 0.48 + c.side * 0.12);
  bone(pose, 'rightUpperArm', -0.24 - armDrive * 0.3, 0.12, -0.48 + c.side * 0.12);
  bone(pose, 'leftLowerArm', 0.62 - leftStep * 0.28, 0, 0.16);
  bone(pose, 'rightLowerArm', -0.62 + rightStep * 0.28, 0, -0.16);
  if (c.phrase === 2) {
    addBone(pose, 'hips', 0, c.side * 0.2, -c.side * 0.06);
    addBone(pose, 'leftUpperLeg', 0, c.side * 0.08, c.side * 0.09);
    addBone(pose, 'rightUpperLeg', 0, c.side * 0.08, c.side * 0.09);
  }
  bone(pose, 'head', -0.035 * c.bounce, -0.055 * c.side, 0);
  return applyArmRest(pose, c);
}

function sampleElegant(time) {
  const c = danceClock('elegant', time);
  const pose = createPose('elegant', c.phrase, c.meta.bpm);
  const arc = Math.sin(TAU * c.beat / 8);
  pose.hips.x = c.side * 0.045;
  pose.hips.y = c.bounce * 0.016;
  pose.hips.z = -Math.abs(c.side) * 0.008;
  bone(pose, 'hips', 0.015 * arc, c.wide * 0.15, -c.side * 0.035);
  bone(pose, 'spine', -0.02, -c.side * 0.05, c.side * 0.03);
  bone(pose, 'chest', 0.055 + 0.025 * arc, -c.side * 0.1, c.side * 0.055);
  bone(pose, 'leftUpperLeg', c.alternating * 0.12, -0.025 * c.side, 0.035 + c.side * 0.02);
  bone(pose, 'rightUpperLeg', -c.alternating * 0.12, 0.025 * c.side, -0.035 + c.side * 0.02);
  bone(pose, 'leftLowerLeg', c.rightLift * 0.18, 0, 0);
  bone(pose, 'rightLowerLeg', c.leftLift * 0.18, 0, 0);
  if (c.phrase === 0) {
    bone(pose, 'leftUpperArm', 0.12 + arc * 0.18, -0.16, 0.46 + c.side * 0.12);
    bone(pose, 'rightUpperArm', -0.12 + arc * 0.18, 0.16, -0.46 + c.side * 0.12);
    bone(pose, 'leftLowerArm', 0.46 + arc * 0.18, 0.12, 0.26);
    bone(pose, 'rightLowerArm', -0.46 + arc * 0.18, -0.12, -0.26);
  } else if (c.phrase === 1) {
    bone(pose, 'leftUpperArm', -0.16 + arc * 0.26, -0.05, 0.76);
    bone(pose, 'rightUpperArm', 0.16 - arc * 0.26, 0.05, -0.76);
    bone(pose, 'leftLowerArm', 0.38, 0.1 + arc * 0.15, 0.2);
    bone(pose, 'rightLowerArm', -0.38, -0.1 - arc * 0.15, -0.2);
  } else {
    bone(pose, 'leftUpperArm', 0.1 + c.side * 0.2, -0.22, 0.58);
    bone(pose, 'rightUpperArm', -0.1 + c.side * 0.2, 0.22, -0.58);
    bone(pose, 'leftLowerArm', 0.62, 0.16, 0.2);
    bone(pose, 'rightLowerArm', -0.62, -0.16, -0.2);
    addBone(pose, 'hips', 0, arc * 0.34, 0);
  }
  bone(pose, 'neck', -0.015 * arc, -c.side * 0.045, c.side * 0.018);
  bone(pose, 'head', 0.02 * arc, -c.side * 0.085, -c.side * 0.04);
  return applyArmRest(pose, c);
}

function motifEnvelope(beat) {
  const local = beat % 8;
  const enter = smoothstep(0.2, 1.05, local);
  const leave = 1 - smoothstep(6.35, 7.45, local);
  return enter * leave;
}

function sampleGesture(time) {
  const c = danceClock('gesture', time);
  const pose = createPose('gesture', c.phrase, c.meta.bpm);
  const hold = motifEnvelope(c.beat);
  const sway = Math.sin(Math.PI * c.beat / 2);
  const pulse = Math.sin(Math.PI * c.beat) ** 2;
  pose.hips.x = sway * 0.035;
  pose.hips.y = pulse * 0.015;
  bone(pose, 'hips', 0.02 * pulse, 0.08 * sway, -0.035 * sway);
  bone(pose, 'spine', -0.025 + 0.02 * pulse, -0.045 * sway, 0.02 * sway);
  bone(pose, 'chest', 0.045 + 0.035 * pulse, -0.065 * sway, 0.035 * sway);
  bone(pose, 'leftUpperLeg', 0.035 + 0.08 * c.alternating, 0, 0.045);
  bone(pose, 'rightUpperLeg', 0.035 - 0.08 * c.alternating, 0, -0.045);
  bone(pose, 'leftLowerLeg', 0.045 + 0.08 * c.rightLift, 0, 0);
  bone(pose, 'rightLowerLeg', 0.045 + 0.08 * c.leftLift, 0, 0);
  bone(pose, 'head', -0.02 * pulse, -0.05 * sway, -0.025 * sway);

  if (c.phrase === 0) {
    // Both fists travel from a neutral hang to the waist, hold for four
    // beats, then release.  The elbows stay close to the ribcage.
    blendBone(pose, 'leftUpperArm', { x: 0.2, y: -0.16, z: -0.72 }, hold);
    blendBone(pose, 'rightUpperArm', { x: 0.2, y: 0.16, z: 0.72 }, hold);
    blendBone(pose, 'leftLowerArm', { x: 0.9, y: -0.08, z: -0.38 }, hold);
    blendBone(pose, 'rightLowerArm', { x: -0.9, y: 0.08, z: 0.38 }, hold);
    blendBone(pose, 'leftHand', { x: 0.08, y: -0.12, z: -0.08 }, hold);
    blendBone(pose, 'rightHand', { x: -0.08, y: 0.12, z: 0.08 }, hold);
    pose.ik = { type: 'waist-fists', amount: hold };
  } else if (c.phrase === 1) {
    // Mirrored shoulder/elbow targets bring both palms to the sternum.
    blendBone(pose, 'leftUpperArm', { x: 0.16, y: -0.38, z: -0.42 }, hold);
    blendBone(pose, 'rightUpperArm', { x: 0.16, y: 0.38, z: 0.42 }, hold);
    blendBone(pose, 'leftLowerArm', { x: 0.34, y: -0.18, z: -1.02 }, hold);
    blendBone(pose, 'rightLowerArm', { x: -0.34, y: 0.18, z: 1.02 }, hold);
    blendBone(pose, 'leftHand', { x: 0.04, y: -0.26, z: -0.12 }, hold);
    blendBone(pose, 'rightHand', { x: -0.04, y: 0.26, z: 0.12 }, hold);
    addBone(pose, 'chest', -0.035 * hold, 0, 0);
    pose.ik = { type: 'prayer', amount: hold };
  } else if (c.phrase === 2) {
    // A controlled squat with both hands settling on the upper thighs.
    pose.hips.y -= 0.105 * hold;
    addBone(pose, 'hips', -0.16 * hold, 0, 0);
    addBone(pose, 'spine', 0.2 * hold, 0, 0);
    addBone(pose, 'chest', 0.12 * hold, 0, 0);
    addBone(pose, 'leftUpperLeg', 0.33 * hold, 0, 0.08 * hold);
    addBone(pose, 'rightUpperLeg', 0.33 * hold, 0, -0.08 * hold);
    addBone(pose, 'leftLowerLeg', 0.43 * hold, 0, 0);
    addBone(pose, 'rightLowerLeg', 0.43 * hold, 0, 0);
    blendBone(pose, 'leftUpperArm', { x: 0.32, y: -0.08, z: -0.98 }, hold);
    blendBone(pose, 'rightUpperArm', { x: 0.32, y: 0.08, z: 0.98 }, hold);
    blendBone(pose, 'leftLowerArm', { x: 0.22, y: 0, z: -0.12 }, hold);
    blendBone(pose, 'rightLowerArm', { x: -0.22, y: 0, z: 0.12 }, hold);
    blendBone(pose, 'leftHand', { x: -0.2, y: 0, z: -0.08 }, hold);
    blendBone(pose, 'rightHand', { x: -0.2, y: 0, z: 0.08 }, hold);
    pose.ik = { type: 'thighs', amount: hold };
  } else {
    blendBone(pose, 'leftUpperArm', { x: -0.22, y: -0.08, z: 0.55 }, hold);
    blendBone(pose, 'rightUpperArm', { x: -0.22, y: 0.08, z: -0.55 }, hold);
    blendBone(pose, 'leftLowerArm', { x: 0.24, y: 0, z: 0.16 }, hold);
    blendBone(pose, 'rightLowerArm', { x: -0.24, y: 0, z: -0.16 }, hold);
    addBone(pose, 'chest', -0.06 * hold, 0, 0);
  }
  return pose;
}

const SAMPLERS = Object.freeze({
  idle: sampleIdle,
  cute: sampleCute,
  energetic: sampleEnergetic,
  kpop: sampleKpop,
  hiphop: sampleHiphop,
  shuffle: sampleShuffle,
  elegant: sampleElegant,
  gesture: sampleGesture,
});

export function sampleDance(style, elapsedSeconds) {
  const resolvedStyle = resolveDanceStyle(style, elapsedSeconds);
  return (SAMPLERS[resolvedStyle] || SAMPLERS.idle)(Math.max(0, elapsedSeconds));
}
