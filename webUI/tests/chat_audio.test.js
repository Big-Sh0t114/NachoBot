const assert = require('node:assert/strict');
const test = require('node:test');
require('../static/js/chat-audio.js');

class Context {
    constructor() { this.sampleRate = 16000; this.state = 'running'; this.currentTime = 0; this.destination = {}; }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createAnalyser() { return {connect() {}, disconnect() {}, fftSize: 512}; }
    createScriptProcessor() { return {connect() {}, disconnect() {}}; }
    createGain() { return {gain: {value: 1}, connect() {}, disconnect() {}}; }
    async close() { this.state = 'closed'; }
}

test('automatic speech onset and silence produce mono 16k PCM WAV only in memory', async () => {
    let starts = 0; const segments = []; let constraints;
    const track = {stop() {}, enabled: true};
    const engine = ChatAudio.create({AudioContextClass: Context, windowRef: {btoa},
        navigatorRef: {mediaDevices: {getUserMedia: async c => {constraints = c; return {getTracks: () => [track], getAudioTracks: () => [track]}; }}},
        onSpeechStart: () => starts++, onSegment: segment => segments.push(segment), onError: error => {throw error;}});
    await engine.start();
    for (let i = 0; i < 65; i++) {
        engine.__test.handleAudioFrame({playbackTime: (i + 1) * .02,
            inputBuffer: {getChannelData: () => new Float32Array(320).fill(i < 20 ? .2 : 0)}});
    }
    assert.equal(starts, 1);
    assert.equal(segments.length, 1);
    const wav = Buffer.from(segments[0].audioBase64, 'base64');
    assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
    assert.equal(wav.readUInt32LE(24), 16000);
    assert.equal(wav.readUInt16LE(22), 1);
    assert.equal(wav.readUInt16LE(34), 16);
    assert.equal(constraints.audio.echoCancellation, true);
    engine.setMuted(true);
    assert.equal(track.enabled, false);
    await engine.stop();
});

test('late microphone permission after hangup immediately releases tracks', async () => {
    let resolve; let stopped = 0;
    const engine = ChatAudio.create({AudioContextClass: Context, windowRef: {},
        navigatorRef: {mediaDevices: {getUserMedia: () => new Promise(r => {resolve = r;})}}});
    const starting = engine.start();
    await engine.stop();
    resolve({getTracks: () => [{stop: () => stopped++}]});
    assert.equal(await starting, false);
    assert.equal(stopped, 1);
    assert.equal(engine.isActive(), false);
});
