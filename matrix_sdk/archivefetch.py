# SPDX-License-Identifier: MIT
"""
matrix_sdk.archivefetch

Production-ready helper to fetch HTTP(S) artifacts and (optionally) unpack them.
Used by matrix_sdk.installer.LocalInstaller to materialize plan artifacts.

Features
- HTTP(S) download via httpx with redirect support and timeout
- Optional SHA-256 integrity verification
- Optional unpack for .zip / .tar(.gz|.tgz) with path traversal protection
- Writes raw artifact to target/dest when requested
- Lightweight, library-safe logging (opt-in via MATRIX_SDK_DEBUG=1)

Public API
----------
fetch_http_artifact(
    url: str,
    target: pathlib.Path | str,
    dest: str | None = None,
    sha256: str | None = None,
    unpack: bool = False,
    timeout: int | float = 60,
    logger: logging.Logger | None = None,
) -> None
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

import httpx


__all__ = [
    "ArchiveFetchError",
    "fetch_http_artifact",
]

# --------------------------------------------------------------------------------------
# Logging (library-safe): use module logger; only attach a handler if MATRIX_SDK_DEBUG=1
# --------------------------------------------------------------------------------------
_log = logging.getLogger("matrix_sdk.archivefetch")


def _maybe_configure_logging() -> None:
    dbg = (os.getenv("MATRIX_SDK_DEBUG") or "").strip().lower()
    if dbg in ("1", "true", "yes", "on"):
        if not _log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("[matrix-sdk][archivefetch] %(levelname)s: %(message)s")
            )
            _log.addHandler(handler)
        _log.setLevel(logging.DEBUG)


_maybe_configure_logging()


class ArchiveFetchError(RuntimeError):
    """Raised when an HTTP artifact cannot be downloaded or verified."""


# --------------------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------------------
def _short(path: Path | str, maxlen: int = 120) -> str:
    s = str(path)
    return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


def _is_probably_zip(url: str, dest: Optional[str]) -> bool:
    u = url.lower()
    d = (dest or "").lower()
    return u.endswith(".zip") or d.endswith(".zip")


def _is_probably_targz(url: str, dest: Optional[str]) -> bool:
    u = url.lower()
    d = (dest or "").lower()
    return (
        u.endswith(".tar")
        or u.endswith(".tar.gz")
        or u.endswith(".tgz")
        or d.endswith(".tar")
        or d.endswith(".tar.gz")
        or d.endswith(".tgz")
    )


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _check_sha256(data: bytes, expected: str) -> None:
    digest = hashlib.sha256(data).hexdigest().lower()
    if digest != expected.lower():
        raise ArchiveFetchError(f"sha256 mismatch: expected={expected} got={digest}")


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """
    Extracts ZIP with protection against path traversal ("zip slip").
    """
    for member in zf.infolist():
        # Normalize member path
        member_path = Path(member.filename)
        # Skip directory entries; ZipFile handles them implicitly when extracting files
        if member.is_dir():
            continue
        # Compute final destination
        dest = (target_dir / member_path).resolve()
        if not str(dest).startswith(str(target_dir.resolve())):
            raise ArchiveFetchError(f"unsafe zip entry path: {member.filename}")
        _ensure_parent(dest)
        with zf.open(member, "r") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _safe_extract_tar(tf: tarfile.TarFile, target_dir: Path) -> None:
    """
    Extracts TAR/TGZ with protection against path traversal ("tar slip").
    """
    for member in tf.getmembers():
        # TarInfo with absolute path or parent traversal is not allowed
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise ArchiveFetchError(f"unsafe tar entry path: {name}")
        dest = (target_dir / name).resolve()
        if not str(dest).startswith(str(target_dir.resolve())):
            raise ArchiveFetchError(f"unsafe tar entry path: {name}")
        if member.isdir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        _ensure_parent(dest)
        # Extract file content safely
        extracted = tf.extractfile(member)
        if extracted is None:
            # Could be a special file type; skip silently
            continue
        with extracted as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _maybe_flatten_extracted_tree(target_path: Path) -> None:
    """
    If the target contains exactly one subdirectory at its root and nothing else,
    move that subdirectory's contents up to the target root.

    This handles the common GitHub ZIP pattern: <name>-<version>/...
    """
    try:
        entries = [p for p in target_path.iterdir() if p.is_dir()]
        # Only flatten when there is exactly one top-level directory
        if len(entries) != 1:
            _log.debug("flatten: skip (dirs at root: %d) → %s", len(entries), _short(target_path))
            return
        sub = entries[0]
        _log.info("flatten: moving %s/* up into %s", sub.name, _short(target_path))
        for child in sub.iterdir():
            dest = target_path / child.name
            if dest.exists():
                _log.debug("flatten: skipping existing %s", _short(dest))
                continue
            shutil.move(str(child), str(dest))
        try:
            sub.rmdir()
        except Exception:
            _log.debug("flatten: could not remove now-empty %s (ignored)", sub)
    except Exception as e:
        _log.debug("flatten: skipped due to error: %s", e)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------
def fetch_http_artifact(
    *,
    url: str,
    target: Path | str,
    dest: Optional[str] = None,
    sha256: Optional[str] = None,
    unpack: bool = False,
    timeout: int | float = 60,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Download an artifact via HTTP(S), verify integrity, optionally unpack archives.

    Parameters
    ----------
    url : str
        HTTP(S) URL to download.
    target : Path | str
        Target directory (created if necessary).
    dest : Optional[str]
        Relative path (under target) to write the raw artifact. If omitted, the file
        is not saved as-is (useful when only unpacking).
    sha256 : Optional[str]
        If provided, the downloaded bytes are verified against this hex digest.
    unpack : bool
        If True, and the artifact looks like a ZIP/TAR (by extension) or is explicitly
        requested, extract into `target`.
    timeout : int | float
        Total request timeout (seconds).
    logger : Optional[logging.Logger]
        Optional logger; otherwise the module logger is used.

    Raises
    ------
    ArchiveFetchError for network errors, integrity failures, or unsafe archives.
    """
    lg = logger or _log
    tgt = Path(target).expanduser().resolve()
    tgt.mkdir(parents=True, exist_ok=True)

    lg.info("http: GET %s", url)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.content
    except httpx.RequestError as e:
        raise ArchiveFetchError(f"download failed: {e}") from e
    except httpx.HTTPStatusError as e:
        raise ArchiveFetchError(f"http {e.response.status_code} for {url}") from e

    lg.debug("http: downloaded %d bytes from %s", len(data), url)

    # Integrity check
    if sha256:
        _check_sha256(data, sha256)
        lg.debug("http: sha256 verified OK")

    # Write raw artifact (optional)
    if dest:
        raw_path = (tgt / dest).resolve()
        if not str(raw_path).startswith(str(tgt)):
            raise ArchiveFetchError(f"refusing to write outside target: {raw_path}")
        _ensure_parent(raw_path)
        raw_path.write_bytes(data)
        lg.debug("http: wrote raw artifact → %s", _short(raw_path))

    # Decide whether to unpack
    looks_zip = _is_probably_zip(url, dest)
    looks_tar = _is_probably_targz(url, dest)
    should_unpack = unpack or looks_zip or looks_tar
    lg.debug(
        "http: unpack? %s (zip=%s, tar=%s, flag=%s)",
        should_unpack,
        looks_zip,
        looks_tar,
        unpack,
    )

    if not should_unpack:
        return

    bio = io.BytesIO(data)
    if looks_zip:
        try:
            with zipfile.ZipFile(bio) as zf:
                _safe_extract_zip(zf, tgt)
                lg.debug("http: zip extracted %d members", len(zf.namelist()))
        except zipfile.BadZipFile as e:
            raise ArchiveFetchError(f"bad zip file: {e}") from e
    elif looks_tar:
        mode = "r:gz" if (url.lower().endswith((".tar.gz", ".tgz")) or (dest or "").lower().endswith((".tar.gz", ".tgz"))) else "r:"
        try:
            with tarfile.open(fileobj=bio, mode=mode) as tf:
                _safe_extract_tar(tf, tgt)
                lg.debug("http: tar extracted members")
        except tarfile.TarError as e:
            raise ArchiveFetchError(f"bad tar file: {e}") from e
    else:
        # If flagged to unpack but we cannot infer type, try zip first then tar as fallback
        try:
            with zipfile.ZipFile(bio) as zf:
                _safe_extract_zip(zf, tgt)
                lg.debug("http: zip extracted (fallback type detection)")
        except Exception:
            bio.seek(0)
            try:
                with tarfile.open(fileobj=bio, mode="r:*") as tf:
                    _safe_extract_tar(tf, tgt)
                    lg.debug("http: tar extracted (fallback type detection)")
            except Exception as e:
                raise ArchiveFetchError(f"cannot unpack unknown archive type: {e}") from e

    # Flatten single top-level dir (GitHub ZIP pattern)
    _maybe_flatten_extracted_tree(tgt)
