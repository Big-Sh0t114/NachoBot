const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '..', 'static', 'js', 'doodle-background.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function createFixture({ startup = 'visible', readyState = 'complete' } = {}) {
    const metrics = { styleWrites: 0, transformWrites: 0, opacityWrites: 0 };
    const viewport = { left: 280, top: 0, width: 1440, height: 900 };
    const rafCallbacks = new Map();
    const idleCallbacks = new Map();
    const timers = new Map();
    const windowListeners = new Map();
    const documentListeners = new Map();
    const mutationObservers = [];
    const resizeObservers = [];
    const fetchRequests = [];
    const reduceMotionListeners = [];
    const lavaInstances = [];
    const lavaMetrics = { createCalls: 0 };
    let nextRafId = 1;
    let nextIdleId = 1;
    let nextTimerId = 1;
    let randomSeed = 123456789;

    const seededMath = Object.create(Math);
    seededMath.random = () => {
        randomSeed = (1664525 * randomSeed + 1013904223) >>> 0;
        return randomSeed / 0x100000000;
    };

    class MockElement {
        constructor(tagName) {
            this.tagName = tagName.toUpperCase();
            this.id = '';
            this.className = '';
            this.dataset = {};
            this.attributes = {};
            this.children = [];
            this.parentElement = null;
            this.hidden = false;
            this.replaceChildrenCount = 0;
            this.style = new Proxy({}, {
                set: (target, key, value) => {
                    target[key] = value;
                    if (key === 'transform' || key === 'opacity') metrics.styleWrites += 1;
                    if (key === 'transform') metrics.transformWrites += 1;
                    if (key === 'opacity') metrics.opacityWrites += 1;
                    return true;
                },
            });
        }

        get isConnected() {
            let current = this;
            while (current) {
                if (current === document.body) return true;
                current = current.parentElement;
            }
            return false;
        }

        setAttribute(name, value) {
            this.attributes[name] = String(value);
        }

        appendChild(child) {
            if (child.parentElement) child.parentElement.removeChild(child);
            this.children.push(child);
            child.parentElement = this;
            return child;
        }

        insertBefore(child, before) {
            if (child.parentElement) child.parentElement.removeChild(child);
            const index = this.children.indexOf(before);
            if (index < 0) this.children.push(child);
            else this.children.splice(index, 0, child);
            child.parentElement = this;
            return child;
        }

        removeChild(child) {
            const index = this.children.indexOf(child);
            if (index >= 0) this.children.splice(index, 1);
            child.parentElement = null;
            return child;
        }

        replaceChildren(...children) {
            for (const child of this.children) child.parentElement = null;
            this.children = [];
            this.replaceChildrenCount += 1;
            for (const child of children) this.appendChild(child);
        }

        remove() {
            if (this.parentElement) this.parentElement.removeChild(this);
        }

        getClientRects() {
            if (this.id !== 'startup-screen') return [{}];
            return this.isConnected && !this.hidden && this.style.display !== 'none'
                && this.style.visibility !== 'hidden' ? [{}] : [];
        }

        getBoundingClientRect() {
            if (this.id === 'doodle-background-layer') return { ...viewport, right: viewport.left + viewport.width, bottom: viewport.top + viewport.height };
            return { left: 0, top: 0, width: viewport.width, height: viewport.height };
        }
    }

    const body = new MockElement('body');
    const documentElement = new MockElement('html');
    documentElement.appendChild(body);
    const screen = startup === null ? null : new MockElement('div');
    if (screen) {
        screen.id = 'startup-screen';
        screen.style.display = startup === 'visible' || startup === 'fade' ? 'flex' : 'none';
        screen.style.opacity = startup === 'fade' ? '0' : '1';
        body.appendChild(screen);
    }

    const document = {
        body,
        documentElement,
        readyState,
        hidden: false,
        createElement: (tagName) => new MockElement(tagName),
        getElementById(id) {
            const visit = (element) => {
                if (element.id === id) return element;
                for (const child of element.children) {
                    const result = visit(child);
                    if (result) return result;
                }
                return null;
            };
            return visit(body);
        },
        addEventListener(type, callback, options = {}) {
            addListener(documentListeners, type, callback, options);
        },
        removeEventListener(type, callback) {
            removeListener(documentListeners, type, callback);
        },
    };

    function addListener(collection, type, callback, options) {
        const listeners = collection.get(type) || [];
        listeners.push({ callback, once: Boolean(options && options.once) });
        collection.set(type, listeners);
    }

    function removeListener(collection, type, callback) {
        const listeners = collection.get(type) || [];
        collection.set(type, listeners.filter((item) => item.callback !== callback));
    }

    function dispatch(collection, type, event = {}) {
        const listeners = collection.get(type) || [];
        for (const item of [...listeners]) {
            item.callback({ type, ...event });
            if (item.once) removeListener(collection, type, item.callback);
        }
    }

    class MockMutationObserver {
        constructor(callback) {
            this.callback = callback;
            this.connected = false;
            mutationObservers.push(this);
        }

        observe() { this.connected = true; }
        disconnect() { this.connected = false; }
        notify() {
            if (this.connected) this.callback([{ type: 'attributes', target: screen }]);
        }
    }

    class MockResizeObserver {
        constructor(callback) {
            this.callback = callback;
            this.connected = false;
            resizeObservers.push(this);
        }

        observe() { this.connected = true; }
        disconnect() { this.connected = false; }
        resize(width, height) {
            viewport.width = width;
            viewport.height = height;
            this.callback([{ contentRect: { width, height } }]);
        }
    }

    const reduceMotion = {
        matches: false,
        addEventListener(type, callback) {
            if (type === 'change') reduceMotionListeners.push(callback);
        },
        removeEventListener(type, callback) {
            if (type === 'change') {
                const index = reduceMotionListeners.indexOf(callback);
                if (index >= 0) reduceMotionListeners.splice(index, 1);
            }
        },
        addListener(callback) { reduceMotionListeners.push(callback); },
        removeListener(callback) {
            const index = reduceMotionListeners.indexOf(callback);
            if (index >= 0) reduceMotionListeners.splice(index, 1);
        },
    };
    const window = {
        innerWidth: viewport.width,
        innerHeight: viewport.height,
        ResizeObserver: MockResizeObserver,
        addEventListener(type, callback, options = {}) {
            addListener(windowListeners, type, callback, options);
        },
        removeEventListener(type, callback) {
            removeListener(windowListeners, type, callback);
        },
        matchMedia: () => reduceMotion,
        LavaBackground: {
            create() {
                lavaMetrics.createCalls += 1;
                const controller = {
                    resizeCalls: [],
                    starts: 0,
                    stops: [],
                    disposes: 0,
                    running: false,
                    resize(...args) { this.resizeCalls.push(args); },
                    start() {
                        if (!this.running) this.starts += 1;
                        this.running = true;
                    },
                    stop(makeStatic = false) {
                        this.stops.push(Boolean(makeStatic));
                        this.running = false;
                    },
                    dispose() {
                        this.disposes += 1;
                        this.running = false;
                    },
                };
                lavaInstances.push(controller);
                return controller;
            },
        },
        getComputedStyle(element) {
            return {
                display: element.style.display || 'block',
                visibility: element.style.visibility || 'visible',
            };
        },
        requestAnimationFrame(callback) {
            const id = nextRafId++;
            rafCallbacks.set(id, callback);
            return id;
        },
        cancelAnimationFrame(id) { rafCallbacks.delete(id); },
        requestIdleCallback(callback) {
            const id = nextIdleId++;
            idleCallbacks.set(id, callback);
            return id;
        },
        cancelIdleCallback(id) { idleCallbacks.delete(id); },
        setTimeout(callback) {
            const id = nextTimerId++;
            timers.set(id, callback);
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
    };

    function fetch(url, options) {
        return new Promise((resolve, reject) => fetchRequests.push({ url, options, resolve, reject }));
    }

    const sandbox = vm.createContext({
        AbortController,
        Math: seededMath,
        MutationObserver: MockMutationObserver,
        ResizeObserver: MockResizeObserver,
        document,
        fetch,
        window,
        console: { warn() {} },
    });
    vm.runInContext(source, sandbox, { filename: sourcePath });

    return {
        document,
        fetchRequests,
        idleCallbacks,
        lavaInstances,
        lavaMetrics,
        metrics,
        mutationObservers,
        rafCallbacks,
        resizeObservers,
        screen,
        timers,
        viewport,
        window,
        hideScreen() {
            screen.style.display = 'none';
            for (const observer of mutationObservers) observer.notify();
        },
        hideScreenAttribute() {
            screen.hidden = true;
            for (const observer of mutationObservers) observer.notify();
        },
        hideScreenVisibility() {
            screen.style.visibility = 'hidden';
            for (const observer of mutationObservers) observer.notify();
        },
        removeScreen() {
            screen.remove();
            for (const observer of mutationObservers) observer.notify();
        },
        notifyScreenMutation() {
            for (const observer of mutationObservers) observer.notify();
        },
        fireIdle() {
            const entry = idleCallbacks.entries().next().value;
            assert.ok(entry, 'a bounded idle callback should be pending');
            idleCallbacks.delete(entry[0]);
            entry[1]();
        },
        resolveFetch(index = 0, assets = null) {
            fetchRequests[index].resolve({
                ok: true,
                json: async () => ({
                    assets: assets || Array.from({ length: 14 }, (_, item) => ({
                        filename: `doodle-${item}.svg`,
                        width: 100,
                        height: 100,
                    })),
                }),
            });
        },
        flushTimers() {
            const entries = [...timers.entries()];
            timers.clear();
            for (const [, callback] of entries) callback();
        },
        fireFrames(frameRate, durationSeconds = 1, onFrame = null) {
            const frameCount = Math.round(frameRate * durationSeconds);
            for (let index = 0; index <= frameCount; index += 1) {
                const entry = rafCallbacks.entries().next().value;
                assert.ok(entry, 'the animation loop should have exactly one pending frame');
                rafCallbacks.delete(entry[0]);
                const timestamp = index * 1000 / frameRate;
                entry[1](timestamp);
                if (onFrame) onFrame(timestamp, index);
            }
        },
        dispatchWindow(type, event) { dispatch(windowListeners, type, event); },
        dispatchDocument(type, event) { dispatch(documentListeners, type, event); },
        setReducedMotion(matches) {
            reduceMotion.matches = matches;
            for (const callback of [...reduceMotionListeners]) callback({ matches });
        },
        doodleLayer() { return document.getElementById('doodle-background-layer'); },
        settlePromises: async () => {
            await Promise.resolve();
            await Promise.resolve();
            await Promise.resolve();
        },
    };
}

function parseRenderedPose(element) {
    const match = /^translate3d\((-?[\d.]+)px, (-?[\d.]+)px, 0\)(?: rotate\((-?[\d.]+)deg\))?$/.exec(element.style.transform || '');
    assert.ok(match, `expected a rendered doodle transform, got ${element.style.transform}`);
    const pose = {
        x: Number(match[1]),
        y: Number(match[2]),
        angle: Number(match[3] || 0) * (Math.PI / 180),
    };
    assert.ok(Number.isFinite(pose.x) && Number.isFinite(pose.y) && Number.isFinite(pose.angle));
    return pose;
}

function renderedSatPenetration(firstElement, secondElement) {
    const firstMotion = firstElement.motion;
    const secondMotion = secondElement.motion;
    const firstPose = parseRenderedPose(firstElement);
    const secondPose = parseRenderedPose(secondElement);
    const axes = [firstPose.angle, firstPose.angle + Math.PI / 2, secondPose.angle, secondPose.angle + Math.PI / 2];
    const firstCenterX = firstPose.x + firstMotion.width / 2;
    const firstCenterY = firstPose.y + firstMotion.height / 2;
    const secondCenterX = secondPose.x + secondMotion.width / 2;
    const secondCenterY = secondPose.y + secondMotion.height / 2;
    const deltaX = secondCenterX - firstCenterX;
    const deltaY = secondCenterY - firstCenterY;
    let minimumOverlap = Infinity;

    for (const angle of axes) {
        const axisX = Math.cos(angle);
        const axisY = Math.sin(angle);
        const firstCosine = Math.cos(firstPose.angle);
        const firstSine = Math.sin(firstPose.angle);
        const secondCosine = Math.cos(secondPose.angle);
        const secondSine = Math.sin(secondPose.angle);
        const firstRadius = (firstMotion.width / 2) * Math.abs(firstCosine * axisX + firstSine * axisY)
            + (firstMotion.height / 2) * Math.abs(-firstSine * axisX + firstCosine * axisY);
        const secondRadius = (secondMotion.width / 2) * Math.abs(secondCosine * axisX + secondSine * axisY)
            + (secondMotion.height / 2) * Math.abs(-secondSine * axisX + secondCosine * axisY);
        const overlap = firstRadius + secondRadius - Math.abs(deltaX * axisX + deltaY * axisY);
        if (overlap <= 0) return 0;
        minimumOverlap = Math.min(minimumOverlap, overlap);
    }
    return minimumOverlap;
}

function assertRenderedDoodlesDoNotOverlap(layer, tolerance = 0.25) {
    let maximumPenetration = 0;
    for (let firstIndex = 0; firstIndex < layer.children.length - 1; firstIndex += 1) {
        for (let secondIndex = firstIndex + 1; secondIndex < layer.children.length; secondIndex += 1) {
            maximumPenetration = Math.max(
                maximumPenetration,
                renderedSatPenetration(layer.children[firstIndex], layer.children[secondIndex]),
            );
        }
    }
    assert.ok(maximumPenetration <= tolerance, `rendered SAT penetration was ${maximumPenetration.toFixed(3)}px`);
    return maximumPenetration;
}

async function mountVisibleDoodles(fixture) {
    fixture.fireIdle();
    assert.equal(fixture.fetchRequests.length, 1, 'the manifest should start only after the idle gate');
    fixture.resolveFetch();
    await fixture.settlePromises();
    return fixture.doodleLayer();
}

test('visible startup, including opacity-zero fade, blocks all doodle work until hidden', async () => {
    const fixture = createFixture({ startup: 'fade' });
    assert.equal(fixture.fetchRequests.length, 0);
    assert.equal(fixture.doodleLayer(), null);
    assert.equal(fixture.rafCallbacks.size, 0);
    assert.equal(fixture.metrics.transformWrites, 0, 'covered content should not render doodles behind the startup screen');
    assert.equal(fixture.lavaMetrics.createCalls, 0, 'the lava canvas controller must not be created behind startup');

    fixture.notifyScreenMutation();
    await fixture.settlePromises();
    assert.equal(fixture.fetchRequests.length, 0, 'opacity zero still covers the page');
    assert.equal(fixture.doodleLayer(), null);
    assert.equal(fixture.metrics.transformWrites, 0);
    assert.equal(fixture.lavaMetrics.createCalls, 0, 'opacity zero still blocks lava canvas allocation');

    fixture.hideScreen();
    assert.equal(fixture.fetchRequests.length, 0, 'mounting is deferred to the bounded idle callback');
    const layer = await mountVisibleDoodles(fixture);
    assert.ok(layer);
    assert.equal(fixture.lavaMetrics.createCalls, 1, 'the controller is created only after the startup gate opens');
    assert.equal(layer.children.length, 14);
    assert.equal(fixture.fetchRequests.length, 1);
    assert.equal(fixture.rafCallbacks.size, 1);
    assert.equal(fixture.metrics.transformWrites, 14, 'the first visible pose is written once per doodle');

    fixture.dispatchWindow('pageshow');
    assert.equal(fixture.fetchRequests.length, 1, 'pageshow must not duplicate an active mount');
    assert.equal(fixture.rafCallbacks.size, 1);
});

test('lava lifecycle follows debounced doodle bounds, visibility, reduced motion, and pagehide', async () => {
    const { fixture, resizeObserver } = await createRunningFixture();
    const [lava] = fixture.lavaInstances;
    assert.ok(lava);
    assert.equal(lava.resizeCalls.length, 1);
    assert.deepEqual(lava.resizeCalls[0], [1440, 900, 280, 0]);
    assert.equal(lava.starts, 1);

    resizeObserver.resize(1024, 720);
    assert.equal(lava.resizeCalls.length, 1, 'lava uses the same 100ms resize debounce');
    fixture.flushTimers();
    assert.deepEqual(lava.resizeCalls.at(-1), [1024, 720, 280, 0]);

    fixture.setReducedMotion(true);
    assert.equal(lava.stops.at(-1), true, 'reduced motion asks lava for one static contour frame');
    assert.equal(fixture.rafCallbacks.size, 0, 'reduced motion leaves no doodle animation frame pending');

    fixture.document.hidden = true;
    fixture.dispatchDocument('visibilitychange');
    assert.equal(lava.stops.at(-1), true);
    fixture.setReducedMotion(false);
    assert.equal(lava.stops.at(-1), false, 'a hidden page stops both background loops without rendering');
    assert.equal(fixture.rafCallbacks.size, 0);

    fixture.document.hidden = false;
    fixture.dispatchDocument('visibilitychange');
    assert.equal(lava.starts, 2, 'visible pages restart the lava controller once');

    fixture.dispatchWindow('pagehide');
    assert.equal(lava.disposes, 1, 'pagehide disposes the canvas and its controller');
});

test('initially hidden, absent, and removed startup screens release the gate once', async (t) => {
    for (const scenario of [
        { name: 'initially hidden', startup: 'hidden', remove: false },
        { name: 'hidden attribute', startup: 'visible', hide: 'attribute' },
        { name: 'visibility hidden', startup: 'visible', hide: 'visibility' },
        { name: 'removed from document', startup: 'visible', remove: true },
        { name: 'no startup screen', startup: null, remove: false },
    ]) {
        await t.test(scenario.name, async () => {
            const fixture = createFixture({ startup: scenario.startup });
            if (scenario.remove) fixture.removeScreen();
            if (scenario.hide === 'attribute') fixture.hideScreenAttribute();
            if (scenario.hide === 'visibility') fixture.hideScreenVisibility();
            const layer = await mountVisibleDoodles(fixture);
            assert.ok(layer);
            assert.equal(fixture.fetchRequests.length, 1);
            fixture.dispatchWindow('pageshow');
            assert.equal(fixture.fetchRequests.length, 1);
        });
    }
});

test('the startup error/fade hide state releases the observer and permits one mount', async () => {
    const fixture = createFixture({ startup: 'visible' });
    fixture.screen.style.opacity = '0';
    fixture.notifyScreenMutation();
    assert.equal(fixture.fetchRequests.length, 0);

    // ui.js hides the screen after its fade on video error, autoplay failure, or normal completion.
    fixture.hideScreen();
    await mountVisibleDoodles(fixture);
    assert.equal(fixture.fetchRequests.length, 1);
    assert.equal(fixture.mutationObservers.filter((observer) => observer.connected).length, 0);
});

test('pagehide cancels gate, idle, and in-flight fetch work; pageshow re-arms from current state', async () => {
    const fixture = createFixture({ startup: 'visible' });
    fixture.dispatchWindow('pagehide');
    assert.equal(fixture.mutationObservers.filter((observer) => observer.connected).length, 0);
    assert.equal(fixture.idleCallbacks.size, 0);

    fixture.dispatchWindow('pageshow');
    fixture.hideScreen();
    assert.equal(fixture.idleCallbacks.size, 1);
    fixture.dispatchWindow('pagehide');
    assert.equal(fixture.idleCallbacks.size, 0, 'pagehide should cancel a scheduled mount');
    assert.equal(fixture.fetchRequests.length, 0);

    fixture.dispatchWindow('pageshow');
    fixture.dispatchWindow('pageshow');
    fixture.fireIdle();
    assert.equal(fixture.fetchRequests.length, 1);
    const firstSignal = fixture.fetchRequests[0].options.signal;
    fixture.dispatchWindow('pagehide');
    assert.equal(firstSignal.aborted, true);
    assert.equal(fixture.doodleLayer(), null);
    fixture.resolveFetch(0);
    await fixture.settlePromises();
    assert.equal(fixture.rafCallbacks.size, 0, 'stale fetch completion must not recreate the layer');

    fixture.dispatchWindow('pageshow');
    fixture.fireIdle();
    assert.equal(fixture.fetchRequests.length, 2);
    fixture.resolveFetch(1);
    await fixture.settlePromises();
    assert.equal(fixture.doodleLayer().children.length, 14);
    assert.equal(fixture.rafCallbacks.size, 1);
});

async function createRunningFixture(assets = null) {
    const fixture = createFixture({ startup: 'hidden' });
    fixture.fireIdle();
    const layer = fixture.doodleLayer();
    const resizeObserver = fixture.resizeObservers[0];
    resizeObserver.resize(fixture.viewport.width, fixture.viewport.height);
    assert.equal(fixture.rafCallbacks.size, 0, 'bounds arriving before assets must not start an empty loop');
    fixture.resolveFetch(0, assets);
    await fixture.settlePromises();
    assert.ok(layer);
    assert.equal(layer.children.length, 14, 'manifest completion must render the already-observed size');
    assert.equal(fixture.rafCallbacks.size, 1, 'manifest completion must start the animation loop');
    return { fixture, layer, resizeObserver };
}

test('initial active render writes collision-free rotated poses at desktop and mobile scales', async () => {
    const { fixture, layer, resizeObserver } = await createRunningFixture();
    assert.equal(layer.children.length, 14);
    for (const element of layer.children) {
        assert.equal(element.motion.width, 42, 'desktop scale remains 0.42 for a 100px native width');
        assert.equal(element.motion.height, 42, 'desktop scale remains 0.42 for a 100px native height');
        parseRenderedPose(element);
    }
    assertRenderedDoodlesDoNotOverlap(layer);

    resizeObserver.resize(700, 900);
    fixture.flushTimers();
    assert.equal(layer.children.length, 6);
    for (const element of layer.children) {
        assert.equal(element.motion.width, 30, 'mobile scale remains 0.30 for a 100px native width');
        assert.equal(element.motion.height, 30, 'mobile scale remains 0.30 for a 100px native height');
        parseRenderedPose(element);
    }
    assertRenderedDoodlesDoNotOverlap(layer);
});

test('switching active motion to reduced motion keeps the static layout collision-free', async () => {
    const { fixture, layer } = await createRunningFixture();
    for (let index = 0; index < layer.children.length; index += 1) {
        neutralizeMotion(layer.children[index].motion, { x: 950 + index * 55, y: 100 });
    }
    const first = layer.children[0].motion;
    const second = layer.children[1].motion;
    neutralizeMotion(first, { x: 300, y: 400 });
    neutralizeMotion(second, { x: 310, y: 460 });
    second.baseY = 400;

    fixture.fireFrames(60, 0.1);
    assertRenderedDoodlesDoNotOverlap(layer);
    fixture.setReducedMotion(true);

    assert.equal(layer.dataset.paused, 'true');
    assert.equal(fixture.rafCallbacks.size, 0, 'reduced motion must stay static without a resumed physics loop');
    for (const element of layer.children) {
        const pose = parseRenderedPose(element);
        assert.equal(pose.angle, 0, 'static rendering omits rotation');
        assert.ok(Math.abs(pose.x - element.motion.x) < 0.02);
        assert.ok(Math.abs(pose.y - element.motion.y) < 0.02);
        assert.equal(element.motion.baseY, element.motion.y, 'static collision corrections become the stable base position');
    }
    assertRenderedDoodlesDoNotOverlap(layer);
});

function neutralizeMotion(doodle, { x, y, speed = 0, rotation = 0 } = {}) {
    doodle.x = x;
    doodle.y = y;
    doodle.baseY = y;
    doodle.speed = speed;
    doodle.phase = 0;
    doodle.sway = 0;
    doodle.rotation = rotation;
    doodle.rotationPhase = 0;
    doodle.rotationRate = 0;
    doodle.avoidX = 0;
    doodle.avoidY = 0;
    doodle.velocityX = 0;
    doodle.velocityY = 0;
}

test('steady motion caps writes at 30 Hz with frame-rate-independent distance', async () => {
    const runs = [];
    for (const frameRate of [60, 120, 144]) {
        const { fixture, layer } = await createRunningFixture();
        const initialY = layer.children[0].motion.y;
        const startWrites = fixture.metrics.transformWrites;
        fixture.fireFrames(frameRate, 1);
        const updates = (fixture.metrics.transformWrites - startWrites) / layer.children.length;
        const displacement = layer.children[0].motion.y - initialY;
        runs.push({ frameRate, updates, displacement });
    }

    for (const run of runs) assert.ok(run.updates <= 30, `${run.frameRate} Hz produced ${run.updates} steady updates`);
    assert.ok(runs[0].updates >= 28, '60 Hz input should produce about 30 updates per second');
    const displacements = runs.map((run) => run.displacement);
    assert.ok(displacements.every((value) => value <= -2.8 && value >= -12.1));
    assert.ok(Math.max(...displacements) - Math.min(...displacements) < 0.2, 'distance should not depend on RAF frequency');
});

test('a faster upward doodle cannot pass a slower one at 60, 120, or 144 Hz', async () => {
    for (const frameRate of [60, 120, 144]) {
        const { fixture, layer } = await createRunningFixture();
        for (let index = 0; index < layer.children.length; index += 1) {
            neutralizeMotion(layer.children[index].motion, { x: 900 + index * 55, y: 100 });
        }
        const slowerLeader = layer.children[0].motion;
        const fasterFollower = layer.children[1].motion;
        neutralizeMotion(slowerLeader, { x: 300, y: 410, speed: 3 });
        neutralizeMotion(fasterFollower, { x: 300, y: 460, speed: 12 });

        let previousWrites = fixture.metrics.transformWrites;
        fixture.fireFrames(frameRate, 4, (timestamp) => {
            const writes = fixture.metrics.transformWrites - previousWrites;
            previousWrites = fixture.metrics.transformWrites;
            assert.ok(writes === 0 || writes === layer.children.length,
                `${frameRate} Hz RAF should write once per doodle or not update, got ${writes}`);
            if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
            if (timestamp > 1000) {
                const leaderPose = parseRenderedPose(layer.children[0]);
                const followerPose = parseRenderedPose(layer.children[1]);
                assert.ok(followerPose.y > leaderPose.y, 'the faster follower must remain behind the slower leader');
            }
        });
        assert.ok(fasterFollower.y > slowerLeader.y, 'the corrected base positions must retain their order');
    }
});

test('rotated doodles displaced by mouse avoidance use their rendered poses for hard contacts', async () => {
    const { fixture, layer } = await createRunningFixture();
    for (let index = 0; index < layer.children.length; index += 1) {
        neutralizeMotion(layer.children[index].motion, { x: 900 + index * 55, y: 100 });
    }
    const first = layer.children[0].motion;
    const second = layer.children[1].motion;
    neutralizeMotion(first, { x: 300, y: 400, rotation: 35 });
    neutralizeMotion(second, { x: 310, y: 400, rotation: -30 });
    fixture.dispatchWindow('pointermove', {
        pointerType: 'mouse',
        clientX: fixture.viewport.left + first.x + first.width / 2,
        clientY: fixture.viewport.top + first.y + first.height / 2,
    });

    let previousWrites = fixture.metrics.transformWrites;
    fixture.fireFrames(120, 2, (timestamp) => {
        const writes = fixture.metrics.transformWrites - previousWrites;
        previousWrites = fixture.metrics.transformWrites;
        assert.ok(writes === 0 || writes === layer.children.length,
            `collision substeps must not add DOM writes inside a render update, got ${writes}`);
        if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
        if (timestamp > 500) {
            assert.ok(Math.hypot(first.avoidX, first.avoidY) > 0.1);
            assert.ok(Math.hypot(second.avoidX, second.avoidY) > 0.1);
        }
    });

    for (const element of [layer.children[0], layer.children[1]]) {
        const motion = element.motion;
        const rendered = parseRenderedPose(element);
        assert.ok(Math.abs(rendered.x - (motion.x + motion.avoidX)) < 0.02,
            'collision geometry should match the translated pointer-avoidance pose');
        assert.ok(Math.abs(rendered.y - (motion.y + motion.avoidY)) < 0.02);
    }
    assertRenderedDoodlesDoNotOverlap(layer);
});

test('rotating thin worm and tiny fish retain nonpenetrating rendered SAT bounds', async () => {
    const assets = [
        { filename: 'thin-worm.svg', width: 180, height: 14 },
        { filename: 'tiny-fish.svg', width: 40, height: 20 },
        ...Array.from({ length: 12 }, (_, index) => ({ filename: `spare-${index}.svg`, width: 100, height: 100 })),
    ];
    const { fixture, layer } = await createRunningFixture(assets);
    for (let index = 0; index < layer.children.length; index += 1) {
        neutralizeMotion(layer.children[index].motion, { x: 950 + index * 60, y: 100 });
    }
    const wormElement = layer.children.find((element) => element.children[0].children[0].src.endsWith('thin-worm.svg'));
    const fishElement = layer.children.find((element) => element.children[0].children[0].src.endsWith('tiny-fish.svg'));
    assert.ok(wormElement && fishElement);
    const worm = wormElement.motion;
    const fish = fishElement.motion;
    neutralizeMotion(worm, { x: 300, y: 400 });
    neutralizeMotion(fish, { x: 365, y: 407 });
    assert.equal(worm.width, 75.6, 'the worm keeps its 0.42 scale');
    assert.equal(worm.height, 5.88);
    assert.equal(fish.width, 16.8);
    assert.equal(fish.height, 8.4);
    worm.rotationRate = 1;
    worm.rotationPhase = 0;

    let previousWrites = fixture.metrics.transformWrites;
    fixture.fireFrames(144, 2, (timestamp) => {
        const writes = fixture.metrics.transformWrites - previousWrites;
        previousWrites = fixture.metrics.transformWrites;
        assert.ok(writes === 0 || writes === layer.children.length);
        if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
        if (timestamp > 1000) assert.ok(Math.abs(parseRenderedPose(wormElement).angle) > 0.05);
    });
    assertRenderedDoodlesDoNotOverlap(layer);
    assert.ok(Math.abs(worm.x - 300) > 0.1 || Math.abs(worm.y - 400) > 0.1
        || Math.abs(fish.x - 365) > 0.1 || Math.abs(fish.y - 407) > 0.1,
    'rotation should close the initial gap and trigger an immediate position correction');
});

test('three-way and coincident-center contacts resolve to finite separated positions', async () => {
    const { fixture, layer } = await createRunningFixture();
    for (let index = 0; index < layer.children.length; index += 1) {
        neutralizeMotion(layer.children[index].motion, { x: 950 + index * 55, y: 100 });
    }
    neutralizeMotion(layer.children[0].motion, { x: 300, y: 500 });
    neutralizeMotion(layer.children[1].motion, { x: 330, y: 500 });
    neutralizeMotion(layer.children[2].motion, { x: 360, y: 500 });

    let previousWrites = fixture.metrics.transformWrites;
    fixture.fireFrames(60, 1, () => {
        const writes = fixture.metrics.transformWrites - previousWrites;
        previousWrites = fixture.metrics.transformWrites;
        assert.ok(writes === 0 || writes === layer.children.length);
        if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
    });
    assertRenderedDoodlesDoNotOverlap(layer);

    neutralizeMotion(layer.children[0].motion, { x: 500, y: 500 });
    neutralizeMotion(layer.children[1].motion, { x: 500, y: 500 });
    fixture.fireFrames(60, 1, () => {
        const writes = fixture.metrics.transformWrites - previousWrites;
        previousWrites = fixture.metrics.transformWrites;
        assert.ok(writes === 0 || writes === layer.children.length);
        if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
    });
    assertRenderedDoodlesDoNotOverlap(layer);
    for (const element of layer.children) {
        assert.ok(Number.isFinite(element.motion.x) && Number.isFinite(element.motion.y));
    }
});

test('a wrapped doodle clears a second doodle placed at its new spawn point', async () => {
    const { fixture, layer } = await createRunningFixture();
    for (let index = 0; index < layer.children.length; index += 1) {
        neutralizeMotion(layer.children[index].motion, { x: 950 + index * 55, y: 100 });
    }
    const wrapped = layer.children[0].motion;
    neutralizeMotion(wrapped, { x: 400, y: -61 });
    fixture.fireFrames(60, 0.1);
    assert.ok(wrapped.y > fixture.viewport.height, 'the forced doodle should have wrapped below the viewport');

    const spawnX = wrapped.x;
    const spawnY = wrapped.y;
    neutralizeMotion(layer.children[1].motion, { x: spawnX, y: spawnY });
    let previousWrites = fixture.metrics.transformWrites;
    fixture.fireFrames(60, 0.1, () => {
        const writes = fixture.metrics.transformWrites - previousWrites;
        previousWrites = fixture.metrics.transformWrites;
        assert.ok(writes === 0 || writes === layer.children.length);
        if (writes > 0) assertRenderedDoodlesDoNotOverlap(layer);
    });
    assertRenderedDoodlesDoNotOverlap(layer);
});

test('pointer avoidance and spring return use a 60 Hz cap at 60, 120, and 144 Hz', async () => {
    for (const frameRate of [60, 120, 144]) {
        const { fixture, layer } = await createRunningFixture();
        const first = layer.children[0].motion;
        fixture.dispatchWindow('pointermove', {
            pointerType: 'mouse',
            clientX: fixture.viewport.left + first.x + first.width / 2,
            clientY: fixture.viewport.top + first.y + first.height / 2,
        });
        const startWrites = fixture.metrics.transformWrites;
        fixture.fireFrames(frameRate, 1);
        const updates = (fixture.metrics.transformWrites - startWrites) / layer.children.length;
        assert.ok(updates > 55, `${frameRate} Hz input should retain responsive 60 Hz updates`);
        assert.ok(updates <= 60, `${frameRate} Hz input produced ${updates} responsive updates`);
    }
});

test('resize bursts coalesce to one render and wrapping keeps its random helper in scope', async () => {
    const { fixture, layer, resizeObserver } = await createRunningFixture();
    assert.equal(layer.replaceChildrenCount, 1);

    for (const width of [1450, 1460, 1470, 1480, 1490]) resizeObserver.resize(width, 900);
    assert.equal(layer.replaceChildrenCount, 1, 'resize observations should not redraw during the transition');
    assert.equal(fixture.timers.size, 1, 'the last resize should own one debounce timer');
    fixture.flushTimers();
    assert.equal(layer.replaceChildrenCount, 2, 'the final settled bounds should render exactly once');

    layer.children[0].motion.y = -layer.children[0].motion.height - 19;
    fixture.fireFrames(60, 0.1);
    const wrappedY = Number(layer.children[0].style.transform.match(/,\s*(-?[\d.]+)px/)[1]);
    assert.ok(wrappedY > 0, 'a doodle above the viewport should wrap to the lower edge');
});
