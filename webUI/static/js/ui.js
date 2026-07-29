/**
 * NachoBot WebUI - UI Enhancements
 * Adds Interactive background, Startup Animation, and Background Music.
 */
const UI = (() => {
    let particleAnimationId = null;
    let particleCleanup = null;
    let particleAudioSource = null;
    let playlist = [];
    let currentTrackIndex = 0;
    const OMEGA_TIP_TRACK = Object.freeze({
        name: 'Flower Man',
        kind: 'loop',
        loopUrl: '/static/js/hellisthat/Flower%20Man.mp3',
    });
    const OMEGA_VOICE_CLIPS = Object.freeze([
        '/static/js/hellisthat/Flowery_voiceclip_get_a_chance_1.wav',
        '/static/js/hellisthat/Flowery_voiceclip_go_home.wav',
        '/static/js/hellisthat/Flowery_voiceclip_hey_boys.wav',
        '/static/js/hellisthat/Flowery_voiceclip_leaf_it_to_me.wav',
        '/static/js/hellisthat/Flowery_voiceclip_Omega_Flowery.wav',
        '/static/js/hellisthat/Flowery_voiceclip_what_a_predictable_creature.wav',
    ]);

    async function init() {
        // Inject DOM Elements
        injectElements();

        const startupCheckbox = document.getElementById('toggle-startup');
        const bgmCheckbox = document.getElementById('toggle-bgm');
        const interactiveCheckbox = document.getElementById('toggle-interactive');

        const startupScreen = document.getElementById('startup-screen');
        const startupVideo = document.getElementById('startup-video');
        const bgm = new SeamlessBgmPlayer();
        particleAudioSource = bgm;
        const bgCanvas = document.getElementById('bg-canvas');

        const miniPlayer = document.getElementById('mini-player');
        const bgmPlayBtn = document.getElementById('bgm-play-btn');
        const bgmTitle = document.getElementById('bgm-title');
        const bgmListBtn = document.getElementById('bgm-list-btn');
        const bgmPlaylist = document.getElementById('bgm-playlist');
        const bgmDisc = document.getElementById('bgm-disc');
        const bgmVolumeSlider = document.getElementById('bgm-volume-slider');
        let omegaTipLocked = false;
        let autoplayRetryHandler = null;
        let omegaVoiceClip = null;
        let lastOmegaVoiceClipIndex = -1;

        // Load settings from localStorage
        const defaultSettings = { startup: true, bgm: false, interactive: true, volume: 0.2 };
        let settings = defaultSettings;
        try {
            const saved = localStorage.getItem('nacho_ui_settings');
            if (saved) settings = { ...defaultSettings, ...JSON.parse(saved) };
        } catch (e) { }

        if (startupCheckbox) startupCheckbox.checked = settings.startup;
        if (bgmCheckbox) bgmCheckbox.checked = settings.bgm;
        if (interactiveCheckbox) interactiveCheckbox.checked = settings.interactive;
        if (settings.bgm) armAutoplayPlayback();

        // 1. Startup Animation — video autoplays from inline HTML, just set up end handlers
        if (settings.startup) {
            if (startupVideo) {
                startupVideo.addEventListener('ended', () => hideStartupScreen(startupScreen));
                setTimeout(() => hideStartupScreen(startupScreen), 8000);
            } else {
                setTimeout(() => hideStartupScreen(startupScreen), 2000);
            }
        } else {
            startupScreen.style.display = 'none';
        }

        // 2. Interactive Background — defer if startup animation is playing
        if (settings.interactive) {
            if (!settings.startup) {
                initParticles();
                bgCanvas.style.display = 'block';
            }
        } else {
            bgCanvas.style.display = 'none';
            stopParticles();
        }

        // 3. Fetch Playlist and Setup Audio (non-blocking for animation)
        try {
            const res = await fetch('/api/music/list', { cache: 'no-store' });
            if (res.ok) {
                playlist = normalizeBgmPlaylist(await res.json());
            }
        } catch (e) {
            console.error('Failed to load playlist:', e);
        }

        if (playlist.length > 0) {
            miniPlayer.style.display = settings.bgm ? 'flex' : 'none';
            bgm.volume = settings.volume;
            if (bgmVolumeSlider) bgmVolumeSlider.value = settings.volume;
            loadTrack(0);
            renderPlaylist();
        } else {
            bgmTitle.innerText = "No Music Found";
        }

        // 4. Play BGM if enabled
        if (settings.bgm && playlist.length > 0) {
            bgm.play().then(() => {
                updatePlayBtn();
            }).catch(e => {
                console.log('Immediate BGM play blocked, waiting for interaction:', e);

                armAutoplayPlayback();
            });
        }


        function clearAutoplayPlaybackRetry() {
            if (!autoplayRetryHandler) return;
            document.removeEventListener('pointerdown', autoplayRetryHandler, true);
            document.removeEventListener('keydown', autoplayRetryHandler, true);
            autoplayRetryHandler = null;
        }

        function shouldResumeBgmPlayback() {
            return omegaTipLocked
                || Boolean(settings.bgm && playlist.length > 0 && bgmCheckbox?.checked);
        }

        function armAutoplayPlayback() {
            if (autoplayRetryHandler || !shouldResumeBgmPlayback()) return;

            autoplayRetryHandler = () => {
                bgm.unlock().then(() => {
                    if (shouldResumeBgmPlayback() && bgm.paused) {
                        return bgm.play({ userInitiated: true });
                    }
                    return undefined;
                }).then(() => {
                    updatePlayBtn();
                    clearAutoplayPlaybackRetry();
                }).catch(error => console.log('BGM is still waiting for interaction:', error));
            };

            document.addEventListener('pointerdown', autoplayRetryHandler, true);
            document.addEventListener('keydown', autoplayRetryHandler, true);
        }
        bgm.addEventListener('play', () => {
            updatePlayBtn();
            miniPlayer.classList.add('is-playing');
        });
        bgm.addEventListener('pause', () => {
            updatePlayBtn();
            miniPlayer.classList.remove('is-playing');
        });

        function updatePlayBtn() {
            bgmPlayBtn.innerHTML = bgm.paused ? '&#9654;' : '&#10074;&#10074;';
            if (bgmDisc) {
                bgmDisc.style.animationPlayState = bgm.paused ? 'paused' : 'running';
            }
        }

        const playlistHideDelay = 400;
        let playlistHideTimer = null;

        function clearPlaylistHideTimer() {
            if (playlistHideTimer) {
                clearTimeout(playlistHideTimer);
                playlistHideTimer = null;
            }
        }

        function showPlaylist() {
            if (omegaTipLocked) return;
            clearPlaylistHideTimer();
            bgmPlaylist.classList.add('is-visible');
            bgmListBtn.setAttribute('aria-expanded', 'true');
        }

        function hidePlaylist() {
            clearPlaylistHideTimer();
            bgmPlaylist.classList.remove('is-visible');
            bgmListBtn.setAttribute('aria-expanded', 'false');
        }

        function schedulePlaylistHide() {
            clearPlaylistHideTimer();
            playlistHideTimer = setTimeout(hidePlaylist, playlistHideDelay);
        }

        bgmPlayBtn.addEventListener('click', () => {
            if (omegaTipLocked) {
                playRandomOmegaVoiceClip();
                return;
            }

            if (bgm.paused) {
                bgm.play({ userInitiated: true }).catch(error => console.log('BGM Play prevented:', error));
            } else {
                bgm.pause();
            }
        });

        bgmListBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (omegaTipLocked) {
                playRandomOmegaVoiceClip();
                return;
            }

            if (bgmPlaylist.classList.contains('is-visible')) {
                hidePlaylist();
            } else {
                showPlaylist();
            }
        });

        document.addEventListener('click', (e) => {
            if (!miniPlayer.contains(e.target)) {
                hidePlaylist();
            }
        });

        miniPlayer.addEventListener('mouseenter', clearPlaylistHideTimer);
        miniPlayer.addEventListener('mouseleave', schedulePlaylistHide);
        bgmPlaylist.addEventListener('mouseenter', clearPlaylistHideTimer);
        bgmPlaylist.addEventListener('mouseleave', schedulePlaylistHide);
        miniPlayer.addEventListener('focusin', clearPlaylistHideTimer);
        miniPlayer.addEventListener('focusout', (e) => {
            if (!miniPlayer.contains(e.relatedTarget)) {
                schedulePlaylistHide();
            }
        });

        if (bgmVolumeSlider) {
            bgmVolumeSlider.addEventListener('input', (e) => {
                const vol = parseFloat(e.target.value);
                bgm.volume = vol;
                settings.volume = vol;
                saveSettings(settings);
            });
        }

        function loadTrack(index) {
            if (omegaTipLocked) return;

            const track = playlist[index];
            if (!track) return;

            currentTrackIndex = index;
            bgm.setTrack(track).catch(error => console.error('Failed to preload BGM:', error));
            bgmTitle.textContent = track.name;
            renderPlaylist();
        }


        function renderPlaylist() {
            bgmPlaylist.innerHTML = '';
            playlist.forEach((track, idx) => {
                const item = document.createElement('div');
                item.style.padding = '8px 12px';
                item.style.cursor = 'pointer';
                item.style.borderBottom = '1px solid var(--border-light)';
                item.style.fontSize = '0.85rem';
                item.style.color = idx === currentTrackIndex ? 'var(--accent)' : 'var(--text-primary)';
                item.style.backgroundColor = idx === currentTrackIndex ? 'var(--accent-bg)' : 'transparent';
                item.textContent = track.name;

                item.addEventListener('mouseenter', () => {
                    if (idx !== currentTrackIndex) item.style.backgroundColor = 'rgba(0,0,0,0.02)';
                });
                item.addEventListener('mouseleave', () => {
                    if (idx !== currentTrackIndex) item.style.backgroundColor = 'transparent';
                });

                item.addEventListener('click', () => {
                    if (omegaTipLocked) return;

                    loadTrack(idx);
                    if (settings.bgm) {
                        bgm.play({ userInitiated: true }).catch(error => console.log('BGM Play prevented:', error));
                    }
                    hidePlaylist();
                });
                bgmPlaylist.appendChild(item);
            });
        }

        // Bind Settings Changes
        if (startupCheckbox) {
            startupCheckbox.addEventListener('change', (e) => {
                settings.startup = e.target.checked;
                saveSettings(settings);
            });
        }

        if (bgmCheckbox) {
            bgmCheckbox.addEventListener('change', (e) => {
                if (omegaTipLocked) {
                    e.target.checked = true;
                    miniPlayer.style.display = 'flex';
                    requestOmegaPlayback();
                    return;
                }

                settings.bgm = e.target.checked;
                saveSettings(settings);
                if (settings.bgm) {
                    miniPlayer.style.display = 'flex';
                    bgm.play({ userInitiated: true }).catch(error => {
                        console.log('BGM Play prevented:', error);
                        armAutoplayPlayback();
                    });
                } else {
                    bgm.pause();
                    hidePlaylist();
                    miniPlayer.style.display = 'none';
                }
            });
        }

        if (interactiveCheckbox) {
            interactiveCheckbox.addEventListener('change', (e) => {
                settings.interactive = e.target.checked;
                saveSettings(settings);
                if (settings.interactive) {
                    initParticles();
                    bgCanvas.style.display = 'block';
                } else {
                    stopParticles();
                    bgCanvas.style.display = 'none';
                }
            });
        }

        function requestOmegaPlayback() {
            bgm.play().then(updatePlayBtn).catch(error => {
                console.log('BGM Play prevented:', error);
                armAutoplayPlayback();
            });
        }

        function playRandomOmegaVoiceClip() {
            if (!omegaTipLocked || OMEGA_VOICE_CLIPS.length === 0) return;
            if (omegaVoiceClip && !omegaVoiceClip.ended) return;

            let index = Math.floor(Math.random() * OMEGA_VOICE_CLIPS.length);
            if (OMEGA_VOICE_CLIPS.length > 1 && index === lastOmegaVoiceClipIndex) {
                index = (index + 1 + Math.floor(Math.random() * (OMEGA_VOICE_CLIPS.length - 1)))
                    % OMEGA_VOICE_CLIPS.length;
            }
            lastOmegaVoiceClipIndex = index;

            const clip = new Audio(OMEGA_VOICE_CLIPS[index]);
            omegaVoiceClip = clip;
            clip.volume = 1;

            const releaseClip = () => {
                if (omegaVoiceClip === clip) omegaVoiceClip = null;
            };
            clip.addEventListener('ended', releaseClip, { once: true });
            clip.addEventListener('error', releaseClip, { once: true });
            clip.play().catch(error => {
                releaseClip();
                console.log('Flowery voiceclip playback failed:', error);
            });
        }

        function activateOmegaTipMusic() {
            if (omegaTipLocked) return;
            omegaTipLocked = true;

            hidePlaylist();
            miniPlayer.style.display = 'flex';
            miniPlayer.classList.add('is-omega-locked');
            bgmTitle.textContent = OMEGA_TIP_TRACK.name;

            bgmPlayBtn.disabled = false;
            bgmPlayBtn.setAttribute('aria-disabled', 'true');
            bgmListBtn.disabled = false;
            bgmListBtn.setAttribute('aria-disabled', 'true');

            if (bgmCheckbox) {
                bgmCheckbox.checked = true;
                bgmCheckbox.disabled = true;
                bgmCheckbox.title = 'What A Predictable Creature!';
            }

            bgm.setTrack(OMEGA_TIP_TRACK)
                .then(() => requestOmegaPlayback())
                .catch(error => console.error('Failed to load Flower Man:', error));
        }

        window.addEventListener('nachobot:omega-tip', activateOmegaTipMusic);
        if (document.documentElement.dataset.omegaTipActive === 'true') {
            activateOmegaTipMusic();
        }
    }

    function hideStartupScreen(screen) {
        if (!screen || screen.style.display === 'none') return;
        screen.style.opacity = '0';
        setTimeout(() => {
            screen.style.display = 'none';
            const canvas = document.getElementById('bg-canvas');
            const cb = document.getElementById('toggle-interactive');
            if (cb && cb.checked) {
                initParticles();
                if (canvas) canvas.style.display = 'block';
            }
        }, 1000);
    }

    function injectElements() {
        // Dynamic Keyframes and Player Hover CSS
        const style = document.createElement('style');
        style.innerHTML = `
            @keyframes discSpin {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
            #mini-player {
                transition: all 0.5s cubic-bezier(0.4, 0, 0.2, 1);
            }
            #player-controls {
                display: flex;
                align-items: center;
                gap: 14px;
                margin-left: 14px;
                overflow: hidden;
                white-space: nowrap;
                transition: all 0.5s cubic-bezier(0.4, 0, 0.2, 1);
                opacity: 1;
                max-width: 400px;
            }
            #mini-player.is-playing:not(:hover) {
                padding: 6px;
                border-radius: 50px;
            }
            #mini-player.is-playing:not(:hover) #player-controls {
                opacity: 0;
                max-width: 0;
                margin-left: 0;
                gap: 0;
                pointer-events: none;
            }
            #bgm-playlist {
                opacity: 0;
                visibility: hidden;
                pointer-events: none;
                transform: translateX(-50%) translateY(-4px);
                transition: opacity 0.25s ease, transform 0.25s ease, visibility 0s linear 0.25s;
            }
            #bgm-playlist.is-visible {
                opacity: 1;
                visibility: visible;
                pointer-events: auto;
                transform: translateX(-50%) translateY(0);
                transition: opacity 0.25s ease, transform 0.25s ease, visibility 0s;
            }
        `;
        document.head.appendChild(style);

        // 1. Canvas for interactive background
        const canvas = document.createElement('canvas');
        canvas.id = 'bg-canvas';
        canvas.style.position = 'fixed';
        canvas.style.top = '0';
        canvas.style.left = '0';
        canvas.style.width = '100vw';
        canvas.style.height = '100vh';
        canvas.style.zIndex = '-1';
        canvas.style.pointerEvents = 'none';
        document.body.appendChild(canvas);

        // 2. Mini Player

        const playerUI = document.createElement('div');
        playerUI.id = 'mini-player';
        playerUI.style.position = 'fixed';
        playerUI.style.top = '24px';
        playerUI.style.left = '50%'; // Center horizontally
        playerUI.style.transform = 'translateX(-50%)'; // Center horizontally
        playerUI.style.zIndex = '1000';
        playerUI.style.display = 'none'; // Shown if bgm is enabled and tracks exist
        playerUI.style.alignItems = 'center';
        playerUI.style.background = 'rgba(255, 255, 255, 0.65)'; // Glassmorphism
        playerUI.style.backdropFilter = 'blur(16px)';
        playerUI.style.WebkitBackdropFilter = 'blur(16px)';
        playerUI.style.padding = '6px 16px 6px 8px';
        playerUI.style.borderRadius = '40px';
        playerUI.style.boxShadow = '0 8px 32px rgba(0, 0, 0, 0.08)';
        playerUI.style.border = '1px solid rgba(255, 255, 255, 0.5)';
        // gap is now handled by #player-controls margin-left

        playerUI.innerHTML = `
            <div id="bgm-disc" style="position: relative; flex-shrink: 0; width: 34px; height: 34px; border-radius: 50%; background: conic-gradient(#111 0deg, #333 45deg, #111 90deg, #333 135deg, #111 180deg, #333 225deg, #111 270deg, #333 315deg, #111 360deg); display: flex; align-items: center; justify-content: center; animation: discSpin 4s linear infinite; animation-play-state: paused; box-shadow: 0 4px 10px rgba(0,0,0,0.15);">
                <!-- 模拟唱片纹路 -->
                <div style="position: absolute; width: 26px; height: 26px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.08);"></div>
                <div style="position: absolute; width: 18px; height: 18px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.08);"></div>
                <!-- 唱片中心贴纸 -->
                <div style="width: 12px; height: 12px; border-radius: 50%; background: #ff4757; border: 1px solid #111; display: flex; align-items: center; justify-content: center; position: relative;">
                    <!-- 旋转视觉标记 -->
                    <div style="position: absolute; top: 1px; left: 2px; width: 3px; height: 3px; background: rgba(255,255,255,0.9); border-radius: 50%;"></div>
                    <!-- 中心孔 -->
                    <div style="width: 3px; height: 3px; background: #fff; border-radius: 50%; box-shadow: inset 0 1px 2px rgba(0,0,0,0.8);"></div>
                </div>
            </div>
            <div id="player-controls">
                <button id="bgm-play-btn" style="background: none; border: none; cursor: pointer; font-size: 1.1rem; color: var(--accent); display: flex; align-items: center; justify-content: center; width: 24px; height: 24px; transition: transform 0.2s;">&#9654;</button>
                <div id="bgm-title" style="flex: 1; min-width: 120px; max-width: 200px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 0.9rem; color: #334155; font-weight: 600; text-align: center; letter-spacing: 0.5px;">Loading...</div>
                <div style="display: flex; align-items: center; gap: 4px; margin-left: 4px;">
                    <span style="font-size: 1rem; color: #64748b; line-height: 1;">🔈</span>
                    <input type="range" id="bgm-volume-slider" min="0" max="1" step="0.01" value="0.2" style="width: 50px; cursor: pointer; accent-color: var(--accent);">
                </div>
                <button id="bgm-list-btn" aria-controls="bgm-playlist" aria-expanded="false" style="background: none; border: none; cursor: pointer; font-size: 1.2rem; color: #64748b; display: flex; align-items: center; transition: color 0.2s;">&#9776;</button>
            </div>
            <div id="bgm-playlist" style="display: flex; position: absolute; top: calc(100% + 12px); left: 50%; background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(12px); border: 1px solid rgba(0,0,0,0.05); border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); max-height: 280px; overflow-y: auto; width: 280px; flex-direction: column; overflow: hidden;">
            </div>
        `;
        document.body.appendChild(playerUI);
    }

    function saveSettings(settings) {
        localStorage.setItem('nacho_ui_settings', JSON.stringify(settings));
    }

    // --- Interactive Particle System ---
    function initParticles() {
        if (particleAnimationId) return;

        const canvas = document.getElementById('bg-canvas');
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        let width = 0;
        let height = 0;
        let particles = [];
        const BASE_PARTICLE_COUNT = 60;
        const NORMAL_PARTICLE_LIMIT = 120;
        const OMEGA_PARTICLE_LIMIT = 180;
        const OMEGA_REACTIVE_MULTIPLIER = 1.2;
        let visualPulse = 0;
        let visualIntensity = 0;
        let currentSpeedMultiplier = 0.5;
        let pendingHeartEchoes = [];
        const mouse = { x: null, y: null };
        const chatScrollContainer = document.getElementById('chat-messages');
        const mainScrollContainer = document.getElementById('main-content');
        let lastWindowScrollY = window.scrollY || 0;
        let lastChatScrollTop = chatScrollContainer?.scrollTop || 0;
        let lastMainScrollTop = mainScrollContainer?.scrollTop || 0;

        const resizeCanvas = () => {
            width = window.innerWidth;
            height = window.innerHeight;

            const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
            canvas.width = Math.max(1, Math.floor(width * pixelRatio));
            canvas.height = Math.max(1, Math.floor(height * pixelRatio));
            canvas.style.width = `${width}px`;
            canvas.style.height = `${height}px`;
            ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
        };

        const onMouseMove = (event) => {
            mouse.x = event.clientX;
            mouse.y = event.clientY;
        };

        const onMouseLeave = () => {
            mouse.x = null;
            mouse.y = null;
        };

        const wrapParticleY = particle => {
            if (height <= 0) return;
            particle.y = ((particle.y % height) + height) % height;
        };

        const shiftParticlesWithScroll = deltaY => {
            if (!Number.isFinite(deltaY) || Math.abs(deltaY) < 0.01) return;

            // 内容向上滚动时，粒子同步向上移动；反向滚动时同步向下。
            for (const particle of particles) {
                particle.y -= deltaY;
                wrapParticleY(particle);
            }
        };

        const onWindowScroll = () => {
            const nextScrollY = window.scrollY || 0;
            shiftParticlesWithScroll(nextScrollY - lastWindowScrollY);
            lastWindowScrollY = nextScrollY;
        };

        const onChatScroll = () => {
            if (!chatScrollContainer) return;
            const nextScrollTop = chatScrollContainer.scrollTop;
            shiftParticlesWithScroll(nextScrollTop - lastChatScrollTop);
            lastChatScrollTop = nextScrollTop;
        };

        const onMainScroll = () => {
            if (!mainScrollContainer) return;
            const nextScrollTop = mainScrollContainer.scrollTop;
            shiftParticlesWithScroll(nextScrollTop - lastMainScrollTop);
            lastMainScrollTop = nextScrollTop;
        };

        window.addEventListener('resize', resizeCanvas);
        window.addEventListener('scroll', onWindowScroll, { passive: true });
        chatScrollContainer?.addEventListener('scroll', onChatScroll, { passive: true });
        mainScrollContainer?.addEventListener('scroll', onMainScroll, { passive: true });
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseleave', onMouseLeave);
        resizeCanvas();

        class Particle {
            constructor() {
                this.x = Math.random() * width;
                this.y = Math.random() * height;
                this.baseSize = Math.random() * 3 + 1;
                this.speedX = Math.random() * 1.5 - 0.75;
                this.speedY = Math.random() * 1.5 - 0.75;

                // 节点群搏动使用位移弹簧，不使用单节点缩放。
                this.pulseX = 0;
                this.pulseY = 0;
                this.pulseVelocityX = 0;
                this.pulseVelocityY = 0;
                this.heartbeatLevel = 0;
                this.lifeAlpha = 0;
                this.isDying = false;
            }

            get drawX() {
                return this.x + this.pulseX;
            }

            get drawY() {
                return this.y + this.pulseY;
            }

            applyHeartbeatKick(directionX, directionY, strength) {
                this.pulseVelocityX += directionX * strength;
                this.pulseVelocityY += directionY * strength;
                this.heartbeatLevel = Math.min(1, this.heartbeatLevel + strength / 11);
            }

            update(reaction, speedMultiplier = 0.5) {
                if (this.isDying) {
                    this.lifeAlpha -= 0.015;
                } else {
                    this.lifeAlpha += 0.015;
                    if (this.lifeAlpha > 1) this.lifeAlpha = 1;
                }

                this.x += this.speedX * speedMultiplier;
                this.y += this.speedY * speedMultiplier;

                // 位移弹簧令整组节点向外张开后同步回到原来的结构。
                const springStrength = 0.155;
                const damping = 0.79;

                this.pulseVelocityX += -this.pulseX * springStrength;
                this.pulseVelocityY += -this.pulseY * springStrength;
                this.pulseVelocityX *= damping;
                this.pulseVelocityY *= damping;
                this.pulseX += this.pulseVelocityX;
                this.pulseY += this.pulseVelocityY;
                this.heartbeatLevel *= 0.86;

                if (
                    Math.abs(this.pulseX) < 0.01 &&
                    Math.abs(this.pulseY) < 0.01 &&
                    Math.abs(this.pulseVelocityX) < 0.01 &&
                    Math.abs(this.pulseVelocityY) < 0.01
                ) {
                    this.pulseX = 0;
                    this.pulseY = 0;
                    this.pulseVelocityX = 0;
                    this.pulseVelocityY = 0;
                }

                if (this.x < 0) this.x = width;
                if (this.x > width) this.x = 0;
                if (this.y < 0) this.y = height;
                if (this.y > height) this.y = 0;

                // 鼠标排斥作用于节点的基础位置，不干扰音乐搏动弹簧。
                if (mouse.x !== null && mouse.y !== null) {
                    const dx = mouse.x - this.drawX;
                    const dy = mouse.y - this.drawY;
                    const distance = Math.hypot(dx, dy);

                    if (distance > 0 && distance < 120) {
                        const force = (120 - distance) / 120;
                        const repel = force * 3;
                        this.x -= (dx / distance) * repel;
                        this.y -= (dy / distance) * repel;
                    }
                }
            }

            draw(reaction) {
                // 节点尺寸基本不变；视觉重点是节点群整体向外扩张。
                const size = this.baseSize * (1 + reaction.intensity * 0.06);
                const alpha = Math.min(
                    0.78,
                    0.38 +
                    reaction.intensity * 0.08 +
                    this.heartbeatLevel * 0.16
                ) * Math.max(0, this.lifeAlpha);
                const defaultHue =
                    214 -
                    reaction.bass * 12 +
                    reaction.high * 18;
                const rainbowHue = (
                    (this.drawX / Math.max(width, 1)) * 300 +
                    reaction.rainbowPhase
                ) % 360;
                const hue = reaction.rainbowActive
                    ? rainbowHue
                    : defaultHue;

                ctx.fillStyle = `hsla(${hue}, 88%, 60%, ${alpha})`;
                ctx.beginPath();
                ctx.arc(this.drawX, this.drawY, size, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        for (let index = 0; index < BASE_PARTICLE_COUNT; index += 1) {
            particles.push(new Particle());
        }

        function buildHeartbeatCluster(reaction, excludedParticles) {
            const availableSeeds = particles.filter(
                particle => !excludedParticles.has(particle)
            );
            if (availableSeeds.length === 0) return null;

            const seed =
                availableSeeds[
                Math.floor(Math.random() * availableSeeds.length)
                ];
            const desiredNodeCount = 8 + Math.floor(reaction.bass * 8);
            const heartbeatLinkDistance =
                130 + reaction.intensity * 15 + reaction.pulse * 25;

            // 沿当前邻近连线图进行广度遍历，而不是单纯抓取最近节点。
            // 因此参与一次搏动的所有节点都能通过至少一条实际连线相互连接。
            const selected = [];
            const queue = [seed];
            const visited = new Set([seed]);

            while (queue.length > 0 && selected.length < desiredNodeCount) {
                const current = queue.shift();
                if (excludedParticles.has(current)) continue;
                selected.push(current);

                const linkedNeighbours = particles
                    .filter(particle => {
                        if (
                            particle === current ||
                            visited.has(particle) ||
                            excludedParticles.has(particle)
                        ) {
                            return false;
                        }

                        return Math.hypot(
                            particle.drawX - current.drawX,
                            particle.drawY - current.drawY
                        ) < heartbeatLinkDistance;
                    })
                    .sort((left, right) => {
                        const leftDistance = Math.hypot(
                            left.drawX - current.drawX,
                            left.drawY - current.drawY
                        );
                        const rightDistance = Math.hypot(
                            right.drawX - current.drawX,
                            right.drawY - current.drawY
                        );
                        return leftDistance - rightDistance;
                    });

                for (const neighbour of linkedNeighbours) {
                    visited.add(neighbour);
                    queue.push(neighbour);
                }
            }

            if (selected.length < 3) return null;

            const centerX =
                selected.reduce((total, particle) => total + particle.drawX, 0) /
                selected.length;
            const centerY =
                selected.reduce((total, particle) => total + particle.drawY, 0) /
                selected.length;

            const members = selected.map((particle, index) => {
                let dx = particle.drawX - centerX;
                let dy = particle.drawY - centerY;
                let distance = Math.hypot(dx, dy);

                // 位于节点群圆心附近的节点也分配稳定的径向方向。
                if (distance < 1) {
                    const angle = (index / selected.length) * Math.PI * 2;
                    dx = Math.cos(angle);
                    dy = Math.sin(angle);
                    distance = 1;
                }

                excludedParticles.add(particle);
                return {
                    particle,
                    directionX: dx / distance,
                    directionY: dy / distance,
                };
            });

            return { members };
        }

        function applyClusterKick(cluster, strength) {
            for (const member of cluster.members) {
                member.particle.applyHeartbeatKick(
                    member.directionX,
                    member.directionY,
                    strength
                );
            }
        }

        function triggerClusterHeartbeat(reaction, now) {
            const excludedParticles = new Set();

            // 普通鼓点触发一个局部节点群；强低频时同时触发第二个节点群。
            const clusterCount = reaction.bass > 0.45 ? 2 : 1;
            const clusters = [];

            for (let index = 0; index < clusterCount; index += 1) {
                const cluster = buildHeartbeatCluster(
                    reaction,
                    excludedParticles
                );
                if (cluster) clusters.push(cluster);
            }

            const primaryStrength =
                7.5 +
                reaction.bass * 8.5 +
                reaction.intensity * 3.0;

            for (const cluster of clusters) {
                applyClusterKick(cluster, primaryStrength);
            }

            // 约 120ms 后补一个较弱的第二次搏动，形成“咚—咚”的心跳感。
            if (clusters.length > 0) {
                pendingHeartEchoes.push({
                    dueAt: now + 120,
                    clusters,
                    strength: primaryStrength * 0.43,
                });

                // 防止极密集音乐导致待处理回声无限堆积。
                if (pendingHeartEchoes.length > 6) {
                    pendingHeartEchoes = pendingHeartEchoes.slice(-6);
                }
            }
        }

        function processHeartbeatEchoes(now) {
            if (pendingHeartEchoes.length === 0) return;

            const remaining = [];
            for (const echo of pendingHeartEchoes) {
                if (now >= echo.dueAt) {
                    for (const cluster of echo.clusters) {
                        applyClusterKick(cluster, echo.strength);
                    }
                } else {
                    remaining.push(echo);
                }
            }
            pendingHeartEchoes = remaining;
        }

        function animate(now = performance.now()) {
            const audioFrame = particleAudioSource?.getReactiveFrame?.() || {
                bass: 0,
                mid: 0,
                high: 0,
                intensity: 0,
                pulse: 0,
                beat: false,
            };
            const rainbowActive =
                document.documentElement.dataset.omegaTipActive === 'true';
            const reactiveMultiplier = rainbowActive
                ? OMEGA_REACTIVE_MULTIPLIER
                : 1;
            const reactiveFrame = {
                ...audioFrame,
                bass: Math.min(1, audioFrame.bass * reactiveMultiplier),
                mid: Math.min(1, audioFrame.mid * reactiveMultiplier),
                high: Math.min(1, audioFrame.high * reactiveMultiplier),
                intensity: Math.min(
                    1,
                    audioFrame.intensity * reactiveMultiplier
                ),
                pulse: Math.min(1, audioFrame.pulse * reactiveMultiplier),
            };

            if (reactiveFrame.beat) {
                visualPulse = 1;
                triggerClusterHeartbeat(reactiveFrame, now);
            } else {
                const pulseRate =
                    reactiveFrame.pulse > visualPulse ? 0.55 : 0.12;
                visualPulse +=
                    (reactiveFrame.pulse - visualPulse) * pulseRate;
            }

            processHeartbeatEchoes(now);

            visualIntensity +=
                (reactiveFrame.intensity - visualIntensity) * 0.12;

            const reaction = {
                ...reactiveFrame,
                pulse: visualPulse,
                intensity: visualIntensity,
                rainbowActive,
                rainbowPhase: (now * 0.08) % 360,
            };

            // 线性映射：将 0.5~1.0 之间的响度直接线性放大到 0~1.0，比二次方更敏感
            const activeIntensity = Math.min(1, Math.max(0, reaction.intensity - 0.6) * 2.5);
            const intensityCurve = activeIntensity; // 取消二次方，使用线性响应

            const targetSpeedMultiplier = 0.5 + intensityCurve * 1.0;
            currentSpeedMultiplier += (targetSpeedMultiplier - currentSpeedMultiplier) * 0.02;

            const particleLimit = reaction.rainbowActive
                ? OMEGA_PARTICLE_LIMIT
                : NORMAL_PARTICLE_LIMIT;
            const targetParticleCount =
                BASE_PARTICLE_COUNT +
                Math.floor(
                    intensityCurve *
                    (particleLimit - BASE_PARTICLE_COUNT)
                );

            let aliveCount = 0;
            for (const p of particles) {
                if (!p.isDying) aliveCount++;
            }

            if (aliveCount < targetParticleCount) {
                particles.push(new Particle());
                if (aliveCount + 1 < targetParticleCount) particles.push(new Particle());
            } else if (
                aliveCount > targetParticleCount &&
                aliveCount > BASE_PARTICLE_COUNT
            ) {
                let killed = 0;
                for (let i = particles.length - 1; i >= 0; i--) {
                    if (!particles[i].isDying) {
                        particles[i].isDying = true;
                        killed++;
                        if (killed >= 2) break;
                    }
                }
            }

            for (let i = particles.length - 1; i >= 0; i--) {
                if (particles[i].isDying && particles[i].lifeAlpha <= 0) {
                    particles.splice(i, 1);
                }
            }

            ctx.clearRect(0, 0, width, height);

            // 连线随整体鼓点稍微增强；真正的张开由节点群位移产生。
            const connectionDistance =
                100 +
                reaction.intensity * 8 +
                reaction.pulse * 14;
            const lineWidth =
                0.8 +
                reaction.intensity * 0.12 +
                reaction.pulse * 0.38;
            const defaultLineHue =
                214 -
                reaction.bass * 12 +
                reaction.high * 18;

            for (let i = 0; i < particles.length; i += 1) {
                const particle = particles[i];
                particle.update(reaction, currentSpeedMultiplier);
                particle.draw(reaction);

                for (let j = i + 1; j < particles.length; j += 1) {
                    const other = particles[j];
                    const dx = particle.drawX - other.drawX;
                    const dy = particle.drawY - other.drawY;
                    const distance = Math.hypot(dx, dy);

                    if (distance < connectionDistance) {
                        const distanceFade =
                            1 - distance / connectionDistance;
                        const heartbeatAlpha =
                            Math.max(
                                particle.heartbeatLevel,
                                other.heartbeatLevel
                            ) * 0.22;
                        const alpha =
                            (
                                0.22 +
                                reaction.intensity * 0.06 +
                                reaction.pulse * 0.12 +
                                heartbeatAlpha
                            ) * distanceFade * Math.max(0, particle.lifeAlpha) * Math.max(0, other.lifeAlpha);

                        const rainbowLineHue = (
                            ((particle.drawX + other.drawX) /
                                (2 * Math.max(width, 1))) * 300 +
                            reaction.rainbowPhase
                        ) % 360;
                        const lineHue = reaction.rainbowActive
                            ? rainbowLineHue
                            : defaultLineHue;

                        ctx.beginPath();
                        ctx.strokeStyle =
                            `hsla(${lineHue}, 88%, 58%, ${alpha})`;
                        ctx.lineWidth =
                            lineWidth +
                            Math.max(
                                particle.heartbeatLevel,
                                other.heartbeatLevel
                            ) * 0.45;
                        ctx.moveTo(particle.drawX, particle.drawY);
                        ctx.lineTo(other.drawX, other.drawY);
                        ctx.stroke();
                    }
                }
            }

            particleAnimationId = requestAnimationFrame(animate);
        }

        particleCleanup = () => {
            window.removeEventListener('resize', resizeCanvas);
            window.removeEventListener('scroll', onWindowScroll);
            chatScrollContainer?.removeEventListener('scroll', onChatScroll);
            mainScrollContainer?.removeEventListener('scroll', onMainScroll);
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseleave', onMouseLeave);
            pendingHeartEchoes = [];
        };

        animate();
    }

    function stopParticles() {
        if (particleAnimationId) {
            cancelAnimationFrame(particleAnimationId);
            particleAnimationId = null;
        }

        if (particleCleanup) {
            particleCleanup();
            particleCleanup = null;
        }

        const canvas = document.getElementById('bg-canvas');
        if (canvas) {
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
    }

    return { init };
})();

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', UI.init);
} else {
    UI.init();
}
