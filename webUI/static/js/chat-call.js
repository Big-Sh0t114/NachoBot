/* Separate, durable voice-call UI and transport for the WebUI chat. */
(function attachChatCall(root) {
    'use strict';

    const CALLS_URL = '/api/chat/calls';
    const LIVE2D_MODELS_URL = '/api/chat/live2d/models';

    function create(options = {}) {
        const doc = options.documentRef || root.document;
        const win = options.windowRef || root;
        const fetcher = options.fetchRef || (typeof root.fetch === 'function' ? root.fetch.bind(root) : null);
        const getConversation = options.getConversation || (() => null);
        const getUserName = options.getUserName || (() => 'WebUI');
        const getConversationLabel = options.getConversationLabel || (id => id);
        const getFallbackAvatar = options.getFallbackAvatar || (() => '');
        const stopTextTTS = options.stopTextTTS || (() => {});
        const onActivityChange = options.onActivityChange || (() => {});
        const toast = options.toast || (() => {});
        const audioFactory = options.audioFactory || ((audioOptions) => root.ChatAudio.create(audioOptions));

        let elements = {};
        let initialized = false;
        let status = null;
        let modelEntries = [];
        let activeRecord = null;
        let transcriptOpen = false;
        let audio = null;
        let live2d = null;
        let startedAtMs = 0;
        let timerId = null;
        let heartbeatId = null;
        let callEpoch = 0;
        let localSpeechFence = 0;
        let interruptionPending = false;
        let interruptionFailed = false;
        let interruptionTask = null;
        let currentSpeech = null;
        let currentPlayer = null;
        let currentTtsAbortController = null;
        let spokenAcks = new Set();
        let pendingSwitch = null;
        let startBusy = false;
        let activityNotified = false;
        let requestIdSerial = 0;
        let speechChain = Promise.resolve();
        const queuedSpeechIds = new Set();
        const motions = new WeakMap();
        const motionProfiles = {
            panel: {enter: 'translateY(12px) scale(0.985)', exit: 'translateY(-8px) scale(0.99)', duration: 280},
            transcript: {enter: 'translateX(20px)', exit: 'translateX(16px)', duration: 240},
            dialog: {enter: 'translateY(18px) scale(0.97)', exit: 'translateY(10px) scale(0.98)', duration: 240},
            compact: {enter: 'translateY(-6px) scale(0.96)', exit: 'translateY(-4px) scale(0.98)', duration: 200},
            content: {enter: 'translateY(8px)', exit: 'translateY(-6px)', duration: 240},
            backdrop: {enter: 'none', exit: 'none', duration: 200},
        };

        function reducedMotion() {
            return win.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;
        }

        function pinForExit(element) {
            const bounds = element.getBoundingClientRect?.();
            const parentBounds = element.parentElement?.getBoundingClientRect?.();
            if (!bounds?.width || !bounds.height || !parentBounds || !element.style?.setProperty) return null;
            const localBounds = element.offsetParent === element.parentElement ? {
                left: element.offsetLeft, top: element.offsetTop, width: element.offsetWidth, height: element.offsetHeight,
            } : {
                left: bounds.left - parentBounds.left, top: bounds.top - parentBounds.top, width: bounds.width, height: bounds.height,
            };
            for (const [name, value] of Object.entries(localBounds)) element.style.setProperty(`--call-motion-${name}`, `${value}px`);
            element.dataset.callMotionPinned = 'true';
            return () => {
                delete element.dataset.callMotionPinned;
                for (const name of ['left', 'top', 'width', 'height']) element.style.removeProperty(`--call-motion-${name}`);
            };
        }

        // Visual transitions never delay transport, microphone, or conversation state changes.
        function transitionVisibility(element, visible, {kind = 'content', pinExit = false, immediate = false, forceEnter = false} = {}) {
            if (!element) return Promise.resolve();
            const previous = motions.get(element);
            if (previous && element.dataset.callMotionVisible === String(visible) && !immediate && !reducedMotion()) {
                return previous.finished;
            }
            const wasHidden = element.hidden;
            const computed = previous && win.getComputedStyle?.(element);
            const current = computed && {opacity: computed.opacity, transform: computed.transform};
            motions.delete(element);
            previous?.animation.cancel();
            previous?.unpin?.();
            element.dataset.callMotionVisible = String(visible);
            element.inert = !visible;
            element.setAttribute?.('aria-hidden', String(!visible));
            if (immediate || !initialized || reducedMotion() || typeof element.animate !== 'function'
                || (!previous && wasHidden === !visible && !(visible && forceEnter))) {
                element.hidden = !visible;
                return Promise.resolve();
            }
            const profile = motionProfiles[kind];
            const unpin = !visible && pinExit ? pinForExit(element) : null;
            element.hidden = false;
            let animation;
            try {
                animation = element.animate([
                    {opacity: current?.opacity || (visible ? 0 : 1), transform: current?.transform || (visible ? profile.enter : 'none')},
                    {opacity: visible ? 1 : 0, transform: visible ? 'none' : profile.exit},
                ], {duration: visible ? profile.duration : 180, easing: 'cubic-bezier(0.22, 1, 0.36, 1)', fill: 'both'});
            } catch (_) {
                unpin?.();
                element.hidden = !visible;
                return Promise.resolve();
            }
            const motion = {animation, unpin};
            motions.set(element, motion);
            const finish = () => {
                if (motions.get(element) !== motion) return;
                motions.delete(element);
                element.hidden = !visible;
                unpin?.();
                animation.cancel();
            };
            motion.finished = animation.finished.then(finish, finish);
            return motion.finished;
        }

        function animateContent(element) {
            transitionVisibility(element, true, {kind: 'content', forceEnter: true});
        }

        function setCallActivity(active) {
            if (activityNotified === active) return;
            activityNotified = active;
            onActivityChange(active);
        }

        function init() {
            if (initialized || !doc) return;
            elements = {
                workspace: doc.getElementById('chat-workspace'),
                messages: doc.getElementById('chat-messages'),
                composer: doc.querySelector?.('#chat-workspace > .chat-composer-area'),
                open: doc.getElementById('chat-call-open'),
                historyOpen: doc.getElementById('chat-call-history-open'),
                startDialog: doc.getElementById('chat-call-start-dialog'),
                startAvatar: doc.getElementById('chat-call-start-avatar'),
                readyState: doc.getElementById('chat-call-ready-state'),
                startConfirm: doc.getElementById('chat-call-start-confirm'),
                modelSelect: doc.getElementById('chat-call-live2d-model'),
                modelReason: doc.getElementById('chat-call-live2d-reason'),
                panel: doc.getElementById('chat-call-panel'),
                panelTitle: doc.getElementById('chat-call-panel-title'),
                stage: doc.getElementById('chat-call-avatar-stage'),
                canvas: doc.getElementById('chat-call-live2d-canvas'),
                fallback: doc.getElementById('chat-call-avatar-fallback'),
                timer: doc.getElementById('chat-call-timer'),
                minimizedTimer: doc.getElementById('chat-call-minimized-timer'),
                state: doc.getElementById('chat-call-state'),
                stateDot: doc.getElementById('chat-call-state-dot'),
                selfState: doc.getElementById('chat-call-self-state'),
                conversationPanel: doc.getElementById('chat-call-conversation'),
                transcript: doc.getElementById('chat-call-transcript'),
                transcriptToggle: doc.getElementById('chat-call-transcript-toggle'),
                transcriptLabel: doc.getElementById('chat-call-transcript-label'),
                typedForm: doc.getElementById('chat-call-typed-form'),
                typedInput: doc.getElementById('chat-call-typed-input'),
                typedSend: doc.getElementById('chat-call-typed-send'),
                mute: doc.getElementById('chat-call-mute'),
                muteLabel: doc.getElementById('chat-call-mute-label'),
                minimize: doc.getElementById('chat-call-minimize'),
                end: doc.getElementById('chat-call-end'),
                minimizedBar: doc.getElementById('chat-call-minimized-bar'),
                restore: doc.getElementById('chat-call-restore'),
                minimizedEnd: doc.getElementById('chat-call-minimized-end'),
                historyDialog: doc.getElementById('chat-call-history-dialog'),
                historyConversation: doc.getElementById('chat-call-history-conversation'),
                historyList: doc.getElementById('chat-call-history-list'),
                switchDialog: doc.getElementById('chat-call-switch-dialog'),
                switchCopy: doc.getElementById('chat-call-switch-copy'),
                switchCancel: doc.getElementById('chat-call-switch-cancel'),
                switchContinue: doc.getElementById('chat-call-switch-continue'),
                switchEnd: doc.getElementById('chat-call-switch-end'),
            };
            bindUi();
            setCallView('text');
            setTranscriptExpanded(false);
            initialized = true;
            win.addEventListener?.('pagehide', onPageHide);
        }

        function bindUi() {
            listen(elements.open, 'click', openStartDialog);
            listen(elements.historyOpen, 'click', openHistory);
            listen(elements.startConfirm, 'click', () => beginCall(selectedModelId()));
            listen(elements.mute, 'click', toggleMute);
            listen(elements.transcriptToggle, 'click', toggleTranscript);
            listen(elements.minimize, 'click', minimize);
            listen(elements.end, 'click', () => endCall());
            listen(elements.minimizedEnd, 'click', () => endCall());
            listen(elements.restore, 'click', restorePanel);
            listen(elements.typedForm, 'submit', event => {
                event.preventDefault();
                const text = String(elements.typedInput?.value || '').trim();
                if (text) {
                    elements.typedInput.value = '';
                    sendTextMessage(text);
                }
            });
            listen(elements.switchCancel, 'click', () => resolveSwitch('cancel'));
            listen(elements.switchContinue, 'click', () => resolveSwitch('continue'));
            listen(elements.switchEnd, 'click', async () => {
                if (!pendingSwitch) return;
                await endCall();
                resolveSwitch('hangup');
            });
            doc?.querySelectorAll?.('[data-call-close]').forEach(button => {
                listen(button, 'click', () => closeDialog(button.dataset.callClose));
            });
            doc?.addEventListener?.('keydown', event => {
                if (event.key !== 'Escape') return;
                if (pendingSwitch) {
                    event.preventDefault();
                    resolveSwitch('cancel');
                    return;
                }
                if (isVisible(elements.startDialog)) closeDialog('start');
                else if (isVisible(elements.historyDialog)) closeDialog('history');
                else if (activeRecord && isTranscriptExpanded()) {
                    event.preventDefault();
                    setTranscriptExpanded(false);
                    elements.transcriptToggle?.focus?.();
                }
                else if (activeRecord && elements.workspace?.dataset.callView === 'active') minimize();
            });
            elements.startDialog?.addEventListener?.('click', event => {
                if (event.target === elements.startDialog) closeDialog('start');
            });
            elements.historyDialog?.addEventListener?.('click', event => {
                if (event.target === elements.historyDialog) closeDialog('history');
            });
        }

        function listen(element, eventName, callback) {
            element?.addEventListener?.(eventName, callback);
        }

        function isVisible(element) {
            return Boolean(element && !element.hidden && element.dataset.callMotionVisible !== 'false');
        }

        function setCallView(view, {immediate = false} = {}) {
            const previousView = elements.workspace?.dataset.callView;
            const panelMotion = transitionVisibility(elements.panel, view === 'active', {kind: 'panel', pinExit: true, immediate});
            if (elements.workspace) elements.workspace.dataset.callView = view;
            if (elements.panel) {
                elements.panel.dataset.minimized = String(view === 'minimized');
            }
            transitionVisibility(elements.minimizedBar, view === 'minimized', {kind: 'compact', immediate});
            transitionVisibility(elements.open, view === 'text', {kind: 'compact', immediate});
            if (initialized && !immediate && previousView === 'active' && view !== 'active') {
                animateContent(elements.messages);
                animateContent(elements.composer);
            }
            return panelMotion;
        }

        function setTranscriptExpanded(expanded) {
            transcriptOpen = Boolean(expanded);
            if (elements.conversationPanel) {
                transitionVisibility(elements.conversationPanel, transcriptOpen, {kind: 'transcript', pinExit: true});
            }
            if (elements.panel) elements.panel.dataset.transcriptOpen = String(transcriptOpen);
            if (elements.transcriptToggle) {
                elements.transcriptToggle.setAttribute('aria-expanded', String(transcriptOpen));
                elements.transcriptToggle.setAttribute('aria-label', transcriptOpen ? '关闭通话转写' : '打开通话转写');
                elements.transcriptToggle.setAttribute('title', transcriptOpen ? '关闭通话转写' : '打开通话转写');
                elements.transcriptToggle.setAttribute('aria-controls', 'chat-call-conversation');
            }
            setText(elements.transcriptLabel, transcriptOpen ? '隐藏转写' : '转写');
        }

        function toggleTranscript() {
            setTranscriptExpanded(!transcriptOpen);
        }

        function setButtonLabel(button, labelElement, text, accessibleName = text) {
            if (labelElement) setText(labelElement, text);
            else setText(button, text);
            button?.setAttribute?.('aria-label', accessibleName);
            button?.setAttribute?.('title', accessibleName);
        }

        function showDialog(element) {
            if (!element) return;
            const opening = !isVisible(element);
            transitionVisibility(element, true, {kind: 'backdrop'});
            transitionVisibility(element.querySelector?.('.chat-call-dialog'), true, {kind: 'dialog', forceEnter: opening});
        }

        function hideDialog(element) {
            if (!element) return;
            transitionVisibility(element, false, {kind: 'backdrop'});
            transitionVisibility(element.querySelector?.('.chat-call-dialog'), false, {kind: 'dialog'});
        }

        function closeDialog(kind) {
            if (kind === 'start') hideDialog(elements.startDialog);
            if (kind === 'history') hideDialog(elements.historyDialog);
        }

        async function openStartDialog() {
            if (activeRecord) { restorePanel(); return; }
            if (elements.startAvatar) elements.startAvatar.innerHTML = getFallbackAvatar() || 'N';
            showDialog(elements.startDialog);
            setText(elements.readyState, '正在检查语音服务…');
            if (elements.readyState) elements.readyState.dataset.ready = 'checking';
            if (elements.startConfirm) elements.startConfirm.disabled = true;
            const results = await Promise.allSettled([refreshStatus(), refreshModels()]);
            const statusResult = results[0];
            if (statusResult.status === 'rejected') {
                setText(elements.readyState, `无法检查语音服务：${statusResult.reason?.message || statusResult.reason}`);
                return;
            }
            renderReadiness();
        }

        async function refreshStatus() {
            const data = await requestJson(`${CALLS_URL}/status`);
            status = data && typeof data === 'object' ? data : null;
            renderReadiness();
            return status;
        }

        function renderReadiness() {
            if (!elements.readyState || !status) return;
            const ttsReady = status.tts_ready === true;
            elements.readyState.dataset.ready = String(ttsReady);
            const asrReady = status.asr_ready === true;
            const reason = typeof status.reason === 'string' && status.reason.trim() ? ` ${status.reason.trim()}` : '';
            setText(elements.readyState, ttsReady
                ? `语音服务就绪 · ${asrReady ? '语音识别可用' : '可文字交流'}`
                : `语音服务不可用${reason ? `：${reason.trim()}` : ''}`);
            if (elements.startConfirm) elements.startConfirm.disabled = !ttsReady || startBusy;
        }

        async function refreshModels() {
            try {
                const data = await requestJson(LIVE2D_MODELS_URL);
                modelEntries = data?.available === true && Array.isArray(data.models)
                    ? data.models.filter(item => item && typeof item.id === 'string') : [];
                const select = elements.modelSelect;
                if (!select) return modelEntries;
                select.replaceChildren();
                const none = doc.createElement('option');
                none.value = '';
                none.textContent = '不使用live2D';
                select.appendChild(none);
                modelEntries.forEach(entry => {
                    const option = doc.createElement('option');
                    option.value = entry.id;
                    option.textContent = entry.name || entry.id;
                    select.appendChild(option);
                });
                setText(elements.modelReason, data?.available === false
                    ? (data.reason || '未发现可用 Live2D 模型。')
                    : (modelEntries.length ? '选择模型使用 Live2D；不使用时显示头像。' : '未扫描到 Live2D 模型，将使用头像。'));
                select.disabled = false;
                return modelEntries;
            } catch (error) {
                modelEntries = [];
                if (elements.modelSelect) {
                    const none = doc.createElement('option');
                    none.value = '';
                    none.textContent = '不使用live2D';
                    elements.modelSelect.replaceChildren(none);
                    elements.modelSelect.disabled = false;
                }
                setText(elements.modelReason, `Live2D 暂不可用：${error.message}`);
                return modelEntries;
            }
        }

        function selectedModelId() {
            const id = String(elements.modelSelect?.value || '');
            return modelEntries.some(entry => entry.id === id) ? id : null;
        }

        async function beginCall(modelId = null) {
            if (startBusy || activeRecord) return false;
            if (!status || status.tts_ready !== true) {
                try { await refreshStatus(); } catch (error) {
                    setText(elements.readyState, `语音服务检查失败：${error.message}`);
                    return false;
                }
            }
            if (status?.tts_ready !== true) {
                renderReadiness();
                return false;
            }
            if (startBusy || activeRecord) return false;
            const conversation = getConversation();
            if (!conversation?.id) {
                toast('请先打开一个对话，再开始语音通话。', 'error');
                return false;
            }

            startBusy = true;
            if (elements.startConfirm) elements.startConfirm.disabled = true;
            stopTextTTS();
            setCallActivity(true);
            const epoch = ++callEpoch;
            const engine = audioFactory({
                onSpeechStart: () => beginBargeIn(epoch),
                onSegment: segment => transcribeSegment(segment, epoch),
                onError: error => {
                    if (isEpochActive(epoch)) setCallState(`麦克风处理失败：${error.message}。可以继续使用文字输入。`, 'is-error');
                },
            });
            audio = engine;
            const micPromise = engine.unlock().then(ready => {
                if (ready === false || epoch !== callEpoch || audio !== engine) return false;
                return status.asr_ready === true ? engine.start() : true;
            });
            micPromise.catch(() => {});
            const createPromise = requestJson(CALLS_URL, {
                method: 'POST',
                body: {
                    conversation_id: conversation.id,
                    user_name: getUserName() || 'WebUI',
                    model_id: modelId || null,
                },
            });
            let record;
            try {
                record = await createPromise;
            } catch (error) {
                await engine.stop();
                if (audio === engine) audio = null;
                startBusy = false;
                if (elements.startConfirm) elements.startConfirm.disabled = false;
                setText(elements.readyState, `通话启动失败：${error.message}`);
                if (epoch === callEpoch) setCallActivity(false);
                return false;
            }
            if (epoch !== callEpoch) {
                await engine.stop();
                if (record?.id) await requestJson(`${callPath(record.id)}/end`, {method: 'POST', body: {}}).catch(() => {});
                return false;
            }
            activeRecord = normalizeRecord(record);
            if (!activeRecord?.id) {
                activeRecord = null;
                await engine.stop();
                startBusy = false;
                if (epoch === callEpoch) setCallActivity(false);
                return false;
            }
            startBusy = false;
            spokenAcks = new Set();
            queuedSpeechIds.clear();
            speechChain = Promise.resolve();
            localSpeechFence = 0;
            interruptionPending = false;
            interruptionFailed = false;
            interruptionTask = null;
            startedAtMs = epochMilliseconds(activeRecord.started_at) || Date.now();
            showCallPanel();
            closeDialog('start');
            renderTranscript();
            mountSelectedModel(modelId);
            updateTimer();
            timerId = win.setInterval?.(updateTimer, 1000) || null;
            heartbeatId = win.setInterval?.(() => heartbeat(epoch), 8000) || null;
                Promise.resolve(micPromise).then(ready => {
                if (!isEpochActive(epoch)) return;
                if (ready === false) {
                    setCallState('麦克风未启用，可在下方输入消息。', 'is-quiet');
                    if (elements.mute) elements.mute.disabled = true;
                    setTranscriptExpanded(true);
                    return;
                }
                if (status.asr_ready === true) setCallState('正在聆听…', 'is-listening');
                else {
                    setCallState('通话已连接，可输入消息。', 'is-quiet');
                    setTranscriptExpanded(true);
                }
                if (elements.mute) elements.mute.disabled = !engine.isActive?.();
            }).catch(error => {
                if (isEpochActive(epoch)) {
                    setCallState(`麦克风不可用：${error.message}。可继续输入消息。`, 'is-error');
                    if (elements.mute) elements.mute.disabled = true;
                    setTranscriptExpanded(true);
                }
            });
            return true;
        }

        function showCallPanel() {
            setCallView('active');
            setTranscriptExpanded(false);
            if (elements.mute) {
                elements.mute.disabled = !audio?.isActive?.();
                elements.mute.setAttribute('aria-pressed', 'false');
                setButtonLabel(elements.mute, elements.muteLabel, '静音', '静音麦克风');
            }
            setText(elements.panelTitle, 'Nacho');
            if (elements.fallback) elements.fallback.innerHTML = getFallbackAvatar();
        }

        function mountSelectedModel(modelId) {
            destroyLive2D();
            if (!modelId || !elements.canvas || !root.ChatLive2D?.create) return;
            const entry = modelEntries.find(item => item.id === modelId);
            if (!entry) return;
            try {
                // A destroyed Pixi renderer loses its canvas WebGL context.
                const freshCanvas = elements.canvas.cloneNode(false);
                elements.canvas.replaceWith(freshCanvas);
                elements.canvas = freshCanvas;
                const epoch = callEpoch;
                const instance = root.ChatLive2D.create({
                    canvas: elements.canvas,
                    onFailure: error => { if (epoch === callEpoch && live2d === instance) showLive2DFallback(error); },
                });
                live2d = instance;
                Promise.resolve(instance.load(entry)).then(() => {
                    if (!activeRecord || live2d !== instance || epoch !== callEpoch) return;
                    elements.stage?.classList.add('has-live2d');
                    setText(elements.fallback, '');
                }).catch(error => { if (epoch === callEpoch && live2d === instance) showLive2DFallback(error); });
            } catch (error) {
                showLive2DFallback(error);
            }
        }

        function showLive2DFallback(error) {
            destroyLive2D();
            elements.stage?.classList.remove('has-live2d');
            if (elements.fallback) elements.fallback.innerHTML = getFallbackAvatar();
            if (activeRecord) setCallState(`Live2D 加载失败，已切换头像：${error?.message || error}`, 'is-quiet');
        }

        function destroyLive2D() {
            const current = live2d;
            live2d = null;
            if (current) {
                try { current.setMouth?.(0); } catch (_) {}
                try { current.destroy?.(); } catch (_) {}
            }
            elements.stage?.classList.remove('has-live2d');
        }

        function minimize() {
            if (!activeRecord || !elements.panel) return;
            setCallView('minimized');
        }

        function restorePanel() {
            if (!activeRecord || !elements.panel) return;
            setCallView('active');
        }

        function updateTimer() {
            if (!activeRecord || !startedAtMs) return;
            const seconds = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
            const value = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
            setText(elements.timer, value);
            setText(elements.minimizedTimer, value);
        }

        function setCallState(message, className = '') {
            setText(elements.state, message);
            if (elements.stateDot) elements.stateDot.className = `chat-call-state-dot ${className}`.trim();
            if (elements.panel) elements.panel.dataset.callState = className;
            const micEnabled = audio?.isActive?.() === true;
            const micMuted = audio?.isMuted?.() === true;
            if (elements.panel) elements.panel.dataset.micState = micEnabled ? (micMuted ? 'muted' : 'active') : 'unavailable';
            setText(elements.selfState, micEnabled ? (micMuted ? '已静音' : '麦克风已开启') : '麦克风未启用');
        }

        function toggleMute() {
            if (!audio?.isActive?.()) return;
            const muted = !audio.isMuted();
            audio.setMuted(muted);
            if (elements.mute) {
                elements.mute.setAttribute('aria-pressed', String(muted));
                setButtonLabel(elements.mute, elements.muteLabel, muted ? '已静音' : '静音', muted ? '取消静音' : '静音麦克风');
            }
            setCallState(muted ? '麦克风已静音。' : '正在聆听…', muted ? 'is-quiet' : 'is-listening');
        }

        function beginBargeIn(epoch = callEpoch) {
            if (!isEpochActive(epoch)) return Promise.resolve(false);
            localSpeechFence += 1;
            interruptionFailed = false;
            if (interruptionTask) {
                interruptionPending = true;
                return interruptionTask;
            }
            interruptionPending = true;
            speechChain = Promise.resolve();
            stopCurrentSpeech('interrupted');
            setCallState('正在聆听…', 'is-listening');
            const record = activeRecord;
            const oldGeneration = Number(record.generation) || 0;
            const task = requestJson(`${callPath(record.id)}/interrupt`, {
                method: 'POST',
                body: { generation: oldGeneration },
            }).then(data => {
                if (!isEpochActive(epoch)) return false;
                if (Number.isFinite(Number(data?.generation))) record.generation = Number(data.generation);
                else record.generation = oldGeneration + 1;
                interruptionPending = false;
                interruptionFailed = false;
                return true;
            }).catch(error => {
                if (isEpochActive(epoch)) {
                    interruptionPending = false;
                    interruptionFailed = true;
                    setCallState(`无法安全中断上一段回复：${error.message}。请重试或使用文字输入。`, 'is-error');
                }
                return false;
            }).finally(() => {
                if (interruptionTask === task) interruptionTask = null;
            });
            interruptionTask = task;
            return task;
        }

        async function transcribeSegment(segment, epoch = callEpoch) {
            const record = activeRecord;
            if (!record || !isEpochActive(epoch) || !segment?.audioBase64) return false;
            const audioBase64 = segment.audioBase64;
            try {
                const interrupted = interruptionTask ? await interruptionTask : !interruptionFailed;
                if (!interrupted || !isEpochActive(epoch) || activeRecord !== record) return false;
                const generation = Number(record.generation) || 0;
                const data = await requestJson(`${callPath(record.id)}/transcribe`, {
                    method: 'POST',
                    body: { audio_base64: audioBase64, generation },
                });
                if (!isEpochActive(epoch) || activeRecord !== record || Number(record.generation) !== generation) return false;
                if (Number.isFinite(Number(data?.generation)) && Number(data.generation) !== generation) return false;
                const text = typeof data?.text === 'string' ? data.text.trim() : '';
                if (!text) {
                    setCallState('没有识别到语音，可直接在下方输入。', 'is-quiet');
                    return false;
                }
                return await sendMessage(text, { epoch, interrupted: true });
            } catch (error) {
                if (isEpochActive(epoch)) setCallState(`语音识别失败：${error.message}。可继续文字输入。`, 'is-error');
                return false;
            }
        }

        async function sendTextMessage(text) {
            return sendMessage(text, { epoch: callEpoch, interrupted: false });
        }

        async function sendMessage(text, { epoch = callEpoch, interrupted = false } = {}) {
            const record = activeRecord;
            const content = String(text || '').trim();
            if (!record || !content || !isEpochActive(epoch)) return false;
            try {
                if (!interrupted) {
                    const ready = await beginBargeIn(epoch);
                    if (!ready || !isEpochActive(epoch)) return false;
                } else if (interruptionTask) {
                    const ready = await interruptionTask;
                    if (!ready || !isEpochActive(epoch)) return false;
                }
                const generation = Number(record.generation) || 0;
                const requestMessageId = makeRequestId();
                setCallState('正在发送…', 'is-listening');
                const response = await requestJson(`${callPath(record.id)}/message`, {
                    method: 'POST',
                    body: {
                        message: content,
                        request_message_id: requestMessageId,
                        user_name: getUserName() || 'WebUI',
                        generation,
                    },
                });
                if (!isEpochActive(epoch) || activeRecord !== record || Number(record.generation) !== generation) return false;
                if (Number.isFinite(Number(response?.generation)) && Number(response.generation) !== generation) return false;
                setCallState(audio?.isActive?.() && !audio.isMuted() ? '正在聆听…' : '等待 Nacho 回复…', 'is-listening');
                refreshActiveRecord(epoch).catch(() => {});
                return true;
            } catch (error) {
                if (isEpochActive(epoch)) setCallState(`消息发送失败：${error.message}。`, 'is-error');
                return false;
            }
        }

        async function refreshActiveRecord(epoch = callEpoch) {
            const record = activeRecord;
            if (!record || !isEpochActive(epoch)) return false;
            const updated = await requestJson(callPath(record.id));
            if (!isEpochActive(epoch) || activeRecord !== record) return false;
            if (updated.status !== 'active') { await endCall(); return false; }
            mergeRecordMessages(record, updated);
            if (Number.isFinite(Number(updated.generation)) && Number(updated.generation) >= Number(record.generation)) {
                record.generation = Number(updated.generation);
            }
            renderTranscript();
            return true;
        }

        function mergeRecordMessages(record, updated) {
            const incoming = Array.isArray(updated?.messages) ? updated.messages : [];
            const byId = new Map((record.messages || []).map(message => [message.id, message]));
            incoming.forEach(message => {
                if (message && typeof message.id === 'string') byId.set(message.id, message);
            });
            record.messages = Array.from(byId.values()).sort((a, b) => epochMilliseconds(a.created_at) - epochMilliseconds(b.created_at));
        }

        function handleVoiceEvent(event) {
            if (event?.channel !== 'voice') return false;
            const record = activeRecord;
            if (!record || event.call_id !== record.id || event.conversation_id !== record.conversation_id) return true;
            const generation = Number(event.generation);
            if (Number.isFinite(generation) && generation !== Number(record.generation)) return true;
            if (interruptionPending || interruptionFailed) return true;

            const voiceId = typeof event.voice_message_id === 'string' ? event.voice_message_id : '';
            const messageId = voiceId || (typeof event.message_id === 'string' ? `event-${event.message_id}` : makeRequestId());
            if (!record.messages.some(message => message.id === messageId)) {
                record.messages.push({
                    id: messageId,
                    role: event?.message?.role === 'user' ? 'user' : 'assistant',
                    content: String(event?.message?.content || ''),
                    created_at: event?.message?.created_at || Date.now(),
                    request_message_id: event.reply_to_message_id,
                    generation: Number.isFinite(generation) ? generation : record.generation,
                    delivery_status: event.message?.delivery_status,
                });
                renderTranscript();
            }
            if (event?.message?.role !== 'user' && voiceId && event?.message?.content) {
                queueSpeech({ messageId: voiceId, generation, content: event.message.content, controlId: event.control_id }, callEpoch);
            }
            return true;
        }

        function queueSpeech(entry, epoch) {
            if (queuedSpeechIds.has(entry.messageId) || queuedSpeechIds.size >= 500) return;
            queuedSpeechIds.add(entry.messageId);
            speechChain = speechChain.then(() => speak(entry, epoch)).catch(error => {
                if (isEpochActive(epoch) && Number(activeRecord?.generation) === Number(entry.generation)
                    && !isAbort(error)) handleTtsFailure(error, entry.messageId, epoch);
            });
        }

        async function speak(entry, epoch) {
            const record = activeRecord;
            if (!record || !isEpochActive(epoch) || !entry.messageId) return false;
            const generation = Number(entry.generation);
            if (generation !== Number(record.generation) || interruptionPending || interruptionFailed) return false;
            const token = ++localSpeechFence;
            stopCurrentSpeech('interrupted');
            const player = audio?.createPlayer?.();
            if (!player) throw new Error('音频播放设备未就绪');
            currentSpeech = { messageId: entry.messageId, generation };
            currentPlayer = player;
            setCallState('正在回复…', 'is-speaking');
            const response = await fetchResponse(`${callPath(record.id)}/tts`, {
                method: 'POST',
                body: { message_id: entry.messageId, generation },
                signal: (currentTtsAbortController = new AbortController()).signal,
            });
            if (!speechIsCurrent(epoch, token, generation, record)) {
                player.stop();
                return false;
            }
            if (!response.ok) throw await responseError(response);
            const wav = await response.arrayBuffer();
            if (!speechIsCurrent(epoch, token, generation, record)) {
                player.stop();
                ackPlayback(record.id, entry.messageId, generation, 'interrupted');
                return false;
            }
            const result = await player.play(wav, {
                onStarted: () => { if (entry.controlId && speechIsCurrent(epoch, token, generation, record)) applyControl(entry.controlId, generation, epoch); },
                onLevel: level => { if (speechIsCurrent(epoch, token, generation, record)) live2d?.setMouth?.(level); },
            });
            if (!speechIsCurrent(epoch, token, generation, record)) return false;
            currentPlayer = null;
            currentSpeech = null;
            live2d?.setMouth?.(0);
            await ackPlayback(record.id, entry.messageId, generation, result === 'played' ? 'played' : 'interrupted');
            if (result === 'played') setCallState(audio?.isActive?.() && !audio.isMuted() ? '正在聆听…' : '通话已连接，可继续输入文字。', 'is-listening');
            return result === 'played';
        }

        function speechIsCurrent(epoch, token, generation, record) {
            return isEpochActive(epoch)
                && activeRecord === record
                && token === localSpeechFence
                && generation === Number(record.generation)
                && !interruptionPending
                && !interruptionFailed;
        }

        function stopCurrentSpeech(statusValue) {
            const record = activeRecord;
            const speech = currentSpeech;
            const player = currentPlayer;
            currentSpeech = null;
            currentPlayer = null;
            currentTtsAbortController?.abort();
            currentTtsAbortController = null;
            live2d?.setMouth?.(0);
            if (player) player.stop();
            if (speech && record) ackPlayback(record.id, speech.messageId, speech.generation, statusValue);
        }

        function ackPlayback(callId, messageId, generation, statusValue) {
            const key = `${callId}:${messageId}`;
            if (!callId || !messageId || spokenAcks.has(key)) return Promise.resolve(false);
            spokenAcks.add(key);
            return requestJson(`${callPath(callId)}/playback`, {
                method: 'POST',
                body: { message_id: messageId, status: statusValue, generation },
            }).then(() => true).catch(() => { spokenAcks.delete(key); return false; });
        }

        function handleTtsFailure(error, messageId, epoch) {
            if (currentSpeech?.messageId === messageId) stopCurrentSpeech('interrupted');
            audio?.setMuted?.(true);
            if (elements.mute) {
                setButtonLabel(elements.mute, elements.muteLabel, '麦克风关闭', '开启麦克风');
                elements.mute.setAttribute('aria-pressed', 'true');
            }
            if (isEpochActive(epoch)) {
                setCallState(`语音合成中断：${error.message}。麦克风已静音，请检查 TTS 服务；文字输入仍可用。`, 'is-error');
            }
        }

        async function applyControl(controlId, generation, epoch) {
            const record = activeRecord;
            if (!record || !isEpochActive(epoch) || Number(generation) !== Number(record.generation)) return false;
            try {
                const data = await requestJson(`${callPath(record.id)}/control`, {
                    method: 'POST',
                    body: { control_id: controlId, generation },
                });
                if (!isEpochActive(epoch) || activeRecord !== record || Number(record.generation) !== Number(generation)) return false;
                if (Array.isArray(data?.commands)) live2d?.apply?.(data.commands);
                return true;
            } catch (error) {
                if (isEpochActive(epoch)) setCallState(`Live2D 动作未能应用：${error.message}`, 'is-quiet');
                return false;
            }
        }

        async function heartbeat(epoch) {
            const record = activeRecord;
            if (!record || !isEpochActive(epoch)) return;
            try {
                await requestJson(`${callPath(record.id)}/heartbeat`, { method: 'POST', body: {} });
            } catch (error) {
                if (error.status === 409 || error.status === 404) { await endCall(); return; }
                if (isEpochActive(epoch)) setCallState(`连接暂时中断，正在重试：${error.message}`, 'is-error');
            }
        }

        async function endCall({ pagehide = false } = {}) {
            const record = activeRecord;
            if (!record) {
                callEpoch += 1;
                startBusy = false;
                setCallActivity(false);
                if (audio) { const pendingAudio = audio; audio = null; await pendingAudio.stop().catch(() => {}); }
                return true;
            }
            speechChain = Promise.resolve();
            const endpoint = `${callPath(record.id)}/end`;
            const generation = Number(record.generation) || 0;
            stopCurrentSpeech('interrupted');
            activeRecord = null;
            callEpoch += 1;
            localSpeechFence += 1;
            interruptionPending = false;
            interruptionFailed = false;
            interruptionTask = null;
            if (timerId !== null) win.clearInterval?.(timerId);
            if (heartbeatId !== null) win.clearInterval?.(heartbeatId);
            timerId = null;
            heartbeatId = null;
            if (audio) {
                const currentAudio = audio;
                audio = null;
                await currentAudio.stop().catch(() => {});
            }
            const endingModel = live2d;
            const panelMotion = setCallView('text', {immediate: pagehide});
            if (pagehide) destroyLive2D();
            else panelMotion.then(() => { if (live2d === endingModel) destroyLive2D(); });
            setTranscriptExpanded(false);
            setText(elements.timer, '00:00');
            setText(elements.minimizedTimer, '00:00');
            setCallActivity(false);
            if (pagehide) {
                requestJson(endpoint, { method: 'POST', body: { generation }, keepalive: true }).catch(() => {});
                return true;
            }
            try {
                await requestJson(endpoint, { method: 'POST', body: { generation } });
            } catch (error) {
                toast(`本地已挂断；服务器将在租约超时后结束通话：${error.message}`, 'error');
            }
            return true;
        }

        function onPageHide() {
            if (activeRecord || startBusy) endCall({ pagehide: true });
        }

        async function guardConversationSwitch(targetConversationId, action = 'switch') {
            const record = activeRecord;
            if (startBusy) { await endCall(); return true; }
            if (!record || record.conversation_id === targetConversationId) return true;
            if (!elements.switchDialog || pendingSwitch) return false;
            const targetLabel = targetConversationId ? getConversationLabel(targetConversationId) : '新对话';
            setText(elements.switchCopy, `挂断后才能${action === 'delete' ? '删除此对话' : `切换到「${targetLabel}」`}。继续通话会留在当前对话。`);
            showDialog(elements.switchDialog);
            return new Promise(resolve => { pendingSwitch = { resolve }; });
        }

        function resolveSwitch(choice) {
            if (!pendingSwitch) return;
            const pending = pendingSwitch;
            pendingSwitch = null;
            hideDialog(elements.switchDialog);
            pending.resolve(choice === 'hangup');
        }

        async function openHistory() {
            showDialog(elements.historyDialog);
            if (!elements.historyList) return;
            elements.historyList.replaceChildren();
            const loading = doc.createElement('p');
            loading.className = 'chat-call-history-empty';
            loading.textContent = '正在读取已保存的通话记录…';
            elements.historyList.appendChild(loading);
            const conversationIds = getConversation()?.id ? [getConversation().id] : [];
            if (elements.historyConversation) {
                elements.historyConversation.replaceChildren();
                const option = doc.createElement('option');
                option.value = conversationIds[0] || '';
                option.textContent = getConversationLabel(option.value);
                elements.historyConversation.appendChild(option);
                elements.historyConversation.disabled = true;
            }
            try {
                const groups = await Promise.all(conversationIds.map(async conversationId => {
                    const data = await requestJson(`${CALLS_URL}?conversation_id=${encodeURIComponent(conversationId)}`);
                    return { conversationId, calls: Array.isArray(data?.calls) ? data.calls : [] };
                }));
                renderHistory(groups);
            } catch (error) {
                elements.historyList.replaceChildren();
                const failed = doc.createElement('p');
                failed.className = 'chat-call-history-empty';
                failed.textContent = `读取通话记录失败：${error.message}`;
                elements.historyList.appendChild(failed);
            }
        }

        function renderHistory(groups) {
            if (!elements.historyList) return;
            elements.historyList.replaceChildren();
            let total = 0;
            groups.forEach(group => {
                group.calls.forEach(call => {
                    total += 1;
                    const article = doc.createElement('article');
                    article.className = 'chat-call-history-call';
                    const heading = doc.createElement('h3');
                    heading.textContent = `${getConversationLabel(group.conversationId) || group.conversationId} · ${formatCallTime(call.started_at)}`;
                    article.appendChild(heading);
                    const state = doc.createElement('p');
                    state.className = 'chat-call-history-meta';
                    state.textContent = call.status === 'active' ? '服务器记录为进行中' : '已结束';
                    article.appendChild(state);
                    const list = doc.createElement('ol');
                    list.className = 'chat-call-history-messages';
                    (Array.isArray(call.messages) ? call.messages : []).forEach(message => {
                        const item = doc.createElement('li');
                        item.className = message.role === 'user' ? 'is-user' : 'is-assistant';
                        const who = doc.createElement('strong');
                        who.textContent = message.role === 'user' ? '你' : 'Nacho';
                        const content = doc.createElement('span');
                        content.textContent = String(message.content || '');
                        item.append(who, content);
                        list.appendChild(item);
                    });
                    article.appendChild(list);
                    elements.historyList.appendChild(article);
                });
            });
            if (!total) {
                const empty = doc.createElement('p');
                empty.className = 'chat-call-history-empty';
                empty.textContent = '还没有保存的语音通话记录。';
                elements.historyList.appendChild(empty);
            }
        }

        function renderTranscript() {
            if (!elements.transcript || !doc) return;
            elements.transcript.replaceChildren();
            const messages = activeRecord?.messages || [];
            messages.forEach(message => {
                const item = doc.createElement('li');
                item.className = `chat-call-transcript-message ${message.role === 'user' ? 'is-user' : 'is-assistant'}`;
                const who = doc.createElement('strong');
                who.textContent = message.role === 'user' ? '你' : 'Nacho';
                const content = doc.createElement('span');
                content.textContent = String(message.content || '');
                if (message.interrupted) item.classList.add('is-interrupted');
                item.append(who, content);
                elements.transcript.appendChild(item);
            });
            elements.transcript.scrollTop = elements.transcript.scrollHeight;
        }

        async function requestJson(url, { method = 'GET', body, keepalive = false } = {}) {
            const response = await fetchResponse(url, { method, body, keepalive });
            if (!response.ok) throw await responseError(response);
            if (response.status === 204) return {};
            return response.json();
        }

        async function fetchResponse(url, { method = 'GET', body, keepalive = false, signal } = {}) {
            if (!fetcher) throw new Error('Fetch API 不可用');
            const init = { method, headers: {}, keepalive, signal };
            if (body !== undefined) {
                init.headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(body);
            }
            return fetcher(url, init);
        }

        async function responseError(response) {
            let detail = '';
            try {
                const data = await response.json();
                detail = data?.detail || data?.reason || '';
            } catch (_) {}
            const error = new Error(detail || `HTTP ${response.status}`);
            error.status = response.status;
            return error;
        }

        function callPath(callId) {
            return `${CALLS_URL}/${encodeURIComponent(callId)}`;
        }

        function normalizeRecord(record) {
            if (!record || typeof record !== 'object') return null;
            return {
                ...record,
                id: typeof record.id === 'string' ? record.id : '',
                conversation_id: typeof record.conversation_id === 'string' ? record.conversation_id : '',
                generation: Number(record.generation) || 0,
                messages: Array.isArray(record.messages) ? record.messages.slice() : [],
            };
        }

        function isEpochActive(epoch) {
            return Boolean(activeRecord && epoch === callEpoch);
        }

        function epochMilliseconds(value) {
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) return 0;
            return numeric > 1e12 ? numeric : numeric * 1000;
        }

        function formatCallTime(value) {
            const milliseconds = epochMilliseconds(value);
            if (!milliseconds) return '时间未知';
            return new Date(milliseconds).toLocaleString('zh-CN', { hour12: false });
        }

        function makeRequestId() {
            requestIdSerial += 1;
            if (win.crypto?.randomUUID) return win.crypto.randomUUID();
            return `voice-${Date.now().toString(36)}-${requestIdSerial.toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
        }

        function setText(element, value) {
            if (element) element.textContent = String(value || '');
        }

        function isAbort(error) {
            return error?.name === 'AbortError';
        }

        return {
            init,
            beginCall,
            endCall,
            handleVoiceEvent,
            guardConversationSwitch,
            openHistory,
            isActive: () => Boolean(activeRecord),
            getActiveRecord: () => activeRecord,
            __test: {
                beginBargeIn,
                transcribeSegment,
                sendTextMessage,
                renderTranscript,
                callPath,
                setRecord(record) {
                    callEpoch += 1;
                    activeRecord = normalizeRecord(record);
                    setCallActivity(Boolean(activeRecord));
                    if (activeRecord) showCallPanel();
                },
                getEpoch: () => callEpoch,
                getCurrentSpeech: () => currentSpeech,
                getAudio: () => audio,
                setAudio(value) { audio = value; },
            },
        };
    }

    root.ChatCall = { create };
})(typeof window !== 'undefined' ? window : globalThis);
