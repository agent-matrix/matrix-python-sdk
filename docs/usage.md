# Usage Examples

## Basic Search

```python
from matrix_sdk.client import MatrixClient
client = MatrixClient("http://localhost:7300", token="...")
resp = client.search(q="summarize pdfs", type="agent", limit=5)
print(resp.total, "agents found")
```

## Get Entity Detail
```python
detail = client.get_entity("agent:pdf-summarizer@1.4.2")
print(detail.name, detail.description)
```

## Install into Project
```python
outcome = client.install("agent:pdf-summarizer@1.4.2", target="./my-app")
print("Files:", outcome.files_written)
```

## Manage Remotes
```python
remotes = client.list_remotes()
client.add_remote("https://.../index.json", name="official")
client.trigger_ingest("official")
```
