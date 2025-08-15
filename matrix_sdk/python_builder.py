# matrix_sdk/python_builder.py
# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging
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

def _run_command(cmd: List[str], cwd: Path, logger: logging.Logger, timeout: int) -> bool:
    """Helper to run a command and log the outcome."""
    # Use a shortened cwd for cleaner logs
    short_cwd = cwd.name if len(str(cwd)) > 40 else str(cwd)
    logger.info("build: executing -> `%s` in %s", " ".join(cmd), short_cwd)
    try:
        # Run and capture output for better error reporting
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        # Log stdout only if it's useful (e.g., not just empty)
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
        logger.error("build: command not found: `%s`. Is it installed and in the PATH?", cmd[0])
        return False
    except Exception as e:
        logger.error("build: an unexpected error occurred: %s", e)
        return False

def _handle_pyproject(
    target_path: Path, python_executable: str, logger: logging.Logger, timeout: int
) -> bool:
    """Handles installation logic for a pyproject.toml file."""
    pyproject_path = target_path / "pyproject.toml"
    if tomllib is None:
        logger.warning("build: cannot parse 'pyproject.toml', 'tomllib' or 'toml' package is required.")
        return False

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        logger.error("build: failed to read or parse 'pyproject.toml': %s", e)
        return False

    # Check for Poetry's application mode (package-mode = false)
    poetry_config = data.get("tool", {}).get("poetry", {})
    if poetry_config.get("package-mode") is False:
        logger.info("build: detected Poetry project in non-package (application) mode.")
        dependencies = poetry_config.get("dependencies", {})
        
        # Construct a list of dependencies for pip, excluding 'python'
        deps_to_install = [
            f"{pkg}{spec}" for pkg, spec in dependencies.items() if pkg.lower() != "python"
        ]

        if not deps_to_install:
            logger.info("build: no dependencies to install for this application.")
            return True

        logger.info("build: installing application dependencies directly...")
        cmd = [python_executable, "-m", "pip", "install"] + deps_to_install
        return _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout)
    else:
        # Default behavior for standard packages
        logger.info("build: detected standard package. Installing with 'pip install .'")
        cmd = [python_executable, "-m", "pip", "install", "."]
        return _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout)

def run_python_build(
    *,
    target_path: Path,
    python_executable: str,
    logger: logging.Logger,
    timeout: int,
) -> bool:
    """
    Runs a build process for a Python project by detecting the dependency file.

    It tries strategies in the following order:
    1. pyproject.toml (handles both package and non-package modes)
    2. requirements.txt
    3. Makefile
    """
    # Strategy 1: pyproject.toml (modern standard)
    if (target_path / "pyproject.toml").exists():
        return _handle_pyproject(target_path, python_executable, logger, timeout)

    # Strategy 2: requirements.txt (legacy)
    if (target_path / "requirements.txt").exists():
        logger.info("build: detected 'requirements.txt'. Installing with 'pip install -r ...'")
        cmd = [python_executable, "-m", "pip", "install", "-r", "requirements.txt"]
        return _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout)

    # Strategy 3: Makefile
    if (target_path / "Makefile").exists():
        logger.info("build: detected 'Makefile'. Searching for known installation targets.")
        for target in MAKEFILE_TARGETS:
            logger.info("build: attempting `make %s`...", target)
            cmd = ["make", target]
            if _run_command(cmd, cwd=target_path, logger=logger, timeout=timeout):
                return True # Stop after the first successful make target
        logger.warning("build: found Makefile, but failed to run targets: %s", MAKEFILE_TARGETS)
        return False

    # If no strategy was successful
    return False