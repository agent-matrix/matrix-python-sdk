# tests/conftest.py
# Loads .env/.env.local (if present) and provides helpers for tests.
from __future__ import annotations

import json
import os
from pathlib import Path
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


# ---- fixtures (existing, unchanged) -----------------------------------------
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


# ---- new fixtures (additive, non-breaking) ----------------------------------


@pytest.fixture
def is_windows() -> bool:
    """True on Windows, else False. Useful for path assertions."""
    return os.name == "nt"


@pytest.fixture
def installer_env(monkeypatch: pytest.MonkeyPatch):
    """
    Stable env defaults for installer tests (opt-in).
    Nothing global; only applied when the test requests this fixture.
    """
    # Keep connector strategies enabled by default in tests.
    monkeypatch.setenv("MATRIX_SDK_ENABLE_CONNECTOR", "1")
    # Make network-related tests fast & deterministic.
    monkeypatch.setenv("MATRIX_SDK_HTTP_TIMEOUT", "5")
    # Keep shallow search predictable.
    monkeypatch.setenv("MATRIX_SDK_RUNNER_SEARCH_DEPTH", "2")
    # Allow resolving relative manifest/runner URLs without host restrictions in tests.
    # (Leave unset if your CI wants to exercise allowlisting.)
    monkeypatch.setenv("MATRIX_SDK_MANIFEST_DOMAINS", "")
    # Opt-in verbose installer logs only if requested at runtime
    # (do NOT force on; preserve host project logging control).
    if os.getenv("MATRIX_SDK_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        monkeypatch.setenv(
            "MATRIX_SDK_DEBUG", os.getenv("MATRIX_SDK_DEBUG")
        )  # no-op but explicit
    yield


@pytest.fixture
def chdir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Change the CWD to a dedicated tmp dir for the test. Handy when code expects
    to run relative to the project root.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def make_tree(tmp_path: Path) -> Callable[[dict[str, str | bytes]], Path]:
    """
    Build a small project tree under tmp_path from a mapping:
        {"a/b.txt": "content", "bin/app": b"..."}
    Returns tmp_path for convenience.
    """

    def _make(spec: dict[str, str | bytes]) -> Path:
        for rel, content in spec.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        return tmp_path

    return _make


@pytest.fixture
def write_json(tmp_path: Path) -> Callable[[str | Path, dict], Path]:
    """
    Convenience to write JSON under tmp_path.
        p = write_json("runner.json", {"type": "python", "entry": "server.py"})
    """

    def _write(rel: str | Path, data: dict) -> Path:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return p

    return _write


@pytest.fixture
def caplog_debug(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """
    Set DEBUG level for the installer logger only when the test requests it.
    """
    caplog.set_level("DEBUG", logger="matrix_sdk.installer")
    return caplog
