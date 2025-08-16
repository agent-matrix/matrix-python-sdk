# Usage

This guide covers common tasks using `MatrixClient` and (optionally) the bulk registrar for Gateways.

---

## Top-5 Search

```python
from matrix_sdk import MatrixClient

hub = MatrixClient(base_url="http://127.0.0.1:7300")

res = hub.search(
    q="summarize pdfs",
    type="any",            # search across all entity types
    limit=5,               # Top-5 by default
    with_snippets=True     # include short summary snippets when available
)

for item in res.get("items", []):
    print(
        item.get("id"),
        item.get("name"),
        item.get("manifest_url"),  # absolute link to the manifest
        item.get("install_url"),   # convenience link to /catalog/install
    )
```

**Notes**

* Omit `type` or use `type="any"` to search across agents, tools, and MCP servers.
* `include_pending=True` shows items not yet registered with Gateway (useful in dev).

---

## Show Entity Details

```python
from matrix_sdk import MatrixClient

hub = MatrixClient("http://127.0.0.1:7300")
detail = hub.entity("tool:hello@0.1.0")
print(detail.get("name"), "-", detail.get("summary"))
```

---

## Install an Item

```python
from matrix_sdk import MatrixClient

hub = MatrixClient("http://127.0.0.1:7300")
hub.install(
    id="tool:hello@0.1.0",
    target="./.matrix/runners/demo",
    # alias="hello",            # optional; hub may ignore (CLI uses locally)
    # options={"force": True},  # optional pass-through
)
```

The Hub returns an install plan + results (artifacts, adapters written, lockfile path). Some hubs may return a simple `{ "ok": true }`.

---

## Manage Catalog Remotes

```python
from matrix_sdk import MatrixClient

hub = MatrixClient("http://127.0.0.1:7300")

print("Remotes:", hub.list_remotes())
hub.add_remote("https://example.org/catalog/index.json", name="example")
hub.trigger_ingest("example")
```

> Route names can be overridden via `routes={...}` in the client if your Hub uses different paths.

---

## Manifest helpers (optional)

```python
from matrix_sdk import MatrixClient

hub = MatrixClient("http://127.0.0.1:7300")
print("Manifest URL:", hub.manifest_url("tool:hello@0.1.0"))
doc = hub.fetch_manifest("tool:hello@0.1.0")  # requires json or pyyaml
```

---

## Bulk Registration (Gateway, optional)

If you also manage an MCP Gateway, you can discover servers in a ZIP/dir/Git repo and register them via the Admin API.

### Python (single source)

```python
import os, asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# Choose ONE source; priority is ZIP > DIR > GIT
if os.getenv("ZIP_PATH"):
    sources = [{"kind": "zip", "path": os.getenv("ZIP_PATH"), "probe": True}]
elif os.getenv("DIR_PATH"):
    sources = [{"kind": "dir", "path": os.getenv("DIR_PATH"), "probe": True}]
else:
    sources = [{
        "kind": "git",
        "url": os.getenv("GIT_URL", "https://github.com/ruslanmv/hello-mcp"),
        "ref": os.getenv("GIT_REF", "main"),
        "probe": True,  # try ${endpoint}/capabilities and merge
    }]

registrar = BulkRegistrar(
    gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:4444"),
    token=os.getenv("ADMIN_TOKEN"),
    concurrency=10,  # adjust as needed
    probe=True,
)

results = asyncio.run(registrar.register_servers(sources))
print(results)  # e.g., [{"message":"Server created successfully!","success":true}]
```

### CLI-style (module)

```bash
# Git source
python -m matrix_sdk.bulk.cli \
  --git https://github.com/ruslanmv/hello-mcp --ref main \
  --gateway-url http://127.0.0.1:4444 \
  --token "$ADMIN_TOKEN"

# ZIP source (preferred to avoid git/network)
python -m matrix_sdk.bulk.cli \
  --zip ./hello-mcp-main.zip \
  --gateway-url http://127.0.0.1:4444 \
  --token "$ADMIN_TOKEN"

# Directory source (already cloned)
python -m matrix_sdk.bulk.cli \
  --dir ./repo \
  --gateway-url http://127.0.0.1:4444 \
  --token "$ADMIN_TOKEN"
```

**How it works**

* **Discovery (matrix-first):** looks for `matrix/index.json` or `matrix/*.manifest.json`; else falls back to `[tool.mcp_server]` in `pyproject.toml`.
* **Gateway compatibility:** posts **JSON** first; if the gateway expects form data on `/admin/servers`, it **auto-falls back** to URL-encoded form and **sanitizes `name`** to meet gateway rules.
* **Resilience:** stable idempotency key, concurrency, and exponential backoff with jitter.

---

## Error handling

```python
from matrix_sdk import MatrixClient, MatrixError

hub = MatrixClient("http://127.0.0.1:7300")

try:
    hub.install(id="tool:does-not-exist@0.0.0", target="./dest")
except MatrixError as e:
    print("Install failed:", e.status, e.detail)
```


