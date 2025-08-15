# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import os
import signal
import socket  # --- NEW: Import socket to check ports ---
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx
from .policy import default_port

# --- IMPROVEMENT: Centralized, consistent logging ---
logger = logging.getLogger("matrix_sdk.runtime")

def _maybe_configure_logging() -> None:
    # Mirrors the setup in other SDK modules for consistency.
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on"):
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[matrix-sdk][runtime] %(levelname)s: %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

_maybe_configure_logging()

# --- IMPROVEMENT: Encapsulated path management ---
HOME = Path(os.getenv("MATRIX_HOME") or (Path.home() / ".matrix")).expanduser()
STATE_DIR = HOME / "state"
LOGS_DIR = HOME / "logs"

for d in (STATE_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- IMPROVEMENT: Clearer data structures ---
@dataclass(frozen=True)
class LockInfo:
    """Represents the contents of a .lock file for a running process."""
    pid: int
    port: Optional[int]
    alias: str
    target: str
    started_at: float
    runner_path: str

# --- NEW: Helper function to check port availability ---
def _is_port_available(port: int) -> bool:
    """Checks if a TCP port is available to bind to on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            # Try to bind to the port. If it succeeds, the port is free.
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            # The port is already in use.
            return False

def _get_python_executable(target_path: Path, runner_data: Dict[str, Any]) -> str:
    """
    Determines the absolute path to the Python executable within the project's
    virtual environment. Fails if it's not found.
    """
    venv_dir_name = runner_data.get("python", {}).get("venv", ".venv")
    venv_path = target_path / venv_dir_name
    py_exe = venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    if py_exe.is_file():
        logger.debug("runtime: found venv python executable at %s", py_exe)
        return str(py_exe)

    raise FileNotFoundError(
        f"Python executable not found in expected venv path: {py_exe}. "
        "Ensure the environment was prepared correctly."
    )

def log_path(alias: str) -> str:
    """Returns the standardized path to the log file for an alias."""
    return str(LOGS_DIR / f"{alias}.log")

def start(
    target: str, *, alias: Optional[str] = None, port: Optional[int] = None
) -> LockInfo:
    """Starts a server process, finding an available port if necessary."""
    target_path = Path(target).expanduser().resolve()
    alias = alias or target_path.name
    lock_path = STATE_DIR / alias / "runner.lock.json"

    if lock_path.exists():
        raise RuntimeError(f"Lock file already exists for alias '{alias}' at {lock_path}")

    runner_path = target_path / "runner.json"
    if not runner_path.exists():
        raise FileNotFoundError(f"Cannot start server: runner.json not found in {target_path}")

    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    entry = runner.get("entry")
    if not entry:
        raise ValueError("runner.json is missing the required 'entry' field")

    runner_type = (runner.get("type") or "").lower()
    command: List[str]

    if runner_type == "python":
        python_executable = _get_python_executable(target_path, runner)
        command = [python_executable, entry]
    elif runner_type == "node":
        node_executable = os.environ.get("NODE", "node")
        command = [node_executable, entry]
    else:
        raise RuntimeError(f"Unsupported runner type: '{runner_type}'")

    # --- MODIFIED: Find an available port before starting ---
    initial_port = port or default_port()
    effective_port = initial_port
    max_retries = 100  # Prevent an infinite loop

    for i in range(max_retries):
        if _is_port_available(effective_port):
            break  # Found a free port
        logger.debug("runtime: Port %d is occupied, trying next.", effective_port)
        effective_port += 1
    else:
        # This 'else' runs if the loop finishes without a 'break'
        raise RuntimeError(f"Could not find an available port after trying {max_retries} ports from {initial_port}.")

    if effective_port != initial_port:
        logger.warning(
            "runtime: Port %d was occupied. Switched to the next available port: %d",
            initial_port,
            effective_port,
        )

    env = os.environ.copy()
    env.update(runner.get("env") or {})
    env["PORT"] = str(effective_port) # Use the final, available port
    
    # Spawn the process
    logf_path = Path(log_path(alias))
    logger.info("runtime: starting server for alias '%s'. Command: `%s`", alias, " ".join(command))
    logger.info("runtime: logs will be written to %s", logf_path)
    
    with open(logf_path, "ab") as log_file:
        child = subprocess.Popen(command, cwd=str(target_path), env=env, stdout=log_file, stderr=log_file)

    lock_info = LockInfo(
        alias=alias,
        pid=child.pid,
        port=effective_port,
        started_at=time.time(),
        target=str(target_path),
        runner_path=str(runner_path),
    )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(asdict(lock_info), indent=2), encoding="utf-8")
    
    logger.info("runtime: process for '%s' started with pid %d on port %d", alias, child.pid, effective_port)
    return lock_info

def stop(alias: str) -> bool:
    """Stops a running process by its alias."""
    lock_path = STATE_DIR / alias / "runner.lock.json"
    if not lock_path.exists():
        logger.info("runtime: stop command for '%s' ignored, no lock file found.", alias)
        return False

    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        if not pid:
            return False

        logger.info("runtime: stopping process with pid %d for alias '%s'", pid, alias)
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        logger.warning("runtime: process with pid %d for alias '%s' not found.", pid, alias)
        return True
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("runtime: could not parse lock file for alias '%s': %s", alias, e)
        return False
    finally:
        lock_path.unlink(missing_ok=True)

def status() -> List[LockInfo]:
    """Lists the status of all running processes managed by the SDK."""
    running_processes: List[LockInfo] = []
    if not STATE_DIR.exists():
        return running_processes

    for lock_file in STATE_DIR.glob("*/runner.lock.json"):
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid:
                os.kill(pid, 0)
            running_processes.append(LockInfo(**data))
        except (json.JSONDecodeError, TypeError, ProcessLookupError, KeyError):
            logger.warning("runtime: removing stale lock file: %s", lock_file)
            lock_file.unlink(missing_ok=True)
            continue
    return running_processes

def tail_logs(alias: str, *, follow: bool = False, n: int = 20) -> Iterator[str]:
    """Tails the log file for a given alias."""
    p = Path(log_path(alias))
    if not p.exists():
        return

    with p.open("r", encoding="utf-8", errors="replace") as f:
        if not follow:
            lines = f.readlines()
            for line in lines[-n:]:
                yield line
            return
        
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            yield line

def doctor(alias: str, timeout: int = 5) -> Dict[str, Any]:
    """Performs a health check on a running server."""
    lock_path = STATE_DIR / alias / "runner.lock.json"
    if not lock_path.exists():
        return {"status": "fail", "reason": "Server not running (no lock file)."}

    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        port = data.get("port")
        pid = data.get("pid")

        if pid:
            os.kill(pid, 0)

        if not port:
            return {"status": "ok", "reason": f"Process {pid} is running (no port to check)."}

        url = f"http://127.0.0.1:{port}/health"
        logger.debug("doctor: probing health at %s", url)
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            return {"status": "ok", "reason": f"Responded {response.status_code} from {url}"}
    except ProcessLookupError:
        return {"status": "fail", "reason": f"Process {pid} not found."}
    except httpx.RequestError as e:
        return {"status": "fail", "reason": f"HTTP request to health endpoint failed: {e}"}
    except Exception as e:
        return {"status": "fail", "reason": f"An unexpected error occurred: {e}"}