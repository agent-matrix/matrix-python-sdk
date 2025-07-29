# Bulk Registration

The `BulkRegistrar` provides an efficient, asynchronous framework for discovering, validating, and registering a large number of agents, tools, and MCP servers into the Matrix Hub. It is designed for large-scale deployments where manual registration is not feasible.

---

## How It Works

The registration process is a multi-step pipeline designed for robustness and idempotency:

1.  **Discovery**: Given a source descriptor (e.g., a Git repository, a local ZIP file), the registrar fetches and parses its manifest (`pyproject.toml` or `mcp.json`).
2.  **Probing (Optional)**: If enabled, the registrar can start the server in a sandboxed environment to automatically discover its capabilities, enriching the manifest with live data.
3.  **Idempotency Key Generation**: A stable hash is generated from the manifest content. This key ensures that identical servers are not registered multiple times, making the operation safe to re-run.
4.  **Upsert**: The manifest is sent to the MCP Gateway's Admin API. The registrar uses an exponential backoff and retry mechanism to handle transient network errors gracefully.

---

## Python Example

The following example demonstrates how to register a server from a Git repository.

```python
import asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# 1. Define a list of sources to discover servers from.
#    Each source can be a Git repository, a local ZIP file, etc.
sources = [
    {
        "kind": "git",
        "url": "[https://github.com/IBM/docling-mcp](https://github.com/IBM/docling-mcp)",
        "ref": "main",
        "probe": True  # Enable probing to discover live capabilities
    }
]

# 2. Initialize the registrar with the gateway URL, an admin token,
#    and the desired concurrency level.
registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN",
    concurrency=100
)

# 3. Run the registration process and print the results.
#    The results will contain either success statuses or exceptions.
print("--- Starting bulk server registration... ---")
results = asyncio.run(registrar.register_servers(sources))
print("Registration complete:", results)
```



## Command-Line Interface (CLI)

For convenience, you can perform bulk registration directly from the command line using the `matrix servers` command. This is available if you installed the SDK with the `[cli]` extra.

The command reads a newline-delimited JSON (`.ndjson`) file where each line is a source descriptor.

```bash
# Register multiple servers from a sources.ndjson file
matrix servers bulk-add sources.ndjson \
    --gateway http://localhost:4444 \
    --token $ADMIN_TOKEN
```

*See the [Usage Examples](usage.md) documentation for more detailed scenarios.*

