# matrix_sdk/deep_link.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional
from urllib.parse import parse_qs, unquote, urlsplit

# Import from the real client module (not .hub)
from .client import MatrixClient  # existing types


class InvalidMatrixUri(ValueError):
    """Raised for malformed or unsupported matrix:// URIs."""


_ALIAS_RX = r"^[a-z0-9](?:[a-z0-9._-]{0,63})$"
_FORBID_ID_CHARS = {"/", "\\", "\n", "\r", "\t"}


@dataclass(frozen=True)
class DeepLink:
    """
    Canonical parse of a matrix:// deep link.

    Currently only action='install' is supported.
    """

    action: Literal["install"]
    id: str
    alias: Optional[str] = None


@dataclass(frozen=True)
class HandleResult:
    """
    Result of handling a deep link. SDK performs the Hub call; the caller (CLI/UI)
    decides if/how to persist alias state.
    """

    id: str
    target: str
    response: Dict[str, Any]  # Hub /catalog/install payload


def parse(url: str) -> DeepLink:
    """
    Parse and validate a matrix:// URL.
    Accepts 'matrix://install?...' and defensively also 'matrix:/install?...'.

    Required:
      • scheme == 'matrix'
      • action == 'install'
      • query contains id=<type:name@version> (percent-decoded)
    Optional:
      • alias=<friendly_name> (must match ^[a-z0-9][a-z0-9._-]{0,63}$)

    Raises:
      • InvalidMatrixUri on any validation error.
    """
    u = urlsplit(url)
    if u.scheme.lower() != "matrix":
        raise InvalidMatrixUri("invalid scheme; expected matrix://")

    action = (u.netloc or u.path.lstrip("/")).lower()
    if action != "install":
        raise InvalidMatrixUri(
            "unsupported action (only matrix://install is supported)"
        )

    qs = parse_qs(u.query, keep_blank_values=False, strict_parsing=False)
    raw_id = (qs.get("id") or [None])[0]
    raw_alias = (qs.get("alias") or [None])[0]

    mid = unquote(raw_id) if raw_id else None
    alias = unquote(raw_alias) if raw_alias else None

    if not mid:
        raise InvalidMatrixUri("matrix://install requires ?id=<type:name@version>")

    # Guardrails; server remains the ultimate validator
    if any(ch in mid for ch in _FORBID_ID_CHARS):
        raise InvalidMatrixUri("invalid characters in id")
    if len(mid) > 256:
        raise InvalidMatrixUri("id too long")

    if alias and not re.match(_ALIAS_RX, alias):
        raise InvalidMatrixUri("alias must match [a-z0-9][a-z0-9._-]{0,63}")

    return DeepLink(action="install", id=mid, alias=alias)


def handle_install(
    url: str,
    client: MatrixClient,
    *,
    target: str,
) -> HandleResult:
    """
    Parse a matrix://install URL and perform the Hub install call.

    The SDK does NOT persist aliases or decide filesystem layout. The caller (CLI/UI)
    must compute a target directory and pass it here.

    Args:
      url:    matrix://install?id=...&alias=...
      client: configured MatrixClient
      target: absolute or relative install directory path (caller-defined)

    Returns:
      HandleResult with id, target, and raw Hub 'install' response (plan/results/etc).

    Raises:
      InvalidMatrixUri on parse/validation error.
      MatrixError on any non-2xx from the Hub.
    """
    dl = parse(url)
    resp = client.install(dl.id, target=target)
    return HandleResult(id=dl.id, target=target, response=resp)
