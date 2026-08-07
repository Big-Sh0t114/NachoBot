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
    const DELETE_CONVERSATION_ENDPOINT = '/api/chat/conversations';
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
    let ttsStatusTimer = null;
    let profile = null;
    let ttsController = null;
    let modalOpen = false;
    let coreRunning = false;
    let coreToggleBusy = false;
    let coreStatusTimer = null;
    let coreStatusRequestSerial = 0;
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
            form: document.getElementById('chat-composer-form'),
            input: document.getElementById('chat-input'),
            send: document.getElementById('chat-send-button'),
            status: document.getElementById('chat-backend-status'),
            title: document.getElementById('chat-current-title'),
            mobileHistory: document.getElementById('chat-mobile-history-button'),
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
        restoreSidebarState();
        setRandomWelcomeSubtitle(els.welcomeSubtitle, WELCOME_SUBTITLES);
        bindEvents();
        renderAll();
        connectLiveStream(activeSessionId);
        updateBackendStatus();
        ttsController.updateStatus();
        coreStatusTimer = window.setInterval(updateBackendStatus, 2_000);
        ttsStatusTimer = window.setInterval(() => ttsController.updateStatus(), 15_000);
        initialized = true;
        profile.scheduleFirstUseNamePrompt();
    }

    function refresh() {
        if (!initialized) init();
        renderAll();
        updateBackendStatus();
        ttsController.updateStatus();
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

    function startNewChat() {
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
        if (!session || session.messages.length === 0) return;
        if (!window.confirm('清空当前对话中的全部消息？')) return;

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

        const confirmed = await confirmDeleteSession(session);
        if (!confirmed) return;

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

    function switchSession(id) {
        if (!sessions.some(session => session.id === id)) return;
        ttsController.stop();
        activeSessionId = id;
        App.switchTab('chat');
        connectLiveStream(id);
        renderAll();
        closeSidebarOnMobile();
    }

    async function handleSubmit(event) {
        event.preventDefault();
        const text = els.input.value.trim();
        if (!text) return;

        let session = getActiveSession();
        if (!session) {
            session = createSession();
            sessions.unshift(session);
            activeSessionId = session.id;
        }

        const requestMessageId = createId();
        addPendingRequest(session.id, requestMessageId);
        session.messages.push({
            id: requestMessageId,
            role: 'user',
            content: text,
            createdAt: Date.now(),
        });
        if (session.title === '新对话') session.title = makeTitle(text);
        session.updatedAt = Date.now();

        els.input.value = '';
        autoResizeInput();

        const controller = new AbortController();
        const requestTimeout = window.setTimeout(() => controller.abort(), 10_000);
        let requestAccepted = false;
        try {
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
                        ? '聊天界面已经就绪，但 NachoBot 聊天后端尚未接入。后端实现 POST /api/chat/message 后即可返回真实回复。'
                        : `消息提交失败：${error.message}`,
                createdAt: Date.now(),
            });
        } finally {
            window.clearTimeout(requestTimeout);
            session.updatedAt = Date.now();
            saveSessions();
            // accepted 后保留当前 DOM 中的 thinking 动画；首条 WebSocket 回复到达时
            // onmessage -> renderAll() 会自然移除它。发送失败时则立即重绘并清除。
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
        liveSocket = new WebSocket(
            `${protocol}//${location.host}/ws/chat/${encodeURIComponent(conversationId)}`
        );

        liveSocket.onmessage = event => {
            try {
                const data = JSON.parse(event.data);
                if (data?.type !== 'message' || data.conversation_id !== liveConversationId) return;
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
        const content = event?.message?.content;
        if (typeof content !== 'string' || !content.trim()) return false;

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
        els.send.disabled = !els.input.value.trim();
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

        els.messages.querySelectorAll('.chat-message, .chat-thinking').forEach(node => node.remove());
        messages.forEach(message => els.messages.appendChild(createMessageElement(message)));

        const pendingIds = session ? pendingRequestIds.get(session.id) : null;
        if (pendingIds) {
            pendingIds.forEach(requestMessageId => renderThinking(requestMessageId));
        }

        ttsController.syncButtons();
        scrollToBottom();
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
        article.innerHTML = `
            <div class="chat-message-inner">
                ${isUser ? '' : profile.createBotAvatarMarkup()}
                ${isUser ? `
                    <div class="chat-message-content">${formatContent(message.content)}</div>
                ` : `
                    <div class="chat-message-body">
                        <div class="chat-message-content">
                            ${formatContent(message.content)}${ttsController.createSpeakerMarkup(message)}
                        </div>
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

    async function toggleCoreService() {
        if (!els.status || coreToggleBusy) return;

        const shouldStart = !coreRunning;
        coreToggleBusy = true;
        coreStatusRequestSerial += 1;
        els.status.disabled = true;
        els.status.className = 'chat-status-chip is-checking';
        els.status.textContent = shouldStart ? '核心启动中' : '核心关闭中';
        els.status.title = shouldStart ? '正在启动 NachoBot Core' : '正在关闭 NachoBot Core';

        try {
            await apiPost(`/api/services/nachobot/${shouldStart ? 'start' : 'stop'}`);

            let reachedTarget = false;
            for (let attempt = 0; attempt < 360; attempt += 1) {
                await new Promise(resolve => window.setTimeout(resolve, 500));
                try {
                    const data = await apiGet('/api/chat/status');
                    const coreStatus = String(data.core_status || '');
                    coreRunning = coreStatus
                        ? coreStatus === 'running'
                        : Boolean(data.core_running);

                    if (shouldStart && coreStatus === 'error') {
                        throw new Error('NachoBot Core 启动失败，请查看终端日志');
                    }

                    if (shouldStart && !coreRunning) {
                        els.status.className = 'chat-status-chip is-checking';
                        els.status.textContent = '核心启动中';
                        els.status.title = 'NachoBot Core 正在启动并等待端口就绪';
                    } else if (!shouldStart && coreStatus !== 'stopped') {
                        els.status.className = 'chat-status-chip is-checking';
                        els.status.textContent = '核心关闭中';
                        els.status.title = 'NachoBot Core 正在关闭';
                    }

                    const startCompleted = shouldStart && coreRunning;
                    const stopCompleted = !shouldStart
                        && (coreStatus === 'stopped' || coreStatus === 'error' || (!coreStatus && !coreRunning));
                    if (startCompleted || stopCompleted) {
                        reachedTarget = true;
                        break;
                    }
                } catch (error) {
                    if (error.message?.includes('启动失败')) throw error;
                    if (!shouldStart) {
                        coreRunning = false;
                        reachedTarget = true;
                        break;
                    }
                }
            }

            if (!reachedTarget) {
                throw new Error(shouldStart ? '核心服务启动超时，请查看终端日志' : '核心服务关闭超时，请查看终端日志');
            }

            toast(shouldStart ? 'NachoBot Core 已启动' : 'NachoBot Core 已关闭', 'success');
            App.pollStatus();
        } catch (error) {
            console.warn('Failed to toggle NachoBot Core:', error);
            toast(`核心服务操作失败：${error.message}`, 'error');
        } finally {
            coreToggleBusy = false;
            els.status.disabled = false;
            await updateBackendStatus();
        }
    }

    async function updateBackendStatus() {
        if (!els.status || coreToggleBusy) return;
        const requestSerial = ++coreStatusRequestSerial;

        try {
            const data = await apiGet('/api/chat/status');
            if (coreToggleBusy || requestSerial !== coreStatusRequestSerial) return;
            const coreStatus = String(data.core_status || '');
            coreRunning = coreStatus
                ? coreStatus === 'running'
                : Boolean(data.core_running);
            els.status.setAttribute('aria-pressed', String(coreRunning));

            if (coreStatus === 'starting') {
                els.status.className = 'chat-status-chip is-checking';
                els.status.textContent = '核心启动中';
                els.status.disabled = true;
            } else if (coreStatus === 'stopping') {
                els.status.className = 'chat-status-chip is-checking';
                els.status.textContent = '核心关闭中';
                els.status.disabled = true;
            } else if (coreRunning) {
                els.status.className = 'chat-status-chip is-online';
                els.status.textContent = '核心服务运行中';
            } else {
                els.status.className = 'chat-status-chip is-offline';
                els.status.textContent = coreStatus === 'error' ? '核心启动失败' : '核心服务未运行';
            }
        } catch (error) {
            if (coreToggleBusy || requestSerial !== coreStatusRequestSerial) return;
            coreRunning = false;
            els.status.setAttribute('aria-pressed', 'false');
            els.status.className = 'chat-status-chip is-offline';
            els.status.textContent = '无法读取服务状态';
            els.status.title = '点击尝试启动 NachoBot Core';
        } finally {
            if (
                !coreToggleBusy
                && requestSerial === coreStatusRequestSerial
                && !['核心启动中', '核心关闭中'].includes(els.status.textContent)
            ) {
                els.status.disabled = false;
            }
        }
    }

    return { init, refresh };
})();
