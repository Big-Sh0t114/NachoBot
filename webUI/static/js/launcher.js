/**
 * NachoBot WebUI — Launcher Module
 * Manages service groups: start/stop with status display.
 */

const LauncherModule = (() => {
    let groups = [];
    let pollInterval = null;

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
        grid.innerHTML = '';

        for (const g of groups) {
            grid.appendChild(createGroupCard(g));
        }
    }

    function createGroupCard(group) {
        const card = document.createElement('div');
        card.className = 'group-card';

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

        card.innerHTML = `
            <div class="group-card-header">
                <div class="group-info">
                    <span class="group-icon">${group.icon}</span>
                    <div>
                        <div class="group-name">${escapeHtml(group.name)}</div>
                    </div>
                </div>
                <span class="group-status-badge ${badgeClass}">${badgeText}</span>
            </div>
            <div class="group-card-body">
                ${group.services.map(s => `
                    <div class="service-row">
                        <div class="service-info">
                            <div class="service-dot ${s.status}"></div>
                            <span class="service-name">${escapeHtml(s.name)}</span>
                        </div>
                        <span class="service-port">${s.port ? ':' + s.port : ''}</span>
                    </div>
                `).join('')}
            </div>
            <div class="group-card-footer">
                <button class="btn ${anyRunning ? 'btn-outline' : 'btn-primary'} btn-full"
                        id="btn-start-${group.id}"
                        ${allStarting ? 'disabled' : ''}>
                    ${anyRunning ? '⟳ 重启组' : '▶ 启动组'}
                </button>
                ${anyRunning ? `
                    <button class="btn btn-danger" id="btn-stop-${group.id}">
                        ■ 停止
                    </button>
                ` : ''}
            </div>
        `;

        // Bind events
        const startBtn = card.querySelector(`#btn-start-${group.id}`);
        startBtn.addEventListener('click', () => startGroup(group.id, anyRunning));

        const stopBtn = card.querySelector(`#btn-stop-${group.id}`);
        if (stopBtn) {
            stopBtn.addEventListener('click', () => stopGroup(group.id));
        }

        return card;
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
