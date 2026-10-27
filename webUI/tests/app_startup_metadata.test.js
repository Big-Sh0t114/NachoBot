'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

async function main() {
    let abortTimer;
    const initialized = [];
    const version = { textContent: '', removeAttribute() {} };
    const context = {
        console, AbortController,
        document: {
            addEventListener() {}, querySelectorAll: () => [],
            getElementById: id => id === 'sidebar-version' ? version : null,
        },
        setTimeout(callback, timeout) { assert.strictEqual(timeout, 5000); abortTimer = callback; return 1; },
        clearTimeout() {},
        fetch(url, options) {
            assert.strictEqual(url, '/api/webui/info');
            return new Promise((resolve, reject) => {
                options.signal.addEventListener('abort', () => reject(new Error('metadata request stalled')));
            });
        },
    };
    for (const name of ['Setup', 'Chat', 'Config', 'Launcher', 'Terminal', 'Plugins', 'Database', 'Knowledge', 'Memory']) {
        context[`${name}Module`] = { init() { initialized.push(name); } };
    }
    const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
    vm.runInNewContext(`${source}\nthis.app = App;`, context);
    const starting = context.app.init();
    assert.strictEqual(initialized.length, 0);
    abortTimer();
    await starting;
    assert.strictEqual(initialized[0], 'Setup', 'a stalled version API must not prevent setup initialization');
    assert.strictEqual(version.textContent, 'WebUI');
    console.log('app startup metadata timeout: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
