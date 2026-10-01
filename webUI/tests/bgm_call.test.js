'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
};

function harness({enabled = true, delayedPlaylist = false} = {}) {
    const elements = new Map();
    const styles = [];
    const gestures = new Map();
    const saved = new Map([['nacho_ui_settings', JSON.stringify({startup: false, interactive: false, bgm: enabled})]]);
    const events = new EventTarget();
    const audioContexts = [];
    const pendingAudioLoads = new Map();
    const playlist = deferred();
    const makeElement = () => {
        const classes = new Set();
        return {
            style: {}, dataset: {}, hidden: false, handlers: {}, children: [],
            classList: {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)},
            addEventListener(name, callback) { this.handlers[name] = callback; },
            setAttribute() {}, appendChild(child) { this.children.push(child); }, contains: () => false,
        };
    };
    class AudioContext {
        constructor() {
            this.state = 'suspended';
            this.currentTime = 0;
            this.destination = {};
            this.starts = 0;
            audioContexts.push(this);
        }
        createGain() {
            const gain = {value: 0, cancelScheduledValues() {},
                setTargetAtTime(value) { this.value = value; }, setValueAtTime(value) { this.value = value; }};
            return {gain, connect: target => target, disconnect() {}};
        }
        createAnalyser() { return {frequencyBinCount: 256}; }
        createBufferSource() {
            return {connect: target => target, start: () => { this.starts++; }, stop() {}, disconnect() {}};
        }
        decodeAudioData() { return Promise.resolve({duration: 10}); }
        async resume() {
            if (this.pendingResume) await this.pendingResume.promise;
            this.state = 'running';
        }
        async suspend() { this.state = 'suspended'; }
    }
    let bgm;
    const context = {
        console, Event, EventTarget, AudioContext, Uint8Array, setTimeout, clearTimeout,
        navigator: {userActivation: {isActive: true}},
        localStorage: {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key)},
        document: {
            readyState: 'loading', documentElement: {dataset: {}}, body: {appendChild() {}},
            head: {appendChild: style => styles.push(style.innerHTML)},
            createElement: makeElement,
            getElementById(id) {
                if (!elements.has(id)) elements.set(id, makeElement());
                return elements.get(id);
            },
            addEventListener: (name, callback) => gestures.set(name, callback),
            removeEventListener: (name, callback) => { if (gestures.get(name) === callback) gestures.delete(name); },
        },
        ParticleSystem: {setAudioSource: player => { bgm = player; }, start() {}, stop() {}},
        addEventListener: (...args) => events.addEventListener(...args),
        fetch: async url => url === '/api/music/list'
            ? delayedPlaylist ? playlist.promise : {ok: true, json: async () => [{name: 'song', loopUrl: '/song.mp3'}]}
            : pendingAudioLoads.get(url)?.promise || {ok: true, arrayBuffer: async () => new ArrayBuffer(1)},
    };
    context.window = context;
    vm.createContext(context);
    for (const name of ['easter-eggs.js', 'bgm-player.js', 'ui.js']) {
        const sourcePath = path.join(__dirname, '../static/js', name);
        vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context, {filename: sourcePath});
    }
    const ui = vm.runInContext('UI', context);
    return {ui, context, elements, saved, styles, gestures, audioContexts, get bgm() { return bgm; },
        deferAudio(url) { const load = deferred(); pendingAudioLoads.set(url, load); return () => load.resolve({ok: true, arrayBuffer: async () => new ArrayBuffer(1)}); },
        activateOmega: () => events.dispatchEvent(new Event('nachobot:omega-tip')),
        releasePlaylist: () => playlist.resolve({ok: true, json: async () => [{name: 'song', loopUrl: '/song.mp3'}]})};
}

test('call hides the player, pauses music, and restores display without changing settings or resuming', async () => {
    const h = harness();
    await h.ui.init();
    await tick();
    const player = h.elements.get('mini-player');
    const settings = h.saved.get('nacho_ui_settings');
    assert.equal(h.bgm.paused, false);
    assert.equal(player.style.display, 'flex');
    h.ui.setVoiceCallActive(true);
    assert.equal(player.hidden, true);
    assert.equal(h.bgm.paused, true);
    assert.equal(h.audioContexts[0].state, 'suspended');
    assert.equal(h.bgm._masterGain.gain.value, 0);
    assert(h.styles.some(style => /#mini-player\[hidden\]\s*\{\s*display: none !important;/.test(style)));
    h.ui.setVoiceCallActive(false);
    assert.equal(player.hidden, false);
    assert.equal(h.bgm.paused, true);
    assert.equal(h.saved.get('nacho_ui_settings'), settings);
    h.elements.get('bgm-play-btn').handlers.click();
    await tick();
    assert.equal(h.bgm.paused, false);
    assert.equal(h.bgm._masterGain.gain.value, 0.2);
});

test('settings and OMEGA playback requests cannot restart BGM or reveal the player during a call', async () => {
    const h = harness();
    await h.ui.init();
    h.ui.setVoiceCallActive(true);
    const toggle = h.elements.get('toggle-bgm');
    toggle.handlers.change({target: {checked: true}});
    await tick();
    assert.equal(h.bgm.paused, true);
    h.activateOmega();
    await tick();
    assert.equal(h.bgm.paused, true);
    assert.equal(h.elements.get('mini-player').hidden, true);
    assert.equal(h.bgm._masterGain.gain.value, 0);
    assert.equal(h.gestures.has('pointerdown'), false);
    h.ui.setVoiceCallActive(false);
    assert.equal(h.bgm.paused, true);
});

test('a call started before UI initialization or playlist loading prevents initial autoplay', async () => {
    const h = harness({delayedPlaylist: true});
    h.ui.setVoiceCallActive(true);
    const initialized = h.ui.init();
    assert.equal(h.elements.get('mini-player').hidden, true);
    h.releasePlaylist();
    await initialized;
    await tick();
    assert.equal(h.bgm.paused, true);
    assert.equal(h.audioContexts[0].starts, 0);
    assert.equal(h.elements.get('mini-player').hidden, true);
});

test('a disabled player stays disabled after hanging up', async () => {
    const h = harness({enabled: false});
    await h.ui.init();
    h.ui.setVoiceCallActive(true);
    h.ui.setVoiceCallActive(false);
    assert.equal(h.elements.get('mini-player').style.display, 'none');
    assert.equal(h.bgm.paused, true);
});

test('late audio resume cannot make paused music audible even after the call ends', async () => {
    const h = harness();
    await h.ui.init();
    await tick();
    const audio = h.audioContexts[0];
    h.bgm.pause();
    audio.pendingResume = deferred();
    const playing = h.bgm.play({userInitiated: true});
    h.ui.setVoiceCallActive(true);
    await h.bgm.play({userInitiated: true});
    h.ui.setVoiceCallActive(false);
    audio.pendingResume.resolve();
    await playing;
    assert.equal(h.bgm.paused, true);
    assert.equal(h.bgm._masterGain.gain.value, 0);
    audio.pendingResume = null;
    await h.bgm.play({userInitiated: true});
    assert.equal(h.bgm.paused, false);
    assert.equal(h.bgm._masterGain.gain.value, 0.2);
});

test('playlist and OMEGA loads that finish after hangup still wait for manual playback', async () => {
    const loading = harness({delayedPlaylist: true});
    const initialized = loading.ui.init();
    loading.ui.setVoiceCallActive(true);
    loading.ui.setVoiceCallActive(false);
    loading.releasePlaylist();
    await initialized;
    await tick();
    assert.equal(loading.bgm.paused, true);

    const h = harness();
    await h.ui.init();
    await tick();
    const finishOmega = h.deferAudio('/static/js/hellisthat/Flower%20Man.mp3');
    h.ui.setVoiceCallActive(true);
    h.activateOmega();
    await tick();
    h.ui.setVoiceCallActive(false);
    finishOmega();
    await tick();
    assert.equal(h.bgm.paused, true);
    h.elements.get('bgm-play-btn').handlers.click();
    await tick();
    assert.equal(h.bgm.paused, false);
});

test('an in-flight autoplay gesture cannot resume BGM after a call', async () => {
    const h = harness();
    h.context.navigator.userActivation.isActive = false;
    await h.ui.init();
    await tick();
    assert(h.gestures.has('pointerdown'));
    const audio = h.audioContexts[0];
    audio.pendingResume = deferred();
    h.gestures.get('pointerdown')();
    h.ui.setVoiceCallActive(true);
    h.ui.setVoiceCallActive(false);
    audio.pendingResume.resolve();
    await tick();
    assert.equal(h.bgm.paused, true);
    assert.equal(h.bgm._masterGain.gain.value, 0);
});
