# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import subprocess
import sys
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from .client import MatrixClient
from .manifest import ManifestResolutionError
from .policy import default_install_target

try:
    import tomllib as _tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    try:
        import tomli as _tomllib  # Optional fallback for Python < 3.11
    except ImportError:
        _tomllib = None  # type: ignore

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
# Logging & env helpers
# --------------------------------------------------------------------------------------
logger = logging.getLogger("matrix_sdk.installer")


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.getenv(name) or "").strip()
        return int(raw) if raw else default
    except Exception:
        return default


HTTP_TIMEOUT = max(3, _env_int("MATRIX_SDK_HTTP_TIMEOUT", 15))
RUNNER_SEARCH_DEPTH_DEFAULT = _env_int("MATRIX_SDK_RUNNER_SEARCH_DEPTH", 2)


def _maybe_configure_logging() -> None:
    """Configure logging if the MATRIX_SDK_DEBUG environment variable is set."""
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
    """Truncate a path string for cleaner logging."""
    s = str(path)
    return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


def _connector_enabled() -> bool:
    """Feature flag: allow synthesizing connector runners by default."""
    val = (os.getenv("MATRIX_SDK_ENABLE_CONNECTOR") or "1").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _is_valid_runner_schema(runner: Dict[str, Any], logger: logging.Logger) -> bool:
    """
    Basic schema validation for a runner.json-like object.

    * Process runners (python/node) require 'type' + 'entry'.
    * Connector runners require type='connector' + 'url'.
    """
    logger.debug(f"runner validation: checking schema for runner object: {runner}")
    if not isinstance(runner, dict):
        logger.debug("runner validation: failed (not a dict)")
        return False

    rtype = (runner.get("type") or "").strip().lower()
    if not rtype:
        logger.warning("runner validation: failed (missing 'type')")
        return False

    if rtype == "connector":
        ok = bool((runner.get("url") or "").strip())
        if not ok:
            logger.warning(
                "runner validation: 'connector' missing required 'url' field"
            )
        else:
            logger.debug("runner validation: connector schema is valid")
        return ok

    # FIX: For any runnable process type, the 'entry' field is essential for the
    # runtime. We now enforce its presence strictly. The old permissive check for
    # 'python' was the source of the bug.
    if rtype in ("python", "node"):
        if not (runner.get("entry") or "").strip():
            logger.warning(
                "runner validation: failed (missing required 'entry' for type=%r)", rtype
            )
            return False

    logger.debug("runner validation: schema appears valid for type=%r", rtype)
    return True


@dataclass(frozen=True)
class BuildReport:
    """A report summarizing the results of the materialization step."""

    files_written: int = 0
    artifacts_fetched: int = 0
    runner_path: Optional[str] = None


@dataclass(frozen=True)
class EnvReport:
    """A report summarizing the results of the environment preparation step."""

    python_prepared: bool = False
    node_prepared: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class BuildResult:
    """The final result of a successful build, containing all reports and data."""

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
    """Orchestrates a local project installation from a Hub plan."""

    def __init__(
        self, client: MatrixClient, *, fs_root: Optional[str | Path] = None
    ) -> None:
        """Initialize the installer with a MatrixClient and optional filesystem root."""
        self.client = client
        self.fs_root = Path(fs_root).expanduser() if fs_root else None
        logger.debug("LocalInstaller created (fs_root=%s)", self.fs_root)

    def plan(self, id: str, target: str | os.PathLike[str]) -> Dict[str, Any]:
        """
        Request an installation plan from the Hub.

        SECURITY: Converts local absolute paths to server-safe labels to avoid
        leaking client filesystem details.
        """
        logger.info("plan: requesting Hub plan for id=%s target=%s", id, target)

        # Use a server-safe label instead of an absolute path unless overridden.
        send_abs = (os.getenv("MATRIX_INSTALL_SEND_ABS_TARGET") or "").strip().lower()
        if send_abs in {"1", "true", "yes", "on"}:
            to_send = str(target)
            logger.debug("plan: sending absolute target path to server: %s", to_send)
        else:
            to_send = _plan_target_for_server(id, target)
            logger.debug("plan: sending server-safe target label to server: %s", to_send)

        outcome = self.client.install(id, target=to_send)
        logger.debug("plan: received outcome from Hub: %s", outcome)
        return _as_dict(outcome)

    def materialize(
        self, outcome: Dict[str, Any], target: str | os.PathLike[str]
    ) -> BuildReport:
        """Write files and fetch artifacts based on the installation plan."""
        logger.debug("materialize: starting materialization process.")
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
        logger.debug(f"materialize: finished. BuildReport: {report}")
        return report

    def prepare_env(
        self,
        target: str | os.PathLike[str],
        runner: Dict[str, Any],
        *,
        timeout: int = 900,
    ) -> EnvReport:
        """Prepare the runtime environment (e.g., venv, npm install)."""
        target_path = self._abs(target)
        runner_type = (runner.get("type") or "").lower()
        logger.info(
            "env: preparing environment (type=%s) in %s",
            runner_type or "-",
            _short(target_path),
        )
        logger.debug(f"env: using runner config: {runner}")

        py_ok, node_ok, notes = False, False, []
        if runner_type == "python":
            logger.debug("env: python runner detected, preparing python environment.")
            py_ok = self._prepare_python_env(target_path, runner, timeout)

        # Also check for 'node' key for mixed-language projects
        if runner_type == "node" or runner.get("node"):
            logger.debug("env: node runner or config detected, preparing node environment.")
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
        logger.debug(f"env: finished. EnvReport: {report}")
        return report

    def build(
        self,
        id: str,
        *,
        target: Optional[str | os.PathLike[str]] = None,
        alias: Optional[str] = None,
        timeout: int = 900,
    ) -> BuildResult:
        """Perform the full plan, materialize, and prepare_env workflow."""
        logger.info(f"build: starting full build for id='{id}', alias='{alias}'")
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
        logger.debug(f"build: finished. Final BuildResult: {result}")
        return result

    # --- Private Materialization Helpers ---

    def _find_file_candidates(self, outcome: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract all file descriptions from the various parts of the outcome."""
        logger.debug("materialize(files): searching for file candidates in outcome.")
        candidates: List[Dict[str, Any]] = []
        sources = [
            (outcome.get("plan") or {}).get("files", []),
            *(
                step.get("files", [])
                for step in outcome.get("results", [])
                if isinstance(step, dict)
            ),
            outcome.get("files", []),
        ]
        for i, source in enumerate(sources):
            if isinstance(source, list):
                logger.debug(f"materialize(files): found {len(source)} candidates in source #{i+1}.")
                candidates.extend(item for item in source if isinstance(item, dict))
        logger.debug(f"materialize(files): total candidates found: {len(candidates)}")
        return candidates

    def _materialize_files(self, outcome: Dict[str, Any], target_path: Path) -> int:
        """Find and write all declared files from the installation plan."""
        logger.info("materialize(files): writing declared files → %s", _short(target_path))
        candidates = self._find_file_candidates(outcome)
        files_written = 0
        for f in candidates:
            path = f.get("path") or f.get("rel") or f.get("dest")
            if not path:
                logger.debug("materialize(files): skipping candidate with no path: %s", f)
                continue

            # Normalize path separators (cross-platform safety)
            norm = str(path).replace("\\", "/").strip("/")
            p = (target_path / norm).resolve()
            logger.debug("materialize(files): preparing to write to %s", _short(p))
            p.parent.mkdir(parents=True, exist_ok=True)

            if (content_b64 := f.get("content_b64")) is not None:
                logger.debug("materialize(files): writing base64 content.")
                p.write_bytes(base64.b64decode(content_b64))
            elif (content := f.get("content")) is not None:
                logger.debug("materialize(files): writing text content.")
                p.write_text(content, encoding="utf-8")
            else:
                logger.debug("materialize(files): content not provided, touching file to create it.")
                p.touch()
            files_written += 1
        logger.info(f"materialize(files): successfully wrote {files_written} files.")
        return files_written

    def _handle_git_artifact(self, artifact: Dict[str, Any], target_path: Path) -> None:
        """Handle fetching a git-based artifact, including the legacy shim."""
        logger.debug(f"artifact(git): handling git artifact: {artifact}")
        spec = artifact.get("spec") or {}
        # WORKAROUND SHIM for legacy 'command' field
        if not spec.get("repo") and (
            cmd := (artifact.get("command") or "").strip()
        ).startswith("git clone"):
            logger.warning(
                "artifact(git): SHIM: Found legacy 'command' field. Attempting to derive spec."
            )
            try:
                parts = cmd.split()
                repo_idx = parts.index("clone") + 1
                spec["repo"] = parts[repo_idx]
                if "--branch" in parts:
                    ref_idx = parts.index("--branch") + 1
                    spec["ref"] = parts[ref_idx]
                logger.info(
                    "artifact(git): SHIM: Derived spec from legacy 'command' field: %s", spec
                )
            except (ValueError, IndexError) as e:
                logger.error(
                    "artifact(git): SHIM: Could not parse legacy 'command' (%s)", e
                )

        if fetch_git_artifact:
            logger.info(f"artifact(git): fetching with spec {spec} into {target_path}")
            fetch_git_artifact(spec=spec, target=target_path)
        else:
            logger.error("artifact(git): fetcher not available but git artifact was specified.")

    def _handle_http_artifact(
        self, artifact: Dict[str, Any], target_path: Path
    ) -> None:
        """Handle fetching a URL-based artifact."""
        logger.debug(f"artifact(http): handling http artifact: {artifact}")
        if fetch_http_artifact:
            url=artifact["url"]
            dest=artifact.get("path") or artifact.get("dest")
            sha256=str(s) if (s := artifact.get("sha256")) else None
            unpack=bool(artifact.get("unpack", False))
            logger.info(
                f"artifact(http): fetching url='{url}', dest='{dest}', unpack={unpack}"
            )
            fetch_http_artifact(
                url=url,
                target=target_path,
                dest=dest,
                sha256=sha256,
                unpack=unpack,
                logger=logger,
            )
        else:
            logger.error("artifact(http): fetcher not available but http artifact was specified.")

    def _materialize_artifacts(self, plan: Dict[str, Any], target_path: Path) -> int:
        """Dispatch artifact fetching to specialized handlers."""
        artifacts = plan.get("artifacts", [])
        if not isinstance(artifacts, list) or not artifacts:
            logger.debug("materialize(artifacts): no artifacts to fetch.")
            return 0
        logger.info("materialize(artifacts): fetching %d artifact(s)", len(artifacts))
        fetched_count = 0
        for i, a in enumerate(artifacts):
            logger.debug(f"materialize(artifacts): processing artifact #{i+1}: {a}")
            try:
                if isinstance(a, dict):
                    if a.get("kind") == "git":
                        self._handle_git_artifact(a, target_path)
                        fetched_count += 1
                    elif a.get("url"):
                        self._handle_http_artifact(a, target_path)
                        fetched_count += 1
                    else:
                        logger.warning(f"materialize(artifacts): skipping unknown artifact kind: {a}")
            except (GitFetchError, ArchiveFetchError) as e:
                logger.error("artifact: failed to fetch: %s", e)
                raise ManifestResolutionError(str(e)) from e
        logger.info(f"materialize(artifacts): successfully fetched {fetched_count} artifacts.")
        return fetched_count

    def _materialize_runner(
        self, outcome: Dict[str, Any], target_path: Path
    ) -> Optional[str]:
        """
        Find, infer, or synthesize a runner.json file for the project.
        Delegates to specialized helpers for each discovery strategy.
        Priority order is intentional to avoid surprising the user.
        """
        logger.info("runner: attempting to find or synthesize a runner configuration.")
        plan_node = outcome.get("plan") or {}

        strategies = [
            _try_fetch_runner_from_b64,           # 1) embedded b64
            _try_fetch_runner_from_url,           # 2) explicit URL
            _try_find_runner_from_object,         # 3) object in plan
            _try_find_runner_in_embedded_manifest, # 4) NEW: v2 embedded runner or v1 synth
            _try_find_runner_from_file,           # 5) file at known path
            _try_find_runner_via_shallow_search, # 6) shallow search
            _try_fetch_runner_from_manifest_url, # 7) synthesize via manifest URL
            _try_infer_runner_from_structure,     # 8) infer from files
            _try_synthesize_connector_runner,     # 9) fallback synthesize
        ]

        for strategy in strategies:
            strategy_name = strategy.__name__
            logger.debug(f"runner: trying strategy '{strategy_name}'...")
            runner_path = strategy(self, plan_node, target_path, outcome)
            if runner_path:
                logger.info(f"runner: success! Found runner using strategy '{strategy_name}'. Path: {_short(runner_path)}")
                return runner_path
            logger.debug(f"runner: strategy '{strategy_name}' did not find a runner.")

        logger.warning("runner: a valid runner config was not found or inferred")
        return None

    # --- Private Environment Helpers ---

    def _prepare_python_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> bool:
        """
        Creates a robust, fully isolated Python virtual environment (venv),
        upgrades core packaging tools, and installs dependencies.
        """
        logger.info("env(python): starting python environment preparation.")
        rp = runner.get("python") or {}
        venv_dir = rp.get("venv") or ".venv"
        venv_path = target_path / venv_dir

        # 1. --- CREATE A FRESH, ISOLATED VIRTUAL ENVIRONMENT ---
        logger.info(f"env(python): creating fresh, isolated venv in '{_short(venv_path)}'")
        try:
            venv.create(
                venv_path,
                with_pip=True,
                clear=True,
                symlinks=True,
                system_site_packages=False,  # Explicitly guarantee isolation
            )
        except Exception as e:
            logger.warning(
                f"env(python): venv creation with symlinks failed ({e}). Retrying without them."
            )
            venv.create(
                venv_path,
                with_pip=True,
                clear=True,
                symlinks=False,
                system_site_packages=False,  # Also apply isolation here
            )

        pybin = _python_bin(venv_path)
        logger.debug(f"env(python): venv created. Python executable is at '{pybin}'")

        # 2. --- UPGRADE CORE PACKAGING TOOLS ---
        logger.info("env(python): upgrading pip, setuptools, and wheel...")
        _run(
            [pybin, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            cwd=target_path,
            timeout=timeout,
        )

        # 3. --- PREPARE BASE INSTALL COMMAND ---
        pip_cmd = [pybin, "-m", "pip", "install"]
        index_url = (os.getenv("MATRIX_SDK_PIP_INDEX_URL") or "").strip()
        extra_index = (os.getenv("MATRIX_SDK_PIP_EXTRA_INDEX_URL") or "").strip()
        if index_url:
            pip_cmd.extend(["--index-url", index_url])
            logger.debug(f"env(python): using custom index-url: {index_url}")
        if extra_index:
            pip_cmd.extend(["--extra-index-url", extra_index])
            logger.debug(f"env(python): using custom extra-index-url: {extra_index}")

        # 4. --- ATTEMPT INSTALLATION WITH MODERN `python_builder` (if available) ---
        if python_builder:
            logger.info("env(python): trying modern python_builder to install dependencies...")
            ok = False
            try:
                # Modern signature check to pass new args if supported
                sig = inspect.signature(python_builder.run_python_build)
                kwargs = dict(target_path=target_path, runner_data=runner, logger=logger, timeout=timeout)
                if "index_url" in sig.parameters and index_url: kwargs["index_url"] = index_url
                if "extra_index_url" in sig.parameters and extra_index: kwargs["extra_index_url"] = extra_index
                ok = bool(python_builder.run_python_build(**kwargs))
            except TypeError:
                logger.debug("env(python): python_builder has legacy signature, calling without timeout/index args.")
                ok = bool(python_builder.run_python_build(target_path=target_path, runner_data=runner, logger=logger))
            except Exception as e:
                logger.warning(f"env(python): python_builder failed ({e}); continuing with legacy pip flow")
                ok = False

            if ok:
                logger.info("env(python): python_builder successfully installed dependencies.")
                return True
            else:
                logger.warning("env(python): python_builder did not find a known dependency file. Falling back.")

        # 5. --- FIND AND INSTALL FROM STANDARD DEPENDENCY FILES (LEGACY FALLBACK) ---
        logger.debug("env(python): using legacy dependency file search.")
        runner_reqs = (runner.get("python") or {}).get("requirements")
        req_path = target_path / "requirements.txt"
        pyproject = target_path / "pyproject.toml"
        setup_py = target_path / "setup.py"

        # Priority 1: `runner.json` specification
        if isinstance(runner_reqs, list) and runner_reqs:
            logger.debug(f"env(python): found requirements in runner.json: {runner_reqs}")
            # Special case for '-r file.txt'
            if len(runner_reqs) == 2 and runner_reqs[0] in ("-r", "--requirement"):
                req_file = target_path / runner_reqs[1]
                if not req_file.is_file():
                    logger.warning(f"env(python): runner specified '{req_file}', but it was not found. Proceeding to failover.")
                else:
                    logger.info(f"env(python): installing from runner-specified file: {req_file}")
                    _run(pip_cmd + runner_reqs, cwd=target_path, timeout=timeout)
                    return True
            else:
                logger.info("env(python): installing dependencies from runner.json specification...")
                _run(pip_cmd + runner_reqs, cwd=target_path, timeout=timeout)
                return True

        # Priority 2: `requirements.txt`
        if req_path.is_file():
            logger.info("env(python): found requirements.txt, installing dependencies...")
            _run(pip_cmd + ["-r", str(req_path)], cwd=target_path, timeout=timeout)
            return True

        # Priority 3: `pyproject.toml` or `setup.py`
        if pyproject.is_file() or setup_py.is_file():
            logger.info("env(python): found pyproject.toml/setup.py, installing project...")
            is_poetry_non_package = False
            if pyproject.is_file():
                _, is_poetry_non_package = _pyproject_backend_info(pyproject)

            if is_poetry_non_package:
                logger.info("env(python): poetry non-package mode detected. Running 'poetry install'.")
                try:
                    _run([pybin, "-m", "pip", "install", "poetry"], cwd=target_path, timeout=timeout)
                    _run([pybin, "-m", "poetry", "install"], cwd=target_path, timeout=timeout)
                except subprocess.CalledProcessError as e:
                    logger.error(f"env(python): 'poetry install' failed. Error: {e}")
                    raise
            else:
                try:
                    install_args = ["-e", "."] if _env_bool("MATRIX_SDK_PIP_EDITABLE", True) else ["."]
                    logger.debug(f"env(python): attempting project install with args: {install_args}")
                    _run(pip_cmd + install_args, cwd=target_path, timeout=timeout)
                except subprocess.CalledProcessError as e:
                    if "-e" in install_args:
                        logger.warning(f"env(python): editable install failed ({e}); retrying non-editable")
                        _run(pip_cmd + ["."], cwd=target_path, timeout=timeout)
                    else:
                        raise
            return True

        logger.info("env(python): no standard dependency file found. Skipping python deps install.")
        return True

    def _prepare_node_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> tuple[bool, Optional[str]]:
        """Install Node.js dependencies."""
        logger.info("env(node): starting node environment preparation.")
        np = runner.get("node") or {}
        pm = np.get("package_manager") or _detect_package_manager(target_path)
        if not pm:
            logger.warning("env(node): node config present but no package manager detected.")
            return False, "node requested but no package manager detected"

        logger.info(f"env(node): using package manager '{pm}'")
        cmd = [pm, "install"] + list(np.get("install_args", []))
        logger.debug(f"env(node): running install command: {' '.join(cmd)}")
        _run(cmd, cwd=target_path, timeout=timeout)
        logger.info("env(node): node dependencies installed successfully.")
        return True, None

    # --- Private Utility Helpers ---

    def _abs(self, path: str | os.PathLike[str]) -> Path:
        """Resolve a path, prepending the fs_root if necessary."""
        p = Path(path)
        if self.fs_root and not p.is_absolute():
            abs_path = self.fs_root / p
            logger.debug(f"_abs: prepended fs_root. {path} -> {abs_path}")
            return abs_path
        abs_path = p.expanduser().resolve()
        logger.debug(f"_abs: resolved path. {path} -> {abs_path}")
        return abs_path

    def _infer_runner(self, target: Path) -> Optional[Dict[str, Any]]:
        """Infer a default runner config from common file names."""
        logger.debug(f"runner(infer): checking for common files in {_short(target)}")
        # Priority 1: Specific entry points
        if (target / "server.py").exists():
            logger.info("runner(infer): found 'server.py', inferring python runner.")
            return {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}
        if (target / "server.js").exists() or (target / "package.json").exists():
            entry = "server.js" if (target / "server.js").exists() else "index.js"
            logger.info(f"runner(infer): found node files, inferring node runner with entry '{entry}'.")
            return {"type": "node", "entry": entry}

        # Priority 2: Generic Python project files
        if (target / "pyproject.toml").is_file() or (target / "requirements.txt").is_file() or (target / "setup.py").is_file():
            logger.info("runner(infer): found python project file. Will synthesize a runner and search for entry points.")

            # --- NEW LOGIC: Run the helper script to find potential servers ---
            potential_servers = []
            notes_lines = [
                "This runner was synthesized because no explicit 'runner.json' was found.",
                "An entry point could not be automatically determined.",
                "ACTION REQUIRED: Please edit the 'entry' field below with the correct server file.",
            ]
            try:
                # FIX: Correctly locate the helper script relative to the SDK package structure.
                # Assuming find_potential_servers.py is in the same directory as installer.py
                helper_script_path = Path(__file__).parent / "find_potential_servers.py"
                if not helper_script_path.is_file():
                    logger.error(f"runner(infer): Helper script not found at expected path: {helper_script_path}")
                    notes_lines.append("Automated server discovery failed: helper script not found.")
                else:
                    cmd = [sys.executable, str(helper_script_path), str(target)]
                    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
                    # Parse the output of the script
                    found_any = False
                    for line in result.stdout.splitlines():
                        if line.startswith("- "):
                            potential_servers.append(line[2:].strip())
                            found_any = True
                    if found_any:
                        notes_lines.append("Potential entry points found in the project:")
                        notes_lines.extend([f"  - {s}" for s in potential_servers])
                    else:
                        notes_lines.append("No likely server entry points were found during analysis.")

            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
                logger.warning(f"runner(infer): Could not run server finder script: {e}")
                notes_lines.append("Automated server discovery failed.")
            except Exception as e:
                logger.error(f"runner(infer): An unexpected error occurred running server finder: {e}")
                notes_lines.append("An unexpected error occurred during server discovery.")


            # If we found potential servers, use the first one as a default guess.
            # Otherwise, use a placeholder that will fail loudly.
            entry_point = potential_servers[0] if potential_servers else "EDIT_ME.py"

            return {
                "type": "python",
                "entry": entry_point,
                "python": {"venv": ".venv"},
                "notes": "\n".join(notes_lines)
            }

        logger.debug("runner(infer): no common files found for inference.")
        return None

    def _load_runner_from_report(
        self, report: BuildReport, target_path: Path
    ) -> Dict[str, Any]:
        """Load the runner.json file after materialization."""
        logger.debug("build: loading runner.json from build report.")
        runner_path = (
            Path(report.runner_path)
            if report.runner_path
            else target_path / "runner.json"
        )
        logger.debug(f"build: effective runner path is '{_short(runner_path)}'")
        if runner_path.is_file():
            try:
                runner_data = json.loads(runner_path.read_text("utf-8"))
                logger.info(f"build: successfully loaded runner config from {_short(runner_path)}")
                logger.debug(f"build: loaded runner data: {runner_data}")
                return runner_data
            except json.JSONDecodeError as e:
                logger.error(f"build: failed to decode runner JSON from {_short(runner_path)}: {e}")
                raise ManifestResolutionError(f"Invalid runner.json at {runner_path}") from e

        logger.warning("build: runner.json not found in %s; env prepare may be skipped.", _short(runner_path.parent))
        return {}


# --------------------------------------------------------------------------------------
# Standalone Helper Functions (contents omitted for brevity, they are unchanged)
# ... all functions from _python_bin to the end of the file remain the same ...
# --------------------------------------------------------------------------------------


def _python_bin(venv_path: Path) -> str:
    """Return the platform-specific path to the python executable in a venv."""
    return str(venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    """Execute a command in a subprocess (cross-platform, no shell)."""
    logger.debug(
        "exec: %s (cwd=%s, timeout=%ss)", " ".join(map(str, cmd)), _short(cwd), timeout
    )
    try:
        # Capture output to prevent it from cluttering the main logs unless there's an error
        result = subprocess.run(cmd, cwd=str(cwd), check=True, timeout=timeout, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if result.stdout:
            logger.debug(f"exec: --- STDOUT ---\n{result.stdout.strip()}")
        if result.stderr:
            logger.debug(f"exec: --- STDERR ---\n{result.stderr.strip()}")
        logger.debug("exec: command finished successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"exec: command failed with exit code {e.returncode}.")
        logger.error(f"exec: STDOUT:\n{e.stdout}")
        logger.error(f"exec: STDERR:\n{e.stderr}")
        raise
    except FileNotFoundError as e:
        logger.error(f"exec: command not found: {cmd[0]}. Is it in the system's PATH? Error: {e}")
        raise
    except subprocess.TimeoutExpired as e:
        logger.error(f"exec: command timed out after {timeout} seconds.")
        logger.error(f"exec: STDOUT:\n{e.stdout}")
        logger.error(f"exec: STDERR:\n{e.stderr}")
        raise


def _detect_package_manager(path: Path) -> Optional[str]:
    """Detect the Node.js package manager based on lock files."""
    logger.debug(f"node: detecting package manager in {_short(path)}")
    if (path / "pnpm-lock.yaml").exists():
        logger.debug("node: found pnpm-lock.yaml, using pnpm.")
        return "pnpm"
    if (path / "yarn.lock").exists():
        logger.debug("node: found yarn.lock, using yarn.")
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        logger.debug("node: found package-lock.json or package.json, using npm.")
        return "npm"
    logger.debug("node: no lockfile found.")
    return None


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Normalize Pydantic models or dataclasses to a plain dict."""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return {}


def _plan_target_for_server(id_str: str, target: str | os.PathLike[str]) -> str:
    """Convert a local absolute path into a server-safe label."""
    p = Path(str(target))
    alias = (p.parent.name or "runner").strip()
    version = (p.name or "0").strip()
    label = f"{alias}/{version}".replace("\\", "/").lstrip("/")
    result = label or "runner/0"
    logger.debug(f"_plan_target_for_server: converted '{target}' to label '{result}'")
    return result


def _ensure_local_writable(path: Path) -> None:
    """Fail fast with a clear error if the target directory isn't writable."""
    logger.debug(f"Checking write permissions for '{_short(path)}'")
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".matrix_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        logger.debug(f"Write probe successful for '{_short(path)}'")
    except Exception as e:  # pragma: no cover
        logger.error(f"Local install target not writable: {_short(path)} — {e}")
        raise PermissionError(f"Local install target not writable: {path} — {e}") from e
    finally:
        try:
            probe.unlink()
        except Exception:
            pass

def _pyproject_backend_info(pyproject: Path) -> tuple[Optional[str], bool]:
    """
    Return (build_backend, poetry_non_package_mode).

    poetry_non_package_mode is True iff tool.poetry.package-mode == false.
    Uses a fast, safe fallback to simple string sniffing if a TOML parser
    is unavailable or the file is malformed.
    """
    logger.debug(f"pyproject: analyzing {_short(pyproject)}")
    if not pyproject.is_file():
        logger.debug("pyproject: file not found.")
        return None, False

    try:
        data = pyproject.read_bytes()
    except OSError as e:
        logger.warning(f"pyproject: could not read file: {e}")
        return None, False

    backend: Optional[str] = None
    non_package = False

    if _tomllib:
        logger.debug("pyproject: using toml library for parsing.")
        try:
            obj = _tomllib.loads(data.decode("utf-8", "replace"))
            backend = (obj.get("build-system") or {}).get("build-backend")
            poetry_tool = (obj.get("tool") or {}).get("poetry", {})
            pkg_mode = poetry_tool.get("package-mode")
            if isinstance(pkg_mode, bool):
                non_package = pkg_mode is False
            logger.debug(f"pyproject: parsed backend='{backend}', non_package={non_package}")
        except Exception as e:
            logger.warning(f"pyproject: toml parsing failed ({e}), falling back to sniffing.")
            pass

    if backend is None and non_package is False:
        logger.debug("pyproject: using string sniffing as fallback.")
        try:
            s = data.decode("utf-8", "replace").lower()
            if "build-backend" in s and "poetry.core.masonry.api" in s:
                backend = "poetry.core.masonry.api"
            if "[tool.poetry]" in s and "package-mode" in s and "false" in s:
                non_package = True
            logger.debug(f"pyproject: sniffed backend='{backend}', non_package={non_package}")
        except Exception as e:
            logger.warning(f"pyproject: string sniffing failed: {e}")
            pass

    return backend, non_package

# ---------------------------- Connector & runner helpers (Refactored) -----------------


def _base_url_from_outcome(outcome_or_plan: Dict[str, Any]) -> Optional[str]:
    """Try to locate a provenance base URL for resolving relative runner URLs."""
    logger.debug("Resolving base URL from provenance...")
    try:
        # Check nested dicts
        for node in (outcome_or_plan or {}).values() if isinstance(outcome_or_plan, dict) else []:
            if isinstance(node, dict):
                if (prov := node.get("provenance")) and isinstance(prov, dict):
                    url = (prov.get("source_url") or prov.get("manifest_url") or "").strip()
                    if url:
                        logger.debug(f"Found base URL in nested provenance: {url}")
                        return url
        # Check top-level keys
        prov = outcome_or_plan.get("provenance") if isinstance(outcome_or_plan, dict) else None
        if isinstance(prov, dict):
            url = (prov.get("source_url") or prov.get("manifest_url") or "").strip()
            if url:
                logger.debug(f"Found base URL in top-level provenance: {url}")
                return url
    except Exception as e:
        logger.warning(f"Error while extracting base URL: {e}")
        return None
    logger.debug("No base URL found in provenance.")
    return None


def _resolve_url_with_base(
    raw_url: str, outcome: Dict[str, Any], plan_node: Dict[str, Any]
) -> str:
    raw = (raw_url or "").strip()
    if not raw: return ""

    if "://" in raw:
        logger.debug(f"URL '{raw}' is already absolute.")
        return raw

    base = _base_url_from_outcome(outcome) or _base_url_from_outcome(plan_node) or ""
    if not base:
        logger.warning(f"Could not resolve relative URL '{raw}' because no base URL was found.")
        return raw

    try:
        resolved = urljoin(base, raw)
        logger.debug(f"Resolved relative URL '{raw}' against base '{base}' -> '{resolved}'")
        return resolved
    except Exception as e:
        logger.error(f"Failed to join base URL '{base}' with '{raw}': {e}")
        return raw


def _try_fetch_runner_from_b64(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 0: Embedded base64 runner (plan.runner_b64)."""
    b64 = (plan_node.get("runner_b64") or "").strip()
    if not b64: return None
    logger.debug("runner(b64): found runner_b64 content.")
    try:
        data = base64.b64decode(b64)
        obj = json.loads(data.decode("utf-8"))
        if _is_valid_runner_schema(obj, logger):
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            logger.info("runner(b64): materialized from runner_b64 → %s", _short(rp))
            return str(rp)
        else:
            logger.warning("runner(b64): decoded runner object has invalid schema.")
    except Exception as e:
        logger.warning("runner(b64): failed to materialize from runner_b64 (%s)", e)
    return None


def _try_fetch_runner_from_url(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 1: Fetch runner from a URL specified in the plan."""
    runner_url = (plan_node.get("runner_url") or "").strip()
    if not runner_url: return None

    resolved = _resolve_url_with_base(runner_url, args[-1] if args else {}, plan_node)
    logger.info("runner(url): fetching from resolved runner_url → %s", resolved)
    try:
        with urllib.request.urlopen(resolved, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read().decode("utf-8")
        obj = json.loads(data)
        if _is_valid_runner_schema(obj, logger):
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            logger.info("runner(url): saved fetched runner.json → %s", _short(rp))
            return str(rp)
        else:
            logger.warning("runner(url): invalid schema from runner_url (ignored)")
    except Exception as e:
        logger.warning("runner(url): failed to fetch runner_url (%s)", e)
    return None


def _try_find_runner_from_object(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 2: Find runner from a direct object in the plan."""
    runner_obj = plan_node.get("runner")
    if isinstance(runner_obj, dict):
        logger.debug("runner(object): found runner object in plan.")
        if _is_valid_runner_schema(runner_obj, logger):
            runner_path = target_path / "runner.json"
            runner_path.write_text(json.dumps(runner_obj, indent=2), encoding="utf-8")
            logger.info("runner(object): materialized from plan.runner object → %s", _short(runner_path))
            return str(runner_path)
    return None


def _try_find_runner_in_embedded_manifest(
    installer: "LocalInstaller",
    plan_node: Dict[str, Any],
    target_path: Path,
    outcome: Dict[str, Any],
) -> Optional[str]:
    """Strategy: Look in embedded manifest objects for a 'runner' (v2) or synthesize one (v1)."""
    candidate_keys = ("manifest", "source_manifest", "echo_manifest", "input_manifest")
    nodes: List[Dict[str, Any]] = []

    for container in (plan_node, outcome):
        if isinstance(container, dict):
            for k in candidate_keys:
                if isinstance(v := container.get(k), dict):
                    nodes.append(v)

    if not nodes: return None
    logger.debug(f"runner(manifest): found {len(nodes)} embedded manifest candidate(s).")

    # v2 path: manifest['runner']
    for m in nodes:
        if isinstance(r := m.get("runner"), dict) and _is_valid_runner_schema(r, logger):
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(r, indent=2), encoding="utf-8")
            logger.info("runner(manifest): materialized from embedded manifest.runner (v2) → %s", _short(rp))
            return str(rp)

    # v1 path: synthesize from server URL
    for m in nodes:
        if url := _url_from_manifest(m):
            logger.debug(f"runner(manifest): found server URL '{url}' for v1 synthesis.")
            connector = _make_connector_runner(url)
            if _is_valid_runner_schema(connector, logger):
                rp = target_path / "runner.json"
                rp.write_text(json.dumps(connector, indent=2), encoding="utf-8")
                logger.info("runner(manifest): synthesized from embedded manifest server URL (v1) → %s", _short(rp))
                return str(rp)
    return None


def _try_find_runner_from_file(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 3: Find runner from a file on disk."""
    runner_file_name = (plan_node.get("runner_file") or "runner.json").strip()
    runner_file_name = runner_file_name.replace("\\", "/").lstrip("/")
    runner_path = (target_path / runner_file_name).resolve()
    logger.debug(f"runner(file): checking for file at {_short(runner_path)}")
    if runner_path.is_file():
        try:
            data = json.loads(runner_path.read_text("utf-8"))
            if _is_valid_runner_schema(data, logger):
                logger.info("runner(file): found valid runner file at %s", _short(runner_path))
                return str(runner_path)
        except json.JSONDecodeError:
            logger.warning("runner(file): file exists but is not valid JSON: %s", runner_path)
    return None


def _try_find_runner_via_shallow_search(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 4: Perform a shallow BFS search for the runner file."""
    runner_file_name = (plan_node.get("runner_file") or "runner.json").strip()
    search_depth = max(0, RUNNER_SEARCH_DEPTH_DEFAULT)
    is_bare_name = "/" not in runner_file_name and "\\" not in runner_file_name

    if search_depth and is_bare_name:
        logger.debug(f"runner(search): starting shallow search for '{runner_file_name}' (depth={search_depth})")
        if found := _find_runner_file_shallow(target_path, runner_file_name, search_depth):
            try:
                data = json.loads(found.read_text("utf-8"))
                if _is_valid_runner_schema(data, logger):
                    logger.info("runner(search): discovered valid runner at %s", _short(found))
                    return str(found)
            except json.JSONDecodeError:
                logger.warning("runner(search): discovered file but it's invalid JSON: %s", found)
    return None


def _make_connector_runner(url: str) -> Dict[str, Any]:
    return {
        "type": "connector",
        "integration_type": "MCP",
        "request_type": "SSE",
        "url": _ensure_sse_url(url),
        "endpoint": "/sse",
        "headers": {},
    }


def _host_allowed(url: str) -> bool:
    """Optional domain allowlist for manifest fetches."""
    raw = (os.getenv("MATRIX_SDK_MANIFEST_DOMAINS") or "").strip()
    if not raw: return True

    host = urlparse(url).hostname or ""
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    logger.debug(f"runner(manifest_url): checking host '{host}' against allowlist: {allowed}")
    return host.lower() in allowed


def _try_fetch_runner_from_manifest_url(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, outcome: Dict
) -> Optional[str]:
    """Strategy 6: fetch a manifest and synthesize a connector runner if possible."""
    if not _env_bool("MATRIX_SDK_ALLOW_MANIFEST_FETCH", True):
        logger.debug("runner(manifest_url): skipping, disabled by env var.")
        return None

    src = (
        plan_node.get("manifest_url")
        or (plan_node.get("provenance") or {}).get("source_url")
        or (outcome.get("provenance") or {}).get("source_url")
        or ""
    ).strip()
    if not src: return None

    resolved = _resolve_url_with_base(src, outcome, plan_node)
    if not _host_allowed(resolved):
        logger.debug("runner(manifest_url): host not allowed: %s", resolved)
        return None

    logger.debug(f"runner(manifest_url): attempting to fetch manifest from {resolved}")
    try:
        with urllib.request.urlopen(resolved, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read().decode("utf-8")
        manifest = json.loads(data)
        if url := _url_from_manifest(manifest):
            logger.info(f"runner(manifest_url): found server URL '{url}', synthesizing runner.")
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(_make_connector_runner(url), indent=2), encoding="utf-8")
            return str(rp)
    except Exception as e:
        logger.debug("runner(manifest_url): fetch/synthesis failed: %s", e)
    return None


def _try_infer_runner_from_structure(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 5: Infer runner from project file structure."""
    if inferred := installer._infer_runner(target_path):
        if _is_valid_runner_schema(inferred, logger):
            inferred_path = target_path / "runner.json"
            inferred_path.write_text(json.dumps(inferred, indent=2), encoding="utf-8")
            logger.info("runner(infer): inferred from structure → %s", _short(inferred_path))
            return str(inferred_path)
    return None


def _try_synthesize_connector_runner(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, outcome: Dict
) -> Optional[str]:
    """Strategy 8: Synthesize a connector runner from manifest data."""
    if _connector_enabled():
        logger.debug("runner(synth): connector synthesis is enabled, searching for MCP/SSE URL.")
        if url := _extract_mcp_sse_url(outcome) or _extract_mcp_sse_url(plan_node):
            logger.info(f"runner(synth): found MCP/SSE URL '{url}', synthesizing connector.")
            connector = _make_connector_runner(url)
            if _is_valid_runner_schema(connector, logger):
                synth_path = target_path / "runner.json"
                synth_path.write_text(json.dumps(connector, indent=2), encoding="utf-8")
                return str(synth_path)
    return None


def _ensure_sse_url(url: str) -> str:
    """Normalize a server URL to end with '/sse' (no trailing slash)."""
    try:
        url = (url or "").strip()
        if not url: return ""
        parsed = urlparse(url)
        path = (parsed.path or "").strip()
        if path.endswith("/sse/"):
            path = path[:-1]
        elif not path.endswith("/sse"):
            path = path.rstrip("/") + "/sse"
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    except Exception:
        return url


def _url_from_manifest(m: Dict[str, Any]) -> str:
    """Extract a server URL from a manifest dictionary and normalize to /sse."""
    try:
        reg = m.get("mcp_registration") or {}
        srv = reg.get("server") or m.get("server") or {}
        url = srv.get("url") or m.get("server_url") or ""
        return _ensure_sse_url(str(url)) if url else ""
    except Exception:
        return ""


def _extract_mcp_sse_url(node: Any) -> str | None:  # noqa: C901
    """Recursively walk a dictionary or list to find an MCP/SSE URL."""
    if isinstance(node, dict):
        for key in ("manifest", "source_manifest", "echo_manifest", "input_manifest"):
            if key in node and isinstance(node[key], dict):
                if url := _url_from_manifest(node[key]):
                    return url
        for v in node.values():
            if url := _extract_mcp_sse_url(v):
                return url
    elif isinstance(node, list):
        for item in node:
            if url := _extract_mcp_sse_url(item):
                return url
    return None


def _find_runner_file_shallow(root: Path, name: str, max_depth: int) -> Optional[Path]:
    """Perform a limited-depth, breadth-first search for a runner file."""
    if max_depth <= 0: return None
    from collections import deque
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visited: set[Path] = {root}
    logger.debug(f"Shallow search started for '{name}' from '{_short(root)}'")
    while queue:
        current_path, current_depth = queue.popleft()
        candidate = current_path / name
        if candidate.is_file():
            logger.debug(f"Shallow search found at '{_short(candidate)}'")
            return candidate

        if current_depth < max_depth:
            try:
                for child in current_path.iterdir():
                    if child.is_dir() and child not in visited:
                        visited.add(child)
                        queue.append((child, current_depth + 1))
            except OSError as e:
                logger.debug(f"Shallow search could not list dir '{_short(current_path)}': {e}")
                continue
    logger.debug(f"Shallow search for '{name}' finished, no file found.")
    return None
