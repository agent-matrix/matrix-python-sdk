# matrix_sdk/bulk/gateway_client.py
from typing import Any, Dict, Optional

import httpx

from .schemas import ServerManifest


class GatewayAdminClient:
    """
    Thin async client to interact with MCP-Gateway admin API.
    Provides upsert and other server registry operations.
    """

    def __init__(self, base_url: str, token: Optional[str] = None):
        if not base_url:
            raise ValueError("base_url is required")
        self.base = base_url.rstrip("/")
        self.headers: Dict[str, str] = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def upsert_server(
        self, manifest: ServerManifest, idempotency_key: str
    ) -> Dict[str, Any]:
        """
        POST /admin/servers to register or update a server manifest.
        Requires Idempotency-Key header for deduplication.
        Returns the JSON response.
        """
        url = f"{self.base}/admin/servers"
        hdrs = dict(self.headers)
        hdrs["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=manifest.model_dump(), headers=hdrs)
            resp.raise_for_status()
            return resp.json()
