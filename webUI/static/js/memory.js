/**
 * NachoBot WebUI — Memory Module (A_Memorix)
 * Long-term memory search, stats display, and maintenance actions.
 */

const MemoryModule = (() => {
    let loaded = false;
    const MEMORY_STATS_TIMEOUT_MS = 10000;
    const MEMORY_SEARCH_TIMEOUT_MS = 18000;
    const MEMORY_MAINTAIN_TIMEOUT_MS = 25000;

    function init() {
        // Bind search button
        const searchBtn = document.getElementById('memory-search-btn');
        if (searchBtn) searchBtn.addEventListener('click', doSearch);

        // Bind Enter key in search input
        const searchInput = document.getElementById('memory-search-input');
        if (searchInput) {
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') doSearch();
            });
        }

        // Bind maintenance buttons
        document.querySelectorAll('.memory-maintain-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                doMaintain(btn.dataset.action, btn.dataset.label);
            });
        });
    }

    async function refresh() {
        await loadStats();
    }

    async function loadStats() {
        const container = document.getElementById('memory-stats');
        if (!container) return;

        try {
            const status = await apiGetWithTimeout('/api/memory/status', MEMORY_STATS_TIMEOUT_MS);
            if (!status.enabled) {
                container.innerHTML = `
                    <div class="memory-stat-card">
                        <span class="stat-icon">⚠️</span>
                        <div class="stat-info">
                            <span class="stat-label">状态</span>
                            <span class="stat-value" style="color: var(--text-secondary);">A_Memorix 未启用</span>
                        </div>
                    </div>
                `;
                return;
            }

            const stats = await apiGetWithTimeout('/api/memory/stats', MEMORY_STATS_TIMEOUT_MS);
            const coreRunning = stats.core_running !== false && status.core_running !== false;

            let cards = `
                <div class="memory-stat-card">
                    <span class="stat-icon">${coreRunning ? '🟢' : '⏸️'}</span>
                    <div class="stat-info">
                        <span class="stat-label">状态</span>
                        <span class="stat-value">${coreRunning ? '已启用' : '核心未运行'}</span>
                    </div>
                </div>
            `;

            if (stats.total_memories !== undefined) {
                cards += `
                    <div class="memory-stat-card">
                        <span class="stat-icon">🧠</span>
                        <div class="stat-info">
                            <span class="stat-label">记忆总数</span>
                            <span class="stat-value">${stats.total_memories}</span>
                        </div>
                    </div>
                `;
            }

            if (stats.storage_dir) {
                cards += `
                    <div class="memory-stat-card">
                        <span class="stat-icon">📁</span>
                        <div class="stat-info">
                            <span class="stat-label">存储路径</span>
                            <span class="stat-value" style="font-size: 0.8rem;">${escapeHtml(stats.storage_dir)}</span>
                        </div>
                    </div>
                `;
            }

            if (stats.note) {
                cards += `
                    <div class="memory-stat-card" style="grid-column: 1 / -1;">
                        <span class="stat-icon">ℹ️</span>
                        <div class="stat-info">
                            <span class="stat-value" style="font-size: 0.85rem; color: var(--text-secondary);">${escapeHtml(stats.note)}</span>
                        </div>
                    </div>
                `;
            }

            container.innerHTML = cards;
        } catch (e) {
            container.innerHTML = `
                <div class="memory-stat-card" style="grid-column: 1 / -1;">
                    <span class="stat-icon">❌</span>
                    <div class="stat-info">
                        <span class="stat-label">错误</span>
                        <span class="stat-value">${escapeHtml(e.message)}</span>
                    </div>
                </div>
            `;
        }
    }

    async function doSearch() {
        const input = document.getElementById('memory-search-input');
        const resultsEl = document.getElementById('memory-search-results');
        const query = input.value.trim();
        if (!query) {
            toast('请输入搜索关键词', 'warning');
            return;
        }

        resultsEl.innerHTML = '<div class="memory-loading">🔍 正在检索长期记忆...</div>';

        try {
            const res = await apiPostWithTimeout('/api/memory/search', {
                query,
                limit: 15,
            }, MEMORY_SEARCH_TIMEOUT_MS);

            const items = res.results || res.data || [];
            if (items.length === 0) {
                resultsEl.innerHTML = `
                    <div class="memory-empty">
                        <span class="placeholder-icon">🔍</span>
                        <p>未找到与「${escapeHtml(query)}」相关的记忆</p>
                    </div>
                `;
                return;
            }

            let html = '';
            items.forEach((item, idx) => {
                const text = item.text || item.content || item.summary || JSON.stringify(item);
                const score = item.score !== undefined ? `<span class="memory-score">相关度: ${(item.score * 100).toFixed(1)}%</span>` : '';
                const time = item.timestamp || item.time || '';
                const timeTag = time ? `<span class="memory-time">${escapeHtml(String(time))}</span>` : '';

                html += `
                    <div class="memory-result-card">
                        <div class="memory-result-header">
                            <span class="memory-result-idx">#${idx + 1}</span>
                            ${score}
                            ${timeTag}
                        </div>
                        <div class="memory-result-text">${escapeHtml(text)}</div>
                    </div>
                `;
            });

            resultsEl.innerHTML = html;
        } catch (e) {
            resultsEl.innerHTML = `<div class="memory-error">❌ 检索失败: ${escapeHtml(e.message)}</div>`;
        }
    }

    async function doMaintain(action, label) {
        if (!confirm(`确定执行「${label}」操作吗？`)) return;

        try {
            const result = await apiPostWithTimeout('/api/memory/maintain', { action }, MEMORY_MAINTAIN_TIMEOUT_MS);
            if (result.success !== false) {
                toast(`${label} 执行成功`, 'success');
            } else {
                toast(`${label} 失败: ${result.error || '未知错误'}`, 'error');
            }
        } catch (e) {
            toast(`${label} 失败: ${e.message}`, 'error');
        }
    }

    async function apiGetWithTimeout(url, timeoutMs) {
        const res = await fetchWithTimeout(url, {}, timeoutMs);
        if (!res.ok) throw new Error(`API Error: ${res.status}`);
        return res.json();
    }

    async function apiPostWithTimeout(url, body, timeoutMs) {
        const res = await fetchWithTimeout(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }, timeoutMs);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || `API Error: ${res.status}`);
        }
        return res.json();
    }

    async function fetchWithTimeout(url, options, timeoutMs) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
            return await fetch(url, { ...options, signal: controller.signal });
        } catch (e) {
            if (e.name === 'AbortError') {
                throw new Error('请求超时，长期记忆服务可能正在初始化或已卡住');
            }
            throw e;
        } finally {
            clearTimeout(timer);
        }
    }

    return { init, refresh };
})();
