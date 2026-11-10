/** Shared setup/config UI for local TTS and VLM device assignments. */
window.ModelDevicesPanel = (() => {
    let activeContainer = null;
    let activeRequest = 0;
    let draft = { tts: 'auto', vlm: 'auto' };
    let inventory = [];

    function addOption(select, value, label, disabled = false) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        option.disabled = disabled;
        select.appendChild(option);
    }

    function visible(container, requestId) {
        return activeContainer === container
            && activeRequest === requestId
            && container.isConnected;
    }

    function renderChoices(container) {
        for (const model of ['tts', 'vlm']) {
            const select = container.querySelector(`[data-model-device="${model}"]`);
            if (!select) continue;
            select.replaceChildren();
            addOption(select, 'auto', '自动（沿用现有模型配置）');
            addOption(select, 'cpu', 'CPU');
            const selected = draft[model];
            if (model === 'vlm' && /^cuda(?::\d+)?$/.test(selected)) {
                addOption(select, selected, `配置设备 · ${selected}`);
            } else if (selected !== 'auto' && selected !== 'cpu'
                && !inventory.some(gpu => gpu.cuda && gpu.uuid === selected)) {
                addOption(select, selected, `当前不可用的显卡 · ${selected}`);
            }
            for (const gpu of inventory) {
                if (!gpu.cuda || !gpu.selectable || !gpu.uuid) continue;
                const total = gpu.vram_mb !== null && gpu.vram_mb !== undefined
                    && Number.isFinite(Number(gpu.vram_mb))
                    ? ` · ${(Number(gpu.vram_mb) / 1024).toFixed(2)} GB`
                    : '';
                addOption(select, gpu.uuid, `GPU ${gpu.index} · ${gpu.name}${total}`);
            }
            select.value = selected;
        }

        const list = container.querySelector('[data-gpu-inventory]');
        if (!list) return;
        list.replaceChildren();
        if (!inventory.length) {
            const empty = document.createElement('p');
            empty.className = 'model-device-muted';
            empty.textContent = '未检测到 NVIDIA CUDA 显卡。仍可选择自动或 CPU。';
            list.appendChild(empty);
            return;
        }
        for (const gpu of inventory) {
            const row = document.createElement('div');
            row.className = 'model-device-gpu';
            const total = gpu.vram_mb !== null && gpu.vram_mb !== undefined
                && Number.isFinite(Number(gpu.vram_mb))
                ? `${(Number(gpu.vram_mb) / 1024).toFixed(2)} GB`
                : '显存未知';
            const label = document.createElement('span');
            const deviceLabel = gpu.cuda
                ? `GPU ${gpu.index} · ${gpu.name}`
                : `显卡 · ${gpu.name}`;
            label.textContent = `${deviceLabel} · ${total}`;
            const state = document.createElement('span');
            state.textContent = gpu.cuda ? 'NVIDIA CUDA，可分配' : '未验证 CUDA，不可分配';
            row.append(label, state);
            list.appendChild(row);
        }
    }

    function setStatus(container, message, isError = false) {
        const status = container.querySelector('[data-model-device-status]');
        if (!status) return;
        status.textContent = message;
        status.classList.toggle('is-error', isError);
        status.classList.toggle('is-success', !isError && Boolean(message));
    }

    async function save(container, requestId) {
        if (!visible(container, requestId)) return;
        const button = container.querySelector('[data-model-device-save]');
        const refreshButton = container.querySelector('[data-model-device-refresh]');
        button.disabled = true;
        if (refreshButton) refreshButton.disabled = true;
        for (const select of container.querySelectorAll('[data-model-device]')) select.disabled = true;
        setStatus(container, '正在保存…');
        try {
            const result = await apiPut('/api/model-devices', { ...draft });
            if (!visible(container, requestId)) return;
            draft = { ...result.assignments };
            inventory = Array.isArray(result.gpus) ? result.gpus : inventory;
            renderChoices(container);
            setStatus(container, '已保存；对应托管服务下次启动或重启时生效。');
        } catch (error) {
            if (visible(container, requestId)) setStatus(container, error.message || '保存失败', true);
        } finally {
            if (visible(container, requestId)) {
                button.disabled = false;
                if (refreshButton) refreshButton.disabled = false;
                for (const select of container.querySelectorAll('[data-model-device]')) select.disabled = false;
            }
        }
    }

    async function getModelDevices() {
        const response = await fetch('/api/model-devices');
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            const detail = payload.detail;
            throw new Error(typeof detail === 'string' ? detail : `设备配置读取失败（HTTP ${response.status}）`);
        }
        return response.json();
    }

    async function loadInventory(container, preserveDraft) {
        if (!container) return;
        if (activeContainer !== container) activeContainer = container;
        const requestId = ++activeRequest;
        const hadLoaded = container.dataset.modelDevicesLoaded === 'true';
        const oldDraft = { ...draft };
        const saveButton = container.querySelector('[data-model-device-save]');
        const refreshButton = container.querySelector('[data-model-device-refresh]');
        if (saveButton) saveButton.disabled = true;
        if (refreshButton) refreshButton.disabled = true;
        for (const select of container.querySelectorAll('[data-model-device]')) select.disabled = true;
        setStatus(container, '正在检测 WebUI 服务所在电脑的显卡…');
        try {
            const result = await getModelDevices();
            if (!visible(container, requestId)) return;
            draft = preserveDraft && hadLoaded ? oldDraft : { ...result.assignments };
            inventory = Array.isArray(result.gpus) ? result.gpus : [];
            renderChoices(container);
            for (const select of container.querySelectorAll('[data-model-device]')) select.disabled = false;
            container.dataset.modelDevicesLoaded = 'true';
            if (saveButton) saveButton.disabled = false;
            setStatus(container, result.restart_note || '保存后将在下次服务启动时生效。');
        } catch (error) {
            if (visible(container, requestId)) {
                setStatus(container, error.message || '无法读取设备配置', true);
                if (hadLoaded) {
                    if (saveButton) saveButton.disabled = false;
                    for (const select of container.querySelectorAll('[data-model-device]')) select.disabled = false;
                }
            }
        } finally {
            if (visible(container, requestId) && refreshButton) refreshButton.disabled = false;
        }
    }

    async function mount(container) {
        if (!container) return;
        if (activeContainer === container && container.dataset.modelDevicesMounted === 'true') {
            return loadInventory(container, true);
        }
        activeContainer = container;
        container.dataset.modelDevicesMounted = 'true';
        container.innerHTML = `
            <div class="model-device-panel">
                <div class="model-device-panel-header">
                    <div>
                        <h3>模型运行设备</h3>
                        <p data-model-device-scope></p>
                    </div>
                    <div class="model-device-panel-actions">
                        <button type="button" class="btn btn-ghost" data-model-device-refresh>重新检测显卡</button>
                        <button type="button" class="btn btn-primary" data-model-device-save disabled>保存设备选择</button>
                    </div>
                </div>
                <div class="model-device-assignments">
                    <label>TTS 本地语音模型
                        <select class="form-select" data-model-device="tts" disabled></select>
                    </label>
                    <label>Florence-2 VLM
                        <select class="form-select" data-model-device="vlm" disabled></select>
                    </label>
                </div>
                <div class="model-device-inventory" data-gpu-inventory></div>
                <p class="model-device-muted">设备列表对应 WebUI 服务所在电脑。ASR 当前使用 CPU；设备选择仅适用于本地模型。</p>
                <p class="model-device-status" data-model-device-status role="status" aria-live="polite">正在读取已保存的设备选择…</p>
            </div>`;
        const scope = container.querySelector('[data-model-device-scope]');
        scope.textContent = '选择自动、CPU，或一张 NVIDIA CUDA 显卡。每个模型分别分配；显存不会合并。';
        container.querySelector('[data-model-device-save]')?.addEventListener(
            'click', () => save(container, activeRequest),
        );
        container.querySelector('[data-model-device-refresh]')?.addEventListener(
            'click', () => loadInventory(container, true),
        );
        for (const model of ['tts', 'vlm']) {
            container.querySelector(`[data-model-device="${model}"]`)?.addEventListener('change', event => {
                draft[model] = event.currentTarget.value;
            });
        }
        await loadInventory(container, false);
    }

    function unmount(container = activeContainer) {
        if (container && activeContainer === container) {
            activeRequest += 1;
            activeContainer = null;
            delete container.dataset.modelDevicesMounted;
            delete container.dataset.modelDevicesLoaded;
        }
    }

    return { mount, unmount };
})();
