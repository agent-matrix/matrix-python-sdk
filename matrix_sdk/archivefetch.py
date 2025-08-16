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
                logging.Formatter(
                    "[matrix-sdk][archivefetch] %(levelname)s: %(message)s"
                )
            )
            _log.addHandler(handler)
        _log.setLevel(logging.DEBUG)


_maybe_configure_logging()


class ArchiveFetchError(RuntimeError):
    """Raised when an HTTP artifact cannot be downloaded or verified."""


# --------------------------------------------------------------------------------------
# Internal Helper Functions
# --------------------------------------------------------------------------------------
def _short(path: Path | str, maxlen: int = 120) -> str:
    s = str(path)
    return s if len(s) <= maxlen else ("…" + s[-(maxlen - 1) :])


def _is_probably_zip(url: str, dest: Optional[str]) -> bool:
    return url.lower().endswith(".zip") or (dest or "").lower().endswith(".zip")


def _is_probably_targz(url: str, dest: Optional[str]) -> bool:
    extensions = (".tar", ".tar.gz", ".tgz")
    u_lower = url.lower()
    d_lower = (dest or "").lower()
    return any(u_lower.endswith(ext) for ext in extensions) or any(
        d_lower.endswith(ext) for ext in extensions
    )


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _check_sha256(data: bytes, expected: str) -> None:
    digest = hashlib.sha256(data).hexdigest().lower()
    if digest != expected.lower():
        raise ArchiveFetchError(f"sha256 mismatch: expected={expected} got={digest}")


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Extracts ZIP with protection against path traversal ("zip slip")."""
    for member in zf.infolist():
        if member.is_dir():
            continue
        dest = (target_dir / member.filename).resolve()
        if not str(dest).startswith(str(target_dir.resolve())):
            raise ArchiveFetchError(f"unsafe zip entry path: {member.filename}")
        _ensure_parent(dest)
        with zf.open(member, "r") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _safe_extract_tar(tf: tarfile.TarFile, target_dir: Path) -> None:
    """Extracts TAR/TGZ with protection against path traversal ("tar slip")."""
    for member in tf.getmembers():
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
        extracted = tf.extractfile(member)
        if extracted is not None:
            with extracted as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def _maybe_flatten_extracted_tree(target_path: Path) -> None:
    """Handles the common GitHub ZIP pattern of a single top-level directory."""
    try:
        entries = [p for p in target_path.iterdir() if p.is_dir()]
        if len(entries) != 1:
            _log.debug("flatten: skip (dirs at root: %d)", len(entries))
            return

        sub = entries[0]
        _log.info("flatten: moving %s/* up into %s", sub.name, _short(target_path))
        for child in sub.iterdir():
            dest = target_path / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))

        try:
            sub.rmdir()
        except OSError:
            _log.debug("flatten: could not remove now-empty %s (ignored)", sub)
    except Exception as e:
        _log.debug("flatten: skipped due to error: %s", e)


# --- NEW: Extracted function to handle the complexity of unpacking ---
def _unpack_archive(
    data: bytes, target_dir: Path, url: str, dest: Optional[str], lg: logging.Logger
) -> None:
    """
    Detects archive type from downloaded data and unpacks it safely.
    This function contains the logic that was previously making fetch_http_artifact too complex.
    """
    is_zip = _is_probably_zip(url, dest)
    is_tar = _is_probably_targz(url, dest)
    bio = io.BytesIO(data)

    try:
        if is_zip:
            with zipfile.ZipFile(bio) as zf:
                _safe_extract_zip(zf, target_dir)
                lg.debug("http: zip extracted %d members", len(zf.namelist()))
        elif is_tar:
            mode = (
                "r:gz"
                if url.lower().endswith((".tgz", ".tar.gz"))
                or (dest or "").lower().endswith((".tgz", ".tar.gz"))
                else "r:*"
            )
            with tarfile.open(fileobj=bio, mode=mode) as tf:
                _safe_extract_tar(tf, target_dir)
                lg.debug("http: tar extracted members")
        else:
            # Fallback for unknown types when unpack=True
            try:
                with zipfile.ZipFile(bio) as zf:
                    _safe_extract_zip(zf, target_dir)
                    lg.debug("http: zip extracted (fallback detection)")
            except zipfile.BadZipFile:
                bio.seek(0)
                with tarfile.open(fileobj=bio, mode="r:*") as tf:
                    _safe_extract_tar(tf, target_dir)
                    lg.debug("http: tar extracted (fallback detection)")
    except (zipfile.BadZipFile, tarfile.TarError) as e:
        raise ArchiveFetchError(f"bad archive file: {e}") from e
    except Exception as e:
        raise ArchiveFetchError(f"cannot unpack unknown archive type: {e}") from e

    _maybe_flatten_extracted_tree(target_dir)


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
    Download an artifact via HTTP(S), verify integrity, and optionally unpack archives.
    """
    lg = logger or _log
    tgt = Path(target).expanduser().resolve()
    tgt.mkdir(parents=True, exist_ok=True)

    # Step 1: Download the artifact
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

    # Step 2: Verify integrity (optional)
    if sha256:
        _check_sha256(data, sha256)
        lg.debug("http: sha256 verified OK")

    # Step 3: Write raw artifact to disk (optional)
    if dest:
        raw_path = (tgt / dest).resolve()
        if not str(raw_path).startswith(str(tgt)):
            raise ArchiveFetchError(f"refusing to write outside target: {raw_path}")
        _ensure_parent(raw_path)
        raw_path.write_bytes(data)
        lg.debug("http: wrote raw artifact → %s", _short(raw_path))

    # Step 4: Unpack the archive if requested
    should_unpack = (
        unpack or _is_probably_zip(url, dest) or _is_probably_targz(url, dest)
    )
    lg.debug(
        "http: unpack? %s (explicit=%s, is_zip=%s, is_tar=%s)",
        should_unpack,
        unpack,
        _is_probably_zip(url, dest),
        _is_probably_targz(url, dest),
    )
    if should_unpack:
        _unpack_archive(data, tgt, url, dest, lg)
