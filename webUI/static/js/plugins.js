/**
 * NachoBot WebUI — Plugins Module
 * Lists plugins and provides config editing via modal.
 */

const PluginsModule = (() => {
    let plugins = [];

    function init() {
        refresh();
    }

    async function refresh() {
        try {
            plugins = await apiGet('/api/plugins');
            render();
        } catch (e) {
            toast('加载插件列表失败', 'error');
        }
    }

    function render() {
        const grid = document.getElementById('plugins-grid');
        grid.innerHTML = '';

        if (plugins.length === 0) {
            grid.innerHTML = '<div style="color: var(--text-muted); text-align: center; padding: 40px;">未发现插件</div>';
            return;
        }

        for (const p of plugins) {
            grid.appendChild(createPluginCard(p));
        }
    }

    function createPluginCard(plugin) {
        const card = document.createElement('div');
        card.className = 'plugin-card';

        const desc = plugin.description || '暂无描述';
        const ver = plugin.version || '-';

        card.innerHTML = `
            <div class="plugin-card-header">
                <span class="plugin-name">${escapeHtml(plugin.name)}</span>
                <span class="plugin-version">v${escapeHtml(ver)}</span>
            </div>
            <div class="plugin-desc">${escapeHtml(desc)}</div>
            <div class="plugin-actions">
                ${plugin.has_config ? `
                    <button class="btn btn-outline btn-config" data-id="${plugin.id}">⚙️ 编辑配置</button>
                ` : ''}
                ${plugin.readme ? `
                    <button class="btn btn-ghost btn-readme" data-id="${plugin.id}">📖 说明</button>
                ` : ''}
            </div>
        `;

        // Bind events
        const configBtn = card.querySelector('.btn-config');
        if (configBtn) {
            configBtn.addEventListener('click', () => openConfigModal(plugin));
        }

        const readmeBtn = card.querySelector('.btn-readme');
        if (readmeBtn) {
            readmeBtn.addEventListener('click', () => openReadmeModal(plugin));
        }

        return card;
    }

    async function openConfigModal(plugin) {
        try {
            const res = await apiGet(`/api/plugins/${plugin.id}/config`);
            showModal(
                `${plugin.name} — 配置`,
                `<textarea id="plugin-config-editor" spellcheck="false">${escapeHtml(res.raw)}</textarea>`,
                [
                    { label: '取消', class: 'btn btn-ghost', action: hideModal },
                    { label: '保存', class: 'btn btn-primary', action: () => savePluginConfig(plugin.id) },
                ]
            );
        } catch (e) {
            toast('加载插件配置失败: ' + e.message, 'error');
        }
    }

    async function savePluginConfig(pluginId) {
        const raw = document.getElementById('plugin-config-editor').value;
        try {
            await apiPut(`/api/plugins/${pluginId}/config`, { raw });
            toast('插件配置已保存', 'success');
            hideModal();
        } catch (e) {
            toast('保存失败: ' + e.message, 'error');
        }
    }

    function openReadmeModal(plugin) {
        // Simple markdown-ish rendering
        const html = plugin.readme
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/^### (.+)$/gm, '<h4>$1</h4>')
            .replace(/^## (.+)$/gm, '<h3>$1</h3>')
            .replace(/^# (.+)$/gm, '<h2>$1</h2>')
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/`([^`]+)`/g, '<code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:0.85em;">$1</code>')
            .replace(/\n/g, '<br>');

        showModal(
            `${plugin.name} — 说明`,
            `<div style="line-height:1.8;color:var(--text-primary);">${html}</div>`,
            [{ label: '关闭', class: 'btn btn-ghost', action: hideModal }]
        );
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

        // Close on overlay click
        document.getElementById('modal-overlay').addEventListener('click', (e) => {
            if (e.target === document.getElementById('modal-overlay')) hideModal();
        });
    }

    function hideModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
    }

    return { init, refresh };
})();
