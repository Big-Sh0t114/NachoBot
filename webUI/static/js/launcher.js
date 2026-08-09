/**
 * NachoBot WebUI — Launcher Module
 * Manages service groups: start/stop with status display.
 */

const LauncherModule = (() => {
    let groups = [];
    let pollInterval = null;

    const GROUP_ICON_NAMES = Object.freeze({
        core: 'brain',
        qq_adapter: 'message-circle',
        tts_full: 'audio-lines',
        tts_lite: 'audio-lines',
        potato: 'cpu',
        bilibili: 'monitor-play',
        live2d: 'monitor-play',
        discord: 'message-circle',
        universalvc: 'microphone',
    });

    function svgIcon(name) {
        return typeof window.NachoIcon === 'function' ? window.NachoIcon(name) : '';
    }

    function init() {
        refresh();
        // Poll every 2s when launcher tab is active
        pollInterval = setInterval(() => {
            if (document.getElementById('tab-launcher').classList.contains('active')) {
                refresh();
            }
        }, 2000);
    }

    async function refresh() {
        try {
            groups = await apiGet('/api/groups');
            render();
        } catch (e) {
            // Will retry on next poll
        }
    }

    function render() {
        const grid = document.getElementById('launcher-grid');

        // Build a set of current group IDs for cleanup
        const currentIds = new Set(groups.map(g => g.id));

        // Remove cards for groups that no longer exist
        for (const card of [...grid.children]) {
            if (!currentIds.has(card.dataset.groupId)) {
                card.remove();
            }
        }

        // Update or create cards
        for (const g of groups) {
            let card = grid.querySelector(`[data-group-id="${g.id}"]`);
            if (card) {
                updateGroupCard(card, g);
            } else {
                card = createGroupCard(g);
                grid.appendChild(card);
            }
        }
    }

    /** Generate the inner HTML for a group card (without the wrapper div). */
    function groupCardInnerHTML(group) {
        const runCount = group.services.filter(s => s.status === 'running').length;
        const errCount = group.services.filter(s => s.status === 'error').length;
        const total = group.services.length;

        let badgeClass = 'stopped';
        let badgeText = '已停止';
        if (errCount > 0) { badgeClass = 'error'; badgeText = '错误'; }
        else if (runCount === total) { badgeClass = 'running'; badgeText = '运行中'; }
        else if (runCount > 0) { badgeClass = 'partial'; badgeText = `${runCount}/${total}`; }

        const anyRunning = runCount > 0;
        const allStarting = group.services.some(s => s.status === 'starting');
        const groupIcon = svgIcon(GROUP_ICON_NAMES[group.id] || 'component');
        const primaryActionIcon = svgIcon(anyRunning ? 'rotate-ccw' : 'play');
        const stopIcon = svgIcon('square');

        return `
            <div class="group-card-header">
                <div class="group-info">
                    <span class="group-icon">${groupIcon}</span>
                    <div>
                        <div class="group-name">${escapeHtml(group.name)}</div>
                        ${group.detail ? `<div class="group-detail">${escapeHtml(group.detail)}</div>` : ''}
                    </div>
                </div>
                <span class="group-status-badge ${badgeClass}">${badgeText}</span>
            </div>
            <div class="group-card-body">
                ${group.services.map(s => `
                    <div class="service-row">
                        <div class="service-info">
                            <div class="service-dot ${s.status}"></div>
                            <div class="service-text">
                                <span class="service-name">${escapeHtml(s.name)}</span>
                                ${s.detail ? `<span class="service-detail">${escapeHtml(s.detail)}</span>` : ''}
                            </div>
                        </div>
                        ${!s.detail && s.port ? `<span class="service-port">:${s.port}</span>` : ''}
                    </div>
                `).join('')}
            </div>
            <div class="group-card-footer">
                <button class="btn ${anyRunning ? 'btn-outline' : 'btn-primary'} btn-full"
                        id="btn-start-${group.id}"
                        ${allStarting ? 'disabled' : ''}>
                    ${primaryActionIcon}${anyRunning ? '重启组' : '启动组'}
                </button>
                ${anyRunning ? `
                    <button class="btn btn-danger" id="btn-stop-${group.id}">
                        ${stopIcon}停止
                    </button>
                ` : ''}
            </div>
        `;
    }

    /** Update an existing card's content in-place (preserves hover state). */
    function updateGroupCard(card, group) {
        const newHTML = groupCardInnerHTML(group);
        // Only touch the DOM if something actually changed
        if (card._lastHTML !== newHTML) {
            card._lastHTML = newHTML;
            card.innerHTML = newHTML;
            bindCardEvents(card, group);
        }
    }

    function createGroupCard(group) {
        const card = document.createElement('div');
        card.className = 'group-card';
        card.dataset.groupId = group.id;
        card._lastHTML = groupCardInnerHTML(group);
        card.innerHTML = card._lastHTML;
        bindCardEvents(card, group);
        return card;
    }

    function bindCardEvents(card, group) {
        const anyRunning = group.services.some(s => s.status === 'running');

        const startBtn = card.querySelector(`#btn-start-${group.id}`);
        if (startBtn) {
            startBtn.addEventListener('click', () => startGroup(group.id, anyRunning));
        }

        const stopBtn = card.querySelector(`#btn-stop-${group.id}`);
        if (stopBtn) {
            stopBtn.addEventListener('click', () => stopGroup(group.id));
        }
    }

    async function startGroup(groupId, isRestart) {
        try {
            if (isRestart) {
                await apiPost(`/api/groups/${groupId}/stop`);
                // Wait a bit before restarting
                setTimeout(async () => {
                    await apiPost(`/api/groups/${groupId}/start`);
                    toast(`${groupId} 正在启动...`, 'info');
                    refresh();
                }, 3000);
            } else {
                await apiPost(`/api/groups/${groupId}/start`);
                toast('正在启动...', 'info');
            }
            refresh();
        } catch (e) {
            toast(`启动失败: ${e.message}`, 'error');
        }
    }

    async function stopGroup(groupId) {
        try {
            await apiPost(`/api/groups/${groupId}/stop`);
            toast('正在停止...', 'info');
            refresh();
        } catch (e) {
            toast(`停止失败: ${e.message}`, 'error');
        }
    }

    return { init, refresh };
})();
