# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
from typing import Tuple

# Accept:
#  - "mcp_server:hello-sse-server@0.1.0"
#  - "tool:hello@0.1.0"
#  - "io.matrix/hello-sse-server@0.1.0" (legacy)
_RX_COLON = re.compile(
    r"^(?P<ns>[a-z0-9_]+):(?P<name>[a-z0-9._\-]+)@(?P<ver>[0-9][^@\s]*)$"
)
_RX_SLASH = re.compile(
    r"^(?P<ns>[a-z0-9_.-]+)/(?P<name>[a-z0-9._\-]+)@(?P<ver>[0-9][^@\s]*)$"
)


def parse_id(s: str) -> Tuple[str, str, str]:
    s = s.strip()
    m = _RX_COLON.match(s)
    if m:
        return m.group("ns"), m.group("name"), m.group("ver")
    m = _RX_SLASH.match(s)
    if m:
        return m.group("ns").replace(".", "_"), m.group("name"), m.group("ver")
    raise ValueError(f"invalid id: {s!r}")


def normalize_id(s: str) -> str:
    ns, name, ver = parse_id(s)
    return f"{ns}:{name}@{ver}"


def suggest_alias(s: str) -> str:
    """Generate a friendly alias from an id (name portion)."""
    try:
        _, name, _ = parse_id(s)
    except Exception:
        name = s
    alias = "".join(ch if (ch.isalnum() or ch in "-._") else "-" for ch in name).strip(
        "-._"
    )
    return alias or "matrix-entity"


def encode_id_for_path(s: str) -> str:
    """A filesystem-friendly token used when no alias is provided."""
    return normalize_id(s).replace(":", "_").replace("@", "_")
