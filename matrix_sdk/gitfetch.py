# SPDX-License-Identifier: MIT
# matrix_sdk/gitfetch.py
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

__all__ = ["GitFetchError", "fetch_git_artifact"]


class GitFetchError(RuntimeError):
    """Raised when a git artifact cannot be fetched safely."""


# ------------------------------- logging ------------------------------------ #

def _default_logger() -> logging.Logger:
    """
    A conservative default logger:
      - INFO level only if MATRIX_SDK_DEBUG_GIT=1, else silent.
    """
    name = "matrix_sdk.gitfetch"
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    fmt = "[matrix-sdk][git] %(levelname)s: %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO if os.getenv("MATRIX_SDK_DEBUG_GIT") == "1" else logging.WARNING)
    logger.propagate = False
    return logger


def _log(logger: Optional[logging.Logger]) -> logging.Logger:
    return logger or _default_logger()


# ------------------------------ validation ---------------------------------- #

def _is_https_repo(url: str, *, allow_http: bool = False) -> bool:
    try:
        p = urlsplit(url)
        if allow_http and p.scheme == "http":
            return bool(p.netloc)
        return p.scheme == "https" and bool(p.netloc)
    except Exception:
        return False


def _host_allowed(repo_url: str, allow_hosts: Iterable[str]) -> bool:
    p = urlsplit(repo_url)
    host = (p.hostname or "").lower()
    if not host:
        return False
    normalized = [h.strip().lower() for h in allow_hosts if h and h.strip()]
    if not normalized:
        # if no allow-list, deny by default (defensive)
        return False
    for h in normalized:
        if host == h or host.endswith("." + h):
            return True
    return False


def _normalize_subdir(subdir: Optional[str]) -> Optional[str]:
    if not subdir:
        return None
    s = str(subdir).strip().lstrip("/").rstrip("/")
    return s or None


def _safe_ref(ref: str) -> bool:
    """
    Best-effort validation: reject obviously unsafe refs (spaces, control chars).
    Accepts tags/branches/commit SHAs like v1.2.3, main, feature/x, abcdef... etc.
    """
    if not ref:
        return False
    if any(ch.isspace() for ch in ref):
        return False
    # keep permissive: allow alnum + /._-@
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-@")
    return all(ch in allowed for ch in ref)


# ------------------------------ subprocess ---------------------------------- #

def _run_git(
    args: list[str],
    *,
    timeout: int,
    logger: logging.Logger,
    redacted: Optional[list[str]] = None,
) -> str:
    """
    Run a git command (`shell=False`), raise GitFetchError on failure.
    Returns stdout decoded as UTF-8 (stripped).
    """
    redacted_cmd = redacted if redacted is not None else args
    logger.debug("exec: %s", " ".join(redacted_cmd))
    try:
        proc = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if err:
            logger.debug("stderr: %s", err)
        return out
    except subprocess.TimeoutExpired as e:
        raise GitFetchError(f"git command timed out: {' '.join(args[:3])} …") from e
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="replace").strip() if e.stderr else ""
        # Keep a concise error, include a hint of the command for diagnostics
        raise GitFetchError(f"git failed ({e.returncode}): {args[:3]}… — {err}") from e
    except FileNotFoundError as e:
        raise GitFetchError("git not found; install git or set MATRIX_GIT_BIN") from e


# ------------------------------ copy helpers -------------------------------- #

def _copy_tree(src: Path, dst: Path, *, exclude: set[str] | None = None) -> None:
    exclude = exclude or set()
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in exclude:
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


# ------------------------------ public API ---------------------------------- #

def fetch_git_artifact(
    *,
    spec: Mapping[str, object],
    target: Path,
    git_bin: str = "git",
    allow_hosts: Optional[Iterable[str]] = None,
    timeout: int = 180,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Safely materialize a git artifact described by a *structured* spec into `target`.

    Supported spec keys:
        repo (str, required)  : HTTPS repository URL
        ref (str, required)   : tag/branch/commit to checkout
        subdir (str, opt)     : copy only this subdirectory (sparse-checkout)
        depth (int, opt)      : shallow clone depth (default: 1)
        strip_vcs (bool, opt) : remove .git in the final target (default: true)
        recurse_submodules (bool, opt) : clone submodules (default: false)
        lfs (bool, opt)       : attempt `git lfs pull` after checkout (default: false)
        verify_sha (str, opt) : if provided, verify HEAD starts with this SHA prefix

    Safety:
        - HTTPS-only by default (set MATRIX_GIT_ALLOW_INSECURE=1 to allow http)
        - Host allowlist (default: github.com, gitlab.com, bitbucket.org; override
          via MATRIX_GIT_ALLOWED_HOSTS or function arg)
        - No shells; subprocess with explicit argv
        - Removes `.git` in final target unless strip_vcs=False
    """
    lg = _log(logger)

    # ------------- parse/validate spec -------------
    if not isinstance(spec, Mapping):
        raise GitFetchError("spec must be a mapping")
    repo = str(spec.get("repo") or "").strip()
    ref = str(spec.get("ref") or "").strip()
    subdir = _normalize_subdir(spec.get("subdir") if "subdir" in spec else None)
    depth_raw = spec.get("depth", 1)
    strip_vcs = bool(spec.get("strip_vcs", True))
    recurse_submodules = bool(spec.get("recurse_submodules", False))
    lfs = bool(spec.get("lfs", False))
    verify_sha = str(spec.get("verify_sha") or "").strip() or None

    # Defensive: reject unknown dangerous keys (like "command")
    forbidden = {"command", "shell", "args", "script"}
    if any(k in spec for k in forbidden):
        raise GitFetchError("forbidden key(s) present in spec")

    # Normalize depth
    try:
        depth = int(depth_raw)
        if depth < 1:
            depth = 1
    except Exception:
        depth = 1

    allow_http = os.getenv("MATRIX_GIT_ALLOW_INSECURE") == "1"
    if not _is_https_repo(repo, allow_http=allow_http):
        raise GitFetchError("repo must be an https URL (set MATRIX_GIT_ALLOW_INSECURE=1 to allow http)")

    default_hosts = ["github.com", "gitlab.com", "bitbucket.org"]
    env_hosts = os.getenv("MATRIX_GIT_ALLOWED_HOSTS")
    env_hosts_list = [h.strip() for h in env_hosts.split(",")] if env_hosts else []
    allow_list = list(allow_hosts) if allow_hosts is not None else (env_hosts_list or default_hosts)
    if not _host_allowed(repo, allow_list):
        p = urlsplit(repo)
        raise GitFetchError(f"host not allowed: {p.hostname or ''}")

    if not _safe_ref(ref):
        raise GitFetchError("invalid ref (unsafe characters)")

    # Prepare target
    target = Path(target).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    # ------------- run git steps -------------
    lg.info("cloning repo=%s ref=%s depth=%s subdir=%s", repo, ref, depth, subdir or "-")

    # Validate git presence
    _run_git([git_bin, "--version"], timeout=15, logger=lg)

    # Use a temp directory for the clone
    with tempfile.TemporaryDirectory(prefix="matrix-git-") as tmpdir:
        tmp = Path(tmpdir)

        # Prefer minimal data transfer
        clone_cmd = [
            git_bin,
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth",
            str(depth),
        ]
        if recurse_submodules:
            clone_cmd.append("--recurse-submodules")
        clone_cmd.extend([repo, str(tmp)])
        _run_git(clone_cmd, timeout=timeout, logger=lg, redacted=[*clone_cmd[:-2], "<repo>", "<tmp>"])

        # Optional sparse checkout if subdir requested
        if subdir:
            # init cone mode, then set subdir
            _run_git([git_bin, "-C", str(tmp), "sparse-checkout", "init", "--cone"], timeout=timeout, logger=lg)
            _run_git([git_bin, "-C", str(tmp), "sparse-checkout", "set", subdir], timeout=timeout, logger=lg)

        # Checkout the requested ref in detached mode
        _run_git(
            [git_bin, "-C", str(tmp), "-c", "advice.detachedHead=false", "checkout", "--detach", ref],
            timeout=timeout,
            logger=lg,
        )

        # Optional: Git LFS pull (no-op if LFS isn't installed)
        if lfs:
            try:
                _run_git([git_bin, "-C", str(tmp), "lfs", "pull"], timeout=timeout, logger=lg)
            except GitFetchError as e:
                # Don't fail the whole operation if LFS isn't available; log a warning
                lg.warning("git lfs pull failed (continuing): %s", e)

        # Optional: verify HEAD SHA prefix
        if verify_sha:
            head = _run_git([git_bin, "-C", str(tmp), "rev-parse", "--verify", "HEAD"], timeout=timeout, logger=lg)
            if not head.lower().startswith(verify_sha.lower()):
                raise GitFetchError(f"HEAD {head[:12]}… does not match verify_sha={verify_sha}")

        # Copy to target (subdir if set)
        src = tmp / subdir if subdir else tmp
        if not src.exists():
            raise GitFetchError(f"subdir not found: {subdir}")

        # Exclude VCS dir(s) on copy
        excludes = {".git"} if strip_vcs else set()
        _copy_tree(src, target, exclude=excludes)

        # Ensure no .git remains (paranoia)
        if strip_vcs:
            git_dir = target / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir, ignore_errors=True)

        lg.info("materialized repository into target=%s", target)
