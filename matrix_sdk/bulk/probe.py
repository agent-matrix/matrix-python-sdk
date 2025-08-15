# matrix_sdk/bulk/probe.py
from __future__ import annotations

from typing import Any, Dict

import httpx


def probe_capabilities(
    manifest: Dict[str, Any], timeout: float = 5.0
) -> Dict[str, Any]:
    """Best-effort capability probe. Non-fatal on errors.

    If endpoint.transport is http/sse and exposes `/capabilities`, try to fetch
    and merge into manifest["capabilities"]. Otherwise return manifest unchanged.
    """
    try:
        endpoint = manifest.get("endpoint", {})
        transport = endpoint.get("transport")
        url = endpoint.get("url")
        if not (transport and url) or transport not in {"http", "sse"}:
            return manifest
        cap_url = str(url).rstrip("/") + "/capabilities"
        with httpx.Client(timeout=timeout) as client:
            r = client.get(cap_url)
            if r.status_code == 200:
                data = r.json()
                caps = data if isinstance(data, list) else data.get("capabilities", [])
                if isinstance(caps, list):
                    existing = manifest.get("capabilities") or []
                    manifest["capabilities"] = sorted({*existing, *caps})
    except Exception:
        return manifest
    return manifest
