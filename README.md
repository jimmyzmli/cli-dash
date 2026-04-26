# cli-dash

A drop-in Control Center dashboard for any CLI tool. Provides a web-based dashboard with command execution, cron scheduling, live console output, and job history — all backed by a SQLite database.

## Features

- **Command Palette** — Define command cards in `commands.json` for one-click execution
- **Live Console** — Real-time streaming log output with incremental polling
- **Job History** — Persistent record of all runs, filterable by type (Manual vs. Cron)
- **Cron Scheduler** — Built-in cron engine with catch-up support, managed via the UI
- **Extensible** — Add custom tabs, API routes, and database tables for your project
- **Daemonized** — Runs as a background process with PID management and graceful shutdown

## Quick Start

### Install

```bash
pip install -e /path/to/cli-dash
```

### Minimal Setup

Create a `server.py` in your project:

```python
from cli_dash import DashServer, AppConfig

config = AppConfig(
    title="My Project | Control Center",
)

if __name__ == "__main__":
    DashServer(config).run()
```

### Run

```bash
# Start the dashboard (daemonizes by default)
python server.py -s start

# Stop
python server.py -s stop

# Restart
python server.py -s restart

# Override port
python server.py -s start -p 8080

# Reset the database
python server.py db reset
```

The dashboard will be available at `http://127.0.0.1:3000` by default.

## Configuration

### AppConfig

All configuration is passed through the `AppConfig` dataclass:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `title` | `str` | `"Control Center"` | Browser title and header text |
| `host` | `str` | `"127.0.0.1"` | Bind address |
| `port` | `int` | `3000` | Listen port |
| `data_dir` | `str` | `"data"` | Directory for SQLite DB and job logs |
| `log_dir` | `str` | `"logs"` | Directory for server and access logs |
| `web_ui_dir` | `str` | `"web_ui"` | Path to custom frontend assets |
| `extra_routes` | `Callable` | `None` | Hook to register custom FastAPI routes |
| `extra_env` | `dict` | `None` | Extra env vars injected into subprocess execution |
| `on_startup` | `Callable` | `None` | Hook called after DB init (e.g. seed custom tables) |
| `schedules_file` | `str` | `None` | Path to `schedules.json` for initial seeding |

### Environment Variables

These override `AppConfig` values:

| Variable | Overrides | Example |
|----------|-----------|---------|
| `WEB_UI_HOST` | `host` | `0.0.0.0` |
| `WEB_UI_PORT` | `port` | `9990` |

A `.env` file in the project root is automatically loaded via `python-dotenv`.

## Project Structure

When cli-dash runs in your project directory, it creates and manages these directories:

```
your-project/
├── server.py              # Your thin wrapper
├── .env                   # WEB_UI_HOST, WEB_UI_PORT, etc.
├── web_ui/                # Optional: project-local overrides
│   ├── config/            # Config files (override package defaults)
│   │   ├── app.json       # App icon, title, header toggle options
│   │   ├── commands.json  # Command cards for the Commands tab
│   │   └── schedules.json # Cron schedules seeded on first run
│   ├── extensions.js      # Optional: register custom tabs + hooks
│   └── extensions.css     # Optional: styles for custom tabs
├── data/                  # Created automatically
│   ├── web-ui.sqlite      # Jobs + schedules database
│   ├── web-ui.pid         # PID file for daemon
│   └── jobs/              # Per-job log files ({job_id}.log)
└── logs/                  # Created automatically
    ├── web-ui.log         # Server error log
    └── web-ui-access.log  # Access log (POST /run, DELETE /api/job)
```

### Layered Static Serving

Static files are resolved with project-first fallback:
1. Check `web_ui/{path}` in the project directory
2. Fall back to `cli_dash/web_ui/{path}` in the package

This means projects only need to provide files they want to override (config, extensions).
The core `index.html`, `style.css`, and `app.js` come from the package.

## Frontend Configuration

All config files live under `web_ui/config/`. The base HTML/CSS/JS is bundled in the package.

### config/app.json

Controls the dashboard header — icon, title, and toggle options:

```json
{
    "icon": "📊",
    "title": "My Dashboard",
    "header_options": [
        {"id": "dry-run", "label": "Dry Run (-n)", "flag": "-n", "warn_off": true},
        {"id": "force", "label": "Force (-f)", "flag": "-f"}
    ]
}
```

Header options become toggle checkboxes. When checked, `flag` is appended to commands. State persists in localStorage.

### config/commands.json

Defines the command cards on the Commands tab. Supports two formats:

**Grouped format** (multiple buttons per card):

```json
[
    {
        "title": "Generate",
        "icon": "📊",
        "commands": [
            { "label": "Summary", "command": "generate.py summary", "description": "Generate Summary" },
            { "label": "Backup", "command": "generate.py backup" }
        ]
    }
]
```

**Flat format** (one button per card):

```json
[
    {
        "label": "Fix Files",
        "command": "python3 tool.py fix-files",
        "icon": "📁",
        "description": "Remap file paths to match disk"
    }
]
```

### config/schedules.json

Seeded into the database on first run (only if no schedules exist):

```json
[
    {
        "label": "Nightly Sync",
        "command": "python3 sync.py --since yesterday",
        "cron": "30 23 * * *",
        "catch_up": true
    }
]
```

## Extending cli-dash

### Custom Routes

Use `extra_routes` to add project-specific API endpoints:

```python
def register_routes(app, db, run_job_fn):
    """
    Args:
        app:          FastAPI application instance
        db:           cli_dash.Database instance (jobs + schedules CRUD)
        run_job_fn:   Callable(command, job_type='command') -> job_id
    """
    from fastapi import HTTPException

    @app.get("/api/my-data")
    def get_my_data():
        return {"items": [...]}

    @app.post("/api/my-action")
    def run_my_action():
        job_id = run_job_fn("python3 my_script.py", job_type="custom")
        return {"job_id": job_id}

config = AppConfig(
    title="My App",
    extra_routes=register_routes,
)
```

### Custom Database Tables

For project-specific data, use a **separate SQLite database** alongside cli-dash's `web-ui.sqlite`. This keeps concerns separated and avoids coupling:

```python
# my_project/my_database.py
import sqlite3, os

MY_DB_PATH = "data/my-app.sqlite"

def init_my_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(MY_DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS my_table (
            id TEXT PRIMARY KEY,
            status TEXT,
            data TEXT
        )
    """)
    conn.commit()
    conn.close()
```

Hook into `on_startup` to initialize on server start:

```python
def on_startup(db):
    from my_database import init_my_db
    init_my_db()

config = AppConfig(
    on_startup=on_startup,
    ...
)
```

### Custom Tabs via Extensions

Add custom tabs without touching the core HTML/JS. Create `web_ui/extensions.js`:

```js
// Register a custom tab (appears before Commands/Scheduled)
registerTab({
    id: 'my-tab',
    label: 'My Tab',
    html: '<div class="card"><h2>My Content</h2><div id="my-data"></div></div>',
    onActivate: () => fetchMyData()
});

// Hook into job log output (e.g. for custom progress bars)
window.onJobLogUpdate = function(job, fullText) {
    if (job.job_type === 'my-type') parseProgress(fullText);
};

// Run code after the dashboard initializes
window.dashExtensions.onInit.push(() => {
    setInterval(refreshMyData, 3000);
});
```

Add custom styles via `web_ui/extensions.css`. Both files are loaded automatically via `onerror` fallback (no errors if absent).

### Built-in Features (from package)

- Tab switching via URL hash (`#Commands`, `#Scheduled`, custom tabs)
- Job deep-linking (`/job/{id}`)
- Command card rendering from `config/commands.json`
- Live Console polling with extension hook
- Job History with search/filter/rerun/delete
- Schedule management (CRUD, catch-up toggle, inline edit)
- Resizable split pane, server status indicator
- Modal overlay available via `showModal(html)` / `closeModals()`

## API Reference

All endpoints are served by the FastAPI application.

### Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/run` | Execute a command: `{"command": "...", "is_cron": 0}` |
| `GET` | `/api/jobs` | List recent jobs (default limit: 50) |
| `GET` | `/api/job/{id}` | Get job details |
| `GET` | `/api/job/{id}/log?offset=N` | Incremental log output |
| `DELETE` | `/api/job/{id}` | Kill (if running) and delete a job |
| `DELETE` | `/api/jobs` | Kill all running jobs and clear history |

### Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/schedules` | List all schedules with `next_run_iso`, `minutes_until` |
| `POST` | `/api/schedules` | Create: `{"label", "command", "cron_expr", "catch_up"}` |
| `PATCH` | `/api/schedules/{id}` | Update: `{"enabled", "label", "command", "cron_expr", "catch_up"}` |
| `DELETE` | `/api/schedules/{id}` | Delete a schedule |

## CLI

```bash
# Via entry point (if on PATH)
cli-dash -s start|stop|restart
cli-dash -p 8080 -s start
cli-dash db init
cli-dash db reset

# Via python module
python -m cli_dash -s start
```

## Dependencies

- [FastAPI](https://fastapi.tiangolo.com/) — Web framework
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [croniter](https://github.com/kiorky/croniter) — Cron expression parsing
- [python-dotenv](https://github.com/theskumar/python-dotenv) — `.env` file loading
- [python-multipart](https://github.com/Kludex/python-multipart) — Form data parsing

## Examples

### Minimal (no custom UI)

```python
from cli_dash import DashServer, AppConfig

DashServer(AppConfig(title="My Tool")).run()
```

Uses the bundled dashboard. Place config files in `web_ui/config/` to populate tabs.

### Config-only (actual-report)

```python
from cli_dash import DashServer, AppConfig

config = AppConfig(
    title="Actual Budget | Control Center",
    extra_env={"ACTUAL_CLI": "/path/to/actual-wrapper.sh"},
)

if __name__ == "__main__":
    DashServer(config).run()
```

Project provides only `web_ui/config/` with `app.json`, `commands.json`, `schedules.json`. No custom HTML/CSS/JS needed.

### Full Extension (xbvr-utils)

```python
from cli_dash import DashServer, AppConfig

def register_routes(app, db, run_job_fn):
    @app.get("/api/queue")
    def api_queue():
        return manager.get_status()

    @app.post("/api/add")
    def api_add(data: dict):
        job_id = manager.add_url(data["url"])
        return {"job_id": job_id}

def on_startup(db):
    from my_database import init_custom_tables
    init_custom_tables()

config = AppConfig(
    title="XBVR Control Center",
    extra_routes=register_routes,
    on_startup=on_startup,
)

if __name__ == "__main__":
    DashServer(config).run()
```
