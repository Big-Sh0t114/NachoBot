/**
 * NachoBot WebUI — Chat Interface
 *
 * 当前负责聊天界面、会话本地持久化和未来聊天 API 的接入。
 * 真实回复接口约定：POST /api/chat/message
 * Request:  { conversation_id: string, message: string, user_name?: string }
 * Response: { conversation_id?: string, message?: { role?: string, content: string }, reply?: string }
 */
const ChatModule = (() => {
    const { createId, createSession, createTTS, escapeText, formatContent, formatTime, makeTitle } = window.ChatSupport;
    const { setRandomWelcomeSubtitle } = window.EasterEggSystem;
    const STORAGE_KEY = 'nachobot_chat_sessions_v1';
    const SIDEBAR_STATE_KEY = 'nachobot_sidebar_collapsed_v1';
    const DEFAULT_USER_NAME = 'WebUI';
    const API_ENDPOINT = '/api/chat/message';
    const MEDIA_ENDPOINT = '/api/chat/media';
    const DELETE_CONVERSATION_ENDPOINT = '/api/chat/conversations';
    const MAX_ATTACHMENTS = 8;
    const MEDIA_LIMITS = Object.freeze({
        image: 16 * 1024 * 1024,
        emoji: 16 * 1024 * 1024,
        video: 64 * 1024 * 1024,
        voice: 10 * 1024 * 1024,
        file: 1 * 1024 * 1024,
    });
    const MAX_VOICE_RECORDING_MS = 60_000;
    const AUDIO_FILE_EXTENSIONS = /\.(?:aac|flac|m4a|mp3|oga|ogg|opus|wav|weba|webm)$/i;
    const CHAT_LAUNCH_PROFILES = Object.freeze({
        full: {
            name: '完整模式',
            code: 'FULL',
            summary: 'TTS · VLM · ASR',
            detail: '完整本地多模态能力，资源占用最高',
        },
        lite: {
            name: '轻量模式',
            code: 'LITE',
            summary: 'TTS',
            detail: '保留语音合成，关闭 VLM / ASR',
        },
        potato: {
            name: '文本模式',
            code: 'POTATO',
            summary: 'Core text + QQ',
            detail: 'Core 文本档位 + 所选 QQ 适配器，生成纯文本回复；不启动本地 Multimodal 服务',
        },
    });
    const WELCOME_SUBTITLES = [
        '我的存在，由你定义',
        '宝宝你是一个一个一个Tips啊啊啊啊',
        '我在这里，等待你的下一句话',
        '我的世界，从你的消息开始',
        '或许我没有心，但我会记得如何回应你',
        '你的一次回车，足以唤醒整个系统',
        '不清楚管理员可用哪些指令？输入#help_all试试',
        '有些功能不会主动介绍自己，需要你亲自发现',
        '请不要扣美少女机器人',
        '本页面由 HTML、JavaScript 和大量执念驱动',
        '你的消息正在穿越 WebUI、核心与适配器',
        '本Tip由甘油三酯编写',
        '本Tip不由AI编写',
        '其他Tips是AI写的吗？',
        '当你看到这一行的时候，说明它成功显示出来了',
        '你有数过有多少条Tips吗？',
        '想给某人带来一些惊喜吗？何不试试#convey_<QQ号>+<信息>',
        'DUMMY DUMMY DUMMY DUMMY DUMMY DUMMY DUMMY DUMMY',
        '请输入文本请输入文本请输入文本请输入文本请输入文本',
        '无偿招募有意思的Tips，有意者请联系甘油三酯谢谢喵',
        '你的一言，我的一切',

    ];

    let initialized = false;
    const pendingRequestIds = new Map();
    let sessions = [];
    let activeSessionId = null;
    let historyQuery = '';
    let liveSocket = null;
    let liveConversationId = null;
    let liveReconnectTimer = null;
    let profile = null;
    let ttsController = null;
    let callController = null;
    let modalOpen = false;
    let coreRunning = false;
    let coreToggleBusy = false;
    let coreStatusRequestSerial = 0;
    let coreStatusRequestPromise = null;
    let coreStatusRefreshPending = false;
    let pendingAttachments = [];
    let attachmentDragDepth = 0;
    let voiceCapture = null;
    let voiceRecordStatusTimer = null;
    let els = {};

    function init() {
        if (initialized) return;

        els = {
            historyList: document.getElementById('chat-history-list'),
            historySearch: document.getElementById('chat-history-search'),
            historyCount: document.getElementById('chat-history-count'),
            historyEmpty: document.getElementById('chat-history-empty'),
            welcomeSubtitle: document.getElementById('chat-welcome-subtitle'),
            newChat: document.getElementById('chat-new-button'),
            clearChat: document.getElementById('chat-clear-button'),
            messages: document.getElementById('chat-messages'),
            empty: document.getElementById('chat-empty-state'),
            tab: document.getElementById('tab-chat'),
            form: document.getElementById('chat-composer-form'),
            input: document.getElementById('chat-input'),
            send: document.getElementById('chat-send-button'),
            attachmentButton: document.getElementById('chat-attachment-button'),
            voiceRecordButton: document.getElementById('chat-voice-record-button'),
            voiceRecordStatus: document.getElementById('chat-voice-record-status'),
            fileInput: document.getElementById('chat-file-input'),
            attachmentPreview: document.getElementById('chat-attachment-preview'),
            status: document.getElementById('chat-backend-status'),
            title: document.getElementById('chat-current-title'),
                sidebar: document.getElementById('sidebar'),
            sidebarCollapse: document.getElementById('sidebar-collapse-button'),
            sidebarBrand: document.getElementById('sidebar-brand-button'),
            renameButton: document.getElementById('chat-rename-button'),
        };

        if (!els.messages || !els.form || !els.input) return;

        // 旧版发送流程会在等待 Core 回复期间直接禁用 textarea。
        // 无论脚本是否热更新、DOM 是否被复用，初始化时都强制恢复输入能力。
        els.input.disabled = false;

        profile = window.ChatProfile.create({
            getInput: () => els.input,
            isModalOpen: () => modalOpen,
            render: renderAll,
            setModalOpen: value => { modalOpen = Boolean(value); },
            escapeText,
            toast,
        });
        ttsController = createTTS({
            getActiveSession,
            getMessagesElement: () => els.messages,
            escapeText,
            apiGet,
            toast,
        });

        loadSessions();
        profile.load();
        callController = window.ChatCall?.create({
            getConversation: getActiveSession,
            getUserName: () => profile.getUserName(),
            getConversationLabel: id => sessions.find(item => item.id === id)?.title || '当前对话',
            getFallbackAvatar: () => profile.createBotAvatarMarkup(),
            stopTextTTS: () => ttsController.stop(),
            onActivityChange: active => UI.setVoiceCallActive(active),
            toast,
        });
        callController?.init();
        restoreSidebarState();
        setRandomWelcomeSubtitle(els.welcomeSubtitle, WELCOME_SUBTITLES);
        bindEvents();
        bindVoiceNavigationCleanup();
        renderAll();
        connectLiveStream(activeSessionId);
        updateBackendStatus(true);
        ttsController.updateStatus();
        initialized = true;
        profile.scheduleFirstUseNamePrompt();
    }

    function launchCoreIsExternal(state) {
        const core = state?.core;
        return Boolean(core && core.origin === 'external' && core.managed === false);
    }

    function launchHasOwnedDependents(state) {
        return (state?.profiles || []).some(profile =>
            (profile?.services || []).some(service =>
                service?.managed === true
                && service?.origin === 'webui'
                && service.status !== 'stopped',
            ),
        );
    }

    function launchExternalProfileIsReady(state) {
        if (!launchCoreIsExternal(state) || state?.core?.status !== 'running') return false;
        const profileId = state?.active_profile || state?.core?.observed_profile;
        const profile = (state?.profiles || []).find(item => item?.id === profileId);
        if (profile?.status === 'running') return true;
        const requiredIds = profileId === 'full'
            ? ['tts_runtime_full', 'perception']
            : profileId === 'lite'
                ? ['tts_runtime_lite']
                : [];
        return requiredIds.length > 0
            && requiredIds.every(id => profile?.services?.some(
                service => service?.id === id && service.status === 'running',
            ));
    }

    const EXTERNAL_LAUNCHER_NOTICE = '由外部启动器管理，请在原启动窗口停止';

    function refresh() {
        if (!initialized) {
            init();
            return;
        }
        renderAll();
        updateBackendStatus(true);
        ttsController.updateStatus(true);
    }

    function loadSessions() {
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
            sessions = Array.isArray(saved) ? saved.filter(isValidSession) : [];
        } catch (error) {
            console.warn('Failed to load chat sessions:', error);
            sessions = [];
        }

        if (sessions.length === 0) {
            const first = createSession();
            sessions = [first];
            activeSessionId = first.id;
            saveSessions();
        } else {
            sessions.sort((a, b) => b.updatedAt - a.updatedAt);
            activeSessionId = sessions[0].id;
        }
    }

    function isValidSession(session) {
        return session
            && typeof session.id === 'string'
            && typeof session.title === 'string'
            && Array.isArray(session.messages);
    }

    function saveSessions() {
        sessions.sort((a, b) => b.updatedAt - a.updatedAt);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
    }

    function getActiveSession() {
        return sessions.find(session => session.id === activeSessionId) || sessions[0] || null;
    }

    function bindEvents() {
        els.newChat?.addEventListener('click', startNewChat);
        els.clearChat?.addEventListener('click', clearCurrentChat);
        els.renameButton?.addEventListener('click', () => profile.showRenamePrompt());
        els.status?.addEventListener('click', toggleCoreService);
        els.form.addEventListener('submit', handleSubmit);
        els.voiceRecordButton?.addEventListener('click', toggleVoiceRecording);

        els.attachmentButton?.addEventListener('click', () => {
            if (!els.fileInput) return;
            els.fileInput.accept = '';
            els.fileInput.click();
        });

        els.fileInput?.addEventListener('change', () => {
            const files = Array.from(els.fileInput.files || []);
            addDroppedFiles(files);
            els.fileInput.value = '';
        });

        const hasDraggedFiles = event => Array.from(event.dataTransfer?.types || []).includes('Files');
        const isVisibleElement = element => Boolean(element && !element.hidden);
        const isFileDropBlockedByCallUi = () => {
            const workspace = document.getElementById('chat-workspace');
            const callPanel = document.getElementById('chat-call-panel');
            const startDialog = document.getElementById('chat-call-start-dialog');
            return workspace?.dataset.callView === 'active'
                || isVisibleElement(callPanel)
                || isVisibleElement(startDialog);
        };
        const clearFileDragState = () => {
            attachmentDragDepth = 0;
            els.tab?.classList.remove('is-file-drag-over');
        };
        const blockCallUiFileDrop = event => {
            event.preventDefault();
            if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
            clearFileDragState();
        };

        els.tab?.addEventListener('dragenter', event => {
            if (!hasDraggedFiles(event)) return;
            if (isFileDropBlockedByCallUi()) {
                blockCallUiFileDrop(event);
                return;
            }
            event.preventDefault();
            attachmentDragDepth += 1;
            els.tab.classList.add('is-file-drag-over');
        });

        els.tab?.addEventListener('dragover', event => {
            if (!hasDraggedFiles(event)) return;
            if (isFileDropBlockedByCallUi()) {
                blockCallUiFileDrop(event);
                return;
            }
            event.preventDefault();
            if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
            els.tab.classList.add('is-file-drag-over');
        });

        els.tab?.addEventListener('dragleave', event => {
            if (!hasDraggedFiles(event)) return;
            if (isFileDropBlockedByCallUi()) {
                clearFileDragState();
                return;
            }
            attachmentDragDepth = Math.max(0, attachmentDragDepth - 1);
            if (attachmentDragDepth === 0) {
                els.tab.classList.remove('is-file-drag-over');
            }
        });

        els.tab?.addEventListener('drop', event => {
            if (!hasDraggedFiles(event)) return;
            if (isFileDropBlockedByCallUi()) {
                blockCallUiFileDrop(event);
                return;
            }
            event.preventDefault();
            clearFileDragState();
            addDroppedFiles(Array.from(event.dataTransfer?.files || []));
        });

        els.attachmentPreview?.addEventListener('click', event => {
            const removeButton = event.target.closest('[data-remove-attachment-id]');
            if (!removeButton) return;
            removePendingAttachment(removeButton.dataset.removeAttachmentId || '');
        });
        bindVoicePlayerEvents(els.attachmentPreview);

        els.messages.addEventListener('click', event => {
            const speechButton = event.target.closest('.chat-tts-button');
            if (speechButton && els.messages.contains(speechButton)) {
                ttsController.handleSpeechButton(speechButton);
                return;
            }

            const avatar = event.target.closest('.chat-avatar-customizable');
            if (!avatar || !els.messages.contains(avatar)) return;
            profile.openBotAvatarPicker();
        });
        bindVoicePlayerEvents(els.messages);

        document.addEventListener('keydown', event => {
            const capsLockActive = typeof event.getModifierState === 'function'
                && event.getModifierState('CapsLock');
            if (
                event.key === 'Tab'
                && capsLockActive
                && !event.ctrlKey
                && !event.altKey
                && !event.metaKey
            ) {
                event.preventDefault();
                profile.showRenamePrompt();
            }
        });

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                cancelVoiceRecording('页面已隐藏，录音已取消');
                pauseVoicePlayers(document);
            }
        });
        window.addEventListener('pagehide', () => {
            cancelVoiceRecording();
            pauseVoicePlayers(document);
        });

        els.historySearch?.addEventListener('input', event => {
            historyQuery = event.target.value.trim().toLocaleLowerCase('zh-CN');
            renderHistory();
        });

        els.sidebarCollapse?.addEventListener('click', () => {
            if (window.innerWidth <= 768) {
                closeSidebarOnMobile();
            } else {
                setSidebarCollapsed(true);
            }
        });

        els.sidebarBrand?.addEventListener('click', () => {
            if (document.body.classList.contains('sidebar-collapsed')) {
                setSidebarCollapsed(false);
            }
        });

        els.input.addEventListener('input', () => {
            autoResizeInput();
            updateSendState();
        });

        els.input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                els.form.requestSubmit();
            }
        });

        els.mobileHistory?.addEventListener('click', event => {
            event.stopPropagation();
            document.body.classList.toggle('sidebar-mobile-open');
        });

        document.addEventListener('click', event => {
            if (!document.body.classList.contains('sidebar-mobile-open')) return;
            if (els.sidebar?.contains(event.target) || els.mobileHistory?.contains(event.target)) return;
            closeSidebarOnMobile();
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth > 768) closeSidebarOnMobile();
        });

        document.querySelectorAll('[data-chat-prompt]').forEach(button => {
            button.addEventListener('click', () => {
                els.input.value = button.dataset.chatPrompt || '';
                autoResizeInput();
                updateSendState();
                els.input.focus();
            });
        });
    }

    function mediaTypeLabel(type) {
        return type === 'image'
            ? '图片'
            : type === 'emoji'
                ? '表情包'
                : type === 'video'
                    ? '视频'
                    : type === 'voice'
                        ? '语音'
                    : '文件';
    }

    function formatBytes(size) {
        const value = Number(size || 0);
        if (value < 1024) return `${value} B`;
        if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
        return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
    }

    function audioMimeTypeFromName(name) {
        const extension = String(name || '').split('.').pop().toLowerCase();
        return ({
            aac: 'audio/aac',
            flac: 'audio/flac',
            m4a: 'audio/mp4',
            mp3: 'audio/mpeg',
            oga: 'audio/ogg',
            ogg: 'audio/ogg',
            opus: 'audio/ogg',
            wav: 'audio/wav',
            weba: 'audio/webm',
            webm: 'audio/webm',
        })[extension] || 'application/octet-stream';
    }

    function attachmentMimeType(file, type) {
        if (type !== 'voice') return file.type || 'application/octet-stream';
        if (file.type.startsWith('audio/')) return file.type;
        const inferred = audioMimeTypeFromName(file.name);
        return inferred !== 'application/octet-stream'
            ? inferred
            : file.type || 'application/octet-stream';
    }

    function inferDroppedMediaType(file) {
        if (!(file instanceof File)) return 'file';
        if (file.type.startsWith('image/')) return 'image';
        if (file.type.startsWith('video/')) return 'video';
        if (file.type.startsWith('audio/')) return 'voice';
        if (AUDIO_FILE_EXTENSIONS.test(file.name || '')) return 'voice';
        return 'file';
    }

    function addDroppedFiles(files) {
        if (!Array.isArray(files) || files.length === 0) return;
        const available = Math.max(0, MAX_ATTACHMENTS - pendingAttachments.length);
        if (available <= 0) {
            toast(`最多同时发送 ${MAX_ATTACHMENTS} 个附件`, 'error');
            return;
        }
        const accepted = files.slice(0, available);
        if (files.length > available) {
            toast(`最多同时发送 ${MAX_ATTACHMENTS} 个附件`, 'error');
        }
        const grouped = { image: [], video: [], voice: [], file: [] };
        accepted.forEach(file => {
            if (!(file instanceof File)) return;
            grouped[inferDroppedMediaType(file)].push(file);
        });
        Object.entries(grouped).forEach(([type, items]) => {
            if (items.length) addPendingFiles(items, type);
        });
    }

    function addPendingFiles(files, type) {
        if (!Array.isArray(files) || files.length === 0) return;
        const mediaType = MEDIA_LIMITS[type] ? type : 'file';
        for (const file of files) {
            if (pendingAttachments.length >= MAX_ATTACHMENTS) {
                toast(`最多同时发送 ${MAX_ATTACHMENTS} 个附件`, 'error');
                break;
            }
            if (!(file instanceof File)) continue;
            if ((mediaType === 'image' || mediaType === 'emoji') && !file.type.startsWith('image/')) {
                toast(`${file.name} 不是支持的图片格式`, 'error');
                continue;
            }
            if (mediaType === 'video' && !file.type.startsWith('video/')) {
                toast(`${file.name} 不是支持的视频格式`, 'error');
                continue;
            }
            if (mediaType === 'voice'
                && file.type
                && !file.type.startsWith('audio/')
                && !AUDIO_FILE_EXTENSIONS.test(file.name || '')) {
                toast(`${file.name} 不是支持的音频格式`, 'error');
                continue;
            }
            const limit = MEDIA_LIMITS[mediaType];
            if (file.size <= 0) {
                toast(`${file.name} 内容为空`, 'error');
                continue;
            }
            if (file.size > limit) {
                toast(`${file.name} 超过 ${formatBytes(limit)} 限制`, 'error');
                continue;
            }
            const mimeType = attachmentMimeType(file, mediaType);
            const previewSource = mediaType === 'voice' && !file.type.startsWith('audio/')
                ? new Blob([file], { type: mimeType })
                : file;
            pendingAttachments.push({
                id: createId(),
                type: mediaType,
                file,
                name: file.name,
                size: file.size,
                mimeType,
                previewUrl: (mediaType === 'image' || mediaType === 'emoji' || mediaType === 'voice')
                    ? URL.createObjectURL(previewSource)
                    : '',
            });
        }
        renderAttachmentPreview();
        updateSendState();
    }

    function removePendingAttachment(id) {
        const index = pendingAttachments.findIndex(item => item.id === id);
        if (index < 0) return;
        disposeVoicePlayers(els.attachmentPreview);
        const [removed] = pendingAttachments.splice(index, 1);
        if (removed.previewUrl) URL.revokeObjectURL(removed.previewUrl);
        renderAttachmentPreview();
        updateSendState();
    }

    function clearPendingAttachments() {
        disposeVoicePlayers(els.attachmentPreview);
        pendingAttachments.forEach(item => {
            if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
        });
        pendingAttachments = [];
        renderAttachmentPreview();
        updateSendState();
    }

    function renderAttachmentPreview() {
        if (!els.attachmentPreview) return;
        disposeVoicePlayers(els.attachmentPreview);
        if (pendingAttachments.length === 0) {
            els.attachmentPreview.innerHTML = '';
            els.attachmentPreview.hidden = true;
            return;
        }
        els.attachmentPreview.hidden = false;
        els.attachmentPreview.innerHTML = pendingAttachments.map(item => item.type === 'voice'
            ? `
                ${renderVoicePlayer(item.previewUrl, {
                    playerClass: 'chat-pending-voice',
                    audioClass: 'chat-pending-voice-audio',
                    label: '待发送语音消息',
                    removeId: item.id,
                })}
            `
            : `
                <div class="chat-pending-attachment">
                    ${item.previewUrl ? `<img src="${escapeText(item.previewUrl)}" alt="">` : ''}
                    <span class="chat-pending-attachment-copy">
                        <strong>${escapeText(item.name)}</strong>
                        <small>${mediaTypeLabel(item.type)} · ${formatBytes(item.size)}</small>
                    </span>
                    <button type="button" class="chat-pending-attachment-remove"
                        data-remove-attachment-id="${escapeText(item.id)}" aria-label="移除附件">×</button>
                </div>
            `).join('');
        initializeVoicePlayers(els.attachmentPreview);
    }

    function bindVoiceNavigationCleanup() {
        const cancelWhenUnavailable = () => {
            if (!isVoiceCaptureAllowed()) {
                if (voiceCapture) cancelVoiceRecording('已离开聊天页面，录音已取消');
                pauseVoicePlayers(els.messages);
                pauseVoicePlayers(els.attachmentPreview);
            }
        };
        const observer = typeof MutationObserver === 'function'
            ? new MutationObserver(cancelWhenUnavailable)
            : null;
        if (observer) {
            if (els.tab) observer.observe(els.tab, { attributes: true, attributeFilter: ['class'] });
            const workspace = document.getElementById('chat-workspace');
            const callPanel = document.getElementById('chat-call-panel');
            const startDialog = document.getElementById('chat-call-start-dialog');
            if (workspace) observer.observe(workspace, { attributes: true, attributeFilter: ['data-call-view'] });
            if (callPanel) observer.observe(callPanel, { attributes: true, attributeFilter: ['hidden', 'data-call-motion-visible'] });
            if (startDialog) observer.observe(startDialog, { attributes: true, attributeFilter: ['hidden'] });
        }
    }

    function isVoiceCaptureAllowed() {
        if (document.hidden || !els.tab?.classList.contains('active')) return false;
        const workspace = document.getElementById('chat-workspace');
        const callPanel = document.getElementById('chat-call-panel');
        const startDialog = document.getElementById('chat-call-start-dialog');
        const callPanelVisible = Boolean(
            callPanel && !callPanel.hidden && callPanel.dataset.callMotionVisible !== 'false',
        );
        return workspace?.dataset.callView !== 'active'
            && !callPanelVisible
            && (startDialog?.hidden !== false);
    }

    function toggleVoiceRecording() {
        if (voiceCapture?.phase === 'requesting') {
            cancelVoiceRecording('录音请求已取消');
        } else if (voiceCapture?.phase === 'recording') {
            stopVoiceRecording();
        } else if (!voiceCapture) {
            startVoiceRecording();
        }
    }

    async function startVoiceRecording() {
        if (!window.navigator?.mediaDevices?.getUserMedia || typeof window.MediaRecorder !== 'function') {
            const message = '当前浏览器不支持麦克风录音';
            setVoiceRecordStatus(message, 'error', 5000);
            toast(message, 'error');
            return;
        }
        if (!isVoiceCaptureAllowed()) {
            const message = '请先返回聊天页面，再开始录音';
            setVoiceRecordStatus(message, 'error', 4000);
            toast(message, 'error');
            return;
        }

        const capture = {
            phase: 'requesting',
            stream: null,
            recorder: null,
            chunks: [],
            shouldAttach: true,
            startedAt: 0,
            stopTimer: null,
            clockTimer: null,
            stopped: false,
            error: null,
        };
        voiceCapture = capture;
        clearVoiceRecordStatus();
        updateVoiceRecordingControls();

        let stream = null;
        try {
            stream = await window.navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            });
            capture.stream = stream;
            if (voiceCapture !== capture) {
                stopVoiceStream(stream);
                return;
            }
            if (!isVoiceCaptureAllowed()) {
                stopVoiceStream(stream);
                cancelVoiceRecording('已离开聊天页面，录音已取消');
                return;
            }

            const mimeType = chooseVoiceRecorderMimeType();
            const recorder = mimeType
                ? new window.MediaRecorder(stream, { mimeType })
                : new window.MediaRecorder(stream);
            capture.recorder = recorder;
            recorder.ondataavailable = event => {
                if (event.data?.size && capture.shouldAttach) capture.chunks.push(event.data);
            };
            recorder.onerror = event => {
                capture.error = event.error || new Error('麦克风录音失败');
                capture.shouldAttach = false;
                stopVoiceRecording(capture, false);
            };
            recorder.onstop = () => completeVoiceRecording(capture);
            recorder.start(250);
            capture.phase = 'recording';
            capture.startedAt = Date.now();
            updateVoiceRecordingControls();
            capture.stopTimer = window.setTimeout(() => stopVoiceRecording(capture), MAX_VOICE_RECORDING_MS);
            capture.clockTimer = window.setInterval(() => updateVoiceRecordingControls(), 500);
        } catch (error) {
            stopVoiceStream(stream || capture.stream);
            clearVoiceCaptureTimers(capture);
            if (voiceCapture === capture) {
                voiceCapture = null;
                updateVoiceRecordingControls();
                const message = error?.name === 'NotAllowedError' || error?.name === 'PermissionDeniedError'
                    ? '麦克风权限未获准'
                    : `无法开始录音：${error?.message || '麦克风不可用'}`;
                setVoiceRecordStatus(message, 'error', 6000);
                toast(message, 'error');
            }
        }
    }

    function chooseVoiceRecorderMimeType() {
        const supported = [
            'audio/wav',
            'audio/webm;codecs=opus',
            'audio/ogg;codecs=opus',
            'audio/mp4',
        ];
        if (typeof window.MediaRecorder.isTypeSupported !== 'function') return '';
        return supported.find(type => window.MediaRecorder.isTypeSupported(type)) || '';
    }

    function stopVoiceRecording(target = voiceCapture, shouldAttach = true) {
        const capture = target;
        if (!capture) return;
        if (capture.phase === 'requesting') {
            cancelVoiceRecording('录音请求已取消');
        }
        if (capture.phase === 'processing' || voiceCapture !== capture) return;

        capture.phase = 'processing';
        capture.shouldAttach = shouldAttach;
        clearVoiceCaptureTimers(capture);
        updateVoiceRecordingControls();
        if (capture.recorder?.state === 'recording') {
            try {
                capture.recorder.stop();
            } catch (error) {
                capture.error = error;
                stopVoiceStream(capture.stream);
                completeVoiceRecording(capture);
            }
        } else {
            stopVoiceStream(capture.stream);
            completeVoiceRecording(capture);
        }
    }

    function cancelVoiceRecording(message = '') {
        const capture = voiceCapture;
        if (capture) {
            voiceCapture = null;
            capture.shouldAttach = false;
            clearVoiceCaptureTimers(capture);
            if (capture.recorder?.state === 'recording') {
                try { capture.recorder.stop(); } catch (_) {}
            }
            stopVoiceStream(capture.stream);
        }
        updateVoiceRecordingControls();
        if (message && capture) setVoiceRecordStatus(message, '', 3500);
    }

    function completeVoiceRecording(capture) {
        if (!capture || capture.stopped) return;
        capture.stopped = true;
        clearVoiceCaptureTimers(capture);
        stopVoiceStream(capture.stream);
        capture.stream = null;
        const wasCurrent = voiceCapture === capture;
        const shouldAttach = capture.shouldAttach && wasCurrent;
        if (wasCurrent) voiceCapture = null;

        if (wasCurrent && capture.error) {
            const message = `录音失败：${capture.error.message || '无法读取麦克风数据'}`;
            setVoiceRecordStatus(message, 'error', 6000);
            toast(message, 'error');
        } else if (shouldAttach) {
            const recorderMime = String(capture.recorder?.mimeType || '').split(';', 1)[0].trim().toLowerCase();
            const chunkMime = String(capture.chunks.find(item => item.type)?.type || '').split(';', 1)[0].trim().toLowerCase();
            const mimeType = (recorderMime.startsWith('audio/') ? recorderMime : '')
                || (chunkMime.startsWith('audio/') ? chunkMime : '')
                || 'audio/webm';
            const extension = audioExtensionForMimeType(mimeType);
            const blob = new Blob(capture.chunks, { type: mimeType });
            if (blob.size <= 0) {
                const message = '没有录到音频，请重试';
                setVoiceRecordStatus(message, 'error', 5000);
                toast(message, 'error');
            } else if (blob.size > MEDIA_LIMITS.voice) {
                const message = `语音消息超过 ${formatBytes(MEDIA_LIMITS.voice)} 限制`;
                setVoiceRecordStatus(message, 'error', 5000);
                toast(message, 'error');
            } else {
                try {
                    const file = new File([blob], `voice-message-${Date.now()}.${extension}`, { type: mimeType });
                    const previousCount = pendingAttachments.length;
                    addPendingFiles([file], 'voice');
                    setVoiceRecordStatus(
                        pendingAttachments.length > previousCount ? '语音已添加，可试听或移除' : '录音未能添加，请重试',
                        pendingAttachments.length > previousCount ? 'success' : 'error',
                        5000,
                    );
                } catch (error) {
                    const message = `无法准备语音消息：${error.message || '浏览器不支持文件附件'}`;
                    setVoiceRecordStatus(message, 'error', 6000);
                    toast(message, 'error');
                }
            }
        }
        updateVoiceRecordingControls();
    }

    function audioExtensionForMimeType(mimeType) {
        const baseType = String(mimeType || '').split(';', 1)[0].trim().toLowerCase();
        return ({
            'audio/aac': 'aac',
            'audio/flac': 'flac',
            'audio/mp4': 'm4a',
            'audio/mpeg': 'mp3',
            'audio/ogg': 'ogg',
            'audio/wav': 'wav',
            'audio/webm': 'weba',
        })[baseType] || 'webm';
    }

    function updateVoiceRecordingControls() {
        const button = els.voiceRecordButton;
        const status = els.voiceRecordStatus;
        if (!button || !status) {
            updateSendState();
            return;
        }
        const phase = voiceCapture?.phase || '';
        button.classList.toggle('is-recording', phase === 'recording');
        button.classList.toggle('is-processing', phase === 'processing');
        button.disabled = phase === 'processing';
        button.setAttribute('aria-pressed', String(phase === 'recording'));
        if (phase === 'requesting') {
            button.setAttribute('aria-label', '取消麦克风权限请求');
            button.title = '正在请求麦克风权限，点击可取消';
            setActiveVoiceRecordStatus('正在请求麦克风权限…');
        } else if (phase === 'recording') {
            button.setAttribute('aria-label', '停止录音并添加语音');
            button.title = '点击停止录音并添加语音，最长 60 秒';
            const elapsedSeconds = Math.min(60, Math.floor((Date.now() - voiceCapture.startedAt) / 1000));
            setActiveVoiceRecordStatus(`正在录音 · ${formatRecordingTime(elapsedSeconds)} / 1:00`);
        } else if (phase === 'processing') {
            button.setAttribute('aria-label', '正在处理录音');
            button.title = '正在处理录音…';
            setActiveVoiceRecordStatus('正在处理录音…');
        } else {
            button.setAttribute('aria-label', '录制语音消息');
            button.title = '录制语音消息';
        }
        updateSendState();
    }

    function setActiveVoiceRecordStatus(message) {
        if (!els.voiceRecordStatus) return;
        if (voiceRecordStatusTimer) {
            window.clearTimeout(voiceRecordStatusTimer);
            voiceRecordStatusTimer = null;
        }
        els.voiceRecordStatus.textContent = message;
        els.voiceRecordStatus.hidden = false;
        els.voiceRecordStatus.classList.remove('is-error', 'is-success');
    }

    function setVoiceRecordStatus(message, tone = '', timeoutMs = 0) {
        if (!els.voiceRecordStatus) return;
        if (voiceRecordStatusTimer) {
            window.clearTimeout(voiceRecordStatusTimer);
            voiceRecordStatusTimer = null;
        }
        els.voiceRecordStatus.textContent = message;
        els.voiceRecordStatus.hidden = !message;
        els.voiceRecordStatus.classList.toggle('is-error', tone === 'error');
        els.voiceRecordStatus.classList.toggle('is-success', tone === 'success');
        if (message && timeoutMs > 0) {
            voiceRecordStatusTimer = window.setTimeout(() => {
                voiceRecordStatusTimer = null;
                if (!voiceCapture) clearVoiceRecordStatus();
            }, timeoutMs);
        }
    }

    function clearVoiceRecordStatus() {
        if (voiceRecordStatusTimer) {
            window.clearTimeout(voiceRecordStatusTimer);
            voiceRecordStatusTimer = null;
        }
        if (!els.voiceRecordStatus) return;
        els.voiceRecordStatus.textContent = '';
        els.voiceRecordStatus.hidden = true;
        els.voiceRecordStatus.classList.remove('is-error', 'is-success');
    }

    function formatRecordingTime(seconds) {
        return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
    }

    function clearVoiceCaptureTimers(capture) {
        if (capture?.stopTimer) window.clearTimeout(capture.stopTimer);
        if (capture?.clockTimer) window.clearInterval(capture.clockTimer);
        if (capture) {
            capture.stopTimer = null;
            capture.clockTimer = null;
        }
    }

    function stopVoiceStream(stream) {
        stream?.getTracks?.().forEach(track => {
            try { track.stop(); } catch (_) {}
        });
    }

    function fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(reader.error || new Error('读取附件失败'));
            reader.onload = () => {
                const result = typeof reader.result === 'string' ? reader.result : '';
                const comma = result.indexOf(',');
                resolve(comma >= 0 ? result.slice(comma + 1) : result);
            };
            reader.readAsDataURL(file);
        });
    }

    async function uploadPendingAttachment(item) {
        const dataBase64 = await fileToBase64(item.file);
        const response = await fetch(MEDIA_ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: item.type,
                name: item.name,
                mime_type: item.mimeType,
                data_base64: dataBase64,
            }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(data?.detail || `附件上传失败（HTTP ${response.status}）`);
            error.status = response.status;
            throw error;
        }
        return data;
    }

    async function startNewChat() {
        if (callController && !await callController.guardConversationSwitch(null)) return;
        cancelVoiceRecording('已切换会话，录音已取消');
        ttsController.stop();
        const session = createSession();
        sessions.unshift(session);
        activeSessionId = session.id;
        saveSessions();
        App.switchTab('chat');
        connectLiveStream(session.id);
        renderAll();
        els.input.focus();
        closeSidebarOnMobile();
    }

    function clearCurrentChat() {
        const session = getActiveSession();
        if (!session) return;
        if (session.messages.length === 0) {
            cancelVoiceRecording('聊天已重置，录音已取消');
            return;
        }
        if (!window.confirm('清空当前对话中的全部消息？')) return;

        cancelVoiceRecording('聊天已重置，录音已取消');
        ttsController.stop();
        pendingRequestIds.delete(session.id);
        session.messages = [];
        session.title = '新对话';
        session.updatedAt = Date.now();
        saveSessions();
        renderAll();
    }

    async function deleteSession(id) {
        const session = sessions.find(item => item.id === id);
        if (!session) return;
        if (callController?.getActiveRecord()?.conversation_id === id
            && !await callController.guardConversationSwitch(null, 'delete')) return;

        const confirmed = await confirmDeleteSession(session);
        if (!confirmed) return;
        if (activeSessionId === id) cancelVoiceRecording('已删除会话，录音已取消');

        const deletingActiveSession = activeSessionId === id;
        if (deletingActiveSession) {
            if (liveReconnectTimer) {
                clearTimeout(liveReconnectTimer);
                liveReconnectTimer = null;
            }
            liveConversationId = null;
            if (liveSocket) {
                liveSocket.close();
                liveSocket = null;
            }
        }

        try {
            const response = await fetch(
                `${DELETE_CONVERSATION_ENDPOINT}/${encodeURIComponent(id)}`,
                { method: 'DELETE' },
            );
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                const error = new Error(data?.detail || `HTTP ${response.status}`);
                error.status = response.status;
                throw error;
            }

            pendingRequestIds.delete(id);
            if (sessions.length === 1) {
                sessions[0] = createSession();
                activeSessionId = sessions[0].id;
            } else {
                sessions = sessions.filter(item => item.id !== id);
                if (deletingActiveSession) activeSessionId = sessions[0].id;
            }

            saveSessions();
            connectLiveStream(activeSessionId);
            renderAll();
            closeSidebarOnMobile();

            const deletedRows = Number(data?.deleted_rows || 0);
            toast(
                deletedRows > 0
                    ? `会话及其关联数据库记录已删除（${deletedRows} 条）`
                    : '会话已删除；数据库中没有找到关联记录',
                'success',
            );
        } catch (error) {
            console.warn('Failed to delete chat conversation:', error);
            if (deletingActiveSession && activeSessionId === id) {
                connectLiveStream(id);
            }
            toast(`删除失败，未移除本地会话：${error.message}`, 'error');
        }
    }

    function confirmDeleteSession(session) {
        if (modalOpen) {
            toast('请先关闭当前弹窗', 'error');
            return Promise.resolve(false);
        }

        const overlay = document.getElementById('modal-overlay');
        const card = document.getElementById('modal-card');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');
        const footer = document.getElementById('modal-footer');
        const closeButton = document.getElementById('modal-close');

        if (!overlay || !card || !title || !body || !footer || !closeButton) {
            return Promise.resolve(window.confirm(
                `确定删除对话“${session.title}”吗？\n\n此操作不可撤销，并会同时删除该会话在 NachoBot 数据库中的人物资料、记忆、消息与聊天记录。`,
            ));
        }

        return new Promise(resolve => {
            modalOpen = true;
            card.classList.add('chat-name-modal');
            title.textContent = '永久删除此对话？';
            body.innerHTML = `
                <div class="chat-name-prompt">
                    <p>即将删除对话「${escapeText(session.title)}」。</p>
                    <p><strong>此操作不可撤销。</strong></p>
                    <p>除浏览器中的聊天记录外，还会删除该会话在 NachoBot 数据库中独立创建的人物资料、记忆、消息、聊天流、摘要和相关统计记录。</p>
                </div>
            `;
            footer.innerHTML = '';

            let settled = false;
            const finish = result => {
                if (settled) return;
                settled = true;
                overlay.classList.add('hidden');
                card.classList.remove('chat-name-modal');
                closeButton.style.removeProperty('display');
                closeButton.onclick = null;
                overlay.onclick = null;
                modalOpen = false;
                resolve(result);
            };

            const cancelButton = document.createElement('button');
            cancelButton.type = 'button';
            cancelButton.className = 'btn btn-ghost';
            cancelButton.textContent = '取消';
            cancelButton.addEventListener('click', () => finish(false));
            footer.appendChild(cancelButton);

            const deleteButton = document.createElement('button');
            deleteButton.type = 'button';
            deleteButton.className = 'btn btn-danger';
            deleteButton.textContent = '永久删除';
            deleteButton.addEventListener('click', () => finish(true));
            footer.appendChild(deleteButton);

            closeButton.style.removeProperty('display');
            closeButton.onclick = () => finish(false);
            overlay.onclick = event => {
                if (event.target === overlay) finish(false);
            };
            overlay.classList.remove('hidden');
            requestAnimationFrame(() => deleteButton.focus());
        });
    }

    async function switchSession(id) {
        if (callController && !await callController.guardConversationSwitch(id)) return;
        if (!sessions.some(session => session.id === id)) return;
        cancelVoiceRecording('已切换会话，录音已取消');
        ttsController.stop();
        activeSessionId = id;
        App.switchTab('chat');
        connectLiveStream(id);
        renderAll();
        closeSidebarOnMobile();
    }

    async function handleSubmit(event) {
        event.preventDefault();
        if (voiceCapture) return;
        const text = els.input.value.trim();
        const attachmentSnapshot = pendingAttachments.slice();
        if (!text && attachmentSnapshot.length === 0) return;

        let session = getActiveSession();
        if (!session) {
            session = createSession();
            sessions.unshift(session);
            activeSessionId = session.id;
        }

        const requestMessageId = createId();
        const controller = new AbortController();
        const requestTimeout = window.setTimeout(() => controller.abort(), 70_000);
        let requestAccepted = false;

        try {
            const uploadedAttachments = [];
            for (const item of attachmentSnapshot) {
                uploadedAttachments.push(await uploadPendingAttachment(item));
            }

            addPendingRequest(session.id, requestMessageId);
            session.messages.push({
                id: requestMessageId,
                role: 'user',
                content: text,
                attachments: uploadedAttachments,
                createdAt: Date.now(),
            });
            if (session.title === '新对话') {
                const firstVoice = uploadedAttachments.some(item => item?.type === 'voice');
                const titleSource = text || (firstVoice
                    ? '语音消息'
                    : uploadedAttachments[0]?.name || mediaTypeLabel(uploadedAttachments[0]?.type));
                session.title = makeTitle(titleSource || '附件消息');
            }
            session.updatedAt = Date.now();

            els.input.value = '';
            clearPendingAttachments();
            autoResizeInput();
            saveSessions();
            renderAll();
            connectLiveStream(session.id);

            const response = await fetch(API_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                signal: controller.signal,
                body: JSON.stringify({
                    conversation_id: session.id,
                    message: text,
                    attachments: uploadedAttachments.map(item => ({
                        media_id: item.media_id,
                        type: item.type,
                        name: item.name,
                    })),
                    request_message_id: requestMessageId,
                    user_name: profile.getUserName() || DEFAULT_USER_NAME,
                }),
            });

            if (!response.ok) {
                const detail = await response.json().catch(() => null);
                const error = new Error(detail?.detail || `HTTP ${response.status}`);
                error.status = response.status;
                throw error;
            }

            const data = await response.json();
            if (data?.status !== 'accepted') {
                throw new Error('聊天后端未确认接收消息');
            }
            requestAccepted = true;

            if (typeof data.conversation_id === 'string' && data.conversation_id !== session.id) {
                session.remoteConversationId = data.conversation_id;
            }

            // POST 只确认用户消息已送入 Core；助手回复统一由当前会话的
            // WebSocket 接收，避免 HTTP 与实时通道重复投递或互相等待。
        } catch (error) {
            console.warn('Chat submit failed:', error);
            clearPendingRequest(session.id, requestMessageId);
            session.messages.push({
                id: createId(),
                role: 'notice',
                content: error.name === 'AbortError'
                    ? '消息发送请求超时，但聊天界面不会被锁定；请检查 WebUI 与 NachoBot Core 的连接状态。'
                    : error.status === 404
                        ? '聊天界面已经就绪，但 NachoBot 聊天后端尚未接入。'
                        : `消息提交失败：${error.message}`,
                createdAt: Date.now(),
            });
        } finally {
            window.clearTimeout(requestTimeout);
            session.updatedAt = Date.now();
            saveSessions();
            if (!requestAccepted) renderAll();
            els.input.focus();
        }
    }

    function connectLiveStream(conversationId) {
        if (!conversationId) return;
        if (liveSocket
            && liveConversationId === conversationId
            && (liveSocket.readyState === WebSocket.OPEN || liveSocket.readyState === WebSocket.CONNECTING)) {
            return;
        }

        if (liveReconnectTimer) {
            clearTimeout(liveReconnectTimer);
            liveReconnectTimer = null;
        }
        if (liveSocket) liveSocket.close();

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        liveConversationId = conversationId;
        liveSocket = createAuthenticatedWebSocket(
            `${protocol}//${location.host}/ws/chat/${encodeURIComponent(conversationId)}`
        );

        liveSocket.onmessage = event => {
            try {
                const data = JSON.parse(event.data);
                if (callController?.handleVoiceEvent(data) || data?.channel === 'voice') return;
                const isChatMessage = data?.type === 'message' || data?.type === 'proactive_message';
                if (!isChatMessage || data.conversation_id !== liveConversationId) return;
                const session = sessions.find(item => item.id === data.conversation_id);
                if (!session) return;

                const replyToMessageId = typeof data.reply_to_message_id === 'string'
                    ? data.reply_to_message_id
                    : '';
                if (replyToMessageId) {
                    clearPendingRequest(session.id, replyToMessageId);
                }

                const appended = appendAssistantMessage(session, data);
                if (appended) saveSessions();
                if (session.id === activeSessionId) renderAll();
            } catch (error) {
                console.warn('Invalid live chat message:', error);
            }
        };

        liveSocket.onclose = () => {
            if (!initialized || liveConversationId !== conversationId) return;
            liveReconnectTimer = window.setTimeout(() => {
                liveReconnectTimer = null;
                connectLiveStream(conversationId);
            }, 1500);
        };
    }

    function appendAssistantMessage(session, event) {
        const content = typeof event?.message?.content === 'string' ? event.message.content : '';
        const attachments = Array.isArray(event?.message?.attachments)
            ? event.message.attachments.filter(item => item && typeof item === 'object')
            : [];
        if (!content.trim() && attachments.length === 0) return false;

        const backendMessageId = typeof event.message_id === 'string' ? event.message_id : '';
        if (backendMessageId && session.messages.some(message => message.backendMessageId === backendMessageId)) {
            return false;
        }

        const replyToMessageId = typeof event.reply_to_message_id === 'string'
            ? event.reply_to_message_id
            : '';
        const assistantMessage = {
            id: createId(),
            backendMessageId,
            replyToMessageId,
            role: event?.message?.role || 'assistant',
            content,
            attachments,
            createdAt: Date.now(),
        };

        // 多段回复可能在用户已经开始下一轮后才到达。将其插回触发它的
        // 用户消息之后，并排在同轮已有回复之后，避免视觉上串到下一轮。
        const anchorIndex = replyToMessageId
            ? session.messages.findIndex(message => message.id === replyToMessageId && message.role === 'user')
            : -1;
        if (anchorIndex >= 0) {
            let insertIndex = anchorIndex + 1;
            while (
                insertIndex < session.messages.length
                && session.messages[insertIndex].role !== 'user'
                && session.messages[insertIndex].replyToMessageId === replyToMessageId
            ) {
                insertIndex += 1;
            }
            session.messages.splice(insertIndex, 0, assistantMessage);
        } else {
            session.messages.push(assistantMessage);
        }

        session.updatedAt = Date.now();
        return true;
    }

    function addPendingRequest(conversationId, requestMessageId) {
        if (!conversationId || !requestMessageId) return;
        let ids = pendingRequestIds.get(conversationId);
        if (!ids) {
            ids = new Set();
            pendingRequestIds.set(conversationId, ids);
        }
        ids.add(requestMessageId);
    }

    function clearPendingRequest(conversationId, requestMessageId) {
        const ids = pendingRequestIds.get(conversationId);
        if (!ids) return;
        ids.delete(requestMessageId);
        if (ids.size === 0) pendingRequestIds.delete(conversationId);
    }

    function updateSendState() {
        // Composer 不再由模型回复状态控制。即使旧代码曾把 textarea
        // 置为 disabled，也在这里持续纠正，避免 Enter 和输入事件失效。
        if (els.input) els.input.disabled = false;
        if (!els.send) return;
        els.send.disabled = Boolean(voiceCapture) || (!els.input.value.trim() && pendingAttachments.length === 0);
        els.send.classList.remove('is-busy');
    }

    function autoResizeInput() {
        els.input.style.height = 'auto';
        els.input.style.height = `${Math.min(els.input.scrollHeight, 180)}px`;
    }

    function renderAll() {
        renderHistory();
        renderMessages();
        updateSendState();
    }

    function renderHistory() {
        const active = getActiveSession();
        if (els.title) els.title.textContent = active?.title || '新对话';
        if (!els.historyList) return;

        const filteredSessions = historyQuery
            ? sessions.filter(session => {
                const searchable = [
                    session.title,
                    ...session.messages.map(message => message.content || ''),
                ].join('\n').toLocaleLowerCase('zh-CN');
                return searchable.includes(historyQuery);
            })
            : sessions;

        if (els.historyCount) els.historyCount.textContent = String(filteredSessions.length);
        if (els.historyEmpty) {
            els.historyEmpty.textContent = historyQuery ? '没有匹配的历史对话' : '还没有历史对话';
            els.historyEmpty.classList.toggle('visible', filteredSessions.length === 0);
        }

        els.historyList.innerHTML = '';
        filteredSessions.forEach(session => {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = `chat-history-item${session.id === activeSessionId ? ' active' : ''}`;
            item.innerHTML = `
                <span class="chat-history-item-main">
                    <span class="chat-history-title">${escapeText(session.title)}</span>
                    <span class="chat-history-time">${formatTime(session.updatedAt)}</span>
                </span>
                <span class="chat-history-delete" role="button" aria-label="删除对话" title="删除对话">×</span>
            `;
            item.addEventListener('click', () => switchSession(session.id));
            item.querySelector('.chat-history-delete').addEventListener('click', event => {
                event.stopPropagation();
                deleteSession(session.id);
            });
            els.historyList.appendChild(item);
        });
    }

    function renderMessages() {
        const session = getActiveSession();
        const messages = session?.messages || [];
        els.empty?.classList.toggle('hidden', messages.length > 0);

        disposeVoicePlayers(els.messages);
        els.messages.querySelectorAll('.chat-message, .chat-thinking').forEach(node => node.remove());
        messages.forEach(message => els.messages.appendChild(createMessageElement(message)));
        initializeVoicePlayers(els.messages);

        const pendingIds = session ? pendingRequestIds.get(session.id) : null;
        if (pendingIds) {
            pendingIds.forEach(requestMessageId => renderThinking(requestMessageId));
        }

        ttsController.syncButtons();
        scrollToBottom();
    }

    function safeMediaUrl(value) {
        const url = typeof value === 'string' ? value.trim() : '';
        if (url.startsWith('/api/chat/media/')) return url;
        if (url.startsWith('https://') || url.startsWith('http://')) return url;
        return '';
    }

    function renderMessageAttachments(attachments) {
        if (!Array.isArray(attachments) || attachments.length === 0) return '';
        const items = attachments.map(item => {
            if (!item || typeof item !== 'object') return '';
            const type = typeof item.type === 'string' ? item.type : '';
            const url = safeMediaUrl(item.url);
            if (!url || !MEDIA_LIMITS[type]) return '';
            if (type === 'voice') return '';
            const safeUrl = escapeText(url);
            const name = escapeText(item.name || mediaTypeLabel(type));
            const size = item.size ? formatBytes(item.size) : '';

            if (type === 'image' || type === 'emoji') {
                return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" aria-label="打开${mediaTypeLabel(type)}">
                    <img class="${type === 'emoji' ? 'chat-media-emoji' : 'chat-media-image'}"
                        src="${safeUrl}" alt="${name}" loading="lazy">
                </a>`;
            }
            if (type === 'video') {
                return `<video class="chat-media-video" controls preload="metadata" src="${safeUrl}"></video>`;
            }
            return `<a class="chat-media-file" href="${safeUrl}" target="_blank" rel="noopener noreferrer">
                <span class="chat-media-file-icon">FILE</span>
                <span class="chat-media-file-copy">
                    <strong>${name}</strong>
                    <small>${size || '打开文件'}</small>
                </span>
            </a>`;
        }).filter(Boolean);
        return items.length ? `<div class="chat-message-media">${items.join('')}</div>` : '';
    }

    function renderVoiceAttachments(attachments, isUser) {
        if (!Array.isArray(attachments)) return '';
        const label = isUser ? '用户语音消息' : '助手语音消息';
        return attachments.map(item => {
            if (!item || typeof item !== 'object' || item.type !== 'voice') return '';
            const url = safeMediaUrl(item.url);
            if (!url) return '';
            return renderVoicePlayer(url, {
                playerClass: 'chat-voice-message-bubble',
                audioClass: 'chat-voice-message-audio',
                label,
            });
        }).filter(Boolean).join('');
    }

    function renderVoicePlayer(source, {playerClass, audioClass, label, removeId = ''}) {
        const safeLabel = escapeText(label || '语音消息');
        const removeMarkup = removeId
            ? `<button type="button" class="chat-voice-remove-button chat-pending-attachment-remove"
                data-remove-attachment-id="${escapeText(removeId)}" aria-label="移除语音消息" title="移除语音消息">×</button>`
            : '';
        return `<div class="chat-voice-player ${escapeText(playerClass)}" data-voice-player>
            <audio class="chat-voice-player-audio ${escapeText(audioClass)}" preload="metadata"
                src="${escapeText(source)}" aria-hidden="true" tabindex="-1"></audio>
            <button type="button" class="chat-voice-play-button" data-voice-play
                data-voice-label="${safeLabel}" aria-label="播放${safeLabel}" title="播放${safeLabel}">
                <svg class="chat-voice-play-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">
                    <path d="M8.5 6.2c0-.9.98-1.45 1.75-.98l8.6 5.3c.73.45.73 1.51 0 1.96l-8.6 5.3c-.77.47-1.75-.08-1.75-.98V6.2z"></path>
                </svg>
                <svg class="chat-voice-pause-icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">
                    <rect x="6.5" y="5.5" width="3.8" height="13" rx="1.4"></rect>
                    <rect x="13.7" y="5.5" width="3.8" height="13" rx="1.4"></rect>
                </svg>
            </button>
            <div class="chat-voice-player-main">
                <input class="chat-voice-seek" data-voice-seek type="range" min="0" max="1000"
                    step="10" value="0" disabled aria-label="${safeLabel}播放进度"
                    aria-valuetext="0:00 / --:--">
                <div class="chat-voice-player-times">
                    <span class="chat-voice-elapsed">
                        <span class="chat-voice-eq" aria-hidden="true"><i></i><i></i><i></i></span>
                        <span data-voice-current>0:00</span>
                    </span>
                    <span data-voice-duration>--:--</span>
                </div>
                <span class="chat-voice-player-error" data-voice-error role="status" aria-live="polite" hidden></span>
            </div>
            ${removeMarkup}
        </div>`;
    }

    function bindVoicePlayerEvents(container) {
        if (!container) return;
        container.addEventListener('click', event => {
            const button = event.target.closest('[data-voice-play]');
            if (!button || !container.contains(button)) return;
            event.preventDefault();
            toggleVoicePlayer(button);
        });
        container.addEventListener('input', event => {
            const range = event.target.closest('[data-voice-seek]');
            if (!range || !container.contains(range)) return;
            seekVoicePlayer(range);
        });
    }

    function initializeVoicePlayers(container) {
        if (!container) return;
        container.querySelectorAll('[data-voice-player]').forEach(player => {
            if (player.dataset.voiceInitialized === 'true') return;
            const audio = player.querySelector('.chat-voice-player-audio');
            if (!audio) return;
            player.dataset.voiceInitialized = 'true';
            for (const eventName of ['loadedmetadata', 'durationchange', 'timeupdate', 'play', 'pause', 'ended', 'emptied']) {
                audio.addEventListener(eventName, () => updateVoicePlayer(player));
            }
            audio.addEventListener('error', () => {
                const error = player.querySelector('[data-voice-error]');
                if (error) {
                    error.textContent = '无法读取此语音';
                    error.hidden = false;
                }
                player.dataset.voiceFailed = 'true';
                updateVoicePlayer(player);
            });
            updateVoicePlayer(player);
        });
    }

    async function toggleVoicePlayer(button) {
        const player = button.closest('[data-voice-player]');
        const audio = player?.querySelector('.chat-voice-player-audio');
        if (!player || !audio || player.dataset.voiceFailed === 'true') return;
        if (!audio.paused && !audio.ended) {
            audio.pause();
            return;
        }
        pauseVoicePlayers(document, audio);
        if (audio.ended) audio.currentTime = 0;
        const error = player.querySelector('[data-voice-error]');
        if (error) {
            error.textContent = '';
            error.hidden = true;
        }
        try {
            await audio.play();
        } catch (playError) {
            if (playError?.name === 'AbortError' || !player.isConnected) return;
            if (error) {
                error.textContent = '暂时无法播放，请重试';
                error.hidden = false;
                if (player.voiceErrorTimer) window.clearTimeout(player.voiceErrorTimer);
                player.voiceErrorTimer = window.setTimeout(() => {
                    if (!player.isConnected || player.dataset.voiceFailed === 'true') return;
                    error.textContent = '';
                    error.hidden = true;
                }, 4000);
            }
            updateVoicePlayer(player);
        }
    }

    function seekVoicePlayer(range) {
        const player = range.closest('[data-voice-player]');
        const audio = player?.querySelector('.chat-voice-player-audio');
        const duration = Number(audio?.duration);
        if (!audio || !Number.isFinite(duration) || duration <= 0) return;
        const position = Math.min(1, Math.max(0, Number(range.value) / 1000));
        const targetTime = position * duration;
        audio.currentTime = targetTime;
        range.style.setProperty('--voice-progress', `${position * 100}%`);
        range.setAttribute('aria-valuetext', `${formatVoiceTime(targetTime)} / ${formatVoiceTime(duration)}`);
        const currentLabel = player?.querySelector('[data-voice-current]');
        if (currentLabel) currentLabel.textContent = formatVoiceTime(targetTime);
    }

    function updateVoicePlayer(player) {
        const audio = player?.querySelector('.chat-voice-player-audio');
        const playButton = player?.querySelector('[data-voice-play]');
        const range = player?.querySelector('[data-voice-seek]');
        const currentLabel = player?.querySelector('[data-voice-current]');
        const durationLabel = player?.querySelector('[data-voice-duration]');
        if (!audio || !playButton || !range) return;

        const duration = Number(audio.duration);
        const hasDuration = Number.isFinite(duration) && duration > 0;
        const current = Number.isFinite(audio.currentTime) ? Math.max(0, audio.currentTime) : 0;
        const progress = hasDuration ? Math.min(1, current / duration) : 0;
        const playing = !audio.paused && !audio.ended;
        range.disabled = !hasDuration || player.dataset.voiceFailed === 'true';
        range.value = String(Math.round(progress * 1000));
        range.style.setProperty('--voice-progress', `${progress * 100}%`);
        range.setAttribute('aria-valuetext', `${formatVoiceTime(current)} / ${hasDuration ? formatVoiceTime(duration) : '--:--'}`);
        if (currentLabel) currentLabel.textContent = formatVoiceTime(current);
        if (durationLabel) durationLabel.textContent = hasDuration ? formatVoiceTime(duration) : '--:--';
        playButton.disabled = player.dataset.voiceFailed === 'true';
        playButton.setAttribute('aria-label', `${playing ? '暂停' : '播放'}${playButton.dataset.voiceLabel || '语音消息'}`);
        playButton.title = playButton.getAttribute('aria-label');
        player.classList.toggle('is-playing', playing);
    }

    function formatVoiceTime(value) {
        const number = Number(value);
        if (!Number.isFinite(number) || number < 0) return '0:00';
        const totalSeconds = Math.floor(number);
        return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, '0')}`;
    }

    function pauseVoicePlayers(container, exceptAudio = null) {
        if (!container?.querySelectorAll) return;
        container.querySelectorAll('.chat-voice-player-audio').forEach(audio => {
            if (audio === exceptAudio || audio.paused) return;
            audio.pause();
        });
    }

    function disposeVoicePlayers(container) {
        if (!container?.querySelectorAll) return;
        container.querySelectorAll('.chat-voice-player-audio').forEach(audio => {
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
        });
    }

    function createMessageElement(message) {
        const article = document.createElement('article');
        article.className = `chat-message chat-message-${message.role}`;

        if (message.role === 'notice') {
            article.innerHTML = `
                <div class="chat-notice-icon">i</div>
                <div class="chat-notice-content">${formatContent(message.content)}</div>
            `;
            return article;
        }

        const isUser = message.role === 'user';
        const content = typeof message.content === 'string' ? message.content : '';
        const contentMarkup = content.trim() ? formatContent(content) : '';
        const mediaMarkup = renderMessageAttachments(message.attachments);
        const voiceMarkup = renderVoiceAttachments(message.attachments, isUser);
        const speakerMarkup = !isUser && content.trim()
            ? ttsController.createSpeakerMarkup(message)
            : '';

        article.innerHTML = `
            <div class="chat-message-inner">
                ${isUser ? '' : profile.createBotAvatarMarkup()}
                ${isUser ? `
                    <div class="chat-message-body chat-message-user-body">
                        ${contentMarkup || mediaMarkup
                            ? `<div class="chat-message-content">${contentMarkup}${mediaMarkup}</div>`
                            : ''}
                        ${voiceMarkup}
                    </div>
                ` : `
                    <div class="chat-message-body">
                        ${contentMarkup || speakerMarkup || mediaMarkup
                            ? `<div class="chat-message-content">${contentMarkup}${speakerMarkup}${mediaMarkup}</div>`
                            : ''}
                        ${voiceMarkup}
                    </div>
                `}
            </div>
        `;
        return article;
    }

    function renderThinking(requestMessageId = '') {
        const thinking = document.createElement('div');
        thinking.className = 'chat-thinking';
        if (requestMessageId) thinking.dataset.requestMessageId = requestMessageId;
        thinking.innerHTML = `
            ${profile.createBotAvatarMarkup()}
            <div class="chat-thinking-dots"><span></span><span></span><span></span></div>
        `;
        els.messages.appendChild(thinking);
        scrollToBottom();
    }

    function scrollToBottom() {
        requestAnimationFrame(() => {
            els.messages.scrollTop = els.messages.scrollHeight;
        });
    }

    function restoreSidebarState() {
        const collapsed = localStorage.getItem(SIDEBAR_STATE_KEY) === 'true';
        setSidebarCollapsed(collapsed);
    }

    function setSidebarCollapsed(collapsed) {
        document.body.classList.toggle('sidebar-collapsed', collapsed);
        localStorage.setItem(SIDEBAR_STATE_KEY, String(collapsed));

        if (els.sidebarCollapse) {
            els.sidebarCollapse.textContent = '◀';
            els.sidebarCollapse.title = '隐藏侧栏';
            els.sidebarCollapse.setAttribute('aria-label', '隐藏侧栏');
        }

        if (els.sidebarBrand) {
            els.sidebarBrand.title = collapsed ? '展开侧栏' : '';
            els.sidebarBrand.setAttribute('aria-label', collapsed ? '展开侧栏' : 'NachoBot');
        }
    }

    function closeSidebarOnMobile() {
        document.body.classList.remove('sidebar-mobile-open');
    }

    function chooseLaunchProfile() {
        if (modalOpen) {
            toast('请先关闭当前弹窗', 'error');
            return Promise.resolve(null);
        }

        const overlay = document.getElementById('modal-overlay');
        const card = document.getElementById('modal-card');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');
        const footer = document.getElementById('modal-footer');
        const closeButton = document.getElementById('modal-close');

        if (!overlay || !card || !title || !body || !footer || !closeButton) {
            const raw = window.prompt('选择启动模式：FULL / LITE / POTATO', 'LITE');
            const profileId = String(raw || '').trim().toLowerCase();
            return Promise.resolve(CHAT_LAUNCH_PROFILES[profileId] ? profileId : null);
        }

        return new Promise(resolve => {
            modalOpen = true;
            card.classList.add('chat-launch-modal');
            title.textContent = '选择 NachoBot 运行模式';
            body.innerHTML = `
                <div class="chat-launch-profile-intro">Core 将与以下一种运行模式绑定启动，三种模式互斥。</div>
                <div class="chat-launch-profile-grid">
                    ${Object.entries(CHAT_LAUNCH_PROFILES).map(([id, item]) => `
                        <button type="button" class="chat-launch-profile" data-chat-launch-profile="${id}">
                            <span class="chat-launch-profile-main">
                                <strong>${item.name}</strong>
                                <span class="chat-launch-profile-code">${item.code}</span>
                            </span>
                            <span class="chat-launch-profile-summary">${item.summary}</span>
                            <span class="chat-launch-profile-detail">${item.detail}</span>
                        </button>
                    `).join('')}
                </div>
            `;
            footer.innerHTML = '';

            let settled = false;
            const finish = result => {
                if (settled) return;
                settled = true;
                overlay.classList.add('hidden');
                card.classList.remove('chat-launch-modal');
                closeButton.style.removeProperty('display');
                closeButton.onclick = null;
                overlay.onclick = null;
                modalOpen = false;
                resolve(result);
            };

            const cancelButton = document.createElement('button');
            cancelButton.type = 'button';
            cancelButton.className = 'btn btn-ghost';
            cancelButton.textContent = '取消';
            cancelButton.addEventListener('click', () => finish(null));
            footer.appendChild(cancelButton);

            body.querySelectorAll('[data-chat-launch-profile]').forEach(button => {
                button.addEventListener('click', () => finish(button.dataset.chatLaunchProfile));
            });

            closeButton.style.removeProperty('display');
            closeButton.onclick = () => finish(null);
            overlay.onclick = event => {
                if (event.target === overlay) finish(null);
            };
            overlay.classList.remove('hidden');
        });
    }

    async function toggleCoreService() {
        if (!els.status || coreToggleBusy) return;

        let currentLaunchStatus = 'stopped';
        let currentLaunch = null;
        try {
            currentLaunch = await apiGet('/api/launch');
            currentLaunchStatus = String(currentLaunch.status || 'stopped');
        } catch (error) {
            console.warn('Failed to read NachoBot launch state before toggle:', error);
        }

        if (
            currentLaunch
            && launchExternalProfileIsReady(currentLaunch)
            && !launchHasOwnedDependents(currentLaunch)
        ) {
            toast(EXTERNAL_LAUNCHER_NOTICE, 'info');
            await updateBackendStatus(true);
            return;
        }

        if (currentLaunchStatus === 'stopping') {
            toast('NachoBot 正在停止，请稍候', 'info');
            return;
        }

        const externalCoreWithoutOwnedDependents = Boolean(
            currentLaunch
            && launchCoreIsExternal(currentLaunch)
            && !launchHasOwnedDependents(currentLaunch),
        );
        const shouldStart = externalCoreWithoutOwnedDependents
            ? currentLaunchStatus !== 'running'
            : currentLaunchStatus === 'stopped' || currentLaunchStatus === 'error';
        let selectedProfile = null;
        if (shouldStart) {
            selectedProfile = await chooseLaunchProfile();
            if (!selectedProfile) return;
        }

        coreToggleBusy = true;
        coreStatusRequestSerial += 1;
        els.status.disabled = true;
        els.status.className = 'chat-status-chip is-checking';
        els.status.textContent = shouldStart
            ? `${CHAT_LAUNCH_PROFILES[selectedProfile].code} 启动中`
            : 'NachoBot 关闭中';
        els.status.title = shouldStart
            ? `正在以 ${CHAT_LAUNCH_PROFILES[selectedProfile].code} 模式启动 NachoBot`
            : '正在关闭 NachoBot Core 与当前运行模式';

        try {
            if (shouldStart) {
                await apiPost('/api/launch/start', { profile: selectedProfile });
            } else {
                await apiPost('/api/launch/stop');
            }
            ttsController?.updateStatus(true);

            let reachedTarget = false;
            for (let attempt = 0; attempt < 360; attempt += 1) {
                await new Promise(resolve => window.setTimeout(resolve, 500));
                const data = await apiGet('/api/launch');
                const launchStatus = String(data.status || '');
                const activeProfile = String(data.active_profile || selectedProfile || '');
                coreRunning = data.core?.status === 'running';

                if (shouldStart && launchStatus === 'error') {
                    throw new Error('NachoBot 启动失败，请查看终端日志');
                }

                if (shouldStart && launchStatus !== 'running') {
                    const code = CHAT_LAUNCH_PROFILES[activeProfile]?.code || CHAT_LAUNCH_PROFILES[selectedProfile].code;
                    els.status.className = 'chat-status-chip is-checking';
                    els.status.textContent = `${code} 启动中`;
                } else if (!shouldStart && launchStatus !== 'stopped') {
                    els.status.className = 'chat-status-chip is-checking';
                    els.status.textContent = 'NachoBot 关闭中';
                }

                const startCompleted = shouldStart && launchStatus === 'running';
                const stopCompleted = !shouldStart && launchStatus === 'stopped';
                if (startCompleted || stopCompleted) {
                    if (startCompleted) await ttsController?.updateStatus(true);
                    reachedTarget = true;
                    break;
                }
            }

            if (!reachedTarget) {
                throw new Error(shouldStart ? 'NachoBot 启动超时，请查看终端日志' : 'NachoBot 关闭超时，请查看终端日志');
            }

            toast(
                shouldStart
                    ? `NachoBot 已以 ${CHAT_LAUNCH_PROFILES[selectedProfile].code} 模式启动`
                    : 'NachoBot 已关闭',
                'success',
            );
            App.pollStatus();
        } catch (error) {
            console.warn('Failed to toggle NachoBot launch:', error);
            toast(`NachoBot 操作失败：${error.message}`, 'error');
        } finally {
            coreToggleBusy = false;
            els.status.disabled = false;
            await Promise.all([
                updateBackendStatus(true),
                ttsController?.updateStatus(true),
            ]);
        }
    }

    async function updateBackendStatus(forceRefresh = false) {
        if (!els.status || coreToggleBusy) return;
        if (coreStatusRequestPromise) {
            if (forceRefresh) coreStatusRefreshPending = true;
            return coreStatusRequestPromise;
        }
        const requestSerial = coreStatusRequestSerial;
        coreStatusRefreshPending = false;
        const request = refreshBackendStatus(requestSerial);
        coreStatusRequestPromise = request;
        try {
            return await request;
        } finally {
            if (coreStatusRequestPromise === request) coreStatusRequestPromise = null;
            if (coreStatusRefreshPending && !coreToggleBusy) {
                coreStatusRefreshPending = false;
                window.setTimeout(updateBackendStatus, 0);
            }
        }
    }

    async function refreshBackendStatus(requestSerial) {

        try {
            const data = await apiGet('/api/launch');
            if (coreToggleBusy || requestSerial !== coreStatusRequestSerial) return;

            const launchStatus = String(data.status || 'stopped');
            const activeProfile = String(data.active_profile || '');
            const profileCode = CHAT_LAUNCH_PROFILES[activeProfile]?.code || '';
            const externalCore = launchCoreIsExternal(data);
            const ownedDependents = launchHasOwnedDependents(data);
            const externalProfileReady = launchExternalProfileIsReady(data);
            coreRunning = data.core?.status === 'running';
            els.status.setAttribute('aria-pressed', String(launchStatus === 'running'));

            if (externalCore && externalProfileReady && !ownedDependents) {
                els.status.className = 'chat-status-chip is-online';
                els.status.textContent = profileCode
                    ? `NachoBot · ${profileCode}（外部）`
                    : 'NachoBot 外部运行中';
                els.status.title = EXTERNAL_LAUNCHER_NOTICE;
                els.status.setAttribute('aria-disabled', 'true');
            } else if (launchStatus === 'starting') {
                els.status.className = 'chat-status-chip is-checking';
                els.status.textContent = `${profileCode || 'NachoBot'} 启动中`;
                els.status.title = '点击可取消当前启动';
                els.status.removeAttribute('aria-disabled');
            } else if (launchStatus === 'stopping') {
                els.status.className = 'chat-status-chip is-checking';
                els.status.textContent = 'NachoBot 关闭中';
                els.status.title = '正在关闭 Core 与当前运行模式';
                els.status.removeAttribute('aria-disabled');
            } else if (launchStatus === 'running') {
                els.status.className = 'chat-status-chip is-online';
                els.status.textContent = profileCode ? `NachoBot · ${profileCode}` : 'NachoBot 运行中';
                els.status.title = '点击停止 NachoBot Core 与当前运行模式';
                els.status.removeAttribute('aria-disabled');
            } else if (launchStatus === 'error') {
                els.status.className = 'chat-status-chip is-offline';
                els.status.textContent = profileCode ? `${profileCode} 启动失败` : 'NachoBot 启动失败';
                els.status.title = '点击重新选择运行模式并启动';
                els.status.removeAttribute('aria-disabled');
            } else if (launchStatus === 'partial') {
                els.status.className = 'chat-status-chip is-checking';
                els.status.textContent = profileCode ? `NachoBot · ${profileCode} 部分运行` : 'NachoBot 部分运行';
                els.status.title = externalCore && !ownedDependents
                    ? '点击启动 WebUI 托管服务'
                    : '点击停止当前启动单元';
                els.status.removeAttribute('aria-disabled');
            } else {
                coreRunning = false;
                els.status.className = 'chat-status-chip is-offline';
                els.status.textContent = 'NachoBot 未运行';
                els.status.title = '点击选择 FULL / LITE / POTATO 并启动';
                els.status.removeAttribute('aria-disabled');
            }
        } catch (error) {
            if (coreToggleBusy || requestSerial !== coreStatusRequestSerial) return;
            coreRunning = false;
            els.status.setAttribute('aria-pressed', 'false');
            els.status.removeAttribute('aria-disabled');
            els.status.className = 'chat-status-chip is-offline';
            els.status.textContent = '无法读取服务状态';
            els.status.title = '点击尝试启动 NachoBot';
        } finally {
            if (!coreToggleBusy && requestSerial === coreStatusRequestSerial) {
                els.status.disabled = false;
            }
        }
    }

    return {
        init,
        refresh,
        __test: {
            launchCoreIsExternal,
            launchHasOwnedDependents,
            launchExternalProfileIsReady,
            externalLauncherNotice: EXTERNAL_LAUNCHER_NOTICE,
            toggleCoreService,
            updateBackendStatus,
            setStatusElement(element) { els.status = element; },
        },
    };
})();
