# examples/import_docling_mcp.py
# -*- coding: utf-8 -*-
"""
Import (upsert) the docling-mcp server into an MCP Gateway via the BulkRegistrar.

Usage:
  python -m examples.import_docling_mcp

Environment variables (optional):
  MATRIX_GATEWAY_URL  or GATEWAY_URL   : e.g., http://localhost:4444
  MATRIX_ADMIN_TOKEN  or ADMIN_TOKEN   : admin token for the gateway
  GATEWAY_TOKEN / GATEWAY_ADMIN_TOKEN  : alternates for the token
  CONCURRENCY                          : integer parallelism (default 10)
  PROBE                                : true|false (default true)
  DOC_MCP_GIT_URL                      : repo URL (default https://github.com/IBM/docling-mcp.git)
  DOC_MCP_GIT_REF                      : branch/tag (default main)
  DOC_MCP_ZIP_PATH                     : local zip path (preferred over git if present)
  DOC_MCP_DIR_PATH                     : local directory path (preferred over git if no zip)
  DOTENV_FILE                          : path to .env (default: ./.env.local)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from matrix_sdk.bulk.bulk_registrar import BulkRegistrar


# --------------------------- helpers ---------------------------

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


def _load_env(path: str | None) -> None:
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.lower().startswith("export "):
                s = s[7:].strip()
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        print(f"Loaded env: {p.resolve()}")
    except Exception:
        pass  # non-fatal


def _fmt_error(e: Exception) -> str:
    try:
        import httpx  # type: ignore
    except Exception:
        httpx = None  # type: ignore

    if httpx and isinstance(e, httpx.HTTPStatusError):
        resp = e.response
        code = resp.status_code if resp is not None else "?"
        try:
            body = resp.json()
        except Exception:
            body = resp.text if resp is not None else None
        rid = resp.headers.get("x-request-id") if resp is not None else None
        return f"http {code} (request_id={rid}): {body}"

    return str(e)


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return obj
    # pydantic v2
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(by_alias=True, exclude_none=True)
        except Exception:
            pass
    # pydantic v1
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        try:
            return as_dict(by_alias=True, exclude_none=True)
        except Exception:
            pass
    return str(obj)


def _build_sources() -> List[Dict[str, Any]]:
    """Prefer ZIP, then DIR, else Git."""
    zip_path = os.getenv("DOC_MCP_ZIP_PATH")
    dir_path = os.getenv("DOC_MCP_DIR_PATH")
    git_url = os.getenv("DOC_MCP_GIT_URL", "https://github.com/IBM/docling-mcp.git")
    git_ref = os.getenv("DOC_MCP_GIT_REF", "main")
    probe = _bool_env("PROBE", True)

    if zip_path and Path(zip_path).is_file():
        return [{"kind": "zip", "path": str(Path(zip_path).resolve()), "probe": probe}]
    if dir_path and Path(dir_path).is_dir():
        return [{"kind": "dir", "path": str(Path(dir_path).resolve()), "probe": probe}]
    return [{"kind": "git", "url": git_url, "ref": git_ref, "probe": probe}]


# ----------------------------- main ----------------------------

async def main() -> int:
    # Load .env first
    dotenv = os.getenv("DOTENV_FILE", ".env.local")
    _load_env(dotenv)

    sources = _build_sources()

    gateway_url = (
        os.getenv("MATRIX_GATEWAY_URL")
        or os.getenv("GATEWAY_URL")
        or "http://localhost:4444"
    )
    admin_token = (
        os.getenv("MATRIX_ADMIN_TOKEN")
        or os.getenv("ADMIN_TOKEN")
        or os.getenv("GATEWAY_ADMIN_TOKEN")
        or os.getenv("GATEWAY_TOKEN")
        or ""
    )
    concurrency = int(os.getenv("CONCURRENCY", "10"))
    probe = sources[0].get("probe", True)

    print("── Bulk register docling-mcp ───────────────────────────")
    print(f" Gateway URL : {gateway_url}")
    print(f" Token set   : {'yes' if admin_token else 'no'}")
    print(f" Concurrency : {concurrency}")
    print(f" Probe       : {probe}")
    print(" Sources     :")
    for s in sources:
        desc = s.get("path") or (f"{s.get('url')}@{s.get('ref')}" if s.get("url") else "?")
        print(f"  - {s['kind']}: {desc}")
    print("────────────────────────────────────────────────────────")

    registrar = BulkRegistrar(
        gateway_url=gateway_url,
        token=admin_token,
        concurrency=concurrency,
        probe=probe,
    )

    results = await registrar.register_servers(sources)

    ok = 0
    errors = 0
    print("\nResults:")
    for i, res in enumerate(results, 1):
        if isinstance(res, Exception):
            errors += 1
            print(f" {i}. ERROR: {_fmt_error(res)}")
        else:
            ok += 1
            try:
                print(f" {i}. OK:")
                print(json.dumps(_to_jsonable(res), indent=2))
            except Exception:
                print(f" {i}. OK: {res}")

    print("\nSummary:")
    print(f"  Success: {ok}")
    print(f"  Errors : {errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
