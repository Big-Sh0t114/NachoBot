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
    let qqAdapterStatus = null;
    let snowlumaProcesses = null;
    let snowlumaProcessError = '';
    let snowlumaProcessBusy = false;
    let snowlumaProcessRefreshToken = null;
    let snowlumaRuntimeRunning = false;
    let snowlumaAgreementModal = null;
    let snowlumaOptimisticStart = false;
    let snowlumaStartRequestInFlight = false;
    let snowlumaOptimisticBaseGroup = null;
    let snowlumaOptimisticStartTimer = null;

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
    const SNOWLUMA_PROCESS_REFRESH_FAILED = 'PROCESS_REFRESH_FAILED';
    const SNOWLUMA_PROCESS_REFRESH_MESSAGE = '操作已提交，但 SnowLuma 实例状态刷新失败，请点击“刷新实例”确认状态';

    function createSnowLumaRefreshFlight() {
        let inFlight = null;
        return async function run(execute, { requireFresh = false } = {}) {
            if (!requireFresh && inFlight) return inFlight;
            if (requireFresh && inFlight) {
                const previous = inFlight;
                try {
                    await previous;
                } catch (_) {
                    // A required refresh still needs its own post-action fetch.
                }
                if (inFlight && inFlight !== previous) return inFlight;
            }
            const current = Promise.resolve().then(execute);
            const tracked = current.finally(() => {
                if (inFlight === tracked) inFlight = null;
            });
            inFlight = tracked;
            return tracked;
        };
    }

    const snowlumaProcessRefreshFlight = createSnowLumaRefreshFlight();

    async function snowlumaApiPost(url, body = {}) {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = payload?.detail;
            const error = new Error(
                typeof detail === 'string'
                    ? detail
                    : detail?.message || `SnowLuma API Error: ${response.status}`,
            );
            error.status = response.status;
            error.code = detail?.code || payload?.code || '';
            error.payload = typeof detail === 'object' && detail ? detail : payload;
            throw error;
        }
        return payload;
    }

    function snowlumaErrorCode(error) {
        return String(error?.code || error?.payload?.code || '').toUpperCase();
    }

    function snowlumaErrorPayload(error) {
        return error?.payload && typeof error.payload === 'object' ? error.payload : {};
    }

    function clearSnowLumaPasswordInput(card) {
        const input = card?.querySelector('#launcher-snowluma-password');
        if (input) input.value = '';
    }

    function focusSnowLumaPasswordInput(card) {
        const input = card?.querySelector('#launcher-snowluma-password');
        if (input) {
            input.focus?.();
            input.select?.();
        }
    }

    function showSnowLumaAgreement(payload) {
        const version = typeof payload?.version === 'string' ? payload.version : '';
        const documents = Array.isArray(payload?.documents) ? payload.documents.slice(0, 4) : [];
        if (!version || !documents.length || !document?.createElement || !document?.body) {
            return Promise.reject(new Error('SnowLuma 协议内容无效'));
        }
        if (snowlumaAgreementModal) snowlumaAgreementModal.resolve(false);
        const overlay = document.createElement('div');
        overlay.className = 'snowluma-agreement-modal';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        const panel = document.createElement('div');
        panel.className = 'snowluma-agreement-dialog';
        const heading = document.createElement('h2');
        heading.className = 'snowluma-agreement-title';
        heading.textContent = '请阅读并同意 SnowLuma 协议';
        panel.appendChild(heading);
        const hint = document.createElement('p');
        hint.className = 'snowluma-agreement-version';
        hint.textContent = `协议版本：${version}`;
        panel.appendChild(hint);
        const body = document.createElement('div');
        body.className = 'snowluma-agreement-documents';
        for (const raw of documents) {
            const documentNode = document.createElement('section');
            documentNode.className = 'snowluma-agreement-document';
            const title = document.createElement('h3');
            title.textContent = String(raw?.title || raw?.id || '协议');
            documentNode.appendChild(title);
            const metadata = document.createElement('p');
            metadata.className = 'snowluma-agreement-metadata';
            metadata.textContent = [raw?.declaredVersion, raw?.effectiveDate].filter(Boolean).join(' · ');
            documentNode.appendChild(metadata);
            const text = document.createElement('pre');
            text.className = 'snowluma-agreement-text';
            // Agreement text is untrusted upstream content: textContent is
            // deliberate so Markdown/HTML can never execute in the modal.
            text.textContent = String(raw?.text || '');
            documentNode.appendChild(text);
            body.appendChild(documentNode);
        }
        panel.appendChild(body);
        const actions = document.createElement('div');
        actions.className = 'snowluma-agreement-actions';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-outline';
        cancel.textContent = '取消';
        const accept = document.createElement('button');
        accept.type = 'button';
        accept.className = 'btn btn-primary';
        accept.textContent = '已阅读并同意';
        actions.append(cancel, accept);
        panel.appendChild(actions);
        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        return new Promise(resolve => {
            let settled = false;
            const finish = accepted => {
                if (settled) return;
                settled = true;
                overlay.remove();
                if (snowlumaAgreementModal?.overlay === overlay) snowlumaAgreementModal = null;
                resolve(accepted);
            };
            cancel.addEventListener('click', () => finish(false));
            accept.addEventListener('click', () => finish(true));
            overlay.addEventListener('click', event => {
                if (event.target === overlay) finish(false);
            });
            snowlumaAgreementModal = { overlay, resolve: finish };
        });
    }

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
            const [nextLaunch, nextGroups, nextRuntimeStatus, nextQqAdapterStatus] = await Promise.all([
                apiGet('/api/launch'),
                apiGet('/api/groups'),
                apiGet('/api/setup/deps/multimodal/status'),
                apiGet('/api/setup/qq-adapter'),
            ]);
            launch = nextLaunch;
            groups = mergeSnowLumaOptimisticGroups(nextGroups);
            runtimeStatus = nextRuntimeStatus;
            qqAdapterStatus = nextQqAdapterStatus;
            const qqGroup = (groups || []).find(group => group.id === 'qq_adapter');
            const runtimeIsRunning = Boolean((qqGroup?.services || []).some(service =>
                service.id === 'snowluma_runtime' && service.status === 'running',
            ));
            const runtimeJustStarted = runtimeIsRunning && !snowlumaRuntimeRunning;
            snowlumaRuntimeRunning = runtimeIsRunning;
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
            if (runtimeJustStarted && qqAdapterStatus?.selected === 'snowluma') {
                // Render first so the card exists, then perform one autonomous
                // saved-credential refresh.  A missing/invalid saved secret
                // simply moves the operator to the password prompt.
                setTimeout(() => {
                    const card = document.querySelector('[data-group-id="qq_adapter"]');
                    if (card) refreshSnowLumaProcesses(card, true);
                }, 0);
            }
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

    function qqGroupIsBusy(group) {
        return (group?.services || []).some(service =>
            service.status === 'starting' || service.status === 'running' || service.status === 'stopping'
        );
    }

    function qqGroupIsTransitioning(group) {
        return (group?.services || []).some(service =>
            service.status === 'starting' || service.status === 'stopping'
        );
    }

    const SNOWLUMA_START_SERVICE_IDS = new Set(['snowluma_runtime', 'snowluma_adapter']);
    const SNOWLUMA_OPTIMISTIC_START_DETAIL = '正在启动 SnowLuma Runtime + Adapter…';
    const SNOWLUMA_OPTIMISTIC_START_TIMEOUT_MS = 60_000;
    const SNOWLUMA_UNLOAD_RECONCILIATION_DELAY_MS = 1_800;

    function applySnowLumaOptimisticStart(group) {
        if (!group || group.id !== 'qq_adapter') return group;
        return {
            ...group,
            services: (group.services || []).map(service => {
                if (!SNOWLUMA_START_SERVICE_IDS.has(service.id)) return service;
                return {
                    ...service,
                    status: 'starting',
                    detail: SNOWLUMA_OPTIMISTIC_START_DETAIL,
                };
            }),
        };
    }

    function snowLumaStartReachedTerminalState(group) {
        const services = (group?.services || []).filter(service =>
            SNOWLUMA_START_SERVICE_IDS.has(service.id),
        );
        if (services.length !== SNOWLUMA_START_SERVICE_IDS.size) return false;
        return services.some(service => service.status === 'error')
            || services.every(service => service.status === 'running');
    }

    function reconcileSnowLumaOptimisticGroup(group, optimistic, requestSettled = true) {
        if (!optimistic || group?.id !== 'qq_adapter') {
            return { group, active: Boolean(optimistic) };
        }
        // A restart starts from an already-running response.  Keep the
        // optimistic state until the new start request itself has returned so
        // an in-flight poll cannot clear it with that pre-restart snapshot.
        if (requestSettled && snowLumaStartReachedTerminalState(group)) {
            return { group, active: false };
        }
        return { group: applySnowLumaOptimisticStart(group), active: true };
    }

    function mergeSnowLumaOptimisticGroups(nextGroups) {
        if (!Array.isArray(nextGroups) || !snowlumaOptimisticStart) return nextGroups;
        let active = true;
        const merged = nextGroups.map(group => {
            if (group?.id !== 'qq_adapter') return group;
            // Retain the newest backend snapshot separately from the rendered
            // optimistic copy so timeout/failure cleanup can restore trusted
            // state instead of a synthetic `starting` response.
            snowlumaOptimisticBaseGroup = cloneSnowLumaGroup(group);
            const result = reconcileSnowLumaOptimisticGroup(
                group,
                true,
                !snowlumaStartRequestInFlight,
            );
            active = result.active;
            return result.group;
        });
        snowlumaOptimisticStart = active;
        if (!active) {
            snowlumaStartRequestInFlight = false;
            snowlumaOptimisticBaseGroup = null;
            cancelSnowLumaOptimisticStartTimer();
        }
        return merged;
    }

    function cloneSnowLumaGroup(group) {
        if (!group) return null;
        return {
            ...group,
            services: (group.services || []).map(service => ({ ...service })),
        };
    }

    function cancelSnowLumaOptimisticStartTimer() {
        if (snowlumaOptimisticStartTimer !== null) {
            clearTimeout(snowlumaOptimisticStartTimer);
            snowlumaOptimisticStartTimer = null;
        }
    }

    function scheduleSnowLumaOptimisticStartTimeout() {
        cancelSnowLumaOptimisticStartTimer();
        snowlumaOptimisticStartTimer = setTimeout(() => {
            snowlumaOptimisticStartTimer = null;
            if (!snowlumaOptimisticStart) return;
            clearSnowLumaOptimisticStart(true);
            toast('SnowLuma 启动超时，请检查运行时状态后重试', 'error');
        }, SNOWLUMA_OPTIMISTIC_START_TIMEOUT_MS);
    }

    function beginSnowLumaOptimisticStart() {
        if (snowlumaOptimisticStart) return false;
        const current = groups.find(group => group.id === 'qq_adapter');
        if (!current) return false;
        snowlumaOptimisticBaseGroup = cloneSnowLumaGroup(current);
        snowlumaOptimisticStart = true;
        snowlumaStartRequestInFlight = true;
        groups = groups.map(group => (
            group.id === 'qq_adapter' ? applySnowLumaOptimisticStart(group) : group
        ));
        scheduleSnowLumaOptimisticStartTimeout();
        render();
        return true;
    }

    function clearSnowLumaOptimisticStart(restore = true) {
        const baseGroup = snowlumaOptimisticBaseGroup;
        cancelSnowLumaOptimisticStartTimer();
        snowlumaOptimisticStart = false;
        snowlumaStartRequestInFlight = false;
        snowlumaOptimisticBaseGroup = null;
        if (restore && baseGroup) {
            groups = groups.map(group => (
                group.id === 'qq_adapter' ? cloneSnowLumaGroup(baseGroup) : group
            ));
        }
        if (baseGroup) render();
    }

    function selectedQqAdapter(group) {
        if (qqAdapterStatus?.selected === 'snowluma') return 'snowluma';
        if (qqAdapterStatus?.selected === 'napcat') return 'napcat';
        return String(group?.name || '').toLowerCase().includes('snowluma') ? 'snowluma' : 'napcat';
    }

    function qqComponentStatus(adapter) {
        return qqAdapterStatus?.[adapter] || {
            selected: adapter,
            installed: false,
            missing: ['状态尚未刷新'],
            download_url: adapter === 'snowluma'
                ? 'https://github.com/SnowLuma/SnowLuma/releases/latest'
                : 'https://github.com/NapNeko/NapCatQQ/releases',
        };
    }

    function normalizeSnowLumaProcess(item) {
        const rawPid = Number(item?.pid);
        return {
            pid: Number.isInteger(rawPid) && rawPid > 0 ? rawPid : 0,
            name: String(item?.name || 'QQ').slice(0, 120),
            uin: item?.uin ? String(item.uin).slice(0, 40) : '',
            status: String(item?.status || '').slice(0, 120),
            injected: Boolean(item?.injected),
            connected: Boolean(item?.connected),
            loggedIn: Boolean(item?.loggedIn),
        };
    }

    function snowlumaProcessMarkup() {
        if (!Array.isArray(snowlumaProcesses)) return '';
        if (!snowlumaProcesses.length) {
            return '<div class="snowluma-process-empty">未发现可管理的 QQ 进程，请确认 SnowLuma Runtime 已启动。</div>';
        }
        return `
            <div class="snowluma-process-list">
                ${snowlumaProcesses.map(raw => {
                    const process = normalizeSnowLumaProcess(raw);
                    if (!process.pid) return '';
                    const account = process.uin || '未登录/未知';
                    const state = process.status || (process.loggedIn ? '已登录' : '未知');
                    const action = process.injected ? 'unload' : 'load';
                    const actionLabel = process.injected ? '解除注入' : '注入';
                    return `
                        <div class="snowluma-process-row" data-snowluma-pid="${process.pid}">
                            <div class="snowluma-process-main">
                                <strong>${escapeHtml(process.name)}</strong>
                                <span>PID ${process.pid}</span>
                                <span>QQ: ${escapeHtml(account)}</span>
                                <span>状态: ${escapeHtml(state)}</span>
                            </div>
                            <div class="snowluma-process-actions">
                                <button type="button" class="btn btn-outline btn-sm" data-snowluma-action="${action}" data-snowluma-pid="${process.pid}">${actionLabel}</button>
                                ${process.injected ? `<button type="button" class="btn btn-primary btn-sm snowluma-refresh-button" data-snowluma-action="refresh" data-snowluma-pid="${process.pid}">↻ 刷新</button>` : ''}
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;
    }

    function snowlumaGroupCardInnerHTML(group) {
        const adapter = selectedQqAdapter(group);
        const component = qqComponentStatus(adapter);
        const services = group.services || [];
        const runCount = services.filter(s => s.status === 'running').length;
        const errCount = services.filter(s => s.status === 'error').length;
        const anyStarting = services.some(s => s.status === 'starting');
        const anyStopping = services.some(s => s.status === 'stopping');
        const anyRunning = runCount > 0;
        const busy = qqGroupIsBusy(group);
        const transitioning = qqGroupIsTransitioning(group);
        const badge = errCount > 0
            ? ['error', '错误']
            : anyStarting
                ? ['starting', '启动中']
                : anyStopping
                    ? ['partial', '停止中']
                    : runCount === services.length && services.length
                        ? ['running', '运行中']
                        : runCount > 0
                            ? ['partial', `${runCount}/${services.length}`]
                            : ['stopped', '已停止'];
        const downloadUrl = component.download_url || (
            adapter === 'snowluma'
                ? 'https://github.com/SnowLuma/SnowLuma/releases/latest'
                : 'https://github.com/NapNeko/NapCatQQ/releases'
        );
        const missing = Array.isArray(component.missing) ? component.missing : [];
        const installed = component.installed === true;
        const startDisabled = transitioning || !installed;
        const selectedSnowLuma = adapter === 'snowluma';
        return `
            <div class="group-card-header">
                <div class="group-info">
                    <span class="group-icon">${svgIcon('message-circle')}</span>
                    <div>
                        <div class="group-name">QQ / ${selectedSnowLuma ? 'SnowLuma' : 'NapCat'}</div>
                        <div class="group-detail">选择后由 WebUI 管理对应 QQ 运行时与适配器</div>
                    </div>
                </div>
                <span class="group-status-badge ${badge[0]}">${badge[1]}</span>
            </div>
            <div class="group-card-body qq-launch-body">
                <div class="qq-adapter-selector-row">
                    <label for="launcher-qq-adapter"><strong>QQ 后端</strong></label>
                    <select id="launcher-qq-adapter" class="form-select" ${busy ? 'disabled' : ''}>
                        <option value="napcat" ${adapter === 'napcat' ? 'selected' : ''}>NapCat</option>
                        <option value="snowluma" ${selectedSnowLuma ? 'selected' : ''}>SnowLuma</option>
                    </select>
                </div>
                <div class="qq-component-status ${installed ? 'installed' : 'missing'}">
                    <div class="qq-component-status-heading">
                        <strong>${installed ? '✓ 组件已安装' : '⚠ 组件缺失'}</strong>
                        <span>${selectedSnowLuma ? 'SnowLuma Runtime + Adapter' : 'NapCat Shell + Adapter'}</span>
                    </div>
                    ${missing.length ? `<div class="qq-component-missing">缺少: ${escapeHtml(missing.join('、'))}</div>` : ''}
                    ${!installed ? `<div class="qq-component-guidance">请下载或重新部署所选组件后再启动。</div><a class="btn-download" href="${escapeHtml(downloadUrl)}" target="_blank" rel="noopener noreferrer">📥 官方下载 / 重新部署</a>` : ''}
                </div>
                ${selectedSnowLuma ? `
                    <div class="snowluma-instance-panel">
                        <div class="snowluma-instance-toolbar">
                            <label for="launcher-snowluma-password">WebUI 密码</label>
                            <input type="password" class="form-input" id="launcher-snowluma-password" autocomplete="current-password" placeholder="留空使用已保存密码；首次或失效时输入">
                            <button type="button" class="btn btn-primary btn-sm snowluma-refresh-button" data-snowluma-refresh ${(!installed || snowlumaProcessBusy) ? 'disabled' : ''}>↻ 刷新实例</button>
                        </div>
                        ${snowlumaProcessError ? `<div class="snowluma-process-error">${escapeHtml(snowlumaProcessError)}</div>` : ''}
                        ${snowlumaProcessMarkup()}
                    </div>
                ` : ''}
                <div class="qq-service-status-list">
                    ${services.map(service => `
                        <div class="service-row">
                            <div class="service-info"><div class="service-dot ${service.status}"></div><div class="service-text"><span class="service-name">${escapeHtml(service.name)}</span>${service.detail ? `<span class="service-detail">${escapeHtml(service.detail)}</span>` : ''}</div></div>
                            ${service.port ? `<span class="service-port">:${service.port}</span>` : ''}
                        </div>
                    `).join('')}
                </div>
            </div>
            <div class="group-card-footer">
                <button class="btn ${anyRunning ? 'btn-outline' : 'btn-primary'} btn-full" id="btn-start-${group.id}" ${startDisabled ? 'disabled' : ''}>
                    ${svgIcon(anyRunning ? 'rotate-ccw' : 'play')}${anyRunning ? '重启组' : '启动组'}
                </button>
                ${(anyRunning || anyStarting) ? `<button class="btn btn-danger" id="btn-stop-${group.id}">${svgIcon('square')}${anyStarting && !anyRunning ? '取消启动' : '停止'}</button>` : ''}
            </div>
        `;
    }

    function bindSnowLumaCardEvents(card, group) {
        card.querySelector('#launcher-qq-adapter')?.addEventListener('change', event => {
            selectQqAdapter(event.target.value, group, card);
        });
        card.querySelector('[data-snowluma-refresh]')?.addEventListener('click', () => {
            refreshSnowLumaProcesses(card);
        });
        card.querySelectorAll('[data-snowluma-action]').forEach(button => {
            button.addEventListener('click', () => {
                processSnowLumaAction(card, Number(button.dataset.snowlumaPid), button.dataset.snowlumaAction);
            });
        });
        const anyRunning = group.services.some(s => s.status === 'running');
        card.querySelector(`#btn-start-${group.id}`)?.addEventListener('click', () => startGroup(group.id, anyRunning));
        card.querySelector(`#btn-stop-${group.id}`)?.addEventListener('click', () => stopGroup(group.id));
    }

    async function selectQqAdapter(adapter, group, card) {
        if (!['napcat', 'snowluma'].includes(adapter) || qqGroupIsBusy(group)) return;
        const select = card.querySelector('#launcher-qq-adapter');
        if (select) select.disabled = true;
        try {
            await apiPost('/api/setup/qq-adapter', { qq_adapter: adapter });
            toast(`已切换到 ${adapter === 'snowluma' ? 'SnowLuma' : 'NapCat'}`, 'success');
            await refresh();
        } catch (e) {
            toast(`切换 QQ 后端失败: ${e?.message || '请求失败'}`, 'error');
            if (select) select.value = selectedQqAdapter(group);
        }
    }

    function normalizeSnowLumaRefreshOptions(automaticOrOptions = false, extraOptions = {}) {
        if (automaticOrOptions && typeof automaticOrOptions === 'object') {
            return { ...automaticOrOptions };
        }
        return { automatic: automaticOrOptions === true, ...extraOptions };
    }

    function createSnowLumaProcessRefreshError() {
        const error = new Error(SNOWLUMA_PROCESS_REFRESH_MESSAGE);
        error.code = SNOWLUMA_PROCESS_REFRESH_FAILED;
        return error;
    }

    async function performSnowLumaProcessRefresh(card, options) {
        const refreshToken = {};
        snowlumaProcessRefreshToken = refreshToken;
        snowlumaProcessBusy = true;
        snowlumaProcessError = '';
        render();
        try {
            const result = await withSnowLumaPassword(
                card,
                password => snowlumaApiPost('/api/setup/snowluma/processes', password ? { password } : {}),
            );
            const list = Array.isArray(result?.list)
                ? result.list
                : Array.isArray(result?.processes) ? result.processes : [];
            snowlumaProcesses = list.map(normalizeSnowLumaProcess);
            return { ok: true };
        } catch (e) {
            const code = snowlumaErrorCode(e);
            const suppressFailureUi = options.throwOnFailure === true;
            if (code === 'PASSWORD_REQUIRED') {
                snowlumaProcessError = '请输入 SnowLuma WebUI 密码后重试';
                if (!suppressFailureUi) {
                    toast(snowlumaProcessError, 'error');
                    focusSnowLumaPasswordInput(card);
                }
            } else if (code === 'AGREEMENT_REQUIRED') {
                snowlumaProcessError = '请先阅读并同意 SnowLuma 协议';
                if (!suppressFailureUi) toast(snowlumaProcessError, 'error');
            } else {
                snowlumaProcessError = e?.message || 'SnowLuma 实例刷新失败';
                if (!options.automatic && !suppressFailureUi) toast(snowlumaProcessError, 'error');
            }
            return { ok: false, error: e };
        } finally {
            if (snowlumaProcessRefreshToken === refreshToken) {
                snowlumaProcessRefreshToken = null;
                snowlumaProcessBusy = false;
                render();
            }
        }
    }

    async function refreshSnowLumaProcesses(card, automaticOrOptions = false, extraOptions = {}) {
        const options = normalizeSnowLumaRefreshOptions(automaticOrOptions, extraOptions);
        const result = await snowlumaProcessRefreshFlight(
            () => performSnowLumaProcessRefresh(card, options),
            { requireFresh: options.requireFresh === true },
        );
        if (options.throwOnFailure === true && result?.ok !== true) {
            throw createSnowLumaProcessRefreshError();
        }
        return result;
    }

    async function runSnowLumaActionAndRefresh(actionRequest, refreshRequest, onSuccess) {
        await actionRequest();
        const result = await refreshRequest();
        if (!result || result.ok !== true) throw createSnowLumaProcessRefreshError();
        onSuccess();
    }

    function shouldScheduleSnowLumaUnloadReconciliation(error) {
        return snowlumaErrorCode(error) === 'UNLOAD_VERIFICATION_FAILED';
    }

    function scheduleSnowLumaUnloadReconciliation(
        card,
        schedule = setTimeout,
        refresh = refreshSnowLumaProcesses,
    ) {
        schedule(() => {
            void refresh(card, true).catch(() => {});
        }, SNOWLUMA_UNLOAD_RECONCILIATION_DELAY_MS);
    }

    async function processSnowLumaAction(card, pid, action) {
        if (!Number.isInteger(pid) || pid <= 0 || !['load', 'unload', 'refresh'].includes(action)) return;
        try {
            await runSnowLumaActionAndRefresh(
                () => withSnowLumaPassword(
                    card,
                    password => snowlumaApiPost(
                        `/api/setup/snowluma/processes/${pid}/${action}`,
                        password ? { password } : {},
                    ),
                ),
                () => refreshSnowLumaProcesses(card, {
                    automatic: true,
                    requireFresh: true,
                    throwOnFailure: true,
                }),
                () => toast(`PID ${pid} ${action === 'load' ? '注入' : action === 'unload' ? '解除注入' : '刷新'}完成`, 'success'),
            );
        } catch (e) {
            const code = snowlumaErrorCode(e);
            if (shouldScheduleSnowLumaUnloadReconciliation(e)) {
                // SnowLuma's watcher adopts a still-live pipe on its next
                // tick.  Keep the current list for the immediate error
                // feedback, then reconcile exactly once after that tick.
                toast(e?.message || 'SnowLuma 未能解除注入，请稍后重试', 'error');
                scheduleSnowLumaUnloadReconciliation(card);
                return;
            }
            if (code === SNOWLUMA_PROCESS_REFRESH_FAILED) {
                toast(SNOWLUMA_PROCESS_REFRESH_MESSAGE, 'error');
                return;
            }
            if (code === 'PASSWORD_REQUIRED') {
                toast('请输入 SnowLuma WebUI 密码后重试', 'error');
                focusSnowLumaPasswordInput(card);
            } else {
                toast(e?.message || 'SnowLuma 实例操作失败', 'error');
            }
        }
    }

    async function withSnowLumaPassword(card, request) {
        const password = card?.querySelector('#launcher-snowluma-password')?.value || '';
        let replayed = false;
        try {
            return await request(password || undefined);
        } catch (error) {
            if (snowlumaErrorCode(error) !== 'AGREEMENT_REQUIRED' || replayed) throw error;
            // The modal must not retain a manual password.  The replay uses
            // the saved credential only and is deliberately bounded to once.
            clearSnowLumaPasswordInput(card);
            const accepted = await showSnowLumaAgreement(snowlumaErrorPayload(error));
            if (!accepted) {
                const cancelled = new Error('已取消 SnowLuma 协议确认');
                cancelled.code = 'AGREEMENT_CANCELLED';
                throw cancelled;
            }
            replayed = true;
            const agreement = snowlumaErrorPayload(error);
            await snowlumaApiPost(
                '/api/setup/snowluma/agreements/accept',
                { version: agreement.version },
            );
            return await request(undefined);
        } finally {
            clearSnowLumaPasswordInput(card);
        }
    }

    /** Generate the inner HTML for a group card (without the wrapper div). */
    function groupCardInnerHTML(group) {
        if (group.id === 'qq_adapter') return snowlumaGroupCardInnerHTML(group);
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
            const requestOnlyPassword = card.querySelector('#launcher-snowluma-password')?.value || '';
            card._lastHTML = newHTML;
            card.innerHTML = newHTML;
            const passwordInput = card.querySelector('#launcher-snowluma-password');
            if (passwordInput && requestOnlyPassword) passwordInput.value = requestOnlyPassword;
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
        if (group.id === 'qq_adapter') {
            bindSnowLumaCardEvents(card, group);
            return;
        }
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
        let snowLumaStart = false;
        try {
            if (groupId === 'qq_adapter') {
                const group = groups.find(item => item.id === groupId);
                const adapter = selectedQqAdapter(group);
                snowLumaStart = adapter === 'snowluma';
                if (snowLumaStart && snowlumaOptimisticStart) return;
                const component = qqComponentStatus(adapter);
                if (component.installed !== true) {
                    throw new Error(`所选 ${adapter === 'snowluma' ? 'SnowLuma' : 'NapCat'} 组件缺失，请先下载或重新部署`);
                }
                if (snowLumaStart) beginSnowLumaOptimisticStart();
            }
            if (isRestart) {
                await apiPost(`/api/groups/${groupId}/stop`);
                // Keep the existing restart delay, but await the bounded
                // timer so a rejected start request reaches this try/catch.
                refresh();
                await new Promise(resolve => setTimeout(resolve, 3000));
                await apiPost(`/api/groups/${groupId}/start`);
                if (snowLumaStart) snowlumaStartRequestInFlight = false;
                toast(`${groupId} 正在启动...`, 'info');
            } else {
                await apiPost(`/api/groups/${groupId}/start`);
                if (snowLumaStart) snowlumaStartRequestInFlight = false;
                toast('正在启动...', 'info');
            }
            await refresh();
        } catch (e) {
            if (snowLumaStart) {
                clearSnowLumaOptimisticStart(true);
                void refresh();
            }
            toast(`启动失败: ${e.message}`, 'error');
        }
    }

    async function stopGroup(groupId) {
        if (groupId === 'qq_adapter' && snowlumaOptimisticStart) {
            clearSnowLumaOptimisticStart(true);
        }
        try {
            await apiPost(`/api/groups/${groupId}/stop`);
            toast('正在停止...', 'info');
            void refresh();
        } catch (e) {
            toast(`停止失败: ${e.message}`, 'error');
        }
    }

    return {
        init,
        refresh,
        // Test-only pure hooks keep the instance rendering/action contract
        // executable without starting a browser or contacting SnowLuma.
        __test: {
            normalizeSnowLumaProcess,
            snowlumaProcessMarkup,
            qqGroupIsBusy,
            withSnowLumaPassword,
            showSnowLumaAgreement,
            snowlumaErrorCode,
            createSnowLumaRefreshFlight,
            runSnowLumaActionAndRefresh,
            scheduleSnowLumaUnloadReconciliation,
            shouldScheduleSnowLumaUnloadReconciliation,
            applySnowLumaOptimisticStart,
            reconcileSnowLumaOptimisticGroup,
            snowLumaStartReachedTerminalState,
            snowlumaOptimisticStartTimeoutMs: SNOWLUMA_OPTIMISTIC_START_TIMEOUT_MS,
            snowlumaUnloadReconciliationDelayMs: SNOWLUMA_UNLOAD_RECONCILIATION_DELAY_MS,
        },
    };
})();
