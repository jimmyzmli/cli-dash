/* cli-dash Vue 3 app.js */
;(function() {
const { createApp, ref, reactive, computed, onMounted, onUnmounted, nextTick, markRaw } = Vue;

window.dashExtensions = { tabs: [], headerOptions: [], onInit: [] };

window.registerTab = function(tab) { window.dashExtensions.tabs.push(tab); }
window.registerHeaderOption = function(opt) { window.dashExtensions.headerOptions.push(opt); }

// Define Vue app
const App = {
    setup() {
        const appConfig = ref({});
        const connected = ref(false);
        const headerState = reactive({});
        const allHeaderOptions = ref([]);

        const currentTab = ref('commands');
        const tabs = ref([]);
        
        const showMonitor = ref(false);
        const isSwiping = ref(false);
        
        const activeJobId = ref(null);
        const activeJobLabel = ref('');
        const activeJob = ref(null);
        const consoleContent = ref('');
        
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
                const jobId = parseInt(path.split('/')[2]);
                if (jobId && activeJobId.value !== jobId) {
                    showJob({ id: jobId, command: 'Job #' + jobId });
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

        const closeConsole = () => {
            activeJobId.value = null;
            activeJobLabel.value = '';
            activeJob.value = null;
            consoleContent.value = '';
            if (window.location.pathname !== '/') window.history.pushState(null, '', '/');
            if (window.innerWidth <= 768) showMonitor.value = false;
        };

        window.runCommand = async (command, label, isCron = 0, jobType = 'command', queue = null) => {
            appConfig.value.header_options?.forEach(opt => {
                if (headerState[opt.id] && opt.flag && !command.includes(` ${opt.flag}`) && !command.includes(` --${opt.id}`))
                    command += ` ${opt.flag}`;
            });
            try {
                const r = await fetch('/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command, is_cron: isCron, job_type: jobType, queue }) });
                const data = await r.json();
                if (data.job_id) showJob({ id: data.job_id, command: command, job_type: jobType, queue_name: queue });
            } catch (e) { alert('Failed to start command'); }
        };
        
        window.showJobGlobal = showJob;

        const saveHeaderState = (id) => {
            localStorage.setItem(id, headerState[id]);
        };

        onMounted(async () => {
            // Load app config
            try { const r = await fetch('/static/config/app.json'); if (r.ok) appConfig.value = await r.json(); } catch(e){}
            try { const r = await fetch('/api/config'); if (r.ok) { const d = await r.json(); if(d.title) document.title = d.title; } } catch(e){}
            
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
            
            tabs.value = [
                ...extTabs,
                { id: 'commands', label: 'Commands', component: 'CommandsView' },
                { id: 'scheduled', label: 'Scheduled', component: 'ScheduledView' }
            ];

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
        });

        return {
            appConfig, connected, headerState, allHeaderOptions, saveHeaderState,
            tabs, currentTab, switchTab, activeTabComponent, monitorHidden,
            showMonitor, isSwiping, onTouchStart, onTouchMove, onTouchEnd, startResize,
            activeJobId, activeJobLabel, activeJob, consoleContent, closeConsole,
            modal, closeModal, logComponent
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

        return { commandCategories, customCmd, presets, queues, runCustom, loadCommands, runCommand: window.runCommand };
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
            if(!newSched.label || !newSched.command || !newSched.cron_expr) { alert('All fields required'); return; }
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
                    alert(`Exported successfully to ${data.path}`);
                } else {
                    alert('Failed to export schedules');
                }
            } catch (e) {
                alert('Error exporting schedules');
            }
        };

        const importSchedules = async () => {
            if (!confirm('Are you sure you want to import from schedules.json? This will OVERWRITE all current schedules!')) return;
            try {
                const r = await fetch('/api/schedules/import', { method: 'POST' });
                if (r.ok) {
                    alert('Imported successfully');
                    fetchSchedules();
                } else {
                    const err = await r.json();
                    alert('Failed to import: ' + (err.detail || 'Unknown error'));
                }
            } catch (e) {
                alert('Error importing schedules');
            }
        };

        return {
            schedules, searchQuery, filteredSchedules, showNew, newSched, saveNew,
            editingId, editData, startEdit, saveEdit, deleteSchedule, toggleEnabled, toggleCatchUp, formatNextRun,
            runSchedule, filterHistory, exportSchedules, importSchedules
        };
    }
};

const HistoryView = {
    template: '#tpl-history',
    setup() {
        const jobs = ref([]);
        const searchQuery = ref('');
        const showCron = ref(true);

        const fetchJobs = async () => {
            try {
                const r = await fetch('/api/jobs');
                jobs.value = await r.json();
            } catch (e) {}
        };

        const handleFilter = (e) => {
            showCron.value = true;
            searchQuery.value = e.detail;
            document.getElementById('tab-views').scrollTop = 0;
        };

        onMounted(() => {
            fetchJobs();
            window.addEventListener('refresh-history', fetchJobs);
            window.addEventListener('filter-history', handleFilter);
        });

        onUnmounted(() => {
            window.removeEventListener('refresh-history', fetchJobs);
            window.removeEventListener('filter-history', handleFilter);
        });

        const filteredJobs = computed(() => {
            const q = searchQuery.value.toLowerCase().trim();
            return jobs.value.filter(j => (showCron.value || !j.is_cron) && (!q || j.command.toLowerCase().includes(q)));
        });

        const resetFilters = () => { searchQuery.value = ''; showCron.value = false; };
        
        const deleteJob = async (job) => {
            let msg = 'Delete logs for this job?';
            if(job.status === 'running') msg = 'Warning: This job is still running. Terminating it now may lead to data loss. Kill process and delete logs?';
            else if(job.status === 'pending') msg = 'Delete this pending job?';
            if(!confirm(msg)) return;
            await fetch(`/api/job/${job.id}`, { method: 'DELETE' });
            fetchJobs();
        };

        const runJobAgain = (job) => {
            const runCmdEsc = job.command.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            window.runCommand(job.command, 'Rerun', job.is_cron ? 1 : 0, job.job_type);
        };

        const formatTime = (ts) => {
            const d = new Date(ts + 'Z');
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString();
        };

        window.showMonitorGlobal = (show = true) => {
            showMonitor.value = show;
        };

        return { jobs, searchQuery, showCron, filteredJobs, resetFilters, deleteJob, runJobAgain, formatTime, showJob: window.showJobGlobal };
    }
};

// Initialize app when called
window.initVueApp = function() {
    const app = createApp(App);
    app.component('CommandsView', CommandsView);
    app.component('ScheduledView', ScheduledView);
    app.component('HistoryView', HistoryView);
    app.mount('#app');
};

})();
