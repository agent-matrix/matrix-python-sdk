# SPDX-License-Identifier: MIT
# matrix_sdk/gitfetch.py
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

__all__ = ["GitFetchError", "fetch_git_artifact"]


class GitFetchError(RuntimeError):
    """Raised when a git artifact cannot be fetched safely."""


# ------------------------------- Data Structure for Spec --------------------------------- #


@dataclass(frozen=True)
class _GitSpec:
    """A validated, structured representation of the git artifact spec."""

    repo: str
    ref: str
    depth: int
    subdir: Optional[str]
    strip_vcs: bool
    recurse_submodules: bool
    lfs: bool
    verify_sha: Optional[str]

    @classmethod
    def from_mapping(cls, spec: Mapping[str, object]) -> "_GitSpec":
        """Parses a raw mapping into a validated _GitSpec object."""
        if not isinstance(spec, Mapping):
            raise GitFetchError("spec must be a mapping")

        # Defensive: reject unknown dangerous keys
        forbidden = {"command", "shell", "args", "script"}
        if any(k in spec for k in forbidden):
            raise GitFetchError("forbidden key(s) present in spec")

        try:
            depth = int(spec.get("depth", 1))
            if depth < 1:
                depth = 1
        except (ValueError, TypeError):
            depth = 1

        return cls(
            repo=str(spec.get("repo") or "").strip(),
            ref=str(spec.get("ref") or "").strip(),
            depth=depth,
            subdir=_normalize_subdir(spec.get("subdir") if "subdir" in spec else None),
            strip_vcs=bool(spec.get("strip_vcs", True)),
            recurse_submodules=bool(spec.get("recurse_submodules", False)),
            lfs=bool(spec.get("lfs", False)),
            verify_sha=str(spec.get("verify_sha") or "").strip() or None,
        )


# ------------------------------- logging ------------------------------------ #


def _default_logger() -> logging.Logger:
    """A conservative default logger."""
    name = "matrix_sdk.gitfetch"
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = "[matrix-sdk][git] %(levelname)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        logger.setLevel(
            logging.INFO
            if os.getenv("MATRIX_SDK_DEBUG_GIT") == "1"
            else logging.WARNING
        )
        logger.propagate = False
    return logger


def _log(logger: Optional[logging.Logger]) -> logging.Logger:
    return logger or _default_logger()


# ------------------------------ validation & Helpers ---------------------------------- #


def _validate_spec_security(spec: _GitSpec, allow_hosts: Iterable[str]):
    """Runs security checks against a parsed git spec."""
    allow_http = os.getenv("MATRIX_GIT_ALLOW_INSECURE") == "1"
    if not _is_https_repo(spec.repo, allow_http=allow_http):
        raise GitFetchError(
            "repo must be an https URL (set MATRIX_GIT_ALLOW_INSECURE=1 to allow http)"
        )

    if not _host_allowed(spec.repo, allow_hosts):
        p = urlsplit(spec.repo)
        raise GitFetchError(f"host not allowed: {p.hostname or ''}")

    if not _safe_ref(spec.ref):
        raise GitFetchError("invalid ref (unsafe characters)")


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
    normalized = {h.strip().lower() for h in allow_hosts if h and h.strip()}
    if not normalized:
        return False  # Deny by default if allow-list is empty
    return any(host == h or host.endswith("." + h) for h in normalized)


def _normalize_subdir(subdir: Optional[str]) -> Optional[str]:
    if not subdir:
        return None
    return str(subdir).strip().lstrip("/").rstrip("/") or None


def _safe_ref(ref: str) -> bool:
    """Best-effort validation for git refs."""
    if not ref or any(ch.isspace() for ch in ref):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-@")
    return all(ch in allowed for ch in ref)


def _run_git(args: list[str], *, timeout: int, logger: logging.Logger, **kwargs) -> str:
    """Runs a git command, raising GitFetchError on failure."""
    redacted_cmd = kwargs.get("redacted", args)
    logger.debug("exec: %s", " ".join(redacted_cmd))
    try:
        proc = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", "replace").strip()
        if err := proc.stderr.decode("utf-8", "replace").strip():
            logger.debug("stderr: %s", err)
        return out
    except subprocess.TimeoutExpired as e:
        raise GitFetchError(f"git command timed out: {' '.join(args[:3])}…") from e
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", "replace").strip() if e.stderr else ""
        raise GitFetchError(f"git failed ({e.returncode}): {args[:3]}… — {err}") from e
    except FileNotFoundError as e:
        raise GitFetchError("git not found; install git or set MATRIX_GIT_BIN") from e


def _copy_tree(src: Path, dst: Path, *, exclude: set[str] | None = None) -> None:
    """Recursively copies a directory tree."""
    exclude = exclude or set()
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name not in exclude:
            target = dst / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target)


# ------------------------------ Core Execution Logic ---------------------------------- #


def _execute_git_clone(
    spec: _GitSpec,
    target: Path,
    git_bin: str,
    timeout: int,
    lg: logging.Logger,
) -> None:
    """Performs the git operations in a temporary directory and copies to target."""
    _run_git([git_bin, "--version"], timeout=15, logger=lg)

    with tempfile.TemporaryDirectory(prefix="matrix-git-") as tmpdir:
        tmp = Path(tmpdir)

        # Build and run the main clone command
        clone_cmd = [
            git_bin,
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            "--depth",
            str(spec.depth),
        ]
        if spec.recurse_submodules:
            clone_cmd.append("--recurse-submodules")
        clone_cmd.extend([spec.repo, str(tmp)])
        _run_git(
            clone_cmd,
            timeout=timeout,
            logger=lg,
            redacted=[*clone_cmd[:-2], "<repo>", "<tmp>"],
        )

        # Handle sparse checkout if a subdirectory is requested
        if spec.subdir:
            _run_git(
                [git_bin, "-C", str(tmp), "sparse-checkout", "init", "--cone"],
                timeout=timeout,
                logger=lg,
            )
            _run_git(
                [git_bin, "-C", str(tmp), "sparse-checkout", "set", spec.subdir],
                timeout=timeout,
                logger=lg,
            )

        # Checkout the specific revision
        _run_git(
            [
                git_bin,
                "-C",
                str(tmp),
                "-c",
                "advice.detachedHead=false",
                "checkout",
                "--detach",
                spec.ref,
            ],
            timeout=timeout,
            logger=lg,
        )

        # Handle Git LFS if requested
        if spec.lfs:
            try:
                _run_git(
                    [git_bin, "-C", str(tmp), "lfs", "pull"], timeout=timeout, logger=lg
                )
            except GitFetchError as e:
                lg.warning("git lfs pull failed (continuing): %s", e)

        # Verify commit hash if requested
        if spec.verify_sha:
            head = _run_git(
                [git_bin, "-C", str(tmp), "rev-parse", "HEAD"],
                timeout=timeout,
                logger=lg,
            )
            if not head.lower().startswith(spec.verify_sha.lower()):
                raise GitFetchError(
                    f"HEAD {head[:12]}… does not match verify_sha={spec.verify_sha}"
                )

        # Copy the final files to the target directory
        src = tmp / spec.subdir if spec.subdir else tmp
        if not src.is_dir():
            raise GitFetchError(
                f"cloned repository or subdir '{spec.subdir}' not found"
            )

        _copy_tree(src, target, exclude={".git"} if spec.strip_vcs else None)


# ------------------------------ Public API ---------------------------------- #


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
    Safely materializes a git artifact into `target` based on a structured spec.
    """
    lg = _log(logger)

    # 1. Parse the input spec into a structured object
    parsed_spec = _GitSpec.from_mapping(spec)

    # 2. Validate the spec against security rules
    default_hosts = ["github.com", "gitlab.com", "bitbucket.org"]
    env_hosts = os.getenv("MATRIX_GIT_ALLOWED_HOSTS")
    env_hosts_list = [h.strip() for h in env_hosts.split(",")] if env_hosts else []
    effective_allow_list = (
        allow_hosts if allow_hosts is not None else (env_hosts_list or default_hosts)
    )
    _validate_spec_security(parsed_spec, effective_allow_list)

    # 3. Prepare the target directory
    target_path = Path(target).expanduser().resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    lg.info(
        "cloning repo=%s ref=%s depth=%s subdir=%s",
        parsed_spec.repo,
        parsed_spec.ref,
        parsed_spec.depth,
        parsed_spec.subdir or "-",
    )

    # 4. Execute the git operations
    _execute_git_clone(
        spec=parsed_spec,
        target=target_path,
        git_bin=git_bin,
        timeout=timeout,
        lg=lg,
    )

    lg.info("materialized repository into target=%s", target_path)
