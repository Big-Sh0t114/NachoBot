/* Browser-owned Live2D model instances for voice-call avatars. */
(function (root) {
    'use strict';

    const CORE_SCRIPT = '/api/chat/live2d/runtime/core.js';
    const DISPLAY_SCRIPT = '/static/vendor/live2d/pixi-live2d-display-0.4.0-cubism4.min.js';
    const MAX_RESOLUTION = 2;
    const MAX_RENDER_LONG_EDGE = 2560;
    const MAX_RENDER_SHORT_EDGE = 1440;
    const canvasRestores = new WeakMap();

    function isSameOriginAsset(url) {
        if (typeof url !== 'string' || !url.startsWith('/api/chat/live2d/assets/')) return false;
        try {
            return new URL(url, root.location && root.location.href || 'http://localhost/').origin ===
                new URL(root.location && root.location.href || 'http://localhost/').origin;
        } catch (_) {
            return false;
        }
    }

    function bounded(promise, timeoutMs, message) {
        let timer;
        const timeout = new Promise((_, reject) => {
            timer = root.setTimeout(() => reject(new Error(message)), timeoutMs);
        });
        return Promise.race([promise, timeout]).finally(() => root.clearTimeout(timer));
    }

    function ensureCore(timeoutMs) {
        if (root.Live2DCubismCore) return Promise.resolve();
        if (root.__nachobotLive2dCorePromise) return root.__nachobotLive2dCorePromise;
        const document = root.document;
        if (!document || typeof document.createElement !== 'function' || typeof root.fetch !== 'function') {
            return Promise.reject(new Error('Browser document or authenticated fetch is unavailable'));
        }
        root.__nachobotLive2dCorePromise = bounded((async () => {
            const response = await root.fetch(CORE_SCRIPT, { cache: 'no-store' });
            if (!response || !response.ok) throw new Error('Cubism Core could not be loaded');
            const source = await response.text();
            if (!source || source.length < 1000) throw new Error('Cubism Core response is invalid');
            const script = document.createElement('script');
            const currentScript = document.currentScript;
            if (currentScript && currentScript.nonce) script.nonce = currentScript.nonce;
            script.textContent = source;
            (document.head || document.documentElement).appendChild(script);
            if (!root.Live2DCubismCore) throw new Error('Cubism Core did not initialize');
        })(), timeoutMs, 'Timed out loading Cubism Core').catch(error => {
            root.__nachobotLive2dCorePromise = null;
            throw error;
        });
        return root.__nachobotLive2dCorePromise;
    }

    function ensureDisplay(timeoutMs) {
        if (root.PIXI && root.PIXI.live2d && root.PIXI.live2d.Live2DModel) return Promise.resolve();
        if (root.__nachobotLive2dDisplayPromise) return root.__nachobotLive2dDisplayPromise;
        const document = root.document;
        if (!document || typeof document.createElement !== 'function') {
            return Promise.reject(new Error('Browser document is unavailable'));
        }
        root.__nachobotLive2dDisplayPromise = bounded(new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = DISPLAY_SCRIPT;
            script.async = true;
            script.onload = () => root.PIXI && root.PIXI.live2d && root.PIXI.live2d.Live2DModel
                ? resolve()
                : reject(new Error('Pinned Live2D renderer did not initialize'));
            script.onerror = () => reject(new Error('Pinned Live2D renderer could not be loaded'));
            (document.head || document.documentElement).appendChild(script);
        }), timeoutMs, 'Timed out loading pinned Live2D renderer').catch(error => {
            root.__nachobotLive2dDisplayPromise = null;
            throw error;
        });
        return root.__nachobotLive2dDisplayPromise;
    }

    function safeDestroyModel(model) {
        if (!model) return;
        try { model.destroy({ children: true, texture: !!model.webuiOwnsTextures, baseTexture: !!model.webuiOwnsTextures }); } catch (_) { /* already destroyed */ }
    }

    function safeDestroyApp(app, canvas) {
        if (!app) return;
        const renderer = app.renderer;
        const gl = renderer && (renderer.gl || renderer.context && renderer.context.gl);
        const extension = gl && typeof gl.getExtension === 'function' && gl.getExtension('WEBGL_lose_context');
        // Pixi intentionally loses its WebGL context during destroy(). Let the browser
        // restore that caller-owned canvas before another instance creates a renderer.
        if (canvas && extension && typeof extension.restoreContext === 'function' && typeof canvas.addEventListener === 'function') {
            let settled = false;
            let sawLoss = false;
            let restoreRequested = false;
            let timer = null;
            let resolveRestore;
            let rejectRestore;
            const promise = new Promise((resolve, reject) => {
                resolveRestore = resolve;
                rejectRestore = reject;
            });
            const cleanup = () => {
                canvas.removeEventListener('webglcontextlost', onLost);
                canvas.removeEventListener('webglcontextrestored', onRestored);
                if (timer !== null) root.clearTimeout(timer);
            };
            const finish = (error) => {
                if (settled) return;
                settled = true;
                cleanup();
                if (error) rejectRestore(error);
                else resolveRestore();
            };
            const requestRestore = () => {
                if (restoreRequested) return;
                restoreRequested = true;
                try { extension.restoreContext(); } catch (error) { finish(error); }
            };
            const onLost = event => {
                sawLoss = true;
                if (event && typeof event.preventDefault === 'function') event.preventDefault();
                root.setTimeout(requestRestore, 0);
            };
            const onRestored = () => finish();
            canvas.addEventListener('webglcontextlost', onLost);
            canvas.addEventListener('webglcontextrestored', onRestored);
            timer = root.setTimeout(() => {
                if (!sawLoss && !(gl && typeof gl.isContextLost === 'function' && gl.isContextLost())) finish();
                else finish(new Error('Timed out restoring the canvas WebGL context'));
            }, 5000);
            try { app.destroy(false, { children: true, texture: false, baseTexture: false }); }
            catch (_) { finish(); }
            root.setTimeout(() => {
                const lost = gl && typeof gl.isContextLost === 'function' && gl.isContextLost();
                if (lost) requestRestore();
                else if (!sawLoss) finish();
            }, 25);
            const tracked = promise.finally(() => {
                if (canvasRestores.get(canvas) === tracked) canvasRestores.delete(canvas);
            });
            canvasRestores.set(canvas, tracked);
            tracked.catch(() => {});
            return;
        }
        // The canvas belongs to the caller, so destroy only Pixi's renderer.
        try { app.destroy(false, { children: true, texture: false, baseTexture: false }); } catch (_) { /* already destroyed */ }
    }

    async function waitForCanvasRestore(canvas, timeoutMs) {
        const restore = canvas && canvasRestores.get(canvas);
        if (restore) await bounded(restore, timeoutMs, 'Timed out waiting for the canvas WebGL context');
    }

    function enumerateParameterIds(coreModel) {
        if (coreModel && typeof coreModel.getParameterIds === 'function') {
            const ids = coreModel.getParameterIds();
            if (Array.isArray(ids)) return ids.map(String);
        }
        if (coreModel && typeof coreModel.getParameterCount === 'function' && typeof coreModel.getModel === 'function') {
            const count = Number(coreModel.getParameterCount());
            const modelData = coreModel.getModel();
            const ids = modelData && modelData.parameters && modelData.parameters.ids;
            if (Number.isInteger(count) && count >= 0 && ids && Number(ids.length) >= count) {
                return Array.from(ids).slice(0, count).map(String);
            }
        }
        return null;
    }

    async function authenticatedModelSource(entry, urls, signal) {
        // Pixi's image and XHR loaders cannot use WebUI's bearer-auth fetch
        // wrapper. Fetch authenticated resources first, then give Pixi only
        // short-lived local blob URLs, never credentials in asset URLs.
        if (!root.sessionStorage?.getItem('nachobot-webui-token')) return entry.model_url;
        const response = await root.fetch(entry.model_url, {signal});
        if (!response.ok) throw new Error('Live2D model descriptor is unavailable');
        const settings = await response.json();
        settings.url = new URL(entry.model_url, root.location.href).href;
        const refs = settings.FileReferences || {};
        const fetchFile = async path => {
            const url = new URL(path, settings.url);
            if (url.origin !== root.location.origin || !isSameOriginAsset(url.pathname)) throw new Error('Invalid model asset');
            const resource = await root.fetch(url.href, {signal});
            if (!resource.ok) throw new Error('Live2D model asset is unavailable');
            const blobUrl = root.URL.createObjectURL(await resource.blob());
            urls.push(blobUrl);
            return blobUrl;
        };
        for (const key of ['Moc', 'Physics', 'Pose', 'DisplayInfo', 'UserData']) {
            if (refs[key]) refs[key] = await fetchFile(refs[key]);
        }
        if (Array.isArray(refs.Textures)) {
            for (let i = 0; i < refs.Textures.length; i++) refs.Textures[i] = await fetchFile(refs.Textures[i]);
        }
        for (const entry of [...(refs.Expressions || []), ...Object.values(refs.Motions || {}).flat()]) {
            if (entry.File) entry.File = await fetchFile(entry.File);
            if (entry.Sound) entry.Sound = await fetchFile(entry.Sound);
        }
        return settings;
    }

    function create(options) {
        const config = options || {};
        const canvas = config.canvas;
        const onFailure = typeof config.onFailure === 'function' ? config.onFailure : () => {};
        const timeoutMs = Math.max(100, Math.min(120000, Number(config.loadTimeoutMs) || 30000));
        let generation = 0;
        let app = null;
        let model = null;
        let destroyed = false;
        let resizeObserver = null;
        let resizeHandler = null;
        let lastRendererSize = null;
        let lastRendererResolution = null;
        let lastModelSize = null;
        let lipSyncIds = [];
        let modelBaseSize = null;
        let mouthLevel = 0;
        let mouthUpdateHandler = null;
        let expressionNames = new Set();
        let motionGroups = new Set();
        let ownedAssetUrls = [];
        let assetAbort = null;

        function reportFailure(error) {
            try { onFailure(error instanceof Error ? error : new Error(String(error))); } catch (_) { /* caller hook */ }
        }

        function getCanvasLayoutSize() {
            if (!canvas) return { width: 1, height: 1 };
            const parent = canvas.parentElement;
            if (parent) {
                // Pixi autoDensity writes a CSS width/height onto the canvas. Those
                // inline values describe its previous renderer size, so the canvas
                // itself must not be used as the source for a later resize.
                const bounds = parent.getBoundingClientRect?.();
                const width = Math.floor(Number(parent.clientWidth) || Number(bounds?.width) || 0);
                const height = Math.floor(Number(parent.clientHeight) || Number(bounds?.height) || 0);
                return width > 0 && height > 0 ? { width, height } : null;
            }
            const width = Math.floor(Number(canvas.clientWidth) || Number(canvas.width) || 0);
            const height = Math.floor(Number(canvas.clientHeight) || Number(canvas.height) || 0);
            return width > 0 && height > 0 ? { width, height } : null;
        }

        function resize() {
            if (!app || !canvas) return;
            const layoutSize = getCanvasLayoutSize();
            // A minimized call hides its stage. Keep the last renderer/model
            // dimensions while hidden so Cubism masks are never resized to 1x1.
            if (!layoutSize) return;
            const { width, height } = layoutSize;
            const resolution = getRenderResolution(layoutSize);
            const rendererSizeChanged = !sameSize(lastRendererSize, layoutSize) || resolution !== lastRendererResolution;
            const modelSizeChanged = Boolean(model && !sameSize(lastModelSize, layoutSize));
            if (!rendererSizeChanged && !modelSizeChanged) return;
            try {
                // Pixi's resize runner resets GL viewport/framebuffer state even
                // when the box is unchanged. A hide/restore cycle often reports
                // the same final size; avoid disturbing Cubism's cached mask RT.
                if (rendererSizeChanged) {
                    // Keep layout coordinates in CSS pixels; only the backing
                    // buffer is reduced, so the portrait keeps its framing.
                    app.renderer.resolution = resolution;
                    app.renderer.resize(width, height);
                }
                if (model) {
                    const naturalWidth = Math.max(1, Number(modelBaseSize && modelBaseSize.width) || Number(model.width) || width);
                    const naturalHeight = Math.max(1, Number(modelBaseSize && modelBaseSize.height) || Number(model.height) || height);
                    // Place the head inside the call's camera frame rather than
                    // aligning the model's transparent canvas to the top edge.
                    const scale = Math.min(width * 0.64 / (naturalWidth * 0.42), height * 0.74 / (naturalHeight * 0.27));
                    model.scale.set(scale);
                    model.x = width / 2 + (naturalWidth / 2 - (modelBaseSize.headCenterX ?? naturalWidth / 2)) * scale;
                    const headCenterY = (modelBaseSize.headTop ?? naturalHeight * 0.06) + naturalHeight * 0.12;
                    model.y = height * 0.43 - headCenterY * scale;
                    lastModelSize = layoutSize;
                }
                if (rendererSizeChanged) {
                    lastRendererSize = layoutSize;
                    lastRendererResolution = resolution;
                }
            } catch (error) {
                reportFailure(error);
            }
        }

        function getRenderResolution({ width, height }) {
            const dpr = Number(root.devicePixelRatio);
            return Math.min(
                Number.isFinite(dpr) && dpr > 0 ? dpr : 1,
                MAX_RESOLUTION,
                MAX_RENDER_LONG_EDGE / Math.max(width, height),
                MAX_RENDER_SHORT_EDGE / Math.min(width, height)
            );
        }

        function sameSize(left, right) {
            return Boolean(left && right && left.width === right.width && left.height === right.height);
        }

        function stopResizeWatchers() {
            if (resizeObserver) {
                try { resizeObserver.disconnect(); } catch (_) { /* no-op */ }
                resizeObserver = null;
            }
            if (resizeHandler && root.removeEventListener) {
                root.removeEventListener('resize', resizeHandler);
                resizeHandler = null;
            }
        }

        function clearModel() {
            assetAbort?.abort();
            assetAbort = null;
            stopResizeWatchers();
            if (model) {
                if (mouthUpdateHandler && model.internalModel) {
                    try { model.internalModel.off('beforeModelUpdate', mouthUpdateHandler); } catch (_) { /* model is closing */ }
                }
                for (const id of lipSyncIds) {
                    try { model.internalModel.coreModel.setParameterValueById(id, 0); } catch (_) { /* model is closing */ }
                }
                if (app && app.stage) {
                    try { app.stage.removeChild(model); } catch (_) { /* already detached */ }
                }
            }
            safeDestroyModel(model);
            model = null;
            lipSyncIds = [];
            modelBaseSize = null;
            lastModelSize = null;
            mouthLevel = 0;
            mouthUpdateHandler = null;
            expressionNames = new Set();
            motionGroups = new Set();
            for (const url of ownedAssetUrls) root.URL.revokeObjectURL(url);
            ownedAssetUrls = [];
        }

        function clearOwned() {
            clearModel();
            safeDestroyApp(app, canvas);
            app = null;
            lastRendererSize = null;
            lastRendererResolution = null;
            lastModelSize = null;
        }

        async function load(entry) {
            if (destroyed) throw new Error('Live2D instance is destroyed');
            const token = ++generation;
            // Keep this instance's WebGL application for model switches. Pixi destroys
            // the canvas context with the renderer; recreating it on the same canvas
            // is unreliable in several browsers and is unnecessary for a model swap.
            clearModel();
            if (!canvas || typeof canvas.getContext !== 'function') {
                const error = new Error('A canvas element is required');
                reportFailure(error);
                throw error;
            }
            if (!entry || !isSameOriginAsset(entry.model_url)) {
                const error = new Error('Live2D model URL is invalid');
                reportFailure(error);
                throw error;
            }
            try {
                await ensureCore(timeoutMs);
                if (token !== generation || destroyed) throw new Error('Live2D load was superseded');
                await ensureDisplay(timeoutMs);
                if (token !== generation || destroyed) throw new Error('Live2D load was superseded');
                const pixi = root.PIXI;
                const live2d = pixi && pixi.live2d;
                if (!pixi || typeof pixi.Application !== 'function' || !live2d || !live2d.Live2DModel) {
                    throw new Error('Pinned PixiJS and pixi-live2d-display scripts are unavailable');
                }
                await waitForCanvasRestore(canvas, timeoutMs);
                const layoutSize = getCanvasLayoutSize();
                if (!layoutSize) throw new Error('Live2D stage has no measurable size');
                const { width, height } = layoutSize;
                if (!app) {
                    const resolution = getRenderResolution(layoutSize);
                    app = new pixi.Application({
                        view: canvas,
                        width,
                        height,
                        autoDensity: true,
                        resolution,
                        backgroundAlpha: 0,
                        antialias: true,
                        autoStart: true,
                    });
                    lastRendererSize = layoutSize;
                    lastRendererResolution = resolution;
                    if (app.ticker && 'maxFPS' in app.ticker) app.ticker.maxFPS = 30;
                }
                const renderer = app.renderer;
                if (!renderer || !(renderer.gl || renderer.context && renderer.context.gl)) {
                    throw new Error('WebGL is unavailable for Live2D rendering');
                }
                const assetUrls = [];
                const assetController = typeof root.AbortController === 'function' ? new root.AbortController() : null;
                assetAbort = assetController;
                ownedAssetUrls = assetUrls;
                let modelSource;
                try {
                    modelSource = await bounded(authenticatedModelSource(entry, assetUrls, assetController?.signal), timeoutMs, 'Timed out loading model assets');
                    if (token !== generation || destroyed) throw new Error('Live2D load was superseded');
                } catch (error) {
                    assetController?.abort();
                    for (const url of assetUrls) root.URL.revokeObjectURL(url);
                    throw error;
                }
                ownedAssetUrls = assetUrls;
                const loadPromise = live2d.Live2DModel.from(modelSource, {
                    // This model is voice-only. pixi-live2d-display defaults
                    // autoInteract to true and maps pointer coordinates to
                    // Cubism focus parameters, which is unsafe while its stage
                    // can be hidden with a zero-sized DOM rect.
                    autoInteract: false,
                    motionPreload: live2d.MotionPreloadStrategy && live2d.MotionPreloadStrategy.IDLE || 'IDLE',
                });
                let abandoned = false;
                loadPromise.then(loaded => {
                    loaded.webuiOwnsTextures = assetUrls.length > 0;
                    if (token !== generation || destroyed || abandoned) safeDestroyModel(loaded);
                }, () => {});
                let loadedModel;
                try {
                    loadedModel = await bounded(loadPromise, timeoutMs, 'Timed out loading Live2D model');
                } catch (error) {
                    abandoned = true;
                    throw error;
                }
                if (token !== generation || destroyed) {
                    safeDestroyModel(loadedModel);
                    throw new Error('Live2D load was superseded');
                }
                const internal = loadedModel && loadedModel.internalModel;
                const coreModel = internal && internal.coreModel;
                if (!internal || !coreModel) {
                    safeDestroyModel(loadedModel);
                    throw new Error('Live2D model metadata is unavailable');
                }
                const parameterIds = enumerateParameterIds(coreModel);
                if (!parameterIds) {
                    safeDestroyModel(loadedModel);
                    throw new Error('Live2D runtime parameter enumeration failed');
                }
                const runtimeParameters = new Set(parameterIds);
                lipSyncIds = Array.isArray(entry.lip_sync_parameters)
                    ? entry.lip_sync_parameters.map(String).filter(id => runtimeParameters.has(id))
                    : [];
                const definitions = internal.motionManager || {};
                motionGroups = new Set(Object.keys(definitions.definitions || {}));
                const expressions = definitions.expressionManager && definitions.expressionManager.definitions || [];
                expressionNames = new Set(expressions.map(definition => String(definition && definition.Name || '')).filter(Boolean));
                mouthUpdateHandler = () => {
                    if (!model || model !== loadedModel) return;
                    for (const id of lipSyncIds) coreModel.setParameterValueById(id, mouthLevel);
                };
                if (typeof internal.on === 'function') internal.on('beforeModelUpdate', mouthUpdateHandler);
                modelBaseSize = {
                    width: Math.max(1, Number(loadedModel.width) || width),
                    height: Math.max(1, Number(loadedModel.height) || height),
                };
                // Estimate the portrait axis from upper geometry, excluding lower
                // accessories that can skew the full model's bounding box.
                try {
                    const xs = [];
                    let headTop = Infinity;
                    const count = coreModel.getDrawableCount?.() || 0;
                    for (let index = 0; index < count; index++) {
                        const vertices = internal.getDrawableVertices(index);
                        for (let offset = 0; offset < vertices.length; offset += 2) {
                            const point = { x: vertices[offset], y: vertices[offset + 1] };
                            const local = internal.localTransform?.apply(point) || point;
                            if (local.y >= 0 && local.y <= modelBaseSize.height * 0.22 && Number.isFinite(local.x)) {
                                xs.push(local.x);
                                headTop = Math.min(headTop, local.y);
                            }
                        }
                    }
                    if (xs.length) {
                        xs.sort((a, b) => a - b);
                        modelBaseSize.headCenterX = xs[Math.floor(xs.length / 2)];
                        modelBaseSize.headTop = headTop;
                    }
                } catch (_) { /* Models without drawable metadata use the canvas center. */ }
                loadedModel.anchor && loadedModel.anchor.set(0.5, 0);
                model = loadedModel;
                app.stage.addChild(model);
                resizeHandler = resize;
                if (root.addEventListener) root.addEventListener('resize', resizeHandler);
                if (typeof root.ResizeObserver === 'function' && canvas.parentElement) {
                    resizeObserver = new root.ResizeObserver(resize);
                    resizeObserver.observe(canvas.parentElement);
                }
                resize();
                renderer.render(app.stage);
                if (token !== generation || destroyed || !model.parent) throw new Error('Live2D model did not become render-ready');
                const actualExpressions = {};
                for (const [emotion, name] of Object.entries(entry.expressions || {})) {
                    if (expressionNames.has(String(name))) actualExpressions[emotion] = String(name);
                }
                const actualActions = {};
                for (const [action, group] of Object.entries(entry.actions || {})) {
                    if (motionGroups.has(String(group))) actualActions[action] = String(group);
                }
                return {
                    parameters: Array.from(runtimeParameters),
                    lip_sync_parameters: lipSyncIds.slice(),
                    expressions: actualExpressions,
                    actions: actualActions,
                };
            } catch (error) {
                if (token === generation) {
                    reportFailure(error);
                    clearOwned();
                    generation += 1;
                }
                throw error;
            }
        }

        async function apply(commands) {
            if (destroyed || !model || !Array.isArray(commands)) return [];
            const currentModel = model;
            const token = generation;
            const applied = [];
            for (const command of commands.slice(0, 8)) {
                if (token !== generation || destroyed || model !== currentModel) break;
                if (!command || typeof command !== 'object') continue;
                if (command.type === 'expression' && expressionNames.has(String(command.name))) {
                    if (typeof model.expression === 'function') {
                        const result = await currentModel.expression(String(command.name));
                        if (token !== generation || destroyed || model !== currentModel) break;
                        if (result !== false) applied.push({ type: 'expression', name: String(command.name) });
                    }
                } else if (command.type === 'motion' && motionGroups.has(String(command.group))) {
                    if (typeof model.motion === 'function') {
                        const priority = root.PIXI.live2d.MotionPriority && root.PIXI.live2d.MotionPriority.NORMAL;
                        const result = await currentModel.motion(String(command.group), undefined, priority);
                        if (token !== generation || destroyed || model !== currentModel) break;
                        if (result !== false) applied.push({ type: 'motion', group: String(command.group) });
                    }
                }
            }
            return applied;
        }

        function setMouth(level) {
            if (!model || destroyed) return;
            mouthLevel = Number.isFinite(Number(level)) ? Math.max(0, Math.min(1, Number(level))) : 0;
            const coreModel = model.internalModel && model.internalModel.coreModel;
            if (!coreModel || typeof coreModel.setParameterValueById !== 'function') return;
            if (mouthUpdateHandler) mouthUpdateHandler();
            else for (const id of lipSyncIds) coreModel.setParameterValueById(id, mouthLevel);
        }

        function destroy() {
            if (destroyed) return;
            setMouth(0);
            destroyed = true;
            generation += 1;
            clearOwned();
        }

        return { load, apply, setMouth, destroy };
    }

    root.ChatLive2D = { create };
})(typeof window !== 'undefined' ? window : globalThis);
