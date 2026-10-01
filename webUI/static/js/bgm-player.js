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
    const ANALYSER_FFT_SIZE = 4096;
    const ANALYSER_SMOOTHING = 0;
    const BEAT_MIN_INTERVAL_SECONDS = 0.16;
    const RMS_NORMALIZATION_REFERENCE = 0.20;
    const RMS_SILENCE_FLOOR = 0.00001;
    const ENVELOPE_SILENCE_FLOOR = 0.001;
    const ENVELOPE_ATTACK_SECONDS = 0.020;
    const ENVELOPE_RELEASE_SECONDS = 0.140;
    const BASELINE_SECONDS = 0.75;
    const MAX_ONSET_GAP_SECONDS = 0.35;
    const MIN_ONSET_LEVEL = 0.045;
    const MIN_ONSET_SLOPE_PER_SECOND = 0.8;
    const ONSET_BASELINE_LEVEL_RATIO = 0.45;
    const ONSET_MIN_EXCURSION = 0.025;
    const ONSET_BASELINE_EXCURSION_RATIO = 0.12;
    const ONSET_REARM_SLOPE_RATIO = 0.25;
    const ONSET_REARM_QUIET_SECONDS = 0.06;
    const PULSE_SLOPE_WINDOW_SECONDS = 0.04;
    const PULSE_RISE_REFERENCE = 0.30;
    const PULSE_RELEASE_SECONDS = 0.22;
    const PAUSE_GAP_THRESHOLD_SECONDS = 0.08;
    const MAX_REACTIVE_DELTA_SECONDS = 10;

    function monotonicTimeSeconds() {
        if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
            return performance.now() / 1000;
        }
        return Date.now() / 1000;
    }

    function createReactiveState() {
        return {
            bass: 0,
            mid: 0,
            high: 0,
            intensity: 0,
            bassBaseline: 0,
            previousBass: 0,
            hasOnsetSample: false,
            onsetArmed: true,
            onsetQuietSeconds: 0,
            lastBeatAt: null,
            pulse: 0,
            lastAudioTime: null,
            lastWallTime: monotonicTimeSeconds(),
            wasRunning: false,
        };
    }

    function makeBandRange(sampleRate, fftSize, binCount, lowHz, highHz) {
        const binHz = sampleRate / fftSize;
        const nyquist = sampleRate / 2;
        const start = Math.min(
            binCount,
            Math.max(1, Math.ceil(lowHz / binHz))
        );
        const end = Math.max(
            start,
            Math.min(binCount, Math.ceil(Math.min(highHz, nyquist) / binHz))
        );
        return [start, end];
    }

    function sumBandPower(frequencyData, range) {
        let total = 0;
        for (let index = range[0]; index < range[1]; index += 1) {
            const decibels = frequencyData[index];
            if (!Number.isFinite(decibels)) continue;

            const power = 10 ** (decibels / 10);
            if (Number.isFinite(power) && power > 0) total += power;
        }
        return total;
    }

    function normalizeSpectralRms(power) {
        const rms = Math.sqrt(Math.max(0, power));
        if (!Number.isFinite(rms) || rms <= RMS_SILENCE_FLOOR) return 0;
        // This is proportional spectral RMS from the analyser's dB bins, not LUFS.
        return Math.min(1, rms / RMS_NORMALIZATION_REFERENCE);
    }

    function approach(current, target, deltaSeconds, timeConstant) {
        if (deltaSeconds <= 0) return current;
        const coefficient = 1 - Math.exp(-deltaSeconds / timeConstant);
        const value = current + (target - current) * coefficient;
        return Math.abs(value) < ENVELOPE_SILENCE_FLOOR ? 0 : value;
    }

    function applyReactiveFrame(state, targets, deltaSeconds) {
        state.bass = approach(
            state.bass,
            targets.bass,
            deltaSeconds,
            targets.bass > state.bass ? ENVELOPE_ATTACK_SECONDS : ENVELOPE_RELEASE_SECONDS
        );
        state.mid = approach(
            state.mid,
            targets.mid,
            deltaSeconds,
            targets.mid > state.mid ? ENVELOPE_ATTACK_SECONDS : ENVELOPE_RELEASE_SECONDS
        );
        state.high = approach(
            state.high,
            targets.high,
            deltaSeconds,
            targets.high > state.high ? ENVELOPE_ATTACK_SECONDS : ENVELOPE_RELEASE_SECONDS
        );
        state.intensity = approach(
            state.intensity,
            targets.intensity,
            deltaSeconds,
            targets.intensity > state.intensity ? ENVELOPE_ATTACK_SECONDS : ENVELOPE_RELEASE_SECONDS
        );
        state.pulse = approach(
            state.pulse,
            0,
            deltaSeconds,
            PULSE_RELEASE_SECONDS
        );
    }

    function reactiveFrame(state, beat = false) {
        return {
            bass: state.bass,
            mid: state.mid,
            high: state.high,
            intensity: state.intensity,
            pulse: state.pulse,
            beat,
        };
    }

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
            this._playbackBlocked = false;
            this._volume = 0.2;
            this._analyser = null;
            this._frequencyData = null;
            this._bandRanges = null;
            this._reactive = createReactiveState();
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
            if (this._playbackBlocked || this._paused) {
                gain.setValueAtTime(0, this._audioContext.currentTime);
            } else {
                gain.setTargetAtTime(this._volume, this._audioContext.currentTime, 0.015);
            }
        }

        setPlaybackBlocked(blocked) {
            this._playbackBlocked = Boolean(blocked);
            // Mute immediately as well as suspending: an older resume() may still finish.
            this.volume = this._volume;
            if (this._playbackBlocked) this.pause();
        }

        /**
         * 返回当前音频帧的低频、中频、高频能量以及鼓点脉冲。
         * 所有数值均归一化到 0..1；暂停时会平滑衰减到 0。
         */
        getReactiveFrame() {
            const state = this._reactive;
            const wallNow = monotonicTimeSeconds();
            const wallDelta = state.lastWallTime === null
                ? 0
                : Math.max(0, wallNow - state.lastWallTime);
            state.lastWallTime = wallNow;

            if (
                this._paused ||
                !this._analyser ||
                !this._frequencyData ||
                this._audioContext?.state !== 'running'
            ) {
                const contextTime = this._audioContext?.currentTime;
                state.lastAudioTime = Number.isFinite(contextTime) ? contextTime : null;
                state.hasOnsetSample = false;
                state.previousBass = 0;
                state.onsetArmed = true;
                state.onsetQuietSeconds = 0;
                state.bassBaseline = approach(
                    state.bassBaseline,
                    0,
                    Math.min(wallDelta, MAX_REACTIVE_DELTA_SECONDS),
                    BASELINE_SECONDS
                );
                state.wasRunning = false;
                applyReactiveFrame(state, { bass: 0, mid: 0, high: 0, intensity: 0 },
                    Math.min(wallDelta, MAX_REACTIVE_DELTA_SECONDS));
                return reactiveFrame(state);
            }

            const audioNow = this._audioContext.currentTime;
            if (!Number.isFinite(audioNow)) {
                state.wasRunning = false;
                state.hasOnsetSample = false;
                state.onsetArmed = true;
                state.onsetQuietSeconds = 0;
                applyReactiveFrame(state, { bass: 0, mid: 0, high: 0, intensity: 0 },
                    Math.min(wallDelta, MAX_REACTIVE_DELTA_SECONDS));
                return reactiveFrame(state);
            }

            this._analyser.getFloatFrequencyData(this._frequencyData);

            const bassPower = sumBandPower(this._frequencyData, this._bandRanges.bass);
            const midPower = sumBandPower(this._frequencyData, this._bandRanges.mid);
            const highPower = sumBandPower(this._frequencyData, this._bandRanges.high);
            const bass = normalizeSpectralRms(bassPower);
            const mid = normalizeSpectralRms(midPower);
            const high = normalizeSpectralRms(highPower);
            const intensity = normalizeSpectralRms(bassPower + midPower + highPower);

            let audioDelta = 0;
            let clockReset = false;
            if (state.lastAudioTime !== null) {
                if (audioNow >= state.lastAudioTime) {
                    audioDelta = audioNow - state.lastAudioTime;
                } else {
                    clockReset = true;
                }
            }

            let pauseGap = 0;
            if (!state.wasRunning) {
                pauseGap = wallDelta;
            } else if (state.lastAudioTime !== null) {
                pauseGap = Math.max(0, wallDelta - audioDelta);
            }
            const pausedBetweenFrames = pauseGap >= PAUSE_GAP_THRESHOLD_SECONDS;
            if (pausedBetweenFrames || clockReset) {
                applyReactiveFrame(state, { bass: 0, mid: 0, high: 0, intensity: 0 },
                    Math.min(pauseGap, MAX_REACTIVE_DELTA_SECONDS));
                state.hasOnsetSample = false;
                state.previousBass = 0;
                state.onsetArmed = true;
                state.onsetQuietSeconds = 0;
            }

            const firstAudioFrame = !state.wasRunning || state.lastAudioTime === null;
            const envelopeDelta = firstAudioFrame
                ? Math.min(wallDelta, 0.1)
                : Math.min(audioDelta, MAX_REACTIVE_DELTA_SECONDS);
            applyReactiveFrame(state, { bass, mid, high, intensity }, envelopeDelta);

            let beat = false;
            if (!state.hasOnsetSample || clockReset || pausedBetweenFrames || audioDelta > MAX_ONSET_GAP_SECONDS) {
                // A first frame or long gap seeds the detector without inventing an onset.
                state.bassBaseline = bass;
                state.previousBass = bass;
                state.hasOnsetSample = true;
                state.onsetArmed = true;
                state.onsetQuietSeconds = 0;
            } else if (audioDelta > 0) {
                const baselineCoefficient = 1 - Math.exp(-audioDelta / BASELINE_SECONDS);
                state.bassBaseline += (bass - state.bassBaseline) * baselineCoefficient;

                const rise = Math.max(0, bass - state.previousBass);
                const positiveSlope = rise / audioDelta;
                const minimumLevel = Math.max(
                    MIN_ONSET_LEVEL,
                    state.bassBaseline * (1 + ONSET_BASELINE_LEVEL_RATIO)
                );
                const minimumExcursion = Math.max(
                    ONSET_MIN_EXCURSION,
                    state.bassBaseline * ONSET_BASELINE_EXCURSION_RATIO
                );
                const risingOnsetCandidate = bass >= minimumLevel &&
                    bass - state.bassBaseline >= minimumExcursion &&
                    positiveSlope >= MIN_ONSET_SLOPE_PER_SECOND;
                const outsideRefractory = state.lastBeatAt === null ||
                    audioNow - state.lastBeatAt >= BEAT_MIN_INTERVAL_SECONDS;

                if (!state.onsetArmed) {
                    if (positiveSlope <= MIN_ONSET_SLOPE_PER_SECOND * ONSET_REARM_SLOPE_RATIO) {
                        state.onsetQuietSeconds += audioDelta;
                        if (state.onsetQuietSeconds >= ONSET_REARM_QUIET_SECONDS) {
                            state.onsetArmed = true;
                            state.onsetQuietSeconds = 0;
                        }
                    } else {
                        state.onsetQuietSeconds = 0;
                    }
                }

                if (state.onsetArmed && risingOnsetCandidate) {
                    // Latch each rise through refractory so one ramp cannot fire again later.
                    state.onsetArmed = false;
                    state.onsetQuietSeconds = 0;
                    if (outsideRefractory) {
                        beat = true;
                        state.lastBeatAt = audioNow;
                        const onsetStrength =
                            positiveSlope * PULSE_SLOPE_WINDOW_SECONDS;
                        state.pulse = Math.min(1, onsetStrength / PULSE_RISE_REFERENCE);
                    }
                }

                state.previousBass = bass;
            }

            state.lastAudioTime = audioNow;
            state.lastWallTime = wallNow;
            state.wasRunning = true;
            return reactiveFrame(state, beat);
        }

        setTrack(track) {
            const wasPlaying = !this._paused;
            this._generation += 1;
            this._playRequested = false;
            this._track = track;
            this._resetReactiveState();
            this._stopSources();
            this._paused = true;
            this.volume = this._volume;

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
            if (this._playbackBlocked || !this._track) return;

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
            if (this._playbackBlocked) return;
            if (context.state !== 'running') {
                this._playRequested = false;
                throw new Error('Audio playback requires a user interaction.');
            }

            const prepared = await this._trackReady;
            if (this._playbackBlocked || !this._playRequested || generation !== this._generation || !prepared) return;

            if (this._nodes.length === 0) {
                this._startPreparedTrack(prepared);
            }

            if (this._paused) {
                this._paused = false;
                this.volume = this._volume;
                this.dispatchEvent(new Event('play'));
            }
        }

        pause() {
            this._playRequested = false;
            if (this._paused) return;

            this._paused = true;
            this.volume = this._volume;
            if (this._audioContext?.state === 'running') {
                this._audioContext.suspend().catch(error => console.warn('Failed to pause BGM:', error));
            }
            this.dispatchEvent(new Event('pause'));
        }

        async unlock() {
            if (this._playbackBlocked) return;
            const context = this._ensureAudioGraph();
            if (context.state !== 'running') {
                await context.resume();
            }
            if (this._playbackBlocked) {
                await context.suspend();
                return;
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

            this._masterGain.gain.value = this._playbackBlocked || this._paused ? 0 : this._volume;
            this._analyser.fftSize = ANALYSER_FFT_SIZE;
            this._analyser.smoothingTimeConstant = ANALYSER_SMOOTHING;
            this._analyser.minDecibels = -90;
            this._analyser.maxDecibels = -10;
            // Cache disjoint bin-center ranges once; the upper bin is exclusive.
            this._frequencyData = new Float32Array(this._analyser.frequencyBinCount);
            this._bandRanges = {
                bass: makeBandRange(this._audioContext.sampleRate, this._analyser.fftSize,
                    this._analyser.frequencyBinCount, 45, 180),
                mid: makeBandRange(this._audioContext.sampleRate, this._analyser.fftSize,
                    this._analyser.frequencyBinCount, 180, 1800),
                high: makeBandRange(this._audioContext.sampleRate, this._analyser.fftSize,
                    this._analyser.frequencyBinCount, 1800, 8000),
            };

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
            this._reactive = createReactiveState();
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
