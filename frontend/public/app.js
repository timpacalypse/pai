/* ── PAI Frontend App ── */

const API = '/api';  // proxied through nginx to orchestrator:8000

const state = {
    mode: 'chat',
    role: '',
    secondaryRole: '',
    conversationId: crypto.randomUUID(),
    history: [],
    sending: false,
};

// ── DOM refs ──
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');
const roleSelect = document.getElementById('role-select');
const secondarySelect = document.getElementById('secondary-select');
const clearBtn = document.getElementById('clear-btn');
const healthDot = document.querySelector('.health-dot');
const healthText = document.getElementById('health-text');
const researchOpts = document.getElementById('research-options');
const competeOpts = document.getElementById('compete-options');
const mealsOpts = document.getElementById('meals-options');
const homeOpts = document.getElementById('home-options');
const medicalOpts = document.getElementById('medical-options');
const recipesOpts = document.getElementById('recipes-options');
const calendarOpts = document.getElementById('calendar-options');
const skillsOpts = document.getElementById('skills-options');
const timeFilter = document.getElementById('time-filter');
const autoIngest = document.getElementById('auto-ingest');
const strategySelect = document.getElementById('strategy-select');

// ── Init ──
async function init() {
    await loadRoles();
    checkHealth();
    setInterval(checkHealth, 30000);

    // Event listeners
    sendBtn.addEventListener('click', handleSend);
    inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    });
    clearBtn.addEventListener('click', clearChat);
    roleSelect.addEventListener('change', () => { state.role = roleSelect.value; });
    secondarySelect.addEventListener('change', () => { state.secondaryRole = secondarySelect.value; });

    // Mode buttons
    document.querySelectorAll('.mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.mode = btn.dataset.mode;

            researchOpts.classList.toggle('hidden', state.mode !== 'research');
            competeOpts.classList.toggle('hidden', state.mode !== 'compete');
            mealsOpts.classList.toggle('hidden', state.mode !== 'meals');
            homeOpts.classList.toggle('hidden', state.mode !== 'home');
            medicalOpts.classList.toggle('hidden', state.mode !== 'medical');
            recipesOpts.classList.toggle('hidden', state.mode !== 'recipes');
            calendarOpts.classList.toggle('hidden', state.mode !== 'calendar');
            skillsOpts.classList.toggle('hidden', state.mode !== 'skills');

            if (state.mode === 'skills') { loadSkillsInventory(); }

            // Update placeholder
            const placeholders = {
                medical: 'Tell PAI about a medical event (e.g. "Tim had a dental cleaning today")...',
                recipes: 'Save a recipe (e.g. paste a recipe title + ingredients) or search...',
                calendar: 'Tell PAI about an event (e.g. "Emma\'s birthday is June 15")...',
                chat: 'Type a message...',
                task: 'Describe a task...',
                research: 'Enter a research topic...',
                compete: 'Enter a task for multi-agent competition...',
                meals: 'Add family member or preference (e.g. "Add Tim, adult") or extra meal plan instructions...',
                home: 'Tell PAI about your home (e.g. "I replaced the air filter today, replace every 3 months")...',
                medical: 'Tell PAI about a medical event (e.g. "Tim had a dental cleaning today")...',
                recipes: 'Save a recipe (e.g. paste a recipe title + ingredients) or search...',
                calendar: 'Tell PAI about an event (e.g. "Emma\'s birthday is June 15")...',
                skills: 'Skills mode — click a skill or use "Refresh Skills" to see the inventory',
            };
            inputEl.placeholder = placeholders[state.mode] || 'Type a message...';
        });
    });


    // Medical mode buttons
    document.getElementById('medical-records-btn').addEventListener('click', loadMedicalRecords);
    document.getElementById('medical-upload').addEventListener('change', handleMedicalUpload);

    // Recipes mode buttons
    document.getElementById('recipes-list-btn').addEventListener('click', loadRecipes);

    // Calendar mode buttons
    document.getElementById('calendar-agenda-btn').addEventListener('click', loadAgenda);
    document.getElementById('calendar-events-btn').addEventListener('click', loadAllEvents);
    // Meals mode buttons
    document.getElementById('load-family-btn').addEventListener('click', loadFamily);
    document.getElementById('gen-plan-btn').addEventListener('click', generateMealPlan);
    document.getElementById('view-plans-btn').addEventListener('click', viewPastPlans);

    // Home mode buttons
    document.getElementById('home-items-btn').addEventListener('click', loadHomeItems);
    document.getElementById('home-alerts-btn').addEventListener('click', checkHomeAlerts);
    document.getElementById('home-docs-btn').addEventListener('click', searchHomeDocs);
    document.getElementById('home-upload').addEventListener('change', handleHomeUpload);

    // Skills mode buttons
    document.getElementById('skills-load-btn').addEventListener('click', loadSkillsInventory);

    addSystemMessage('Welcome to PAI. Your roles and agents are auto-selected from your prompt. Override with the dropdowns if needed.');

    // Try to reload last conversation from server
    await loadConversationHistory();
}

// ── Load roles into dropdowns ──
async function loadRoles() {
    try {
        const resp = await fetch(`${API}/roles`);
        const data = await resp.json();
        for (const [domain, roles] of Object.entries(data.domains)) {
            const group = document.createElement('optgroup');
            group.label = domain.replace('_', ' ').toUpperCase();
            const group2 = group.cloneNode(true);

            for (const r of roles) {
                const opt = new Option(r.role.replace(/_/g, ' '), r.role);
                opt.title = r.description;
                group.appendChild(opt);
                group2.appendChild(opt.cloneNode(true));
            }
            roleSelect.appendChild(group);
            secondarySelect.appendChild(group2);
        }
    } catch (e) {
        console.error('Failed to load roles', e);
    }
}

// ── Health check ──
async function checkHealth() {
    try {
        const resp = await fetch(`${API}/health`);
        const data = await resp.json();
        healthDot.className = 'health-dot ' + (data.status === 'healthy' ? 'ok' : 'degraded');
        healthText.textContent = data.status;
    } catch {
        healthDot.className = 'health-dot error';
        healthText.textContent = 'Unreachable';
    }
}

// ── Send message ──
async function handleSend() {
    const text = inputEl.value.trim();
    if (!text || state.sending) return;

    addMessage(text, 'user');
    inputEl.value = '';
    state.sending = true;
    sendBtn.disabled = true;

    const loadingEl = addLoading();

    try {
        let data;
        switch (state.mode) {
            case 'chat':
                data = await sendChat(text);
                break;
            case 'task':
                data = await sendTask(text);
                break;
            case 'research':
                data = await sendResearch(text);
                break;
            case 'compete':
                data = await sendCompete(text);
                break;
            case 'meals':
                data = await handleMealsInput(text);
                break;
            case 'home':
                data = await handleHomeInput(text);
                break;
            case 'medical':
                data = await handleMedicalInput(text);
                break;
            case 'recipes':
                data = await handleRecipeInput(text);
                break;
            case 'calendar':
                data = await handleCalendarInput(text);
                break;
        }
        loadingEl.remove();

        if (data) {
            if (state.mode === 'research') {
                renderResearchResults(data);
            } else {
                const role = data.role || '';
                const duration = data.duration_ms ? `${Math.round(data.duration_ms)}ms` : '';
                const workflow = data.workflow || '';
                const intent = data.intent || '';
                const meta = [role, workflow, intent, duration].filter(Boolean).join(' · ');
                addMessage(data.content, 'ai', meta);

                // Update history for chat mode
                if (state.mode === 'chat') {
                    state.history.push({ role_name: 'user', content: text });
                    state.history.push({ role_name: 'assistant', content: data.content });
                    // Keep last 20
                    if (state.history.length > 20) state.history = state.history.slice(-20);
                }
            }
        }
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }

    state.sending = false;
    sendBtn.disabled = false;
    inputEl.focus();
}

// ── API calls ──
async function sendChat(message) {
    const body = {
        message,
        conversation_id: state.conversationId,
        history: state.history,
    };
    if (state.role) body.role = state.role;
    if (state.secondaryRole) body.secondary_role = state.secondaryRole;

    const resp = await fetch(`${API}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    return resp.json();
}

async function sendTask(input) {
    const body = { input };
    if (state.role) body.role = state.role;
    if (state.secondaryRole) body.secondary_role = state.secondaryRole;

    const resp = await fetch(`${API}/task`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    return resp.json();
}

async function sendResearch(topic) {
    const body = {
        topic,
        max_results: 10,
        time_filter: timeFilter.value,
        auto_ingest: autoIngest.checked,
    };
    if (state.role) body.role = state.role;

    const resp = await fetch(`${API}/skills/web-research`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    return resp.json();
}

async function sendCompete(input) {
    const agents = [];
    document.querySelectorAll('.agent-checkboxes input:checked').forEach(cb => agents.push(cb.value));
    if (agents.length < 2) {
        throw new Error('Select at least 2 agents for competition');
    }

    const body = {
        input,
        agents,
        strategy: strategySelect.value,
    };
    if (state.role) body.role = state.role;
    if (state.secondaryRole) body.secondary_role = state.secondaryRole;

    const resp = await fetch(`${API}/compete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    return resp.json();
}

// ── Render helpers ──
function addMessage(text, type, meta = '') {
    const div = document.createElement('div');
    div.className = `message ${type}`;

    if (meta && type === 'ai') {
        const metaEl = document.createElement('div');
        metaEl.className = 'meta';
        metaEl.innerHTML = `<span class="role-tag">${escapeHtml(meta)}</span>`;
        div.appendChild(metaEl);
    }

    const contentEl = document.createElement('div');
    contentEl.innerHTML = formatContent(text);
    div.appendChild(contentEl);

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

function addSystemMessage(text) {
    const div = document.createElement('div');
    div.className = 'message ai';
    div.style.opacity = '0.7';
    div.style.fontStyle = 'italic';
    div.textContent = text;
    messagesEl.appendChild(div);
}

function addLoading() {
    const div = document.createElement('div');
    div.className = 'message ai';
    div.innerHTML = '<div class="loading-dots"><span></span><span></span><span></span></div>';
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

function renderResearchResults(data) {
    const duration = data.duration_ms ? `${Math.round(data.duration_ms)}ms` : '';
    const meta = `Found ${data.total_found} articles · Ingested ${data.ingested_count} · ${duration}`;

    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">research</span> <span>${escapeHtml(meta)}</span>`;
    div.appendChild(metaEl);

    for (const article of data.articles) {
        const card = document.createElement('div');
        card.className = 'research-card';
        const score = article.score?.total ?? 0;
        card.innerHTML = `
            <span class="rc-score">${score.toFixed(2)}</span>
            <div class="rc-title">${escapeHtml(article.title)}</div>
            <div class="rc-url"><a href="${escapeHtml(article.url)}" target="_blank" rel="noopener">${escapeHtml(article.url)}</a></div>
            <div class="rc-snippet">${escapeHtml(article.snippet || '')}</div>
        `;
        div.appendChild(card);
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function formatContent(text) {
    // Basic markdown-like formatting
    let html = escapeHtml(text);

    // Code blocks
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Bold
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // Line breaks
    html = html.replace(/\n/g, '<br>');

    return html;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function clearChat() {
    messagesEl.innerHTML = '';
    state.history = [];
    state.conversationId = crypto.randomUUID();
    addSystemMessage('Chat cleared. Starting fresh conversation.');
}

async function loadConversationHistory() {
    try {
        const resp = await fetch(`${API}/chat/history?conversation_id=${state.conversationId}`);
        const data = await resp.json();
        if (data.turns && data.turns.length > 0) {
            state.history = data.turns.slice(-20);
            for (const turn of data.turns) {
                addMessage(turn.content, turn.role_name === 'user' ? 'user' : 'ai');
            }
        }
    } catch (e) {
        // No history to load — that's fine
    }
}

// ── Meals Mode ──

async function handleMealsInput(text) {
    // Use the text as extra instructions for meal plan generation
    const resp = await fetch(`${API}/skills/meal-plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ extra_instructions: text }),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    renderMealPlan(data);
    return null; // already rendered
}

async function loadFamily() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/family`);
        const data = await resp.json();
        loadingEl.remove();
        renderFamily(data);
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error loading family: ${e.message}`, 'ai', 'error');
    }
}

async function generateMealPlan() {
    addSystemMessage('Generating meal plan based on family preferences... this may take a moment.');
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/meal-plan`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
        const data = await resp.json();
        loadingEl.remove();
        renderMealPlan(data);
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error generating plan: ${e.message}`, 'ai', 'error');
    }
}

async function viewPastPlans() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/meal-plan?limit=3`);
        const data = await resp.json();
        loadingEl.remove();
        if (!data.plans || data.plans.length === 0) {
            addSystemMessage('No meal plans generated yet. Click "Generate Meal Plan" to create one.');
            return;
        }
        for (const plan of data.plans) {
            renderMealPlan(plan.plan ? { ...plan.plan, plan_id: plan.id, week_label: plan.week_label } : plan);
        }
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

function renderFamily(data) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = '<span class="role-tag">family_chef</span> Family Members & Preferences';
    div.appendChild(metaEl);

    if (!data.members || data.members.length === 0) {
        const p = document.createElement('p');
        p.textContent = 'No family members yet. Use the API or chat to add members.';
        div.appendChild(p);
    } else {
        for (const m of data.members) {
            const card = document.createElement('div');
            card.className = 'research-card';
            const restrictions = m.dietary_restrictions?.length ? m.dietary_restrictions.join(', ') : 'none';
            const memberPrefs = (data.preferences || []).filter(p => p.family_member_id === m.id);
            let prefsHtml = '';
            if (memberPrefs.length) {
                prefsHtml = '<div style="margin-top:4px;font-size:0.85em">';
                for (const p of memberPrefs) {
                    const color = { love: '#4caf50', like: '#8bc34a', neutral: '#999', dislike: '#ff9800', hate: '#f44336', allergy: '#e91e63' }[p.sentiment] || '#999';
                    prefsHtml += `<span style="color:${color};margin-right:8px">${escapeHtml(p.sentiment)}: ${escapeHtml(p.item)}</span>`;
                }
                prefsHtml += '</div>';
            }
            card.innerHTML = `
                <div class="rc-title">${escapeHtml(m.name)} <span style="opacity:0.6;font-size:0.85em">(${escapeHtml(m.age_group)})</span></div>
                <div class="rc-snippet">Dietary restrictions: ${escapeHtml(restrictions)}</div>
                ${prefsHtml}
            `;
            div.appendChild(card);
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderMealPlan(data) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    const label = data.week_label || '';
    metaEl.innerHTML = `<span class="role-tag">family_chef</span> Meal Plan ${escapeHtml(label)}`;
    div.appendChild(metaEl);

    if (data.parse_error) {
        const p = document.createElement('div');
        p.innerHTML = formatContent(data.raw_plan || 'Plan generation returned unparseable format.');
        div.appendChild(p);
    } else {
        const week = data.week || [];
        if (week.length) {
            const table = document.createElement('table');
            table.className = 'meal-table';
            table.innerHTML = `
                <thead><tr>
                    <th>Day</th><th>Breakfast</th><th>Lunch</th><th>Dinner</th><th>Snack</th>
                </tr></thead>
            `;
            const tbody = document.createElement('tbody');
            for (const day of week) {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><strong>${escapeHtml(day.day || '')}</strong></td>
                    <td>${escapeHtml(day.breakfast || '-')}</td>
                    <td>${escapeHtml(day.lunch || '-')}</td>
                    <td>${escapeHtml(day.dinner || '-')}</td>
                    <td>${escapeHtml(day.snack || '-')}</td>
                `;
                tbody.appendChild(tr);
            }
            table.appendChild(tbody);
            div.appendChild(table);
        }

        if (data.shopping_list?.length) {
            const sl = document.createElement('div');
            sl.style.marginTop = '12px';
            sl.innerHTML = `<strong>Shopping List:</strong> ${data.shopping_list.map(i => escapeHtml(i)).join(', ')}`;
            div.appendChild(sl);
        }

        if (data.notes) {
            const notes = document.createElement('div');
            notes.style.marginTop = '8px';
            notes.style.opacity = '0.8';
            notes.innerHTML = `<em>${escapeHtml(data.notes)}</em>`;
            div.appendChild(notes);
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Home Mode ──

async function handleHomeInput(text) {
    const resp = await fetch(`${API}/skills/home/tell`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    renderHomeTellResult(data);
    return null;
}

async function loadHomeItems() {
    const loadingEl = addLoading();
    try {
        const [itemsResp, tasksResp] = await Promise.all([
            fetch(`${API}/skills/home/items`),
            fetch(`${API}/skills/home/tasks`),
        ]);
        const items = await itemsResp.json();
        const tasks = await tasksResp.json();
        loadingEl.remove();
        renderHomeItems(items.items || [], tasks.tasks || []);
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

async function checkHomeAlerts() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/home/alerts`);
        const data = await resp.json();
        loadingEl.remove();
        renderHomeAlerts(data);
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

async function searchHomeDocs() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/home/documents`);
        const data = await resp.json();
        loadingEl.remove();
        renderHomeDocs(data.documents || []);
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

function renderHomeTellResult(data) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = '<span class="role-tag">home_kb</span> Processed';
    div.appendChild(metaEl);

    if (data.error) {
        div.appendChild(Object.assign(document.createElement('p'), {
            textContent: data.error,
            style: 'color: #f44336;',
        }));
    } else {
        const actions = data.actions || [];
        if (actions.length) {
            const ul = document.createElement('ul');
            ul.style.cssText = 'list-style:none;padding:0;margin:8px 0;';
            for (const a of actions) {
                const li = document.createElement('li');
                li.style.cssText = 'padding:4px 0;color:#4caf50;';
                li.textContent = '✓ ' + a;
                ul.appendChild(li);
            }
            div.appendChild(ul);
        }

        if (data.task?.next_due_at) {
            const due = document.createElement('p');
            due.style.cssText = 'color:#ff9800;margin-top:8px;';
            const dueDate = new Date(data.task.next_due_at).toLocaleDateString();
            due.textContent = `Next due: ${dueDate}`;
            div.appendChild(due);
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderHomeItems(items, tasks) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">home_kb</span> ${items.length} items, ${tasks.length} tasks`;
    div.appendChild(metaEl);

    if (!items.length) {
        div.appendChild(Object.assign(document.createElement('p'), {
            textContent: 'No home items tracked yet. Use the input to tell PAI about your home.',
        }));
    } else {
        for (const item of items) {
            const card = document.createElement('div');
            card.className = 'research-card';
            const itemTasks = tasks.filter(t => t.home_item_id === item.id);
            let tasksHtml = '';
            if (itemTasks.length) {
                tasksHtml = '<div style="margin-top:6px;font-size:0.85em;">';
                for (const t of itemTasks) {
                    const statusColor = t.status === 'overdue' ? '#f44336' : t.status === 'upcoming' ? '#ff9800' : '#4caf50';
                    const due = t.next_due_at ? new Date(t.next_due_at).toLocaleDateString() : 'no date';
                    tasksHtml += `<div style="color:${statusColor};padding:2px 0;">• ${escapeHtml(t.description)} — ${t.status} (${due})</div>`;
                }
                tasksHtml += '</div>';
            }
            card.innerHTML = `
                <div class="rc-title">${escapeHtml(item.name)} <span style="opacity:0.6;font-size:0.85em;">[${escapeHtml(item.category)}]</span></div>
                <div class="rc-snippet">${[item.location, item.brand, item.model_info].filter(Boolean).map(s => escapeHtml(s)).join(' · ') || 'No details'}</div>
                ${tasksHtml}
            `;
            div.appendChild(card);
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderHomeAlerts(data) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">home_kb</span> ${data.overdue_count} overdue, ${data.upcoming_count} upcoming`;
    div.appendChild(metaEl);

    if (data.overdue_count === 0 && data.upcoming_count === 0) {
        div.appendChild(Object.assign(document.createElement('p'), {
            textContent: 'All clear — no maintenance tasks due.',
            style: 'color: #4caf50;',
        }));
    } else {
        if (data.overdue?.length) {
            const h = document.createElement('h4');
            h.style.cssText = 'color:#f44336;margin:8px 0 4px;';
            h.textContent = `Overdue (${data.overdue.length})`;
            div.appendChild(h);
            for (const t of data.overdue) {
                const p = document.createElement('div');
                const due = t.next_due_at ? new Date(t.next_due_at).toLocaleDateString() : '';
                p.style.cssText = 'color:#f44336;padding:3px 0;';
                p.textContent = `⚠ ${t.item_name}: ${t.description} — was due ${due}`;
                div.appendChild(p);
            }
        }
        if (data.upcoming?.length) {
            const h = document.createElement('h4');
            h.style.cssText = 'color:#ff9800;margin:12px 0 4px;';
            h.textContent = `Upcoming (${data.upcoming.length})`;
            div.appendChild(h);
            for (const t of data.upcoming) {
                const p = document.createElement('div');
                const due = t.next_due_at ? new Date(t.next_due_at).toLocaleDateString() : '';
                p.style.cssText = 'color:#ff9800;padding:3px 0;';
                p.textContent = `📋 ${t.item_name}: ${t.description} — due ${due}`;
                div.appendChild(p);
            }
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderHomeDocs(docs) {
    const div = document.createElement('div');
    div.className = 'message ai';

    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">home_kb</span> ${docs.length} documents`;
    div.appendChild(metaEl);

    if (!docs.length) {
        div.appendChild(Object.assign(document.createElement('p'), {
            textContent: 'No documents stored yet. Add manuals, warranties, or notes via the API.',
        }));
    } else {
        for (const doc of docs) {
            const card = document.createElement('div');
            card.className = 'research-card';
            card.innerHTML = `
                <span class="rc-score">${escapeHtml(doc.doc_type)}</span>
                <div class="rc-title">${escapeHtml(doc.title)}</div>
                <div class="rc-url">${doc.item_name ? escapeHtml(doc.item_name) : 'General'}</div>
                <div class="rc-snippet">${escapeHtml(doc.preview || '')}</div>
            `;
            div.appendChild(card);
        }
    }

    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Skills Inventory ──

async function loadSkillsInventory() {
    const loading = addLoading();
    try {
        const resp = await fetch(`${API}/skills`);
        const data = await resp.json();
        loading.remove();

        const skills = data.skills || [];
        const grouped = {};
        for (const s of skills) {
            const cat = s.category || 'general';
            if (!grouped[cat]) grouped[cat] = [];
            grouped[cat].push(s);
        }

        const div = document.createElement('div');
        div.className = 'message ai skills-inventory';

        const metaEl = document.createElement('div');
        metaEl.className = 'meta';
        metaEl.innerHTML = `<span class="role-tag">skills</span> ${skills.length} registered skills`;
        div.appendChild(metaEl);

        const categoryOrder = ['family', 'professional', 'process', 'general'];
        const categoryLabels = {
            family: 'Family & Home',
            professional: 'Professional',
            process: 'Automated Processes',
            general: 'General',
        };

        // Sort categories: known order first, then unknown
        const sortedCats = Object.keys(grouped).sort((a, b) => {
            const ia = categoryOrder.indexOf(a);
            const ib = categoryOrder.indexOf(b);
            return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
        });

        for (const cat of sortedCats) {
            const catLabel = categoryLabels[cat] || cat.charAt(0).toUpperCase() + cat.slice(1);
            const header = document.createElement('div');
            header.className = 'skills-category-header';
            header.textContent = catLabel;
            div.appendChild(header);

            for (const skill of grouped[cat]) {
                const card = document.createElement('div');
                card.className = 'skill-card';

                const badges = [];
                if (skill.can_read) badges.push('<span class="skill-badge read">read</span>');
                if (skill.can_write) badges.push('<span class="skill-badge write">write</span>');

                card.innerHTML = `
                    <div class="skill-card-header">
                        <span class="skill-card-name">${escapeHtml(skill.name)}</span>
                        <span class="skill-card-badges">${badges.join('')}</span>
                    </div>
                    <div class="skill-card-desc">${escapeHtml(skill.description)}</div>
                    <div class="skill-card-examples">${skill.examples.map(e => `<span class="skill-example">"${escapeHtml(e)}"</span>`).join('')}</div>
                `;
                div.appendChild(card);
            }
        }

        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (e) {
        loading.remove();
        addSystemMessage('Failed to load skills inventory.');
        console.error('Skills fetch failed', e);
    }
}

// ── Start ──
init();


// ── Medical Mode ──

async function handleMedicalInput(text) {
    const resp = await fetch(`${API}/skills/medical/tell`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    renderActionResult(data, 'medical');
    return null;
}

async function loadMedicalRecords() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/medical/records`);
        const data = await resp.json();
        loadingEl.remove();
        renderListCards(data.records || [], 'medical', r =>
            `${r.member_name} — ${r.category} on ${r.record_date}: ${r.summary}`
        );
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

async function handleMedicalUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    event.target.value = '';

    const loadingEl = addLoading();
    try {
        // First ingest the file into semantic memory
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(`${API}/skills/ingest/file`, {
            method: 'POST',
            body: formData,
        });
        if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
        const data = await resp.json();
        loadingEl.remove();
        const chunks = data.chunks_stored || 0;
        addMessage(
            `Uploaded and ingested "${file.name}" — ${chunks} chunks stored in semantic memory. ` +
            `You can now ask questions about this document in chat.`,
            'ai', 'medical'
        );
    } catch (e) {
        loadingEl.remove();
        addMessage(`Upload error: ${e.message}`, 'ai', 'error');
    }
}

async function handleHomeUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    event.target.value = '';

    const loadingEl = addLoading();
    try {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(`${API}/skills/ingest/file`, {
            method: 'POST',
            body: formData,
        });
        if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
        const data = await resp.json();
        loadingEl.remove();
        const chunks = data.chunks_stored || 0;
        addMessage(
            `Uploaded and ingested "${file.name}" — ${chunks} chunks stored in semantic memory. ` +
            `You can now ask questions about this document in chat.`,
            'ai', 'home'
        );
    } catch (e) {
        loadingEl.remove();
        addMessage(`Upload error: ${e.message}`, 'ai', 'error');
    }
}


// ── Recipes Mode ──

async function handleRecipeInput(text) {
    // If it looks like a search, search. Otherwise parse and save the pasted recipe.
    const lower = text.toLowerCase();
    if (lower.startsWith('search ') || lower.startsWith('find ')) {
        const query = text.replace(/^(search|find)\s+/i, '');
        const resp = await fetch(`${API}/skills/recipes?search=${encodeURIComponent(query)}`);
        const data = await resp.json();
        renderListCards(data.recipes || [], 'recipes', r => {
            const rating = r.family_rating ? ` ★${r.family_rating}` : '';
            return `${r.title}${rating}${r.cuisine ? ' [' + r.cuisine + ']' : ''}`;
        });
        return null;
    }
    // Paste recipe — uses deterministic parser, no LLM, no token limit
    const resp = await fetch(`${API}/skills/recipes/paste`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        addMessage(`Could not parse recipe: ${err.detail || 'Unknown error'}. Try adding "Ingredients" and "Instructions" headings.`, 'ai', 'error');
        return null;
    }
    const data = await resp.json();
    const r = data.recipe;
    const fields = data.parsed_fields;
    let summary = `Saved: **${r.title}**`;
    if (fields.ingredients) summary += ` · ${fields.ingredients} ingredients`;
    if (fields.instructions) summary += ` · ${fields.instructions} steps`;
    if (r.cuisine) summary += ` · ${r.cuisine}`;
    addMessage(summary, 'ai', 'recipes');
    return null;
}

async function loadRecipes() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/recipes?limit=20`);
        const data = await resp.json();
        loadingEl.remove();
        renderListCards(data.recipes || [], 'recipes', r => {
            const rating = r.family_rating ? ` ★${r.family_rating}` : '';
            return `${r.title}${rating}${r.cuisine ? ' [' + r.cuisine + ']' : ''}`;
        });
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}


// ── Calendar Mode ──

async function handleCalendarInput(text) {
    const resp = await fetch(`${API}/skills/calendar/tell`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
    const data = await resp.json();
    renderActionResult(data, 'calendar');
    return null;
}

async function loadAgenda() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/calendar/agenda?days=14`);
        const data = await resp.json();
        loadingEl.remove();
        const div = document.createElement('div');
        div.className = 'message ai';
        const metaEl = document.createElement('div');
        metaEl.className = 'meta';
        metaEl.innerHTML = `<span class="role-tag">calendar</span> ${data.total_events} events next 14 days`;
        div.appendChild(metaEl);

        if (data.total_events === 0) {
            div.appendChild(Object.assign(document.createElement('p'), { textContent: 'No upcoming events.' }));
        } else {
            for (const [date, events] of Object.entries(data.agenda)) {
                const h = document.createElement('h4');
                h.style.cssText = 'color:#4f8ef7;margin:10px 0 4px;font-size:13px;';
                h.textContent = date;
                div.appendChild(h);
                for (const e of events) {
                    const p = document.createElement('div');
                    const time = e.event_time ? ` at ${e.event_time}` : '';
                    const who = e.family_member_name !== 'family' ? ` (${e.family_member_name})` : '';
                    p.style.cssText = 'padding:2px 0;font-size:13px;';
                    p.textContent = `• ${e.title}${time}${who}`;
                    div.appendChild(p);
                }
            }
        }
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}

async function loadAllEvents() {
    const loadingEl = addLoading();
    try {
        const resp = await fetch(`${API}/skills/calendar/events?limit=30`);
        const data = await resp.json();
        loadingEl.remove();
        renderListCards(data.events || [], 'calendar', e => {
            const time = e.event_time ? ` at ${e.event_time}` : '';
            const who = e.family_member_name !== 'family' ? ` (${e.family_member_name})` : '';
            const recur = e.recurrence !== 'none' ? ` [${e.recurrence}]` : '';
            return `${e.event_date}${time}: ${e.title}${who}${recur}`;
        });
    } catch (e) {
        loadingEl.remove();
        addMessage(`Error: ${e.message}`, 'ai', 'error');
    }
}


// ── Shared Renderers ──

function renderActionResult(data, tag) {
    const div = document.createElement('div');
    div.className = 'message ai';
    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">${escapeHtml(tag)}</span> Processed`;
    div.appendChild(metaEl);

    if (data.error) {
        div.appendChild(Object.assign(document.createElement('p'), {
            textContent: data.error, style: 'color:#f44336;',
        }));
    } else {
        const actions = data.actions || [];
        if (actions.length) {
            const ul = document.createElement('ul');
            ul.style.cssText = 'list-style:none;padding:0;margin:8px 0;';
            for (const a of actions) {
                const li = document.createElement('li');
                li.style.cssText = 'padding:4px 0;color:#4caf50;';
                li.textContent = '✓ ' + a;
                ul.appendChild(li);
            }
            div.appendChild(ul);
        }
    }
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderListCards(items, tag, formatter) {
    const div = document.createElement('div');
    div.className = 'message ai';
    const metaEl = document.createElement('div');
    metaEl.className = 'meta';
    metaEl.innerHTML = `<span class="role-tag">${escapeHtml(tag)}</span> ${items.length} items`;
    div.appendChild(metaEl);

    if (!items.length) {
        div.appendChild(Object.assign(document.createElement('p'), { textContent: 'No items found.' }));
    } else {
        for (const item of items) {
            const card = document.createElement('div');
            card.className = 'research-card';
            card.innerHTML = `<div class="rc-title">${escapeHtml(formatter(item))}</div>`;
            div.appendChild(card);
        }
    }
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
}
