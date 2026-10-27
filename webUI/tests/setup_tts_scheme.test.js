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

function fixture(defaultEngine = 'GPT_Sovits', deferred = false) {
    const engine = element('GPT_Sovits');
    const ttsCheckbox = { ...element('tts'), checked: false };
    const refs = { 'setup-tts-engine': engine, 'setup-step-3': element() };
    refs['setup-step-3'].querySelectorAll = () => [engine];
    let releaseDefaults;
    const defaults = deferred
        ? new Promise(resolve => { releaseDefaults = resolve; })
        : Promise.resolve({ tts: { engine: defaultEngine } });
    let loader;
    const context = {
        console, window: {}, toast() {}, apiPost() {},
        apiGet: () => defaults,
        document: {
            createElement: element,
            getElementById: id => refs[id] || null,
            querySelector: selector => selector === '.setup-component-cb[value="tts"]' ? ttsCheckbox : null,
            querySelectorAll(selector) {
                if (selector === '.setup-component-cb') return [ttsCheckbox];
                if (selector === '.setup-component-cb:checked' && ttsCheckbox.checked) return [ttsCheckbox];
                return [];
            },
        },
    };
    const read = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
    vm.runInNewContext(read('setup-defaults.js'), context);
    const originalCreate = context.window.SetupDefaults.create;
    context.window.SetupDefaults.create = options => {
        loader = originalCreate(options);
        return loader;
    };
    const source = read('setup.js').replace('__test: {', '__test: { applyScheme,');
    vm.runInNewContext(`${source}\nthis.module = SetupModule;`, context);
    context.module.init();
    return {
        engine, ttsCheckbox, loader, hooks: context.module.__test,
        release: () => releaseDefaults({ tts: { engine: defaultEngine } }),
    };
}

async function main() {
    for (const scheme of ['vox_lite', 'vox_full']) {
        const f = fixture();
        f.hooks.applyScheme(scheme);
        assert.strictEqual(f.engine.value, 'Vox');
        assert(f.ttsCheckbox.checked);
        await f.loader.load();
        assert.strictEqual(f.engine.value, 'Vox', `${scheme} must survive first-page defaults hydration`);
        assert.strictEqual(f.hooks.collectWizardData().tts.engine, 'Vox');
    }
    for (const scheme of ['gpt_lite', 'gpt_full']) {
        const f = fixture('Vox');
        f.hooks.applyScheme(scheme);
        await f.loader.load();
        assert.strictEqual(f.engine.value, 'GPT_Sovits', `${scheme} must override saved Vox defaults`);
    }
    const untouched = fixture('Vox');
    await untouched.loader.load();
    assert.strictEqual(untouched.engine.value, 'Vox', 'saved configuration must load when no engine was selected');

    const none = fixture('Vox');
    none.hooks.applyScheme('none');
    await none.loader.load();
    assert.strictEqual(none.engine.value, 'Vox', 'disabling local models must not pick an engine');
    assert(!none.ttsCheckbox.checked);

    const late = fixture('GPT_Sovits', true);
    const loading = late.loader.load();
    late.hooks.applyScheme('vox_full');
    late.release();
    await loading;
    assert.strictEqual(late.engine.value, 'Vox', 'a late defaults response must preserve the latest scheme');

    const manual = fixture('GPT_Sovits');
    manual.engine.value = 'Vox';
    manual.engine.dispatchEvent({ type: 'change' });
    await manual.loader.load();
    assert.strictEqual(manual.engine.value, 'Vox', 'manual engine selection must take precedence');
    manual.hooks.applyScheme('gpt_full');
    manual.hooks.applyScheme('vox_lite');
    assert.strictEqual(manual.engine.value, 'Vox', 'latest explicit choice must win');
    console.log('setup TTS scheme selection: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
