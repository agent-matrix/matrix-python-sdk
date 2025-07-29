# Usage Examples

This document provides practical examples for using the Matrix Python SDK.

---

## Basic Client Workflow

This example shows a common workflow: initializing the client, searching for an agent, getting its details, and then installing it.

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.schemas import SearchResponse, EntityDetail, InstallOutcome

# Initialize the client, optionally with a cache for better performance
client = MatrixClient("http://localhost:7300", token="YOUR_TOKEN")

# 1. Search for an agent
print("--- Searching for agents... ---")
search_resp: SearchResponse = client.search(q="summarize pdfs", type="agent", limit=5)

if not search_resp.items:
    print("No agents found.")
else:
    print(f"Found {search_resp.total} agents.")
    for item in search_resp.items:
        print(f"  - {item.id} | Summary: {item.summary}")

    # 2. Get details for the first agent found
    first_agent_id = search_resp.items[0].id
    print(f"\n--- Getting details for {first_agent_id}... ---")
    detail: EntityDetail = client.get_entity(first_agent_id)
    print(f"  Name: {detail.name}")
    print(f"  Description: {detail.description}")
    print(f"  Capabilities: {detail.capabilities}")

    # 3. Install the agent
    print(f"\n--- Installing {first_agent_id}... ---")
    outcome: InstallOutcome = client.install(id=first_agent_id, target="./my-app")
    print(f"  Installation complete. Files written to ./my-app")
    print(f"  Files: {outcome.files_written}")
```


## Managing Catalog Remotes

You can programmatically list, add, and trigger ingestion for catalog remotes.

```python
from matrix_sdk.client import MatrixClient

client = MatrixClient("http://localhost:7300", token="YOUR_TOKEN")

# List currently configured remotes
print("--- Listing remotes... ---")
remotes = client.list_remotes()
print("Current remotes:", remotes)

# Add a new remote
print("\n--- Adding a new remote... ---")
client.add_remote("[https://some-org.github.io/my-index.json](https://some-org.github.io/my-index.json)", name="my-custom-org")
print("Added remote 'my-custom-org'.")

# Trigger ingestion for the new remote
print("\n--- Triggering ingestion... ---")
client.trigger_ingest("my-custom-org")
print("Ingestion triggered for 'my-custom-org'.")
```


## Bulk Server Registration

The `BulkRegistrar` is designed for efficiently registering a large number of MCP servers.

### Example: Register from a Git Source

```python
import asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# Define a source pointing to a Git repository containing a server manifest
sources = [
    {
        "kind": "git",
        "url": "[https://github.com/IBM/docling-mcp](https://github.com/IBM/docling-mcp)",
        "ref": "main",
        "probe": True  # Optionally probe the server for capabilities
    }
]

# Initialize the registrar with the gateway URL and an admin token
registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN"
)

# Run the registration process
print("--- Registering server from Git source... ---")
results = asyncio.run(registrar.register_servers(sources))
print("Registration results:", results)
```

### Example: Register from an NDJSON File

You can also load a list of sources from a newline-delimited JSON file (`.ndjson`).

```python
# sources.ndjson file content:
# {"kind":"git", "url":"[https://github.com/some/repo.git](https://github.com/some/repo.git)", "ref":"main"}
# {"kind":"git", "url":"[https://github.com/another/repo.git](https://github.com/another/repo.git)", "ref":"v1.2"}

import asyncio
import json
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# Load sources from the file
try:
    with open("sources.ndjson") as f:
        sources = [json.loads(line) for line in f if line.strip()]
except FileNotFoundError:
    print("Error: sources.ndjson not found.")
    sources = []

if sources:
    # Initialize the registrar
    registrar = BulkRegistrar(
        gateway_url="http://localhost:4444",
        token="YOUR_ADMIN_TOKEN"
    )

    # Run the registration process
    print("--- Registering servers from NDJSON file... ---")
    results = asyncio.run(registrar.register_servers(sources))
    print("Registration results:", results)
```