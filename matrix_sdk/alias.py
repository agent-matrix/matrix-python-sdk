# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def _home() -> Path:
    return Path(os.getenv("MATRIX_HOME") or (Path.home() / ".matrix")).expanduser()


def _aliases_path() -> Path:
    return _home() / "aliases.json"


class AliasStore:
    """
    Simple, atomic JSON KV at ~/.matrix/aliases.json

    Value shape (extensible):
    {
      "target": "/abs/path/to/install",
      "id": "tool:hello@0.1.0",
      "created_at": "2025-08-15T12:34:56Z",
      "updated_at": "2025-08-15T12:34:56Z"
    }
    """

    def __init__(self, file: Optional[str | Path] = None) -> None:
        self.file = Path(file).expanduser() if file else _aliases_path()
        self.file.parent.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.file.write_text("{}", encoding="utf-8")

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[k] = v
            elif isinstance(v, str):
                out[k] = {"target": v}
            else:
                out[k] = {"target": str(v)}
        return out

    def _write_atomic(self, data: Dict[str, Dict[str, Any]]) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="aliases.", suffix=".json", dir=str(self.file.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.file)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

    def set(self, alias: str, *, id: Optional[str] = None, target: str) -> None:
        from datetime import datetime, timezone

        a = alias.strip().lower()
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        d = self._read()
        meta = d.get(a, {})
        meta["target"] = target
        meta["updated_at"] = now
        if id:
            meta.setdefault("id", id)
        meta.setdefault("created_at", now)
        d[a] = meta
        self._write_atomic(d)

    def get(self, alias: str) -> Optional[Dict[str, Any]]:
        return self._read().get(alias)

    def remove(self, alias: str) -> bool:
        d = self._read()
        if alias in d:
            del d[alias]
            self._write_atomic(d)
            return True
        return False

    def all(self) -> Dict[str, Dict[str, Any]]:
        return self._read()
