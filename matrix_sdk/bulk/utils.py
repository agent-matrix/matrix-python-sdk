# matrix_sdk/bulk/utils.py
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator


def make_idempotency_key(manifest: Dict[str, Any]) -> str:
    """
    Generate a stable idempotency key for a manifest dict by SHA256 hashing its
    JSON representation with sorted keys.
    """
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def with_temp_extract(zip_path: str) -> Iterator[str]:
    """
    Context manager that extracts a ZIP file into a temporary directory and
    yields its path, then cleans up on exit.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        import zipfile

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(temp_dir)
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def htpasswd_hash(password: str) -> str:
    """
    Compute an Apache-style htpasswd SHA1 hash for HTTP Basic auth.
    """
    import crypt

    # On most UNIX systems crypt.crypt uses SHA512 or MD5 by default; for htpasswd, use SHA1
    return crypt.crypt(password, salt="$apr1$")
