# tests/conftest.py
# Loads .env.local (if present) and provides helpers for tests.

from __future__ import annotations

import os
from typing import Dict, Any, Optional

import httpx
import pytest

# ---- minimal .env.local loader (no extra deps) ------------------------------
def _load_env_local(path: str = ".env.local") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # do not override pre-set env (e.g., in CI)
            os.environ.setdefault(k, v)

_load_env_local()

# ---- pytest markers ----------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line("markers", "live: mark test that hits a real Matrix Hub")

# ---- fixtures ----------------------------------------------------------------
@pytest.fixture
def mock_transport_factory():
    """
    Factory returning an httpx.MockTransport from a handler function.

    Usage:
        def handler(request): ...
        transport = mock_transport_factory(handler)
    """
    def _factory(handler):
        return httpx.MockTransport(handler)
    return _factory


@pytest.fixture
def hub_url() -> str:
    return os.getenv("MATRIX_HUB_URL", "http://127.0.0.1:7300")


@pytest.fixture
def hub_token() -> Optional[str]:
    tok = os.getenv("MATRIX_TOKEN")
    return tok if tok else None
