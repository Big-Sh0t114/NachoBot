/**
 * NachoBot WebUI — Chat Interface
 *
 * 当前负责聊天界面、会话本地持久化和未来聊天 API 的接入。
 * 真实回复接口约定：POST /api/chat/message
 * Request:  { conversation_id: string, message: string, user_name?: string }
 * Response: { conversation_id?: string, message?: { role?: string, content: string }, reply?: string }
 */
const ChatModule = (() => {
    const STORAGE_KEY = 'nachobot_chat_sessions_v1';
    const SIDEBAR_STATE_KEY = 'nachobot_sidebar_collapsed_v1';
    const USER_NAME_STORAGE_KEY = 'nachobot_chat_user_name_v1';
    const BOT_AVATAR_STORAGE_KEY = 'nachobot_chat_bot_avatar_v1';
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
    const WELCOME_EASTER_EGGS = [
        { type: 'editable', text: 'Tip：你可以修改这条Tip', chance: 0.01 },
        { type: 'gradient', text: 'OMEGAAAA TIPPSSSS!!!', chance: 0.01 },
        { type: 'evasive', text: '你跑不过我你信吗', chance: 0.01 },
    ];

    let initialized = false;
    let busy = false;
    let sessions = [];
    let activeSessionId = null;
    let historyQuery = '';
    let userName = '';
    let botAvatarDataUrl = '';
    let namePromptOpen = false;
    let liveSocket = null;
    let liveConversationId = null;
    let liveReconnectTimer = null;
    let coreRunning = false;
    let coreToggleBusy = false;
    let els = {};

    function createId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function createSession(title = '新对话') {
        return {
            id: createId(),
            title,
            createdAt: Date.now(),
            updatedAt: Date.now(),
            messages: [],
        };
    }

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

        loadSessions();
        loadUserName();
        loadBotAvatar();
        restoreSidebarState();
        setRandomWelcomeSubtitle();
        bindEvents();
        renderAll();
        connectLiveStream(activeSessionId);
        updateBackendStatus();
        initialized = true;
        scheduleFirstUseNamePrompt();
    }

    function refresh() {
        if (!initialized) init();
        renderAll();
        updateBackendStatus();
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

    function loadUserName() {
        try {
            userName = (localStorage.getItem(USER_NAME_STORAGE_KEY) || '').trim();
        } catch (error) {
            console.warn('Failed to load WebUI chat user name:', error);
            userName = '';
        }
    }

    function loadBotAvatar() {
        try {
            const savedAvatar = localStorage.getItem(BOT_AVATAR_STORAGE_KEY) || '';
            botAvatarDataUrl = savedAvatar.startsWith('data:image/') ? savedAvatar : '';
        } catch (error) {
            console.warn('Failed to load WebUI bot avatar:', error);
            botAvatarDataUrl = '';
        }
    }

    function scheduleFirstUseNamePrompt() {
        if (userName || namePromptOpen) return;

        const startupScreen = document.getElementById('startup-screen');
        if (!startupScreen || getComputedStyle(startupScreen).display === 'none') {
            requestAnimationFrame(showFirstUseNamePrompt);
            return;
        }

        const observer = new MutationObserver(() => {
            if (getComputedStyle(startupScreen).display !== 'none') return;
            observer.disconnect();
            showFirstUseNamePrompt();
        });
        observer.observe(startupScreen, { attributes: true, attributeFilter: ['style'] });
    }

    function showFirstUseNamePrompt() {
        if (userName || namePromptOpen) return;

        const overlay = document.getElementById('modal-overlay');
        const card = document.getElementById('modal-card');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');
        const footer = document.getElementById('modal-footer');
        const closeButton = document.getElementById('modal-close');
        if (!overlay || !card || !title || !body || !footer || !closeButton) return;

        namePromptOpen = true;
        card.classList.add('chat-name-modal');
        title.textContent = '你希望bot怎么称呼你';
        body.innerHTML = `
            <div class="chat-name-prompt">
                <p>取一个你希望bot在聊天中使用的称呼。</p>
                <input type="text" id="chat-user-name-input" class="chat-name-input"
                    maxlength="32" autocomplete="nickname" placeholder="输入你的名字或昵称">
                <p class="chat-name-error" id="chat-user-name-error" aria-live="polite"></p>
            </div>
        `;
        footer.innerHTML = '';

        const confirmButton = document.createElement('button');
        confirmButton.type = 'button';
        confirmButton.className = 'btn btn-primary';
        confirmButton.textContent = '确认';
        footer.appendChild(confirmButton);

        closeButton.style.display = 'none';
        closeButton.onclick = null;
        overlay.classList.remove('hidden');

        const input = document.getElementById('chat-user-name-input');
        const error = document.getElementById('chat-user-name-error');

        const saveName = () => {
            const value = input?.value.trim() || '';
            if (!value) {
                if (error) error.textContent = '请输入一个称呼';
                input?.focus();
                return;
            }

            userName = value;
            try {
                localStorage.setItem(USER_NAME_STORAGE_KEY, userName);
            } catch (storageError) {
                console.warn('Failed to save WebUI chat user name:', storageError);
            }
            overlay.classList.add('hidden');
            card.classList.remove('chat-name-modal');
            closeButton.style.removeProperty('display');
            namePromptOpen = false;
            toast(`之后我会称呼你为「${userName}」`, 'success');
            els.input?.focus();
        };

        confirmButton.addEventListener('click', saveName);
        input?.addEventListener('input', () => {
            if (error) error.textContent = '';
        });
        input?.addEventListener('keydown', event => {
            if (event.key === 'Enter' && !event.isComposing) {
                event.preventDefault();
                saveName();
            }
        });
        requestAnimationFrame(() => input?.focus());
    }

    function showRenamePrompt() {
        if (namePromptOpen) return;

        const overlay = document.getElementById('modal-overlay');
        const card = document.getElementById('modal-card');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');
        const footer = document.getElementById('modal-footer');
        const closeButton = document.getElementById('modal-close');
        if (!overlay || !card || !title || !body || !footer || !closeButton) return;

        const closePrompt = () => {
            overlay.classList.add('hidden');
            card.classList.remove('chat-name-modal');
            closeButton.style.removeProperty('display');
            closeButton.onclick = null;
            overlay.onclick = null;
            namePromptOpen = false;
            els.input?.focus();
        };

        namePromptOpen = true;
        card.classList.add('chat-name-modal');
        title.textContent = '修改称呼';
        body.innerHTML = `
            <div class="chat-name-prompt">
                <p>修改后，bot 会从下一条消息开始使用新的称呼。</p>
                <input type="text" id="chat-user-name-input" class="chat-name-input"
                    maxlength="32" autocomplete="nickname" placeholder="输入你的名字或昵称">
                <p class="chat-name-error" id="chat-user-name-error" aria-live="polite"></p>
            </div>
        `;
        footer.innerHTML = '';

        const cancelButton = document.createElement('button');
        cancelButton.type = 'button';
        cancelButton.className = 'btn btn-ghost';
        cancelButton.textContent = '取消';
        cancelButton.addEventListener('click', closePrompt);
        footer.appendChild(cancelButton);

        const saveButton = document.createElement('button');
        saveButton.type = 'button';
        saveButton.className = 'btn btn-primary';
        saveButton.textContent = '保存';
        footer.appendChild(saveButton);

        closeButton.style.removeProperty('display');
        closeButton.onclick = closePrompt;
        overlay.onclick = event => {
            if (event.target === overlay) closePrompt();
        };
        overlay.classList.remove('hidden');

        const input = document.getElementById('chat-user-name-input');
        const error = document.getElementById('chat-user-name-error');
        if (input) input.value = userName;

        const saveName = () => {
            const value = input?.value.trim() || '';
            if (!value) {
                if (error) error.textContent = '请输入一个称呼';
                input?.focus();
                return;
            }

            userName = value;
            try {
                localStorage.setItem(USER_NAME_STORAGE_KEY, userName);
            } catch (storageError) {
                console.warn('Failed to save WebUI chat user name:', storageError);
            }

            closePrompt();
            toast(`之后我会称呼你为「${userName}」`, 'success');
        };

        saveButton.addEventListener('click', saveName);
        input?.addEventListener('input', () => {
            if (error) error.textContent = '';
        });
        input?.addEventListener('keydown', event => {
            if (event.key === 'Enter' && !event.isComposing) {
                event.preventDefault();
                saveName();
            } else if (event.key === 'Escape') {
                event.preventDefault();
                closePrompt();
            }
        });

        requestAnimationFrame(() => {
            input?.focus();
            input?.select();
        });
    }

    function getActiveSession() {
        return sessions.find(session => session.id === activeSessionId) || sessions[0] || null;
    }

    function setRandomWelcomeSubtitle() {
        const subtitle = els.welcomeSubtitle;
        if (!subtitle || WELCOME_SUBTITLES.length === 0) return;

        resetWelcomeSubtitle(subtitle);

        const roll = Math.random();
        let threshold = 0;
        const easterEgg = WELCOME_EASTER_EGGS.find(item => {
            threshold += item.chance;
            return roll < threshold;
        });

        if (!easterEgg) {
            const index = Math.floor(Math.random() * WELCOME_SUBTITLES.length);
            subtitle.textContent = WELCOME_SUBTITLES[index];
            return;
        }

        subtitle.textContent = easterEgg.text;

        if (easterEgg.type === 'editable') {
            subtitle.classList.add('is-editable-tip');
            subtitle.contentEditable = 'true';
            subtitle.spellcheck = false;
            subtitle.setAttribute('role', 'textbox');
            subtitle.setAttribute('aria-label', '可编辑 Tip');
            subtitle.title = '点击后可以直接修改';
            subtitle.onkeydown = event => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    subtitle.blur();
                }
            };
            return;
        }

        if (easterEgg.type === 'gradient') {
            subtitle.classList.add('is-gradient-tip');
            document.documentElement.dataset.omegaTipActive = 'true';
            window.dispatchEvent(new CustomEvent('nachobot:omega-tip'));
            return;
        }

        if (easterEgg.type === 'evasive') {
            subtitle.classList.add('is-evasive-tip');
            subtitle.title = '试试看能不能追上';
            subtitle.onpointerenter = () => moveEvasiveSubtitle(subtitle);
        }
    }

    function resetWelcomeSubtitle(subtitle) {
        delete document.documentElement.dataset.omegaTipActive;
        subtitle.classList.remove('is-editable-tip', 'is-gradient-tip', 'is-evasive-tip');
        subtitle.contentEditable = 'false';
        subtitle.removeAttribute('role');
        subtitle.removeAttribute('aria-label');
        subtitle.removeAttribute('title');
        subtitle.style.removeProperty('--tip-shift-x');
        subtitle.style.removeProperty('--tip-shift-y');
        subtitle.onkeydown = null;
        subtitle.onpointerenter = null;
    }

    function moveEvasiveSubtitle(subtitle) {
        const container = subtitle.parentElement;
        const horizontalRange = Math.max(90, Math.min(260, (container?.clientWidth || 600) * 0.28));
        const verticalRange = 72;
        const x = (Math.random() * 2 - 1) * horizontalRange;
        const y = (Math.random() * 2 - 1) * verticalRange;
        subtitle.style.setProperty('--tip-shift-x', `${x.toFixed(0)}px`);
        subtitle.style.setProperty('--tip-shift-y', `${y.toFixed(0)}px`);
    }

    function bindEvents() {
        els.newChat?.addEventListener('click', startNewChat);
        els.clearChat?.addEventListener('click', clearCurrentChat);
        els.renameButton?.addEventListener('click', showRenamePrompt);
        els.status?.addEventListener('click', toggleCoreService);
        els.form.addEventListener('submit', handleSubmit);

        els.messages.addEventListener('click', event => {
            const avatar = event.target.closest('.chat-avatar-customizable');
            if (!avatar || !els.messages.contains(avatar)) return;
            openBotAvatarPicker();
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
                showRenamePrompt();
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

        session.messages = [];
        session.title = '新对话';
        session.updatedAt = Date.now();
        saveSessions();
        renderAll();
    }

    async function deleteSession(id) {
        const session = sessions.find(item => item.id === id);
        if (!session) return;

        if (busy) {
            toast('当前消息仍在处理中，请等待回复完成后再删除会话', 'error');
            return;
        }

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
        if (namePromptOpen) {
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
            namePromptOpen = true;
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
                namePromptOpen = false;
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
        activeSessionId = id;
        App.switchTab('chat');
        connectLiveStream(id);
        renderAll();
        closeSidebarOnMobile();
    }

    async function handleSubmit(event) {
        event.preventDefault();
        const text = els.input.value.trim();
        if (!text || busy) return;

        let session = getActiveSession();
        if (!session) {
            session = createSession();
            sessions.unshift(session);
            activeSessionId = session.id;
        }

        session.messages.push({
            id: createId(),
            role: 'user',
            content: text,
            createdAt: Date.now(),
        });
        if (session.title === '新对话') session.title = makeTitle(text);
        session.updatedAt = Date.now();

        els.input.value = '';
        autoResizeInput();
        saveSessions();
        renderAll();
        setBusy(true);
        renderThinking();
        connectLiveStream(session.id);

        try {
            const response = await fetch(API_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    conversation_id: session.id,
                    message: text,
                    user_name: userName || DEFAULT_USER_NAME,
                }),
            });

            if (!response.ok) {
                const detail = await response.json().catch(() => null);
                const error = new Error(detail?.detail || `HTTP ${response.status}`);
                error.status = response.status;
                throw error;
            }

            const data = await response.json();
            const reply = data?.message?.content ?? data?.reply ?? data?.content;
            if (typeof reply !== 'string' || !reply.trim()) {
                throw new Error('聊天接口未返回有效文本');
            }

            if (typeof data.conversation_id === 'string' && data.conversation_id !== session.id) {
                session.remoteConversationId = data.conversation_id;
            }

            appendAssistantMessage(session, {
                message_id: data?.message_id,
                message: {
                    role: data?.message?.role || 'assistant',
                    content: reply,
                },
            });
        } catch (error) {
            console.warn('Chat backend unavailable:', error);
            session.messages.push({
                id: createId(),
                role: 'notice',
                content: error.status === 404
                    ? '聊天界面已经就绪，但 NachoBot 聊天后端尚未接入。后端实现 POST /api/chat/message 后即可返回真实回复。'
                    : `消息未能发送到聊天后端：${error.message}`,
                createdAt: Date.now(),
            });
        } finally {
            session.updatedAt = Date.now();
            saveSessions();
            setBusy(false);
            renderAll();
            els.input.focus();
        }
    }

    function makeTitle(text) {
        const compact = text.replace(/\s+/g, ' ').trim();
        return compact.length > 24 ? `${compact.slice(0, 24)}…` : compact;
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
                if (!session || !appendAssistantMessage(session, data)) return;
                saveSessions();
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

        session.messages.push({
            id: createId(),
            backendMessageId,
            role: event?.message?.role || 'assistant',
            content,
            createdAt: Date.now(),
        });
        session.updatedAt = Date.now();
        return true;
    }

    function setBusy(value) {
        busy = value;
        els.input.disabled = value;
        updateSendState();
    }

    function updateSendState() {
        if (!els.send) return;
        els.send.disabled = busy || !els.input.value.trim();
        els.send.classList.toggle('is-busy', busy);
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

    function openBotAvatarPicker() {
        const picker = document.createElement('input');
        picker.type = 'file';
        picker.accept = 'image/png,image/jpeg,image/webp,image/gif';
        picker.hidden = true;

        picker.addEventListener('change', async () => {
            const file = picker.files?.[0];
            picker.remove();
            if (!file) return;

            if (!file.type.startsWith('image/')) {
                toast('请选择有效的图片文件', 'error');
                return;
            }

            if (file.size > 12 * 1024 * 1024) {
                toast('头像图片不能超过 12 MB', 'error');
                return;
            }

            try {
                botAvatarDataUrl = await createAvatarDataUrl(file);
                localStorage.setItem(BOT_AVATAR_STORAGE_KEY, botAvatarDataUrl);
                renderAll();
                toast('bot 头像已更新', 'success');
            } catch (error) {
                console.warn('Failed to update bot avatar:', error);
                toast('无法读取这张图片', 'error');
            }
        }, { once: true });

        document.body.appendChild(picker);
        picker.click();
    }

    function createAvatarDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onerror = () => reject(reader.error || new Error('读取图片失败'));
            reader.onload = () => {
                const image = new Image();
                image.onerror = () => reject(new Error('图片格式不受支持'));
                image.onload = () => {
                    const size = 256;
                    const canvas = document.createElement('canvas');
                    const context = canvas.getContext('2d');
                    if (!context) {
                        reject(new Error('无法创建图片画布'));
                        return;
                    }

                    canvas.width = size;
                    canvas.height = size;
                    const sourceSize = Math.min(image.naturalWidth, image.naturalHeight);
                    const sourceX = (image.naturalWidth - sourceSize) / 2;
                    const sourceY = (image.naturalHeight - sourceSize) / 2;
                    context.drawImage(
                        image,
                        sourceX,
                        sourceY,
                        sourceSize,
                        sourceSize,
                        0,
                        0,
                        size,
                        size,
                    );
                    resolve(canvas.toDataURL('image/webp', 0.9));
                };
                image.src = String(reader.result || '');
            };
            reader.readAsDataURL(file);
        });
    }

    function createBotAvatarMarkup() {
        const content = botAvatarDataUrl
            ? `<img src="${escapeText(botAvatarDataUrl)}" alt="">`
            : 'N';
        return `<button type="button" class="chat-avatar chat-avatar-customizable"
            aria-label="更换 bot 头像" title="点击更换 bot 头像">${content}</button>`;
    }

    function renderMessages() {
        const session = getActiveSession();
        const messages = session?.messages || [];
        els.empty?.classList.toggle('hidden', messages.length > 0);

        els.messages.querySelectorAll('.chat-message, .chat-thinking').forEach(node => node.remove());
        messages.forEach(message => els.messages.appendChild(createMessageElement(message)));
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
                ${isUser ? '' : createBotAvatarMarkup()}
                <div class="chat-message-content">${formatContent(message.content)}</div>
            </div>
        `;
        return article;
    }

    function renderThinking() {
        const thinking = document.createElement('div');
        thinking.className = 'chat-thinking';
        thinking.innerHTML = `
            ${createBotAvatarMarkup()}
            <div class="chat-thinking-dots"><span></span><span></span><span></span></div>
        `;
        els.messages.appendChild(thinking);
        scrollToBottom();
    }

    function formatContent(content) {
        const source = String(content);
        const fragments = [];
        const codeFence = /```([\w-]*)\n?([\s\S]*?)```/g;
        let cursor = 0;
        let match;

        while ((match = codeFence.exec(source)) !== null) {
            if (match.index > cursor) {
                fragments.push(formatTextBlocks(source.slice(cursor, match.index)));
            }

            const language = escapeText(match[1] || '代码');
            const code = escapeText(match[2].replace(/^\n|\n$/g, ''));
            fragments.push(
                `<pre class="chat-code"><div class="chat-code-header"><span>${language}</span></div><code>${code}</code></pre>`
            );
            cursor = codeFence.lastIndex;
        }

        if (cursor < source.length) {
            fragments.push(formatTextBlocks(source.slice(cursor)));
        }

        return fragments.join('');
    }

    function formatTextBlocks(text) {
        return escapeText(text)
            .split(/\n{2,}/)
            .filter(block => block.trim())
            .map(block => `<p>${block.replace(/\n/g, '<br>')}</p>`)
            .join('');
    }

    function escapeText(value) {
        return value
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function formatTime(timestamp) {
        const date = new Date(timestamp || Date.now());
        const today = new Date();
        if (date.toDateString() === today.toDateString()) {
            return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        }
        return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
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
        els.status.disabled = true;
        els.status.className = 'chat-status-chip is-checking';
        els.status.textContent = shouldStart ? '核心服务启动中' : '核心服务关闭中';
        els.status.title = shouldStart ? '正在启动 NachoBot Core' : '正在关闭 NachoBot Core';

        try {
            await apiPost(`/api/services/nachobot/${shouldStart ? 'start' : 'stop'}`);

            let reachedTarget = false;
            for (let attempt = 0; attempt < 40; attempt += 1) {
                await new Promise(resolve => window.setTimeout(resolve, 500));
                try {
                    const data = await apiGet('/api/chat/status');
                    coreRunning = Boolean(data.core_running);
                    if (coreRunning === shouldStart) {
                        reachedTarget = true;
                        break;
                    }
                } catch (error) {
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
        els.status.disabled = true;
        els.status.className = 'chat-status-chip is-checking';
        els.status.textContent = '正在检查服务';
        els.status.removeAttribute('title');

        try {
            const data = await apiGet('/api/chat/status');
            coreRunning = Boolean(data.core_running);
            els.status.setAttribute('aria-pressed', String(coreRunning));

            if (coreRunning) {
                els.status.className = 'chat-status-chip is-online';
                els.status.textContent = '核心服务运行中';
                els.status.title = data.available
                    ? '点击关闭 NachoBot Core'
                    : `点击关闭 NachoBot Core；聊天后端异常：${data.error || '不可用'}`;
            } else {
                els.status.className = 'chat-status-chip is-offline';
                els.status.textContent = '核心服务未运行';
                els.status.title = '点击启动 NachoBot Core';
            }
        } catch (error) {
            coreRunning = false;
            els.status.setAttribute('aria-pressed', 'false');
            els.status.className = 'chat-status-chip is-offline';
            els.status.textContent = '无法读取服务状态';
            els.status.title = '点击尝试启动 NachoBot Core';
        } finally {
            els.status.disabled = false;
        }
    }

    return { init, refresh };
})();
