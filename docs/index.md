```markdown
# matrix-python-sdk

**matrix-python-sdk** is the official Python SDK for [Matrix Hub](https://github.com/agent-matrix/matrix-hub) — a catalog and installer for agents, tools, and MCP servers.

With this SDK you can **search**, **inspect**, and **install** items from a Matrix Hub, and (optionally) **bulk-register** MCP servers with a Gateway.

---

- [Installation](install.md)
- [Configuration](config.md)
- [Usage](usage.md)
- [API Reference](reference.md)
- [Bulk Registration](bulk.md)

---

## Why this SDK?

- **Simple Top-5 search** across agents/tools/servers.
- **One-line install**: download artifacts and write adapters/lockfiles via the Hub.
- **Matrix-first discovery** for bulk: reads `matrix/` manifests, falls back to `pyproject.toml`.
- **Gateway-friendly bulk registrar**: JSON first, with **auto fallback to form** and safe **name sanitization**.
- **Typed models** (Pydantic v1/v2 compatible) and optional **ETag-aware caching**.

## Quickstart (Hub)

```python
from matrix_sdk import MatrixClient

hub = MatrixClient(base_url="http://127.0.0.1:7300")

# Top-5 search across all types; include short snippets when available
res = hub.search(q="extract pdf tables", type="any", limit=5, with_snippets=True)
for item in res.get("items", []):
    print(item.get("name"), "→", item.get("manifest_url"))

# Install the first result into your project (creates adapters/lockfile when applicable)
if res.get("items"):
    hub.install(id=res["items"][0]["id"], target="./.matrix/runners/demo")
```

See [Usage](usage.md) for more examples.

## Quickstart (Bulk, optional)

If you also manage an MCP Gateway, you can bulk-register servers discovered in a ZIP/dir/Git repo:

```python
import os, asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [{"kind":"git","url":"https://github.com/ruslanmv/hello-mcp","ref":"main","probe":True}]

registrar = BulkRegistrar(
    gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:4444"),
    token=os.getenv("ADMIN_TOKEN"),
    concurrency=10,
    probe=True
)

results = asyncio.run(registrar.register_servers(sources))
print(results)  # e.g., {"message":"Server created successfully!","success":true}
```

See [Bulk Registration](bulk.md) for discovery rules, environment variables, and CLI usage.

