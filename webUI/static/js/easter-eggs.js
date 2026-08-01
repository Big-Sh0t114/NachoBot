/**
 * NachoBot WebUI — Easter Egg System
 *
 * Owns welcome-tip easter eggs and the OMEGA mode shared by chat, BGM,
 * and the interactive particle background.
 */
window.EasterEggSystem = (() => {
    'use strict';

    const OMEGA_TIP_EVENT = 'nachobot:omega-tip';
    const WELCOME_EASTER_EGGS = Object.freeze([
        { type: 'editable', text: 'Tip：你可以修改这条Tip', chance: 0.01 },
        { type: 'evasive', text: '你跑不过我你信吗', chance: 0.01 },
        { type: 'invisible', text: '你知道的太多了', chance: 0.01 },
        { type: 'omega', text: 'OMEGAAAA TIPPSSSS!!!', chance: 0.005 },
    ]);
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
    const DEFAULT_PARTICLE_EFFECTS = Object.freeze({
        particleLimit: null,
        rainbowActive: false,
        reactiveMultiplier: 1,
    });
    const OMEGA_PARTICLE_EFFECTS = Object.freeze({
        particleLimit: 180,
        rainbowActive: true,
        reactiveMultiplier: 1.2,
    });

    function setRandomWelcomeSubtitle(subtitle, welcomeSubtitles) {
        if (!subtitle || !Array.isArray(welcomeSubtitles) || welcomeSubtitles.length === 0) return;

        resetWelcomeSubtitle(subtitle);
        const easterEgg = pickWelcomeEasterEgg();
        if (!easterEgg) {
            const index = Math.floor(Math.random() * welcomeSubtitles.length);
            subtitle.textContent = welcomeSubtitles[index];
            return;
        }

        subtitle.textContent = easterEgg.text;
        applyWelcomeEasterEgg(subtitle, easterEgg.type);
    }

    function pickWelcomeEasterEgg() {
        const roll = Math.random();
        let threshold = 0;
        return WELCOME_EASTER_EGGS.find(item => {
            threshold += item.chance;
            return roll < threshold;
        });
    }

    function applyWelcomeEasterEgg(subtitle, type) {
        if (type === 'editable') {
            subtitle.classList.add('is-editable-tip');
            subtitle.contentEditable = 'true';
            subtitle.spellcheck = false;
            subtitle.setAttribute('role', 'textbox');
            subtitle.setAttribute('aria-label', '可编辑 Tip');
            subtitle.onkeydown = event => {
                if (event.key === 'Enter') {
                    event.preventDefault();
                    subtitle.blur();
                }
            };
            return;
        }

        if (type === 'invisible') {
            subtitle.classList.add('is-invisible-tip');
            subtitle.style.setProperty('color', 'transparent');
            subtitle.style.setProperty('text-shadow', 'none');
            subtitle.style.setProperty('opacity', '1');
            subtitle.style.setProperty('visibility', 'visible');
            subtitle.style.setProperty('user-select', 'text');
            subtitle.style.setProperty('cursor', 'text');
            subtitle.setAttribute('aria-label', 'invisible');
            return;
        }

        if (type === 'omega') {
            subtitle.classList.add('is-gradient-tip');
            activateOmegaTip();
            return;
        }

        if (type === 'evasive') {
            subtitle.classList.add('is-evasive-tip');
            subtitle.title = '关注哔哩哔哩Big_Sh0t谢谢喵';
            subtitle.onpointerenter = () => moveEvasiveSubtitle(subtitle);
        }
    }

    function resetWelcomeSubtitle(subtitle) {
        delete document.documentElement.dataset.omegaTipActive;
        subtitle.classList.remove(
            'is-editable-tip',
            'is-gradient-tip',
            'is-evasive-tip',
            'is-invisible-tip'
        );
        subtitle.contentEditable = 'false';
        subtitle.removeAttribute('role');
        subtitle.removeAttribute('aria-label');
        subtitle.removeAttribute('title');
        subtitle.style.removeProperty('--tip-shift-x');
        subtitle.style.removeProperty('--tip-shift-y');
        subtitle.style.removeProperty('color');
        subtitle.style.removeProperty('text-shadow');
        subtitle.style.removeProperty('opacity');
        subtitle.style.removeProperty('visibility');
        subtitle.style.removeProperty('user-select');
        subtitle.style.removeProperty('cursor');
        subtitle.onkeydown = null;
        subtitle.onpointerenter = null;
    }

    function moveEvasiveSubtitle(subtitle) {
        const container = subtitle.parentElement;
        const horizontalRange = Math.max(90, Math.min(260, (container?.clientWidth || 600) * 0.28));
        const verticalRange = 72;
        const x = (Math.random() * 2 - 1) * horizontalRange;
        const y = (Math.random() * 2 - 1) * verticalRange;
        subtitle.style.setProperty('--tip-shift-x', `${x.toFixed(0)}px`);
        subtitle.style.setProperty('--tip-shift-y', `${y.toFixed(0)}px`);
    }

    function activateOmegaTip() {
        document.documentElement.dataset.omegaTipActive = 'true';
        window.dispatchEvent(new CustomEvent(OMEGA_TIP_EVENT));
    }

    function isOmegaTipActive() {
        return document.documentElement.dataset.omegaTipActive === 'true';
    }

    function getParticleEffects() {
        return isOmegaTipActive() ? OMEGA_PARTICLE_EFFECTS : DEFAULT_PARTICLE_EFFECTS;
    }

    function createOmegaPlayerController({
        bgm,
        bgmCheckbox,
        bgmListBtn,
        bgmPlayBtn,
        bgmTitle,
        hidePlaylist,
        miniPlayer,
        requestPlayback,
    }) {
        let bound = false;
        let locked = false;
        let voiceClip = null;
        let lastVoiceClipIndex = -1;

        function bind() {
            if (bound) return;
            bound = true;
            window.addEventListener(OMEGA_TIP_EVENT, activate);
            if (isOmegaTipActive()) activate();
        }

        function activate() {
            if (locked) return;
            locked = true;

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
                .then(() => requestPlayback())
                .catch(error => console.error('Failed to load Flower Man:', error));
        }

        function handlePlayerControl() {
            if (!locked) return false;
            playRandomVoiceClip();
            return true;
        }

        function handleBgmToggle(event) {
            if (!locked) return false;
            event.target.checked = true;
            miniPlayer.style.display = 'flex';
            requestPlayback();
            return true;
        }

        function playRandomVoiceClip() {
            if (!locked || OMEGA_VOICE_CLIPS.length === 0) return;
            if (voiceClip && !voiceClip.ended) return;

            let index = Math.floor(Math.random() * OMEGA_VOICE_CLIPS.length);
            if (OMEGA_VOICE_CLIPS.length > 1 && index === lastVoiceClipIndex) {
                index = (index + 1 + Math.floor(Math.random() * (OMEGA_VOICE_CLIPS.length - 1)))
                    % OMEGA_VOICE_CLIPS.length;
            }
            lastVoiceClipIndex = index;

            const clip = new Audio(OMEGA_VOICE_CLIPS[index]);
            voiceClip = clip;
            clip.volume = 1;

            const releaseClip = () => {
                if (voiceClip === clip) voiceClip = null;
            };
            clip.addEventListener('ended', releaseClip, { once: true });
            clip.addEventListener('error', releaseClip, { once: true });
            clip.play().catch(error => {
                releaseClip();
                console.log('Flowery voiceclip playback failed:', error);
            });
        }

        function isReservedTrackUrl(url) {
            return url === OMEGA_TIP_TRACK.loopUrl;
        }

        return Object.freeze({
            bind,
            handleBgmToggle,
            handlePlayerControl,
            isActive: () => locked,
            isReservedTrackUrl,
        });
    }

    return Object.freeze({
        createOmegaPlayerController,
        getParticleEffects,
        isOmegaTipActive,
        setRandomWelcomeSubtitle,
    });
})();
