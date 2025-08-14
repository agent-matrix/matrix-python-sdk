```markdown
# API Reference

This page summarizes the primary client and data models.

---

## `MatrixClient`

Synchronous client for Matrix Hub.

**Location**: `matrix_sdk.client.MatrixClient`

```python
class MatrixClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float | None = 15.0,
        cache: "Cache" | None = None,
        routes: dict[str, str] | None = None,
        session: "httpx.Client" | None = None,
    ) -> None: ...

    # Catalog
    def search(
        self,
        q: str,
        *,
        type: str | None = None,                 # "agent" | "tool" | "mcp_server" | "any"
        capabilities: str | None = None,
        frameworks: str | None = None,
        providers: str | None = None,
        mode: str | None = None,                 # "keyword" | "semantic" | "hybrid"
        limit: int | None = None,                # default 5
        include_pending: bool | str | None = None,
        with_snippets: bool | str | None = None,
    ) -> dict: ...

    def entity(self, id: str) -> dict: ...

    def install(
        self,
        id: str,
        *,
        target: str,
        alias: str | None = None,
        options: dict | None = None,
        manifest: dict | None = None,     # optional inline path
        source_url: str | None = None,    # provenance for inline installs
    ) -> dict: ...

    # Remotes
    def list_remotes(self) -> list | dict: ...
    def add_remote(self, url: str, *, name: str | None = None) -> dict: ...
    def delete_remote(self, url: str) -> dict: ...
    def trigger_ingest(self, name: str) -> dict: ...

    # Optional manifest helpers
    def manifest_url(self, id: str) -> str | None: ...
    def fetch_manifest(self, id: str) -> dict: ...
```

### Error type

```python
class SDKError(Exception):
    def __init__(self, status: int, detail: str | None = None): ...
```

* Non-2xx HTTP → `SDKError(status, detail)`
* Timeouts / network issues → `SDKError(status=0, detail=...)`

---

## Data Models

The SDK ships with Pydantic models for ergonomic typing. Fields are **additive/optional** to stay compatible with newer Hubs.

**Location**: `matrix_sdk.schemas`

* `SearchItem`

  * Common: `id`, `type`, `name`, `version`, `summary`
  * Scores: `score_lexical`, `score_semantic`, `score_quality`, `score_recency`, `score_final`
  * Compatibility: `capabilities`, `frameworks`, `providers`
  * Extras: `fit_reason`
  * **New optional**: `manifest_url`, `install_url`, `snippet`
* `SearchResponse` → `{ items: [SearchItem], total: int }`
* `EntityDetail` → permissive (extra fields allowed)
* `InstallStepResult`, `InstallOutcome`
* `MatrixAPIError` (legacy error class kept for compatibility)

---

## Cache

Small ETag-aware cache.

**Location**: `matrix_sdk.cache.Cache`

```python
class Cache:
    def make_key(self, path: str, params: dict) -> str: ...
    def save(self, key: str, *, etag: str | None, body: dict) -> None: ...
    def get_etag(self, key: str) -> str | None: ...
    def get_body(self, key: str) -> dict | None: ...
```

---

## Bulk (Gateway)

Async helpers to **discover** MCP server manifests and **register** them into an MCP Gateway Admin API.

### `BulkRegistrar`

**Location**: `matrix_sdk.bulk.bulk_registrar.BulkRegistrar`

```python
class BulkRegistrar:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        concurrency: int = 50,
        probe: bool = True,
        backoff_config: dict | None = None,  # {"max_retries":5, "base_delay":1.0, "jitter":0.1}
    ) -> None: ...

    async def register_servers(self, sources: "Iterable[dict]") -> list[Any]: ...
```

* **Discovery (matrix-first):** looks for `matrix/index.json` or `matrix/*.manifest.json`; falls back to `pyproject.toml` → `[tool.mcp_server]`.
* **Probe (optional):** `GET {endpoint}/capabilities` merged into `capabilities` (best-effort).
* **Resilience:** concurrency-limited tasks; exponential backoff with jitter; stable idempotency key (SHA-256 of payload).
* **Return:** flat list of results (each item is the gateway’s response dict, or \`{"error": "..."} on failure).

**Source descriptor schema**

```python
# one of:
{"kind": "zip", "path": "/abs/path/repo.zip", "probe": True}
{"kind": "dir", "path": "/abs/path/repo",     "probe": True}
{"kind": "git", "url": "https://...", "ref": "main", "probe": True}
```

### Discovery

**Location**: `matrix_sdk.bulk.discovery`

```python
def discover_manifests_from_source(source: dict) -> list["ServerManifest"]:
    """
    kind: "zip" | "dir" | "git"
    matrix-first; else pyproject.toml
    """
```

### Models (Gateway manifest)

**Location**: `matrix_sdk.bulk.models`

```python
class EndpointDescriptor(BaseModel):
    transport: Literal["http", "ws", "sse", "stdio"]
    url: AnyUrl
    auth: Literal["bearer", "none"] | None = "none"
    wire_schema: str = Field(..., alias="schema")  # serialized as "schema"

class ServerManifest(BaseModel):
    entity_type: Literal["mcp_server"] = Field("mcp_server", alias="type")
    id: str
    uid: str | None = None
    name: str
    version: str | None = None
    summary: str | None = ""
    description: str | None = None
    providers: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    endpoint: EndpointDescriptor
    labels: dict[str, str] = Field(default_factory=dict)
    quality_score: float | None = 0.0
    source_url: AnyUrl | None = None
    license: str | None = None

    def to_jsonable(self) -> dict: ...
```

* Pydantic **v1/v2 compatible**.
* `to_jsonable()` returns a plain-JSON-safe dict (e.g., `AnyUrl` becomes `str`).

### Gateway client

**Location**: `matrix_sdk.bulk.gateway.GatewayAdminClient`

```python
class GatewayAdminClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 20.0) -> None: ...

    async def upsert_server(
        self,
        manifest: "ServerManifest | dict",
        *,
        idempotency_key: str,
    ) -> dict: ...
```

* **POST** `/admin/servers` with `Idempotency-Key`.
* Tries **JSON** first; on recognizable errors (e.g., 422 “Missing/Invalid name”), **auto-falls back** to **form URL-encoded** with a **sanitized `name`** (letters/digits/underscore/dot/hyphen/space; 255 chars max; stable fallback if empty).
* Returns parsed JSON response (or `{"ok": True, "raw": "..."} ` if the gateway returns non-JSON on success).

### Utilities

**Location**: `matrix_sdk.bulk.utils`

```python
def make_idempotency_key(manifest: dict) -> str:
    """SHA-256 of the normalized JSON payload (sorted keys)."""

@contextmanager
def with_temp_extract(zip_path: str) -> "Iterator[str]":
    """Extract ZIP to a temp dir; yields the path; cleans up on exit."""
```

**Location**: `matrix_sdk.bulk.backoff`

```python
def with_backoff(max_retries: int = 5, base_delay: float = 1.0, jitter: float = 0.1):
    """Decorator for async functions that retries with exponential backoff + jitter."""
```

**Location**: `matrix_sdk.bulk.probe`

```python
def probe_capabilities(manifest: dict, timeout: float = 5.0) -> dict:
    """GET {endpoint}/capabilities for http/sse endpoints; merge list into manifest['capabilities'] (best-effort)."""
```

---

## Minimal bulk example

```python
import os, asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [{"kind":"git","url":"https://github.com/ruslanmv/hello-mcp","ref":"main","probe":True}]

registrar = BulkRegistrar(
    gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:4444"),
    token=os.getenv("ADMIN_TOKEN"),
    concurrency=10,
    probe=True,
)

results = asyncio.run(registrar.register_servers(sources))
print(results)  # e.g., [{"message":"Server created successfully!","success":true}]
```

```
```
