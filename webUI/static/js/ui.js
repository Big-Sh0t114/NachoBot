/**
 * NachoBot WebUI - UI Enhancements
 * Adds Interactive background, Startup Animation, and Background Music.
 */
const UI = (() => {
    let particleAnimationId = null;
    let playlist = [];
    let currentTrackIndex = 0;

    async function init() {
        // Inject DOM Elements
        injectElements();

        const startupCheckbox = document.getElementById('toggle-startup');
        const bgmCheckbox = document.getElementById('toggle-bgm');
        const interactiveCheckbox = document.getElementById('toggle-interactive');

        const startupScreen = document.getElementById('startup-screen');
        const startupVideo = document.getElementById('startup-video');
        const bgm = document.getElementById('bgm');
        const bgCanvas = document.getElementById('bg-canvas');

        const miniPlayer = document.getElementById('mini-player');
        const bgmPlayBtn = document.getElementById('bgm-play-btn');
        const bgmTitle = document.getElementById('bgm-title');
        const bgmListBtn = document.getElementById('bgm-list-btn');
        const bgmPlaylist = document.getElementById('bgm-playlist');
        const bgmDisc = document.getElementById('bgm-disc');
        const bgmVolumeSlider = document.getElementById('bgm-volume-slider');

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

        // 1. Startup Animation — show immediately, don't wait for music fetch
        if (settings.startup) {
            startupScreen.style.display = 'flex';
            requestAnimationFrame(() => { startupScreen.style.opacity = '1'; });
            if (startupVideo) {
                startupVideo.play().catch(e => console.log('Autoplay blocked:', e));
                startupVideo.addEventListener('ended', () => {
                    hideStartupScreen(startupScreen);
                });
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
            const res = await fetch('/api/music/list');
            if (res.ok) {
                playlist = await res.json();
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

                const playAudio = () => {
                    if (bgmCheckbox && bgmCheckbox.checked && bgm.paused) {
                        bgm.play().then(() => updatePlayBtn()).catch(err => console.log(err));
                    }
                    document.body.removeEventListener('click', playAudio, true);
                };
                document.body.addEventListener('click', playAudio, true);
            });
        }

        // Audio Events
        bgm.addEventListener('ended', () => {
            currentTrackIndex = (currentTrackIndex + 1) % playlist.length;
            loadTrack(currentTrackIndex);
            if (settings.bgm) bgm.play();
        });

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

        bgmPlayBtn.addEventListener('click', () => {
            if (bgm.paused) {
                bgm.play();
            } else {
                bgm.pause();
            }
        });

        bgmListBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            bgmPlaylist.style.display = bgmPlaylist.style.display === 'none' ? 'flex' : 'none';
        });

        document.addEventListener('click', (e) => {
            if (!bgmPlaylist.contains(e.target) && e.target !== bgmListBtn) {
                bgmPlaylist.style.display = 'none';
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
            if (!playlist[index]) return;
            currentTrackIndex = index;
            bgm.src = playlist[index].url;
            bgmTitle.innerText = playlist[index].name.replace(/\.[^/.]+$/, "");
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
                item.innerText = track.name.replace(/\.[^/.]+$/, "");

                item.addEventListener('mouseenter', () => {
                    if (idx !== currentTrackIndex) item.style.backgroundColor = 'rgba(0,0,0,0.02)';
                });
                item.addEventListener('mouseleave', () => {
                    if (idx !== currentTrackIndex) item.style.backgroundColor = 'transparent';
                });

                item.addEventListener('click', () => {
                    loadTrack(idx);
                    if (settings.bgm) {
                        bgm.play().catch(e => console.log(e));
                    }
                    bgmPlaylist.style.display = 'none';
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
                settings.bgm = e.target.checked;
                saveSettings(settings);
                if (settings.bgm) {
                    miniPlayer.style.display = 'flex';
                    bgm.play().catch(e => console.log('BGM Play prevented:', e));
                } else {
                    bgm.pause();
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

        // 2. Startup Screen with Video (Scale to 75%)
        const startup = document.createElement('div');
        startup.id = 'startup-screen';
        startup.style.position = 'fixed';
        startup.style.top = '0';
        startup.style.left = '0';
        startup.style.width = '100vw';
        startup.style.height = '100vh';
        startup.style.backgroundColor = 'var(--bg-base)'; // Use webpage background instead of black
        startup.style.zIndex = '9999';
        startup.style.display = 'none';
        startup.style.alignItems = 'center';
        startup.style.justifyContent = 'center';
        startup.style.transition = 'opacity 1s ease-in-out';
        startup.style.opacity = '0';

        // Use CSS mask to feather the edges of the video to transparent, perfectly eliminating any color difference bounds
        startup.innerHTML = `
            <video id="startup-video" src="/resources/NachoBotLogoAnime.mp4" muted playsinline style="width: 75%; height: auto; max-height: 75vh; object-fit: contain; pointer-events: none; transform: translateZ(0); will-change: transform; -webkit-mask-image: radial-gradient(ellipse at center, black 60%, transparent 95%); mask-image: radial-gradient(ellipse at center, black 60%, transparent 95%);"></video>
        `;
        document.body.appendChild(startup);

        // 3. Audio Element and Mini Player
        const audio = document.createElement('audio');
        audio.id = 'bgm';
        audio.volume = 0.2;
        document.body.appendChild(audio);

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
                <button id="bgm-list-btn" style="background: none; border: none; cursor: pointer; font-size: 1.2rem; color: #64748b; display: flex; align-items: center; transition: color 0.2s;">&#9776;</button>
            </div>
            <div id="bgm-playlist" style="display: none; position: absolute; top: calc(100% + 12px); left: 50%; transform: translateX(-50%); background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(12px); border: 1px solid rgba(0,0,0,0.05); border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); max-height: 280px; overflow-y: auto; width: 280px; flex-direction: column; overflow: hidden;">
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
        let width = canvas.width = window.innerWidth;
        let height = canvas.height = window.innerHeight;

        let particles = [];
        const mouse = { x: null, y: null };

        // We use an internal resize listener
        const onResize = () => {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        };
        window.addEventListener('resize', onResize);

        const onMouseMove = (e) => {
            mouse.x = e.clientX;
            mouse.y = e.clientY;
        };
        document.addEventListener('mousemove', onMouseMove);

        class Particle {
            constructor() {
                this.x = Math.random() * width;
                this.y = Math.random() * height;
                this.size = Math.random() * 3 + 1;
                this.speedX = Math.random() * 1.5 - 0.75;
                this.speedY = Math.random() * 1.5 - 0.75;
                this.baseColor = 'rgba(59, 130, 246, 0.4)'; // Increased opacity for better visibility
            }
            update() {
                this.x += this.speedX;
                this.y += this.speedY;

                if (this.x < 0) this.x = width;
                if (this.x > width) this.x = 0;
                if (this.y < 0) this.y = height;
                if (this.y > height) this.y = 0;

                // Mouse interaction
                if (mouse.x && mouse.y) {
                    let dx = mouse.x - this.x;
                    let dy = mouse.y - this.y;
                    let distance = Math.sqrt(dx * dx + dy * dy);
                    if (distance < 120) {
                        const forceDirectionX = dx / distance;
                        const forceDirectionY = dy / distance;
                        const force = (120 - distance) / 120;
                        this.x -= forceDirectionX * force * 3;
                        this.y -= forceDirectionY * force * 3;
                    }
                }
            }
            draw() {
                ctx.fillStyle = this.baseColor;
                ctx.beginPath();
                ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
                ctx.fill();
            }
        }

        for (let i = 0; i < 80; i++) {
            particles.push(new Particle());
        }

        function animate() {
            ctx.clearRect(0, 0, width, height);

            // Draw connections
            for (let i = 0; i < particles.length; i++) {
                particles[i].update();
                particles[i].draw();

                for (let j = i + 1; j < particles.length; j++) {
                    let dx = particles[i].x - particles[j].x;
                    let dy = particles[i].y - particles[j].y;
                    let dist = Math.sqrt(dx * dx + dy * dy);

                    if (dist < 100) {
                        ctx.beginPath();
                        ctx.strokeStyle = `rgba(59, 130, 246, ${0.3 * (1 - dist / 100)})`; // Scaled opacity for better visibility
                        ctx.lineWidth = 1.0; // Slightly thicker lines
                        ctx.moveTo(particles[i].x, particles[i].y);
                        ctx.lineTo(particles[j].x, particles[j].y);
                        ctx.stroke();
                    }
                }
            }
            particleAnimationId = requestAnimationFrame(animate);
        }
        animate();
    }

    function stopParticles() {
        if (particleAnimationId) {
            cancelAnimationFrame(particleAnimationId);
            particleAnimationId = null;
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
