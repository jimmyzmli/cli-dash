"""
Core database module for cli-dash.
Manages jobs and schedules tables in a SQLite database.
"""

import sqlite3
import os
import time
from datetime import datetime

# Sentinel for PATCH updates where a field can be cleared to None / null.
NOT_SET = object()


class Database:
    """SQLite-backed storage for jobs and cron schedules."""

    def __init__(self, db_path="data/web-ui.sqlite"):
        self.db_path = db_path

    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                status TEXT NOT NULL,
                output TEXT,
                pid INTEGER,
                is_cron INTEGER DEFAULT 0,
                job_type TEXT DEFAULT 'command',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                queue_name TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                command TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_run TIMESTAMP,
                next_run TIMESTAMP,
                catch_up INTEGER DEFAULT 0,
                queue_name TEXT
            )
        ''')

        # Migrations for existing databases
        for col, table, default in [
            ("is_cron", "jobs", "0"),
            ("job_type", "jobs", "'command'"),
            ("output", "jobs", None),
            ("queue_name", "jobs", "NULL"),
            ("catch_up", "schedules", "0"),
            ("queue_name", "schedules", "NULL"),
        ]:
            try:
                if default == "NULL":
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                else:
                    default_clause = f" DEFAULT {default}" if default is not None else ""
                    col_type = "INTEGER" if default and default.isdigit() else "TEXT"
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}{default_clause}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Rename legacy columns
        for old, new, table in [
            ("created", "created_at", "jobs"),
            ("finished", "finished_at", "jobs"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            except sqlite3.OperationalError:
                pass

        # Stale jobs are now handled by the server's recovery logic
        conn.commit()
        conn.close()

    # --- Job CRUD ---

    def create_job(self, command, is_cron=0, job_type='command', queue_name=None):
        for _ in range(5):
            try:
                conn = self._connect()
                c = conn.cursor()
                c.execute(
                    'INSERT INTO jobs (command, status, is_cron, job_type, queue_name) VALUES (?, ?, ?, ?, ?)',
                    (command, 'pending', is_cron, job_type, queue_name),
                )
                job_id = c.lastrowid
                conn.commit()
                conn.close()
                return job_id
            except sqlite3.OperationalError as e:
                if "locked" in str(e):
                    time.sleep(0.1)
                    continue
                raise

    def update_job(self, job_id, status=None, output=None, pid=None, finished=False):
        for _ in range(5):
            try:
                conn = self._connect()
                c = conn.cursor()
                if status:
                    c.execute('UPDATE jobs SET status = ? WHERE id = ?', (status, job_id))
                if output is not None:
                    c.execute('UPDATE jobs SET output = ? WHERE id = ?', (output, job_id))
                if pid is not None:
                    c.execute('UPDATE jobs SET pid = ? WHERE id = ?', (pid, job_id))
                if finished:
                    c.execute('UPDATE jobs SET finished_at = CURRENT_TIMESTAMP WHERE id = ?', (job_id,))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e):
                    time.sleep(0.1)
                    continue
                raise

    def get_jobs(self, limit=50, offset=0):
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?', (limit, offset))
        jobs = [dict(row) for row in c.fetchall()]
        conn.close()
        return jobs

    def get_job(self, job_id):
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT * FROM jobs WHERE id = ?', (job_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def clear_jobs(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute('DELETE FROM jobs')
        conn.commit()
        conn.close()

    def delete_job(self, job_id):
        conn = self._connect()
        c = conn.cursor()
        c.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
        conn.commit()
        conn.close()

    def get_running_jobs(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE status IN ('running', 'pending')")
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows

    def get_running_jobs_in_queue(self, queue_name):
        conn = self._connect()
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE queue_name = ? AND status = 'running'", (queue_name,))
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows

    def get_next_pending_job_in_queue(self, queue_name):
        conn = self._connect()
        c = conn.cursor()
        c.execute("SELECT * FROM jobs WHERE queue_name = ? AND status = 'pending' ORDER BY id ASC LIMIT 1", (queue_name,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    # --- Schedule CRUD ---

    def get_schedules(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT * FROM schedules')
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows

    def create_schedule(self, label, command, cron_expr, catch_up=0, queue_name=None):
        conn = self._connect()
        c = conn.cursor()
        c.execute(
            'INSERT INTO schedules (label, command, cron_expr, catch_up, queue_name) VALUES (?, ?, ?, ?, ?)',
            (label, command, cron_expr, 1 if catch_up else 0, queue_name),
        )
        conn.commit()
        conn.close()

    def update_schedule(self, schedule_id, label=None, command=None, cron_expr=None,
                        enabled=None, last_run=None, next_run=None, catch_up=None, queue_name=NOT_SET):
        conn = self._connect()
        c = conn.cursor()
        if label is not None:
            c.execute('UPDATE schedules SET label = ? WHERE id = ?', (label, schedule_id))
        if command is not None:
            c.execute('UPDATE schedules SET command = ? WHERE id = ?', (command, schedule_id))
        if cron_expr is not None:
            c.execute('UPDATE schedules SET cron_expr = ? WHERE id = ?', (cron_expr, schedule_id))
        if enabled is not None:
            c.execute('UPDATE schedules SET enabled = ? WHERE id = ?', (1 if enabled else 0, schedule_id))
        if last_run:
            c.execute('UPDATE schedules SET last_run = ? WHERE id = ?', (last_run, schedule_id))
        if next_run:
            c.execute('UPDATE schedules SET next_run = ? WHERE id = ?', (next_run, schedule_id))
        if catch_up is not None:
            c.execute('UPDATE schedules SET catch_up = ? WHERE id = ?', (1 if catch_up else 0, schedule_id))
        if queue_name is not NOT_SET:
            c.execute('UPDATE schedules SET queue_name = ? WHERE id = ?', (queue_name, schedule_id))
        conn.commit()
        conn.close()

    def delete_schedule(self, schedule_id):
        conn = self._connect()
        c = conn.cursor()
        c.execute('DELETE FROM schedules WHERE id = ?', (schedule_id,))
        conn.commit()
        conn.close()
