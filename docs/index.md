# matrix-python-sdk

**matrix-python-sdk** is the official Python SDK for \[Matrix Hub] — the open catalog and installer for **agents**, **tools**, and **MCP servers**.

It gives teams a *single, dependable path* to **discover**, **evaluate**, **install**, and **run** AI components. Whether you’re curating a marketplace, wiring up internal tools, or shipping end-user assistants, this SDK helps you move from *“I think I need X”* to *“X is installed, configured, and running”* in minutes.

---

## Why it matters

* **One interface for many ecosystems.** Agents, tools, and MCP servers are published in different places and formats. Matrix Hub normalizes them; this SDK makes them easy to query and install.
* **Reproducible installs.** The Hub returns concrete install plans and lockfiles; the SDK materializes them safely and consistently.
* **Built for production.** Secure archive extraction, Git host allow-lists, optional ETag caching, typed models, and careful retries mean fewer surprises in CI and prod.
* **Scales as your catalog grows.** Efficient search helpers (keyword/semantic/hybrid) with normalization and fallback keep UX fast even as catalogs reach millions of entries (the heavy lifting stays server-side).

---

## What this package offers

* **Ergonomic client** (`MatrixClient`) to call Hub APIs: `search`, `entity`, `install`, and catalog remotes.
* **Search helpers** (`matrix_sdk.search`) with filter normalization, small retries, and *mode fallbacks* (semantic → hybrid → keyword).
* **Local installer** (`matrix_sdk.installer.LocalInstaller`) to fetch artifacts, write adapters, and prepare runtime environments.
* **Lightweight runtime** (`matrix_sdk.runtime`) to start/stop/list/tail/doctor local MCP servers—no daemon required.
* **Bulk registrar** (`matrix_sdk.bulk.*`, optional) to discover manifests (ZIP/dir/Git) and register MCP servers into a Gateway with idempotency and capability probing.

---

## Install

```bash
pip install matrix-python-sdk
```

---

## Quickstart — Search & install

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient(base_url="http://127.0.0.1:7300")

# Top-5 search across all types; include snippets when available
res = hub.search(
    q="extract pdf tables",
    type="any",
    limit=5,
    with_snippets=True,
    mode="hybrid",         # "keyword" | "semantic" | "hybrid"
    with_rag=False,        # ask for short fit_reason if supported
    include_pending=False, # show unregistered entities in dev if True
)

for item in res.get("items", []):
    print(item.get("id"), "→", item.get("manifest_url"))

# Install the first result (writes adapters/lockfile when applicable)
if res.get("items"):
    first = res["items"][0]["id"]
    hub.install(id=first, target="./.matrix/runners/demo")
```

---

## Quickstart — Run locally

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller
from matrix_sdk import runtime

hub = MatrixClient("http://127.0.0.1:7300")
installer = LocalInstaller(hub)

result = installer.build("mcp_server:hello-sse-server@0.1.0", alias="my-server")
lock = runtime.start(result.target, alias="my-server")

print("PID:", lock.pid, "PORT:", lock.port)
print("Status:", runtime.status())
runtime.stop("my-server")
```

---

## Advanced search (helper)

Prefer the high-level helper when you want resilience and typed results.

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.search import search, SearchOptions, search_try_modes

hub = MatrixClient("http://127.0.0.1:7300")

# Hybrid with safe fallbacks; returns dict by default
res = search(
    hub, "summarize pdfs",
    type="any",
    limit=5,
    with_snippets=True,
    include_pending=True,
)

# Semantic-first, typed response (Pydantic), with small retries
res_typed = search(
    hub, "chat with PDFs",
    type="agent",
    mode="semantic",
    options=SearchOptions(as_model=True, max_attempts=3),
)
print(res_typed.total, [it.id for it in res_typed.items])

# Try fixed modes without fallbacks (diagnostics)
for mode, payload in search_try_modes(hub, "hello", modes=("keyword","semantic","hybrid"), type="any", limit=5):
    print(mode, len(payload.get("items", [])))
```

**Why use the helper?**

* Normalizes filters (lists/sets → CSV), clamps limits, and retries transient errors with jittered backoff.
* Smart mode fallbacks avoid user-visible “empty” results when semantic indices aren’t available in dev.
* Optional typed responses (`SearchResponse`) for ergonomic, safe code.

---

## Bulk registration (optional)

Discover MCP server manifests and register them into a Gateway Admin API.

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

**Highlights**

* **Matrix-first discovery:** looks for `matrix/index.json` or `matrix/*.manifest.json`; falls back to `pyproject.toml`.
* **Gateway compatibility:** JSON first; auto-fallback to form with safe name sanitization.
* **Resilience:** concurrency control, idempotency keys, exponential backoff with jitter.

---

## Design values

* **Reliability:** strict error types, small safe retries, idempotent bulk writes, ETag-aware client cache.
* **Security by default:** safe archive extraction, Git host allow-lists, deny-by-default where sensible.
* **Performance:** lightweight client, normalized query params, and server-side scoring/indexing.
* **Compatibility:** Pydantic v1/v2 models, tolerant schema (unknown fields allowed), portable runtime.

---

## Docs

* [Installation](install.md)
* [Configuration](config.md)
* [Usage](usage.md)
* [API Reference](reference.md)
* [Bulk Registration](bulk.md)

---

*matrix-python-sdk* helps you move faster from **idea → discovery → install → run** with production-grade guardrails and a clean, Pythonic API.
