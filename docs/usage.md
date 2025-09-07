# Usage (v0.1.9)

Quick, copy-pasteable examples for searching the Hub, installing locally, and (optionally) running a server.

> Requires **Python 3.11+**.

---

## 1) Quick search

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient(base_url="https://api.matrixhub.io")

res = hub.search(
    q="summarize pdfs",
    type="any",          # omit or use "any" to search across types
    mode="hybrid",       # "keyword" | "semantic" | "hybrid"
    limit=5,
    with_snippets=True,  # short snippets when supported
    with_rag=False,      # add fit_reason when supported
    rerank="none",       # "none" | "llm"
)

for item in res.get("items", []):
    print(item.get("id"), item.get("name"))
```

**Tip:** prefer the high-level helper below for tiny retries and optional fallbacks.

---

## 2) Install & build locally

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller

hub = MatrixClient("https://api.matrixhub.io")
installer = LocalInstaller(hub)

result = installer.build(
    "mcp_server:hello-sse-server@0.1.0",
    alias="hello-sse",   # optional; influences default target label
    # target=None        # omit to use policy default
)

print("Installed to:", result.target)
print("Python env:", result.env.python_prepared, "Node env:", result.env.node_prepared)
print("Runner type:", result.runner.get("type"))
```

The build step: 1) requests a plan, 2) writes files/artifacts, 3) ensures `runner.json`, 4) prepares envs.

---

## 3) Run locally (no daemon)

```python
from matrix_sdk import runtime

lock = runtime.start("/abs/path/to/install", alias="hello-sse")  # reads runner.json
print(lock.pid, lock.port)

print(runtime.status())
runtime.stop("hello-sse")
```

**Connector / attach mode:** if `runner.json` contains `{"type":"connector","url":"http://127.0.0.1:6288/sse"}`, `runtime.start(...)` **does not** spawn a process; it records the URL in the lock (`pid=0`). Use your MCP client to talk to that endpoint directly.

---

## 4) Advanced search helper

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.search import search, SearchOptions, search_try_modes

hub = MatrixClient("https://api.matrixhub.io")

# Hybrid with safe fallbacks; returns dict by default
res = search(
    hub, "chat with PDFs",
    type="any",
    limit=5,
    with_snippets=True,
)

# Typed result (Pydantic model) with tiny retries
res_typed = search(
    hub, "vector stores",
    type="tool",
    options=SearchOptions(as_model=True, max_attempts=3)
)

# Try modes explicitly (no fallbacks)
for mode, payload in search_try_modes(hub, "hello", modes=("keyword","semantic","hybrid")):
    print(mode, payload.get("total"))
```

---

## 5) Manage catalog remotes

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient("https://api.matrixhub.io")
print(hub.list_remotes())

hub.add_remote("https://example.org/catalog/index.json", name="example")
hub.trigger_ingest("example")
```

---

## 6) Bulk registration (Gateway, optional)

```python
import asyncio, os
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [{
    "kind": "git",
    "url": "https://github.com/ruslanmv/hello-mcp",
    "ref": "main",
    "probe": True,  # fetch and merge capabilities when possible
}]

registrar = BulkRegistrar(
    gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:4444"),
    token=os.getenv("ADMIN_TOKEN"),
    concurrency=25,
    probe=True,
)

results = asyncio.run(registrar.register_servers(sources))
print(results)
```

---

## 7) Error handling

```python
from matrix_sdk.client import MatrixClient, MatrixError

hub = MatrixClient("https://api.matrixhub.io")

try:
    hub.install(id="tool:does-not-exist@0.0.0", target="./dest")
except MatrixError as e:
    print("Install failed:", getattr(e, "status", None), e)
```

---

## Environment variables (quick reference)

* `MATRIX_SDK_DEBUG=1` — verbose logs (installer/runtime/search/etc.).
* `MATRIX_HOME` — base dir for state/logs (default: `~/.matrix`).
* `MATRIX_SDK_ENABLE_CONNECTOR=1` — allow connector runner synthesis (default **on**).
* `MATRIX_SDK_HTTP_TIMEOUT` — network timeout (seconds; default **15**).
