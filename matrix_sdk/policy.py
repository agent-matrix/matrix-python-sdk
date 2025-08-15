# SPDX-License-Identifier: MIT
from __future__ import annotations

import socket
from pathlib import Path
from typing import Optional

from .ids import parse_id, suggest_alias


def default_install_target(
    component_id: str, alias: Optional[str] = None, *, base: Optional[str | Path] = None
) -> str:
    """
    Suggest a canonical install target for an entity id.

    ~/.matrix/runners/<alias-or-name>/<version>
    """
    base_dir = Path(base) if base else Path.home() / ".matrix" / "runners"
    name = alias or suggest_alias(component_id)
    _, _, ver = parse_id(component_id)
    return str((base_dir / name / ver).expanduser())


def default_port() -> int:
    """Suggest a free localhost port by briefly binding to :0."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()
