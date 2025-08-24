# -*- coding: utf-8 -*-
"""
examples/route_probe.py

Probe all Matrix Hub routes that MatrixClient exposes:
  - search
  - entity
  - manifest resolver (+ optional fetch)
  - install (catalog or inline manifest)
  - remotes list/add/ingest/delete (skipped if not exposed)

This script is defensive: it prints reasons for failures and continues.

Usage:
  python -m examples.route_probe

Environment variables (override as needed):
  HUB_URL        : Hub base URL (default: https://api.matrixhub.io)
  HUB_TOKEN      : Bearer token if required
  HUB_TIMEOUT    : Float seconds for client timeout (default: 20)

  HUB_QUERY      : Search query (default: "hello")
  HUB_TYPE       : agent|tool|mcp_server|any (default: any)
  HUB_LIMIT      : results cap (default: 5)
  HUB_INCLUDE_PENDING : true|false (default: true)
  HUB_MODE       : keyword|semantic|hybrid (default: server default)

  TEST_ID        : Entity id to test /entities and install if search finds none
  MANIFEST_URL   : If set, install uses inline manifest (Hub need not ingest it)
  REMOTE_URL     : A catalog index.json URL to add/remove and ingest
  REMOTE_NAME    : Name for the remote (default auto-generated)
"""
from __future__ import annotations

import ast
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from matrix_sdk import MatrixClient, MatrixError


# ---------------------------- small helpers ----------------------------------

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


def _ok(msg: str) -> None:
    print(f"[OK]  {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _err(msg: str) -> None:
    print(f"[ERR] {msg}")


def _to_dict(obj: Any) -> Dict[str, Any]:
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


def _normalize_items(items: Any) -> List[Dict[str, Any]]:
    if not items:
        return []
    out: List[Dict[str, Any]] = []
    for it in items:
        out.append(it if isinstance(it, dict) else _to_dict(it))
    return out


def _extract_reason(detail: str) -> Optional[str]:
    """
    Try to pull a JSON/dict-ish payload out of a detail string like:
      "POST /catalog/install failed (422) — {'error': {'error': 'InstallError', 'reason': '...'}, ...}"
    and then extract error.reason.
    """
    if not detail:
        return None
    # Most messages contain ' — ' then a repr of dict/json
    if "—" in detail:
        tail = detail.split("—", 1)[-1].strip()
        # Try JSON first
        try:
            payload = json.loads(tail)
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    return err.get("reason") or err.get("error")
        except Exception:
            # Try Python literal dict
            try:
                payload = ast.literal_eval(tail)
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict):
                        return err.get("reason") or err.get("error")
            except Exception:
                return None
    return None


def _load_manifest(url: str, *, timeout: float = 10.0) -> Dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url, headers={"Accept": "application/json,*/*"})
    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text
    if "application/json" in ctype or ctype.endswith("+json"):
        return r.json()
    # Some hosts mislabel content-types; try json anyway
    try:
        return json.loads(text)
    except Exception:
        pass
    # Fallback to YAML if available
    try:
        import yaml  # optional
        return yaml.safe_load(text)
    except Exception as e:
        raise RuntimeError(f"Unsupported or invalid manifest content: {e}")


def _uid_from_manifest(m: Dict[str, Any]) -> str:
    t = m.get("type")
    i = m.get("id")
    v = m.get("version")
    if not t or not i:
        raise RuntimeError("manifest missing 'type' or 'id'")
    return f"{t}:{i}@{v}" if v else f"{t}:{i}"


# ---------------------------- probes -----------------------------------------

def probe_search(client: MatrixClient, *, q: str, typ: Optional[str], limit: int,
                 include_pending: Optional[bool], mode: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    try:
        res = client.search(
            q=q,
            type=typ,
            limit=limit,
            include_pending=include_pending,
            with_snippets=True,
            mode=mode,
        )
        res_dict = _to_dict(res)
        items = _normalize_items(
            res_dict.get("items") if "items" in res_dict else getattr(res, "items", [])
        )
        total = (
            res_dict.get("total")
            if "total" in res_dict
            else getattr(res, "total", len(items))
        )
        _ok(f"search: q={q!r}, type={typ or 'any'} → total={total}, showing {min(limit, len(items))}")
        return items, int(total or 0)
    except MatrixError as e:
        _err(f"search failed (HTTP {e.status}): {e.detail or ''}")
        return [], 0


def probe_entity(client: MatrixClient, id: str) -> Optional[Dict[str, Any]]:
    try:
        ent = client.entity(id)
        ed = _to_dict(ent)
        name = ed.get("name") or "(no name)"
        _ok(f"entity: {id} → name={name!r}")
        return ed
    except MatrixError as e:
        _err(f"entity failed (HTTP {e.status}): {e.detail or ''}")
        return None


def probe_manifest(client: MatrixClient, id: str, fetch: bool = True) -> Optional[str]:
    try:
        url = client.manifest_url(id)
        _ok(f"manifest_url: {id} → {url}")
        if fetch:
            try:
                m = client.fetch_manifest(id)
                md = _to_dict(m)
                keys = sorted(list(md.keys()))
                t = md.get("type"); name = md.get("name")
                _ok(f"fetch_manifest: type={t}, name={name}, keys={keys[:10]}{'…' if len(keys) > 10 else ''}")
                if not t or not name:
                    _warn("fetch_manifest: manifest missing 'type'/'name' — this may be a placeholder, proxy, or non-standard file.")
            except MatrixError as e:
                # Resolver works but fetch might fail (e.g., auth, 404)
                _warn(f"fetch_manifest failed (HTTP {e.status}): {e.detail or ''}")
        return url
    except MatrixError as e:
        _err(f"manifest_url failed (HTTP {e.status}): {e.detail or ''}")
        return None


def probe_install(client: MatrixClient, *, id: Optional[str], manifest_url: Optional[str], target: str) -> None:
    if manifest_url:
        # Inline install path (robust even when Hub hasn't ingested)
        try:
            m = _load_manifest(manifest_url)
            uid = _uid_from_manifest(m)
            _ok(f"inline manifest loaded; uid={uid}")
            out = client.install(id=uid, target=target, manifest=m, source_url=manifest_url)
            od = _to_dict(out)
            steps = [r.get("step") for r in od.get("results", []) if isinstance(r, dict)]
            _ok(f"install (inline) succeeded; steps={steps or '[]'}")
        except (RuntimeError, MatrixError) as e:
            if isinstance(e, MatrixError):
                reason = _extract_reason(e.detail or "") or e.detail or "install failed"
                _warn(f"install (inline) failed (HTTP {e.status}): {reason}")
            else:
                _warn(f"install (inline) failed: {e}")
        return

    # Normal catalog install (requires Hub to know the entity's source_url)
    if not id:
        _warn("install skipped: no id and no MANIFEST_URL provided")
        return
    try:
        out = client.install(id=id, target=target)
        od = _to_dict(out)
        steps = [r.get("step") for r in od.get("results", []) if isinstance(r, dict)]
        _ok(f"install (catalog) succeeded; steps={steps or '[]'}")
    except MatrixError as e:
        reason = _extract_reason(e.detail or "") or e.detail or "install failed"
        _warn(f"install (catalog) failed (HTTP {e.status}): {reason}")


def probe_remotes_and_ingest(base_client: MatrixClient, *, remote_url: str, remote_name: Optional[str]) -> None:
    """
    Try remotes; if endpoints are not exposed on this Hub (404/405), skip the whole section gracefully.
    """
    name = remote_name or f"probe-{uuid.uuid4().hex[:8]}"

    # list
    try:
        lr = base_client.list_remotes()
        total = len(lr) if isinstance(lr, list) else (len(lr.get("items", [])) if isinstance(lr, dict) else 0)
        _ok(f"remotes list → {total} remotes")
    except MatrixError as e:
        if e.status in (404, 405):
            _warn(f"remotes endpoints not exposed on this Hub (HTTP {e.status}); skipping remotes/add/ingest/delete probes.")
            return
        _err(f"remotes list failed (HTTP {e.status}): {e.detail or ''}")
        return

    # add
    try:
        base_client.add_remote(remote_url, name=name)
        _ok(f"add_remote '{name}' ok")
    except MatrixError as e:
        _err(f"add_remote failed (HTTP {e.status}): {e.detail or ''}")
        return

    # trigger ingest (best-effort)
    try:
        base_client.trigger_ingest(name)
        _ok(f"trigger_ingest '{name}' ok")
    except MatrixError as e:
        _warn(f"trigger_ingest failed for '{name}' (HTTP {e.status}): {e.detail or ''}")

    # small pause; some hubs enqueue ingest
    time.sleep(0.25)

    # delete
    try:
        base_client.delete_remote(remote_url)
        _ok(f"delete_remote '{name}' ok (by URL)")
    except MatrixError as e:
        _warn(f"delete_remote by URL failed (HTTP {e.status}); Hub may require deletion by name. Skipping cleanup.")


# ---------------------------- main -------------------------------------------

def main() -> int:
    hub_url = os.getenv("HUB_URL", "https://api.matrixhub.io")
    token = os.getenv("HUB_TOKEN")
    timeout = float(os.getenv("HUB_TIMEOUT", "20"))

    q = os.getenv("HUB_QUERY", "hello")
    typ = os.getenv("HUB_TYPE", "any")
    limit = int(os.getenv("HUB_LIMIT", "5"))
    include_pending = _bool_env("HUB_INCLUDE_PENDING", True)
    mode = os.getenv("HUB_MODE") or None

    test_id = os.getenv("TEST_ID")  # optional id for entity/install
    manifest_url = os.getenv("MANIFEST_URL")  # optional inline install
    remote_url = os.getenv(
        "REMOTE_URL",
        # hello-mcp index.json (public example)
        "https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/index.json",
    )
    remote_name = os.getenv("REMOTE_NAME")  # optional

    print("── Route probe configuration ─────────────────────────────")
    print(f" HUB_URL            : {hub_url}")
    print(f" HUB_TIMEOUT        : {timeout}")
    print(f" HUB_QUERY          : {q}")
    print(f" HUB_TYPE           : {typ}")
    print(f" HUB_LIMIT          : {limit}")
    print(f" HUB_INCLUDE_PENDING: {include_pending}")
    print(f" HUB_MODE           : {mode or '(default)'}")
    print(f" TEST_ID            : {test_id or '(auto from search)'}")
    print(f" MANIFEST_URL       : {manifest_url or '(none)'}")
    print(f" REMOTE_URL         : {remote_url}")
    print(f" REMOTE_NAME        : {remote_name or '(auto)'}")
    print("──────────────────────────────────────────────────────────\n")

    # Note: current MatrixClient ctor doesn't accept `routes=`; use as-is.
    client = MatrixClient(base_url=hub_url, token=token, timeout=timeout)

    # 1) search
    items, total = probe_search(
        client,
        q=q,
        typ=typ,
        limit=limit,
        include_pending=include_pending,
        mode=mode,
    )

    # choose id
    picked_id = test_id
    if not picked_id and items:
        picked_id = items[0].get("id")

    # 2) entity
    if picked_id:
        probe_entity(client, picked_id)
    else:
        _warn("entity probe skipped (no id)")

    # 3) manifest resolver + optional fetch
    if picked_id:
        probe_manifest(client, picked_id, fetch=True)
    else:
        _warn("manifest probe skipped (no id)")

    # 4) install (inline if MANIFEST_URL set, else catalog path)
    probe_install(client, id=picked_id, manifest_url=manifest_url, target="./.matrix/runners/probe")

    # 5) remotes + ingest + delete (skip if not exposed)
    probe_remotes_and_ingest(client, remote_url=remote_url, remote_name=remote_name)

    print("\nProbe complete.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
