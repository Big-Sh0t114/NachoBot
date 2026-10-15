'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const chatPath = path.join(root, 'webUI', 'static', 'js', 'chat.js');
const chatSource = fs.readFileSync(chatPath, 'utf8');
const context = {
    console,
    window: {
        ChatSupport: {
            createId() {},
            createSession() {},
            createTTS() {},
            escapeText(value) { return String(value); },
            formatContent(value) { return String(value); },
            formatTime() { return ''; },
            makeTitle() { return ''; },
        },
        EasterEggSystem: { setRandomWelcomeSubtitle() {} },
        setTimeout,
        clearTimeout,
    },
    document: {},
};
vm.runInNewContext(`${chatSource}\nthis.__chatModule = ChatModule;`, context, {
    filename: chatPath,
});

const contract = context.__chatModule.__test;
assert(contract, 'chat test hooks are unavailable');
const healthyExternal = {
    status: 'running',
    active_profile: 'full',
    core: { status: 'running', managed: false, origin: 'external', observed_profile: 'full' },
    profiles: [{
        id: 'full',
        status: 'running',
        services: [
            { id: 'tts_runtime_full', status: 'running', managed: false, origin: 'external' },
            { id: 'perception', status: 'running', managed: false, origin: 'external' },
        ],
    }],
};

assert.strictEqual(contract.launchCoreIsExternal(healthyExternal), true);
assert.strictEqual(contract.launchExternalProfileIsReady(healthyExternal), true);
assert.strictEqual(contract.launchHasOwnedDependents(healthyExternal), false);
assert.strictEqual(contract.externalLauncherNotice, '由外部启动器管理，请在原启动窗口停止');
const legacyShape = { ...healthyExternal, profiles: [{ ...healthyExternal.profiles[0] }] };
delete legacyShape.profiles[0].status;
assert.strictEqual(contract.launchExternalProfileIsReady(legacyShape), true);

const mixed = {
    ...healthyExternal,
    profiles: [{
        ...healthyExternal.profiles[0],
        services: [{ id: 'perception', status: 'running', managed: true, origin: 'webui' }],
    }],
};
assert.strictEqual(contract.launchHasOwnedDependents(mixed), true);

const guard = chatSource.indexOf('launchExternalProfileIsReady(currentLaunch)');
const stopPost = chatSource.indexOf("apiPost('/api/launch/stop')");
assert(guard >= 0 && stopPost > guard, 'external ownership guard must precede stop POST');
assert(chatSource.includes("toast(EXTERNAL_LAUNCHER_NOTICE, 'info')"));
assert(chatSource.includes("'点击启动 WebUI 托管服务'"));
assert(!/setInterval\s*\(\s*updateBackendStatus/.test(chatSource), 'Chat Core status must not be polled on an interval');
assert(!/setInterval\s*\(\s*\(\)\s*=>\s*ttsController\.updateStatus/.test(chatSource), 'Chat TTS status must not be polled on an interval');

async function verifySlowStatusPollIsNotStarved() {
    let calls = 0;
    let resolveRequest;
    const statusElement = {
        disabled: false,
        className: '',
        textContent: '',
        title: '',
        setAttribute() {},
        removeAttribute() {},
    };
    context.apiGet = () => {
        calls += 1;
        return new Promise(resolve => { resolveRequest = resolve; });
    };
    contract.setStatusElement(statusElement);

    const first = contract.updateBackendStatus();
    const overlappingInterval = contract.updateBackendStatus();
    assert.strictEqual(calls, 1, 'slow status polls should be single-flight');
    resolveRequest({
        status: 'running',
        active_profile: 'lite',
        core: { status: 'running', managed: true, origin: 'webui' },
        profiles: [],
    });
    await Promise.all([first, overlappingInterval]);
    assert.strictEqual(statusElement.textContent, 'NachoBot · LITE');
    assert(chatSource.includes('coreStatusRequestSerial += 1'), 'toggle must keep its stale-response guard');
}

verifySlowStatusPollIsNotStarved()
    .then(() => process.stdout.write('external Core chat contract: ok\n'))
    .catch(error => {
        process.stderr.write(`${error.stack || error}\n`);
        process.exitCode = 1;
    });
