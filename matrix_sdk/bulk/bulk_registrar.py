# matrix_sdk/bulk/bulk_registrar.py
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Iterable, List, Optional, Union

from pydantic import BaseModel

from .discovery import discover_manifests_from_source  # matrix-first discovery
from .gateway import GatewayAdminClient  # <-- fixed import
from .models import ServerManifest  # <-- fixed import

try:
    from .probe import probe_capabilities
except Exception:

    def probe_capabilities(manifest: Dict[str, Any]) -> Dict[str, Any]:
        return manifest


from .backoff import with_backoff
from .utils import make_idempotency_key

_DEBUG = os.getenv("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _jsonable(obj: Union[ServerManifest, BaseModel, Dict[str, Any]]) -> Dict[str, Any]:
    """Return a JSON-safe dict (AnyUrl→str) for any supported manifest-ish object."""
    if isinstance(obj, ServerManifest):
        return obj.to_jsonable()
    if isinstance(obj, dict):
        # ensure JSON-serializable scalars
        return json.loads(json.dumps(obj, default=str))
    # Pydantic v2 or v1 models
    try:
        return json.loads(obj.model_dump_json(by_alias=True, exclude_none=True))  # type: ignore[attr-defined]
    except Exception:
        return json.loads(obj.json(by_alias=True, exclude_none=True))  # type: ignore[attr-defined]


class BulkRegistrar:
    """
    Discover manifests from sources (zip/dir/git) and register them with the gateway.
    - matrix-first discovery: matrix/ (index.json, *.manifest.json), else pyproject.toml
    - optional capability probing
    - concurrent upserts with retries + idempotency
    """

    def __init__(
        self,
        gateway_url: str,
        token: str,
        concurrency: int = 50,
        probe: bool = True,
        backoff_config: Optional[dict] = None,
    ) -> None:
        self.client = GatewayAdminClient(gateway_url, token)
        self.sema = asyncio.Semaphore(concurrency)
        self.probe_enabled = probe
        bc = backoff_config or {"max_retries": 5, "base_delay": 1.0, "jitter": 0.1}
        self._retry = with_backoff(
            max_retries=int(bc.get("max_retries", 5)),
            base_delay=float(bc.get("base_delay", 1.0)),
            jitter=float(bc.get("jitter", 0.1)),
        )

    async def _register_manifest(self, payload: Dict[str, Any]) -> Any:
        """Validate payload, compute idempotency key, then upsert with retry."""
        # Validate into canonical schema to enforce shape
        manifest = ServerManifest.model_validate(payload)
        json_payload = manifest.to_jsonable()  # JSON-safe dict

        idem_key = make_idempotency_key(json_payload)

        if _DEBUG:
            print("[DEBUG] Prepared payload:")
            print(json.dumps(json_payload, indent=2))

        upsert = self._retry(self.client.upsert_server)
        return await upsert(json_payload, idempotency_key=idem_key)

    async def _process_source(self, source: Dict[str, Any]) -> List[Any]:
        """Discover 0..N manifests from a single source and upsert them."""
        manifests = discover_manifests_from_source(source)
        if not manifests:
            return [
                {
                    "warning": "no manifests discovered",
                    "source": {k: v for k, v in source.items() if k != "token"},
                }
            ]

        tasks: List["asyncio.Task[Any]"] = []
        for m in manifests:
            payload = m.to_jsonable()
            if self.probe_enabled and source.get("probe", True):
                payload = probe_capabilities(payload)
            tasks.append(asyncio.create_task(self._register_manifest(payload)))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Normalize exceptions to printable dicts
        fixed: List[Any] = []
        for r in results:
            if isinstance(r, Exception):
                fixed.append({"error": str(r)})
            else:
                fixed.append(r)
        return fixed

    async def register_servers(self, sources: Iterable[Dict[str, Any]]) -> List[Any]:
        """
        Process each source (zip/dir/git). Returns a flat list of results across all sources.
        """
        tasks = []
        for src in sources:
            # Bound overall concurrency across sources
            async def _worker(s: Dict[str, Any]) -> List[Any]:
                async with self.sema:
                    return await self._process_source(s)

            tasks.append(asyncio.create_task(_worker(src)))

        grouped = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[Any] = []
        for g in grouped:
            if isinstance(g, Exception):
                results.append({"error": str(g)})
            else:
                results.extend(g)
        return results
