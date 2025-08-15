# matrix_sdk/__init__.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from . import deep_link
from .client import MatrixClient, MatrixError
from .deep_link import (
    DeepLink,
    HandleResult,
    InvalidMatrixUri,
)
from .deep_link import (
    handle_install as handle_deep_link_install,
)
from .deep_link import (
    parse as parse_deep_link,
)

__all__ = [
    "MatrixClient",
    "MatrixError",
    "deep_link",
    "InvalidMatrixUri",
    "DeepLink",
    "HandleResult",
    "parse_deep_link",
    "handle_deep_link_install",
]
