# -*- coding: utf-8 -*-
"""
examples/install_from_manifest.py

Install a component directly from a manifest URL (no prior ingest required).

Default manifest:
  https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/hello-server.manifest.json

Usage:
  python -m examples.install_from_manifest

Environment variables (override as needed):
  HUB_URL       : Matrix Hub base URL (default: http://127.0.0.1:7300)
  HUB_TOKEN     : Bearer token (if Hub requires auth)
  MANIFEST_URL  : URL to a JSON (or YAML) manifest
  TARGET        : Where to install locally (default: ./.matrix/runners/hello-mcp)
  ALIAS         : Optional alias persisted by your own tooling (Hub may ignore)
  DEBUG         : true|false (prints extra detail)
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, Optional

import httpx

from matrix_sdk import MatrixClient, SDKError


# ---------------------------- helpers ---------------------------- #

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


def _to_jsonable(obj: Any) -> Any:
    """Best-effort conversion of Pydantic v2/v1 models (or arbitrary objects) to plain JSON-serializable types."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    # Pydantic v2
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return _to_jsonable(dump())  # type: ignore[misc]
        except Exception:
            pass
    # Pydantic v1
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        try:
            return _to_jsonable(as_dict())  # type: ignore[misc]
        except Exception:
            pass
    # Fallback
    return repr(obj)


def _load_manifest(url: str, *, timeout: float = 12.0, debug: bool = False) -> Dict[str, Any]:
    """
    Fetch and parse a manifest from `url`. Prefers JSON; tolerates mislabeled content-types.
    If JSON parsing fails, attempts YAML (if PyYAML present). Raises RuntimeError on failure.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url, headers={"Accept": "application/json,*/*"})
    except Exception as e:
        raise RuntimeError(f"Failed to GET manifest: {e}")

    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text

    # JSON by header
    if "application/json" in ctype or ctype.endswith("+json"):
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"Invalid JSON manifest: {e}")

    # Try JSON anyway (mislabelled content-type)
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try YAML as a fallback
    try:
        import yaml  # optional dependency
        return yaml.safe_load(text)
    except Exception as e:
        if debug:
            print(f"[DEBUG] YAML parse failed: {e}")
        raise RuntimeError("Unsupported or invalid manifest content")


def _compose_uid(manifest: Dict[str, Any]) -> str:
    """
    Compose a catalog uid string from a manifest:
      "<type>:<id>@<version>"
    """
    t = manifest.get("type")
    i = manifest.get("id")
    v = manifest.get("version")
    if not t or not i:
        raise RuntimeError("Manifest missing required 'type' and/or 'id'")
    if not v:
        # Hub generally expects a version; keep graceful fallback if absent.
        return f"{t}:{i}"
    return f"{t}:{i}@{v}"


def _try_load_with_fallbacks(url: str, *, timeout: float, debug: bool) -> Dict[str, Any]:
    """
    Attempt to load the manifest from `url`. If it 404s and matches the 'refs/heads/main'
    pattern, retry with the simpler '/main/' path variant.
    """
    # First attempt
    try:
        return _load_manifest(url, timeout=timeout, debug=debug)
    except RuntimeError as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            # Try a common raw.githubusercontent fallback (refs/heads/main → /main/)
            if "refs/heads/main" in url:
                alt = url.replace("refs/heads/main", "main")
                if debug:
                    print(f"[WARN] HTTP 404 when fetching manifest: {url}")
                    print(f"[DEBUG] Retrying with alternate URL: {alt}")
                try:
                    m = _load_manifest(alt, timeout=timeout, debug=debug)
                    print(f"[OK]  fetched manifest: {alt}")
                    return m
                except RuntimeError as e2:
                    raise RuntimeError(f"Manifest fetch failed for both URLs:\n  1) {url}\n  2) {alt}\n  last error: {e2}") from e2
        # Not a recognized fallback scenario; re-raise
        raise


# ------------------------------ main ------------------------------ #

def main() -> int:
    base_url = os.getenv("HUB_URL", "http://127.0.0.1:7300")
    token = os.getenv("HUB_TOKEN")
    manifest_url = os.getenv(
        "MANIFEST_URL",
        "https://raw.githubusercontent.com/ruslanmv/hello-mcp/refs/heads/main/matrix/hello-server.manifest.json",
    )
    target = os.getenv("TARGET", "./.matrix/runners/hello-mcp")
    alias = os.getenv("ALIAS", None)
    debug = _bool_env("DEBUG", False)

    print(f"Hub        : {base_url}")
    print(f"Manifest   : {manifest_url}")
    print(f"Target     : {target}")
    if alias:
        print(f"Alias      : {alias}")
    print()

    # 1) Fetch the manifest (with smart fallback for common GitHub raw URLs)
    try:
        manifest = _try_load_with_fallbacks(manifest_url, timeout=12.0, debug=debug)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 2

    # Small manifest summary
    mtype = manifest.get("type")
    mid   = manifest.get("id")
    mver  = manifest.get("version")
    keys  = sorted(list(manifest.keys()))
    print(f"[OK]  manifest keys: {keys[:10]}{' …' if len(keys) > 10 else ''}")
    print(f"[OK]  manifest type/id/version: {mtype!r}/{mid!r}/{mver!r}")

    if debug:
        try:
            print("\nManifest (truncated pretty-print):")
            print(json.dumps(manifest, indent=2)[:1600] + ("\n" if len(json.dumps(manifest)) > 1600 else ""))
        except Exception:
            print("\n(manifest could not be pretty-printed; non-JSON?)")

    # 2) Compose the uid for install
    try:
        uid = _compose_uid(manifest)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 2

    print(f"\nComposed uid: {uid}")

    # 3) Call the Hub to install using inline manifest
    hub = MatrixClient(base_url=base_url, token=token)

    print("Installing (inline manifest)…")
    try:
        result = hub.install(
            id=uid,
            target=target,
            alias=alias,
            manifest=manifest,       # <- direct inline manifest
            source_url=manifest_url  # <- provenance: where we got it from
        )
    except SDKError as e:
        status = getattr(e, "status", None)
        detail = getattr(e, "detail", None) or str(e)

        # Try to extract server's error.reason if present (don’t crash the pipeline)
        reason: Optional[str] = None
        body = getattr(e, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                reason = err.get("reason") or err.get("error")

        print(f"\nInstall failed (HTTP {status}). {reason or detail}")
        if debug and body:
            print("Server body:")
            try:
                print(json.dumps(body, indent=2))
            except Exception:
                print(str(body))
        print("\nTip: ensure the manifest URL is reachable by the Hub and contains a valid JSON/YAML manifest.")
        return 3

    # 4) Print a concise, robust summary (works with dicts or Pydantic models)
    res = _to_jsonable(result)
    if not isinstance(res, dict):
        # Unexpected but tolerate
        print("\nInstall completed (non-dict result):")
        print(res)
    else:
        files = res.get("files_written") or []
        results = res.get("results", [])
        print("\nInstall OK")
        if results:
            print("Install steps:")
            for r in results:
                if not isinstance(r, dict):
                    continue
                step = r.get("step")
                ok = r.get("ok")
                extra = r.get("extra") or {}
                line = f" - {step}: {'ok' if ok else 'FAILED'}"
                # Surface gateway registration note if present
                if step == "gateway.register" and extra.get("gateway_error"):
                    line += f" (gateway_error: {extra['gateway_error']})"
                print(line)
        if files:
            print("Files:", files)

    # 5) Optional tiny delay (some adapters write async artifacts)
    time.sleep(0.25)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
