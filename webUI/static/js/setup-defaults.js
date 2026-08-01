/**
 * Template-backed configuration hydration for the setup wizard.
 */
window.SetupDefaults = (() => {
    function create({
        apiGet,
        addProviderRow,
        createProviderRow,
        bindProviderRow,
        addModelRow,
        createModelRow,
        bindModelRow,
        syncProviderDropdowns,
    }) {
        let loading = false;
        let loaded = false;

    async function loadConfigDefaults() {
            loading = true;

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

                loaded = true;
            } catch (e) {
                console.error('Failed to load config defaults:', e);
            } finally {
                loading = false;
                // Remove loading overlay
                const ov = document.getElementById('setup-defaults-loading');
                if (ov) ov.remove();
                step3.style.position = origPosition;
                // Re-enable form inputs
                inputs.forEach(el => { if (el.dataset._wasEnabled) { el.disabled = false; delete el.dataset._wasEnabled; } });
            }
        }

        return {
            isLoaded: () => loaded,
            isLoading: () => loading,
            load: loadConfigDefaults,
        };
    }

    return { create };
})();
