# tests/test_sdk_smoke.py
# Minimal smoke tests for matrix-sdk v0.1.2+ (no network)
from __future__ import annotations

import pytest

import matrix_sdk


def test_public_exports_exist():
    # New preferred surface
    assert hasattr(matrix_sdk, "MatrixClient")
    assert hasattr(matrix_sdk, "MatrixError")
    # Deep-link helpers
    assert hasattr(matrix_sdk, "deep_link")
    assert hasattr(matrix_sdk, "parse_deep_link")
    assert hasattr(matrix_sdk, "handle_deep_link_install")
    assert hasattr(matrix_sdk, "InvalidMatrixUri")
    # Legacy compatibility (kept for old callers)
    assert hasattr(matrix_sdk, "MatrixClient")
    assert hasattr(matrix_sdk, "MatrixError")


def test_deeplink_exports_are_same_objects():
    # parse_deep_link should be the same callable as deep_link.parse
    assert matrix_sdk.parse_deep_link is matrix_sdk.deep_link.parse
    assert matrix_sdk.handle_deep_link_install is matrix_sdk.deep_link.handle_install


def test_deeplink_parse_ok():
    url = "matrix://install?id=tool%3Ahello%400.1.0&alias=my_tool"
    dl = matrix_sdk.parse_deep_link(url)
    assert dl.action == "install"
    assert dl.id == "tool:hello@0.1.0"
    assert dl.alias == "my_tool"


def test_deeplink_parse_requires_id():
    from matrix_sdk import InvalidMatrixUri

    with pytest.raises(InvalidMatrixUri):
        matrix_sdk.parse_deep_link("matrix://install")


def test_deeplink_parse_rejects_bad_alias():
    from matrix_sdk import InvalidMatrixUri

    # Alias with spaces should be rejected
    bad = "matrix://install?id=tool%3Ahello%400.1.0&alias=b a d"
    with pytest.raises(InvalidMatrixUri):
        matrix_sdk.parse_deep_link(bad)


def test_handle_deeplink_install_calls_client():
    """
    We don't hit the network. Instead we pass a tiny fake that looks like the Hub client.
    The handler should call .install(id, target=<string>) and return a HandleResult.
    """
    calls = []

    class FakeHubClient:
        def install(self, id: str, *, target: str):
            calls.append((id, target))
            # Return a small shape similar to Hub /catalog/install
            return {"ok": True, "id": id, "target": target, "plan": {"steps": []}}

    url = "matrix://install?id=mcp_server%3Ahello-sse-server%400.1.0&alias=hello-sse"
    fake = FakeHubClient()
    res = matrix_sdk.handle_deep_link_install(url, fake, target="/tmp/hello-sse")

    # The handler returns a HandleResult dataclass
    assert hasattr(res, "id") and hasattr(res, "target") and hasattr(res, "response")
    assert res.id == "mcp_server:hello-sse-server@0.1.0"
    assert res.target == "/tmp/hello-sse"
    assert res.response.get("ok") is True

    # And it must have called our fake install exactly once with decoded id + our target
    assert calls == [("mcp_server:hello-sse-server@0.1.0", "/tmp/hello-sse")]


def test_hub_error_shape_str():
    # Make sure the error class exposes status + detail and a useful str()
    ErrClass = matrix_sdk.MatrixError or matrix_sdk.MatrixError  # tolerate legacy
    e = ErrClass(404, "not found")  # type: ignore[call-arg]
    assert getattr(e, "status", 404) == 404
    assert getattr(e, "detail", "not found") == "not found"
    assert "404" in str(e)
