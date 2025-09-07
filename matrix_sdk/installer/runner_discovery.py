# SPDX-License-Identifier: MIT
"""Runner discovery strategies (well-logged, cross-platform, production-grade).

Public:
    materialize_runner(outcome: dict, target_path: Path) -> str | None

Design:
- Deterministic strategy order; no surprise network calls.
- Pure orchestration + light I/O (only writes runner.json).
- Windows/macOS/Linux safe; shallow search skips noisy dirs.
- Connector synthesis is gated by env toggles and domain allow-lists.
- Robust logging: INFO for steps/summaries, DEBUG for details.

Environment toggles
-------------------
MATRIX_SDK_ALLOW_MANIFEST_FETCH   (default: 1)   allow fetching embedded manifest_url
MATRIX_SDK_MANIFEST_DOMAINS       (csv)          allow-list of domains for remote fetches
MATRIX_SDK_ENABLE_CONNECTOR       (default: 1)   enable connector synthesis strategies
MATRIX_SDK_HTTP_TIMEOUT           (seconds)      network timeout (also via util.HTTP_TIMEOUT)
MATRIX_SDK_RUNNER_SEARCH_DEPTH    (int)          depth for shallow search (also via util)
MATRIX_SDK_DEBUG                  (bool)         enable debug logging handler in util
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

# Schema helpers
from .runner_schema import (
    _coerce_runner_to_legacy_process,
    _ensure_sse_url,
    _extract_mcp_sse_url,
    _is_valid_runner_schema,
    _url_from_manifest,
)

# -----------------------------------------------------------------------------
# Centralized helpers (with safe fallbacks during migration)
# -----------------------------------------------------------------------------
try:
    from .util import (
        HTTP_TIMEOUT,
        RUNNER_SEARCH_DEPTH_DEFAULT,
        _env_bool,
        _short,
    )
    from .util import (
        logger as _LOGGER,
    )
except Exception:  # pragma: no cover - transitional fallback
    _LOGGER = logging.getLogger("matrix_sdk.installer")
    if not _LOGGER.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(
            logging.Formatter("[matrix-sdk][installer] %(levelname)s: %(message)s")
        )
        _LOGGER.addHandler(_h)
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    _LOGGER.setLevel(
        logging.DEBUG if dbg in {"1", "true", "yes", "on"} else logging.INFO
    )

    def _env_bool(name: str, default: bool = False) -> bool:
        v = (os.getenv(name) or "").strip().lower()
        if not v:
            return default
        return v in {"1", "true", "yes", "on"}

    def _short(p: Path | str, maxlen: int = 120) -> str:  # type: ignore[override]
        s = str(p)
        return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])

    HTTP_TIMEOUT = max(3, int((os.getenv("MATRIX_SDK_HTTP_TIMEOUT") or 15)))
    RUNNER_SEARCH_DEPTH_DEFAULT = int(
        (os.getenv("MATRIX_SDK_RUNNER_SEARCH_DEPTH") or 2)
    )


logger = _LOGGER


__all__ = ["materialize_runner"]

# Module-level constant to avoid recreating per call
_SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "__pycache__"}


# =============================================================================
# Public entrypoint
# =============================================================================


def materialize_runner(outcome: Dict[str, Any], target_path: Path) -> Optional[str]:
    """Find, infer, or synthesize a ``runner.json`` for *outcome* into *target_path*.

    Returns the path to the materialized ``runner.json`` or ``None`` if no strategy
    succeeded.
    """
    logger.info("runner: attempting to find or synthesize a runner configuration.")
    plan_node: Dict[str, Any] = outcome.get("plan") or outcome

    strategies = [
        _try_fetch_runner_from_b64,  # 1) embedded b64
        _try_fetch_runner_from_url,  # 2) explicit URL (absolute/relative)
        _try_find_runner_from_object,  # 3) object in plan
        _try_find_runner_in_embedded_manifest,  # 4) v2 embedded runner or v1 synth
        _try_find_runner_from_file,  # 5) file at known path
        _try_find_runner_via_shallow_search,  # 6) shallow search
        _try_fetch_runner_from_manifest_url,  # 7) synthesize via manifest URL
        _try_infer_runner_from_structure,  # 8) infer from files
        _try_synthesize_connector_runner,  # 9) from any embedded URL hint
        _write_min_connector_if_any_url_hint,  # 10) last-ditch minimal connector
    ]

    for strat in strategies:
        name = strat.__name__
        logger.debug("runner: trying strategy '%s'...", name)
        try:
            path = strat(plan_node, target_path, outcome)
        except Exception as e:  # be resilient; continue to next strategy
            logger.debug("runner: strategy '%s' raised %s; continuing", name, e)
            path = None
        if path:
            logger.info("runner: found using '%s' → %s", name, _short(path))
            return path
        logger.debug("runner: strategy '%s' did not find a runner.", name)

    logger.warning("runner: a valid runner config was not found or inferred")
    return None


# =============================================================================
# Strategy implementations (private)
# =============================================================================


def _try_fetch_runner_from_b64(
    plan_node: Dict[str, Any], target: Path, *_: Any
) -> Optional[str]:
    b64 = str(plan_node.get("runner_b64") or "").strip()
    if not b64:
        return None
    logger.debug("runner(b64): found runner_b64 content.")
    try:
        data = base64.b64decode(b64)
        obj = json.loads(data.decode("utf-8", "replace"))
        obj = _coerce_runner_to_legacy_process(obj)
        if _is_valid_runner_schema(obj, logger):
            rp = target / "runner.json"
            _write_json_atomic(rp, obj)
            logger.info("runner(b64): materialized from runner_b64 → %s", _short(rp))
            return str(rp)
        logger.warning("runner(b64): decoded runner object has invalid schema.")
    except Exception as e:
        logger.warning("runner(b64): failed to decode/materialize (%s)", e)
    return None


def _try_fetch_runner_from_url(
    plan_node: Dict[str, Any], target: Path, outcome: Dict[str, Any]
) -> Optional[str]:
    runner_url = str(plan_node.get("runner_url") or "").strip()
    if not runner_url:
        return None

    resolved = _resolve_url_with_base(runner_url, outcome, plan_node)
    logger.info("runner(url): fetching runner.json from %s", resolved)
    try:
        data = _http_get_text(resolved, timeout=HTTP_TIMEOUT)
        obj = json.loads(data)
        obj = _coerce_runner_to_legacy_process(obj)
        if _is_valid_runner_schema(obj, logger):
            rp = target / "runner.json"
            _write_json_atomic(rp, obj)
            logger.info("runner(url): saved fetched runner.json → %s", _short(rp))
            return str(rp)
        logger.warning("runner(url): invalid schema from runner_url (ignored)")
    except Exception as e:
        logger.warning("runner(url): fetch failed (%s)", e)
    return None


def _try_find_runner_from_object(
    plan_node: Dict[str, Any], target: Path, *_: Any
) -> Optional[str]:
    obj = plan_node.get("runner")
    if not isinstance(obj, dict):
        return None
    logger.debug("runner(object): found runner object in plan.")
    obj = _coerce_runner_to_legacy_process(obj)
    if _is_valid_runner_schema(obj, logger):
        rp = target / "runner.json"
        _write_json_atomic(rp, obj)
        logger.info("runner(object): materialized from plan.runner → %s", _short(rp))
        return str(rp)
    logger.warning("runner(object): plan.runner present but invalid.")
    return None


def _try_find_runner_in_embedded_manifest(
    plan_node: Dict[str, Any], target: Path, outcome: Dict[str, Any]
) -> Optional[str]:
    candidate_keys = ("manifest", "source_manifest", "echo_manifest", "input_manifest")
    nodes: List[Dict[str, Any]] = []

    for container in (plan_node, outcome):
        if isinstance(container, dict):
            for k in candidate_keys:
                v = container.get(k)
                if isinstance(v, dict):
                    nodes.append(v)

    if not nodes:
        return None

    logger.debug("runner(manifest): %d embedded manifest candidate(s).", len(nodes))

    # v2 path: manifest['runner']
    for m in nodes:
        r = m.get("runner")
        if isinstance(r, dict):
            r = _coerce_runner_to_legacy_process(r)
            if _is_valid_runner_schema(r, logger):
                rp = target / "runner.json"
                _write_json_atomic(rp, r)
                logger.info(
                    "runner(manifest): materialized from manifest.runner → %s",
                    _short(rp),
                )
                return str(rp)

    # v1 path: synthesize from server URL
    for m in nodes:
        if url := _url_from_manifest(m):
            logger.debug(
                "runner(manifest): found server URL '%s' for v1 synthesis.", url
            )
            connector = _make_connector_runner(url)
            if _is_valid_runner_schema(connector, logger):
                rp = target / "runner.json"
                _write_json_atomic(rp, connector)
                logger.info(
                    "runner(manifest): synthesized from v1 server URL → %s", _short(rp)
                )
                return str(rp)

    return None


def _try_find_runner_from_file(
    plan_node: Dict[str, Any], target: Path, *_: Any
) -> Optional[str]:
    name = str(plan_node.get("runner_file") or "runner.json").strip()
    name = name.replace("\\", "/").lstrip("/")
    rp = (target / name).resolve()
    # traversal protection: ensure rp under target
    try:
        rp.relative_to(target.resolve())
    except Exception:
        logger.warning(
            "runner(file): blocked path traversal for runner_file='%s' (target=%s)",
            name,
            _short(target),
        )
        return None

    logger.debug("runner(file): checking for file at %s", _short(rp))
    if rp.is_file():
        try:
            obj = json.loads(rp.read_text("utf-8"))
            obj = _coerce_runner_to_legacy_process(obj)
            if _is_valid_runner_schema(obj, logger):
                # Normalize into canonical runner.json (even if same file, write atomically)
                out = target / "runner.json"
                _write_json_atomic(out, obj)
                logger.info("runner(file): loaded+normalized runner → %s", _short(out))
                return str(out)
        except json.JSONDecodeError:
            logger.warning(
                "runner(file): file exists but is not valid JSON: %s", _short(rp)
            )
        except Exception as e:
            logger.debug("runner(file): error reading %s (%s)", _short(rp), e)
    return None


def _try_find_runner_via_shallow_search(
    plan_node: Dict[str, Any], target: Path, *_: Any
) -> Optional[str]:
    name = str(plan_node.get("runner_file") or "runner.json").strip()
    if "/" in name or "\\" in name:
        return None  # path provided; skip search

    depth = max(0, RUNNER_SEARCH_DEPTH_DEFAULT)
    if depth <= 0:
        return None

    logger.debug("runner(search): shallow search for '%s' (depth=%d)", name, depth)

    if found := _find_runner_file_shallow(target, name, depth):
        try:
            obj = json.loads(found.read_text("utf-8"))
            obj = _coerce_runner_to_legacy_process(obj)
            if _is_valid_runner_schema(obj, logger):
                out = target / "runner.json"
                _write_json_atomic(out, obj)
                logger.info(
                    "runner(search): discovered valid runner at %s", _short(found)
                )
                return str(out)
        except json.JSONDecodeError:
            logger.warning(
                "runner(search): discovered file but invalid JSON: %s", _short(found)
            )
        except Exception as e:
            logger.debug("runner(search): error reading %s (%s)", _short(found), e)
    return None


def _try_fetch_runner_from_manifest_url(
    plan_node: Dict[str, Any], target: Path, outcome: Dict[str, Any]
) -> Optional[str]:
    if not _env_bool("MATRIX_SDK_ALLOW_MANIFEST_FETCH", True):
        logger.debug("runner(manifest_url): disabled by env var.")
        return None

    src = (
        plan_node.get("manifest_url")
        or (plan_node.get("provenance") or {}).get("source_url")
        or (outcome.get("provenance") or {}).get("source_url")
        or ""
    )
    src = str(src or "").strip()
    if not src:
        return None

    resolved = _resolve_url_with_base(src, outcome, plan_node)
    if not _host_allowed(resolved):
        logger.debug("runner(manifest_url): host not allowed: %s", resolved)
        return None

    logger.debug("runner(manifest_url): fetching manifest from %s", resolved)
    try:
        data = _http_get_text(resolved, timeout=HTTP_TIMEOUT)
        manifest = json.loads(data)
        if url := _url_from_manifest(manifest):
            rp = target / "runner.json"
            _write_json_atomic(rp, _make_connector_runner(url))
            logger.info(
                "runner(manifest_url): synthesized connector from manifest → %s",
                _short(rp),
            )
            return str(rp)
    except Exception as e:
        logger.debug("runner(manifest_url): fetch/synthesis failed: %s", e)
    return None


def _try_infer_runner_from_structure(
    plan_node: Dict[str, Any], target: Path, *_: Any
) -> Optional[str]:
    # Priority 1: Specific entry points
    if (target / "server.py").exists():
        obj = {"type": "python", "entry": "server.py", "python": {"venv": ".venv"}}
        rp = target / "runner.json"
        _write_json_atomic(rp, obj)
        logger.info(
            "runner(infer): found 'server.py', inferring python runner → %s", _short(rp)
        )
        return str(rp)

    if (target / "server.js").exists() or (target / "package.json").exists():
        entry = "server.js" if (target / "server.js").exists() else "index.js"
        obj = {"type": "node", "entry": entry}
        rp = target / "runner.json"
        _write_json_atomic(rp, obj)
        logger.info(
            "runner(infer): found node files, inferring node runner → %s", _short(rp)
        )
        return str(rp)

    # Priority 2: Generic Python project files
    if (
        (target / "pyproject.toml").is_file()
        or (target / "requirements.txt").is_file()
        or (target / "setup.py").is_file()
    ):
        logger.info(
            "runner(infer): python project hints present; trying server finder."
        )
        potential_servers: List[str] = []
        notes_lines = [
            "Runner synthesized because no explicit 'runner.json' was found.",
            "We could not determine a single entry point automatically.",
            "ACTION REQUIRED: edit the 'entry' field to the correct server file.",
        ]
        try:
            helper = Path(__file__).parent / "find_potential_servers.py"
            if helper.is_file():
                import subprocess
                import sys  # local import to keep module import cheap

                cmd = [sys.executable, str(helper), str(target)]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, check=True, timeout=30
                )
                for line in res.stdout.splitlines():
                    if line.startswith("- "):
                        potential_servers.append(line[2:].strip())
                if potential_servers:
                    notes_lines.append("Potential entry points found:")
                    notes_lines.extend([f"  - {s}" for s in potential_servers])
                else:
                    notes_lines.append("No likely server entry points were found.")
            else:
                logger.debug(
                    "runner(infer): helper script not found at %s", _short(helper)
                )
                notes_lines.append("Automated server discovery helper not found.")
        except Exception as e:
            logger.debug("runner(infer): server finder failed: %s", e)
            notes_lines.append("Automated server discovery failed.")

        entry_point = potential_servers[0] if potential_servers else "EDIT_ME.py"
        obj = {
            "type": "python",
            "entry": entry_point,
            "python": {"venv": ".venv"},
            "notes": "\n".join(notes_lines),
        }
        rp = target / "runner.json"
        _write_json_atomic(rp, obj)
        logger.info("runner(infer): synthesized python runner → %s", _short(rp))
        return str(rp)

    return None


def _try_synthesize_connector_runner(
    plan_node: Dict[str, Any], target: Path, outcome: Dict[str, Any]
) -> Optional[str]:
    if not _connector_enabled():
        return None

    url = _extract_mcp_sse_url(outcome) or _extract_mcp_sse_url(plan_node)
    if not url:
        return None

    obj = _make_connector_runner(url)
    if _is_valid_runner_schema(obj, logger):
        rp = target / "runner.json"
        _write_json_atomic(rp, obj)
        logger.info(
            "runner(synth): synthesized connector from embedded URL → %s", _short(rp)
        )
        return str(rp)
    return None


def _write_min_connector_if_any_url_hint(
    plan_node: Dict[str, Any], target: Path, outcome: Dict[str, Any]
) -> Optional[str]:
    """Last-ditch fallback: if *any* URL hint exists, write a minimal connector.

    This is intentionally conservative and only runs after all other strategies
    have failed.
    """
    if not _connector_enabled():
        return None

    url = _extract_mcp_sse_url(plan_node) or _extract_mcp_sse_url(outcome)
    if not url:
        return None

    rp = target / "runner.json"
    _write_json_atomic(rp, _make_connector_runner(url))
    logger.info("runner(fallback): minimal connector from URL hint → %s", _short(rp))
    return str(rp)


# =============================================================================
# Local helpers (private)
# =============================================================================


def _connector_enabled() -> bool:
    val = (os.getenv("MATRIX_SDK_ENABLE_CONNECTOR") or "1").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _make_connector_runner(url: str) -> Dict[str, Any]:
    return {
        "type": "connector",
        "integration_type": "MCP",
        "request_type": "SSE",
        "url": _ensure_sse_url(url),
        "endpoint": "/sse",
        "headers": {},
    }


def _host_allowed(url: str) -> bool:
    raw = (os.getenv("MATRIX_SDK_MANIFEST_DOMAINS") or "").strip()
    if not raw:
        return True
    host = urlparse(url).hostname or ""
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    logger.debug(
        "runner(manifest_url): checking host '%s' against allowlist: %s", host, allowed
    )
    return host.lower() in allowed


def _base_url_from_outcome(outcome_or_plan: Dict[str, Any]) -> Optional[str]:
    """Best-effort base URL extraction from outcome/plan provenance."""
    try:
        if isinstance(outcome_or_plan, dict):
            # shallow scan common nesting first
            prov = outcome_or_plan.get("provenance")
            if isinstance(prov, dict):
                url = (prov.get("source_url") or prov.get("manifest_url") or "").strip()
                if url:
                    return url
            # scan direct children
            for node in outcome_or_plan.values():
                if isinstance(node, dict):
                    prov2 = node.get("provenance")
                    if isinstance(prov2, dict):
                        url = (
                            prov2.get("source_url") or prov2.get("manifest_url") or ""
                        ).strip()
                        if url:
                            return url
    except Exception as e:
        logger.debug("resolve(base): error while extracting base URL: %s", e)
        return None
    return None


def _resolve_url_with_base(
    raw_url: str, outcome: Dict[str, Any], plan_node: Dict[str, Any]
) -> str:
    raw = (raw_url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        logger.debug("URL '%s' is already absolute.", raw)
        return raw

    base = _base_url_from_outcome(outcome) or _base_url_from_outcome(plan_node) or ""
    if not base:
        logger.debug("No base URL available for '%s'; returning as-is.", raw)
        return raw
    try:
        joined = urljoin(base, raw)
        logger.debug(
            "Resolved relative URL '%s' against base '%s' -> '%s'", raw, base, joined
        )
        return joined
    except Exception as e:
        logger.debug("Failed to join base URL '%s' with '%s': %s", base, raw, e)
        return raw


def _find_runner_file_shallow(root: Path, name: str, max_depth: int) -> Optional[Path]:
    """Breadth-first search for *name* up to *max_depth*, skipping noisy dirs.

    Skipped directories (case-sensitive):
        node_modules, .venv, venv, .git, __pycache__
    """
    if max_depth <= 0:
        return None

    from collections import deque

    dq: "deque[tuple[Path, int]]" = deque([(root, 0)])
    seen: set[Path] = {root}
    logger.debug(
        "Shallow search started for '%s' from '%s' (depth=%d)",
        name,
        _short(root),
        max_depth,
    )

    while dq:
        cur, d = dq.popleft()
        cand = cur / name
        if cand.is_file():
            logger.debug("Shallow search found at '%s'", _short(cand))
            return cand
        if d < max_depth:
            try:
                for child in cur.iterdir():
                    if (
                        child.is_dir()
                        and child not in seen
                        and (child.name not in _SKIP_DIRS)
                    ):
                        seen.add(child)
                        dq.append((child, d + 1))
            except OSError as e:
                logger.debug(
                    "Shallow search could not list dir '%s': %s", _short(cur), e
                )
                continue

    # ✅ Fixed: include 'name' in the log args
    logger.debug("Shallow search for '%s' finished, no file found.", name)
    return None


def _http_get_text(url: str, *, timeout: int) -> str:
    """HTTP GET helper with sensible headers and strict timeout."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, */*;q=0.1",
            "User-Agent": "matrix-sdk-installer/1",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # honor declared encoding when available; else UTF-8
        ct = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ct:
            try:
                charset = ct.split("charset=", 1)[1].split(";")[0].strip()
            except Exception:
                pass
        data = resp.read()
    return data.decode(charset, "replace")


def _write_json_atomic(path: Path, obj: Dict[str, Any]) -> None:
    """Write JSON atomically to avoid partial files on crashes (cross-platform)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use NamedTemporaryFile in the same directory to ensure atomic replace
    fd = None
    tmp_path: Optional[Path] = None
    try:
        # delete=False for Windows replace semantics
        fd, tmp_name = tempfile.mkstemp(
            prefix=".runner.", suffix=".json.tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # atomic replace when possible
        try:
            os.replace(str(tmp_path), str(path))
        except Exception:
            # best-effort fallback
            tmp_path.replace(path)
    finally:
        # If something failed before replace, clean up temp file
        try:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink(
                    missing_ok=True
                )  # Py3.8+: missing_ok ignored gracefully
        except Exception:
            pass
