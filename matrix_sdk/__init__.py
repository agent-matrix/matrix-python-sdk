# -*- coding: utf-8 -*-
"""
matrix_sdk.__init__

Public SDK surface for Matrix Hub Python SDK.

This module intentionally re-exports the primary client adapter and the
typed error class used by callers (including matrix-cli). The change is
additive and safe for production: no previous runtime behavior is removed.

Exports:
    - MatrixClient : High-level HTTP client compatible with Matrix Hub APIs
                     and the refactored Matrix CLI contract.
    - SDKError     : Lightweight exception carrying HTTP status + detail.
"""

from .client import MatrixClient, SDKError

__all__ = ["MatrixClient", "SDKError"]
