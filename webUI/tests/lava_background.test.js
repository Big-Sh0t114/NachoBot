const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '..', 'static', 'js', 'lava-background.js');
const source = fs.readFileSync(sourcePath, 'utf8').replace(/\r\n/g, '\n');
const backgroundCss = fs.readFileSync(path.join(__dirname, '..', 'static', 'css', 'doodle-background.css'), 'utf8');
const POOL_VERTICAL_SCALE = 2 / 3;

function createFixture({ width = 1100, height = 900, dpr = 2, inspectInternals = false } = {}) {
    const rafCallbacks = new Map();
    const metrics = {
        contextCalls: 0,
        clearRects: 0,
        quadraticCalls: 0,
        frameQuadratics: [],
        fillPaths: [],
        fillStates: [],
        strokeStyles: [],
        fillRectCalls: [],
        drawImageCalls: [],
        putImageDataCalls: [],
        imageDataCreates: [],
        drawCalls: [],
        gradients: [],
        radialGradients: [],
        shadowColors: [],
        shadowBlurs: [],
        shadowOffsets: [],
        arcs: 0,
        lines: 0,
        clips: 0,
        strokes: 0,
        listenerAdds: [],
        listenerRemoves: [],
    };
    let nextRafId = 1;
    let previousFrameQuadratics = 0;
    let currentPath = [];
    const contextStack = [];

    const context = {
        createLinearGradient(...coordinates) {
            const gradient = {
                coordinates,
                stops: [],
                addColorStop(position, color) { this.stops.push([position, color]); },
            };
            metrics.gradients.push(gradient);
            return gradient;
        },
        createRadialGradient(...coordinates) {
            const gradient = {
                coordinates,
                stops: [],
                addColorStop(position, color) { this.stops.push([position, color]); },
            };
            metrics.radialGradients.push(gradient);
            return gradient;
        },
        save() {
            contextStack.push({
                shadowColor: context.shadowColor,
                shadowBlur: context.shadowBlur,
                shadowOffsetX: context.shadowOffsetX,
                shadowOffsetY: context.shadowOffsetY,
                globalAlpha: context.globalAlpha,
                fillStyle: context.fillStyle,
                strokeStyle: context.strokeStyle,
                lineWidth: context.lineWidth,
                lineCap: context.lineCap,
                lineJoin: context.lineJoin,
                transform: metrics.currentTransform?.slice() || [1, 0, 0, 1, 0, 0],
            });
        },
        restore() {
            const state = contextStack.pop();
            if (state) {
                Object.assign(context, state);
                metrics.currentTransform = state.transform;
            }
        },
        setTransform(...values) { metrics.currentTransform = values; },
        drawImage(image, ...args) {
            const call = {
                type: 'drawImage',
                image,
                imageWidth: image.width,
                imageHeight: image.height,
                args,
                transform: metrics.currentTransform.slice(),
            };
            metrics.drawImageCalls.push(call);
            metrics.drawCalls.push(call);
        },
        createImageData(width, height) {
            const imageData = { width, height, data: new Uint8ClampedArray(width * height * 4) };
            metrics.imageDataCreates.push({ width, height, imageData });
            return imageData;
        },
        putImageData(imageData, x, y) {
            const call = { type: 'putImageData', imageData, x, y };
            metrics.putImageDataCalls.push(call);
            metrics.drawCalls.push(call);
        },
        clearRect(x, y, rectWidth, rectHeight) {
            metrics.clearRects += 1;
            metrics.drawCalls.push({ type: 'clearRect', x, y, width: rectWidth, height: rectHeight });
            if (metrics.clearRects > 1) {
                metrics.frameQuadratics.push(metrics.quadraticCalls - previousFrameQuadratics);
            }
            previousFrameQuadratics = metrics.quadraticCalls;
        },
        beginPath() { currentPath = []; },
        moveTo(x, y) { currentPath.push({ type: 'move', values: [x, y] }); },
        lineTo(x, y) { metrics.lines += 1; currentPath.push({ type: 'line', values: [x, y] }); },
        arc() { metrics.arcs += 1; },
        clip() { metrics.clips += 1; },
        quadraticCurveTo(controlX, controlY, x, y) {
            metrics.quadraticCalls += 1;
            currentPath.push({ type: 'quadratic', values: [controlX, controlY, x, y] });
        },
        closePath() { currentPath.push({ type: 'close', values: [] }); },
        fill() {
            metrics.fillPaths.push(currentPath.map((command) => ({ ...command, values: [...command.values] })));
            metrics.fillStates.push({ globalAlpha: context.globalAlpha, fillStyle: context.fillStyle });
            metrics.drawCalls.push({ type: 'fill', fillStyle: context.fillStyle, globalAlpha: context.globalAlpha });
        },
        fillRect(x, y, rectWidth, rectHeight) {
            throw new Error(`The exterior pool aura must use its bounded cached raster, not fillRect (${x}, ${y}, ${rectWidth}, ${rectHeight})`);
        },
        stroke() {
            metrics.strokes += 1;
            metrics.strokeStyles.push(context.strokeStyle);
        },
    };

    Object.defineProperties(context, {
        shadowColor: {
            configurable: true,
            get() { return metrics.currentShadowColor || 'rgba(0, 0, 0, 0)'; },
            set(value) { metrics.currentShadowColor = value; metrics.shadowColors.push(value); },
        },
        shadowBlur: {
            configurable: true,
            get() { return metrics.currentShadowBlur || 0; },
            set(value) { metrics.currentShadowBlur = value; metrics.shadowBlurs.push(value); },
        },
        shadowOffsetX: {
            configurable: true,
            get() { return metrics.currentShadowOffsetX || 0; },
            set(value) { metrics.currentShadowOffsetX = value; metrics.shadowOffsets.push(['x', value]); },
        },
        shadowOffsetY: {
            configurable: true,
            get() { return metrics.currentShadowOffsetY || 0; },
            set(value) { metrics.currentShadowOffsetY = value; metrics.shadowOffsets.push(['y', value]); },
        },
    });

    class MockElement {
        constructor(tagName) {
            this.tagName = tagName.toUpperCase();
            this.id = '';
            this.style = {};
            this.attributes = {};
            this.children = [];
            this.parentElement = null;
            this.width = 0;
            this.height = 0;
        }

        get firstChild() { return this.children[0] || null; }

        setAttribute(name, value) { this.attributes[name] = String(value); }

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

        remove() {
            if (this.parentElement) this.parentElement.removeChild(this);
        }

        getContext(type) {
            assert.equal(type, '2d');
            metrics.contextCalls += 1;
            metrics.contextCanvases ??= [];
            metrics.contextCanvases.push({ tagName: this.tagName, id: this.id, width: this.width, height: this.height });
            return context;
        }
    }

    const body = new MockElement('body');
    const document = {
        body,
        hidden: false,
        eventListeners: new Map(),
        addEventListener(type, callback) {
            if (!this.eventListeners.has(type)) this.eventListeners.set(type, new Set());
            this.eventListeners.get(type).add(callback);
            metrics.listenerAdds.push(['document', type]);
        },
        removeEventListener(type, callback) {
            this.eventListeners.get(type)?.delete(callback);
            metrics.listenerRemoves.push(['document', type]);
        },
        createElement: (tagName) => new MockElement(tagName),
        getElementById(id) {
            const visit = (element) => {
                if (element.id === id) return element;
                for (const child of element.children) {
                    const match = visit(child);
                    if (match) return match;
                }
                return null;
            };
            return visit(body);
        },
    };
    const reduceMotion = { matches: false };
    const window = {
        devicePixelRatio: dpr,
        eventListeners: new Map(),
        addEventListener(type, callback, options) {
            if (!this.eventListeners.has(type)) this.eventListeners.set(type, new Set());
            this.eventListeners.get(type).add(callback);
            metrics.listenerAdds.push(['window', type, options]);
        },
        removeEventListener(type, callback) {
            this.eventListeners.get(type)?.delete(callback);
            metrics.listenerRemoves.push(['window', type]);
        },
        matchMedia: () => reduceMotion,
        requestAnimationFrame(callback) {
            const id = nextRafId++;
            rafCallbacks.set(id, callback);
            return id;
        },
        cancelAnimationFrame(id) { rafCallbacks.delete(id); },
        emit(type, event) {
            for (const callback of [...(this.eventListeners.get(type) || [])]) callback({ type, ...event });
        },
    };

    const seededMath = Object.create(Math);
    let randomState = 0x12345678;
    seededMath.random = () => {
        randomState = (1664525 * randomState + 1013904223) >>> 0;
        return randomState / 0x100000000;
    };
    const sandbox = vm.createContext({ document, Math: seededMath, window });
    let executableSource = source;
    if (inspectInternals) {
        const returnMarker = 'return { resize, start, stop, dispose };';
        const nodeInjectionMarker = '            for (let index = 0; index < poolNodes.length; index += 1) {\n'
            + '                const node = poolNodes[index];\n'
            + '                const x = width * node.ratioX + Math.sin(time * node.freq1 + node.phase1) * 16;';
        const nodeInjectionCall = '                injectEllipse(x, y, radius, radiusY, radius * radius);';
        const testReturn = 'return { resize, start, stop, dispose, __inspect: () => ({ '
            + 'blobCount: blobs.length, poolNodeCount: poolNodes.length, '
            + 'blobPositions: blobs.map(({ x, y, radius }) => ({ x, y, radius })), '
            + 'blobs: blobs.map(({ x, y, radius, birth, carvePhase, sliceCooldown, separationCooldown, riseSpeed, vx, vy }) => ({ '
            + 'x, y, radius, birth, carvePhase, sliceCooldown, separationCooldown, riseSpeed, vx, vy })), '
            + 'bladeCount: bladeNodes.length, sparkCount: sparks.length, trailCount: trail.length, '
            + 'bladeNodes: bladeNodes.map(({ x, y, radius, life }) => ({ x, y, radius, life })), '
            + 'poolTargetOffsets: poolNodes.map(({ targetOffset }) => targetOffset), '
            + 'lastPointer: lastPointer && ({ x: lastPointer.x, y: lastPointer.y }), '
            + 'poolAuraReady, poolSliceCooldown, poolCutSurfaceAt: (x) => getPoolCutSurfaceAtX(x), '
            + 'poolCutFieldAt: (x, y) => getPoolCutFieldAt(x, y), '
            + 'poolCutState: () => ({ ...poolCutResult }), '
            + 'hardLimit: getBlobHardLimit(), poolSurfaceAt: (x) => getPoolSurfaceY(x), '
            + 'poolSurfaceAtTime: (x, atTime) => getPoolSurfaceY(x, atTime), '
            + 'poolHalfWidth: getPoolHalfWidth(), poolDepth: getPoolDepth(), cellSize, active, '
            + 'gridGeometry: { originX, originY, cellSize, cols, rows }, '
            + 'poolAuraGeometry: { cols: poolAuraCols, rows: poolAuraRows, startRow: poolAuraStartRow, top: poolAuraTop, height: poolAuraHeight, cellSize }, '
            + 'poolAuraCanvas, poolAuraImageData, '
            + 'poolAuraAlphaAt: (x, y) => { const gx = Math.round((x - originX) / cellSize); '
            + 'const localY = Math.round((y - originY) / cellSize) - poolAuraStartRow; '
            + 'if (gx < 0 || gx >= poolAuraCols || localY < 0 || localY >= poolAuraRows) return null; '
            + 'return poolAuraPixels[(localY * poolAuraCols + gx) * 4 + 3]; }, '
            + 'poolAuraFieldAt: (x, y) => { const gx = Math.round((x - originX) / cellSize); '
            + 'const localY = Math.round((y - originY) / cellSize) - poolAuraStartRow; '
            + 'if (gx < 0 || gx >= poolAuraCols || localY < 0 || localY >= poolAuraRows) return null; '
            + 'return poolAuraField[localY * poolAuraCols + gx]; }, '
            + 'poolAuraDistanceAt: (x, y) => { const gx = Math.round((x - originX) / cellSize); '
            + 'const localY = Math.round((y - originY) / cellSize) - poolAuraStartRow; '
            + 'if (gx < 0 || gx >= poolAuraCols || localY < 0 || localY >= poolAuraRows) return null; '
            + 'return poolAuraDistance[localY * poolAuraCols + gx] * cellSize; }, '
            + 'poolAuraAlphaSnapshot: () => { const result = new Uint8Array(poolAuraField.length); '
            + 'for (let i = 0; i < result.length; i += 1) result[i] = poolAuraPixels[i * 4 + 3]; return result; }, '
            + 'poolAuraDistanceSnapshot: () => poolAuraDistance.slice(), '
            + 'poolAuraFieldSnapshot: () => poolAuraField.slice(), '
            + 'gridNodeAt: (gx, gy) => grid[gy * (cols + 1) + gx], '
            + 'gridAt: (x, y) => { const gx = Math.round((x - originX) / cellSize); '
            + 'const gy = Math.round((y - originY) / cellSize); return grid[gy * (cols + 1) + gx]; }, '
            + 'baseGridSampleAt: (x, y) => { const sample = window.__lavaBaseGridSnapshot; '
            + 'const gx = Math.round((x - sample.originX) / sample.cellSize); '
            + 'const gy = Math.round((y - sample.originY) / sample.cellSize); '
            + 'return { x: sample.originX + gx * sample.cellSize, y: sample.originY + gy * sample.cellSize, '
            + 'value: sample.values[gy * (sample.cols + 1) + gx] }; }, '
            + 'poolNodeDetails: () => poolNodes.map(({ ratioX, phase1, phase2, freq1, freq2, baseRadius, viscousOffset }) => '
            + '({ ratioX, phase1, phase2, freq1, freq2, baseRadius, viscousOffset })), '
            + 'poolNodeInjectionSnapshot: () => window.__lavaPoolNodeInjectionSnapshot, '
            + 'renderFrameForTest: () => renderFrame(), '
            + 'setPoolViscousForTest: (values) => poolNodes.forEach((node, index) => { node.viscousOffset = values[index]; }), '
            + 'setBladeForTest: (x, y, radius) => addBladeNode(x, y, radius), '
            + 'setBlobForTest: (index, x, y, radius, options = {}) => Object.assign(blobs[index], '
            + '{ x, y, radius, birth: 1, carvePhase: 1 }, options), '
            + 'seedBlobsForTest: (count) => { while (blobs.length < count) newBlob({ '
            + 'x: 32 + (blobs.length * 13) % Math.max(1, width - 64), '
            + 'y: height * 0.32 + (blobs.length % 2) * 70, radius: 20, birth: 1 }); }, '
            + 'shrinkAt: (y) => getBlobShrink(y) }) };';
        assert.equal(source.split(returnMarker).length - 1, 1, 'test instrumentation has one controller return marker');
    assert.equal(source.split(nodeInjectionMarker).length - 1, 1,
        'test instrumentation can snapshot the continuous pool field before node injection');
        assert.equal(source.split(nodeInjectionCall).length - 1, 1,
            'test instrumentation can observe the source node ellipse geometry');
        executableSource = source
            .replace(nodeInjectionMarker, '            window.__lavaBaseGridSnapshot = { originX, originY, cellSize, cols, values: grid.slice() };\n'
                + '            window.__lavaPoolNodeInjectionSnapshot = [];\n'
                + nodeInjectionMarker)
            .replace(nodeInjectionCall, '                window.__lavaPoolNodeInjectionSnapshot.push({ x, y, radiusX: radius, radiusY, time, strength: radius * radius });\n'
                + nodeInjectionCall)
            .replace(returnMarker, testReturn);
    }
    vm.runInContext(executableSource, sandbox, { filename: sourcePath });

    return {
        body,
        document,
        metrics,
        rafCallbacks,
        reduceMotion,
        window,
        width,
        height,
        lastFrameTimestamp: null,
        createController() { return window.LavaBackground.create(); },
        emitWindow(type, event = {}) { window.emit(type, event); },
        emitDocument(type, event = {}) {
            for (const callback of [...(document.eventListeners.get(type) || [])]) callback({ type, ...event });
        },
        fireNextFrame(timestamp) {
            const entry = rafCallbacks.entries().next().value;
            assert.ok(entry, 'the active background should have one pending frame');
            rafCallbacks.delete(entry[0]);
            this.lastFrameTimestamp = timestamp;
            entry[1](timestamp);
        },
        fireFrames(frameRate, durationSeconds = 1) {
            const frameCount = Math.round(frameRate * durationSeconds);
            for (let index = 0; index <= frameCount; index += 1) {
                this.fireNextFrame(index * 1000 / frameRate);
            }
        },
    };
}

function contourLoops(pathCommands) {
    const loops = [];
    let points = [];
    for (const command of pathCommands) {
        if (command.type === 'move') {
            if (points.length) loops.push(points);
            points = [command.values];
        } else if (command.type === 'quadratic') {
            points.push([command.values[2], command.values[3]]);
        } else if (command.type === 'close' && points.length) {
            loops.push(points);
            points = [];
        }
    }
    if (points.length) loops.push(points);
    return loops;
}

function assertByteArraysEqual(actual, expected, message) {
    assert.equal(actual.length, expected.length, `${message}: length`);
    for (let index = 0; index < actual.length; index += 1) {
        assert.equal(actual[index], expected[index], `${message}: byte ${index}`);
    }
}

function getContourShape(loop) {
    const xs = loop.map(([x]) => x);
    const ys = loop.map(([, y]) => y);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    const radii = loop.map(([x, y]) => Math.hypot(x - centerX, y - centerY));
    return {
        width: Math.max(...xs) - Math.min(...xs),
        height: Math.max(...ys) - Math.min(...ys),
        radialSpread: Math.max(...radii) - Math.min(...radii),
    };
}

test('script load allocates no canvas; resize builds a bounded opaque field canvas and cached pool raster', () => {
    const fixture = createFixture();
    assert.equal(fixture.metrics.contextCalls, 0);
    assert.equal(fixture.body.children.length, 0);

    const controller = fixture.createController();
    const layer = fixture.document.getElementById('lava-background-layer');
    const canvas = fixture.document.getElementById('lava-background-canvas');
    assert.ok(layer && canvas);
    controller.resize(900, 700, 292, 0);
    assert.equal(layer.style.left, '292px');
    assert.equal(layer.style.width, '900px');
    assert.equal(canvas.width, 1125, 'DPR is capped at 1.25');
    assert.equal(canvas.height, 875);
    assert.equal(fixture.metrics.contextCalls, 2,
        'the main canvas context and one detached pool-only aura context are created at resize');
    assert.equal(fixture.metrics.gradients.length, 1,
        'only the pale core gradient is allocated; the exterior aura uses a reusable raster');
    assert.deepEqual(fixture.metrics.gradients[0].stops.map(([, color]) => color), ['#FFFEFA', '#FFF8EF']);
    const auraRaster = fixture.metrics.imageDataCreates[0];
    assert.ok(auraRaster.width > 50 && auraRaster.width < 200);
    assert.ok(auraRaster.height > 10 && auraRaster.height < 60,
        'the cached low-resolution distance raster contains only the bounded pool band');
    assert.deepEqual(fixture.metrics.contextCanvases[1], {
        tagName: 'CANVAS', id: '', width: auraRaster.width, height: auraRaster.height,
    }, 'the alpha-enabled aura raster is detached from the visible layer');
    assert.equal(fixture.metrics.fillRectCalls.length, 0);
    controller.dispose();
    assert.equal(fixture.document.getElementById('lava-background-layer'), null);
});

test('organic scalar contours stay finite, move over time, and render at no more than 30 Hz', async (t) => {
    const finalOuterContours = [];
    for (const frameRate of [60, 120, 144]) {
        await t.test(`${frameRate} Hz`, () => {
            const fixture = createFixture();
            const controller = fixture.createController();
            controller.resize(1100, 900, 292, 0);
            controller.start();
            assert.equal(fixture.rafCallbacks.size, 1);
            fixture.fireFrames(frameRate);

            assert.equal(fixture.metrics.clearRects, 31,
                'the initial static frame plus 30 fixed updates should render at every source frame rate');
            assert.equal(fixture.rafCallbacks.size, 1, 'the controller keeps a single RAF chain');
            assert.ok(fixture.metrics.quadraticCalls > 0, 'Marching Squares must emit smoothed Bezier contours');
            assert.ok(fixture.metrics.frameQuadratics.every((count) => count < 10000), 'contour work stays bounded per frame');

            const renderedPaths = fixture.metrics.fillPaths.filter((commands) => commands.some((command) => command.type === 'quadratic'));
            assert.ok(renderedPaths.length >= 4, 'outer and core fills should contain actual contour loops');
            for (const commands of renderedPaths) {
                for (const command of commands) {
                    assert.ok(command.values.every(Number.isFinite), 'all field contour coordinates must remain finite');
                }
            }

            const lastOuterFill = fixture.metrics.fillPaths.at(-2);
            finalOuterContours.push(lastOuterFill);
            const loops = contourLoops(lastOuterFill);
            const blobContour = loops
                .map(getContourShape)
                .find((shape) => shape.width >= 28 && shape.width <= 210
                    && shape.height >= 28 && shape.height <= 210);
            assert.ok(blobContour, 'the upper scalar field should produce a separate blob contour');
            assert.ok(blobContour.radialSpread > 2, 'the blob contour should retain visible organic asymmetry');

            const firstFill = fixture.metrics.fillPaths[0];
            assert.notDeepEqual(lastOuterFill, firstFill, 'surface and blob contours should move as time advances');
            controller.stop();
            assert.equal(fixture.rafCallbacks.size, 0);
            controller.dispose();
        });
    }
    assert.deepEqual(finalOuterContours[1], finalOuterContours[0], '60 and 120 Hz should advance identical simulated time');
    assert.deepEqual(finalOuterContours[2], finalOuterContours[0], '60 and 144 Hz should advance identical simulated time');
});

test('desktop-to-mobile resize caps existing blobs and rebuilds the pool without resetting survivors', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    fixture.fireFrames(30, 14);

    const desktop = controller.__inspect();
    assert.equal(desktop.blobCount, 8, 'normal pool gestation fills the desktop target');
    assert.equal(desktop.poolNodeCount, 20);
    assert.ok(desktop.blobPositions.every((blob) => blob.radius >= 30 && blob.radius <= 65),
        'pool compression does not change floating blob radii');
    assert.ok(Math.abs(desktop.shrinkAt(0.7 * 900) - 1) < 1e-9);
    assert.ok(desktop.shrinkAt(0.2 * 900) < 0.7, 'blobs shrink while still in the upper viewport');
    assert.ok(desktop.shrinkAt(0) < 0.2, 'blobs are nearly gone before leaving the viewport');

    controller.resize(650, 800, 0, 0);
    const mobile = controller.__inspect();
    assert.equal(mobile.blobCount, 5, 'resize immediately trims existing blobs to the mobile cap');
    assert.equal(mobile.poolNodeCount, 20, 'the source keeps twenty pool nodes at every viewport size');
    desktop.blobPositions.slice(0, 5).forEach((blob, index) => {
        assert.ok(Math.abs(mobile.blobPositions[index].x - blob.x * (650 / 1100)) < 1e-6);
        assert.ok(Math.abs(mobile.blobPositions[index].y - blob.y * (800 / 900)) < 1e-6);
    });

    controller.resize(1100, 900, 292, 0);
    assert.equal(controller.__inspect().poolNodeCount, 20, 'returning to desktop retains the source pool density');
    assert.equal(controller.__inspect().blobCount, 5, 'existing survivors remain in place after a viewport expansion');
    controller.dispose();
});

test('the pool field spans both content edges and closes beyond them on wide desktop and mobile', async (t) => {
    for (const viewport of [
        { width: 2400, height: 1000, left: 292, label: '2400px wide desktop' },
        { width: 390, height: 844, left: 0, label: 'mobile' },
    ]) {
        await t.test(viewport.label, () => {
            const fixture = createFixture({ width: viewport.width, height: viewport.height, inspectInternals: true });
            const controller = fixture.createController();
            controller.resize(viewport.width, viewport.height, viewport.left, 0);
            controller.start();
            fixture.fireNextFrame(0);

            const state = controller.__inspect();
            assert.equal(state.poolHalfWidth, viewport.width * 0.5,
                'the pool has no fixed pixel cap or narrowed ends');
            const expectedDepth = Math.max(56, Math.min(84, viewport.height * 0.092)) * POOL_VERTICAL_SCALE;
            assert.equal(state.poolDepth, expectedDepth, 'pool depth follows the source height clamp at 2/3 vertical scale');
            for (const x of [-12, 0, 8, viewport.width - 8, viewport.width, viewport.width + 12]) {
                const y = state.poolSurfaceAt(x) + 50;
                assert.ok(state.gridAt(x, y) > 1.2,
                    `the pool scalar field remains solid near x=${x} instead of fading at the edge`);
            }

            controller.dispose();
        });
    }
});

test('pool surface, node layout, and base scalar ramp match the reference formulas', () => {
    const fixture = createFixture({ width: 2400, height: 1000, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(2400, 1000, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);
    fixture.fireNextFrame(1000 / 30);

    const state = controller.__inspect();
    const nodes = state.poolNodeDetails();
    assert.equal(nodes.length, 20);
    const nodeSamples = state.poolNodeInjectionSnapshot();
    assert.equal(nodeSamples.length, 20, 'the renderer injects all twenty pool nodes into the field');
    nodes.forEach((node, index) => {
        const ratio = index / 19;
        assert.equal(node.ratioX, ratio);
        assert.ok(node.phase1 >= ratio * Math.PI * 2.4
            && node.phase1 < ratio * Math.PI * 2.4 + 1.5);
        assert.ok(node.phase2 <= ratio * Math.PI * 4
            && node.phase2 > ratio * Math.PI * 4 - 1.5);
        assert.ok(node.freq1 >= 0.15 && node.freq1 < 0.23);
        assert.ok(node.freq2 >= 0.22 && node.freq2 < 0.32);
        assert.ok(Math.abs(node.baseRadius - (45 + Math.sin(ratio * Math.PI) * 12)) < 1e-12);
        assert.equal(node.viscousOffset, 0);

        const sample = nodeSamples[index];
        const sourceNodeY = 1000 - state.poolDepth * 0.44
            + Math.sin(node.phase1 + sample.time * node.freq1) * 9
            + Math.cos(node.phase2 - sample.time * node.freq2) * 5.5
            + node.viscousOffset;
        const pulse = Math.sin(sample.time * node.freq2 + node.phase1) * 4.5;
        const sourceRadius = node.baseRadius + pulse;
        assert.ok(sample.time > 0, 'the node pulse and center checks use an animated frame');
        assert.ok(Math.abs(sample.x - (2400 * ratio + Math.sin(sample.time * node.freq1 + node.phase1) * 16)) < 1e-9,
            'the 2/3 pool compression leaves node spacing and horizontal movement unchanged');
        assert.ok(Math.abs(sample.y - sourceNodeY) < 1e-9,
            'pool depth is compressed, while node center waves and viscous offset retain source amplitude');
        assert.ok(Math.abs(sample.radiusX - sourceRadius) < 1e-9,
            'node width retains its original source radius');
        assert.ok(Math.abs(sample.radiusY - (node.baseRadius * POOL_VERTICAL_SCALE + pulse)) < 1e-9,
            'node base height compresses while its 4.5px breathing pulse keeps source amplitude');
        assert.ok(Math.abs(sample.strength - sourceRadius * sourceRadius) < 1e-7,
            'vertical compression does not reduce node scalar strength');
    });

    const sample = state.baseGridSampleAt(1273, state.poolSurfaceAt(1273) + 30);
    const depthBelow = (sample.y - state.poolSurfaceAt(sample.x)) / POOL_VERTICAL_SCALE;
    const expectedField = depthBelow >= 0
        ? 0.54 + Math.min(depthBelow * 0.0185, 1.05)
        : 0.54 * (1 + depthBelow / 44) ** 2 * (3 - 2 * (1 + depthBelow / 44));
    assert.ok(Math.abs(sample.value - expectedField) < 1e-6,
        `the compressed base scalar preserves the source ramp (value=${sample.value}, expected=${expectedField}, sourceDepth=${depthBelow})`);

    const offsets = nodes.map((node, index) => Math.sin(index * 0.37) * 11);
    state.setPoolViscousForTest(offsets);
    for (const x of [0, 1, 417.5, 1199, 2399, 2400]) {
        const nx = Math.min(1, Math.max(0, x / 2400));
        const nodePosition = nx * 19;
        const index0 = Math.floor(nodePosition);
        const index1 = Math.min(19, index0 + 1);
        const fraction = nodePosition - index0;
        const viscous = offsets[index0] * (1 - fraction) + offsets[index1] * fraction;
        const time = 13.25;
        const expected = 1000 - state.poolDepth * 0.70
            + Math.sin(nx * 4.2 + time * 0.21) * 9.2
            + Math.cos(nx * 6.8 - time * 0.29 + 1.2) * 5.6
            + Math.sin(nx * 2.5 + time * 0.14 + 2.5) * 7.0
            + viscous;
        assert.ok(Math.abs(state.poolSurfaceAtTime(x, time) - expected) < 1e-9,
            `compressed baseline retains full source wave and viscous amplitudes at x=${x}`);
    }

    state.renderFrameForTest();
    const viscousNodeSamples = state.poolNodeInjectionSnapshot();
    nodes.forEach((node, index) => {
        assert.ok(Math.abs(viscousNodeSamples[index].y - nodeSamples[index].y - offsets[index]) < 1e-9,
            `pool node ${index} retains its full viscous center offset`);
    });

    controller.dispose();
});

test('visibility, reduced motion, and disposal leave no animation callback pending', () => {
    const fixture = createFixture({ width: 840, height: 700 });
    const controller = fixture.createController();
    controller.resize(840, 700, 292, 0);
    controller.start();
    assert.equal(fixture.rafCallbacks.size, 1);

    fixture.document.hidden = true;
    controller.stop();
    assert.equal(fixture.rafCallbacks.size, 0, 'a hidden document can stop without scheduling another frame');
    fixture.document.hidden = false;

    fixture.reduceMotion.matches = true;
    const priorRenderCount = fixture.metrics.clearRects;
    controller.start();
    assert.equal(fixture.rafCallbacks.size, 0, 'reduced motion must not create a RAF loop');
    assert.equal(fixture.metrics.clearRects, priorRenderCount + 1, 'reduced motion paints one static contour frame');

    fixture.reduceMotion.matches = false;
    controller.start();
    assert.equal(fixture.rafCallbacks.size, 1);
    controller.dispose();
    assert.equal(fixture.rafCallbacks.size, 0);
    assert.equal(fixture.document.getElementById('lava-background-layer'), null);
});

function moveMouse(fixture, x, y, left = 292, target = null) {
    fixture.emitWindow('pointermove', {
        clientX: left + x,
        clientY: y,
        pointerType: 'mouse',
        target: target || { closest() { return null; } },
    });
}

function moveMouseAt(fixture, x, y, timeStamp, left = 292) {
    fixture.emitWindow('pointermove', {
        clientX: left + x,
        clientY: y,
        pointerType: 'mouse',
        timeStamp,
        target: { closest() { return null; } },
    });
}

function setTestBlob(controller, radius, x = 550, y = 260, options = {}) {
    const state = controller.__inspect();
    const index = state.blobs.reduce((best, blob, candidate) => blob.y < state.blobs[best].y ? candidate : best, 0);
    state.setBlobForTest(index, x, y, radius, options);
    return { index, x, y, radius };
}

function sendStroke(fixture, center, radius, angle = 0, timeMs = 60, reachFactor = 2.7) {
    const reach = radius * reachFactor;
    const dx = Math.cos(angle) * reach;
    const dy = Math.sin(angle) * reach;
    moveMouseAt(fixture, center.x - dx, center.y - dy, 0);
    moveMouseAt(fixture, center.x + dx, center.y + dy, timeMs);
}

function advanceFramesTo(fixture, frameRate, targetTimeMs) {
    const step = 1000 / frameRate;
    for (let timestamp = (fixture.lastFrameTimestamp ?? 0) + step;
        timestamp <= targetTimeMs + 0.001; timestamp += step) {
        fixture.fireNextFrame(timestamp);
    }
}

function cutAcrossBlob(fixture, blob, left = 292) {
    const reach = blob.radius * 1.45;
    moveMouse(fixture, blob.x - reach, blob.y, left);
    moveMouse(fixture, blob.x + reach, blob.y, left);
}

test('reference lighting stays local and restores canvas shadow state', () => {
    const fixture = createFixture();
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);

    assert.ok(fixture.metrics.shadowColors.includes('rgba(217, 194, 175, 0.25)'));
    assert.ok(fixture.metrics.shadowColors.includes('rgba(255, 250, 240, 0.8)'));
    assert.ok(fixture.metrics.shadowBlurs.includes(28), 'desktop caramel skin uses the reference 28px shadow');
    assert.ok(fixture.metrics.shadowBlurs.includes(20), 'the pale inner contour uses the reference 20px shadow');
    assert.equal(fixture.metrics.currentShadowBlur, 0, 'both local shadows restore before the transparent canvas is returned');
    assert.equal(fixture.metrics.currentShadowOffsetY, 0, 'the outer rim offset does not leak into other drawing');
    assert.ok(fixture.metrics.strokeStyles.includes('#D9C2AF'), 'outer antialiasing uses the caramel skin color');
    assert.ok(!fixture.metrics.strokeStyles.includes('rgba(251, 246, 238, 0.36)'),
        'the outer contour has no pale stroke that could create a white pool edge');
    assert.ok(fixture.metrics.strokes > 0, 'the antialiased outer contour is rendered');
    controller.dispose();
});

test('opaque lava contours cover lower background layers while UI remains above them', () => {
    const fixture = createFixture();
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);

    const outer = fixture.metrics.fillStates[0];
    const core = fixture.metrics.fillStates[1];
    assert.equal(outer.globalAlpha, 1);
    assert.equal(outer.fillStyle, '#D9C2AF', 'the outer contour uses the SVG tone preblended at full opacity');
    assert.equal(core.globalAlpha, 1, 'the warm-white core stays fully opaque and distinct from the caramel skin');
    const compositeAlpha = core.globalAlpha + outer.globalAlpha * (1 - core.globalAlpha);
    assert.equal(compositeAlpha, 1, 'the inner core is still fully opaque over its outer contour');

    assert.match(backgroundCss, /body\s*\{[^}]*isolation:\s*isolate/s);
    assert.match(backgroundCss, /#bg-canvas\s*\{[^}]*z-index:\s*-3\s*!important/s);
    assert.match(backgroundCss, /#doodle-background-layer\s*\{[^}]*z-index:\s*-2/s);
    assert.match(backgroundCss, /#lava-background-layer\s*\{[^}]*z-index:\s*-1/s);
    assert.match(backgroundCss, /#lava-background-canvas\s*\{[^}]*opacity:\s*1\s*;/s);
    assert.match(backgroundCss, /body:has\(#lava-background-layer\)\s*\.chat-composer-area\s*\{[^}]*background:\s*transparent/s);
    controller.dispose();
});

test('the short exterior aura follows concave pool contours in a bounded 2D raster', () => {
    const fixture = createFixture({ width: 1100, height: 900, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    controller.__inspect().setBlobForTest(0, 550, 250, 58);
    fixture.fireNextFrame(0);

    const state = controller.__inspect();
    const geometry = state.gridGeometry;
    const aura = state.poolAuraGeometry;
    const raster = fixture.metrics.imageDataCreates[0];
    assert.equal(raster.width, aura.cols);
    assert.equal(raster.height, aura.rows);
    assert.ok(aura.height >= 15 && aura.height <= 22.5, 'the exterior falloff is limited to 15–22.5 CSS pixels');
    assert.ok(aura.top <= 900 - (Math.max(state.poolDepth * 0.70 + 58,
        state.poolDepth * 0.44 + 110)
        + 96 + aura.height + geometry.cellSize * 4),
    'the raster begins above the full pool halo, blade reach, and aura falloff to avoid cropping');
    assert.ok(aura.rows < 60 && aura.cols < 200, 'the 2D buffer remains a small bottom band');
    assert.equal(fixture.metrics.imageDataCreates.length, 1, 'resize creates one reusable ImageData buffer');
    assert.equal(fixture.metrics.gradients.length, 1, 'the core gradient remains the only cached canvas gradient');
    assert.equal(fixture.metrics.fillRectCalls.length, 0, 'the aura never paints a flat rectangular wash');
    assert.ok(state.gridAt(550, 250) > 1, 'the detached upper blob still has a real scalar contour');
    assert.equal(fixture.metrics.drawImageCalls.length, 1, 'one cached pool raster is drawn per fluid frame');
    assert.equal(fixture.metrics.drawImageCalls[0].image, state.poolAuraCanvas);
    assert.deepEqual(fixture.metrics.drawImageCalls[0].args, [
        geometry.originX - geometry.cellSize * 0.5,
        aura.top - geometry.cellSize * 0.5,
        aura.cols * geometry.cellSize,
        aura.rows * geometry.cellSize,
    ]);
    assert.equal(fixture.metrics.drawImageCalls[0].args[0] + geometry.cellSize * 0.5, geometry.originX,
        'the first raster pixel center aligns with the scalar grid node at the content edge');
    assert.equal(fixture.metrics.drawImageCalls[0].args[1] + geometry.cellSize * 0.5, aura.top,
        'the first raster pixel center aligns with the pool-band grid node');
    assert.equal(fixture.metrics.putImageDataCalls.length, 1);

    const pixels = state.poolAuraImageData.data;
    const distanceInCells = state.poolAuraDistanceSnapshot();
    let insideCount = 0;
    let litExteriorCount = 0;
    let fadedExteriorCount = 0;
    let falloffSampleCount = 0;
    for (let localY = 0; localY < aura.rows; localY += 1) {
        const worldY = geometry.originY + (aura.startRow + localY) * geometry.cellSize;
        for (let gx = 0; gx < aura.cols; gx += 1) {
            const worldX = geometry.originX + gx * geometry.cellSize;
            const field = state.poolAuraFieldAt(worldX, worldY);
            const pixelIndex = (localY * aura.cols + gx) * 4;
            const alpha = pixels[pixelIndex + 3];
            if (field >= 1) {
                insideCount += 1;
                assert.equal(alpha, 255,
                    'opaque D9 backing avoids bilinear white seams and is covered by the later contour fills');
                assert.deepEqual(Array.from(pixels.slice(pixelIndex, pixelIndex + 3)), [217, 194, 175]);
            } else {
                const distance = distanceInCells[localY * aura.cols + gx] * geometry.cellSize;
                const expectedAlpha = distance >= aura.height ? 0 : Math.round((1 - distance / aura.height) * 255);
                assert.equal(alpha, expectedAlpha, 'exterior alpha falls off linearly over its measured 2D distance');
                if (alpha > 0) {
                    litExteriorCount += 1;
                    assert.deepEqual(Array.from(pixels.slice(pixelIndex, pixelIndex + 3)), [217, 194, 175],
                        'the cached aura keeps the agreed caramel RGB with alpha only in the exterior');
                    falloffSampleCount += 1;
                } else {
                    fadedExteriorCount += 1;
                }
            }
        }
    }
    assert.ok(insideCount > 100, 'the raster contains the source pool field');
    assert.ok(litExteriorCount > 100, 'the aura wraps the pool and its concave exterior contours');
    assert.ok(fadedExteriorCount > 100, 'the aura fades to transparent outside its fixed radius');
    assert.ok(falloffSampleCount > 100);

    const clearCall = fixture.metrics.drawCalls.findIndex((call) => call.type === 'clearRect'
        && call.width === 1100 && call.height === 900);
    const putCall = fixture.metrics.drawCalls.findIndex((call) => call.type === 'putImageData');
    const auraCall = fixture.metrics.drawCalls.findIndex((call) => call.type === 'drawImage');
    const outerCall = fixture.metrics.drawCalls.findIndex((call) => call.type === 'fill'
        && call.fillStyle === '#D9C2AF');
    const coreCall = fixture.metrics.drawCalls.findIndex((call) => call.type === 'fill'
        && call.fillStyle === fixture.metrics.gradients[0]);
    assert.ok(putCall < clearCall && clearCall < auraCall && auraCall < outerCall && outerCall < coreCall,
        'the pool-only raster is built before clear, then drawn before both original opaque fluid contours');
    assert.equal(fixture.metrics.fillPaths.length, 2, 'the aura adds no contour pass or shape change');
    assert.equal(fixture.metrics.fillStates[0].fillStyle, '#D9C2AF');
    assert.equal(fixture.metrics.fillStates[1].globalAlpha, 1, 'the warm-white core remains fully opaque');
    assert.equal(fixture.metrics.clips, 0, 'the external aura remains outside the fluid fills');

    const cachedImage = state.poolAuraImageData;
    fixture.fireNextFrame(34);
    assert.equal(fixture.metrics.imageDataCreates.length, 1, 'the 30 Hz renderer reuses the raster allocation');
    assert.equal(controller.__inspect().poolAuraImageData, cachedImage);
    assert.equal(fixture.metrics.putImageDataCalls.length, 2);
    assert.equal(fixture.metrics.drawImageCalls.length, 2, 'each fluid frame draws one bounded raster');
    controller.dispose();
});

test('pool aura includes blade grooves, excludes positive blobs, and follows viscous pool movement', () => {
    function renderScenario(scenario = 'base') {
        const fixture = createFixture({ width: 1100, height: 900, inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1100, 900, 292, 0);
        controller.start();
        const setup = controller.__inspect();
        const sampleX = 550;
        const surfaceY = setup.poolSurfaceAt(sampleX);
        const sampleY = surfaceY + 40;
        let bodyY = 250;
        if (scenario === 'detached') {
            setup.setBlobForTest(0, sampleX, bodyY, 58);
        } else if (scenario === 'connected') {
            bodyY = surfaceY + 25;
            setup.setBlobForTest(0, sampleX, bodyY, 58);
        } else if (scenario === 'blade') {
            setup.setBladeForTest(sampleX, sampleY, 28);
        } else if (scenario === 'viscous') {
            setup.setPoolViscousForTest(Array(20).fill(24));
        } else if (scenario === 'pointer-inward') {
            moveMouseAt(fixture, sampleX, surfaceY - 30, 0);
            moveMouseAt(fixture, sampleX, surfaceY + 30, 60);
            assert.equal(controller.__inspect().blobCount, 5,
                'a downward inward pool stroke both cuts the field and extrudes a rising body');
        }
        fixture.fireNextFrame(0);
        const state = controller.__inspect();
        const auraGeometry = state.poolAuraGeometry;
        const gridGeometry = state.gridGeometry;
        const alpha = state.poolAuraAlphaSnapshot();
        const field = state.poolAuraFieldSnapshot();
        const readAt = (buffer, x, y) => {
            const gx = Math.round((x - gridGeometry.originX) / gridGeometry.cellSize);
            const localY = Math.round((y - gridGeometry.originY) / gridGeometry.cellSize) - auraGeometry.startRow;
            if (gx < 0 || gx >= auraGeometry.cols || localY < 0 || localY >= auraGeometry.rows) return null;
            return buffer[localY * auraGeometry.cols + gx];
        };
        const result = {
            alpha,
            field,
            fieldAtCenter: readAt(field, sampleX, sampleY),
            auraAtCenter: readAt(alpha, sampleX, sampleY),
            surfaceY,
            renderedSurfaceY: state.poolSurfaceAt(sampleX),
            sampleY,
            gridAtCenter: state.gridAt(sampleX, sampleY),
            inwardGrooveGrid: state.gridAt(sampleX, surfaceY + 30),
            bodyField: state.gridAt(sampleX, bodyY),
            geometry: auraGeometry,
            gridGeometry,
            alphaAt: (x, y) => readAt(alpha, x, y),
            fieldAt: (x, y) => readAt(field, x, y),
        };
        controller.dispose();
        return result;
    }

    const base = renderScenario();
    const detached = renderScenario('detached');
    const connected = renderScenario('connected');
    const blade = renderScenario('blade');
    const viscous = renderScenario('viscous');
    const pointerInward = renderScenario('pointer-inward');
    assertByteArraysEqual(detached.alpha, base.alpha,
        'a detached positive blob cannot change the pool-only aura field');
    assertByteArraysEqual(connected.alpha, base.alpha,
        'a gestating positive bridge cannot pull or reshape the pool-only aura');
    assert.ok(detached.bodyField > 1 && connected.bodyField > 1,
        'both positive bodies still exist in the final fluid field');
    assert.notDeepEqual(blade.alpha, base.alpha,
        'the temporary negative field changes the exterior aura around an inward pool cut');
    assert.notDeepEqual(pointerInward.alpha, base.alpha,
        'a real downward pointer sweep applies the same negative cut to the cached exterior aura');
    assert.ok(base.fieldAtCenter >= 1, 'the probe starts inside the uncut pool field');
    assert.ok(blade.fieldAtCenter < 1, 'the same pool field is carved below threshold by the blade');
    assert.ok(blade.gridAtCenter < 1, 'the final combined field keeps the real negative cut');
    const inwardGrooveY = pointerInward.surfaceY + 30;
    assert.ok(pointerInward.fieldAt(550, inwardGrooveY) < base.fieldAt(550, base.surfaceY + 30),
        'the pointer-created blade samples subtract from the pool aura field along the inward path');
    assert.ok(pointerInward.inwardGrooveGrid < base.inwardGrooveGrid,
        'the pointer-created inward groove appears in the final fluid field');

    let notchAuraFound = false;
    const { originX, originY, cellSize } = base.gridGeometry;
    for (let y = blade.sampleY - 60; y <= blade.sampleY + 60 && !notchAuraFound; y += cellSize) {
        for (let x = 490; x <= 610; x += cellSize) {
            if (base.fieldAt(x, y) >= 1 && blade.fieldAt(x, y) < 1
                && blade.alphaAt(x, y) > 0) {
                notchAuraFound = true;
                break;
            }
        }
    }
    assert.ok(notchAuraFound,
        'aura follows the exterior of the actual negative notch instead of hanging above it');

    assert.notDeepEqual(viscous.alpha, base.alpha, 'a viscous pool deformation moves its aura raster');
    assert.ok(Math.abs(viscous.renderedSurfaceY - base.renderedSurfaceY - 24) < 1e-9,
        'a +24 source-unit viscous offset retains its full +24 CSS-pixel movement');
    const firstLitY = (scenario) => {
        for (let y = scenario.geometry.top; y < 899; y += cellSize) {
            if (scenario.alphaAt(550, y) > 0) return y;
        }
        return null;
    };
    const auraSurfaceShift = firstLitY(viscous) - firstLitY(base);
    assert.ok(Math.abs(auraSurfaceShift - 24) <= cellSize,
        `the exterior distance raster follows the pool shift within one grid cell (got ${auraSurfaceShift}px)`);
    assert.ok(originX < 0 && originY < 0, 'the sampled distance field retains grid padding at content edges');
});

test('passive mouse sweeps split a visible contour with area conservation and bounded transient FX', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const before = controller.__inspect();
    const target = before.blobs.reduce((highest, blob) => blob.y < highest.y ? blob : highest);
    const originalArea = target.radius * target.radius;
    cutAcrossBlob(fixture, target);

    const cut = controller.__inspect();
    assert.equal(cut.blobCount, before.blobCount + 1, 'one visible blob becomes two children');
    const parent = cut.blobs.find((blob) => blob.sliceCooldown > 0);
    const child = cut.blobs.at(-1);
    const areaRatio = (parent.radius * parent.radius + child.radius * child.radius) / originalArea;
    assert.ok(areaRatio >= 1.025 && areaRatio <= 1.035, `split radii should conserve area (ratio ${areaRatio})`);
    assert.ok(parent.vx !== 0 || parent.vy !== 0, 'the cut gives the children a brief separating impulse');
    assert.ok(cut.bladeCount > 0 && cut.bladeCount <= 24);
    assert.ok(cut.sparkCount > 0 && cut.sparkCount <= 48, 'the cut adds bounded warm microdroplets');
    assert.ok(cut.trailCount > 1 && cut.trailCount <= 22, 'the pointer leaves a short bounded trail');

    fixture.fireNextFrame(0);
    const blade = cut.bladeNodes[0];
    assert.ok(controller.__inspect().gridAt(blade.x, blade.y) < 0,
        'the temporary negative blade field opens a clear contour slit');
    assert.ok(fixture.metrics.arcs > 0, 'cut highlights and warm droplets render as small local circles');
    assert.ok(fixture.metrics.lines > 0, 'the caramel and pale pointer trails render as fine strokes');
    assert.ok(fixture.metrics.strokeStyles.includes('rgba(154, 99, 63, 0.78)'),
        'the warm trail uses the SVG-matched caramel brown');
    assert.ok(fixture.metrics.fillStates.some(({ fillStyle }) => fillStyle === '#D9C2AF'),
        'warm microdroplets use the same light outer-skin color');
    assert.ok(fixture.metrics.clips > 0, 'warm cut light is clipped to the outer fluid contour');

    const countAfterCut = controller.__inspect().blobCount;
    const reach = target.radius * 1.45;
    moveMouse(fixture, target.x - reach, target.y);
    assert.equal(controller.__inspect().blobCount, countAfterCut, 'the split cooldown suppresses an immediate repeat swipe');
    assert.ok(controller.__inspect().bladeCount <= 24);
    assert.ok(controller.__inspect().sparkCount <= 48);
    assert.ok(controller.__inspect().trailCount <= 22);
    controller.dispose();
});

test('long single-event cuts split 30, 50, and 65px bodies across horizontal, vertical, diagonal, and edge paths', () => {
    const cases = [
        { radius: 30, center: { x: 550, y: 260 }, angle: 0 },
        { radius: 50, center: { x: 550, y: 260 }, angle: Math.PI / 2 },
        { radius: 65, center: { x: 550, y: 260 }, angle: Math.PI / 4 },
        { radius: 30, center: { x: 90, y: 260 }, angle: Math.PI / 2 },
    ];

    for (const scenario of cases) {
        const fixture = createFixture({ inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1100, 900, 292, 0);
        controller.start();
        const before = controller.__inspect();
        setTestBlob(controller, scenario.radius, scenario.center.x, scenario.center.y);
        sendStroke(fixture, scenario.center, scenario.radius, scenario.angle, 55);

        const cut = controller.__inspect();
        assert.equal(cut.blobCount, before.blobCount + 1,
            `${scenario.radius}px body should split on a long single pointer event`);
        assert.equal(cut.blobs.filter((blob) => blob.separationCooldown > 0).length, 2,
            'both split bodies enter bounded sibling separation protection');
        assert.ok(cut.bladeCount >= 4 && cut.bladeCount <= 24,
            'one intersected body receives a dense, locally clipped negative blade');

        fixture.fireNextFrame(0);
        const blade = controller.__inspect().bladeNodes.reduce((closest, node) => {
            const distance = Math.hypot(node.x - scenario.center.x, node.y - scenario.center.y);
            return distance < closest.distance ? { ...node, distance } : closest;
        }, { distance: Infinity });
        assert.ok(controller.__inspect().gridAt(blade.x, blade.y) < 0,
            'the sampled blade capsule cuts the real scalar field at the body');
        controller.dispose();
    }
});

test('small high-polling pointer moves accumulate intent and cut the same path as slower events', () => {
    function runPath(stepCount, stepDistance, stepTimeMs) {
        const fixture = createFixture({ inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1100, 900, 292, 0);
        controller.start();
        const before = controller.__inspect();
        setTestBlob(controller, 50, 550, 260);
        moveMouseAt(fixture, 130, 260, 0);
        for (let index = 1; index <= stepCount; index += 1) {
            moveMouseAt(fixture, 130 + index * stepDistance, 260, index * stepTimeMs);
        }
        const after = controller.__inspect();
        assert.equal(after.blobCount, before.blobCount + 1,
            `${stepDistance}px pointer samples should combine into a visible split`);
        const pair = after.blobs.filter((blob) => blob.separationCooldown > 0);
        assert.equal(pair.length, 2);
        assert.ok(pair.every((blob) => blob.radius > 0));
        controller.dispose();
    }

    runPath(420, 2, 1);
    runPath(160, 5.25, 8.75);
});

test('split sibling scalar fields stay visibly separate after the blade fades for 3.2 seconds', () => {
    for (const radius of [30, 50, 65]) {
        const fixture = createFixture({ inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1100, 900, 292, 0);
        controller.start();
        const before = controller.__inspect();
        const center = { x: 200, y: 440 };
        const target = setTestBlob(controller, radius, center.x, center.y);
        sendStroke(fixture, center, radius, 0, 55);
        const splitState = controller.__inspect();
        assert.equal(splitState.blobCount, before.blobCount + 1,
            'the cut adds exactly one body before timed pool gestation begins');
        const siblingIndices = [target.index, splitState.blobs.length - 1];
        fixture.fireNextFrame(0);

        for (const elapsedMs of [700, 1200, 2200, 3200]) {
            advanceFramesTo(fixture, 30, elapsedMs);
            const state = controller.__inspect();
            const siblings = state.blobs.filter((blob) => blob.separationCooldown > 0);
            const pair = elapsedMs < 2600
                ? siblings
                : siblingIndices.map((index) => state.blobs[index]);
            assert.equal(siblings.length, elapsedMs < 2600 ? 2 : 0,
                `${radius}px sibling protection expires at 2.6s (checked at ${elapsedMs}ms)`);
            assert.equal(state.bladeCount, 0, 'the negative blade has faded before long-term separation checks');
            assert.ok(pair.every((blob) => state.gridAt(blob.x, blob.y) >= 1),
                'both child bodies remain inside their rendered scalar contours');
            let valley = Infinity;
            for (let step = 1; step < 8; step += 1) {
                const amount = step / 8;
                const x = pair[0].x + (pair[1].x - pair[0].x) * amount;
                const y = pair[0].y + (pair[1].y - pair[0].y) * amount;
                valley = Math.min(valley, state.gridAt(x, y));
            }
            assert.ok(valley < 1,
                `${radius}px children have a below-contour scalar valley at ${elapsedMs}ms (got ${valley})`);
        }
        const finalState = controller.__inspect();
        assert.equal(finalState.blobCount, before.blobCount + 2,
            'the split pair remains separate while one normal pool body spawns during the 3.2s window');
        const normalSpawn = finalState.blobs.at(-1);
        assert.ok(normalSpawn.birth < 1 && normalSpawn.y > 850,
            'the additional body is a fresh spawn gestating near the bottom of the viewport');
        controller.dispose();
    }
});

test('opposite vertical cuts keep both siblings inside a viewport edge with a scalar-field gap', () => {
    const fixture = createFixture({ width: 1100, height: 900, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const center = { x: 1060, y: 260 };
    const target = setTestBlob(controller, 65, center.x, center.y);
    sendStroke(fixture, center, 65, -Math.PI / 2, 55);
    fixture.fireNextFrame(0);
    advanceFramesTo(fixture, 30, 800);

    const state = controller.__inspect();
    const pair = [state.blobs[target.index], state.blobs.at(-1)];
    assert.ok(pair.every((blob) => Number.isFinite(blob.x) && Number.isFinite(blob.y)
        && Number.isFinite(blob.radius)), 'both near-edge child bodies retain finite scalar geometry');
    for (const blob of pair) {
        const pad = blob.radius * 0.8;
        assert.ok(blob.x >= pad - 1e-6 && blob.x <= 1100 - pad + 1e-6,
            `each visible child center remains within the viewport with its wall pad (x=${blob.x})`);
        assert.ok(blob.y >= -blob.radius && blob.y <= 900 + blob.radius,
            'each child remains in the finite vertical field around the viewport');
        assert.ok(state.gridAt(blob.x, blob.y) >= 1, 'each child retains a rendered scalar body');
    }
    assert.ok(pair[0].x > pair[1].x,
        'an upward vertical slash keeps the reversed normal ordering through edge clamping');
    assert.equal(state.bladeCount, 0, 'the visible field gap persists after the temporary blade has faded');
    let valley = Infinity;
    for (let step = 1; step < 8; step += 1) {
        const amount = step / 8;
        valley = Math.min(valley, state.gridAt(
            pair[0].x + (pair[1].x - pair[0].x) * amount,
            pair[0].y + (pair[1].y - pair[0].y) * amount,
        ));
    }
    assert.ok(valley < 1, `near-edge siblings retain a below-contour valley after clamping (got ${valley})`);
    controller.dispose();
});

test('the brief recut cooldown expires while a newly cut pair receives fresh separation protection', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const center = { x: 550, y: 260 };
    setTestBlob(controller, 65, center.x, center.y);
    sendStroke(fixture, center, 65, 0, 55);
    fixture.fireNextFrame(0);
    advanceFramesTo(fixture, 30, 700);

    const firstPair = controller.__inspect().blobs.filter((blob) => blob.separationCooldown > 0);
    assert.equal(firstPair.length, 2);
    const target = firstPair.reduce((larger, blob) => blob.radius > larger.radius ? blob : larger);
    const countBeforeRecut = controller.__inspect().blobCount;
    fixture.emitWindow('pointerleave');
    const reach = target.radius * 1.5;
    moveMouseAt(fixture, target.x - reach, target.y, 760);
    moveMouseAt(fixture, target.x + reach, target.y, 810);

    const recut = controller.__inspect();
    assert.equal(recut.blobCount, countBeforeRecut + 1,
        'the original 0.56s cut cooldown expires while its sibling gap remains protected');
    const freshPair = recut.blobs.filter((blob) => blob.separationCooldown > 0);
    assert.equal(freshPair.length, 2, 'recutting replaces the old reciprocal pair with one bounded new pair');
    assert.ok(freshPair.every((blob) => blob.sliceCooldown < blob.separationCooldown),
        'recut suppression and postcut visual separation use independent timers');
    controller.dispose();
});

test('deformed visible lobes are hittable while an outside graze keeps its blade without splitting', () => {
    const shape = {
        orientation: 0.48,
        cutDeform2X: 0.62,
        cutDeform2Y: -0.35,
        cutDeform3X: 0.22,
        cutDeform3Y: -0.16,
    };
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const center = setTestBlob(controller, 50, 550, 260, shape);
    const state = controller.__inspect();
    state.renderFrameForTest();
    const cellSize = state.gridGeometry.cellSize;
    let visibleEdge = null;
    for (let y = center.y - 95; y <= center.y + 95; y += cellSize) {
        for (let x = center.x - 95; x <= center.x + 95; x += cellSize) {
            const distance = Math.hypot(x - center.x, y - center.y);
            if (distance > center.radius * 0.86 && distance < center.radius * 1.8
                && state.gridAt(x, y) >= 1 && (!visibleEdge || distance > visibleEdge.distance)) {
                visibleEdge = { x, y, distance };
            }
        }
    }
    assert.ok(visibleEdge, 'the deformed body renders a visible contour beyond the old inner-radius hit test');

    const radialX = (visibleEdge.x - center.x) / visibleEdge.distance;
    const radialY = (visibleEdge.y - center.y) / visibleEdge.distance;
    const tangentX = -radialY;
    const tangentY = radialX;
    const halfStroke = center.radius * 1.15;
    moveMouseAt(fixture, visibleEdge.x - tangentX * halfStroke, visibleEdge.y - tangentY * halfStroke, 0);
    moveMouseAt(fixture, visibleEdge.x + tangentX * halfStroke, visibleEdge.y + tangentY * halfStroke, 60);
    assert.equal(controller.__inspect().blobCount, state.blobCount + 1,
        'a stroke through the actual deformed scalar contour splits the body');
    controller.dispose();

    const grazeFixture = createFixture({ inspectInternals: true });
    const grazeController = grazeFixture.createController();
    grazeController.resize(1100, 900, 292, 0);
    grazeController.start();
    const grazeCenter = setTestBlob(grazeController, 50, 550, 260, shape);
    const grazeBefore = grazeController.__inspect().blobCount;
    const farX = grazeCenter.x + radialX * grazeCenter.radius * 1.55;
    const farY = grazeCenter.y + radialY * grazeCenter.radius * 1.55;
    moveMouseAt(grazeFixture, farX - tangentX * halfStroke, farY - tangentY * halfStroke, 0);
    moveMouseAt(grazeFixture, farX + tangentX * halfStroke, farY + tangentY * halfStroke, 60);
    const grazed = grazeController.__inspect();
    assert.equal(grazed.blobCount, grazeBefore,
        'a true outside graze leaves the four initial bodies unsplit');
    assert.ok(grazed.bladeCount > 0 && grazed.bladeCount <= 24,
        'the continuous blade field follows an outside graze even without a blob cut');
    assert.ok(grazed.bladeNodes.some((node) => Math.hypot(node.x - farX, node.y - farY) < halfStroke),
        'generic blade samples remain on the non-generative stroke away from the pool');
    grazeController.dispose();
});

test('successive 7px pointer segments each add a continuous blade sample', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();

    moveMouseAt(fixture, 400, 100, 0);
    moveMouseAt(fixture, 407, 100, 16);
    const first = controller.__inspect();
    moveMouseAt(fixture, 414, 100, 32);
    const second = controller.__inspect();

    assert.equal(first.bladeCount, 1, 'the first processed 7px segment injects one generic blade node');
    assert.equal(second.bladeCount, 2, 'the next 7px segment is not suppressed by a 28px spacing gate');
    assert.equal(second.bladeNodes[0].x, 407);
    assert.equal(second.bladeNodes[1].x, 414);
    assert.equal(second.bladeNodes[0].y, 100);
    assert.equal(second.bladeNodes[1].y, 100);
    assert.equal(second.blobCount, first.blobCount, 'air-only blade samples do not extrude positive blobs');
    controller.dispose();
});

test('bottom-pool slash carves a ramped blob and a negative-field groove with viscous deflection', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const before = controller.__inspect();
    assert.equal(before.poolAuraReady, false,
        'the first pointer gesture exercises the bounded field-formula fallback before any RAF render');
    const x = 236;
    const surfaceY = before.poolSurfaceAt(x);
    moveMouse(fixture, x - 12, surfaceY + 30);
    moveMouse(fixture, x + 12, surfaceY - 30);

    const carved = controller.__inspect();
    assert.equal(carved.blobCount, before.blobCount + 1, 'the upward slash extrudes one fresh pool blob');
    assert.ok(carved.blobs.at(-1).carvePhase > 0 && carved.blobs.at(-1).carvePhase < 1,
        'the carved body enters through the reference linear emergence ramp');
    assert.ok(carved.bladeCount >= 4 && carved.bladeCount <= 24, 'the blade and pool groove remain bounded');
    assert.ok(carved.poolTargetOffsets.some((offset) => Math.abs(offset) > 0.01),
        'the slash nudges the pool through its viscous target offsets');
    const groove = carved.bladeNodes.reduce((smallest, node) => node.radius < smallest.radius ? node : smallest);

    fixture.fireNextFrame(0);
    assert.ok(controller.__inspect().gridAt(groove.x, groove.y) < 0,
        'the groove nodes subtract from the scalar field below the pool crest');
    moveMouse(fixture, x - 12, surfaceY + 30);
    assert.equal(controller.__inspect().blobCount, carved.blobCount,
        'a second reverse stroke does not immediately carve again');
    controller.dispose();
});

test('bottom-pool cuts use the whole swept segment for vertical strokes and crest skims', () => {
    const scenarios = [
        {
            name: 'long downward stroke whose midpoint stays above the visible pool',
            x: 180,
            startOffset: -180,
            endOffset: 61,
            timeMs: 160,
        },
        {
            name: 'pure vertical upward stroke',
            x: 680,
            startOffset: 61,
            endOffset: -70,
            timeMs: 120,
        },
        {
            name: 'horizontal crest skim',
            horizontal: true,
            timeMs: 180,
        },
    ];

    for (const scenario of scenarios) {
        const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1600, 1000, 292, 0);
        controller.start();
        fixture.fireNextFrame(0);
        const before = controller.__inspect();
        const anchorX = scenario.horizontal ? 780 : scenario.x;
        const visibleSurfaceY = before.poolCutSurfaceAt(anchorX);
        assert.ok(Number.isFinite(visibleSurfaceY), `${scenario.name} has a cached pool contour`);
        if (scenario.x === 180) {
            assert.ok(Math.abs(visibleSurfaceY - before.poolSurfaceAt(anchorX)) > 10,
                'the test contour differs materially from the analytic surface estimate');
            assert.ok((scenario.startOffset + scenario.endOffset) / 2
                + visibleSurfaceY - before.poolSurfaceAt(anchorX) < -24,
            'the long downward stroke midpoint misses the old analytic depth gate');
        }

        let startX;
        let startY;
        let endX;
        let endY;
        if (scenario.horizontal) {
            startX = 160;
            endX = 1440;
            startY = endY = visibleSurfaceY;
        } else {
            startX = endX = scenario.x;
            startY = Math.max(0, Math.min(999, visibleSurfaceY + scenario.startOffset));
            endY = Math.max(0, Math.min(999, visibleSurfaceY + scenario.endOffset));
        }
        moveMouseAt(fixture, startX, startY, 0);
        moveMouseAt(fixture, endX, endY, scenario.timeMs);

        const carved = controller.__inspect();
        assert.equal(carved.blobCount, before.blobCount + 1,
            `${scenario.name} extrudes one pool body; segment=${startY}->${endY}, surface=${visibleSurfaceY}, hit=${JSON.stringify(carved.poolCutState())}`);
        assert.ok(carved.bladeCount >= 4 && carved.bladeCount <= 24,
            `${scenario.name} keeps its negative groove within the transient cap`);
        const hit = carved.poolCutState();
        assert.ok(Math.abs(hit.hitSurfaceY - carved.poolCutSurfaceAt(hit.hitX)) < 0.01,
            `${scenario.name} anchors the body to the sampled pool-only contour`);
        assert.ok(hit.hitT >= 0 && hit.hitT <= 1, `${scenario.name} hit lies on the swept segment`);
        assert.ok(hit.sampleCount <= 256, `${scenario.name} uses bounded segment samples`);
        const body = carved.blobs.at(-1);
        assert.ok(Math.abs(body.y - Math.min(hit.hitY, hit.hitSurfaceY + body.radius * 0.15)) < 0.01,
            `${scenario.name} starts at its contact edge instead of teleporting above the contour`);
        assert.ok(body.y >= hit.hitSurfaceY - 0.25 && body.vy <= -72,
            `${scenario.name} remains attached to the contour with the reference upward floor`);

        fixture.fireNextFrame(33.4);
        const groove = controller.__inspect().bladeNodes.reduce((closest, node) => {
            const distance = Math.hypot(node.x - hit.hitX, node.y - hit.hitSurfaceY);
            return distance < closest.distance ? { ...node, distance } : closest;
        }, { distance: Infinity });
        assert.ok(groove.distance < 40, `${scenario.name} places negative blades along the real hit`);
        assert.ok(controller.__inspect().gridAt(groove.x, groove.y) < 0,
            `${scenario.name} subtracts the combined field at its groove`);
        controller.dispose();
    }
});

test('bottom-pool cuts accept accumulated 2px input and follow viscous contour movement', () => {
    const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1600, 1000, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);
    let state = controller.__inspect();
    const x = 180;
    const originalSurface = state.poolCutSurfaceAt(x);
    state.setPoolViscousForTest(Array(20).fill(24));
    state.renderFrameForTest();
    state = controller.__inspect();
    const movedSurface = state.poolCutSurfaceAt(x);
    assert.ok(Math.abs(movedSurface - originalSurface - 24) < state.poolAuraGeometry.cellSize,
        'the hit contour follows the viscous surface rendered into the cached pool-only field');

    moveMouseAt(fixture, x, movedSurface - 48, 0);
    for (let index = 1; index <= 52; index += 1) {
        moveMouseAt(fixture, x, movedSurface - 48 + index * 2, index * 8);
    }
    const carved = controller.__inspect();
    assert.equal(carved.blobCount, state.blobCount + 1,
        '2px high-polling samples accumulate into one downward pool cut');
    assert.ok(Math.abs(carved.poolCutState().hitSurfaceY - movedSurface) < 10,
        'the accumulated stroke anchors to the moved contour');
    assert.ok(carved.poolSliceCooldown > 0, 'the pool cut starts its existing repeat-swipe cooldown');
    controller.dispose();
});

test('a near-crest air graze cuts the pool edge without extruding a detached body', () => {
    const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1600, 1000, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);
    const before = controller.__inspect();
    const x = 780;
    const surfaceY = before.poolCutSurfaceAt(x);
    const grazeY = surfaceY - 12;
    assert.ok(before.poolCutFieldAt(x, grazeY) < 1,
        'the reference graze line stays above the actual pool material threshold');

    moveMouseAt(fixture, x - 30, grazeY, 0);
    moveMouseAt(fixture, x + 30, grazeY, 60);
    fixture.fireNextFrame(33.4);
    const grazed = controller.__inspect();
    assert.equal(grazed.blobCount, before.blobCount,
        'a stroke that stays 12px above the rendered contour cannot create a positive blob');
    assert.ok(grazed.bladeNodes.some((node) => Math.abs(node.y - grazeY) < 0.01),
        'the blade remains injected along the pool-edge graze');
    assert.ok(grazed.poolCutFieldAt(x, surfaceY) < 1,
        'the same negative blade field cuts the pool edge during the non-generative graze');
    controller.dispose();
});

test('bottom-pool launch radius and horizontal impulse follow timestamp speed, not segment distance', () => {
    function carveForElapsed(elapsedMs) {
        const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1600, 1000, 292, 0);
        controller.start();
        fixture.fireNextFrame(0);
        const state = controller.__inspect();
        const x = 780;
        const surfaceY = state.poolCutSurfaceAt(x);
        moveMouseAt(fixture, x - 12, surfaceY - 12, 0);
        moveMouseAt(fixture, x + 12, surfaceY + 12, elapsedMs);
        const carved = controller.__inspect().blobs.at(-1);
        controller.dispose();
        return carved;
    }

    const fast = carveForElapsed(16);
    const slow = carveForElapsed(800);
    assert.ok(fast.radius > slow.radius * 1.4,
        'the same 24px crossing grows a larger body when its timestamp speed is higher');
    assert.ok(fast.vx > slow.vx,
        'the faster timestamp-derived swipe gives the body more horizontal launch under the reference direction ratios');
    assert.ok(fast.vy <= -72 && slow.vy <= -72,
        'both timing profiles retain the reference upward buoyancy floor');
});

test('bottom-pool sky and deep misses, cooldown, and the hard cap suppress extrusion', () => {
    function prepare() {
        const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
        const controller = fixture.createController();
        controller.resize(1600, 1000, 292, 0);
        controller.start();
        fixture.fireNextFrame(0);
        return { fixture, controller, state: controller.__inspect() };
    }

    for (const offset of [-90, 66]) {
        const { fixture, controller, state } = prepare();
        let x = 180;
        let surfaceY = state.poolCutSurfaceAt(x);
        if (offset > 0) {
            for (let candidateX = 80; candidateX < 1520; candidateX += 80) {
                const candidateSurfaceY = state.poolCutSurfaceAt(candidateX);
                if (Number.isFinite(candidateSurfaceY) && candidateSurfaceY < surfaceY) {
                    x = candidateX;
                    surfaceY = candidateSurfaceY;
                }
            }
        }
        const y = offset < 0 ? surfaceY + offset : 999;
        moveMouseAt(fixture, x, y, 0);
        moveMouseAt(fixture, x, y + (offset < 0 ? 12 : -12), 30);
        assert.equal(controller.__inspect().blobCount, state.blobCount,
            offset < 0 ? 'distant sky strokes do not extrude' : 'deep pool strokes do not extrude');
        controller.dispose();
    }

    {
        const { fixture, controller, state } = prepare();
        const x = 180;
        const surfaceY = state.poolCutSurfaceAt(x);
        moveMouseAt(fixture, x, surfaceY - 64, 0);
        moveMouseAt(fixture, x, 999, 140);
        const first = controller.__inspect();
        assert.equal(first.blobCount, state.blobCount + 1, 'a real pool crossing carves once');
        assert.ok(Math.abs(first.poolSliceCooldown - 0.16) < 1e-9,
            'the positive pool slice uses the reference 0.16 second cooldown');
        fixture.emitWindow('pointerleave');
        moveMouseAt(fixture, x, 999, 180);
        moveMouseAt(fixture, x, surfaceY - 64, 220);
        const duringCooldown = controller.__inspect();
        assert.equal(duringCooldown.blobCount, first.blobCount,
            'the pool cooldown rejects an immediate reverse crossing');
        assert.ok(duringCooldown.bladeCount > first.bladeCount,
            'the negative field continues through the pool while positive extrusion is on cooldown');
        fixture.fireNextFrame(33.4);
        const cooldownBlade = duringCooldown.bladeNodes
            .filter((node) => Math.abs(node.x - x) < 1)
            .reduce((closest, node) => Math.abs(node.y - surfaceY) < Math.abs(closest.y - surfaceY) ? node : closest);
        assert.ok(controller.__inspect().gridAt(cooldownBlade.x, cooldownBlade.y) < 0,
            'the cooldown stroke still subtracts from the combined pool field');

        advanceFramesTo(fixture, 60, 200);
        assert.equal(controller.__inspect().poolSliceCooldown, 0,
            'the pool cooldown expires after about 0.16 seconds');
        fixture.emitWindow('pointerleave');
        const cooled = controller.__inspect();
        const nextSurface = cooled.poolCutSurfaceAt(x + 120);
        moveMouseAt(fixture, x + 120, nextSurface - 64, 240);
        moveMouseAt(fixture, x + 120, 999, 380);
        assert.equal(controller.__inspect().blobCount, first.blobCount + 1,
            'a real pool contact can extrude again after the reference cooldown expires');
        controller.dispose();
    }

    {
        const { fixture, controller, state } = prepare();
        state.seedBlobsForTest(state.hardLimit);
        const full = controller.__inspect();
        const x = 180;
        const surfaceY = full.poolCutSurfaceAt(x);
        moveMouseAt(fixture, x, surfaceY - 64, 0);
        moveMouseAt(fixture, x, 999, 140);
        const capped = controller.__inspect();
        assert.equal(capped.blobCount, full.hardLimit,
            'pool cuts respect the existing desktop hard cap');
        assert.ok(capped.bladeNodes.some((node) => Math.abs(node.x - x) < 40),
            'the full-cap stroke still injects its pool negative field');
        fixture.fireNextFrame(33.4);
        const capBlade = capped.bladeNodes
            .filter((node) => Math.abs(node.x - x) < 1)
            .reduce((closest, node) => Math.abs(node.y - surfaceY) < Math.abs(closest.y - surfaceY) ? node : closest);
        assert.ok(controller.__inspect().gridAt(capBlade.x, capBlade.y) < 0,
            'the hard cap suppresses positive extrusion without suppressing fluid cutting');
        controller.dispose();
    }
});

test('bottom-pool bodies emerge from the real contact edge and keep rising', () => {
    const fixture = createFixture({ width: 1600, height: 1000, inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1600, 1000, 292, 0);
    controller.start();
    fixture.fireNextFrame(0);
    const before = controller.__inspect();
    const x = 180;
    const surfaceY = before.poolCutSurfaceAt(x);
    moveMouseAt(fixture, x, surfaceY - 64, 0);
    moveMouseAt(fixture, x, 999, 140);
    const justCarved = controller.__inspect().blobs.at(-1);
    const contact = controller.__inspect().poolCutState();
    assert.ok(Math.abs(justCarved.y - Math.min(contact.hitY, contact.hitSurfaceY + justCarved.radius * 0.15)) < 0.01,
        'the new body begins at the actual cut edge');
    assert.ok(justCarved.y >= contact.hitSurfaceY - 0.25,
        'the initial body field remains attached to the pool instead of appearing in air');

    advanceFramesTo(fixture, 30, 1500);
    const emerged = controller.__inspect();
    const body = emerged.blobs.reduce((closest, candidate) => {
        const score = Math.abs(candidate.x - justCarved.x) + Math.abs(candidate.radius - justCarved.radius);
        return !closest || score < closest.score ? { body: candidate, score } : closest;
    }, null).body;
    const currentSurface = emerged.poolCutSurfaceAt(body.x);
    const gapProbeY = (body.y + currentSurface) * 0.5;
    assert.ok(body.carvePhase > 0.99, 'the emergence ramp completes within 1.5 seconds');
    assert.ok(body.y < justCarved.y - 40, 'the carved body continues rising after it leaves the pool');
    assert.ok(currentSurface - body.y > body.radius * 0.8,
        'the body center clears the actual upper contour by more than its visible lower radius');
    assert.ok(emerged.poolAuraFieldAt(body.x, gapProbeY) < 1,
        'a pool-only scalar gap separates the body from the source pool');
    assert.ok(emerged.gridAt(body.x, body.y) > 1,
        'the detached body remains visibly solid in the combined fluid field');
    controller.dispose();
});

test('UI targets, non-mouse events, stop, reduced motion, and dispose cannot leave stale cuts or listeners', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const before = controller.__inspect();
    const target = before.blobs.reduce((highest, blob) => blob.y < highest.y ? blob : highest);
    const reach = target.radius * 1.45;
    const button = { closest(selector) { assert.match(selector, /button/); return this; } };
    moveMouse(fixture, target.x - reach, target.y);
    moveMouse(fixture, target.x, target.y, 292, button);
    moveMouse(fixture, target.x + reach, target.y);
    assert.equal(controller.__inspect().blobCount, before.blobCount,
        'leaving a button starts a fresh pointer segment instead of cutting across the control');
    assert.equal(controller.__inspect().bladeCount, 0);
    assert.equal(controller.__inspect().trailCount, 0);

    fixture.emitWindow('pointermove', {
        clientX: 292 + target.x - reach,
        clientY: target.y,
        pointerType: 'touch',
        target: { closest() { return null; } },
    });
    assert.equal(controller.__inspect().lastPointer, null,
        'touch input is ignored and clears the prior mouse segment');

    controller.stop(false);
    moveMouse(fixture, target.x - reach, target.y);
    moveMouse(fixture, target.x + reach, target.y);
    assert.equal(controller.__inspect().blobCount, before.blobCount, 'stop disables pointer interactions');
    assert.equal(controller.__inspect().lastPointer, null);

    controller.start();
    cutAcrossBlob(fixture, target);
    assert.equal(controller.__inspect().blobCount, before.blobCount + 1);
    const countBeforeHidden = controller.__inspect().blobCount;
    fixture.document.hidden = true;
    fixture.emitDocument('visibilitychange');
    assert.equal(fixture.rafCallbacks.size, 0, 'visibility change stops the animation chain');
    assert.equal(controller.__inspect().lastPointer, null);
    assert.equal(controller.__inspect().bladeCount, 0);
    moveMouse(fixture, target.x - reach, target.y);
    moveMouse(fixture, target.x + reach, target.y);
    assert.equal(controller.__inspect().blobCount, countBeforeHidden, 'hidden pages do not accept pointer cuts');
    fixture.document.hidden = false;
    controller.start();

    fixture.reduceMotion.matches = true;
    controller.stop(true);
    assert.equal(controller.__inspect().bladeCount, 0);
    assert.equal(controller.__inspect().sparkCount, 0);
    assert.equal(controller.__inspect().trailCount, 0);
    assert.equal(controller.__inspect().lastPointer, null);
    assert.equal(fixture.rafCallbacks.size, 0);

    fixture.reduceMotion.matches = false;
    controller.start();
    assert.equal(fixture.rafCallbacks.size, 1);
    controller.dispose();
    assert.equal(fixture.rafCallbacks.size, 0);
    assert.equal([...fixture.window.eventListeners.values()].reduce((count, set) => count + set.size, 0), 0);
    assert.equal([...fixture.document.eventListeners.values()].reduce((count, set) => count + set.size, 0), 0);
    assert.equal(fixture.metrics.listenerAdds.filter(([, type]) => type === 'pointermove')[0][2].passive, true,
        'the global pointer listener is passive');
});

test('resize scales cut children without restoring their pre-cut minimum radius', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    const before = controller.__inspect();
    const target = before.blobs.reduce((highest, blob) => blob.y < highest.y ? blob : highest);
    cutAcrossBlob(fixture, target);
    const afterCut = controller.__inspect();
    const cutChildren = afterCut.blobs.filter((blob) => blob.sliceCooldown > 0);
    assert.equal(cutChildren.length, 2);
    const expectedScale = Math.min(1099 / 1100, 899 / 900);
    controller.resize(1099, 899, 292, 0);
    const afterResize = controller.__inspect().blobs.filter((blob) => blob.sliceCooldown > 0);
    assert.equal(afterResize.length, 2);
    cutChildren.forEach((blob, index) => {
        assert.ok(afterResize[index].radius <= blob.radius + 1e-8,
            'a one-pixel resize must not inflate a sliced child to a fresh-blob minimum');
        assert.ok(Math.abs(afterResize[index].radius - blob.radius * expectedScale) < 1e-8,
            'existing children scale proportionally while retaining their conserved area');
    });
    controller.dispose();
});

test('interaction-created bodies stay under the desktop and mobile hard caps', () => {
    const fixture = createFixture({ inspectInternals: true });
    const controller = fixture.createController();
    controller.resize(1100, 900, 292, 0);
    controller.start();
    let state = controller.__inspect();
    state.seedBlobsForTest(state.hardLimit);
    state = controller.__inspect();
    assert.equal(state.blobCount, 20);
    cutAcrossBlob(fixture, state.blobs[0]);
    assert.equal(controller.__inspect().blobCount, 20, 'cuts are skipped cleanly while the desktop cap is full');

    controller.resize(650, 800, 0, 0);
    state = controller.__inspect();
    assert.equal(state.blobCount, 5, 'crossing into mobile viewport trims to its normal sparse population');
    assert.equal(state.hardLimit, 12, 'later mobile interactions can still use bounded headroom');
    state.seedBlobsForTest(state.hardLimit);
    state = controller.__inspect();
    assert.equal(state.blobCount, 12);
    cutAcrossBlob(fixture, state.blobs[0], 0);
    assert.equal(controller.__inspect().blobCount, 12, 'cuts are skipped cleanly while the mobile cap is full');
    controller.dispose();
});
