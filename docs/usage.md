# Usage Examples

## Search Agents/Tools/Servers

```python
from matrix_sdk.client import MatrixClient

client = MatrixClient("http://localhost:7300", token="YOUR_TOKEN")
resp = client.search(q="summarize pdfs", type="agent", limit=5)
print(resp.total, "agents found")
for item in resp.items:
    print(item.id, item.summary)
```

## Get Entity Detail

```python
detail = client.get_entity("agent:pdf-summarizer@1.4.2")
print(detail.name, detail.description, detail.capabilities)
```

## Install an Agent or Tool

```python
outcome = client.install("agent:pdf-summarizer@1.4.2", target="./my-app")
print("Files installed:", outcome.files_written)
```

## Manage Remotes

```python
remotes = client.list_remotes()
client.add_remote("https://some-org.github.io/my-index.json", name="myorg")
client.trigger_ingest("myorg")
```

---

## Example: Import an MCP server (docling-mcp) into the Matrix Hub

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

registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN"
)
import asyncio
results = asyncio.run(registrar.register_servers(sources))
print(results)
```

---

## Bulk Import from NDJSON

```python
# each line: {"kind":"git", "url":"...", "ref":"main"}
import json
with open("sources.ndjson") as f:
    sources = [json.loads(line) for line in f if line.strip()]

from matrix_sdk.bulk.bulk_registrar import BulkRegistrar
registrar = BulkRegistrar(gateway_url="http://localhost:4444", token="...")
import asyncio
asyncio.run(registrar.register_servers(sources))
```
