const assert = require('node:assert/strict');
const test = require('node:test');
require('../static/js/chat-call.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
function element() {
    const attributes = new Map();
    return {hidden: true, dataset: {}, attributes, classList: {add() {}, remove() {}}, handlers: {},
        addEventListener(name, callback) { this.handlers[name] = callback; },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        focus() { this.focused = true; }, replaceChildren() {}, appendChild() {}, append() {}};
}
function harness(fetchOverride, {activeRecord = true, status = {tts_ready: true, asr_ready: false}, motion = false, reduced = false} = {}) {
    const elements = new Map();
    const animations = [];
    const docHandlers = {};
    function getElement(id) {
        if (!elements.has(id)) {
            const item = element();
            elements.set(id, item);
            if (motion) {
                const properties = new Map();
                item.style = {properties, setProperty: (name, value) => properties.set(name, value), removeProperty: name => properties.delete(name)};
                item.getBoundingClientRect = () => ({left: 200, top: 80, width: 880, height: 600});
                item.parentElement = {getBoundingClientRect: () => ({left: 200, top: 0, width: 880, height: 680})};
                item.animate = (frames, options) => {
                    let resolve, reject;
                    const finished = new Promise((done, fail) => { resolve = done; reject = fail; });
                    const animation = {element: item, id, frames, options, finished, finish: resolve,
                        cancel() { this.cancelled = true; reject(new Error('cancelled')); }};
                    animations.push(animation);
                    return animation;
                };
                if (id.endsWith('-dialog')) {
                    const surface = getElement(`${id}:surface`);
                    surface.hidden = false;
                    item.querySelector = () => surface;
                }
            }
        }
        return elements.get(id);
    }
    const requests = [];
    const played = [];
    const players = [];
    const activity = [];
    const audio = {isActive: () => true, isMuted: () => false, start: async () => true,
        unlock: async () => true, stop: async () => {}, createPlayer() {
            const done = deferred();
            const player = {play: async (_, hooks) => { played.push(player); hooks?.onStarted?.(); return done.promise; },
                stop: () => done.resolve('interrupted'), finish: () => done.resolve('played')};
            players.push(player); return player;
        }};
    const controller = global.ChatCall.create({
        documentRef: {getElementById: getElement, querySelector: () => getElement('composer'),
            querySelectorAll: () => [], addEventListener(name, handler) { docHandlers[name] = handler; }, createElement: element},
        windowRef: {addEventListener() {}, setInterval: () => 1, clearInterval() {},
            matchMedia: () => ({matches: reduced}), getComputedStyle: () => ({opacity: '0.5', transform: 'matrix(1, 0, 0, 1, 0, 4)'})},
        getConversation: () => ({id: 'a'}), audioFactory: () => audio,
        onActivityChange: active => activity.push(active),
        fetchRef: async (url, init) => {
            const body = init.body ? JSON.parse(init.body) : {};
            requests.push({url, body});
            if (fetchOverride) { const result = await fetchOverride(url, init); if (result) return result; }
            const data = url.endsWith('/status') ? status
                : url.endsWith('/interrupt') ? {generation: body.generation + 1}
                : url === '/api/chat/calls' ? {id: 'call-a', conversation_id: 'a', generation: 0, status: 'active', messages: []} : {};
            return {ok: true, status: 200, json: async () => data, arrayBuffer: async () => new ArrayBuffer(1)};
        },
    });
    controller.init();
    if (activeRecord) controller.__test.setRecord({id: 'call-a', conversation_id: 'a', generation: 0, status: 'active', messages: []});
    controller.__test.setAudio(audio);
    const reply = (id, generation = 0, call = 'call-a') => controller.handleVoiceEvent({
        channel: 'voice', call_id: call, conversation_id: 'a', generation, voice_message_id: id,
        control_id: `control-${id}`, message: {role: 'assistant', content: id},
    });
    return {controller, elements, requests, played, players, reply, audio, activity, animations, docHandlers};
}

test('dialogs animate both surfaces and finish hiding only after exit, while closing disables input immediately', async () => {
    const h = harness(undefined, {activeRecord: false, motion: true});
    await h.elements.get('chat-call-open').handlers.click();
    const dialog = h.elements.get('chat-call-start-dialog');
    const surface = h.elements.get('chat-call-start-dialog:surface');
    assert.equal(dialog.hidden, false);
    assert(h.animations.some(item => item.id.endsWith(':surface') && item.frames[0].transform.includes('scale')));
    h.docHandlers.keydown({key: 'Escape'});
    assert.equal(dialog.hidden, false, 'exit stays visible until it finishes');
    assert.equal(dialog.inert, true);
    assert.equal(dialog.getAttribute('aria-hidden'), 'true');
    assert.equal(dialog.dataset.callMotionVisible, 'false');
    for (const animation of h.animations.filter(item => !item.cancelled)) animation.finish();
    await tick();
    assert.equal(dialog.hidden, true);
    assert.equal(surface.hidden, true);
});

test('quick dialog reopen cancels exit and stale completions cannot hide it', async () => {
    const h = harness(undefined, {activeRecord: false, motion: true});
    const open = h.elements.get('chat-call-open').handlers.click;
    await open();
    h.docHandlers.keydown({key: 'Escape'});
    const oldExit = h.animations.filter(item => item.id === 'chat-call-start-dialog').at(-1);
    await open();
    assert.equal(oldExit.cancelled, true);
    oldExit.finish();
    await tick();
    const dialog = h.elements.get('chat-call-start-dialog');
    assert.equal(dialog.hidden, false);
    assert.equal(dialog.inert, false);
    assert.equal(h.animations.filter(item => item.id === 'chat-call-start-dialog').at(-1).frames[0].opacity, '0.5');
});

test('minimize and restore pin the exiting call without delaying state or hiding the newer view', async () => {
    const h = harness(undefined, {motion: true});
    const panel = h.elements.get('chat-call-panel');
    h.elements.get('chat-call-minimize').handlers.click();
    const exit = h.animations.filter(item => item.id === 'chat-call-panel').at(-1);
    assert.equal(panel.dataset.callMotionPinned, 'true');
    assert.equal(panel.hidden, false);
    assert.equal(panel.inert, true);
    assert.equal(h.elements.get('chat-workspace').dataset.callView, 'minimized');
    assert.equal(h.controller.isActive(), true);
    h.elements.get('chat-call-restore').handlers.click();
    assert.equal(exit.cancelled, true);
    exit.finish();
    await tick();
    assert.equal(panel.hidden, false);
    assert.equal(panel.inert, false);
    assert.equal(panel.dataset.callMotionPinned, undefined);
    assert.equal(h.elements.get('chat-workspace').dataset.callView, 'active');
    await h.controller.endCall();
    assert.equal(h.controller.isActive(), false, 'hangup does not wait for animations');
    assert.equal(h.elements.get('chat-workspace').dataset.callView, 'text');
    for (const animation of h.animations.filter(item => !item.cancelled)) animation.finish();
    await tick();
    assert.equal(panel.hidden, true);
    assert.equal(panel.dataset.callMotionPinned, undefined);
});

test('reduced motion skips transitions and applies final visibility immediately', async () => {
    const h = harness(undefined, {motion: true, reduced: true});
    h.elements.get('chat-call-transcript-toggle').handlers.click();
    h.elements.get('chat-call-transcript-toggle').handlers.click();
    assert.equal(h.elements.get('chat-call-conversation').hidden, true);
    h.elements.get('chat-call-minimize').handlers.click();
    assert.equal(h.elements.get('chat-call-panel').hidden, true);
    const pending = h.controller.guardConversationSwitch('b');
    h.elements.get('chat-call-switch-continue').handlers.click();
    assert.equal(await pending, false);
    assert.equal(h.elements.get('chat-call-switch-dialog').hidden, true);
    assert.equal(h.animations.length, 0);
    await h.controller.endCall();
});

test('hangup during minimize preserves the outgoing slot and releases it once', async () => {
    const h = harness(undefined, {motion: true});
    const panel = h.elements.get('chat-call-panel');
    h.elements.get('chat-call-minimize').handlers.click();
    const exit = h.animations.filter(item => item.id === 'chat-call-panel').at(-1);
    const bounds = Array.from(panel.style.properties.entries());
    await h.controller.endCall();
    assert.equal(h.controller.isActive(), false);
    assert.equal(exit.cancelled, undefined);
    assert.equal(panel.dataset.callMotionPinned, 'true');
    assert.deepEqual(Array.from(panel.style.properties.entries()), bounds);
    exit.finish();
    await tick();
    assert.equal(panel.hidden, true);
    assert.equal(panel.dataset.callMotionPinned, undefined);
    assert.equal(panel.style.properties.size, 0);
});

test('call activity starts before microphone capture and lasts through minimize until hangup', async () => {
    const h = harness(undefined, {activeRecord: false, status: {tts_ready: true, asr_ready: true}});
    h.audio.start = async () => {
        assert.deepEqual(h.activity, [true]);
        return true;
    };
    assert.equal(await h.controller.beginCall(), true);
    await tick();
    h.elements.get('chat-call-minimize').handlers.click();
    h.elements.get('chat-call-restore').handlers.click();
    assert.deepEqual(h.activity, [true]);
    await h.controller.endCall();
    await h.controller.endCall();
    assert.deepEqual(h.activity, [true, false]);
});

test('failed or cancelled call startup releases player suppression', async () => {
    const failed = harness(async url => {
        if (url === '/api/chat/calls') throw new Error('offline');
    }, {activeRecord: false});
    assert.equal(await failed.controller.beginCall(), false);
    assert.deepEqual(failed.activity, [true, false]);

    const created = deferred();
    const cancelled = harness(async url => url === '/api/chat/calls' ? created.promise : undefined,
        {activeRecord: false});
    const pending = cancelled.controller.beginCall();
    await tick();
    assert.deepEqual(cancelled.activity, [true]);
    await cancelled.controller.endCall();
    assert.deepEqual(cancelled.activity, [true, false]);
    created.resolve({ok: true, json: async () => ({id: 'late', conversation_id: 'a'})});
    assert.equal(await pending, false);
    assert.deepEqual(cancelled.activity, [true, false]);
});

test('unavailable TTS does not suppress the music player', async () => {
    const h = harness(undefined, {activeRecord: false, status: {tts_ready: false}});
    assert.equal(await h.controller.beginCall(), false);
    assert.deepEqual(h.activity, []);
});

test('multipart replies play FIFO and controls start with their own playback', async () => {
    const h = harness();
    h.reply('one'); h.reply('two'); h.reply('one');
    await tick();
    assert.equal(h.played.length, 1);
    assert.equal(h.requests.filter(r => r.url.endsWith('/control')).length, 1);
    h.players[0].finish(); await tick();
    assert.equal(h.played.length, 2);
    h.players[1].finish(); await tick();
    assert.equal(h.requests.filter(r => r.url.endsWith('/tts')).length, 2);
    await h.controller.endCall();
});

test('barge-in fences a delayed TTS response and queued old replies', async () => {
    const delayed = deferred();
    const h = harness(async (url, init) => {
        if (url.endsWith('/tts') && JSON.parse(init.body).message_id === 'old') return delayed.promise;
    });
    h.reply('old'); h.reply('old-queued'); await tick();
    await h.controller.__test.beginBargeIn();
    h.reply('fresh', 1); await tick();
    assert.equal(h.played.length, 1);
    delayed.resolve({ok: true, arrayBuffer: async () => new ArrayBuffer(1)});
    await tick();
    assert.equal(h.played.length, 1);
    assert.equal(h.requests.filter(r => r.url.endsWith('/tts')).length, 2);
    await h.controller.endCall();
});

test('continue stays in conversation; hangup permits switching', async () => {
    const h = harness();
    const keep = h.controller.guardConversationSwitch('b');
    h.elements.get('chat-call-switch-continue').handlers.click();
    assert.equal(await keep, false);
    assert.equal(h.controller.isActive(), true);
    const leave = h.controller.guardConversationSwitch('b');
    await h.elements.get('chat-call-switch-end').handlers.click();
    assert.equal(await leave, true);
    assert.equal(h.controller.isActive(), false);
});

test('unknown and stale voice events are consumed without playback', async () => {
    const h = harness();
    assert.equal(h.reply('other-call', 0, 'call-b'), true);
    assert.equal(h.reply('future-generation', 8), true);
    await tick();
    assert.equal(h.requests.length, 0);
    await h.controller.endCall();
});

test('hanging up while start is pending ends the late-created server call', async () => {
    const created = deferred();
    const h = harness(async url => url === '/api/chat/calls' ? created.promise : undefined);
    await h.controller.endCall();
    const start = h.controller.beginCall(); await tick();
    await h.controller.endCall();
    created.resolve({ok: true, status: 200, json: async () => ({id: 'late-call', conversation_id: 'a'})});
    assert.equal(await start, false);
    assert(h.requests.some(r => r.url === '/api/chat/calls/late-call/end'));
    assert.equal(h.controller.isActive(), false);
});

test('late audio unlock after hangup cannot reacquire the microphone', async () => {
    const unlocked = deferred(); let micStarts = 0;
    const h = harness(async url => url.endsWith('/status') ? {
        ok: true, status: 200, json: async () => ({tts_ready: true, asr_ready: true}),
    } : undefined);
    await h.controller.endCall();
    h.audio.unlock = () => unlocked.promise;
    h.audio.start = async () => { micStarts++; return true; };
    assert.equal(await h.controller.beginCall(), true);
    await h.controller.endCall();
    unlocked.resolve(false);
    await tick();
    assert.equal(micStarts, 0);
    assert.equal(h.controller.isActive(), false);
});

test('transcript starts collapsed and its toggle keeps hidden and ARIA state aligned', () => {
    const h = harness();
    const conversation = h.elements.get('chat-call-conversation');
    const toggle = h.elements.get('chat-call-transcript-toggle');
    assert.equal(conversation.hidden, true);
    assert.equal(conversation.getAttribute('aria-hidden'), 'true');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(toggle.getAttribute('aria-controls'), 'chat-call-conversation');

    toggle.handlers.click();
    assert.equal(conversation.hidden, false);
    assert.equal(conversation.getAttribute('aria-hidden'), 'false');
    assert.equal(toggle.getAttribute('aria-expanded'), 'true');
    toggle.handlers.click();
    assert.equal(conversation.hidden, true);
    assert.equal(conversation.getAttribute('aria-hidden'), 'true');
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('call with unavailable ASR opens transcript for typed input', async () => {
    const h = harness(undefined, {activeRecord: false, status: {tts_ready: true, asr_ready: false}});
    assert.equal(await h.controller.beginCall(), true);
    await tick();
    assert.equal(h.elements.get('chat-call-conversation').hidden, false);
    assert.equal(h.elements.get('chat-call-transcript-toggle').getAttribute('aria-expanded'), 'true');
    await h.controller.endCall();
});

test('microphone permission failure opens transcript and disables mute', async () => {
    const h = harness(undefined, {activeRecord: false, status: {tts_ready: true, asr_ready: true}});
    h.audio.start = async () => false;
    assert.equal(await h.controller.beginCall(), true);
    await tick();
    assert.equal(h.elements.get('chat-call-conversation').hidden, false);
    assert.equal(h.elements.get('chat-call-transcript-toggle').getAttribute('aria-expanded'), 'true');
    assert.equal(h.elements.get('chat-call-mute').disabled, true);
    await h.controller.endCall();
});
