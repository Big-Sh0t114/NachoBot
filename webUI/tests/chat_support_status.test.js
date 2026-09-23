'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const supportPath = path.resolve(__dirname, '..', 'static', 'js', 'chat-support.js');
const supportSource = fs.readFileSync(supportPath, 'utf8');
const timers = new Map();
let nextTimerId = 1;
const window = {
    setTimeout(callback, delay) {
        const id = nextTimerId++;
        timers.set(id, { callback, delay });
        return id;
    },
    clearTimeout(id) {
        timers.delete(id);
    },
};
const context = { window, console, Uint8Array, Date, Promise, Map, Set, Array, String, Boolean };
vm.runInNewContext(supportSource, context, { filename: supportPath });

async function verifySingleFlightWithoutTimedRetry() {
    const responses = [];
    let calls = 0;
    const controller = context.window.ChatSupport.createTTS({
        getActiveSession: () => null,
        getMessagesElement: () => ({ querySelectorAll: () => [] }),
        escapeText: value => String(value),
        toast() {},
        apiGet() {
            calls += 1;
            return new Promise((resolve, reject) => responses.push({ resolve, reject }));
        },
    });

    const first = controller.updateStatus();
    const overlappingPoll = controller.updateStatus();
    assert.strictEqual(calls, 1, 'overlapping TTS status requests should share one fetch');
    responses.shift().resolve({ ready: true });
    await Promise.all([first, overlappingPoll]);

    const refresh = controller.updateStatus();
    const forcedRefresh = controller.updateStatus(true);
    assert.strictEqual(calls, 2, 'forced refresh during a request should queue one follow-up');
    responses.shift().resolve({ ready: false, transient: true });
    await Promise.all([refresh, forcedRefresh]);
    assert(
        ![...timers.values()].some(timer => timer.delay > 0),
        'transient TTS status must not schedule a timed retry',
    );

    const immediate = [...timers.entries()].find(([, timer]) => timer.delay === 0);
    assert(immediate, 'a forced follow-up should be scheduled after the active request');
    immediate[1].callback();
    assert.strictEqual(calls, 3);
    responses.shift().reject(Object.assign(new Error('temporary TTS status failure'), { status: 503 }));
    await new Promise(resolve => setImmediate(resolve));
    assert(
        ![...timers.values()].some(timer => timer.delay > 0),
        'transport failures must not schedule a timed retry',
    );
}

async function verifyTransientStatusCanAttemptAndRecover() {
    const button = {
        dataset: { messageId: 'message-1' },
        classList: { toggle() {} },
        disabled: false,
        title: '',
        ariaLabel: '',
        setAttribute(name, value) {
            if (name === 'aria-label') this.ariaLabel = value;
        },
    };
    const messagesElement = {
        querySelectorAll(selector) {
            assert.strictEqual(selector, '.chat-tts-button');
            return [button];
        },
    };
    let statusResponse = { ready: false, transient: true, error: 'timed out' };
    let deferNextStatus = false;
    let deferredStatusResolver = null;
    const controller = context.window.ChatSupport.createTTS({
        getActiveSession: () => ({ messages: [{ id: 'message-1', content: 'hello' }] }),
        getMessagesElement: () => messagesElement,
        escapeText: value => String(value),
        toast() {},
        apiGet: () => {
            if (deferNextStatus) {
                deferNextStatus = false;
                return new Promise(resolve => {
                    deferredStatusResolver = resolve;
                });
            }
            return Promise.resolve(statusResponse);
        },
    });

    const markup = controller.createSpeakerMarkup({ id: 'message-1' });
    assert(!/\sdisabled(?:\s|>)/.test(markup), 'initial TTS markup must remain clickable');
    assert(markup.includes('尝试生成语音'), 'initial TTS label must describe an attempt');

    controller.syncButtons();
    assert.strictEqual(button.disabled, false, 'unknown readiness must remain clickable');
    await controller.updateStatus();
    assert.strictEqual(button.disabled, false, 'transient not-ready status must remain clickable');
    assert.strictEqual(button.ariaLabel, '尝试生成语音');
    assert(button.title.includes('尝试生成语音'));

    const requests = [];
    const audioInstances = [];
    context.fetch = async (url, options) => {
        requests.push({ url, options });
        return { ok: true, blob: async () => ({ size: 4 }) };
    };
    context.URL = {
        createObjectURL: () => 'blob:test-audio',
        revokeObjectURL() {},
    };
    context.Audio = class MockAudio {
        constructor(url) {
            this.url = url;
            this.paused = true;
            this.listeners = new Map();
            audioInstances.push(this);
        }
        addEventListener(event, callback) {
            this.listeners.set(event, callback);
        }
        async play() {
            this.paused = false;
        }
        pause() {
            this.paused = true;
        }
        removeAttribute() {}
        load() {}
    };

    deferNextStatus = true;
    const staleStatus = controller.updateStatus();
    const synthesis = controller.handleSpeechButton(button);
    await synthesis;
    deferredStatusResolver({ ready: false, transient: true, error: 'timed out' });
    await staleStatus;
    assert.strictEqual(requests.length, 1, 'a transient false status must still attempt synthesis');
    assert.strictEqual(requests[0].url, '/api/chat/tts');
    assert.strictEqual(requests[0].options.method, 'POST');
    assert.deepStrictEqual(JSON.parse(requests[0].options.body), { text: 'hello' });
    assert.strictEqual(button.ariaLabel, '停止播放', 'successful synthesis should update the playback UI');

    audioInstances[0].listeners.get('ended')();
    assert.strictEqual(button.disabled, false);
    assert.strictEqual(button.ariaLabel, '生成并播放语音', 'success should restore the ready UI');

    statusResponse = { ready: false, text_only: true };
    await controller.updateStatus();
    assert.strictEqual(button.disabled, true, 'explicit text-only status should disable TTS');
    assert(button.ariaLabel.includes('POTATO'));
    await controller.handleSpeechButton(button);
    assert.strictEqual(requests.length, 1, 'text-only status must not attempt synthesis');
    assert(
        ![...timers.values()].some(timer => timer.delay > 0),
        'TTS status and synthesis must not schedule timed retries',
    );
}

verifySingleFlightWithoutTimedRetry()
    .then(verifyTransientStatusCanAttemptAndRecover)
    .then(() => process.stdout.write('chat TTS status contract: ok\n'))
    .catch(error => {
        process.stderr.write(`${error.stack || error}\n`);
        process.exitCode = 1;
    });
