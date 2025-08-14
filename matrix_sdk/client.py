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

Additions (backwards-compatible):
- entity(...)                  → alias of get_entity(...) for CLI compatibility
- delete_remote(...)           → DELETE /catalog/remotes (POST fallback)
- manifest_url(...)            → resolve a manifest URL for an entity
- fetch_manifest(...)          → fetch and parse manifest (JSON or YAML)
- SDKError                     → subclass of MatrixAPIError; raised by this client
- search(...) enhancements     → accept positional `q`; treat type="any" as no filter;
                                normalize booleans for include_pending/with_snippets
- Cache compatibility          → supports both legacy cache (get/set) and simple cache
                                (make_key/get_etag/get_body/save) for ETag

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

# Optional cache (both legacy and simple supported)
try:  # pragma: no cover - imports depend on your package layout
    # Legacy style: Cache.get(key, allow_expired) -> entry{etag,payload}; Cache.set(key, payload, etag=?)
    # Plus a helper to form a stable key
    from .cache import Cache, make_cache_key  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    Cache = None  # type: ignore

    def make_cache_key(url: str, params: Dict[str, Any]) -> str:  # type: ignore
        # Minimal fallback; not persisted; stable enough for tests/local
        return url + "?" + urlencode(sorted((k, str(v)) for k, v in (params or {}).items()))

__all__ = [
    "MatrixClient",
    "SDKError",
]

T = TypeVar("T")


class SDKError_old(MatrixAPIError):
    """
    SDK-visible error that the CLI can catch. Subclasses MatrixAPIError
    for backward compatibility with existing client code that already catches it.
    """
    pass


# --- keep the existing imports ---

# Make SDKError predictable and compatible with MatrixAPIError
class SDKError(MatrixAPIError):
    """
    Structured SDK error.

    Attributes:
        status (int): HTTP status code (0 for network errors).
        detail (str|None): Short human-friendly explanation (if available).
        body (Any): Parsed error payload (dict/text) returned by the server.
    """
    def __init__(self, status: int, detail: Optional[str] = None, *, body: Any = None) -> None:
        # Public attrs used by examples/CLI
        self.status = status
        self.detail = detail
        self.body = body
        # Also initialize MatrixAPIError with keywords for compatibility
        super().__init__(
            detail or f"HTTP {status}",
            status_code=status,
            body=body,
            detail=detail,
        )

# … unchanged code …

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
        url = f"{self.base_url}{path}"
        hdrs = dict(self._headers)
        if headers:
            hdrs.update(headers)

        try:
            with httpx.Client(timeout=self.timeout, headers=hdrs) as client:
                resp = client.request(method, url, params=params, json=json_body)
        except httpx.RequestError as e:
            # Transport error: status 0, keep message as detail
            raise SDKError(0, str(e)) from e

        if resp.status_code not in expected:
            # Try decoding body and extract a concise detail string if possible
            body: Any
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = resp.text

            detail: Optional[str] = None
            if isinstance(body, dict):
                # Common FastAPI error shape: {"detail": "..."}
                detail = body.get("detail")
                # Your Hub's install error shape: {"error":{"error":"InstallError","reason":"..."}}
                if detail is None:
                    err = body.get("error")
                    if isinstance(err, dict):
                        detail = err.get("reason") or err.get("error")
                    elif isinstance(err, str):
                        detail = err
            # Fallback generic message
            if not detail:
                detail = f"{method} {path} failed ({resp.status_code})"

            raise SDKError(resp.status_code, detail, body=body)

        return resp




def _to_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


class MatrixClient:
    """
    Thin sync client around httpx for Matrix Hub.

    Example:
        from matrix_sdk.client import MatrixClient
        c = MatrixClient("http://localhost:7300", token="...")
        res = c.search(q="summarize pdfs", type="agent", capabilities="pdf,summarize")
    """

    # ---------------------------- construction ---------------------------- #

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

        # Detect cache API flavor (legacy vs. simple)
        # legacy: get/set/entry.etag/entry.payload (+make_cache_key function)
        # simple: make_key/get_etag/get_body/save
        self._cache_mode: Optional[str] = None
        if self.cache is not None:
            if hasattr(self.cache, "get") and hasattr(self.cache, "set"):
                self._cache_mode = "legacy"
            elif all(hasattr(self.cache, n) for n in ("make_key", "get_etag", "get_body", "save")):
                self._cache_mode = "simple"

    # ------------------------------- public API --------------------------- #

    def search(
        self,
        q: str,
        *,
        type: Optional[str] = None,
        **filters: Any,
    ) -> Union[SearchResponse, Dict[str, Any]]:
        """
        Perform a catalog search.

        Parameters:
            q: free-text query (required)
            type: "agent" | "tool" | "mcp_server" | "any" (optional; "any" → omit type)
            **filters: capabilities, frameworks, providers, limit, offset, mode,
                       with_rag, rerank, include_pending, with_snippets...
        """
        if not q:
            raise ValueError("q (query) is required")

        # Base params
        params: Dict[str, Any] = {"q": q}

        # Treat type="any" (or empty) as no filter to search across all entity types
        if type and type not in ("", "any"):
            params["type"] = type

        # Normalize booleans for common flags if they arrive as strings
        normalized: Dict[str, Any] = {}
        for k, v in filters.items():
            if v is None:
                continue
            if k in ("include_pending", "with_snippets"):
                b = _to_bool(v)
                normalized[k] = b if b is not None else v
            else:
                normalized[k] = v
        params.update(normalized)

        path = "/catalog/search"
        url = f"{self.base_url}{path}"

        # Optional cache: add If-None-Match and handle 304
        headers = dict(self._headers)

        # Compute key depending on available cache style
        cache_key: Optional[str] = None
        legacy_entry = None
        if self.cache and self._cache_mode == "legacy":
            cache_key = make_cache_key(url, params)  # type: ignore[arg-type]
            legacy_entry = self.cache.get(cache_key, allow_expired=True)  # type: ignore[attr-defined]
            if legacy_entry and getattr(legacy_entry, "etag", None):
                headers["If-None-Match"] = legacy_entry.etag
        elif self.cache and self._cache_mode == "simple":
            # new simple style: use cache.make_key and cache.get_etag
            try:
                cache_key = self.cache.make_key(path, params)  # type: ignore[attr-defined]
                et = self.cache.get_etag(cache_key)            # type: ignore[attr-defined]
                if et:
                    headers["If-None-Match"] = et
            except Exception:
                cache_key = None  # ignore cache if anything goes wrong

        try:
            resp = self._request("GET", path, params=params, headers=headers)
            # Serve from cache if server says Not Modified
            if resp.status_code == 304 and self.cache and cache_key:
                if self._cache_mode == "legacy" and legacy_entry is not None:
                    return self._parse(SearchResponse, legacy_entry.payload)
                if self._cache_mode == "simple":
                    try:
                        body = self.cache.get_body(cache_key)  # type: ignore[attr-defined]
                        if body is not None:
                            return self._parse(SearchResponse, body)
                    except Exception:
                        pass

            data = self._safe_json(resp)

            # Save new body & ETag
            if self.cache and cache_key:
                if self._cache_mode == "legacy":
                    self.cache.set(cache_key, data, etag=resp.headers.get("ETag"))  # type: ignore[attr-defined]
                elif self._cache_mode == "simple":
                    try:
                        self.cache.save(cache_key, etag=resp.headers.get("ETag"), body=data)  # type: ignore[attr-defined]
                    except Exception:
                        pass

            return self._parse(SearchResponse, data)

        except httpx.RequestError as e:
            # Network issue; try to serve a fresh cached value if within TTL
            if self.cache:
                if self._cache_mode == "legacy" and cache_key:
                    fresh = self.cache.get(cache_key, allow_expired=False)  # type: ignore[attr-defined]
                    if fresh:
                        return self._parse(SearchResponse, fresh.payload)
                elif self._cache_mode == "simple" and cache_key:
                    try:
                        body = self.cache.get_body(cache_key)  # type: ignore[attr-defined]
                        if body is not None:
                            return self._parse(SearchResponse, body)
                    except Exception:
                        pass
            raise SDKError(0, str(e)) from e

    def get_entity(self, id: str) -> Union[EntityDetail, Dict[str, Any]]:
        """
        Fetch full entity detail by its id (uid), e.g., "agent:pdf-summarizer@1.4.2".
        """
        if not id:
            raise ValueError("id is required")
        # keep : and @ intact for path param
        enc = quote(id, safe=":@")
        resp = self._request("GET", f"/catalog/entities/{enc}")
        return self._parse(EntityDetail, self._safe_json(resp))

    # CLI compatibility alias
    def entity(self, id: str) -> Union[EntityDetail, Dict[str, Any]]:
        """Alias for get_entity(id)."""
        return self.get_entity(id)

    def install(
        self,
        id: str,
        target: str,
        version: Optional[str] = None,
        *,
        alias: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        manifest: Optional[Dict[str, Any]] = None,
        source_url: Optional[str] = None,
    ) -> Union[InstallOutcome, Dict[str, Any]]:
        """
        Execute install plan for an entity.

        Notes:
        - `version` is preserved for backward compatibility.
        - Additional fields (`alias`, `options`, `manifest`, `source_url`) are included for
          forward-compatibility with newer Hub features; the server may ignore unknown fields.
        """
        if not id:
            raise ValueError("id is required")
        if not target:
            raise ValueError("target is required")

        body: Dict[str, Any] = {"id": id, "target": target}
        if version:
            body["version"] = version
        if alias is not None:
            body["alias"] = alias
        if options:
            body["options"] = options
        if manifest is not None:
            body["manifest"] = manifest
            if source_url:
                body["source_url"] = source_url

        resp = self._request("POST", "/catalog/install", json_body=body)
        return self._parse(InstallOutcome, self._safe_json(resp))

    # ----------------------- remotes management ----------------------- #

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

    def delete_remote(self, url: str) -> Dict[str, Any]:
        """
        Remove a configured remote. Tries DELETE first, falls back to POST shim.
        """
        if not url:
            raise ValueError("url is required")
        # Attempt DELETE with JSON body (some servers support this)
        try:
            resp = self._request(
                "DELETE",
                "/catalog/remotes",
                json_body={"url": url},
                expected=(200, 202, 204),  # some servers return 204 No Content
            )
            # Normalize response body
            try:
                return self._safe_json(resp)
            except Exception:
                return {"ok": True}
        except SDKError as e:
            if e.args and "failed" in e.args[0].lower():
                # Fall through to POST shim below
                pass
            else:
                # Non-protocol error, re-raise
                raise

        # Fallback to POST shim
        resp = self._request("POST", "/catalog/remotes", json_body={"url": url, "op": "delete"})
        return self._safe_json(resp)

    def trigger_ingest(self, name: str) -> Dict[str, Any]:
        """
        Manually trigger ingest for a named remote.
        """
        if not name:
            raise ValueError("name is required")
        resp = self._request("POST", "/catalog/ingest", params={"remote": name})
        return self._safe_json(resp)

    # ----------------------- manifest helpers (optional) ----------------------- #

    def manifest_url(self, id: str) -> Optional[str]:
        """
        Resolve a manifest URL for a given entity, preferring the entity's source_url.
        Falls back to a resolver path if exposed by the Hub.
        """
        try:
            ent = self.entity(id)
            url = ent.get("source_url") or ent.get("manifest_url")
            if url:
                return url
        except Exception:
            pass
        # Fallback conventional route if server exposes it
        enc = quote(id, safe=":@")
        return f"{self.base_url}/catalog/manifest/{enc}"

    def fetch_manifest(self, id: str) -> Dict[str, Any]:
        """
        Fetch and parse a manifest (JSON preferred; YAML supported if PyYAML installed).
        """
        url = self.manifest_url(id)
        if not url:
            raise SDKError(404, "Manifest URL not found")
        try:
            with httpx.Client(timeout=self.timeout, headers={"Accept": "application/json"}) as client:
                resp = client.get(url)
        except httpx.RequestError as e:
            raise SDKError(0, str(e)) from e

        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" in ctype or ctype.endswith("+json"):
            return self._safe_json(resp)

        # Best-effort YAML
        try:
            import yaml  # optional dependency
            return yaml.safe_load(resp.text)  # type: ignore[no-any-return]
        except Exception:
            raise SDKError(415, "Unsupported manifest content type")

    # ------------------------------ internals ------------------------------ #

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
            raise SDKError(0, str(e)) from e

        if resp.status_code not in expected:
            # Try decoding body for better diagnostics
            body: Any
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = resp.text
            raise SDKError(
                resp.status_code,
                f"{method} {path} failed ({resp.status_code}) — {body!r}",
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
