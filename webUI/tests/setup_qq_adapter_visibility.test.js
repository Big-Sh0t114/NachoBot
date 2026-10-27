'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element(value = '') {
    return {
        value, dataset: {}, style: {}, disabled: false, listeners: new Map(),
        addEventListener(type, listener) { this.listeners.set(type, listener); },
        dispatchEvent(event) { return this.listeners.get(event.type)?.(event); },
        appendChild() {}, remove() {},
    };
}

function fixture(defaultAdapter = 'napcat') {
    const select = element('napcat');
    const refs = {
        'setup-qq-adapter': select,
        'setup-snowluma-section': element(),
        'setup-snowluma-runtime-hint': element(),
        'setup-snowluma-access-token': element(),
        'setup-snowluma-webui-password': element(),
        'setup-step-3': element(),
    };
    refs['setup-step-3'].querySelectorAll = () => [select];
    const checkbox = { value: 'qq', checked: true };
    let resolvePost, rejectPost;
    const posts = [];
    const context = {
        console, window: {},
        Event: class { constructor(type) { this.type = type; } },
        document: {
            createElement: element,
            getElementById: id => refs[id] || null,
            querySelectorAll: selector => selector === '.setup-component-cb:checked' && checkbox.checked ? [checkbox] : [],
        },
        apiPost(url, body) {
            posts.push({ url, body });
            return new Promise((resolve, reject) => { resolvePost = resolve; rejectPost = reject; });
        },
        toast() {},
    };
    const read = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
    vm.runInNewContext(read('setup-defaults.js'), context);
    // Access the existing module functions without changing their browser API.
    const setup = read('setup.js').replace('__test: {', '__test: { onQqAdapterChange, updateQqAdapterVisibility, updatePathCheckVisibility,');
    vm.runInNewContext(`${setup}\nthis.hooks = SetupModule.__test;`, context);
    select.addEventListener('change', context.hooks.onQqAdapterChange);
    context.hooks.onComponentToggle();
    const loader = context.window.SetupDefaults.create({
        apiGet: async () => ({ env: { qq_adapter: defaultAdapter } }),
        syncProviderDropdowns() {},
        onQqAdapterLoaded() {
            context.hooks.updateQqAdapterVisibility();
            context.hooks.updatePathCheckVisibility();
        },
    });
    return {
        select, refs, checkbox, posts, loader, hooks: context.hooks,
        resolve: () => resolvePost?.({ status: 'ok' }),
        reject: () => rejectPost?.(new Error('rejected')),
    };
}

async function main() {
    const f = fixture();
    await f.loader.load();
    f.select.value = 'snowluma';
    const changing = f.select.dispatchEvent({ type: 'change' });
    assert.strictEqual(f.refs['setup-snowluma-section'].style.display, '',
        'NapCat defaults followed by a SnowLuma selection must reveal credentials immediately');
    assert.strictEqual(f.refs['setup-snowluma-runtime-hint'].style.display, '');
    assert.strictEqual(f.posts.length, 1, 'loading saved defaults must not trigger a backend mutation');
    assert.strictEqual(f.posts[0].body.qq_adapter, 'snowluma');
    f.resolve();
    await changing;
    assert.strictEqual(f.select.dataset.liveValue, 'snowluma');

    const initialSnow = fixture('snowluma');
    await initialSnow.loader.load();
    assert.strictEqual(initialSnow.refs['setup-snowluma-section'].style.display, '',
        'saved SnowLuma defaults must render without a fake change event');
    assert.strictEqual(initialSnow.posts.length, 0);

    const failed = fixture();
    await failed.loader.load();
    failed.select.value = 'snowluma';
    const rejected = failed.select.dispatchEvent({ type: 'change' });
    failed.reject();
    await rejected;
    assert.strictEqual(failed.select.value, 'napcat');
    assert.strictEqual(failed.refs['setup-snowluma-section'].style.display, 'none');

    f.refs['setup-snowluma-access-token'].value = 'request-only-token';
    f.refs['setup-snowluma-webui-password'].value = 'request-only-password';
    f.hooks.updateQqAdapterVisibility();
    assert.strictEqual(f.refs['setup-snowluma-access-token'].value, 'request-only-token',
        'refreshing a visible section must preserve unsent input');
    f.select.value = 'napcat';
    const back = f.select.dispatchEvent({ type: 'change' });
    assert.strictEqual(f.refs['setup-snowluma-section'].style.display, 'none');
    assert.strictEqual(f.refs['setup-snowluma-access-token'].value, '');
    assert.strictEqual(f.refs['setup-snowluma-webui-password'].value, '');
    f.resolve();
    await back;
    f.checkbox.checked = false;
    f.hooks.onComponentToggle();
    assert.strictEqual(f.refs['setup-snowluma-section'].style.display, 'none');
    console.log('setup QQ adapter visibility: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
