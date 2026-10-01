'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const sourcePath = path.resolve(__dirname, '..', 'static', 'js', 'chat-live2d.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const applications = [];
const models = [];
const resizeHandlers = new Set();
const observers = [];
let modelFactory = null;

class MockApplication {
    constructor(options) {
        this.options = options;
        this.destroyArgs = null;
        this.ticker = { maxFPS: 0 };
        const updateBuffer = (width, height, resolution) => {
            options.view.width = Math.round(width * resolution);
            options.view.height = Math.round(height * resolution);
        };
        updateBuffer(options.width, options.height, options.resolution);
        this.renderer = {
            gl: {},
            resolution: options.resolution,
            size: { width: options.width, height: options.height },
            resizeCalls: 0,
            resize: (width, height) => {
                this.renderer.size = { width, height };
                this.renderer.resizeCalls += 1;
                updateBuffer(width, height, this.renderer.resolution);
            },
            render: () => { this.renderer.rendered = true; },
        };
        this.stage = {
            children: [],
            addChild: child => { this.stage.children.push(child); child.parent = this.stage; },
            removeChild: child => {
                this.stage.children = this.stage.children.filter(item => item !== child);
                child.parent = null;
            },
        };
        this.destroy = (removeView, stageOptions) => { this.destroyArgs = { removeView, stageOptions }; };
        applications.push(this);
    }
}

function makeModel() {
    const values = [];
    const events = new Map();
    const model = {
        width: 500,
        height: 700,
        x: 0,
        y: 0,
        parent: null,
        scale: { value: 1, set(value) { this.value = value; } },
        anchor: { set(x, y) { this.value = [x, y]; } },
        internalModel: {
            coreModel: {
                getParameterCount: () => 2,
                getModel: () => ({ parameters: { ids: ['ParamMouthOpenY', 'ParamAngleX'] } }),
                setParameterValueById(id, value) { values.push({ id, value }); },
            },
            motionManager: {
                definitions: { Nod: [{}], Idle: [{}] },
                expressionManager: { definitions: [{ Name: 'shy' }, { Name: 'angry' }] },
            },
            on(name, handler) { events.set(name, handler); },
            off(name, handler) { if (events.get(name) === handler) events.delete(name); },
            emit(name) { if (events.has(name)) events.get(name)(); },
        },
        expressionCalls: [],
        motionCalls: [],
        destroyCalls: 0,
        expression(name) { this.expressionCalls.push(name); return Promise.resolve(true); },
        motion(group) { this.motionCalls.push(group); return Promise.resolve(true); },
        destroy() { this.destroyCalls += 1; },
    };
    model.parameterWrites = values;
    model.events = events;
    models.push(model);
    return model;
}

const root = {
    location: { href: 'http://127.0.0.1:18799/' },
    devicePixelRatio: 1,
    Live2DCubismCore: { Version: 'mock' },
    PIXI: {
        Application: MockApplication,
        live2d: {
            Live2DModel: { from(url, options) { return modelFactory ? modelFactory(url, options) : Promise.resolve(makeModel()); } },
            MotionPriority: { NORMAL: 1 },
            MotionPreloadStrategy: { IDLE: 'IDLE' },
        },
    },
    setTimeout,
    clearTimeout,
    addEventListener(name, handler) { if (name === 'resize') resizeHandlers.add(handler); },
    removeEventListener(name, handler) { if (name === 'resize') resizeHandlers.delete(handler); },
    ResizeObserver: class {
        constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
        observe(target) { this.target = target; }
        disconnect() { this.disconnected = true; }
    },
};
const context = { window: root, URL, Promise, Set, Map, Number, Math, Object, Array, String, Error, console };
vm.runInNewContext(source, context, { filename: sourcePath });

function makeCanvas() {
    return {
        width: 640,
        height: 480,
        clientWidth: 640,
        clientHeight: 480,
        parentElement: { clientWidth: 640, clientHeight: 480 },
        getContext() { return {}; },
    };
}

function entry(overrides = {}) {
    return {
        id: 'l2d-test',
        name: 'test model',
        model_url: '/api/chat/live2d/assets/l2d-test/avatar.model3.json',
        lip_sync_parameters: ['ParamMouthOpenY'],
        expressions: { shy: 'shy', angry: 'angry' },
        actions: { NOD: 'Nod' },
        ...overrides,
    };
}

function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

async function testLoadCapabilitiesControlsMouthAndCleanup() {
    const canvas = makeCanvas();
    const failures = [];
    const manager = root.ChatLive2D.create({ canvas, onFailure: error => failures.push(error) });
    const capabilities = await manager.load(entry());
    const model = models.at(-1);
    assert(capabilities.parameters.includes('ParamMouthOpenY'), 'runtime parameter IDs should be enumerated');
    assert.deepStrictEqual(plain(capabilities.lip_sync_parameters), ['ParamMouthOpenY']);
    assert.deepStrictEqual(plain(capabilities.expressions), { shy: 'shy', angry: 'angry' });
    assert.deepStrictEqual(plain(capabilities.actions), { NOD: 'Nod' });
    assert(applications.at(-1).renderer.rendered, 'load resolves only after a render succeeds');
    assert.strictEqual(applications.at(-1).options.view, canvas);

    manager.setMouth(4);
    assert.strictEqual(model.parameterWrites.at(-1).value, 1, 'mouth level is clamped to one');
    model.internalModel.emit('beforeModelUpdate');
    assert.strictEqual(model.parameterWrites.at(-1).value, 1, 'mouth level is reapplied after model update');
    manager.setMouth(-2);
    assert.strictEqual(model.parameterWrites.at(-1).value, 0, 'mouth level is clamped to zero');

    const applied = await manager.apply([
        { type: 'expression', name: 'shy' },
        { type: 'motion', group: 'Nod' },
        { type: 'motion', group: 'not-a-real-group' },
    ]);
    assert.deepStrictEqual(plain(applied), [
        { type: 'expression', name: 'shy' },
        { type: 'motion', group: 'Nod' },
    ]);
    assert.deepStrictEqual(model.expressionCalls, ['shy']);
    assert.deepStrictEqual(model.motionCalls, ['Nod']);

    const resize = [...resizeHandlers][0];
    resize();
    const firstScale = model.scale.value;
    resize();
    assert.strictEqual(model.scale.value, firstScale, 'resize preserves natural model dimensions');
    assert.strictEqual(observers.at(-1).disconnected, false);

    manager.destroy();
    assert.strictEqual(model.parameterWrites.at(-1).value, 0, 'cleanup closes the mouth');
    assert.strictEqual(model.destroyCalls, 1);
    assert.strictEqual(applications.at(-1).destroyArgs.removeView, false, 'caller canvas remains attached');
    assert.strictEqual(resizeHandlers.size, 0);
    assert.strictEqual(observers.at(-1).disconnected, true);
    assert.deepStrictEqual(failures, []);
}

async function testVoiceOnlyModelDisablesPointerAutoInteract() {
    let receivedOptions = null;
    const previousFactory = modelFactory;
    modelFactory = (url, options) => {
        receivedOptions = options;
        return Promise.resolve(makeModel());
    };
    const manager = root.ChatLive2D.create({ canvas: makeCanvas() });
    try {
        await manager.load(entry());
        assert(receivedOptions, 'the Live2D model factory receives its options');
        assert.strictEqual(receivedOptions.autoInteract, false,
            'the voice-only stage must not map pointer movement into Live2D focus parameters');
    } finally {
        manager.destroy();
        modelFactory = previousFactory;
    }
}

async function testInstancesAreIndependent() {
    const first = root.ChatLive2D.create({ canvas: makeCanvas() });
    const second = root.ChatLive2D.create({ canvas: makeCanvas() });
    await first.load(entry({ id: 'first' }));
    await second.load(entry({ id: 'second' }));
    const [firstModel, secondModel] = models.slice(-2);
    first.setMouth(0.25);
    second.setMouth(0.75);
    assert.strictEqual(firstModel.parameterWrites.at(-1).value, 0.25);
    assert.strictEqual(secondModel.parameterWrites.at(-1).value, 0.75);
    first.destroy();
    assert.strictEqual(secondModel.destroyCalls, 0, 'destroying one instance leaves the other model alive');
    second.destroy();
}

async function testModelSwitchReusesCanvasRenderer() {
    const canvas = makeCanvas();
    const manager = root.ChatLive2D.create({ canvas });
    await manager.load(entry({ id: 'first-model' }));
    const app = applications.at(-1);
    const priorModel = models.at(-1);
    const capabilities = await manager.load(entry({ id: 'second-model' }));
    assert.strictEqual(applications.at(-1), app, 'a model switch reuses the live canvas renderer');
    assert.strictEqual(priorModel.destroyCalls, 1, 'the replaced model is released');
    assert.strictEqual(app.destroyArgs, null, 'the canvas renderer stays live until manager.destroy');
    assert(capabilities.parameters.includes('ParamMouthOpenY'));
    manager.destroy();
    assert(app.destroyArgs, 'final manager cleanup releases its renderer');
}

async function testResizeUsesParentLayoutWhenPixiCanvasDimensionsAreStale() {
    const canvas = makeCanvas();
    canvas.parentElement.clientWidth = 390;
    canvas.parentElement.clientHeight = 644;
    canvas.clientWidth = 1482;
    canvas.clientHeight = 755;
    canvas.width = 2223;
    canvas.height = 1133;
    const manager = root.ChatLive2D.create({ canvas });
    await manager.load(entry());

    const app = applications.at(-1);
    const model = models.at(-1);
    assert.deepStrictEqual(app.renderer.size, { width: 390, height: 644 }, 'initial renderer uses its stage layout box');
    assert.strictEqual(model.x, 195);
    assert(model.y + 700 * 0.18 * model.scale.value > 644 * 0.3 && model.y + 700 * 0.18 * model.scale.value < 644 * 0.6, 'portrait focus stays in the central camera area');
    assert(model.scale.value * 700 > 644, 'full body is intentionally cropped');

    const observer = observers.at(-1);
    for (const size of [{ width: 320, height: 520 }, { width: 900, height: 560 }, { width: 390, height: 644 }]) {
        canvas.parentElement.clientWidth = size.width;
        canvas.parentElement.clientHeight = size.height;
        // Keep the canvas at the old high-DPI inline size to model Pixi autoDensity.
        canvas.clientWidth = 1482;
        canvas.clientHeight = 755;
        observer.callback();
        assert.deepStrictEqual(app.renderer.size, size, 'renderer follows the parent stage after each layout change');
        assert.strictEqual(model.x, size.width / 2);
        assert(model.y + 700 * 0.18 * model.scale.value > size.height * 0.3 && model.y + 700 * 0.18 * model.scale.value < size.height * 0.6, 'portrait focus survives resize');
        assert(model.scale.value * 700 > size.height, 'body remains outside the portrait stage');
    }

    const lastVisibleSize = { ...app.renderer.size };
    const lastVisiblePosition = { x: model.x, y: model.y, scale: model.scale.value };
    canvas.parentElement.clientWidth = 0;
    canvas.parentElement.clientHeight = 0;
    canvas.clientWidth = 1;
    canvas.clientHeight = 1;
    observer.callback();
    assert.deepStrictEqual(app.renderer.size, lastVisibleSize, 'a hidden stage does not resize the renderer to a zero or fallback size');
    assert.deepStrictEqual({ x: model.x, y: model.y, scale: model.scale.value }, lastVisiblePosition,
        'a hidden stage preserves model placement and scale');

    canvas.parentElement.clientWidth = 390;
    canvas.parentElement.clientHeight = 644;
    observer.callback();
    assert.deepStrictEqual(app.renderer.size, { width: 390, height: 644 }, 'restoring the stage resumes its actual layout size');
    assert.strictEqual(model.x, 195);
    assert(model.y + 700 * 0.18 * model.scale.value > 644 * 0.3 && model.y + 700 * 0.18 * model.scale.value < 644 * 0.6, 'restoring the stage keeps portrait focus');
    assert.strictEqual(app.renderer.resizeCalls, 3, 'same-size restoration does not repeat a renderer resize');

    manager.destroy();
}

async function testNewInstanceWaitsForDestroyedCanvasContext() {
    const canvas = makeCanvas();
    const listeners = new Map();
    let contextLost = false;
    canvas.addEventListener = (name, handler) => {
        if (!listeners.has(name)) listeners.set(name, new Set());
        listeners.get(name).add(handler);
    };
    canvas.removeEventListener = (name, handler) => listeners.get(name)?.delete(handler);
    const dispatch = name => {
        for (const handler of [...(listeners.get(name) || [])]) {
            handler({ preventDefault() {} });
        }
    };
    const restoreExtension = {
        restoreContext() {
            setTimeout(() => { contextLost = false; dispatch('webglcontextrestored'); }, 0);
        },
    };
    const priorApplication = root.PIXI.Application;
    root.PIXI.Application = class extends MockApplication {
        constructor(options) {
            if (contextLost) throw new Error('canvas context was not restored before renderer creation');
            super(options);
            this.renderer.gl = {
                isContextLost: () => contextLost,
                getExtension: name => name === 'WEBGL_lose_context' ? restoreExtension : null,
            };
            const pixiDestroy = this.destroy;
            this.destroy = (removeView, stageOptions) => {
                pixiDestroy(removeView, stageOptions);
                contextLost = true;
                setTimeout(() => dispatch('webglcontextlost'), 0);
            };
        }
    };
    try {
        const first = root.ChatLive2D.create({ canvas });
        await first.load(entry({ id: 'before-destroy' }));
        first.destroy();
        assert.strictEqual(contextLost, true);
        const second = root.ChatLive2D.create({ canvas });
        await second.load(entry({ id: 'after-destroy' }));
        assert.strictEqual(contextLost, false, 'new instance waits for restoration of caller canvas');
        second.destroy();
    } finally {
        root.PIXI.Application = priorApplication;
    }
}

async function testRenderBufferIsBoundedAcrossWindowAndDpiChanges() {
    const originalDpr = root.devicePixelRatio;
    const canvas = makeCanvas();
    canvas.parentElement.clientWidth = 3840;
    canvas.parentElement.clientHeight = 2160;
    root.devicePixelRatio = 3;
    const manager = root.ChatLive2D.create({ canvas });
    const assertBounded = () => {
        assert(Math.max(canvas.width, canvas.height) <= 2560, 'the backing buffer long edge stays bounded');
        assert(Math.min(canvas.width, canvas.height) <= 1440, 'the backing buffer short edge stays bounded');
        assert(canvas.width * canvas.height <= 2560 * 1440, 'pixel count never grows past the budget');
        assert(Math.abs(canvas.width / canvas.height - canvas.parentElement.clientWidth / canvas.parentElement.clientHeight) < 0.01,
            'the render buffer preserves the stage aspect ratio');
    };
    try {
        await manager.load(entry());
        const app = applications.at(-1);
        const model = models.at(-1);
        assertBounded();
        assert(app.options.resolution < 1, 'a large high-DPI stage is capped before its first render');
        const observer = observers.at(-1);
        for (const [width, height, dpr] of [[2160, 3840, 3], [8000, 1200, 2], [900, 900, 2], [390, 644, 3], [640, 480, 1]]) {
            canvas.parentElement.clientWidth = width;
            canvas.parentElement.clientHeight = height;
            root.devicePixelRatio = dpr;
            observer.callback();
            assertBounded();
            assert.deepStrictEqual(app.renderer.size, { width, height }, 'CSS layout coordinates are preserved');
        }
        assert.strictEqual(canvas.width, 640, 'small low-DPI stages retain native resolution');
        assert.strictEqual(canvas.height, 480);
        const position = { x: model.x, y: model.y, scale: model.scale.value };
        root.devicePixelRatio = 2;
        observer.callback();
        assertBounded();
        assert(canvas.width > 640, 'a DPI change updates the buffer even without a layout change');
        assert.deepStrictEqual({ x: model.x, y: model.y, scale: model.scale.value }, position,
            'DPI changes do not move or stretch the portrait');
        const resizeCalls = app.renderer.resizeCalls;
        observer.callback();
        assert.strictEqual(app.renderer.resizeCalls, resizeCalls, 'unchanged render settings do not reset Cubism masks');
    } finally {
        manager.destroy();
        root.devicePixelRatio = originalDpr;
    }
}

async function testPortraitFramesGeometryInsideTransparentCanvas() {
    const priorFactory = modelFactory;
    modelFactory = () => {
        const model = makeModel();
        model.internalModel.coreModel.getDrawableCount = () => 1;
        model.internalModel.getDrawableVertices = () => [150, 140, 200, 150, 250, 140];
        return Promise.resolve(model);
    };
    const canvas = makeCanvas();
    canvas.parentElement.clientWidth = 900;
    canvas.parentElement.clientHeight = 560;
    const manager = root.ChatLive2D.create({ canvas });
    try {
        await manager.load(entry());
        const model = models.at(-1);
        const headTop = model.y + 140 * model.scale.value;
        const headCenter = model.x + (200 - 500 / 2) * model.scale.value;
        assert(headTop >= 0 && headTop < 560 * 0.3, 'transparent top padding does not push the head into the lower half');
        assert(headCenter > 900 * 0.35 && headCenter < 900 * 0.65, 'the head stays centered when the model canvas is asymmetric');
    } finally {
        manager.destroy();
        modelFactory = priorFactory;
    }
}

async function testRemoteUrlsAndMissingWebGlFailClosed() {
    const failures = [];
    const manager = root.ChatLive2D.create({ canvas: makeCanvas(), onFailure: error => failures.push(error) });
    await assert.rejects(manager.load(entry({ model_url: 'https://example.invalid/avatar.model3.json' })), /URL is invalid/);
    manager.destroy();

    const priorApplication = root.PIXI.Application;
    root.PIXI.Application = class extends MockApplication {
        constructor(options) { super(options); this.renderer.gl = null; }
    };
    const noWebGl = root.ChatLive2D.create({ canvas: makeCanvas(), onFailure: error => failures.push(error) });
    await assert.rejects(noWebGl.load(entry()), /WebGL is unavailable/);
    noWebGl.destroy();
    root.PIXI.Application = priorApplication;
    assert(failures.length >= 2);
}

async function testTimeoutDestroysLateModelAndFencesStaleCommands() {
    let resolveLoad;
    modelFactory = () => new Promise(resolve => { resolveLoad = resolve; });
    const manager = root.ChatLive2D.create({ canvas: makeCanvas(), loadTimeoutMs: 120 });
    await assert.rejects(manager.load(entry()), /Timed out loading Live2D model/);
    const lateModel = makeModel();
    resolveLoad(lateModel);
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(lateModel.destroyCalls, 1, 'late model resolution is cleaned after load timeout');
    manager.destroy();

    modelFactory = () => Promise.resolve(makeModel());
    const guarded = root.ChatLive2D.create({ canvas: makeCanvas() });
    await guarded.load(entry());
    const oldModel = models.at(-1);
    let finishExpression;
    oldModel.expression = () => new Promise(resolve => { finishExpression = resolve; });
    const delayedApply = guarded.apply([{ type: 'expression', name: 'shy' }, { type: 'motion', group: 'Nod' }]);
    await new Promise(resolve => setImmediate(resolve));
    await guarded.load(entry({ id: 'replacement' }));
    finishExpression(true);
    await delayedApply;
    assert.strictEqual(models.at(-1).motionCalls.length, 0, 'stale command sequence cannot reach replacement model');
    guarded.destroy();
}

(async () => {
    await testLoadCapabilitiesControlsMouthAndCleanup();
    await testVoiceOnlyModelDisablesPointerAutoInteract();
    await testInstancesAreIndependent();
    await testModelSwitchReusesCanvasRenderer();
    await testResizeUsesParentLayoutWhenPixiCanvasDimensionsAreStale();
    await testPortraitFramesGeometryInsideTransparentCanvas();
    await testRenderBufferIsBoundedAcrossWindowAndDpiChanges();
    await testNewInstanceWaitsForDestroyedCanvasContext();
    await testRemoteUrlsAndMissingWebGlFailClosed();
    await testTimeoutDestroysLateModelAndFencesStaleCommands();
    console.log('chat_live2d.test.js passed');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
