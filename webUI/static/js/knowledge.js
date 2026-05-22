/**
 * NachoBot WebUI — Knowledge Base Module
 * View and edit raw knowledge text files; display embedding/RAG stats.
 */

const KnowledgeModule = (() => {
    let files = [];
    let activeFile = null;
    let isDirty = false;

    function init() {
        // nothing special on init
    }

    async function refresh() {
        try {
            files = await apiGet('/api/knowledge/files');
            renderFileList();
            renderStats();
        } catch (e) {
            toast('加载知识库信息失败', 'error');
        }
    }

    async function renderStats() {
        try {
            const stats = await apiGet('/api/knowledge/stats');
            const el = document.getElementById('knowledge-stats');

            const embParts = [];
            for (const [ns, info] of Object.entries(stats.embedding || {})) {
                const label = { paragraph: '段落', entity: '实体', relation: '关系' }[ns] || ns;
                embParts.push(`${label}: <strong>${info.items.toLocaleString()}</strong> 条`);
            }

            const ragParts = [];
            if (stats.rag && stats.rag.nodes !== undefined) {
                ragParts.push(`节点: <strong>${stats.rag.nodes.toLocaleString()}</strong>`);
                ragParts.push(`边: <strong>${stats.rag.edges.toLocaleString()}</strong>`);
            }

            el.innerHTML = `
                <div class="db-stats-bar">
                    <span class="stat-item">📄 知识文件：<strong>${stats.knowledge_files}</strong> 个（${stats.total_knowledge_size}）</span>
                    ${embParts.length ? `<span class="stat-item">🧬 Embedding：${embParts.join('，')}</span>` : ''}
                    ${ragParts.length ? `<span class="stat-item">🕸️ KG：${ragParts.join('，')}</span>` : ''}
                </div>
            `;
        } catch (e) { /* ignore */ }
    }

    function renderFileList() {
        const container = document.getElementById('knowledge-file-list');
        container.innerHTML = '';

        if (files.length === 0) {
            container.innerHTML = '<div style="padding: 16px; color: var(--text-muted);">未找到知识库文件</div>';
            return;
        }

        for (const f of files) {
            const item = document.createElement('div');
            item.className = `db-table-item ${activeFile === f.filename ? 'active' : ''}`;
            item.innerHTML = `
                <span class="db-table-name">${escapeHtml(f.filename)}</span>
                <span class="db-table-count">${f.size_display}</span>
            `;
            item.addEventListener('click', () => {
                if (isDirty && !confirm('当前文件已修改但未保存，确定要切换吗？')) return;
                activeFile = f.filename;
                isDirty = false;
                renderFileList();
                loadFile(f.filename);
            });
            container.appendChild(item);
        }
    }

    async function loadFile(filename) {
        const container = document.getElementById('knowledge-editor');
        try {
            const result = await apiGet(`/api/knowledge/files/${encodeURIComponent(filename)}`);
            const readonly = result.core_running;

            container.innerHTML = `
                <div class="knowledge-editor-header">
                    <h3>${escapeHtml(filename)}</h3>
                    <div class="knowledge-editor-actions">
                        ${readonly ? '<span class="knowledge-readonly-badge">🔒 核心运行中 (只读)</span>' : ''}
                        <button class="btn btn-primary" id="knowledge-save-btn" ${readonly ? 'disabled' : ''}>💾 保存</button>
                    </div>
                </div>
                <textarea id="knowledge-textarea" class="knowledge-textarea" spellcheck="false" ${readonly ? 'readonly' : ''}>${escapeHtml(result.content)}</textarea>
            `;

            document.getElementById('knowledge-textarea').addEventListener('input', () => {
                isDirty = true;
            });

            document.getElementById('knowledge-save-btn').addEventListener('click', () => saveFile(filename));
        } catch (e) {
            container.innerHTML = `<div class="editor-placeholder"><p>加载失败: ${escapeHtml(e.message)}</p></div>`;
        }
    }

    async function saveFile(filename) {
        const content = document.getElementById('knowledge-textarea').value;
        try {
            await apiPut(`/api/knowledge/files/${encodeURIComponent(filename)}`, { content });
            isDirty = false;
            toast('知识库文件已保存', 'success');
            refresh(); // refresh file sizes
        } catch (e) {
            toast('保存失败: ' + e.message, 'error');
        }
    }

    return { init, refresh };
})();
