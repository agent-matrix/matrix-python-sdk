# SPDX-License-Identifier: MIT
"""Runner schema helpers (pure, fast, testable).

This module defines **pure functions** used by the installer to validate and
normalize runner configurations and to extract server URLs from manifests.

Design goals
------------
- No I/O, network, or filesystem access.
- Accept both legacy (``type+entry``) and modern (``process.command``) runners.
- Provide a best-effort translator from modern → legacy without destroying data.
- Be heavily logged at DEBUG to aid field diagnostics without noisy INFO logs.

Functions
---------
- ``_is_valid_runner_schema(runner, logger)``
- ``_coerce_runner_to_legacy_process(runner)``
- ``_ensure_sse_url(url)``
- ``_url_from_manifest(manifest)``
- ``_extract_mcp_sse_url(node)`` (recursive)

All functions are side-effect free and safe to unit-test.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse, urlunparse

# ----------------------------------------------------------------------------
# Logger (prefer centralized one if available)
# ----------------------------------------------------------------------------
try:
    from .util import logger as _LOGGER  # type: ignore
except Exception:  # pragma: no cover - transitional fallback
    _LOGGER = logging.getLogger("matrix_sdk.installer")
    if not _LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
        )
        _LOGGER.addHandler(handler)
    # Honor env toggle for debug without importing util
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    _LOGGER.setLevel(
        logging.DEBUG if dbg in {"1", "true", "yes", "on"} else logging.INFO
    )

logger = _LOGGER

__all__ = [
    "_is_valid_runner_schema",
    "_coerce_runner_to_legacy_process",
    "_ensure_sse_url",
    "_url_from_manifest",
    "_extract_mcp_sse_url",
]


# =============================================================================
# Validation
# =============================================================================


def _is_valid_runner_schema(runner: Dict[str, Any], _log: logging.Logger) -> bool:
    """Return True if *runner* looks runnable.

    Accepted shapes (backwards compatible):
      1) Legacy process runners: ``{"type": "python|node", "entry": "..."}``
      2) Connector runners:     ``{"type": "connector", "url": "..."}``
      3) Modern process form:   ``{"process": {"command": ["python", "server.py", ...]}}``

    Notes
    -----
    * We keep the check intentionally narrow to avoid false positives.
    * This function **does not** modify input.
    """
    _log.debug("runner validation: checking schema for runner object: %s", runner)

    if not isinstance(runner, dict):
        _log.debug("runner validation: failed (not a dict)")
        return False

    rtype = str(runner.get("type") or "").strip().lower()

    # Connector shape
    if rtype == "connector":
        ok = bool(str(runner.get("url") or "").strip())
        if not ok:
            _log.warning("runner validation: 'connector' missing required 'url' field")
        else:
            _log.debug("runner validation: connector schema is valid")
        return ok

    # Legacy process shape
    if rtype in {"python", "node"}:
        entry = str(runner.get("entry") or "").strip()
        if not entry:
            _log.warning(
                "runner validation: failed (missing required 'entry' for type=%r)",
                rtype,
            )
            return False
        _log.debug("runner validation: legacy process schema is valid (type=%s)", rtype)
        return True

    # Modern process shape (no explicit type)
    if isinstance(runner.get("process"), dict):
        cmd = runner["process"].get("command")  # type: ignore[index]
        if (
            isinstance(cmd, Sequence)
            and len(cmd) > 0
            and all(isinstance(x, (str, bytes)) for x in cmd)
        ):
            _log.debug(
                "runner validation: modern process schema is valid (command=list)"
            )
            return True
        _log.warning("runner validation: 'process.command' must be a non-empty list")
        return False

    # Unknown shape
    _log.warning(
        "runner validation: failed (no acceptable shape). Keys=%s", list(runner.keys())
    )
    return False


# =============================================================================
# Normalization (best effort, non-destructive)
# =============================================================================


def _coerce_runner_to_legacy_process(runner: Dict[str, Any]) -> Dict[str, Any]:
    """Return a **new** runner dict that is legacy-friendly when possible.

    Strategy (pure, best-effort):
    - If ``runner`` already has ``type in {python,node}`` and ``entry`` → return copy.
    - If ``type == 'connector'`` → return copy.
    - If modern ``process.command`` present → attempt language inference:
        * python: first token looks like a python interpreter; derive ``entry`` from
          ``-m module`` or first ``*.py`` argument when available.
        * node: first token looks like a node tool; derive ``entry`` from first
          ``*.js`` argument when available; otherwise use a conservative default
          (``server.js`` then ``index.js``).
    - Preserve original ``process`` block and any fields; simply *add* legacy keys
      (``type``, ``entry``) when inferred. Mark with ``x-normalized=True``.

    If inference fails, the original runner is returned unchanged (still valid
    as a modern runner if ``process.command`` is present).
    """
    r = dict(runner)  # shallow copy; keep original untouched

    # Already legacy/connector → return copy as-is
    rtype = str(r.get("type") or "").strip().lower()
    if (
        rtype in {"python", "node", "connector"}
        and str(r.get("entry") or r.get("url") or "").strip()
    ):
        return r

    proc = r.get("process")
    if not isinstance(proc, Mapping):
        return r

    cmd = proc.get("command")
    if not (isinstance(cmd, Sequence) and len(cmd) > 0):
        return r

    tokens = _normalize_command_tokens(cmd)
    if not tokens:
        return r

    lang = _detect_language_from_command(tokens)

    if lang == "python":
        entry = _derive_python_entry(tokens)
        if entry:
            r = {**r, "type": "python", "entry": entry, "x-normalized": True}
            return r
        # Fallback if we really cannot infer a file/module; keep modern shape
        r.setdefault("x-normalized", True)
        return r

    if lang == "node":
        entry = _first_with_suffix(tokens, ".js") or "server.js"
        r = {**r, "type": "node", "entry": entry, "x-normalized": True}
        return r

    # Unknown language; return as-is (still valid modern process)
    return r


# =============================================================================
# URL helpers (pure)
# =============================================================================


def _ensure_sse_url(url: str) -> str:
    """Normalize to a URL whose *path* ends with '/sse' (no trailing slash),
    preserving params/query/fragment.
    """
    try:
        u = (url or "").strip()
        if not u:
            return ""
        p = urlparse(u)
        path = (p.path or "").strip()
        if path.endswith("/sse/"):
            path = path[:-1]
        elif not path.endswith("/sse"):
            path = path.rstrip("/") + "/sse"
        return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))
    except Exception:  # pragma: no cover - defensive
        return url


def _url_from_manifest(m: Dict[str, Any]) -> str:
    """Try to extract a server URL from a manifest-like dict.

    Recognized (broadened) keys, in priority order:
      - mcp_registration.server.url
      - server.url | server.endpoint | server.base_url
      - endpoints.sse | sse.url | sse_endpoint
      - urls.sse | services.mcp.sse | connections.mcp.sse
      - server_url (legacy)

    Returns a string normalized to ``.../sse`` or ``""`` if not found.
    """
    try:
        # 1) mcp_registration.server.url
        reg = _get_dict(m, "mcp_registration")
        srv = _get_dict(reg, "server")
        url = _first_non_empty(
            srv.get("url"),
        )
        if url:
            return _ensure_sse_url(str(url))

        # 2) server.*
        srv2 = _get_dict(m, "server")
        url = _first_non_empty(
            srv2.get("url"), srv2.get("endpoint"), srv2.get("base_url")
        )
        if url:
            return _ensure_sse_url(str(url))

        # 3) endpoints.sse / sse.* / sse_endpoint
        endpoints = _get_dict(m, "endpoints")
        url = _first_non_empty(endpoints.get("sse"))
        if url:
            return _ensure_sse_url(str(url))

        sse = _get_dict(m, "sse")
        url = _first_non_empty(sse.get("url"), m.get("sse_endpoint"))
        if url:
            return _ensure_sse_url(str(url))

        # 4) urls.sse / services.mcp.sse / connections.mcp.sse
        urls = _get_dict(m, "urls")
        url = _first_non_empty(urls.get("sse"))
        if url:
            return _ensure_sse_url(str(url))

        services = _get_dict(m, "services")
        mcp = _get_dict(services, "mcp")
        url = _first_non_empty(mcp.get("sse"))
        if url:
            return _ensure_sse_url(str(url))

        connections = _get_dict(m, "connections")
        mcp2 = _get_dict(connections, "mcp")
        url = _first_non_empty(mcp2.get("sse"))
        if url:
            return _ensure_sse_url(str(url))

        # 5) legacy: server_url
        url = _first_non_empty(m.get("server_url"))
        if url:
            return _ensure_sse_url(str(url))

        return ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _extract_mcp_sse_url(node: Any) -> Optional[str]:  # noqa: C901 (intentional)
    """Recursively walk a dict/list to find an MCP/SSE URL.

    This is a conservative DFS that stops at the first hit.
    """
    # Direct dict probing for embedded manifest-like objects
    if isinstance(node, dict):
        # Common manifest containers in plans/outcomes
        for key in ("manifest", "source_manifest", "echo_manifest", "input_manifest"):
            v = node.get(key)
            if isinstance(v, dict):
                if url := _url_from_manifest(v):
                    logger.debug(
                        "url-extract: found via embedded manifest at key=%s", key
                    )
                    return url
        # Fallback: search all values
        for v in node.values():
            if url := _extract_mcp_sse_url(v):
                return url
        return None

    # Lists/tuples: search elements
    if isinstance(node, (list, tuple)):
        for v in node:
            if url := _extract_mcp_sse_url(v):
                return url
        return None

    return None


# =============================================================================
# Helpers (pure)
# =============================================================================


def _normalize_command_tokens(cmd: Sequence[Any]) -> List[str]:
    """Return a cleaned list of tokens from *cmd* sequence.

    Accepts ``List[str]`` or mixed; bytes are decoded as UTF-8 best-effort.
    """
    tokens: List[str] = []
    for x in cmd:
        if isinstance(x, bytes):
            try:
                x = x.decode("utf-8", "ignore")
            except Exception:  # pragma: no cover - defensive
                continue
        if isinstance(x, str):
            s = x.strip()
            if s:
                tokens.append(s)
    return tokens


def _detect_language_from_command(tokens: Sequence[str]) -> Optional[str]:
    """Return 'python', 'node', or None based on the first token(s)."""
    if not tokens:
        return None

    first = tokens[0].lower()

    # Common python launchers
    if (
        first.endswith("python")
        or first.endswith("python.exe")
        or first in {"python", "python3", "py"}
    ):
        return "python"

    # Some wrappers call: uv run python ... / pipx run python ...
    if first in {"uv", "pipx", "pipenv", "poetry", "hatch"}:
        # Peek next meaningful token if present
        nxt = tokens[1].lower() if len(tokens) > 1 else ""
        if nxt in {"run", "exec", "shell"} and len(tokens) > 2:
            nxt = tokens[2].lower()
        if (
            nxt.endswith("python")
            or nxt.endswith("python.exe")
            or nxt in {"python", "python3", "py"}
        ):
            return "python"

    # Node launchers
    if (
        first.endswith("node")
        or first.endswith("node.exe")
        or first in {"node", "nodejs", "npx"}
    ):
        return "node"
    if first in {"npm", "pnpm", "yarn"}:
        return "node"

    return None


def _derive_python_entry(tokens: Sequence[str]) -> Optional[str]:
    """Best-effort parse of ``python ...`` tokens to derive an entry.

    Preference order:
      1) ``-m module``  → returns the module string (caller may treat as module)
      2) first argument ending with ``.py``
    """
    # Try: python -m package.module
    for i, t in enumerate(tokens):
        if t == "-m" and i + 1 < len(tokens):
            mod = tokens[i + 1].strip()
            if mod:
                return mod  # module entry (caller may record kind)

    # Try: python path/to/script.py
    for t in tokens:
        if t.endswith(".py"):
            return t

    return None


def _first_with_suffix(tokens: Sequence[str], suffix: str) -> Optional[str]:
    for t in tokens:
        if t.endswith(suffix):
            return t
    return None


def _get_dict(obj: Mapping[str, Any], key: str) -> Dict[str, Any]:
    v = obj.get(key)
    return v if isinstance(v, dict) else {}


def _first_non_empty(*vals: Any) -> Optional[str]:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None
