# -*- coding: utf-8 -*-
"""
examples/top5_search.py

Tiny usage example showing the public Top-5 search against a Matrix Hub and a
one-click install of the first result, with extra diagnostics so you can trace
why an install might be skipped or fail.

Usage:
    python -m examples.top5_search

Environment variables (optional):
    HUB_URL             : Base URL of your Matrix Hub (default: http://127.0.0.1:7300)
    HUB_TOKEN           : Bearer token, if your Hub requires auth
    HUB_QUERY           : Search query (default: "hello")
    HUB_TYPE            : agent|tool|mcp_server|any (default: any)
    HUB_LIMIT           : Max results (default: 5)
    HUB_INCLUDE_PENDING : true|false (default: true)
    HUB_MODE            : keyword|semantic|hybrid (default: server default)
    HUB_DEBUG           : true|false (default: false) — print extra fields
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from matrix_sdk import MatrixClient, SDKError


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Best-effort convert Pydantic v2/v1 models (or plain objects) to a dict."""
    if isinstance(obj, dict):
        return obj
    # Pydantic v2
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()  # type: ignore[misc]
        except Exception:
            pass
    # Pydantic v1
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        try:
            return as_dict()  # type: ignore[misc]
        except Exception:
            pass
    # Fallback
    return {}


def _normalize_items(items: Any) -> List[Dict[str, Any]]:
    """Convert a list of SearchItem models or dicts into a list of dicts."""
    if not items:
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        out.append(it if isinstance(it, dict) else _to_dict(it))
    return out


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


def _ellipsize(text: Optional[str], n: int = 120) -> str:
    if not text:
        return ""
    t = str(text).strip().replace("\n", " ")
    return t if len(t) <= n else (t[: n - 1] + "…")


def _probe_url(url: str, *, timeout: float = 4.0) -> str:
    """
    Best-effort probe of a manifest URL. Returns a short status string.
    Never raises; intended for diagnostics only.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            # Try HEAD first, fall back to GET if method not allowed
            r = c.head(url, headers={"Accept": "application/json,*/*"})
            if r.status_code in (405, 501):
                r = c.get(url, headers={"Accept": "application/json,*/*"})
            ctype = r.headers.get("content-type", "")
            loc = r.headers.get("location")
            extra = f", content-type={ctype}" if ctype else ""
            if loc:
                extra += f", location={loc}"
            return f"{r.status_code}{extra}"
    except Exception as e:
        return f"error: {e}"


def _search_once(
    hub: MatrixClient,
    *,
    q: str,
    typ: Optional[str],
    limit: int,
    include_pending: Optional[bool],
    mode: Optional[str],
    with_snippets: bool = True,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any] | Any]:
    params: Dict[str, Any] = {
        "q": q,
        "limit": limit,
        "with_snippets": with_snippets,
    }
    if typ and typ not in ("", "any"):
        params["type"] = typ
    if include_pending is not None:
        params["include_pending"] = include_pending
    if mode:
        params["mode"] = mode

    res = hub.search(**params)
    res_dict = _to_dict(res)
    items = _normalize_items(
        res_dict.get("items") if "items" in res_dict else getattr(res, "items", [])
    )
    total = (
        res_dict.get("total")
        if "total" in res_dict
        else getattr(res, "total", len(items))
    )
    return items, int(total or 0), res


def _print_item(i: int, it: Dict[str, Any], debug: bool = False) -> None:
    name = it.get("name") or it.get("id")
    manifest = it.get("manifest_url") or "(no manifest_url)"
    snippet = it.get("snippet") or it.get("summary")
    install_url = it.get("install_url")
    print(f"{i}. {name} — {manifest}")
    if snippet:
        print(f"    ↳ {_ellipsize(snippet, 160)}")
    if debug:
        # Show a few more helpful hints for debugging pipelines
        print(
            "    · id={id}  type={type}  version={ver}".format(
                id=it.get("id"),
                type=it.get("type"),
                ver=it.get("version") or "-",
            )
        )
        if install_url:
            print(f"    · install_url: {install_url}")


def _diagnose_entity(hub: MatrixClient, id_: str, item: Dict[str, Any]) -> None:
    """
    Print helpful diagnostics when installation fails, without throwing.
    """
    print("\nDiagnosis:")
    try:
        ent = hub.entity(id_)
        ed = _to_dict(ent)
    except Exception as e:
        print(f"  • entity({id_}) lookup failed: {e}")
        ed = {}

    source_url = ed.get("source_url")
    # Prefer item.manifest_url if present, else resolve via helper
    item_manifest = item.get("manifest_url")
    resolved_manifest = None
    try:
        resolved_manifest = hub.manifest_url(id_)
    except Exception:
        pass

    print(f"  • source_url: {source_url or '(missing)'}")
    if item_manifest:
        print(f"  • item.manifest_url: {item_manifest}  → probe: {_probe_url(item_manifest)}")
    else:
        print("  • item.manifest_url: (missing)")

    if resolved_manifest and resolved_manifest != item_manifest:
        print(f"  • resolver URL:    {resolved_manifest}  → probe: {_probe_url(resolved_manifest)}")
    elif resolved_manifest:
        print(f"  • resolver URL:    {resolved_manifest}  (same as item.manifest_url)")

    caps = ed.get("capabilities") or []
    arts = ed.get("artifacts") or []
    print(f"  • capabilities: {len(caps)}  artifacts: {len(arts)}")
    if arts:
        kinds = [a.get("kind") for a in arts if isinstance(a, dict)]
        print(f"    - artifact kinds: {', '.join(str(k) for k in kinds if k)}")

    # Quick actionable hint
    if not source_url and not item_manifest:
        print("  • Hint: entity is missing 'source_url' and 'manifest_url'; the Hub cannot fetch a manifest.")
    elif source_url:
        print(f"  • Suggestion: verify the source_url is reachable and returns JSON/YAML: {_probe_url(source_url)}")


def main() -> None:
    base_url = os.getenv("HUB_URL", "http://127.0.0.1:7300")
    token = os.getenv("HUB_TOKEN")
    q = os.getenv("HUB_QUERY", "hello")
    typ = os.getenv("HUB_TYPE", "any")
    limit = int(os.getenv("HUB_LIMIT", "5"))
    include_pending = _bool_env("HUB_INCLUDE_PENDING", True)
    mode = os.getenv("HUB_MODE")  # None → server default
    debug = _bool_env("HUB_DEBUG", False)

    hub = MatrixClient(base_url=base_url, token=token)

    # 1) First attempt with provided knobs
    items, total, res = _search_once(
        hub,
        q=q,
        typ=typ,
        limit=limit,
        include_pending=include_pending,
        mode=mode,
    )

    tried = [(typ, include_pending, mode, total)]

    # 2) Fallbacks if empty
    if total == 0:
        for t_candidate in ("mcp_server", "tool"):
            if typ not in (None, "", "any") and typ == t_candidate:
                continue
            items, total, res = _search_once(
                hub,
                q=q,
                typ=t_candidate,
                limit=limit,
                include_pending=include_pending,
                mode=mode,
            )
            tried.append((t_candidate, include_pending, mode, total))
            if total > 0:
                typ = t_candidate
                break

    if total == 0:
        items, total, res = _search_once(
            hub,
            q=q,
            typ=typ,
            limit=limit,
            include_pending=include_pending,
            mode="keyword",
        )
        tried.append((typ, include_pending, "keyword", total))

    # Print attempts summary
    print(f"Top results for {q!r} (Hub: {base_url}) — total={total}\n")
    for idx, (t, inc, m, tot) in enumerate(tried, 1):
        print(f"  Attempt {idx}: type={t or 'any'}, include_pending={inc}, mode={m or '(default)'} → total={tot}")
    print()

    # Print the items (up to limit)
    for i, it in enumerate(items[:limit], 1):
        _print_item(i, it, debug=debug)

    # Install the first result into a demo runners directory
    if items:
        first = items[0]
        first_id = first.get("id")
        if first_id:
            print(f"\nInstalling first item: {first_id}")
            try:
                out = hub.install(id=first_id, target="./.matrix/runners/demo")
            except SDKError as e:
                # Non-fatal: report and continue with diagnostics
                status = getattr(e, "status", None)
                detail = getattr(e, "detail", None) or str(e)
                print(f"Warning: install skipped (HTTP {status}). {detail}")
                _diagnose_entity(hub, first_id, first)
            else:
                # Print a concise summary if plan/results are present
                out_dict = _to_dict(out)
                results = out_dict.get("results", [])
                if results:
                    steps = [r.get("step") for r in results if isinstance(r, dict)]
                    print("Install steps:", steps)
                else:
                    # Some hubs may return a simple ack
                    print(out_dict or out)
        else:
            print("\nFirst item is missing 'id'; cannot install.")
    else:
        print("No items found. Tip: set HUB_INCLUDE_PENDING=true and/or HUB_TYPE=mcp_server")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except SDKError as e:
        status = getattr(e, "status", None)
        detail = getattr(e, "detail", None) or str(e)
        print(f"SDKError: HTTP {status} — {detail}")
    except Exception as e:
        print(f"Unexpected error: {e}")
