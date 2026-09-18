'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const launcherPath = path.join(root, 'webUI', 'static', 'js', 'launcher.js');
const setupPath = path.join(root, 'webUI', 'static', 'js', 'setup.js');
const htmlPath = path.join(root, 'webUI', 'static', 'index.html');
const launcherSource = fs.readFileSync(launcherPath, 'utf8');
const setupSource = fs.readFileSync(setupPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');

assert(html.includes('id="setup-snowluma-access-token"'));
assert(html.includes('id="setup-snowluma-webui-password"'));
assert(html.includes('id="path-check-snowluma"'));
assert(html.includes('https://github.com/SnowLuma/SnowLuma/releases/latest'));
assert(html.includes('class="btn btn-primary btn-sm setup-refresh-button" id="btn-recheck"'));
assert(html.includes('↻ 刷新 / 验证'));
assert(!html.includes('node.exe'));
assert(setupSource.includes("/api/setup/snowluma/configure"));
assert(setupSource.includes("pathCheckResults.snowluma !== true"));
assert(setupSource.includes('clearSnowLumaSecrets();'));
assert(setupSource.indexOf("/api/setup/configs/generate") < setupSource.indexOf("/api/setup/snowluma/configure"));
assert(setupSource.indexOf("/api/setup/snowluma/configure") < setupSource.indexOf("/api/setup/deps/tasks"));

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

const normalized = contract.normalizeSnowLumaProcess({
    pid: 123,
    name: 'QQ.exe',
    uin: '',
    status: 'ready',
    injected: true,
});
assert.strictEqual(JSON.stringify(normalized), JSON.stringify({
    pid: 123,
    name: 'QQ.exe',
    uin: '',
    status: 'ready',
    injected: true,
    connected: false,
    loggedIn: false,
}));
assert(contract.qqGroupIsBusy({ services: [{ status: 'starting' }] }));
assert(!contract.qqGroupIsBusy({ services: [{ status: 'stopped' }] }));
assert(launcherSource.includes('snowlumaOptimisticStart'));
assert(launcherSource.includes('正在启动 SnowLuma Runtime + Adapter'));
assert(launcherSource.includes('await new Promise(resolve => setTimeout(resolve, 3000))'));
assert(launcherSource.includes('clearSnowLumaOptimisticStart(true)'));
assert(launcherSource.includes('clearTimeout'));
assert(
    contract.snowlumaOptimisticStartTimeoutMs >= 60_000
        && contract.snowlumaOptimisticStartTimeoutMs <= 90_000,
    'optimistic start timeout must be a single bounded 60-90 second timer',
);
const stoppedSnowLumaGroup = {
    id: 'qq_adapter',
    services: [
        { id: 'snowluma_runtime', status: 'stopped', detail: '' },
        { id: 'snowluma_adapter', status: 'stopped', detail: '' },
        { id: 'unrelated', status: 'running', detail: 'keep' },
    ],
};
const optimisticSnowLumaGroup = contract.applySnowLumaOptimisticStart(stoppedSnowLumaGroup);
assert.deepStrictEqual(
    optimisticSnowLumaGroup.services.slice(0, 2).map(service => service.status),
    ['starting', 'starting'],
    'SnowLuma runtime and adapter must enter starting immediately',
);
assert(optimisticSnowLumaGroup.services[0].detail.includes('正在启动 SnowLuma Runtime + Adapter'));
assert.strictEqual(optimisticSnowLumaGroup.services[2].status, 'running');
assert.strictEqual(stoppedSnowLumaGroup.services[0].status, 'stopped', 'optimistic rendering must not mutate backend state');
const stalePoll = contract.reconcileSnowLumaOptimisticGroup(stoppedSnowLumaGroup, true);
assert(stalePoll.active, 'a stale stopped poll must preserve optimistic starting');
assert(stalePoll.group.services.slice(0, 2).every(service => service.status === 'starting'));
const runningPoll = contract.reconcileSnowLumaOptimisticGroup({
    ...stoppedSnowLumaGroup,
    services: stoppedSnowLumaGroup.services.map(service => (
        ['snowluma_runtime', 'snowluma_adapter'].includes(service.id)
            ? { ...service, status: 'running' }
            : service
    )),
}, true);
assert(!runningPoll.active, 'explicit running must clear optimistic starting');
assert(runningPoll.group.services.slice(0, 2).every(service => service.status === 'running'));
const preRestartRunningPoll = contract.reconcileSnowLumaOptimisticGroup(runningPoll.group, true, false);
assert(preRestartRunningPoll.active, 'pre-restart running poll must not clear the optimistic restart');
assert(preRestartRunningPoll.group.services.slice(0, 2).every(service => service.status === 'starting'));
const errorPoll = contract.reconcileSnowLumaOptimisticGroup({
    ...stoppedSnowLumaGroup,
    services: stoppedSnowLumaGroup.services.map(service => (
        service.id === 'snowluma_adapter' ? { ...service, status: 'error' } : service
    )),
}, true);
assert(!errorPoll.active, 'explicit error must clear optimistic starting');
assert.strictEqual(errorPoll.group.services[1].status, 'error');
assert(launcherSource.includes('withSnowLumaPassword'));
assert(launcherSource.includes('btn btn-primary btn-sm snowluma-refresh-button'));
assert(launcherSource.includes('↻ 刷新实例'));
assert(launcherSource.includes('注入'));
assert(launcherSource.includes('解除注入'));
assert(launcherSource.includes('snowlumaApiPost'));
assert(launcherSource.includes('AGREEMENT_REQUIRED'));
assert(launcherSource.includes('/api/setup/snowluma/agreements/accept'));
assert(launcherSource.includes('已阅读并同意'));
assert(launcherSource.includes('textContent'));
assert(!launcherSource.includes('仅用于本次请求'));
const actionStart = launcherSource.indexOf('async function processSnowLumaAction');
const actionEnd = launcherSource.indexOf('async function withSnowLumaPassword', actionStart);
assert(actionStart >= 0 && actionEnd > actionStart, 'SnowLuma action handler must remain discoverable');
const actionSource = launcherSource.slice(actionStart, actionEnd);
assert(!actionSource.includes('current.injected ='), 'actions must not mutate injected state optimistically');
assert(actionSource.includes('shouldScheduleSnowLumaUnloadReconciliation'), 'only unload verification failures may schedule delayed reconciliation');
assert(actionSource.includes('requireFresh: true'), 'actions must require a fresh post-action list');
assert(
    contract.snowlumaUnloadReconciliationDelayMs >= 1_500
        && contract.snowlumaUnloadReconciliationDelayMs <= 3_000,
    'unload reconciliation delay must be bounded around the watcher tick',
);
const schedulerStart = launcherSource.indexOf('function scheduleSnowLumaUnloadReconciliation');
const schedulerEnd = launcherSource.indexOf('async function processSnowLumaAction', schedulerStart);
assert(schedulerStart >= 0 && schedulerEnd > schedulerStart, 'unload scheduler must remain discoverable');
const schedulerSource = launcherSource.slice(schedulerStart, schedulerEnd);
assert.strictEqual(
    (schedulerSource.match(/schedule\(\(\) => \{/g) || []).length,
    1,
    'unload verification failure must schedule exactly one delayed refresh',
);
assert(schedulerSource.includes('void refresh(card, true).catch(() => {});'));
const successRefreshIndex = actionSource.indexOf('refreshSnowLumaProcesses(card, {');
const successToastIndex = actionSource.indexOf('toast(`PID ${pid}');
assert(successRefreshIndex >= 0 && successRefreshIndex < successToastIndex, 'success toast must follow immediate refresh');

(async () => {
    let refreshCalls = 0;
    let releaseInitialRefresh;
    const initialRefresh = new Promise(resolve => {
        releaseInitialRefresh = resolve;
    });
    const refreshFlight = contract.createSnowLumaRefreshFlight();
    const inFlightRefresh = refreshFlight(() => {
        refreshCalls += 1;
        return initialRefresh;
    });
    const requiredRefresh = refreshFlight(() => {
        refreshCalls += 1;
        return { ok: true };
    }, { requireFresh: true });
    await Promise.resolve();
    assert.strictEqual(refreshCalls, 1, 'an action refresh must wait for an existing refresh instead of running in parallel');
    releaseInitialRefresh({ ok: true });
    await requiredRefresh;
    await inFlightRefresh;
    assert.strictEqual(refreshCalls, 2, 'an action refresh must perform one new fetch after the existing refresh settles');

    let actionSuccess = false;
    await assert.rejects(
        contract.runSnowLumaActionAndRefresh(
            async () => ({ ok: true }),
            async () => ({ ok: false }),
            () => { actionSuccess = true; },
        ),
        error => error?.code === 'PROCESS_REFRESH_FAILED',
        'required refresh failure must prevent the normal action success callback',
    );
    assert(!actionSuccess, 'required refresh failure must not show success');

    const scheduled = [];
    let delayedRefreshCalls = 0;
    const fakeSchedule = (callback, delay) => scheduled.push({ callback, delay });
    const fakeRefresh = async () => {
        delayedRefreshCalls += 1;
    };
    assert(contract.shouldScheduleSnowLumaUnloadReconciliation({ code: 'UNLOAD_VERIFICATION_FAILED' }));
    assert(!contract.shouldScheduleSnowLumaUnloadReconciliation({ code: 'PASSWORD_REQUIRED' }));
    assert(!contract.shouldScheduleSnowLumaUnloadReconciliation({ code: 'AGREEMENT_REQUIRED' }));
    contract.scheduleSnowLumaUnloadReconciliation({}, fakeSchedule, fakeRefresh);
    assert.strictEqual(scheduled.length, 1, 'unload failure must schedule one delayed reconciliation');
    assert.strictEqual(scheduled[0].delay, contract.snowlumaUnloadReconciliationDelayMs);
    await scheduled[0].callback();
    assert.strictEqual(delayedRefreshCalls, 1, 'delayed reconciliation must invoke the refresh once');

    const passwordInput = { value: 'secret-success' };
    const fakeCard = {
        querySelector(selector) {
            assert.strictEqual(selector, '#launcher-snowluma-password');
            return passwordInput;
        },
    };
    let capturedPassword = '';
    await contract.withSnowLumaPassword(fakeCard, async password => {
        capturedPassword = password;
        return { ok: true };
    });
    assert.strictEqual(capturedPassword, 'secret-success');
    assert.strictEqual(passwordInput.value, '', 'success must clear the active password input');

    passwordInput.value = 'secret-failure';
    await assert.rejects(
        contract.withSnowLumaPassword(fakeCard, async password => {
            capturedPassword = password;
            throw new Error('request failed');
        }),
        /request failed/,
    );
    assert.strictEqual(capturedPassword, 'secret-failure');
    assert.strictEqual(passwordInput.value, '', 'failure must clear the active password input');

    process.stdout.write('snowluma frontend contract: ok\n');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
