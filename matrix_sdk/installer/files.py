# SPDX-License-Identifier: MIT
"""File & artifact IO (pure IO; no schema logic).

Public functions (used by core.py):

    - find_file_candidates(outcome) -> list[dict]
    - materialize_files(outcome, target_path) -> int
    - materialize_artifacts(plan_or_outcome, target_path) -> int

Design goals:
    * Cross-platform (Windows-safe) path handling.
    * Never escape *target_path* (security): all writes are confined under target.
    * Lazy-import artifact fetchers; run only when specified by the plan.
    * Small, robust logs – INFO for summary/decisions, DEBUG for details.

Compatibility additions:
    * If no artifacts are present in the plan but a manifest URL exists ANYWHERE in either
      plan or outcome (keys: "manifest_url", "source_url", "provenance.source_url"),
      and MATRIX_SDK_ALLOW_MANIFEST_FETCH is enabled (default ON), fetch the remote
      manifest, extract "repository"/"repositories", and clone them.
    * If an embedded manifest object exists at plan.manifest, use it directly (no network).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Surface artifact failures as manifest-resolution errors
from ..manifest import ManifestResolutionError

# ----------------------------------------------------------------------------
# Centralized logger / helpers (with safe fallback during migration)
# ----------------------------------------------------------------------------
try:
    from .util import HTTP_TIMEOUT, _env_bool, _short
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

    def _env_bool(name: str, default: bool = False) -> bool:  # type: ignore[override]
        v = (os.getenv(name) or "").strip().lower()
        if not v:
            return default
        return v in {"1", "true", "yes", "on"}

    # sensible default if util not available
    HTTP_TIMEOUT = int((os.getenv("MATRIX_SDK_HTTP_TIMEOUT") or 15))

logger = _LOGGER

# ----------------------------------------------------------------------------
# Lazy import fetchers (git / http)
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


def materialize_artifacts(plan_or_outcome: Dict[str, Any], target_path: Path) -> int:
    """Fetch all artifacts declared in *plan_or_outcome* into *target_path*.

    Accepts either the plan-node dict or the entire outcome dict.
    """
    # Normalize inputs (prefer plan node if present)
    outcome = plan_or_outcome if isinstance(plan_or_outcome, dict) else {}
    plan = (
        outcome.get("plan") if isinstance(outcome.get("plan"), dict) else None
    ) or outcome

    artifacts = _collect_artifacts(plan)

    # Fallback to remote (or embedded) manifest if no explicit artifacts were found
    if not artifacts and _env_bool("MATRIX_SDK_ALLOW_MANIFEST_FETCH", True):
        logger.debug("materialize(artifacts): none in plan; trying manifest fallback…")
        manifest_artifacts = _collect_artifacts_from_manifest_url(plan, outcome)
        if manifest_artifacts:
            logger.info(
                "materialize(artifacts): manifest fallback discovered %d artifact(s)",
                len(manifest_artifacts),
            )
            artifacts = manifest_artifacts

    if not artifacts:
        logger.debug("materialize(artifacts): no artifacts to fetch.")
        return 0

    logger.info("materialize(artifacts): fetching %d artifact(s)", len(artifacts))
    count = 0
    for idx, a in enumerate(artifacts, start=1):
        if not isinstance(a, dict):
            logger.debug("materialize(artifacts): skipping non-dict artifact #%d", idx)
            continue
        try:
            kind = (a.get("kind") or a.get("type") or "").lower()
            is_git = (
                kind == "git"
                or (isinstance(a.get("spec"), dict) and bool(a["spec"].get("repo")))
                or bool(a.get("repo"))
            )
            if is_git:
                _handle_git_artifact(a, target_path)
                count += 1
            elif a.get("url"):
                _handle_http_artifact(a, target_path)
                count += 1
            else:
                logger.warning("materialize(artifacts): unknown artifact kind: %s", a)
        except (GitFetchError, ArchiveFetchError) as e:
            logger.error("artifact: failed to fetch: %s", e)
            raise ManifestResolutionError(str(e)) from e
        except Exception as e:
            logger.warning("materialize(artifacts): artifact #%d failed (%s)", idx, e)
            continue

    logger.info("materialize(artifacts): successfully fetched %d artifact(s).", count)
    return count


# =============================================================================
# Private: artifact discovery
# =============================================================================


def _collect_artifacts(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a normalized artifact list from various legacy/modern shapes."""
    artifacts: List[Dict[str, Any]] = []

    # 1) Primary list & singular artifact
    if isinstance(alist := plan.get("artifacts"), list):
        artifacts.extend(x for x in alist if isinstance(x, dict))
    if isinstance(aone := plan.get("artifact"), dict):
        artifacts.append(aone)

    # 2) Top-level repository / repositories
    artifacts.extend(_collect_repos_from_node(plan))

    # 3) Embedded manifest.*.repository / .repositories (if present in plan)
    for k in ("manifest", "source_manifest", "echo_manifest", "input_manifest"):
        if isinstance(m := plan.get(k), dict):
            artifacts.extend(_collect_repos_from_node(m))

    # 4) Convenience HTTP download key
    if isinstance(plan.get("download_url"), str):
        artifacts.append({"url": plan["download_url"], "unpack": True})

    dedup = _deduplicate_artifacts(artifacts)
    logger.debug("materialize(artifacts): collected %d unique artifact(s)", len(dedup))
    return dedup


def _collect_artifacts_from_manifest_url(
    plan: Dict[str, Any], outcome: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Fetch or use embedded manifest and extract repository nodes.

    Accepts multiple key shapes for better compatibility:
      - plan.manifest (embedded dict; used directly without network)
      - ANY depth: manifest_url, source_url, provenance.source_url
      - NEW: outcome.lockfile.entities[].provenance.source_url (Hub lockfile)
    """
    # (A) Use embedded manifest if present (no network I/O)
    if isinstance(plan.get("manifest"), dict):
        logger.debug("manifest(fetch): using embedded plan.manifest object.")
        artifacts = _collect_repos_from_node(plan["manifest"])
        dedup = _deduplicate_artifacts(artifacts)
        return dedup

    # (B) Try to locate a manifest URL anywhere in plan/outcome
    src = _find_manifest_url_anywhere(plan, outcome)

    # (C) If not found, look explicitly in the Hub lockfile
    if not src:
        uid_hint = _extract_uid_from_outcome(outcome)
        src = _pick_manifest_url_from_lockfile(outcome, uid_hint)
        if src:
            logger.debug(
                "manifest(fetch): using lockfile provenance.source_url: %s", src
            )

    if not src:
        logger.debug(
            "manifest(fetch): no manifest_url/source_url found in plan/outcome."
        )
        return []

    if not _host_allowed(src):
        logger.debug("manifest(fetch): host not allowed by allowlist: %s", src)
        return []

    logger.info("manifest(fetch): GET %s", src)
    try:
        data = _http_get_text(src, timeout=HTTP_TIMEOUT)
        manifest = json.loads(data)
    except Exception as e:
        logger.debug("manifest(fetch): failed to fetch/parse: %s", e)
        return []

    artifacts = _collect_repos_from_node(manifest)
    dedup = _deduplicate_artifacts(artifacts)
    if dedup:
        logger.debug(
            "manifest(fetch): discovered %d repository artifact(s) in manifest.",
            len(dedup),
        )
    else:
        logger.debug("manifest(fetch): no repository nodes in manifest.")
    return dedup


def _collect_repos_from_node(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    artifacts: List[Dict[str, Any]] = []
    if isinstance(repo := node.get("repository"), dict):
        if ga := _artifact_from_repository_node(repo):
            artifacts.append(ga)
    if isinstance(repos := node.get("repositories"), list):
        for r in repos:
            if isinstance(r, dict):
                if ga := _artifact_from_repository_node(r):
                    artifacts.append(ga)
    return artifacts


def _deduplicate_artifacts(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for a in artifacts:
        if isinstance(spec := a.get("spec"), dict):
            repo_s = str(spec.get("repo") or a.get("url") or "")
            sub = str(spec.get("subdir") or a.get("dest") or "")
            key = ("git:" + repo_s, sub)
        else:
            key = ("http:" + str(a.get("url") or ""), str(a.get("dest") or ""))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(a)
    return dedup


def _artifact_from_repository_node(repo: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(repo, dict):
        return None
    kind = (repo.get("type") or repo.get("kind") or "git").strip().lower()
    if kind != "git":
        return None

    url = (repo.get("url") or repo.get("repo") or "").strip()
    if not url:
        return None

    ref = (
        repo.get("ref")
        or repo.get("branch")
        or repo.get("tag")
        or repo.get("commit")
        or "HEAD"
    )
    subdir = repo.get("subdir") or repo.get("subdirectory") or repo.get("path")
    spec: Dict[str, Any] = {
        "repo": url,
        "ref": str(ref),
        "subdir": str(subdir) if subdir else None,
        "strip_vcs": bool(repo.get("strip_vcs", True)),
        "recurse_submodules": bool(repo.get("recurse_submodules", False)),
        "lfs": bool(repo.get("lfs", False)),
    }
    if "verify_sha" in repo:
        spec["verify_sha"] = str(repo.get("verify_sha") or "")
    if "sha" in repo and not spec.get("verify_sha"):
        spec["verify_sha"] = str(repo.get("sha") or "")
    if "depth" in repo:
        try:
            spec["depth"] = int(repo.get("depth") or 1)
        except Exception:
            pass

    return {"kind": "git", "spec": spec}


# =============================================================================
# Private: deep discovery helpers
# =============================================================================


def _pick_manifest_url_from_lockfile(
    outcome: Dict[str, Any], uid_hint: Optional[str] = None
) -> str:
    """Return provenance.source_url from outcome.lockfile.entities[*] (prefer matching uid)."""
    try:
        lf = outcome.get("lockfile")
        if not isinstance(lf, dict):
            return ""
        ents = lf.get("entities")
        if not isinstance(ents, list) or not ents:
            return ""

        # Prefer exact uid match
        if uid_hint:
            for e in ents:
                if not isinstance(e, dict):
                    continue
                if str(e.get("id") or "").strip() == uid_hint:
                    prov = e.get("provenance")
                    if isinstance(prov, dict):
                        src = str(prov.get("source_url") or "").strip()
                        if src:
                            return src

        # Fallback: first entity with a source_url
        for e in ents:
            if not isinstance(e, dict):
                continue
            prov = e.get("provenance")
            if isinstance(prov, dict):
                src = str(prov.get("source_url") or "").strip()
                if src:
                    return src
        return ""
    except Exception:
        return ""


def _extract_uid_from_outcome(outcome: Dict[str, Any]) -> str:
    """Best-effort extraction of a UID (type:id@version) from outcome.lockfile or top-level keys."""
    try:
        lf = outcome.get("lockfile")
        if isinstance(lf, dict):
            ents = lf.get("entities")
            if isinstance(ents, list) and ents:
                for e in ents:
                    if isinstance(e, dict):
                        uid = str(e.get("id") or "").strip()
                        if uid:
                            return uid
    except Exception:
        pass
    # Fallback: common top-level keys
    for k in ("uid", "entity_uid", "id"):
        v = outcome.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _check_node_for_url(node: Any) -> Optional[str]:
    """Checks a dictionary for manifest_url or source_url, including its provenance."""
    if not isinstance(node, dict):
        return None

    # Check direct keys first, with priority
    for key in ("manifest_url", "source_url"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Check provenance sub-dictionary
    prov = node.get("provenance")
    if isinstance(prov, dict):
        for key in ("manifest_url", "source_url"):
            value = prov.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _find_manifest_url_anywhere(plan: Dict[str, Any], outcome: Dict[str, Any]) -> str:
    """Deep-search plan and outcome for a usable manifest URL (iterative BFS)."""
    # A queue for breadth-first search, starting with the top-level objects.
    # The order [plan, outcome] preserves the search priority.
    queue: List[Any] = [plan, outcome]
    # Keep track of object ids we've already visited to avoid infinite loops
    visited = {id(plan), id(outcome)}

    # Initial check on top-level objects before deep scan
    for top_level_obj in (plan, outcome):
        url = _check_node_for_url(top_level_obj)
        if url:
            return url

    while queue:
        current_obj = queue.pop(0)

        if isinstance(current_obj, dict):
            # Add dictionary values to the queue for the next level of search
            for value in current_obj.values():
                if isinstance(value, (dict, list)) and id(value) not in visited:
                    url = _check_node_for_url(value)
                    if url:
                        return url
                    queue.append(value)
                    visited.add(id(value))
        elif isinstance(current_obj, list):
            # Add list items to the queue
            for item in current_obj:
                if isinstance(item, (dict, list)) and id(item) not in visited:
                    url = _check_node_for_url(item)
                    if url:
                        return url
                    queue.append(item)
                    visited.add(id(item))

    return ""


# =============================================================================
# Private: artifact handlers
# =============================================================================


def _handle_git_artifact(artifact: Dict[str, Any], target_path: Path) -> None:
    """Handle a git-based artifact with a best-effort legacy shim."""
    spec = artifact.get("spec") or {}

    # Legacy shapes: {type:git, url:..., branch/tag/ref/...}
    if (not isinstance(spec, dict)) or not spec:
        legacy_url = artifact.get("url") or artifact.get("repo")
        if legacy_url:
            spec = _artifact_from_repository_node({**artifact, "type": "git"}) or {}

    # Legacy shim: derive spec from a deprecated 'command: git clone ...' string
    cmd = str(artifact.get("command") or "").strip()
    if (not spec or not spec.get("repo")) and cmd.startswith("git clone"):
        logger.warning("artifact(git): SHIM: deriving spec from legacy 'command'.")
        try:
            parts = cmd.split()
            repo_idx = parts.index("clone") + 1
            repo = parts[repo_idx]
            ref = "HEAD"
            if "--branch" in parts:
                ref_idx = parts.index("--branch") + 1
                ref = parts[ref_idx]
            spec = {"repo": repo, "ref": ref, "strip_vcs": True}
            logger.info("artifact(git): SHIM: derived spec=%s", spec)
        except (ValueError, IndexError) as e:
            logger.error("artifact(git): SHIM parse failed (%s)", e)
            spec = {}

    if not isinstance(spec, dict) or not spec.get("repo"):
        raise ManifestResolutionError(
            f"git artifact: no valid spec/repo provided in artifact: {artifact!r}"
        )

    if fetch_git_artifact is None:
        raise ManifestResolutionError(
            "git artifact specified but git fetcher is unavailable"
        )

    logger.info(
        "artifact(git): fetching with spec %s into %s", spec, _short(target_path)
    )
    fetch_git_artifact(spec=spec, target=target_path)  # type: ignore[misc]


def _handle_http_artifact(artifact: Dict[str, Any], target_path: Path) -> None:
    """Handle a URL-based artifact using the archivefetch helper."""
    if fetch_http_artifact is None:
        raise ManifestResolutionError(
            "http artifact specified but http fetcher is unavailable"
        )
    url = artifact.get("url")
    if not url:
        raise ManifestResolutionError(f"http artifact is missing 'url': {artifact!r}")

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
# Private: path & HTTP utilities
# =============================================================================


def _secure_join(root: Path, rel: str) -> Optional[Path]:
    """Join *rel* under *root* and prevent directory traversal."""
    try:
        norm = rel.replace("\\", "/").strip("/")
        candidate = (root / norm).resolve()
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
        except Exception:
            return None
        return candidate
    except Exception:
        return None


def _http_get_text(url: str, *, timeout: int) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, */*;q=0.1",
            "User-Agent": "matrix-sdk-installer/1",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ct = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ct:
            try:
                charset = ct.split("charset=", 1)[1].split(";")[0].strip()
            except Exception:
                pass
        data = resp.read()
    return data.decode(charset, "replace")


def _host_allowed(url: str) -> bool:
    raw = (os.getenv("MATRIX_SDK_MANIFEST_DOMAINS") or "").strip()
    if not raw:
        return True
    host = (urlparse(url).hostname or "").lower()
    allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
    logger.debug("manifest(fetch): host=%s allowlist=%s", host, allowed)
    return host in allowed
