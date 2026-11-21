/** Manage external HTTP TTS profiles from the Configuration tree. */
const TTSProfilesModule = (() => {
    const API = '/api/tts/profiles';
    const MAX_IMPORT_BYTES = 96 * 1024;
    const PROFILE_FIELD_IDS = [
        'id', 'name', 'url', 'method', 'response-type', 'response-path', 'timeout',
        'audio-format', 'sample-rate', 'channels', 'headers', 'body',
    ];
    let editor = null;
    let state = { profiles: [], active_profile: '', backend_enabled: false };
    let selectedId = '';
    let draftDirty = false;
    let previewUrl = '';
    let previewController = null;
    let previewGeneration = 0;

    const templates = {
        wav: {
            name: 'HTTP TTS · WAV', url: 'https://example.com/tts', method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: { text: '{{text}}', language: '{{language}}', voice: '{{voice}}' },
            response_type: 'audio', response_path: '', timeout_seconds: 60, audio_format: 'wav',
        },
        base64: {
            name: 'HTTP TTS · Base64', url: 'https://example.com/tts', method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: { text: '{{text}}', language: '{{language}}', voice: '{{voice}}' },
            response_type: 'base64', response_path: 'audio_base64', timeout_seconds: 60, audio_format: 'wav',
        },
        url: {
            name: 'HTTP TTS · Audio URL', url: 'https://example.com/tts', method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: { text: '{{text}}', language: '{{language}}', voice: '{{voice}}' },
            response_type: 'url', response_path: 'audio_url', timeout_seconds: 60, audio_format: 'wav',
        },
    };

    function cleanupPreview() {
        previewGeneration += 1;
        if (previewController) {
            previewController.abort();
            previewController = null;
        }
        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
            previewUrl = '';
        }
        const audio = document.getElementById('tts-preview-audio');
        if (audio) {
            audio.removeAttribute('src');
            audio.load();
        }
    }

    function showError(error) {
        const message = error?.message || '请求失败';
        if (typeof toast === 'function') toast(message, 'error');
        const status = document.getElementById('tts-profile-status');
        if (status) status.textContent = message;
    }

    async function open() {
        if (editor && draftDirty && !window.confirm('放弃当前未保存的更改并重新加载配置？')) return;
        editor = document.getElementById('config-editor');
        cleanupPreview();
        try {
            state = await apiGet(API);
            if (!Array.isArray(state.profiles)) state.profiles = [];
            const active = state.profiles.find(item => item.id === state.active_profile);
            const first = state.profiles[0];
            selectedId = active?.id || first?.id || '';
            draftDirty = false;
            if (selectedId) {
                renderProfile(selectedId, selectedProfile());
            } else {
                selectedId = '';
                renderProfile('', emptyProfile());
            }
        } catch (error) {
            showError(error);
        }
    }

    function emptyProfile() {
        return {
            name: '', url: '', method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: { text: '{{text}}', language: '{{language}}', voice: '{{voice}}' },
            response_type: 'audio', response_path: '', timeout_seconds: 60, audio_format: 'wav',
        };
    }

    function selectedProfile() {
        return state.profiles.find(item => item.id === selectedId)?.profile || emptyProfile();
    }

    function getField(id) {
        return document.getElementById(`tts-${id}`);
    }

    function setField(id, value) {
        const field = getField(id);
        if (field) field.value = value == null ? '' : String(value);
    }

    function getMappingField(body, token) {
        if (!body || typeof body !== 'object' || Array.isArray(body)) return token;
        return Object.keys(body).find(key => body[key] === `{{${token}}}`) || token;
    }

    function renderProfile(profileId, profile) {
        cleanupPreview();
        editor.innerHTML = `
            <div class="tts-profile-page">
                <div class="editor-header">
                    <div><span class="editor-title">自定义 HTTP TTS</span><div class="tts-profile-subtitle">管理外部 HTTP 语音服务，测试当前表单不会更改默认配置。</div></div>
                    <div class="editor-actions">
                        <button class="btn btn-ghost" id="tts-new">＋ 新建</button>
                        <button class="btn btn-ghost" id="tts-delete">删除</button>
                        <button class="btn btn-primary" id="tts-save">💾 保存</button>
                    </div>
                </div>
                <div class="tts-profile-status" id="tts-profile-status" role="status"></div>
                <div class="tts-profile-toolbar">
                    <label>已保存配置
                        <select id="tts-profile-select" class="form-select"></select>
                    </label>
                    <button class="btn btn-ghost" id="tts-default">设为默认</button>
                    <button class="btn btn-ghost" id="tts-export">导出模板</button>
                    <label class="btn btn-ghost tts-import-label" for="tts-import-file">导入 JSON</label>
                    <input id="tts-import-file" type="file" accept="application/json,.json" hidden>
                    <label class="tts-template-label" for="tts-template">快速模板</label>
                    <select id="tts-template" class="form-select">
                        <option value="">选择模板…</option>
                        <option value="wav">普通 WAV 响应</option>
                        <option value="base64">JSON Base64 响应</option>
                        <option value="url">JSON 音频 URL 响应</option>
                    </select>
                </div>
                <p class="tts-profile-hint">HTTP GET 会把请求体字段作为查询参数；POST 使用 JSON。原生流式响应暂不支持。试听只使用当前未保存表单，不会设置默认项。</p>
                <div class="tts-profile-grid">
                    <label>配置 ID<input id="tts-id" maxlength="64" autocomplete="off" placeholder="例如 my-tts"></label>
                    <label>名称<input id="tts-name" maxlength="128" placeholder="语音服务名称"></label>
                    <label class="tts-span-2">服务地址<input id="tts-url" type="url" maxlength="2048" placeholder="https://example.com/tts"></label>
                    <label>请求方式<select id="tts-method" class="form-select"><option>POST</option><option>GET</option></select></label>
                    <label>响应内容<select id="tts-response-type" class="form-select"><option value="audio">直接音频</option><option value="base64">JSON Base64</option><option value="url">JSON 音频 URL</option></select></label>
                    <label>响应字段路径<input id="tts-response-path" maxlength="256" placeholder="audio_base64 或 data.audio"></label>
                    <label>超时（秒）<input id="tts-timeout" type="number" min="1" max="120" value="60"></label>
                    <label>音频格式<select id="tts-audio-format" class="form-select"><option value="wav">WAV</option><option value="pcm">PCM16LE</option></select></label>
                    <label class="tts-pcm-field">采样率<input id="tts-sample-rate" type="number" min="1" placeholder="例如 24000"></label>
                    <label class="tts-pcm-field">声道数<input id="tts-channels" type="number" min="1" placeholder="例如 1"></label>
                    <div class="tts-span-2 tts-mapping">
                        <div class="tts-section-title">简单文本字段映射</div>
                        <div class="tts-mapping-row">
                            <label>文本字段<input id="tts-map-text" value="text"></label>
                            <label>语言字段<input id="tts-map-language" value="language"></label>
                            <label>音色字段<input id="tts-map-voice" value="voice"></label>
                            <button type="button" class="btn btn-ghost" id="tts-apply-mapping">应用映射</button>
                        </div>
                    </div>
                    <label class="tts-span-2">请求头 JSON<textarea id="tts-headers" rows="5" spellcheck="false"></textarea></label>
                    <label class="tts-span-2">请求体 JSON<textarea id="tts-body" rows="7" spellcheck="false"></textarea></label>
                    <div class="tts-span-2 tts-section-title">试听</div>
                    <label class="tts-span-2">试听文本<input id="tts-test-text" maxlength="512" value="你好，这是 HTTP TTS 试听。"></label>
                    <label>语言<input id="tts-test-language" maxlength="64" placeholder="例如 zh-CN"></label>
                    <label>音色<input id="tts-test-voice" maxlength="64" placeholder="服务端音色 ID"></label>
                    <div class="tts-preview-actions tts-span-2">
                        <button type="button" class="btn btn-primary" id="tts-test">▶ 测试并播放</button>
                        <audio id="tts-preview-audio" controls preload="none"></audio>
                    </div>
                </div>
                <p class="tts-profile-note">导出模板会移除所有请求头、URL 中的账号凭据和查询参数，并删除请求体中以 token、key、password 等命名的字段。自定义字段中的秘密值无法可靠识别，请检查导出文件后再分享。</p>
            </div>`;

        const idField = getField('id');
        const id = profileId || '';
        idField.value = id;
        setField('name', profile.name);
        setField('url', profile.url);
        setField('method', profile.method || 'POST');
        setField('response-type', profile.response_type || 'audio');
        setField('response-path', profile.response_path || '');
        setField('timeout', profile.timeout_seconds ?? 60);
        setField('audio-format', profile.audio_format || 'wav');
        setField('sample-rate', profile.sample_rate);
        setField('channels', profile.channels);
        setField('headers', JSON.stringify(profile.headers || {}, null, 2));
        setField('body', JSON.stringify(profile.body || {}, null, 2));
        setField('map-text', getMappingField(profile.body, 'text'));
        setField('map-language', getMappingField(profile.body, 'language'));
        setField('map-voice', getMappingField(profile.body, 'voice'));

        const select = getField('profile-select');
        select.innerHTML = '';
        if (!state.profiles.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = '暂无已保存配置';
            select.appendChild(option);
        } else {
            for (const item of state.profiles) {
                const option = document.createElement('option');
                option.value = item.id;
                option.textContent = `${item.profile.name || item.id}${item.id === state.active_profile ? ' · 默认' : ''}`;
                select.appendChild(option);
            }
        }
        select.value = profileId && state.profiles.some(item => item.id === profileId) ? profileId : '';
        getField('delete').disabled = !select.value;
        getField('default').disabled = !select.value;
        getField('export').disabled = !select.value;
        updatePcmFields();
        updateStatus();
        bindEvents();
    }

    function updateStatus(message = '') {
        const status = getField('profile-status');
        if (!status) return;
        if (message) {
            status.textContent = message;
            return;
        }
        const active = state.profiles.find(item => item.id === state.active_profile);
        const current = active ? `当前默认：${active.profile.name || active.id}。` : '尚未设置 HTTP TTS 默认配置。';
        const engine = state.backend_enabled
            ? 'GenericHTTP 已写入基础配置；如果 TTS 服务启动时使用其他后端，请到「一键启动」重启多模态 TTS 服务。GenericHTTP 运行期间切换 profile 会在下一次合成请求时生效。'
            : '设置默认后，请到「一键启动」重启多模态 TTS 服务，才能从现有语音后端切换到 GenericHTTP。';
        status.textContent = `${current} ${engine}`;
    }

    function bindEvents() {
        PROFILE_FIELD_IDS.map(id => getField(id)).filter(Boolean).forEach(field => {
            field.addEventListener('input', () => { draftDirty = true; });
            field.addEventListener('change', () => { draftDirty = true; });
        });
        getField('audio-format').addEventListener('change', updatePcmFields);
        getField('new').addEventListener('click', () => {
            if (draftDirty && !window.confirm('放弃当前未保存的更改并新建配置？')) return;
            selectedId = '';
            draftDirty = false;
            renderProfile('', emptyProfile());
            getField('id').focus();
        });
        getField('profile-select').addEventListener('change', () => {
            const nextId = getField('profile-select').value;
            if (draftDirty && !window.confirm('放弃当前未保存的更改？')) {
                getField('profile-select').value = selectedId;
                return;
            }
            selectedId = nextId;
            draftDirty = false;
            renderProfile(selectedId, selectedProfile());
        });
        getField('template').addEventListener('change', applyTemplate);
        getField('apply-mapping').addEventListener('click', applyTextMapping);
        getField('save').addEventListener('click', saveProfile);
        getField('default').addEventListener('click', setDefault);
        getField('delete').addEventListener('click', deleteProfile);
        getField('test').addEventListener('click', testProfile);
        getField('export').addEventListener('click', exportProfile);
        getField('import-file').addEventListener('change', importProfileFile);
    }

    function updatePcmFields() {
        const show = getField('audio-format')?.value === 'pcm';
        editor?.querySelectorAll('.tts-pcm-field').forEach(field => {
            field.style.display = show ? '' : 'none';
        });
    }

    function applyTemplate() {
        const templateId = getField('template').value;
        const template = templates[templateId];
        if (!template) return;
        Object.entries(template).forEach(([key, value]) => {
            const id = ({ response_type: 'response-type', response_path: 'response-path', timeout_seconds: 'timeout', audio_format: 'audio-format' })[key] || key;
            if (['headers', 'body'].includes(key)) setField(key, JSON.stringify(value, null, 2));
            else setField(id, value);
        });
        if (!getField('id').value) setField('id', templateId === 'wav' ? 'http-tts' : `http-tts-${templateId}`);
        draftDirty = true;
        updatePcmFields();
        updateStatus('模板已填入表单，保存后才会写入配置。');
        getField('template').value = '';
    }

    function parseJsonField(id, label) {
        try {
            return JSON.parse(getField(id).value || '{}');
        } catch (_) {
            throw new Error(`${label} JSON 格式无效`);
        }
    }

    function readProfileForm() {
        const profile = {
            name: getField('name').value.trim(),
            url: getField('url').value.trim(),
            method: getField('method').value,
            headers: parseJsonField('headers', '请求头'),
            body: parseJsonField('body', '请求体'),
            response_type: getField('response-type').value,
            response_path: getField('response-path').value.trim(),
            timeout_seconds: Number(getField('timeout').value || 60),
            audio_format: getField('audio-format').value,
        };
        if (profile.audio_format === 'pcm') {
            profile.sample_rate = Number(getField('sample-rate').value);
            profile.channels = Number(getField('channels').value);
        }
        return profile;
    }

    function readId() {
        const id = getField('id').value.trim();
        if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) {
            throw new Error('配置 ID 只能包含小写字母、数字、连字符和下划线，长度为 1–64');
        }
        return id;
    }

    function applyTextMapping() {
        try {
            const body = parseJsonField('body', '请求体');
            if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('请求体必须是 JSON 对象');
            for (const kind of ['text', 'language', 'voice']) {
                const key = getField(`map-${kind}`).value.trim();
                if (key) {
                    for (const [existingKey, value] of Object.entries(body)) {
                        if (existingKey !== key && value === `{{${kind}}}`) delete body[existingKey];
                    }
                    body[key] = `{{${kind}}}`;
                }
            }
            setField('body', JSON.stringify(body, null, 2));
            draftDirty = true;
            updateStatus('文本字段映射已应用到请求体。');
        } catch (error) {
            showError(error);
        }
    }

    async function saveProfile() {
        try {
            const id = readId();
            const profile = readProfileForm();
            const existing = state.profiles.some(item => item.id === id);
            if (existing && id !== selectedId && !window.confirm(`配置 ${id} 已存在。保存将覆盖它，继续？`)) return;
            await apiPut(`${API}/${encodeURIComponent(id)}`, { profile });
            const updated = await apiGet(API);
            state = updated;
            selectedId = id;
            draftDirty = false;
            renderProfile(id, state.profiles.find(item => item.id === id)?.profile || profile);
            updateStatus('配置已保存。保存不会自动更改默认项。');
            if (typeof toast === 'function') toast('HTTP TTS 配置已保存', 'success');
        } catch (error) {
            showError(error);
        }
    }

    async function setDefault() {
        try {
            if (draftDirty) throw new Error('当前表单有未保存的更改，请先保存，再设置默认配置');
            const id = readId();
            if (!state.profiles.some(item => item.id === id)) throw new Error('请先保存该配置，再设为默认');
            const result = await apiPost(`${API}/${encodeURIComponent(id)}/default`);
            state = await apiGet(API);
            selectedId = id;
            draftDirty = false;
            renderProfile(id, selectedProfile());
            if (result.restart_required) {
                updateStatus('默认配置已保存。需要到「一键启动」重启多模态 TTS 服务，切换到 GenericHTTP 后才会生效。');
            } else {
                updateStatus('默认配置已保存，将在下一次合成请求时生效。');
            }
            if (typeof toast === 'function') toast('默认 HTTP TTS 配置已更新', 'success');
        } catch (error) {
            showError(error);
        }
    }

    async function deleteProfile() {
        const id = getField('profile-select').value;
        if (!id) return;
        const isDefault = id === state.active_profile;
        const suffix = isDefault ? ' 删除会清除该默认选择。' : '';
        if (!window.confirm(`删除配置「${id}」？${suffix}`)) return;
        try {
            const result = await apiDelete(`${API}/${encodeURIComponent(id)}`);
            state = await apiGet(API);
            selectedId = state.profiles.some(item => item.id === selectedId) ? selectedId : (state.profiles[0]?.id || '');
            draftDirty = false;
            renderProfile(selectedId, selectedProfile());
            updateStatus(result.default_cleared ? '已删除默认配置，请选择或创建新的默认配置。' : '配置已删除。');
        } catch (error) {
            showError(error);
        }
    }

    async function testProfile() {
        cleanupPreview();
        const button = getField('test');
        button.disabled = true;
        updateStatus('正在请求试听音频…');
        const generation = ++previewGeneration;
        const controller = new AbortController();
        previewController = controller;
        try {
            const payload = {
                profile: readProfileForm(), text: getField('test-text').value,
                language: getField('test-language').value, voice: getField('test-voice').value,
            };
            const response = await fetch(`${API}/test`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
                signal: controller.signal,
            });
            if (generation !== previewGeneration) return;
            if (!response.ok) {
                const result = await response.json().catch(() => ({}));
                throw new Error(result.detail || `试听失败（HTTP ${response.status}）`);
            }
            const blob = await response.blob();
            if (generation !== previewGeneration) return;
            previewUrl = URL.createObjectURL(blob);
            const audio = getField('preview-audio');
            audio.src = previewUrl;
            await audio.play();
            updateStatus('试听完成。此次请求没有保存配置或更改默认项。');
        } catch (error) {
            if (error.name !== 'AbortError' && generation === previewGeneration) showError(error);
        } finally {
            if (generation === previewGeneration) {
                button.disabled = false;
                previewController = null;
            }
        }
    }

    async function exportProfile() {
        const id = getField('profile-select').value;
        if (!id) return;
        try {
            const data = await apiGet(`${API}/${encodeURIComponent(id)}/export`);
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            try {
                const link = document.createElement('a');
                link.href = url;
                link.download = `${id}-http-tts-template.json`;
                link.click();
            } finally {
                setTimeout(() => URL.revokeObjectURL(url), 0);
            }
            updateStatus('已下载脱敏模板，请检查自定义字段后再分享。');
        } catch (error) {
            showError(error);
        }
    }

    async function importProfileFile(event) {
        const file = event.target.files?.[0];
        event.target.value = '';
        if (!file) return;
        if (file.size > MAX_IMPORT_BYTES) {
            showError(new Error('导入 JSON 不能超过 96 KB'));
            return;
        }
        if (draftDirty && !window.confirm('放弃当前未保存的更改并导入模板？')) return;
        try {
            const imported = JSON.parse(await file.text());
            if (!imported || typeof imported !== 'object' || Array.isArray(imported)) throw new Error('JSON 顶层必须是对象');
            const profile = imported.profile && typeof imported.profile === 'object' ? imported.profile : imported;
            if (Array.isArray(profile)) throw new Error('配置内容必须是对象');
            const id = typeof imported.id === 'string' ? imported.id : '';
            selectedId = id;
            renderProfile('', profile);
            setField('id', id);
            draftDirty = true;
            updateStatus('模板已导入到表单；点击保存后才会写入。导入不会设置默认项。');
        } catch (error) {
            showError(error instanceof SyntaxError ? new Error('导入文件不是有效 JSON') : error);
        }
    }

    function close() {
        cleanupPreview();
        editor = null;
    }

    return { open, close };
})();
