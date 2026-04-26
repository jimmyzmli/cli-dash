# cli-dash Agent Instructions

## Overview

cli-dash is a reusable Python package that provides a web-based Control Center dashboard for CLI tools. It handles server lifecycle, job execution, cron scheduling, and a live console UI. Projects import `cli_dash` and configure it via `AppConfig`.

## Architecture

```
cli_dash/
├── __init__.py       # Exports: DashServer, AppConfig, Database
├── __main__.py       # python -m cli_dash entry point
├── cli.py            # CLI arg parsing (standalone usage)
├── database.py       # Database class — SQLite jobs + schedules CRUD
├── server.py         # DashServer (lifecycle) + create_app() (FastAPI factory)
└── web_ui/           # Default bundled frontend (HTML, CSS, JS)
    ├── index.html    # Config-driven template (loads extensions dynamically)
    ├── style.css     # Base styles (shared across all consumers)
    ├── app.js        # Core logic + extension API (registerTab, onJobLogUpdate)
    └── config/       # Default config (overridden by project's web_ui/config/)
        ├── app.json
        ├── commands.json
        └── schedules.json
```

## Key Classes

### `AppConfig` (dataclass)
Configuration for a cli-dash instance. Fields:
- `title` (str) — Browser title and header. Default: `"Control Center"`
- `host` (str) — Bind address. Default: `"127.0.0.1"`. Overridden by `WEB_UI_HOST` env.
- `port` (int) — Listen port. Default: `3000`. Overridden by `WEB_UI_PORT` env.
- `data_dir` (str) — SQLite DB and job logs directory. Default: `"data"`
- `log_dir` (str) — Server logs directory. Default: `"logs"`
- `web_ui_dir` (str) — Frontend assets path. Default: `"web_ui"`. Falls back to package-bundled if not found locally.
- `extra_routes` (Callable) — `fn(app, db, run_job_fn)` to register custom FastAPI endpoints
- `extra_env` (dict) — Extra env vars for subprocess execution. Values must be valid file paths to be injected.
- `on_startup` (Callable) — `fn(db)` called after DB init on server start
- `schedules_file` (str) — Path to schedules.json for initial seeding

### `DashServer`
Server lifecycle manager. Constructed with an `AppConfig`. Key methods:
- `start()` — Daemonize (fork on Unix, detached process on Windows), init DB, seed schedules, start scheduler thread, run uvicorn
- `stop()` — SIGTERM then SIGKILL if needed, clean PID file
- `restart()` — stop + start
- `init_db_cmd()` — Backup existing DB, create fresh, seed schedules, clear job logs
- `run()` — Parse CLI args (`-s start|stop|restart`, `-p PORT`, `db init|reset`) and dispatch

### `Database`
SQLite-backed storage. Constructed with `db_path` (default: `data/web-ui.sqlite`).

#### Tables
**jobs:**
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Auto-increment PK |
| command | TEXT | Shell command |
| status | TEXT | pending → running → completed/failed |
| output | TEXT | Legacy field, logs now in files |
| pid | INTEGER | Process ID |
| is_cron | INTEGER | 1 if triggered by scheduler |
| job_type | TEXT | 'command' (default), or custom (e.g. 'rclone') |
| created_at | TIMESTAMP | Auto-set |
| finished_at | TIMESTAMP | Set on completion |

**schedules:**
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER | Auto-increment PK |
| label | TEXT | Display name |
| command | TEXT | Shell command |
| cron_expr | TEXT | Cron expression (5-field) |
| enabled | INTEGER | 1 = active |
| last_run | TIMESTAMP | Last execution time |
| next_run | TIMESTAMP | Calculated field |
| catch_up | INTEGER | 1 = run missed jobs on recovery |

#### Methods
- `init_db()` — Create tables, run migrations, mark stale jobs as failed
- `create_job(command, is_cron, job_type)` → `job_id` (with retry on DB lock)
- `update_job(job_id, status, output, pid, finished)`
- `get_jobs(limit=50)`, `get_job(job_id)`, `clear_jobs()`, `delete_job(job_id)`
- `get_schedules()`, `create_schedule(label, command, cron_expr, catch_up)`
- `update_schedule(id, label, command, cron_expr, enabled, last_run, next_run, catch_up)`
- `delete_schedule(id)`

## Runtime Layout

```
project/
├── data/
│   ├── web-ui.sqlite     # Core DB (jobs + schedules)
│   ├── web-ui.pid        # Daemon PID file
│   └── jobs/             # Log files: {job_id}.log
├── logs/
│   ├── web-ui.log        # Server/scheduler errors
│   └── web-ui-access.log # POST /run and DELETE /api/job requests
```

## Server Internals

### Process Model
- **Parent process** forks (Unix) or spawns (Windows), writes child PID to `data/web-ui.pid`, then exits
- **Child process** detaches session (`os.setsid()`), redirects stdout/stderr to `logs/web-ui.log`, runs uvicorn

### Job Execution
1. `POST /run` → `_run_job()` creates DB record (status: pending)
2. Daemon thread starts → `run_command_task()` sets status to running, opens log file
3. `subprocess.Popen(command, shell=True)` with stdout/stderr → log file
4. On completion → status set to completed/failed, `finished_at` timestamp written

### Scheduler
- Background thread (`_scheduler_loop`) runs every 60 seconds
- For each enabled schedule: calculate `next_run` from `last_run` using `croniter`
- If `next_run <= now`: trigger `_run_job()` with `is_cron=1`
- **Catch-up logic**: if a scheduled run was missed by >2 minutes and `catch_up=0`, skip it and reset `last_run`; if `catch_up=1`, run the missed job

### Frontend Resolution (Layered Static Serving)
1. `GET /static/{path}` checks project's `web_ui/{path}` first
2. Falls back to `cli_dash/web_ui/{path}` inside the installed package
3. Projects override only what they need (config, extensions)
4. Base `index.html` includes optional `extensions.js`/`extensions.css` via `onerror` fallback
5. Index served at `/` and `/job/{id}`

### Extension API (app.js)
- `registerTab({id, label, html, onActivate})` — add custom tabs (appear before Commands/Scheduled)
- `registerHeaderOption({id, label, flag, warn_off})` — add header toggles
- `window.onJobLogUpdate = fn(job, fullText)` — hook for log processing (e.g. rclone progress)
- `window.dashExtensions.onInit` — array of callbacks run after DOM init
- `showModal(html)` / `closeModals()` — modal overlay for extensions

### Config Files (web_ui/config/)
- `app.json` — icon, title, header_options (toggle checkboxes with flags)
- `commands.json` — command cards (grouped or flat format)
- `schedules.json` — initial cron schedules (seeded on first run if DB is empty)

### Schedule Seeding
1. On first start, if DB has no schedules, looks for `schedules.json`:
   - Explicit: `config.schedules_file` path
   - Auto-discovered: project's `web_ui/config/schedules.json`
   - Fallback: package's `web_ui/config/schedules.json`
2. Each entry is inserted via `db.create_schedule()`
3. Seeding only runs when the schedules table is empty (idempotent)

## REST API

### Jobs
| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/run` | `{"command": "...", "is_cron": 0}` | `{"job_id": N, "status": "pending"}` |
| GET | `/api/jobs` | — | `[{id, command, status, pid, is_cron, created_at, finished_at}, ...]` |
| GET | `/api/job/{id}` | — | `{id, command, status, ...}` |
| GET | `/api/job/{id}/log?offset=N` | — | `{"content": "...", "offset": M}` |
| DELETE | `/api/job/{id}` | — | `{"status": "deleted"}` (kills if running) |
| DELETE | `/api/jobs` | — | `{"status": "cleared"}` (kills all running) |

### Schedules
| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/api/schedules` | — | `[{id, label, command, cron_expr, enabled, next_run_iso, minutes_until, catch_up}, ...]` |
| POST | `/api/schedules` | `{"label", "command", "cron_expr", "catch_up"}` | `{"status": "created"}` |
| PATCH | `/api/schedules/{id}` | `{"enabled", "label", "command", "cron_expr", "catch_up"}` | `{"status": "updated"}` |
| DELETE | `/api/schedules/{id}` | — | `{"status": "deleted"}` |

## Extension Pattern

Projects extend cli-dash through four mechanisms:

### 1. `extra_routes(app, db, run_job_fn)`
Register additional FastAPI routes on `app`. Arguments:
- `app` — the FastAPI instance
- `db` — `cli_dash.Database` instance for job/schedule access
- `run_job_fn(command, job_type='command')` → `job_id` — use this to start background jobs that appear in history

### 2. `on_startup(db)`
Called after the core database is initialized but before uvicorn starts. Use for initializing project-specific databases, seeding data, or starting background services.

### 3. `web_ui/config/` (Config Override)
Project-local config files override package defaults. Only provide files you want to customize.

### 4. `web_ui/extensions.js` + `web_ui/extensions.css` (Frontend Extensions)
Register custom tabs, header options, and log processing hooks via the JS extension API. Styles for custom tabs go in `extensions.css`. Both are loaded with graceful fallback (no errors if absent).

## Technical Notes

- **SQLite lock retry**: `create_job()` and `update_job()` retry up to 5 times with 100ms backoff on `SQLITE_BUSY`
- **DB migrations**: `init_db()` uses `ALTER TABLE ADD COLUMN` with try/except for backward-compatible schema evolution
- **Stale job recovery**: On server restart, all `running`/`pending` jobs are marked as `failed` with a restart note
- **Log streaming**: Frontend polls `/api/job/{id}/log?offset=N` every 1 second for incremental output. The offset-based approach avoids re-reading the entire log.
- **Access logging**: Only `POST /run` and `DELETE /api/job/*` are logged to `web-ui-access.log` (not static file or API poll requests)
- **extra_env filtering**: Values in `extra_env` are only injected if `os.path.exists(v)` returns True (prevents injecting invalid paths)

## Consumers

### actual-report
- `server.py`: 16 lines. Sets title and `ACTUAL_CLI` env path.
- `web_ui/config/`: `app.json` (📊 icon, Dry Run + Force toggles), `commands.json`, `schedules.json`.
- No extensions, no custom HTML/CSS/JS. Uses 100% package UI.

### xbvr-utils
- `server.py`: ~150 lines. Registers download manager, scene manager, and SSE endpoints via `extra_routes`.
- Uses `on_startup` to init XBVR-specific tables in a separate `data/xbvr.sqlite`.
- `web_ui/config/`: `app.json` (🚀 icon, Dry Run toggle), `commands.json`.
- `web_ui/extensions.js`: Registers Downloads + Scenes tabs via `registerTab()`, rclone progress via `onJobLogUpdate`.
- `web_ui/extensions.css`: Styles for scenes grid, download queue, modals, rclone progress.
- `modules/xbvr_db.py`: XBVR-only DB functions (download_tasks, rclone_downloads).
