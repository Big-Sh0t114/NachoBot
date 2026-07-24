/**
 * Web Audio BGM engine.
 *
 * Audio buffers are decoded before playback. Intro and loop sections are
 * scheduled on the same AudioContext timeline, avoiding the decoder and
 * network gap caused by swapping <audio>.src at the boundary.
 */
(() => {
    const START_DELAY_SECONDS = 0.03;
    const JOIN_FADE_SECONDS = 0.012;
    const MAX_CACHED_BUFFERS = 4;

    class SeamlessBgmPlayer extends EventTarget {
        constructor() {
            super();
            this._audioContext = null;
            this._masterGain = null;
            this._track = null;
            this._trackReady = Promise.resolve(null);
            this._bufferCache = new Map();
            this._nodes = [];
            this._generation = 0;
            this._paused = true;
            this._playRequested = false;
            this._volume = 0.2;
        }

        get paused() {
            return this._paused;
        }

        get volume() {
            return this._volume;
        }

        set volume(value) {
            this._volume = Math.min(1, Math.max(0, Number(value) || 0));
            if (!this._masterGain || !this._audioContext) return;

            const gain = this._masterGain.gain;
            gain.cancelScheduledValues(this._audioContext.currentTime);
            gain.setTargetAtTime(this._volume, this._audioContext.currentTime, 0.015);
        }

        setTrack(track) {
            const wasPlaying = !this._paused;
            this._generation += 1;
            this._playRequested = false;
            this._track = track;
            this._stopSources();
            this._paused = true;

            if (wasPlaying) {
                this._audioContext?.suspend().catch(error => console.warn('Failed to pause BGM:', error));
                this.dispatchEvent(new Event('pause'));
            }

            const generation = this._generation;
            this._trackReady = Promise.resolve()
                .then(() => this._prepareTrack(track))
                .then(prepared => generation === this._generation ? prepared : null);
            return this._trackReady;
        }

        async play() {
            if (!this._track) return;

            this._playRequested = true;
            const generation = this._generation;
            const context = this._ensureAudioGraph();
            await context.resume();
            if (context.state !== 'running') {
                throw new Error('Audio playback requires a user interaction.');
            }

            const prepared = await this._trackReady;
            if (!this._playRequested || generation !== this._generation || !prepared) return;

            if (this._nodes.length === 0) {
                this._startPreparedTrack(prepared);
            }

            if (this._paused) {
                this._paused = false;
                this.dispatchEvent(new Event('play'));
            }
        }

        pause() {
            this._playRequested = false;
            if (this._paused) return;

            this._paused = true;
            if (this._audioContext?.state === 'running') {
                this._audioContext.suspend().catch(error => console.warn('Failed to pause BGM:', error));
            }
            this.dispatchEvent(new Event('pause'));
        }

        _ensureAudioGraph() {
            if (this._audioContext) return this._audioContext;

            const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextConstructor) {
                throw new Error('This browser does not support Web Audio playback.');
            }

            this._audioContext = new AudioContextConstructor();
            this._masterGain = this._audioContext.createGain();
            this._masterGain.gain.value = this._volume;
            this._masterGain.connect(this._audioContext.destination);
            return this._audioContext;
        }

        async _prepareTrack(track) {
            if (!track?.loopUrl) {
                throw new Error('BGM track has no loop segment.');
            }

            const loop = this._loadBuffer(track.loopUrl);
            const intro = track.introUrl ? this._loadBuffer(track.introUrl) : null;
            return {
                intro: intro ? await intro : null,
                loop: await loop,
            };
        }

        _loadBuffer(url) {
            const cached = this._bufferCache.get(url);
            if (cached) return cached;

            const context = this._ensureAudioGraph();
            const bufferPromise = fetch(url, { cache: 'force-cache' })
                .then(response => {
                    if (!response.ok) throw new Error(`Failed to load BGM: ${response.status}`);
                    return response.arrayBuffer();
                })
                .then(bytes => context.decodeAudioData(bytes));

            this._bufferCache.set(url, bufferPromise);
            while (this._bufferCache.size > MAX_CACHED_BUFFERS) {
                this._bufferCache.delete(this._bufferCache.keys().next().value);
            }
            return bufferPromise;
        }

        _startPreparedTrack({ intro, loop }) {
            const context = this._audioContext;
            const startAt = context.currentTime + START_DELAY_SECONDS;
            const loopNode = this._createNode(loop);
            loopNode.source.loop = true;

            if (!intro) {
                loopNode.source.start(startAt);
                return;
            }

            const introNode = this._createNode(intro);
            const fade = Math.min(JOIN_FADE_SECONDS, intro.duration / 2, loop.duration / 2);
            const loopStartAt = startAt + intro.duration - fade;

            if (fade > 0) {
                introNode.gain.gain.setValueAtTime(1, startAt);
                introNode.gain.gain.setValueAtTime(1, loopStartAt);
                introNode.gain.gain.linearRampToValueAtTime(0, loopStartAt + fade);

                loopNode.gain.gain.setValueAtTime(0, loopStartAt);
                loopNode.gain.gain.linearRampToValueAtTime(1, loopStartAt + fade);
            }

            introNode.source.start(startAt);
            loopNode.source.start(loopStartAt);
        }

        _createNode(buffer) {
            const source = this._audioContext.createBufferSource();
            const gain = this._audioContext.createGain();
            source.buffer = buffer;
            source.connect(gain).connect(this._masterGain);
            const node = { source, gain };
            this._nodes.push(node);
            return node;
        }

        _stopSources() {
            for (const { source, gain } of this._nodes) {
                try {
                    source.stop();
                } catch (_) {
                    // The source may already have ended.
                }
                source.disconnect();
                gain.disconnect();
            }
            this._nodes = [];
        }
    }

    function normalizeBgmPlaylist(items) {
        if (!Array.isArray(items)) return [];

        const tracks = [];
        const intros = new Map();
        const loops = new Map();

        for (const item of items) {
            const name = String(item?.name || '');
            if (item?.loopUrl) {
                tracks.push({ ...item, name: stripExtension(name) });
                continue;
            }

            if (!item?.url || !name) continue;
            const stem = stripExtension(name);
            const key = stem.toLowerCase();

            if (key.startsWith('in_') && stem.length > 3) {
                intros.set(stem.slice(3).toLowerCase(), { name: stem.slice(3), url: item.url });
            } else if (key.startsWith('lp_') && stem.length > 3) {
                loops.set(stem.slice(3).toLowerCase(), { name: stem.slice(3), url: item.url });
            } else {
                tracks.push({ name: stem, kind: 'loop', loopUrl: item.url });
            }
        }

        for (const key of new Set([...intros.keys(), ...loops.keys()])) {
            const intro = intros.get(key);
            const loop = loops.get(key);
            if (intro && loop) {
                tracks.push({
                    name: intro.name,
                    kind: 'intro-loop',
                    introUrl: intro.url,
                    loopUrl: loop.url,
                });
            } else if (loop) {
                tracks.push({ name: loop.name, kind: 'loop', loopUrl: loop.url });
            }
        }

        return tracks.sort((left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: 'base' }));
    }

    function stripExtension(name) {
        return name.replace(/\.[^/.]+$/, '');
    }

    window.SeamlessBgmPlayer = SeamlessBgmPlayer;
    window.normalizeBgmPlaylist = normalizeBgmPlaylist;
})();
