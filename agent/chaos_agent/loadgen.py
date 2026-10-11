"""k6 load-generator process: the "fault" of a traffic_spike experiment.

Safety mirrors Chaos Mesh: the k6 script has a fixed duration, so the load ends by itself even if the agent dies;
stopping the process is the kill switch; a pid file lets `chaos-agent abort` find it from another shell.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import config as cfg
from .models import LoadSummary

PIDFILE = cfg.STATE_DIR / "k6.pid"


class K6Process:
    def __init__(self, script: str, run_dir: Path) -> None:
        self.script = script
        self.script_path = run_dir / "load.js"
        self.log_path = run_dir / "k6.log"
        self.summary_path = run_dir / "k6-summary.json"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.script_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path.write_text(self.script)
        log = open(self.log_path, "w")
        # Own session: a Ctrl-C in the agent's terminal reaches the agent (which stops k6 deliberately), not k6.
        self.proc = subprocess.Popen([cfg.K6_BIN, "run", "--quiet", "--no-color", str(self.script_path)],
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(self.proc.pid))

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def stop(self) -> bool:
        """Kill switch: SIGINT (k6 stops gracefully and writes its summary), SIGKILL if it doesn't."""
        if self.proc is None:
            return True
        ok = _stop(self.proc.pid, lambda: self.proc.poll() is not None)
        self.forget()
        return ok

    def forget(self) -> None:
        if self.proc is not None and self.proc.poll() is not None and PIDFILE.exists():
            PIDFILE.unlink(missing_ok=True)

    def summary(self, users_target: int) -> LoadSummary:
        return read_summary(self.summary_path, users_target)


def _stop(pid: int, exited) -> bool:
    for sig, wait in ((signal.SIGINT, cfg.K6_STOP_TIMEOUT_S), (signal.SIGKILL, 3)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        deadline = time.time() + wait
        while time.time() < deadline:
            if exited():
                return True
            time.sleep(0.2)
    return exited()


def _alive(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"k6" in cmdline


def active_pid() -> int | None:
    """PID of a k6 load started by the agent and still running (stale pid files are removed)."""
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if _alive(pid):
        return pid
    PIDFILE.unlink(missing_ok=True)
    return None


def stop_active() -> int | None:
    """Used by `chaos-agent abort` from another shell. Returns the stopped PID, if any."""
    pid = active_pid()
    if pid is None:
        return None
    _stop(pid, lambda: not _alive(pid))
    PIDFILE.unlink(missing_ok=True)
    return pid


def read_summary(path: Path, users_target: int) -> LoadSummary:
    s = LoadSummary(users_target=users_target)
    try:
        m = json.loads(path.read_text())["metrics"]
    except (OSError, ValueError, KeyError):
        return s
    get = lambda name, key: ((m.get(name) or {}).get("values") or {}).get(key)
    s.requests = get("http_reqs", "count")
    s.rps = None if get("http_reqs", "rate") is None else round(get("http_reqs", "rate"), 1)
    s.failed_rate = None if get("http_req_failed", "rate") is None else round(get("http_req_failed", "rate"), 4)
    s.p95_ms = None if get("http_req_duration", "p(95)") is None else round(get("http_req_duration", "p(95)"), 1)
    s.max_vus = get("vus_max", "max")
    return s
