# -*- coding: utf-8 -*-
"""
Matrix Hub Python SDK — local cache (ETag/TTL).

Stores small JSON payloads (e.g., search results) on disk under ~/.cache/matrix
by default. Pairs each payload with:
  - timestamp (Unix seconds)
  - etag (server-provided, used with If-None-Match)

Typical flow in client.search():
  1) Build cache_key from URL + params.
  2) Read cache.get(key, allow_expired=True) to obtain previous ETag.
  3) Send request with If-None-Match = etag.
  4) On 304, return cached payload.
  5) On 200, cache the new payload with response ETag.
  6) On network error, if cache.get(key) within TTL, return cached payload.

Notes:
- The cache format is simple JSON: {"ts": float, "etag": str|null, "payload": <any>}
- You can safely delete the cache directory at any time.
"""
from __future__ import annotations
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode


DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.cache/matrix"))
DEFAULT_TTL_SECONDS = 4 * 60 * 60  # 4 hours

@dataclass
class CachedResponse:
    payload: Any
    etag: Optional[str]
    timestamp: float

    def is_fresh(self, ttl_seconds: int) -> bool:
        return (time.time() - float(self.timestamp)) < float(ttl_seconds)

class Cache:
    """
    File-based cache for small JSON payloads.

    Example:
        cache = Cache()
        key = make_cache_key("http://host/catalog/search", {"q": "pdf"})
        entry = cache.get(key, allow_expired=True)
        cache.set(key, {"items": [], "total": 0}, etag="W/\"abc\"")
    """

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl = int(ttl)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------- Public API ------------------------------- #

    def get(self, key: str, *, allow_expired: bool = False) -> Optional[CachedResponse]:
        """
        Read a cached entry.

        Args:
            key: cache key (e.g., from make_cache_key)
            allow_expired: if True, return entry even if TTL has elapsed (useful to get ETag for conditional requests)

        Returns:
            CachedResponse or None if not found / invalid.
        """
        fpath = self._path_for_key(key)
        if not fpath.exists():
            return None
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            entry = CachedResponse(
                payload=data.get("payload"),
                etag=data.get("etag"),
                timestamp=float(data.get("ts") or 0.0),
            )
        except Exception:
            # Corrupt file — remove and ignore
            try:
                fpath.unlink()
            except Exception:
                pass
            return None

        if not allow_expired and not entry.is_fresh(self.ttl):
            return None
        return entry

    def set(self, key: str, response: Any, *, etag: Optional[str] = None) -> None:
        """
        Persist a payload with metadata.
        """
        fpath = self._path_for_key(key)
        tmp = fpath.with_suffix(".tmp")
        payload = {"ts": time.time(), "etag": etag, "payload": response}
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(fpath)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ------------------------------- Internals -------------------------------- #

    def _path_for_key(self, key: str) -> Path:
        # Hash the key to avoid long filenames and sanitize
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h}.json"

def _normalize_params(params: Dict[str, Any]) -> str:
    """
    Normalize params to a deterministic string (sorted & JSON-friendly)
    for stable cache keys.
    """
    # Convert nested values to JSON strings so they remain stable
    norm_items = []
    for k, v in (params or {}).items():
        if isinstance(v, (dict, list, tuple)):
            v_str = json.dumps(v, sort_keys=True, separators=(",", ":"))
        else:
            v_str = str(v)
        norm_items.append((k, v_str))
    norm_items.sort(key=lambda kv: kv[0])
    return urlencode(norm_items)

def make_cache_key(url: str, params: Dict[str, Any]) -> str:
    """
    Create a stable cache key from URL + normalized query params.
    """
    return f"{url}?{_normalize_params(params or {})}"
