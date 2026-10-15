'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const launcherPath = path.join(root, 'webUI', 'static', 'js', 'launcher.js');
const launcherSource = fs.readFileSync(launcherPath, 'utf8');

const context = {
    console,
    document: {},
    window: {},
    Promise,
    setInterval() {},
    clearInterval() {},
    escapeHtml(value) { return String(value); },
};
vm.runInNewContext(`${launcherSource}\nthis.__launcherModule = LauncherModule;`, context, {
    filename: launcherPath,
});
const contract = context.__launcherModule.__test;
assert(contract, 'launcher test hooks are unavailable');

function externalState(profile, services = [], profileStatus = null) {
    const resolvedStatus = profileStatus || (services.length ? 'running' : 'partial');
    return {
        status: resolvedStatus,
        active_profile: profile,
        core: {
            status: 'running',
            managed: false,
            origin: 'external',
            observed_profile: profile,
        },
        profiles: [{ id: profile, status: resolvedStatus, services }],
    };
}

function renderLaunchCard(state) {
    contract.setLaunchTestState(state);
    return contract.launchCardInnerHTML();
}

for (const profile of ['full', 'lite']) {
    const services = profile === 'full'
        ? [
            { id: 'tts_runtime_full', status: 'running', managed: false, origin: 'external' },
            { id: 'perception', status: 'running', managed: false, origin: 'external' },
        ]
        : [
            { id: 'tts_runtime_lite', status: 'running', managed: false, origin: 'external' },
        ];
    const state = externalState(profile, services, 'running');
    const actions = contract.launchFooterActions(state);
    assert.strictEqual(actions.externalCore, true);
    assert.strictEqual(actions.showStart, false, `${profile} healthy external Core must suppress start`);
    assert.strictEqual(actions.canStop, false, `${profile} without dependents must not offer stop`);
    assert.strictEqual(actions.canSwitch, false, `${profile} external Core must not offer mode switch`);

    const markup = renderLaunchCard(state);
    assert.strictEqual((markup.match(/id="btn-launch-start"/g) || []).length, 0);
    assert(markup.includes('由外部启动器管理，请在原启动窗口停止'));
    assert(!markup.includes('btn-launch-change'));
    assert(!markup.includes('btn-launch-stop'));

    const legacyShape = { ...state, profiles: [{ ...state.profiles[0] }] };
    delete legacyShape.profiles[0].status;
    assert.strictEqual(
        contract.launchFooterActions(legacyShape).showStart,
        false,
        `${profile} ready service rows should suppress start without a profile summary`,
    );
}

const missingDependencies = externalState('full');
const missingActions = contract.launchFooterActions(missingDependencies);
assert.strictEqual(missingActions.showStart, true, 'external Core with missing dependencies keeps WebUI start');
assert.strictEqual(missingActions.canStop, false, 'missing external dependencies have nothing WebUI-owned to stop');
assert(renderLaunchCard(missingDependencies).includes('启动 WebUI 服务'));

for (const status of ['starting', 'running', 'stopping', 'error']) {
    const actions = contract.launchFooterActions(externalState('full', [{
        id: 'tts_runtime_full',
        status,
        managed: true,
        origin: 'webui',
    }]));
    assert.strictEqual(actions.showStart, false, `${status} dependent must suppress duplicate start`);
    assert.strictEqual(actions.canStop, true, `${status} dependent must remain stoppable`);
}

const potatoActions = contract.launchFooterActions(externalState('potato', [], 'running'));
assert.strictEqual(potatoActions.showStart, false, 'external POTATO must not offer start');
assert.strictEqual(potatoActions.canSwitch, false, 'external POTATO must not offer switch');
assert.strictEqual(potatoActions.canStop, false, 'external POTATO must not offer stop');
const potatoMarkup = renderLaunchCard(externalState('potato', [], 'running'));
assert(potatoMarkup.includes('外部启动'));
assert(potatoMarkup.includes('由外部启动器管理，请在原启动窗口停止'));
assert(!potatoMarkup.includes('btn-launch-start'));
assert(!potatoMarkup.includes('btn-launch-change'));
assert(!potatoMarkup.includes('btn-launch-stop'));

const managedRunning = contract.launchFooterActions({
    status: 'running',
    core: { status: 'running', managed: true, origin: 'webui' },
    profiles: [],
});
assert.strictEqual(managedRunning.canSwitch, true, 'managed launch keeps mode switch');
assert.strictEqual(managedRunning.canStop, true, 'managed launch keeps stop');
assert.strictEqual(managedRunning.showStart, false, 'managed running launch hides start');

assert(launcherSource.includes("externalCore ? '启动 WebUI 服务' : '启动 NachoBot'"));
assert(launcherSource.includes("externalCore ? '停止 WebUI 托管服务'"));
assert(launcherSource.includes("'正在停止 WebUI 托管服务...'"));

process.stdout.write('external Core frontend contract: ok\n');
