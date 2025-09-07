# SPDX-License-Identifier: MIT
from __future__ import annotations

from .installer.core import BuildReport, BuildResult, EnvReport, LocalInstaller

# Optional: re-export selected internal helpers if tests/downstream rely on them
try:  # pragma: no cover - optional
    from .installer.runner_schema import (
        _coerce_runner_to_legacy_process,
        _ensure_sse_url,
        _extract_mcp_sse_url,
        _is_valid_runner_schema,
        _url_from_manifest,
    )
except Exception:  # pragma: no cover - keep facade resilient
    pass

"""Compatibility facade for the legacy ``matrix_sdk.installer`` module.

Public API remains unchanged:

    from matrix_sdk.installer import LocalInstaller

Internally, this re-exports the orchestration class and dataclasses
from the new ``matrix_sdk.installer`` subpackage.
"""


__all__ = [
    "LocalInstaller",
    "BuildReport",
    "EnvReport",
    "BuildResult",
    # (Optionally present; don't rely on them for strict API stability)
    "_is_valid_runner_schema",
    "_coerce_runner_to_legacy_process",
    "_ensure_sse_url",
    "_url_from_manifest",
    "_extract_mcp_sse_url",
]
