# matrix-python-sdk

**matrix-python-sdk** is the official Python SDK for [Matrix Hub](https://github.com/agent-matrix/matrix-hub), the open agent, tool, and MCP server registry powering scalable orchestration, discovery, and composition in AI ecosystems like watsonx Orchestrate.

With this SDK, you can **search**, **inspect**, **install**, and **bulk-register** agents, tools, and Model Context Protocol (MCP) servers. It supports both single entity operations and automated, scalable bulk ingestion (millions+).

---

- [Installation](install.md)
- [Usage Examples](usage.md)
- [Bulk Registration](bulk.md)
- [API Reference](reference.md)
- [Configuration](config.md)
- [Contributing](contributing.md)

---

## Why use matrix-python-sdk?

- **Automate onboarding** of new agents/tools/servers with strong validation, health-checking, and idempotency
- **Integrate** your own tool/agent index with any Matrix Hub or Gateway (private or public)
- **Powerful CLI** for bulk operations
- **Cache** and offline-friendly search
- **Standards-based**: typed Pydantic models, JSON Schema validation, async and sync APIs
- **Scalable**: Used as the ingestion engine in enterprise AI orchestration (watsonx, agent-generator)


### Features  matrix-python-sdk 

* **Turns millions of open-source tools into composable, discoverable “skills”**
* **Makes it easy for users, ops, or automated systems to keep catalogs current**
* **Drives adoption of open orchestration standards like MCP**
* **Integrates easily into CI/CD, agent generators, or your own orchestrators**


---

## Quick Example: Importing an MCP Server

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
    gateway_url="http://your-gateway-host:4444",
    token="YOUR_ADMIN_TOKEN"
)
import asyncio
results = asyncio.run(registrar.register_servers(sources))
print(results)
````

See [Bulk Registration](bulk.md) for more.
