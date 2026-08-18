/* cli-dash Vue 3 app.js */
;(function() {
const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, onUnmounted, nextTick, markRaw } = Vue;

window.dashExtensions = { tabs: [], headerOptions: [], onInit: [] };

window.showToast = function(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) {
        alert(message);
        return;
    }
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    
    let icon = '';
    if (type === 'success') icon = '✓';
    else if (type === 'error') icon = '⚠';
    else if (type === 'warning') icon = '⚠';
    else icon = 'ℹ';
    
    toast.innerHTML = `<span class="toast-icon">${icon}</span><span class="toast-message">${message}</span>`;
    
    container.appendChild(toast);
    
    // Trigger reflow for transition
    void toast.offsetWidth;
    toast.classList.add('show');
    
    setTimeout(() => {
        toast.classList.remove('show');
        toast.addEventListener('transitionend', () => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        });
    }, 3000);
};

window.registerTab = function(tab) { window.dashExtensions.tabs.push(tab); }
window.registerHeaderOption = function(opt) { window.dashExtensions.headerOptions.push(opt); }

const DEFAULT_PALETTE = [
    '#3080f4', // Blue
    '#2ea043', // Green
    '#8250df', // Purple
    '#d29922', // Orange/Yellow
    '#f85149', // Red
    '#db61a2', // Pink
    '#00a3a6'  // Teal
];

window.queuesList = [];

window.parseQueues = function(queuesData) {
    if (!queuesData || !Array.isArray(queuesData)) {
        window.queuesList = [];
        return;
    }
    window.queuesList = queuesData.map((item, index) => {
        const defaultColor = DEFAULT_PALETTE[index % DEFAULT_PALETTE.length];
        if (typeof item === 'string') {
            return { name: item, color: defaultColor };
        } else if (item && typeof item === 'object') {
            return { name: item.name || '', color: item.color || defaultColor };
        }
        return { name: '', color: defaultColor };
    });
};

window.getQueueColor = function(name) {
    if (!name) return '#0969da'; // Default fallback
    const found = window.queuesList && window.queuesList.find(q => q.name === name);
    if (found) return found.color;
    
    // Hash the name to consistently pick a color from the palette
    let hash = 0;
    for (let i = 0; i < name.length; i++) {
        hash = name.charCodeAt(i) + ((hash << 5) - hash);
    }
    const index = Math.abs(hash) % DEFAULT_PALETTE.length;
    return DEFAULT_PALETTE[index];
};

// Define Vue app
const App = {
    setup() {
        const appConfig = ref({});
        const connected = ref(false);
        const headerState = reactive({});
        const allHeaderOptions = ref([]);
        const mcpServers = ref([]);

        const currentTab = ref('commands');
        const tabs = ref([]);
        
        const showMonitor = ref(false);
        const isSwiping = ref(false);
        
        const activeJobId = ref(null);
        const activeJobLabel = ref('');
        const activeJob = ref(null);
        const consoleContent = ref('');
        
        const formattedConsoleContent = computed(() => {
            if (!consoleContent.value) return '';
            const el = document.createElement('div');
            el.innerText = consoleContent.value;
            let html = el.innerHTML;
            const urlRegex = /\b((?:https?|ftp|file):\/\/[-A-Z0-9+&@#\/%?=~_|!:,.;]*[-A-Z0-9+&@#\/%=~_|]|magnet:\?[^\s"'<>]+)/gi;
            return html.replace(urlRegex, (match) => {
                return `<a href="${match}" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: underline;">${match}</a>`;
            });
        });

        const logComponent = ref(null);
        
        const modal = reactive({
            show: false,
            html: '',
            component: null,
            props: {}
        });

        // Touch handling
        let touchStartX = 0, touchStartY = 0, currentX = 0, swiping = false;

        const onTouchStart = (e) => {
            if (window.innerWidth > 768) return;
            const target = e.target;
            if (target.closest('.console') || target.closest('.tabs')) return;
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
            swiping = true;
            isSwiping.value = true;
        };

        const onTouchMove = (e) => {
            if (!swiping || window.innerWidth > 768) return;
            const x = e.touches[0].clientX;
            const y = e.touches[0].clientY;
            const dx = x - touchStartX;
            const dy = y - touchStartY;
            
            if (Math.abs(dy) > Math.abs(dx)) {
                swiping = false;
                isSwiping.value = false;
                return;
            }
            currentX = x;
            let offset = dx;
            if (showMonitor.value) {
                offset = -window.innerWidth + dx;
                if (offset > 0) offset = 0;
            } else {
                if (offset < -window.innerWidth) offset = -window.innerWidth;
                if (offset > 0) offset = 0;
            }
            document.getElementById('tab-views').style.transform = `translateX(${offset}px)`;
            document.getElementById('monitor-section').style.transform = `translateX(${offset}px)`;
        };

        const onTouchEnd = (e) => {
            if (!swiping || window.innerWidth > 768) return;
            swiping = false;
            isSwiping.value = false;
            document.getElementById('tab-views').style.transform = '';
            document.getElementById('monitor-section').style.transform = '';

            const dx = currentX - touchStartX;
            if (Math.abs(dx) > 50) {
                if (dx < 0 && !showMonitor.value) showMonitor.value = true;
                else if (dx > 0 && showMonitor.value) showMonitor.value = false;
            }
        };

        const startResize = (e) => {
            if (window.innerWidth <= 768) return;
            e.preventDefault();
            const startX = e.clientX;
            const main = document.querySelector('main');
            const startWidth = document.getElementById('tab-views').getBoundingClientRect().width;
            const totalWidth = main.getBoundingClientRect().width;

            const onMouseMove = (moveEvent) => {
                const newWidth = startWidth + (moveEvent.clientX - startX);
                const percent = (newWidth / totalWidth) * 100;
                if (percent > 20 && percent < 80) {
                    main.style.gridTemplateColumns = `${percent}% 0px 1fr`;
                }
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                document.body.style.cursor = 'default';
                document.getElementById('drag-resizer').classList.remove('dragging');
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
            document.body.style.cursor = 'col-resize';
            document.getElementById('drag-resizer').classList.add('dragging');
        };

        // Modals
        const closeModal = () => { modal.show = false; modal.html = ''; modal.component = null; modal.props = {}; };
        window.showModalHtml = (html) => { modal.html = html; modal.component = null; modal.show = true; };
        window.showModalComponent = (comp, props) => { modal.component = comp; modal.props = props; modal.html = ''; modal.show = true; };
        window.closeModals = closeModal;

        // Routing
        const handleHash = () => {
            const hash = (window.location.hash || '').replace('#', '');
            const matched = tabs.value.find(t => t.label === hash) || tabs.value[0];
            if (matched) currentTab.value = matched.id;
        };
        const handleRoute = () => {
            const path = window.location.pathname;
            if (path.startsWith('/job/')) {
                const parts = path.split('/');
                const jobId = parts[2];
                if (jobId === 'help') {
                    showHelpConsole();
                } else {
                    const parsedId = parseInt(jobId);
                    if (parsedId && activeJobId.value !== parsedId) {
                        showJob({ id: parsedId, command: 'Job #' + parsedId });
                    }
                }
            }
            handleHash();
        };
        window.addEventListener('hashchange', handleHash);
        window.onpopstate = handleRoute;

        const switchTab = (tab) => {
            currentTab.value = tab.id;
            if (window.innerWidth <= 768) showMonitor.value = false;
        };

        const activeTab = computed(() => tabs.value.find(t => t.id === currentTab.value));
        const activeTabComponent = computed(() => activeTab.value ? activeTab.value.component : null);
        const monitorHidden = computed(() => activeTab.value && activeTab.value.hideMonitor);

        // SSE
        const setupSSE = () => {
            if (window._sse) window._sse.close();
            const sse = new EventSource('/api/events');
            window._sse = sse;
            
            sse.addEventListener('jobs', () => { window.dispatchEvent(new Event('refresh-history')); });
            sse.addEventListener('schedules', () => { window.dispatchEvent(new Event('refresh-schedules')); });
            sse.addEventListener('log', (e) => {
                const data = JSON.parse(e.data);
                if (activeJobId.value === data.job_id) {
                    const con = document.getElementById('console');
                    if (con) {
                        const atBottom = con.scrollHeight - con.scrollTop <= con.clientHeight + 50;
                        consoleContent.value += data.content;
                        if (atBottom) {
                            nextTick(() => { con.scrollTop = con.scrollHeight; });
                        }
                    }
                }
            });
            sse.onopen = () => { connected.value = true; };
            sse.onerror = () => {
                connected.value = false;
                sse.close();
                setTimeout(setupSSE, 5000);
            };
        };

        const showJob = async (job) => {
            if (window.location.pathname !== `/job/${job.id}`) window.history.pushState({}, '', `/job/${job.id}`);
            activeJobId.value = job.id;
            activeJobLabel.value = `[Job ${job.id}] ${job.command || 'Loading...'}`;
            activeJob.value = job;
            consoleContent.value = 'Loading output...';
            if (window.innerWidth <= 768) showMonitor.value = true;
            
            try {
                // Always fetch job details to get the authoritative command line from the DB
                const jr = await fetch(`/api/job/${job.id}`);
                if (jr.ok) {
                    const fullJob = await jr.json();
                    activeJobLabel.value = `[Job ${fullJob.id}] ${fullJob.command}`;
                    activeJob.value = fullJob;
                }

                const r = await fetch(`/api/job/${job.id}/log?offset=0`);
                const ld = await r.json();
                consoleContent.value = ld.content || (ld.job_status === 'running' ? 'Initializing...' : 'No output captured.');
                nextTick(() => {
                    const con = document.getElementById('console');
                    if(con) con.scrollTop = con.scrollHeight;
                });
            } catch (e) { console.error('Error showing job:', e); }
        };

        const showHelpConsole = async () => {
            if (window.location.pathname !== `/job/help`) window.history.pushState({}, '', `/job/help`);
            activeJobId.value = 'help';
            activeJobLabel.value = 'Help / Documentation';
            activeJob.value = { id: 'help', command: 'Help' };
            consoleContent.value = 'Loading help documentation...';
            if (window.innerWidth <= 768) showMonitor.value = true;
            
            try {
                const r = await fetch('/api/help');
                if (r.ok) {
                    const data = await r.json();
                    consoleContent.value = data.content || 'No help documentation returned.';
                } else {
                    consoleContent.value = 'Failed to load help documentation.';
                }
                nextTick(() => {
                    const con = document.getElementById('console');
                    if(con) con.scrollTop = 0; // Scroll to top for documentation
                });
            } catch (e) {
                consoleContent.value = 'Error loading help documentation: ' + e;
            }
        };

        const copyCommand = async () => {
            if (activeJob.value && activeJob.value.command) {
                try {
                    await navigator.clipboard.writeText(activeJob.value.command);
                    window.showToast('Copied command to clipboard', 'success');
                } catch (err) {
                    window.showToast('Failed to copy command', 'error');
                }
            } else {
                window.showToast('No command available', 'error');
            }
        };

        const copyLogPath = async () => {
            if (activeJob.value && activeJob.value.log_path) {
                try {
                    await navigator.clipboard.writeText(activeJob.value.log_path);
                    window.showToast('Copied log path to clipboard', 'success');
                } catch (err) {
                    window.showToast('Failed to copy log path', 'error');
                }
            } else {
                window.showToast('No log path available for this job', 'error');
            }
        };

        const closeConsole = () => {
            activeJobId.value = null;
            activeJobLabel.value = '';
            activeJob.value = null;
            consoleContent.value = '';
            if (window.location.pathname !== '/') window.history.pushState(null, '', '/');
            if (window.innerWidth <= 768) showMonitor.value = false;
        };

        window.runCommand = async (command, label, isCron = 0, jobType = 'command', queue = null, env = null, job_exec = null) => {
            appConfig.value.header_options?.forEach(opt => {
                if (headerState[opt.id] && opt.flag && !command.includes(` ${opt.flag}`) && !command.includes(` --${opt.id}`))
                    command += ` ${opt.flag}`;
            });
            try {
                const r = await fetch('/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command, is_cron: isCron, job_type: jobType, queue, env, job_exec }) });
                const data = await r.json();
                if (data.job_id) showJob({ id: data.job_id, command: command, job_type: jobType, queue_name: queue, env, job_exec });
            } catch (e) { window.showToast('Failed to start command', 'error'); }
        };
        
        window.showJobGlobal = showJob;

        const saveHeaderState = (id) => {
            localStorage.setItem(id, headerState[id]);
        };

        onMounted(async () => {
            // Load app config
            try { const r = await fetch('/static/config/app.json'); if (r.ok) appConfig.value = await r.json(); } catch(e){}
            try { const r = await fetch('/api/config'); if (r.ok) { const d = await r.json(); if(d.title) document.title = d.title; if(d.mcp_servers) mcpServers.value = d.mcp_servers; } } catch(e){}
            try {
                const r = await fetch(`/static/config/commands.json?t=${Date.now()}`);
                if (r.ok) {
                    const data = await r.json();
                    const qList = (data && typeof data === 'object') ? (data.queues || []) : [];
                    window.parseQueues(qList);
                }
            } catch(e){}
            
            // Apply header options
            const opts = appConfig.value.header_options || [];
            opts.push(...window.dashExtensions.headerOptions);
            allHeaderOptions.value = opts;
            opts.forEach(opt => {
                headerState[opt.id] = localStorage.getItem(opt.id) === 'true';
            });
            
            // Build tabs
            const extTabs = window.dashExtensions.tabs.map(t => ({
                ...t,
                component: t.component ? (typeof t.component === 'object' ? markRaw(t.component) : t.component) : { template: '<div>No Vue component provided for tab ' + t.label + '</div>' }
            }));
            
            // Find if rclone progress is registered as log component
            if (window.dashExtensions.logComponent) {
                logComponent.value = window.dashExtensions.logComponent;
            }
            
            const allTabs = [
                ...extTabs.map(t => ({...t, order: t.order !== undefined ? t.order : 0})),
                { id: 'commands', label: 'Commands', component: 'CommandsView', order: 100 },
                { id: 'scheduled', label: 'Scheduled', component: 'ScheduledView', order: 110 }
            ];
            
            tabs.value = allTabs.sort((a, b) => a.order - b.order);

            window.dashExtensions.onInit.forEach(fn => fn());

            handleRoute();
            setupSSE();
            
            document.addEventListener('visibilitychange', () => {
                if (document.hidden) {
                    if (window._sse) { window._sse.close(); window._sse = null; }
                } else {
                    setupSSE();
                    window.dispatchEvent(new Event('refresh-history'));
                    window.dispatchEvent(new Event('refresh-schedules'));
                }
            });

            window.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && modal.show) {
                    closeModal();
                }
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
                    e.preventDefault();
                    if (modal.show && modal.component === 'LogSearchModal') {
                        closeModal();
                    } else {
                        window.showModalComponent('LogSearchModal');
                    }
                }
            });
        });

        const getOptionIcon = (name) => {
            const icons = {
                shield: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM12 4.1l6 2.25v5.65c0 4.16-5.06 7.42-6 8-1-.58-6-3.84-6-8V6.35l6-2.25z"/></svg>',
                bolt: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M7 2v11h3v9l7-12h-4l4-8z"/></svg>',
                sync: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg>',
                info: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>'
            };
            return icons[name] || '';
        };

        const toggleHeaderOption = (id) => {
            headerState[id] = !headerState[id];
            saveHeaderState(id);
        };

        const restartService = async (name) => {
            try {
                const r = await fetch(`/api/services/${encodeURIComponent(name)}/restart`, { method: 'POST' });
                if (r.ok) {
                    window.showToast(`Service '${name}' is restarting...`, 'info');
                } else {
                    const data = await r.json().catch(() => ({}));
                    window.showToast(`Failed to restart service '${name}': ${data.detail || 'Unknown error'}`, 'error');
                }
            } catch(e) {
                window.showToast(`Error restarting service: ${e}`, 'error');
            }
        };

        return {
            appConfig, mcpServers, connected, headerState, allHeaderOptions, saveHeaderState,
            tabs, currentTab, switchTab, activeTabComponent, monitorHidden,
            showMonitor, isSwiping, onTouchStart, onTouchMove, onTouchEnd, startResize,
            activeJobId, activeJobLabel, activeJob, consoleContent, formattedConsoleContent, closeConsole, copyLogPath, copyCommand, showHelpConsole,
            modal, closeModal, logComponent, getOptionIcon, toggleHeaderOption, restartService
        };
    }
};

// Global Components
const CommandsView = {
    template: '#tpl-commands',
    setup() {
        const commandCategories = ref([]);
        const customCmd = ref('');
        const presets = ref([]);
        const queues = ref([]);
        
        const normalizedQueues = computed(() => {
            return queues.value.map((item, index) => {
                const defaultColor = DEFAULT_PALETTE[index % DEFAULT_PALETTE.length];
                if (typeof item === 'string') {
                    return { name: item, color: defaultColor };
                } else if (item && typeof item === 'object') {
                    return { name: item.name || '', color: item.color || defaultColor };
                }
                return { name: '', color: defaultColor };
            });
        });
        
        const loadCommands = async () => {
            try {
                const r = await fetch(`/static/config/commands.json?t=${Date.now()}`);
                if (r.ok) {
                    const data = await r.json();
                    if (Array.isArray(data)) {
                        commandCategories.value = data;
                        queues.value = [];
                    } else if (data && typeof data === 'object') {
                        commandCategories.value = data.commands || [];
                        queues.value = data.queues || [];
                    }
                    window.parseQueues(queues.value);
                    presets.value = [];
                    commandCategories.value.forEach(cat => {
                        if(cat.commands) cat.commands.forEach(c => presets.value.push(c.command));
                        else if(cat.command) presets.value.push(cat.command);
                    });
                }
            } catch (e) {}
        };
        
        onMounted(() => {
            loadCommands();
        });
        
        const runCustom = () => {
            if(customCmd.value.trim()) {
                window.runCommand(customCmd.value.trim(), 'Custom Command');
                customCmd.value = '';
            }
        };

        const filterHistory = (cmd) => {
            window.dispatchEvent(new CustomEvent('filter-history', { detail: cmd }));
        };

        return { commandCategories, customCmd, presets, queues, normalizedQueues, runCustom, loadCommands, runCommand: window.runCommand, getQueueColor: window.getQueueColor, filterHistory };
    }
};

const ScheduledView = {
    template: '#tpl-scheduled',
    setup() {
        const schedules = ref([]);
        const searchQuery = ref('');
        const showNew = ref(false);
        const newSched = reactive({ label: '', command: '', cron_expr: '', queue: '' });
        const editingId = ref(null);
        const editData = reactive({ label: '', command: '', cron_expr: '', queue: '' });

        const fetchSchedules = async () => {
            try {
                const r = await fetch('/api/schedules');
                schedules.value = await r.json();
            } catch (e) {}
        };

        onMounted(() => {
            fetchSchedules();
            window.addEventListener('refresh-schedules', fetchSchedules);
        });

        onUnmounted(() => {
            window.removeEventListener('refresh-schedules', fetchSchedules);
        });

        const filteredSchedules = computed(() => {
            const q = searchQuery.value.toLowerCase().trim();
            if (!q) return schedules.value;
            return schedules.value.filter(s => s.label.toLowerCase().includes(q) || s.command.toLowerCase().includes(q));
        });

        const saveNew = async () => {
            if(!newSched.label || !newSched.command || !newSched.cron_expr) { window.showToast('All fields required', 'warning'); return; }
            await fetch('/api/schedules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(newSched) });
            showNew.value = false;
            newSched.label = ''; newSched.command = ''; newSched.cron_expr = ''; newSched.queue = '';
            fetchSchedules();
        };

        const startEdit = (s) => {
            editingId.value = s.id;
            editData.label = s.label;
            editData.command = s.command;
            editData.cron_expr = s.cron_expr;
            editData.queue = s.queue_name || '';
        };

        const saveEdit = async (id) => {
            if(!editData.label || !editData.command || !editData.cron_expr) return;
            await fetch(`/api/schedules/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(editData) });
            editingId.value = null;
            fetchSchedules();
        };

        const deleteSchedule = async (id) => {
            if(!confirm('Delete this schedule?')) return;
            await fetch(`/api/schedules/${id}`, { method: 'DELETE' });
            fetchSchedules();
        };
        const toggleEnabled = async (s) => { await fetch(`/api/schedules/${s.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !s.enabled }) }); fetchSchedules(); };
        const toggleCatchUp = async (s) => { await fetch(`/api/schedules/${s.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ catch_up: !s.catch_up }) }); fetchSchedules(); };
        
        const formatNextRun = (s) => {
            const nd = new Date(s.next_run_iso);
            const ts = nd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const dm = s.minutes_until;
            let rs = dm === 0 ? 'due now' : dm < 60 ? `in ${dm}m` : dm < 1440 ? `in ${Math.floor(dm / 60)}h ${dm % 60}m` : `in ${Math.floor(dm / 1440)}d`;
            return `${ts} (${rs})`;
        };

        const runSchedule = (s) => window.runCommand(s.command, s.label, 1, 'command', s.queue_name);
        const filterHistory = (cmd) => {
            window.dispatchEvent(new CustomEvent('filter-history', { detail: cmd }));
        };

        const exportSchedules = async () => {
            try {
                const r = await fetch('/api/schedules/export', { method: 'POST' });
                if (r.ok) {
                    const data = await r.json();
                    window.showToast(`Exported successfully to ${data.path}`, 'success');
                } else {
                    window.showToast('Failed to export schedules', 'error');
                }
            } catch (e) {
                window.showToast('Error exporting schedules', 'error');
            }
        };

        const importSchedules = async () => {
            if (!confirm('Are you sure you want to import from schedules.json? This will OVERWRITE all current schedules!')) return;
            try {
                const r = await fetch('/api/schedules/import', { method: 'POST' });
                if (r.ok) {
                    window.showToast('Imported successfully', 'success');
                    fetchSchedules();
                } else {
                    const err = await r.json();
                    window.showToast('Failed to import: ' + (err.detail || 'Unknown error'), 'error');
                }
            } catch (e) {
                window.showToast('Error importing schedules', 'error');
            }
        };

        return {
            schedules, searchQuery, filteredSchedules, showNew, newSched, saveNew,
            editingId, editData, startEdit, saveEdit, deleteSchedule, toggleEnabled, toggleCatchUp, formatNextRun,
            runSchedule, filterHistory, exportSchedules, importSchedules, getQueueColor: window.getQueueColor
        };
    }
};

const HistoryView = {
    template: '#tpl-history',
    setup() {
        const jobs = ref([]);
        const searchQuery = ref('');
        const showCron = ref(true);
        const offset = ref(0);
        const limit = 50;
        const hasMore = ref(true);
        const loadingMore = ref(false);

        const fetchJobs = async (append = false) => {
            if (loadingMore.value) return;
            loadingMore.value = true;
            try {
                const r = await fetch(`/api/jobs?limit=${limit}&offset=${offset.value}`);
                const data = await r.json();
                if (data.length < limit) {
                    hasMore.value = false;
                }
                if (append) {
                    const existingIds = new Set(jobs.value.map(j => j.id));
                    data.forEach(j => {
                        if (!existingIds.has(j.id)) {
                            jobs.value.push(j);
                        }
                    });
                } else {
                    jobs.value = data;
                }
            } catch (e) {
                console.error('Error fetching jobs:', e);
            } finally {
                loadingMore.value = false;
            }
        };

        const loadMore = async () => {
            if (!hasMore.value || loadingMore.value) return;
            offset.value += limit;
            await fetchJobs(true);
        };

        const onScroll = (e) => {
            const el = e.target;
            if (el.scrollHeight - el.scrollTop <= el.clientHeight + 50) {
                loadMore();
            }
        };

        const handleRefresh = () => {
            offset.value = 0;
            hasMore.value = true;
            fetchJobs(false);
        };

        const handleFilter = (e) => {
            showCron.value = true;
            searchQuery.value = e.detail;
            document.getElementById('tab-views').scrollTop = 0;
        };

        onMounted(() => {
            fetchJobs();
            window.addEventListener('refresh-history', handleRefresh);
            window.addEventListener('filter-history', handleFilter);
        });

        onUnmounted(() => {
            window.removeEventListener('refresh-history', handleRefresh);
            window.removeEventListener('filter-history', handleFilter);
        });

        const filteredJobs = computed(() => {
            const q = searchQuery.value.toLowerCase().trim();
            return jobs.value.filter(j => (showCron.value || !j.is_cron) && (!q || j.command.toLowerCase().includes(q)));
        });

        const resetFilters = () => { searchQuery.value = ''; showCron.value = false; };
        
        const deleteJob = async (job) => {
            let msg = 'Delete logs for this job?';
            if(job.status === 'pending') msg = 'Delete this pending job?';
            if(!confirm(msg)) return;
            await fetch(`/api/job/${job.id}`, { method: 'DELETE' });
            handleRefresh();
        };

        const terminateJob = async (job) => {
            await fetch(`/api/job/${job.id}/terminate`, { method: 'POST' });
        };


        const runJobAgain = (job) => {
            let env = job.env;
            if (typeof env === 'string') try { env = JSON.parse(env); } catch(e){}
            window.runCommand(job.command, 'Rerun', job.is_cron ? 1 : 0, job.job_type, job.queue_name, env, job.job_exec);
        };

        const formatTime = (ts) => {
            const d = new Date(ts + 'Z');
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString();
        };

        const formatDuration = (seconds) => {
            if (typeof seconds === 'string') {
                seconds = seconds.trim() === '' ? null : Number(seconds);
            }

            // Catch null, undefined, or strings that failed to convert (which become NaN)
            if (seconds === null || seconds === undefined || isNaN(seconds)) return '';

            if (seconds < 60) return seconds.toFixed(2) + 's';
            
            const m = Math.floor(seconds / 60);
            const s = Math.floor(seconds % 60);
            return `${m}m ${s}s`;
        };

        window.showMonitorGlobal = (show = true) => {
            showMonitor.value = show;
        };

        return { jobs, searchQuery, showCron, filteredJobs, resetFilters, deleteJob, terminateJob, runJobAgain, formatTime, formatDuration, showJob: window.showJobGlobal, getQueueColor: window.getQueueColor, onScroll };
    }
};

const logSearchState = reactive({
    query: '',
    results: [],
    statusText: '',
    isSearching: false,
    selectedMatch: null,
    previewLoading: false,
    sidebarWidth: 35
});

const LogSearchModal = {
    template: '#tpl-log-search',
    setup(props, { emit }) {
        const searchInput = ref(null);
        const previewContainer = ref(null);
        let abortController = null;
        let debounceTimer = null;
        const historyKey = 'cli-dash-search-history';
        const searchHistory = ref([]);
        const historyIndex = ref(-1);

        onMounted(() => {
            try {
                const stored = localStorage.getItem(historyKey);
                if (stored) searchHistory.value = JSON.parse(stored);
            } catch (e) {}
            nextTick(() => {
                if (searchInput.value) {
                    searchInput.value.focus();
                }
            });
        });

        let historyDebounceTimer = null;

        const saveHistory = (q) => {
            if (!q) return;
            if (historyIndex.value >= 0 && searchHistory.value[historyIndex.value] === q) return;
            const idx = searchHistory.value.indexOf(q);
            if (idx !== -1) searchHistory.value.splice(idx, 1);
            searchHistory.value.unshift(q);
            if (searchHistory.value.length > 50) searchHistory.value.pop();
            try { localStorage.setItem(historyKey, JSON.stringify(searchHistory.value)); } catch(e){}
            historyIndex.value = -1;
        };

        const saveHistoryDebounced = (q) => {
            if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
            historyDebounceTimer = setTimeout(() => {
                saveHistory(q);
            }, 1500); // Only save if they stop typing for 1.5s
        };

        const onEnter = () => {
            if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
            const q = logSearchState.query.trim();
            if (q) saveHistory(q);
        };

        // Also save history when modal unmounts
        onBeforeUnmount(() => {
            if (historyDebounceTimer) clearTimeout(historyDebounceTimer);
            const q = logSearchState.query.trim();
            if (q) saveHistory(q);
        });

        const historyUp = () => {
            if (searchHistory.value.length === 0) return;
            if (historyIndex.value < searchHistory.value.length - 1) {
                historyIndex.value++;
                logSearchState.query = searchHistory.value[historyIndex.value];
                performSearch();
            }
        };

        const historyDown = () => {
            if (historyIndex.value > 0) {
                historyIndex.value--;
                logSearchState.query = searchHistory.value[historyIndex.value];
                performSearch();
            } else if (historyIndex.value === 0) {
                historyIndex.value = -1;
                logSearchState.query = '';
                performSearch();
            }
        };

        const highlight = (text) => {
            if (!logSearchState.query) return text;
            const q = logSearchState.query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const regex = new RegExp(`(${q})`, 'gi');
            let escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            return escaped.replace(regex, '<mark>$1</mark>');
        };

        const performSearch = async () => {
            if (!logSearchState.query.trim()) {
                logSearchState.results = [];
                logSearchState.statusText = '';
                logSearchState.selectedMatch = null;
                return;
            }

            if (abortController) {
                abortController.abort();
            }

            abortController = new AbortController();
            logSearchState.isSearching = true;
            logSearchState.results = [];
            logSearchState.statusText = 'Searching...';
            let matchCount = 0;
            saveHistoryDebounced(logSearchState.query.trim());

            try {
                const response = await fetch(`/api/logs/search?q=${encodeURIComponent(logSearchState.query.trim())}`, {
                    signal: abortController.signal
                });

                if (!response.ok) throw new Error('Search failed');

                const reader = response.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (line.trim()) {
                            try {
                                const result = JSON.parse(line);
                                logSearchState.results.push(result);
                                matchCount += result.matches.length;
                                logSearchState.statusText = `Found ${matchCount} matches...`;
                            } catch (e) {
                                console.error('Error parsing JSON from search stream:', e);
                            }
                        }
                    }
                }
                logSearchState.statusText = matchCount > 0 ? `Finished: ${matchCount} matches` : '';
            } catch (error) {
                if (error.name !== 'AbortError') {
                    console.error('Search error:', error);
                    logSearchState.statusText = 'Search error occurred.';
                }
            } finally {
                logSearchState.isSearching = false;
            }
        };

        const onInput = () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                performSearch();
            }, 300);
        };

        const selectMatch = async (jobId, lineNumber) => {
            if (logSearchState.selectedMatch && logSearchState.selectedMatch.jobId === jobId) {
                // Same job, just update line number and scroll
                logSearchState.selectedMatch.lineNumber = lineNumber;
                nextTick(() => {
                    const el = document.getElementById('preview-line-' + lineNumber);
                    if (el && previewContainer.value) {
                        const containerHeight = previewContainer.value.clientHeight;
                        const elTop = el.offsetTop;
                        previewContainer.value.scrollTop = elTop - (containerHeight / 2) + 20;
                    }
                });
                return;
            }

            logSearchState.selectedMatch = { jobId, lineNumber, fullLog: [] };
            logSearchState.previewLoading = true;
            try {
                const response = await fetch(`/api/job/${jobId}/log`);
                const data = await response.json();
                if (data && data.content) {
                    logSearchState.selectedMatch.fullLog = data.content.split('\n');
                }
                logSearchState.previewLoading = false; // Must be false before nextTick so DOM renders
                
                nextTick(() => {
                    const el = document.getElementById('preview-line-' + lineNumber);
                    if (el && previewContainer.value) {
                        const containerHeight = previewContainer.value.clientHeight;
                        const elTop = el.offsetTop;
                        previewContainer.value.scrollTop = elTop - (containerHeight / 2) + 20;
                    }
                });
            } catch (e) {
                console.error('Error loading log preview:', e);
                logSearchState.previewLoading = false;
            }
        };

        const matchNavigation = computed(() => {
            if (!logSearchState.selectedMatch) return null;
            const jobMatch = logSearchState.results.find(r => r.job_id === logSearchState.selectedMatch.jobId);
            if (!jobMatch || !jobMatch.matches) return null;
            
            const total = jobMatch.matches.length;
            const currentIndex = jobMatch.matches.findIndex(m => m.line_number === logSearchState.selectedMatch.lineNumber) + 1;
            
            return { currentIndex, total, matches: jobMatch.matches };
        });

        const nextMatch = () => {
            const nav = matchNavigation.value;
            if (!nav || nav.currentIndex >= nav.total) return;
            const nextLineNumber = nav.matches[nav.currentIndex].line_number;
            selectMatch(logSearchState.selectedMatch.jobId, nextLineNumber);
        };

        const prevMatch = () => {
            const nav = matchNavigation.value;
            if (!nav || nav.currentIndex <= 1) return;
            const prevLineNumber = nav.matches[nav.currentIndex - 2].line_number;
            selectMatch(logSearchState.selectedMatch.jobId, prevLineNumber);
        };

        const formatTime = (ts) => {
            if (!ts) return '';
            const d = new Date(ts + 'Z');
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString();
        };

        const startSearchResize = (e) => {
            e.preventDefault();
            const startX = e.clientX;
            const startWidth = logSearchState.sidebarWidth;
            const containerWidth = document.querySelector('.search-modal-body').offsetWidth;

            const onMouseMove = (e) => {
                const deltaX = e.clientX - startX;
                const deltaPercent = (deltaX / containerWidth) * 100;
                let newWidth = startWidth + deltaPercent;
                if (newWidth < 20) newWidth = 20;
                if (newWidth > 80) newWidth = 80;
                logSearchState.sidebarWidth = newWidth;
            };

            const onMouseUp = () => {
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
                document.body.style.cursor = 'default';
            };

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
            document.body.style.cursor = 'col-resize';
        };

        return { searchState: logSearchState, searchInput, previewContainer, onInput, selectMatch, highlight, performSearch, formatTime, matchNavigation, nextMatch, prevMatch, startSearchResize, historyUp, historyDown, onEnter };
    }
};

// Initialize app when called
window.initVueApp = function() {
    const app = createApp(App);
    app.component('CommandsView', CommandsView);
    app.component('ScheduledView', ScheduledView);
    app.component('HistoryView', HistoryView);
    app.component('LogSearchModal', LogSearchModal);
    app.mount('#app');
};

})();
