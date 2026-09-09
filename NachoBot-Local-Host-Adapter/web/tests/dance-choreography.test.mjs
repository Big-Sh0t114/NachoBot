import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DANCE_STYLE_META,
  DANCE_STYLE_IDS,
  describeDance,
  resolveDanceStyle,
  sampleDance,
} from '../dance-choreography.js';

function numericValues(pose) {
  return [
    ...Object.values(pose.hips),
    ...Object.values(pose.bones).flatMap((rotation) => Object.values(rotation)),
  ];
}

test('every selectable dance produces finite full-body pose data', () => {
  for (const style of DANCE_STYLE_IDS) {
    for (const time of [0, 0.31, 1.27, 4.8, 12.5, 25]) {
      const pose = sampleDance(style, time);
      assert.ok(numericValues(pose).every(Number.isFinite), `${style} generated an invalid value`);
      assert.ok(pose.bones.hips || style === 'idle');
    }
  }
});

test('random mode rotates through multiple actual dance styles', () => {
  const styles = new Set([0, 12, 24, 36, 48, 60, 72].map((time) => resolveDanceStyle('random', time)));
  assert.equal(styles.size, 7);
  assert.ok(!styles.has('idle'));
  assert.ok(!styles.has('random'));
});

test('choreography changes phrase and uses legs, spine and arms', () => {
  const samples = [0.4, 4.8, 9.6].map((time) => sampleDance('kpop', time));
  assert.ok(samples.some((pose) => pose.bones.leftUpperLeg));
  assert.ok(samples.some((pose) => pose.bones.chest));
  assert.ok(samples.some((pose) => pose.bones.leftUpperArm));
  assert.notDeepEqual(samples[0].bones.leftUpperArm, samples[2].bones.leftUpperArm);
  assert.notEqual(describeDance('kpop', 0.1).phrase, describeDance('kpop', 8).phrase);
});

test('each dance lowers both arms for a recovery beat', () => {
  for (const style of ['cute', 'energetic', 'kpop', 'hiphop', 'shuffle', 'elegant']) {
    const restTime = 7 * 60 / DANCE_STYLE_META[style].bpm;
    const pose = sampleDance(style, restTime);
    assert.ok(pose.bones.leftUpperArm.z < -0.9, `${style} left arm did not lower`);
    assert.ok(pose.bones.rightUpperArm.z > 0.9, `${style} right arm did not lower`);
    assert.equal(describeDance(style, restTime).phrase, '收臂换气');
  }
});

test('idle pose keeps arms naturally beside the torso instead of a T pose', () => {
  const pose = sampleDance('idle', 1);
  assert.ok(pose.bones.leftUpperArm.z < -0.9);
  assert.ok(pose.bones.rightUpperArm.z > 0.9);
});

test('gesture showcase is a four-part authored movement instead of limb oscillation', () => {
  const bpm = DANCE_STYLE_META.gesture.bpm;
  const atPhrase = (phrase, beat = 3) => sampleDance('gesture', (phrase * 8 + beat) * 60 / bpm);
  const fist = atPhrase(0);
  const prayer = atPhrase(1);
  const thigh = atPhrase(2);
  const finale = atPhrase(3);
  assert.equal(describeDance('gesture', 3 * 60 / bpm).phrase, '握拳蓄力');
  assert.equal(describeDance('gesture', 11 * 60 / bpm).phrase, '双手合掌');
  assert.ok(thigh.hips.y < fist.hips.y - 0.08, 'hands-on-thigh phrase should squat');
  assert.notDeepEqual(fist.bones.leftLowerArm, prayer.bones.leftLowerArm);
  assert.notDeepEqual(prayer.bones.leftUpperArm, finale.bones.leftUpperArm);
});
