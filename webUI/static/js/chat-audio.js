/* Continuous local microphone capture, bounded VAD, and WAV encoding. */
(function attachChatAudio(root) {
    'use strict';

    const DEFAULTS = Object.freeze({
        preRollMs: 260,
        onsetMs: 180,
        silenceMs: 780,
        maxUtteranceMs: 12000,
        frameSize: 2048,
    });

    function create(options = {}) {
        const win = options.windowRef || root;
        const nav = options.navigatorRef || win.navigator;
        const AudioContextCtor = options.AudioContextClass
            || win.AudioContext
            || win.webkitAudioContext;
        const config = { ...DEFAULTS, ...(options.config || {}) };
        let stream = null;
        let context = null;
        let source = null;
        let processor = null;
        let analyser = null;
        let silentGain = null;
        let active = false;
        let muted = false;
        let frameHandle = null;
        let sampleRate = 0;
        let ambientRms = 0.004;
        let candidate = [];
        let candidateMs = 0;
        let preRoll = [];
        let preRollSamples = 0;
        let recording = null;
        let recordingSamples = 0;
        let lastVoiceAt = 0;
        let generation = 0;
        let lifecycle = 0;
        let pendingStart = null;
        const players = new Set();

        const onSpeechStart = typeof options.onSpeechStart === 'function' ? options.onSpeechStart : () => {};
        const onSegment = typeof options.onSegment === 'function' ? options.onSegment : () => {};
        const onError = typeof options.onError === 'function' ? options.onError : () => {};
        const onLevel = typeof options.onLevel === 'function' ? options.onLevel : () => {};

        async function start() {
            if (active) return;
            if (pendingStart) return pendingStart;
            const attempt = ++lifecycle;
            const task = startAttempt(attempt);
            pendingStart = task;
            try {
                return await task;
            } finally {
                if (pendingStart === task) pendingStart = null;
            }
        }

        async function startAttempt(attempt) {
            if (!nav?.mediaDevices?.getUserMedia || !AudioContextCtor) {
                throw new Error('当前浏览器不支持本地麦克风采集');
            }
            const nextStream = await nav.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            });
            if (attempt !== lifecycle) {
                stopTracks(nextStream);
                return false;
            }
            let nextContext = context;
            try {
                if (!nextContext) nextContext = new AudioContextCtor();
                if (nextContext.state === 'suspended') await nextContext.resume();
                if (attempt !== lifecycle) {
                    stopTracks(nextStream);
                    try { await nextContext.close(); } catch (_) {}
                    return false;
                }
                const nextSource = nextContext.createMediaStreamSource(nextStream);
                const nextAnalyser = nextContext.createAnalyser();
                nextAnalyser.fftSize = 1024;
                const nextProcessor = nextContext.createScriptProcessor(config.frameSize, 1, 1);
                const nextGain = nextContext.createGain();
                nextGain.gain.value = 0;
                nextSource.connect(nextAnalyser);
                nextSource.connect(nextProcessor);
                nextProcessor.connect(nextGain);
                nextGain.connect(nextContext.destination);
                stream = nextStream;
                context = nextContext;
                source = nextSource;
                analyser = nextAnalyser;
                processor = nextProcessor;
                silentGain = nextGain;
                sampleRate = nextContext.sampleRate;
                processor.onaudioprocess = handleAudioFrame;
                active = true;
                muted = false;
                generation += 1;
                scheduleLevel();
                return true;
            } catch (error) {
                stopTracks(nextStream);
                if (nextContext) {
                    try { await nextContext.close(); } catch (_) {}
                }
                throw error;
            }
        }

        async function unlock() {
            if (!AudioContextCtor) throw new Error('当前浏览器不支持音频播放');
            const attempt = ++lifecycle;
            const nextContext = context || new AudioContextCtor();
            context = nextContext;
            if (nextContext.state === 'suspended') await nextContext.resume();
            if (attempt !== lifecycle || context !== nextContext) {
                if (context !== nextContext) {
                    try { await nextContext.close(); } catch (_) {}
                }
                return false;
            }
            return true;
        }

        function createPlayer() {
            let canceled = false;
            let outputFrame = null;
            let outputAnalyser = null;
            let sourceNode = null;
            let resolveDone;
            const done = new Promise(resolve => { resolveDone = resolve; });
            let settled = false;
            const finish = status => {
                if (settled) return;
                settled = true;
                if (outputFrame !== null) win.cancelAnimationFrame?.(outputFrame);
                outputAnalyser?.disconnect();
                sourceNode?.disconnect();
                players.delete(player);
                resolveDone(status);
            };
            const player = {
                async play(arrayBuffer, hooks = {}) {
                    const playingContext = context;
                    if (!playingContext) throw new Error('音频播放设备未就绪');
                    if (canceled) return 'interrupted';
                    if (playingContext.state === 'suspended') await playingContext.resume();
                    if (canceled || context !== playingContext) return 'interrupted';
                    const copy = arrayBuffer.slice(0);
                    const decoded = await playingContext.decodeAudioData(copy);
                    if (canceled || context !== playingContext) return 'interrupted';
                    sourceNode = playingContext.createBufferSource();
                    sourceNode.buffer = decoded;
                    outputAnalyser = playingContext.createAnalyser();
                    outputAnalyser.fftSize = 512;
                    sourceNode.connect(outputAnalyser);
                    outputAnalyser.connect(playingContext.destination);
                    sourceNode.onended = () => finish(canceled ? 'interrupted' : 'played');
                    sourceNode.start();
                    if (!canceled) hooks.onStarted?.();
                    const updateMouth = () => {
                        if (settled || canceled || !win.requestAnimationFrame) return;
                        const values = new Uint8Array(outputAnalyser.fftSize);
                        outputAnalyser.getByteTimeDomainData(values);
                        let sum = 0;
                        for (const sample of values) sum += ((sample - 128) / 128) ** 2;
                        hooks.onLevel?.(Math.min(1, Math.sqrt(sum / values.length) * 4));
                        outputFrame = win.requestAnimationFrame(updateMouth);
                    };
                    updateMouth();
                    return done;
                },
                stop() {
                    canceled = true;
                    if (sourceNode) {
                        sourceNode.onended = null;
                        try { sourceNode.stop(); } catch (_) {}
                    }
                    finish('interrupted');
                },
            };
            players.add(player);
            return player;
        }

        function scheduleLevel() {
            if (!active || typeof win.requestAnimationFrame !== 'function') return;
            frameHandle = win.requestAnimationFrame(() => {
                frameHandle = null;
                if (!active) return;
                const values = new Uint8Array(analyser.fftSize);
                analyser.getByteTimeDomainData(values);
                let sum = 0;
                for (let i = 0; i < values.length; i += 1) {
                    const value = (values[i] - 128) / 128;
                    sum += value * value;
                }
                onLevel(Math.min(1, Math.sqrt(sum / values.length) * 3.5));
                scheduleLevel();
            });
        }

        function handleAudioFrame(event) {
            if (!active || muted) return;
            const input = event.inputBuffer.getChannelData(0);
            const samples = new Float32Array(input.length);
            samples.set(input);
            const rms = rootMeanSquare(samples);
            const now = (event.playbackTime || context.currentTime) * 1000;
            const threshold = Math.max(0.014, ambientRms * 2.8);
            const voice = rms >= threshold;

            if (!recording && !voice) {
                ambientRms = Math.min(0.035, (ambientRms * 0.94) + (rms * 0.06));
            }

            if (voice) {
                lastVoiceAt = now;
                if (!recording) {
                    candidate.push(samples);
                    candidateMs += samples.length * 1000 / sampleRate;
                    pushPreRoll(samples);
                    if (candidateMs >= config.onsetMs) beginRecording(now);
                } else {
                    appendRecording(samples);
                }
            } else if (!recording) {
                candidate = [];
                candidateMs = 0;
                pushPreRoll(samples);
            } else {
                appendRecording(samples);
                const durationMs = recordingSamples * 1000 / sampleRate;
                if (now - lastVoiceAt >= config.silenceMs || durationMs >= config.maxUtteranceMs) {
                    finishRecording();
                }
            }

            if (recording && recordingSamples * 1000 / sampleRate >= config.maxUtteranceMs) {
                finishRecording();
            }
        }

        function pushPreRoll(samples) {
            preRoll.push(samples);
            preRollSamples += samples.length;
            const limit = Math.round(sampleRate * config.preRollMs / 1000);
            while (preRollSamples > limit && preRoll.length > 1) {
                preRollSamples -= preRoll.shift().length;
            }
        }

        function beginRecording(now) {
            recording = preRoll.slice();
            recordingSamples = preRollSamples;
            preRoll = [];
            preRollSamples = 0;
            candidate = [];
            candidateMs = 0;
            lastVoiceAt = now;
            onSpeechStart();
        }

        function appendRecording(samples) {
            if (!recording) return;
            const remain = Math.max(0, Math.round(sampleRate * config.maxUtteranceMs / 1000) - recordingSamples);
            if (remain <= 0) return;
            const bounded = samples.length > remain ? samples.subarray(0, remain) : samples;
            recording.push(bounded);
            recordingSamples += bounded.length;
        }

        function finishRecording() {
            if (!recording) return;
            const chunks = recording;
            const count = recordingSamples;
            const utteranceGeneration = generation;
            recording = null;
            recordingSamples = 0;
            candidate = [];
            candidateMs = 0;
            preRoll = [];
            preRollSamples = 0;
            if (!count || utteranceGeneration !== generation) return;
            try {
                const samples = flatten(chunks, count);
                const audioBase64 = encodeWavBase64(samples, sampleRate, 16000);
                Promise.resolve(onSegment({ audioBase64, durationMs: count * 1000 / sampleRate }))
                    .catch(error => onError(error));
            } catch (error) {
                onError(error);
            }
        }

        function setMuted(value) {
            muted = Boolean(value);
            if (stream) stream.getAudioTracks().forEach(track => { track.enabled = !muted; });
            candidate = [];
            candidateMs = 0;
            preRoll = [];
            preRollSamples = 0;
            if (muted) {
                recording = null;
                recordingSamples = 0;
            }
            onLevel(0);
        }

        async function stop() {
            lifecycle += 1;
            active = false;
            muted = true;
            generation += 1;
            if (frameHandle !== null && typeof win.cancelAnimationFrame === 'function') {
                win.cancelAnimationFrame(frameHandle);
                frameHandle = null;
            }
            players.forEach(player => player.stop());
            players.clear();
            if (processor) {
                processor.onaudioprocess = null;
                try { processor.disconnect(); } catch (_) {}
            }
            for (const node of [source, analyser, silentGain]) {
                try { node?.disconnect(); } catch (_) {}
            }
            if (stream) stopTracks(stream);
            stream = null;
            source = null;
            processor = null;
            analyser = null;
            silentGain = null;
            recording = null;
            candidate = [];
            preRoll = [];
            recordingSamples = 0;
            candidateMs = 0;
            preRollSamples = 0;
            onLevel(0);
            if (context) {
                try { await context.close(); } catch (_) {}
            }
            context = null;
        }

        function rootMeanSquare(values) {
            let sum = 0;
            for (let i = 0; i < values.length; i += 1) sum += values[i] * values[i];
            return Math.sqrt(sum / Math.max(1, values.length));
        }

        function flatten(chunks, length) {
            const output = new Float32Array(length);
            let offset = 0;
            for (const chunk of chunks) {
                const remaining = length - offset;
                if (remaining <= 0) break;
                const count = Math.min(remaining, chunk.length);
                output.set(chunk.subarray(0, count), offset);
                offset += count;
            }
            return offset === length ? output : output.slice(0, offset);
        }

        function encodeWavBase64(samples, inputRate, targetRate) {
            const outputLength = Math.max(1, Math.floor(samples.length * targetRate / inputRate));
            const bytes = new Uint8Array(44 + outputLength * 2);
            const view = new DataView(bytes.buffer);
            writeAscii(view, 0, 'RIFF');
            view.setUint32(4, bytes.length - 8, true);
            writeAscii(view, 8, 'WAVE');
            writeAscii(view, 12, 'fmt ');
            view.setUint32(16, 16, true);
            view.setUint16(20, 1, true);
            view.setUint16(22, 1, true);
            view.setUint32(24, targetRate, true);
            view.setUint32(28, targetRate * 2, true);
            view.setUint16(32, 2, true);
            view.setUint16(34, 16, true);
            writeAscii(view, 36, 'data');
            view.setUint32(40, outputLength * 2, true);
            for (let i = 0; i < outputLength; i += 1) {
                const position = i * inputRate / targetRate;
                const before = Math.floor(position);
                const after = Math.min(before + 1, samples.length - 1);
                const fraction = position - before;
                const sample = Math.max(-1, Math.min(1, samples[before] * (1 - fraction) + samples[after] * fraction));
                view.setInt16(44 + (i * 2), sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
            }
            let binary = '';
            const stride = 0x8000;
            for (let start = 0; start < bytes.length; start += stride) {
                binary += String.fromCharCode(...bytes.subarray(start, Math.min(start + stride, bytes.length)));
            }
            return win.btoa(binary);
        }

        function writeAscii(view, offset, value) {
            for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
        }

        function stopTracks(mediaStream) {
            mediaStream?.getTracks?.().forEach(track => track.stop());
        }

        return {
            start,
            unlock,
            stop,
            setMuted,
            createPlayer,
            isActive: () => active,
            isMuted: () => muted,
            __test: { handleAudioFrame, encodeWavBase64, rootMeanSquare },
        };
    }

    root.ChatAudio = { create };
})(typeof window !== 'undefined' ? window : globalThis);
