/**
 * NachoBot WebUI — App Core
 * Routing, global state, and utility functions.
 */

const App = (() => {
    let currentTab = 'config';
    let statusInterval = null;

    // ---- Tab Routing ----
    function init() {
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                switchTab(item.dataset.tab);
            });
        });

        // Initialize modules
        ConfigModule.init();
        LauncherModule.init();
        TerminalModule.init();
        PluginsModule.init();
        DatabaseModule.init();
        KnowledgeModule.init();
        MemoryModule.init();
        SetupModule.init();

        // Start polling status
        statusInterval = setInterval(pollStatus, 3000);
        pollStatus();
    }

    function switchTab(tab) {
        currentTab = tab;

        // Update nav
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        document.querySelector(`[data-tab="${tab}"]`).classList.add('active');

        // Update panels
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        document.getElementById(`tab-${tab}`).classList.add('active');

        // Trigger module refresh
        if (tab === 'config') ConfigModule.refresh();
        if (tab === 'launcher') LauncherModule.refresh();
        if (tab === 'terminal') TerminalModule.refresh();
        if (tab === 'plugins') PluginsModule.refresh();
        if (tab === 'database') DatabaseModule.refresh();
        if (tab === 'knowledge') KnowledgeModule.refresh();
        if (tab === 'memory') MemoryModule.refresh();
        if (tab === 'setup') SetupModule.refresh();
    }

    // ---- Status Polling ----
    async function pollStatus() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            updateStatusBar(data);
        } catch (e) {
            // Server might not be ready
        }
    }

    function updateStatusBar(data) {
        let totalRunning = 0;
        let totalServices = 0;
        for (const g of Object.values(data)) {
            totalRunning += g.running;
            totalServices += g.total;
        }

        const dot = document.getElementById('status-dot');
        const text = document.getElementById('status-text');

        if (totalRunning === 0) {
            dot.style.background = '#94a3b8';
            dot.style.animation = 'none';
            text.textContent = '全部停止';
        } else if (totalRunning === totalServices) {
            dot.style.background = '#22c55e';
            dot.style.animation = 'pulse-dot 2s ease-in-out infinite';
            text.textContent = `全部运行 (${totalRunning})`;
        } else {
            dot.style.background = '#f59e0b';
            dot.style.animation = 'pulse-dot 1.5s ease-in-out infinite';
            text.textContent = `${totalRunning}/${totalServices} 运行中`;
        }
    }

    return { init, switchTab, pollStatus };
})();

// ---- Global Utilities ----

function toast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

async function apiGet(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`API Error: ${res.status}`);
    return res.json();
}

async function apiPost(url, body = null) {
    const res = await fetch(url, {
        method: 'POST',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `API Error: ${res.status}`);
    }
    return res.json();
}

async function apiPut(url, body) {
    const res = await fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `API Error: ${res.status}`);
    }
    return res.json();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

async function apiDelete(url) {
    const res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `API Error: ${res.status}`);
    }
    return res.json();
}

// ---- Init ----
document.addEventListener('DOMContentLoaded', () => App.init());
