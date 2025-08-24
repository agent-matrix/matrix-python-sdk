# tests/conftest.py
# Loads .env/.env.local (if present) and provides helpers for tests.
from __future__ import annotations

import os
from typing import Callable, Optional

import httpx
import pytest


# ---- minimal .env loader (no extra deps) ------------------------------------
def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # Do not override pre-set env (e.g., in CI)
            os.environ.setdefault(k, v)


# Load in priority order: .env.local then .env
_load_env_file(".env.local")
_load_env_file(".env")


# ---- helpers ----------------------------------------------------------------
def _env_first(keys: list[str], default: Optional[str] = None) -> Optional[str]:
    """
    Return the first non-empty environment value from the provided keys.
    """
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return default


# ---- pytest markers ----------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: test that may hit a real Matrix Hub")


# ---- fixtures ----------------------------------------------------------------
@pytest.fixture
def mock_transport_factory() -> (
    Callable[[Callable[[httpx.Request], httpx.Response]], httpx.MockTransport]
):
    """
    Factory returning an httpx.MockTransport from a handler function.

    Usage:
        def handler(request: httpx.Request) -> httpx.Response: ...
        transport = mock_transport_factory(handler)
        client = httpx.Client(transport=transport)
    """

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    return _factory


@pytest.fixture
def hub_base() -> str:
    """
    Canonical base URL for the Matrix Hub (new name).
    Falls back to older env names for compatibility.
    """
    return _env_first(
        ["MATRIX_HUB_BASE", "MATRIX_HUB_URL", "HUB_URL"],
        "https://api.matrixhub.io",
    )


@pytest.fixture
def hub_url(hub_base: str) -> str:
    """
    Backward-compatible alias of hub_base. Prefer using hub_base in new tests.
    """
    return hub_base


@pytest.fixture
def hub_token() -> Optional[str]:
    """
    Canonical bearer token for the Matrix Hub (new name).
    Falls back to older env names for compatibility.
    """
    return _env_first(["MATRIX_HUB_TOKEN", "MATRIX_TOKEN", "HUB_TOKEN"], None)


@pytest.fixture
def hub_headers(hub_token: Optional[str]) -> dict[str, str]:
    """
    Convenience headers for live calls (Accept + optional Authorization).
    """
    h = {"Accept": "application/json"}
    if hub_token:
        h["Authorization"] = f"Bearer {hub_token}"
    return h
