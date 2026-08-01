/**
 * User naming, modal flow, and bot-avatar preferences for chat.
 */
window.ChatProfile = (() => {
    const USER_NAME_STORAGE_KEY = 'nachobot_chat_user_name_v1';
    const BOT_AVATAR_STORAGE_KEY = 'nachobot_chat_bot_avatar_v1';

    function create({ getInput, isModalOpen, render, setModalOpen, escapeText, toast }) {
        let userName = '';
        let botAvatarDataUrl = '';
        const renderAll = render;

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
            if (userName || isModalOpen()) return;

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
            if (userName || isModalOpen()) return;

            const overlay = document.getElementById('modal-overlay');
            const card = document.getElementById('modal-card');
            const title = document.getElementById('modal-title');
            const body = document.getElementById('modal-body');
            const footer = document.getElementById('modal-footer');
            const closeButton = document.getElementById('modal-close');
            if (!overlay || !card || !title || !body || !footer || !closeButton) return;

            setModalOpen(true);
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
                setModalOpen(false);
                toast(`之后我会称呼你为「${userName}」`, 'success');
                getInput()?.focus();
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
            if (isModalOpen()) return;

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
                setModalOpen(false);
                getInput()?.focus();
            };

            setModalOpen(true);
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

        function load() {
            loadUserName();
            loadBotAvatar();
        }

        return {
            createBotAvatarMarkup,
            getUserName: () => userName,
            load,
            openBotAvatarPicker,
            scheduleFirstUseNamePrompt,
            showRenamePrompt,
        };
    }

    return { create };
})();
