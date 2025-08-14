# -*- coding: utf-8 -*-
"""
examples/bulk_register_example.py

Bulk-register MCP servers with the Gateway Admin API.

Discovery:
  1) If a `matrix/` folder exists, load `index.json` and/or `*.manifest.json`.
  2) Else, fall back to `[tool.mcp_server]` in `pyproject.toml`.

Env (override as needed):
  GATEWAY_URL="http://localhost:4444"
  ADMIN_TOKEN="..."            # or GATEWAY_TOKEN / GATEWAY_ADMIN_TOKEN
  ZIP_PATH="/abs/path/to/repo.zip"
  GIT_URL="https://github.com/ruslanmv/hello-mcp"
  GIT_REF="main"
  CONCURRENCY="20"
  PROBE="true"                 # set false to skip capability probing
  ENV_FILE=".env.local"        # optional path to env file to load first
  RETRIES="5"                  # backoff retries

Usage:
  python -m examples.bulk_register_example
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

# --- refactored registrar pieces (matrix-first, pydantic v1/v2 safe) ---
from matrix_sdk.bulk.backoff import with_backoff
from matrix_sdk.bulk.discovery import discover_manifests_from_source
from matrix_sdk.bulk.gateway import GatewayAdminClient
from matrix_sdk.bulk.probe import probe_capabilities
from matrix_sdk.bulk.utils import load_env_file, make_idempotency_key


# ------------------------------- helpers ------------------------------------ #

def _load_env_file(path: str | None) -> None:
    # keep compatibility with your original helper, delegate to lib
    load_env_file(path)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _mk_sources() -> List[Dict[str, Any]]:
    """
    Prefer ZIP if provided; otherwise fall back to git.
    Supports:
      - {"kind":"zip","path":...}
      - {"kind":"git","url":...,"ref":...}
    """
    zip_path = os.getenv("ZIP_PATH")
    git_url = os.getenv("GIT_URL", "https://github.com/ruslanmv/hello-mcp")
    git_ref = os.getenv("GIT_REF", "main")

    sources: List[Dict[str, Any]] = []

    if zip_path and Path(zip_path).is_file():
        sources.append({
            "kind": "zip",
            "path": str(Path(zip_path).resolve()),
            "probe": _bool_env("PROBE", True),
        })
    else:
        sources.append({
            "kind": "git",
            "url": git_url,
            "ref": git_ref,
            "probe": _bool_env("PROBE", True),
        })

    return sources


def _fmt_error(e: Exception) -> str:
    # Short, friendly error summaries
    try:
        import subprocess  # type: ignore
    except Exception:
        subprocess = None  # type: ignore

    if subprocess and isinstance(e, subprocess.CalledProcessError):
        cmd = " ".join(map(str, getattr(e, "cmd", []) or ["git"]))
        rc = getattr(e, "returncode", "?")
        return f"git command failed ({rc}): {cmd}"

    try:
        import httpx  # type: ignore
    except Exception:
        httpx = None  # type: ignore

    if httpx and isinstance(e, httpx.HTTPStatusError):
        resp = e.response
        code = resp.status_code if resp is not None else "?"
        body = None
        try:
            body = resp.json() if resp is not None else None
        except Exception:
            body = resp.text if resp is not None else None
        hint = " (set ADMIN_TOKEN?)" if code in (401, 403) else ""
        return f"http {code}: {e}{hint} — {body!r}"

    if httpx and isinstance(e, httpx.HTTPError):
        return f"http error: {e}"

    try:
        from pydantic import ValidationError  # type: ignore
    except Exception:
        ValidationError = None  # type: ignore

    if ValidationError and isinstance(e, ValidationError):
        try:
            errs = e.errors()
            if errs:
                loc = ".".join(map(str, errs[0].get("loc", [])))
                msg = errs[0].get("msg", "validation error")
                return f"validation error at '{loc}': {msg}"
        except Exception:
            pass
        return "validation error"

    if isinstance(e, FileNotFoundError):
        return f"file not found: {e.filename or e}"
    if e.__class__.__name__ == "BadZipFile":
        return "invalid zip file (BadZipFile)"

    return str(e)


def _fmt_result_for_json(x: Any) -> Any:
    if isinstance(x, Exception):
        return {"error": _fmt_error(x)}
    return x


# ------------------------------- core logic --------------------------------- #

async def _register_one_manifest(client: GatewayAdminClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upsert a single manifest with idempotency + retries.
    """
    idem = make_idempotency_key(payload)
    retries = int(os.getenv("RETRIES", "5"))
    upsert = with_backoff(max_retries=retries, base_delay=1.0, jitter=0.2)(client.upsert_server)
    return await upsert(payload, idempotency_key=idem)


async def _process_source(
    client: GatewayAdminClient,
    source: Dict[str, Any],
    *,
    probe_enabled: bool
) -> List[Any]:
    """
    Discover manifests from a source and register them (concurrently per source).
    Matrix-first discovery: prefer matrix/ manifests; else pyproject.toml.
    """
    manifests = discover_manifests_from_source(source)
    if not manifests:
        return [{"warning": "no manifests discovered", "source": source}]

    tasks: List["asyncio.Task[Any]"] = []
    for m in manifests:
        payload = m.to_jsonable()  # pydantic v1/v2 safe dict (no .model_dump_json(mode=...))
        if probe_enabled and source.get("probe", True):
            payload = probe_capabilities(payload)
        tasks.append(asyncio.create_task(_register_one_manifest(client, payload)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    # normalize exceptions to dicts for JSON printing
    fixed: List[Any] = []
    for r in results:
        fixed.append({"error": _fmt_error(r)} if isinstance(r, Exception) else r)
    return fixed


# -------------------------------- main flow --------------------------------- #

async def _run() -> int:
    # Optional: load a simple .env file first (default: ".env.local")
    _load_env_file(os.getenv("ENV_FILE", ".env.local"))

    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:4444")
    token = (
        os.getenv("ADMIN_TOKEN")
        or os.getenv("GATEWAY_TOKEN")
        or os.getenv("GATEWAY_ADMIN_TOKEN")
        or ""
    )
    concurrency = int(os.getenv("CONCURRENCY", "20"))
    sources = _mk_sources()
    probe = _bool_env("PROBE", True)

    # Intro banner
    print(f"Gateway URL : {gateway_url}\n")
    if token:
        tshow = token[:5] + "…" + token[-3:] if len(token) > 10 else "(set)"
        print(f"Admin token : {tshow} (len={len(token)})\n")
    else:
        print("Admin token : (unset)\n")
    print(f"ZIP_PATH     : {os.getenv('ZIP_PATH','(unset)')}")
    print(f"GIT_URL      : {os.getenv('GIT_URL','') or 'https://github.com/ruslanmv/hello-mcp'}")
    print(f"GIT_REF      : {os.getenv('GIT_REF','main')}")
    print(f"CONCURRENCY  : {concurrency}")
    print(f"PROBE        : {probe}\n")

    print(f"Gateway URL : {gateway_url}")
    print(f"Concurrency : {concurrency}")
    print(f"Probe       : {probe}")
    print("Sources     :")
    try:
        print(json.dumps(sources, indent=2))
    except Exception:
        print(str(sources))
    if not token:
        print("Hint: ADMIN_TOKEN not set — real gateways will likely reject requests (401/403).")

    if sources and sources[0].get("kind") == "git":
        print("Hint: To avoid git/network issues, set ZIP_PATH to a local archive of the repo.\n")

    client = GatewayAdminClient(base_url=gateway_url, token=token)
    sema = asyncio.Semaphore(concurrency)

    async def worker(src: Dict[str, Any]) -> List[Any]:
        async with sema:
            return await _process_source(client, src, probe_enabled=probe)

    print("\nStarting bulk registration…\n")
    grouped = await asyncio.gather(*(worker(s) for s in sources), return_exceptions=True)

    # Flatten + summarize
    results: List[Any] = []
    for g in grouped:
        if isinstance(g, Exception):
            results.append({"error": _fmt_error(g)})
        else:
            results.extend(g)

    ok = sum(1 for r in results if not (isinstance(r, dict) and r.get("error")))
    fail = len(results) - ok
    print(f"\nSummary: {ok}/{ok + fail} succeeded, {fail} failed.\n")

    if fail:
        print("Errors:")
        for i, r in enumerate(results, 1):
            if isinstance(r, dict) and r.get("error"):
                print(f"  - [{i}] {r['error']}")
        print()

    print("Raw results (truncated per item):")
    for i, r in enumerate(results, 1):
        try:
            print(f"[{i}] {json.dumps(r, default=str)[:200]}")
        except Exception:
            print(f"[{i}] {r}")

    print("\nResults JSON:")
    print(json.dumps(results, indent=2))

    return 0 if fail == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        raise SystemExit(130)