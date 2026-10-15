const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '..', 'static', 'js', 'bgm-player.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function createFixture(sampleRate = 48000) {
    let wallTimeMs = 0;

    class MockAnalyser {
        constructor() {
            this._fftSize = 2048;
            this.smoothingTimeConstant = 0;
            this.frequencyBinCount = this._fftSize / 2;
            this.spectrum = new Float32Array(this.frequencyBinCount).fill(-Infinity);
        }

        set fftSize(value) {
            this._fftSize = value;
            this.frequencyBinCount = value / 2;
            this.spectrum = new Float32Array(this.frequencyBinCount).fill(-Infinity);
        }

        get fftSize() {
            return this._fftSize;
        }

        getFloatFrequencyData(target) {
            target.set(this.spectrum);
        }
    }

    class MockAudioContext {
        constructor() {
            this.sampleRate = sampleRate;
            this.currentTime = 0;
            this.state = 'running';
            this.destination = {};
        }

        createGain() {
            return {
                gain: {
                    value: 1,
                    cancelScheduledValues() {},
                    setValueAtTime() {},
                    setTargetAtTime() {},
                },
                connect() {},
                disconnect() {},
            };
        }

        createAnalyser() {
            return new MockAnalyser();
        }

        async decodeAudioData() {
            return { duration: 1 };
        }

        async resume() {
            this.state = 'running';
        }

        async suspend() {
            this.state = 'suspended';
        }
    }

    const context = vm.createContext({
        Event,
        EventTarget,
        Date,
        Float32Array,
        Uint8Array,
        Math,
        Number,
        Promise,
        console,
        performance: { now: () => wallTimeMs },
        navigator: { userActivation: { isActive: true } },
        fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(1) }),
        window: { AudioContext: MockAudioContext },
    });

    vm.runInContext(source, context, { filename: sourcePath });
    const player = new context.window.SeamlessBgmPlayer();
    const audioContext = player._ensureAudioGraph();
    player._paused = false;

    return {
        player,
        audioContext,
        analyser: player._analyser,
        binHz: sampleRate / player._analyser.fftSize,
        advance(seconds) {
            audioContext.currentTime += seconds;
            wallTimeMs += seconds * 1000;
        },
        advanceWall(seconds) {
            wallTimeMs += seconds * 1000;
        },
        setBin(index, normalizedAmplitude) {
            const amplitude = normalizedAmplitude * 0.20;
            player._analyser.spectrum[index] = amplitude > 0
                ? 20 * Math.log10(amplitude)
                : -Infinity;
        },
        setTone(frequencyHz, normalizedAmplitude) {
            const index = Math.round(frequencyHz / this.binHz);
            this.setBin(index, normalizedAmplitude);
            return index;
        },
        clearSpectrum() {
            player._analyser.spectrum.fill(-Infinity);
        },
    };
}

function step(fixture, frames, frameRate = 60) {
    let frame;
    for (let index = 0; index < frames; index += 1) {
        fixture.advance(1 / frameRate);
        frame = fixture.player.getReactiveFrame();
    }
    return frame;
}

function assertNormalizedFrame(frame) {
    for (const key of ['bass', 'mid', 'high', 'intensity', 'pulse']) {
        assert.ok(Number.isFinite(frame[key]), `${key} must remain finite`);
        assert.ok(frame[key] >= 0 && frame[key] <= 1, `${key} must remain in [0, 1]`);
    }
    assert.equal(typeof frame.beat, 'boolean');
}

test('float FFT ranges isolate bass, mid, and high while excluding DC', () => {
    for (const [frequencyHz, band] of [[93.75, 'bass'], [1000, 'mid'], [4000, 'high']]) {
        const fixture = createFixture();
        assert.equal(fixture.analyser.fftSize, 4096);
        assert.equal(fixture.analyser.smoothingTimeConstant, 0);
        fixture.setTone(frequencyHz, 0.35);
        step(fixture, 90);
        const frame = fixture.player.getReactiveFrame();
        assert.ok(frame[band] > 0.2, `${frequencyHz}Hz should activate ${band}`);
        for (const other of ['bass', 'mid', 'high']) {
            if (other !== band) assert.equal(frame[other], 0, `${frequencyHz}Hz leaked into ${other}`);
        }
        fixture.clearSpectrum();
        fixture.setBin(0, 1);
        step(fixture, 120);
        const dcFrame = fixture.player.getReactiveFrame();
        assert.equal(dcFrame.bass, 0);
        assert.equal(dcFrame.mid, 0);
        assert.equal(dcFrame.high, 0);
        assert.equal(dcFrame.intensity, 0);
    }
});

test('equal total power has equal strength regardless of how many bins contain it', () => {
    const singleBin = createFixture();
    singleBin.setTone(1000, 0.12);
    step(singleBin, 90);

    const fourBins = createFixture();
    const firstBin = Math.round(1000 / fourBins.binHz);
    for (let offset = 0; offset < 4; offset += 1) {
        fourBins.setBin(firstBin + offset, 0.06);
    }
    step(fourBins, 90);

    const left = singleBin.player.getReactiveFrame();
    const right = fourBins.player.getReactiveFrame();
    assert.ok(Math.abs(left.mid - right.mid) < 0.01, `${left.mid} vs ${right.mid}`);
    assert.ok(Math.abs(left.intensity - right.intensity) < 0.01);
});

test('halving a steady amplitude halves unsaturated band strength and intensity', () => {
    const fixture = createFixture();
    fixture.setTone(1000, 0.16);
    step(fixture, 90);
    const full = fixture.player.getReactiveFrame();

    fixture.clearSpectrum();
    fixture.setTone(1000, 0.08);
    step(fixture, 90);
    const half = fixture.player.getReactiveFrame();
    assert.ok(Math.abs(half.mid / full.mid - 0.5) < 0.02);
    assert.ok(Math.abs(half.intensity / full.intensity - 0.5) < 0.02);
});

test('non-finite bins, silence, and common sample rates always return normalized values', () => {
    const fixture = createFixture();
    fixture.analyser.spectrum[10] = NaN;
    fixture.analyser.spectrum[11] = Infinity;
    fixture.analyser.spectrum[12] = -Infinity;
    step(fixture, 120);
    const silent = fixture.player.getReactiveFrame();
    assertNormalizedFrame(silent);
    assert.equal(silent.bass, 0);
    assert.equal(silent.intensity, 0);

    for (const sampleRate of [8000, 16000, 44100, 96000]) {
        const atRate = createFixture(sampleRate);
        atRate.setTone(Math.min(4000, sampleRate / 4), 0.4);
        step(atRate, 90);
        assertNormalizedFrame(atRate.player.getReactiveFrame());
    }
});

test('same audio timestamp does not advance envelopes or duplicate an onset', () => {
    const fixture = createFixture();
    const bassIndex = fixture.setTone(100, 0.1);
    fixture.player.getReactiveFrame();
    for (let index = 0; index < 60; index += 1) {
        fixture.advance(1 / 60);
        fixture.player.getReactiveFrame();
    }
    fixture.analyser.spectrum[bassIndex] = 20 * Math.log10(0.5 * 0.20);
    fixture.advance(1 / 60);
    const onset = fixture.player.getReactiveFrame();
    assert.equal(onset.beat, true);
    const repeated = fixture.player.getReactiveFrame();
    assert.equal(repeated.beat, false);
    assert.equal(repeated.pulse, onset.pulse);
    assert.equal(repeated.bass, onset.bass);
});

test('a smooth amplitude ramp produces one comparable onset at 30, 60, and 144 Hz', () => {
    const observations = [];
    for (const frameRate of [30, 60, 144]) {
        const fixture = createFixture();
        const bassIndex = fixture.setTone(100, 0.1);
        fixture.player.getReactiveFrame();
        const beats = [];
        const duration = 1;
        const frameCount = Math.round(duration * frameRate);
        for (let index = 1; index <= frameCount; index += 1) {
            const now = index / frameRate;
            const ramp = Math.max(0, Math.min(1, (now - 0.5) / 0.1));
            const level = 0.1 + 0.5 * ramp;
            fixture.analyser.spectrum[bassIndex] = 20 * Math.log10(level * 0.20);
            fixture.advance(1 / frameRate);
            const frame = fixture.player.getReactiveFrame();
            if (frame.beat) beats.push({ time: now, pulse: frame.pulse });
        }
        assert.equal(beats.length, 1, `${frameRate}Hz generated ${beats.length} beats`);
        assert.ok(beats[0].time >= 0.5 && beats[0].time <= 0.62, `${frameRate}Hz onset at ${beats[0].time}`);
        observations.push(beats[0]);
    }

    const pulses = observations.map(item => item.pulse);
    assert.ok(Math.max(...pulses) - Math.min(...pulses) < 0.03, `pulses differ: ${pulses}`);
});

test('steady tones do not repeat beats; stronger onsets create stronger pulses', () => {
    const steady = createFixture();
    const bassIndex = steady.setTone(100, 0.12);
    steady.player.getReactiveFrame();
    const frames = [];
    for (let index = 0; index < 180; index += 1) {
        steady.advance(1 / 60);
        frames.push(steady.player.getReactiveFrame());
    }
    assert.equal(frames.filter(frame => frame.beat).length, 0);

    function pulseForRamp(target) {
        const fixture = createFixture();
        const index = fixture.setTone(100, 0.1);
        fixture.player.getReactiveFrame();
        for (let frame = 0; frame < 30; frame += 1) {
            fixture.advance(1 / 60);
            fixture.player.getReactiveFrame();
        }
        for (let frame = 1; frame <= 6; frame += 1) {
            const level = 0.1 + (target - 0.1) * frame / 6;
            fixture.analyser.spectrum[index] = 20 * Math.log10(level * 0.20);
            fixture.advance(1 / 60);
            const result = fixture.player.getReactiveFrame();
            if (result.beat) return result.pulse;
        }
        return 0;
    }

    const weaker = pulseForRamp(0.3);
    const stronger = pulseForRamp(0.6);
    assert.ok(weaker > 0);
    assert.ok(stronger > weaker);
    assert.ok(stronger <= 1);
});

test('refractory, pause decay with a frozen audio clock, resume, and track reset are safe', async () => {
    const fixture = createFixture();
    const index = fixture.setTone(100, 0.1);
    fixture.player.getReactiveFrame();
    step(fixture, 45);

    fixture.analyser.spectrum[index] = 20 * Math.log10(0.5 * 0.20);
    fixture.advance(1 / 60);
    assert.equal(fixture.player.getReactiveFrame().beat, true);
    fixture.analyser.spectrum[index] = 20 * Math.log10(0.6 * 0.20);
    fixture.advance(0.05);
    assert.equal(fixture.player.getReactiveFrame().beat, false);

    const beforePause = fixture.player.getReactiveFrame();
    fixture.player._paused = true;
    fixture.audioContext.state = 'suspended';
    fixture.advanceWall(1.5);
    const paused = fixture.player.getReactiveFrame();
    assert.ok(Math.abs(fixture.audioContext.currentTime - (0.05 + 46 / 60)) < 1e-12);
    assert.equal(paused.bass, 0);
    assert.equal(paused.intensity, 0);
    assert.equal(paused.pulse, 0);
    assert.ok(beforePause.bass > paused.bass);

    fixture.audioContext.state = 'running';
    fixture.player._paused = false;
    const resumed = fixture.player.getReactiveFrame();
    assert.equal(resumed.beat, false);
    assertNormalizedFrame(resumed);

    const ready = fixture.player.setTrack({ loopUrl: '/loop.wav' });
    const resetFrame = fixture.player.getReactiveFrame();
    assert.equal(resetFrame.bass, 0);
    assert.equal(resetFrame.pulse, 0);
    await ready;
});
