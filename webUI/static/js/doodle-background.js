(function () {
    'use strict';

    const MANIFEST_URL = '/static/assets/doodles/manifest.json';
    const ASSET_BASE_URL = '/static/assets/doodles/';
    const LAYER_ID = 'doodle-background-layer';
    const DESKTOP_SCALE = 0.42;
    const MOBILE_SCALE = 0.30;
    const MOBILE_BREAKPOINT = 768;
    const PLACEMENT_CLEARANCE = 12;
    const EDGE_CLEARANCE = 18;
    const MAX_PLACEMENT_ATTEMPTS = 180;
    const COLLISION_PADDING = 1.5;
    const COLLISION_CONTACT_BIAS = 0.01;
    const MAX_COLLISION_ITERATIONS = 8;
    const MAX_PHYSICS_STEP_SECONDS = 1 / 120;
    let cleanupActiveMount = null;
    let lifecycleActive = false;
    let pageSuspended = false;
    let lifecycleGeneration = 0;
    let startupGateObserver = null;
    let pendingMountCallback = null;
    let domReadyHandler = null;

    function isMotionReduced() {
        return window.BackgroundMotion?.isReduced?.() === true;
    }

    function clamp(value, minimum, maximum) {
        return Math.min(maximum, Math.max(minimum, value));
    }

    function isMobileViewport(width) {
        return width <= MOBILE_BREAKPOINT;
    }

    function shuffle(items) {
        for (let index = items.length - 1; index > 0; index -= 1) {
            const swapIndex = Math.floor(Math.random() * (index + 1));
            [items[index], items[swapIndex]] = [items[swapIndex], items[index]];
        }
        return items;
    }

    function getVisibleCount(width, height, isMobile) {
        return clamp(Math.round((width * height) / 70000), 3, isMobile ? 6 : 14);
    }

    function overlaps(candidate, placed) {
        return placed.some((item) => (
            candidate.left - PLACEMENT_CLEARANCE < item.left + item.width + PLACEMENT_CLEARANCE
            && candidate.left + candidate.width + PLACEMENT_CLEARANCE > item.left - PLACEMENT_CLEARANCE
            && candidate.top - PLACEMENT_CLEARANCE < item.top + item.height + PLACEMENT_CLEARANCE
            && candidate.top + candidate.height + PLACEMENT_CLEARANCE > item.top - PLACEMENT_CLEARANCE
        ));
    }

    function getPlacement(asset, scale, bounds, placed) {
        const size = {
            width: asset.width * scale,
            height: asset.height * scale,
        };
        const minLeft = EDGE_CLEARANCE;
        const minTop = EDGE_CLEARANCE;
        const maxLeft = bounds.width - size.width - EDGE_CLEARANCE;
        const maxTop = bounds.height - size.height - EDGE_CLEARANCE;

        if (maxLeft < minLeft || maxTop < minTop) return null;

        for (let attempt = 0; attempt < MAX_PLACEMENT_ATTEMPTS; attempt += 1) {
            const candidate = {
                left: minLeft + Math.random() * (maxLeft - minLeft),
                top: minTop + Math.random() * (maxTop - minTop),
                width: size.width,
                height: size.height,
            };

            if (!overlaps(candidate, placed)) return candidate;
        }

        return null;
    }

    function isValidAsset(asset) {
        return asset
            && typeof asset.filename === 'string'
            && /^[a-zA-Z0-9_-]+\.svg$/.test(asset.filename)
            && Number.isFinite(asset.width)
            && asset.width > 0
            && Number.isFinite(asset.height)
            && asset.height > 0;
    }

    function randomSigned(minimum, maximum) {
        const magnitude = minimum + Math.random() * (maximum - minimum);
        return Math.random() < 0.5 ? -magnitude : magnitude;
    }

    function makeDoodleItem(asset, placement, scale, bounds) {
        const width = asset.width * scale;
        const height = asset.height * scale;

        const item = document.createElement('div');
        item.className = 'doodle-background-item';
        item.setAttribute('aria-hidden', 'true');
        item.style.left = '0px';
        item.style.top = '0px';
        item.style.width = `${width}px`;
        item.style.height = `${height}px`;

        const drift = document.createElement('div');
        drift.className = 'doodle-background-drift';

        const image = document.createElement('img');
        image.src = `${ASSET_BASE_URL}${encodeURIComponent(asset.filename)}`;
        image.alt = '';
        image.setAttribute('aria-hidden', 'true');
        image.draggable = false;
        image.decoding = 'async';
        drift.appendChild(image);
        item.appendChild(drift);
        item.motion = {
            element: item,
            width,
            height,
            x: placement.left,
            y: placement.top,
            baseY: placement.top,
            speed: 3 + Math.random() * 9,
            phase: Math.random() * Math.PI * 2,
            sway: randomSigned(7, 18),
            rotation: randomSigned(8, 15),
            rotationPhase: Math.random() * Math.PI * 2,
            rotationRate: randomSigned(0.1, 0.24),
            avoidX: 0,
            avoidY: 0,
            velocityX: 0,
            velocityY: 0,
            fallbackAngle: Math.random() * Math.PI * 2,
        };
        return item;
    }

    function createMotionSystem(layer, onPauseDetected) {
        let width = 0;
        let height = 0;
        let boundsLeft = 0;
        let boundsTop = 0;
        let doodles = [];
        let frameId = null;
        let previousFrameTime = null;
        let disposed = false;
        let elapsed = 0;
        let accumulatedDeltaMs = 0;
        let pointer = null;

        function shouldPause() {
            return disposed || document.hidden || isMotionReduced();
        }

        function randomBetween(minimum, maximum) {
            return minimum + Math.random() * (maximum - minimum);
        }

        function getDoodlePose(doodle, staticPose = false) {
            const sway = staticPose ? 0 : Math.sin(elapsed * 0.32 + doodle.phase) * doodle.sway;
            const rotation = staticPose ? 0 : doodle.rotation
                + Math.sin(elapsed * doodle.rotationRate + doodle.rotationPhase) * 7;
            const x = doodle.x + sway + (staticPose ? 0 : doodle.avoidX);
            const y = doodle.y + (staticPose ? 0 : doodle.avoidY);
            const angle = rotation * (Math.PI / 180);
            return {
                x,
                y,
                centerX: x + doodle.width / 2,
                centerY: y + doodle.height / 2,
                angle,
                cosine: Math.cos(angle),
                sine: Math.sin(angle),
            };
        }

        function getCollider(doodle, pose) {
            const halfWidth = doodle.width / 2 + COLLISION_PADDING;
            const halfHeight = doodle.height / 2 + COLLISION_PADDING;
            const extentX = halfWidth * Math.abs(pose.cosine) + halfHeight * Math.abs(pose.sine);
            const extentY = halfWidth * Math.abs(pose.sine) + halfHeight * Math.abs(pose.cosine);
            return {
                centerX: pose.centerX,
                centerY: pose.centerY,
                axisX: { x: pose.cosine, y: pose.sine },
                axisY: { x: -pose.sine, y: pose.cosine },
                halfWidth,
                halfHeight,
                left: pose.centerX - extentX,
                right: pose.centerX + extentX,
                top: pose.centerY - extentY,
                bottom: pose.centerY + extentY,
            };
        }

        function getContactNormal(first, second, firstIndex, secondIndex) {
            if (first.right <= second.left || second.right <= first.left
                || first.bottom <= second.top || second.bottom <= first.top) return null;

            const axes = [first.axisX, first.axisY, second.axisX, second.axisY];
            const centerX = second.centerX - first.centerX;
            const centerY = second.centerY - first.centerY;
            let minimumOverlap = Infinity;
            let normalX = 1;
            let normalY = 0;

            for (const axis of axes) {
                const firstRadius = first.halfWidth * Math.abs(first.axisX.x * axis.x + first.axisX.y * axis.y)
                    + first.halfHeight * Math.abs(first.axisY.x * axis.x + first.axisY.y * axis.y);
                const secondRadius = second.halfWidth * Math.abs(second.axisX.x * axis.x + second.axisX.y * axis.y)
                    + second.halfHeight * Math.abs(second.axisY.x * axis.x + second.axisY.y * axis.y);
                const projectedDistance = centerX * axis.x + centerY * axis.y;
                const overlap = firstRadius + secondRadius - Math.abs(projectedDistance);
                if (overlap <= 0) return null;
                if (overlap < minimumOverlap) {
                    minimumOverlap = overlap;
                    // A zero projection (including coincident centers) uses the stable pair order.
                    const direction = Math.abs(projectedDistance) < 1e-9
                        ? (firstIndex < secondIndex ? 1 : -1)
                        : (projectedDistance > 0 ? 1 : -1);
                    normalX = axis.x * direction;
                    normalY = axis.y * direction;
                }
            }

            return {
                x: normalX,
                y: normalY,
                depth: minimumOverlap + COLLISION_CONTACT_BIAS,
            };
        }

        function solveDoodleCollisions(staticPose = false) {
            if (doodles.length < 2) return;

            for (let iteration = 0; iteration < MAX_COLLISION_ITERATIONS; iteration += 1) {
                const poses = doodles.map((doodle) => getDoodlePose(doodle, staticPose));
                let correctedPairs = 0;

                for (let firstIndex = 0; firstIndex < doodles.length - 1; firstIndex += 1) {
                    const firstDoodle = doodles[firstIndex];
                    const firstPose = poses[firstIndex];
                    for (let secondIndex = firstIndex + 1; secondIndex < doodles.length; secondIndex += 1) {
                        const secondDoodle = doodles[secondIndex];
                        const secondPose = poses[secondIndex];
                        const contact = getContactNormal(
                            getCollider(firstDoodle, firstPose),
                            getCollider(secondDoodle, secondPose),
                            firstIndex,
                            secondIndex,
                        );
                        if (!contact) continue;

                        const halfDepth = contact.depth / 2;
                        const moveX = contact.x * halfDepth;
                        const moveY = contact.y * halfDepth;
                        firstDoodle.x -= moveX;
                        firstDoodle.y -= moveY;
                        secondDoodle.x += moveX;
                        secondDoodle.y += moveY;
                        firstPose.x -= moveX;
                        firstPose.y -= moveY;
                        firstPose.centerX -= moveX;
                        firstPose.centerY -= moveY;
                        secondPose.x += moveX;
                        secondPose.y += moveY;
                        secondPose.centerX += moveX;
                        secondPose.centerY += moveY;
                        correctedPairs += 1;
                    }
                }

                if (correctedPairs === 0) break;
            }
        }

        function renderDoodles() {
            for (const doodle of doodles) {
                const pose = getDoodlePose(doodle);
                const edgeFade = Math.min(
                    clamp((height - doodle.y) / 90, 0, 1),
                    clamp((doodle.y + doodle.height) / 90, 0, 1),
                );
                const rotation = pose.angle * (180 / Math.PI);
                doodle.element.style.transform = `translate3d(${pose.x.toFixed(2)}px, ${pose.y.toFixed(2)}px, 0) rotate(${rotation.toFixed(2)}deg)`;
                doodle.element.style.opacity = (0.34 * edgeFade).toFixed(3);
            }
        }

        function resize(nextWidth, nextHeight, nextLeft = boundsLeft, nextTop = boundsTop) {
            if (nextWidth <= 0 || nextHeight <= 0) return;
            width = nextWidth;
            height = nextHeight;
            boundsLeft = nextLeft;
            boundsTop = nextTop;
        }

        function pointerTarget(doodle) {
            if (!pointer) return { x: 0, y: 0 };
            const centerX = doodle.x + doodle.width / 2 + doodle.avoidX;
            const centerY = doodle.y + doodle.height / 2 + doodle.avoidY;
            let dx = centerX - pointer.x;
            let dy = centerY - pointer.y;
            let distance = Math.hypot(dx, dy);
            const radius = 132;
            if (distance >= radius) return { x: 0, y: 0 };
            if (distance < 0.001) {
                dx = Math.cos(doodle.fallbackAngle);
                dy = Math.sin(doodle.fallbackAngle);
                distance = 1;
            }
            const force = 66 * ((radius - distance) / radius);
            return { x: (dx / distance) * force, y: (dy / distance) * force };
        }

        function isResponsiveMode() {
            for (const doodle of doodles) {
                const target = pointerTarget(doodle);
                if (target.x !== 0 || target.y !== 0) return true;
                if (Math.abs(doodle.avoidX) > 0.05
                    || Math.abs(doodle.avoidY) > 0.05
                    || Math.abs(doodle.velocityX) > 0.1
                    || Math.abs(doodle.velocityY) > 0.1) return true;
            }
            return false;
        }

        function updateDoodles(deltaSeconds) {
            const stepCount = Math.max(1, Math.ceil(deltaSeconds / MAX_PHYSICS_STEP_SECONDS));
            const stepSeconds = deltaSeconds / stepCount;

            for (let step = 0; step < stepCount; step += 1) {
                elapsed += stepSeconds;
                for (const doodle of doodles) {
                    doodle.y -= doodle.speed * stepSeconds;
                    if (doodle.y + doodle.height < -EDGE_CLEARANCE) {
                        doodle.y = height + EDGE_CLEARANCE + randomBetween(0, 90);
                        doodle.x = clamp(
                            randomBetween(EDGE_CLEARANCE, Math.max(EDGE_CLEARANCE, width - doodle.width - EDGE_CLEARANCE)),
                            0,
                            Math.max(0, width - doodle.width),
                        );
                        doodle.phase = Math.random() * Math.PI * 2;
                    }

                    const target = pointerTarget(doodle);
                    const spring = 28;
                    const damping = 10;
                    doodle.velocityX += ((target.x - doodle.avoidX) * spring - doodle.velocityX * damping) * stepSeconds;
                    doodle.velocityY += ((target.y - doodle.avoidY) * spring - doodle.velocityY * damping) * stepSeconds;
                    doodle.avoidX = clamp(doodle.avoidX + doodle.velocityX * stepSeconds, -66, 66);
                    doodle.avoidY = clamp(doodle.avoidY + doodle.velocityY * stepSeconds, -66, 66);
                }
                solveDoodleCollisions();
            }

            renderDoodles();
        }

        function setStaticDoodles() {
            for (const doodle of doodles) {
                doodle.avoidX = 0;
                doodle.avoidY = 0;
                doodle.velocityX = 0;
                doodle.velocityY = 0;
                doodle.y = doodle.baseY;
            }
            solveDoodleCollisions(true);
            for (const doodle of doodles) {
                doodle.baseY = doodle.y;
                doodle.element.style.transform = `translate3d(${doodle.x}px, ${doodle.y}px, 0)`;
                doodle.element.style.opacity = '0.34';
            }
        }

        function frame(timestamp) {
            frameId = null;
            if (shouldPause()) {
                previousFrameTime = null;
                accumulatedDeltaMs = 0;
                onPauseDetected();
                return;
            }

            const frameDeltaMs = previousFrameTime === null
                ? 0
                : clamp(timestamp - previousFrameTime, 0, 100);
            previousFrameTime = timestamp;
            accumulatedDeltaMs += frameDeltaMs;

            const updateIntervalMs = isResponsiveMode() ? 1000 / 60 : 1000 / 30;
            const elapsedIntervals = Math.floor((accumulatedDeltaMs + 0.0001) / updateIntervalMs);
            if (elapsedIntervals > 0) {
                const updateDeltaMs = elapsedIntervals * updateIntervalMs;
                accumulatedDeltaMs -= updateDeltaMs;
                const deltaSeconds = updateDeltaMs / 1000;
                updateDoodles(deltaSeconds);
            }
            frameId = window.requestAnimationFrame(frame);
        }

        function start() {
            if (width <= 0 || height <= 0 || !doodles.length || frameId !== null || shouldPause()) return;
            previousFrameTime = null;
            accumulatedDeltaMs = 0;
            frameId = window.requestAnimationFrame(frame);
        }

        function stop(makeStatic = false) {
            if (frameId !== null) window.cancelAnimationFrame(frameId);
            frameId = null;
            previousFrameTime = null;
            accumulatedDeltaMs = 0;
            if (makeStatic) setStaticDoodles();
        }

        function dispose() {
            disposed = true;
            stop();
            window.removeEventListener('pointermove', onPointerMove);
            window.removeEventListener('blur', clearPointer);
            document.removeEventListener('pointerleave', clearPointer);
        }

        function setDoodles(items) {
            doodles = items;
            if (isMotionReduced()) {
                setStaticDoodles();
                return;
            }
            solveDoodleCollisions();
            renderDoodles();
        }

        function onPointerMove(event) {
            if (event.pointerType && event.pointerType !== 'mouse') return;
            const x = event.clientX - boundsLeft;
            const y = event.clientY - boundsTop;
            pointer = x >= 0 && x <= width && y >= 0 && y <= height ? { x, y } : null;
        }

        function clearPointer() {
            pointer = null;
        }

        window.addEventListener('pointermove', onPointerMove, { passive: true });
        window.addEventListener('blur', clearPointer);
        document.addEventListener('pointerleave', clearPointer);

        return { resize, start, stop, dispose, setDoodles, clearPointer };
    }

    function isStartupScreenVisible() {
        const screen = document.getElementById('startup-screen');
        if (!screen || screen.hidden) return false;

        const style = window.getComputedStyle(screen);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
            return false;
        }
        // Opacity is intentionally ignored: the startup screen still covers the page during its fade.
        return typeof screen.getClientRects !== 'function' || screen.getClientRects().length > 0;
    }

    function cancelPendingMountCallback() {
        if (!pendingMountCallback) return;
        if (pendingMountCallback.kind === 'idle' && typeof window.cancelIdleCallback === 'function') {
            window.cancelIdleCallback(pendingMountCallback.id);
        } else {
            window.clearTimeout(pendingMountCallback.id);
        }
        pendingMountCallback = null;
    }

    function disconnectStartupGate() {
        if (startupGateObserver) startupGateObserver.disconnect();
        startupGateObserver = null;
    }

    function scheduleMount(generation) {
        if (!lifecycleActive || pageSuspended || generation !== lifecycleGeneration || pendingMountCallback) return;

        const run = () => {
            if (!pendingMountCallback || pendingMountCallback.generation !== generation) return;
            pendingMountCallback = null;
            if (!lifecycleActive || pageSuspended || generation !== lifecycleGeneration) return;
            if (isStartupScreenVisible()) {
                watchStartupScreen(generation);
                return;
            }
            mountDoodles();
        };

        if (typeof window.requestIdleCallback === 'function') {
            const pending = { kind: 'idle', id: null, generation };
            pendingMountCallback = pending;
            pending.id = window.requestIdleCallback(run, { timeout: 200 });
        } else {
            const pending = { kind: 'timeout', id: null, generation };
            pendingMountCallback = pending;
            pending.id = window.setTimeout(run, 80);
        }
    }

    function watchStartupScreen(generation) {
        if (!lifecycleActive || pageSuspended || generation !== lifecycleGeneration) return;
        const screen = document.getElementById('startup-screen');
        if (!screen || !isStartupScreenVisible()) {
            scheduleMount(generation);
            return;
        }
        if (startupGateObserver || typeof MutationObserver !== 'function') return;

        const observer = new MutationObserver(() => {
            if (!lifecycleActive || pageSuspended || generation !== lifecycleGeneration) return;
            if (isStartupScreenVisible()) return;
            if (startupGateObserver === observer) startupGateObserver = null;
            observer.disconnect();
            scheduleMount(generation);
        });
        startupGateObserver = observer;
        const attributeOptions = {
            childList: true,
            attributes: true,
            attributeFilter: ['style', 'class', 'hidden'],
        };
        observer.observe(screen, { attributes: true, attributeFilter: attributeOptions.attributeFilter });
        for (let ancestor = screen.parentElement; ancestor; ancestor = ancestor.parentElement) {
            observer.observe(ancestor, attributeOptions);
        }
    }

    function mountDoodles() {
        if (!lifecycleActive || pageSuspended || isStartupScreenVisible()
            || cleanupActiveMount || !document.body || document.getElementById(LAYER_ID)) return;

        const layer = document.createElement('div');
        layer.id = LAYER_ID;
        layer.setAttribute('aria-hidden', 'true');
        layer.dataset.paused = document.hidden || isMotionReduced() ? 'true' : 'false';

        document.body.insertBefore(layer, document.body.firstChild);

        let lavaController = null;
        try {
            if (window.LavaBackground && typeof window.LavaBackground.create === 'function') {
                lavaController = window.LavaBackground.create();
            }
        } catch (error) {
            console.warn('[doodle-background] Unable to create the lava background.', error);
        }

        let assets = [];
        let lastWidth = 0;
        let lastHeight = 0;
        let observedWidth = 0;
        let observedHeight = 0;
        let pendingResizeSize = null;
        let resizeTimer = null;
        let resizeObserver = null;
        let fetchController = typeof AbortController === 'function' ? new AbortController() : null;
        let unsubscribeMotionPreference = () => {};
        const motionSystem = createMotionSystem(layer, () => {
            syncMotionState();
        });

        function renderDoodles(width, height) {
            if (!assets.length || width <= 0 || height <= 0) return;

            const isMobile = isMobileViewport(width);
            const scale = isMobile ? MOBILE_SCALE : DESKTOP_SCALE;
            const targetCount = Math.min(assets.length, getVisibleCount(width, height, isMobile));
            const candidates = shuffle(assets.slice()).slice(0, targetCount);
            const bounds = { width, height };
            const placements = [];
            const placedAssets = [];

            candidates.sort((left, right) => right.width * right.height - left.width * left.height);
            for (const asset of candidates) {
                const placement = getPlacement(asset, scale, bounds, placements);
                if (!placement) continue;
                placements.push(placement);
                placedAssets.push({ asset, placement });
            }

            const items = placedAssets.map(({ asset, placement }) => makeDoodleItem(asset, placement, scale, bounds));
            layer.replaceChildren(...items);
            motionSystem.setDoodles(items.map((item) => item.motion));
            syncMotionState();
        }

        function applyResize(width, height, forceRender = false) {
            if (width <= 0 || height <= 0) return;
            const changed = Math.abs(width - lastWidth) >= 1 || Math.abs(height - lastHeight) >= 1;
            lastWidth = width;
            lastHeight = height;
            const rect = layer.getBoundingClientRect();
            motionSystem.resize(width, height, rect.left, rect.top);
            if (lavaController && changed) lavaController.resize(width, height, rect.left, rect.top);
            if ((changed || forceRender) && assets.length) renderDoodles(width, height);
            else syncMotionState();
        }

        function flushResize() {
            resizeTimer = null;
            if (!pendingResizeSize) return;
            const { width, height } = pendingResizeSize;
            pendingResizeSize = null;
            applyResize(width, height);
        }

        function onResize(entries) {
            const entry = entries && entries[0];
            const contentRect = entry ? entry.contentRect : layer.getBoundingClientRect();
            const width = contentRect.width;
            const height = contentRect.height;
            if (width <= 0 || height <= 0
                || (Math.abs(width - observedWidth) < 1 && Math.abs(height - observedHeight) < 1)) return;

            observedWidth = width;
            observedHeight = height;
            const layerRect = layer.getBoundingClientRect();
            motionSystem.resize(width, height, layerRect.left, layerRect.top);

            if (lastWidth <= 0 || lastHeight <= 0) {
                if (resizeTimer !== null) window.clearTimeout(resizeTimer);
                resizeTimer = null;
                pendingResizeSize = null;
                applyResize(width, height);
                return;
            }

            pendingResizeSize = { width, height };
            if (resizeTimer !== null) window.clearTimeout(resizeTimer);
            resizeTimer = window.setTimeout(flushResize, 100);
        }

        function syncMotionState() {
            const reduced = isMotionReduced();
            const paused = document.hidden || reduced;
            layer.dataset.paused = paused ? 'true' : 'false';
            if (reduced) {
                motionSystem.stop(true);
                if (lavaController) lavaController.stop(true);
            } else if (paused) {
                motionSystem.stop();
                if (lavaController) lavaController.stop();
            } else {
                motionSystem.start();
                if (lavaController) lavaController.start();
            }
        }

        function onVisibilityChange() {
            syncMotionState();
        }

        function onMotionPreferenceChange() {
            syncMotionState();
        }

        function cleanup() {
            if (cleanupActiveMount !== cleanup) return;
            cleanupActiveMount = null;
            if (resizeTimer !== null) window.clearTimeout(resizeTimer);
            resizeTimer = null;
            pendingResizeSize = null;
            if (fetchController) fetchController.abort();
            if (resizeObserver) resizeObserver.disconnect();
            else window.removeEventListener('resize', onResize);
            document.removeEventListener('visibilitychange', onVisibilityChange);
            unsubscribeMotionPreference();
            motionSystem.dispose();
            if (lavaController) lavaController.dispose();
            layer.remove();
        }

        cleanupActiveMount = cleanup;
        document.addEventListener('visibilitychange', onVisibilityChange);
        unsubscribeMotionPreference = window.BackgroundMotion?.subscribe?.(onMotionPreferenceChange)
            || (() => {});

        if ('ResizeObserver' in window) {
            resizeObserver = new ResizeObserver(onResize);
            resizeObserver.observe(layer);
        } else {
            window.addEventListener('resize', onResize, { passive: true });
        }
        onResize();

        fetch(MANIFEST_URL, {
            cache: 'force-cache',
            ...(fetchController ? { signal: fetchController.signal } : {}),
        })
            .then((response) => {
                if (!response.ok) throw new Error(`Doodle manifest request failed: ${response.status}`);
                return response.json();
            })
            .then((manifest) => {
                if (cleanupActiveMount !== cleanup) return;
                assets = Array.isArray(manifest.assets) ? manifest.assets.filter(isValidAsset) : [];
                if (!assets.length) return;

                if (resizeTimer !== null) window.clearTimeout(resizeTimer);
                resizeTimer = null;
                pendingResizeSize = null;
                if (observedWidth <= 0 || observedHeight <= 0) {
                    const rect = layer.getBoundingClientRect();
                    observedWidth = rect.width;
                    observedHeight = rect.height;
                }
                applyResize(observedWidth, observedHeight, true);
            })
            .catch((error) => {
                if (error && error.name === 'AbortError') return;
                if (cleanupActiveMount !== cleanup) return;
                console.warn('[doodle-background] Unable to load local doodles.', error);
            });
    }

    function beginLifecycle(generation) {
        if (!lifecycleActive || pageSuspended || generation !== lifecycleGeneration) return;
        if (isStartupScreenVisible()) watchStartupScreen(generation);
        else scheduleMount(generation);
    }

    function startLifecycle() {
        if (lifecycleActive || pageSuspended) return;
        lifecycleActive = true;
        const generation = ++lifecycleGeneration;
        if (document.readyState === 'loading') {
            domReadyHandler = () => {
                domReadyHandler = null;
                beginLifecycle(generation);
            };
            document.addEventListener('DOMContentLoaded', domReadyHandler, { once: true });
            return;
        }
        beginLifecycle(generation);
    }

    function suspendLifecycle() {
        pageSuspended = true;
        lifecycleActive = false;
        lifecycleGeneration += 1;
        if (domReadyHandler) document.removeEventListener('DOMContentLoaded', domReadyHandler);
        domReadyHandler = null;
        disconnectStartupGate();
        cancelPendingMountCallback();
        if (cleanupActiveMount) cleanupActiveMount();
    }

    window.addEventListener('pagehide', suspendLifecycle);
    window.addEventListener('pageshow', () => {
        pageSuspended = false;
        startLifecycle();
    });

    startLifecycle();
})();
