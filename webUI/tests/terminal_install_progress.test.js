'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element() {
    return {
        children: [], style: {}, className: '',
        appendChild(node) { this.children.push(node); },
        replaceChildren() { this.children = []; },
        contains(node) { return this.children.includes(node); },
        querySelector() { return null; },
        get textContent() { return this.children.map(child => child.textContent).join(''); },
        set textContent(value) { this.children = [{ textContent: value }]; },
        set innerHTML(value) { assert.strictEqual(value, ''); this.children = []; },
    };
}
const output = element();
const context = {
    document: {
        getElementById(id) { return id === 'terminal-output' ? output : { value: 'all' }; },
        createElement: element,
        createTextNode: text => ({ textContent: text }),
    },
};
const source = fs.readFileSync(path.join(__dirname, '../static/js/terminal.js'), 'utf8')
    .replace('return { init, refresh };', 'return { init, refresh, appendLogLine, clearLog };');
vm.runInNewContext(`${source}\nthis.module = TerminalModule;`, context);
const { appendLogLine, clearLog } = context.module;
appendLogLine('[tts] start', true);
appendLogLine('[tts] torch 10/100 MiB', true, false, 'tts:command-1');
appendLogLine('[core] ordinary output', true);
appendLogLine('[tts] torch 50/100 MiB', true, false, 'tts:command-1');
assert.strictEqual(output.children.length, 3, 'interleaved services must not duplicate an installer frame');
assert(output.children[1].textContent.includes('50/100'));
assert(!output.children[1].textContent.includes('10/100'));
appendLogLine('<img onerror=alert(1)>', false, false, 'command-2');
assert.strictEqual(output.children.at(-1).children[0].textContent, '<img onerror=alert(1)>');
clearLog();
appendLogLine('next frame', false, false, 'command-2');
assert.strictEqual(output.children.length, 1, 'clearing logs must also reset installer frames');
console.log('terminal installation progress: passed');
