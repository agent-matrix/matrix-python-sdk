# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .policy import default_port

HOME = Path(os.getenv("MATRIX_HOME") or (Path.home() / ".matrix")).expanduser()
STATE_DIR = HOME / "state"
LOGS_DIR = HOME / "logs"

for d in (STATE_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)


@dataclass
class LockInfo:
    alias: str
    pid: int
    port: int
    started_at: int
    target: str
    runner_path: str


def _state_for(alias: str) -> Path:
    d = STATE_DIR / alias
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path(alias: str) -> Path:
    return _state_for(alias) / "runner.lock.json"


def log_path(alias: str) -> str:
    return str(LOGS_DIR / f"{alias}.log")


def _load_runner(target: Path) -> Dict:
    p = target / "runner.json"
    if not p.exists():
        raise FileNotFoundError(f"runner.json not found in {target}")
    return json.loads(p.read_text(encoding="utf-8"))


def start(
    target: str, *, alias: Optional[str] = None, port: Optional[int] = None
) -> LockInfo:
    t = Path(target).expanduser().resolve()
    runner = _load_runner(t)

    typ = (runner.get("type") or "").lower()
    entry = runner.get("entry") or ("server.py" if typ == "python" else "server.js")
    env = os.environ.copy()
    env.update(runner.get("env") or {})
    port = port or default_port()
    env["PORT"] = str(port)

    if typ == "python":
        # Prefer venv python if present
        vcfg = runner.get("python") or {}
        venv_dir = vcfg.get("venv") or ".venv"
        py = t / venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pycmd = str(py if py.exists() else (Path(os.sys.executable)))
        cmd = [pycmd, str(t / entry)]
    elif typ == "node":
        node = os.environ.get("NODE", "node")
        cmd = [node, str(t / entry)]
    else:
        raise RuntimeError(f"unsupported runner type: {typ}")

    # spawn
    lp = Path(log_path(alias or "default"))
    lp.parent.mkdir(parents=True, exist_ok=True)
    logf = lp.open("a", buffering=1, encoding="utf-8")
    child = subprocess.Popen(cmd, cwd=str(t), env=env, stdout=logf, stderr=logf)

    lock = {
        "pid": child.pid,
        "port": port,
        "started_at": int(time.time() * 1000),
        "target": str(t),
        "runner_path": str(t / "runner.json"),
    }
    ap = alias or t.name
    _lock_path(ap).write_text(
        json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return LockInfo(
        alias=ap,
        pid=child.pid,
        port=port,
        started_at=lock["started_at"],
        target=str(t),
        runner_path=lock["runner_path"],
    )


def stop(alias: str) -> bool:
    p = _lock_path(alias)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False
    finally:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def status() -> List[LockInfo]:
    out: List[LockInfo] = []
    for lp in STATE_DIR.glob("*/runner.lock.json"):
        alias = lp.parent.name
        try:
            d = json.loads(lp.read_text(encoding="utf-8"))
            out.append(
                LockInfo(
                    alias=alias,
                    pid=int(d.get("pid", 0)),
                    port=int(d.get("port", 0)),
                    started_at=int(d.get("started_at", 0)),
                    target=str(d.get("target", "")),
                    runner_path=str(d.get("runner_path", "")),
                )
            )
        except Exception:
            continue
    return out


def tail_logs(alias: str, *, follow: bool = False) -> Iterator[str]:
    p = Path(log_path(alias))
    if not p.exists():
        return iter(())
    with p.open("r", encoding="utf-8") as f:
        if follow:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                yield line
        else:
            yield from f.readlines()


def doctor(alias: str, path: Optional[str] = None) -> Dict[str, str]:
    """
    Basic health probe. If transport is SSE/HTTP, try GET /health on the recorded port.
    """
    # load lock
    lp = _lock_path(alias)
    if not lp.exists():
        return {"status": "not_running"}
    d = json.loads(lp.read_text(encoding="utf-8"))
    port = int(d.get("port", 0)) or 0
    if not port:
        return {"status": "unknown"}

    # Best-effort probe
    import http.client

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        conn.request("GET", "/health")
        r = conn.getresponse()
        ok = 200 <= r.status < 400
        return {"status": "ok" if ok else f"fail:{r.status}"}
    except Exception as e:
        return {"status": f"fail:{e}"}
