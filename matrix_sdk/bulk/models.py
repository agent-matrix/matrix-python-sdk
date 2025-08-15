# matrix_sdk/bulk/models.py
from __future__ import annotations

import json
from typing import Dict, List, Literal, Optional

# Detect Pydantic major version
try:
    # Pydantic v2
    from pydantic import (
        AnyUrl,
        BaseModel,
        ConfigDict,
        Field,
        field_validator,
        model_validator,
    )

    PYDANTIC_V2 = True
except Exception:  # pragma: no cover
    # Pydantic v1 fallback
    from pydantic import AnyUrl, BaseModel, Field, root_validator, validator  # type: ignore

    PYDANTIC_V2 = False


class EndpointDescriptor(BaseModel):
    """Wire shape expected by gateways (emits `schema` on the wire)."""

    if PYDANTIC_V2:
        model_config = ConfigDict(populate_by_name=True)
    else:

        class Config:  # type: ignore
            allow_population_by_field_name = True

    transport: Literal["http", "ws", "sse", "stdio"]
    url: AnyUrl
    auth: Optional[Literal["bearer", "none"]] = "none"
    wire_schema: str = Field(..., alias="schema")  # emitted as "schema"


class ServerManifest(BaseModel):
    """Normalized server manifest for Gateway Admin API."""

    if PYDANTIC_V2:
        model_config = ConfigDict(populate_by_name=True, extra="ignore")
    else:

        class Config:  # type: ignore
            allow_population_by_field_name = True
            extra = "ignore"

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
    labels: Dict[str, str] = Field(default_factory=dict)
    quality_score: Optional[float] = 0.0
    source_url: Optional[AnyUrl] = None
    license: Optional[str] = None

    # --- validators ---
    if PYDANTIC_V2:

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
                self.uid = (
                    f"{t}:{self.id}{('@' + self.version) if self.version else ''}"
                )
            return self

    else:  # Pydantic v1

        @validator("id")
        def _id_not_empty_v1(cls, v):  # type: ignore
            if not v or not str(v).strip():
                raise ValueError("id is required")
            return v

        @root_validator(pre=False)
        def _compute_uid_if_missing_v1(cls, values):  # type: ignore
            if not values.get("uid"):
                t = values.get("entity_type", "mcp_server")
                ver = values.get("version")
                values["uid"] = f"{t}:{values.get('id')}{('@' + ver) if ver else ''}"
            return values

    # --- helpers ---
    def to_dict(self) -> Dict:
        """Return a dict with aliases but WITHOUT forcing JSON-safe scalars."""
        if PYDANTIC_V2:
            return self.model_dump(by_alias=True, exclude_none=True)
        return self.dict(by_alias=True, exclude_none=True)

    def to_jsonable(self) -> Dict:
        """
        Return a plain-JSON-safe dict (e.g., AnyUrl → str).
        Works on Pydantic v1 and v2.
        """
        if PYDANTIC_V2:
            # v2: ensure coercion to JSON-native types, then parse back
            return json.loads(self.model_dump_json(by_alias=True, exclude_none=True))
        # v1: .json() encodes types; parse back to dict
        return json.loads(self.json(by_alias=True, exclude_none=True))
