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

Change summary:
    * When plan.artifacts is empty, we synthesize git artifacts from any
      repositories declared in the manifest (embedded or via provenance URL).
      This makes repository cloning the default behavior when the manifest
      declares them.
    * If the manifest doesn't specify a ref, we omit it so git uses the repo's
      default branch (safer than forcing 'master').
    * If a provided ref is blank/unsafe, we sanitize it; if it remains invalid,
      we omit it to use the default branch instead of failing.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

    Behavior:
        - If plan.artifacts is present and non-empty, honor it exactly.
        - Otherwise, attempt to synthesize git artifacts from repositories
          declared in the embedded manifest or via its provenance URL.
          (If ref is unspecified, we omit it so git uses the repo's default branch.)

    Raises
    ------
    ManifestResolutionError
        If a git or http artifact fetcher raises a fetch error.
    """
    artifacts = plan.get("artifacts", [])
    if not isinstance(artifacts, list):
        artifacts = []

    # If no artifacts declared, synthesize from repositories in the manifest.
    if not artifacts:
        repos = _discover_repositories_from_plan_or_manifest(plan)
        if repos:
            synthesized: List[Dict[str, Any]] = []
            for repo_url, repo_ref in repos:
                spec: Dict[str, Any] = {
                    "repo": repo_url,
                    "strip_vcs": True,
                    "recurse_submodules": False,
                    "lfs": False,
                }
                # Only include ref when provided and non-empty; otherwise use default branch
                if repo_ref:
                    cleaned = _clean_ref(repo_ref)
                    if cleaned:
                        spec["ref"] = cleaned
                synthesized.append({"kind": "git", "spec": spec})
            artifacts = synthesized
            logger.info(
                "materialize(artifacts): synthesized %d git artifact(s) from manifest repositories",
                len(artifacts),
            )
        else:
            logger.debug("materialize(artifacts): no artifacts to fetch.")
            return 0

    logger.info("materialize(artifacts): fetching %d artifact(s)", len(artifacts))
    count = 0
    for idx, a in enumerate(artifacts, start=1):
        if not isinstance(a, dict):
            logger.debug("materialize(artifacts): skipping non-dict artifact #%d", idx)
            continue
        try:
            kind = (a.get("kind") or "").lower()
            if kind == "git":
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

    # Final ref sanitation: allow default-branch fallback
    raw_ref = spec.get("ref")
    cleaned_ref = _clean_ref(raw_ref)

    if cleaned_ref:
        spec["ref"] = cleaned_ref
    else:
        # This block now covers all invalid cases.
        # WORKAROUND: The underlying fetcher fails when 'ref' is missing,
        # so we explicitly set it to 'HEAD' to use the default branch.
        if raw_ref is not None:  # Log only if an invalid ref was actually provided
            logger.info(
                "artifact(git): provided ref '%s' was invalid, falling back to default branch.",
                raw_ref,
            )

        spec["ref"] = "HEAD"  # Set ref to HEAD for the default branch
        logger.debug(
            "artifact(git): no valid ref specified; using repo's default branch (HEAD)."
        )

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


# =============================================================================
# Private: repository discovery (manifest → repositories)
# =============================================================================


def _discover_repositories_from_plan_or_manifest(
    plan: Dict[str, Any],
) -> List[Tuple[str, Optional[str]]]:
    """Return a list of (repo_url, optional_ref) discovered from the plan/manifest.
    Discovery order:
      1) plan['repositories'] (list of {url, ref}) or plan['repository'] ({url, ref} or string url)
      2) embedded manifest nodes:
        plan['manifest'], 'source_manifest', 'echo_manifest', 'input_manifest'
      3) fetch manifest via provenance/lockfile URL(s) when present
         (supports GitHub blob/raw normalization)
    Returns an empty list if nothing is found.
    """
    repos: List[Tuple[str, Optional[str]]] = []

    def _add(url: Optional[str], ref: Optional[str]) -> None:
        if url:
            url_s = url.strip()
            ref_s = ref.strip() if isinstance(ref, str) and ref.strip() else None
            if url_s:
                repos.append((url_s, ref_s))

    # 1) Direct on plan
    _extract_repositories_from_container(plan, _add)

    # 2) Embedded manifest-like nodes
    for key in ("manifest", "source_manifest", "echo_manifest", "input_manifest"):
        node = plan.get(key)
        if isinstance(node, dict):
            _extract_repositories_from_container(node, _add)

    # If we already found some, stop here (no network needed)
    if repos:
        _dedupe_inplace(repos)
        return repos

    # 3) Try fetching manifest by provenance/lockfile URLs (network)
    for m_url in _iter_manifest_urls(plan):
        try:
            data = _http_get_text(
                _normalize_manifest_like_url(m_url),
                timeout=int(os.getenv("MATRIX_SDK_HTTP_TIMEOUT") or 15),
            )
            manifest = json.loads(data)
            _extract_repositories_from_container(manifest, _add)
        except Exception as e:
            logger.debug(
                "materialize(artifacts): failed to fetch/parse manifest %s (%s)",
                m_url,
                e,
            )

    _dedupe_inplace(repos)
    return repos


def _extract_repositories_from_container(node: Dict[str, Any], add_cb) -> None:
    """Pull repository declarations from a dict and add via callback."""
    # Allow a single repository as dict OR string
    rep = node.get("repository")
    if isinstance(rep, str):
        url = rep.strip() or None
        if url:
            add_cb(url, None)
    elif isinstance(rep, dict):
        url = (rep.get("url") or "").strip() or None
        ref = (rep.get("ref") or "").strip() or None
        if url:
            add_cb(url, ref)

    # Multiple repositories
    reps = node.get("repositories")
    if isinstance(reps, list):
        for r in reps:
            if isinstance(r, str):
                url = r.strip() or None
                if url:
                    add_cb(url, None)
            elif isinstance(r, dict):
                url = (r.get("url") or "").strip() or None
                ref = (r.get("ref") or "").strip() or None
                if url:
                    add_cb(url, ref)


def _iter_manifest_urls(plan: Dict[str, Any]) -> List[str]:
    """Collect candidate manifest URLs from plan/provenance/lockfile (de-duplicated)."""
    cand: List[str] = []

    def _add(v: Optional[str]) -> None:
        v = (v or "").strip()
        if v:
            cand.append(v)

    # Plan-level hints
    _add(plan.get("manifest_url"))
    prov = plan.get("provenance") or {}
    if isinstance(prov, dict):
        _add(prov.get("manifest_url"))
        _add(prov.get("source_url"))

    # Outcome/lockfile provenance (plan may actually be the whole outcome)
    lf = plan.get("lockfile") or {}
    ents = lf.get("entities") or []
    if isinstance(ents, list):
        for ent in ents:
            if not isinstance(ent, dict):
                continue
            p = ent.get("provenance") or {}
            if isinstance(p, dict):
                _add(p.get("source_url") or p.get("manifest_url"))

    # stable order de-dupe
    out: List[str] = []
    seen: set[str] = set()
    for u in cand:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _normalize_manifest_like_url(src: str) -> str:
    """Normalize GitHub blob/refs → raw for easy JSON fetch."""
    s = (src or "").strip()
    if not s:
        return s
    # github blob → raw
    if "://github.com/" in s and "/blob/" in s:
        try:
            parts = s.split("://github.com/", 1)[1]
            owner_repo, _, rest = parts.partition("/blob/")
            return f"https://raw.githubusercontent.com/{owner_repo}/{rest}"
        except Exception:
            return s
    # raw refs/heads -> branch
    if "://raw.githubusercontent.com/" in s and "/refs/heads/" in s:
        return s.replace("/refs/heads/", "/")
    return s


def _http_get_text(url: str, *, timeout: int = 15) -> str:
    """HTTP GET helper with hardened TLS (truststore/certifi) and sensible headers."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, */*;q=0.1",
            "User-Agent": "matrix-sdk-installer/1",
        },
        method="GET",
    )
    with urllib.request.urlopen(
        req, timeout=timeout, context=_ssl_ctx()
    ) as resp:  # nosec - controlled domains
        ct = resp.headers.get("Content-Type", "")
        charset = "utf-8"
        if "charset=" in ct:
            try:
                charset = ct.split("charset=", 1)[1].split(";")[0].strip()
            except Exception:
                pass
        return resp.read().decode(charset, "replace")


def _ssl_ctx() -> ssl.SSLContext:
    """Prefer OS trust (truststore) and fall back to certifi if available."""
    try:
        import truststore  # type: ignore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        try:
            import certifi  # type: ignore

            ctx.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass
        return ctx


def _dedupe_inplace(items: List[Tuple[str, Optional[str]]]) -> None:
    """In-place stable de-duplication of (url, optional_ref) tuples."""
    seen: set[Tuple[str, Optional[str]]] = set()
    i = 0
    while i < len(items):
        t = items[i]
        if t in seen:
            items.pop(i)
        else:
            seen.add(t)
            i += 1


# =============================================================================
# Private: ref sanitation
# =============================================================================


def _clean_ref(ref: Any) -> Optional[str]:
    """Return a safe ref string or None if it's missing/unsafe.

    We accept typical git ref names like:
      - 'main', 'master', 'develop'
      - 'refs/heads/main'
      - 'origin/main'
    We reject empty, whitespace-only, or refs containing whitespace/control chars.
    """
    if not isinstance(ref, str):
        return None
    r = ref.strip()
    if not r:
        return None
    # basic safety: disallow whitespace or control characters inside the ref
    if any(ch.isspace() for ch in r):
        return None
    # very conservative allow – we don't try to validate all git rules here
    return r
