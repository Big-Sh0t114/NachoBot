'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function fixture() {
    const providers = [], models = [];
    const groups = Object.fromEntries(['replyer0', 'planner', 'utils', 'utils-small', 'tool-use']
        .map(name => [`setup-grp-${name}`, { value: '' }]));
    let pendingRow, options;
    const context = {
        console, apiGet() {},
        window: { SetupDefaults: { create(value) { options = value; return { isLoading: () => false }; } } },
        document: {
            createElement: tag => tag === 'option' ? {} : pendingRow,
            getElementById: id => groups[id] || null,
            querySelector: () => null,
            querySelectorAll(selector) {
                if (selector === '.model-row') return models;
                if (selector === '.provider-row') return providers;
                if (selector === '.setup-provider-name') return providers.map(row => row.fields['.setup-provider-name']);
                return [];
            },
        },
    };
    const source = fs.readFileSync(path.join(__dirname, '../static/js/setup.js'), 'utf8')
        .replace('__test: {', '__test: { syncModelGroups, createProviderRow, createModelRow,');
    vm.runInNewContext(`${source}\nthis.module = SetupModule;`, context);
    context.module.init();
    const hooks = context.module.__test;

    function addRow(kind, values) {
        const list = kind === 'provider' ? providers : models;
        const fields = {};
        for (const [key, value] of Object.entries(values)) {
            fields[`.setup-${kind}-${key}`] = { value, appendChild() {} };
        }
        const remove = { addEventListener(_type, fn) { this.click = fn; } };
        fields['.btn-remove-row'] = remove;
        const row = {
            fields, querySelector: selector => fields[selector] || null,
            remove() { const index = list.indexOf(row); if (index >= 0) list.splice(index, 1); },
        };
        pendingRow = row;
        if (kind === 'provider') hooks.createProviderRow();
        else hooks.createModelRow();
        list.push(row);
        return row;
    }
    const sf = addRow('provider', { name: 'SiliconFlow', url: '', key: 'synthetic' });
    const ds = addRow('provider', { name: 'Deepseek', url: '', key: 'synthetic' });
    const free = addRow('model', { id: 'Qwen/free-text', name: 'free-chat', provider: 'SiliconFlow' });
    addRow('model', { id: 'BAAI/bge-m3', name: 'embedding', provider: 'SiliconFlow' });
    const paid = addRow('model', { id: 'deepseek-v4-pro', name: 'custom-paid', provider: 'Deepseek' });
    const flash = addRow('model', { id: 'deepseek-v4-flash', name: 'paid-flash', provider: 'Deepseek' });
    options.onModelsLoaded({
        model_groups: {
            replyer0: 'paid-flash', planner: 'free-chat', utils: 'custom-paid, free-chat',
            file_edit: 'custom-paid, paid-flash', embedding: 'embedding', private_replyer0: '',
        },
        models: [{ model_name: 'custom-paid', api_provider: 'Deepseek' },
            { model_name: 'paid-flash', api_provider: 'Deepseek' }],
        siliconflow_free_text_ids: ['Qwen/free-text'],
    });
    return { hooks, groups, providers, models, sf, ds, free, paid, flash };
}

const f = fixture();
assert.strictEqual(f.groups['setup-grp-replyer0'].value, 'paid-flash');
f.flash.fields['.btn-remove-row'].click();
let data = f.hooks.collectWizardData();
assert.strictEqual(f.groups['setup-grp-replyer0'].value, 'free-chat');
assert.strictEqual(JSON.stringify(data.model_groups.file_edit), JSON.stringify(['custom-paid', 'free-chat']));
assert.strictEqual(JSON.stringify(data.model_groups.embedding), JSON.stringify(['embedding']));
assert.strictEqual(JSON.stringify(data.model_groups.private_replyer0), '[]');

// Deleting the provider removes its remaining models before dropdown refresh
// can reassign them to the only provider left in the form.
f.ds.fields['.btn-remove-row'].click();
data = f.hooks.collectWizardData();
assert(!f.models.some(row => row.fields['.setup-model-name'].value === 'custom-paid'));
assert.strictEqual(JSON.stringify(data.model_groups.file_edit), JSON.stringify(['free-chat']));
assert.strictEqual(JSON.stringify(data.model_groups.utils), JSON.stringify(['free-chat']));
assert.strictEqual(JSON.stringify(data.model_groups.planner), JSON.stringify(['free-chat']));
assert(!data.model_groups.replyer0.includes('embedding'));
assert.strictEqual(data.providers.length, 1);

// Removing another model must not dump all remaining models into every group.
f.free.fields['.btn-remove-row'].click();
assert.strictEqual(f.groups['setup-grp-replyer0'].value, '');
assert.strictEqual(f.groups['setup-grp-planner'].value, 'free-chat');
process.stdout.write('setup DeepSeek model group fallback: passed\n');
