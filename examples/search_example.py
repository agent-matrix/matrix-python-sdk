#!/usr/bin/env python3
# examples/search_example.py

import os
from typing import Any, Dict, List, Tuple
from matrix_sdk.client import MatrixClient, MatrixError

BASE = os.getenv("MATRIX_HUB_BASE", "http://127.0.0.1:7300")
TOKEN = os.getenv("MATRIX_HUB_TOKEN")  # optional


def _to_mapping(obj: Any) -> Dict[str, Any]:
    """Best-effort convert Pydantic v2/v1 models (or dicts) into a plain dict."""
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    d = getattr(obj, "dict", None)
    if callable(d):
        try:
            return d()
        except Exception:
            pass
    return {}


def _items_and_total(res: Any) -> Tuple[List[Dict[str, Any]], Any]:
    """
    Return (items, total) for either a dict payload or a Pydantic SearchResponse.
    """
    if isinstance(res, dict):
        return list(res.get("items", [])), res.get("total")
    # Pydantic SearchResponse-like object
    seq = getattr(res, "items", []) or []
    items = [_to_mapping(it) for it in seq]
    total = getattr(res, "total", None)
    return items, total


def pretty_print(query: str, res: Any) -> None:
    items, total = _items_and_total(res)
    print(f"\n=== Query: {query!r}  (total={total}) ===")
    if not items:
        print("(no items)")
        return

    for it in items:
        score = it.get("score_final")
        try:
            score_str = f"{float(score):.3f}" if score is not None else "n/a"
        except Exception:
            score_str = str(score)

        print(
            f"- {it.get('id')}  "
            f"[{it.get('type')}]  "
            f"{it.get('name')} v{it.get('version')}  "
            f"score={score_str}"
        )
        if it.get("manifest_url"):
            print(f"  manifest: {it.get('manifest_url')}")
        if it.get("install_url"):
            print(f"  install : {it.get('install_url')}")
        if it.get("snippet"):
            print(f"  snippet : {it.get('snippet')}")


def do_search(hub: MatrixClient, query: str) -> None:
    try:
        res = hub.search(
            q=query,
            type="any",           # across agents, tools, mcp_server
            mode="hybrid",        # "keyword" | "semantic" | "hybrid"
            limit=5,
            with_snippets=True,   # ask server for snippets if supported
            include_pending=True, # show unregistered entities (useful in dev)
            # with_rag=False,     # safe to add if your Hub supports it
            # rerank="none",
        )
        pretty_print(query, res)
    except MatrixError as e:
        status = getattr(e, "status", None) or getattr(e, "status_code", None) or "?"
        print(f"Search failed for {query!r}: HTTP {status} — {e}")


def main():
    hub = MatrixClient(base_url=BASE, token=TOKEN)
    for q in ("hello", "hello-sse-server","mcp_server:hello-sse-server@0.1.0"):
        do_search(hub, q)


if __name__ == "__main__":
    main()
