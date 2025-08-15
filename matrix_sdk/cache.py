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

Upgrade (additive, non-breaking):
- Added convenience methods used by newer MatrixClient code:
    • make_key(path, params)       → stable hash key (path+params)
    • get_etag(key)                → read ETag without payload parsing
    • get_body(key)                → read cached payload (even if expired)
    • save(key, etag=?, body=..)  → alias to set()
- Kept legacy API:
    • get(key, allow_expired=False) → returns CachedResponse
    • set(key, response, etag=?)
    • make_cache_key(url, params)  → stable key based on URL+params
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
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
        cache.set(key, {"items": [], "total": 0}, etag='W/"abc"')

    New (compatible) helpers used by MatrixClient:
        cache_key = cache.make_key("/catalog/search", {"q": "pdf", "limit": 5})
        etag = cache.get_etag(cache_key)
        body = cache.get_body(cache_key)
        cache.save(cache_key, etag='W/"abc"', body={"items": [], "total": 0})
    """

    def __init__(
        self, cache_dir: Path | str = DEFAULT_CACHE_DIR, ttl: int = DEFAULT_TTL_SECONDS
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl = int(ttl)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------- Public API (legacy) ------------------------------- #

    def get(self, key: str, *, allow_expired: bool = False) -> Optional[CachedResponse]:
        """
        Read a cached entry.

        Args:
            key: cache key (e.g., from make_cache_key)
            allow_expired: If True, return an entry even if its TTL has
                           elapsed (useful for ETag reuse).

        Returns:
            CachedResponse or None if not found / invalid.
        """
        fpath = self._path_for_key(key)
        if not fpath.exists():
            return None
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("Cached data is not a dictionary")
            entry = CachedResponse(
                payload=data.get("payload"),
                etag=data.get("etag"),
                timestamp=float(data.get("ts") or 0.0),
            )
        except (json.JSONDecodeError, TypeError, KeyError):
            # Corrupt file — remove and ignore
            try:
                fpath.unlink()
            except OSError:
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
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(fpath)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------- Public API (new helpers) ------------------------------- #

    def make_key(self, path: str, params: Dict[str, Any]) -> str:
        """
        Create a stable key for a logical route and its params.
        This mirrors the in-memory helper used by newer clients.

        Note:
            • path should be the route path (e.g., "/catalog/search"), not a full URL
            • params should be JSON-serializable
        """
        canon = json.dumps(
            {"path": path, "params": _jsonify(params)},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def get_etag(self, key: str) -> Optional[str]:
        """
        Return the stored ETag for a key, if present (expired entries allowed).
        """
        entry = self.get(key, allow_expired=True)
        return entry.etag if entry else None

    def get_body(self, key: str) -> Optional[Any]:
        """
        Return the stored payload for a key (expired entries allowed).

        This is primarily used to serve 304 Not Modified responses from cache.
        """
        entry = self.get(key, allow_expired=True)
        return entry.payload if entry else None

    def save(self, key: str, *, etag: Optional[str], body: Any) -> None:
        """
        Alias to set() matching the newer helper signature.
        """
        self.set(key, body, etag=etag)

    # ------------------------------- Internals -------------------------------- #

    def _path_for_key(self, key: str) -> Path:
        # Hash the key to avoid long filenames and sanitize
        h = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{h}.json"


def _jsonify(value: Any) -> Any:
    """
    Ensure values used in key generation are JSON-stable.
    """
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))
        except Exception:
            return str(value)
    return value


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

    (Legacy helper — retained for backwards compatibility.)
    """
    return f"{url}?{_normalize_params(params or {})}"
