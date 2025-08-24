#!/usr/bin/env bash
# examples/example_search_json.sh
# Emit JSON-only output (NDJSON) for interoperability.
# Each line is a JSON object with: { "mode": "...", "query": "...", "params": {...}, "response": {...} }

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
MODES_RAW="${MODES:-default,hybrid,keyword,semantic}"

# Normalize modes into an array
IFS=',' read -r -a MODES <<<"$MODES_RAW"

python - <<'PY'
import json, os, sys
from typing import Any, Dict

from matrix_sdk import MatrixClient, MatrixError
from matrix_sdk.search import search, search_try_modes, SearchOptions

def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None: return default
    s = raw.strip().lower()
    if s in ("1","true","yes","on"): return True
    if s in ("0","false","no","off"): return False
    return default

def _to_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    # pydantic v2
    md = getattr(obj, "model_dump", None)
    if callable(md):
        try:
            return md()
        except Exception:
            pass
    # pydantic v1
    dct = getattr(obj, "dict", None)
    if callable(dct):
        try:
            return dct()
        except Exception:
            pass
    return {}

HUB_URL       = os.getenv("HUB_URL", "https://api.matrixhub.io")
HUB_TOKEN     = os.getenv("HUB_TOKEN") or None
QUERY         = os.getenv("QUERY", "hello")
TYPE          = os.getenv("TYPE", "any")
LIMIT         = int(os.getenv("LIMIT", "1"))
INCLUDE_PENDING = _bool_env("INCLUDE_PENDING", True)
WITH_SNIPPETS   = _bool_env("WITH_SNIPPETS", True)
WITH_RAG        = _bool_env("WITH_RAG", False)
RERANK          = os.getenv("RERANK", "none")
MODES           = [m.strip() for m in os.getenv("MODES", "default,hybrid,keyword,semantic").split(",") if m.strip()]

client = MatrixClient(base_url=HUB_URL, token=HUB_TOKEN)

common_kwargs = dict(
    type=TYPE,
    limit=LIMIT,
    with_snippets=WITH_SNIPPETS,
    with_rag=WITH_RAG,
    rerank=RERANK,
    include_pending=INCLUDE_PENDING,
)

def emit(obj: Dict[str, Any]) -> None:
    # NDJSON line; ensure ASCII-safe & non-pretty to be compact
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

# (1) default (server chooses mode)
if "default" in MODES:
    try:
        res = search(client, QUERY, **common_kwargs)
        emit({
            "mode": "default",
            "query": QUERY,
            "params": common_kwargs,
            "response": _to_mapping(res),
        })
    except MatrixError as e:
        emit({
            "mode": "default",
            "query": QUERY,
            "params": common_kwargs,
            "error": {"status": getattr(e, "status", None), "detail": getattr(e, "detail", str(e)), "body": getattr(e, "body", None)},
        })

# (2) explicit modes
for m in MODES:
    if m == "default":
        continue
    try:
        res = search(client, QUERY, mode=m, **common_kwargs)
        emit({
            "mode": m,
            "query": QUERY,
            "params": dict(common_kwargs, **{"mode": m}),
            "response": _to_mapping(res),
        })
    except MatrixError as e:
        emit({
            "mode": m,
            "query": QUERY,
            "params": dict(common_kwargs, **{"mode": m}),
            "error": {"status": getattr(e, "status", None), "detail": getattr(e, "detail", str(e)), "body": getattr(e, "body", None)},
        })

# (3) generator (no per-call fallback)
modes_no_default = [m for m in MODES if m != "default"]
if modes_no_default:
    try:
        for mode, res in search_try_modes(
            client,
            QUERY,
            modes=modes_no_default,
            **common_kwargs,
        ):
            emit({
                "mode": f"try:{mode}",
                "query": QUERY,
                "params": dict(common_kwargs, **{"mode": mode}),
                "response": _to_mapping(res),
            })
    except MatrixError as e:
        emit({
            "mode": "try_modes",
            "query": QUERY,
            "params": {"modes": modes_no_default, **common_kwargs},
            "error": {"status": getattr(e, "status", None), "detail": getattr(e, "detail", str(e)), "body": getattr(e, "body", None)},
        })
PY
