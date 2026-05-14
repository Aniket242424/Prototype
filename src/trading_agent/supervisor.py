"""
In-memory worker supervisor.

Spawns and manages the 3 background workers (market_data, regime, strategy)
as child processes of the FastAPI app. PIDs and Popen handles are held in a
single process-wide instance. PIDs are NOT persisted — on API restart, prior
children become orphans (they'll keep running). On Windows, the launcher
batch (start.bat) restarts the API, so orphans only happen on hard crashes.

Logs for each worker are tailed to logs/<name>.log so the dashboard can
surface recent lines without scraping a console window.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from trading_agent.core.config import REPO_ROOT
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist

log = get_logger(__name__)


WORKERS: dict[str, str] = {
    "market_data": "trading_agent.market_data.worker",
    "regime": "trading_agent.regime.worker",
    "strategy": "trading_agent.strategy.worker",
}


class WorkerSupervisor:
    """Single-instance, process-local supervisor for the 3 workers."""

    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
        self._started_at: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._log_dir = REPO_ROOT / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ----------------- public API -----------------

    def start(self, name: str) -> dict:
        if name not in WORKERS:
            return {"ok": False, "error": f"unknown worker: {name}"}
        with self._lock:
            if self._is_alive(name):
                return {
                    "ok": True,
                    "status": "already_running",
                    "pid": self._procs[name].pid,
                    "started_at": self._started_at[name].isoformat(),
                }
            proc = self._spawn(name)
            self._procs[name] = proc
            self._started_at[name] = now_ist()
            log.info("supervisor.started", worker=name, pid=proc.pid)
            return {
                "ok": True,
                "status": "started",
                "pid": proc.pid,
                "started_at": self._started_at[name].isoformat(),
            }

    def stop(self, name: str, timeout: int = 10) -> dict:
        if name not in WORKERS:
            return {"ok": False, "error": f"unknown worker: {name}"}
        with self._lock:
            proc = self._procs.get(name)
            if proc is None or proc.poll() is not None:
                self._procs.pop(name, None)
                self._started_at.pop(name, None)
                return {"ok": True, "status": "not_running"}
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                exit_code = proc.returncode
            self._procs.pop(name, None)
            self._started_at.pop(name, None)
            log.info("supervisor.stopped", worker=name, exit_code=exit_code)
            return {"ok": True, "status": "stopped", "exit_code": exit_code}

    def restart(self, name: str) -> dict:
        self.stop(name)
        return self.start(name)

    def start_all(self) -> dict:
        return {n: self.start(n) for n in WORKERS}

    def stop_all(self) -> dict:
        return {n: self.stop(n) for n in WORKERS}

    def restart_all(self) -> dict:
        self.stop_all()
        return self.start_all()

    def status(self) -> dict:
        out = {}
        for name in WORKERS:
            proc = self._procs.get(name)
            alive = proc is not None and proc.poll() is None
            out[name] = {
                "running": alive,
                "pid": proc.pid if proc else None,
                "exit_code": (proc.returncode if (proc and not alive) else None),
                "started_at": (
                    self._started_at[name].isoformat()
                    if (alive and name in self._started_at) else None
                ),
                "log_path": str(self._log_dir / f"{name}.log"),
            }
        return out

    def tail_log(self, name: str, lines: int = 50) -> list[str]:
        if name not in WORKERS:
            return []
        path = self._log_dir / f"{name}.log"
        if not path.exists():
            return []
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # Read last ~16 KB; enough for `lines` rows in practice
                read = min(size, 16 * 1024)
                f.seek(size - read)
                buf = f.read().decode("utf-8", errors="replace")
            return buf.splitlines()[-lines:]
        except Exception as e:
            log.warning("supervisor.tail_failed", worker=name, error=str(e))
            return []

    # ----------------- internals -----------------

    def _is_alive(self, name: str) -> bool:
        proc = self._procs.get(name)
        return proc is not None and proc.poll() is None

    def _spawn(self, name: str) -> subprocess.Popen:
        module = WORKERS[name]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        log_path = self._log_dir / f"{name}.log"
        log_fh = open(log_path, "ab")  # noqa: SIM115 — owned by child stdout
        # Windows: detach from API console so Ctrl+C on API doesn't kill workers
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [sys.executable, "-u", "-m", module],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
            env=env,
            creationflags=creationflags,
        )


# ---- module-level singleton (one supervisor per API process) ----
_supervisor: WorkerSupervisor | None = None


def get_supervisor() -> WorkerSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = WorkerSupervisor()
    return _supervisor
