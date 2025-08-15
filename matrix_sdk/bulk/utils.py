# matrix_sdk/bulk/utils.py
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional


def make_idempotency_key(manifest: Dict[str, Any]) -> str:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def with_temp_extract(zip_path: str) -> Iterator[str]:
    import zipfile

    temp_dir = tempfile.mkdtemp(prefix="mcp_zip_")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def load_env_file(path: Optional[str]) -> None:
    """Tiny .env loader (KEY=VALUE lines)."""
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.lower().startswith("export "):
                    s = s[7:].strip()
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
