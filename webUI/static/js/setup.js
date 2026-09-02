/**
 * NachoBot WebUI — Setup Wizard Module
 * 5-step deployment wizard: env check → component select → config form → integrity check → deploy
 */

const SetupModule = (() => {
    let currentStep = 1;
    let envCheckData = null;
    let selectedComponents = ['core']; // core is always selected
    let deploying = false;
    let defaultsLoader = null;
    let bilibiliQrObjectUrl = null;
    let bilibiliLoginJobId = null;
    let bilibiliLoginContext = null;
    let activeWizardData = null;

    // Track path verification results
    let pathCheckResults = {};

    function init() {
        if (!defaultsLoader) {
            defaultsLoader = window.SetupDefaults.create({
                apiGet,
                addProviderRow,
                createProviderRow,
                bindProviderRow,
                addModelRow,
                createModelRow,
                bindModelRow,
                syncProviderDropdowns,
            });
        }

        // Navigation buttons (5-step flow)
        document.getElementById('setup-next-1')?.addEventListener('click', () => goToStep(2));
        document.getElementById('setup-prev-2')?.addEventListener('click', () => goToStep(1));
        document.getElementById('setup-next-2')?.addEventListener('click', () => goToStep(3));
        document.getElementById('setup-prev-3')?.addEventListener('click', () => goToStep(2));
        document.getElementById('setup-next-3')?.addEventListener('click', onStep3Next);
        document.getElementById('setup-prev-4')?.addEventListener('click', () => goToStep(3));
        document.getElementById('setup-next-4')?.addEventListener('click', () => startDeploy());
        document.getElementById('setup-prev-5')?.addEventListener('click', () => {
            if (!deploying) goToStep(4);
        });
        document.getElementById('setup-finish')?.addEventListener('click', () => {
            App.switchTab('launcher');
        });
        document.getElementById('setup-bilibili-retry')?.addEventListener('click', async () => {
            if (deploying || !bilibiliLoginContext) return;
            deploying = true;
            document.getElementById('setup-prev-5').disabled = true;
            const loginOk = await startBilibiliLogin(bilibiliLoginContext);
            if (loginOk) {
                completeDeployment(bilibiliLoginContext.progressDiv);
            } else {
                deploying = false;
                document.getElementById('setup-prev-5').disabled = false;
                document.getElementById('setup-finish').disabled = true;
            }
        });

        // Recheck button
        document.getElementById('btn-recheck')?.addEventListener('click', runEnvCheck);

        // Component checkbox listeners
        document.querySelectorAll('.setup-component-cb').forEach(cb => {
            cb.addEventListener('change', onComponentToggle);
        });

        // "+" add-row buttons
        document.getElementById('btn-add-provider')?.addEventListener('click', addProviderRow);
        document.getElementById('btn-add-model')?.addEventListener('click', addModelRow);

        // Delegate events on the first provider/model rows
        bindProviderRow(document.querySelector('.provider-row'));
        bindModelRow(document.querySelector('.model-row'));

        // Step 4: verify buttons
        document.querySelectorAll('.btn-verify').forEach(btn => {
            btn.addEventListener('click', () => verifyPath(btn.dataset.type));
        });
        document.getElementById('btn-verify-all')?.addEventListener('click', verifyAllPaths);
    }

    function refresh() {
        if (!envCheckData) {
            runEnvCheck();
        }
    }

    // ---- Step Navigation ----

    function goToStep(step) {
        if (step < 1 || step > 5) return;
        currentStep = step;

        // Update stepper
        document.querySelectorAll('#setup-stepper .step').forEach(el => {
            const s = parseInt(el.dataset.step);
            el.classList.toggle('active', s <= step);
            el.classList.toggle('completed', s < step);
        });

        // Update step-lines
        document.querySelectorAll('#setup-stepper .step-line').forEach((line, i) => {
            line.classList.toggle('active', i + 1 < step);
        });

        // Show/hide panels
        document.querySelectorAll('.setup-step-panel').forEach(p => p.classList.remove('active'));
        document.getElementById(`setup-step-${step}`)?.classList.add('active');

        // Step-specific actions
        if (step === 2) {
            updateComponentVisuals();
            computeGpuRecommendations(envCheckData ? envCheckData.gpu : null);
        }
        if (step === 3) {
            updateFormVisibility();
            if (!defaultsLoader.isLoaded()) {
                defaultsLoader.load();
            }
        }
        if (step === 4) {
            updatePathCheckVisibility();
            verifyAllPaths();
        }
    }

    // ---- Step 1: Environment Check ----

    async function runEnvCheck() {
        const list = document.getElementById('setup-check-list');
        list.innerHTML = '<div class="setup-check-placeholder"><div class="setup-spinner"></div> 正在检测环境...</div>';

        try {
            const data = await apiGet('/api/setup/check');
            envCheckData = data;
            renderEnvCheck(data);
        } catch (e) {
            list.innerHTML = `<div class="setup-check-item error"><span class="check-icon">❌</span><span>检测失败: ${escapeHtml(e.message)}</span></div>`;
        }
    }

    function renderEnvCheck(data) {
        const list = document.getElementById('setup-check-list');
        list.innerHTML = '';
        list.appendChild(makeCheckItem(data.python));
        if (data.git) {
            list.appendChild(makeCheckItem(data.git));
        }
        list.appendChild(makeCheckItem(data.node));
        list.appendChild(makeCheckItem(data.docker));
        if (data.gpu) {
            list.appendChild(makeCheckItem(data.gpu));
        }

        const portCard = document.getElementById('setup-port-card');
        const portList = document.getElementById('setup-port-list');
        if (data.ports && data.ports.length) {
            portCard.style.display = '';
            portList.innerHTML = '';
            data.ports.forEach(p => {
                const el = document.createElement('div');
                el.className = `setup-port-item ${p.status}`;
                el.innerHTML = `
                    <span class="port-status-icon">${p.status === 'ok' ? '✅' : '⚠️'}</span>
                    <span class="port-name">${escapeHtml(p.name)}</span>
                    <span class="port-number">:${p.port}</span>
                    <span class="port-message">${escapeHtml(p.message)}</span>
                `;
                portList.appendChild(el);
            });
        }

        const configCard = document.getElementById('setup-config-card');
        const configList = document.getElementById('setup-config-list');
        if (data.configs && data.configs.length) {
            configCard.style.display = '';
            configList.innerHTML = '';
            data.configs.forEach(c => {
                const el = document.createElement('div');
                el.className = `setup-config-item ${c.target_exists ? 'exists' : 'missing'}`;
                el.innerHTML = `
                    <span class="config-status-icon">${c.target_exists ? '✅' : '📦'}</span>
                    <span class="config-filename">${escapeHtml(c.filename)}</span>
                    <span class="config-target">${escapeHtml(c.target)}</span>
                    <span class="config-status-text">${c.target_exists ? '已存在' : '待生成'}</span>
                `;
                configList.appendChild(el);
            });
        }
    }

    function makeCheckItem(item) {
        const el = document.createElement('div');
        const iconMap = { ok: '✅', warning: '⚠️', error: '❌' };
        el.className = `setup-check-item ${item.status}`;
        el.innerHTML = `
            <span class="check-icon">${iconMap[item.status] || '❓'}</span>
            <span class="check-message">${escapeHtml(item.message)}</span>
        `;

        if (item.download_url) {
            const download = document.createElement('a');
            download.className = 'btn-download';
            download.href = item.download_url;
            download.target = '_blank';
            download.rel = 'noopener noreferrer';
            download.textContent = item.download_label || '下载';
            el.appendChild(download);
        }

        return el;
    }

    // ---- Step 2: Component Selection ----

    function onComponentToggle() {
        selectedComponents = ['core'];
        document.querySelectorAll('.setup-component-cb:checked').forEach(cb => {
            selectedComponents.push(cb.value);
        });
        if (!selectedComponents.includes('discord')) {
            clearDiscordTokenFromRequest(activeWizardData);
        }
        updateComponentVisuals();
    }

    function updateComponentVisuals() {
        document.querySelectorAll('.component-option').forEach(label => {
            const cb = label.querySelector('input[type="checkbox"]');
            label.classList.toggle('checked', cb.checked);
        });
    }

    let recommendedLaunchGroup = '核心'; // default recommendation

    function computeGpuRecommendations(gpuData) {
        const infoEl = document.getElementById('gpu-detect-info');
        const titleEl = document.getElementById('gpu-recommend-title');
        const descEl = document.getElementById('gpu-recommend-desc');
        const actionsEl = document.getElementById('gpu-apply-actions');

        if (!infoEl || !titleEl || !descEl || !actionsEl) return;

        actionsEl.innerHTML = '';

        if (!gpuData) {
            infoEl.textContent = '未检测到显卡数据';
            titleEl.textContent = '纯核心 + 平台适配器';
            descEl.textContent = '未检测到可用 NVIDIA 显卡。推荐不使用任何本地语音或视觉模型，以确保稳定运行。';
            recommendedLaunchGroup = '核心';
            createApplyButton('一键应用方案 (不启用本地模型)', () => applyScheme('none'));
            return;
        }

        const hasGpu = gpuData.has_gpu;
        const vramMb = gpuData.vram_mb || 0;
        const vramGb = vramMb / 1024.0;
        infoEl.textContent = gpuData.message || (hasGpu ? `${gpuData.gpu_name} (显存: ${vramGb.toFixed(2)} GB)` : '无可用 NVIDIA 显卡');

        if (!hasGpu || vramGb <= 4.0) {
            // No GPU or VRAM <= 4GB
            titleEl.textContent = '纯核心 + 平台适配器';
            descEl.textContent = '系统未检测到 NVIDIA 显卡或可用显存不足 4GB。推荐不启用任何本地模型（仅保留 Core 核心与平台适配器），以避免显存溢出或运行缓慢。';
            recommendedLaunchGroup = '核心';
            createApplyButton('一键应用此方案', () => applyScheme('none'));
        }
        else if (vramGb > 4.0 && vramGb < 6.0) {
            // VRAM > 4GB and < 6GB
            titleEl.textContent = 'GPT LITE 方案';
            descEl.textContent = '检测到可用显存介于 4G 到 6G 之间。推荐使用 GPT LITE 方案部署（仅启用 GPT-SoVITS 合成，不启用本地 Florence-2 VLM 视觉与 FunASR 语音识别，以节约显存）。';
            recommendedLaunchGroup = '语音服务 (LITE)';
            createApplyButton('一键应用此方案', () => applyScheme('gpt_lite'));
        }
        else if (vramGb >= 6.0 && vramGb <= 8.0) {
            // VRAM in [6GB, 8GB]
            titleEl.textContent = 'GPT FULL 方案';
            descEl.textContent = '检测到可用显存介于 6G 到 8G 之间。推荐使用 GPT FULL 方案部署（启用 GPT-SoVITS 语音合成 + Florence-2 视觉大模型 + FunASR 语音识别，满足全功能交互需求）。';
            recommendedLaunchGroup = '多模态服务 (FULL)';
            createApplyButton('一键应用此方案', () => applyScheme('gpt_full'));
        }
        else if (vramGb > 8.0 && vramGb <= 12.0) {
            // VRAM in (8GB, 12GB]
            titleEl.textContent = 'GPT FULL 方案 或 Vox LITE 方案';
            descEl.textContent = '检测到可用显存介于 8G 到 12G 之间。您可以选择使用轻量合成但全功能的 GPT FULL 方案，或者尝试使用高质量拟真拟音但消耗较大的 Vox LITE 方案（仅使用 VoxCPM 语音合成，不开启视觉和语音识别）。';
            recommendedLaunchGroup = '多模态服务 (FULL)';
            createApplyButton('应用 GPT FULL 方案', () => {
                recommendedLaunchGroup = '多模态服务 (FULL)';
                applyScheme('gpt_full');
            });
            createApplyButton('应用 Vox LITE 方案', () => {
                recommendedLaunchGroup = '语音服务 (LITE)';
                applyScheme('vox_lite');
            });
        }
        else {
            // VRAM > 12GB
            titleEl.textContent = 'Vox Full 方案';
            descEl.textContent = '检测到可用显存大于 12G。推荐使用极致画质和音质的 Vox Full 方案（VoxCPM 语音合成 + Florence-2 视觉大模型 + FunASR 语音识别，完美释放大显存显卡潜力）。';
            recommendedLaunchGroup = '多模态服务 (FULL)';
            createApplyButton('一键应用此方案', () => applyScheme('vox_full'));
        }

        function createApplyButton(text, callback) {
            const btn = document.createElement('button');
            btn.className = 'btn btn-primary';
            btn.textContent = text;
            btn.style.marginRight = '10px';
            btn.addEventListener('click', callback);
            actionsEl.appendChild(btn);
        }
    }

    function applyScheme(scheme) {
        const ttsCb = document.querySelector('.setup-component-cb[value="tts"]');
        const ttsEngineSelect = document.getElementById('setup-tts-engine');

        if (scheme === 'none') {
            if (ttsCb) ttsCb.checked = false;
            toast('已为您选择: 纯核心方案 (不启用本地模型)', 'success');
        }
        else if (scheme === 'gpt_lite') {
            if (ttsCb) ttsCb.checked = true;
            if (ttsEngineSelect) ttsEngineSelect.value = 'GPT_Sovits';
            toast('已为您选择: gpt lite 方案 (GPT-SoVITS + LITE 模式)', 'success');
        }
        else if (scheme === 'gpt_full') {
            if (ttsCb) ttsCb.checked = true;
            if (ttsEngineSelect) ttsEngineSelect.value = 'GPT_Sovits';
            toast('已为您选择: gpt FULL 方案 (GPT-SoVITS + FULL 模式)', 'success');
        }
        else if (scheme === 'vox_lite') {
            if (ttsCb) ttsCb.checked = true;
            if (ttsEngineSelect) ttsEngineSelect.value = 'Vox';
            toast('已为您选择: vox LITE 方案 (VoxCPM + LITE 模式)', 'success');
        }
        else if (scheme === 'vox_full') {
            if (ttsCb) ttsCb.checked = true;
            if (ttsEngineSelect) ttsEngineSelect.value = 'Vox';
            toast('已为您选择: Vox Full 方案 (VoxCPM + FULL 模式)', 'success');
        }

        onComponentToggle();
    }

    // ---- Step 3: Config Form (repeatable rows) ----

    function updateFormVisibility() {
        const ttsSection = document.getElementById('setup-tts-section');
        const universalvcSection = document.getElementById('setup-universalvc-section');
        const discordSection = document.getElementById('setup-discord-section');
        if (ttsSection) {
            ttsSection.style.display = selectedComponents.includes('tts') ? '' : 'none';
        }
        if (universalvcSection) {
            universalvcSection.style.display = selectedComponents.includes('universalvc') ? '' : 'none';
        }
        if (discordSection) {
            discordSection.style.display = selectedComponents.includes('discord') ? '' : 'none';
        }
    }

    // -- Provider rows --

    function addProviderRow() {
        const container = document.getElementById('provider-rows');
        const row = createProviderRow();
        container.appendChild(row);
        bindProviderRow(row);
        syncProviderDropdowns();
    }

    function createProviderRow() {
        const row = document.createElement('div');
        row.className = 'repeatable-row provider-row';
        row.innerHTML = `
            <div class="repeatable-row-fields">
                <div class="form-row">
                    <label class="form-label">服务商名称</label>
                    <input type="text" class="form-input setup-single-value setup-provider-name" placeholder="例如: SiliconFlow">
                </div>
                <div class="form-row">
                    <label class="form-label">API Base URL</label>
                    <input type="text" class="form-input setup-provider-url" placeholder="https://api.siliconflow.cn/v1">
                </div>
                <div class="form-row">
                    <label class="form-label">API Key</label>
                    <div class="form-input-group">
                        <input type="password" class="form-input setup-provider-key" placeholder="sk-...">
                        <button class="btn-icon toggle-pw" title="显示/隐藏">👁</button>
                    </div>
                </div>
            </div>
            <button class="btn-remove-row" title="移除此服务商">✕</button>
        `;
        row.querySelector('.btn-remove-row').addEventListener('click', () => {
            row.remove();
            syncProviderDropdowns();
            syncModelGroups();
        });
        return row;
    }

    function bindProviderRow(row) {
        if (!row) return;
        const nameInput = row.querySelector('.setup-provider-name');
        if (nameInput) {
            nameInput.addEventListener('input', () => {
                validateSingleValue(nameInput);
                syncProviderDropdowns();
            });
            nameInput.addEventListener('blur', () => validateSingleValue(nameInput));
        }
        // password toggle
        row.querySelectorAll('.toggle-pw').forEach(btn => {
            btn.addEventListener('click', () => {
                const input = btn.closest('.form-input-group').querySelector('input');
                if (input) {
                    input.type = input.type === 'password' ? 'text' : 'password';
                }
            });
        });
    }

    // -- Model rows --

    function addModelRow() {
        const container = document.getElementById('model-rows');
        const row = createModelRow();
        container.appendChild(row);
        bindModelRow(row);
        syncProviderDropdowns();
        syncModelGroups();
    }

    function createModelRow() {
        const row = document.createElement('div');
        row.className = 'repeatable-row model-row';
        row.innerHTML = `
            <div class="repeatable-row-fields">
                <div class="form-row">
                    <label class="form-label">模型标识</label>
                    <input type="text" class="form-input setup-single-value setup-model-id" placeholder="例如: gpt-4o-mini">
                </div>
                <div class="form-row">
                    <label class="form-label">模型名称</label>
                    <input type="text" class="form-input setup-model-name" placeholder="留空则与标识相同">
                </div>
                <div class="form-row">
                    <label class="form-label">所属服务商</label>
                    <select class="form-select setup-model-provider">
                        <option value="">— 请先填写服务商 —</option>
                    </select>
                </div>
            </div>
            <button class="btn-remove-row" title="移除此模型">✕</button>
        `;
        row.querySelector('.btn-remove-row').addEventListener('click', () => {
            row.remove();
            syncModelGroups();
        });
        return row;
    }

    function bindModelRow(row) {
        if (!row) return;
        const idInput = row.querySelector('.setup-model-id');
        const nameInput = row.querySelector('.setup-model-name');
        if (idInput) {
            idInput.addEventListener('input', () => {
                validateSingleValue(idInput);
                // Auto-fill name if user hasn't manually edited it
                if (nameInput && !nameInput.dataset.userEdited) {
                    nameInput.value = idInput.value.trim();
                }
                syncModelGroups();
            });
            idInput.addEventListener('blur', () => validateSingleValue(idInput));
        }
        if (nameInput) {
            nameInput.addEventListener('input', () => {
                nameInput.dataset.userEdited = nameInput.value.trim() ? 'true' : '';
                syncModelGroups();
            });
        }
    }

    // -- Sync helpers --

    /** Collect all provider names and populate every model-provider dropdown */
    function syncProviderDropdowns() {
        const names = [];
        document.querySelectorAll('.setup-provider-name').forEach(input => {
            const v = input.value.trim();
            if (v) names.push(v);
        });

        document.querySelectorAll('.setup-model-provider').forEach(select => {
            const prev = select.value;
            select.innerHTML = '<option value="">— 请选择服务商 —</option>';
            names.forEach(n => {
                const opt = document.createElement('option');
                opt.value = n;
                opt.textContent = n;
                select.appendChild(opt);
            });
            // Restore previous selection if still valid
            if (names.includes(prev)) select.value = prev;
            // Auto-select if only one provider
            if (names.length === 1) select.value = names[0];
        });
    }

    /** Collect all model names and display in model group fields */
    function syncModelGroups() {
        if (defaultsLoader?.isLoading()) return;
        const names = [];
        document.querySelectorAll('.model-row').forEach(row => {
            const idInput = row.querySelector('.setup-model-id');
            const nameInput = row.querySelector('.setup-model-name');
            const name = (nameInput && nameInput.value.trim()) ||
                (idInput && idInput.value.trim()) || '';
            if (name) names.push(name);
        });
        const display = names.join(', ') || '';
        document.querySelectorAll('.setup-model-group').forEach(f => {
            f.value = display;
        });
    }

    // ---- Data Collection ----

    function collectWizardData() {
        // Collect providers
        const providers = [];
        document.querySelectorAll('.provider-row').forEach(row => {
            const name = row.querySelector('.setup-provider-name')?.value.trim() || '';
            const url = row.querySelector('.setup-provider-url')?.value.trim() || '';
            const key = row.querySelector('.setup-provider-key')?.value.trim() || '';
            if (name) {
                providers.push({ name, base_url: url, api_key: key });
            }
        });

        // Collect models
        const models = [];
        document.querySelectorAll('.model-row').forEach(row => {
            const id = row.querySelector('.setup-model-id')?.value.trim() || '';
            const name = row.querySelector('.setup-model-name')?.value.trim() || id;
            const provider = row.querySelector('.setup-model-provider')?.value || '';
            if (id) {
                models.push({ model_identifier: id, model_name: name, api_provider: provider });
            }
        });


        const wizardData = {
            components: selectedComponents,
            core: {
                qq_account: document.getElementById('setup-qq-account')?.value.trim() || '',
                nickname: document.getElementById('setup-nickname')?.value.trim() || '',
            },
            providers,
            models,
            tts: {
                engine: document.getElementById('setup-tts-engine')?.value || 'GPT_Sovits',
            },
            universalvc: {
                target_process_name: document.getElementById('setup-uvc-process')?.value.trim() || '',
                output_device: document.getElementById('setup-uvc-output-device')?.value.trim() || '',
                denoise_enabled: document.getElementById('setup-uvc-denoise')?.checked ?? true,
                speaker_enabled: document.getElementById('setup-uvc-speaker')?.checked ?? true,
            },
            env: {},
        };

        // Keep the token in memory only for this request.  It is never
        // persisted in browser storage or included in logs.  Non-Discord
        // deployments must not carry a secret-shaped field at all.
        if (selectedComponents.includes('discord')) {
            wizardData.discord = {
                token: document.getElementById('setup-discord-token')?.value.trim() || '',
            };
        }
        return wizardData;
    }

    // ---- Step 3 → Step 4 validation ----

    function onStep3Next() {
        // Check single-value validation
        let allValid = true;
        document.querySelectorAll('#setup-step-3 .setup-single-value').forEach(input => {
            if (!validateSingleValue(input)) allValid = false;
        });
        if (!allValid) return;

        // Check for any empty API keys
        let hasEmptyKey = false;
        document.querySelectorAll('.provider-row').forEach(row => {
            const keyInput = row.querySelector('.setup-provider-key');
            if (keyInput && !keyInput.readOnly && !keyInput.value.trim()) {
                hasEmptyKey = true;
                keyInput.classList.add('input-error');
            } else if (keyInput) {
                keyInput.classList.remove('input-error');
            }
        });

        if (hasEmptyKey) {
            alert('发现未填写的 API Key。请填写所有的 API Key，或者点击右侧 "✕" 按钮删除不需要的服务商配置。');
            return;
        }

        // Check SiliconFlow API key for embedding
        let sfKeyValid = false;
        document.querySelectorAll('.provider-row').forEach(row => {
            const name = row.querySelector('.setup-provider-name')?.value || '';
            if (name.toLowerCase() === 'siliconflow') {
                const key = row.querySelector('.setup-provider-key')?.value.trim();
                if (key) {
                    sfKeyValid = true;
                }
            }
        });

        if (!sfKeyValid) {
            alert('必须填写 SiliconFlow API Key（长期记忆系统模型 bge-m3 的必需前置）');
            return;
        }

        // Check at least one provider
        const providerNames = [];
        document.querySelectorAll('.setup-provider-name').forEach(i => {
            if (i.value.trim()) providerNames.push(i.value.trim());
        });
        if (providerNames.length === 0) {
            alert('请至少配置一个 API 服务商');
            return;
        }

        // Check at least one model
        const modelIds = [];
        document.querySelectorAll('.setup-model-id').forEach(i => {
            if (i.value.trim()) modelIds.push(i.value.trim());
        });
        if (modelIds.length === 0) {
            alert('请至少配置一个模型');
            return;
        }

        // Validate QQ account (required for bot_config.toml)
        const qqVal = document.getElementById('setup-qq-account')?.value.trim() || '';
        if (!qqVal) {
            alert('请填写 bot 的 QQ 号（bot_config.toml 必填项）');
            return;
        }
        if (!/^\d{5,12}$/.test(qqVal)) {
            alert('请填写有效的 QQ 号（5-12位数字）');
            return;
        }

        if (selectedComponents.includes('discord')) {
            const discordToken = document.getElementById('setup-discord-token');
            if (!discordToken || !discordToken.value.trim()) {
                discordToken?.classList.add('input-error');
                alert('选择 Discord 时必须填写 Bot Token');
                discordToken?.focus();
                return;
            }
            discordToken.classList.remove('input-error');
        }

        goToStep(4);
    }

    // ---- Step 4: Integrity Check ----

    function getRequiredChecks() {
        const checks = [];
        if (selectedComponents.includes('qq')) checks.push('napcat');
        if (selectedComponents.includes('discord')) checks.push('nodejs');
        if (selectedComponents.includes('bilibili')) checks.push('bilibili_dll');
        if (selectedComponents.includes('universalvc')) checks.push('vb_cable');
        return checks;
    }

    function updatePathCheckVisibility() {
        const checks = getRequiredChecks();
        const allTypes = ['napcat', 'nodejs', 'bilibili_dll', 'vb_cable'];
        // Map type to card ID
        const cardMap = {
            napcat: 'path-check-napcat',
            nodejs: 'path-check-nodejs',
            bilibili_dll: 'path-check-bilibili',
            vb_cable: 'path-check-vb-cable',
        };
        allTypes.forEach(t => {
            const card = document.getElementById(cardMap[t]);
            if (card) card.style.display = checks.includes(t) ? '' : 'none';
        });
        // Show "no checks" message if nothing needed
        const noneMsg = document.getElementById('path-check-none');
        if (noneMsg) noneMsg.style.display = checks.length === 0 ? '' : 'none';
        // Reset results
        pathCheckResults = {};
        updateDeployButton();
    }

    async function verifyPath(type) {
        const statusEl = document.getElementById(`status-${type}`);
        const resultEl = document.getElementById(`result-${type}`);
        const dlBtn = document.getElementById(`dl-${type}`);
        const pathInput = document.getElementById(`path-${type}`);
        const path = pathInput ? pathInput.value.trim() : '';

        if (statusEl) statusEl.textContent = '⏳';
        if (resultEl) resultEl.textContent = '验证中...';
        if (resultEl) resultEl.className = 'path-check-result';

        try {
            const res = await apiPost('/api/setup/verify-path', { type, path });
            pathCheckResults[type] = res.valid;
            if (statusEl) statusEl.textContent = res.valid ? '✅' : '❌';
            if (resultEl) {
                resultEl.textContent = res.message;
                resultEl.className = 'path-check-result ' + (res.valid ? 'valid' : 'invalid');
            }
            if (dlBtn) dlBtn.style.display = res.valid ? 'none' : '';
        } catch (e) {
            pathCheckResults[type] = false;
            if (statusEl) statusEl.textContent = '❌';
            if (resultEl) {
                resultEl.textContent = `验证失败: ${e.message}`;
                resultEl.className = 'path-check-result invalid';
            }
            if (dlBtn) dlBtn.style.display = '';
        }
        updateDeployButton();
    }

    async function verifyAllPaths() {
        const checks = getRequiredChecks();
        if (checks.length === 0) {
            updateDeployButton();
            return;
        }
        for (const type of checks) {
            await verifyPath(type);
        }
    }

    function updateDeployButton() {
        const btn = document.getElementById('setup-next-4');
        if (!btn) return;
        const checks = getRequiredChecks();
        if (checks.length === 0) {
            btn.disabled = false;
            return;
        }
        const allPassed = checks.every(t => pathCheckResults[t] === true);
        btn.disabled = !allPassed;
    }

    // ---- Step 5: Deploy ----

    async function startDeploy() {
        goToStep(5);
        deploying = true;
        document.getElementById('setup-prev-5').disabled = true;
        document.getElementById('setup-finish').disabled = true;

        const progressDiv = document.getElementById('deploy-progress');
        const logDiv = document.getElementById('deploy-log');
        progressDiv.innerHTML = '';
        logDiv.innerHTML = '';
        const launchTip = document.getElementById('setup-launch-tip');
        if (launchTip) launchTip.style.display = 'none';
        const bilibiliCard = document.getElementById('setup-bilibili-login-card');
        if (bilibiliCard) bilibiliCard.style.display = selectedComponents.includes('bilibili') ? '' : 'none';
        if (!selectedComponents.includes('bilibili')) {
            bilibiliLoginContext = null;
            bilibiliLoginJobId = null;
            releaseBilibiliQr();
        }

        const wizardData = collectWizardData();
        activeWizardData = wizardData;

        // Preflight: Git is required for automated dependency deployment.
        addProgressItem(progressDiv, 'preflight-git', '🔧 检查 Git', 'running');
        try {
            const gitBootstrap = await runGitBootstrapAttempt(
                wizardData,
                () => apiPost('/api/setup/bootstrap/git', {})
            );
            if (!gitBootstrap || gitBootstrap.status !== 'ok') {
                const detail = gitBootstrap?.message || 'Git 自动安装或验证失败';
                updateProgressItem('preflight-git', 'error', `❌ Git 准备失败: ${detail}`);
                addLogLine(logDiv, `[Setup] ERROR: ${detail}\n`);
                deploying = false;
                document.getElementById('setup-prev-5').disabled = false;
                document.getElementById('setup-finish').disabled = true;
                return;
            }
            updateProgressItem('preflight-git', 'done', `✅ ${gitBootstrap.message || 'Git 已就绪'}`);
            addLogLine(logDiv, `[Setup] ${gitBootstrap.message || 'Git 已就绪'}\n`);
        } catch (e) {
            const detail = e?.message || String(e);
            updateProgressItem('preflight-git', 'error', `❌ Git 准备失败: ${detail}`);
            addLogLine(logDiv, `[Setup] ERROR: Git bootstrap 请求失败: ${detail}\n`);
            deploying = false;
            document.getElementById('setup-prev-5').disabled = false;
            document.getElementById('setup-finish').disabled = true;
            return;
        }

        // Phase 1: Generate configs
        addProgressItem(progressDiv, 'config-gen', '📄 生成配置文件', 'running');
        addLogLine(logDiv, '[Setup] 正在生成配置文件...\n');

        try {
            const configResult = await runDiscordConfigAttempt(
                wizardData,
                requestData => apiPost('/api/setup/configs/generate', requestData)
            );
            if (configResult.errors && configResult.errors.length) {
                const detail = configResult.errors.join('；');
                updateProgressItem('config-gen', 'error',
                    `❌ 配置生成存在错误: ${detail}`);
                configResult.errors.forEach(err => addLogLine(logDiv, `[Setup] ERROR: ${err}\n`));
                addLogLine(logDiv, '[Setup] 配置生成失败，已中止部署。请修复错误后重试。\n');
                deploying = false;
                document.getElementById('setup-prev-5').disabled = false;
                document.getElementById('setup-finish').disabled = true;
                return;
            } else {
                updateProgressItem('config-gen', 'done',
                    `✅ 配置生成完成 (${configResult.generated.length} 个文件)`);
            }
            configResult.generated.forEach(f => addLogLine(logDiv, `[Setup] 已生成: ${f}\n`));
            if (configResult.patched && configResult.patched.length) {
                configResult.patched.forEach(f => addLogLine(logDiv, `[Setup] 适配器链路配置已更新: ${f}\n`));
            }
            if (configResult.backups.length) {
                addLogLine(logDiv, `[Setup] 已备份 ${configResult.backups.length} 个旧配置\n`);
            }
        } catch (e) {
            updateProgressItem('config-gen', 'error', `❌ 配置生成失败: ${e.message}`);
            addLogLine(logDiv, `[Setup] ERROR: ${e.message}\n`);
            addLogLine(logDiv, '[Setup] 配置生成失败，已中止部署。请修复错误后重试。\n');
            deploying = false;
            document.getElementById('setup-prev-5').disabled = false;
            document.getElementById('setup-finish').disabled = true;
            return;
        }

        // Phase 1.5: Configure NapCat connection (only if qq selected)
        if (selectedComponents.includes('qq')) {
            addProgressItem(progressDiv, 'napcat-config', '🔗 配置 NapCat 连接', 'running');
            addLogLine(logDiv, '\n[Setup] 正在配置 NapCat WebSocket/HTTP 连接...\n');

            try {
                const napcatPath = document.getElementById('path-napcat')?.value.trim() || '';
                const qqAccount = wizardData.core?.qq_account || '';
                const ncResult = await apiPost('/api/setup/napcat/configure', {
                    napcat_dir: napcatPath,
                    qq_account: qqAccount,
                });

                if (ncResult.errors && ncResult.errors.length) {
                    const detail = ncResult.errors.join('；');
                    updateProgressItem('napcat-config', 'error',
                        `❌ NapCat 配置失败: ${detail}`);
                    ncResult.errors.forEach(err => addLogLine(logDiv, `[Setup] ERROR: NapCat: ${err}\n`));
                    addLogLine(logDiv, '[Setup] NapCat 自动配置失败，已中止部署。请修复错误后重试。\n');
                    deploying = false;
                    document.getElementById('setup-prev-5').disabled = false;
                    document.getElementById('setup-finish').disabled = true;
                    return;
                }

                if (ncResult.configured.length > 0) {
                    updateProgressItem('napcat-config', 'done',
                        `✅ NapCat 配置完成 (${ncResult.configured.join(', ')})`);
                } else {
                    updateProgressItem('napcat-config', 'done',
                        '✅ NapCat 已有配置，无需修改');
                }

                ncResult.configured.forEach(f => addLogLine(logDiv, `[Setup] 已配置: ${f} (WS客户端 + 日记HTTP + B站视频HTTP)\n`));
                ncResult.skipped.forEach(f => addLogLine(logDiv, `[Setup] 跳过 (已有配置): ${f}\n`));
            } catch (e) {
                const detail = e?.message || String(e);
                updateProgressItem('napcat-config', 'error', `❌ NapCat 配置请求失败: ${detail}`);
                addLogLine(logDiv, `[Setup] ERROR: NapCat 自动配置请求失败: ${detail}\n`);
                addLogLine(logDiv, '[Setup] NapCat 自动配置失败，已中止部署。请修复错误后重试。\n');
                deploying = false;
                document.getElementById('setup-prev-5').disabled = false;
                document.getElementById('setup-finish').disabled = true;
                return;
            }
        }

        // Phase 2: Install dependencies
        addProgressItem(progressDiv, 'dep-install', '📦 安装依赖', 'running');

        try {
            const tasks = await apiGet(`/api/setup/deps/tasks?components=${wizardData.components.join(',')}`);

            if (tasks.length === 0) {
                updateProgressItem('dep-install', 'done', '✅ 无需安装依赖');
            } else {
                tasks.forEach(t => {
                    addProgressItem(progressDiv, `dep-${t.id}`, `  📁 ${t.name}`, 'pending');
                });
                await installDepsViaWebSocket(tasks, progressDiv, logDiv);
                updateProgressItem('dep-install', 'done', '✅ 依赖安装完成');
            }
        } catch (e) {
            updateProgressItem('dep-install', 'error', `❌ 依赖安装失败: ${e.message}`);
            addLogLine(logDiv, `[Setup] ERROR: ${e.message}\n`);
            addLogLine(logDiv, '[Setup] 部署已中止，请修复上述错误后重试。\n');
            deploying = false;
            document.getElementById('setup-prev-5').disabled = false;
            document.getElementById('setup-finish').disabled = true;
            return;
        }

        if (selectedComponents.includes('bilibili')) {
            const loginContext = { progressDiv, logDiv };
            bilibiliLoginContext = loginContext;
            addProgressItem(progressDiv, 'bilibili-login', '📺 Bilibili 登录', 'running');
            const loginOk = await startBilibiliLogin(loginContext);
            if (!loginOk) {
                deploying = false;
                document.getElementById('setup-prev-5').disabled = false;
                document.getElementById('setup-finish').disabled = true;
                return;
            }
        }

        completeDeployment(progressDiv);
    }

    function installDepsViaWebSocket(tasks, progressDiv, logDiv) {
        return new Promise((resolve, reject) => {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = createAuthenticatedWebSocket(`${proto}//${location.host}/ws/setup/install`);
            let settled = false;

            const resolveOnce = () => {
                if (settled) return;
                settled = true;
                resolve();
            };
            const rejectOnce = (error) => {
                if (settled) return;
                settled = true;
                reject(error);
            };

            ws.onopen = () => {
                ws.send(JSON.stringify({ action: 'install', tasks }));
            };

            ws.onmessage = (evt) => {
                const msg = JSON.parse(evt.data);

                if (msg.type === 'task_start') {
                    updateProgressItem(`dep-${msg.task_id}`, 'running', `  🔄 ${msg.name}...`);
                    addLogLine(logDiv, `\n[Setup] === 安装 ${msg.name} ===\n`);
                } else if (msg.type === 'log') {
                    addLogLine(logDiv, msg.line);
                } else if (msg.type === 'task_done') {
                    if (msg.status === 'ok') {
                        updateProgressItem(`dep-${msg.task_id}`, 'done', `  ✅ ${msg.message}`);
                    } else {
                        updateProgressItem(`dep-${msg.task_id}`, 'error', `  ❌ ${msg.message}`);
                    }
                } else if (msg.type === 'all_done') {
                    resolveOnce();
                    ws.close();
                } else if (msg.type === 'error') {
                    rejectOnce(new Error(msg.message || '依赖安装失败'));
                    ws.close();
                }
            };

            ws.onerror = () => rejectOnce(new Error('WebSocket 连接失败'));
            ws.onclose = (evt) => {
                if (!settled) {
                    rejectOnce(new Error(
                        evt.wasClean ? '依赖安装连接提前关闭' : '依赖安装连接异常关闭'
                    ));
                }
            };
        });
    }

    async function startBilibiliLogin(context) {
        const card = document.getElementById('setup-bilibili-login-card');
        const retryBtn = document.getElementById('setup-bilibili-retry');
        if (card) card.style.display = '';
        if (retryBtn) retryBtn.style.display = 'none';
        releaseBilibiliQr();
        setBilibiliLoginUi('waiting', '正在启动', '请稍候，正在准备 Bilibili 登录二维码。');
        updateProgressItem('bilibili-login', 'running', '📺 等待 Bilibili 扫码确认');

        try {
            const started = await apiPost('/api/setup/bilibili/login/start', {});
            bilibiliLoginJobId = String(started?.job_id || '');
            if (!bilibiliLoginJobId) throw new Error('登录任务未创建');

            let qrShown = false;
            while (true) {
                const jobId = bilibiliLoginJobId;
                const status = await apiGet(
                    `/api/setup/bilibili/login/status/${encodeURIComponent(jobId)}`
                );
                const state = status?.status || status?.state || 'waiting';
                if (state === 'success') {
                    setBilibiliLoginUi('success', '登录成功', status.message || 'Bilibili 登录已验证。');
                    releaseBilibiliQr();
                    updateProgressItem('bilibili-login', 'done', '📺 Bilibili 登录已验证');
                    if (retryBtn) retryBtn.style.display = 'none';
                    return true;
                }

                if (state === 'error' || state === 'expired') {
                    const safeMessage = status.message || '二维码登录失败或已过期，请重试。';
                    setBilibiliLoginUi('error', '登录失败', safeMessage);
                    if (retryBtn) retryBtn.style.display = '';
                    updateProgressItem('bilibili-login', 'error', '❌ Bilibili 登录失败，请重试');
                    if (context?.logDiv) addLogLine(context.logDiv, '[Setup] Bilibili 登录未完成，请扫描新二维码重试。\n');
                    return false;
                }

                setBilibiliLoginUi(
                    'waiting',
                    status.qr_ready ? '等待扫码确认' : '正在准备二维码',
                    '请使用 Bilibili App 扫描二维码，并在 App 中确认登录。'
                );
                if (status.qr_ready && !qrShown) {
                    qrShown = await fetchBilibiliQr(jobId);
                }
                await delay(1000);
            }
        } catch (error) {
            setBilibiliLoginUi('error', '登录失败', error?.message || 'Bilibili 登录请求失败，请重试。');
            if (retryBtn) retryBtn.style.display = '';
            updateProgressItem('bilibili-login', 'error', '❌ Bilibili 登录请求失败，请重试');
            if (context?.logDiv) addLogLine(context.logDiv, '[Setup] Bilibili 登录请求失败，请重试。\n');
            return false;
        }
    }

    async function fetchBilibiliQr(jobId) {
        // Use the global fetch wrapper so X-Nachobot-Token is attached by
        // auth.js.  The protected endpoint is never assigned directly to img.src.
        const response = await fetch(
            `/api/setup/bilibili/login/qr/${encodeURIComponent(jobId)}`,
            { cache: 'no-store' }
        );
        if (!response.ok) return false;
        const blob = await response.blob();
        const image = document.getElementById('setup-bilibili-qr');
        const qrWrap = document.getElementById('setup-bilibili-qr-wrap');
        if (!image || !window.URL || !URL.createObjectURL) return false;
        releaseBilibiliQr();
        bilibiliQrObjectUrl = URL.createObjectURL(blob);
        image.src = bilibiliQrObjectUrl;
        if (qrWrap) qrWrap.style.display = '';
        return true;
    }

    function releaseBilibiliQr() {
        if (bilibiliQrObjectUrl && window.URL?.revokeObjectURL) {
            URL.revokeObjectURL(bilibiliQrObjectUrl);
        }
        bilibiliQrObjectUrl = null;
        const image = document.getElementById('setup-bilibili-qr');
        const qrWrap = document.getElementById('setup-bilibili-qr-wrap');
        if (image) image.removeAttribute('src');
        if (qrWrap) qrWrap.style.display = 'none';
    }

    function setBilibiliLoginUi(state, label, message) {
        const statusEl = document.getElementById('setup-bilibili-login-status');
        const messageEl = document.getElementById('setup-bilibili-login-message');
        if (statusEl) {
            statusEl.className = `bilibili-login-status ${state}`;
            statusEl.textContent = label;
        }
        if (messageEl) messageEl.textContent = message;
    }

    function delay(milliseconds) {
        return new Promise(resolve => setTimeout(resolve, milliseconds));
    }

    async function runGitBootstrapAttempt(wizardData, request) {
        activeWizardData = wizardData;
        let succeeded = false;
        try {
            const result = await request();
            succeeded = Boolean(result && result.status === 'ok');
            return result;
        } finally {
            if (!succeeded) clearDiscordTokenFromRequest(wizardData);
        }
    }

    async function runDiscordConfigAttempt(wizardData, request) {
        activeWizardData = wizardData;
        try {
            // apiPost serializes requestData synchronously before returning its
            // promise; cleanup therefore runs only after serialization settles.
            return await request(wizardData);
        } finally {
            clearDiscordTokenFromRequest(wizardData);
        }
    }

    function clearDiscordTokenFromRequest(wizardData) {
        const activeData = wizardData || activeWizardData;
        const discordTokenInput = document.getElementById('setup-discord-token');
        if (discordTokenInput) {
            discordTokenInput.value = '';
            discordTokenInput.classList?.remove('input-error');
        }
        if (activeData?.discord) delete activeData.discord.token;
        if (activeData === activeWizardData) activeWizardData = null;
    }

    function completeDeployment(progressDiv) {
        addProgressItem(progressDiv, 'done', '🎉 部署完成', 'done');
        deploying = false;
        document.getElementById('setup-prev-5').disabled = false;
        document.getElementById('setup-finish').disabled = false;

        const launchTip = document.getElementById('setup-launch-tip');
        const recommendedGroupEl = document.getElementById('setup-recommended-group');
        if (launchTip && recommendedGroupEl) {
            recommendedGroupEl.textContent = recommendedLaunchGroup;
            launchTip.style.display = 'block';
        }
    }

    // ---- UI Helpers ----

    function addProgressItem(container, id, text, status) {
        const el = document.createElement('div');
        el.className = `deploy-item ${status}`;
        el.id = `deploy-item-${id}`;
        el.innerHTML = `
            <span class="deploy-item-icon">${statusIcon(status)}</span>
            <span class="deploy-item-text">${escapeHtml(text)}</span>
        `;
        container.appendChild(el);
    }

    function updateProgressItem(id, status, text) {
        const el = document.getElementById(`deploy-item-${id}`);
        if (!el) return;
        el.className = `deploy-item ${status}`;
        if (text) el.querySelector('.deploy-item-text').textContent = text;
        el.querySelector('.deploy-item-icon').textContent = statusIcon(status);
    }

    function statusIcon(status) {
        return { pending: '⏳', running: '🔄', done: '✅', error: '❌', warning: '⚠️' }[status] || '❓';
    }

    function addLogLine(container, text) {
        const line = document.createElement('div');
        line.className = 'deploy-log-line';
        line.textContent = text;
        container.appendChild(line);
        container.scrollTop = container.scrollHeight;
    }

    function setupPasswordToggle(btnId, inputId) {
        const btn = document.getElementById(btnId);
        const input = document.getElementById(inputId);
        if (btn && input) {
            btn.addEventListener('click', () => {
                input.type = input.type === 'password' ? 'text' : 'password';
            });
        }
    }

    /** Validate single-value input: no commas, Chinese commas, semicolons */
    function validateSingleValue(input) {
        const separators = /[,，、;；]/;
        if (separators.test(input.value)) {
            input.classList.add('input-error');
            let hint = input.parentElement.querySelector('.input-error-hint');
            if (!hint) {
                hint = document.createElement('div');
                hint.className = 'input-error-hint';
                hint.textContent = '每个输入框只能填写一个值，请点击 ＋ 添加更多';
                input.parentElement.appendChild(hint);
            }
            return false;
        } else {
            input.classList.remove('input-error');
            const hint = input.parentElement.querySelector('.input-error-hint');
            if (hint) hint.remove();
            return true;
        }
    }

    return {
        init,
        refresh,
        // Explicitly test-only hooks.  They keep request-secret lifecycle
        // contracts executable without changing normal browser behavior.
        __test: {
            collectWizardData,
            onComponentToggle,
            runGitBootstrapAttempt,
            runDiscordConfigAttempt,
            clearDiscordTokenFromRequest,
        },
    };
})();
