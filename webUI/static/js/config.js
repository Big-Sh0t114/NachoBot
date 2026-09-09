/**
 * NachoBot WebUI — Config Editor Module
 * Provides a file tree + raw TOML editor with save/backup.
 */

const ConfigModule = (() => {
    let configs = [];
    let activeFileId = null;
    let originalRaw = '';

    async function init() {
        await loadTree();
    }

    async function loadTree() {
        try {
            configs = await apiGet('/api/configs');
            renderTree();
        } catch (e) {
            toast('加载配置列表失败', 'error');
        }
    }

    function renderTree() {
        const tree = document.getElementById('config-tree');
        tree.innerHTML = '';

        // Group configs
        const groups = {};
        for (const c of configs) {
            if (!groups[c.group]) groups[c.group] = [];
            groups[c.group].push(c);
        }

        for (const [groupName, items] of Object.entries(groups)) {
            const title = document.createElement('div');
            title.className = 'config-group-title';
            title.textContent = groupName;
            tree.appendChild(title);

            for (const item of items) {
                const el = document.createElement('div');
                el.className = 'config-item';
                if (!item.exists) el.style.opacity = '0.4';
                el.dataset.id = item.id;

                el.innerHTML = `
                    <span>📄</span>
                    <span>${escapeHtml(item.label)}</span>
                    <span class="dot-modified"></span>
                `;

                if (item.exists) {
                    el.addEventListener('click', () => openFile(item.id));
                }

                tree.appendChild(el);
            }
        }
    }

    async function openFile(fileId) {
        // Update active state
        document.querySelectorAll('.config-item').forEach(el => el.classList.remove('active'));
        const item = document.querySelector(`.config-item[data-id="${fileId}"]`);
        if (item) item.classList.add('active');

        activeFileId = fileId;

        try {
            const res = await apiGet(`/api/configs/${fileId}`);
            originalRaw = res.raw;
            renderEditor(fileId, res.raw);
            if (res.data === null) {
                toast('⚠️ 检测到配置存在语法错误，已开启纯文本安全模式', 'warning');
            }
        } catch (e) {
            toast('加载配置失败: ' + e.message, 'error');
        }
    }

    function renderEditor(fileId, raw) {
        const editor = document.getElementById('config-editor');
        const label = configs.find(c => c.id === fileId)?.label || fileId;

        editor.innerHTML = `
            <div class="editor-header">
                <span class="editor-title">${escapeHtml(label)}</span>
                <div class="editor-actions">
                    <button class="btn btn-ghost" id="btn-backup" title="备份当前">🚀 备份</button>
                    <button class="btn btn-ghost" id="btn-restore" title="恢复至最新备份">↩️ 恢复备份</button>
                    <button class="btn btn-ghost" id="btn-reset" title="撤销本次未保存的编辑">↩️ 撤销</button>
                    <button class="btn btn-primary" id="btn-save" title="保存">💾 保存</button>
                </div>
            </div>
            <textarea class="editor-textarea" id="editor-content" spellcheck="false">${escapeHtml(raw)}</textarea>
        `;

        // Event listeners
        document.getElementById('btn-save').addEventListener('click', saveFile);
        document.getElementById('btn-reset').addEventListener('click', resetFile);
        document.getElementById('btn-backup').addEventListener('click', backupFile);
        document.getElementById('btn-restore').addEventListener('click', restoreBackupFile);

        // Track modifications
        document.getElementById('editor-content').addEventListener('input', () => {
            const modified = document.getElementById('editor-content').value !== originalRaw;
            const item = document.querySelector(`.config-item[data-id="${activeFileId}"]`);
            if (item) item.classList.toggle('modified', modified);
        });
    }

    async function saveFile() {
        if (!activeFileId) return;

        const raw = document.getElementById('editor-content').value;

        try {
            await apiPut(`/api/configs/${activeFileId}`, { raw });
            originalRaw = raw;
            const item = document.querySelector(`.config-item[data-id="${activeFileId}"]`);
            if (item) item.classList.remove('modified');
            if (activeFileId === 'webui_config') {
                await App.loadWebUIInfo();
            }
            toast('配置已保存', 'success');
        } catch (e) {
            toast('保存失败: ' + e.message, 'error');
        }
    }

    function resetFile() {
        if (!activeFileId) return;
        const ta = document.getElementById('editor-content');
        if (ta) {
            ta.value = originalRaw;
            const item = document.querySelector(`.config-item[data-id="${activeFileId}"]`);
            if (item) item.classList.remove('modified');
            toast('已撤销本次编辑', 'info');
        }
    }

    async function backupFile() {
        if (!activeFileId) return;
        try {
            const res = await apiPost(`/api/configs/${activeFileId}/backup`);
            toast(`备份成功: ${res.backup}`, 'success');
        } catch (e) {
            toast('备份失败: ' + e.message, 'error');
        }
    }

    async function restoreBackupFile() {
        if (!activeFileId) return;
        
        try {
            const res = await apiGet(`/api/configs/${activeFileId}/backups`);
            const backups = res.backups;
            
            if (!backups || backups.length === 0) {
                toast('没有找到任何备份文件', 'warning');
                return;
            }
            
            let optionsHtml = '';
            backups.forEach((b) => {
                const date = new Date(b.mtime * 1000).toLocaleString();
                optionsHtml += `<option value="${b.filename}">[${b.type}] ${date} - ${b.filename}</option>`;
            });
            
            const content = `
                <div style="margin-bottom: 1rem; color: var(--text-primary);">请选择要恢复的备份文件。这将覆盖当前配置，操作不可逆：</div>
                <select id="restore-backup-select" class="form-select" style="width: 100%; margin-bottom: 1rem;">
                    ${optionsHtml}
                </select>
            `;
            
            showModal(
                '恢复备份',
                content,
                [
                    { label: '取消', class: 'btn btn-ghost', action: hideModal },
                    { 
                        label: '确定恢复', 
                        class: 'btn btn-primary', 
                        action: async () => {
                            const sel = document.getElementById('restore-backup-select');
                            const backupFile = sel.value;
                            hideModal();
                            
                            try {
                                const restoreRes = await apiPost(`/api/configs/${activeFileId}/restore`, { backup_file: backupFile });
                                toast(`恢复备份成功: ${restoreRes.backup}`, 'success');
                                openFile(activeFileId);
                            } catch (e) {
                                toast('恢复备份失败: ' + e.message, 'error');
                            }
                        }
                    }
                ]
            );
            
        } catch (e) {
            toast('获取备份列表失败: ' + e.message, 'error');
        }
    }

    // ---- Modal helpers ----
    function showModal(title, bodyHtml, buttons) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').innerHTML = bodyHtml;

        const footer = document.getElementById('modal-footer');
        footer.innerHTML = '';
        for (const b of buttons) {
            const btn = document.createElement('button');
            btn.className = b.class;
            btn.textContent = b.label;
            btn.addEventListener('click', b.action);
            footer.appendChild(btn);
        }

        document.getElementById('modal-overlay').classList.remove('hidden');
        document.getElementById('modal-close').onclick = hideModal;

        document.getElementById('modal-overlay').addEventListener('click', (e) => {
            if (e.target === document.getElementById('modal-overlay')) hideModal();
        });
    }

    function hideModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
    }

    // ---- Refresh ----

    function refresh() {
        activeFileId = null;
        originalRaw = '';
        const editor = document.getElementById('config-editor');
        if (editor) {
            editor.innerHTML = `
                <div class="editor-placeholder">
                    <span class="placeholder-icon">📄</span>
                    <p>选择左侧的配置文件开始编辑</p>
                </div>`;
        }
        return loadTree();
    }

    return { init, refresh };
})();
