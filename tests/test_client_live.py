# tests/test_client_live.py
from __future__ import annotations

import os

import pytest

from matrix_sdk import MatrixClient, MatrixError

pytestmark = pytest.mark.live


def _should_run_live() -> bool:
    run = os.getenv("RUN_LIVE", "").lower() in ("1", "true", "yes", "on")
    has_env = os.path.exists(".env.local") or os.getenv("MATRIX_HUB_URL")
    return run and bool(has_env)


@pytest.mark.skipif(
    not _should_run_live(), reason="Live test disabled (set RUN_LIVE=1 and .env.local)"
)
def test_live_search_smoke(hub_url, hub_token):
    client = MatrixClient(base_url=hub_url, token=hub_token, timeout=10.0)
    try:
        res = client.search(q="hello", type="any", limit=5, with_snippets=True)
    except MatrixError as e:
        pytest.skip(f"Hub not reachable or error: {e}")
        return
    assert isinstance(res, dict)
    assert "items" in res
    assert len(res["items"]) <= 5
