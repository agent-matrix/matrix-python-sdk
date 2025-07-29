# Bulk Registration

The SDK enables automated discovery, validation, and registration of *millions* of agents, tools, and MCP servers.

## How it works

1. **Discovery**: Given a source (`git`, `zip`, or `Docker`), the SDK fetches and parses the manifest (e.g., `pyproject.toml`, `mcp.json`).
2. **Optional Probe**: Can start the server to auto-discover its MCP capabilities.
3. **Validation**: Each manifest is checked for schema and health.
4. **Upsert**: The server/tool/agent is added/updated in the MCP Gateway via REST API.
5. **Idempotency**: Safe for bulk (deduplication via source hash).

## Example: Registering a GitHub MCP server

```python
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [
    {
        "kind": "git",
        "url": "https://github.com/IBM/docling-mcp",
        "ref": "main",
        "probe": True
    }
]

registrar = BulkRegistrar("http://localhost:4444", "ADMIN_TOKEN")
import asyncio
results = asyncio.run(registrar.register_servers(sources))
```

## CLI Usage

Register many servers from NDJSON:

```bash
matrix servers bulk-add sources.ndjson --gateway http://localhost:4444 --token $TOKEN
```

*See [Usage](usage.md) for more examples.*
