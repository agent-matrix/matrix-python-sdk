# -*- coding: utf-8 -*-
"""
Matrix Hub Python SDK — HTTP client.

Exposes a small, typed surface over Matrix Hub's REST API:
- search(...)          → GET /catalog/search
- get_entity(...)      → GET /catalog/entities/{id}
- install(...)         → POST /catalog/install
- list_remotes(...)    → GET /catalog/remotes
- add_remote(...)      → POST /catalog/remotes
- trigger_ingest(...)  → POST /catalog/ingest?remote=<name>

Optional ETag/TTL caching:
- If a `Cache` instance is passed, `search()` will send `If-None-Match` and
  return cached payload on 304. On network errors, it can fall back to a fresh
  cached item if still within TTL.

Return types:
- If `matrix_sdk.schemas` is available, responses will be parsed into Pydantic
  models (SearchResponse, EntityDetail, InstallOutcome). Otherwise, `dict`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional, Type, TypeVar, Union
from urllib.parse import quote, urlencode

import httpx

try:
    # Optional typed models (recommended)
    from .schemas import (
        EntityDetail,
        InstallOutcome,
        MatrixAPIError,
        SearchResponse,
    )

    _HAS_TYPES = True
except Exception:  # pragma: no cover
    SearchResponse = EntityDetail = Dict[str, Any]  # type: ignore
    InstallOutcome = Dict[str, Any]  # type: ignore
    MatrixAPIError = RuntimeError  # type: ignore
    _HAS_TYPES = False
try:
    # Optional cache (recommended)
    from .cache import Cache, make_cache_key  # type: ignore
except Exception:  # pragma: no cover
    Cache = None  # type: ignore

    def make_cache_key(url: str, params: Dict[str, Any]) -> str:  # type: ignore
        # Minimal fallback; not persisted
        return (
            url
            + "?"
            + urlencode(sorted((k, str(v)) for k, v in (params or {}).items()))
        )


__all__ = [
    "MatrixClient",
]


T = TypeVar("T")


class MatrixClient:
    """
    Thin sync client around httpx for Matrix Hub.

    Example:
        from matrix_sdk.client import MatrixClient
        c = MatrixClient("http://localhost:7300", token="...")
        res = c.search(q="summarize pdfs", type="agent", capabilities="pdf,summarize")
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        *,
        timeout: float = 20.0,
        cache: Optional["Cache"] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache = cache

        self._headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": user_agent or "matrix-python-sdk/0.1 (+python-httpx)",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    # Public API
    def search(
        self, *, q: str, type: Optional[str] = None, **filters: Any
    ) -> Union[SearchResponse, Dict[str, Any]]:
        """
        Perform a catalog search.

        Parameters:
            q: free-text query (required)
            type: "agent" | "tool" | "mcp_server" (optional)
            **filters: capabilities, frameworks, providers, limit, offset, mode, with_rag, rerank...
        """
        if not q:
            raise ValueError("q (query) is required")

        params: Dict[str, Any] = {"q": q}
        if type:
            params["type"] = type
        params.update({k: v for k, v in filters.items() if v is not None})

        path = "/catalog/search"
        url = f"{self.base_url}{path}"

        # Optional cache: add If-None-Match and handle 304
        headers = dict(self._headers)
        cache_key = make_cache_key(url, params)
        cached_entry = None
        if self.cache:
            cached_entry = self.cache.get(
                cache_key, allow_expired=True
            )  # allow ETag reuse
            if cached_entry and cached_entry.etag:
                headers["If-None-Match"] = cached_entry.etag

        try:
            resp = self._request("GET", path, params=params, headers=headers)
            if resp.status_code == 304 and cached_entry and self.cache:
                # Not modified — serve cached payload even if TTL expired
                # (server guarantees it's unchanged).
                return self._parse(SearchResponse, cached_entry.payload)
            data = self._safe_json(resp)
            # Save to cache with new ETag (if present)
            if self.cache:
                self.cache.set(cache_key, data, etag=resp.headers.get("ETag"))
            return self._parse(SearchResponse, data)
        except httpx.RequestError as e:
            # Network issue; try to serve a fresh cached value if within TTL
            if self.cache:
                fresh = self.cache.get(cache_key, allow_expired=False)
                if fresh:
                    return self._parse(SearchResponse, fresh.payload)
            raise MatrixAPIError(str(e)) from e

    def get_entity(self, id: str) -> Union[EntityDetail, Dict[str, Any]]:
        """
        Fetch full entity detail by its id (uid), e.g., "agent:pdf-summarizer@1.4.2".
        """
        if not id:
            raise ValueError("id is required")
        enc = quote(id, safe="")
        resp = self._request("GET", f"/catalog/entities/{enc}")
        return self._parse(EntityDetail, self._safe_json(resp))

    def install(
        self, id: str, target: str, version: Optional[str] = None
    ) -> Union[InstallOutcome, Dict[str, Any]]:
        """
        Execute install plan for an entity.
        """
        if not id:
            raise ValueError("id is required")
        if not target:
            raise ValueError("target is required")

        body: Dict[str, Any] = {"id": id, "target": target}
        if version:
            body["version"] = version

        resp = self._request("POST", "/catalog/install", json_body=body)
        return self._parse(InstallOutcome, self._safe_json(resp))

    # Optional remotes management
    def list_remotes(self) -> Dict[str, Any]:
        """
        List configured catalog remotes.
        """
        resp = self._request("GET", "/catalog/remotes")
        return self._safe_json(resp)

    def add_remote(
        self,
        url: str,
        *,
        name: Optional[str] = None,
        trust_policy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Add a new catalog remote by URL (usually an index.json).

        Body is intentionally permissive to allow server-side defaults:
            { "url": "...", "name": "...", "trust_policy": {...} }
        """
        if not url:
            raise ValueError("url is required")
        payload: Dict[str, Any] = {"url": url}
        if name is not None:
            payload["name"] = name
        if trust_policy is not None:
            payload["trust_policy"] = trust_policy

        resp = self._request("POST", "/catalog/remotes", json_body=payload)
        return self._safe_json(resp)

    def trigger_ingest(self, name: str) -> Dict[str, Any]:
        """
        Manually trigger ingest for a named remote.
        """
        if not name:
            raise ValueError("name is required")
        resp = self._request("POST", "/catalog/ingest", params={"remote": name})
        return self._safe_json(resp)

    # Internals
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        expected: Iterable[int] = (200, 201, 202, 204, 304),
    ) -> httpx.Response:
        """
        Single-request wrapper with consistent error handling.
        """
        url = f"{self.base_url}{path}"
        hdrs = dict(self._headers)
        if headers:
            hdrs.update(headers)

        try:
            with httpx.Client(timeout=self.timeout, headers=hdrs) as client:
                resp = client.request(method, url, params=params, json=json_body)
        except httpx.RequestError as e:
            # surfacing transport errors (DNS, timeouts, TLS, etc.)
            raise e

        if resp.status_code not in expected:
            # Try decoding body for better diagnostics
            body: Any
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = resp.text
            raise MatrixAPIError(
                f"{method} {path} failed ({resp.status_code})",
                status_code=resp.status_code,
                body=body,
            )
        return resp

    def _safe_json(self, resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text, "status_code": resp.status_code}

    def _parse(self, model_cls: Union[Type[T], Any], data: Any) -> Union[T, Any]:
        """
        Attempt to parse with Pydantic model if available; otherwise return raw dict.
        """
        if _HAS_TYPES and hasattr(model_cls, "model_validate"):
            try:
                return model_cls.model_validate(data)  # type: ignore [union-attr]
            except Exception:
                # Fall back to raw if validation fails
                return data
        return data
