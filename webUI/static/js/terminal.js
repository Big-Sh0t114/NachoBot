/**
 * NachoBot WebUI — Terminal Module
 * Real-time log streaming via WebSocket with history replay.
 */

const TerminalModule = (() => {
    let services = [];
    let activeServiceId = 'all';
    let ws = null;
    let autoScroll = true;
    // NapCat prints QR codes as Unicode half-block art. Browser text layout
    // introduces gaps between those glyphs, so retain the raw rows briefly and
    // redraw them as a pixel-perfect canvas once the QR URL marker arrives.
    const napCatQrCaptures = new Map();
    const NAPCAT_SHELL_ID = 'napcat_shell';
    const NAPCAT_QR_START_RE = /请扫描下面的二维码/;
    const NAPCAT_QR_END_RE = /二维码解码\s*URL\s*:/i;
    const NAPCAT_QR_ART_RE = /^[█▀▄ ]+$/;
    const NAPCAT_QR_ART_START_RE = /^▄{23,}$/;
    const NAPCAT_QR_CELLS = {
        '█': [false, false],
        '▀': [false, true],
        '▄': [true, false],
        ' ': [true, true],
    };

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
        napCatQrCaptures.clear();

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
                        checkEulaPrompt(line, serviceId);
                    }
                } else if (msg.type === 'log') {
                    appendLogLine(msg.line, serviceId === 'all');
                    checkEulaPrompt(msg.line, serviceId);
                }
                // Ignore pings
            } catch (e) {
                appendLogLine(evt.data, false);
                checkEulaPrompt(evt.data, serviceId);
            }
        };

        ws.onclose = () => {
            appendSystemLine('连接已断开');
        };

        ws.onerror = () => {
            appendSystemLine('WebSocket 连接失败');
        };
    }

    function appendLogLine(text, showTag, skipQrHandling = false) {
        const output = document.getElementById('terminal-output');
        text = String(text ?? '');

        // Remove placeholder
        const ph = output.querySelector('.terminal-placeholder');
        if (ph) ph.remove();

        let tag = null;
        if (showTag) {
            // Extract tag from "[service_id] ..." format before inspecting
            // special log output so the merged terminal view works as well.
            const match = text.match(/^\[([^\]]+)\]\s*/);
            if (match) {
                tag = match[1];
                text = text.substring(match[0].length);
            }
        }

        const sourceId = tag || (activeServiceId === 'all' ? null : activeServiceId);
        if (!skipQrHandling && sourceId === NAPCAT_SHELL_ID) {
            if (NAPCAT_QR_START_RE.test(text)) {
                napCatQrCaptures.set(sourceId, { lines: [], showTag, tag });
            } else if (isNapCatQrArtLine(text)) {
                // The preceding warning line can be outside of a replayed log
                // window. NapCat's all-▄ top border is unambiguous, so use it
                // as a second capture trigger instead of rendering it as text.
                if (!napCatQrCaptures.has(sourceId) && isNapCatQrArtStart(text)) {
                    napCatQrCaptures.set(sourceId, { lines: [], showTag, tag });
                }
                const capture = napCatQrCaptures.get(sourceId);
                if (capture) {
                    // Do not place the font-rendered version in the DOM: its glyph
                    // spacing corrupts the QR code before a scanner can see it.
                    capture.lines.push(stripLineEnding(text));
                    return;
                }
            } else if (napCatQrCaptures.has(sourceId) && NAPCAT_QR_END_RE.test(text)) {
                const capture = napCatQrCaptures.get(sourceId);
                napCatQrCaptures.delete(sourceId);
                if (!appendNapCatQr(capture)) {
                    // Keep the original output available if an upstream format
                    // change ever prevents us from decoding the character art.
                    appendNapCatQrFallback(capture);
                }
            }
        }

        // Determine log level
        let level = 'info';
        const upper = text.toUpperCase();
        if (upper.includes('ERROR') || upper.includes('FATAL') || upper.includes('CRITICAL')) level = 'error';
        else if (upper.includes('WARNING') || upper.includes('WARN')) level = 'warning';
        else if (upper.includes('DEBUG')) level = 'debug';

        const line = document.createElement('div');
        line.className = `log-line ${level}`;

        // Check filter — hide (not discard) lines below threshold
        const filter = document.getElementById('log-filter').value;
        if (filter !== 'all') {
            const levels = { 'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3 };
            if ((levels[level.toUpperCase()] || 0) < (levels[filter] || 0)) {
                line.style.display = 'none';
            }
        }

        if (tag) {
            const tagEl = document.createElement('span');
            tagEl.className = `log-tag ${sanitizeLogClassToken(tag)}`;
            tagEl.textContent = tag;
            line.appendChild(tagEl);
        }

        line.appendChild(document.createTextNode(text));
        output.appendChild(line);

        // Limit buffer (keep last 5000 lines in DOM)
        while (output.children.length > 5000) {
            output.removeChild(output.firstChild);
        }

        if (autoScroll) {
            output.scrollTop = output.scrollHeight;
        }
    }

    function sanitizeLogClassToken(value) {
        const token = String(value || '')
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, '-')
            .replace(/^-+|-+$/g, '');
        return token || 'unknown';
    }

    function stripLineEnding(text) {
        return text.replace(/[\r\n]+$/, '');
    }

    function isNapCatQrArtLine(text) {
        const line = stripLineEnding(text);
        return line.length >= 3 && NAPCAT_QR_ART_RE.test(line) && /[█▀▄]/.test(line);
    }

    function isNapCatQrArtStart(text) {
        return NAPCAT_QR_ART_START_RE.test(stripLineEnding(text));
    }

    function decodeNapCatQr(lines) {
        if (lines.length < 2) return null;

        const lineWidth = lines[0].length;
        const moduleCount = lineWidth - 2; // NapCat adds one white cell at each side.
        if (moduleCount < 21 || (moduleCount - 17) % 4 !== 0) return null;

        const matrix = [];
        // The first line is NapCat's terminal-only top border. Every remaining
        // character row contains two QR matrix rows using half-block glyphs.
        for (const line of lines.slice(1)) {
            if (line.length !== lineWidth) return null;
            const encodedCells = line.slice(1, -1);
            const topRow = [];
            const bottomRow = [];

            for (const cell of encodedCells) {
                const rows = NAPCAT_QR_CELLS[cell];
                if (!rows) return null;
                topRow.push(rows[0]);
                bottomRow.push(rows[1]);
            }

            matrix.push(topRow);
            if (matrix.length < moduleCount) matrix.push(bottomRow);
            if (matrix.length >= moduleCount) break;
        }

        return matrix.length === moduleCount ? matrix : null;
    }

    function appendNapCatQr(capture) {
        const matrix = decodeNapCatQr(capture.lines);
        if (!matrix) return false;

        const quietZone = 4;
        const modulePixels = 8;
        const moduleCount = matrix.length;
        const canvasModules = moduleCount + quietZone * 2;
        const canvas = document.createElement('canvas');
        canvas.className = 'terminal-qr-canvas';
        canvas.width = canvasModules * modulePixels;
        canvas.height = canvasModules * modulePixels;
        canvas.setAttribute('role', 'img');
        canvas.setAttribute('aria-label', 'QQ 登录二维码');

        const context = canvas.getContext('2d');
        if (!context) return false;
        context.fillStyle = '#ffffff';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = '#000000';
        matrix.forEach((row, y) => {
            row.forEach((isDark, x) => {
                if (isDark) {
                    context.fillRect(
                        (x + quietZone) * modulePixels,
                        (y + quietZone) * modulePixels,
                        modulePixels,
                        modulePixels,
                    );
                }
            });
        });

        const card = document.createElement('div');
        card.className = 'terminal-qr';
        card.appendChild(canvas);
        document.getElementById('terminal-output').appendChild(card);

        if (autoScroll) {
            const output = document.getElementById('terminal-output');
            output.scrollTop = output.scrollHeight;
        }
        return true;
    }

    function appendNapCatQrFallback(capture) {
        for (const line of capture.lines) {
            const taggedLine = capture.tag ? `[${capture.tag}] ${line}` : line;
            appendLogLine(taggedLine, capture.showTag, true);
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
        napCatQrCaptures.clear();
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

    function checkEulaPrompt(text, currentServiceId) {
        if (text && (text.includes('同意') || text.includes('confirmed') || text.includes('EULA或隐私条款内容已更新'))) {
            if (text.includes('请输入') || text.includes('继续运行视为同意')) {
                showEulaButton(currentServiceId);
            }
        }
    }

    function showEulaButton(currentServiceId) {
        let btn = document.getElementById('btn-eula-confirm');
        if (!btn) {
            btn = document.createElement('button');
            btn.id = 'btn-eula-confirm';
            btn.className = 'btn-sm';
            btn.style.backgroundColor = '#10b981';
            btn.style.color = '#fff';
            btn.style.border = 'none';
            btn.innerHTML = '✅ 同意 EULA (发送 confirmed)';
            btn.onclick = async () => {
                try {
                    const targetService = currentServiceId === 'all' ? 'nachobot' : currentServiceId;
                    await apiPost(`/api/services/${targetService}/input`, { text: 'confirmed\n' });
                    btn.remove();
                    toast('已发送协议确认指令', 'success');
                } catch (e) {
                    toast('发送失败: ' + e.message, 'error');
                }
            };
            const toolbar = document.querySelector('.terminal-toolbar .toolbar-actions');
            if (toolbar) {
                toolbar.prepend(btn);
            }
        }
    }

    return { init, refresh };
})();
