# SPDX-License-Identifier: MIT
# matrix_sdk/search.py
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from .client import MatrixClient, MatrixError
from .schemas import SearchResponse

__all__ = ["search", "search_try_modes", "SearchOptions"]

_log = logging.getLogger("matrix_sdk.search")


def _maybe_configure_logging() -> None:
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on") and not _log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(
            logging.Formatter("[matrix-sdk][search] %(levelname)s: %(message)s")
        )
        _log.addHandler(h)
        _log.setLevel(logging.DEBUG)


_maybe_configure_logging()


def _csv(v: Optional[Iterable[str] | str]) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().strip(",")
        return s or None
    items = [str(x).strip() for x in v if str(x).strip()]
    return ",".join(items) if items else None


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def _to_mapping(obj: Any) -> Dict[str, Any]:
    """
    Normalize either a dict or a Pydantic v2/v1 model into a plain dict.
    """
    if isinstance(obj, dict):
        return obj
    # Pydantic v2
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()  # type: ignore[misc]
        except Exception:
            pass
    # Pydantic v1
    as_dict = getattr(obj, "dict", None)
    if callable(as_dict):
        try:
            return as_dict()  # type: ignore[misc]
        except Exception:
            pass
    # Fallback minimal
    return {}


@dataclass(frozen=True)
class SearchOptions:
    allow_fallback: bool = True
    fallback_order: Tuple[str, ...] | None = None
    max_attempts: int = 2
    backoff_base: float = 0.1
    as_model: bool = False


def _do_request(
    client: MatrixClient,
    params: Dict[str, Any],
    *,
    max_attempts: int,
    backoff_base: float,
) -> Any:
    """
    Perform client.search(...) with retry on transient (>=500) errors.
    Returns whatever the client returns (dict or Pydantic model).
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return client.search(**params)  # type: ignore[arg-type]
        except MatrixError as e:
            # support both MatrixError.status and MatrixError.status_code
            sc = getattr(e, "status", None)
            if sc is None:
                sc = getattr(e, "status_code", None)
            if sc is not None:
                try:
                    code = int(sc)
                except Exception:
                    code = None
            else:
                code = None

            if code is not None and code >= 500 and attempts < max_attempts:
                delay = backoff_base * (2 ** (attempts - 1)) + random.uniform(
                    0, backoff_base / 3
                )
                time.sleep(delay)
                continue
            raise
        except Exception:
            if attempts < max_attempts:
                delay = backoff_base * (2 ** (attempts - 1)) + random.uniform(
                    0, backoff_base / 3
                )
                time.sleep(delay)
                continue
            raise


# -------------------------
# Small helpers to reduce complexity in `search`
# -------------------------
def _build_params(
    *,
    q: str,
    type: Optional[str],
    capabilities: Optional[Iterable[str] | str],
    frameworks: Optional[Iterable[str] | str],
    providers: Optional[Iterable[str] | str],
    mode: Optional[str],
    limit: int,
    with_rag: bool,
    with_snippets: bool,
    rerank: Optional[str],
    include_pending: bool,
) -> Tuple[Dict[str, Any], str]:
    """Normalize inputs into request params and return (params, normalized_mode)."""
    params: Dict[str, Any] = {
        "q": q,
        "limit": _clamp(limit, 1, 100),
        "include_pending": bool(include_pending),
    }

    t = (type or "").strip().lower() or None
    if t and t != "any":
        params["type"] = t

    caps = _csv(capabilities)
    frms = _csv(frameworks)
    prov = _csv(providers)
    if caps:
        params["capabilities"] = caps
    if frms:
        params["frameworks"] = frms
    if prov:
        params["providers"] = prov

    m = (mode or "hybrid").strip().lower()
    if m:
        params["mode"] = m
    if with_rag:
        params["with_rag"] = True
    if with_snippets:
        params["with_snippets"] = True
    if rerank and rerank != "none":
        params["rerank"] = rerank

    return params, m


def _fallback_modes(
    current_mode: str, provided: Tuple[str, ...] | None
) -> Tuple[str, ...]:
    """Compute the fallback chain given current mode and an optional override."""
    if provided:
        return tuple(x for x in provided if x != current_mode)
    # Default order mirrors previous behavior
    return (
        ("hybrid", "keyword", "semantic")
        if current_mode != "hybrid"
        else ("keyword", "semantic")
    )


def _finalize_response(
    *,
    raw_resp: Any,
    mapping: Dict[str, Any],
    as_model: bool,
) -> Dict[str, Any] | SearchResponse:
    """Return either a dict or a SearchResponse without changing existing semantics."""
    if as_model:
        if isinstance(raw_resp, SearchResponse):
            return raw_resp
        return SearchResponse.model_validate(mapping)
    return mapping


# -------------------------
# Public API
# -------------------------
def search(
    client: MatrixClient,
    q: str,
    *,
    type: Optional[str] = None,
    capabilities: Optional[Iterable[str] | str] = None,
    frameworks: Optional[Iterable[str] | str] = None,
    providers: Optional[Iterable[str] | str] = None,
    mode: Optional[str] = "hybrid",
    limit: int = 5,
    with_rag: bool = False,
    with_snippets: bool = False,
    rerank: Optional[str] = "none",
    include_pending: bool = False,
    options: SearchOptions | None = None,
) -> Dict[str, Any] | SearchResponse:
    """
    Robust, easy-to-maintain search helper over /catalog/search.
    (Behavior unchanged; complexity reduced.)
    """
    opts = options or SearchOptions()

    params, m = _build_params(
        q=q,
        type=type,
        capabilities=capabilities,
        frameworks=frameworks,
        providers=providers,
        mode=mode,
        limit=limit,
        with_rag=with_rag,
        with_snippets=with_snippets,
        rerank=rerank,
        include_pending=include_pending,
    )

    # Initial attempt
    resp = _do_request(
        client,
        params,
        max_attempts=max(1, opts.max_attempts),
        backoff_base=max(0.05, opts.backoff_base),
    )
    resp_map = _to_mapping(resp)

    # Fallbacks if empty
    if opts.allow_fallback and not resp_map.get("items"):
        for nxt in _fallback_modes(m, opts.fallback_order):
            if nxt == m:
                continue
            p2 = dict(params)
            p2["mode"] = nxt
            resp = _do_request(
                client,
                p2,
                max_attempts=max(1, opts.max_attempts),
                backoff_base=max(0.05, opts.backoff_base),
            )
            resp_map = _to_mapping(resp)
            if resp_map.get("items"):
                break

    # Return as requested: model or dict
    return _finalize_response(raw_resp=resp, mapping=resp_map, as_model=opts.as_model)


def search_try_modes(
    client: MatrixClient,
    q: str,
    modes: Sequence[str] = ("hybrid", "keyword", "semantic"),
    **kwargs: Any,
):
    for m in modes:
        yield m, search(
            client,
            q,
            mode=m,
            options=SearchOptions(allow_fallback=False, as_model=False),
            **kwargs,
        )
