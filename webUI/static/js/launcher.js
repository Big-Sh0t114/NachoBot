/**
 * NachoBot WebUI — Launcher Module
 * Manages service groups: start/stop with status display.
 */

const LauncherModule = (() => {
    let groups = [];
    let launch = null;
    let runtimeStatus = null;
    let selectedProfile = 'lite';
    let selectedRuntime = 'gpu';
    let runtimeSelectionTouched = false;
    let installingRuntime = null;
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
            [launch, groups, runtimeStatus] = await Promise.all([
                apiGet('/api/launch'),
                apiGet('/api/groups'),
                apiGet('/api/setup/deps/multimodal/status'),
            ]);
            if (launch.active_profile) {
                selectedProfile = launch.active_profile;
            }
            if (launch.active_profile && launch.runtime && launch.runtime !== 'relay') {
                selectedRuntime = launch.runtime;
            } else if (!runtimeSelectionTouched) {
                const recommended = runtimeStatus?.recommended === 'cpu' ? 'cpu' : 'gpu';
                const runtimes = runtimeStatus?.runtimes || {};
                if (runtimes[recommended]?.installed) {
                    selectedRuntime = recommended;
                } else if (runtimes[recommended === 'gpu' ? 'cpu' : 'gpu']?.installed) {
                    selectedRuntime = recommended === 'gpu' ? 'cpu' : 'gpu';
                } else {
                    selectedRuntime = recommended;
                }
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
        const runtimes = runtimeStatus?.runtimes || {};
        const recommendedRuntime = runtimeStatus?.recommended === 'cpu' ? 'cpu' : 'gpu';
        let requiredRuntime = selectedRuntime;
        if (selectedProfile === 'potato') {
            if (runtimes.relay?.installed) {
                requiredRuntime = 'relay';
            } else if (runtimes[selectedRuntime]?.installed) {
                requiredRuntime = selectedRuntime;
            } else if (runtimes[selectedRuntime === 'gpu' ? 'cpu' : 'gpu']?.installed) {
                requiredRuntime = selectedRuntime === 'gpu' ? 'cpu' : 'gpu';
            } else {
                requiredRuntime = null;
            }
        }
        const requiredInstalled = Boolean(requiredRuntime && runtimes[requiredRuntime]?.installed);
        const runtimeMeta = {
            gpu: { name: 'GPU / CUDA', detail: 'CUDA 版 PyTorch 与本地模型依赖' },
            cpu: { name: 'CPU', detail: 'CPU 版 PyTorch 与本地模型依赖' },
            relay: { name: 'Relay / POTATO', detail: '仅消息中继，不安装本地模型栈' },
        };
        const runtimeMarkup = ['gpu', 'cpu'].map(id => {
            const status = runtimes[id] || { installed: false };
            const isRequired = requiredRuntime === id;
            const selectable = selectedProfile !== 'potato' && !busy && !running;
            const selected = selectedProfile === 'potato' ? false : selectedRuntime === id;
            const installing = installingRuntime === id;
            const recommendation = id === recommendedRuntime && id !== 'relay' ? '<span class="launch-profile-tag">推荐</span>' : '';
            return `
                <div class="launch-runtime-item ${selected ? 'selected' : ''} ${isRequired ? 'required' : ''}">
                    <button type="button" class="launch-runtime-select"
                            data-launch-runtime="${id}"
                            ${selectable ? '' : 'disabled'}>
                        <span class="launch-profile-radio" aria-hidden="true"></span>
                        <span class="launch-runtime-copy">
                            <strong>${runtimeMeta[id].name}</strong>
                            <small>${runtimeMeta[id].detail}</small>
                        </span>
                        ${recommendation}
                    </button>
                    <div class="launch-runtime-state ${status.installed ? 'installed' : 'missing'}">
                        <span>${status.installed ? '✓ 已安装' : '未安装'}</span>
                        ${status.installed ? '' : `
                            <button type="button" class="btn btn-primary btn-sm launch-runtime-install"
                                    data-install-runtime="${id}"
                                    ${installingRuntime ? 'disabled' : ''}>
                                ${installing ? '安装中...' : '安装 / 补齐'}
                            </button>
                        `}
                    </div>
                </div>
            `;
        }).join('');
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
                    · 运行环境 <strong>${requiredRuntime ? runtimeMeta[requiredRuntime].name : '不可用'}</strong>
                </div>
                <div class="launch-runtime-section">
                    <div class="launch-profile-heading">
                        <div>
                            <div class="launch-profile-title">Multimodal 运行环境</div>
                            <div class="launch-profile-hint">FULL/LITE 可在 GPU 与 CPU 间选择；POTATO 使用首次部署时预装的轻量 Relay 环境</div>
                        </div>
                    </div>
                    <div class="launch-runtime-grid">${runtimeMarkup}</div>
                </div>
                ${!requiredInstalled ? `<div class="launch-runtime-warning">${selectedProfile === 'potato' ? 'POTATO Relay 环境不可用，且没有已安装的 GPU/CPU Multimodal 环境。' : `当前模式所需的 ${runtimeMeta[requiredRuntime].name} 环境尚未安装，请先点击“安装 / 补齐”。`}</div>` : ''}
                ${launchServiceProgress(state, activeProfile)}
            </div>
            <div class="group-card-footer launch-card-footer">
                ${running ? `
                    <button class="btn btn-outline btn-full" id="btn-launch-change">停止并切换模式</button>
                ` : `
                    <button class="btn btn-primary btn-full" id="btn-launch-start" ${(busy || !requiredInstalled || Boolean(installingRuntime)) ? 'disabled' : ''}>
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

        card.querySelectorAll('[data-launch-runtime]').forEach(button => {
            button.addEventListener('click', () => {
                const runtime = button.dataset.launchRuntime;
                if (runtime !== 'gpu' && runtime !== 'cpu') return;
                selectedRuntime = runtime;
                runtimeSelectionTouched = true;
                updateLaunchCard(card);
            });
        });
        card.querySelectorAll('[data-install-runtime]').forEach(button => {
            button.addEventListener('click', () => installRuntime(button.dataset.installRuntime));
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
            const runtimes = runtimeStatus?.runtimes || {};
            let runtime = selectedRuntime;
            if (selectedProfile === 'potato') {
                if (runtimes.relay?.installed) runtime = 'relay';
                else if (runtimes[selectedRuntime]?.installed) runtime = selectedRuntime;
                else if (runtimes[selectedRuntime === 'gpu' ? 'cpu' : 'gpu']?.installed) {
                    runtime = selectedRuntime === 'gpu' ? 'cpu' : 'gpu';
                } else {
                    throw new Error('POTATO Relay 环境不可用，且没有已安装的 GPU/CPU Multimodal 环境');
                }
            }
            await apiPost('/api/launch/start', { profile: selectedProfile, runtime });
            toast(`${LAUNCH_PROFILES[selectedProfile].code} 模式正在启动（${runtime.toUpperCase()}）...`, 'info');
            refresh();
        } catch (e) {
            toast(`启动失败: ${e.message}`, 'error');
        }
    }

    function installRuntimeTask(task) {
        return new Promise((resolve, reject) => {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = createAuthenticatedWebSocket(`${proto}//${location.host}/ws/setup/install`);
            let settled = false;
            const finish = (fn, value) => {
                if (settled) return;
                settled = true;
                try { ws.close(); } catch (_) { /* ignore */ }
                fn(value);
            };
            ws.addEventListener('open', () => {
                ws.send(JSON.stringify({ action: 'install', tasks: [task] }));
            });
            ws.addEventListener('message', event => {
                try {
                    const msg = JSON.parse(event.data);
                    if (msg.type === 'all_done') finish(resolve);
                    else if (msg.type === 'error') finish(reject, new Error(msg.message || '依赖安装失败'));
                    else if (msg.type === 'task_done' && msg.status !== 'ok') {
                        finish(reject, new Error(msg.message || '依赖安装失败'));
                    }
                } catch (e) {
                    finish(reject, e);
                }
            });
            ws.addEventListener('error', () => finish(reject, new Error('依赖安装 WebSocket 连接失败')));
            ws.addEventListener('close', () => {
                if (!settled) finish(reject, new Error('依赖安装连接意外关闭'));
            });
        });
    }

    async function installRuntime(runtime) {
        if (!['gpu', 'cpu', 'relay'].includes(runtime) || installingRuntime) return;
        installingRuntime = runtime;
        render();
        try {
            const tasks = await apiGet(`/api/setup/deps/tasks?components=tts&multimodal_runtime=${encodeURIComponent(runtime)}`);
            const task = tasks.find(item => item.id === 'tts');
            if (!task) throw new Error(`未找到 ${runtime} 环境安装任务`);
            toast(`开始安装 ${runtime.toUpperCase()} Multimodal 环境...`, 'info');
            await installRuntimeTask(task);
            toast(`${runtime.toUpperCase()} Multimodal 环境安装完成`, 'success');
        } catch (e) {
            toast(`环境安装失败: ${e.message}`, 'error');
        } finally {
            installingRuntime = null;
            await refresh();
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
