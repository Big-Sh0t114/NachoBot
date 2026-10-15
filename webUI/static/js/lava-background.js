(function () {
    'use strict';

    const LAYER_ID = 'lava-background-layer';
    const CANVAS_ID = 'lava-background-canvas';
    const MAX_GRID_CELLS = 76000;
    const MAX_LOOP_POINTS = 4096;
    const UPDATE_INTERVAL_MS = 1000 / 30;
    const DESKTOP_BLOB_LIMIT = 8;
    const MOBILE_BLOB_LIMIT = 5;
    const DESKTOP_BLOB_HARD_LIMIT = 20;
    const MOBILE_BLOB_HARD_LIMIT = 12;
    const MAX_BLADE_NODES = 24;
    const MAX_SPARKS = 48;
    const MAX_TRAIL_POINTS = 22;
    const MAX_BLOB_HIT_SAMPLES = 40;
    const MAX_POOL_HIT_SAMPLES = 256;
    const POINTER_CUT_MIN_DISTANCE = 7;
    const SIBLING_SEPARATION_SECONDS = 2.6;
    const SIBLING_SEPARATION_FACTOR = 1.6;
    const SIBLING_SEPARATION_SPEED = 420;
    const POOL_CUT_FIELD_THRESHOLD = 1;
    const POOL_CUT_FIELD_TOLERANCE = 0.005;
    const POOL_CUT_SURFACE_TOLERANCE = 0.25;
    const POOL_CUT_SHELL_BELOW = 48;
    const POOL_CUT_CONTOUR_SCAN_STEPS = 48;
    const POOL_CUT_REFINE_STEPS = 6;
    const MAX_POOL_GROOVE_NODES = 12;
    const INTERACTIVE_TARGETS = 'input, textarea, button, a, select, option, label, dialog, '
        + '[role="button"], [role="dialog"], [role="menu"], [role="slider"], '
        + '[contenteditable="true"], #sidebar, .sidebar, #mini-player, #player-controls, '
        + '#bgm-playlist, #audio-widget, .audio-widget, .audio-player, .bgm-player, .bgm-panel';
    // Premixed from the SVG's #9A633F at 0.34 opacity over the warm #FAF3E8 backdrop,
    // keeping opaque lava contours close to the artwork's displayed caramel tone.
    const OUTER_COLOR = '#D9C2AF';
    const OUTER_SHADOW_COLOR = 'rgba(217, 194, 175, 0.25)';
    const TRAIL_OUTER_COLOR = 'rgba(154, 99, 63, 0.78)';
    const CORE_COLORS = ['#FFFEFA', '#FFF8EF'];
    const POOL_AURA_HEIGHT_RATIO = 0.025;
    const POOL_AURA_MIN_HEIGHT = 15;
    const POOL_AURA_MAX_HEIGHT = 22.5;
    const POOL_AURA_R = 217;
    const POOL_AURA_G = 194;
    const POOL_AURA_B = 175;
    const POOL_AURA_MAX_BLADE_REACH = 96;
    const POOL_VERTICAL_SCALE = 2 / 3;

    function clamp(value, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, value));
    }

    function smoothstep(value) {
        const t = clamp(value, 0, 1);
        return t * t * (3 - 2 * t);
    }

    function smootherstep(value) {
        const t = clamp(value, 0, 1);
        return t * t * t * (t * (t * 6 - 15) + 10);
    }

    function randomBetween(minimum, maximum) {
        return minimum + Math.random() * (maximum - minimum);
    }

    function makeNoopController() {
        return {
            resize() {},
            start() {},
            stop() {},
            dispose() {},
        };
    }

    function create() {
        if (!document.body || document.getElementById(LAYER_ID)) return makeNoopController();

        const layer = document.createElement('div');
        layer.id = LAYER_ID;
        layer.setAttribute('aria-hidden', 'true');

        const canvas = document.createElement('canvas');
        canvas.id = CANVAS_ID;
        canvas.setAttribute('aria-hidden', 'true');
        layer.appendChild(canvas);
        document.body.insertBefore(layer, document.body.firstChild);

        let context = null;
        try {
            context = canvas.getContext('2d', { alpha: true, desynchronized: true });
        } catch (error) {
            context = null;
        }
        if (!context) {
            layer.remove();
            return makeNoopController();
        }

        const reduceMotionQuery = window.matchMedia
            ? window.matchMedia('(prefers-reduced-motion: reduce)')
            : { matches: false };
        const poolNodes = [];
        const blobs = [];
        const bladeNodes = [];
        const sparks = [];
        const trail = [];
        const segmentResult = { distance: 0, t: 0, x: 0, y: 0, signedDistance: 0, nx: 0, ny: 1 };
        const poolCutResult = {
            hitX: 0,
            hitY: 0,
            hitSurfaceY: 0,
            hitOffset: 0,
            hitT: 0,
            firstT: 0,
            lastT: 0,
            sampleCount: 0,
        };
        const poolCutSample = { x: 0, y: 0, surfaceY: NaN, offset: NaN };
        let lastPointer = null;
        let lastPointerEventTime = null;

        let width = 0;
        let height = 0;
        let left = 0;
        let top = 0;
        let dpr = 1;
        let time = 0;
        let frameId = null;
        let previousFrameTime = null;
        let accumulatedMs = 0;
        let disposed = false;
        let initialized = false;
        let active = false;
        let poolSliceCooldown = 0;

        let cellSize = 12;
        let originX = 0;
        let originY = 0;
        let cols = 0;
        let rows = 0;
        let grid = new Float32Array(0);
        let visited = new Uint8Array(0);
        let poolSurfaceY = new Float32Array(0);
        const loopX = new Float32Array(MAX_LOOP_POINTS);
        const loopY = new Float32Array(MAX_LOOP_POINTS);
        const smoothX = new Float32Array(MAX_LOOP_POINTS);
        const smoothY = new Float32Array(MAX_LOOP_POINTS);
        let activeStartRow = 0;
        let activeEndRow = 0;
        let coreGradient = null;
        let poolAuraCanvas = null;
        let poolAuraContext = null;
        let poolAuraImageData = null;
        let poolAuraPixels = new Uint8ClampedArray(0);
        let poolAuraField = new Float32Array(0);
        let poolAuraDistance = new Float32Array(0);
        let poolAuraCols = 0;
        let poolAuraRows = 0;
        let poolAuraStartRow = 0;
        let poolAuraTop = 0;
        let poolAuraHeight = 0;
        let poolAuraReady = false;

        function isMobile() {
            return width <= 768;
        }

        function getBlobLimit() {
            return isMobile() ? MOBILE_BLOB_LIMIT : DESKTOP_BLOB_LIMIT;
        }

        function getBlobHardLimit() {
            return isMobile() ? MOBILE_BLOB_HARD_LIMIT : DESKTOP_BLOB_HARD_LIMIT;
        }

        function getBlobShrink(y) {
            const shrinkStart = height * 0.55;
            const shrinkEnd = -height * 0.16;
            return 1 - smootherstep((shrinkStart - y) / (shrinkStart - shrinkEnd));
        }

        function getPoolDepth() {
            return clamp(height * 0.092, 56, 84) * POOL_VERTICAL_SCALE;
        }

        function getPoolHalfWidth() {
            return width * 0.5;
        }

        function getPoolSurfaceY(x, atTime = time) {
            const nx = clamp(x / Math.max(1, width), 0, 1);
            const baseLineY = height - getPoolDepth() * 0.70;
            const slowWave1 = Math.sin(nx * 4.2 + atTime * 0.21) * 9.2;
            const slowWave2 = Math.cos(nx * 6.8 - atTime * 0.29 + 1.2) * 5.6;
            const slowWave3 = Math.sin(nx * 2.5 + atTime * 0.14 + 2.5) * 7.0;
            const lastIndex = poolNodes.length - 1;
            const nodePosition = nx * lastIndex;
            const firstIndex = nodePosition | 0;
            const secondIndex = Math.min(lastIndex, firstIndex + 1);
            const amount = nodePosition - firstIndex;
            const viscousOffset = lastIndex >= 0
                ? poolNodes[firstIndex].viscousOffset * (1 - amount)
                    + poolNodes[secondIndex].viscousOffset * amount
                : 0;
            return baseLineY + slowWave1 + slowWave2 + slowWave3 + viscousOffset;
        }

        function deformViscousPool(worldX, deltaY, spreadRadius = 135) {
            const inverseSpread = Math.PI / Math.max(1, spreadRadius);
            for (let index = 0; index < poolNodes.length; index += 1) {
                const node = poolNodes[index];
                const nodeX = node.ratioX * width;
                const distance = Math.abs(worldX - nodeX);
                if (distance >= spreadRadius) continue;
                const falloff = 0.5 * (1 + Math.cos(distance * inverseSpread));
                node.targetOffset = clamp(node.targetOffset + deltaY * falloff, -34, 38);
            }
        }

        function newBlob(options = {}) {
            const mobile = isMobile();
            const radius = options.radius ?? randomBetween(mobile ? 20 : 30, mobile ? 40 : 65);
            const blob = {
                x: options.x ?? randomBetween(width * 0.28, width * 0.72),
                y: options.y ?? height + radius * 0.72,
                radius,
                birth: options.birth ?? 0,
                birthRate: options.birthRate ?? randomBetween(0.34, 0.48),
                riseSpeed: options.riseSpeed ?? randomBetween(14, 32),
                swayPhase: Math.random() * Math.PI * 2,
                swaySpeed: randomBetween(0.18, 0.42),
                swayAmplitude: randomBetween(4, 10),
                phase2: Math.random() * Math.PI * 2,
                phase3: Math.random() * Math.PI * 2,
                phase4: Math.random() * Math.PI * 2,
                phase2Rate: randomBetween(0.36, 0.60),
                phase3Rate: randomBetween(0.28, 0.52),
                phase4Rate: randomBetween(0.22, 0.42),
                amp2: randomBetween(0.11, 0.19),
                amp3: randomBetween(0.08, 0.15),
                amp4: randomBetween(0.04, 0.08),
                orientation: Math.random() * Math.PI * 2,
                angularVelocity: randomBetween(-0.10, 0.10),
                spineBend: options.spineBend ?? randomBetween(-0.15, 0.15),
                taper: randomBetween(-0.10, 0.14),
                lobePhase: Math.random() * Math.PI * 2,
                lobeRate: randomBetween(0.22, 0.46),
                lobeEccentricity: randomBetween(0.24, 0.39),
                vx: options.vx ?? 0,
                vy: options.vy ?? 0,
                sliceCooldown: options.sliceCooldown ?? 0,
                separationCooldown: options.separationCooldown ?? 0,
                separationPartner: null,
                separationNormalX: options.separationNormalX ?? 0,
                separationNormalY: options.separationNormalY ?? 1,
                heatFlash: options.heatFlash ?? 0,
                carvePhase: options.carveRamp === undefined ? 1 : clamp(options.carveRamp, 0, 1),
                cutDeform2X: options.cutDeform2X ?? 0,
                cutDeform2Y: options.cutDeform2Y ?? 0,
                cutDeform3X: options.cutDeform3X ?? 0,
                cutDeform3Y: options.cutDeform3Y ?? 0,
            };
            blobs.push(blob);
            return blob;
        }

        function seedPoolNodes() {
            poolNodes.length = 0;
            const count = 20;
            for (let index = 0; index < count; index += 1) {
                const ratioX = index / (count - 1);
                poolNodes.push({
                    ratioX,
                    phase1: ratioX * Math.PI * 2.4 + Math.random() * 1.5,
                    phase2: ratioX * Math.PI * 4 - Math.random() * 1.5,
                    freq1: 0.15 + Math.random() * 0.08,
                    freq2: 0.22 + Math.random() * 0.10,
                    baseRadius: 45 + Math.sin(ratioX * Math.PI) * 12,
                    viscousOffset: 0,
                    targetOffset: 0,
                });
            }
        }

        function seedInitialBlobs() {
            blobs.length = 0;
            const count = isMobile() ? 3 : 4;
            const positions = [0.72, 0.88, 0.57, 0.80];
            for (let index = 0; index < count; index += 1) {
                const radius = randomBetween(isMobile() ? 20 : 30, isMobile() ? 40 : 65);
                const x = width * randomBetween(0.31, 0.69);
                newBlob({
                    x,
                    y: height * positions[index],
                    radius,
                    birth: 1,
                    riseSpeed: randomBetween(14, 32),
                });
            }
        }

        function setupGrid() {
            const area = Math.max(1, width * height);
            cellSize = Math.max(12, Math.ceil(Math.sqrt(area / MAX_GRID_CELLS)));
            originX = -cellSize * 2;
            originY = -cellSize * 2;
            cols = Math.ceil((width + cellSize * 4) / cellSize);
            rows = Math.ceil((height + cellSize * 4) / cellSize);
            const nodeCount = (cols + 1) * (rows + 1);
            const cellCount = cols * rows;
            if (grid.length !== nodeCount) grid = new Float32Array(nodeCount);
            if (visited.length !== cellCount * 4) visited = new Uint8Array(cellCount * 4);
            if (poolSurfaceY.length !== cols + 1) poolSurfaceY = new Float32Array(cols + 1);
        }

        function rebuildGradients() {
            context.setTransform(dpr, 0, 0, dpr, 0, 0);
            coreGradient = context.createLinearGradient(0, 0, 0, height);
            coreGradient.addColorStop(0, CORE_COLORS[0]);
            coreGradient.addColorStop(1, CORE_COLORS[1]);
            poolAuraHeight = clamp(height * POOL_AURA_HEIGHT_RATIO, POOL_AURA_MIN_HEIGHT, POOL_AURA_MAX_HEIGHT);

            // Keep the aura raster aligned to the scalar grid and tall enough for pool nodes,
            // their viscous motion, blade reach, and the complete exterior falloff.
            const poolTopExtent = Math.max(
                getPoolDepth() * 0.70 + 58,
                getPoolDepth() * 0.44 + 110,
            );
            const auraBandHeadroom = poolTopExtent + POOL_AURA_MAX_BLADE_REACH
                + poolAuraHeight + cellSize * 4;
            const requestedTop = height - auraBandHeadroom;
            poolAuraStartRow = clamp(
                Math.floor((requestedTop - originY) / cellSize),
                0,
                rows - 1,
            );
            poolAuraTop = originY + poolAuraStartRow * cellSize;
            poolAuraCols = cols + 1;
            poolAuraRows = rows - poolAuraStartRow + 1;
            const rasterChanged = !poolAuraCanvas
                || poolAuraCanvas.width !== poolAuraCols
                || poolAuraCanvas.height !== poolAuraRows;
            if (!poolAuraCanvas) poolAuraCanvas = document.createElement('canvas');
            if (poolAuraCanvas.width !== poolAuraCols) poolAuraCanvas.width = poolAuraCols;
            if (poolAuraCanvas.height !== poolAuraRows) poolAuraCanvas.height = poolAuraRows;
            if (!poolAuraContext && poolAuraCanvas.getContext) {
                try {
                    poolAuraContext = poolAuraCanvas.getContext('2d', { alpha: true });
                } catch (error) {
                    poolAuraContext = null;
                }
            }
            const cellCount = poolAuraCols * poolAuraRows;
            if (rasterChanged || poolAuraField.length !== cellCount) {
                poolAuraField = new Float32Array(cellCount);
                poolAuraDistance = new Float32Array(cellCount);
                if (poolAuraContext && typeof poolAuraContext.createImageData === 'function') {
                    poolAuraImageData = poolAuraContext.createImageData(poolAuraCols, poolAuraRows);
                    poolAuraPixels = poolAuraImageData.data;
                } else {
                    poolAuraImageData = null;
                    poolAuraPixels = new Uint8ClampedArray(0);
                }
            }
        }

        function buildPoolAuraMask() {
            if (!poolAuraImageData || !poolAuraContext || poolAuraRows < 2 || poolAuraCols < 2) return;
            const stride = cols + 1;
            for (let localY = 0; localY < poolAuraRows; localY += 1) {
                const sourceOffset = (poolAuraStartRow + localY) * stride;
                const targetOffset = localY * poolAuraCols;
                for (let gx = 0; gx < poolAuraCols; gx += 1) {
                    poolAuraField[targetOffset + gx] = grid[sourceOffset + gx];
                }
            }

            // Apply the same temporary blade subtraction to this isolated pool-only
            // field. Positive blobs and their bridges never enter the aura raster.
            for (let bladeIndex = 0; bladeIndex < bladeNodes.length; bladeIndex += 1) {
                const blade = bladeNodes[bladeIndex];
                if (blade.life <= 0.01) continue;
                const radiusSquared = blade.radius * blade.radius * blade.life * 0.62;
                if (radiusSquared <= 1) continue;
                const cutoff = radiusSquared * 15.5;
                const bias = radiusSquared / (cutoff + 18);
                const reach = Math.sqrt(cutoff);
                const bounds = getGridBounds(blade.x, blade.y, reach);
                const y0 = Math.max(bounds.y0, poolAuraStartRow);
                const y1 = Math.min(bounds.y1, poolAuraStartRow + poolAuraRows - 1);
                if (bounds.x0 > bounds.x1 || y0 > y1) continue;
                for (let gy = y0; gy <= y1; gy += 1) {
                    const worldY = originY + gy * cellSize;
                    const dy = worldY - blade.y;
                    const localRowOffset = (gy - poolAuraStartRow) * poolAuraCols;
                    for (let gx = bounds.x0; gx <= bounds.x1; gx += 1) {
                        const dx = originX + gx * cellSize - blade.x;
                        const distanceSquared = dx * dx + dy * dy;
                        if (distanceSquared < cutoff) {
                            poolAuraField[localRowOffset + gx] -= radiusSquared / (distanceSquared + 18) - bias;
                        }
                    }
                }
            }

            // Two chamfer passes give a bounded 8-neighbour distance field without
            // contours, per-frame gradients, or a single-valued surface projection.
            const maxAuraSteps = poolAuraHeight / cellSize + 4;
            for (let index = 0; index < poolAuraField.length; index += 1) {
                poolAuraDistance[index] = poolAuraField[index] >= 1 ? 0 : maxAuraSteps;
            }
            // Seed each exterior grid point with its interpolated distance to any
            // adjacent threshold crossing, rather than assuming a half-cell edge.
            for (let localY = 0; localY < poolAuraRows; localY += 1) {
                const rowOffset = localY * poolAuraCols;
                for (let gx = 0; gx < poolAuraCols; gx += 1) {
                    const index = rowOffset + gx;
                    const outsideValue = poolAuraField[index];
                    if (outsideValue >= 1) continue;
                    let distance = poolAuraDistance[index];
                    if (gx > 0) {
                        const neighbor = index - 1;
                        const insideValue = poolAuraField[neighbor];
                        if (insideValue >= 1) {
                            distance = Math.min(distance, (1 - outsideValue) / (insideValue - outsideValue));
                        }
                    }
                    if (gx + 1 < poolAuraCols) {
                        const neighbor = index + 1;
                        const insideValue = poolAuraField[neighbor];
                        if (insideValue >= 1) {
                            distance = Math.min(distance, (1 - outsideValue) / (insideValue - outsideValue));
                        }
                    }
                    if (localY > 0) {
                        const above = index - poolAuraCols;
                        const insideValue = poolAuraField[above];
                        if (insideValue >= 1) {
                            distance = Math.min(distance, (1 - outsideValue) / (insideValue - outsideValue));
                        }
                        if (gx > 0) {
                            const neighbor = above - 1;
                            const insideDiagonal = poolAuraField[neighbor];
                            if (insideDiagonal >= 1) {
                                distance = Math.min(distance, Math.SQRT2 * (1 - outsideValue) / (insideDiagonal - outsideValue));
                            }
                        }
                        if (gx + 1 < poolAuraCols) {
                            const neighbor = above + 1;
                            const insideDiagonal = poolAuraField[neighbor];
                            if (insideDiagonal >= 1) {
                                distance = Math.min(distance, Math.SQRT2 * (1 - outsideValue) / (insideDiagonal - outsideValue));
                            }
                        }
                    }
                    if (localY + 1 < poolAuraRows) {
                        const below = index + poolAuraCols;
                        const insideValue = poolAuraField[below];
                        if (insideValue >= 1) {
                            distance = Math.min(distance, (1 - outsideValue) / (insideValue - outsideValue));
                        }
                        if (gx > 0) {
                            const neighbor = below - 1;
                            const insideDiagonal = poolAuraField[neighbor];
                            if (insideDiagonal >= 1) {
                                distance = Math.min(distance, Math.SQRT2 * (1 - outsideValue) / (insideDiagonal - outsideValue));
                            }
                        }
                        if (gx + 1 < poolAuraCols) {
                            const neighbor = below + 1;
                            const insideDiagonal = poolAuraField[neighbor];
                            if (insideDiagonal >= 1) {
                                distance = Math.min(distance, Math.SQRT2 * (1 - outsideValue) / (insideDiagonal - outsideValue));
                            }
                        }
                    }
                    poolAuraDistance[index] = distance;
                }
            }
            const diagonal = Math.SQRT2;
            for (let localY = 0; localY < poolAuraRows; localY += 1) {
                const rowOffset = localY * poolAuraCols;
                for (let gx = 0; gx < poolAuraCols; gx += 1) {
                    const index = rowOffset + gx;
                    let distance = poolAuraDistance[index];
                    if (gx > 0) distance = Math.min(distance, poolAuraDistance[index - 1] + 1);
                    if (localY > 0) {
                        const above = index - poolAuraCols;
                        distance = Math.min(distance, poolAuraDistance[above] + 1);
                        if (gx > 0) distance = Math.min(distance, poolAuraDistance[above - 1] + diagonal);
                        if (gx + 1 < poolAuraCols) distance = Math.min(distance, poolAuraDistance[above + 1] + diagonal);
                    }
                    poolAuraDistance[index] = distance;
                }
            }
            for (let localY = poolAuraRows - 1; localY >= 0; localY -= 1) {
                const rowOffset = localY * poolAuraCols;
                for (let gx = poolAuraCols - 1; gx >= 0; gx -= 1) {
                    const index = rowOffset + gx;
                    let distance = poolAuraDistance[index];
                    if (gx + 1 < poolAuraCols) distance = Math.min(distance, poolAuraDistance[index + 1] + 1);
                    if (localY + 1 < poolAuraRows) {
                        const below = index + poolAuraCols;
                        distance = Math.min(distance, poolAuraDistance[below] + 1);
                        if (gx > 0) distance = Math.min(distance, poolAuraDistance[below - 1] + diagonal);
                        if (gx + 1 < poolAuraCols) distance = Math.min(distance, poolAuraDistance[below + 1] + diagonal);
                    }
                    poolAuraDistance[index] = distance;
                }
            }

            for (let index = 0; index < poolAuraField.length; index += 1) {
                const pixelOffset = index * 4;
                poolAuraPixels[pixelOffset] = POOL_AURA_R;
                poolAuraPixels[pixelOffset + 1] = POOL_AURA_G;
                poolAuraPixels[pixelOffset + 2] = POOL_AURA_B;
                if (poolAuraField[index] >= 1) {
                    // Opaque backing prevents interpolation at the threshold from
                    // exposing a pale seam; the later opaque lava contours cover it.
                    poolAuraPixels[pixelOffset + 3] = 255;
                    continue;
                }
                const distance = poolAuraDistance[index] * cellSize;
                const alpha = distance >= poolAuraHeight ? 0 : 1 - distance / poolAuraHeight;
                poolAuraPixels[pixelOffset + 3] = Math.round(alpha * 255);
            }
            poolAuraContext.putImageData(poolAuraImageData, 0, 0);
            poolAuraReady = true;
        }

        function drawExternalPoolAura() {
            if (!poolAuraCanvas || !poolAuraContext || !poolAuraImageData) return;
            context.save();
            context.globalAlpha = 1;
            context.shadowColor = 'rgba(0, 0, 0, 0)';
            context.shadowBlur = 0;
            context.shadowOffsetX = 0;
            context.shadowOffsetY = 0;
            context.imageSmoothingEnabled = true;
            context.drawImage(
                poolAuraCanvas,
                originX - cellSize * 0.5,
                poolAuraTop - cellSize * 0.5,
                poolAuraCols * cellSize,
                poolAuraRows * cellSize,
            );
            context.restore();
        }

        function getGridBounds(cx, cy, reach) {
            const inverseCell = 1 / cellSize;
            let x0 = Math.floor((cx - reach - originX) * inverseCell);
            let x1 = Math.ceil((cx + reach - originX) * inverseCell);
            let y0 = Math.floor((cy - reach - originY) * inverseCell);
            let y1 = Math.ceil((cy + reach - originY) * inverseCell);
            x0 = Math.max(1, x0);
            x1 = Math.min(cols - 1, x1);
            y0 = Math.max(1, y0);
            y1 = Math.min(rows - 1, y1);
            return { x0, x1, y0, y1 };
        }

        function noteActiveRows(y0, y1) {
            activeStartRow = Math.min(activeStartRow, y0);
            activeEndRow = Math.max(activeEndRow, y1);
        }

        function injectEllipse(cx, cy, radiusX, radiusY, strength) {
            if (strength <= 2 || radiusX <= 1 || radiusY <= 1) return;
            const cutoff = strength * 18.5;
            const bias = strength / (cutoff + 28);
            const reach = Math.max(radiusX, radiusY) * 4.35;
            const bounds = getGridBounds(cx, cy, reach);
            if (bounds.x0 > bounds.x1 || bounds.y0 > bounds.y1) return;
            noteActiveRows(bounds.y0, bounds.y1);
            const baseRadius = Math.sqrt(strength);
            const scaleX = baseRadius / radiusX;
            const scaleY = baseRadius / radiusY;
            const stride = cols + 1;

            for (let gy = bounds.y0; gy <= bounds.y1; gy += 1) {
                const dy = originY + gy * cellSize - cy;
                const rowOffset = gy * stride;
                for (let gx = bounds.x0; gx <= bounds.x1; gx += 1) {
                    const dx = originX + gx * cellSize - cx;
                    const distance = (dx * scaleX) ** 2 + (dy * scaleY) ** 2;
                    if (distance < cutoff) {
                        grid[rowOffset + gx] += strength / (distance + 28) - bias;
                    }
                }
            }
        }

        function injectBladeNegativeField(cx, cy, radiusSquared) {
            if (radiusSquared <= 1) return;
            const cutoff = radiusSquared * 15.5;
            const bias = radiusSquared / (cutoff + 18);
            const reach = Math.sqrt(cutoff);
            const bounds = getGridBounds(cx, cy, reach);
            if (bounds.x0 > bounds.x1 || bounds.y0 > bounds.y1) return;
            noteActiveRows(bounds.y0, bounds.y1);
            const stride = cols + 1;

            for (let gy = bounds.y0; gy <= bounds.y1; gy += 1) {
                const dy = originY + gy * cellSize - cy;
                const rowOffset = gy * stride;
                for (let gx = bounds.x0; gx <= bounds.x1; gx += 1) {
                    const dx = originX + gx * cellSize - cx;
                    const distanceSquared = dx * dx + dy * dy;
                    if (distanceSquared < cutoff) {
                        grid[rowOffset + gx] -= radiusSquared / (distanceSquared + 18) - bias;
                    }
                }
            }
        }

        function addBladeNode(x, y, radius, life = 1) {
            if (bladeNodes.length >= MAX_BLADE_NODES) bladeNodes.shift();
            const glowRadius = radius * 1.95;
            const highlight = context.createRadialGradient(x, y, 2, x, y, glowRadius);
            highlight.addColorStop(0, '#FFFDF9');
            highlight.addColorStop(0.45, '#EED8BA');
            highlight.addColorStop(1, 'rgba(238, 216, 186, 0)');
            bladeNodes.push({ x, y, radius, life, highlight });
        }

        function addSparkBurst(x, y, dirX, dirY, nx, ny, count) {
            const spawnCount = Math.min(count, MAX_SPARKS - sparks.length);
            for (let index = 0; index < spawnCount; index += 1) {
                const side = index % 2 === 0 ? 1 : -1;
                const speed = randomBetween(34, 130);
                sparks.push({
                    x: x + randomBetween(-7, 7),
                    y: y + randomBetween(-7, 7),
                    vx: dirX * speed * 0.75 + nx * side * speed * randomBetween(0.35, 0.85),
                    vy: dirY * speed * 0.75 + ny * side * speed * randomBetween(0.35, 0.85) - 14,
                    radius: randomBetween(2, 4.8),
                    life: randomBetween(0.55, 0.9),
                    decay: randomBetween(1.3, 2.1),
                });
            }
        }

        function computePointToSegment(px, py, x1, y1, x2, y2) {
            const dx = x2 - x1;
            const dy = y2 - y1;
            const lengthSquared = dx * dx + dy * dy;
            if (lengthSquared < 0.0001) {
                segmentResult.distance = Math.hypot(px - x1, py - y1);
                segmentResult.t = 0;
                segmentResult.x = x1;
                segmentResult.y = y1;
                segmentResult.signedDistance = 0;
                segmentResult.nx = 0;
                segmentResult.ny = 1;
                return segmentResult;
            }
            const length = Math.sqrt(lengthSquared);
            const t = clamp(((px - x1) * dx + (py - y1) * dy) / lengthSquared, 0, 1);
            const nearestX = x1 + t * dx;
            const nearestY = y1 + t * dy;
            const nx = -dy / length;
            const ny = dx / length;
            segmentResult.distance = Math.hypot(px - nearestX, py - nearestY);
            segmentResult.t = t;
            segmentResult.x = nearestX;
            segmentResult.y = nearestY;
            segmentResult.signedDistance = (px - x1) * nx + (py - y1) * ny;
            segmentResult.nx = nx;
            segmentResult.ny = ny;
            return segmentResult;
        }

        function blobFieldAt(blob, effectiveRadius, worldX, worldY) {
            const radiusSquared = effectiveRadius * effectiveRadius;
            const mainStrength = radiusSquared * 0.78;
            const subStrength = radiusSquared * 0.25;
            const cutoff = mainStrength * 18.5;
            const mainBias = mainStrength / (cutoff + 28);
            const subCutoff = subStrength * 18.5;
            const subBias = subStrength / (subCutoff + 24);
            const cosine = Math.cos(blob.orientation);
            const sine = Math.sin(blob.orientation);
            const inverseRadius = 1 / effectiveRadius;
            const lobeDistance = effectiveRadius * blob.lobeEccentricity;
            const lobeX = blob.x + Math.cos(blob.lobePhase) * lobeDistance * 0.85;
            const lobeY = blob.y + Math.sin(blob.lobePhase * 1.15) * lobeDistance;
            const dx = worldX - blob.x;
            const dy = worldY - blob.y;
            const rawU = dx * cosine + dy * sine;
            const rawV = -dx * sine + dy * cosine;
            const normalizedV = clamp(rawV * inverseRadius, -1.8, 1.8);
            const u = (rawU - blob.spineBend * rawV * normalizedV * 0.45)
                * (1 - blob.taper * normalizedV * 0.28);
            const u2 = u * u;
            const v2 = rawV * rawV;
            let field = 0;

            if (u2 + v2 < cutoff * 1.65) {
                const inverseDistance = 1 / (u2 + v2 + 20);
                const u2MinusV2 = u2 - v2;
                const twoUV = 2 * u * rawV;
                const a2 = Math.cos(blob.phase2) * blob.amp2 + blob.cutDeform2X;
                const b2 = Math.sin(blob.phase2) * blob.amp2 * 0.85 + blob.cutDeform2Y;
                const a3 = Math.cos(blob.phase3) * blob.amp3 + blob.cutDeform3X;
                const b3 = Math.sin(blob.phase3) * blob.amp3 + blob.cutDeform3Y;
                const a4 = Math.cos(blob.phase4) * blob.amp4;
                const b4 = Math.sin(blob.phase4) * blob.amp4;
                const h2 = (a2 * u2MinusV2 + b2 * twoUV) * inverseDistance;
                const h3 = (a3 * u * (u2 - 3 * v2) + b3 * rawV * (3 * u2 - v2))
                    * inverseDistance * inverseRadius;
                const h4 = (a4 * (u2MinusV2 * u2MinusV2 - twoUV * twoUV)
                    + 2 * b4 * u2MinusV2 * twoUV) * inverseDistance * inverseDistance;
                const shape = clamp(1 + h2 + h3 + h4, 0.62, 1.45);
                const warpedDistance = (u2 + v2) / (shape * shape);
                if (warpedDistance < cutoff) {
                    field += mainStrength / (warpedDistance + 28) - mainBias;
                }
            }

            const subDx = worldX - lobeX;
            const subDy = worldY - lobeY;
            const subDistance = subDx * subDx + subDy * subDy;
            if (subDistance < subCutoff) field += subStrength / (subDistance + 24) - subBias;
            return field;
        }

        function segmentCutsVisibleBlob(blob, effectiveRadius, segment, x1, y1, x2, y2) {
            const broadphaseRadius = effectiveRadius * 1.95;
            if (segment.distance > broadphaseRadius) return false;

            const dx = x2 - x1;
            const dy = y2 - y1;
            const length = Math.hypot(dx, dy);
            if (length < 0.001) return false;
            const halfChord = Math.sqrt(Math.max(0,
                broadphaseRadius * broadphaseRadius - segment.distance * segment.distance));
            const firstAlong = Math.max(0, segment.t * length - halfChord);
            const lastAlong = Math.min(length, segment.t * length + halfChord);
            const sampleLength = Math.max(4.5, Math.min(cellSize * 0.65, effectiveRadius * 0.22));
            const sampleCount = clamp(Math.ceil((lastAlong - firstAlong) / sampleLength), 1, MAX_BLOB_HIT_SAMPLES);
            for (let index = 0; index <= sampleCount; index += 1) {
                const along = firstAlong + (lastAlong - firstAlong) * index / sampleCount;
                const field = blobFieldAt(blob, effectiveRadius, x1 + dx * along / length, y1 + dy * along / length);
                if (field >= 0.78) return true;
            }
            return false;
        }

        function splitBlob(blob, segment, dirX, dirY, swipeSpeed, effectiveRadius, x1, y1, x2, y2) {
            if (!blob || blob.sliceCooldown > 0 || blob.radius < 13.5 || blobs.length >= getBlobHardLimit()) return false;
            const oldPartner = blob.separationPartner;
            if (oldPartner && oldPartner.separationPartner === blob) {
                oldPartner.separationPartner = null;
                oldPartner.separationCooldown = 0;
            }
            blob.separationPartner = null;
            blob.separationCooldown = 0;
            const originalRadius = blob.radius;
            const offsetRatio = clamp(segment.signedDistance / originalRadius, -0.55, 0.55);
            const alpha = clamp(0.5 + offsetRatio * 0.68, 0.24, 0.76);
            const volumeBoost = 1.03;
            const radiusOne = originalRadius * Math.sqrt(alpha * volumeBoost);
            const radiusTwo = originalRadius * Math.sqrt((1 - alpha) * volumeBoost);
            const nx = segment.nx;
            const ny = segment.ny;
            const separation = originalRadius * 0.34;
            const pushSpeed = clamp(swipeSpeed / 75, 1.8, 4.2) * 30;
            const carrySpeed = clamp(swipeSpeed / 250, 0, 2.2) * 30;
            const cutAngle = Math.atan2(dirY, dirX);
            const cut2X = Math.cos(2 * cutAngle) * 0.22;
            const cut2Y = Math.sin(2 * cutAngle) * 0.22;

            const originalX = blob.x;
            const originalY = blob.y;
            const originalVx = blob.vx;
            const originalVy = blob.vy;
            blob.x = originalX + nx * separation * (1 - alpha);
            blob.y = originalY + ny * separation * (1 - alpha);
            blob.vx = originalVx * 0.4 + nx * pushSpeed * (1 - alpha + 0.35) + dirX * carrySpeed;
            blob.vy = originalVy * 0.4 + ny * pushSpeed * (1 - alpha + 0.35) + dirY * carrySpeed;
            blob.radius = radiusOne;
            blob.riseSpeed = clamp(blob.riseSpeed * randomBetween(0.82, 1.04), 14, 32);
            blob.cutDeform2X = cut2X;
            blob.cutDeform2Y = cut2Y;
            blob.cutDeform3X = nx * 0.18;
            blob.cutDeform3Y = ny * 0.18;
            blob.spineBend = randomBetween(-0.15, 0.15);
            blob.sliceCooldown = 0.56;
            blob.separationCooldown = SIBLING_SEPARATION_SECONDS;
            blob.heatFlash = 0.85;

            const child = newBlob({
                x: originalX - nx * separation * alpha,
                y: originalY - ny * separation * alpha,
                radius: radiusTwo,
                riseSpeed: clamp(blob.riseSpeed * randomBetween(0.88, 1.18), 14, 32),
                vx: originalVx * 0.4 - nx * pushSpeed * (alpha + 0.35) + dirX * carrySpeed,
                vy: originalVy * 0.4 - ny * pushSpeed * (alpha + 0.35) + dirY * carrySpeed,
                birth: 1,
                cutDeform2X: -cut2X,
                cutDeform2Y: -cut2Y,
                cutDeform3X: -nx * 0.18,
                cutDeform3Y: -ny * 0.18,
                sliceCooldown: 0.56,
                separationCooldown: SIBLING_SEPARATION_SECONDS,
                heatFlash: 0.85,
            });
            blob.separationPartner = child;
            child.separationPartner = blob;
            blob.separationNormalX = -nx;
            blob.separationNormalY = -ny;
            child.separationNormalX = nx;
            child.separationNormalY = ny;

            const bladeRadius = clamp(12 + swipeSpeed * 0.015, 14, 25);
            const dx = x2 - x1;
            const dy = y2 - y1;
            const sweepLength = Math.hypot(dx, dy);
            const clipRadius = effectiveRadius * 1.95 + bladeRadius * 0.35;
            const halfChord = Math.sqrt(Math.max(0, clipRadius * clipRadius - segment.distance * segment.distance));
            const firstAlong = Math.max(0, segment.t * sweepLength - halfChord);
            const lastAlong = Math.min(sweepLength, segment.t * sweepLength + halfChord);
            const clippedLength = lastAlong - firstAlong;
            const steps = clamp(Math.ceil(clippedLength / (bladeRadius * 0.64)), 1, 16);
            for (let step = 0; step <= steps; step += 1) {
                const amount = step / steps;
                addBladeNode(
                    x1 + dx * (firstAlong + clippedLength * amount) / Math.max(1, sweepLength),
                    y1 + dy * (firstAlong + clippedLength * amount) / Math.max(1, sweepLength),
                    bladeRadius,
                );
            }
            addSparkBurst(segment.x, segment.y, dirX, dirY, nx, ny, Math.min(10, Math.floor(originalRadius * 0.2)));
            return true;
        }

        function getPoolCutFieldAt(worldX, worldY) {
            if (poolAuraReady) {
                const gridX = (worldX - originX) / cellSize;
                const gridY = (worldY - originY) / cellSize - poolAuraStartRow;
                if (gridX < 0 || gridY < 0 || gridX > poolAuraCols - 1 || gridY > poolAuraRows - 1) {
                    return -Infinity;
                }
                const x0 = Math.floor(gridX);
                const y0 = Math.floor(gridY);
                const x1 = Math.min(poolAuraCols - 1, x0 + 1);
                const y1 = Math.min(poolAuraRows - 1, y0 + 1);
                const xAmount = gridX - x0;
                const yAmount = gridY - y0;
                const topLeft = poolAuraField[y0 * poolAuraCols + x0];
                const topRight = poolAuraField[y0 * poolAuraCols + x1];
                const bottomLeft = poolAuraField[y1 * poolAuraCols + x0];
                const bottomRight = poolAuraField[y1 * poolAuraCols + x1];
                const top = topLeft + (topRight - topLeft) * xAmount;
                const bottom = bottomLeft + (bottomRight - bottomLeft) * xAmount;
                return top + (bottom - top) * yAmount;
            }

            // Before the first rendered frame, evaluate the same pool-only formulas
            // at this point. This is bounded by the fixed pool/blade counts and avoids
            // building or scanning the full raster from the pointer handler.
            const surfaceY = getPoolSurfaceY(worldX);
            const depthBelow = worldY - surfaceY;
            const transitionHeight = 44 * POOL_VERTICAL_SCALE;
            let field = 0;
            if (depthBelow >= 0) {
                field = 0.54 + Math.min(depthBelow * 0.0185 / POOL_VERTICAL_SCALE, 1.05);
            } else if (depthBelow > -transitionHeight) {
                const amount = 1 + depthBelow / transitionHeight;
                field = 0.54 * amount * amount * (3 - 2 * amount);
            }

            for (let index = 0; index < poolNodes.length; index += 1) {
                const node = poolNodes[index];
                const centerX = width * node.ratioX + Math.sin(time * node.freq1 + node.phase1) * 16;
                const baselineY = height - getPoolDepth() * 0.44;
                const centerY = baselineY
                    + Math.sin(node.phase1 + time * node.freq1) * 9
                    + Math.cos(node.phase2 - time * node.freq2) * 5.5
                    + node.viscousOffset;
                const pulse = Math.sin(time * node.freq2 + node.phase1) * 4.5;
                const radiusX = node.baseRadius + pulse;
                const radiusY = node.baseRadius * POOL_VERTICAL_SCALE + pulse;
                const strength = radiusX * radiusX;
                if (strength <= 2 || radiusX <= 1 || radiusY <= 1) continue;
                const cutoff = strength * 18.5;
                const bias = strength / (cutoff + 28);
                const dx = worldX - centerX;
                const dy = (worldY - centerY) * radiusX / radiusY;
                const distanceSquared = dx * dx + dy * dy;
                if (distanceSquared < cutoff) field += strength / (distanceSquared + 28) - bias;
            }

            for (let index = 0; index < bladeNodes.length; index += 1) {
                const blade = bladeNodes[index];
                if (blade.life <= 0.01) continue;
                const radiusSquared = blade.radius * blade.radius * blade.life * 0.62;
                const cutoff = radiusSquared * 15.5;
                const dx = worldX - blade.x;
                const dy = worldY - blade.y;
                const distanceSquared = dx * dx + dy * dy;
                if (distanceSquared < cutoff) {
                    field -= radiusSquared / (distanceSquared + 18) - radiusSquared / (cutoff + 18);
                }
            }
            return field;
        }

        function getPoolCutSurfaceAtX(worldX) {
            if (worldX < 0 || worldX > width || !poolNodes.length) return NaN;
            const approximateSurfaceY = getPoolSurfaceY(worldX);
            const maximumGridY = originY + rows * cellSize;
            let insideY = Math.min(height + cellSize, maximumGridY);
            let insideField = getPoolCutFieldAt(worldX, insideY);
            if (insideField < POOL_CUT_FIELD_THRESHOLD) {
                insideY = Math.min(approximateSurfaceY + 48, maximumGridY);
                insideField = getPoolCutFieldAt(worldX, insideY);
            }
            if (insideField < POOL_CUT_FIELD_THRESHOLD) return NaN;

            const upperLimitY = Math.max(poolAuraTop, approximateSurfaceY - 180);
            const scanStep = Math.max(1, cellSize);
            let outsideY = insideY;
            for (let step = 0; step < POOL_CUT_CONTOUR_SCAN_STEPS; step += 1) {
                outsideY -= scanStep;
                if (outsideY < upperLimitY) break;
                const outsideField = getPoolCutFieldAt(worldX, outsideY);
                if (outsideField < POOL_CUT_FIELD_THRESHOLD) {
                    let lowY = outsideY;
                    let highY = insideY;
                    for (let refine = 0; refine < POOL_CUT_REFINE_STEPS; refine += 1) {
                        const middleY = (lowY + highY) * 0.5;
                        if (getPoolCutFieldAt(worldX, middleY) >= POOL_CUT_FIELD_THRESHOLD) highY = middleY;
                        else lowY = middleY;
                    }
                    return (lowY + highY) * 0.5;
                }
                insideY = outsideY;
            }
            return NaN;
        }

        function samplePoolCutOffset(x1, y1, dx, dy, amount) {
            const worldX = x1 + dx * amount;
            const worldY = y1 + dy * amount;
            const surfaceY = getPoolCutSurfaceAtX(worldX);
            if (!Number.isFinite(surfaceY)) return false;
            poolCutSample.x = worldX;
            poolCutSample.y = worldY;
            poolCutSample.surfaceY = surfaceY;
            poolCutSample.offset = worldY - surfaceY;
            return true;
        }

        function refinePoolCutOffset(x1, y1, dx, dy, lowAmount, highAmount, targetOffset, lowOffset) {
            let lowIsBelow = lowOffset < targetOffset;
            for (let refine = 0; refine < POOL_CUT_REFINE_STEPS; refine += 1) {
                const middleAmount = (lowAmount + highAmount) * 0.5;
                if (!samplePoolCutOffset(x1, y1, dx, dy, middleAmount)) break;
                if ((poolCutSample.offset < targetOffset) === lowIsBelow) lowAmount = middleAmount;
                else highAmount = middleAmount;
            }
            return (lowAmount + highAmount) * 0.5;
        }

        function recordPoolCutHit(x1, y1, dx, dy, amount) {
            if (!samplePoolCutOffset(x1, y1, dx, dy, amount)) return false;
            poolCutResult.hitX = poolCutSample.x;
            poolCutResult.hitY = poolCutSample.y;
            poolCutResult.hitSurfaceY = poolCutSample.surfaceY;
            poolCutResult.hitOffset = poolCutSample.offset;
            poolCutResult.hitT = amount;
            return true;
        }

        function tryCarveBottomPool(x1, y1, x2, y2, dirX, dirY, distance, swipeSpeed) {
            if (poolSliceCooldown > 0 || blobs.length >= getBlobHardLimit() || distance < 0.001) return false;
            const dx = x2 - x1;
            const dy = y2 - y1;
            const sampleSpacing = Math.max(4, cellSize * 0.6);
            const sampleCount = clamp(Math.ceil(distance / sampleSpacing), 1, MAX_POOL_HIT_SAMPLES);
            const maximumOffset = POOL_CUT_SHELL_BELOW;
            let previousT = 0;
            let previousOffset = NaN;
            let hasPrevious = false;
            let foundContact = false;
            poolCutResult.hitSurfaceY = NaN;
            poolCutResult.sampleCount = sampleCount;
            poolCutResult.firstT = 0;
            poolCutResult.lastT = 0;

            for (let index = 0; index <= sampleCount; index += 1) {
                const amount = index / sampleCount;
                if (!samplePoolCutOffset(x1, y1, dx, dy, amount)) {
                    hasPrevious = false;
                    continue;
                }
                const offset = poolCutSample.offset;
                const withinContactDepth = offset >= -POOL_CUT_SURFACE_TOLERANCE
                    && offset <= maximumOffset;
                if (withinContactDepth) {
                    let contactT = amount;
                    if (hasPrevious && previousOffset < 0 && offset >= 0) {
                        contactT = refinePoolCutOffset(x1, y1, dx, dy, previousT, amount, 0, previousOffset);
                    } else if (hasPrevious && previousOffset > maximumOffset && offset <= maximumOffset) {
                        contactT = refinePoolCutOffset(
                            x1, y1, dx, dy, previousT, amount, maximumOffset, previousOffset,
                        );
                    }

                    const contactField = getPoolCutFieldAt(poolCutSample.x, poolCutSample.y);
                    if (contactField >= POOL_CUT_FIELD_THRESHOLD - POOL_CUT_FIELD_TOLERANCE) {
                        if (!foundContact) {
                            const hitRecorded = recordPoolCutHit(x1, y1, dx, dy, contactT);
                            const hitField = hitRecorded
                                ? getPoolCutFieldAt(poolCutResult.hitX, poolCutResult.hitY)
                                : -Infinity;
                            if (hitRecorded
                                && hitField >= POOL_CUT_FIELD_THRESHOLD - POOL_CUT_FIELD_TOLERANCE
                                && poolCutResult.hitOffset >= -POOL_CUT_SURFACE_TOLERANCE
                                && poolCutResult.hitOffset <= maximumOffset + POOL_CUT_SURFACE_TOLERANCE) {
                                foundContact = true;
                                poolCutResult.firstT = poolCutResult.hitT;
                                poolCutResult.lastT = amount;
                            } else if (recordPoolCutHit(x1, y1, dx, dy, amount)
                                && getPoolCutFieldAt(poolCutResult.hitX, poolCutResult.hitY)
                                    >= POOL_CUT_FIELD_THRESHOLD - POOL_CUT_FIELD_TOLERANCE
                                && poolCutResult.hitOffset >= -POOL_CUT_SURFACE_TOLERANCE
                                && poolCutResult.hitOffset <= maximumOffset + POOL_CUT_SURFACE_TOLERANCE) {
                                foundContact = true;
                                poolCutResult.firstT = poolCutResult.hitT;
                                poolCutResult.lastT = amount;
                            }
                        } else {
                            poolCutResult.lastT = amount;
                        }
                    }
                }
                previousT = amount;
                previousOffset = offset;
                hasPrevious = true;
            }

            if (!foundContact || !Number.isFinite(poolCutResult.hitSurfaceY)) return false;
            const minDimension = Math.min(width, height);
            const speedFactor = clamp(swipeSpeed / 760, 0.65, 1.25);
            const shellFactor = clamp(1 + poolCutResult.hitOffset * 0.002, 0.82, 1.14);
            const radius = clamp(
                minDimension * 0.036 * speedFactor * shellFactor,
                16,
                minDimension * 0.065,
            );
            const referenceEventDistance = swipeSpeed / 60;
            const launch = clamp(referenceEventDistance * 0.080, 1.4, 3.4);
            const launchVx = dirX * launch * 0.75 * 60;
            const launchVy = Math.min(-1.20, dirY * launch - 0.80) * 60;
            const cutAngle = Math.atan2(launchVy, launchVx);
            const hitX = poolCutResult.hitX;
            const hitY = poolCutResult.hitY;
            const hitSurfaceY = poolCutResult.hitSurfaceY;
            newBlob({
                x: hitX + dirX * Math.min(6, radius * 0.1),
                y: Math.min(hitY, hitSurfaceY + radius * 0.15),
                radius,
                birth: 1,
                riseSpeed: randomBetween(18, 30),
                vx: launchVx,
                vy: launchVy,
                carveRamp: 0.22,
                cutDeform2X: Math.cos(2 * cutAngle) * 0.24,
                cutDeform2Y: Math.sin(2 * cutAngle) * 0.24,
                cutDeform3X: dirX * 0.18,
                cutDeform3Y: dirY * 0.18,
                spineBend: dirX >= 0 ? 0.22 : -0.22,
                sliceCooldown: 0.32,
                heatFlash: 0.85,
            });

            const grooveRadius = clamp(radius * 0.44, 11, 22);
            const clippedLength = distance * Math.max(0, poolCutResult.lastT - poolCutResult.firstT);
            const grooveSteps = clamp(
                Math.ceil(clippedLength / (grooveRadius * 2.1)),
                1,
                MAX_POOL_GROOVE_NODES - 1,
            );
            for (let index = 0; index <= grooveSteps; index += 1) {
                const amount = poolCutResult.firstT
                    + (poolCutResult.lastT - poolCutResult.firstT) * index / grooveSteps;
                addBladeNode(x1 + dx * amount, y1 + dy * amount, grooveRadius, 0.95);
            }
            deformViscousPool(hitX, radius * 0.45, radius * 3.4);
            addSparkBurst(hitX, hitSurfaceY, dirX, dirY, -dirY, dirX, 8);
            poolSliceCooldown = 0.16;
            return true;
        }

        function smootherBlobField(blob, effectiveRadius) {
            if (effectiveRadius <= 1) return;
            const radiusSquared = effectiveRadius * effectiveRadius;
            const mainStrength = radiusSquared * 0.78;
            const subStrength = radiusSquared * 0.25;
            const cutoff = mainStrength * 18.5;
            const mainBias = mainStrength / (cutoff + 28);
            const subCutoff = subStrength * 18.5;
            const subBias = subStrength / (subCutoff + 24);
            const reach = effectiveRadius * 4.35;
            const bounds = getGridBounds(blob.x, blob.y, reach);
            if (bounds.x0 > bounds.x1 || bounds.y0 > bounds.y1) return;
            noteActiveRows(bounds.y0, bounds.y1);

            const cosine = Math.cos(blob.orientation);
            const sine = Math.sin(blob.orientation);
            const inverseRadius = 1 / effectiveRadius;
            const lobeDistance = effectiveRadius * blob.lobeEccentricity;
            const lobeX = blob.x + Math.cos(blob.lobePhase) * lobeDistance * 0.85;
            const lobeY = blob.y + Math.sin(blob.lobePhase * 1.15) * lobeDistance;
            const a2 = Math.cos(blob.phase2) * blob.amp2 + blob.cutDeform2X;
            const b2 = Math.sin(blob.phase2) * blob.amp2 * 0.85 + blob.cutDeform2Y;
            const a3 = Math.cos(blob.phase3) * blob.amp3 + blob.cutDeform3X;
            const b3 = Math.sin(blob.phase3) * blob.amp3 + blob.cutDeform3Y;
            const a4 = Math.cos(blob.phase4) * blob.amp4;
            const b4 = Math.sin(blob.phase4) * blob.amp4;
            const stride = cols + 1;

            for (let gy = bounds.y0; gy <= bounds.y1; gy += 1) {
                const worldY = originY + gy * cellSize;
                const dy = worldY - blob.y;
                const subDy = worldY - lobeY;
                const rowOffset = gy * stride;
                for (let gx = bounds.x0; gx <= bounds.x1; gx += 1) {
                    const worldX = originX + gx * cellSize;
                    const dx = worldX - blob.x;
                    const rawU = dx * cosine + dy * sine;
                    const rawV = -dx * sine + dy * cosine;
                    const normalizedV = clamp(rawV * inverseRadius, -1.8, 1.8);
                    const u = (rawU - blob.spineBend * rawV * normalizedV * 0.45)
                        * (1 - blob.taper * normalizedV * 0.28);
                    const u2 = u * u;
                    const v2 = rawV * rawV;
                    const distanceSquared = u2 + v2;
                    if (distanceSquared < cutoff * 1.65) {
                        const inverseDistance = 1 / (distanceSquared + 20);
                        const u2MinusV2 = u2 - v2;
                        const twoUV = 2 * u * rawV;
                        const h2 = (a2 * u2MinusV2 + b2 * twoUV) * inverseDistance;
                        const h3 = (a3 * u * (u2 - 3 * v2)
                            + b3 * rawV * (3 * u2 - v2)) * inverseDistance * inverseRadius;
                        const h4 = (a4 * (u2MinusV2 * u2MinusV2 - twoUV * twoUV)
                            + 2 * b4 * u2MinusV2 * twoUV) * inverseDistance * inverseDistance;
                        const shape = clamp(1 + h2 + h3 + h4, 0.62, 1.45);
                        const warpedDistance = distanceSquared / (shape * shape);
                        if (warpedDistance < cutoff) {
                            grid[rowOffset + gx] += mainStrength / (warpedDistance + 28) - mainBias;
                        }
                    }

                    const subDx = worldX - lobeX;
                    const subDistance = subDx * subDx + subDy * subDy;
                    if (subDistance < subCutoff) {
                        grid[rowOffset + gx] += subStrength / (subDistance + 24) - subBias;
                    }
                }
            }
        }

        function buildField() {
            const stride = cols + 1;
            grid.fill(0);
            activeStartRow = rows - 1;
            activeEndRow = 1;

            let minPoolY = height;
            for (let gx = 1; gx < cols; gx += 1) {
                const x = originX + gx * cellSize;
                const surfaceY = getPoolSurfaceY(x);
                poolSurfaceY[gx] = surfaceY;
                if (surfaceY < minPoolY) minPoolY = surfaceY;
            }

            const poolTransitionHeight = 44 * POOL_VERTICAL_SCALE;
            let startRow = Math.floor((minPoolY - poolTransitionHeight - originY) / cellSize);
            startRow = Math.max(1, startRow);
            noteActiveRows(startRow, rows - 1);
            for (let gy = startRow; gy < rows; gy += 1) {
                const y = originY + gy * cellSize;
                const rowOffset = gy * stride;
                for (let gx = 1; gx < cols; gx += 1) {
                    const depthBelow = y - poolSurfaceY[gx];
                    if (depthBelow <= -poolTransitionHeight) continue;
                    if (depthBelow >= 0) {
                        grid[rowOffset + gx] = 0.54 + Math.min(depthBelow * 0.0185 / POOL_VERTICAL_SCALE, 1.05);
                    } else {
                        const t = 1 + depthBelow / poolTransitionHeight;
                        grid[rowOffset + gx] = 0.54 * t * t * (3 - 2 * t);
                    }
                }
            }

            for (let index = 0; index < poolNodes.length; index += 1) {
                const node = poolNodes[index];
                const x = width * node.ratioX + Math.sin(time * node.freq1 + node.phase1) * 16;
                const baselineY = height - getPoolDepth() * 0.44;
                const y = baselineY
                    + Math.sin(node.phase1 + time * node.freq1) * 9
                    + Math.cos(node.phase2 - time * node.freq2) * 5.5
                    + node.viscousOffset;
                const pulse = Math.sin(time * node.freq2 + node.phase1) * 4.5;
                const radius = node.baseRadius + pulse;
                const radiusY = node.baseRadius * POOL_VERTICAL_SCALE + pulse;
                injectEllipse(x, y, radius, radiusY, radius * radius);
            }

            // Rebuild a pool-only aura from this stable base field. The scratch copy
            // receives blade cuts but not positive body/bridge fields.
            buildPoolAuraMask();

            for (let index = 0; index < blobs.length; index += 1) {
                const blob = blobs[index];
                const birthWeight = smootherstep(blob.birth);
                const shrink = getBlobShrink(blob.y);
                const carveWeight = blob.carvePhase;
                const effectiveRadius = blob.radius * birthWeight * shrink * carveWeight;
                if (effectiveRadius <= 0.8) continue;

                const surfaceY = getPoolSurfaceY(blob.x);
                const enterY = surfaceY + blob.radius * 1.15;
                const exitY = surfaceY - blob.radius * 2.15;
                if (blob.y < enterY && blob.y > exitY) {
                    const progress = clamp((enterY - blob.y) / (enterY - exitY), 0, 1);
                    const bridgeWeight = 16 * progress * progress * (1 - progress) * (1 - progress);
                    const bridgeRadius = effectiveRadius * 0.56 * bridgeWeight;
                    if (bridgeRadius > 2) {
                        const bridgeY = surfaceY * 0.62 + blob.y * 0.38;
                        injectEllipse(
                            blob.x,
                            bridgeY,
                            bridgeRadius * 0.82,
                            bridgeRadius * 1.24,
                            bridgeRadius * bridgeRadius,
                        );
                    }
                }
                smootherBlobField(blob, effectiveRadius);
            }

            for (let index = 0; index < bladeNodes.length; index += 1) {
                const blade = bladeNodes[index];
                if (blade.life > 0.01) {
                    injectBladeNegativeField(blade.x, blade.y, blade.radius * blade.radius * blade.life * 0.62);
                }
            }

            const lastRowOffset = rows * stride;
            for (let gx = 0; gx <= cols; gx += 1) {
                grid[gx] = 0;
                grid[lastRowOffset + gx] = 0;
            }
            for (let gy = 0; gy <= rows; gy += 1) {
                grid[gy * stride] = 0;
                grid[gy * stride + cols] = 0;
            }
        }

        function nextExitEdge(v0, v1, v2, v3, threshold, inEdge) {
            const mask = (v0 >= threshold ? 1 : 0)
                | (v1 >= threshold ? 2 : 0)
                | (v2 >= threshold ? 4 : 0)
                | (v3 >= threshold ? 8 : 0);
            switch (mask) {
                case 1: return inEdge === 3 ? 0 : -1;
                case 2: return inEdge === 0 ? 1 : -1;
                case 3: return inEdge === 3 ? 1 : -1;
                case 4: return inEdge === 1 ? 2 : -1;
                case 5: {
                    const center = 0.25 * (v0 + v1 + v2 + v3);
                    if (center >= threshold) return inEdge === 3 ? 2 : (inEdge === 1 ? 0 : -1);
                    return inEdge === 3 ? 0 : (inEdge === 1 ? 2 : -1);
                }
                case 6: return inEdge === 0 ? 2 : -1;
                case 7: return inEdge === 3 ? 2 : -1;
                case 8: return inEdge === 2 ? 3 : -1;
                case 9: return inEdge === 2 ? 0 : -1;
                case 10: {
                    const center = 0.25 * (v0 + v1 + v2 + v3);
                    if (center >= threshold) return inEdge === 0 ? 3 : (inEdge === 2 ? 1 : -1);
                    return inEdge === 0 ? 1 : (inEdge === 2 ? 3 : -1);
                }
                case 11: return inEdge === 2 ? 1 : -1;
                case 12: return inEdge === 1 ? 3 : -1;
                case 13: return inEdge === 1 ? 0 : -1;
                case 14: return inEdge === 0 ? 3 : -1;
                default: return -1;
            }
        }

        function traceContours(threshold) {
            const stride = cols + 1;
            const startRow = Math.max(0, activeStartRow - 1);
            const endRow = Math.min(rows - 1, activeEndRow + 1);
            visited.fill(0, startRow * cols * 4, (endRow + 1) * cols * 4);
            context.beginPath();

            for (let cy = startRow; cy <= endRow; cy += 1) {
                const row0 = cy * stride;
                const row1 = (cy + 1) * stride;
                for (let cx = 0; cx < cols; cx += 1) {
                    const v0 = grid[row0 + cx];
                    const v1 = grid[row0 + cx + 1];
                    const v2 = grid[row1 + cx + 1];
                    const v3 = grid[row1 + cx];
                    const mask = (v0 >= threshold ? 1 : 0)
                        | (v1 >= threshold ? 2 : 0)
                        | (v2 >= threshold ? 4 : 0)
                        | (v3 >= threshold ? 8 : 0);
                    if (mask === 0 || mask === 15) continue;

                    for (let startEdge = 0; startEdge < 4; startEdge += 1) {
                        const firstIndex = (cy * cols + cx) * 4 + startEdge;
                        if (visited[firstIndex]) continue;
                        if (nextExitEdge(v0, v1, v2, v3, threshold, startEdge) === -1) continue;

                        let pointCount = 0;
                        let currentX = cx;
                        let currentY = cy;
                        let currentIn = startEdge;
                        while (pointCount < MAX_LOOP_POINTS) {
                            const visitIndex = (currentY * cols + currentX) * 4 + currentIn;
                            if (visited[visitIndex]) break;
                            visited[visitIndex] = 1;

                            const cellRow0 = currentY * stride + currentX;
                            const cellRow1 = (currentY + 1) * stride + currentX;
                            const a = grid[cellRow0];
                            const b = grid[cellRow0 + 1];
                            const c = grid[cellRow1 + 1];
                            const d = grid[cellRow1];
                            const exitEdge = nextExitEdge(a, b, c, d, threshold, currentIn);
                            if (exitEdge === -1) break;

                            const x0 = originX + currentX * cellSize;
                            const y0 = originY + currentY * cellSize;
                            if (exitEdge === 0) {
                                const amount = clamp((threshold - a) / ((b - a) || 1e-6), 0, 1);
                                loopX[pointCount] = x0 + amount * cellSize;
                                loopY[pointCount] = y0;
                                currentY -= 1;
                                currentIn = 2;
                            } else if (exitEdge === 1) {
                                const amount = clamp((threshold - b) / ((c - b) || 1e-6), 0, 1);
                                loopX[pointCount] = x0 + cellSize;
                                loopY[pointCount] = y0 + amount * cellSize;
                                currentX += 1;
                                currentIn = 3;
                            } else if (exitEdge === 2) {
                                const amount = clamp((threshold - d) / ((c - d) || 1e-6), 0, 1);
                                loopX[pointCount] = x0 + amount * cellSize;
                                loopY[pointCount] = y0 + cellSize;
                                currentY += 1;
                                currentIn = 0;
                            } else {
                                const amount = clamp((threshold - a) / ((d - a) || 1e-6), 0, 1);
                                loopX[pointCount] = x0;
                                loopY[pointCount] = y0 + amount * cellSize;
                                currentX -= 1;
                                currentIn = 1;
                            }
                            pointCount += 1;
                            if (currentX < 0 || currentX >= cols || currentY < startRow || currentY > endRow) break;
                        }

                        if (pointCount < 4 || pointCount >= MAX_LOOP_POINTS) continue;
                        for (let index = 0; index < pointCount; index += 1) {
                            const previous = index === 0 ? pointCount - 1 : index - 1;
                            const next = index === pointCount - 1 ? 0 : index + 1;
                            smoothX[index] = 0.25 * loopX[previous] + 0.5 * loopX[index] + 0.25 * loopX[next];
                            smoothY[index] = 0.25 * loopY[previous] + 0.5 * loopY[index] + 0.25 * loopY[next];
                        }

                        const startX = 0.5 * (smoothX[0] + smoothX[pointCount - 1]);
                        const startY = 0.5 * (smoothY[0] + smoothY[pointCount - 1]);
                        context.moveTo(startX, startY);
                        for (let index = 0; index < pointCount; index += 1) {
                            const next = index === pointCount - 1 ? 0 : index + 1;
                            const middleX = 0.5 * (smoothX[index] + smoothX[next]);
                            const middleY = 0.5 * (smoothY[index] + smoothY[next]);
                            context.quadraticCurveTo(smoothX[index], smoothY[index], middleX, middleY);
                        }
                        context.closePath();
                    }
                }
            }
        }

        function renderFrame() {
            if (!width || !height || !coreGradient) return;
            buildField();
            context.setTransform(dpr, 0, 0, dpr, 0, 0);
            context.clearRect(0, 0, width, height);
            context.globalAlpha = 1;
            context.lineJoin = 'round';
            context.lineWidth = 0.9;
            drawExternalPoolAura();

            traceContours(1.0);
            context.save();
            context.shadowColor = OUTER_SHADOW_COLOR;
            context.shadowBlur = isMobile() ? 22 : 28;
            context.shadowOffsetX = 0;
            context.shadowOffsetY = 2;
            context.fillStyle = OUTER_COLOR;
            context.fill();
            context.restore();
            context.strokeStyle = OUTER_COLOR;
            context.lineWidth = 1.15;
            context.stroke();

            if (bladeNodes.length) {
                context.save();
                context.clip();
                for (let index = 0; index < bladeNodes.length; index += 1) {
                    const blade = bladeNodes[index];
                    if (blade.life <= 0.01) continue;
                    const radius = blade.radius * 1.95;
                    context.globalAlpha = Math.min(1, blade.life * 0.82);
                    context.fillStyle = blade.highlight;
                    context.beginPath();
                    context.arc(blade.x, blade.y, radius, 0, Math.PI * 2);
                    context.fill();
                }
                context.restore();
            }

            traceContours(1.85);
            context.save();
            context.shadowColor = 'rgba(255, 250, 240, 0.8)';
            context.shadowBlur = 20;
            context.shadowOffsetX = 0;
            context.shadowOffsetY = 0;
            context.globalAlpha = 1;
            context.fillStyle = coreGradient;
            context.fill();
            context.restore();

            renderInteractionFX();
        }

        function renderInteractionFX() {
            context.save();
            context.setTransform(dpr, 0, 0, dpr, 0, 0);
            context.lineCap = 'round';
            context.lineJoin = 'round';

            for (let index = 1; index < trail.length; index += 1) {
                const previous = trail[index - 1];
                const point = trail[index];
                const alpha = Math.max(0, point.life);
                context.globalAlpha = alpha * 0.42;
                context.strokeStyle = TRAIL_OUTER_COLOR;
                context.lineWidth = point.width * alpha * 0.55;
                context.beginPath();
                context.moveTo(previous.x, previous.y);
                context.lineTo(point.x, point.y);
                context.stroke();

                context.globalAlpha = alpha * 0.85;
                context.strokeStyle = 'rgba(255, 253, 249, 0.92)';
                context.lineWidth = point.width * alpha * 0.25;
                context.beginPath();
                context.moveTo(previous.x, previous.y);
                context.lineTo(point.x, point.y);
                context.stroke();
            }

            for (let index = 0; index < sparks.length; index += 1) {
                const spark = sparks[index];
                context.globalAlpha = Math.max(0, spark.life * 0.9);
                context.fillStyle = index % 2 === 0 ? OUTER_COLOR : '#FBF6EE';
                context.beginPath();
                context.arc(spark.x, spark.y, spark.radius * spark.life, 0, Math.PI * 2);
                context.fill();
            }
            context.restore();
        }

        function updatePhysics(deltaSeconds) {
            time += deltaSeconds;
            poolSliceCooldown = Math.max(0, poolSliceCooldown - deltaSeconds);
            for (let index = 0; index < poolNodes.length; index += 1) {
                const node = poolNodes[index];
                node.targetOffset *= Math.exp(-0.22 * deltaSeconds);
                node.viscousOffset += (node.targetOffset - node.viscousOffset)
                    * Math.min(1, deltaSeconds * 4.5);
            }

            for (let index = blobs.length - 1; index >= 0; index -= 1) {
                const blob = blobs[index];
                blob.birth = Math.min(1, blob.birth + deltaSeconds * blob.birthRate);
                blob.sliceCooldown = Math.max(0, blob.sliceCooldown - deltaSeconds);
                blob.heatFlash = Math.max(0, blob.heatFlash - deltaSeconds * 1.1);
                blob.carvePhase = Math.min(1, blob.carvePhase + deltaSeconds * 4.5);
                blob.y += (-blob.riseSpeed + blob.vy) * deltaSeconds;
                blob.x += blob.vx * deltaSeconds
                    + Math.sin(time * blob.swaySpeed + blob.swayPhase) * blob.swayAmplitude * deltaSeconds;
                blob.vx *= Math.exp(-2.1 * deltaSeconds);
                blob.vy *= Math.exp(-1.8 * deltaSeconds);
                const horizontalPad = blob.radius * 0.8;
                if (blob.x < horizontalPad) {
                    blob.x = horizontalPad;
                    blob.vx = Math.abs(blob.vx) * 0.5 + 8;
                } else if (blob.x > width - horizontalPad) {
                    blob.x = width - horizontalPad;
                    blob.vx = -Math.abs(blob.vx) * 0.5 - 8;
                }
                blob.phase2 += deltaSeconds * blob.phase2Rate;
                blob.phase3 += deltaSeconds * blob.phase3Rate;
                blob.phase4 += deltaSeconds * blob.phase4Rate;
                blob.lobePhase += deltaSeconds * blob.lobeRate;
                blob.orientation += deltaSeconds * blob.angularVelocity;
                blob.spineBend = clamp(
                    blob.spineBend + (Math.sin(blob.phase2 * 0.72) * 0.12 - blob.spineBend) * deltaSeconds * 0.34,
                    -0.34,
                    0.34,
                );
                blob.taper = clamp(
                    blob.taper + (Math.cos(blob.phase3 * 0.80) * 0.12 - blob.taper) * deltaSeconds * 0.30,
                    -0.24,
                    0.28,
                );
                const deformRelax = Math.exp(-1.9 * deltaSeconds);
                blob.cutDeform2X *= deformRelax;
                blob.cutDeform2Y *= deformRelax;
                blob.cutDeform3X *= deformRelax;
                blob.cutDeform3Y *= deformRelax;

                const shrinkEnd = -height * 0.16;
                if (blob.y <= shrinkEnd) blobs.splice(index, 1);
            }

            for (let index = 0; index < blobs.length; index += 1) {
                const first = blobs[index];
                const second = first.separationPartner;
                if (!second || first.separationCooldown <= 0) continue;
                const secondIndex = blobs.indexOf(second);
                if (secondIndex < 0) {
                    first.separationPartner = null;
                    first.separationCooldown = 0;
                    continue;
                }
                if (index > secondIndex) continue;

                const remaining = Math.max(0,
                    Math.min(first.separationCooldown, second.separationCooldown) - deltaSeconds);
                const dx = second.x - first.x;
                const dy = second.y - first.y;
                const distance = Math.hypot(dx, dy);
                const requiredDistance = (first.radius + second.radius) * SIBLING_SEPARATION_FACTOR;
                if (distance < requiredDistance) {
                    const nx = distance > 0.001 ? dx / distance : first.separationNormalX;
                    const ny = distance > 0.001 ? dy / distance : first.separationNormalY;
                    const normalLength = Math.hypot(nx, ny) || 1;
                    const unitX = nx / normalLength;
                    const unitY = ny / normalLength;
                    const totalMove = Math.min(requiredDistance - distance, SIBLING_SEPARATION_SPEED * deltaSeconds);
                    const halfMove = totalMove * 0.5;
                    let firstX = first.x - unitX * halfMove;
                    let firstY = first.y - unitY * halfMove;
                    let secondX = second.x + unitX * halfMove;
                    let secondY = second.y + unitY * halfMove;
                    let shiftX = 0;
                    const minimumExtent = Math.min(
                        firstX - first.radius * 0.8,
                        secondX - second.radius * 0.8,
                    );
                    const maximumExtent = Math.max(
                        firstX + first.radius * 0.8,
                        secondX + second.radius * 0.8,
                    );
                    if (minimumExtent + shiftX < 0) shiftX = -minimumExtent;
                    if (maximumExtent + shiftX > width) shiftX += width - (maximumExtent + shiftX);
                    first.x = firstX + shiftX;
                    first.y = firstY;
                    second.x = secondX + shiftX;
                    second.y = secondY;
                }

                first.separationCooldown = remaining;
                second.separationCooldown = remaining;
                if (remaining <= 0) {
                    first.separationPartner = null;
                    second.separationPartner = null;
                }
            }

            for (let index = bladeNodes.length - 1; index >= 0; index -= 1) {
                bladeNodes[index].life = Math.max(0, bladeNodes[index].life - deltaSeconds * 3.8);
                if (bladeNodes[index].life <= 0) bladeNodes.splice(index, 1);
            }
            for (let index = sparks.length - 1; index >= 0; index -= 1) {
                const spark = sparks[index];
                spark.life = Math.max(0, spark.life - deltaSeconds * spark.decay);
                spark.vy += 36 * deltaSeconds;
                spark.x += spark.vx * deltaSeconds;
                spark.y += spark.vy * deltaSeconds;
                spark.vx *= Math.exp(-1.4 * deltaSeconds);
                spark.vy *= Math.exp(-1.1 * deltaSeconds);
                if (spark.life <= 0) sparks.splice(index, 1);
            }
            for (let index = trail.length - 1; index >= 0; index -= 1) {
                trail[index].life = Math.max(0, trail[index].life - deltaSeconds * 2.7);
                if (trail[index].life <= 0) trail.splice(index, 1);
            }

            const targetCount = getBlobLimit();
            if (blobs.length < targetCount) {
                spawnTimer += deltaSeconds;
                if (spawnTimer >= 2.6) {
                    spawnTimer = 0;
                    const poolHalfWidth = getPoolHalfWidth();
                    newBlob({
                        x: width * 0.5 + randomBetween(-poolHalfWidth * 0.66, poolHalfWidth * 0.66),
                        y: height + randomBetween(18, 52),
                    });
                }
            } else {
                spawnTimer = Math.min(spawnTimer, 1.8);
            }
        }

        let spawnTimer = 0;

        function shouldPause() {
            return disposed || document.hidden || Boolean(reduceMotionQuery.matches);
        }

        function frame(timestamp) {
            frameId = null;
            if (shouldPause()) {
                previousFrameTime = null;
                accumulatedMs = 0;
                if (reduceMotionQuery.matches) renderFrame();
                return;
            }

            if (previousFrameTime === null) {
                previousFrameTime = timestamp;
                renderFrame();
            } else {
                const deltaMs = clamp(timestamp - previousFrameTime, 0, 100);
                previousFrameTime = timestamp;
                accumulatedMs = Math.min(100, accumulatedMs + deltaMs);
                if (accumulatedMs + 0.0001 >= UPDATE_INTERVAL_MS) {
                    const updateCount = Math.floor((accumulatedMs + 0.0001) / UPDATE_INTERVAL_MS);
                    const updateMs = updateCount * UPDATE_INTERVAL_MS;
                    accumulatedMs = Math.max(0, accumulatedMs - updateMs);
                    updatePhysics(updateMs / 1000);
                    renderFrame();
                }
            }

            frameId = window.requestAnimationFrame(frame);
        }

        function resetPointer() {
            lastPointer = null;
            lastPointerEventTime = null;
        }

        function clearTransientInteraction() {
            resetPointer();
            bladeNodes.length = 0;
            sparks.length = 0;
            trail.length = 0;
            poolSliceCooldown = 0;
            for (let index = 0; index < poolNodes.length; index += 1) {
                poolNodes[index].targetOffset = 0;
                poolNodes[index].viscousOffset = 0;
            }
        }

        function isInteractiveTarget(target) {
            if (!target || typeof target.closest !== 'function') return false;
            try {
                return Boolean(target.closest(INTERACTIVE_TARGETS));
            } catch (error) {
                return true;
            }
        }

        function handlePointerMove(event) {
            if (disposed || !active || !initialized || document.hidden || reduceMotionQuery.matches
                || (event.pointerType && event.pointerType !== 'mouse') || isInteractiveTarget(event.target)) {
                resetPointer();
                return;
            }

            const x = event.clientX - left;
            const y = event.clientY - top;
            if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || x > width || y < 0 || y > height) {
                resetPointer();
                return;
            }
            const eventTime = Number.isFinite(event.timeStamp)
                ? event.timeStamp
                : (lastPointerEventTime === null ? 0 : lastPointerEventTime + 1000 / 60);
            lastPointerEventTime = eventTime;
            if (!lastPointer) {
                lastPointer = { x, y, time: eventTime };
                return;
            }

            const x1 = lastPointer.x;
            const y1 = lastPointer.y;
            const dx = x - x1;
            const dy = y - y1;
            const distance = Math.hypot(dx, dy);
            if (distance < POINTER_CUT_MIN_DISTANCE) return;
            const elapsedSeconds = Math.max(0.008, (eventTime - lastPointer.time) / 1000);
            const swipeSpeed = clamp(distance / elapsedSeconds, 45, 1600);
            lastPointer = { x, y, time: eventTime };

            const dirX = dx / distance;
            const dirY = dy / distance;
            if (!trail.length) {
                trail.push({ x: x1, y: y1, life: 0.42, width: clamp(distance * 0.25, 3.5, 12) });
            }
            trail.push({ x, y, life: 0.42, width: clamp(distance * 0.25, 3.5, 12) });
            if (trail.length > MAX_TRAIL_POINTS) trail.shift();

            const steps = Math.min(4, Math.max(1, Math.ceil(distance / 15)));
            const startingCount = blobs.length;
            tryCarveBottomPool(x1, y1, x, y, dirX, dirY, distance, swipeSpeed);
            for (let step = 1; step <= steps; step += 1) {
                const amount = step / steps;
                const bladeX = x1 + dx * amount;
                const bladeY = y1 + dy * amount;
                addBladeNode(bladeX, bladeY, clamp(11 + distance * 0.30, 13, 28));
                const localSurfaceY = getPoolSurfaceY(bladeX);
                if (Math.abs(bladeY - localSurfaceY) < 38) {
                    deformViscousPool(bladeX, clamp(dy * 0.18, -7, 10), 88);
                }
            }

            for (let index = 0; index < startingCount; index += 1) {
                const blob = blobs[index];
                const effectiveRadius = blob.radius * smootherstep(blob.birth)
                    * blob.carvePhase * getBlobShrink(blob.y);
                if (effectiveRadius < 13.5) continue;
                const segment = computePointToSegment(blob.x, blob.y, x1, y1, x, y);
                if (blob.sliceCooldown <= 0
                    && segmentCutsVisibleBlob(blob, effectiveRadius, segment, x1, y1, x, y)) {
                    splitBlob(blob, segment, dirX, dirY, swipeSpeed, effectiveRadius, x1, y1, x, y);
                } else if (segment.distance < effectiveRadius * 1.45) {
                    const side = segment.signedDistance >= 0 ? 1 : -1;
                    const push = (1 - segment.distance / (effectiveRadius * 1.45)) * 0.34;
                    blob.vx += (dirX * 0.5 + segment.nx * side * 0.65) * push * 30;
                    blob.vy += (dirY * 0.5 + segment.ny * side * 0.65) * push * 30;
                    blob.cutDeform2X += dirX * push * 0.25;
                    blob.cutDeform2Y += dirY * push * 0.25;
                    blob.cutDeform3X += segment.nx * push * 0.22;
                    blob.cutDeform3Y += segment.ny * push * 0.22;
                }
            }
        }

        function handlePointerOut(event) {
            if (!event || !event.relatedTarget) resetPointer();
        }

        function handleVisibilityChange() {
            if (document.hidden) {
                stop(false);
                clearTransientInteraction();
            }
        }

        function removeInteractionListeners() {
            if (typeof window.removeEventListener === 'function') {
                window.removeEventListener('pointermove', handlePointerMove);
                window.removeEventListener('pointerleave', resetPointer);
                window.removeEventListener('pointerout', handlePointerOut);
                window.removeEventListener('blur', resetPointer);
            }
            if (typeof document.removeEventListener === 'function') {
                document.removeEventListener('visibilitychange', handleVisibilityChange);
            }
        }

        if (typeof window.addEventListener === 'function') {
            window.addEventListener('pointermove', handlePointerMove, { passive: true });
            window.addEventListener('pointerleave', resetPointer, { passive: true });
            window.addEventListener('pointerout', handlePointerOut, { passive: true });
            window.addEventListener('blur', resetPointer, { passive: true });
        }
        if (typeof document.addEventListener === 'function') {
            document.addEventListener('visibilitychange', handleVisibilityChange);
        }

        function resize(nextWidth, nextHeight, nextLeft = left, nextTop = top) {
            if (disposed || !Number.isFinite(nextWidth) || !Number.isFinite(nextHeight)
                || nextWidth <= 0 || nextHeight <= 0) return;

            const oldWidth = width;
            const oldHeight = height;
            const wasMobile = oldWidth > 0 && oldWidth <= 768;
            if (oldWidth > 0) clearTransientInteraction();
            width = nextWidth;
            height = nextHeight;
            left = Number.isFinite(nextLeft) ? nextLeft : 0;
            top = Number.isFinite(nextTop) ? nextTop : 0;
            layer.style.left = `${left}px`;
            layer.style.top = `${top}px`;
            layer.style.width = `${width}px`;
            layer.style.height = `${height}px`;

            if (oldWidth > 0 && oldHeight > 0 && initialized) {
                const xScale = width / oldWidth;
                const yScale = height / oldHeight;
                const radiusScale = Math.min(xScale, yScale);
                for (const blob of blobs) {
                    blob.x *= xScale;
                    blob.y *= yScale;
                    blob.radius = clamp(blob.radius * radiusScale, 1, isMobile() ? 40 : 65);
                }
                if (wasMobile !== isMobile()) {
                    seedPoolNodes();
                    if (isMobile() && blobs.length > getBlobLimit()) blobs.length = getBlobLimit();
                }
                if (blobs.length > getBlobHardLimit()) blobs.length = getBlobHardLimit();
            }

            dpr = Math.min(window.devicePixelRatio || 1, 1.25);
            const pixelWidth = Math.max(1, Math.ceil(width * dpr));
            const pixelHeight = Math.max(1, Math.ceil(height * dpr));
            if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
            if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
            setupGrid();
            rebuildGradients();
            if (!initialized) {
                seedPoolNodes();
                seedInitialBlobs();
                initialized = true;
            }
            previousFrameTime = null;
            accumulatedMs = 0;
            poolAuraReady = false;
            if (reduceMotionQuery.matches) renderFrame();
        }

        function start() {
            if (disposed || width <= 0 || height <= 0 || frameId !== null) return;
            if (document.hidden) {
                stop(false);
                return;
            }
            if (reduceMotionQuery.matches) {
                stop(true);
                return;
            }
            active = true;
            resetPointer();
            previousFrameTime = null;
            accumulatedMs = 0;
            frameId = window.requestAnimationFrame(frame);
        }

        function stop(makeStatic = false) {
            if (frameId !== null) window.cancelAnimationFrame(frameId);
            frameId = null;
            active = false;
            previousFrameTime = null;
            accumulatedMs = 0;
            clearTransientInteraction();
            if (makeStatic) renderFrame();
        }

        function dispose() {
            if (disposed) return;
            stop(false);
            disposed = true;
            blobs.length = 0;
            poolNodes.length = 0;
            grid = new Float32Array(0);
            visited = new Uint8Array(0);
            poolSurfaceY = new Float32Array(0);
            coreGradient = null;
            poolAuraCanvas = null;
            poolAuraContext = null;
            poolAuraImageData = null;
            poolAuraPixels = new Uint8ClampedArray(0);
            poolAuraField = new Float32Array(0);
            poolAuraDistance = new Float32Array(0);
            poolAuraCols = 0;
            poolAuraRows = 0;
            poolAuraStartRow = 0;
            poolAuraTop = 0;
            poolAuraHeight = 0;
            poolAuraReady = false;
            layer.remove();
            removeInteractionListeners();
        }

        return { resize, start, stop, dispose };
    }

    window.LavaBackground = { create };
})();
