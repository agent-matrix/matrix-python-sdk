# SPDX-License-Identifier: MIT
"""File & artifact IO (pure IO; no schema logic).

Public functions (used by core.py):

    - find_file_candidates(outcome) -> list[dict]
    - materialize_files(outcome, target_path) -> int
    - materialize_artifacts(plan, target_path) -> int

Design goals:
    * Cross-platform (Windows-safe) path handling.
    * Never escape *target_path* (security): all writes are confined under target.
    * Lazy-import artifact fetchers; run only when specified by the plan.
    * Small, robust logs – INFO for summary/decisions, DEBUG for details.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# Production fix: surface artifact failures as manifest-resolution errors
from ..manifest import ManifestResolutionError

# ----------------------------------------------------------------------------
# Centralized logger / helpers (with safe fallback during migration)
# ----------------------------------------------------------------------------
try:
    from .util import _short
    from .util import logger as _LOGGER  # type: ignore
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

    def _short(path: Path | str, maxlen: int = 120) -> str:  # type: ignore[override]
        s = str(path)
        return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


logger = _LOGGER

# ----------------------------------------------------------------------------
# Lazy import fetchers (git / http). Keep signature parity with current code.
# ----------------------------------------------------------------------------
try:  # git artifacts
    from ..gitfetch import GitFetchError, fetch_git_artifact  # type: ignore
except Exception:  # pragma: no cover
    fetch_git_artifact = None  # type: ignore

    class GitFetchError(RuntimeError):  # type: ignore
        pass


try:  # http/archive artifacts
    from ..archivefetch import ArchiveFetchError, fetch_http_artifact  # type: ignore
except Exception:  # pragma: no cover
    fetch_http_artifact = None  # type: ignore

    class ArchiveFetchError(RuntimeError):  # type: ignore
        pass


__all__ = [
    "find_file_candidates",
    "materialize_files",
    "materialize_artifacts",
]


# =============================================================================
# Public: files
# =============================================================================


def find_file_candidates(outcome: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract all file description dicts from *outcome*.

    Looks at: outcome.plan.files, each results[i].files, and outcome.files.
    Ignores non-dict entries; returns a flat list of dicts.
    """
    logger.debug("materialize(files): scanning outcome for file candidates...")
    candidates: List[Dict[str, Any]] = []
    plan_files = (outcome.get("plan") or {}).get("files", [])
    if isinstance(plan_files, list):
        candidates.extend(x for x in plan_files if isinstance(x, dict))
        logger.debug("materialize(files): plan.files -> %d entries", len(plan_files))

    results = outcome.get("results", [])
    if isinstance(results, list):
        for step in results:
            if isinstance(step, dict):
                step_files = step.get("files", [])
                if isinstance(step_files, list):
                    candidates.extend(x for x in step_files if isinstance(x, dict))

    tail = outcome.get("files", [])
    if isinstance(tail, list):
        candidates.extend(x for x in tail if isinstance(x, dict))

    logger.debug("materialize(files): total candidates = %d", len(candidates))
    return candidates


def materialize_files(outcome: Dict[str, Any], target_path: Path) -> int:
    """Write all declared files from *outcome* below *target_path*.

    Returns the number of files written.
    """
    logger.info("materialize(files): writing declared files → %s", _short(target_path))
    target_path.mkdir(parents=True, exist_ok=True)

    candidates = find_file_candidates(outcome)
    written = 0

    for f in candidates:
        raw_path = f.get("path") or f.get("rel") or f.get("dest")
        if not raw_path:
            logger.debug("materialize(files): skipping candidate without a path: %s", f)
            continue

        p = _secure_join(target_path, str(raw_path))
        if p is None:
            logger.warning(
                "materialize(files): blocked path traversal for '%s' (target=%s)",
                raw_path,
                _short(target_path),
            )
            continue

        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if (content_b64 := f.get("content_b64")) is not None:
                logger.debug(
                    "materialize(files): writing base64 content to %s", _short(p)
                )
                p.write_bytes(base64.b64decode(content_b64))
            elif (content := f.get("content")) is not None:
                logger.debug(
                    "materialize(files): writing text content to %s", _short(p)
                )
                p.write_text(str(content), encoding="utf-8")
            else:
                logger.debug("materialize(files): touching empty file at %s", _short(p))
                p.touch()
            written += 1
        except Exception as e:
            logger.warning("materialize(files): could not write %s (%s)", _short(p), e)
            continue

    logger.info("materialize(files): successfully wrote %d file(s).", written)
    return written


# =============================================================================
# Public: artifacts
# =============================================================================


def materialize_artifacts(plan: Dict[str, Any], target_path: Path) -> int:
    """Fetch all artifacts declared in *plan* into *target_path*.

    Returns the number of artifacts fetched successfully.

    Raises
    ------
    ManifestResolutionError
        If a git or http artifact fetcher raises a fetch error.
    """
    artifacts = plan.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        logger.debug("materialize(artifacts): no artifacts to fetch.")
        return 0

    logger.info("materialize(artifacts): fetching %d artifact(s)", len(artifacts))
    count = 0
    for idx, a in enumerate(artifacts, start=1):
        if not isinstance(a, dict):
            logger.debug("materialize(artifacts): skipping non-dict artifact #%d", idx)
            continue
        try:
            if a.get("kind") == "git":
                _handle_git_artifact(a, target_path)
                count += 1
            elif a.get("url"):
                _handle_http_artifact(a, target_path)
                count += 1
            else:
                logger.warning("materialize(artifacts): unknown artifact kind: %s", a)
        except (GitFetchError, ArchiveFetchError) as e:
            logger.error("artifact: failed to fetch: %s", e)
            # Production fix: escalate as ManifestResolutionError
            raise ManifestResolutionError(str(e)) from e
        except Exception as e:
            logger.warning("materialize(artifacts): artifact #%d failed (%s)", idx, e)
            continue

    logger.info("materialize(artifacts): successfully fetched %d artifact(s).", count)
    return count


# =============================================================================
# Private: artifact handlers
# =============================================================================


def _handle_git_artifact(artifact: Dict[str, Any], target_path: Path) -> None:
    """Handle a git-based artifact with a best-effort legacy shim."""
    spec = artifact.get("spec") or {}

    # Legacy shim: derive spec from a deprecated 'command: git clone ...' string
    cmd = str(artifact.get("command") or "").strip()
    if not spec.get("repo") and cmd.startswith("git clone"):
        logger.warning("artifact(git): SHIM: deriving spec from legacy 'command'.")
        try:
            parts = cmd.split()
            repo_idx = parts.index("clone") + 1
            spec["repo"] = parts[repo_idx]
            if "--branch" in parts:
                ref_idx = parts.index("--branch") + 1
                spec["ref"] = parts[ref_idx]
            logger.info("artifact(git): SHIM: derived spec=%s", spec)
        except (ValueError, IndexError) as e:
            logger.error("artifact(git): SHIM parse failed (%s)", e)

    if fetch_git_artifact is None:
        logger.error(
            "artifact(git): fetcher not available but git artifact was specified."
        )
        return

    logger.info(
        "artifact(git): fetching with spec %s into %s", spec, _short(target_path)
    )
    fetch_git_artifact(spec=spec, target=target_path)  # type: ignore[misc]


def _handle_http_artifact(artifact: Dict[str, Any], target_path: Path) -> None:
    """Handle a URL-based artifact using the archivefetch helper."""
    if fetch_http_artifact is None:
        logger.error(
            "artifact(http): fetcher not available but http artifact was specified."
        )
        return

    url = artifact.get("url")
    dest = artifact.get("path") or artifact.get("dest")
    sha256 = str(s) if (s := artifact.get("sha256")) else None
    unpack = bool(artifact.get("unpack", False))

    logger.info(
        "artifact(http): fetching url='%s', dest='%s', unpack=%s", url, dest, unpack
    )
    fetch_http_artifact(  # type: ignore[misc]
        url=url,
        target=target_path,
        dest=dest,
        sha256=sha256,
        unpack=unpack,
        logger=logger,
    )


# =============================================================================
# Private: path utilities (security & normalization)
# =============================================================================


def _secure_join(root: Path, rel: str) -> Optional[Path]:
    """Join *rel* under *root* and prevent directory traversal.

    - Converts backslashes to forward slashes.
    - Strips leading slashes.
    - Resolves and checks that the resulting path is within *root*.

    Returns the resolved Path, or ``None`` if traversal would escape *root*.
    """
    try:
        norm = rel.replace("\\", "/").strip("/")
        candidate = (root / norm).resolve()
        root_resolved = root.resolve()
        # 3.9+ compatible relative-to check
        try:
            candidate.relative_to(root_resolved)
        except Exception:
            return None
        return candidate
    except Exception:
        return None
