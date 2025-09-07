# SPDX-License-Identifier: MIT
from __future__ import annotations

# --- Core (required) ---------------------------------------------------------
# Make the primary public API available immediately.
from .core import BuildReport, BuildResult, EnvReport, LocalInstaller

"""
Matrix SDK — Installer subpackage (backwards compatible facade)

This package splits the legacy monolithic `matrix_sdk.installer` into
smaller modules, while keeping the legacy public surface:

    from matrix_sdk.installer import LocalInstaller

We *eagerly* re-export the core orchestration API so `matrix_cli` and
existing code import cleanly, and *optionally* re-export a few helper
functions that some downstream code imports defensively.

Modules expected (if present):
- core.py               → LocalInstaller + Build* dataclasses
- runner_schema.py      → validators, URL helpers (preferred)
- runner_discovery.py   → (fallback location for the same helpers)
"""

# --- Optional helpers (best effort) ------------------------------------------
# Prefer runner_schema; fall back to runner_discovery; else provide safe stubs.
try:
    from .runner_schema import (  # type: ignore
        _coerce_runner_to_legacy_process,
        _ensure_sse_url,
        _extract_mcp_sse_url,
        _is_valid_runner_schema,
        _url_from_manifest,
    )
except ImportError:
    try:
        from .runner_discovery import (  # type: ignore
            _coerce_runner_to_legacy_process,
            _ensure_sse_url,
            _extract_mcp_sse_url,
            _is_valid_runner_schema,
            _url_from_manifest,
        )
    except ImportError:
        # Safe stubs so downstream defensive imports won't crash at import time.
        # These match the names used in legacy codepaths but do nothing harmful.
        def _is_valid_runner_schema(*_args, **_kwargs):  # pragma: no cover
            return False

        def _coerce_runner_to_legacy_process(obj):  # pragma: no cover
            return obj

        def _ensure_sse_url(url: str):  # pragma: no cover
            try:
                u = (url or "").rstrip("/")
                return f"{u}/sse" if u and not u.endswith("/sse") else u
            except Exception:
                return url

        def _url_from_manifest(m: dict):  # pragma: no cover
            try:
                return (
                    (m.get("mcp_registration", {}) or {})
                    .get("server", {})
                    .get("url", "")
                ) or ""
            except Exception:
                return ""

        def _extract_mcp_sse_url(_node):  # pragma: no cover
            return None


__all__ = [
    # Core orchestration API
    "LocalInstaller",
    "BuildReport",
    "EnvReport",
    "BuildResult",
    # Optional helpers (present if module available; otherwise stubs)
    "_is_valid_runner_schema",
    "_coerce_runner_to_legacy_process",
    "_ensure_sse_url",
    "_url_from_manifest",
    "_extract_mcp_sse_url",
]
