# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .policy import default_port

# --------------------------------------------------------------------------------------
# Module Setup
# --------------------------------------------------------------------------------------
logger = logging.getLogger("matrix_sdk.runtime")

HOME = Path(os.getenv("MATRIX_HOME") or (Path.home() / ".matrix")).expanduser()
STATE_DIR = HOME / "state"
LOGS_DIR = HOME / "logs"


@dataclass(frozen=True)
class LockInfo:
    """Represents the contents of a .lock file for a running process or connector."""

    pid: int
    port: Optional[int]
    alias: str
    target: str
    started_at: float
    runner_path: str
    url: Optional[str] = None  # NEW: remote URL for connector-type runners


def _maybe_configure_logging() -> None:
    """Attaches a handler to the logger if MATRIX_SDK_DEBUG is set."""
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on") and not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "[matrix-sdk][runtime] %(levelname)s: %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


_maybe_configure_logging()
for d in (STATE_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------------------
# Private Helper Functions
# --------------------------------------------------------------------------------------


def _get_lock_path(alias: str) -> Path:
    """Returns the standardized path to the lock file for an alias."""
    return STATE_DIR / alias / "runner.lock.json"


def _load_lock_info(alias: str) -> Optional[LockInfo]:
    """Safely loads and parses a lock file into a LockInfo object."""
    lock_path = _get_lock_path(alias)
    if not lock_path.is_file():
        return None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        # Backward-compatible: tolerate older locks without 'url'
        if "url" not in data:
            data["url"] = None
        return LockInfo(**data)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("runtime: could not parse lock file for alias '%s': %s", alias, e)
        return None


def _is_port_available(port: int) -> bool:
    """Checks if a TCP port is available to bind to on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_available_port(start_port: int, max_retries: int = 100) -> int:
    """Finds the next available TCP port starting from a given port."""
    port = start_port
    for _ in range(max_retries):
        if _is_port_available(port):
            if port != start_port:
                logger.warning(
                    "runtime: Port %d was occupied. Switched to next available: %d",
                    start_port,
                    port,
                )
            return port
        port += 1
    raise RuntimeError(
        f"Could not find an available port after trying {max_retries} ports "
        f"from {start_port}."
    )


def _get_python_executable(target_path: Path, runner_data: Dict[str, Any]) -> str:
    """Determines the absolute path to the venv Python, failing if not found."""
    venv_dir = runner_data.get("python", {}).get("venv", ".venv")
    py_exe = (
        target_path
        / venv_dir
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if py_exe.is_file():
        logger.debug("runtime: found venv python executable at %s", py_exe)
        return str(py_exe)
    raise FileNotFoundError(f"Python executable not found in venv path: {py_exe}")


def _build_command(target_path: Path, runner: Dict[str, Any]) -> List[str]:
    """Constructs the command list for launching the server process."""
    entry = runner.get("entry")
    if not entry:
        raise ValueError("runner.json is missing the required 'entry' field")

    runner_type = (runner.get("type") or "").lower()
    if runner_type == "python":
        python_executable = _get_python_executable(target_path, runner)
        return [python_executable, entry]
    if runner_type == "node":
        node_executable = os.environ.get("NODE", "node")
        return [node_executable, entry]

    raise RuntimeError(f"Unsupported runner type: '{runner_type}'")


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def log_path(alias: str) -> str:
    """Returns the standardized path to the log file for an alias."""
    return str(LOGS_DIR / f"{alias}.log")


def start(
    target: str, *, alias: Optional[str] = None, port: Optional[int] = None
) -> LockInfo:
    """Starts a server process, or attaches to a remote connector if configured."""
    target_path = Path(target).expanduser().resolve()
    alias = alias or target_path.name
    lock_path = _get_lock_path(alias)

    if lock_path.exists():
        raise RuntimeError(f"Lock file already exists for alias '{alias}'")

    runner_path = target_path / "runner.json"
    if not runner_path.is_file():
        raise FileNotFoundError(f"runner.json not found in {target_path}")

    runner = json.loads(runner_path.read_text(encoding="utf-8"))
    rtype = (runner.get("type") or "").lower()

    # Connector mode: do NOT spawn a process; simply persist the URL
    if rtype == "connector" and runner.get("url"):
        url = str(runner.get("url")).strip()
        lock_info = LockInfo(
            alias=alias,
            pid=0,
            port=None,
            started_at=time.time(),
            target=str(target_path),
            runner_path=str(runner_path),
            url=url,
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(asdict(lock_info), indent=2), encoding="utf-8")
        logger.info(
            "runtime: attached connector '%s' to %s (no local process)", alias, url
        )
        return lock_info

    # Local process mode (python/node etc.)
    command = _build_command(target_path, runner)
    effective_port = _find_available_port(port or default_port())

    env = os.environ.copy()
    env.update(runner.get("env", {}))
    env["PORT"] = str(effective_port)

    logf_path = Path(log_path(alias))
    logger.info("runtime: starting '%s' with command: `%s`", alias, " ".join(command))
    logger.info("runtime: logs at %s", logf_path)

    with open(logf_path, "ab") as log_file:
        child = subprocess.Popen(
            command, cwd=target_path, env=env, stdout=log_file, stderr=log_file
        )

    lock_info = LockInfo(
        alias=alias,
        pid=child.pid,
        port=effective_port,
        started_at=time.time(),
        target=str(target_path),
        runner_path=str(runner_path),
        url=None,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(asdict(lock_info), indent=2), encoding="utf-8")

    logger.info(
        "runtime: process for '%s' started with pid %d on port %d",
        alias,
        child.pid,
        effective_port,
    )
    return lock_info


def stop(alias: str) -> bool:
    """Stops a running process by its alias or detaches a connector."""
    lock_info = _load_lock_info(alias)
    if not lock_info:
        logger.info("runtime: stop for '%s' ignored; no lock file found.", alias)
        return False

    try:
        if lock_info.pid and lock_info.pid > 0:
            logger.info(
                "runtime: stopping process with pid %d for alias '%s'",
                lock_info.pid,
                alias,
            )
            os.kill(lock_info.pid, signal.SIGTERM)
            return True
        else:
            # Connector (no local process)
            logger.info(
                "runtime: '%s' is a connector (no local process to stop)", alias
            )
            return True
    except ProcessLookupError:
        logger.warning(
            "runtime: process with pid %d for alias '%s' already gone.",
            lock_info.pid,
            alias,
        )
        return True  # The process is gone, so the goal is achieved.
    finally:
        _get_lock_path(alias).unlink(missing_ok=True)


def status() -> List[LockInfo]:
    """Lists the status of all running processes managed by the SDK."""
    running_processes: List[LockInfo] = []
    if not STATE_DIR.is_dir():
        return running_processes

    for lock_file in STATE_DIR.glob("*/runner.lock.json"):
        alias = lock_file.parent.name
        if lock_info := _load_lock_info(alias):
            try:
                if lock_info.pid and lock_info.pid > 0:
                    os.kill(lock_info.pid, 0)  # Check if process exists
                running_processes.append(lock_info)
            except ProcessLookupError:
                logger.warning(
                    "runtime: removing stale lock file for dead process: %s", lock_file
                )
                lock_file.unlink(missing_ok=True)
    return running_processes


def tail_logs(alias: str, *, follow: bool = False, n: int = 20) -> Iterator[str]:
    """Tails the log file for a given alias."""
    p = Path(log_path(alias))
    if not p.is_file():
        return

    with p.open("r", encoding="utf-8", errors="replace") as f:
        if follow:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                yield line
        else:
            lines = f.readlines()
            yield from lines[-n:]


def doctor(alias: str, timeout: int = 5) -> Dict[str, Any]:
    """Performs a health check on a running server or connector."""
    lock_info = _load_lock_info(alias)
    if not lock_info:
        return {"status": "fail", "reason": "Server not running (no lock file)."}

    # Connector: we don't control the process; report configured URL
    if lock_info.pid <= 0 and lock_info.url:
        url = lock_info.url
        # Connector: quick probe that won't hang on SSE
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                # Try a HEAD first (fast, common)
                resp = client.head(url, headers={"Accept": "text/event-stream"})
                code = resp.status_code
        except httpx.RequestError:
            # Some SSE servers don't support HEAD; use a streamed GET
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    with client.stream(
                        "GET", url, headers={"Accept": "text/event-stream"}
                    ) as resp:
                        code = resp.status_code
            except httpx.RequestError as e:
                return {
                    "status": "warn",
                    "reason": f"connector configured for {url} but HTTP probe failed: {e}",
                }

        if 200 <= code < 500:
            return {
                "status": "ok",
                "reason": f"connector configured for {url} (HTTP {code})",
            }
        return {"status": "ok", "reason": f"connector configured for {url}"}

    # Local process
    try:
        os.kill(lock_info.pid, 0)  # Check if process is alive
        if not lock_info.port:
            return {
                "status": "ok",
                "reason": f"Process {lock_info.pid} is running (no port to check).",
            }

        url = f"http://127.0.0.1:{lock_info.port}/health"
        logger.debug("doctor: probing health at %s", url)
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
            response.raise_for_status()
            return {
                "status": "ok",
                "reason": f"Responded {response.status_code} from {url}",
            }
    except ProcessLookupError:
        return {"status": "fail", "reason": f"Process {lock_info.pid} not found."}
    except httpx.RequestError as e:
        return {
            "status": "fail",
            "reason": f"HTTP request to health endpoint failed: {e}",
        }
    except Exception as e:
        return {"status": "fail", "reason": f"An unexpected error occurred: {e}"}
