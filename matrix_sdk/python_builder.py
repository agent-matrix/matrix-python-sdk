# matrix_sdk/python_builder.py
# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

# A list of Makefile targets to try, in order of preference.
MAKEFILE_TARGETS = ["install", "setup"]

# For parsing pyproject.toml
try:
    import tomllib
except ImportError:
    # Fallback for Python < 3.11
    try:
        import toml as tomllib
    except ImportError:
        tomllib = None


def _run_command(
    cmd: List[str], cwd: Path, logger: logging.Logger, timeout: int
) -> bool:
    """Helper to run a command and log the outcome."""
    short_cwd = cwd.name if len(str(cwd)) > 40 else str(cwd)
    logger.info("build: executing -> `%s` in %s", " ".join(cmd), short_cwd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if proc.stdout and proc.stdout.strip():
            logger.debug("build: --- STDOUT ---\n%s", proc.stdout.strip())
        logger.info("build: command successful.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error("build: command failed with exit code %d.", e.returncode)
        logger.error("build: --- STDOUT ---\n%s", e.stdout.strip())
        logger.error("build: --- STDERR ---\n%s", e.stderr.strip())
        return False
    except FileNotFoundError:
        logger.error(
            "build: command not found: `%s`. Is it installed and in the PATH?", cmd[0]
        )
        return False
    except Exception as e:
        logger.error("build: an unexpected error occurred: %s", e)
        return False


# --- NEW: Internal helper to reliably find the Python executable ---
def _get_python_executable(venv_path: Path) -> str:
    """Constructs the absolute path to the Python executable in a venv."""
    py_exe = venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py_exe.is_file():
        raise FileNotFoundError(f"Python executable not found in venv: {py_exe}")
    return str(py_exe)


def _handle_pyproject(
    py_exe: str, target_path: Path, logger: logging.Logger, timeout: int
) -> bool:
    """Handles installation logic for a pyproject.toml file."""
    if tomllib is None:
        logger.warning(
            "build: install 'toml' (pip install toml) for Python < 3.11 to parse pyproject.toml"
        )
        return False

    try:
        data = tomllib.loads((target_path / "pyproject.toml").read_text("utf-8"))
        poetry_config = data.get("tool", {}).get("poetry", {})

        if poetry_config.get("package-mode") is False:
            logger.info(
                "build: detected Poetry project in non-package (application) mode."
            )
            dependencies = poetry_config.get("dependencies", {})
            deps_to_install = [
                f"{pkg}{spec}"
                for pkg, spec in dependencies.items()
                if pkg.lower() != "python"
            ]
            if not deps_to_install:
                logger.info("build: no dependencies to install for this application.")
                return True
            logger.info("build: installing application dependencies directly...")
            cmd = [py_exe, "-m", "pip", "install"] + deps_to_install
        else:
            logger.info(
                "build: detected standard package. Installing with 'pip install .'"
            )
            cmd = [py_exe, "-m", "pip", "install", "."]

        return _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout)
    except Exception as e:
        logger.error("build: failed to process 'pyproject.toml': %s", e)
        return False


# --- MODIFIED: The public function now finds the Python executable itself ---
def run_python_build(
    *,
    target_path: Path,
    runner_data: Dict[str, Any],
    logger: logging.Logger,
    timeout: int,
) -> bool:
    """
    Runs a build process for a Python project by detecting the dependency file.
    """
    try:
        # Determine the venv path from the runner config and find the executable
        venv_dir = runner_data.get("python", {}).get("venv", ".venv")
        py_exe_path = _get_python_executable(target_path / venv_dir)
    except FileNotFoundError as e:
        logger.error("build: could not find python executable: %s", e)
        return False

    # Strategy 1: pyproject.toml (modern standard)
    if (target_path / "pyproject.toml").exists():
        return _handle_pyproject(py_exe_path, target_path, logger, timeout)

    # Strategy 2: requirements.txt (legacy)
    if (target_path / "requirements.txt").exists():
        logger.info(
            "build: detected 'requirements.txt'. Installing with 'pip install -r ...'"
        )
        cmd = [py_exe_path, "-m", "pip", "install", "-r", "requirements.txt"]
        return _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout)

    # Strategy 3: Makefile
    if (target_path / "Makefile").exists():
        logger.info(
            "build: detected 'Makefile'. Searching for known installation targets."
        )
        for target in MAKEFILE_TARGETS:
            logger.info("build: attempting `make %s`...", target)
            cmd = ["make", target]
            if _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout):
                return True  # Stop after the first successful make target
        logger.warning(
            "build: found Makefile, but failed to run targets: %s", MAKEFILE_TARGETS
        )
        return False

    # If no strategy was found
    logger.warning("build: no installable dependency file found.")
    return False
