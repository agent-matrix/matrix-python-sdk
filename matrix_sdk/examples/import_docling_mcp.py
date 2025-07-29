# examples/import_docling_mcp.py

import asyncio

from matrix_sdk.bulk.bulk_registrar import BulkRegistrar


async def main():
    # 1) Define your source spec.
    #    Here we point at the GitHub repo for docling-mcp.
    sources = [
        {
            "kind": "git",
            "url": "https://github.com/IBM/docling-mcp.git",
            "ref": "main",  # optional branch/tag
            "probe": True,  # run a quick probe to harvest capabilities
        }
    ]

    # 2) Instantiate the BulkRegistrar with your gateway URL & admin token.
    gateway_url = "http://localhost:4444"  # or your MCP‑Gateway address
    admin_token = "YOUR_GATEWAY_ADMIN_TOKEN"  # e.g. from .env

    registrar = BulkRegistrar(
        gateway_url=gateway_url,
        token=admin_token,
        concurrency=10,  # adjust parallelism as needed
    )

    # 3) Register (upsert) all sources.
    results = await registrar.register_servers(sources)

    # 4) Examine responses for success / error.
    for res in results:
        print(res)  # typically contains the newly created/updated server record


if __name__ == "__main__":
    asyncio.run(main())
