/**
 * Interactive and audio-reactive background used by the WebUI.
 */
window.ParticleSystem = (() => {
    let particleAnimationId = null;
    let particleCleanup = null;
    let particleAudioSource = null;
    const NOMINAL_FRAME_RATE = 60;
    const MAX_FRAME_DELTA_SECONDS = 0.1;
    const PARTICLE_FADE_PER_SECOND = 0.9;
    const POPULATION_TRANSITIONS_PER_SECOND = 90;
    const MAX_POPULATION_CATCH_UP = 3;
    const SPRING_ACCELERATION_PER_SECOND_SQUARED = 0.155 * NOMINAL_FRAME_RATE * NOMINAL_FRAME_RATE;
    const SPRING_DAMPING_PER_SECOND = -Math.log(0.79) * NOMINAL_FRAME_RATE;
    const HEARTBEAT_DECAY_PER_SECOND = -Math.log(0.86) * NOMINAL_FRAME_RATE;

    function setAudioSource(source) {
        particleAudioSource = source;
    }

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
        let populationTransitionBudget = 0;
        let lastFrameTimestamp = null;
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
                this.pulseVelocityX += directionX * strength * NOMINAL_FRAME_RATE;
                this.pulseVelocityY += directionY * strength * NOMINAL_FRAME_RATE;
                this.heartbeatLevel = Math.min(1, this.heartbeatLevel + strength / 11);
            }

            update(reaction, speedMultiplier = 0.5, deltaSeconds = 1 / NOMINAL_FRAME_RATE) {
                const frameScale = deltaSeconds * NOMINAL_FRAME_RATE;
                if (this.isDying) {
                    this.lifeAlpha -= PARTICLE_FADE_PER_SECOND * deltaSeconds;
                } else {
                    this.lifeAlpha += PARTICLE_FADE_PER_SECOND * deltaSeconds;
                    if (this.lifeAlpha > 1) this.lifeAlpha = 1;
                }

                this.x += this.speedX * speedMultiplier * frameScale;
                this.y += this.speedY * speedMultiplier * frameScale;

                // Substep at or below 60Hz; this keeps the spring stable at low frame rates.
                const stepCount = Math.max(1, Math.ceil(frameScale));
                const stepSeconds = deltaSeconds / stepCount;
                const damping = Math.exp(-SPRING_DAMPING_PER_SECOND * stepSeconds);
                const heartbeatDecay = Math.exp(-HEARTBEAT_DECAY_PER_SECOND * stepSeconds);
                for (let step = 0; step < stepCount; step += 1) {
                    this.pulseVelocityX += -this.pulseX *
                        SPRING_ACCELERATION_PER_SECOND_SQUARED * stepSeconds;
                    this.pulseVelocityY += -this.pulseY *
                        SPRING_ACCELERATION_PER_SECOND_SQUARED * stepSeconds;
                    this.pulseVelocityX *= damping;
                    this.pulseVelocityY *= damping;
                    this.pulseX += this.pulseVelocityX * stepSeconds;
                    this.pulseY += this.pulseVelocityY * stepSeconds;
                    this.heartbeatLevel *= heartbeatDecay;
                }

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
                        const repel = force * 3 * frameScale;
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
                    reaction.high * 0.08 +
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

                const lightness = 60 + reaction.high * 8;
                ctx.fillStyle = `hsla(${hue}, 88%, ${lightness}%, ${alpha})`;
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

        function triggerClusterHeartbeat(reaction) {
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
                applyClusterKick(cluster, primaryStrength * reaction.pulse);
            }
        }

        function animate(now = performance.now()) {
            const timestamp = Number.isFinite(now) ? now : performance.now();
            const elapsed = lastFrameTimestamp === null
                ? 1 / NOMINAL_FRAME_RATE
                : Math.max(0, (timestamp - lastFrameTimestamp) / 1000);
            const deltaSeconds = Math.min(MAX_FRAME_DELTA_SECONDS, elapsed);
            lastFrameTimestamp = timestamp;

            const audioFrame = particleAudioSource?.getReactiveFrame?.() || {
                bass: 0,
                mid: 0,
                high: 0,
                intensity: 0,
                pulse: 0,
                beat: false,
            };
            const easterEggEffects = window.EasterEggSystem.getParticleEffects();
            const { rainbowActive, reactiveMultiplier } = easterEggEffects;
            const scaleStrength = value => Number.isFinite(value)
                ? Math.min(1, Math.max(0, value * reactiveMultiplier))
                : 0;
            const reactiveFrame = {
                ...audioFrame,
                bass: scaleStrength(audioFrame.bass),
                mid: scaleStrength(audioFrame.mid),
                high: scaleStrength(audioFrame.high),
                intensity: scaleStrength(audioFrame.intensity),
                pulse: scaleStrength(audioFrame.pulse),
                beat: Boolean(audioFrame.beat),
            };

            if (reactiveFrame.beat && reactiveFrame.pulse > 0) {
                triggerClusterHeartbeat(reactiveFrame);
            }

            const reaction = {
                ...reactiveFrame,
                particleLimit: easterEggEffects.particleLimit ?? NORMAL_PARTICLE_LIMIT,
                rainbowActive,
                rainbowPhase: (timestamp * 0.08) % 360,
            };

            // The analyser already provides a time-smoothed normalized intensity.
            const speedMultiplier = 0.45 + reaction.intensity + reaction.mid * 0.45;

            const particleLimit = Math.max(BASE_PARTICLE_COUNT, reaction.particleLimit);
            const targetParticleCount = Math.min(
                particleLimit,
                BASE_PARTICLE_COUNT + Math.floor(
                    reaction.intensity * (particleLimit - BASE_PARTICLE_COUNT) +
                    reaction.mid * 8
                )
            );

            let aliveCount = 0;
            for (const p of particles) {
                if (!p.isDying) aliveCount++;
            }

            const populationDifference = targetParticleCount - aliveCount;
            if (populationDifference !== 0) {
                populationTransitionBudget = Math.min(
                    MAX_POPULATION_CATCH_UP,
                    populationTransitionBudget + deltaSeconds * POPULATION_TRANSITIONS_PER_SECOND
                );
                const transitionCount = Math.min(
                    Math.abs(populationDifference),
                    Math.floor(populationTransitionBudget)
                );
                populationTransitionBudget -= transitionCount;

                if (populationDifference > 0) {
                    for (let index = 0; index < transitionCount; index += 1) {
                        particles.push(new Particle());
                    }
                } else {
                    let killed = 0;
                    const killLimit = Math.min(
                        transitionCount,
                        Math.max(0, aliveCount - BASE_PARTICLE_COUNT)
                    );
                    for (let i = particles.length - 1; i >= 0 && killed < killLimit; i -= 1) {
                        if (particles[i].isDying) continue;
                        particles[i].isDying = true;
                        killed += 1;
                    }
                    populationTransitionBudget -= transitionCount - killed;
                }
            } else {
                populationTransitionBudget = 0;
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
                reaction.bass * 16 +
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
                particle.update(reaction, speedMultiplier, deltaSeconds);
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

    return {
        setAudioSource,
        start: initParticles,
        stop: stopParticles,
    };
})();
