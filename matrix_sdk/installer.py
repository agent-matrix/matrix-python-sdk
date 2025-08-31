# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import inspect
import json
import logging
import os
import subprocess
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

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

    if not (runner.get("entry") or "").strip():
        logger.warning(
            "runner validation: failed (missing required 'entry' for type=%r)", rtype
        )
        return False

    logger.debug("runner validation: schema appears valid")
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
        to_send = (
            str(target)
            if (os.getenv("MATRIX_INSTALL_SEND_ABS_TARGET") or "").strip().lower()
            in {"1", "true", "yes", "on"}
            else _plan_target_for_server(id, target)
        )

        outcome = self.client.install(id, target=to_send)
        return _as_dict(outcome)

    def materialize(
        self, outcome: Dict[str, Any], target: str | os.PathLike[str]
    ) -> BuildReport:
        """Write files and fetch artifacts based on the installation plan."""
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
        """Prepare the runtime environment (e.g., venv, npm install)."""
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
        """Perform the full plan, materialize, and prepare_env workflow."""
        tgt = self._abs(target or default_install_target(id, alias=alias))
        logger.info("build: target resolved → %s", _short(tgt))

        # Fail fast if the local install location isn't writable.
        _ensure_local_writable(tgt)

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
        """Extract all file descriptions from the various parts of the outcome."""
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
        for source in sources:
            if isinstance(source, list):
                candidates.extend(item for item in source if isinstance(item, dict))
        return candidates

    def _materialize_files(self, outcome: Dict[str, Any], target_path: Path) -> int:
        """Find and write all declared files from the installation plan."""
        logger.info("materialize: writing declared files → %s", _short(target_path))
        candidates = self._find_file_candidates(outcome)
        logger.debug("materialize: %d file candidate(s) found", len(candidates))
        files_written = 0
        for f in candidates:
            path = f.get("path") or f.get("rel") or f.get("dest")
            if not path:
                continue

            # Normalize path separators (cross-platform safety)
            norm = str(path).replace("\\", "/").strip("/")
            p = (target_path / norm).resolve()
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
        """Handle fetching a git-based artifact, including the legacy shim."""
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
        """Handle fetching a URL-based artifact."""
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
        """Dispatch artifact fetching to specialized handlers."""
        artifacts = plan.get("artifacts", [])
        if not isinstance(artifacts, list) or not artifacts:
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
        """
        Find, infer, or synthesize a runner.json file for the project.
        Delegates to specialized helpers for each discovery strategy.
        Priority order is intentional to avoid surprising the user.
        """
        plan_node = outcome.get("plan") or {}

        strategies = [
            _try_fetch_runner_from_b64,  # 1) embedded b64
            _try_fetch_runner_from_url,  # 2) explicit URL
            _try_find_runner_from_object,  # 3) object in plan
            _try_find_runner_in_embedded_manifest,  # 4) NEW: v2 embedded runner or v1 synth
            _try_find_runner_from_file,  # 4) file at known path
            _try_find_runner_via_shallow_search,  # 5) shallow search
            _try_fetch_runner_from_manifest_url,  # 6) synthesize via manifest URL
            _try_infer_runner_from_structure,  # 7) infer from files
            _try_synthesize_connector_runner,  # 8) fallback synthesize
        ]

        for strategy in strategies:
            runner_path = strategy(self, plan_node, target_path, outcome)
            if runner_path:
                return runner_path

        logger.warning("runner: a valid runner config was not found or inferred")
        return None

    # --- Private Environment Helpers ---

    def _prepare_python_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> bool:  # noqa: C901
        """Create a venv and install Python dependencies (cross-platform)."""
        rp = runner.get("python") or {}
        venv_dir = rp.get("venv") or ".venv"
        venv_path = target_path / venv_dir
        if not venv_path.exists():
            logger.info("env: creating venv → %s", _short(venv_path))
            try:
                venv.create(venv_path, with_pip=True, clear=False, symlinks=True)
            except Exception as e:
                # Windows/FS robustness: retry without symlinks
                logger.warning(
                    "env: venv.create failed with symlinks=True (%s); retrying with symlinks=False",
                    e,
                )
                venv.create(venv_path, with_pip=True, clear=False, symlinks=False)

        # Optional pip index overrides (used only in fallback path below)
        index_url = (os.getenv("MATRIX_SDK_PIP_INDEX_URL") or "").strip()
        extra_index = (os.getenv("MATRIX_SDK_PIP_EXTRA_INDEX_URL") or "").strip()
        pybin = _python_bin(venv_path)

        if python_builder:
            logger.info("env: using modern python_builder to install dependencies…")
            ok = False
            try:
                # Probe signature to avoid passing kwargs unsupported by older releases.
                sig = inspect.signature(python_builder.run_python_build)  # type: ignore[attr-defined]
                kwargs = dict(
                    target_path=target_path,
                    runner_data=runner,
                    logger=logger,
                    timeout=timeout,
                )
                if "index_url" in sig.parameters and index_url:
                    kwargs["index_url"] = index_url
                if "extra_index_url" in sig.parameters and extra_index:
                    kwargs["extra_index_url"] = extra_index

                ok = bool(python_builder.run_python_build(**kwargs))  # type: ignore[misc]
            except TypeError:
                # Legacy signature without timeout/index args
                ok = bool(
                    python_builder.run_python_build(  # type: ignore[misc]
                        target_path=target_path,
                        runner_data=runner,
                        logger=logger,
                    )
                )
            except Exception as e:
                logger.warning(
                    "env: python_builder failed (%s); continuing with legacy pip flow if needed",
                    e,
                )
                ok = False

            if not ok:
                logger.warning(
                    "env: python_builder did not find a known dependency file to install."
                )
        else:
            logger.warning(
                "env: python_builder not found, falling back to requirements.txt/pyproject"
            )

        # Legacy pip/pyproject fallback (kept intact)
        # Always keep pip/setuptools current
        base_cmd = [pybin, "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"]
        _run(base_cmd, cwd=target_path, timeout=timeout)

        pip_cmd = [pybin, "-m", "pip", "install"]
        if index_url:
            pip_cmd += ["--index-url", index_url]
        if extra_index:
            pip_cmd += ["--extra-index-url", extra_index]

        req_path = target_path / "requirements.txt"
        pyproject = target_path / "pyproject.toml"
        setup_cfg = target_path / "setup.cfg"
        setup_py = target_path / "setup.py"

        if req_path.exists():
            _run(pip_cmd + ["-r", str(req_path)], cwd=target_path, timeout=timeout)
        elif pyproject.exists() or setup_py.exists() or setup_cfg.exists():
            # Best-effort editable install if a build config is present
            _run(pip_cmd + ["-e", "."], cwd=target_path, timeout=timeout)
        else:
            logger.info(
                "env: no requirements/pyproject/setup found; skipping python deps"
            )

        return True

    def _prepare_node_env(
        self, target_path: Path, runner: Dict[str, Any], timeout: int
    ) -> tuple[bool, Optional[str]]:
        """Install Node.js dependencies."""
        np = runner.get("node") or {}
        pm = np.get("package_manager") or _detect_package_manager(target_path)
        if not pm:
            return False, "node requested but no package manager detected"
        cmd = [pm, "install"] + list(np.get("install_args", []))
        _run(cmd, cwd=target_path, timeout=timeout)
        return True, None

    # --- Private Utility Helpers ---

    def _abs(self, path: str | os.PathLike[str]) -> Path:
        """Resolve a path, prepending the fs_root if necessary."""
        p = Path(path)
        if self.fs_root and not p.is_absolute():
            return self.fs_root / p
        return p.expanduser().resolve()

    def _infer_runner(self, target: Path) -> Optional[Dict[str, Any]]:
        """Infer a default runner config from common file names."""
        if (target / "server.py").exists():
            return {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}
        if (target / "server.js").exists() or (target / "package.json").exists():
            entry = "server.js" if (target / "server.js").exists() else "index.js"
            return {"type": "node", "entry": entry}
        return None

    def _load_runner_from_report(
        self, report: BuildReport, target_path: Path
    ) -> Dict[str, Any]:
        """Load the runner.json file after materialization."""
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
    """Return the platform-specific path to the python executable in a venv."""
    return str(venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> None:
    """Execute a command in a subprocess (cross-platform, no shell)."""
    logger.debug(
        "exec: %s (cwd=%s, timeout=%ss)", " ".join(map(str, cmd)), _short(cwd), timeout
    )
    subprocess.run(cmd, cwd=str(cwd), check=True, timeout=timeout)


def _detect_package_manager(path: Path) -> Optional[str]:
    """Detect the Node.js package manager based on lock files."""
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return "npm"
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
    return label or "runner/0"


def _ensure_local_writable(path: Path) -> None:
    """Fail fast with a clear error if the target directory isn't writable."""
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".matrix_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except Exception as e:  # pragma: no cover
        raise PermissionError(f"Local install target not writable: {path} — {e}") from e
    finally:
        try:
            probe.unlink()
        except Exception:
            pass


# ---------------------------- Connector & runner helpers (Refactored) -----------------


def _base_url_from_outcome(outcome_or_plan: Dict[str, Any]) -> Optional[str]:
    """Try to locate a provenance base URL for resolving relative runner URLs."""
    try:
        for node in (
            (outcome_or_plan or {}).values()
            if isinstance(outcome_or_plan, dict)
            else []
        ):
            if isinstance(node, dict):
                if (prov := node.get("provenance")) and isinstance(prov, dict):
                    url = (
                        prov.get("source_url") or prov.get("manifest_url") or ""
                    ).strip()
                    if url:
                        return url
        # direct keys (common patterns)
        prov = (
            outcome_or_plan.get("provenance")
            if isinstance(outcome_or_plan, dict)
            else None
        )
        if isinstance(prov, dict):
            url = (prov.get("source_url") or prov.get("manifest_url") or "").strip()
            if url:
                return url
    except Exception:
        return None
    return None


def _resolve_url_with_base(
    raw_url: str, outcome: Dict[str, Any], plan_node: Dict[str, Any]
) -> str:
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    # Absolute URL already
    if "://" in raw:
        return raw
    base = _base_url_from_outcome(outcome) or _base_url_from_outcome(plan_node) or ""
    try:
        return urljoin(base, raw) if base else raw
    except Exception:
        return raw


def _try_fetch_runner_from_b64(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 0: Embedded base64 runner (plan.runner_b64)."""
    b64 = (plan_node.get("runner_b64") or "").strip()
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
        obj = json.loads(data.decode("utf-8"))
        if _is_valid_runner_schema(obj, logger):
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            logger.info("runner: materialized from runner_b64 → %s", _short(rp))
            return str(rp)
    except Exception as e:
        logger.warning("runner: failed to materialize from runner_b64 (%s)", e)
    return None


def _try_fetch_runner_from_url(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 1: Fetch runner from a URL specified in the plan."""
    runner_url = (plan_node.get("runner_url") or "").strip()
    if not runner_url:
        return None
    resolved = _resolve_url_with_base(runner_url, args[-1] if args else {}, plan_node)
    logger.info("runner: fetching from runner_url → %s", resolved)
    try:
        with urllib.request.urlopen(resolved, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read().decode("utf-8")
        obj = json.loads(data)
        if _is_valid_runner_schema(obj, logger):
            rp = target_path / "runner.json"
            rp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
            logger.info("runner: saved fetched runner.json → %s", _short(rp))
            return str(rp)
        else:
            logger.warning("runner: invalid schema from runner_url (ignored)")
    except Exception as e:
        logger.warning("runner: failed to fetch runner_url (%s)", e)
    return None


def _try_find_runner_from_object(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 2: Find runner from a direct object in the plan."""
    if (runner_obj := plan_node.get("runner")) and isinstance(runner_obj, dict):
        if _is_valid_runner_schema(runner_obj, logger):
            runner_path = target_path / "runner.json"
            runner_path.write_text(json.dumps(runner_obj, indent=2), encoding="utf-8")
            logger.info(
                "runner: materialized from plan.runner object → %s", _short(runner_path)
            )
            return str(runner_path)
    return None


def _try_find_runner_in_embedded_manifest(
    installer: "LocalInstaller",
    plan_node: Dict[str, Any],
    target_path: Path,
    outcome: Dict[str, Any],
) -> Optional[str]:
    """
    Strategy: If the plan/outcome carries a manifest object:

    • v2 path: manifest['runner'] exists → validate & write runner.json
    • v1 path: no runner, but MCP server URL present → synthesize connector runner

    Works on common keys: 'manifest', 'source_manifest', 'echo_manifest', 'input_manifest'.
    """
    # Look for any embedded manifest-like dicts
    candidate_keys = ("manifest", "source_manifest", "echo_manifest", "input_manifest")
    nodes: List[Dict[str, Any]] = []

    for container in (plan_node, outcome):
        if not isinstance(container, dict):
            continue
        for k in candidate_keys:
            v = container.get(k)
            if isinstance(v, dict):
                nodes.append(v)

    if not nodes:
        return None

    # 1) v2: manifest has embedded 'runner'
    for m in nodes:
        try:
            r = m.get("runner")
            if isinstance(r, dict) and _is_valid_runner_schema(r, logger):
                rp = target_path / "runner.json"
                rp.write_text(json.dumps(r, indent=2), encoding="utf-8")
                logger.info(
                    "runner: materialized from embedded manifest.runner → %s",
                    _short(rp),
                )
                return str(rp)
        except Exception:
            # Fall through to v1 synthesis
            pass

    # 2) v1: no runner; try to synthesize connector runner from manifest server URL
    for m in nodes:
        try:
            url = _url_from_manifest(m)  # already normalizes to /sse
            if url:
                connector = {
                    "type": "connector",
                    "integration_type": "MCP",
                    "request_type": "SSE",
                    "url": url,
                    "endpoint": "/sse",
                    "headers": {},
                }
                if _is_valid_runner_schema(connector, logger):
                    rp = target_path / "runner.json"
                    rp.write_text(json.dumps(connector, indent=2), encoding="utf-8")
                    logger.info(
                        "runner: synthesized from embedded manifest server URL → %s",
                        _short(rp),
                    )
                    return str(rp)
        except Exception:
            pass

    return None


def _try_fetch_runner_from_manifest_url(
    installer: "LocalInstaller",
    plan_node: Dict[str, Any],
    target_path: Path,
    outcome: Dict[str, Any],
) -> Optional[str]:
    """
    Strategy: If no runner is provided but we have a provenance/source URL to a manifest,
    fetch it (opt-in) and synthesize a connector runner from its MCP server URL.
    """
    allow = (os.getenv("MATRIX_SDK_ALLOW_MANIFEST_FETCH") or "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow:
        return None

    src = (
        plan_node.get("manifest_url")
        or (plan_node.get("provenance") or {}).get("source_url")
        or (outcome.get("provenance") or {}).get("source_url")
        or ""
    )
    src = (src or "").strip()
    if not src:
        return None

    try:
        with urllib.request.urlopen(src, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read().decode("utf-8")
        manifest = json.loads(data)
        url = _url_from_manifest(manifest)
        if url:
            rp = target_path / "runner.json"
            connector = {
                "type": "connector",
                "integration_type": "MCP",
                "request_type": "SSE",
                "url": url,
                "endpoint": "/sse",
                "headers": {},
            }
            rp.write_text(json.dumps(connector, indent=2), encoding="utf-8")
            logger.info(
                "runner: synthesized from fetched manifest_url → %s", _short(rp)
            )
            return str(rp)
    except Exception as e:
        logger.debug("runner: manifest_url fetch/synthesis failed: %s", e)
    return None


def _try_find_runner_from_file(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 3: Find runner from a file on disk."""
    runner_file_name = (plan_node.get("runner_file") or "runner.json").strip()
    runner_file_name = runner_file_name.replace("\\", "/").lstrip("/")
    runner_path = (target_path / runner_file_name).resolve()
    if runner_path.is_file():
        try:
            data = json.loads(runner_path.read_text("utf-8"))
            if _is_valid_runner_schema(data, logger):
                logger.info("runner: found runner file at %s", _short(runner_path))
                return str(runner_path)
        except json.JSONDecodeError:
            logger.warning("runner: file exists but is not valid JSON: %s", runner_path)
    return None


def _try_find_runner_via_shallow_search(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 4: Perform a shallow BFS search for the runner file."""
    runner_file_name = (plan_node.get("runner_file") or "runner.json").strip()
    search_depth = max(0, RUNNER_SEARCH_DEPTH_DEFAULT)
    is_bare_name = "/" not in runner_file_name and "\\" not in runner_file_name
    if search_depth and is_bare_name:
        if found := _find_runner_file_shallow(
            target_path, runner_file_name, search_depth
        ):
            try:
                data = json.loads(found.read_text("utf-8"))
                if _is_valid_runner_schema(data, logger):
                    logger.info("runner: discovered %s", _short(found))
                    return str(found)
            except json.JSONDecodeError:
                logger.warning("runner: discovered but invalid JSON: %s", found)
    return None


# --- NEW: synthesize runner via manifest URL (opt-in, safe) ---


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
    """Optional domain allowlist for manifest fetches.

    MATRIX_SDK_MANIFEST_DOMAINS: comma-separated hostnames. If unset/empty → allow all.
    """
    raw = (os.getenv("MATRIX_SDK_MANIFEST_DOMAINS") or "").strip()
    if not raw:
        return True
    host = urlparse(url).hostname or ""
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return host.lower() in allowed if allowed else True


def _try_fetch_runner_from_manifest_url(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, outcome: Dict
) -> Optional[str]:
    """Strategy 6: fetch a manifest and synthesize a connector runner if possible.

    Controlled by env MATRIX_SDK_ALLOW_MANIFEST_FETCH (default: on). Optional domain
    allowlist via MATRIX_SDK_MANIFEST_DOMAINS.
    """
    if not _env_bool("MATRIX_SDK_ALLOW_MANIFEST_FETCH", True):
        return None

    # Try various locations for a manifest URL, and resolve relative URLs if needed
    src = (
        plan_node.get("manifest_url")
        or (plan_node.get("provenance") or {}).get("source_url")
        or (outcome.get("provenance") or {}).get("source_url")
        or ""
    ).strip()
    if not src:
        return None

    resolved = _resolve_url_with_base(src, outcome, plan_node)
    if not _host_allowed(resolved):
        logger.debug("runner: manifest host not allowed: %s", resolved)
        return None

    try:
        with urllib.request.urlopen(resolved, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read().decode("utf-8")
        manifest = json.loads(data)
        url = _url_from_manifest(manifest)
        if url:
            rp = target_path / "runner.json"
            rp.write_text(
                json.dumps(_make_connector_runner(url), indent=2), encoding="utf-8"
            )
            logger.info("runner: synthesized from manifest_url → %s", _short(rp))
            return str(rp)
    except Exception as e:
        logger.debug("runner: manifest_url fetch/synthesis failed: %s", e)
    return None


def _try_infer_runner_from_structure(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, *args
) -> Optional[str]:
    """Strategy 5: Infer runner from project file structure."""
    if inferred := installer._infer_runner(target_path):
        if _is_valid_runner_schema(inferred, logger):
            inferred_path = target_path / "runner.json"
            inferred_path.write_text(json.dumps(inferred, indent=2), encoding="utf-8")
            logger.info("runner: inferred from structure → %s", _short(inferred_path))
            return str(inferred_path)
    return None


def _try_synthesize_connector_runner(
    installer: LocalInstaller, plan_node: Dict, target_path: Path, outcome: Dict
) -> Optional[str]:
    """Strategy 8: Synthesize a connector runner from manifest data."""
    if _connector_enabled():
        url = _extract_mcp_sse_url(outcome) or _extract_mcp_sse_url(plan_node)
        if url:
            connector = {
                "type": "connector",
                "integration_type": "MCP",
                "request_type": "SSE",
                "url": url,
                "endpoint": "/sse",
                "headers": {},
            }
            if _is_valid_runner_schema(connector, logger):
                synth_path = target_path / "runner.json"
                synth_path.write_text(json.dumps(connector, indent=2), encoding="utf-8")
                logger.info(
                    "runner: synthesized MCP/SSE connector runner → %s", synth_path
                )
                return str(synth_path)
    return None


def _ensure_sse_url(url: str) -> str:
    """Normalize a server URL to end with '/sse' (no trailing slash)."""
    try:
        url = (url or "").strip()
        if not url:
            return ""
        parsed = urlparse(url)
        path = (parsed.path or "").strip()
        if path.endswith("/sse/"):
            path = path[:-1]
        elif not path.endswith("/sse"):
            path = (path.rstrip("/")) + "/sse"
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
        # Prioritize manifest keys for direct lookup
        for key in ("manifest", "source_manifest", "echo_manifest", "input_manifest"):
            if key in node and isinstance(node[key], dict):
                if url := _url_from_manifest(node[key]):
                    return url
        # Generic recursive search on values
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
    if max_depth <= 0:
        return None

    from collections import deque

    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visited: set[Path] = {root}

    while queue:
        current_path, current_depth = queue.popleft()

        candidate = current_path / name
        if candidate.is_file():
            return candidate

        if current_depth < max_depth:
            try:
                for child in current_path.iterdir():
                    if child.is_dir() and child not in visited:
                        visited.add(child)
                        queue.append((child, current_depth + 1))
            except OSError:
                continue

    return None
