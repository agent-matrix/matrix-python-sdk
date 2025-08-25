# matrix_sdk/ssl_compat.py
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["configure_ssl_trust", "resolve_system_ca_file"]

# Conservative set of common OS CA bundle locations
_SYSTEM_CA_CANDIDATES: tuple[str, ...] = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/WSL
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/CentOS/Fedora/Alma/Rocky
    "/etc/ssl/cert.pem",  # macOS & Alpine
    "/usr/local/etc/openssl/cert.pem",  # Homebrew on macOS (Intel)
    "/opt/homebrew/etc/openssl@3/cert.pem",  # Homebrew on macOS (Apple Silicon)
    "/etc/ssl/certs/ca-bundle.crt",  # SUSE/Arch variants
)


_DEBUG = os.getenv("MATRIX_SSL_TRUST_DEBUG") in {"1", "true", "TRUE", "on", "On"}


def _first_existing(paths: Iterable[str]) -> Optional[str]:
    """Return the first existing file path from *paths*, else None."""
    for p in paths:
        try:
            if Path(p).exists():
                return p
        except Exception:
            # Extremely defensive: ignore rare filesystem errors
            pass
    return None


def resolve_system_ca_file() -> Optional[str]:
    """
    Best-effort resolution of a system CA bundle file on Unix/macOS.

    On Windows there isn't a canonical CA *file*; callers should prefer the OS
    store via the ``truststore`` package when available.
    """
    return _first_existing(_SYSTEM_CA_CANDIDATES)


def _env_overridden() -> bool:
    """Return True if user explicitly provided CA env overrides."""
    return bool(os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE"))


def _should_disable(mode: str) -> bool:
    return mode in {"off", "0", "false", "disabled"}


def _log(msg: str) -> None:
    if _DEBUG:
        # Print only in debug mode to avoid altering production output
        print(f"[ssl_compat] {msg}")


def configure_ssl_trust() -> None:
    """
    Harden TLS trust configuration for diverse environments.

    Modes via env ``MATRIX_SSL_TRUST`` (default: ``auto``):
      • ``off``        – no changes (use library defaults/certifi).
      • ``truststore`` – prefer the OS trust store via ``truststore`` lib.
      • ``system``     – set ``SSL_CERT_FILE`` to a known system bundle if found.
      • ``auto``       – try ``truststore``; if unavailable, fall back to ``system``.

    Never overrides user variables ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``.
    Safe to call multiple times (idempotent, process-wide effect only once).
    """
    mode = (os.getenv("MATRIX_SSL_TRUST", "auto") or "auto").strip().lower()

    # 1) Respect explicit user overrides
    if _env_overridden():
        _log("User CA env vars set; leaving TLS trust as-is.")
        return

    if _should_disable(mode):
        _log("MATRIX_SSL_TRUST=off; leaving TLS trust as-is.")
        return

    # 2) Prefer the OS trust store via 'truststore' if requested / in auto
    if mode in {"truststore", "auto"}:
        try:
            import truststore  # type: ignore

            truststore.inject_into_ssl()  # global injection
            _log("Injected OS trust store via 'truststore'.")
            return
        except Exception as e:  # pragma: no cover - optional dep
            _log(f"truststore unavailable or failed ({e!r}); trying system bundle.")

    # 3) Fallback to a known system CA bundle (Unix/macOS). On Windows the
    # OS store is not a file; if truststore isn't present we leave defaults.
    if mode in {"system", "auto"} and platform.system() != "Windows":
        ca = resolve_system_ca_file()
        if ca:
            # ssl.create_default_context respects SSL_CERT_FILE
            os.environ.setdefault("SSL_CERT_FILE", ca)
            _log(f"Using system CA file: {ca}")
            return

    _log("No trust adjustments applied (defaults in effect).")
