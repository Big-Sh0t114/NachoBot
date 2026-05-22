/**
 * NachoBot WebUI — Terminal Module
 * Real-time log streaming via WebSocket with history replay.
 */

const TerminalModule = (() => {
    let services = [];
    let activeServiceId = 'all';
    let ws = null;
    let autoScroll = true;

    function init() {
        // Toolbar events
        document.getElementById('auto-scroll').addEventListener('change', (e) => {
            autoScroll = e.target.checked;
        });

        document.getElementById('btn-clear-log').addEventListener('click', clearLog);
        document.getElementById('btn-export-log').addEventListener('click', exportLog);
        document.getElementById('log-filter').addEventListener('change', applyFilter);

        // Add wheel event listener for horizontal scrolling on terminal tabs
        document.getElementById('terminal-tabs').addEventListener('wheel', (e) => {
            if (e.deltaY !== 0) {
                e.preventDefault();
                e.currentTarget.scrollLeft += e.deltaY;
            }
        });
    }

    async function refresh() {
        try {
            services = await apiGet('/api/services');
            renderTabs();
            // Auto-connect if not connected
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                connectWs(activeServiceId);
            }
        } catch (e) {
            // retry next poll
        }
    }

    function renderTabs() {
        const container = document.getElementById('terminal-tabs');
        container.innerHTML = '';

        // "All" tab
        const allTab = createTab('all', '📋 全部合并');
        container.appendChild(allTab);

        for (const s of services) {
            const tab = createTab(s.id, s.name);
            container.appendChild(tab);
        }
    }

    function createTab(id, label) {
        const btn = document.createElement('button');
        btn.className = `terminal-tab ${id === activeServiceId ? 'active' : ''}`;
        btn.textContent = label;
        btn.addEventListener('click', () => {
            activeServiceId = id;
            document.querySelectorAll('.terminal-tab').forEach(t => t.classList.remove('active'));
            btn.classList.add('active');
            clearLog();
            connectWs(id);
        });
        return btn;
    }

    function connectWs(serviceId) {
        // Close existing
        if (ws) {
            ws.close();
            ws = null;
        }

        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws/logs/${serviceId}`;

        ws = new WebSocket(url);

        ws.onopen = () => {
            appendSystemLine(`已连接到 ${serviceId} 的日志流`);
        };

        ws.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.type === 'history') {
                    for (const line of msg.lines) {
                        appendLogLine(line, serviceId === 'all');
                    }
                } else if (msg.type === 'log') {
                    appendLogLine(msg.line, serviceId === 'all');
                }
                // Ignore pings
            } catch (e) {
                appendLogLine(evt.data, false);
            }
        };

        ws.onclose = () => {
            appendSystemLine('连接已断开');
        };

        ws.onerror = () => {
            appendSystemLine('WebSocket 连接失败');
        };
    }

    function appendLogLine(text, showTag) {
        const output = document.getElementById('terminal-output');

        // Remove placeholder
        const ph = output.querySelector('.terminal-placeholder');
        if (ph) ph.remove();

        // Determine log level
        let level = 'info';
        const upper = text.toUpperCase();
        if (upper.includes('ERROR') || upper.includes('FATAL') || upper.includes('CRITICAL')) level = 'error';
        else if (upper.includes('WARNING') || upper.includes('WARN')) level = 'warning';
        else if (upper.includes('DEBUG')) level = 'debug';

        // Check filter
        const filter = document.getElementById('log-filter').value;
        if (filter !== 'all') {
            const levels = { 'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3 };
            if ((levels[level.toUpperCase()] || 0) < (levels[filter] || 0)) return;
        }

        const line = document.createElement('div');
        line.className = `log-line ${level}`;

        let html = '';
        if (showTag) {
            // Extract tag from "[service_id] ..." format
            const match = text.match(/^\[([^\]]+)\]\s*/);
            if (match) {
                const tag = match[1];
                html += `<span class="log-tag ${tag}">${escapeHtml(tag)}</span>`;
                text = text.substring(match[0].length);
            }
        }

        html += escapeHtml(text);
        line.innerHTML = html;
        output.appendChild(line);

        // Limit buffer (keep last 5000 lines in DOM)
        while (output.children.length > 5000) {
            output.removeChild(output.firstChild);
        }

        if (autoScroll) {
            output.scrollTop = output.scrollHeight;
        }
    }

    function appendSystemLine(text) {
        const output = document.getElementById('terminal-output');
        const ph = output.querySelector('.terminal-placeholder');
        if (ph) ph.remove();

        const line = document.createElement('div');
        line.className = 'log-line';
        line.style.color = '#5eead4';
        line.style.fontStyle = 'italic';
        line.textContent = `── ${text} ──`;
        output.appendChild(line);

        if (autoScroll) {
            output.scrollTop = output.scrollHeight;
        }
    }

    function clearLog() {
        const output = document.getElementById('terminal-output');
        output.innerHTML = '';
    }

    function exportLog() {
        const output = document.getElementById('terminal-output');
        const lines = Array.from(output.querySelectorAll('.log-line')).map(el => el.textContent);
        const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `nachobot_log_${activeServiceId}_${new Date().toISOString().slice(0,19).replace(/:/g,'-')}.txt`;
        a.click();
        URL.revokeObjectURL(url);
        toast('日志已导出', 'success');
    }

    function applyFilter() {
        // Re-render is handled by appendLogLine filter check
        // For existing lines, show/hide based on filter
        const filter = document.getElementById('log-filter').value;
        const levels = { 'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3 };
        const minLevel = levels[filter] || 0;

        document.querySelectorAll('#terminal-output .log-line').forEach(el => {
            let elLevel = 1; // default: info
            if (el.classList.contains('debug')) elLevel = 0;
            else if (el.classList.contains('warning')) elLevel = 2;
            else if (el.classList.contains('error')) elLevel = 3;

            el.style.display = (filter === 'all' || elLevel >= minLevel) ? '' : 'none';
        });
    }

    return { init, refresh };
})();
