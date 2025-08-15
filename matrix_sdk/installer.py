# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .hub import MatrixClient
from .manifest import ManifestResolutionError
from .policy import default_install_target


@dataclass(frozen=True)
class BuildReport:
    files_written: int = 0
    artifacts_fetched: int = 0
    runner_path: Optional[str] = None


@dataclass(frozen=True)
class EnvReport:
    python_prepared: bool = False
    node_prepared: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class BuildResult:
    id: str
    target: str
    plan: Dict[str, Any]
    build: BuildReport
    env: EnvReport
    runner: Dict[str, Any]


class LocalInstaller:
    """
    Orchestrates a local install:
      plan()        → call Hub '/catalog/install' (no side-effects)
      materialize() → write files/artifacts, ensure runner.json
      prepare_env() → python venv / node install per runner.json
      build()       → plan + materialize + prepare_env
    """

    def __init__(
        self, client: MatrixClient, *, fs_root: Optional[str | Path] = None
    ) -> None:
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None

    # ---- Public API ----------------------------------------------------------

    def plan(self, id: str, target: str) -> Dict[str, Any]:
        target = self._abs(target)
        return self.client.install(id, target=target)

    def _materialize_files(self, plan: Dict[str, Any], target_path: Path) -> int:
        """Finds file descriptions in the plan and writes them to the target directory."""
        files_written = 0
        candidates = []
        # Look for file lists in various plausible keys within the plan
        for key in ("files", "plan", "results"):
            node = plan.get(key)
            if isinstance(node, dict) and "files" in node:
                candidates = node["files"]
                break
            if isinstance(node, list):
                for step in node:
                    if isinstance(step, dict) and "files" in step:
                        candidates.extend(step["files"])

        for f in candidates or []:
            path = f.get("path") or f.get("rel") or f.get("dest")
            if not path:
                continue

            p = (target_path / path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)

            if (content_b64 := f.get("content_b64")) is not None:
                p.write_bytes(base64.b64decode(content_b64))
            elif (content := f.get("content")) is not None:
                p.write_text(content, encoding="utf-8")
            else:
                p.touch()  # Create empty file
            files_written += 1
        return files_written

    def _materialize_artifacts(self, plan: Dict[str, Any], target_path: Path) -> int:
        """Downloads and verifies artifacts defined in the plan."""
        artifacts_fetched = 0
        artifacts = plan.get("artifacts") or plan.get("plan", {}).get("artifacts") or []
        with httpx.Client(timeout=60.0, follow_redirects=True) as http:
            for a in artifacts or []:
                url, dest, sha256 = (
                    a.get("url"),
                    a.get("path") or a.get("dest"),
                    a.get("sha256"),
                )
                if not url or not dest:
                    continue

                p = (target_path / dest).resolve()
                p.parent.mkdir(parents=True, exist_ok=True)

                resp = http.get(url)
                resp.raise_for_status()
                data = resp.content

                if (
                    sha256
                    and hashlib.sha256(data).hexdigest().lower() != sha256.lower()
                ):
                    raise ManifestResolutionError(
                        f"Checksum mismatch for artifact: {url}"
                    )

                p.write_bytes(data)
                artifacts_fetched += 1
        return artifacts_fetched

    def _materialize_runner(
        self, plan: Dict[str, Any], target_path: Path
    ) -> Optional[str]:
        """Ensures a runner.json file exists, creating one if necessary."""
        runner_path = target_path / "runner.json"

        # 1. Use runner from plan if provided
        if runner := plan.get("runner"):
            runner_path.write_text(json.dumps(runner, indent=2), encoding="utf-8")
            return str(runner_path)

        # 2. If a runner.json was written in the files step, we're done
        if runner_path.exists():
            return str(runner_path)

        # 3. Otherwise, infer a runner from the project structure
        if inferred_runner := self._infer_runner(target_path):
            runner_path.write_text(
                json.dumps(inferred_runner, indent=2), encoding="utf-8"
            )
            return str(runner_path)

        return None

    def materialize(self, plan: Dict[str, Any], target: str) -> BuildReport:
        """Write files, fetch artifacts, and produce runner.json if needed."""
        target_path = self._abs(target)
        target_path.mkdir(parents=True, exist_ok=True)

        files_written = self._materialize_files(plan, target_path)
        artifacts_fetched = self._materialize_artifacts(plan, target_path)
        runner_path = self._materialize_runner(plan, target_path)

        return BuildReport(
            files_written=files_written,
            artifacts_fetched=artifacts_fetched,
            runner_path=runner_path,
        )

    def prepare_env(
        self, target: str, runner: Dict[str, Any], *, timeout: int = 900
    ) -> EnvReport:
        """Create a python venv and/or run node install as requested by runner."""
        t = self._abs(target)
        python_prepared = False
        node_prepared = False
        notes: list[str] = []

        typ = (runner.get("type") or "").lower()
        if typ == "python":
            rp = runner.get("python") or {}
            venv_dir = rp.get("venv") or ".venv"
            venv_path = t / venv_dir
            if not venv_path.exists():
                venv.create(
                    venv_path,
                    with_pip=True,
                    clear=False,
                    symlinks=True,
                    upgrade_deps=False,
                )
            pybin = _python_bin(venv_path)
            reqs = rp.get("requirements")

            if isinstance(reqs, list) and reqs:
                cmd = [pybin, "-m", "pip", "install"] + list(reqs)
            elif (t / "requirements.txt").exists():
                cmd = [pybin, "-m", "pip", "install", "-r", "requirements.txt"]
            else:
                # nothing to install; still ensure pip is OK
                cmd = [pybin, "-m", "pip", "--version"]
            _run(cmd, cwd=t, timeout=timeout)
            python_prepared = True

        if typ == "node" or (typ == "python" and runner.get("node")):
            np = runner.get("node") or {}
            pm = np.get("package_manager") or _detect_package_manager(t)
            install_args = np.get("install_args") or []
            if pm:
                cmd = [pm, "install"] + list(install_args)
                _run(cmd, cwd=t, timeout=timeout)
                node_prepared = True
            else:
                notes.append("node requested but no package manager detected")

        return EnvReport(
            python_prepared=python_prepared,
            node_prepared=node_prepared,
            notes="; ".join(notes) or None,
        )

    def build(
        self,
        id: str,
        *,
        target: Optional[str] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        tgt = self._abs(target or default_install_target(id, alias=alias))
        tgt.mkdir(parents=True, exist_ok=True)
        plan = self.plan(id, str(tgt))
        br = self.materialize(plan, str(tgt))

        # Load runner for prepare step
        runner = {}
        if br.runner_path and Path(br.runner_path).exists():
            runner = json.loads(Path(br.runner_path).read_text(encoding="utf-8"))
        else:
            # try fallback
            r = tgt / "runner.json"
            if r.exists():
                runner = json.loads(r.read_text(encoding="utf-8"))
            else:
                # last resort → empty runner (no env prep)
                runner = {}

        er = self.prepare_env(str(tgt), runner, timeout=timeout)
        return BuildResult(
            id=id, target=str(tgt), plan=plan, build=br, env=er, runner=runner
        )

    # ---- Internals -----------------------------------------------------------

    def _abs(self, path: str | Path) -> Path:
        p = Path(path)
        if not p.is_absolute() and self.fs_root:
            p = self.fs_root / p
        return p.expanduser().resolve()

    def _infer_runner(self, target: Path) -> Dict[str, Any] | None:
        # simple heuristics
        if (target / "server.py").exists():
            # python default
            runner = {
                "type": "python",
                "entry": "server.py",
                "transport": "sse",
                "python": {
                    "venv": ".venv",
                    # if requirements.txt exists, prepare_env will use it automatically
                },
                "env": {},
            }
            return runner
        if (target / "server.js").exists() or (target / "package.json").exists():
            runner = {
                "type": "node",
                "entry": "server.js" if (target / "server.js").exists() else "index.js",
                "transport": "sse",
                "node": {
                    # package manager auto-detected if not supplied
                },
                "env": {},
            }
            return runner
        return None


def _python_bin(venv_path: Path) -> str:
    if os.name == "nt":
        return str(venv_path / "Scripts" / "python.exe")
    return str(venv_path / "bin" / "python")


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, timeout=timeout)


def _detect_package_manager(path: Path) -> Optional[str]:
    # prefer pnpm > yarn > npm if lockfiles present
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return "npm"
    # else None
    return None
