# -*- coding: utf-8 -*-
"""
Schemas for the Matrix Hub Python SDK.

These models mirror the API responses exposed by Matrix Hub so callers can work
with fully-typed objects. Pydantic v2 is used (see pyproject.toml constraints).

Notes:
- `EntityDetail` is intentionally permissive (`extra='allow'`) to carry full
  manifest payloads and future server-side fields without breaking the client.
- All timestamps are optional and parsed into `datetime` if present.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EntityType",
    "SearchItem",
    "SearchResponse",
    "EntityDetail",
    "InstallStepResult",
    "InstallOutcome",
    "MatrixAPIError",
]

# --------------------------------------------------------------------------- #
# Common aliases                                                              #
# --------------------------------------------------------------------------- #

EntityType = Literal["agent", "tool", "mcp_server"]


# --------------------------------------------------------------------------- #
# Search models                                                               #
# --------------------------------------------------------------------------- #
class SearchItem(BaseModel):
    """
    One row in /catalog/search results.
    """

    id: str = Field(
        ..., description="Fully-qualified entity id (e.g., agent:name@1.2.3)"
    )
    type: EntityType
    name: str
    version: str
    summary: str = Field(default="", description="Short description or summary")
    score_lexical: float = Field(ge=0.0, le=1.0, default=0.0)
    score_semantic: float = Field(ge=0.0, le=1.0, default=0.0)
    score_final: float = Field(ge=0.0, le=1.0, default=0.0)
    capabilities: List[str] = Field(default_factory=list)
    frameworks: List[str] = Field(default_factory=list)
    providers: List[str] = Field(default_factory=list)
    fit_reason: Optional[str] = Field(
        default=None,
        description="Short natural-language justification for why this result fits the query.",
    )

    # --- New optional convenience fields (non-breaking, ignored if not present) ---
    manifest_url: Optional[str] = Field(
        default=None,
        description="Direct URL to the entity manifest (if provided by the server).",
    )
    install_url: Optional[str] = Field(
        default=None,
        description="Convenience link for one-click install flow (if provided by the server).",
    )
    snippet: Optional[str] = Field(
        default=None,
        description="Short snippet derived from summary/description when with_snippets=true.",
    )


class SearchResponse(BaseModel):
    """
    Top-level response for GET /catalog/search
    """

    items: List[SearchItem] = Field(default_factory=list)
    total: int = 0


# --------------------------------------------------------------------------- #
# Entity detail                                                               #
# --------------------------------------------------------------------------- #
class EntityDetail(BaseModel):
    """
    Full entity detail as returned by GET /catalog/entities/{id}.

    Includes:
      - Manifest fields: schema_version, type, id, name, version, description, license, etc.
      - Computed/normalized fields: capabilities, artifacts, adapters, mcp_registration,
        compatibility, provenance, created_at, updated_at.

    The model allows extra fields so the server can evolve without breaking the client.
    """

    model_config = ConfigDict(extra="allow")

    # Core manifest-ish fields (optional to be robust to partial payloads)
    schema_version: Optional[int] = None
    type: Optional[EntityType] = None
    id: Optional[str] = None
    name: Optional[str] = None
    version: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    license: Optional[str] = None
    homepage: Optional[str] = None
    source_url: Optional[str] = None
    readme: Optional[str] = None

    # Computed / normalized
    capabilities: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list)
    adapters: List[Dict[str, Any]] = Field(default_factory=list)
    mcp_registration: Optional[Dict[str, Any]] = None
    compatibility: Optional[Dict[str, Any]] = None
    provenance: Optional[Dict[str, Any]] = None

    # Timestamps
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Install results                                                             #
# --------------------------------------------------------------------------- #
class InstallStepResult(BaseModel):
    """
    One step in the installation result (pip, docker pull, git clone, adapters.write, etc.).
    """

    step: str
    ok: bool
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    elapsed_secs: float = 0.0
    extra: Optional[Dict[str, Any]] = None


class InstallOutcome(BaseModel):
    """
    Full response for POST /catalog/install
    """

    plan: Dict[str, Any] = Field(default_factory=dict)
    results: List[InstallStepResult] = Field(default_factory=list)
    files_written: List[str] = Field(default_factory=list)
    lockfile: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Optional error type                                                         #
# --------------------------------------------------------------------------- #
class MatrixAPIError(RuntimeError):
    """
    SDK-visible error type. Tolerates both keyword-only and positional forms.

    Accepted forms:
      - MatrixAPIError("message", *, status_code=..., body=...)
      - MatrixAPIError(status_code, detail)
      - MatrixAPIError(status_code, detail, body)
    """

    def __init__(
        self,
        *args,
        status_code: Optional[int] = None,
        body: Any = None,
        detail: Optional[str] = None,
        **kwargs,
    ) -> None:
        # Back-compat argument parsing
        sc = status_code
        b = body
        det = detail
        msg: str

        if len(args) == 0:
            # No message provided; synthesize a generic one
            msg = f"HTTP {sc}" if sc is not None else "Matrix API error"

        elif len(args) == 1:
            # MatrixAPIError("message", ...)
            msg = str(args[0])

        elif len(args) == 2 and isinstance(args[0], int):
            # MatrixAPIError(status_code, detail)
            sc = args[0]
            det = str(args[1]) if args[1] is not None else None
            msg = f"HTTP {sc}: {det or ''}".rstrip()
            # Keep a minimal body if not supplied explicitly
            if b is None and det is not None:
                b = {"detail": det}

        elif len(args) >= 3 and isinstance(args[0], int):
            # MatrixAPIError(status_code, detail, body)
            sc = args[0]
            det = str(args[1]) if args[1] is not None else None
            b = args[2]
            msg = f"HTTP {sc}: {det or ''}".rstrip()

        else:
            # Fallback: treat first arg as message
            msg = str(args[0])

        self.status_code = sc
        # Prefer explicit body; otherwise stash the textual detail for callers
        self.body = (
            b if b is not None else ({"detail": det} if det is not None else None)
        )

        super().__init__(msg)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"MatrixAPIError(status_code={self.status_code}, message={self.args[0]!r})"
        )
