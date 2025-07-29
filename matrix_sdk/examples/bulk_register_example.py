# matrix_python_sdk/examples/bulk_register_example.py
import asyncio
import json

from matrix_sdk.bulk.bulk_registrar import BulkRegistrar


async def main():
    sources = [
        {"kind": "git", "url": "https://github.com/IBM/docling-mcp", "ref": "main"},
        {"kind": "zip", "path": "/tmp/docling-mcp-main.zip", "probe": True},
    ]
    reg = BulkRegistrar("http://localhost:4444", "supersecret", concurrency=20)
    out = await reg.register_servers(sources)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
