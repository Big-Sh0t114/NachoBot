/**
 * NachoBot WebUI - UI Enhancements
 * Adds Interactive background, Startup Animation, and Background Music.
 */
const UI = (() => {
    let playlist = [];
    let currentTrackIndex = 0;
    const LAST_BGM_TRACK_STORAGE_KEY = 'nacho_last_bgm_track';

    async function init() {
        // Inject DOM Elements
        injectElements();

        const startupCheckbox = document.getElementById('toggle-startup');
        const bgmCheckbox = document.getElementById('toggle-bgm');
        const interactiveCheckbox = document.getElementById('toggle-interactive');

        const startupScreen = document.getElementById('startup-screen');
        const startupVideo = document.getElementById('startup-video');
        const bgm = new SeamlessBgmPlayer();
        window.ParticleSystem.setAudioSource(bgm);
        const bgCanvas = document.getElementById('bg-canvas');

        const miniPlayer = document.getElementById('mini-player');
        const bgmPlayBtn = document.getElementById('bgm-play-btn');
        const bgmTitle = document.getElementById('bgm-title');
        const bgmListBtn = document.getElementById('bgm-list-btn');
        const bgmPlaylist = document.getElementById('bgm-playlist');
        const bgmDisc = document.getElementById('bgm-disc');
        const bgmVolumeSlider = document.getElementById('bgm-volume-slider');
        let autoplayRetryHandler = null;
        const easterEggs = window.EasterEggSystem.createOmegaPlayerController({
            bgm,
            bgmCheckbox,
            bgmListBtn,
            bgmPlayBtn,
            bgmTitle,
            hidePlaylist,
            miniPlayer,
            requestPlayback: () => {
                bgm.play().then(updatePlayBtn).catch(error => {
                    console.log('BGM Play prevented:', error);
                    armAutoplayPlayback();
                });
            },
        });

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

        // 1. Startup Animation — fail gracefully if loading, decoding, or autoplay fails
        if (settings.startup) {
            if (startupVideo) {
                let startupFinished = false;

                const finishStartup = () => {
                    if (startupFinished) return;
                    startupFinished = true;
                    hideStartupScreen(startupScreen);
                };

                startupVideo.addEventListener('ended', finishStartup, { once: true });
                startupVideo.addEventListener('error', () => {
                    console.error('Startup video failed:', startupVideo.error);
                    finishStartup();
                }, { once: true });

                startupVideo.play().catch(error => {
                    console.error('Startup video autoplay failed:', error);
                    finishStartup();
                });

                setTimeout(finishStartup, 8000);
            } else {
                setTimeout(() => hideStartupScreen(startupScreen), 2000);
            }
        } else {
            startupScreen.style.display = 'none';
        }

        // 2. Interactive Background — defer if startup animation is playing
        if (settings.interactive) {
            if (!settings.startup) {
                window.ParticleSystem.start();
                bgCanvas.style.display = 'block';
            }
        } else {
            bgCanvas.style.display = 'none';
            window.ParticleSystem.stop();
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
            loadTrack(getSavedTrackIndex());
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
            return easterEggs.isActive()
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
            if (easterEggs.isActive()) return;
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
            if (easterEggs.handlePlayerControl()) return;

            if (bgm.paused) {
                bgm.play({ userInitiated: true }).catch(error => console.log('BGM Play prevented:', error));
            } else {
                bgm.pause();
            }
        });

        bgmListBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (easterEggs.handlePlayerControl()) return;

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

        function getSavedTrackIndex() {
            let savedLoopUrl;
            try {
                savedLoopUrl = localStorage.getItem(LAST_BGM_TRACK_STORAGE_KEY);
            } catch (e) {
                return 0;
            }

            // Flower Man is an OMEGA-tip-only hidden track and must never be restored.
            if (!savedLoopUrl || easterEggs.isReservedTrackUrl(savedLoopUrl)) {
                if (easterEggs.isReservedTrackUrl(savedLoopUrl)) {
                    try {
                        localStorage.removeItem(LAST_BGM_TRACK_STORAGE_KEY);
                    } catch (e) { }
                }
                return 0;
            }

            const savedIndex = playlist.findIndex(track => track.loopUrl === savedLoopUrl);
            return savedIndex >= 0 ? savedIndex : 0;
        }

        function saveLastTrack(track) {
            if (!track?.loopUrl || easterEggs.isReservedTrackUrl(track.loopUrl)) return;

            try {
                localStorage.setItem(LAST_BGM_TRACK_STORAGE_KEY, track.loopUrl);
            } catch (e) { }
        }

        function loadTrack(index) {
            if (easterEggs.isActive()) return;

            const track = playlist[index];
            if (!track) return;

            currentTrackIndex = index;
            saveLastTrack(track);
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
                    if (easterEggs.isActive()) return;

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
                if (easterEggs.handleBgmToggle(e)) return;

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
                    window.ParticleSystem.start();
                    bgCanvas.style.display = 'block';
                } else {
                    window.ParticleSystem.stop();
                    bgCanvas.style.display = 'none';
                }
            });
        }

        easterEggs.bind();
    }

    function hideStartupScreen(screen) {
        if (!screen || screen.style.display === 'none') return;
        screen.style.opacity = '0';
        setTimeout(() => {
            screen.style.display = 'none';
            const canvas = document.getElementById('bg-canvas');
            const cb = document.getElementById('toggle-interactive');
            if (cb && cb.checked) {
                window.ParticleSystem.start();
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

    return { init };
})();

// Initialize when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', UI.init);
} else {
    UI.init();
}
