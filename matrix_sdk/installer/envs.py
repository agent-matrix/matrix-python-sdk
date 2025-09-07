# SPDX-License-Identifier: MIT
"""Python/Node environment preparation (OS-sensitive).

Public API (used by core.py):
    - prepare_python_env(target_path: Path, runner: dict, timeout: int) -> bool
    - prepare_node_env(target_path: Path, runner: dict, timeout: int) -> tuple[bool, str|None]

Internal helpers:
    - _create_and_upgrade_venv
    - _try_modern_builder
    - _try_legacy_install
    - _install_local_project
    - _detect_package_manager
    - _python_bin
    - _run (exec wrapper)

Design:
    * Cross-platform and robust. Windows symlink fallback preserved.
    * No schema logic; only environment work.
    * Lazy import of optional modules (python_builder).
    * Small, actionable logs. INFO for decisions; DEBUG for details.
"""
from __future__ import annotations

import inspect
import logging
import os
import subprocess
import venv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Centralized logger / helpers (with safe fallback during migration)
# ---------------------------------------------------------------------------
try:
    from .util import _env_bool, _short
    from .util import logger as _LOGGER  # type: ignore
except Exception:  # pragma: no cover - transitional fallback
    _LOGGER = logging.getLogger("matrix_sdk.installer")
    if not _LOGGER.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(
            logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
        )
        _LOGGER.addHandler(_h)
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    _LOGGER.setLevel(
        logging.DEBUG if dbg in {"1", "true", "yes", "on"} else logging.INFO
    )

    def _short(path: Path | str, maxlen: int = 120) -> str:  # type: ignore[override]
        s = str(path)
        return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])

    def _env_bool(name: str, default: bool = False) -> bool:  # type: ignore[override]
        v = (os.getenv(name) or "").strip().lower()
        if not v:
            return default
        return v in {"1", "true", "yes", "on"}


logger = _LOGGER

# Optional modern python builder (lazy import)
try:
    from .. import python_builder  # type: ignore
except Exception:  # pragma: no cover
    python_builder = None  # type: ignore

__all__ = [
    "prepare_python_env",
    "prepare_node_env",
    "_detect_package_manager",
    "_python_bin",
]


# =============================================================================
# Public API
# =============================================================================


def prepare_python_env(target_path: Path, runner: Dict[str, Any], timeout: int) -> bool:
    """Create a venv and install python deps.

    Returns True if the environment is usable (deps installed or intentionally skipped).
    """
    logger.info("env(python): preparing in %s", _short(target_path))
    rp = runner.get("python") or {}
    venv_dir = rp.get("venv") or ".venv"
    venv_path = target_path / venv_dir

    pybin = _create_and_upgrade_venv(venv_path, target_path, timeout)

    # Build common pip install command (with optional index URLs)
    pip_cmd = [pybin, "-m", "pip", "install"]
    index_url = (os.getenv("MATRIX_SDK_PIP_INDEX_URL") or "").strip()
    extra_index = (os.getenv("MATRIX_SDK_PIP_EXTRA_INDEX_URL") or "").strip()
    if index_url:
        pip_cmd.extend(["--index-url", index_url])
        logger.debug("env(python): using index-url=%s", index_url)
    if extra_index:
        pip_cmd.extend(["--extra-index-url", extra_index])
        logger.debug("env(python): using extra-index-url=%s", extra_index)

    # Modern builder path (poetry/pdm/uv/etc.)
    if _try_modern_builder(target_path, runner, timeout, index_url, extra_index):
        logger.info("env(python): python_builder installed dependencies.")
        return True
    elif python_builder:
        logger.warning(
            "env(python): python_builder found but no known dep file – fallback."
        )

    # Legacy path: requirements/pyproject/setup
    if _try_legacy_install(target_path, runner, timeout, pip_cmd):
        return True

    logger.info("env(python): no dependency file found – skipping install.")
    return True


def prepare_node_env(
    target_path: Path, runner: Dict[str, Any], timeout: int
) -> Tuple[bool, Optional[str]]:
    """Install Node.js dependencies based on lockfile or explicit config."""
    logger.info("env(node): preparing in %s", _short(target_path))
    np = runner.get("node") or {}
    pm = np.get("package_manager") or _detect_package_manager(target_path)
    if not pm:
        logger.warning("env(node): config present but no package manager detected.")
        return False, "node requested but no package manager detected"

    cmd = [pm, "install"] + list(np.get("install_args", []))
    logger.debug("env(node): running command: %s", " ".join(map(str, cmd)))
    _run(cmd, cwd=target_path, timeout=timeout)
    logger.info("env(node): dependencies installed.")
    return True, None


# =============================================================================
# Venv & Python dependency helpers
# =============================================================================


def _create_and_upgrade_venv(venv_path: Path, target_path: Path, timeout: int) -> str:
    """Create a fresh venv and upgrade pip/setuptools/wheel.

    Preserves Windows symlink fallback semantics.
    Returns python binary path as string.
    """
    logger.info("env(python): creating venv → %s", _short(venv_path))
    try:
        venv.create(
            venv_path,
            with_pip=True,
            clear=True,
            symlinks=True,
            system_site_packages=False,
        )
    except Exception as e:
        logger.warning(
            "env(python): symlinked venv failed (%s); retry without symlinks.", e
        )
        venv.create(
            venv_path,
            with_pip=True,
            clear=True,
            symlinks=False,
            system_site_packages=False,
        )

    pybin = _python_bin(venv_path)
    logger.debug("env(python): python executable at %s", pybin)

    logger.info("env(python): upgrading pip/setuptools/wheel…")
    _run(
        [pybin, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        cwd=target_path,
        timeout=timeout,
    )
    return pybin


def _try_modern_builder(
    target_path: Path,
    runner: Dict[str, Any],
    timeout: int,
    index_url: str,
    extra_index: str,
) -> bool:
    """Attempt to install dependencies using optional python_builder module."""
    if not python_builder:
        return False

    logger.info("env(python): trying python_builder…")
    try:
        sig = inspect.signature(python_builder.run_python_build)  # type: ignore[attr-defined]
        kwargs = dict(
            target_path=target_path, runner_data=runner, logger=logger, timeout=timeout
        )
        if "index_url" in sig.parameters and index_url:
            kwargs["index_url"] = index_url
        if "extra_index_url" in sig.parameters and extra_index:
            kwargs["extra_index_url"] = extra_index
        ok = bool(python_builder.run_python_build(**kwargs))  # type: ignore[misc]
        return ok
    except TypeError:
        logger.debug("env(python): legacy python_builder signature – calling fallback.")
        try:
            return bool(
                python_builder.run_python_build(  # type: ignore[attr-defined]
                    target_path=target_path, runner_data=runner, logger=logger
                )
            )
        except Exception as e:  # pragma: no cover
            logger.warning("env(python): python_builder (legacy) failed: %s", e)
            return False
    except Exception as e:  # pragma: no cover
        logger.warning("env(python): python_builder failed: %s", e)
        return False


def _try_legacy_install(
    target_path: Path,
    runner: Dict[str, Any],
    timeout: int,
    pip_cmd: List[str],
) -> bool:
    """Search and install dependencies from conventional files.

    Order:
        1) runner[python][requirements]
        2) requirements.txt
        3) pyproject.toml / setup.py (editable if enabled)
    """
    logger.debug("env(python): legacy dep search…")

    runner_reqs = (runner.get("python") or {}).get("requirements")
    if isinstance(runner_reqs, list) and runner_reqs:
        if len(runner_reqs) == 2 and runner_reqs[0] in ("-r", "--requirement"):
            req_file = target_path / runner_reqs[1]
            if req_file.is_file():
                logger.info(
                    "env(python): installing from runner file: %s", _short(req_file)
                )
                _run(pip_cmd + runner_reqs, cwd=target_path, timeout=timeout)
                return True
            logger.warning(
                "env(python): runner specified '%s' not found – failover.",
                _short(req_file),
            )
        else:
            logger.info("env(python): installing from runner requirements list.")
            _run(pip_cmd + runner_reqs, cwd=target_path, timeout=timeout)
            return True

    req_path = target_path / "requirements.txt"
    if req_path.is_file():
        logger.info("env(python): installing from requirements.txt")
        _run(pip_cmd + ["-r", str(req_path)], cwd=target_path, timeout=timeout)
        return True

    pyproject = target_path / "pyproject.toml"
    setup_py = target_path / "setup.py"
    if pyproject.is_file() or setup_py.is_file():
        return _install_local_project(target_path, pyproject, pip_cmd, timeout)

    return False


def _install_local_project(
    target_path: Path,
    pyproject: Path,
    pip_cmd: List[str],
    timeout: int,
) -> bool:
    """Install local project via pip (editable by default) or poetry when needed."""
    logger.info("env(python): installing local project (pyproject/setup.py)…")

    # Light-weight backend inspection (kept local to avoid TOML dep here)
    is_poetry_non_package = False
    try:
        backend, non_pkg = _pyproject_backend_info(pyproject)
        is_poetry_non_package = bool(non_pkg)
        logger.debug(
            "env(python): backend=%s, poetry_non_package=%s",
            backend,
            is_poetry_non_package,
        )
    except Exception:  # pragma: no cover
        pass

    pybin = pip_cmd[0]
    if is_poetry_non_package:
        logger.info("env(python): poetry non-package mode – running 'poetry install'.")
        try:
            _run(
                [pybin, "-m", "pip", "install", "poetry"],
                cwd=target_path,
                timeout=timeout,
            )
            _run([pybin, "-m", "poetry", "install"], cwd=target_path, timeout=timeout)
        except subprocess.CalledProcessError as e:  # pragma: no cover
            logger.error("env(python): poetry install failed: %s", e)
            raise
    else:
        try:
            editable = _env_bool("MATRIX_SDK_PIP_EDITABLE", True)
            install_args = ["-e", "."] if editable else ["."]
            logger.debug("env(python): pip install args=%s", install_args)
            _run(pip_cmd + install_args, cwd=target_path, timeout=timeout)
        except subprocess.CalledProcessError as e:
            if "-e" in install_args:
                logger.warning(
                    "env(python): editable install failed (%s); retry non-editable.", e
                )
                _run(pip_cmd + ["."], cwd=target_path, timeout=timeout)
            else:  # pragma: no cover
                raise

    return True


# =============================================================================
# Node helpers
# =============================================================================


def _detect_package_manager(path: Path) -> Optional[str]:
    """Detect Node package manager by lockfiles, prefer pnpm>yarn>npm."""
    logger.debug("env(node): detecting package manager in %s", _short(path))
    if (path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (path / "yarn.lock").exists():
        return "yarn"
    if (path / "package-lock.json").exists() or (path / "package.json").exists():
        return "npm"
    return None


# =============================================================================
# Utilities
# =============================================================================


def _python_bin(venv_path: Path) -> str:
    """Return path to venv python executable (Windows/Unix)."""
    if os.name == "nt":
        return str(venv_path / "Scripts" / "python.exe")
    return str(venv_path / "bin" / "python")


def _run(cmd: List[str], *, cwd: Path, timeout: int) -> None:
    """Execute a command with logging and error surfacing."""
    logger.debug(
        "exec: %s (cwd=%s, timeout=%ss)", " ".join(map(str, cmd)), _short(cwd), timeout
    )
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if result.stdout:
            logger.debug("exec: --- STDOUT ---\n%s", result.stdout.strip())
        if result.stderr:
            logger.debug("exec: --- STDERR ---\n%s", result.stderr.strip())
    except subprocess.CalledProcessError as e:
        logger.error("exec: exit code %s", e.returncode)
        logger.error("exec: STDOUT:\n%s", e.stdout)
        logger.error("exec: STDERR:\n%s", e.stderr)
        raise
    except FileNotFoundError as e:
        logger.error("exec: command not found: %s (%s)", cmd[0], e)
        raise
    except subprocess.TimeoutExpired as e:
        logger.error("exec: timed out after %s s", timeout)
        logger.error("exec: STDOUT:\n%s", e.stdout)
        logger.error("exec: STDERR:\n%s", e.stderr)
        raise


# =============================================================================
# Minimal pyproject backend sniff (local copy to avoid toml dep here)
# =============================================================================


def _pyproject_backend_info(pyproject: Path) -> tuple[Optional[str], bool]:
    """Return (build_backend, poetry_non_package_mode) from pyproject.toml.

    Best-effort fast parse; falls back to string sniffing.
    """
    if not pyproject.is_file():
        return None, False

    try:
        data = pyproject.read_bytes()
    except OSError:  # pragma: no cover
        return None, False

    backend: Optional[str] = None
    non_package = False

    # Use stdlib tomllib if present, else sniff strings.
    try:
        import tomllib as _tomllib  # type: ignore
    except Exception:  # pragma: no cover
        _tomllib = None  # type: ignore

    if _tomllib:
        try:
            obj = _tomllib.loads(data.decode("utf-8", "replace"))  # type: ignore[attr-defined]
            backend = (obj.get("build-system") or {}).get("build-backend")
            poetry_tool = (obj.get("tool") or {}).get("poetry", {})
            pkg_mode = poetry_tool.get("package-mode")
            if isinstance(pkg_mode, bool):
                non_package = pkg_mode is False
        except Exception:  # pragma: no cover
            pass

    if backend is None and non_package is False:
        try:
            s = data.decode("utf-8", "replace").lower()
            if "build-backend" in s and "poetry.core.masonry.api" in s:
                backend = "poetry.core.masonry.api"
            if "[tool.poetry]" in s and "package-mode" in s and "false" in s:
                non_package = True
        except Exception:  # pragma: no cover
            pass

    return backend, non_package
