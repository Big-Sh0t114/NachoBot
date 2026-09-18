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
assert(launcherSource.includes('withSnowLumaPassword'));
assert(launcherSource.includes('注入'));
assert(launcherSource.includes('解除注入'));

(async () => {
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
