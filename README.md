# matrix‑python‑sdk

**Python SDK for Matrix Hub** — programmatic access to catalog search, entity detail, install, and remote management.

---

## 🚀 Install

```bash
pip install matrix-python-sdk
```
Requires Python 3.11+.

## 🔧 Quickstart
```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.cache import Cache
from matrix_sdk.types import SearchResponse

# Optional: local caching
cache = Cache(cache_dir="~/.cache/matrix", ttl=4*60*60)

# Initialize client
client = MatrixClient(
    base_url="http://localhost:7300",
    token="YOUR_MATRIX_TOKEN",
    cache=cache,
)

# Search for agents that summarize PDFs
resp: SearchResponse = client.search(
    q="summarize pdfs",
    type="agent",
    capabilities="pdf,summarize",
    frameworks="langgraph,crewai",
    providers="openai,watsonx",
    mode="hybrid",
    limit=10,
)

print(f"Found {resp.total} results")
for item in resp.items:
    print(f"- {item.id} ({item.score_final:.2f}) — {item.summary}")
```

## 📦 API Reference
* `MatrixClient` (`matrix_sdk.client`):
    * `.search(...)` → `SearchResponse`
    * `.get_entity(id)` → `EntityDetail`
    * `.install(id, target, version=None)` → `InstallOutcome`
    * `.list_remotes()`, `.add_remote(url, name=None)`, `.trigger_ingest(name)`
* `Caching` (`matrix_sdk.cache`):
    * `Cache(cache_dir, ttl)`
    * `make_cache_key(url, params)`
* `Types` (`matrix_sdk.types`):
    * `SearchItem`
    * `SearchResponse`
    * `EntityDetail`
    * `InstallStepResult`, `InstallOutcome`
    * `MatrixAPIError`

See the docs for full details.

## 🧪 Testing
```bash
pip install pytest
pytest  # run the SDK’s own unit tests
```

## 📄 License
Apache‑2.0 © agent‑matrix
