# SPDX-License-Identifier: MIT
"""Installer orchestration (public API).

This module exposes the public orchestration surface for local installs:

- ``LocalInstaller``: the entrypoint used by callers/CLI
- ``BuildReport``, ``EnvReport``, ``BuildResult``: dataclasses for structured results

It delegates all heavy lifting to small, testable submodules:
- ``installer.files``               → file writes & artifact fetching
- ``installer.runner_discovery``    → strategy pipeline to produce ``runner.json``
- ``installer.envs``                → Python/Node environment preparation
- ``installer.util``                → logging & small helpers

Design goals
------------
- Keep this file small and readable (orchestration only).
- Lazy-import submodules inside methods to avoid import-time overhead
  and to ease incremental refactors.
- Preserve backwards compatibility and current behavior.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..client import MatrixClient
from ..manifest import ManifestResolutionError
from ..policy import default_install_target

# ---------------------------------------------------------------------------
# Logging & helper shims
# Prefer util.* if available; fall back to local shims during migration.
# ---------------------------------------------------------------------------
try:  # Prefer centralized logging and helpers
    from .util import (
        _as_dict,
        _ensure_local_writable,
        _plan_target_for_server,
        _short,
    )
    from .util import (
        logger as _LOGGER,
    )
except Exception:  # pragma: no cover - transitional fallback
    _LOGGER = logging.getLogger("matrix_sdk.installer")
    if not _LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
        )
        _LOGGER.addHandler(handler)
    # Honor MATRIX_SDK_DEBUG for fallback logger as well
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    _LOGGER.setLevel(
        logging.DEBUG if dbg in {"1", "true", "yes", "on"} else logging.INFO
    )

    def _short(path: Path | str, maxlen: int = 120) -> str:
        s = str(path)
        return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])

    def _as_dict(obj: Any) -> Dict[str, Any]:
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()  # type: ignore[attr-defined]
            except Exception:
                pass
        if hasattr(obj, "dict"):
            try:
                return obj.dict()  # type: ignore[attr-defined]
            except Exception:
                pass
        return dict(obj) if isinstance(obj, dict) else {}

    def _ensure_local_writable(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".matrix_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            try:
                probe.unlink()
            except Exception:
                pass

    def _plan_target_for_server(id_str: str, target: str | os.PathLike[str]) -> str:
        p = Path(str(target))
        alias = (p.parent.name or "runner").strip()
        version = (p.name or "0").strip()
        return f"{alias}/{version}".replace("\\", "/").lstrip("/") or "runner/0"


logger = _LOGGER


# ---------------------------------------------------------------------------
# Public dataclasses  (must match legacy API exactly)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BuildReport:
    """Summary of materialization results."""

    files_written: int = 0
    artifacts_fetched: int = 0
    runner_path: Optional[str] = None


@dataclass(frozen=True)
class EnvReport:
    """Summary of environment preparation."""

    python_prepared: bool = False
    node_prepared: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class BuildResult:
    """Final result of a successful build."""

    id: str
    target: str
    plan: Dict[str, Any]
    build: BuildReport
    env: EnvReport
    runner: Dict[str, Any]


# ---------------------------------------------------------------------------
# Orchestration class (public entry point)
# ---------------------------------------------------------------------------
class LocalInstaller:
    """Orchestrates a local project installation from a Hub plan.

    This class intentionally contains only orchestration logic; all heavy
    work is delegated to small submodules that can evolve independently.
    """

    def __init__(
        self, client: MatrixClient, *, fs_root: Optional[str | Path] = None
    ) -> None:
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None
        logger.debug("LocalInstaller created (fs_root=%s)", self.fs_root)

    # --------------------------- Public workflow steps -------------------- #
    def plan(self, id: str, target: str | os.PathLike[str]) -> Dict[str, Any]:
        """Request an installation plan from the Hub.

        SECURITY: Converts local absolute paths to server-safe labels unless
        explicitly overridden via ``MATRIX_INSTALL_SEND_ABS_TARGET``.
        """
        logger.info("plan: requesting Hub plan for id=%s target=%s", id, target)

        send_abs = (os.getenv("MATRIX_INSTALL_SEND_ABS_TARGET") or "").strip().lower()
        if send_abs in {"1", "true", "yes", "on"}:
            to_send = str(target)
            logger.debug("plan: sending absolute target path to server: %s", to_send)
        else:
            to_send = _plan_target_for_server(id, target)
            logger.debug(
                "plan: sending server-safe target label to server: %s", to_send
            )

        outcome = self.client.install(id, target=to_send)
        logger.debug("plan: received outcome from Hub: %s", outcome)
        return _as_dict(outcome)

    def materialize(
        self, outcome: Dict[str, Any], target: str | os.PathLike[str]
    ) -> BuildReport:
        """Write files, fetch artifacts, and attempt to produce a runner.json."""
        logger.debug("materialize: starting materialization process.")
        target_path = self._abs(target)
        target_path.mkdir(parents=True, exist_ok=True)
        logger.info("materialize: target directory ready → %s", _short(target_path))

        # Lazy imports keep import time low and let the refactor land gradually.
        from .files import materialize_artifacts as _materialize_artifacts
        from .files import materialize_files as _materialize_files
        from .runner_discovery import materialize_runner as _materialize_runner

        files_written = _materialize_files(outcome, target_path)
        plan_node = outcome.get("plan") or outcome
        artifacts_fetched = _materialize_artifacts(plan_node, target_path)

        # Orchestration → discovery; support both new/old signatures for safety.
        try:
            runner_path = _materialize_runner(outcome, target_path)
        except TypeError:
            try:
                runner_path = _materialize_runner(self, outcome, target_path)
            except TypeError:
                try:
                    runner_path = _materialize_runner(
                        self, plan_node, target_path, outcome
                    )
                except Exception:
                    runner_path = None

        report = BuildReport(
            files_written=files_written,
            artifacts_fetched=artifacts_fetched,
            runner_path=runner_path,
        )
        logger.info(
            "materialize: summary files=%d artifacts=%d runner=%s",
            report.files_written,
            report.artifacts_fetched,
            report.runner_path or "-",
        )
        logger.debug("materialize: finished. BuildReport: %s", report)
        return report

    def prepare_env(
        self,
        target: str | os.PathLike[str],
        runner: Dict[str, Any],
        *,
        timeout: int = 900,
    ) -> EnvReport:
        """Create Python venv + pip install OR run Node package-manager install."""
        target_path = self._abs(target)
        runner_type = (runner.get("type") or "").lower()
        logger.info(
            "env: preparing environment (type=%s) in %s",
            runner_type or "-",
            _short(target_path),
        )
        logger.debug("env: using runner config: %s", runner)

        # Lazy import to avoid heavy dependencies until needed.
        from .envs import prepare_node_env as _prepare_node_env
        from .envs import prepare_python_env as _prepare_python_env

        py_ok: bool = False
        node_ok: bool = False
        notes_list: List[str] = []

        if runner_type == "python":
            logger.debug("env: python runner detected, preparing python environment.")
            py_ok = _prepare_python_env(target_path, runner, timeout)

        # Also check for 'node' key for mixed-language projects
        if runner_type == "node" or runner.get("node"):
            logger.debug(
                "env: node runner or config detected, preparing node environment."
            )
            node_ok, node_notes = _prepare_node_env(target_path, runner, timeout)
            if node_notes:
                notes_list.append(node_notes)

        report = EnvReport(
            python_prepared=bool(py_ok),
            node_prepared=bool(node_ok),
            notes="; ".join([n for n in notes_list if n]) or None,
        )
        logger.info(
            "env: summary python=%s node=%s notes=%s",
            report.python_prepared,
            report.node_prepared,
            report.notes or "-",
        )
        logger.debug("env: finished. EnvReport: %s", report)
        return report

    def build(
        self,
        id: str,
        *,
        target: Optional[str | os.PathLike[str]] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        """Full orchestration: plan → materialize → load runner → prepare_env."""
        logger.info("build: starting full build for id='%s', alias='%s'", id, alias)
        tgt = self._abs(target or default_install_target(id, alias=alias))
        logger.info("build: target resolved → %s", _short(tgt))

        # Fail fast if the local install location isn't writable.
        logger.debug("build: ensuring target is writable.")
        _ensure_local_writable(tgt)
        logger.debug("build: target is writable.")

        logger.info("build: STEP 1: Planning...")
        outcome = self.plan(id, tgt)
        logger.info("build: STEP 2: Materializing...")
        build_report = self.materialize(outcome, tgt)

        logger.info("build: STEP 3: Loading runner config...")
        runner = self._load_runner_from_report(build_report, tgt)
        logger.info("build: STEP 4: Preparing environment...")
        env_report = self.prepare_env(tgt, runner, timeout=timeout)

        result = BuildResult(
            id=id,
            target=str(tgt),
            plan=outcome,
            build=build_report,
            env=env_report,
            runner=runner,
        )
        logger.info(
            "build: complete id=%s target=%s files=%d artifacts=%d python=%s node=%s",
            id,
            _short(tgt),
            build_report.files_written,
            build_report.artifacts_fetched,
            env_report.python_prepared,
            env_report.node_prepared,
        )
        logger.debug("build: finished. Final BuildResult: %s", result)
        return result

    # --------------------------- Private helpers -------------------------- #
    def _abs(self, path: str | os.PathLike[str]) -> Path:
        """Resolve a path, prepending the fs_root if provided."""
        p = Path(path)
        if self.fs_root and not p.is_absolute():
            abs_path = self.fs_root / p
            logger.debug("_abs: prepended fs_root. %s -> %s", path, abs_path)
            return abs_path
        abs_path = p.expanduser().resolve()
        logger.debug("_abs: resolved path. %s -> %s", path, abs_path)
        return abs_path

    def _load_runner_from_report(
        self, report: BuildReport, target_path: Path
    ) -> Dict[str, Any]:
        """Prefer report.runner_path if set; fallback to target/runner.json."""
        logger.debug("build: loading runner.json from build report.")
        runner_path = (
            Path(report.runner_path)
            if report.runner_path
            else Path(target_path) / "runner.json"
        )
        logger.debug("build: effective runner path is '%s'", _short(runner_path))
        if runner_path.is_file():
            try:
                runner_data = json.loads(runner_path.read_text("utf-8"))
                logger.info(
                    "build: successfully loaded runner config from %s",
                    _short(runner_path),
                )
                logger.debug("build: loaded runner data: %s", runner_data)
                return runner_data
            except json.JSONDecodeError as e:  # pragma: no cover - defensive
                logger.error(
                    "build: failed to decode runner JSON from %s: %s",
                    _short(runner_path),
                    e,
                )
                raise ManifestResolutionError(
                    f"Invalid runner.json at {runner_path}"
                ) from e

        logger.warning(
            "build: runner.json not found in %s; env prepare may be skipped.",
            _short(runner_path.parent),
        )
        return {}

    # NOTE: Keep this inference helper for compatibility; the discovery pipeline
    # may call back into it. In future, inference can live fully in runner_discovery.
    def _infer_runner(self, target: Path) -> Optional[Dict[str, Any]]:
        """Infer a default runner config from common file names (best-effort)."""
        logger.debug("runner(infer): checking for common files in %s", _short(target))

        # Priority 1: Specific entry points
        if (target / "server.py").exists():
            logger.info("runner(infer): found 'server.py', inferring python runner.")
            return {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}

        if (target / "server.js").exists() or (target / "package.json").exists():
            entry = "server.js" if (target / "server.js").exists() else "index.js"
            logger.info(
                "runner(infer): found node files, inferring node runner with entry '%s'.",
                entry,
            )
            return {"type": "node", "entry": entry}

        # Priority 2: Generic Python project files
        if (
            (target / "pyproject.toml").is_file()
            or (target / "requirements.txt").is_file()
            or (target / "setup.py").is_file()
        ):
            logger.info(
                "runner(infer): found python project file. "
                "Will synthesize a runner and search for entry points."
            )

            potential_servers: List[str] = []
            notes_lines = [
                "This runner was synthesized because no explicit 'runner.json' was found.",
                "An entry point could not be automatically determined.",
                "ACTION REQUIRED: Please edit the 'entry' field below "
                "with the correct server file.",
            ]

            # Try local script path first; if missing (e.g., zip/egg), fall back to -m invocation.
            helper_script_path = Path(__file__).with_name("find_potential_servers.py")

            def _run_finder(cmd: List[str]) -> List[str]:
                import subprocess

                try:
                    result = subprocess.run(
                        cmd,
                        cwd=str(target),  # ensure relative output is relative to target
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=30,
                    )
                    hits: List[str] = []
                    for line in result.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("- "):
                            hits.append(line[2:].strip())
                    return hits
                except (
                    subprocess.CalledProcessError,
                    FileNotFoundError,
                    subprocess.TimeoutExpired,
                ) as e:
                    logger.debug(
                        "runner(infer): server finder invocation failed: %s", e
                    )
                    return []

            try:
                found_any = False
                # Strategy A: run helper via file path if present
                if helper_script_path.is_file():
                    logger.debug(
                        "runner(infer): using helper script at %s",
                        _short(helper_script_path),
                    )
                    potential_servers = _run_finder(
                        [
                            sys.executable,
                            str(helper_script_path),
                            str(target),
                            "--top",
                            "10",
                        ]
                    )
                    found_any = bool(potential_servers)
                # Strategy B: run helper as a module (works when packaged)
                if not found_any:
                    logger.debug("runner(infer): trying module invocation for helper")
                    potential_servers = _run_finder(
                        [
                            sys.executable,
                            "-m",
                            "matrix_sdk.installer.find_potential_servers",
                            str(target),
                            "--top",
                            "10",
                        ]
                    )
                    found_any = bool(potential_servers)

                if found_any:
                    notes_lines.append("Potential entry points found in the project:")
                    notes_lines.extend([f"  - {s}" for s in potential_servers])
                else:
                    notes_lines.append(
                        "No likely server entry points were found during analysis."
                    )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "runner(infer): server finder encountered an error: %s", e
                )
                notes_lines.append("Automated server discovery failed.")

            # If we found potential servers, use the first one as a default guess.
            entry_point = potential_servers[0] if potential_servers else "EDIT_ME.py"
            return {
                "type": "python",
                "entry": entry_point,
                "python": {"venv": ".venv"},
                "notes": "\n".join(notes_lines),
            }

        logger.debug("runner(infer): no common files found for inference.")
        return None
