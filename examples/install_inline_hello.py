# -*- coding: utf-8 -*-
"""
examples/install_inline_hello.py

Inline-install a single MCP server by fetching its manifest from GitHub Raw
and passing it directly to Matrix Hub. This does NOT require /catalog/remotes.

Usage:
    python -m examples.install_inline_hello

Env (optional):
    HUB_URL      : default https://api.matrixhub.io
    HUB_TOKEN    : bearer token if your Hub requires auth
    MANIFEST_URL : override the raw manifest URL to fetch
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from matrix_sdk import MatrixClient, MatrixError

HUB_URL = os.getenv("HUB_URL", "https://api.matrixhub.io")
HUB_TOKEN = os.getenv("HUB_TOKEN")

# Canonical (correct) GitHub Raw URL (no `refs/heads`)
DEFAULT_RAW = (
    "https://raw.githubusercontent.com/ruslanmv/hello-mcp/main/"
    "matrix/hello-sse-server.manifest.json"
)

# Allow override; if not provided, try a small set of candidates
OVERRIDE = os.getenv("MANIFEST_URL")
CANDIDATES = [
    OVERRIDE or DEFAULT_RAW,
    # Alternate file name fallback
    "https://raw.githubusercontent.com/ruslanmv/hello-mcp/main/matrix/hello-server.manifest.json",
    # Legacy (wrong) style kept as a last-ditch fallback in case a mirror served it
    "https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/hello-sse-server.manifest.json",
    "https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/hello-server.manifest.json",
]


def fetch_json(url: str, *, timeout: float = 15.0) -> Dict[str, Any]:
    """GET JSON from URL with simple headers and timeout."""
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        payload = r.read().decode("utf-8")
    try:
        return json.loads(payload)
    except Exception as e:  # malformed or non-json
        raise ValueError(f"Malformed JSON from {url}: {e}")


def first_manifest(urls: list[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Try candidates in order; return (chosen_url, manifest_dict) or (None, None)."""
    for u in urls:
        try:
            m = fetch_json(u)
            print(f"[OK]  fetched manifest: {u}")
            return u, m
        except HTTPError as e:
            print(f"[WARN] HTTP {e.code} when fetching manifest: {u}")
        except URLError as e:
            print(f"[WARN] Network error for manifest {u}: {getattr(e, 'reason', e)}")
        except ValueError as e:
            # JSON parse error
            print(f"[WARN] {e}")
        except Exception as e:
            print(f"[WARN] Unexpected error for {u}: {e}")
    return None, None


def entity_id_from_manifest(m: Dict[str, Any]) -> Optional[str]:
    t = m.get("type")
    ident = m.get("id")
    ver = m.get("version")
    if t and ident and ver:
        return f"{t}:{ident}@{ver}"
    return None


def _to_jsonable(obj: Any) -> Any:
    """
    Best-effort conversion to something json.dumps can handle.
    Handles Pydantic v2 (model_dump), v1 (dict), lists, dicts, and primitives.
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    # Pydantic v2
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return _to_jsonable(obj.model_dump())
        except Exception:
            pass
    # Pydantic v1
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        try:
            return _to_jsonable(obj.dict())
        except Exception:
            pass
    # Fallback: string repr
    return repr(obj)


def main() -> None:
    print(f"Hub: {HUB_URL}")
    client = MatrixClient(base_url=HUB_URL, token=HUB_TOKEN)

    chosen_url, manifest = first_manifest(CANDIDATES)
    if not manifest:
        print(
            "\n[ERROR] Could not fetch a valid manifest from any candidate URL.\n"
            "Hints:\n"
            f"  • Try setting MANIFEST_URL explicitly to a working raw URL.\n"
            f"  • Expected default: {DEFAULT_RAW}\n"
        )
        raise SystemExit(2)

    # Derive entity id if possible; otherwise use the known one
    eid = entity_id_from_manifest(manifest) or "mcp_server:hello-sse-server@0.1.0"
    print(f"Entity id: {eid}")

    try:
        out = client.install(
            id=eid,
            target="./.matrix/runners/hello-sse",
            manifest=manifest,
            source_url=chosen_url or DEFAULT_RAW,
        )
        print("\nInstall OK")
        print(json.dumps(_to_jsonable(out), indent=2, ensure_ascii=False))
    except MatrixError as e:
        # Keep the pipeline; report why install was skipped/failed
        detail = e.detail or ""
        print(
            f"\n[WARN] install failed (HTTP {e.status}): {detail}\n"
            "This often happens if the Hub rejects inline manifests or requires different artifact handling.\n"
            "You can still continue working; nothing else was halted."
        )


if __name__ == "__main__":  # pragma: no cover
    main()
