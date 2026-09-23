'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appPath = path.resolve(__dirname, '..', 'static', 'js', 'app.js');
const appSource = fs.readFileSync(appPath, 'utf8');
const timers = new Map();
const fetches = [];
const pendingStatusResponses = [];
const moduleCalls = [];
let nextTimerId = 1;

function makeElement() {
    return {
        classList: { add() {}, remove() {} },
        addEventListener() {},
        removeAttribute() {},
        style: {},
        dataset: {},
        textContent: '',
    };
}

const navItems = ['chat', 'config', 'launcher'].map(tab => {
    const item = makeElement();
    item.dataset.tab = tab;
    return item;
});
const panels = ['chat', 'config', 'launcher'].map(tab => makeElement());
const elements = new Map([
    ...['chat', 'config', 'launcher'].map(tab => [`tab-${tab}`, panels[['chat', 'config', 'launcher'].indexOf(tab)]]),
    ['status-dot', makeElement()],
    ['status-text', makeElement()],
]);

const document = {
    addEventListener() {},
    querySelectorAll(selector) {
        if (selector === '.nav-item') return navItems;
        if (selector === '.tab-panel') return panels;
        return [];
    },
    querySelector(selector) {
        const match = selector.match(/^\.nav-item\[data-tab="([^"]+)"\]$/);
        return match ? navItems.find(item => item.dataset.tab === match[1]) : null;
    },
    getElementById(id) {
        return elements.get(id) || null;
    },
};

const moduleNames = ['Chat', 'Config', 'Launcher', 'Terminal', 'Plugins', 'Database', 'Knowledge', 'Memory', 'Setup'];
const context = {
    console,
    document,
    fetch(url) {
        fetches.push(url);
        if (url === '/api/status') {
            return new Promise(resolve => pendingStatusResponses.push(resolve));
        }
        return Promise.resolve({ json: async () => ({ services: { running: 1, total: 2 } }) });
    },
    setInterval(callback, delay) {
        const id = nextTimerId++;
        timers.set(id, { callback, delay });
        return id;
    },
    clearInterval(id) {
        timers.delete(id);
    },
};
for (const name of moduleNames) {
    context[`${name}Module`] = {
        init() { moduleCalls.push(`${name}.init`); },
        refresh() { moduleCalls.push(`${name}.refresh`); },
    };
}

vm.runInNewContext(`${appSource}\nthis.__app = App;`, context, { filename: appPath });

async function verifyLauncherOnlyPolling() {
    await context.__app.init();
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 0, 'initial Chat load must not fetch global status');
    assert.strictEqual(timers.size, 0, 'initial Chat load must not create a status interval');
    assert(moduleCalls.includes('Chat.init'), 'initial page should initialize Chat');
    assert.strictEqual(moduleCalls.filter(call => call === 'Chat.refresh').length, 0, 'initial Chat initialization must not refresh Chat twice');

    context.__app.switchTab('chat');
    assert.strictEqual(moduleCalls.filter(call => call === 'Chat.refresh').length, 0, 'same-tab Chat switch must not refresh Chat');

    context.__app.switchTab('config');
    assert.strictEqual(moduleCalls.filter(call => call === 'Config.refresh').length, 1, 'config activation should refresh config');
    context.__app.switchTab('chat');
    assert.strictEqual(moduleCalls.filter(call => call === 'Chat.refresh').length, 1, 'config to Chat transition should refresh Chat once');
    context.__app.switchTab('chat');
    assert.strictEqual(moduleCalls.filter(call => call === 'Chat.refresh').length, 1, 'subsequent same-tab Chat switch must not refresh Chat again');

    context.__app.switchTab('config');
    assert.strictEqual(moduleCalls.filter(call => call === 'Config.refresh').length, 2, 'other tab refresh behavior should remain unchanged');
    await context.__app.pollStatus();
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 0, 'non-Launcher tabs must not fetch global status');
    assert.strictEqual(timers.size, 0, 'non-Launcher tabs must not retain a status interval');

    context.__app.switchTab('launcher');
    assert(moduleCalls.includes('Launcher.refresh'), 'entering Launcher should refresh its module');
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 1, 'entering Launcher must immediately fetch global status');
    assert.strictEqual(timers.size, 1, 'Launcher should own one status interval');

    // Re-enter Launcher before visit A resolves. Visit B refreshes immediately,
    // while the epoch fence prevents A from updating B's status.
    context.__app.switchTab('chat');
    context.__app.switchTab('launcher');
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 2, 're-entering Launcher must refresh immediately');
    assert.strictEqual(pendingStatusResponses.length, 2, 'each Launcher visit may have its own in-flight request');
    const [timerId, timer] = [...timers.entries()][0];
    assert.strictEqual(timer.delay, 3000, 'Launcher global status interval should remain 3 seconds');
    pendingStatusResponses.shift()({ json: async () => ({ adapters: { running: 0, total: 1 } }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(elements.get('status-text').textContent, '', 'visit A response must not update the status bar during visit B');
    pendingStatusResponses.shift()({ json: async () => ({ adapters: { running: 1, total: 2 } }) });
    await new Promise(resolve => setImmediate(resolve));
    const statusText = elements.get('status-text').textContent;
    assert.strictEqual(statusText, '1/2 运行中', 'active Launcher response should update the status bar');

    timer.callback();
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 3, 'Launcher interval should refresh global status');
    timer.callback();
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 3, 'slow Launcher requests must not stack across interval ticks');
    assert.strictEqual(pendingStatusResponses.length, 1, 'only one global status request may be in flight');

    context.__app.switchTab('chat');
    assert.strictEqual(timers.size, 0, 'leaving Launcher must stop global status polling');
    pendingStatusResponses.shift()({ json: async () => ({ adapters: { running: 0, total: 1 } }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.strictEqual(elements.get('status-text').textContent, statusText, 'stale Launcher response must not update the status bar on Chat');
    await context.__app.pollStatus();
    assert.strictEqual(fetches.filter(url => url === '/api/status').length, 3, 'Chat must reject manual and stale interval polling');
    assert(!timers.has(timerId), 'Launcher interval should be cleared on tab exit');
}

verifyLauncherOnlyPolling()
    .then(() => process.stdout.write('Launcher-only global status polling: ok\n'))
    .catch(error => {
        process.stderr.write(`${error.stack || error}\n`);
        process.exitCode = 1;
    });
