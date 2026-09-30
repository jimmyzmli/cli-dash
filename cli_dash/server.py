"""
Core server module for cli-dash.
Provides DashServer (lifecycle management) and create_app (FastAPI factory).
"""

import os
import sys
import subprocess
import signal
import time
import json
import logging
import threading
import asyncio
import shutil
import plistlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, List, Union

from cli_dash.database import Database, NOT_SET
from cli_dash.utils import get_next_cron_run, get_env, rotate_log_if_needed
from cli_dash.process import (
    is_pid_running,
    terminate_process,
    kill_port_zombies,
    kill_ports_zombies,
    parse_services_config,
    load_services_config,
    get_service_ports,
    service_manager,
    ServiceManager,
)



class Broadcaster:
    """Manages SSE connections and broadcasts events to all active clients."""
    def __init__(self):
        self.queues: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        self.loop = None

    def set_loop(self, loop):
        self.loop = loop

    async def subscribe(self):
        queue = asyncio.Queue()
        with self._lock:
            self.queues.append(queue)
            logging.info(f"SSE client subscribed. Total clients: {len(self.queues)}")
        try:
            yield queue
        finally:
            with self._lock:
                self.queues.remove(queue)
                logging.info(f"SSE client unsubscribed. Total clients: {len(self.queues)}")

    def publish(self, event_type: str, data: dict):
        if not self.loop:
            logging.warning(f"Broadcaster loop not set, cannot publish {event_type}")
            return
        
        message = {
            "event": event_type,
            "data": data
        }
        
        def _put():
            logging.info(f"Broadcaster publishing {event_type} to {len(self.queues)} clients")
            for q in self.queues:
                try:
                    q.put_nowait(message)
                except Exception as e:
                    logging.error(f"Error putting to SSE queue: {e}")

        self.loop.call_soon_threadsafe(_put)

# Global broadcaster instance
broadcaster = Broadcaster()


@dataclass
class AppConfig:
    """Configuration for a cli-dash instance."""
    title: str = "Control Center"
    host: Union[str, List[str]] = "127.0.0.1"
    port: int = 3000
    data_dir: str = "data"
    log_dir: str = "logs"
    web_ui_dir: str = "web_ui"
    extra_routes: Optional[Callable] = None
    extra_env: Optional[dict] = None
    on_startup: Optional[Callable] = None
    schedules_file: Optional[str] = None  # Path to schedules.json for seeding
    ssl_dir: Optional[str] = None  # Directory containing cert.pem, privkey.pem, etc.
    bind_retries: int = 0  # Number of times to retry socket bind (Errno 49)


def _get_package_web_ui() -> str:
    """Return the path to the package-bundled web_ui directory."""
    return str(Path(__file__).parent / "web_ui")


def _get_project_web_ui(config: AppConfig):
    """Return the project's local web_ui dir, or None if it doesn't exist."""
    if os.path.isdir(config.web_ui_dir):
        return config.web_ui_dir
    return None


_parse_services_config = parse_services_config


def _get_help_command(config: AppConfig) -> Optional[str]:
    """Look up the help_command from project's or package's commands.json."""
    project_ui = _get_project_web_ui(config)
    paths = []
    if project_ui:
        paths.append(os.path.join(project_ui, "config", "commands.json"))
    paths.append(os.path.join(_get_package_web_ui(), "config", "commands.json"))
    
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data.get("help_command")
            except Exception as e:
                logging.error(f"Error reading help command from {path}: {e}")
    return None


def _get_schedules_file_path(config: AppConfig) -> str:
    schedules_file = config.schedules_file
    if not schedules_file:
        project_ui = _get_project_web_ui(config)
        if project_ui:
            candidate = os.path.join(project_ui, "config", "schedules.json")
            if os.path.exists(candidate) or os.path.isdir(os.path.join(project_ui, "config")):
                return candidate
        candidate = os.path.join(_get_package_web_ui(), "config", "schedules.json")
        return candidate
    return schedules_file


def _seed_schedules(db: Database, schedules_file: str):
    """Seed schedules from a JSON file if the DB has none."""
    existing = db.get_schedules()
    if existing:
        return
    if not schedules_file or not os.path.exists(schedules_file):
        return
    logging.info("Seeding schedules from %s...", schedules_file)
    try:
        with open(schedules_file, "r") as f:
            schedules = json.load(f)
            for s in schedules:
                if "label" in s and "command" in s and "cron" in s:
                    db.create_schedule(s["label"], s["command"], s["cron"],
                                       catch_up=s.get("catch_up", 0), queue_name=s.get("queue"),
                                       env=s.get("env"), job_exec=s.get("job_exec"))
    except Exception as e:
        logging.error("Error seeding schedules: %s", e)


def run_command_task(db: Database, job_id: int, command: str, data_dir: str,
                     extra_env: Optional[dict] = None, job_type: str = 'command'):
    """Execute a shell command in a background thread, streaming output to a log file."""
    db.update_job(job_id, status="running")
    log_dir = os.path.join(data_dir, "jobs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{job_id}.log")

    try:
        env = get_env(extra_env)

        with open(log_path, "w", encoding="utf-8") as log_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            start_msg = f"[{timestamp}] Starting job: {command}\n"
            log_file.write(start_msg)
            log_file.flush()
            broadcaster.publish("log", {"job_id": job_id, "content": start_msg, "job_type": job_type})

            popen_kwargs = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "env": env,
                "text": True,
                "bufsize": 1,
                "universal_newlines": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                popen_kwargs["startupinfo"] = si
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | 
                    0x08000000  # CREATE_NO_WINDOW
                )
            else:
                popen_kwargs["start_new_session"] = True

            start_time = time.monotonic()
            process = subprocess.Popen(command, **popen_kwargs)
            db.update_job(job_id, pid=process.pid)
            broadcaster.publish("jobs", {"action": "updated", "job_id": job_id})

            # Read output in real-time
            for line in process.stdout:
                log_file.write(line)
                log_file.flush()
                broadcaster.publish("log", {"job_id": job_id, "content": line, "job_type": job_type})

            return_code = process.wait()
            duration = time.monotonic() - start_time
            end_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = "completed" if return_code == 0 else "failed"
            end_msg = f"[{end_timestamp}] Job {status} with exit code {return_code} in {duration:.2f}s\n"
            log_file.write(end_msg)
            db.update_job(job_id, status=status, finished=True, duration=duration)
            broadcaster.publish("log", {"job_id": job_id, "content": end_msg, "job_type": job_type})
            broadcaster.publish("jobs", {"action": "updated", "job_id": job_id})
    except Exception as e:
        with open(log_path, "a", encoding="utf-8") as log_file:
            err_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            err_msg = f"\n[{err_timestamp}] [SERVER ERROR] {str(e)}\n"
            log_file.write(err_msg)
        db.update_job(job_id, status="failed", finished=True)
        broadcaster.publish("log", {"job_id": job_id, "content": err_msg, "job_type": job_type})
        broadcaster.publish("jobs", {"action": "updated", "job_id": job_id})


def _recovery_worker(db: Database, job_id: int, pid: int, data_dir: str, extra_env: Optional[dict] = None):
    """Monitor an orphaned background process and update job status when it finishes."""
    while is_pid_running(pid):
        time.sleep(5)
    
    # Process finished
    status = "completed"
    log_dir = os.path.join(data_dir, "jobs")
    log_path = os.path.join(log_dir, f"{job_id}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n[{timestamp}] Job {status} (detected termination after server restart)\n")
    except:
        pass
    db.update_job(job_id, status=status, finished=True)
    
    # Trigger next in queue if applicable
    job = db.get_job(job_id)
    if job and job.get("queue_name"):
        trigger_next_in_queue(db, job["queue_name"], data_dir, extra_env)


queue_lock = threading.Lock()


def trigger_next_in_queue(db: Database, queue_name: str, data_dir: str, extra_env: Optional[dict] = None):
    """Checks if there's a running job in the queue, and if not, starts the next pending one."""
    with queue_lock:
        running = db.get_running_jobs_in_queue(queue_name)
        if running:
            return
        
        next_job = db.get_next_pending_job_in_queue(queue_name)
        if next_job:
            job_id = next_job["id"]
            command = next_job["command"]
            job_type = next_job.get("job_type", "command")
            
            thread = threading.Thread(
                target=run_command_task_wrapper,
                args=(db, job_id, command, data_dir, extra_env, job_type, queue_name),
                daemon=True,
            )
            thread.start()


def run_command_task_wrapper(db: Database, job_id: int, command: str, data_dir: str,
                             extra_env: Optional[dict] = None, job_type: str = 'command',
                             queue_name: Optional[str] = None):
    """Wrapper that executes a command and then kicks off the next queued task in finally block."""
    try:
        run_command_task(db, job_id, command, data_dir, extra_env, job_type)
    finally:
        if queue_name:
            trigger_next_in_queue(db, queue_name, data_dir, extra_env)


def _run_job(db: Database, command: str, data_dir: str, extra_env=None,
             is_cron=0, job_type='command', queue_name=None, cmd_env=None, job_exec=None, job_source='manual'):
    """Create a job record and start execution in a daemon thread."""
    exec_prefix = job_exec if job_exec is not None else os.getenv("WEB_UI_JOB_EXEC")
    bypass_prefixes = ("/", "./", "../", ".venv/", "~/", "cd ", "sh ", "bash ", "zsh ", "source ")
    if exec_prefix:
        norm_exec = exec_prefix.strip()
        prefix_clean = norm_exec[2:] if norm_exec.startswith("./") else norm_exec
        prefix_with_dot = norm_exec if norm_exec.startswith("./") else f"./{norm_exec}"
        already_prefixed = (
            command.startswith(f"{norm_exec} ")
            or command.startswith(f"{prefix_clean} ")
            or command.startswith(f"{prefix_with_dot} ")
        )
        if not already_prefixed and not command.startswith(bypass_prefixes):
            command = f"{exec_prefix} {command}"

    final_env = {}
    if extra_env:
        final_env.update(extra_env)
    if cmd_env:
        final_env.update(cmd_env)

    job_id = db.create_job(command, is_cron=is_cron, job_type=job_type, queue_name=queue_name, env=cmd_env, job_exec=job_exec, job_source=job_source)
    broadcaster.publish("jobs", {"action": "created", "job_id": job_id})

    if queue_name:
        with queue_lock:
            running = db.get_running_jobs_in_queue(queue_name)
            if not running:
                thread = threading.Thread(
                    target=run_command_task_wrapper,
                    args=(db, job_id, command, data_dir, final_env, job_type, queue_name),
                    daemon=True,
                )
                thread.start()
    else:
        thread = threading.Thread(
            target=run_command_task_wrapper,
            args=(db, job_id, command, data_dir, final_env, job_type, None),
            daemon=True,
        )
        thread.start()
        
    return job_id


def _scheduler_loop(db: Database, data_dir: str, extra_env=None):
    """Background thread that checks cron schedules every 60 seconds."""
    logging.info("Scheduler thread started")
    while True:
        try:
            now = datetime.now()
            schedules = db.get_schedules()
            for s in schedules:
                if not s["enabled"]:
                    continue
                try:
                    base_time = now.replace(second=0, microsecond=0)
                    if s["last_run"]:
                        try:
                            base_time = datetime.strptime(s["last_run"], "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            pass

                    if s["last_run"] is None:
                        db.update_schedule(s["id"], last_run=now.strftime("%Y-%m-%d %H:%M:%S"))
                        continue

                    next_run = get_next_cron_run(s["cron_expr"], base_time)
                    if next_run <= now:
                        is_missed = (now - next_run).total_seconds() > 120
                        if is_missed and not s.get("catch_up"):
                            logging.info("Skipping missed job (Catch Up disabled): %s", s["label"])
                            db.update_schedule(s["id"], last_run=now.strftime("%Y-%m-%d %H:%M:%S"))
                            continue

                        logging.info("Triggering scheduled job: %s", s["label"])
                        cmd_env = json.loads(s["env"]) if s.get("env") else None
                        job_exec = s.get("job_exec")
                        _run_job(db, s["command"], data_dir, extra_env=extra_env, is_cron=1, queue_name=s.get("queue_name"), cmd_env=cmd_env, job_exec=job_exec)
                        db.update_schedule(s["id"], last_run=now.strftime("%Y-%m-%d %H:%M:%S"))
                        broadcaster.publish("schedules", {"action": "updated", "schedule_id": s["id"]})
                except Exception as e:
                    logging.error("Error processing schedule %s: %s", s["label"], e)
        except Exception as e:
            logging.error("Scheduler loop error: %s", e)

        time.sleep(60)


# Service management (delegated to cli_dash.process)
_service_processes = service_manager._processes
_service_states = service_manager._states
_kill_zombies_on_port = kill_port_zombies
_stop_service_proc = service_manager.stop_service
_spawn_service = service_manager.spawn_service
_service_manager_loop = service_manager.run_manager_loop



def create_app(config: AppConfig, db: Database):
    """
    Build and return a FastAPI application with all core routes.

    If config.extra_routes is provided, it is called with (app, db, run_job_fn)
    so consuming projects can register additional endpoints.
    """
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
    import asyncio

    app = FastAPI(title=config.title)

    @app.on_event("startup")
    async def startup():
        broadcaster.set_loop(asyncio.get_running_loop())
        logging.info("Broadcaster loop initialized")
        
        async def ping_loop():
            while True:
                await asyncio.sleep(60) # Heartbeat every minute
                broadcaster.publish("ping", {})
        
        asyncio.create_task(ping_loop())

    pkg_web_ui = _get_package_web_ui()
    project_web_ui = _get_project_web_ui(config)
    data_dir = config.data_dir

    # --- Access log middleware ---
    access_log_path = os.path.join(config.log_dir, "web-ui-access.log")

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        log_line = None
        client = f"{request.client.host}:{request.client.port}"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if request.url.path == "/run" and request.method == "POST":
            body = await request.body()
            command = "N/A"
            try:
                data = json.loads(body)
                command = data.get("command", "N/A")
            except Exception:
                pass
            log_line = f'{timestamp} [INFO] {client} - "POST /run" [CMD: {command}]\n'

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            request._receive = receive

        elif request.url.path.startswith("/api/job") and request.method == "DELETE":
            log_line = f'{timestamp} [INFO] {client} - "DELETE {request.url.path}"\n'

        if log_line:
            rotate_log_if_needed(access_log_path)
            with open(access_log_path, "a") as f:
                f.write(log_line)

        return await call_next(request)

    # --- Core routes ---

    @app.get("/", response_class=HTMLResponse)
    @app.get("/job/{job_id}", response_class=HTMLResponse)
    async def get_index(request: Request):
        # Project can override index.html, otherwise use package default
        if project_web_ui:
            local = os.path.join(project_web_ui, "index.html")
            if os.path.exists(local):
                return FileResponse(local)
        return FileResponse(os.path.join(pkg_web_ui, "index.html"))

    @app.get("/api/config")
    async def get_app_config():
        """Return merged app config for the frontend."""
        mcp_servers = []
        if config.web_ui_dir:
            services_config_path = os.path.join(config.web_ui_dir, "config", "services.json")
            mcp_servers = service_manager.get_services_status(services_config_path)
        return {
            "title": config.title,
            "mcp_servers": mcp_servers
        }

    @app.post("/api/services/{service_name}/restart")
    async def restart_service(service_name: str):
        if not config.web_ui_dir:
            raise HTTPException(status_code=404, detail="Services not configured")
        
        services_config_path = os.path.join(config.web_ui_dir, "config", "services.json")
        if not os.path.exists(services_config_path):
            raise HTTPException(status_code=404, detail="Services config not found")
            
        log_dir = os.path.join(data_dir, "jobs")
        if not service_manager.restart_service_async(service_name, services_config_path, log_dir, config.extra_env):
            raise HTTPException(status_code=404, detail="Service not found")
            
        return {"status": "restarting"}


    @app.get("/api/help")
    def get_help():
        """Run the configured help_command and return its stdout/stderr."""
        help_cmd = _get_help_command(config)
        if not help_cmd:
            return {"content": "No help command configured."}
        
        try:
            env = get_env(config.extra_env)
            
            res = subprocess.run(
                help_cmd,
                shell=True,
                capture_output=True,
                text=True,
                env=env,
                encoding="utf-8",
                errors="replace"
            )
            output = res.stdout + res.stderr
            return {"content": output}
        except Exception as e:
            return {"content": f"Error running help command: {str(e)}"}

    # Jobs API

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 50, offset: int = 0):
        return db.get_jobs(limit=limit, offset=offset)

    @app.delete("/api/jobs")
    async def clear_jobs():
        jobs = db.get_jobs(limit=100)
        for job in jobs:
            if job.get("status") == "running" and job.get("pid"):
                terminate_process(job["pid"])
        db.clear_jobs()
        log_dir = os.path.join(data_dir, "jobs")
        if os.path.exists(log_dir):
            for f in os.listdir(log_dir):
                if f.endswith(".log"):
                    try:
                        os.remove(os.path.join(log_dir, f))
                    except Exception:
                        pass
        return {"status": "cleared"}

    @app.get("/api/job/{job_id}")
    async def get_job(job_id: int):
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        job["log_path"] = os.path.abspath(os.path.join(data_dir, "jobs", f"{job_id}.log"))
        return job

    @app.delete("/api/job/{job_id}")
    async def delete_job(job_id: int):
        job = db.get_job(job_id)
        if job and job.get("pid"):
            terminate_process(job["pid"])
        db.delete_job(job_id)
        broadcaster.publish("jobs", {"action": "deleted", "job_id": job_id})
        log_path = os.path.join(data_dir, "jobs", f"{job_id}.log")
        if os.path.exists(log_path):
            try:
                os.remove(log_path)
            except Exception:
                pass
        return {"status": "deleted"}

    @app.post("/api/job/{job_id}/terminate")
    async def terminate_job_api(job_id: int):
        job = db.get_job(job_id)
        if job and job.get("pid"):
            terminate_process(job["pid"])
        return {"status": "terminated"}

    @app.get("/api/job/{job_id}/log")
    async def get_job_log(job_id: int, offset: int = 0):
        """
        Simple REST log endpoint. Returns current log content from offset.
        Used for initial load; real-time updates now use SSE (/api/events).
        """
        log_path = os.path.join(data_dir, "jobs", f"{job_id}.log")
        job = db.get_job(job_id)
        job_status = job["status"] if job else "unknown"
        job_type = job.get("job_type", "command") if job else "command"

        if not os.path.exists(log_path):
            return {"content": "", "offset": 0, "job_status": job_status, "job_type": job_type}

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read()
            new_offset = f.tell()
        return {"content": content, "offset": new_offset,
                "job_status": job_status, "job_type": job_type}

    @app.get("/api/logs/search")
    async def search_logs(request: Request, q: str = "", match_case: bool = False, match_word: bool = False, use_regex: bool = False):
        """
        Streaming endpoint that searches all job logs for a given query
        and yields results incrementally as JSON objects separated by newlines.
        """
        if not q:
            return StreamingResponse(iter([]), media_type="application/x-ndjson")

        async def search_generator():
            import re
            try:
                flags = 0 if match_case else re.IGNORECASE
                pattern = q if use_regex else re.escape(q)
                if match_word:
                    pattern = r'\b' + pattern + r'\b'
                
                try:
                    compiled_regex = re.compile(pattern, flags)
                except re.error:
                    yield json.dumps({"error": "Invalid regular expression"}) + "\n"
                    return

                jobs = {str(j["id"]): j for j in db.get_jobs(limit=1000)}
                log_dir = os.path.join(data_dir, "jobs")
                if not os.path.exists(log_dir):
                    return

                # Get all log files and sort by job id descending
                files = []
                for f in os.listdir(log_dir):
                    if f.endswith(".log"):
                        try:
                            job_id = int(f[:-4])
                            files.append((job_id, f))
                        except ValueError:
                            pass
                files.sort(key=lambda x: x[0], reverse=True)

                for job_id_int, filename in files:
                    if await request.is_disconnected():
                        break

                    log_path = os.path.join(log_dir, filename)
                    matches = []
                    try:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                            for i, line in enumerate(f):
                                if compiled_regex.search(line):
                                    matches.append({"line_number": i + 1, "content": line.strip()})
                                    if len(matches) > 100:  # limit matches per file
                                        break
                    except Exception:
                        pass

                    if matches:
                        job = jobs.get(str(job_id_int), {})
                        result = {
                            "job_id": job_id_int,
                            "command": job.get("command", f"Job #{job_id_int}"),
                            "created_at": job.get("created_at"),
                            "matches": matches
                        }
                        yield json.dumps(result) + "\n"
                        # Tiny sleep to yield to event loop so we don't block other requests
                        await asyncio.sleep(0.01)

            except Exception as e:
                logging.error(f"Error during log search: {e}")

        return StreamingResponse(search_generator(), media_type="application/x-ndjson")

    @app.get("/api/events")
    async def sse_events(request: Request):
        """
        Unified SSE stream for jobs, schedules, and logs.
        """
        async def event_generator():
            # Send initial event to confirm connection
            try:
                yield "event: connected\ndata: {}\n\n"
            except Exception:
                return

            async for queue in broadcaster.subscribe():
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        break
                break

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )

    # Schedules API

    @app.get("/api/schedules")
    async def get_schedules():
        schedules = db.get_schedules()
        now = datetime.now()
        for s in schedules:
            if not s["enabled"]:
                s["next_run_iso"] = None
                s["minutes_until"] = 999999
                continue
            try:
                next_run = get_next_cron_run(s["cron_expr"], now)
                s["next_run_iso"] = next_run.isoformat()
                s["minutes_until"] = int((next_run - now).total_seconds() / 60)
            except Exception as e:
                logging.error("Error calculating next run for %s: %s", s["label"], e)
                s["next_run_iso"] = None
                s["minutes_until"] = 999999

        schedules.sort(key=lambda x: x["minutes_until"])
        return schedules

    @app.post("/api/schedules")
    async def create_schedule(request: Request):
        data = await request.json()
        db.create_schedule(data["label"], data["command"], data["cron_expr"],
                           catch_up=data.get("catch_up", 0),
                           queue_name=data.get("queue"),
                           env=data.get("env"),
                           job_exec=data.get("job_exec"))
        broadcaster.publish("schedules", {"action": "created"})
        return {"status": "created"}

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: int, request: Request):
        data = await request.json()
        db.update_schedule(
            schedule_id,
            enabled=data.get("enabled"),
            label=data.get("label"),
            command=data.get("command"),
            cron_expr=data.get("cron_expr"),
            catch_up=data.get("catch_up"),
            queue_name=data.get("queue") if "queue" in data else NOT_SET,
            env=data.get("env") if "env" in data else NOT_SET,
            job_exec=data.get("job_exec") if "job_exec" in data else NOT_SET
        )
        broadcaster.publish("schedules", {"action": "updated", "schedule_id": schedule_id})
        return {"status": "updated"}

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: int):
        db.delete_schedule(schedule_id)
        broadcaster.publish("schedules", {"action": "deleted", "schedule_id": schedule_id})
        return {"status": "deleted"}

    @app.post("/api/schedules/export")
    async def export_schedules():
        schedules = db.get_schedules()
        out = []
        for s in schedules:
            out.append({
                "label": s["label"],
                "command": s["command"],
                "cron": s["cron_expr"],
                "catch_up": bool(s.get("catch_up", 0)),
                "queue": s.get("queue_name"),
                "env": json.loads(s["env"]) if s.get("env") else None,
                "job_exec": s.get("job_exec")
            })
        path = _get_schedules_file_path(config)
        try:
            with open(path, "w") as f:
                json.dump(out, f, indent=4)
            return {"status": "exported", "path": path}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/schedules/import")
    async def import_schedules():
        path = _get_schedules_file_path(config)
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="schedules.json not found")
        
        # Clear existing schedules directly
        conn = db._connect()
        c = conn.cursor()
        c.execute('DELETE FROM schedules')
        conn.commit()
        conn.close()
        
        _seed_schedules(db, path)
        broadcaster.publish("schedules", {"action": "imported"})
        return {"status": "imported"}

    # Run command endpoint

    @app.post("/run")
    async def run_cmd(request: Request):
        data = await request.json()
        command = data.get("command")
        is_cron = data.get("is_cron", 0)
        job_type = data.get("job_type", "command")
        queue_name = data.get("queue")
        cmd_env = data.get("env")
        job_exec = data.get("job_exec")
        job_source = data.get("job_source", "manual")
        if not command:
            raise HTTPException(status_code=400, detail="No command provided")
        job_id = _run_job(db, command, data_dir, extra_env=config.extra_env, is_cron=is_cron, job_type=job_type, queue_name=queue_name, cmd_env=cmd_env, job_exec=job_exec, job_source=job_source)
        return {"job_id": job_id, "status": "pending"}

    # --- Extension hook: let consuming projects add custom routes ---
    if config.extra_routes:
        def _run_job_fn(command, job_type='command', queue=None, env=None, job_exec=None, job_source='manual'):
            return _run_job(db, command, data_dir, extra_env=config.extra_env, job_type=job_type, queue_name=queue, cmd_env=env, job_exec=job_exec, job_source=job_source)
        config.extra_routes(app, db, _run_job_fn)

    # Layered static file serving: project files override package defaults
    @app.get("/static/{path:path}")
    async def serve_static(path: str):
        # Check project dir first
        if project_web_ui:
            local_path = os.path.join(project_web_ui, path)
            if os.path.isfile(local_path):
                return FileResponse(local_path)
        # Fall back to package
        pkg_path = os.path.join(pkg_web_ui, path)
        if os.path.isfile(pkg_path):
            return FileResponse(pkg_path)
        
        # Graceful fallback for optional files to avoid 404
        if path in ["extensions.js", "extensions.css", "favicon.ico", "templates.html"]:
            if path == "favicon.ico":
                media_type = "image/x-icon"
            elif path == "templates.html":
                media_type = "text/html"
            else:
                media_type = "application/javascript" if path.endswith(".js") else "text/css"
            return Response(content="", media_type=media_type)

        raise HTTPException(status_code=404, detail=f"Static file not found: {path}")

    return app


class DashServer:
    """
    Manages the server lifecycle: start (daemonize), stop, restart, db init.
    """

    def __init__(self, config: AppConfig = None):
        self.config = config or AppConfig()
        
        if os.getenv("WEB_UI_SSL_DIR"):
            self.config.ssl_dir = os.getenv("WEB_UI_SSL_DIR")

        def load_config_file(filepath: str):
            if os.path.isfile(filepath):
                try:
                    with open(filepath, "r") as f:
                        app_data = json.load(f)
                        if "host" in app_data:
                            self.config.host = app_data["host"]
                        if "port" in app_data:
                            self.config.port = int(app_data["port"])
                        if "job_exec" in app_data:
                            os.environ["WEB_UI_JOB_EXEC"] = app_data["job_exec"]
                        if "env" in app_data and isinstance(app_data["env"], dict):
                            if not self.config.extra_env:
                                self.config.extra_env = {}
                            self.config.extra_env.update(app_data["env"])
                        if "ssl_dir" in app_data:
                            self.config.ssl_dir = app_data["ssl_dir"]
                        if "bind_retries" in app_data:
                            self.config.bind_retries = int(app_data["bind_retries"])
                except Exception as e:
                    logging.error(f"Failed to read {filepath}: {e}")

        # Read app.json overrides if present
        load_config_file(os.path.join(self.config.web_ui_dir, "config", "app.json"))
        
        # Read server.json overrides if present (machine-specific)
        load_config_file(os.path.join(self.config.web_ui_dir, "config", "server.json"))

        # Ensure paths are absolute relative to CWD
        self.config.web_ui_dir = os.path.abspath(self.config.web_ui_dir)
        self.config.data_dir = os.path.abspath(self.config.data_dir)
        self.config.log_dir = os.path.abspath(self.config.log_dir)
        if self.config.ssl_dir:
            self.config.ssl_dir = os.path.abspath(self.config.ssl_dir)

        self.pid_file = os.path.join(self.config.data_dir, "web-ui.pid")
        self.error_log = os.path.join(self.config.log_dir, "web-ui.log")

        # Ensure directories exist
        os.makedirs(self.config.data_dir, exist_ok=True)
        os.makedirs(self.config.log_dir, exist_ok=True)
        os.makedirs(os.path.join(self.config.data_dir, "jobs"), exist_ok=True)

        # Setup logging
        self.error_log = os.path.join(self.config.log_dir, "web-ui.log")
        rotate_log_if_needed(self.error_log)
        logging.basicConfig(
            filename=self.error_log,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

        host_display = ", ".join(self.config.host) if isinstance(self.config.host, list) else self.config.host
        logging.info(f"Configuration loaded: host={host_display}, port={self.config.port}")
        logging.info(f"Project UI: {self.config.web_ui_dir}")
        logging.info(f"Data Dir: {self.config.data_dir}")

        self.pid_file = os.path.join(self.config.data_dir, "web-ui.pid")
        self.db = Database(db_path=os.path.join(self.config.data_dir, "web-ui.sqlite"))

        # Ensure PATH is active process-wide
        os.environ["PATH"] = get_env(self.config.extra_env)["PATH"]

    def is_running(self):
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file, "r") as f:
                    pid = int(f.read().strip())
                if is_pid_running(pid):
                    return pid
            except:
                pass
        return None

    def _get_cert_expiry(self, cert_path):
        """Get the expiry date of a PEM certificate."""
        import _ssl
        import re
        try:
            cert = _ssl._test_decode_cert(cert_path)
            if 'notAfter' in cert:
                date_str = cert['notAfter']
                # Normalize spaces
                date_str = re.sub(' +', ' ', date_str)
                try:
                    return datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
                except ValueError:
                    # Try without timezone
                    return datetime.strptime(" ".join(date_str.split()[:-1]), "%b %d %H:%M:%S %Y")
        except Exception as e:
            logging.error(f"Error checking cert expiry: {e}")
        return None

    def _kill_port_zombies(self):
        """Find and kill any processes listening on the configured port or service ports."""
        ports = [self.config.port]
        if self.config.web_ui_dir:
            services_config_path = os.path.join(self.config.web_ui_dir, "config", "services.json")
            ports.extend(get_service_ports(services_config_path))

        for port in set(ports):
            print(f"Force mode: checking for zombie processes on port {port}...")
            kill_port_zombies(port)

    def start(self, force=False):
        # ANSI Colors
        GREEN = "\033[92m"
        BLUE = "\033[94m"
        YELLOW = "\033[93m"
        RED = "\033[91m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        if force:
            self._kill_port_zombies()

        pid = self.is_running()
        if pid:
            print(f"{YELLOW}Server is already running (PID: {pid}){RESET}")
            return

        # Print summary to console before daemonizing
        host_display = ", ".join(self.config.host) if isinstance(self.config.host, list) else self.config.host
        print(f"{BOLD}{BLUE}Starting Control Center on {host_display}:{self.config.port}{RESET}")
        print(f"  {BLUE}Data Dir:{RESET}    {self.config.data_dir}")
        print(f"  {BLUE}Project UI:{RESET}  {self.config.web_ui_dir}")

        if self.config.ssl_dir:
            ssl_cert = os.path.join(self.config.ssl_dir, "fullchain.pem")
            if os.path.exists(ssl_cert):
                expiry = self._get_cert_expiry(ssl_cert)
                if expiry:
                    expiry_str = expiry.strftime("%Y-%m-%d %H:%M:%S")
                    color = GREEN if expiry > datetime.now() else RED
                    print(f"  {BLUE}SSL Expiry:{RESET}   {color}{expiry_str}{RESET}")
                    if expiry < datetime.now():
                        print(f"  {RED}{BOLD}WARNING: SSL Certificate is EXPIRED!{RESET}")
                else:
                    print(f"  {YELLOW}WARNING: Could not determine SSL certificate expiry date.{RESET}")
            else:
                print(f"  {YELLOW}WARNING: SSL files missing in {self.config.ssl_dir}{RESET}")

        # Check dependencies
        try:
            import fastapi
            import uvicorn
            import croniter
        except ImportError:
            print("Installing dependencies...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install",
                 "fastapi", "uvicorn", "python-multipart", "croniter"],
                check=True,
                creationflags=0x08000000 if os.name == "nt" else 0
            )

        # Daemonize (Unix)
        if "--internal-run" not in sys.argv:
            if os.name != "nt":
                try:
                    child_pid = os.fork()
                    if child_pid > 0:
                        with open(self.pid_file, "w") as f:
                            f.write(str(child_pid))
                        print(f"{GREEN}Server started in background (PID: {child_pid}){RESET}")
                        sys.exit(0)
                except OSError as e:
                    print(f"Fork failed: {e}")
                    sys.exit(1)

                os.setsid()
                sys.stdout.flush()
                sys.stderr.flush()
                with open(os.devnull, "r") as devnull:
                    os.dup2(devnull.fileno(), sys.stdin.fileno())
                with open(self.error_log, "a") as errlog:
                    os.dup2(errlog.fileno(), sys.stdout.fileno())
                    os.dup2(errlog.fileno(), sys.stderr.fileno())
            else:
                # Windows daemonization
                cmd = [sys.executable] + sys.argv + ["--internal-run"]
                with open(self.error_log, "a", encoding="utf-8") as log_file:
                    subprocess.Popen(
                        cmd,
                        creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000),
                        stdout=log_file, stderr=subprocess.STDOUT, close_fds=True,
                    )
                print(f"{GREEN}Server starting in background...{RESET}")
                sys.exit(0)

        # Write PID
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()))

        # Init DB
        self.db.init_db()

        # Recover orphaned jobs
        try:
            running_jobs = self.db.get_running_jobs()
            if running_jobs:
                logging.info(f"Found {len(running_jobs)} running/pending jobs to recover.")
            
            queues_to_trigger = set()
            for job in running_jobs:
                job_id = job['id']
                pid = job.get('pid')
                q_name = job.get('queue_name')
                
                # If the job is pending and belongs to a queue, leave it alone!
                if job['status'] == 'pending' and q_name:
                    queues_to_trigger.add(q_name)
                    continue
                    
                if not pid or not is_pid_running(pid):
                    self.db.update_job(job_id, status='failed', finished=True)
                    log_path = os.path.join(self.config.data_dir, "jobs", f"{job_id}.log")
                    try:
                        with open(log_path, "a", encoding="utf-8") as f:
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            f.write(f"\n[{timestamp}] Job failed (detected process death after server restart)\n")
                    except: pass
                    if q_name:
                        queues_to_trigger.add(q_name)
                else:
                    # Still running! Start recovery monitor
                    t = threading.Thread(
                        target=_recovery_worker,
                        args=(self.db, job_id, pid, self.config.data_dir, self.config.extra_env),
                        daemon=True
                    )
                    t.start()
            
            # Resume queue processing for all queues
            for q_name in queues_to_trigger:
                trigger_next_in_queue(self.db, q_name, self.config.data_dir, self.config.extra_env)
                
        except Exception as e:
            logging.error(f"Error during job recovery: {e}")

        # Seed schedules
        schedules_file = _get_schedules_file_path(self.config)
        _seed_schedules(self.db, schedules_file)

        # On-startup hook
        if self.config.on_startup:
            self.config.on_startup(self.db)

        # Build FastAPI app
        app = create_app(self.config, self.db)

        # Start service manager thread
        if self.config.web_ui_dir:
            services_config_path = os.path.join(self.config.web_ui_dir, "config", "services.json")
            if os.path.exists(services_config_path):
                svc_thread = threading.Thread(
                    target=_service_manager_loop,
                    args=(services_config_path, self.config.log_dir, self.config.extra_env),
                    daemon=True,
                )
                svc_thread.start()

        # Start scheduler thread
        sched_thread = threading.Thread(
            target=_scheduler_loop,
            args=(self.db, self.config.data_dir, self.config.extra_env),
            daemon=True,
        )
        sched_thread.start()
        # Run uvicorn
        import uvicorn

        log_config = uvicorn.config.LOGGING_CONFIG
        log_config["formatters"]["default"]["fmt"] = "%(asctime)s [%(levelname)s] %(message)s"
        log_config["formatters"]["access"]["fmt"] = (
            '%(asctime)s [%(levelname)s] %(client_addr)s - "%(request_line)s" %(status_code)s'
        )
        log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
        log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"

        # SSL Configuration
        ssl_keyfile = None
        ssl_certfile = None
        if self.config.ssl_dir:
            ssl_keyfile = os.path.join(self.config.ssl_dir, "privkey.pem")
            ssl_certfile = os.path.join(self.config.ssl_dir, "fullchain.pem")
            
            if not os.path.exists(ssl_keyfile) or not os.path.exists(ssl_certfile):
                logging.error(f"SSL requested but files missing in {self.config.ssl_dir}")
                ssl_keyfile = None
                ssl_certfile = None
            else:
                logging.info(f"Using SSL with cert: {ssl_certfile}")
                expiry = self._get_cert_expiry(ssl_certfile)
                if expiry:
                    expiry_str = expiry.strftime("%Y-%m-%d %H:%M:%S")
                    logging.info(f"SSL Certificate Expiry: {expiry_str}")
                    if expiry < datetime.now():
                        logging.warning("!!! SSL CERTIFICATE EXPIRED !!!")
                else:
                    logging.warning("Could not determine SSL certificate expiry date.")

        if isinstance(self.config.host, list):
            import socket
            import asyncio
            import time
            
            sockets = []
            for h in self.config.host:
                addr_family = socket.AF_INET6 if ":" in h else socket.AF_INET
                sock = socket.socket(addr_family, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                
                max_retries = max(1, self.config.bind_retries + 1)
                for attempt in range(max_retries):
                    try:
                        sock.bind((h, self.config.port))
                        break
                    except OSError as e:
                        if e.errno == 49 and attempt < max_retries - 1:
                            logging.warning(f"Could not bind to {h}:{self.config.port} (Errno 49). Retrying in 2s...")
                            time.sleep(2)
                        else:
                            raise
                            
                sock.listen(256)
                sockets.append(sock)
                
            config = uvicorn.Config(
                app,
                port=self.config.port,
                log_level="info",
                access_log=False,
                log_config=log_config,
                ssl_keyfile=ssl_keyfile,
                ssl_certfile=ssl_certfile,
                http="h11",
                loop="asyncio"
            )
            server = uvicorn.Server(config)
            asyncio.run(server.serve(sockets=sockets))
        else:
            uvicorn.run(
                app,
                host=self.config.host,
                port=self.config.port,
                log_level="info",
                access_log=False,
                log_config=log_config,
                ssl_keyfile=ssl_keyfile,
                ssl_certfile=ssl_certfile,
                http="h11",
                loop="asyncio"
            )

    def stop(self, force=False):
        YELLOW = "\033[93m"
        RED = "\033[91m"
        RESET = "\033[0m"

        pid = self.is_running()
        if not pid and not force:
            print(f"{YELLOW}Server is not running.{RESET}")
            return

        if pid:
            print(f"Stopping server (PID: {pid})...")
            try:
                if not terminate_process(pid, timeout=1.5, force=True):
                    print(f"{RED}Failed to kill PID {pid}{RESET}")
                if os.path.exists(self.pid_file):
                    os.remove(self.pid_file)
                print(f"{YELLOW}Server stopped.{RESET}")
            except ProcessLookupError:
                print(f"{RED}Process not found.{RESET}")
                if os.path.exists(self.pid_file):
                    os.remove(self.pid_file)
                
        if force:
            self._kill_port_zombies()

    def restart(self, force=False):
        self.stop(force=force)
        time.sleep(1)
        self.start(force=force)

    def init_db_cmd(self):
        import shutil

        db_path = self.db.db_path
        backup_path = db_path + ".backup"

        if os.path.exists(db_path):
            print(f"Backing up existing database to {backup_path}...")
            shutil.copy2(db_path, backup_path)
            print("Removing existing database...")
            os.remove(db_path)

        print("Initializing fresh database...")
        self.db.init_db()
        print("Database initialized successfully.")

        schedules_file = _get_schedules_file_path(self.config)
        _seed_schedules(self.db, schedules_file)
        print("Core schedules seeded.")

        # Clear logs
        log_dir = os.path.join(self.config.data_dir, "jobs")
        if os.path.exists(log_dir):
            print(f"Clearing job logs in {log_dir}...")
            for f in os.listdir(log_dir):
                if f.endswith(".log"):
                    try:
                        os.remove(os.path.join(log_dir, f))
                    except Exception as e:
                        print(f"Error removing {f}: {e}")

    def export_schedules_cmd(self):
        schedules = self.db.get_schedules()
        out = []
        for s in schedules:
            out.append({
                "label": s["label"],
                "command": s["command"],
                "cron": s["cron_expr"],
                "catch_up": bool(s.get("catch_up", 0))
            })
        path = _get_schedules_file_path(self.config)
        try:
            with open(path, "w") as f:
                json.dump(out, f, indent=4)
            print(f"Exported {len(out)} schedules to {path}")
        except Exception as e:
            print(f"Error exporting schedules: {e}")

    def import_schedules_cmd(self):
        path = _get_schedules_file_path(self.config)
        if not os.path.exists(path):
            print(f"Error: {path} not found.")
            return

        conn = self.db._connect()
        c = conn.cursor()
        c.execute('DELETE FROM schedules')
        conn.commit()
        conn.close()

        _seed_schedules(self.db, path)
        print(f"Imported schedules from {path}")

    def truncate_jobs_cmd(self, until_str: str = None):
        import datetime
        import re

        cutoff_date = None
        if until_str:
            match = re.match(r"^(\d+)([d])$", until_str)
            if not match:
                print("Invalid --until format. Use e.g. 10d for 10 days.")
                return
            val = int(match.group(1))
            now = datetime.datetime.now()
            cutoff_date = now - datetime.timedelta(days=val)

        cutoff_str = cutoff_date.strftime("%Y-%m-%d %H:%M:%S") if cutoff_date else None

        conn = self.db._connect()
        c = conn.cursor()
        
        if cutoff_str:
            c.execute("SELECT id FROM jobs WHERE created_at < ?", (cutoff_str,))
        else:
            c.execute("SELECT id FROM jobs")
            
        jobs_to_delete = [row[0] for row in c.fetchall()]
        
        if not jobs_to_delete:
            print("No jobs to truncate.")
            conn.close()
            return
            
        print(f"Truncating {len(jobs_to_delete)} jobs...")
        
        if cutoff_str:
            c.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff_str,))
        else:
            c.execute("DELETE FROM jobs")
            
        conn.commit()
        conn.close()
        
        # Now delete log files
        log_dir = os.path.join(self.config.data_dir, "jobs")
        deleted_files = 0
        if os.path.exists(log_dir):
            for job_id in jobs_to_delete:
                log_path = os.path.join(log_dir, f"{job_id}.log")
                if os.path.exists(log_path):
                    try:
                        os.remove(log_path)
                        deleted_files += 1
                    except Exception as e:
                        pass
                        
        print(f"Truncated {len(jobs_to_delete)} database rows and {deleted_files} log files.")

    def autostart_cmd(self, action: str):
        if os.name == "nt":
            print("Notice: Autostart management is currently only implemented for macOS (LaunchAgents).")
            print("Windows support is planned for a future update. Skipping for now.")
            return

        # ANSI Colors
        GREEN = "\033[92m"
        BLUE = "\033[94m"
        YELLOW = "\033[93m"
        RED = "\033[91m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        project_name = os.path.basename(os.getcwd())
        label = f"com.cli-dash.{project_name}"
        plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")

        if action == "enable":
            # 1. Cleanup legacy agents (if any)
            agent_dir = os.path.expanduser("~/Library/LaunchAgents")
            legacy_labels = []
            if project_name == "actual-report":
                legacy_labels.append("com.scheduled-script.actual-web-ui")
            
            # Try to remove by label directly in case file is already gone
            for l in legacy_labels:
                subprocess.run(["launchctl", "remove", l], stderr=subprocess.DEVNULL)

            if os.path.exists(agent_dir):
                # Patterns to find and delete plist files
                legacy_patterns = [f"com.scheduled-script.{project_name}*.plist"]
                if project_name == "actual-report":
                    legacy_patterns.append("com.scheduled-script.actual-web-ui.plist")
                
                import glob
                for pattern in legacy_patterns:
                    for legacy_path in glob.glob(os.path.join(agent_dir, pattern)):
                        legacy_name = os.path.basename(legacy_path)
                        # Extract label from filename (remove .plist)
                        legacy_label = legacy_name[:-6] if legacy_name.endswith(".plist") else legacy_name
                        if legacy_name == f"{label}.plist":
                            continue
                        print(f"🧹 Cleaning up legacy agent: {legacy_name}")
                        subprocess.run(["launchctl", "unload", legacy_path], stderr=subprocess.DEVNULL)
                        subprocess.run(["launchctl", "remove", legacy_label], stderr=subprocess.DEVNULL)
                        try:
                            os.remove(legacy_path)
                        except: pass

            # 2. Generate plist
            python_exe = sys.executable
            # Ensure we use absolute path for the server script
            server_script = os.path.abspath(sys.argv[0])
            working_dir = os.getcwd()
            env_from_config = get_env(self.config.extra_env)
            env_vars = {"PATH": env_from_config["PATH"]}
            if self.config.extra_env:
                for k, v in self.config.extra_env.items():
                    if k != "PATH" and v is not None:
                        env_vars[k] = str(v)

            plist_data = {
                "Label": label,
                "ProgramArguments": [
                    python_exe,
                    server_script,
                    "-s",
                    "restart",
                ],
                "WorkingDirectory": working_dir,
                "RunAtLoad": True,
                "AbandonProcessGroup": True,
                "EnvironmentVariables": env_vars,
            }
            with open(plist_path, "wb") as f:
                plistlib.dump(plist_data, f)
            
            # 3. Load it
            subprocess.run(["launchctl", "unload", plist_path], stderr=subprocess.DEVNULL)
            res = subprocess.run(["launchctl", "load", plist_path])
            if res.returncode == 0:
                print(f"{GREEN}{BOLD}Autostart enabled!{RESET}")
                print(f"  {BLUE}Label:{RESET} {label}")
                print(f"  {BLUE}Path:{RESET}  {plist_path}")
            else:
                print(f"{RED}Error loading LaunchAgent.{RESET}")
            
        elif action == "disable":
            if os.path.exists(plist_path):
                subprocess.run(["launchctl", "unload", plist_path], stderr=subprocess.DEVNULL)
                try:
                    os.remove(plist_path)
                    print(f"{YELLOW}Autostart disabled (removed {label}.plist){RESET}")
                except Exception as e:
                    print(f"{RED}Error removing plist: {e}{RESET}")
            else:
                print(f"{YELLOW}Autostart is not enabled.{RESET}")
                
        elif action == "status":
            print(f"{BOLD}Autostart Status for {project_name}:{RESET}")
            if os.path.exists(plist_path):
                print(f"  {GREEN}Enabled:{RESET}  Yes ({plist_path})")
                res = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
                if res.returncode == 0:
                    print(f"  {GREEN}Loaded:{RESET}   Yes (Active in launchctl)")
                else:
                    print(f"  {RED}Loaded:{RESET}    No (Exists but not loaded)")
            else:
                print(f"  {YELLOW}Enabled:{RESET}  No")

    def exec_cmd(self, cmd_args):
        import urllib.request
        import json
        import ssl
        
        RED = "\033[91m"
        GREEN = "\033[92m"
        RESET = "\033[0m"
        
        queue = None
        if len(cmd_args) >= 2 and cmd_args[0] == "--queue":
            queue = cmd_args[1]
            cmd_args = cmd_args[2:]
            
        monitor = False
        if "--monitor" in cmd_args:
            monitor = True
            cmd_args.remove("--monitor")
            
        if cmd_args and cmd_args[0] == "--":
            cmd_args = cmd_args[1:]
            
        env_overrides = {}
        while cmd_args and "=" in cmd_args[0] and not cmd_args[0].startswith("-"):
            key, val = cmd_args[0].split("=", 1)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            env_overrides[key] = val
            cmd_args = cmd_args[1:]
            
        command_str = " ".join(cmd_args)
        if not command_str:
            print(f"{RED}Error: No command provided to exec.{RESET}")
            sys.exit(1)
            
        payload = {
            "command": command_str,
            "is_cron": 0,
            "job_type": "command",
            "queue": queue,
            "job_source": "exec"
        }
        if env_overrides:
            payload["env"] = env_overrides
        
        protocol = "https" if self.config.ssl_dir else "http"
        url = f"{protocol}://127.0.0.1:{self.config.port}/run"
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            req = urllib.request.Request(
                url, 
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as response:
                res_data = json.loads(response.read().decode())
                job_id = res_data.get("job_id")
                log_file = os.path.abspath(os.path.join(self.config.data_dir, "jobs", f"{job_id}.log"))
                print(f"{GREEN}Job #{job_id} successfully queued.{RESET}")
                print(f"Log File: file://{log_file}")
                
                if monitor:
                    print("Monitoring job output...\n")
                    offset = 0
                    import time
                    while True:
                        log_url = f"{protocol}://127.0.0.1:{self.config.port}/api/job/{job_id}/log?offset={offset}"
                        content = ""
                        try:
                            log_req = urllib.request.Request(log_url)
                            with urllib.request.urlopen(log_req, timeout=5, context=ctx) as log_res:
                                log_data = json.loads(log_res.read().decode())
                                content = log_data.get("content", "")
                                if content:
                                    print(content, end="", flush=True)
                                    offset = log_data.get("offset", offset)
                        except Exception:
                            pass
                        
                        stat_url = f"{protocol}://127.0.0.1:{self.config.port}/api/job/{job_id}"
                        try:
                            stat_req = urllib.request.Request(stat_url)
                            with urllib.request.urlopen(stat_req, timeout=5, context=ctx) as stat_res:
                                stat_data = json.loads(stat_res.read().decode())
                                if stat_data.get("status") in ("completed", "failed"):
                                    # Drain any remaining logs one last time if we didn't just get some
                                    if not content:
                                        log_req = urllib.request.Request(log_url)
                                        with urllib.request.urlopen(log_req, timeout=5, context=ctx) as log_res:
                                            log_data = json.loads(log_res.read().decode())
                                            content = log_data.get("content", "")
                                            if content:
                                                print(content, end="", flush=True)
                                    break
                        except Exception:
                            break
                        time.sleep(1)
                    print(f"\n{GREEN}Job finished.{RESET}")
        except Exception as e:
            print(f"{RED}Error: Could not connect to cli-dash server at {url}. Is it running?{RESET}")
            print(f"Details: {e}")
            sys.exit(1)

    def run(self):
        """Parse CLI args and dispatch to start/stop/restart/db init."""
        import argparse

        parser = argparse.ArgumentParser(description=f"{self.config.title} Manager")
        parser.add_argument("-s", "--server", choices=["start", "stop", "restart"],
                            help="Server control")
        parser.add_argument("-p", "--port", type=int, help="Port to run on")
        parser.add_argument("-f", "--force", action="store_true", help="Force kill processes on port")
        parser.add_argument("--ssl-dir", help="SSL directory (Let's Encrypt format)")
        parser.add_argument("--internal-run", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("command", nargs="?", choices=["db", "autostart", "exec"], help="Subcommand")
        parser.add_argument("action", nargs=argparse.REMAINDER, help="Subcommand action")
        args = parser.parse_args()

        if args.port:
            self.config.port = args.port
        if args.ssl_dir:
            self.config.ssl_dir = os.path.abspath(args.ssl_dir)

        if args.server == "start":
            self.start(force=args.force)
        elif args.server == "stop":
            self.stop(force=args.force)
        elif args.server == "restart":
            self.restart(force=args.force)
        elif args.command == "db":
            action = args.action[0] if args.action else None
            if action == "init":
                self.init_db_cmd()
            elif action == "reset":
                db_path = self.db.db_path
                if os.path.exists(db_path):
                    os.remove(db_path)
                self.init_db_cmd()
            elif action == "export":
                self.export_schedules_cmd()
            elif action == "import":
                self.import_schedules_cmd()
            elif action == "truncate":
                until_str = None
                if len(args.action) >= 3 and args.action[1] == "--until":
                    until_str = args.action[2]
                self.truncate_jobs_cmd(until_str)
            else:
                print("Unknown db action")
        elif args.command == "autostart":
            action = args.action[0] if args.action else None
            if action in ["enable", "disable", "status"]:
                self.autostart_cmd(action)
            else:
                print(f"Unknown autostart action: {action}")
                print("Usage: autostart [enable|disable|status]")
        elif args.command == "exec":
            self.exec_cmd(args.action)
        else:
            parser.print_help()
