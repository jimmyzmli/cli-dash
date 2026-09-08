"""Process and service management for cli-dash."""

import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Union

from cli_dash.utils import get_env, rotate_log_if_needed


def is_pid_running(pid: int) -> bool:
    """Check if a process is running by PID."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            cmd = ["tasklist", "/FI", f"PID eq {pid}", "/NH"]
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, shell=True,
                creationflags=0x08000000
            )
            return str(pid) in res.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def terminate_process(pid: int, timeout: float = 3.0, force: bool = True) -> bool:
    """
    Gracefully terminate a process tree by PID, escalating to SIGKILL
    if it does not exit within the timeout period.
    """
    if not pid or not is_pid_running(pid):
        return True

    if os.name == "nt":
        res = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, creationflags=0x08000000, shell=True
        )
        return res.returncode == 0
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return True

        if timeout > 0:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not is_pid_running(pid):
                    return True
                time.sleep(0.1)

        if force and is_pid_running(pid):
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.1)

        return not is_pid_running(pid)


def find_pids_on_port(port: int) -> List[int]:
    """Find all PIDs listening on the specified port."""
    if not port:
        return []
    pids = []
    try:
        if os.name == "nt":
            res = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True)
            for line in res.stdout.strip().split("\n"):
                parts = line.strip().split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) != 0:
                        pids.append(int(pid))
        else:
            res = subprocess.run(["lsof", "-t", f"-i:{port}"], capture_output=True, text=True)
            for pid_str in res.stdout.strip().split():
                if pid_str.isdigit():
                    pids.append(int(pid_str))
    except Exception as e:
        logging.warning(f"Error finding PIDs on port {port}: {e}")
    return sorted(list(set(pids)))


def kill_port_zombies(port: int, exclude_pid: Optional[int] = None) -> List[int]:
    """Kill rogue processes listening on port, excluding exclude_pid."""
    killed = []
    pids = find_pids_on_port(port)
    for pid in pids:
        if exclude_pid is None or pid != exclude_pid:
            try:
                terminate_process(pid, timeout=1.0, force=True)
                killed.append(pid)
                logging.info(f"Cleaned up zombie process (PID {pid}) on port {port}.")
            except Exception:
                pass
    return killed


def kill_ports_zombies(ports: List[int], exclude_pid: Optional[int] = None) -> Dict[int, List[int]]:
    """Kill rogue processes on multiple ports."""
    results = {}
    for port in set(ports):
        if port:
            results[port] = kill_port_zombies(port, exclude_pid=exclude_pid)
    return results


def parse_services_config(config_data: Any) -> List[dict]:
    """Normalize services configuration to a list of dicts."""
    if isinstance(config_data, list):
        return config_data
    elif isinstance(config_data, dict):
        if "command" in config_data:
            return [config_data]
        else:
            services = []
            for k, v in config_data.items():
                if isinstance(v, dict):
                    if "name" not in v:
                        v["name"] = k
                    services.append(v)
            return services
    return []


def load_services_config(services_config_path: str) -> List[dict]:
    """Safely load and normalize services from services.json."""
    if not services_config_path or not os.path.exists(services_config_path):
        return []
    try:
        with open(services_config_path, "r") as f:
            config_data = json.load(f)
            return parse_services_config(config_data)
    except Exception as e:
        logging.error(f"Error reading {services_config_path}: {e}")
        return []


def get_service_ports(services_config_path: str) -> List[int]:
    """Extract all service ports defined in services.json."""
    services = load_services_config(services_config_path)
    ports = []
    for srv in services:
        port = srv.get("port")
        if port and port not in ports:
            ports.append(port)
    return ports


class ServiceManager:
    """
    Manages long-running services defined in services.json (e.g. MCP servers).
    Handles spawning, graceful restarts, health check supervision, and status reporting.
    """

    def __init__(self):
        self._processes: Dict[str, Optional[subprocess.Popen]] = {}
        self._states: Dict[str, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_command(cmd: str, env: dict) -> str:
        resolved = shutil.which(cmd, path=env.get("PATH"))
        if not resolved and cmd == "node":
            nvm_nodes = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")), reverse=True)
            if nvm_nodes:
                resolved = nvm_nodes[0]
            elif os.path.exists("/opt/homebrew/bin/node"):
                resolved = "/opt/homebrew/bin/node"
            elif os.path.exists("/usr/local/bin/node"):
                resolved = "/usr/local/bin/node"
        return resolved or cmd

    def spawn_service(self, srv: dict, log_dir: str, extra_env: Optional[dict] = None) -> Optional[subprocess.Popen]:
        """Spawn a service defined in services.json, cleaning up port zombies and setting cwd."""
        name = srv.get("name", "Unknown")
        cmd = srv.get("command", "")
        if not cmd:
            return None

        cmd = os.path.expanduser(cmd) if isinstance(cmd, str) and cmd.startswith("~") else str(cmd)
        args_list = srv.get("args", [])
        args_list = [os.path.expanduser(a) if isinstance(a, str) and a.startswith("~") else str(a) for a in args_list]

        port = srv.get("port")
        if port:
            kill_port_zombies(port)

        combined_env = {}
        if extra_env:
            combined_env.update(extra_env)
        if "env" in srv and isinstance(srv["env"], dict):
            combined_env.update(srv["env"])

        env = get_env(combined_env)
        resolved_cmd = self._resolve_command(cmd, env)
        full_cmd = [resolved_cmd] + args_list

        cwd = srv.get("cwd")
        if cwd:
            cwd = os.path.expanduser(cwd) if isinstance(cwd, str) and cwd.startswith("~") else str(cwd)
            if not os.path.exists(cwd):
                cwd = None

        log_path = os.path.join(log_dir, f"service-{name}.log")
        rotate_log_if_needed(log_path)
        out_file = open(log_path, "a")

        try:
            proc = subprocess.Popen(
                full_cmd,
                cwd=cwd,
                env=env,
                stdout=out_file,
                stderr=subprocess.STDOUT
            )
            with self._lock:
                self._processes[name] = proc
                self._states[name] = {
                    "status": "starting",
                    "pid": proc.pid,
                    "started_at": time.time(),
                    "last_check": time.time(),
                    "failures": 0,
                    "port": port
                }
            logging.info(f"Service '{name}' started (PID {proc.pid}) with cwd: {cwd or os.getcwd()}.")
            return proc
        except Exception as e:
            logging.error(f"Failed to start service '{name}': {e}")
            with self._lock:
                self._processes[name] = None
                self._states[name] = {
                    "status": "stopped",
                    "pid": None,
                    "started_at": time.time(),
                    "last_check": time.time(),
                    "failures": 0,
                    "port": port
                }
            return None

    def stop_service(self, name: str, port: Optional[int] = None, wait_ms: int = 5000) -> None:
        """Gracefully terminate a service process and clean up lingering port listeners."""
        with self._lock:
            proc = self._processes.get(name)
            self._processes[name] = None

        if proc:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGTERM)
            except Exception:
                pass

            try:
                proc.wait(timeout=wait_ms / 1000.0)
            except (subprocess.TimeoutExpired, Exception):
                try:
                    proc.kill()
                except Exception:
                    pass

        if port:
            kill_port_zombies(port)

        with self._lock:
            if name in self._states:
                self._states[name]["status"] = "stopped"
                self._states[name]["pid"] = None

    def restart_service_async(self, name: str, services_config_path: str, log_dir: str, extra_env: Optional[dict] = None) -> bool:
        """Restart a service by name asynchronously in a background thread."""
        services = load_services_config(services_config_path)
        matching_srv = None
        for srv in services:
            if srv.get("name") == name:
                matching_srv = srv
                break

        if not matching_srv:
            return False

        wait_ms = matching_srv.get("SIGKILL_WAIT_MS", 5000)
        port = matching_srv.get("port")

        def restart_worker():
            self.stop_service(name, port=port, wait_ms=wait_ms)
            self.spawn_service(matching_srv, log_dir, extra_env)

        threading.Thread(target=restart_worker, daemon=True).start()
        return True

    def get_services_status(self, services_config_path: str) -> List[dict]:
        """Return real-time status of all enabled services for /api/config."""
        services = load_services_config(services_config_path)
        results = []
        for srv in services:
            if not srv.get("enabled", True):
                continue
            name = srv.get("name", "MCP")
            port = srv.get("port", 9991)
            with self._lock:
                state = self._states.get(name, {})
                proc = self._processes.get(name)
                is_alive = (proc is not None and proc.poll() is None)
                if is_alive:
                    pid = proc.pid
                    status = state.get("status", "starting")
                else:
                    pid = None
                    status = "stopped"

            results.append({
                "name": name,
                "port": port,
                "pid": pid,
                "status": status,
                "is_healthy": (status == "healthy")
            })
        return results

    def run_manager_loop(self, services_config_path: str, log_dir: str, extra_env: Optional[dict] = None):
        """Monitor and supervise services defined in services.json."""
        while True:
            services = load_services_config(services_config_path)
            for srv in services:
                if not srv.get("enabled", True):
                    continue

                name = srv.get("name", "Unknown")
                port = srv.get("port")
                with self._lock:
                    proc = self._processes.get(name)

                if proc is None or proc.poll() is not None:
                    if proc is not None:
                        logging.warning(f"Service '{name}' exited with code {proc.returncode}. Restarting...")
                    else:
                        logging.info(f"Starting service '{name}'...")
                    self.spawn_service(srv, log_dir, extra_env)
                else:
                    hc = srv.get("health_check")
                    if hc and hc.get("enabled", True) and hc.get("type") == "http":
                        if port:
                            path = hc.get("path", "/health")
                            timeout = hc.get("timeout_seconds", srv.get("timeout_seconds", 3))
                            max_failures = hc.get("max_failures", srv.get("max_failures", 4))
                            initial_delay = hc.get("initial_delay_seconds", srv.get("initial_delay_seconds", 15))
                            url = f"http://localhost:{port}{path}"

                            with self._lock:
                                state = self._states.setdefault(name, {})
                                uptime = time.time() - state.get("started_at", 0)

                            is_healthy = False
                            try:
                                req = urllib.request.Request(url)
                                with urllib.request.urlopen(req, timeout=timeout) as response:
                                    if response.status == 200:
                                        is_healthy = True
                            except Exception:
                                pass

                            with self._lock:
                                state["last_check"] = time.time()
                                state["pid"] = proc.pid
                                state["port"] = port

                                if is_healthy:
                                    state["status"] = "healthy"
                                    state["failures"] = 0
                                else:
                                    if uptime < initial_delay:
                                        state["status"] = "starting"
                                        state["failures"] = 0
                                    else:
                                        fails = state.get("failures", 0) + 1
                                        state["failures"] = fails
                                        state["status"] = "unhealthy" if fails < max_failures else "stopped"
                                        logging.warning(f"Service '{name}' health check failed ({fails}/{max_failures}) on {url}")
                                        if fails >= max_failures:
                                            logging.error(f"Service '{name}' exceeded max health check failures ({fails}). Restarting gracefully...")
                                            self.stop_service(name, port=port)

            time.sleep(5)


service_manager = ServiceManager()
