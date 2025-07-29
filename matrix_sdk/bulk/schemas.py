# matrix_sdk/bulk/schemas.py
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl

# Import the canonical EntityType from the main schemas file
# This import is no longer used by ServerManifest but may be used by other models.


class EndpointDescriptor(BaseModel):
    transport: Literal["http", "ws", "sse", "stdio"]
    url: HttpUrl
    auth: Optional[Literal["bearer", "none"]] = "none"
    schema: str


class ServerManifest(BaseModel):
    # This is the corrected line.
    # The field is renamed to `entity_type` to avoid the name clash.
    # `alias="type"` ensures it still serializes/deserializes as "type".
    entity_type: Literal["mcp_server"] = Field("mcp_server", alias="type")

    uid: str
    name: str
    version: str
    summary: Optional[str] = ""
    description: Optional[str]
    providers: List[str] = []
    frameworks: List[str] = []
    capabilities: List[str] = []
    endpoint: EndpointDescriptor
    labels: Optional[Dict[str, str]] = {}
    quality_score: Optional[float] = Field(default=0.0, ge=0.0, le=1.0)
    source_url: HttpUrl
    license: Optional[str]
