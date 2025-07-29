# Matrix Python SDK

[![PyPI Version](https://img.shields.io/pypi/v/matrix-python-sdk.svg)](https://pypi.org/project/matrix-python-sdk/)
[![Python Versions](https://img.shields.io/pypi/pyversions/matrix-python-sdk.svg)](https://pypi.org/project/matrix-python-sdk/)
[![CI Status](https://github.com/agent-matrix/matrix-python-sdk/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/agent-matrix/matrix-python-sdk/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/matrix-python-sdk.svg)](https://github.com/agent-matrix/matrix-python-sdk/blob/master/LICENSE)


The **matrix-python-sdk** is the official Python Software Development Kit (SDK) for the [Matrix Hub](https://github.com/agent-matrix/matrix-hub) API. It provides a high-level, programmatic interface for interacting with the Matrix Hub, designed for developers and enterprises building solutions within the Matrix ecosystem.

### Key Features

  * **Catalog Management**: Search and retrieve detailed information about agents, tools, and other catalog entities.
  * **Package Installation**: Programmatically install agents, tools, and MCP servers with dependency resolution.
  * **Remote & Ingestion Control**: Manage catalog remotes and trigger data ingestion processes.
  * **Bulk Operations**: A lightweight micro-framework for registering millions of MCP servers concurrently.

-----

## Installation

To install the library, run the following command:

```bash
pip install matrix-python-sdk
```

### Requirements

  * **Python**: Version `3.11` or newer.

### Optional Extras

You can install additional dependencies for specific functionalities:

  * **CLI**: `pip install "matrix-python-sdk[cli]"` for the `matrix servers` bulk-registration command-line interface.
  * **Development**: `pip install "matrix-python-sdk[dev]"` for installing development dependencies (testing, linting).

-----

## Getting Started

The following example demonstrates how to initialize the client and perform a basic catalog search.

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.cache import Cache
from matrix_sdk.types import SearchResponse

# 1. (Optional) Initialize a local cache to improve performance for repeated requests.
#    The TTL (Time To Live) is set in seconds.
cache = Cache(cache_dir="~/.cache/matrix", ttl=(4 * 60 * 60)) # 4 hours

# 2. Initialize the MatrixClient with your Hub URL and authentication token.
client = MatrixClient(
    base_url="http://localhost:7300",
    token="YOUR_MATRIX_TOKEN",
    cache=cache,
)

# 3. Perform a catalog search for agents related to "summarize pdfs".
#    Filters can be applied for capabilities, frameworks, providers, etc.
resp: SearchResponse = client.search(
    q="summarize pdfs",
    type="agent",
    capabilities="pdf,summarize",
    frameworks="langgraph,crewai",
    providers="openai,watsonx",
    mode="hybrid",
    limit=10,
)

# 4. Process and display the search results.
print(f"Found {resp.total} results matching your query:")
for item in resp.items:
    print(f"- ID: {item.id} (Score: {item.score_final:.2f})\n  Summary: {item.summary}\n")

```

-----

## API Reference

### `MatrixClient`

The `matrix_sdk.client.MatrixClient` is the primary interface for interacting with the Matrix Hub API.

| Method                    | Description                                                      | Returns          |
| :------------------------ | :--------------------------------------------------------------- | :--------------- |
| **`.search(...)`** | Performs a full-text and filtered search of the catalog.         | `SearchResponse` |
| **`.get_entity(id)`** | Retrieves the full manifest and metadata for a given entity UID. | `EntityDetail`   |
| **`.install(id, target, …)`** | Executes an installation plan (e.g., `pip`, `docker`, adapters). | `InstallOutcome` |
| **`.list_remotes()`** | Lists all configured catalog remotes.                            | `dict`           |
| **`.add_remote(url, …)`** | Adds a new remote index, including its name and trust policy.    | `dict`           |
| **`.trigger_ingest(name)`** | Manually initiates the ingestion process for a specified remote. | `dict`           |

### `Cache`

The `matrix_sdk.cache.Cache` is an optional component for caching API responses to reduce latency and API load.

  * **Constructor**: `Cache(cache_dir: Path | str, ttl: int)`
  * **Methods**:
      * `.get(key, allow_expired=False)` → `CachedResponse | None`
      * `.set(key, response, etag=None)` → `None`

### Data Types (`matrix_sdk.types`)

The SDK uses Pydantic models for structured, type-hinted data in API requests and responses. Key models include:

  * **Search & Entities**: `SearchItem`, `SearchResponse`, `EntityDetail`
  * **Installation**: `InstallStepResult`, `InstallOutcome`
  * **Errors**: `MatrixAPIError`

### Bulk Server Registration

For managing large-scale deployments, the `BulkRegistrar` provides an efficient, asynchronous method to register multiple MCP servers from various sources.

```python
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar
import asyncio

# Define sources for server discovery (e.g., Git repository)
sources = [
    {
        "kind": "git",
        "url": "https://github.com/IBM/docling-mcp",
        "ref": "main",
        "probe": True
    }
]

# Initialize the registrar with the gateway URL and an admin token
registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN",
    concurrency=100
)

# Asynchronously register all servers found in the defined sources
results = asyncio.run(registrar.register_servers(sources))
print(results)
```

-----

## Development & Testing

To contribute to development or run tests locally, first set up the environment.

```bash
# Install development dependencies
pip install "matrix-python-sdk[dev]"
```

### Running Tests

Execute the test suite using `pytest`:

```bash
pytest
```

### Code Style & Linting

We use `ruff` for linting and formatting.

```bash
# Check for issues
ruff check .

# Automatically fix issues
ruff format .
ruff check . --fix
```

Continuous Integration (CI) is configured via GitHub Actions. See the workflow file at `.github/workflows/ci.yml`.

-----

## License

This project is licensed under the **Apache 2.0 License**.

© Agent Matrix — [LICENSE](LICENSE)

-----

## Contributing

We welcome contributions from the community. Please read our [**CONTRIBUTING.md**](CONTRIBUTING.md) for guidelines on how to submit issues, feature requests, and pull requests.

-----

*Matrix Hub and the Matrix Python SDK are open-source projects by the Agent Matrix team.*