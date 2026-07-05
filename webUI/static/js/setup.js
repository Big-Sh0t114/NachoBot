/**
 * NachoBot WebUI — Setup Wizard Module
 * 5-step deployment wizard: env check → component select → config form → integrity check → deploy
 */

const SetupModule = (() => {
    let currentStep = 1;
    let envCheckData = null;
    let selectedComponents = ['core']; // core is always selected
    let deploying = false;
    let _loadingDefaults = false;
    let defaultsLoaded = false;

    // Track path verification results
    let pathCheckResults = {};

    function init() {
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
            if (!defaultsLoaded) {
                loadConfigDefaults();
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
        return el;
    }

    // ---- Step 2: Component Selection ----

    function onComponentToggle() {
        selectedComponents = ['core'];
        document.querySelectorAll('.setup-component-cb:checked').forEach(cb => {
            selectedComponents.push(cb.value);
        });
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
            titleEl.textContent = 'gpt lite 方案';
            descEl.textContent = '检测到可用显存介于 4G 到 6G 之间。推荐使用 gpt lite 方案部署（仅启用 GPT-SoVITS 合成，不启用本地 Florence-2 VLM 视觉与 FunASR 语音识别，以节约显存）。';
            recommendedLaunchGroup = 'TTS 语音 (LITE)';
            createApplyButton('一键应用此方案', () => applyScheme('gpt_lite'));
        }
        else if (vramGb >= 6.0 && vramGb <= 8.0) {
            // VRAM in [6GB, 8GB]
            titleEl.textContent = 'gpt FULL 方案';
            descEl.textContent = '检测到可用显存介于 6G 到 8G 之间。推荐使用 gpt FULL 方案部署（启用 GPT-SoVITS 语音合成 + Florence-2 视觉大模型 + FunASR 语音识别，满足全功能交互需求）。';
            recommendedLaunchGroup = 'TTS 语音 (FULL)';
            createApplyButton('一键应用此方案', () => applyScheme('gpt_full'));
        }
        else if (vramGb > 8.0 && vramGb <= 12.0) {
            // VRAM in (8GB, 12GB]
            titleEl.textContent = 'gpt FULL 方案 或 vox LITE 方案';
            descEl.textContent = '检测到可用显存介于 8G 到 12G 之间。您可以选择使用轻量合成但全功能的 gpt FULL 方案，或者尝试使用高质量拟真拟音但消耗较大的 vox LITE 方案（仅使用 VoxCPM 语音合成，不开启视觉和语音识别）。';
            recommendedLaunchGroup = 'TTS 语音 (FULL)';
            createApplyButton('应用 gpt FULL 方案', () => {
                recommendedLaunchGroup = 'TTS 语音 (FULL)';
                applyScheme('gpt_full');
            });
            createApplyButton('应用 vox LITE 方案', () => {
                recommendedLaunchGroup = 'TTS 语音 (LITE)';
                applyScheme('vox_lite');
            });
        }
        else {
            // VRAM > 12GB
            titleEl.textContent = 'Vox Full 方案';
            descEl.textContent = '检测到可用显存大于 12G。推荐使用极致画质和音质的 Vox Full 方案（VoxCPM 语音合成 + Florence-2 视觉大模型 + FunASR 语音识别，完美释放大显存显卡潜力）。';
            recommendedLaunchGroup = 'TTS 语音 (FULL)';
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
        document.getElementById('setup-tts-section').style.display =
            selectedComponents.includes('tts') ? '' : 'none';
        document.getElementById('setup-universalvc-section').style.display =
            selectedComponents.includes('universalvc') ? '' : 'none';
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
        if (_loadingDefaults) return;
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

    // ---- Load defaults from template files ----

    async function loadConfigDefaults() {
        _loadingDefaults = true;

        // Show loading overlay on Step 3 panel
        const step3 = document.getElementById('setup-step-3');
        const overlay = document.createElement('div');
        overlay.id = 'setup-defaults-loading';
        overlay.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(255,255,255,0.75);z-index:10;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:12px;backdrop-filter:blur(2px);';
        overlay.innerHTML = '<div class="setup-spinner"></div><p style="margin-top:12px;color:var(--text-secondary);font-size:0.9rem;">正在读取模板配置...</p>';
        const origPosition = step3.style.position;
        step3.style.position = 'relative';
        step3.appendChild(overlay);

        // Disable form inputs during load (keep nav buttons enabled)
        const inputs = step3.querySelectorAll('input, select, textarea, button:not(#setup-prev-3):not(#setup-next-3)');
        inputs.forEach(el => { if (!el.disabled) { el.disabled = true; el.dataset._wasEnabled = '1'; } });

        try {
            const defaults = await apiGet('/api/setup/configs/defaults');

            // Core fields
            const qqEl = document.getElementById('setup-qq-account');
            if (qqEl) qqEl.value = defaults.core?.qq_account || '';
            const nickEl = document.getElementById('setup-nickname');
            if (nickEl) nickEl.value = defaults.core?.nickname || '';

            // TTS engine
            const ttsEl = document.getElementById('setup-tts-engine');
            if (ttsEl) ttsEl.value = defaults.tts?.engine || 'GPT_Sovits';

            // UniversalVC fields
            if (defaults.universalvc) {
                const uvc = defaults.universalvc;
                const pEl = document.getElementById('setup-uvc-process');
                if (pEl) pEl.value = uvc.target_process_name || '';
                const oEl = document.getElementById('setup-uvc-output-device');
                if (oEl) oEl.value = uvc.output_device || '';
                const dEl = document.getElementById('setup-uvc-denoise');
                if (dEl) dEl.checked = uvc.denoise_enabled ?? true;
                const sEl = document.getElementById('setup-uvc-speaker');
                if (sEl) sEl.checked = uvc.speaker_enabled ?? true;
            }

            // Provider rows — replace placeholders with template data
            const providerContainer = document.getElementById('provider-rows');
            if (providerContainer) {
                providerContainer.innerHTML = '';
                const providers = defaults.providers || [];
                if (providers.length === 0) {
                    addProviderRow();
                } else {
                    providers.forEach(p => {
                        const row = createProviderRow();
                        providerContainer.appendChild(row);
                        bindProviderRow(row);
                        row.querySelector('.setup-provider-name').value = p.name || '';
                        row.querySelector('.setup-provider-url').value = p.base_url || '';
                        row.querySelector('.setup-provider-key').value = p.api_key || '';

                        if ((p.name || '').toLowerCase() === 'siliconflow') {
                            row.querySelector('.setup-provider-name').readOnly = true;
                            row.querySelector('.setup-provider-url').readOnly = true;
                            row.querySelector('.btn-remove-row').style.display = 'none';

                            const keyLabel = row.querySelector('.setup-provider-key').closest('.form-row').querySelector('.form-label');
                            if (keyLabel) {
                                keyLabel.innerHTML = 'API Key <span style="color: var(--error); margin-left: 4px;">*</span>';
                            }

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🔒</span>
                                <div>
                                    <strong>强制锁定：核心长期记忆系统服务商</strong><br>
                                    本系统依赖 SiliconFlow 的bge-m3免费向量模型，请在下方填入有效的 API Key。
                                </div>
                                <a href="https://cloud.siliconflow.cn/i/vwQ4iW0r" target="_blank" class="btn btn-primary" style="margin-left: auto; text-decoration: none; font-size: 0.85rem; padding: 6px 14px; border-radius: var(--radius-sm); white-space: nowrap; box-shadow: var(--shadow-sm);">获取 API Key 🔗</a>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if ((p.name || '').toLowerCase() === 'localmodel' || (p.name || '').toLowerCase() === 'localmodellarge') {
                            row.querySelector('.setup-provider-name').readOnly = true;
                            row.querySelector('.setup-provider-url').readOnly = true;
                            row.querySelector('.setup-provider-key').readOnly = true;
                            row.querySelector('.btn-remove-row').style.display = 'none';

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🔒</span>
                                <div>
                                    <strong>强制锁定：本地模型专用连接</strong><br>
                                    向导自带本地服务的预留接口，为保障本地引擎运行，请勿修改或删除此项。
                                </div>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if ((p.name || '').toLowerCase() === 'qhaigc') {
                            row.querySelector('.setup-provider-name').readOnly = true;
                            row.querySelector('.setup-provider-url').readOnly = true;

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.style.background = 'rgba(59, 130, 246, 0.08)';
                            hintBanner.style.borderColor = 'rgba(59, 130, 246, 0.2)';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🌐</span>
                                <div>
                                    <strong>可选配置：联网查询服务商</strong><br>
                                    专供内置联网模型 grok-4.1#search 使用。如无需联网功能，可删除此项及对应模型。<br><span style="color: var(--accent);">价格参考：输入 0.2 / 输出 0.5 (元 / 万 token)</span>
                                </div>
                                <a href="https://www.qhaigc.net" target="_blank" class="btn btn-primary" style="margin-left: auto; text-decoration: none; font-size: 0.85rem; padding: 6px 14px; border-radius: var(--radius-sm); white-space: nowrap; box-shadow: var(--shadow-sm);">获取 API Key 🔗</a>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if ((p.name || '').toLowerCase() === 'deepseek') {
                            row.querySelector('.setup-provider-name').readOnly = true;
                            row.querySelector('.setup-provider-url').readOnly = true;

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.style.background = 'rgba(245, 158, 11, 0.08)';
                            hintBanner.style.borderColor = 'rgba(245, 158, 11, 0.2)';
                            hintBanner.innerHTML = `
                                <span class="hint-icon" style="color: #f59e0b;">💎</span>
                                <div>
                                    <strong>强烈推荐：高性价比服务商</strong><br>
                                    Deepseek 官方接口，为系统提供顶级代码与文本生成能力。强烈推荐您保留并配置此项。<br><span style="color: #f59e0b;">优势：极致性能与极低价格</span>
                                </div>
                                <a href="https://platform.deepseek.com/" target="_blank" class="btn btn-primary" style="margin-left: auto; text-decoration: none; font-size: 0.85rem; padding: 6px 14px; border-radius: var(--radius-sm); white-space: nowrap; box-shadow: var(--shadow-sm); background: #f59e0b; color: white; border: none;">获取 API Key 🔗</a>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        }
                    });
                }
            }

            // Model rows — replace placeholders with template data
            const modelContainer = document.getElementById('model-rows');
            if (modelContainer) {
                modelContainer.innerHTML = '';
                const models = defaults.models || [];
                if (models.length === 0) {
                    addModelRow();
                } else {
                    models.forEach(m => {
                        const row = createModelRow();
                        modelContainer.appendChild(row);
                        bindModelRow(row);
                        row.dataset.defaultProvider = m.api_provider || '';
                        row.querySelector('.setup-model-id').value = m.model_identifier || '';
                        if (m.model_name) {
                            row.querySelector('.setup-model-name').value = m.model_name;
                            row.querySelector('.setup-model-name').dataset.userEdited = 'true';
                        }

                        const mId = (m.model_identifier || '').toLowerCase();
                        const mName = (m.model_name || '').toLowerCase();
                        const isBge = mId.includes('bge-m3') || mName.includes('bge-m3');
                        const isLocal = mName === 'sensevoice-small' || mName === 'florence-2' || mName === 'teleai/telespeechasr/silicon'; // Assuming SenseVoice and Florence
                        const isFree = mName.endsWith('/silicon');
                        const isSearch = mName.includes('#search') || mId.includes('#search');

                        if (isBge) {
                            row.querySelector('.setup-model-id').readOnly = true;
                            row.querySelector('.setup-model-name').readOnly = true;
                            const sel = row.querySelector('.setup-model-provider');
                            if (sel) {
                                sel.style.pointerEvents = 'none';
                                sel.style.opacity = '0.7';
                            }
                            row.querySelector('.btn-remove-row').style.display = 'none';

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🔒</span>
                                <div>
                                    <strong>强制锁定：核心长记忆向量模型</strong><br>
                                    为防止记忆空间错乱，本向导限制修改此依赖。如需更换，部署后请直接编辑配置并清空 data/embedding 目录。
                                </div>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if (mName === 'sensevoice-small' || mName === 'florence-2') {
                            row.querySelector('.setup-model-id').readOnly = true;
                            row.querySelector('.setup-model-name').readOnly = true;
                            const sel = row.querySelector('.setup-model-provider');
                            if (sel) {
                                sel.style.pointerEvents = 'none';
                                sel.style.opacity = '0.7';
                            }
                            row.querySelector('.btn-remove-row').style.display = 'none';

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🔒</span>
                                <div>
                                    <strong>强制锁定：本地多模态模型</strong><br>
                                    部署向导会自动拉起这些本地模型（语音/视觉）。请勿修改此条目，以免本地功能异常。
                                </div>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if (isSearch) {
                            row.querySelector('.setup-model-id').readOnly = true;
                            row.querySelector('.setup-model-name').readOnly = true;
                            const sel = row.querySelector('.setup-model-provider');
                            if (sel) {
                                sel.style.pointerEvents = 'none';
                                sel.style.opacity = '0.7';
                            }

                            const hintBanner = document.createElement('div');
                            hintBanner.className = 'setup-hint-banner';
                            hintBanner.style.marginBottom = '12px';
                            hintBanner.style.alignItems = 'center';
                            hintBanner.style.background = 'rgba(59, 130, 246, 0.08)';
                            hintBanner.style.borderColor = 'rgba(59, 130, 246, 0.2)';
                            hintBanner.innerHTML = `
                                <span class="hint-icon">🌐</span>
                                <div>
                                    <strong>半锁定项：联网查询模型（可选）</strong><br>
                                    此模型原生支持联网搜索查询。其服务商已被锁定，但若您无需联网功能，可自由<strong style="color: var(--error);">删除</strong>此模型。<br><span style="color: var(--accent);">价格参考：输入 0.2 / 输出 0.5 (元 / 百万 token)</span>
                                </div>
                            `;
                            row.insertBefore(hintBanner, row.firstChild);
                        } else if (isFree) {
                            // Lock provider for free models
                            const sel = row.querySelector('.setup-model-provider');
                            if (sel) {
                                sel.style.pointerEvents = 'none';
                                sel.style.opacity = '0.7';
                            }
                        }

                        // Add visual badges
                        const nameContainer = row.querySelector('.setup-model-name').parentNode;
                        if (isFree) {
                            const badge = document.createElement('span');
                            badge.className = 'form-row-tag';
                            badge.style.background = 'rgba(16, 185, 129, 0.1)';
                            badge.style.color = '#10b981';
                            badge.style.border = '1px solid rgba(16, 185, 129, 0.2)';
                            badge.style.padding = '4px 8px';
                            badge.style.marginTop = '6px';
                            badge.textContent = 'Silicon 免费';
                            badge.title = '此模型为硅基流动免费提供，部分模型可能停止服务，请以其官网政策为准';
                            nameContainer.appendChild(badge);
                        }
                        if (mName === 'sensevoice-small' || mName === 'florence-2') {
                            const badge = document.createElement('span');
                            badge.className = 'form-row-tag';
                            badge.style.background = 'rgba(16, 185, 129, 0.1)';
                            badge.style.color = '#10b981';
                            badge.style.border = '1px solid rgba(16, 185, 129, 0.2)';
                            badge.style.padding = '4px 8px';
                            badge.style.marginTop = '6px';
                            badge.textContent = '本地免费';
                            badge.title = '项目自带的本地模型，无推理费用';
                            nameContainer.appendChild(badge);
                        }
                        if (isSearch) {
                            const badge = document.createElement('span');
                            badge.className = 'form-row-tag';
                            badge.style.background = 'rgba(59, 130, 246, 0.1)';
                            badge.style.color = '#3b82f6';
                            badge.style.border = '1px solid rgba(59, 130, 246, 0.2)';
                            badge.style.padding = '4px 8px';
                            badge.style.marginTop = '6px';
                            badge.textContent = '🌐 联网查询模型';
                            badge.title = '此模型原生支持联网搜索查询，建议配置';
                            nameContainer.appendChild(badge);
                        }
                        if (mName === 'deepseek-v4-pro' || mName === 'deepseek-v4-flash') {
                            const badge = document.createElement('span');
                            badge.className = 'form-row-tag';
                            badge.style.background = 'rgba(245, 158, 11, 0.1)';
                            badge.style.color = '#f59e0b';
                            badge.style.border = '1px solid rgba(245, 158, 11, 0.2)';
                            badge.style.padding = '4px 8px';
                            badge.style.marginTop = '6px';
                            badge.textContent = '高性价比';
                            badge.title = '高性价比，推荐作为思考/对话模型';
                            nameContainer.appendChild(badge);
                        }
                    });
                }
            }

            // Sync dropdowns (populates provider options in model rows)
            syncProviderDropdowns();

            // Now set the model-provider selects from the stashed default provider
            document.querySelectorAll('.model-row').forEach(row => {
                const defProv = row.dataset.defaultProvider;
                if (defProv) {
                    const select = row.querySelector('.setup-model-provider');
                    if (select) {
                        const options = Array.from(select.options);
                        if (options.some(o => o.value === defProv)) {
                            select.value = defProv;
                        }
                    }
                }
            });

            // Populate model group fields with per-group defaults from template
            const modelGroups = defaults.model_groups || {};
            const groupIds = {
                replyer0: 'setup-grp-replyer0',
                planner: 'setup-grp-planner',
                utils: 'setup-grp-utils',
                utils_small: 'setup-grp-utils-small',
                tool_use: 'setup-grp-tool-use',
            };
            for (const [key, id] of Object.entries(groupIds)) {
                const val = modelGroups[key];
                if (val) {
                    const el = document.getElementById(id);
                    if (el) el.value = val;
                }
            }

            defaultsLoaded = true;
        } catch (e) {
            console.error('Failed to load config defaults:', e);
        } finally {
            _loadingDefaults = false;
            // Remove loading overlay
            const ov = document.getElementById('setup-defaults-loading');
            if (ov) ov.remove();
            step3.style.position = origPosition;
            // Re-enable form inputs
            inputs.forEach(el => { if (el.dataset._wasEnabled) { el.disabled = false; delete el.dataset._wasEnabled; } });
        }
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


        return {
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
    }

    // ---- Step 3 → Step 4 validation ----

    function onStep3Next() {
        // Check single-value validation
        let allValid = true;
        document.querySelectorAll('#setup-step-3 .setup-single-value').forEach(input => {
            if (!validateSingleValue(input)) allValid = false;
        });
        if (!allValid) return;

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

        goToStep(4);
    }

    // ---- Step 4: Integrity Check ----

    function getRequiredChecks() {
        const checks = [];
        if (selectedComponents.includes('qq')) checks.push('napcat');
        if (selectedComponents.includes('discord')) checks.push('nodejs');
        if (selectedComponents.includes('bilibili')) checks.push('bilibili_dll');
        if (selectedComponents.includes('tts')) {
            const engine = document.getElementById('setup-tts-engine')?.value || 'GPT_Sovits';
            if (engine === 'GPT_Sovits') checks.push('sovits');
            else if (engine === 'Vox') checks.push('voxcpm');
        }
        if (selectedComponents.includes('universalvc')) checks.push('vb_cable');
        return checks;
    }

    function updatePathCheckVisibility() {
        const checks = getRequiredChecks();
        const allTypes = ['napcat', 'nodejs', 'bilibili_dll', 'sovits', 'voxcpm', 'vb_cable'];
        // Map type to card ID
        const cardMap = {
            napcat: 'path-check-napcat',
            nodejs: 'path-check-nodejs',
            bilibili_dll: 'path-check-bilibili',
            sovits: 'path-check-sovits',
            voxcpm: 'path-check-voxcpm',
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

        const wizardData = collectWizardData();

        // Phase 1: Generate configs
        addProgressItem(progressDiv, 'config-gen', '📄 生成配置文件', 'running');
        addLogLine(logDiv, '[Setup] 正在生成配置文件...\n');

        try {
            const configResult = await apiPost('/api/setup/configs/generate', wizardData);
            if (configResult.errors && configResult.errors.length) {
                updateProgressItem('config-gen', 'warning',
                    `⚠️ 配置生成完成 (${configResult.generated.length} 成功, ${configResult.errors.length} 失败)`);
                configResult.errors.forEach(err => addLogLine(logDiv, `[Setup] ERROR: ${err}\n`));
            } else {
                updateProgressItem('config-gen', 'done',
                    `✅ 配置生成完成 (${configResult.generated.length} 个文件)`);
            }
            configResult.generated.forEach(f => addLogLine(logDiv, `[Setup] 已生成: ${f}\n`));
            if (configResult.patched && configResult.patched.length) {
                configResult.patched.forEach(f => addLogLine(logDiv, `[Setup] TTS链路已调整: ${f}\n`));
            }
            if (configResult.backups.length) {
                addLogLine(logDiv, `[Setup] 已备份 ${configResult.backups.length} 个旧配置\n`);
            }
        } catch (e) {
            updateProgressItem('config-gen', 'error', `❌ 配置生成失败: ${e.message}`);
            addLogLine(logDiv, `[Setup] ERROR: ${e.message}\n`);
            deploying = false;
            document.getElementById('setup-prev-5').disabled = false;
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
                    updateProgressItem('napcat-config', 'warning',
                        `⚠️ NapCat 配置完成 (${ncResult.configured.length} 成功, ${ncResult.errors.length} 失败)`);
                    ncResult.errors.forEach(err => addLogLine(logDiv, `[Setup] ERROR: ${err}\n`));
                } else if (ncResult.configured.length > 0) {
                    updateProgressItem('napcat-config', 'done',
                        `✅ NapCat 配置完成 (${ncResult.configured.join(', ')})`);
                } else {
                    updateProgressItem('napcat-config', 'done',
                        '✅ NapCat 已有配置，无需修改');
                }

                ncResult.configured.forEach(f => addLogLine(logDiv, `[Setup] 已配置: ${f} (WS客户端 + 日记HTTP + B站视频HTTP)\n`));
                ncResult.skipped.forEach(f => addLogLine(logDiv, `[Setup] 跳过 (已有配置): ${f}\n`));
            } catch (e) {
                updateProgressItem('napcat-config', 'warning', `⚠️ NapCat 配置失败: ${e.message}`);
                addLogLine(logDiv, `[Setup] WARNING: NapCat 自动配置失败，请手动前往 NapCat WebUI 配置\n`);
                // Don't block deployment — NapCat config is best-effort
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
        }

        // Done
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

    function installDepsViaWebSocket(tasks, progressDiv, logDiv) {
        return new Promise((resolve, reject) => {
            const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = new WebSocket(`${proto}//${location.host}/ws/setup/install`);

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
                    ws.close();
                    resolve();
                } else if (msg.type === 'error') {
                    ws.close();
                    reject(new Error(msg.message));
                }
            };

            ws.onerror = () => reject(new Error('WebSocket 连接失败'));
            ws.onclose = (evt) => { if (!evt.wasClean) resolve(); };
        });
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

    return { init, refresh };
})();
