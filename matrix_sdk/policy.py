# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Optional, Tuple

from .ids import parse_id, suggest_alias

__all__ = [
    "matrix_home",
    "runners_dir",
    "default_install_target",
    "default_port",
    "sanitize_segment",
]

# ---------------------------- path policy (HF-style) ----------------------------


def matrix_home() -> Path:
    """
    Return the base directory for all SDK state (HF-style "app home").

    Precedence:
      1) MATRIX_HOME              → full override (like HF_HOME)
      2) default: ~/.matrix       → legacy-safe default (do not break existing users)

    Note: We intentionally do NOT switch the default to XDG or platform-specific
    locations to avoid relocating existing installations. Users who want a
    different base can set MATRIX_HOME.
    """
    env = os.getenv("MATRIX_HOME")
    base = Path(env).expanduser() if env else (Path.home() / ".matrix")
    return base


def runners_dir(base: Optional[str | Path] = None) -> Path:
    """
    Return the directory where runner installs live.

    Examples:
      ~/.matrix/runners
      $MATRIX_HOME/runners
    """
    root = Path(base).expanduser() if base else matrix_home()
    return root / "runners"


_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_segment(s: str, *, fallback: str = "unnamed") -> str:
    """
    Sanitize a single path segment (no separators), portable across OSes.
    - allow letters, digits, dot, underscore, hyphen
    - collapse other chars to '_'
    - strip leading/trailing whitespace and dots
    - ensure not empty; fall back if necessary
    """
    s = (s or "").strip()
    if not s:
        return fallback
    s = _SAFE_SEGMENT.sub("_", s)
    # avoid weird edge cases like "." or ".." or empty after cleaning
    s = s.strip(" .") or fallback
    return s


def _derive_name_and_version(
    component_id: str, alias: Optional[str]
) -> Tuple[str, str]:
    """
    Derive a safe install name and version from the component id and/or alias.
    """
    # parse_id expected to return (namespace/name, ???, version) – we only need version here
    _, _, ver = parse_id(component_id)
    name = alias or suggest_alias(component_id)  # prefer explicit alias
    return sanitize_segment(name, fallback="runner"), sanitize_segment(
        ver or "0", fallback="0"
    )


def default_install_target(
    component_id: str,
    alias: Optional[str] = None,
    *,
    base: Optional[str | Path] = None,
) -> str:
    """
    Suggest a canonical install target for an entity id (without creating it).

    Layout (backwards compatible):
        ~/.matrix/runners/<alias-or-name>/<version>

    Honored overrides:
        MATRIX_HOME    – relocate the entire tree, e.g. /data/matrix
        base=...       – explicit function override (tests/customization)

    This function only computes the path; callers create directories as needed.
    """
    root = runners_dir(base)
    name, ver = _derive_name_and_version(component_id, alias)
    return str((root / name / ver).expanduser().resolve())


# --------------------------- networking convenience ----------------------------


def default_port() -> int:
    """
    Suggest a free localhost TCP port (best-effort).
    Note: This does not reserve the port; callers should still handle bind errors.
    """
    # Use a short-lived socket to let the kernel pick an ephemeral port.
    # IPv4 localhost is broadly compatible; if you need IPv6, consider AF_INET6.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
