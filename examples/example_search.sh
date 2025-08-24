#!/usr/bin/env bash
# examples/example_search.sh
# Tiny driver that uses matrix_sdk.search to test search() across modes.

set -euo pipefail

HUB_URL="${HUB_URL:-https://api.matrixhub.io}"
HUB_TOKEN="${HUB_TOKEN:-}"
QUERY="${QUERY:-hello}"
TYPE="${TYPE:-any}"                  # any|agent|tool|mcp_server
LIMIT="${LIMIT:-5}"
INCLUDE_PENDING="${INCLUDE_PENDING:-true}"
WITH_SNIPPETS="${WITH_SNIPPETS:-true}"
WITH_RAG="${WITH_RAG:-false}"
RERANK="${RERANK:-none}"
MODES="${MODES:-default,hybrid,keyword,semantic}"

echo "── Search helper example ─────────────────────────────"
echo " HUB_URL         : $HUB_URL"
echo " QUERY           : $QUERY"
echo " TYPE            : $TYPE"
echo " LIMIT           : $LIMIT"
echo " INCLUDE_PENDING : $INCLUDE_PENDING"
echo " WITH_SNIPPETS   : $WITH_SNIPPETS"
echo " WITH_RAG        : $WITH_RAG"
echo " RERANK          : $RERANK"
echo " MODES           : $MODES"
echo "──────────────────────────────────────────────────────"
echo

python - <<'PY'
import json
import os
from typing import Any, Dict

from matrix_sdk import MatrixClient, MatrixError
from matrix_sdk.search import search, search_try_modes, SearchOptions

HUB_URL = os.getenv("HUB_URL", "https://api.matrixhub.io")
HUB_TOKEN = os.getenv("HUB_TOKEN") or None
QUERY = os.getenv("QUERY", "hello")
TYPE = os.getenv("TYPE", "any")
LIMIT = int(os.getenv("LIMIT", "5"))
INCLUDE_PENDING = (os.getenv("INCLUDE_PENDING", "true").strip().lower() in ("1","true","yes","on"))
WITH_SNIPPETS = (os.getenv("WITH_SNIPPETS", "true").strip().lower() in ("1","true","yes","on"))
WITH_RAG = (os.getenv("WITH_RAG", "false").strip().lower() in ("1","true","yes","on"))
RERANK = os.getenv("RERANK", "none")
MODES = [m.strip() for m in os.getenv("MODES", "default,hybrid,keyword,semantic").split(",") if m.strip()]

def _to_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()  # pydantic v2
        except Exception:
            pass
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        try:
            return as_dict()  # pydantic v1
        except Exception:
            pass
    return {}

def _print_items(label: str, body_like: Any, limit: int = 5) -> None:
    body = _to_mapping(body_like)
    total = int(body.get("total", 0))
    items = body.get("items", []) or []
    print(f"[{label}] total={total}, showing={min(len(items), limit)}")
    for i, it in enumerate(items[:limit], 1):
        if isinstance(it, dict):
            name = it.get("name") or it.get("id") or "(unnamed)"
            mid = it.get("id")
            murl = it.get("manifest_url")
        else:
            id_ = getattr(it, "id", None)
            name = getattr(it, "name", None) or id_ or "(unnamed)"
            mid = id_
            murl = getattr(it, "manifest_url", None)
        print(f"  {i}. {name} [{mid or '-'}] — {murl or '(no manifest_url)'}")
    print()

def main() -> int:
    client = MatrixClient(base_url=HUB_URL, token=HUB_TOKEN)

    common_kwargs = dict(
        type=TYPE,
        limit=LIMIT,
        with_snippets=WITH_SNIPPETS,
        with_rag=WITH_RAG,
        rerank=RERANK,
        include_pending=INCLUDE_PENDING,
    )

    # 1) Default mode (omit mode)
    if "default" in MODES:
        print(f"→ default search (server decides mode)…")
        try:
            out = search(client, QUERY, **common_kwargs)
            _print_items("default", out, limit=LIMIT)
        except MatrixError as e:
            print(f"[WARN] default search failed (HTTP {getattr(e, 'status', 'n/a')}): {getattr(e, 'detail', str(e))}")
            body = getattr(e, 'body', None)
            if body:
                try:
                    print("  server body:", json.dumps(body, indent=2)[:400])
                except Exception:
                    print("  server body:", str(body)[:400])
            print()

    # 2) Explicit modes
    for m in MODES:
        if m == "default":
            continue
        print(f"→ mode={m}…")
        try:
            out = search(client, QUERY, mode=m, **common_kwargs)
            _print_items(m, out, limit=LIMIT)
        except MatrixError as e:
            print(f"[WARN] mode={m} failed (HTTP {getattr(e, 'status', 'n/a')}): {getattr(e, 'detail', str(e))}")
            body = getattr(e, 'body', None)
            if body:
                try:
                    print("  server body:", json.dumps(body, indent=2)[:400])
                except Exception:
                    print("  server body:", str(body)[:400])
            print()

    # 3) Generator: no fallback; just prints totals per mode
    print("→ search_try_modes (no per-call fallback):")
    try:
        for mode, res in search_try_modes(
            client,
            QUERY,
            modes=[m for m in MODES if m != "default"],
            type=TYPE,
            limit=LIMIT,
            with_snippets=WITH_SNIPPETS,
            with_rag=WITH_RAG,
            rerank=RERANK,
            include_pending=INCLUDE_PENDING,
        ):
            body = _to_mapping(res)
            print(f"  mode={mode}: total={int(body.get('total', 0))}")
        print()
    except MatrixError as e:
        print(f"[WARN] search_try_modes failed (HTTP {getattr(e, 'status', 'n/a')}): {getattr(e, 'detail', str(e))}")
        body = getattr(e, 'body', None)
        if body:
            try:
                print("  server body:", json.dumps(body, indent=2)[:400])
            except Exception:
                print("  server body:", str(body)[:400])
        print()

    print("Done.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
PY
