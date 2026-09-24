'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..', '..');
const sourcePath = path.join(root, 'webUI', 'static', 'js', 'database.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const context = {
    encodeURIComponent,
    escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, character => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        })[character]);
    },
    apiPost: async () => { throw new Error('apiPost stub was not configured'); },
};
vm.runInNewContext(`${source}\nthis.__databaseModule = DatabaseModule;`, context, {
    filename: sourcePath,
});

const contract = context.__databaseModule.__test;
assert(contract, 'database test hooks are unavailable');

const locatorField = '__webui_primary_key__';
const readOnlyLocator = { event_key: 'READONLY-LOCATOR-SECRET' };
const readOnlyMarkup = contract.buildTableMarkup({
    table: 'read_only_events',
    editable: false,
    row_locator_field: locatorField,
    columns: [{ name: 'event', type: 'TEXT' }],
    data: [{ event: 'visible value', [locatorField]: readOnlyLocator }],
});
assert(readOnlyMarkup.includes('<th class="db-th db-th-actions">操作</th>'));
assert(readOnlyMarkup.includes('<tr class="db-row-detail-available" data-row-index="0" title="双击查看详情">'));
assert(readOnlyMarkup.includes('class="btn-sm btn-row-detail"'));
assert(readOnlyMarkup.includes('>详情</button>'));
assert(!readOnlyMarkup.includes('btn-edit-row'));
assert(!readOnlyMarkup.includes('btn-delete-row'));
assert(!readOnlyMarkup.includes('READONLY-LOCATOR-SECRET'));
assert(!/\bdata-[\w-]+="\{[^\"]*\}"/.test(readOnlyMarkup), 'primary-key JSON must not enter DOM attributes');

const editableMarkup = contract.buildTableMarkup({
    table: 'person_info',
    editable: true,
    row_locator_field: locatorField,
    columns: [{ name: 'id', type: 'INTEGER' }, { name: 'name', type: 'TEXT' }],
    data: [{ id: 9, name: 'Ada', [locatorField]: { person_key: 'EDITABLE-LOCATOR-SECRET' } }],
});
assert(editableMarkup.includes('>详情</button>'));
assert(editableMarkup.includes('class="btn-sm btn-edit-row" data-id="9"'));
assert(editableMarkup.includes('class="btn-sm btn-delete-row" data-id="9"'));
assert(!editableMarkup.includes('EDITABLE-LOCATOR-SECRET'));

const noKeyMarkup = contract.buildTableMarkup({
    table: 'legacy_records',
    editable: false,
    row_locator_field: locatorField,
    columns: [{ name: 'payload', type: 'TEXT' }],
    data: [{ payload: 'still visible', [locatorField]: null }],
});
assert(noKeyMarkup.includes('btn-row-detail" data-row-index="0" disabled'));
assert(!noKeyMarkup.includes('db-row-detail-available'));
assert(!noKeyMarkup.includes('双击查看详情'));

assert(
    source.includes("container.querySelectorAll('.db-row-detail-available')")
        && source.includes("row.addEventListener('dblclick', createRowDetailDoubleClickHandler(result, rowIndex))"),
    'rows with locators must bind a double-click detail handler',
);

const doubleClickResult = {
    row_locator_field: locatorField,
    data: [
        { [locatorField]: { event_key: 'row-1' } },
        { [locatorField]: null },
    ],
};
let openedDetails = 0;
const doubleClickHandler = contract.createRowDetailDoubleClickHandler(
    doubleClickResult,
    0,
    () => { openedDetails += 1; },
);
doubleClickHandler({
    target: { closest(selector) { return selector.includes('.db-td-actions') ? {} : null; } },
});
doubleClickHandler({
    target: { closest(selector) { return selector.includes('button') ? {} : null; } },
});
assert.strictEqual(openedDetails, 0, 'action cells and interactive controls must not open row details');
doubleClickHandler({
    target: { closest() { return null; } },
});
assert.strictEqual(openedDetails, 1, 'ordinary row double-click must open details');

const noLocatorDoubleClickHandler = contract.createRowDetailDoubleClickHandler(
    doubleClickResult,
    1,
    () => { openedDetails += 1; },
);
noLocatorDoubleClickHandler({ target: { closest() { return null; } } });
assert.strictEqual(openedDetails, 1, 'rows without locators must not open or request details');

const longValue = 'line one\n' + 'unbroken'.repeat(80) + ' & <full value>';
const detailMarkup = contract.renderDetailFieldsHtml([
    { name: '<img src=x onerror=bad()>', type: '<script>alert(1)</script>' },
    { name: 'nullable', type: 'TEXT' },
], {
    '<img src=x onerror=bad()>': longValue,
    nullable: null,
});
assert(detailMarkup.includes('&lt;img src=x onerror=bad()&gt;'));
assert(detailMarkup.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
assert(detailMarkup.includes('line one\n'));
assert(detailMarkup.includes('unbroken'.repeat(80)));
assert(detailMarkup.includes('&amp; &lt;full value&gt;'));
assert(detailMarkup.includes('<span class="db-null">NULL</span>'));
assert(!detailMarkup.includes('<img src=x'));
assert(!detailMarkup.includes('<script>alert(1)'));

async function verifyDetailRequest() {
    let request = null;
    context.apiPost = async (url, body) => {
        request = { url, body };
        return { columns: [], data: {} };
    };
    const locator = { chat_id: 'chat/one', cursor: 'latest' };
    const response = await contract.requestRowDetail('focus_chat_cursor', locator);
    assert.strictEqual(response.data.constructor, Object);
    assert(request, 'detail request was not dispatched');
    assert.strictEqual(request.url, '/api/db/tables/focus_chat_cursor/detail');
    assert.strictEqual(JSON.stringify(request.body), JSON.stringify({ primary_key: locator }));
}

verifyDetailRequest().then(() => {
    process.stdout.write('database details frontend contract: ok\n');
}).catch(error => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exitCode = 1;
});
