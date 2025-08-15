# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx


class ManifestResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManifestResult:
    url: str
    json: dict


def _is_http_url(u: str) -> bool:
    try:
        p = urlsplit(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _host_allowed(
    host: str, *, allow: Optional[Iterable[str]], block: Optional[Iterable[str]]
) -> bool:
    h = host.lower()
    if block:
        for b in block:
            if h == b.lower() or h.endswith(f".{b.lower()}"):
                return False
    if allow:
        for a in allow:
            if h == a.lower() or h.endswith(f".{a.lower()}"):
                return True
        return False
    return True


def resolve_manifest(
    url: str,
    *,
    allow_hosts: Optional[Iterable[str]] = None,
    block_hosts: Optional[Iterable[str]] = None,
    require_json: bool = True,
    size_limit: int = 512_000,
    timeout: float = 8.0,
) -> ManifestResult:
    """
    Safely resolve a manifest URL to JSON.

    - Only http/https allowed
    - Optional allow/block host policy
    - Enforces content-type JSON (if require_json)
    - Enforces size limit
    """
    if not _is_http_url(url):
        raise ManifestResolutionError("manifest URL must be http(s)")

    p = urlsplit(url)
    if not _host_allowed(p.hostname or "", allow=allow_hosts, block=block_hosts):
        raise ManifestResolutionError(f"host not allowed: {p.hostname}")

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        if r.status_code >= 400:
            raise ManifestResolutionError(f"http {r.status_code}")

        ctype = r.headers.get("content-type", "")
        if require_json and "json" not in ctype:
            raise ManifestResolutionError(f"non-JSON content-type: {ctype}")

        body = r.content
        if len(body) > size_limit:
            raise ManifestResolutionError(f"manifest too large ({len(body)} bytes)")

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            raise ManifestResolutionError(f"invalid JSON: {e}") from e

        return ManifestResult(url=url, json=data)
