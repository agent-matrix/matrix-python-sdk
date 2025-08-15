# matrix_sdk/bulk/cli.py
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List

from .backoff import with_backoff
from .discovery import discover_manifests_from_source
from .gateway import GatewayAdminClient
from .probe import probe_capabilities
from .utils import load_env_file, make_idempotency_key


async def _register_source(
    client: GatewayAdminClient, source: Dict[str, Any], *, probe: bool
) -> List[Any]:
    manifests = discover_manifests_from_source(source)
    results: List[Any] = []

    for m in manifests:
        payload = m.to_jsonable()
        if probe:
            payload = probe_capabilities(payload)
        idem = make_idempotency_key(payload)
        upsert = with_backoff()(client.upsert_server)
        try:
            res = await upsert(payload, idempotency_key=idem)
            results.append(res)
        except Exception as e:  # capture and continue
            results.append({"error": str(e)})
    return results


async def _run(args: argparse.Namespace) -> int:
    load_env_file(args.env_file or os.getenv("ENV_FILE"))

    base = args.gateway_url or os.getenv("GATEWAY_URL", "http://localhost:4444")
    token = (
        args.token
        or os.getenv("ADMIN_TOKEN")
        or os.getenv("GATEWAY_TOKEN")
        or os.getenv("GATEWAY_ADMIN_TOKEN")
    )
    probe = not args.no_probe and (
        os.getenv("PROBE", "true").lower() in {"1", "true", "yes", "on"}
    )

    sources: List[Dict[str, Any]] = []
    if args.zip:
        sources.append({"kind": "zip", "path": args.zip})
    elif args.dir:
        sources.append({"kind": "dir", "path": args.dir})
    else:
        url = args.git or os.getenv("GIT_URL")
        ref = args.ref or os.getenv("GIT_REF", "main")
        if not url:
            raise SystemExit("Provide --zip, --dir, or --git URL")
        sources.append({"kind": "git", "url": url, "ref": ref})

    client = GatewayAdminClient(base_url=base, token=token)

    # Concurrency control
    sema = asyncio.Semaphore(args.concurrency)

    async def worker(src: Dict[str, Any]):
        async with sema:
            return await _register_source(client, src, probe=probe)

    tasks = [worker(src) for src in sources]
    grouped = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten results
    results: List[Any] = []
    for g in grouped:
        if isinstance(g, Exception):
            results.append({"error": str(g)})
        else:
            results.extend(g)

    ok = sum(1 for r in results if not (isinstance(r, dict) and r.get("error")))
    fail = len(results) - ok

    print(
        json.dumps({"summary": {"ok": ok, "fail": fail}, "results": results}, indent=2)
    )
    return 0 if fail == 0 else 2


def run_cli(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover and register MCP servers (matrix-first)"
    )
    parser.add_argument("--zip", help="Path to local ZIP archive")
    parser.add_argument("--dir", help="Path to local directory (already checked out)")
    parser.add_argument("--git", help="Git repo URL")
    parser.add_argument("--ref", default="main", help="Git ref (branch/tag)")

    parser.add_argument(
        "--gateway-url", default=os.getenv("GATEWAY_URL", "http://localhost:4444")
    )
    parser.add_argument("--token", help="Admin token (Bearer)")
    parser.add_argument(
        "--concurrency", type=int, default=int(os.getenv("CONCURRENCY", "10"))
    )
    parser.add_argument(
        "--no-probe", action="store_true", help="Disable capability probing"
    )
    parser.add_argument("--env-file", help="Optional .env file to load first")

    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(run_cli())
