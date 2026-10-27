'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element() {
    return {
        className: '', textContent: '', children: [], scrollHeight: 100,
        get lastElementChild() { return this.children.at(-1); },
        get classList() {
            return { contains: name => this.className.split(' ').includes(name) };
        },
        appendChild(child) { this.children.push(child); },
    };
}

const sockets = [];
const context = {
    document: { createElement: element, getElementById: () => null },
    window: {}, location: { protocol: 'http:', host: 'localhost' },
    createAuthenticatedWebSocket() {
        const socket = { send() {}, close() {} };
        sockets.push(socket);
        return socket;
    },
};
const source = fs.readFileSync(path.join(__dirname, '../static/js/setup.js'), 'utf8');
vm.runInNewContext(`${source}\nthis.hooks = SetupModule.__test;`, context);
const { addLogLine, installDepsViaWebSocket, bootstrapGitViaWebSocket } = context.hooks;

async function main() {
    const log = element();
    addLogLine(log, '[Setup] preparing\n');
    addLogLine(log, 'torch 10/100 MiB', true);
    for (let i = 0; i < 100; i++) addLogLine(log, `torch ${i}/100 MiB`, true);
    assert.strictEqual(log.children.length, 2, 'redraws must reuse the current command block');
    assert.strictEqual(log.children[0].textContent, '[Setup] preparing\n');
    assert.strictEqual(log.children[1].textContent, 'torch 99/100 MiB');
    addLogLine(log, '[Runtime] verified\n');
    addLogLine(log, 'next command', true);
    assert.strictEqual(log.children.length, 4, 'completed commands and ordinary logs must remain');
    addLogLine(log, '<img src=x onerror=alert(1)>', true);
    assert.strictEqual(log.children[3].textContent, '<img src=x onerror=alert(1)>');
    assert.strictEqual(log.children[3].innerHTML, undefined, 'installer text must never be parsed as HTML');

    const streamed = element();
    const pending = installDepsViaWebSocket([], element(), streamed);
    const socket = sockets.at(-1);
    const message = data => socket.onmessage({ data: JSON.stringify(data) });
    message({ type: 'task_start', task_id: 'tts', name: 'Multimodal' });
    message({ type: 'log', line: 'torch 10/100 MiB', replace: true });
    message({ type: 'log', line: 'torch 50/100 MiB', replace: true });
    assert.strictEqual(streamed.children.length, 2);
    assert.strictEqual(streamed.lastElementChild.textContent, 'torch 50/100 MiB');
    message({ type: 'all_done' });
    await pending;

    const failed = installDepsViaWebSocket([], element(), element());
    sockets.at(-1).onclose({ wasClean: false });
    await assert.rejects(failed, /连接异常关闭/);

    const gitLog = element();
    const git = bootstrapGitViaWebSocket(gitLog);
    const gitSocket = sockets.at(-1);
    gitSocket.onmessage({ data: JSON.stringify({ type: 'log', line: 'Git 10%', replace: true }) });
    gitSocket.onmessage({ data: JSON.stringify({ type: 'log', line: 'Git 50%', replace: true }) });
    assert.strictEqual(gitLog.children.length, 1);
    assert.strictEqual(gitLog.lastElementChild.textContent, 'Git 50%');
    gitSocket.onmessage({ data: JSON.stringify({ type: 'bootstrap_done', status: 'ok' }) });
    assert.strictEqual((await git).status, 'ok');
    const gitFailure = bootstrapGitViaWebSocket(element());
    sockets.at(-1).onclose();
    await assert.rejects(gitFailure, /提前关闭/);

    const launcherSource = fs.readFileSync(path.join(__dirname, '../static/js/launcher.js'), 'utf8');
    Object.assign(context, {
        escapeHtml: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
        svgIcon: () => '',
    });
    vm.runInNewContext(`${launcherSource}\nthis.launchHooks = LauncherModule.__test;`, context);
    context.launchHooks.setLaunchTestState({ status: 'stopped', core: { status: 'stopped' }, profiles: [] });
    const card = context.launchHooks.launchCardInnerHTML();
    assert.strictEqual((card.match(/data-install-runtime=/g) || []).length, 2,
        'both installed runtime environments must retain their repair buttons');
    assert(card.includes('验证 / 补齐'));
    context.launchHooks.recordRuntimeInstallLog('torch 10/100 MiB', true);
    context.launchHooks.recordRuntimeInstallLog('torch 50/100 MiB', true);
    const progressing = context.launchHooks.launchCardInnerHTML();
    assert(progressing.includes('torch 50/100 MiB'));
    assert(!progressing.includes('torch 10/100 MiB'));
    console.log('setup installation progress: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
