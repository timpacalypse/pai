const shell = document.getElementById('shell');
const titleEl = document.getElementById('state-title');
const copyEl = document.getElementById('state-copy');
const inputEl = document.getElementById('wake-input');
const formEl = document.getElementById('command-form');
const shockwaveEl = document.getElementById('shockwave');
const starfieldEl = document.getElementById('starfield');
const svg = document.getElementById('network-svg');
const lineLayer = document.getElementById('line-layer');
const pulseLayer = document.getElementById('pulse-layer');
const nodeLayer = document.getElementById('node-layer');
const orbLabel = document.getElementById('orb-state-label');
const wakeBtn = document.getElementById('wake-btn');

const ORCHESTRATOR_HTTP = `${location.protocol}//${location.hostname}:8000`;
const ORCHESTRATOR_WS = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:8000`;

const stateContent = {
    sleeping: {
        title: 'SLEEPING',
        copy: 'Dormant but alive. Synaptic pathways drift quietly until AEGIS is called.',
    },
    awake: {
        title: 'AWAKE',
        copy: 'Alert and energized. Network visibility rises, pulses accelerate, and the core shifts warmer.',
    },
};

const width = 1600;
const height = 980;
const center = { x: width / 2, y: height / 2 };
const orbRadius = 180;
const nodeCount = 90;
const nodePaletteSleep = ['#00d4ff', '#39dcff', '#75e7ff', '#b7f3ff', '#ffffff'];
const nodePaletteAwake = ['#38e3ff', '#65ecff', '#91f2ff', '#d6fbff', '#ffffff'];

const nodes = [];
const links = [];
const activePulses = [];

let currentState = 'sleeping';
let pulseTimeout = null;
let lastFrameTime = performance.now();
let statePollInterval = null;

function rand(min, max) {
    return Math.random() * (max - min) + min;
}

function pick(arr) {
    return arr[Math.floor(Math.random() * arr.length)];
}

function createStars() {
    const total = 44;
    for (let i = 0; i < total; i += 1) {
        const star = document.createElement('span');
        star.style.left = `${rand(1, 99)}%`;
        star.style.top = `${rand(2, 98)}%`;
        const size = rand(1, 2.4);
        const opacity = rand(0.1, 0.3);
        star.style.width = `${size}px`;
        star.style.height = `${size}px`;
        star.style.opacity = opacity;
        star.style.transform = `scale(${rand(0.8, 1.4)})`;
        starfieldEl.appendChild(star);
    }
}

function pointOnOrbitalBand(index) {
    const angle = (Math.PI * 2 * index) / nodeCount + rand(-0.3, 0.3);
    const radius = rand(20, orbRadius * 0.85);
    const x = center.x + Math.cos(angle) * radius * rand(0.9, 1.1);
    const y = center.y + Math.sin(angle) * radius * rand(0.9, 1.1);
    return { x, y };
}

function createNode(index) {
    const base = pointOnOrbitalBand(index);
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('class', 'network-node');
    const radius = rand(0.5, 1.5);
    circle.setAttribute('r', radius.toFixed(2));
    nodeLayer.appendChild(circle);
    return {
        id: index,
        baseX: base.x,
        baseY: base.y,
        x: base.x,
        y: base.y,
        r: radius,
        phaseX: rand(0, Math.PI * 2),
        phaseY: rand(0, Math.PI * 2),
        driftX: rand(1, 4),
        driftY: rand(1, 4),
        colorIndex: index % nodePaletteSleep.length,
        el: circle,
        linkIds: [],
    };
}

function distance(a, b) {
    const dx = a.x - b.x;
    const dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
}

function curvatureControl(a, b, bend = 0.18) {
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.max(1, Math.sqrt(dx * dx + dy * dy));
    const nx = -dy / len;
    const ny = dx / len;
    const strength = Math.min(110, len * bend) * (Math.random() > 0.5 ? 1 : -1);
    return { x: mx + nx * strength, y: my + ny * strength };
}

function buildLinks() {
    const seen = new Set();

    nodes.forEach((node) => {
        const nearest = [...nodes]
            .filter((other) => other.id !== node.id)
            .map((other) => ({ other, dist: distance(node, other) }))
            .sort((a, b) => a.dist - b.dist)
            .slice(0, 5);

        nearest.forEach(({ other }) => {
            const key = [node.id, other.id].sort((a, b) => a - b).join('-');
            if (seen.has(key)) return;
            seen.add(key);
            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('class', 'network-line');
            lineLayer.appendChild(path);
            const control = curvatureControl(node, other, 0.18);
            const link = { id: links.length, from: node.id, to: other.id, control, el: path };
            links.push(link);
            node.linkIds.push(link.id);
            other.linkIds.push(link.id);
        });
    });

    const anchorAngles = [-0.98, -0.72, -0.34, 0.08, 0.42, 0.88, 1.28, 1.7, 2.08, 2.54, 2.92, 3.28];
    const anchors = anchorAngles.map((angle, idx) => ({
        id: `anchor-${idx}`,
        x: center.x + Math.cos(angle) * (orbRadius * 0.45),
        y: center.y + Math.sin(angle) * (orbRadius * 0.45),
        linkIds: [],
    }));

    anchors.forEach((anchor, idx) => {
        const node = nodes[(idx * 5) % nodes.length];
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('class', 'network-line');
        lineLayer.appendChild(path);
        const control = curvatureControl(anchor, node, 0.12);
        const link = { id: links.length, from: anchor.id, to: node.id, control, el: path, anchor };
        links.push(link);
        anchor.linkIds.push(link.id);
        node.linkIds.push(link.id);
    });

    return anchors;
}

const anchors = [];

function getPointById(id) {
    if (typeof id === 'string') return anchors.find((a) => a.id === id);
    return nodes[id];
}

function updateLinkPath(link) {
    const from = getPointById(link.from);
    const to = getPointById(link.to);
    if (!from || !to) return;
    const control = link.anchor ? curvatureControl(from, to, 0.12) : curvatureControl(from, to, 0.22);
    link.control = control;
    link.el.setAttribute('d', `M ${from.x.toFixed(1)} ${from.y.toFixed(1)} Q ${control.x.toFixed(1)} ${control.y.toFixed(1)} ${to.x.toFixed(1)} ${to.y.toFixed(1)}`);
}

function updateNodeVisual(node) {
    const palette = currentState === 'awake' ? nodePaletteAwake : nodePaletteSleep;
    node.el.setAttribute('cx', node.x.toFixed(2));
    node.el.setAttribute('cy', node.y.toFixed(2));
    node.el.setAttribute('fill', palette[node.colorIndex % palette.length]);
    node.el.setAttribute('opacity', currentState === 'awake' ? '0.5' : '0.3');
}

function fireNode(node, strength = 'soft') {
    node.el.classList.add('flash');
    const targetR = strength === 'burst' ? node.r + 1.2 : node.r + 0.6;
    node.el.setAttribute('r', targetR.toFixed(2));
    setTimeout(() => {
        node.el.classList.remove('flash');
        node.el.setAttribute('r', node.r.toFixed(2));
    }, strength === 'burst' ? 320 : 180);
}

function animatePulse(link, duration = 420) {
    const from = getPointById(link.from);
    const to = getPointById(link.to);
    if (!from || !to) return;

    const pulse = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    pulse.setAttribute('class', 'pulse-dot');
    pulse.setAttribute('r', currentState === 'awake' ? '1.8' : '1.2');
    pulseLayer.appendChild(pulse);

    link.el.classList.add('active');

    const start = performance.now();
    activePulses.push({ pulse, link, start, duration, from, to });

    setTimeout(() => {
        link.el.classList.remove('active');
    }, duration + 60);
}

function pulseAlongChain(startNode, depth = 0, strength = 'soft') {
    fireNode(startNode, strength);

    const candidates = startNode.linkIds
        .map((id) => links[id])
        .filter(Boolean)
        .sort(() => Math.random() - 0.5);

    const maxBranches = depth === 0 && Math.random() > 0.82 ? rand(2, 4) : 1;
    candidates.slice(0, Math.floor(maxBranches)).forEach((link, index) => {
        animatePulse(link, currentState === 'awake' ? 240 : 420);
        const nextId = link.from === startNode.id ? link.to : link.from;
        const nextNode = typeof nextId === 'string' ? null : nodes[nextId];
        if (!nextNode) return;
        const delay = rand(50, 150) + index * 40;
        if (depth < (currentState === 'awake' ? 3 : 2)) {
            setTimeout(() => pulseAlongChain(nextNode, depth + 1, depth === 0 ? 'medium' : 'soft'), delay);
        } else {
            setTimeout(() => fireNode(nextNode, 'soft'), delay);
        }
    });
}

function triggerBurst() {
    nodes.forEach((node, index) => {
        setTimeout(() => pulseAlongChain(node, 0, 'burst'), rand(0, 480) + index * 10);
    });
}

function triggerSleepCollapse() {
    const ordered = [...nodes].sort((a, b) => distance(b, center) - distance(a, center));
    ordered.forEach((node, index) => {
        setTimeout(() => fireNode(node, 'soft'), index * 16);
    });
}

function setState(nextState) {
    currentState = nextState;
    shell.className = `shell state-${nextState}`;
    shell.dataset.state = nextState;
    if (titleEl) titleEl.textContent = stateContent[nextState].title;
    if (copyEl) copyEl.textContent = stateContent[nextState].copy;
    if (orbLabel) orbLabel.textContent = nextState === 'awake' ? 'ACTIVE' : 'DORMANT';
}

function applyVoiceState(rawState) {
    if (!rawState) return;
    // Update granular orb label
    const labelMap = { sleeping: 'DORMANT', listening: 'LISTENING', thinking: 'PROCESSING', responding: 'SPEAKING' };
    if (orbLabel) orbLabel.textContent = labelMap[rawState] || 'ACTIVE';

    if (rawState === 'sleeping') {
        transitionToSleeping();
        return;
    }
    // Any active voice phase should visually map to the awake orb state.
    if (rawState === 'listening' || rawState === 'thinking' || rawState === 'responding') {
        transitionToAwake();
    }
}

function transitionToAwake() {
    if (currentState === 'awake') return;
    shell.classList.add('transition-awake');
    setState('awake');
    shockwaveEl.getAnimations?.().forEach((anim) => anim.cancel());
    shockwaveEl.style.animation = 'none';
    void shockwaveEl.offsetWidth;
    shockwaveEl.style.animation = '';
    triggerBurst();
    setTimeout(() => shell.classList.remove('transition-awake'), 1800);
    scheduleNextPulse();
}

function transitionToSleeping() {
    if (currentState === 'sleeping') return;
    shell.classList.add('transition-sleep');
    triggerSleepCollapse();
    setTimeout(() => {
        setState('sleeping');
        shell.classList.remove('transition-sleep');
        scheduleNextPulse();
    }, 1500);
}

function scheduleNextPulse() {
    clearTimeout(pulseTimeout);
    const min = currentState === 'awake' ? 160 : 500;
    const max = currentState === 'awake' ? 520 : 2000;
    pulseTimeout = setTimeout(() => {
        const burstRoll = Math.random();
        if (burstRoll > (currentState === 'awake' ? 0.76 : 0.94)) {
            const burstCount = currentState === 'awake' ? rand(5, 9) : rand(3, 6);
            for (let i = 0; i < burstCount; i += 1) {
                setTimeout(() => pulseAlongChain(pick(nodes), 0, 'burst'), i * rand(45, 90));
            }
        } else {
            pulseAlongChain(pick(nodes), 0, 'soft');
        }
        scheduleNextPulse();
    }, rand(min, max));
}

function buildHudTicks() {
    const tickGroup = document.getElementById('hud-ticks');
    if (!tickGroup) return;
    for (let deg = 0; deg < 360; deg += 6) {
        const rad = (deg * Math.PI) / 180;
        const r1 = deg % 30 === 0 ? 178 : deg % 12 === 0 ? 188 : 193;
        const r2 = 196;
        const x1 = 210 + r1 * Math.cos(rad);
        const y1 = 210 + r1 * Math.sin(rad);
        const x2 = 210 + r2 * Math.cos(rad);
        const y2 = 210 + r2 * Math.sin(rad);
        const opacity = deg % 30 === 0 ? 0.58 : deg % 12 === 0 ? 0.36 : 0.2;
        const w = deg % 30 === 0 ? 1.6 : 0.9;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1.toFixed(2));
        line.setAttribute('y1', y1.toFixed(2));
        line.setAttribute('x2', x2.toFixed(2));
        line.setAttribute('y2', y2.toFixed(2));
        line.setAttribute('stroke', `rgba(0,212,255,${opacity})`);
        line.setAttribute('stroke-width', w);
        tickGroup.appendChild(line);
    }
}

function buildNetwork() {
    createStars();
    for (let i = 0; i < nodeCount; i += 1) {
        nodes.push(createNode(i));
    }
    anchors.push(...buildLinks());
    buildHudTicks();
}

function updatePositions(time) {
    const dt = Math.min(0.045, (time - lastFrameTime) / 1000);
    lastFrameTime = time;
    const speed = currentState === 'awake' ? 1.25 : 0.38;

    nodes.forEach((node, idx) => {
        const driftScale = currentState === 'awake' ? 1.2 : 0.65;
        node.x = node.baseX + Math.cos(time * 0.00042 * speed + node.phaseX) * node.driftX * driftScale;
        node.y = node.baseY + Math.sin(time * 0.0005 * speed + node.phaseY) * node.driftY * driftScale;
        updateNodeVisual(node);
    });

    links.forEach(updateLinkPath);

    for (let i = activePulses.length - 1; i >= 0; i -= 1) {
        const item = activePulses[i];
        const progress = (time - item.start) / item.duration;
        if (progress >= 1) {
            item.pulse.remove();
            activePulses.splice(i, 1);
            const endNode = typeof item.link.to === 'string' ? null : nodes[item.link.to];
            if (endNode) fireNode(endNode, currentState === 'awake' ? 'medium' : 'soft');
            continue;
        }

        const t = Math.max(0, progress);
        const from = getPointById(item.link.from);
        const to = getPointById(item.link.to);
        const c = item.link.control;
        const x = (1 - t) * (1 - t) * from.x + 2 * (1 - t) * t * c.x + t * t * to.x;
        const y = (1 - t) * (1 - t) * from.y + 2 * (1 - t) * t * c.y + t * t * to.y;
        item.pulse.setAttribute('cx', x.toFixed(2));
        item.pulse.setAttribute('cy', y.toFixed(2));
        item.pulse.setAttribute('opacity', (currentState === 'awake' ? 1 : 0.82).toString());
    }

    requestAnimationFrame(updatePositions);
}

// ── WebSocket connection to PAI voice state ──────────────────────────────────
function connectVoiceWS() {
    const wsUrl = `${ORCHESTRATOR_WS}/voice/ws`;

    let ws;
    let pingInterval;

    function open() {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            pingInterval = setInterval(() => {
                if (ws.readyState === WebSocket.OPEN) ws.send('ping');
            }, 25000);
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                applyVoiceState(data.state);
            } catch (_) {}
        };

        ws.onclose = () => {
            clearInterval(pingInterval);
            // Reconnect after 3 seconds
            setTimeout(open, 3000);
        };

        ws.onerror = () => ws.close();
    }

    open();
}

function startStatePolling() {
    if (statePollInterval) return;
    statePollInterval = setInterval(async () => {
        try {
            const resp = await fetch(`${ORCHESTRATOR_HTTP}/voice/state`, { cache: 'no-store' });
            if (!resp.ok) return;
            const data = await resp.json();
            applyVoiceState(data.state);
        } catch (_) {
            // Ignore transient polling failures; websocket may still be connected.
        }
    }, 1500);
}

formEl.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = inputEl.value.trim().toLowerCase();
    if (!value) return;
    if (value === 'aegis') {
        // Also tell the server to wake
        fetch(`${ORCHESTRATOR_HTTP}/voice/wake`, { method: 'POST' }).catch(() => {});
        transitionToAwake();
    } else if (value === 'sleep') {
        fetch(`${ORCHESTRATOR_HTTP}/voice/sleep`, { method: 'POST' }).catch(() => {});
        transitionToSleeping();
    }
    inputEl.value = '';
});

// ── Touch wake button ──
if (wakeBtn) {
    wakeBtn.addEventListener('click', async () => {
        if (currentState === 'awake') {
            fetch(`${ORCHESTRATOR_HTTP}/voice/sleep`, { method: 'POST' }).catch(() => {});
            transitionToSleeping();
        } else {
            transitionToAwake();
            try {
                const resp = await fetch(`${ORCHESTRATOR_HTTP}/voice/wake`, { method: 'POST' });
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.audio) {
                        const audio = new Audio('data:audio/wav;base64,' + data.audio);
                        audio.play().catch(() => {});
                    }
                    if (data.greeting) {
                        const responseEl = document.getElementById('dash-response');
                        if (responseEl) responseEl.textContent = data.greeting;
                    }
                }
            } catch (_) {}
        }
    });
}

// ── Dashboard data (clock, weather, calendar, last exchange) ──
function updateClock() {
    const now = new Date();
    const clock = document.getElementById('dash-clock');
    const dateEl = document.getElementById('dash-date');
    if (clock) clock.textContent = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
    if (dateEl) dateEl.textContent = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}
updateClock();
setInterval(updateClock, 10000);

async function fetchDashData() {
    try {
        const resp = await fetch(`${ORCHESTRATOR_HTTP}/dashboard/summary`, { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();

        const weatherEl = document.getElementById('dash-weather');
        const tempEl = document.getElementById('dash-temp');
        const eventEl = document.getElementById('dash-event');
        const eventTimeEl = document.getElementById('dash-event-time');
        const transcriptEl = document.getElementById('dash-transcript');
        const responseEl = document.getElementById('dash-response');

        if (data.weather && weatherEl) {
            weatherEl.textContent = data.weather.condition || '--';
            if (tempEl) tempEl.textContent = data.weather.temperature || '--';
        }
        if (data.next_event && eventEl) {
            eventEl.textContent = data.next_event.title || '--';
            if (eventTimeEl) eventTimeEl.textContent = data.next_event.time || '--';
        }
        if (data.last_exchange) {
            if (transcriptEl) transcriptEl.textContent = data.last_exchange.user || 'Awaiting input...';
            if (responseEl) responseEl.textContent = data.last_exchange.assistant || '--';
        }
    } catch (_) {}
}

// Fetch dashboard data every 30s
fetchDashData();
setInterval(fetchDashData, 30000);

buildNetwork();
setState('sleeping');
scheduleNextPulse();
requestAnimationFrame(updatePositions);
connectVoiceWS();
startStatePolling();