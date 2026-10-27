'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element() {
    return {
        style: {}, children: [], listeners: new Map(),
        addEventListener(type, fn) { this.listeners.set(type, fn); },
        appendChild(node) { this.children.push(node); },
        set innerHTML(value) { this.html = value; this.children = []; },
        get innerHTML() { return this.html || ''; },
    };
}

function fixture() {
    const list = element();
    list.innerHTML = '点击下方按钮开始检测...';
    let resolve, reject, calls = 0;
    const context = {
        window: { SetupDefaults: { create: () => ({}) } },
        document: {
            getElementById: id => id === 'setup-check-list' ? list : null,
            querySelector: () => null, querySelectorAll: () => [], createElement: element,
        },
        escapeHtml: String,
        apiGet(url) {
            assert.strictEqual(url, '/api/setup/check');
            calls++;
            return new Promise((yes, no) => { resolve = yes; reject = no; });
        },
    };
    const source = fs.readFileSync(path.join(__dirname, '../static/js/setup.js'), 'utf8');
    vm.runInNewContext(`${source}\nthis.module = SetupModule;`, context);
    return {
        module: context.module, list, calls: () => calls,
        complete: () => resolve({ python: { status: 'ok', message: 'Python' }, docker: { status: 'warning', message: 'Docker' } }),
        fail: () => reject(new Error('temporarily unavailable')),
    };
}

async function main() {
    const f = fixture();
    f.module.init();
    assert.strictEqual(f.calls(), 1, 'setup initialization must automatically start its first environment check');
    assert(f.list.innerHTML.includes('正在检测环境'));
    const pending = f.module.refresh();
    f.module.refresh();
    assert.strictEqual(f.calls(), 1, 'opening setup during initialization must reuse the in-flight check');
    f.complete();
    await pending;
    assert.strictEqual(f.list.children.length, 2);
    await f.module.refresh();
    assert.strictEqual(f.calls(), 1, 'a completed check must be reused');

    const failure = fixture();
    failure.module.init();
    const first = failure.module.refresh();
    failure.fail();
    await first;
    assert(failure.list.innerHTML.includes('检测失败'));
    const retry = failure.module.refresh();
    assert.strictEqual(failure.calls(), 2, 'an unsuccessful first check must allow a retry');
    failure.complete();
    await retry;
    console.log('setup initial environment check: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
