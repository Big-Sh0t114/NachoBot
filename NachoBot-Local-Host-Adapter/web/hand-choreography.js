const FINGERS = Object.freeze({
  thumb: ['ThumbMetacarpal', 'ThumbProximal', 'ThumbDistal'],
  index: ['IndexProximal', 'IndexIntermediate', 'IndexDistal'],
  middle: ['MiddleProximal', 'MiddleIntermediate', 'MiddleDistal'],
  ring: ['RingProximal', 'RingIntermediate', 'RingDistal'],
  little: ['LittleProximal', 'LittleIntermediate', 'LittleDistal'],
});

export const HAND_GESTURE_IDS = Object.freeze(['relaxed', 'open', 'fist', 'point', 'soft']);

const GESTURES = Object.freeze({
  open: {
    thumb: [0.12, 0.05, 0.04], index: [0.04, 0.03, 0.02], middle: [0.03, 0.03, 0.02],
    ring: [0.05, 0.04, 0.03], little: [0.08, 0.05, 0.04],
  },
  soft: {
    thumb: [0.16, 0.09, 0.07], index: [0.12, 0.15, 0.1], middle: [0.15, 0.19, 0.13],
    ring: [0.18, 0.22, 0.15], little: [0.22, 0.26, 0.18],
  },
  relaxed: {
    thumb: [0.2, 0.12, 0.09], index: [0.2, 0.25, 0.16], middle: [0.24, 0.3, 0.2],
    ring: [0.28, 0.34, 0.23], little: [0.32, 0.39, 0.26],
  },
  fist: {
    thumb: [0.4, 0.32, 0.22], index: [0.62, 0.84, 0.58], middle: [0.68, 0.9, 0.62],
    ring: [0.72, 0.94, 0.65], little: [0.75, 0.97, 0.68],
  },
  point: {
    thumb: [0.28, 0.17, 0.12], index: [0.03, 0.02, 0.01], middle: [0.66, 0.87, 0.6],
    ring: [0.7, 0.91, 0.63], little: [0.73, 0.94, 0.66],
  },
});

function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

export function sampleHandGesture(name, side, amount = 1) {
  const gesture = GESTURES[name] || GESTURES.relaxed;
  const sign = side === 'left' ? -1 : 1;
  const weight = clamp01(amount);
  const bones = {};
  for (const [finger, suffixes] of Object.entries(FINGERS)) {
    const curls = gesture[finger];
    suffixes.forEach((suffix, index) => {
      const isThumb = finger === 'thumb';
      bones[`${side}${suffix}`] = {
        x: isThumb ? curls[index] * 0.22 * weight : 0,
        y: isThumb ? sign * curls[index] * 0.32 * weight : 0,
        z: sign * curls[index] * weight,
      };
    });
  }
  return bones;
}

// AnyaDance exports one curl value per finger. Blend from a relaxed hand to
// the authored fist pose so imported MMD fingers remain expressive without
// depending on the source model's finger axes.
export function sampleHandCurls(curls, side) {
  const values = Array.isArray(curls) ? curls : [];
  const sign = side === 'left' ? -1 : 1;
  const bones = {};
  for (const [fingerIndex, finger] of Object.keys(FINGERS).entries()) {
    const suffixes = FINGERS[finger];
    const amount = clamp01(Number(values[fingerIndex] ?? 0));
    const relaxed = GESTURES.relaxed[finger];
    const fist = GESTURES.fist[finger];
    suffixes.forEach((suffix, index) => {
      const curl = relaxed[index] + (fist[index] - relaxed[index]) * amount;
      const isThumb = finger === 'thumb';
      bones[`${side}${suffix}`] = {
        x: isThumb ? curl * 0.22 : 0,
        y: isThumb ? sign * curl * 0.32 : 0,
        z: sign * curl,
      };
    });
  }
  return bones;
}

export function danceHandGesture(style, elapsedSeconds, bpm) {
  const beat = Math.max(0, elapsedSeconds) * bpm / 60;
  const phrase = Math.floor(beat / 8);
  const sequences = {
    cute: [['soft', 'open'], ['soft', 'soft'], ['fist', 'fist'], ['open', 'open']],
    energetic: [['fist', 'fist'], ['open', 'fist'], ['fist', 'open'], ['soft', 'soft']],
    kpop: [['point', 'soft'], ['soft', 'point'], ['fist', 'fist'], ['open', 'open']],
    hiphop: [['fist', 'fist'], ['point', 'fist'], ['fist', 'point'], ['relaxed', 'relaxed']],
    shuffle: [['soft', 'soft'], ['fist', 'fist'], ['open', 'open'], ['relaxed', 'relaxed']],
    elegant: [['soft', 'soft'], ['open', 'open'], ['relaxed', 'soft'], ['soft', 'relaxed']],
    gesture: [['fist', 'fist'], ['soft', 'soft'], ['relaxed', 'relaxed'], ['open', 'open']],
  };
  const sequence = sequences[style] || [['relaxed', 'relaxed']];
  const selected = sequence[phrase % sequence.length];
  return { left: selected[0], right: selected[1] };
}
