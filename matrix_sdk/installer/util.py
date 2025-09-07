# SPDX-License-Identifier: MIT
"""Shared utilities and constants for the installer package.

Design goals
------------
- Tiny, dependency-free, and stable.
- No imports from other installer modules (avoid cycles).
- Cross-platform safe (Windows/macOS/Linux).
- Structured, minimal logging; DEBUG when MATRIX_SDK_DEBUG is set.

Public API
----------
- logger: module logger ("matrix_sdk.installer")
- _env_bool(name, default=False) -> bool
- _env_int(name, default) -> int
- HTTP_TIMEOUT (seconds)
- RUNNER_SEARCH_DEPTH_DEFAULT (int)
- _short(path, maxlen=120) -> str
- _as_dict(obj) -> dict
- _plan_target_for_server(id_str, target) -> str
- _ensure_local_writable(path: Path) -> None
- _find_runner_file_shallow(root: Path, name: str, max_depth: int) -> Optional[Path]
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

__all__ = [
    "logger",
    "_env_bool",
    "_env_int",
    "HTTP_TIMEOUT",
    "RUNNER_SEARCH_DEPTH_DEFAULT",
    "_short",
    "_as_dict",
    "_plan_target_for_server",
    "_ensure_local_writable",
    "_find_runner_file_shallow",
]


# ---------------------------------------------------------------------------
# Logger bootstrap (opt-in verbose logging via MATRIX_SDK_DEBUG)
# ---------------------------------------------------------------------------
logger = logging.getLogger("matrix_sdk.installer")


def _maybe_configure_logging() -> None:
    """Attach a simple stream handler if MATRIX_SDK_DEBUG is on.

    We *do not* force handlers otherwise to let host apps control logging.
    """
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in {"1", "true", "yes", "on"}:
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


_maybe_configure_logging()


# ---------------------------------------------------------------------------
# Env helpers & constants
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean from an environment variable with safe defaults."""
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an int from an environment variable with a fallback on errors."""
    try:
        raw = (os.getenv(name) or "").strip()
        return int(raw) if raw else default
    except Exception:
        return default


# Network and discovery tunables (override via env when needed)
HTTP_TIMEOUT: int = max(3, _env_int("MATRIX_SDK_HTTP_TIMEOUT", 15))
RUNNER_SEARCH_DEPTH_DEFAULT: int = _env_int("MATRIX_SDK_RUNNER_SEARCH_DEPTH", 2)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _short(path: Path | str, maxlen: int = 120) -> str:
    """Truncate a path-like string for compact, readable logs."""
    s = str(path)
    if len(s) <= maxlen:
        return s
    # Keep the tail; prefix with ellipsis
    return "…" + s[-(maxlen - 1) :]


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Normalize Pydantic models or dataclasses to a plain dict.

    This is intentionally permissive to avoid importing heavy libs here.
    """
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()  # type: ignore[no-any-return]
        except Exception:
            pass
    # Pydantic v1 / custom objects
    if hasattr(obj, "dict"):
        try:
            return obj.dict()  # type: ignore[no-any-return]
        except Exception:
            pass
    if isinstance(obj, dict):
        return obj
    return {}


def _plan_target_for_server(id_str: str, target: str | os.PathLike[str]) -> str:
    """Convert a local absolute path into a server-safe label.

    Mapping:
        <...>/<alias>/<version>  ->  "alias/version"
    Both components are sanitized for cross-platform use.
    """
    p = Path(str(target))
    alias = (p.parent.name or "runner").strip()
    version = (p.name or "0").strip()
    label = f"{alias}/{version}".replace("\\", "/").lstrip("/")
    result = label or "runner/0"
    logger.debug("plan: converted '%s' → '%s' (id=%s)", _short(target), result, id_str)
    return result


def _ensure_local_writable(path: Path) -> None:
    """Fail fast if the target directory isn't writable.

    Creates the directory if it doesn't exist, writes a tiny probe file, then
    removes it. Raises PermissionError with context on failure.
    """
    logger.debug("fs: ensuring writable target '%s'", _short(path))
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".matrix_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        logger.debug("fs: write probe succeeded for '%s'", _short(path))
    except Exception as e:  # pragma: no cover (OS/FS dependent)
        logger.error("fs: local target not writable: %s — %s", _short(path), e)
        raise PermissionError(f"Local install target not writable: {path} — {e}") from e
    finally:
        try:
            probe.unlink()
        except Exception:
            # Non-fatal: leave the probe if remove fails; surface no extra noise
            pass


def _find_runner_file_shallow(root: Path, name: str, max_depth: int) -> Optional[Path]:
    """Breadth-first search for *name* up to *max_depth*, skipping noisy dirs.

    Skipped directories (case-sensitive):
        node_modules, .venv, venv, .git, __pycache__

    Returns:
        The first matching Path found, or None if not found within depth.
    """
    if max_depth <= 0:
        return None

    from collections import deque

    skip_dirs = {"node_modules", ".venv", "venv", ".git", "__pycache__"}
    dq: "deque[tuple[Path, int]]" = deque([(root, 0)])
    seen: set[Path] = {root}

    logger.debug(
        "search: shallow search for '%s' from '%s' (depth=%d)",
        name,
        _short(root),
        max_depth,
    )

    while dq:
        cur, depth = dq.popleft()
        candidate = cur / name
        if candidate.is_file():
            logger.debug("search: found '%s'", _short(candidate))
            return candidate

        if depth < max_depth:
            try:
                for child in cur.iterdir():
                    if (
                        child.is_dir()
                        and child not in seen
                        and child.name not in skip_dirs
                    ):
                        seen.add(child)
                        dq.append((child, depth + 1))
            except OSError as e:
                logger.debug("search: could not list '%s': %s", _short(cur), e)
                continue

    logger.debug("search: finished for '%s'; no file found.", name)
    return None
