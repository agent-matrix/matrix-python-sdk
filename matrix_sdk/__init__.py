# matrix_sdk/__init__.py
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
Matrix SDK — package exports.

We keep this file minimal and re-export the public API and the TLS/httpx helpers
from matrix_sdk.tls so the rest of the codebase can do:

    from matrix_sdk import _VERIFY, httpx_defaults, HTTPX_DEFAULTS

or import directly:

    from matrix_sdk.tls import VERIFY, httpx_defaults
"""

from .client import MatrixClient, MatrixError
from . import deep_link
from .deep_link import (
    DeepLink,
    HandleResult,
    InvalidMatrixUri,
    handle_install as handle_deep_link_install,
    parse as parse_deep_link,
)

# TLS / httpx defaults (re-exported for convenience & BC)
from .tls import (
    VERIFY,
    _VERIFY,           # legacy alias, kept intentionally
    HTTPX_DEFAULTS,
    httpx_defaults,
    http2_enabled,
)

__all__ = [
    # Core client
    "MatrixClient",
    "MatrixError",

    # Deep link utilities
    "deep_link",
    "InvalidMatrixUri",
    "DeepLink",
    "HandleResult",
    "parse_deep_link",
    "handle_deep_link_install",

    # TLS / httpx helpers
    "VERIFY",
    "_VERIFY",
    "HTTPX_DEFAULTS",
    "httpx_defaults",
    "http2_enabled",
]
