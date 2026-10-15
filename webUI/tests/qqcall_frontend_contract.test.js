'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/launcher.js'), 'utf8');
const context = { console, document: {}, window: {}, Promise,
    setInterval() {}, clearInterval() {}, escapeHtml: String, svgIcon: () => '' };
vm.runInNewContext(`${source}\nthis.contract = LauncherModule.__test;`, context);
const contract = context.contract;
function group(phase, options = {}) {
    const ready = phase === 'ready';
    return { id: 'qqcall', name: 'QQ语音通话', services: [{
        id: 'qqcall', name: 'QQCall 控制服务',
        status: phase ? (ready ? 'running' : 'error') : 'stopped',
        managed: false, origin: phase ? 'external' : null,
        external_state: phase ? (ready ? 'ready' : 'present_unready') : null,
        qqcall: phase ? { phase, ready } : null, ...options,
    }] };
}
for (const phase of [null, 'needs_attachment', 'ready', 'waiting_kernel_restart', 'waiting_adapter']) {
    const value = group(phase);
    const state = contract.qqcallControlState(value);
    assert.equal(state.canInject, phase === null || phase === 'needs_attachment');
    const markup = contract.groupCardInnerHTML(value);
    assert(markup.includes('id="btn-inject-qqcall"'));
    assert(!markup.includes('id="btn-start-qqcall"'));
    assert(!markup.includes('重启组'));
    assert(!markup.includes('解除注入'));
    assert(markup.includes(phase === 'ready' ? '已注入' : '注入'));
    const button = markup.match(/<button[^>]*id="btn-inject-qqcall"[^>]*>/)[0];
    assert.equal(button.includes('disabled'), !state.canInject);
}
const pending = group('needs_attachment', { injection_pending: true });
assert.equal(contract.qqcallControlState(pending).canInject, false);
assert(contract.groupCardInnerHTML(pending).includes('正在注入'));
assert.equal(contract.qqcallControlState(group(null, { external_state: 'indeterminate' })).canInject, false);
const stale = group(null, { origin: 'external', status: 'error', external_state: 'present_unready' });
assert.equal(contract.qqcallControlState(stale).canInject, false);
assert(contract.groupCardInnerHTML(stale).includes('服务运行中'));
assert(contract.groupCardInnerHTML(stale).includes('等待状态更新'));
assert(source.includes("apiPost('/api/qqcall/inject')"));
assert(source.includes("if (groupId === 'qqcall') return injectQQCall();"));
console.log('qqcall frontend contract: ok');
