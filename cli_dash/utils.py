from datetime import datetime, timedelta
import os
from typing import Optional


def is_windows() -> bool:
    """Return True if running on Windows and FORCE_POSIX is not enabled."""
    if os.environ.get("FORCE_POSIX", "").lower() in ("1", "true", "yes", "posix"):
        return False
    return os.name == "nt"


def normalize_path(p: Optional[str]) -> Optional[str]:
    r"""Normalize Windows drive paths (e.g. C:\...) to POSIX / WSL paths (/mnt/c/...) if not running on Windows."""
    if not p:
        return p
    if not is_windows():
        import re
        m = re.match(r"^([A-Za-z]):[/\\](.*)", str(p))
        if m:
            drive = m.group(1).lower()
            rest = m.group(2).replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
    return p


def get_env(extra_env: Optional[dict] = None) -> dict:
    """
    Construct a clean execution environment with Python executable dir
    prepended to PATH and any extra_env merged.
    """
    import os
    import sys

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # Prepend virtualenv / Python binary directory to PATH
    sys_exe_dir = os.path.dirname(sys.executable)
    if sys_exe_dir:
        path_val = env.get("PATH", "")
        env["PATH"] = f"{sys_exe_dir}{os.pathsep}{path_val}" if path_val else sys_exe_dir

    if extra_env:
        for k, v in extra_env.items():
            if v is not None:
                if k == "PATH":
                    # Prepend custom PATH (e.g. from server.json)
                    expanded = os.path.expanduser(str(v))
                    curr = env.get("PATH", "")
                    env["PATH"] = f"{expanded}{os.pathsep}{curr}" if curr else expanded
                else:
                    env[k] = str(v)

    return env


def expand_env_vars(obj):
    """
    Recursively expand ${env:VAR} or ${env:VAR:-default} syntax in strings, dicts, and lists.
    """
    import os
    import re

    pattern = re.compile(r"\$\{env:([A-Za-z0-9_]+)(?::-(.*?))?\}")

    def _replace_str(s: str):
        m = pattern.fullmatch(s)
        if m:
            var_name = m.group(1)
            default_val = m.group(2) if m.group(2) is not None else ""
            val = os.environ.get(var_name)
            if val is not None and val != "":
                return val
            if default_val.startswith("${env:"):
                return _replace_str(default_val)
            return default_val
        def _sub(match):
            var_name = match.group(1)
            default_val = match.group(2) if match.group(2) is not None else ""
            val = os.environ.get(var_name)
            if val is not None and val != "":
                return val
            if default_val.startswith("${env:"):
                return _replace_str(default_val)
            return default_val
        return pattern.sub(_sub, s)

    if isinstance(obj, str):
        return _replace_str(obj)
    elif isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [expand_env_vars(item) for item in obj]
    return obj


def get_next_cron_run(cron_expr: str, base_time: datetime) -> datetime:
    """
    Calculate the next run time for a cron expression, supporting L (last) extensions.
    - L in DOM field: Last day of the month (native croniter)
    - LW in DOM field: Last weekday of the month
    - dL in DOW field: Last day-of-week d of the month (e.g. 5L for last Friday)
    """
    from croniter import croniter
    
    # Normalize
    cron_expr = cron_expr.strip()
    fields = cron_expr.split()
    if len(fields) != 5:
        return croniter(cron_expr, base_time).get_next(datetime)
    
    dom = fields[2].upper()
    dow = fields[4].upper()
    
    # 1. Handle LW (Last Weekday of month) in DOM field
    if dom == "LW":
        # Strategy: Use croniter with 'L' in DOM and the original minutes/hours.
        # If the result is a weekend, adjust it back.
        # If the adjusted result is <= base_time, get the NEXT 'L' and adjust again.
        new_expr = " ".join(fields[:2] + ["L"] + fields[3:])
        it = croniter(new_expr, base_time)
        while True:
            candidate = it.get_next(datetime)
            lw = candidate
            if lw.weekday() == 5: # Saturday
                lw -= timedelta(days=1)
            elif lw.weekday() == 6: # Sunday
                lw -= timedelta(days=2)
            
            if lw > base_time:
                return lw

    # 2. Handle dL (Last day-of-week d) in DOW field
    if dow.endswith('L') and len(dow) > 1 and dow[:-1].isdigit():
        target_dow = dow[:-1]
        new_expr = " ".join(fields[:4] + [target_dow])
        it = croniter(new_expr, base_time)
        while True:
            candidate = it.get_next(datetime)
            # Check if adding 7 days moves it to a different month
            if (candidate + timedelta(days=7)).month != candidate.month:
                return candidate

    # 3. Native croniter (handles L in DOM automatically)
    return croniter(cron_expr, base_time).get_next(datetime)


def rotate_log_if_needed(log_path: str, max_lines: int = 1000):
    """Rotates the log file if it exceeds max_lines, keeping all backups."""
    import os
    import glob
    import shutil
    import logging

    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = sum(1 for _ in f)
        if lines >= max_lines:
            backups = glob.glob(f"{log_path}.*")
            indices = []
            for b in backups:
                try:
                    indices.append(int(b.split('.')[-1]))
                except ValueError:
                    pass
            max_idx = max(indices) if indices else 0
            for i in range(max_idx, 0, -1):
                old_log = f"{log_path}.{i}"
                new_log = f"{log_path}.{i+1}"
                if os.path.exists(old_log):
                    try:
                        os.replace(old_log, new_log)
                    except OSError:
                        pass
            shutil.copy2(log_path, f"{log_path}.1")
            with open(log_path, "w", encoding="utf-8") as f:
                pass
    except Exception as e:
        logging.error(f"Error rotating log {log_path}: {e}")


