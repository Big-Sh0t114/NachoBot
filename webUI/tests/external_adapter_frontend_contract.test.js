'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const launcherPath = path.join(root, 'webUI', 'static', 'js', 'launcher.js');
const launcherSource = fs.readFileSync(launcherPath, 'utf8');
const apiCalls = [];

assert.match(
    launcherSource,
    /pollInterval = setInterval\([\s\S]*?\}, 60_000\);/,
    'idle adapter status polling must run every 60 seconds',
);

const context = {
    console,
    document: {},
    window: {},
    Promise,
    setInterval() {},
    clearInterval() {},
    setTimeout(callback) { callback(); return 1; },
    clearTimeout() {},
    escapeHtml(value) { return String(value); },
    toast() {},
    apiGet() { return Promise.reject(new Error('contract refresh suppressed')); },
    apiPost(url, body) {
        apiCalls.push({ url, body });
        return Promise.resolve({ status: 'starting' });
    },
};
vm.runInNewContext(`${launcherSource}\nthis.__launcherModule = LauncherModule;`, context, {
    filename: launcherPath,
});
const contract = context.__launcherModule.__test;
assert(contract, 'launcher test hooks are unavailable');

function service(id, status, extras = {}) {
    return {
        id,
        name: id,
        status,
        managed: false,
        origin: 'external',
        external_state: 'ready',
        ...extras,
    };
}

function group(id, services, name = id) {
    return { id, name, services };
}

function render(groupValue) {
    return groupValue.id === 'qq_adapter'
        ? contract.snowlumaGroupCardInnerHTML(groupValue)
        : contract.groupCardInnerHTML(groupValue);
}

// Fully external generic and QQ groups are informational only: no WebUI
// start/stop action may be offered, and QQ backend switching is fenced.
const externalGeneric = group('bilibili', [service('bilibili', 'running')]);
let control = contract.groupControlState(externalGeneric);
assert.strictEqual(control.allExternalReady, true);
assert.strictEqual(control.showStart, false);
assert(!render(externalGeneric).includes('id="btn-start-bilibili"'));
assert(!render(externalGeneric).includes('id="btn-stop-bilibili"'));
assert(render(externalGeneric).includes('由外部启动器管理，请在原启动窗口停止'));

contract.setQqAdapterTestState({ selected: 'napcat', napcat: { installed: true }, snowluma: { installed: true } });
const externalQq = group('qq_adapter', [
    service('napcat_adapter', 'running'),
    service('napcat_shell', 'running'),
], 'QQ / NapCat');
control = contract.groupControlState(externalQq);
assert.strictEqual(control.allExternalReady, true);
const externalQqMarkup = render(externalQq);
assert(!externalQqMarkup.includes('id="btn-start-qq_adapter"'));
assert(!externalQqMarkup.includes('id="btn-stop-qq_adapter"'));
assert(externalQqMarkup.includes('id="launcher-qq-adapter"'));
assert(externalQqMarkup.includes('id="launcher-qq-adapter" class="form-select" disabled'));

// External + missing keeps a targeted start action and never performs a
// preliminary group stop.
const mixedMissing = group('discord', [
    service('koishi', 'running'),
    service('koishi_adapter', 'stopped', { managed: false, origin: null, external_state: null }),
]);
control = contract.groupControlState(mixedMissing);
assert.strictEqual(control.anyExternal, true);
assert.strictEqual(control.hasMissing, true);
assert.strictEqual(control.showStart, true);
assert.strictEqual(control.startLabel, '启动缺失服务');
assert(render(mixedMissing).includes('启动缺失服务'));
assert(render(mixedMissing).includes('id="btn-start-discord"'));

// External + WebUI-owned complete groups expose only the owned-service stop;
// Start is reserved for missing rows or an entirely WebUI-owned restart.
const mixedComplete = group('discord', [
    service('koishi', 'running'),
    service('koishi_adapter', 'running', { managed: true, origin: 'webui', external_state: null }),
]);
control = contract.groupControlState(mixedComplete);
assert.strictEqual(control.anyExternal, true);
assert.strictEqual(control.owned, true);
assert.strictEqual(control.showStart, false);
const mixedCompleteMarkup = render(mixedComplete);
assert(!mixedCompleteMarkup.includes('id="btn-start-discord"'));
assert(mixedCompleteMarkup.includes('停止 WebUI 托管服务'));

// A blocked external row must not hide the stop action for a different
// manager-owned row in the same group.
const ownedWithBlockedExternal = group('discord', [
    service('koishi', 'running', { managed: true, origin: 'webui', external_state: null }),
    service('koishi_adapter', 'error', {
        managed: false,
        origin: 'external',
        external_state: 'present_unready',
    }),
]);
control = contract.groupControlState(ownedWithBlockedExternal);
assert.strictEqual(control.blocked, true);
assert.strictEqual(control.owned, true);
const ownedWithBlockedMarkup = render(ownedWithBlockedExternal);
assert(!ownedWithBlockedMarkup.includes('id="btn-start-discord"'));
assert(ownedWithBlockedMarkup.includes('id="btn-stop-discord"'));
assert(ownedWithBlockedMarkup.includes('停止 WebUI 托管服务'));

const ownedQqWithBlockedExternal = group('qq_adapter', [
    service('napcat_adapter', 'running', { managed: true, origin: 'webui', external_state: null }),
    service('napcat_shell', 'error', {
        managed: false,
        origin: null,
        external_state: 'indeterminate',
    }),
], 'QQ / NapCat');
control = contract.groupControlState(ownedQqWithBlockedExternal);
assert.strictEqual(control.blocked, true);
assert.strictEqual(control.owned, true);
const ownedQqWithBlockedMarkup = render(ownedQqWithBlockedExternal);
assert(!ownedQqWithBlockedMarkup.includes('id="btn-start-qq_adapter"'));
assert(ownedQqWithBlockedMarkup.includes('id="btn-stop-qq_adapter"'));
assert(ownedQqWithBlockedMarkup.includes('停止 WebUI 托管服务'));

// Indeterminate external ownership is blocking and fences QQ selection.
const indeterminateQq = group('qq_adapter', [
    service('napcat_adapter', 'error', {
        managed: false,
        origin: null,
        external_state: 'indeterminate',
    }),
    service('napcat_shell', 'stopped', { managed: false, origin: null, external_state: null }),
], 'QQ / NapCat');
control = contract.groupControlState(indeterminateQq);
assert.strictEqual(control.blocked, true);
assert.strictEqual(control.showStart, false);
const indeterminateMarkup = render(indeterminateQq);
assert(indeterminateMarkup.includes('id="launcher-qq-adapter" class="form-select" disabled'));
assert(indeterminateMarkup.includes('无法确认外部进程状态'));

const presentUnready = group('qq_adapter', [
    service('napcat_adapter', 'error', {
        managed: false,
        origin: 'external',
        external_state: 'present_unready',
    }),
    service('napcat_shell', 'stopped', { managed: false, origin: null, external_state: null }),
], 'QQ / NapCat');
control = contract.groupControlState(presentUnready);
assert.strictEqual(control.blocked, true);
assert.strictEqual(control.showStart, false);
assert(render(presentUnready).includes('检测到外部进程但尚未就绪'));

const selector = { disabled: false, value: 'napcat' };
let selectorCalls = 0;
context.apiPost = () => {
    selectorCalls += 1;
    return Promise.resolve({});
};
contract.selectQqAdapter('snowluma', indeterminateQq, {
    querySelector() { return selector; },
});
assert.strictEqual(selectorCalls, 0, 'blocked QQ selection must be refused');

async function assertNoPreliminaryStop(groupValue, expectedStartUrl) {
    apiCalls.length = 0;
    context.apiGet = url => {
        if (url === '/api/groups?fresh=1') return Promise.resolve([groupValue]);
        return Promise.reject(new Error('contract refresh suppressed'));
    };
    context.apiPost = (url, body) => {
        apiCalls.push({ url, body });
        return Promise.resolve({ status: 'starting' });
    };
    contract.setGroupsTestState([groupValue]);
    await contract.startGroup(groupValue.id, false);
    assert.deepStrictEqual(apiCalls.map(call => call.url), [expectedStartUrl]);
}

(async () => {
    await assertNoPreliminaryStop(mixedMissing, '/api/groups/discord/start');
    const mixedQqMissing = group('qq_adapter', [
        service('napcat_adapter', 'running'),
        service('napcat_shell', 'stopped', { managed: false, origin: null, external_state: null }),
    ], 'QQ / NapCat');
    await assertNoPreliminaryStop(mixedQqMissing, '/api/groups/qq_adapter/start');

    // The displayed state may be stale while a BAT launcher starts. The click
    // waits for a new group snapshot and must not submit another start.
    const staleStopped = group('bilibili', [
        service('bilibili', 'stopped', { managed: false, origin: null, external_state: null }),
    ]);
    let releaseFreshStatus;
    const freshStatus = new Promise(resolve => { releaseFreshStatus = resolve; });
    context.apiGet = url => url === '/api/groups?fresh=1'
        ? freshStatus
        : Promise.reject(new Error('contract refresh suppressed'));
    contract.setGroupsTestState([staleStopped]);
    apiCalls.length = 0;
    const firstClick = contract.startGroup('bilibili', false);
    const secondClick = contract.startGroup('bilibili', false);
    assert(contract.getAdapterRefreshState().pendingStarts.includes('bilibili'));
    assert.strictEqual(contract.getAdapterRefreshState().loading, true);
    assert(render(staleStopped).includes('正在核对状态'));
    assert(render(staleStopped).includes('launcher-status-spinner'));

    const previousDocument = context.document;
    const indicator = {
        classList: { toggle() {} },
        setAttribute() {},
        hidden: false,
        innerHTML: '',
    };
    const grid = { parentNode: { insertBefore() {} } };
    context.document = {
        getElementById(id) {
            if (id === 'launcher-grid') return grid;
            if (id === 'launcher-adapter-status') return indicator.id ? indicator : null;
            return null;
        },
        createElement() { return indicator; },
    };
    contract.updateAdapterStatusIndicator();
    assert(indicator.innerHTML.includes('launcher-status-spinner'));
    assert(indicator.innerHTML.includes('正在更新适配器状态'));
    context.document = previousDocument;

    releaseFreshStatus([externalGeneric]);
    await Promise.all([firstClick, secondClick]);
    assert.deepStrictEqual(apiCalls, [], 'fresh external state must prevent duplicate start');
    assert.strictEqual(contract.getAdapterRefreshState().pendingStarts.length, 0);

    // Status failure is visible and blocks the stale start button until a
    // successful fresh snapshot restores reliable state.
    context.apiGet = () => Promise.reject(new Error('status unavailable'));
    await contract.refreshAdapterGroups().catch(() => {});
    assert.strictEqual(contract.getAdapterRefreshState().error, true);
    assert(render(staleStopped).includes('disabled'));
    context.apiGet = url => url === '/api/groups'
        ? Promise.resolve([staleStopped])
        : Promise.reject(new Error('contract refresh suppressed'));
    await contract.refreshAdapterGroups();
    assert.strictEqual(contract.getAdapterRefreshState().error, false);

    // A click arriving during an older poll must wait for a separate new
    // snapshot; an older "stopped" result is not start authorization.
    let releaseOldPoll;
    const oldPoll = new Promise(resolve => { releaseOldPoll = resolve; });
    let groupReads = 0;
    context.apiGet = url => {
        if (url !== '/api/groups' && url !== '/api/groups?fresh=1') {
            return Promise.reject(new Error('contract refresh suppressed'));
        }
        groupReads += 1;
        return groupReads === 1 ? oldPoll : Promise.resolve([externalGeneric]);
    };
    contract.setGroupsTestState([staleStopped]);
    apiCalls.length = 0;
    const polling = contract.refreshAdapterGroups();
    const clickDuringPoll = contract.startGroup('bilibili', false);
    releaseOldPoll([staleStopped]);
    await Promise.all([polling, clickDuringPoll]);
    assert.strictEqual(groupReads, 2);
    assert.deepStrictEqual(apiCalls, []);

    process.stdout.write('external adapter frontend contract: ok\n');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
