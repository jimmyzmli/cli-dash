/* cli-dash core app.js — config-driven, extensible */

let currentOffset = 0, activeJobId = null;

// Extension registry — projects push to these before DOMContentLoaded
window.dashExtensions = { tabs: [], headerOptions: [], onInit: [] };

// --- Extension API ---
function registerTab(tab) { window.dashExtensions.tabs.push(tab); }
function registerHeaderOption(opt) { window.dashExtensions.headerOptions.push(opt); }
function showModal(html) { document.getElementById('modal-content').innerHTML = html; document.getElementById('modal-overlay').style.display = 'flex'; }
function closeModals() { document.getElementById('modal-overlay').style.display = 'none'; }

// --- Init ---
document.addEventListener('DOMContentLoaded', async () => {
    // Load app config
    let appConfig = {};
    try {
        const r = await fetch('/static/config/app.json');
        if (r.ok) appConfig = await r.json();
    } catch (e) { }

    // Load server config (title)
    try {
        const r = await fetch('/api/config');
        if (r.ok) { const d = await r.json(); if (d.title) document.title = d.title; }
    } catch (e) { }

    // Apply icon + title + favicon
    if (appConfig.icon) document.getElementById('app-icon').textContent = appConfig.icon;
    if (appConfig.title) document.getElementById('app-title').textContent = appConfig.title;
    
    const favicon = document.getElementById('favicon');
    if (appConfig.favicon) {
        favicon.href = appConfig.favicon;
    } else if (appConfig.icon) {
        favicon.href = `data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>${appConfig.icon}</text></svg>`;
    }

    // Build header options
    const headerContainer = document.getElementById('header-options');
    const options = (appConfig.header_options || []);
    // Merge extension-registered options
    options.push(...window.dashExtensions.headerOptions);
    options.forEach(opt => {
        const div = document.createElement('div');
        div.className = 'option-toggle';
        if (opt.warn_off) div.id = `${opt.id}-option-container`;
        div.innerHTML = `<input type="checkbox" id="${opt.id}-option"><label for="${opt.id}-option">${opt.label}</label>`;
        headerContainer.appendChild(div);
        const cb = div.querySelector('input');
        if (localStorage.getItem(opt.id) === 'true') cb.checked = true;
        cb.addEventListener('change', () => localStorage.setItem(opt.id, cb.checked));
    });

    // Build tabs: extension tabs first, then Commands + Scheduled
    const tabBar = document.getElementById('tab-bar');
    const tabViews = document.getElementById('tab-views');
    const allTabs = [...window.dashExtensions.tabs];
    // Add built-in tabs
    allTabs.push({ id: 'commands', label: 'Commands', html: buildCommandsHTML(), onActivate: loadCommands });
    allTabs.push({ id: 'scheduled', label: 'Scheduled', html: buildScheduledHTML(), onActivate: refreshSchedules });

    allTabs.forEach((tab, i) => {
        const link = document.createElement('a');
        link.href = `#${tab.label}`;
        link.className = 'tab-link';
        link.id = `tab-${tab.id}`;
        link.textContent = tab.label;
        tabBar.appendChild(link);

        const view = document.createElement('div');
        view.id = `view-${tab.id}`;
        view.className = 'view';
        view.style.display = 'none';
        view.innerHTML = tab.html || '';
        tabViews.appendChild(view);
    });

    // Run extension init hooks
    window.dashExtensions.onInit.forEach(fn => fn());

    // Route + SSE initialization
    handleRoute();
    refreshHistory();
    refreshSchedules();
    checkStatus();
    
    setupSSE();
    
    // Visibility-aware SSE
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (window._sse) { window._sse.close(); window._sse = null; }
        } else {
            setupSSE();
            refreshHistory();
            refreshSchedules();
            checkStatus();
        }
    });
});

function buildCommandsHTML() {
    return `
    <div class="controls">
        <div id="command-cards" style="display:contents"></div>
        <div class="card">
            <h2><span class="icon">⌨️</span> Custom Command</h2>
            <div class="custom-cmd-input">
                <input type="text" id="custom-command" list="command-presets" placeholder="e.g. my-script.py --flag">
                <button onclick="runCustomCommand()">Run</button>
                <datalist id="command-presets"></datalist>
            </div>
        </div>
    </div>`;
}

function buildScheduledHTML() {
    return `
    <div class="controls">
        <div class="card">
            <div class="card-header" style="flex-direction:column;align-items:stretch;gap:10px">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <h2><span class="icon">📅</span> Cron Schedules</h2>
                    <button class="icon-btn accent" onclick="showNewScheduleForm()" title="New Schedule">➕</button>
                </div>
                <div class="search-wrapper">
                    <input type="text" id="schedule-search" placeholder="Search schedules..." oninput="refreshSchedules()" style="width:100%">
                    <button id="clear-schedule-search" class="clear-btn" onclick="clearScheduleSearch()" style="display:none">✕</button>
                </div>
            </div>
            <div id="new-schedule-form" class="card form-card" style="display:none;margin-bottom:20px">
                <h3>Add New Schedule</h3>
                <div class="form-group">
                    <input type="text" id="sched-label" placeholder="Label">
                    <input type="text" id="sched-cmd" placeholder="Command">
                    <input type="text" id="sched-cron" placeholder="Cron (e.g. 0 10 * * *)">
                </div>
                <div class="form-actions">
                    <button class="small-btn" onclick="hideNewScheduleForm()">Cancel</button>
                    <button class="small-btn accent" onclick="saveNewSchedule()">Save</button>
                </div>
            </div>
            <div id="schedule-list" class="schedule-list"></div>
        </div>
    </div>`;
}

// --- Routing ---
window.addEventListener('hashchange', handleHash);
window.onpopstate = handleRoute;

function handleHash() {
    const hash = (window.location.hash || '').replace('#', '');
    document.querySelectorAll('.tab-link').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.style.display = 'none');

    // Find matching tab
    const allTabs = [...window.dashExtensions.tabs,
    { id: 'commands', label: 'Commands', onActivate: loadCommands },
    { id: 'scheduled', label: 'Scheduled', onActivate: refreshSchedules }];

    let matched = allTabs.find(t => t.label === hash);
    if (!matched) matched = allTabs[0]; // Default to first tab

    const tabEl = document.getElementById(`tab-${matched.id}`);
    const viewEl = document.getElementById(`view-${matched.id}`);
    if (tabEl) tabEl.classList.add('active');
    if (viewEl) viewEl.style.display = 'block';
    if (matched.onActivate) matched.onActivate();
}

function handleRoute() {
    const path = window.location.pathname;
    if (path.startsWith('/job/')) {
        const jobId = parseInt(path.split('/')[2]);
        if (jobId && activeJobId !== jobId) { currentOffset = 0; showJob(jobId); }
    }
    handleHash();
}

function navigateTo(path) { window.history.pushState({}, '', path); handleRoute(); }

// --- Commands ---
async function loadCommands() {
    try {
        const r = await fetch('/static/config/commands.json');
        const categories = await r.json();
        const container = document.getElementById('command-cards');
        const datalist = document.getElementById('command-presets');
        if (!container) return;
        container.innerHTML = '';
        if (datalist) datalist.innerHTML = '';

        categories.forEach(cat => {
            const card = document.createElement('div');
            card.className = 'card';
            const title = document.createElement('h2');
            title.innerHTML = `<span class="icon">${cat.icon}</span> ${cat.title || cat.label}`;
            card.appendChild(title);

            if (cat.commands) {
                const bg = document.createElement('div');
                bg.className = 'button-group';
                cat.commands.forEach(cmd => {
                    const btn = document.createElement('button');
                    btn.textContent = cmd.label;
                    btn.title = cmd.command;
                    btn.onclick = () => runCommand(cmd.command, cmd.description || cmd.label);
                    bg.appendChild(btn);
                    if (datalist) { const o = document.createElement('option'); o.value = cmd.command; datalist.appendChild(o); }
                });
                card.appendChild(bg);
            } else if (cat.command) {
                if (cat.description) { const p = document.createElement('p'); p.style.cssText = 'font-size:0.8rem;color:var(--text-secondary);margin-bottom:15px'; p.textContent = cat.description; card.appendChild(p); }
                const btn = document.createElement('button');
                btn.className = 'accent';
                btn.textContent = 'Run Command';
                btn.onclick = () => runCommand(cat.command, cat.label);
                card.appendChild(btn);
                if (datalist) { const o = document.createElement('option'); o.value = cat.command; datalist.appendChild(o); }
            }
            container.appendChild(card);
        });
    } catch (e) { console.error('Error loading commands:', e); }
}

// --- Run Command ---
async function runCommand(command, label, isCron = 0) {
    // Command prefixing is now handled server-side via WEB_UI_JOB_EXEC

    // Apply header option flags
    const appConfig = window._appConfig || {};
    (appConfig.header_options || []).forEach(opt => {
        const cb = document.getElementById(`${opt.id}-option`);
        if (cb && cb.checked && opt.flag && !command.includes(` ${opt.flag}`) && !command.includes(` --${opt.id}`))
            command += ` ${opt.flag}`;
    });

    try {
        const r = await fetch('/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command, is_cron: isCron }) });
        const data = await r.json();
        if (data.job_id) { 
            currentOffset = 0; 
            // SSE will handle the log streaming and history refresh
            showJob(data.job_id, label); 
        }
    } catch (e) { console.error('Error running command:', e); alert('Failed to start command'); }
}

function runCustomCommand() {
    const input = document.getElementById('custom-command');
    if (!input || !input.value.trim()) return;
    runCommand(input.value.trim(), 'Custom Command');
    input.value = '';
}

// Enter key for custom command
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => {
        const ci = document.getElementById('custom-command');
        if (ci) ci.addEventListener('keypress', e => { if (e.key === 'Enter') runCustomCommand(); });
    }, 100);
});

// --- Unified SSE ---
function setupSSE() {
    if (window._sse) window._sse.close();
    const sse = new EventSource('/api/events');
    window._sse = sse;

    sse.addEventListener('jobs', (e) => { 
        refreshHistory(); 
    });
    sse.addEventListener('schedules', (e) => { 
        refreshSchedules(); 
    });
    sse.addEventListener('log', (e) => {
        const data = JSON.parse(e.data);
        if (activeJobId === data.job_id) {
            const con = document.getElementById('console');
            if (con) {
                // If this is the first log line and we have a placeholder, clear it
                if (con.querySelector('.placeholder') || con.querySelector('.running-indicator')) con.textContent = '';
                
                const atBottom = con.scrollHeight - con.scrollTop <= con.clientHeight + 50;
                con.textContent += data.content;
                if (atBottom) con.scrollTop = con.scrollHeight;
                
                if (window.onJobLogUpdate) window.onJobLogUpdate(data, con.textContent);
            }
        }
    });

    sse.onopen = () => {
        console.log('SSE connection opened');
        document.getElementById('server-status').style.backgroundColor = 'var(--success-color)';
        document.getElementById('server-status').style.boxShadow = '0 0 8px var(--success-color)';
        document.getElementById('status-text').textContent = 'Connected';
    };

    sse.addEventListener('connected', () => {});
    sse.addEventListener('ping', () => {});

    sse.onerror = () => {
        console.warn('SSE connection lost. Retrying in 5s...');
        document.getElementById('server-status').style.backgroundColor = 'var(--error-color)';
        document.getElementById('server-status').style.boxShadow = '0 0 8px var(--error-color)';
        document.getElementById('status-text').textContent = 'Disconnected';
        sse.close();
        setTimeout(setupSSE, 5000);
    };
}

async function showJob(job_id, label) {
    if (window.location.pathname !== `/job/${job_id}`) window.history.pushState({}, '', `/job/${job_id}`);
    activeJobId = job_id;
    document.getElementById('active-job-id').textContent = `Viewing: ${label || `#${job_id}`}`;
    document.getElementById('close-console').style.display = 'block';
    const con = document.getElementById('console');
    con.textContent = 'Loading output...';
    currentOffset = 0;
    try {
        const lr = await fetch(`/api/job/${job_id}/log?offset=0`);
        const ld = await lr.json();
        const jobStatus = ld.job_status || 'unknown';
        con.textContent = ld.content || (jobStatus === 'running' ? 'Initializing...' : 'No output captured.');
        con.scrollTop = con.scrollHeight;
        if (window.onJobLogUpdate) window.onJobLogUpdate(ld, con.textContent);
    } catch (e) { console.error('Error showing job:', e); }
}

function closeConsole() {
    activeJobId = null;
    document.getElementById('active-job-id').textContent = 'Ready';
    document.getElementById('close-console').style.display = 'none';
    document.getElementById('console').innerHTML = '<div class="placeholder">Select a task to see output...</div>';
    if (window.onConsoleClose) window.onConsoleClose();
    if (window.location.pathname !== '/') window.history.pushState(null, '', '/');
}


// --- History ---
async function refreshHistory() {
    try {
        const r = await fetch('/api/jobs');
        const jobs = await r.json();
        renderHistory(jobs);
    } catch (e) { console.error('Error refreshing history:', e); }
}

function renderHistory(jobs) {
    const showCronEl = document.getElementById('show-cron');
    const showCron = showCronEl ? showCronEl.checked : true;
    const searchInput = document.getElementById('history-search');
    const q = searchInput ? searchInput.value.toLowerCase().trim() : '';
    const clearBtn = document.getElementById('clear-search');
    if (clearBtn) clearBtn.style.display = q ? 'block' : 'none';

    const filtered = jobs.filter(j => (showCron || !j.is_cron) && (!q || j.command.toLowerCase().includes(q)));
    const hl = document.getElementById('history-list');
    if (!hl) return;
    hl.innerHTML = '';

    filtered.forEach(job => {
        const item = document.createElement('div');
        item.className = 'history-item';
        if (job.is_cron) item.classList.add('cron-job');
        item.title = job.command;
        item.onclick = () => { currentOffset = 0; showJob(job.id, job.command); };
        const d = new Date(job.created_at + 'Z');
        const timeStr = d.toLocaleTimeString();
        const dateStr = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        let cmdDisplay = job.command;
        const cronTag = job.is_cron ? '<span class="cron-badge">CRON</span>' : '';
        const runCmdEsc = job.command.replace(/'/g, "\\'").replace(/"/g, '&quot;');
        item.innerHTML = `
                <div class="job-info" onclick="showJob(${job.id})">
                    <span class="job-time">${dateStr} ${timeStr}</span>
                    <span class="job-cmd">${cmdDisplay}</span>${cronTag}
                    <span class="status-badge status-${job.status}">${job.status}</span>
                </div>
                <div class="job-actions">
                    <button class="action-job-btn rerun-btn" onclick="runCommand('${runCmdEsc}','Rerun',${job.is_cron ? 1 : 0});event.stopPropagation()" title="Rerun">▶</button>
                    <button class="action-job-btn delete-btn" onclick="deleteJob(${job.id},'${job.status}');event.stopPropagation()" title="Delete">✕</button>
                </div>`;
        hl.appendChild(item);
    });
}

async function deleteJob(id, status) {
    let msg = 'Delete logs for this job?';
    if (status === 'running') {
        msg = 'Warning: This job is still running. Terminating it now may lead to data loss. Kill process and delete logs?';
    } else if (status === 'pending') {
        msg = 'Delete this pending job?';
    }

    if (!confirm(msg)) return;
    try {
        await fetch(`/api/job/${id}`, { method: 'DELETE' });
        if (activeJobId === id) closeConsole();
        refreshHistory();
    } catch (e) { console.error('Error deleting job:', e); }
}

function clearSearch() { const s = document.getElementById('history-search'); if (s) s.value = ''; refreshHistory(); }
function resetFilters() { clearSearch(); const sc = document.getElementById('show-cron'); if (sc) sc.checked = false; refreshHistory(); }
function filterHistoryByCron(cmd) { const sc = document.getElementById('show-cron'); if (sc) sc.checked = true; const s = document.getElementById('history-search'); if (s) s.value = cmd; refreshHistory(); }

// --- Schedules ---
let editingScheduleId = null;

async function refreshSchedules() {
    try {
        const r = await fetch('/api/schedules');
        const schedules = await r.json();
        renderSchedules(schedules);
    } catch (e) { console.error('Error refreshing schedules:', e); }
}

function renderSchedules(schedules) {
    const si = document.getElementById('schedule-search');
    if (si) {
        const q = si.value.toLowerCase().trim();
        const cb = document.getElementById('clear-schedule-search');
        if (cb) cb.style.display = q ? 'block' : 'none';
        if (q) schedules = schedules.filter(s => s.label.toLowerCase().includes(q) || s.command.toLowerCase().includes(q));
    }
    const list = document.getElementById('schedule-list');
    if (!list) return;
    list.innerHTML = '';
    schedules.forEach(s => {
        const item = document.createElement('div');
        item.className = 'schedule-item';
        if (!s.enabled) item.classList.add('disabled');
        const isEdit = editingScheduleId === s.id;
        const dc = s.command;
        let nrs = '';
        if (s.enabled && s.next_run_iso) {
            const nd = new Date(s.next_run_iso);
            const ts = nd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const dm = s.minutes_until;
            let rs = dm === 0 ? 'due now' : dm < 60 ? `in ${dm}m` : dm < 1440 ? `in ${Math.floor(dm / 60)}h ${dm % 60}m` : `in ${Math.floor(dm / 1440)}d`;
            nrs = `<div class="schedule-subtitle">Next run: ${ts} (${rs})</div>`;
        }
        item.innerHTML = `
                ${s.catch_up ? '<div class="catch-up-overlay" title="Catch Up Enabled">⚡</div>' : ''}
                <div class="schedule-info" onclick="filterHistoryByCron('${dc}')">
                    <div class="schedule-label">${isEdit ? `<input type="text" id="edit-label-${s.id}" value="${s.label}" onkeypress="handleEditKP(event,${s.id})">` : s.label}</div>
                    ${nrs}
                    <div class="schedule-meta">${isEdit ? `<input type="text" id="edit-cron-${s.id}" value="${s.cron_expr}" onkeypress="handleEditKP(event,${s.id})"><input type="text" id="edit-cmd-${s.id}" value="${dc}" onkeypress="handleEditKP(event,${s.id})">` : `${s.cron_expr} • ${dc}`}</div>
                </div>
                <div class="schedule-actions"><div class="kebab-menu"><button class="icon-btn">⋮</button><div class="dropdown-content">
                    <div class="dropdown-item" onclick="runCommand('${s.command}','${s.label}',1)">Run Now</div>
                    <div class="dropdown-item" onclick="${isEdit ? `saveSchedule(${s.id})` : `startEditSchedule(${s.id})`}">${isEdit ? 'Save' : 'Edit'}</div>
                    <div class="dropdown-item" onclick="toggleSchedule(${s.id},${s.enabled})">${s.enabled ? 'Deactivate' : 'Activate'}</div>
                    <div class="dropdown-item" onclick="toggleCatchUp(${s.id},${s.catch_up || 0})">${s.catch_up ? 'Disable Catch Up' : 'Enable Catch Up'}</div>
                    <div class="dropdown-item warning" onclick="deleteSchedule(${s.id})">Delete</div>
                </div></div></div>`;
        list.appendChild(item);
    });
}

function startEditSchedule(id) { editingScheduleId = id; refreshSchedules(); }
async function saveSchedule(id) {
    const l = document.getElementById(`edit-label-${id}`).value.trim();
    const c = document.getElementById(`edit-cron-${id}`).value.trim();
    const cmd = document.getElementById(`edit-cmd-${id}`).value.trim();
    if (!l || !c || !cmd) return;
    await fetch(`/api/schedules/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: l, cron_expr: c, command: cmd }) });
    editingScheduleId = null;
    refreshSchedules();
}
function handleEditKP(e, id) { if (e.key === 'Enter') saveSchedule(id); }
function showNewScheduleForm() { document.getElementById('new-schedule-form').style.display = 'block'; }
function hideNewScheduleForm() { document.getElementById('new-schedule-form').style.display = 'none'; }
async function saveNewSchedule() {
    const l = document.getElementById('sched-label').value.trim();
    const cmd = document.getElementById('sched-cmd').value.trim();
    const c = document.getElementById('sched-cron').value.trim();
    if (!l || !cmd || !c) { alert('All fields are required'); return; }
    await fetch('/api/schedules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label: l, command: cmd, cron_expr: c }) });
    hideNewScheduleForm();
    refreshSchedules();
}
function clearScheduleSearch() { const s = document.getElementById('schedule-search'); if (s) { s.value = ''; refreshSchedules(); } }
async function toggleSchedule(id, cur) { await fetch(`/api/schedules/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !cur }) }); refreshSchedules(); }
async function toggleCatchUp(id, cur) { await fetch(`/api/schedules/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ catch_up: !cur }) }); refreshSchedules(); }
async function deleteSchedule(id) { if (!confirm('Delete this schedule?')) return; await fetch(`/api/schedules/${id}`, { method: 'DELETE' }); refreshSchedules(); }

// --- Resizer ---
document.addEventListener('DOMContentLoaded', () => {
    const resizer = document.getElementById('drag-resizer');
    const main = document.querySelector('main');
    if (!resizer || !main) return;
    let dragging = false;
    resizer.addEventListener('mousedown', () => { dragging = true; resizer.classList.add('dragging'); document.body.style.cursor = 'col-resize'; });
    document.addEventListener('mousemove', e => {
        if (!dragging) return;
        const pct = ((e.clientX - main.getBoundingClientRect().left) / main.clientWidth) * 100;
        if (pct > 10 && pct < 90) main.style.gridTemplateColumns = `${pct}% 0px 1fr`;
    });
    document.addEventListener('mouseup', () => { if (dragging) { dragging = false; resizer.classList.remove('dragging'); document.body.style.cursor = 'default'; } });
});

// --- Server status ---
async function checkStatus() {
    try { await fetch('/api/jobs'); document.getElementById('server-status').style.backgroundColor = 'var(--success-color)'; document.getElementById('server-status').style.boxShadow = '0 0 8px var(--success-color)'; document.getElementById('status-text').textContent = 'Connected'; }
    catch (e) { document.getElementById('server-status').style.backgroundColor = 'var(--error-color)'; document.getElementById('server-status').style.boxShadow = '0 0 8px var(--error-color)'; document.getElementById('status-text').textContent = 'Disconnected'; }
}

// Store config globally for runCommand flag logic
(async () => { try { const r = await fetch('/static/config/app.json'); if (r.ok) window._appConfig = await r.json(); } catch (e) { } })();
