const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '..', 'static', 'js', 'particle-system.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function createSimulation({ audioFrame, frameRate = 60, duration = 0 } = {}) {
    let currentTimeMs = 0;
    let nextAnimationId = 1;
    let nextAnimationCallback = null;
    let randomSeed = 123456789;
    let currentArcs = [];
    let currentFills = [];
    let currentStrokes = 0;
    let currentStrokeStyles = [];
    const snapshots = [];

    const canvasContext = {
        fillStyle: '',
        strokeStyle: '',
        lineWidth: 1,
        setTransform() {},
        clearRect() {
            currentArcs = [];
            currentFills = [];
            currentStrokes = 0;
            currentStrokeStyles = [];
        },
        beginPath() {},
        arc(x, y, radius) {
            currentArcs.push({ x, y, radius });
        },
        fill() {
            currentFills.push(this.fillStyle);
        },
        moveTo() {},
        lineTo() {},
        stroke() {
            currentStrokes += 1;
            currentStrokeStyles.push(this.strokeStyle);
        },
    };
    const canvas = {
        width: 0,
        height: 0,
        style: {},
        getContext: () => canvasContext,
    };
    const document = {
        getElementById(id) {
            if (id === 'bg-canvas') return canvas;
            return null;
        },
        addEventListener() {},
        removeEventListener() {},
    };
    const window = {
        innerWidth: 1000,
        innerHeight: 800,
        devicePixelRatio: 1,
        scrollY: 0,
        addEventListener() {},
        removeEventListener() {},
        EasterEggSystem: {
            getParticleEffects: () => ({
                rainbowActive: false,
                reactiveMultiplier: 1,
                particleLimit: 120,
            }),
        },
    };
    const seededMath = Object.create(Math);
    seededMath.random = () => {
        randomSeed = (1664525 * randomSeed + 1013904223) >>> 0;
        return randomSeed / 0x100000000;
    };

    const sandbox = vm.createContext({
        Math: seededMath,
        document,
        window,
        performance: { now: () => currentTimeMs },
        requestAnimationFrame(callback) {
            nextAnimationCallback = callback;
            return nextAnimationId++;
        },
        cancelAnimationFrame() {
            nextAnimationCallback = null;
        },
    });
    vm.runInContext(source, sandbox, { filename: sourcePath });

    const baseFrame = audioFrame || {
        bass: 0,
        mid: 0,
        high: 0,
        intensity: 0,
        pulse: 0,
        beat: false,
    };
    let frameIndex = 0;
    window.ParticleSystem.setAudioSource({
        getReactiveFrame: () => typeof baseFrame === 'function'
            ? baseFrame(currentTimeMs, frameIndex)
            : baseFrame,
    });
    window.ParticleSystem.start();

    const snapshot = timestamp => ({
        timestamp,
        arcs: currentArcs.map(point => ({ ...point })),
        fills: [...currentFills],
        strokes: currentStrokes,
        strokeStyles: [...currentStrokeStyles],
    });
    snapshots.push(snapshot(currentTimeMs));

    const frameCount = Math.round(duration * frameRate);
    for (let index = 1; index <= frameCount; index += 1) {
        currentTimeMs = index * 1000 / frameRate;
        frameIndex = index;
        const callback = nextAnimationCallback;
        nextAnimationCallback = null;
        assert.equal(typeof callback, 'function', 'particle animation should schedule the next frame');
        callback(currentTimeMs);
        snapshots.push(snapshot(currentTimeMs));
    }
    window.ParticleSystem.stop();

    return snapshots;
}

function findNearestSnapshot(snapshots, seconds) {
    const target = seconds * 1000;
    return snapshots.reduce((best, item) =>
        Math.abs(item.timestamp - target) < Math.abs(best.timestamp - target) ? item : best
    );
}

function displacementFromBase(snapshot, base, count = 60) {
    let total = 0;
    for (let index = 0; index < count; index += 1) {
        const dx = snapshot.arcs[index].x - base.arcs[index].x;
        const dy = snapshot.arcs[index].y - base.arcs[index].y;
        total += dx * dx + dy * dy;
    }
    return Math.sqrt(total / count);
}

test('base movement and normalized particle population stay comparable at 30, 60, and 144 Hz', () => {
    const input = { bass: 0.1, mid: 0.3, high: 0.2, intensity: 0.25, pulse: 0, beat: false };
    const runs = [30, 60, 144].map(frameRate => ({
        frameRate,
        snapshots: createSimulation({ audioFrame: input, frameRate, duration: 1 }),
    }));
    const finalFrames = runs.map(run => run.snapshots.at(-1));
    assert.ok(finalFrames.every(frame => frame.arcs.length === finalFrames[0].arcs.length));
    assert.ok(finalFrames.every(frame => frame.arcs.length > 60));

    const firstPositions = finalFrames.map(frame => frame.arcs[0]);
    for (const position of firstPositions.slice(1)) {
        assert.ok(Math.hypot(position.x - firstPositions[0].x, position.y - firstPositions[0].y) < 0.02);
    }
});

test('low continuous intensity has no dead zone and bass, mid, and high affect distinct visuals', () => {
    const quiet = createSimulation({
        audioFrame: { bass: 0, mid: 0, high: 0, intensity: 0, pulse: 0, beat: false },
        duration: 0.5,
    }).at(-1);
    const lowIntensity = createSimulation({
        audioFrame: { bass: 0, mid: 0, high: 0, intensity: 0.2, pulse: 0, beat: false },
        duration: 0.5,
    }).at(-1);
    assert.ok(lowIntensity.arcs.length > quiet.arcs.length);

    const noBass = createSimulation({
        audioFrame: { bass: 0, mid: 0.2, high: 0.1, intensity: 0.1, pulse: 0, beat: false },
    })[0];
    const strongBass = createSimulation({
        audioFrame: { bass: 1, mid: 0.2, high: 0.1, intensity: 0.1, pulse: 0, beat: false },
    })[0];
    assert.ok(strongBass.strokes > noBass.strokes, 'bass should expand connected clusters');

    const noMid = createSimulation({
        audioFrame: { bass: 0, mid: 0, high: 0.1, intensity: 0.1, pulse: 0, beat: false },
        duration: 0.5,
    }).at(-1);
    const strongMid = createSimulation({
        audioFrame: { bass: 0, mid: 0.8, high: 0.1, intensity: 0.1, pulse: 0, beat: false },
        duration: 0.5,
    }).at(-1);
    assert.ok(strongMid.arcs.length > noMid.arcs.length, 'mid should affect particle density');
    assert.notEqual(strongMid.arcs[0].x, noMid.arcs[0].x, 'mid should affect particle motion');

    const dimHigh = createSimulation({
        audioFrame: { bass: 0, mid: 0, high: 0, intensity: 0.1, pulse: 0, beat: false },
    })[0];
    const brightHigh = createSimulation({
        audioFrame: { bass: 0, mid: 0, high: 1, intensity: 0.1, pulse: 0, beat: false },
    })[0];
    assert.notEqual(brightHigh.fills[0], dimHigh.fills[0], 'high should brighten particle color and alpha');
});

test('real pulse magnitude drives comparable spring response without a delayed echo', () => {
    const responseAtRate = (frameRate, pulse, seconds) => {
        const snapshots = createSimulation({
            frameRate,
            duration: seconds,
            audioFrame: (_timestamp, frameIndex) => ({
                bass: 0.2,
                mid: 0.1,
                high: 0,
                intensity: 0.1,
                pulse: frameIndex === 1
                    ? pulse
                    : pulse * Math.exp(-Math.max(0, frameIndex / frameRate - 1 / frameRate) / 0.22),
                beat: frameIndex === 1,
            }),
        });
        return {
            snapshots,
            response: seconds => displacementFromBase(
                findNearestSnapshot(snapshots, seconds),
                snapshots[0]
            ),
        };
    };

    const lowPulse = [30, 60, 144].map(rate => responseAtRate(rate, 0.25, 0.1));
    const highPulse = [30, 60, 144].map(rate => responseAtRate(rate, 0.75, 0.1));
    assert.ok(lowPulse.every(run => run.response(0.1) > 0));
    assert.ok(highPulse.every((run, index) => run.response(0.1) > lowPulse[index].response(0.1)));

    const lowResponses = lowPulse.map(run => run.response(0.1));
    const highResponses = highPulse.map(run => run.response(0.1));
    assert.ok(Math.max(...highResponses) / Math.min(...highResponses) < 1.12);
    assert.ok(Math.max(...lowResponses) / Math.min(...lowResponses) < 1.12);

    const singleOnset = createSimulation({
        frameRate: 60,
        duration: 0.24,
        audioFrame: (_timestamp, frameIndex) => ({
            bass: 0.2,
            mid: 0,
            high: 0,
            intensity: 0,
            pulse: frameIndex === 1 ? 0.75 : 0,
            beat: frameIndex === 1,
        }),
    });
    const noOnset = createSimulation({
        frameRate: 60,
        duration: 0.24,
        audioFrame: { bass: 0.2, mid: 0, high: 0, intensity: 0, pulse: 0, beat: false },
    });
    const isolatedResponse = seconds => {
        const onsetFrame = findNearestSnapshot(singleOnset, seconds);
        const baselineFrame = findNearestSnapshot(noOnset, seconds);
        return displacementFromBase(onsetFrame, baselineFrame);
    };
    const earlyResponse = isolatedResponse(0.08);
    const lateResponse = isolatedResponse(0.2);
    assert.ok(lateResponse < earlyResponse,
        `single spring kick should settle (${earlyResponse} -> ${lateResponse})`);
});
