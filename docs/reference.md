# API Reference (v0.12)

This page summarizes the primary client, helper modules, and data models included in the SDK.

---

## Summary

This SDK provides four pillars:

1. **MatrixClient** — search the Hub, fetch entity details, request installation.
2. **LocalInstaller** — materialize a plan locally (files, artifacts, env, `runner.json`).
3. **runtime** — start/stop/list/tail/doctor local MCP servers (no daemon).
4. **search helpers** — thin, defensive wrappers around `/catalog/search` with retries and mode fallbacks.

> Deprecated APIs have been removed in this version. (Bulk remains supported.)

---

## MatrixClient

**Location**: `matrix_sdk.client.MatrixClient`
**Errors**: raises `matrix_sdk.client.MatrixError(status, detail)` on non-2xx.

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
        type: str | None = None,                 # "agent" | "tool" | "mcp_server" | "any"/None
        capabilities: str | None = None,         # CSV
        frameworks: str | None = None,           # CSV
        providers: str | None = None,            # CSV
        mode: str | None = None,                 # "keyword" | "semantic" | "hybrid"
        limit: int | None = None,                # default 5 (1..100)
        include_pending: bool | str | None = None,
        with_snippets: bool | str | None = None,
        with_rag: bool | str | None = None,      # include short fit_reason if supported
        rerank: str | None = "none",             # "none" | "llm"
    ) -> dict: ...

    def entity(self, id: str) -> dict: ...

    def install(
        self,
        id: str,
        *,
        target: str,
        alias: str | None = None,
        options: dict | None = None,
        manifest: dict | None = None,     # optional inline manifest
        source_url: str | None = None,    # provenance for inline installs
    ) -> dict: ...

    # Remotes
    def list_remotes(self) -> list | dict: ...
    def add_remote(self, url: str, *, name: str | None = None) -> dict: ...
    def delete_remote(self, url: str) -> dict: ...
    def trigger_ingest(self, name: str) -> dict: ...
```

**Notes**

* `entity(id)` URL-encodes the identifier (e.g., `tool:hello@0.1.0`) internally.
* `install(id, target)` calls Hub `/catalog/install` and returns the Hub payload (plan/results/etc).
* Treat `type="any"` or `None` as *no type filter*.
* Optional `cache` enables ETag-aware caching (see **Cache** below).

### Error type

```python
class MatrixError(Exception):
    def __init__(self, status: int, detail: str | None = None): ...
```

* Non-2xx HTTP → `MatrixError(status, detail)`
* Timeouts / network issues → `MatrixError(status=0, detail=...)`

---

## Search helpers

**Location**: `matrix_sdk.search`
**Purpose**: Thin, defensive wrappers around `GET /catalog/search` with normalization, tiny retries, and optional mode fallbacks.

```python
from matrix_sdk.search import search, search_try_modes, SearchOptions
```

### `search(...)`

```python
def search(
    client: "MatrixClient",
    q: str,
    *,
    type: str | None = None,                           # "agent" | "tool" | "mcp_server" | "any"/None
    capabilities: "Iterable[str] | str | None" = None, # list/tuple/set or CSV -> CSV
    frameworks: "Iterable[str] | str | None" = None,   # list/tuple/set or CSV -> CSV
    providers: "Iterable[str] | str | None" = None,    # list/tuple/set or CSV -> CSV
    mode: str | None = "hybrid",                       # "keyword" | "semantic" | "hybrid"
    limit: int = 5,                                    # clamped to 1..100
    with_rag: bool = False,                            # add fit_reason if supported
    with_snippets: bool = False,                       # add snippet if supported
    rerank: str | None = "none",                       # "none" | "llm"
    include_pending: bool = False,
    options: "SearchOptions | None" = None,            # retries, fallbacks, typed return
) -> dict | "SearchResponse": ...
```

**Behavior**

* Normalizes filters (iterables → CSV), clamps `limit`.
* Retries transient 5xx/network errors with exponential backoff.
* Optional fallback chain across modes (e.g., semantic → hybrid → keyword) when no results.
* Returns a `dict` by default, or a typed `SearchResponse` if `options.as_model=True`.

### `SearchOptions`

```python
from dataclasses import dataclass
from typing import Tuple

@dataclass(frozen=True)
class SearchOptions:
    allow_fallback: bool = True                      # try more modes when no results
    fallback_order: "Tuple[str, ...] | None" = None  # override mode order
    max_attempts: int = 2                            # transient retry attempts
    backoff_base: float = 0.1                        # seconds; jittered exponential
    as_model: bool = False                           # return pydantic SearchResponse
```

### `search_try_modes(...)`

```python
def search_try_modes(
    client: "MatrixClient",
    q: str,
    modes: "Sequence[str]" = ("hybrid", "keyword", "semantic"),
    **kwargs,
):
    """Yield (mode, response) for each mode (no fallbacks)."""
    ...
```

---

## LocalInstaller

**Location**: `matrix_sdk.installer.LocalInstaller`
**Data classes**: `BuildReport`, `EnvReport`, `BuildResult`
**Other errors**: `matrix_sdk.manifest.ManifestResolutionError`, `ArchiveFetchError`, `GitFetchError`.

```python
from matrix_sdk.installer import LocalInstaller

installer = LocalInstaller(client)

result = installer.build(
    "mcp_server:hello-sse-server@0.1.0",
    alias="hello-sse",
    # target defaults via policy if omitted
)

print(result.target)         # install directory
print(result.env.python_prepared, result.env.node_prepared)
print(result.build.files_written, result.build.artifacts_fetched)
print(result.runner)         # contents of runner.json (dict)
```

### Lifecycle

1. **plan(id, target)** → ask Hub for an install plan.
2. **materialize(outcome, target)** → write files, fetch artifacts, ensure/validate `runner.json`.
3. **prepare\_env(target, runner)** → create `.venv` (Python) and/or run `npm|yarn|pnpm install` (Node).
4. **build(id, …)** → runs all three and returns `BuildResult`.

**Runner inference** (when missing):

* If `server.py` → `{"type":"python","entry":"server.py","python":{"venv":".venv"}}`
* If `server.js`/`package.json` → Node with sensible default entry.

---

## runtime

**Location**: `matrix_sdk.runtime`
**State/Logs**: `~/.matrix/state/<alias>/runner.lock.json`, `~/.matrix/logs/<alias>.log`
**Model**: `LockInfo(pid:int, port:int|None, alias:str, target:str, started_at:float, runner_path:str)`

```python
from matrix_sdk import runtime

lock = runtime.start("/path/to/install", alias="hello-sse")   # reads runner.json
# uses venv python for python runners; finds a free port if needed

runtime.status()              # -> list[LockInfo]
runtime.doctor("hello-sse")   # -> {"status": "ok"|"fail", "reason": "..."}
runtime.stop("hello-sse")     # -> bool
for line in runtime.tail_logs("hello-sse", n=40): print(line, end="")
runtime.log_path("hello-sse") # -> str
```

**Requirements**

* `runner.json` must define at least: `{"type": "python"|"node", "entry": "…"}`
* Python runners: venv Python must exist (created during `prepare_env`).

---

## Deep links

**Location**: `matrix_sdk.deep_link`
**Errors**: `InvalidMatrixUri` for bad/unsupported URIs.

```python
from matrix_sdk.deep_link import parse, handle_install, InvalidMatrixUri

dl = parse("matrix://install?id=tool%3Ahello%400.1.0&alias=hello")
# -> DeepLink(action='install', id='tool:hello@0.1.0', alias='hello')

res = handle_install(url, client, target="/abs/install/dir")
# -> HandleResult(id=..., target=..., response=<hub install payload>)
```

Rules:

* Only `matrix://install` is supported.
* `id` is **required**; `alias` matches `^[a-z0-9][a-z0-9._-]{0,63}$`.

---

## Artifact fetchers

### HTTP archives

**Location**: `matrix_sdk.archivefetch`

```python
from matrix_sdk.archivefetch import fetch_http_artifact, ArchiveFetchError

fetch_http_artifact(
    url: str,
    target: pathlib.Path | str,
    dest: str | None = None,        # optional: also save raw file at target/dest
    sha256: str | None = None,      # optional integrity
    unpack: bool = False,           # auto-detects .zip/.tar(.gz) too
    timeout: int | float = 60,
    logger: logging.Logger | None = None,
)
```

* Safe ZIP/TAR extraction (no path traversal).
* Optional flatten of GitHub-style single-folder archives.

### Git repos

**Location**: `matrix_sdk.gitfetch`

```python
from matrix_sdk.gitfetch import fetch_git_artifact, GitFetchError

fetch_git_artifact(
    spec: Mapping[str, object],     # {repo, ref, depth?, subdir?, strip_vcs?, recurse_submodules?, lfs?, verify_sha?}
    target: pathlib.Path,
    git_bin: str = "git",
    allow_hosts: Iterable[str] | None = None,  # defaults via env/standard hosts
    timeout: int = 180,
    logger: logging.Logger | None = None,
)
```

* HTTPS by default; host allow-list enforced (deny-by-default if none given).
* Shallow clone, optional sparse subdir, optional LFS, optional commit verification.

---

## Data Models (Pydantic)

**Location**: `matrix_sdk.schemas`
Models mirror Hub responses; unknown fields are allowed.

* `SearchItem`

  * Common: `id`, `type`, `name`, `version`, `summary`
  * Scores: `score_lexical`, `score_semantic`, `score_quality`, `score_recency`, `score_final`
  * Compatibility: `capabilities`, `frameworks`, `providers`
  * Extras: `fit_reason`
  * Optional links/snippets: `manifest_url`, `install_url`, `snippet`
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

Async helpers to **discover** MCP server manifests and **register** them in an MCP Gateway Admin API.

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
* **Return:** flat list of results (each item is the gateway’s response dict, or `{"error": "..."} ` on failure).

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

* Pydantic v1/v2 compatible.
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
def with_backoff(max_retries: int = 5, base_delay: float = 1.0, jitter: 0.1 = 0.1):
    """Decorator for async functions that retries with exponential backoff + jitter."""
```

**Location**: `matrix_sdk.bulk.probe`

```python
def probe_capabilities(manifest: dict, timeout: float = 5.0) -> dict:
    """GET {endpoint}/capabilities for http/sse endpoints; merge list into manifest['capabilities'] (best-effort)."""
```

---

## Environment variables

* `MATRIX_SDK_DEBUG=1` — verbose logs for installer/runtime/archivefetch/search.
* `MATRIX_HOME` — base dir for `~/.matrix` (state/logs).
* `MATRIX_GIT_ALLOWED_HOSTS` — CSV allow-list for git fetch (defaults to common hosts if not provided via API).
* `MATRIX_GIT_ALLOW_INSECURE=1` — allow `http://` git (discouraged).
* `MATRIX_SDK_DEBUG_GIT=1` — extra git logs.

---

## Minimal examples

### Client + search

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient("https://api.matrixhub.io")
res = hub.search("hello", type="mcp_server", mode="keyword", limit=5, include_pending=True)
print([it["id"] for it in res.get("items", [])])
```

### Search helper (hybrid with fallbacks)

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.search import search, SearchOptions

hub = MatrixClient("https://api.matrixhub.io")
res = search(hub, "summarize pdfs", type="any", options=SearchOptions(as_model=False))
print(res["total"])
```

### Install + run

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller
from matrix_sdk import runtime

hub = MatrixClient("https://api.matrixhub.io")
installer = LocalInstaller(hub)

result = installer.build("mcp_server:hello-sse-server@0.1.0", alias="my-server")
lock = runtime.start(result.target, alias="my-server")
print(lock.pid, lock.port)
```

---

**Deprecated**: none (prior deprecated helpers have been removed; Bulk remains supported).
