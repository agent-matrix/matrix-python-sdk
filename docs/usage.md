# Usage Examples

This guide shows two distinct workflows:

1. **Catalog operations** against the Matrix Hub (port 7300)  
   – Use `MatrixClient` to search, install, and manage catalog remotes.  
2. **Gateway registration** against the MCP Gateway Admin API (port 4444)  
   – Use `BulkRegistrar` to bulk‑register MCP servers (e.g. from Git, ZIP, NDJSON).

---

## 1. Catalog Operations (`MatrixClient`)

> **When to use**: you want to discover or install agents/tools in your codebase, or manage which remote catalogs the Hub ingests.

```python
from matrix_sdk.client import MatrixClient

# Initialize client for the Hub
client = MatrixClient("http://localhost:7300", token="YOUR_TOKEN")

# — Search for agents in the catalog —
resp = client.search(q="summarize pdfs", type="agent", limit=5)
if not resp.items:
    print("No agents found.")
else:
    # Print basic info
    for item in resp.items:
        print(f"{item.id}: {item.summary}")

    # Inspect details of the first agent
    first_id = resp.items[0].id
    detail = client.get_entity(first_id)
    print(detail.name, "-", detail.description)

    # Install into your project directory
    outcome = client.install(id=first_id, target="./apps/pdf-bot")
    print("Installed files:", outcome.files_written)

# — Manage remote catalogs —  
print("Remotes before:", client.list_remotes())
client.add_remote("https://some-org.github.io/my-index.json", name="custom-org")
client.trigger_ingest("custom-org")
print("Triggered ingestion for 'custom-org'")
````

**Key points**

* All methods are **synchronous**, wrap HTTP calls, and return typed Pydantic models.
* Use for anything under `/catalog/*` (search, install, remotes, ingest).

---

## 2. Bulk MCP‑Server Registration (`BulkRegistrar`)

> **When to use**: you have one or more MCP server manifests (Git repos, ZIPs, NDJSON lists) and need to register them **in bulk** with your Gateway.

```python
import asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# 1. Define your sources (e.g. Git repo containing server manifest)
sources = [{
    "kind": "git",
    "url": "https://github.com/IBM/docling-mcp",
    "ref": "main",
    "probe": True   # optional: check server capabilities before registering
}]

# 2. Initialize the registrar for the Gateway Admin API
registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN"
)

# 3. Run bulk registration (async)
results = asyncio.run(registrar.register_servers(sources))
print("Registration results:", results)
```

### Alternative: NDJSON file of sources

```python
import asyncio, json
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# Load newline‑delimited JSON list of sources
with open("sources.ndjson") as f:
    sources = [json.loads(line) for line in f if line.strip()]

registrar = BulkRegistrar("http://localhost:4444", token="YOUR_ADMIN_TOKEN")
results = asyncio.run(registrar.register_servers(sources))
print("Results:", results)
```

**Key points**

* Fully **asynchronous**, optimized for high‑throughput & retries.
* Manifests can come from Git, ZIP, Docker, etc., and will be **upserted** into the Gateway.
* **Does not** touch your local codebase or lockfiles—only talks to the Admin API under `/admin/servers`.

---

## Why two clients?

* **`MatrixClient`** = Catalog & Install

  * Manages **what** agents/tools are available and **pulls** their code into your projects.
* **`BulkRegistrar`** = Gateway Registration

  * Manages **where** those agents/tools run at runtime, registering them with the MCP‑Gateway.

Their separation keeps each focused, but you can wrap both in your own façade if you prefer a single entrypoint.
