/**
 * Pure chat helpers and the self-contained TTS playback controller.
 */
window.ChatSupport = (() => {
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

    function makeTitle(text) {
        const compact = text.replace(/\s+/g, ' ').trim();
        return compact.length > 24 ? `${compact.slice(0, 24)}…` : compact;
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

    function createTTS({ getActiveSession, getMessagesElement, escapeText, apiGet, toast }) {
        let ttsReady = false;
        let ttsLoadingMessageId = '';
        let activeSpeechMessageId = '';
        let activeSpeechAudio = null;
        let activeSpeechUrl = '';

        function createSpeakerMarkup(message) {
            return `
                <button type="button" class="chat-tts-button"
                    data-message-id="${escapeText(String(message.id || ''))}"
                    aria-label="生成并播放语音" title="TTS 服务未就绪" disabled>
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M4 9v6h4l5 4V5L8 9H4z"></path>
                        <path class="chat-tts-wave chat-tts-wave-one"
                            d="M16 9.5a4 4 0 0 1 0 5"></path>
                        <path class="chat-tts-wave chat-tts-wave-two"
                            d="M18.5 7a7 7 0 0 1 0 10"></path>
                    </svg>
                </button>
            `;
        }

        async function handleSpeechButton(button) {
            const messageId = button.dataset.messageId || '';
            const session = getActiveSession();
            const message = session?.messages.find(item => item.id === messageId);
            if (!message || !ttsReady || ttsLoadingMessageId) return;

            if (
                activeSpeechMessageId === messageId
                && activeSpeechAudio
                && !activeSpeechAudio.paused
            ) {
                stopActiveSpeech();
                syncSpeakerButtons();
                return;
            }

            stopActiveSpeech();
            ttsLoadingMessageId = messageId;
            syncSpeakerButtons();

            try {
                const response = await fetch('/api/chat/tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: message.content }),
                });

                if (!response.ok) {
                    const detail = await response.json().catch(() => null);
                    const error = new Error(detail?.detail || `HTTP ${response.status}`);
                    error.status = response.status;
                    throw error;
                }

                const audioBlob = await response.blob();
                if (!audioBlob.size) throw new Error('TTS 服务返回了空音频');

                const audioUrl = URL.createObjectURL(audioBlob);
                const audio = new Audio(audioUrl);
                activeSpeechMessageId = messageId;
                activeSpeechAudio = audio;
                activeSpeechUrl = audioUrl;

                audio.addEventListener('ended', () => {
                    stopActiveSpeech();
                    syncSpeakerButtons();
                }, { once: true });
                audio.addEventListener('error', () => {
                    stopActiveSpeech();
                    syncSpeakerButtons();
                    toast('语音播放失败', 'error');
                }, { once: true });

                await audio.play();
            } catch (error) {
                console.warn('TTS generation or playback failed:', error);
                stopActiveSpeech();
                if (error.status === 503) {
                    ttsReady = false;
                }
                toast(`语音生成失败：${error.message}`, 'error');
            } finally {
                ttsLoadingMessageId = '';
                syncSpeakerButtons();
            }
        }

        function stopActiveSpeech() {
            if (activeSpeechAudio) {
                activeSpeechAudio.pause();
                activeSpeechAudio.removeAttribute('src');
                activeSpeechAudio.load();
            }
            if (activeSpeechUrl) URL.revokeObjectURL(activeSpeechUrl);
            activeSpeechAudio = null;
            activeSpeechUrl = '';
            activeSpeechMessageId = '';
        }

        function syncSpeakerButtons() {
            if (!getMessagesElement()) return;
            getMessagesElement().querySelectorAll('.chat-tts-button').forEach(button => {
                const messageId = button.dataset.messageId || '';
                const isLoading = ttsLoadingMessageId === messageId;
                const isPlaying = activeSpeechMessageId === messageId
                    && activeSpeechAudio
                    && !activeSpeechAudio.paused;

                button.classList.toggle('is-loading', isLoading);
                button.classList.toggle('is-playing', Boolean(isPlaying));
                button.disabled = !ttsReady || Boolean(ttsLoadingMessageId);

                if (!ttsReady) {
                    button.title = 'TTS 服务未就绪';
                    button.setAttribute('aria-label', 'TTS 服务未就绪');
                } else if (isLoading) {
                    button.title = '正在生成语音';
                    button.setAttribute('aria-label', '正在生成语音');
                } else if (isPlaying) {
                    button.title = '停止播放';
                    button.setAttribute('aria-label', '停止播放');
                    button.disabled = false;
                } else {
                    button.title = '生成并播放语音';
                    button.setAttribute('aria-label', '生成并播放语音');
                }
            });
        }

        async function updateTTSStatus() {
            try {
                const data = await apiGet('/api/chat/tts/status');
                ttsReady = Boolean(data.ready);
            } catch (error) {
                ttsReady = false;
            }
            syncSpeakerButtons();
        }

        return {
            createSpeakerMarkup,
            handleSpeechButton,
            stop: stopActiveSpeech,
            syncButtons: syncSpeakerButtons,
            updateStatus: updateTTSStatus,
        };
    }

    return {
        createSession,
        createTTS,
        escapeText,
        formatContent,
        formatTime,
        makeTitle,
    };
})();
