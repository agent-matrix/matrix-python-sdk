# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .client import MatrixClient
from .manifest import ManifestResolutionError
from .policy import default_install_target

# Modular fetchers
try:
    from .gitfetch import GitFetchError, fetch_git_artifact
except ImportError:  # pragma: no cover
    fetch_git_artifact = None  # type: ignore

    class GitFetchError(RuntimeError):  # type: ignore
        pass


try:
    from .archivefetch import ArchiveFetchError, fetch_http_artifact
except ImportError:  # pragma: no cover
    fetch_http_artifact = None  # type: ignore

    class ArchiveFetchError(RuntimeError):  # type: ignore
        pass


try:
    from . import python_builder
except ImportError:
    python_builder = None  # type: ignore

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
logger = logging.getLogger("matrix_sdk.installer")


def _maybe_configure_logging() -> None:
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on"):
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


_maybe_configure_logging()

# --------------------------------------------------------------------------------------
# Helper Functions & Dataclasses
# --------------------------------------------------------------------------------------


def _short(path: Path | str, maxlen: int = 120) -> str:
    s = str(path)
    return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


def _is_valid_runner_schema(runner: Dict[str, Any], logger: logging.Logger) -> bool:
    """Basic schema validation for a runner.json-like object."""
    if not isinstance(runner, dict):
        logger.debug("runner validation: failed (not a dict)")
        return False
    if not runner.get("type") or not runner.get("entry"):
        logger.warning(
            "runner validation: failed (missing required 'type' or 'entry' keys in %r)",
            list(runner.keys()),
        )
        return False
    logger.debug("runner validation: schema appears valid")
    return True


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


# --------------------------------------------------------------------------------------
# Main Installer Class
# --------------------------------------------------------------------------------------
class LocalInstaller:
    """Orchestrates a local project installation."""

    def __init__(
        self, client: MatrixClient, *, fs_root: Optional[str | Path] = None
    ) -> None:
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None
        logger.debug("LocalInstaller created (fs_root=%s)", self.fs_root)

    def plan(self, id: str, target: str | os.PathLike[str]) -> Dict[str, Any]:
        """Requests an installation plan from the Hub."""
        logger.info("plan: requesting Hub plan for id=%s target=%s", id, target)
        outcome = self.client.install(id, target=target)
        return _as_dict(outcome)

    def materialize(
        self, outcome: Dict[str, Any], target: str | os.PathLike[str]
    ) -> BuildReport:
        """Writes files and fetches artifacts based on the plan."""
        target_path = self._abs(target)
        target_path.mkdir(parents=True, exist_ok=True)
        logger.info("materialize: target directory ready → %s", _short(target_path))

        files_written = self._materialize_files(outcome, target_path)
        plan_node = outcome.get("plan", outcome)
        artifacts_fetched = self._materialize_artifacts(plan_node, target_path)
        runner_path = self._materialize_runner(outcome, target_path)

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
        return report

    def prepare_env(
        self,
        target: str | os.PathLike[str],
        runner: Dict[str, Any],
        *,
        timeout: int = 900,
    ) -> EnvReport:
        """Prepares the runtime environment (e.g., venv, npm install)."""
        target_path = self._abs(target)
        runner_type = (runner.get("type") or "").lower()
        logger.info(
            "env: preparing environment (type=%s) in %s",
            runner_type or "-",
            _short(target_path),
        )

        py_ok, node_ok, notes = False, False, []
        if runner_type == "python":
            py_ok = self._prepare_python_env(target_path, runner, timeout)

        if runner_type == "node" or runner.get("node"):
            node_ok, node_notes = self._prepare_node_env(target_path, runner, timeout)
            if node_notes:
                notes.append(node_notes)

        report = EnvReport(
            python_prepared=py_ok,
            node_prepared=node_ok,
            notes="; ".join(notes) or None,
        )
        logger.info(
            "env: summary python=%s node=%s notes=%s",
            report.python_prepared,
            report.node_prepared,
            report.notes or "-",
        )
        return report

    def build(
        self,
        id: str,
        *,
        target: Optional[str | os.PathLike[str]] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        """Performs the full plan, materialize, and prepare_env workflow."""
        tgt = self._abs(target or default_install_target(id, alias=alias))
        logger.info("build: target resolved → %s", _short(tgt))

        outcome = self.plan(id, tgt)
        build_report = self.materialize(outcome, tgt)

        runner = self._load_runner_from_report(build_report, tgt)
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
        return result

    # --- Private Materialization Helpers ---

    def _find_file_candidates(self, outcome: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extracts all file descriptions from the various parts of the outcome."""
        candidates: List[Dict[str, Any]] = []
        for source in (
            (outcome.get("plan") or {}).get("files", []),
            *(
                step.get("files", [])
                for step in outcome.get("results", [])
                if isinstance(step, dict)
            ),
            outcome.get("files", []),
        ):
            if isinstance(source, list):
                candidates.extend(item for item in source if isinstance(item, dict))
        return candidates

    def _materialize_files(self, outcome: Dict[str, Any], target_path: Path) -> int:
        """Finds and writes all declared files from the installation plan."""
        logger.info("materialize: writing declared files → %s", _short(target_path))
        candidates = self._find_file_candidates(outcome)
        logger.debug("materialize: %d file candidate(s) found", len(candidates))
        files_written = 0
        for f in candidates:
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
                p.touch()
            files_written += 1
        return files_written

    def _handle_git_artifact(self, artifact: Dict[str, Any], target_path: Path) -> None:
        """Handles fetching a git-based artifact, including the legacy shim."""
        spec = artifact.get("spec") or {}
        # WORKAROUND SHIM for legacy 'command' field
        if not spec.get("repo") and (
            cmd := (artifact.get("command") or "").strip()
        ).startswith("git clone"):
            try:
                parts = cmd.split()
                repo_idx = parts.index("clone") + 1
                spec["repo"] = parts[repo_idx]
                if "--branch" in parts:
                    ref_idx = parts.index("--branch") + 1
                    spec["ref"] = parts[ref_idx]
                logger.warning(
                    "artifact(git): SHIM: Derived spec from legacy 'command' field"
                )
            except (ValueError, IndexError) as e:
                logger.error(
                    "artifact(git): SHIM: Could not parse legacy 'command' (%s)", e
                )

        if fetch_git_artifact:
            fetch_git_artifact(spec=spec, target=target_path)

    def _handle_http_artifact(
        self, artifact: Dict[str, Any], target_path: Path
    ) -> None:
        """Handles fetching a URL-based artifact."""
        if fetch_http_artifact:
            fetch_http_artifact(
                url=artifact["url"],
                target=target_path,
                dest=artifact.get("path") or artifact.get("dest"),
                sha256=str(s) if (s := artifact.get("sha256")) else None,
                unpack=bool(artifact.get("unpack", False)),
                logger=logger,
            )

    def _materialize_artifacts(self, plan: Dict[str, Any], target_path: Path) -> int:
        """Dispatches artifact fetching to specialized handlers."""
        artifacts = plan.get("artifacts", [])
        if not artifacts:
            return 0
        logger.info("materialize: fetching %d artifact(s)", len(artifacts))
        fetched_count = 0
        for a in artifacts:
            try:
                if isinstance(a, dict):
                    if a.get("kind") == "git":
                        self._handle_git_artifact(a, target_path)
                        fetched_count += 1
                    elif a.get("url"):
                        self._handle_http_artifact(a, target_path)
                        fetched_count += 1
            except (GitFetchError, ArchiveFetchError) as e:
                logger.error("artifact: failed to fetch: %s", e)
                raise ManifestResolutionError(str(e)) from e
        return fetched_count

    def _materialize_runner(
        self, outcome: Dict[str, Any], target_path: Path
    ) -> Optional[str]:
        """Finds or infers a runner.json file for the project."""
        plan_node = outcome.get("plan") or {}
        # Attempt 1: From a direct object in the plan
        if (runner_obj := plan_node.get("runner")) and isinstance(runner_obj, dict):
            if _is_valid_runner_schema(runner_obj, logger):
                runner_path = target_path / "runner.json"
                runner_path.write_text(
                    json.dumps(runner_obj, indent=2), encoding="utf-8"
                )
                return str(runner_path)
        # Attempt 2: From a file on disk (could have been written in _materialize_files)
        runner_file_name = plan_node.get("runner_file", "runner.json")
        runner_path = target_path / runner_file_name
        if runner_path.is_file():
            try:
                data = json.loads(runner_path.read_text("utf-8"))
                if _is_valid_runner_schema(data, logger):
                    return str(runner_path)
            except json.JSONDecodeError:
                logger.warning(
                    "runner: file exists but is not valid JSON: %s", runner_path
                )
        # Attempt 3: Infer from project structure
        if inferred := self._infer_runner(target_path):
            if _is_valid_runner_schema(inferred, logger):
                inferred_path = target_path / "runner.json"
                inferred_path.write_text(json.dumps(inferred, indent=2), "utf-8")
                return str(inferred_path)
        logger.warning("runner: a valid runner config was not found or inferred")
        return None

    # --- Private Environment Helpers ---
    # In class LocalInstaller, replace this entire method:
    def _prepare_python_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> bool:
        """Creates a venv and installs Python dependencies."""
        rp = runner.get("python") or {}
        venv_dir = rp.get("venv") or ".venv"
        venv_path = target_path / venv_dir
        if not venv_path.exists():
            logger.info("env: creating venv → %s", _short(venv_path))
            venv.create(venv_path, with_pip=True, clear=False, symlinks=True)

        if python_builder:
            logger.info("env: using modern python_builder to install dependencies...")
            if not python_builder.run_python_build(
                target_path=target_path,
                runner_data=runner,
                logger=logger,
                timeout=timeout,
            ):
                logger.warning(
                    "env: python_builder did not find a known dependency file to install."
                )
        else:  # Fallback for legacy installs
            logger.warning(
                "env: python_builder not found, falling back to requirements.txt."
            )
            pybin = _python_bin(venv_path)
            req_path = target_path / "requirements.txt"
            if req_path.exists():
                cmd = [pybin, "-m", "pip", "install", "-r", str(req_path)]
                _run(cmd, cwd=target_path, timeout=timeout)

        return True

    def _prepare_node_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> tuple[bool, Optional[str]]:
        """Installs Node.js dependencies."""
        np = runner.get("node") or {}
        pm = np.get("package_manager") or _detect_package_manager(target_path)
        if not pm:
            return False, "node requested but no package manager detected"
        cmd = [pm, "install"] + list(np.get("install_args", []))
        _run(cmd, cwd=target_path, timeout=timeout)
        return True, None

    # --- Private Utility Helpers ---

    def _abs(self, path: str | os.PathLike[str]) -> Path:
        p = Path(path)
        return (
            self.fs_root / p
            if self.fs_root and not p.is_absolute()
            else p.expanduser().resolve()
        )

    def _infer_runner(self, target: Path) -> Optional[Dict[str, Any]]:
        """Infers a default runner config from file structure."""
        if (target / "server.py").exists():
            return {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}
        if (target / "server.js").exists() or (target / "package.json").exists():
            entry = "server.js" if (target / "server.js").exists() else "index.js"
            return {"type": "node", "entry": entry}
        return None

    def _load_runner_from_report(
        self, report: BuildReport, target_path: Path
    ) -> Dict[str, Any]:
        """Loads the runner.json file after materialization."""
        runner_path = (
            Path(report.runner_path)
            if report.runner_path
            else target_path / "runner.json"
        )
        if runner_path.is_file():
            try:
                return json.loads(runner_path.read_text("utf-8"))
            except json.JSONDecodeError:
                logger.error("build: failed to decode runner JSON from %s", runner_path)
        logger.warning("build: runner.json not found; env prepare may be skipped.")
        return {}


# --------------------------------------------------------------------------------------
# Standalone Helper Functions
# --------------------------------------------------------------------------------------


def _python_bin(venv_path: Path) -> str:
    return str(venv_path / "Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    logger.debug(
        "exec: %s (cwd=%s, timeout=%ss)", " ".join(map(str, cmd)), _short(cwd), timeout
    )
    subprocess.run(cmd, cwd=str(cwd), check=True, timeout=timeout)


def _detect_package_manager(path: Path) -> Optional[str]:
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return "npm"
    return None


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Normalizes possible Pydantic models or dataclasses to a plain dict."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return {}
