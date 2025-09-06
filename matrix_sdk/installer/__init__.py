# SPDX-License-Identifier: MIT
from __future__ import annotations

"""
Matrix SDK — Installer subpackage (internal)

This package splits the legacy monolithic `matrix_sdk.installer` into
small, testable modules while keeping **backwards compatibility**.

Back-compat:
- `from matrix_sdk.installer import LocalInstaller` keeps working.
- This __init__ lazily re-exports public API from submodules to keep
  import-time overhead small and avoid circular imports.

Submodules
----------
- core.py              → LocalInstaller + Build* dataclasses (orchestration)
- runner_schema.py     → validators, coercers, URL helpers (pure)
- runner_discovery.py  → runner discovery strategies
- files.py             → file writes & artifact fetching
- envs.py              → Python/Node environment preparation
- util.py              → env flags, logging setup, small helpers
"""

from typing import Final

__all__ = [
    # Public orchestration API
    "LocalInstaller",
    "BuildReport",
    "EnvReport",
    "BuildResult",
    # Selected internal helpers used by tests/downstream
    "_is_valid_runner_schema",
    "_coerce_runner_to_legacy_process",
    "_ensure_sse_url",
    "_url_from_manifest",
    "_extract_mcp_sse_url",
]

# ---- Lazy attribute loader (PEP 562) --------------------------------------- #
_CORE_EXPORTS: Final = {
    "LocalInstaller",
    "BuildReport",
    "EnvReport",
    "BuildResult",
}

_SCHEMA_EXPORTS: Final = {
    "_is_valid_runner_schema",
    "_coerce_runner_to_legacy_process",
    "_ensure_sse_url",
    "_url_from_manifest",
    "_extract_mcp_sse_url",
}


def __getattr__(name: str):
    """Lazily import attributes from submodules on first access."""
    if name in _CORE_EXPORTS:
        # Resolve to matrix_sdk/installer/core.py
        if name == "LocalInstaller":
            from .core import LocalInstaller as _sym
        elif name == "BuildReport":
            from .core import BuildReport as _sym
        elif name == "EnvReport":
            from .core import EnvReport as _sym
        elif name == "BuildResult":
            from .core import BuildResult as _sym
        else:  # defensive
            raise AttributeError(name)
        globals()[name] = _sym
        return _sym

    if name in _SCHEMA_EXPORTS:
        # Resolve to matrix_sdk/installer/runner_schema.py
        if name == "_is_valid_runner_schema":
            from .runner_schema import _is_valid_runner_schema as _sym
        elif name == "_coerce_runner_to_legacy_process":
            from .runner_schema import _coerce_runner_to_legacy_process as _sym
        elif name == "_ensure_sse_url":
            from .runner_schema import _ensure_sse_url as _sym
        elif name == "_url_from_manifest":
            from .runner_schema import _url_from_manifest as _sym
        elif name == "_extract_mcp_sse_url":
            from .runner_schema import _extract_mcp_sse_url as _sym
        else:  # defensive
            raise AttributeError(name)
        globals()[name] = _sym
        return _sym

    raise AttributeError(name)


def __dir__():
    # Make star-imports and IDE completion pleasant
    return sorted(set(list(globals().keys()) + list(__all__)))
