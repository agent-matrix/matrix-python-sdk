#!/usr/bin/env bash
# Simple helper to install a Matrix manifest.
#
# It will try to add a remote & trigger ingest IF requested,
# but will gracefully fallback to an INLINE INSTALL when those endpoints
# are not exposed (404/405).
#
# You can override defaults via environment variables before running:
#
#   HUB_URL="http://127.0.0.1:7300" \
#   HUB_TOKEN="..." \
#   # Either point to an index.json...
#   INDEX_URL="https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/index.json" \
#   # ...or directly to a manifest (takes precedence if set):
#   MANIFEST_URL="https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/hello-server.manifest.json" \
#   # Optional entity id (if omitted, derived from manifest):
#   ENTITY_ID="mcp_server:hello-sse-server@0.1.0" \
#   # Where to install:
#   TARGET="./.matrix/runners/hello-sse" \
#   # Try remotes add/ingest first (defaults to 0 / disabled)
#   DO_REMOTE=1 \
#   bash examples/install_from_manifest.sh
#
# Notes:
# - If MANIFEST_URL is provided, it's used directly for inline install.
# - If only INDEX_URL is provided and looks like an index.json, the script
#   will fetch it and pick the first manifest entry.
# - This script never exits non-zero due to missing remotes; it will still
#   inline-install when possible.

set -euo pipefail

export HUB_URL="${HUB_URL:-http://127.0.0.1:7300}"
export HUB_TOKEN="${HUB_TOKEN:-}"
export INDEX_URL="${INDEX_URL:-https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/index.json}"
export MANIFEST_URL="${MANIFEST_URL:-}"
export ENTITY_ID="${ENTITY_ID:-}"  # if empty, we’ll derive from manifest
export TARGET="${TARGET:-./.matrix/runners/hello-sse}"
export REMOTE_NAME="${REMOTE_NAME:-hello-mcp}"
export DO_REMOTE="${DO_REMOTE:-0}"  # 1 to attempt remotes endpoints

python - <<'PY'
from __future__ import annotations
import json, os, sys, urllib.request, urllib.error
from typing import Any, Dict, Optional
from matrix_sdk import MatrixClient, SDKError

HUB_URL    = os.getenv("HUB_URL", "http://127.0.0.1:7300")
HUB_TOKEN  = os.getenv("HUB_TOKEN") or None
INDEX_URL  = os.getenv("INDEX_URL") or ""
MANIFEST_URL_ENV = os.getenv("MANIFEST_URL") or ""
ENTITY_ID  = os.getenv("ENTITY_ID") or ""
TARGET     = os.getenv("TARGET") or "./.matrix/runners/hello-sse"
REMOTE_NAME= os.getenv("REMOTE_NAME") or "hello-mcp"
DO_REMOTE  = os.getenv("DO_REMOTE", "0") in ("1","true","yes","on")

print(f"Hub: {HUB_URL}")
print(f"Index URL   : {INDEX_URL or '(none)'}")
print(f"Manifest URL: {MANIFEST_URL_ENV or '(auto)'}")
print(f"Remote name : {REMOTE_NAME}")
print(f"Target      : {TARGET}")
print(f"Try remotes : {'yes' if DO_REMOTE else 'no'}")

client = MatrixClient(base_url=HUB_URL, token=HUB_TOKEN)

def http_get_json(url: str) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent":"matrix-sdk/route-probe"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = (r.headers.get("content-type") or "").lower()
            # Best-effort JSON
            data = r.read().decode("utf-8", errors="replace")
            return json.loads(data)
    except urllib.error.HTTPError as e:
        print(f"[WARN] HTTP {e.code} when fetching {url}", flush=True)
        return None
    except Exception as e:
        print(f"[WARN] fetch failed for {url}: {e}", flush=True)
        return None

def find_manifest_url() -> Optional[str]:
    # 1) explicit manifest URL
    if MANIFEST_URL_ENV:
        return MANIFEST_URL_ENV

    # 2) If INDEX_URL looks like an index.json, try to load and pick a manifest entry
    if INDEX_URL and "index.json" in INDEX_URL:
        idx = http_get_json(INDEX_URL)
        if isinstance(idx, dict):
            # Accept common shapes:
            #   {"manifests":["..."]} OR {"items":[{"manifest_url":"..."}]} OR {"entries":[{"manifest_url":"..."}]}
            if "manifests" in idx and isinstance(idx["manifests"], list) and idx["manifests"]:
                if isinstance(idx["manifests"][0], str):
                    return idx["manifests"][0]
            if "items" in idx and isinstance(idx["items"], list) and idx["items"]:
                first = idx["items"][0]
                if isinstance(first, dict) and "manifest_url" in first:
                    return first["manifest_url"]
            if "entries" in idx and isinstance(idx["entries"], list) and idx["entries"]:
                first = idx["entries"][0]
                # allow {"base_url": "...","path":"..."} or direct "manifest_url"
                if isinstance(first, dict):
                    if "manifest_url" in first:
                        return first["manifest_url"]
                    base = first.get("base_url")
                    path = first.get("path")
                    if base and path:
                        return base.rstrip("/") + "/" + path.lstrip("/")
        # If index load failed, fall through and maybe INDEX_URL is already a raw manifest.
    # 3) If INDEX_URL is actually a manifest URL, just use it
    if INDEX_URL and INDEX_URL.endswith(".json"):
        # Quick sanity: is it a manifest (has "type"/"id")? Not required, but try:
        maybe = http_get_json(INDEX_URL)
        if maybe is not None and any(k in maybe for k in ("type","name","mcp_registration","artifacts")):
            return INDEX_URL
    return None

def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str,int,float,bool)): return obj
    if isinstance(obj, list): return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict): return {k: to_jsonable(v) for k,v in obj.items()}
    # pydantic v2
    md = getattr(obj, "model_dump", None)
    if callable(md):
        try: return to_jsonable(md())
        except Exception: pass
    # pydantic v1
    d = getattr(obj, "dict", None)
    if callable(d):
        try: return to_jsonable(d())
        except Exception: pass
    return repr(obj)

# ----------------------- Optional: remotes add/ingest ------------------------
if DO_REMOTE:
    try:
        print("Adding remote…")
        resp = client.add_remote(INDEX_URL, name=REMOTE_NAME)
        print(to_jsonable(resp))
    except SDKError as e:
        print(f"Warning: add_remote failed (HTTP {getattr(e,'status','n/a')}): {getattr(e,'detail',str(e))}")
    except Exception as e:
        print(f"Warning: add_remote raised: {e}")

    try:
        print("Triggering ingest…")
        resp = client.trigger_ingest(REMOTE_NAME)
        print(to_jsonable(resp))
    except SDKError as e:
        print(f"Warning: trigger_ingest failed (HTTP {getattr(e,'status','n/a')}): {getattr(e,'detail',str(e))}")
    except Exception as e:
        print(f"Warning: trigger_ingest raised: {e}")

# ----------------------- Inline install path --------------------------------
murl = find_manifest_url()
if not murl:
    print("[ERROR] Could not determine a manifest URL (set MANIFEST_URL or provide a valid INDEX_URL).")
    sys.exit(0)  # do not fail pipelines per your requirement

manifest = http_get_json(murl)
if not manifest:
    print(f"[ERROR] Failed to fetch manifest from: {murl}")
    sys.exit(0)

# Derive entity id if not provided
entity_id = ENTITY_ID
if not entity_id:
    t = manifest.get("type") or "mcp_server"
    ident = manifest.get("id") or "unknown"
    ver = manifest.get("version") or "0.0.0"
    entity_id = f"{t}:{ident}@{ver}"

print(f"Manifest URL: {murl}")
print(f"Entity id   : {entity_id}")

try:
    out = client.install(
        id=entity_id,
        target=TARGET,
        manifest=manifest,
        source_url=murl,
    )
    print("Install OK")
    outj = to_jsonable(out)
    # concise summary
    results = outj.get("results", []) if isinstance(outj, dict) else []
    if results:
        print("Install steps:")
        for r in results:
            step = r.get("step"); ok = r.get("ok")
            extra = r.get("extra") or {}
            line = f" - {step}: {'ok' if ok else 'FAILED'}"
            if step == "gateway.register" and extra.get("gateway_error"):
                line += f" (gateway_error: {extra['gateway_error']})"
            print(line)
    else:
        print(json.dumps(outj, indent=2))
except SDKError as e:
    # Non-fatal – print reason and continue
    reason = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            reason = err.get("reason") or err.get("error")
    msg = reason or getattr(e, "detail", str(e)) or "install failed"
    print(f"Warning: inline install failed (HTTP {getattr(e,'status','0')}): {msg}")

print("Done.")
PY
