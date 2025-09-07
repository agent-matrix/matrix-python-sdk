# Matrix Python SDK

[![PyPI Version](https://img.shields.io/pypi/v/matrix-python-sdk.svg)](https://pypi.org/project/matrix-python-sdk/)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/matrix-python-sdk.svg)](https://pypi.org/project/matrix-python-sdk/)
[![CI Status](https://github.com/agent-matrix/matrix-python-sdk/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/agent-matrix/matrix-python-sdk/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blu)](https://github.com/agent-matrix/matrix-python-sdk/blob/master/LICENSE)

**matrix-python-sdk** is the official Python SDK for the [Matrix Hub](https://github.com/agent-matrix/matrix-hub) — the open catalog and installer for **agents**, **tools**, and **MCP servers**.

Built for teams that need **fast discovery**, **reproducible installs**, and **safe runtime** operations at scale.

---
## What’s new in 0.1.9

A focused, compatibility-preserving refresh.

* **Modular installer** with legacy import preserved:

  ```python
  from matrix_sdk.installer import LocalInstaller
  ```

  Internals split into `installer/core.py`, `runner_schema.py`, `runner_discovery.py`, `envs.py`, `util.py`.

* **Runner discovery** restored to legacy strategy order with modern safeguards:
  b64 → URL → object → embedded manifest → file → shallow search → manifest URL (opt-in) → infer by structure → **connector** fallback.

* **Connector synthesis** (attach mode): auto-generates minimal
  `{"type":"connector","url":"…/sse"}` when an MCP/SSE endpoint is found. Gate via `MATRIX_SDK_ENABLE_CONNECTOR=1` (default **on**).

* **Windows venv reliability**: venv creation retries without symlinks when required.

* **Shared utilities**: env toggles, timeouts, runner search depth, safe FS checks in `installer/util.py`.

* **Docs**: updated install/usage/API pages; clearer examples.

> Requires Python **3.11–3.12**.

---

## Why teams choose this SDK

* **One interface** for a messy ecosystem — consistent search/install across agents, tools, and MCP servers.
* **Reproducible installs** — Hub-backed plans, adapters, and lockfiles you can ship to CI and prod.
* **Production guardrails** — safe archive extraction, Git host allow-lists, ETag-aware caching, typed models.
* **Performance at scale** — lean client, server-side indexing/scoring, normalized params to maximize cache hits.

---

## Install

```bash
pip install matrix-python-sdk
```

Python **3.11+** supported.

---

## Quickstart

### 1) Search

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient("https://api.matrixhub.io")

res = hub.search(
    q="extract pdf tables",
    type="any",
    mode="hybrid",       # "keyword" | "semantic" | "hybrid"
    limit=5,
    with_snippets=True,
    with_rag=False,
    include_pending=False,
    rerank="none",
)

for it in res.get("items", []):
    print(it.get("id"), "→", it.get("manifest_url"))
```

Prefer the high-level helper for resilience and typed results:

```python
from matrix_sdk.search import search, SearchOptions
res = search(hub, "chat with PDFs", type="agent",
             options=SearchOptions(as_model=True, max_attempts=3))
print(res.total, [i.id for i in res.items])
```

### 2) Install

```python
from matrix_sdk.client import MatrixClient

hub = MatrixClient("https://api.matrixhub.io")

hub.install(
    id="mcp_server:hello-sse-server@0.1.0",
    target="./.matrix/runners/demo",
    # alias="hello-sse",         # optional
    # options={"force": True},   # optional
)
```

### 3) Run locally (no daemon)

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller
from matrix_sdk import runtime

hub = MatrixClient("https://api.matrixhub.io")
result = LocalInstaller(hub).build("mcp_server:hello-sse-server@0.1.0", alias="hello-sse")

lock = runtime.start(result.target, alias="hello-sse")
print("PID:", lock.pid, "PORT:", lock.port)
runtime.stop("hello-sse")
```

#### Connector / attach mode (new in 0.1.6)

If `runner.json` has `{"type":"connector","url":"http://127.0.0.1:6288/sse"}`, `runtime.start(...)` **does not** start a process. Instead it stores the URL in the lock (with `pid=0`). Use the URL directly with your MCP client.

```jsonc
// ~/.matrix/runners/<alias>/<version>/runner.json
{
  "type": "connector",
  "integration_type": "MCP",
  "request_type": "SSE",
  "url": "http://127.0.0.1:6288/sse",
  "endpoint": "/sse"
}
```

### 4) Bulk register (optional)

Register servers (ZIP/dir/Git) into an MCP Gateway with concurrency, idempotency keys, and capability probing.

```python
import os, asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [{"kind":"git","url":"https://github.com/ruslanmv/hello-mcp","ref":"main","probe":True}]

registrar = BulkRegistrar(
    gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:4444"),
    token=os.getenv("ADMIN_TOKEN"),
    concurrency=50,
    probe=True,
)

results = asyncio.run(registrar.register_servers(sources))
print(results)
```

---

## API surface (snapshot)

* `matrix_sdk.client.MatrixClient`: `search`, `entity`, `install`, `list_remotes`, `add_remote`, `delete_remote`, `trigger_ingest`
* `matrix_sdk.search`: `search`, `search_try_modes`, `SearchOptions`
* `matrix_sdk.installer.LocalInstaller`: `plan`, `materialize`, `prepare_env`, `build`
* `matrix_sdk.runtime`: `start`, `stop`, `status`, `tail_logs`, `doctor`, `log_path`
  *(Lock files now include an optional `url` for connector runners; `stop()` is a no-op for connectors.)*
* `matrix_sdk.bulk.*` (optional): discovery, gateway client, registrar

Pydantic models (v1/v2 compatible) in `matrix_sdk.schemas`: `SearchItem`, `SearchResponse`, `EntityDetail`, `InstallOutcome`, etc.

---

## Reliability, Security, Performance

* **Reliability**: strict error types, small safe retries, idempotent bulk writes, optional ETag cache.
* **Security**: safe ZIP/TAR extraction, Git host allow-lists, deny-by-default where sensible.
* **Performance**: minimal client overhead, normalized search params, server-side scoring and indexing.

---

## Links

* Docs: see `docs/` (MkDocs) — Usage, API Reference, Bulk
* Source: [https://github.com/agent-matrix/matrix-python-sdk](https://github.com/agent-matrix/matrix-python-sdk)
* Matrix Hub: [https://github.com/agent-matrix/matrix-hub](https://github.com/agent-matrix/matrix-hub)
* License: [Apache 2.0](https://github.com/agent-matrix/matrix-python-sdk/blob/master/LICENSE)

---

## Contributing

We welcome issues and PRs. Please read **CONTRIBUTING.md** before submitting changes.
Join us in shaping a fast, safe, and open ecosystem for AI agents, tools, and MCP servers.
