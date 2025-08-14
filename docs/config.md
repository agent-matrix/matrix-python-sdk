# Configuration

You configure the SDK by **passing options at construction time**. We recommend keeping secrets/URLs in environment variables and reading them in your app.

---

## Environment variables (recommended)

Create a `.env` or export variables in your shell:

```env
# Matrix Hub (catalog + install)
MATRIX_HUB_URL="http://127.0.0.1:7300"
MATRIX_TOKEN="YOUR_API_TOKEN"        # optional

# MCP Gateway (bulk registration; optional)
MATRIX_GATEWAY_URL="http://127.0.0.1:4444"
MATRIX_ADMIN_TOKEN="YOUR_ADMIN_TOKEN"
```

Then load them in Python (e.g., with `os.getenv` or `python-dotenv`) and pass to the client.

---

## MatrixClient (Hub)

Used for search, detail, install, and (optionally) remote/ingest admin.

```python
import os
from matrix_sdk import MatrixClient

hub = MatrixClient(
    base_url=os.getenv("MATRIX_HUB_URL", "http://127.0.0.1:7300"),
    token=os.getenv("MATRIX_TOKEN"),  # optional
    timeout=15.0,                     # seconds (default 15.0)
)
```

### Common usage

```python
# Top-5 search across all types
res = hub.search(q="extract pdf tables", type="any", limit=5, with_snippets=True)

# Show entity
detail = hub.entity("tool:hello@0.1.0")

# Install into a local folder
hub.install(id="tool:hello@0.1.0", target="./.matrix/runners/demo")
```

---

## Bulk Registrar (Gateway) — optional

If you also manage a Gateway, the SDK can **bulk-register** MCP servers you host. This is independent of the Hub client.

### Required env

```env
# MCP Gateway Admin API
GATEWAY_URL="http://127.0.0.1:4444"          # alias: MATRIX_GATEWAY_URL
ADMIN_TOKEN="YOUR_ADMIN_TOKEN"               # aliases: MATRIX_ADMIN_TOKEN / GATEWAY_TOKEN / GATEWAY_ADMIN_TOKEN
```

### Optional env (source + behavior)

```env
# Choose ONE source; priority: ZIP > DIR > GIT
ZIP_PATH="/abs/path/repo.zip"                # preferred (no git/network)
DIR_PATH="/abs/path/checked-out/repo"        # already cloned
GIT_URL="https://github.com/IBM/docling-mcp.git"
GIT_REF="main"

# Behavior
CONCURRENCY="10"                             # default 10
PROBE="true"                                 # try `${endpoint}/capabilities` and merge; default true
ENV_FILE=".env.local"                        # if you load env yourself before running
```

### Minimal Python setup

```python
import os, asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

def _truthy(v: str | None, default: bool = True) -> bool:
    if v is None: return default
    return v.strip().lower() in {"1","true","yes","on"}

# Build a single source descriptor
if os.getenv("ZIP_PATH"):
    sources = [{"kind":"zip", "path": os.getenv("ZIP_PATH"), "probe": _truthy(os.getenv("PROBE"), True)}]
elif os.getenv("DIR_PATH"):
    sources = [{"kind":"dir", "path": os.getenv("DIR_PATH"), "probe": _truthy(os.getenv("PROBE"), True)}]
else:
    sources = [{
        "kind":"git",
        "url": os.getenv("GIT_URL", "https://github.com/IBM/docling-mcp.git"),
        "ref": os.getenv("GIT_REF", "main"),
        "probe": _truthy(os.getenv("PROBE"), True),
    }]

registrar = BulkRegistrar(
    gateway_url = os.getenv("GATEWAY_URL") or os.getenv("MATRIX_GATEWAY_URL", "http://127.0.0.1:4444"),
    token       = os.getenv("ADMIN_TOKEN") or os.getenv("MATRIX_ADMIN_TOKEN")
                 or os.getenv("GATEWAY_ADMIN_TOKEN") or os.getenv("GATEWAY_TOKEN"),
    concurrency = int(os.getenv("CONCURRENCY", "10")),
    probe       = sources[0].get("probe", True),
)

results = asyncio.run(registrar.register_servers(sources))
print(results)
```

**Notes**

* **Discovery (matrix-first):** the registrar looks for `matrix/index.json` or `matrix/*.manifest.json`. If none are present, it falls back to `[tool.mcp_server]` in `pyproject.toml`.
* **Gateway compatibility:** the client POSTs **JSON** first; if your gateway expects form data on `/admin/servers`, it **auto-falls back** to URL-encoded form and **sanitizes `name`** to match gateway rules.
* **Idempotency & resilience:** a stable idempotency key (SHA-256 of the manifest) prevents duplicates; requests use concurrency and exponential backoff with jitter.

---

## Advanced configuration

### Timeouts and sessions

`MatrixClient` accepts a `timeout` (seconds) and an optional pre-configured `httpx.Client` session:

```python
import httpx
from matrix_sdk import MatrixClient

session = httpx.Client(
    timeout=20.0,
    headers={"Accept": "application/json"},
    proxies=None,  # or {"http": "...", "https": "..."}
)

hub = MatrixClient(base_url="http://127.0.0.1:7300", session=session)
```

> If you pass `token=...`, the client sets `Authorization: Bearer <token>` automatically.

### Route overrides (if your Hub paths differ)

```python
hub = MatrixClient(
    base_url="https://hub.example.com",
    routes={
        "remotes_list": "/catalog/remotes",
        "remotes_add": "/catalog/remotes",
        "remotes_delete": "/catalog/remotes",
        "ingest_trigger": "/admin/ingest/{name}",
    },
)
```

### Caching (ETag-aware)

The SDK ships a tiny in-memory cache that honors ETags for `/catalog/search`. It avoids re-downloading unchanged results and can speed up repeated queries.

```python
from matrix_sdk.cache import Cache
from matrix_sdk import MatrixClient

cache = Cache(ttl_seconds=60)  # TTL value is advisory; ETag drives freshness
hub = MatrixClient(base_url="http://127.0.0.1:7300", cache=cache)

res = hub.search(q="hello", type="any", limit=5)
```

**Notes**

* The cache stores response bodies and ETags keyed by request parameters.
* If the server returns **304 Not Modified**, the SDK serves the cached body.
* You can swap in your own cache implementation as long as it provides:

  * `make_key(path: str, params: dict) -> str`
  * `get_etag(key: str) -> str | None`
  * `get_body(key: str) -> dict | None`
  * `save(key: str, *, etag: str | None, body: dict) -> None`

### Proxies and TLS

`httpx` honors `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`. Example:

```bash
export HTTPS_PROXY="http://proxy.local:8080"
```

Or pass `proxies` to your `httpx.Client`.

---

## Error handling

The client raises `SDKError` on non-2xx responses or network errors:

```python
from matrix_sdk import MatrixClient, SDKError

hub = MatrixClient("http://127.0.0.1:7300")

try:
    hub.install(id="tool:missing@0.0.0", target="./dest")
except SDKError as e:
    print("Failed:", e.status, e.detail)
```

* HTTP errors → `SDKError(status=<http code>, detail=<server message>)`
* Timeouts / connection issues → `SDKError(status=0, detail="timeout" or similar)`

---

## Server-side tip (Hub admins)

To produce absolute links (e.g., `manifest_url`, `install_url`) behind a proxy/CDN, set the Hub’s environment variable:

```env
PUBLIC_BASE_URL=https://api.your-hub.example
```

This is **not** an SDK setting, but it affects the links you receive from the Hub.


