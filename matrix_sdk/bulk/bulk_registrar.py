# matrix_sdk/bulk/bulk_registrar.py
"""
BulkRegistrar orchestrates discovery, optional probing, idempotency key generation,
and upserts manifests to an MCP-Gateway Admin API with retry/backoff.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Union

from .backoff import with_backoff
from .discovery import discover_manifest
from .gateway_client import GatewayAdminClient
from .probe import probe_capabilities
from .schemas import ServerManifest
from .utils import make_idempotency_key


class BulkRegistrar:
    """
    BulkRegistrar handles bulk registration of MCP servers.

    Usage:
        registrar = BulkRegistrar(gateway_url, token, concurrency=50)
        results = await registrar.register_servers(list_of_sources)
    """

    def __init__(
        self,
        gateway_url: str,
        token: str,
        concurrency: int = 50,
        probe: bool = True,
        backoff_config: Optional[dict[str, Union[int, float]]] = None,
    ) -> None:
        self.client = GatewayAdminClient(gateway_url, token)
        self.sema = asyncio.Semaphore(concurrency)
        self.probe_enabled = probe
        # prepare backoff decorator with provided config or defaults
        bc = backoff_config or {"max_retries": 5, "base_delay": 1.0, "jitter": 0.1}
        self._retry = with_backoff(
            max_retries=int(bc.get("max_retries", 5)),
            base_delay=bc.get("base_delay", 1.0),
            jitter=bc.get("jitter", 0.1),
        )

    async def _process_one(self, source: Dict[str, Any]) -> Any:
        """
        Process a single source dict:
          1. Discover manifest (zip, git, Dockerfile, etc.)
          2. Optionally probe for extra capabilities
          3. Generate idempotency key
          4. Upsert via GatewayAdminClient with retry/backoff
        """
        async with self.sema:
            # Step 1: discovery
            manifest = discover_manifest(source)
            # Step 2: optional probing
            if self.probe_enabled and source.get("probe", True):
                # probe expects raw dict, returns enriched manifest dict
                enriched = probe_capabilities(manifest.model_dump())
                manifest = ServerManifest(**enriched)
            # Step 3: idempotency key
            idem_key = make_idempotency_key(manifest.model_dump())
            # Step 4: upsert with retry
            upsert_fn = self._retry(self.client.upsert_server)
            result = await upsert_fn(manifest, idempotency_key=idem_key)
            return result

    async def register_servers(self, sources: Iterable[Dict[str, Any]]) -> List[Any]:
        """
        Register multiple servers concurrently.

        Returns list of results or exceptions for each source.
        """
        tasks = [self._process_one(src) for src in sources]
        # gather returns exceptions as results when return_exceptions=True
        return await asyncio.gather(*tasks, return_exceptions=True)
