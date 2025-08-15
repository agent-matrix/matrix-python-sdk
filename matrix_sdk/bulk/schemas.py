# matrix_sdk/bulk/schemas.py
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class EndpointDescriptor(BaseModel):
    """Wire shape expected by gateways, with 'schema' on the wire."""

    model_config = ConfigDict(populate_by_name=True)

    transport: Literal["http", "ws", "sse", "stdio"]
    url: AnyUrl
    auth: Optional[Literal["bearer", "none"]] = "none"
    wire_schema: str = Field(..., alias="schema")  # emits as "schema"


class ServerManifest(BaseModel):
    """Normalized server manifest for Gateway Admin API."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    entity_type: Literal["mcp_server"] = Field("mcp_server", alias="type")

    id: str
    uid: Optional[str] = None

    name: str
    version: Optional[str] = None

    summary: Optional[str] = ""
    description: Optional[str] = None
    providers: List[str] = Field(default_factory=list)
    frameworks: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)
    endpoint: EndpointDescriptor
    labels: Optional[Dict[str, str]] = Field(default_factory=dict)
    quality_score: Optional[float] = Field(default=0.0, ge=0.0, le=1.0)
    source_url: Optional[AnyUrl] = None
    license: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("id is required")
        return v

    @model_validator(mode="after")
    def _compute_uid_if_missing(self) -> "ServerManifest":
        if not self.uid:
            t = self.entity_type
            self.uid = f"{t}:{self.id}{('@' + self.version) if self.version else ''}"
        return self
