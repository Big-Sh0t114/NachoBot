/**
 * NachoBot WebUI — Launcher Module
 * Manages service groups: start/stop with status display.
 */

const LauncherModule = (() => {
    let groups = [];
    let launch = null;
    let selectedProfile = 'lite';
    let pollInterval = null;

    const LAUNCH_PROFILES = Object.freeze({
        full: {
            name: '完整模式',
            code: 'FULL',
            tag: '完整功能',
            summary: 'TTS · VLM · ASR',
            detail: '完整本地多模态能力',
            resource: '高资源占用',
        },
        lite: {
            name: '轻量模式',
            code: 'LITE',
            tag: '推荐',
            summary: 'TTS',
            detail: '保留语音合成，关闭 VLM / ASR',
            resource: '中等资源占用',
        },
        potato: {
            name: '无模型模式',
            code: 'POTATO',
            tag: '低配置',
            summary: 'Relay only',
            detail: '仅消息中继，不加载本地模型',
            resource: '极低资源占用',
        },
    });

    const HIDDEN_LAUNCH_GROUPS = new Set(['core', 'tts_full', 'tts_lite', 'potato']);

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
        // Temporarily hidden from WebUI; restore when VRChat is exposed.
        // vrchat: 'orbit',
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
            [launch, groups] = await Promise.all([
                apiGet('/api/launch'),
                apiGet('/api/groups'),
            ]);
            if (launch.active_profile) {
                selectedProfile = launch.active_profile;
            }
            render();
        } catch (e) {
            // Will retry on next poll
        }
    }

    function render() {
        const grid = document.getElementById('launcher-grid');
        const visibleGroups = groups.filter(g => !HIDDEN_LAUNCH_GROUPS.has(g.id));
        const currentIds = new Set(['nachobot-launch', ...visibleGroups.map(g => g.id)]);

        for (const card of [...grid.children]) {
            if (!currentIds.has(card.dataset.groupId)) {
                card.remove();
            }
        }

        let launchCard = grid.querySelector('[data-group-id="nachobot-launch"]');
        if (!launchCard) {
            launchCard = document.createElement('div');
            launchCard.className = 'group-card launch-card';
            launchCard.dataset.groupId = 'nachobot-launch';
            grid.prepend(launchCard);
        }
        updateLaunchCard(launchCard);

        for (const g of visibleGroups) {
            let card = grid.querySelector(`[data-group-id="${g.id}"]`);
            if (card) {
                updateGroupCard(card, g);
            } else {
                card = createGroupCard(g);
                grid.appendChild(card);
            }
        }
    }

    function launchBadge(status) {
        if (status === 'running') return ['running', '运行中'];
        if (status === 'starting') return ['starting', '启动中'];
        if (status === 'stopping') return ['partial', '停止中'];
        if (status === 'error') return ['error', '错误'];
        if (status === 'partial') return ['partial', '部分运行'];
        return ['stopped', '已停止'];
    }

    function launchServiceProgress(state, activeProfile) {
        const profile = (state.profiles || []).find(item => item.id === activeProfile);
        const services = [state.core, ...(profile?.services || [])].filter(Boolean);
        if (!services.length || state.status === 'stopped') return '';

        const statusText = {
            stopped: '等待',
            starting: '启动中',
            running: '已就绪',
            stopping: '停止中',
            error: '错误',
        };

        return `
            <div class="launch-progress">
                <div class="launch-progress-title">启动单元状态</div>
                <div class="launch-progress-list">
                    ${services.map(service => `
                        <div class="launch-progress-row">
                            <div class="service-info">
                                <div class="service-dot ${service.status}"></div>
                                <div class="service-text">
                                    <span class="service-name">${escapeHtml(service.name)}</span>
                                    ${service.detail ? `<span class="service-detail">${escapeHtml(service.detail)}</span>` : ''}
                                </div>
                            </div>
                            <span class="launch-progress-status ${service.status}">${statusText[service.status] || service.status}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    function launchCardInnerHTML() {
        const state = launch || { status: 'stopped', core: { status: 'stopped' }, profiles: [] };
        const [badgeClass, badgeText] = launchBadge(state.status);
        const busy = state.status === 'starting' || state.status === 'stopping';
        const running = state.status === 'running';
        const canStop = state.status !== 'stopped';
        const activeProfile = state.active_profile || selectedProfile;
        const profileStates = new Map((state.profiles || []).map(profile => [profile.id, profile.status]));

        const profileMarkup = Object.entries(LAUNCH_PROFILES).map(([id, profile]) => {
            const selected = selectedProfile === id;
            const active = activeProfile === id && state.status !== 'stopped';
            const profileStatus = profileStates.get(id) || 'stopped';
            const disabled = busy || running;
            return `
                <button class="launch-profile ${selected ? 'selected' : ''} ${active ? 'active' : ''}"
                        type="button"
                        data-launch-profile="${id}"
                        ${disabled ? 'disabled' : ''}>
                    <div class="launch-profile-topline">
                        <span class="launch-profile-radio" aria-hidden="true"></span>
                        <span class="launch-profile-name">${profile.name}</span>
                        <span class="launch-profile-code">${profile.code}</span>
                        <span class="launch-profile-tag">${profile.tag}</span>
                    </div>
                    <div class="launch-profile-summary">${profile.summary}</div>
                    <div class="launch-profile-detail">${profile.detail}</div>
                    <div class="launch-profile-resource">${profile.resource}</div>
                    ${active ? `<div class="launch-profile-state ${profileStatus}">当前模式</div>` : ''}
                </button>
            `;
        }).join('');

        const selectedMeta = LAUNCH_PROFILES[selectedProfile];
        return `
            <div class="group-card-header launch-card-header">
                <div class="group-info">
                    <span class="group-icon">${svgIcon('brain')}</span>
                    <div>
                        <div class="group-name">NachoBot</div>
                        <div class="group-detail">Core 消息总线 + Multimodal Runtime</div>
                    </div>
                </div>
                <span class="group-status-badge ${badgeClass}">${badgeText}</span>
            </div>
            <div class="launch-card-body">
                <div class="launch-profile-heading">
                    <div>
                        <div class="launch-profile-title">运行模式</div>
                        <div class="launch-profile-hint">三种模式互斥，只能选择一种与 Core 一同启动</div>
                    </div>
                    <span class="launch-exclusive-badge">三选一</span>
                </div>
                <div class="launch-profile-grid">${profileMarkup}</div>
                <div class="launch-selection-summary">
                    已选择 <strong>${selectedMeta.name}</strong> · ${selectedMeta.code}
                </div>
                ${launchServiceProgress(state, activeProfile)}
            </div>
            <div class="group-card-footer launch-card-footer">
                ${running ? `
                    <button class="btn btn-outline btn-full" id="btn-launch-change">停止并切换模式</button>
                ` : `
                    <button class="btn btn-primary btn-full" id="btn-launch-start" ${busy ? 'disabled' : ''}>
                        ${svgIcon('play')}启动 NachoBot
                    </button>
                `}
                ${canStop ? `
                    <button class="btn btn-danger" id="btn-launch-stop">
                        ${svgIcon('square')}${state.status === 'starting' ? '取消启动' : '停止 NachoBot'}
                    </button>
                ` : ''}
            </div>
        `;
    }

    function updateLaunchCard(card) {
        const newHTML = launchCardInnerHTML();
        if (card._lastHTML !== newHTML) {
            card._lastHTML = newHTML;
            card.innerHTML = newHTML;
            bindLaunchEvents(card);
        }
    }

    function bindLaunchEvents(card) {
        card.querySelectorAll('[data-launch-profile]').forEach(button => {
            button.addEventListener('click', () => {
                selectedProfile = button.dataset.launchProfile;
                updateLaunchCard(card);
            });
        });

        card.querySelector('#btn-launch-start')?.addEventListener('click', startLaunch);
        card.querySelector('#btn-launch-stop')?.addEventListener('click', stopLaunch);
        card.querySelector('#btn-launch-change')?.addEventListener('click', async () => {
            try {
                await apiPost('/api/launch/stop');
                toast('正在停止当前模式，停止后即可选择新的运行模式', 'info');
                refresh();
            } catch (e) {
                toast(`切换准备失败: ${e.message}`, 'error');
            }
        });
    }

    async function startLaunch() {
        try {
            await apiPost('/api/launch/start', { profile: selectedProfile });
            toast(`${LAUNCH_PROFILES[selectedProfile].code} 模式正在启动...`, 'info');
            refresh();
        } catch (e) {
            toast(`启动失败: ${e.message}`, 'error');
        }
    }

    async function stopLaunch() {
        try {
            await apiPost('/api/launch/stop');
            toast(launch?.status === 'starting' ? '正在取消启动...' : '正在停止 NachoBot...', 'info');
            refresh();
        } catch (e) {
            toast(`停止失败: ${e.message}`, 'error');
        }
    }

    /** Generate the inner HTML for a group card (without the wrapper div). */
    function groupCardInnerHTML(group) {
        const runCount = group.services.filter(s => s.status === 'running').length;
        const errCount = group.services.filter(s => s.status === 'error').length;
        const total = group.services.length;

        const anyStarting = group.services.some(s => s.status === 'starting');

        let badgeClass = 'stopped';
        let badgeText = '已停止';
        if (errCount > 0) { badgeClass = 'error'; badgeText = '错误'; }
        else if (anyStarting) { badgeClass = 'starting'; badgeText = '启动中'; }
        else if (runCount === total) { badgeClass = 'running'; badgeText = '运行中'; }
        else if (runCount > 0) { badgeClass = 'partial'; badgeText = `${runCount}/${total}`; }

        const anyRunning = runCount > 0;
        const canStop = anyRunning || anyStarting;
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
                        ${anyStarting ? 'disabled' : ''}>
                    ${primaryActionIcon}${anyRunning ? '重启组' : '启动组'}
                </button>
                ${canStop ? `
                    <button class="btn btn-danger" id="btn-stop-${group.id}">
                        ${stopIcon}${anyStarting && !anyRunning ? '取消启动' : '停止'}
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
