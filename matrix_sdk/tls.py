# matrix_sdk/tls.py
from __future__ import annotations

from typing import Any, Dict
import os, ssl

__all__ = ["VERIFY", "_VERIFY", "HTTPX_DEFAULTS", "httpx_defaults", "http2_enabled"]

def _env_on(name: str, default: bool = True) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v == "": return default
    return v in {"1", "true", "yes", "on"}

def _env_first(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return ""

def verify_context() -> Any:
    if not _env_on("MATRIX_TLS_VERIFY", True):
        return False
    cafile = _env_first("MATRIX_TLS_CA_FILE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
    if cafile:
        return cafile

    prefer = (os.getenv("MATRIX_TLS_PREFER") or "truststore").strip().lower()

    if prefer == "certifi":
        try:
            import certifi  # type: ignore
            return certifi.where()
        except Exception:
            pass
        try:
            import truststore  # type: ignore
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            return True
    else:
        try:
            import truststore  # type: ignore
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            pass
        try:
            import certifi  # type: ignore
            return certifi.where()
        except Exception:
            return True

VERIFY: Any = verify_context()
_VERIFY: Any = VERIFY  # legacy alias

HTTPX_DEFAULTS: Dict[str, Any] = {
    "verify": VERIFY,
    "trust_env": True,
}

def httpx_defaults(**extra: Any) -> Dict[str, Any]:
    opts: Dict[str, Any] = dict(HTTPX_DEFAULTS)
    opts.update(extra)
    return opts

def http2_enabled(default: bool = False) -> bool:
    v = (os.getenv("MATRIX_HTTP2") or "").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        try:
            import h2  # type: ignore
            return True
        except Exception:
            return False
    return default
