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
    const ANALYSER_FFT_SIZE = 512;
    const ANALYSER_SMOOTHING = 0.7;
    const BEAT_MIN_INTERVAL_SECONDS = 0.16;

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
            this._analyser = null;
            this._frequencyData = null;
            this._reactive = {
                bassBaseline: 0,
                previousBass: 0,
                lastBeatAt: -Infinity,
                pulse: 0,
            };
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

        /**
         * 返回当前音频帧的低频、中频、高频能量以及鼓点脉冲。
         * 所有数值均归一化到 0..1；暂停时会平滑衰减到 0。
         */
        getReactiveFrame() {
            const state = this._reactive;

            if (
                this._paused ||
                !this._analyser ||
                !this._frequencyData ||
                this._audioContext?.state !== 'running'
            ) {
                state.pulse *= 0.82;
                return {
                    bass: 0,
                    mid: 0,
                    high: 0,
                    intensity: 0,
                    pulse: state.pulse,
                    beat: false,
                };
            }

            this._analyser.getByteFrequencyData(this._frequencyData);

            const binHz = this._audioContext.sampleRate / this._analyser.fftSize;
            const averageBand = (lowHz, highHz) => {
                const start = Math.max(0, Math.floor(lowHz / binHz));
                const end = Math.min(
                    this._frequencyData.length - 1,
                    Math.ceil(highHz / binHz)
                );
                if (end < start) return 0;

                let total = 0;
                for (let index = start; index <= end; index += 1) {
                    total += this._frequencyData[index];
                }
                return total / ((end - start + 1) * 255);
            };

            const bass = averageBand(45, 180);
            const mid = averageBand(180, 1800);
            const high = averageBand(1800, 8000);

            if (state.bassBaseline <= 0) {
                state.bassBaseline = bass;
            } else {
                const baselineRate = bass > state.bassBaseline ? 0.025 : 0.08;
                state.bassBaseline += (bass - state.bassBaseline) * baselineRate;
            }

            const now = this._audioContext.currentTime;
            const transient = bass - state.previousBass;
            const threshold = Math.max(0.06, state.bassBaseline * 1.1 + 0.01);
            const minTransient = Math.max(0.015, state.bassBaseline * 0.08);
            const beat =
                bass > threshold &&
                transient > minTransient &&
                now - state.lastBeatAt >= BEAT_MIN_INTERVAL_SECONDS;

            if (beat) {
                state.lastBeatAt = now;
                state.pulse = Math.min(1, 0.75 + bass * 1.0);
            } else {
                state.pulse *= 0.88;
            }
            state.previousBass = bass;

            const weightedEnergy = bass * 0.55 + mid * 0.3 + high * 0.15;
            const intensity = Math.min(1, Math.sqrt(Math.max(0, weightedEnergy)) * 0.95);

            return {
                bass,
                mid,
                high,
                intensity,
                pulse: state.pulse,
                beat,
            };
        }

        setTrack(track) {
            const wasPlaying = !this._paused;
            this._generation += 1;
            this._playRequested = false;
            this._track = track;
            this._resetReactiveState();
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

        async play({ userInitiated = typeof navigator !== 'undefined' && navigator.userActivation?.isActive === true } = {}) {
            if (!this._track) return;

            const generation = this._generation;
            const context = this._ensureAudioGraph();
            if (context.state !== 'running' && !userInitiated) {
                this._playRequested = false;
                throw new Error('Audio playback requires a user interaction.');
            }

            this._playRequested = true;
            try {
                await this.unlock();
            } catch (error) {
                this._playRequested = false;
                throw error;
            }
            if (context.state !== 'running') {
                this._playRequested = false;
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

        async unlock() {
            const context = this._ensureAudioGraph();
            if (context.state !== 'running') {
                await context.resume();
            }
            if (context.state !== 'running') {
                throw new Error('Audio playback requires a user interaction.');
            }
        }
        _ensureAudioGraph() {
            if (this._audioContext) return this._audioContext;

            const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextConstructor) {
                throw new Error('This browser does not support Web Audio playback.');
            }

            this._audioContext = new AudioContextConstructor();
            this._inputNode = this._audioContext.createGain();
            this._masterGain = this._audioContext.createGain();
            this._analyser = this._audioContext.createAnalyser();

            this._masterGain.gain.value = this._volume;
            this._analyser.fftSize = ANALYSER_FFT_SIZE;
            this._analyser.smoothingTimeConstant = ANALYSER_SMOOTHING;
            this._analyser.minDecibels = -90;
            this._analyser.maxDecibels = -10;
            this._frequencyData = new Uint8Array(this._analyser.frequencyBinCount);

            this._inputNode.connect(this._analyser);
            this._inputNode.connect(this._masterGain);
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
            source.connect(gain).connect(this._inputNode);
            const node = { source, gain };
            this._nodes.push(node);
            return node;
        }

        _resetReactiveState() {
            this._reactive.bassBaseline = 0;
            this._reactive.previousBass = 0;
            this._reactive.lastBeatAt = -Infinity;
            this._reactive.pulse = 0;
            this._frequencyData?.fill(0);
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
        return name.replace(/\.(?:mp3|wav|ogg|flac)$/i, '');
    }

    window.SeamlessBgmPlayer = SeamlessBgmPlayer;
    window.normalizeBgmPlaylist = normalizeBgmPlaylist;
})();
