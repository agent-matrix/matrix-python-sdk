from __future__ import annotations

import os
import sys
import types

import pytest

from matrix_sdk.ssl_compat import configure_ssl_trust, resolve_system_ca_file


def _clear_ca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear CA/proxy-related env vars and any injected truststore module."""
    for k in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "MATRIX_SSL_TRUST",
        "_TRUSTSTORE_INJECTED",
    ):
        monkeypatch.delenv(k, raising=False)
    # Ensure a clean import slate for truststore between tests
    monkeypatch.delitem(sys.modules, "truststore", raising=False)


def test_resolve_system_ca_file_does_not_crash():
    # Should return a string path or None without raising
    result = resolve_system_ca_file()
    assert (result is None) or isinstance(result, str)


def test_respects_user_overrides_ssl_cert_file(monkeypatch: pytest.MonkeyPatch):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/dummy.pem")
    configure_ssl_trust()  # should not override
    assert os.environ.get("SSL_CERT_FILE") == "/tmp/dummy.pem"


def test_respects_user_overrides_requests_ca_bundle(monkeypatch: pytest.MonkeyPatch):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/requests.pem")
    configure_ssl_trust()  # should not override
    assert os.environ.get("REQUESTS_CA_BUNDLE") == "/tmp/requests.pem"
    # And should not set SSL_CERT_FILE behind our back
    assert os.environ.get("SSL_CERT_FILE") is None


def test_disable_via_env(monkeypatch: pytest.MonkeyPatch):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("MATRIX_SSL_TRUST", "off")
    configure_ssl_trust()  # no changes expected
    assert os.environ.get("SSL_CERT_FILE") is None
    assert os.environ.get("REQUESTS_CA_BUNDLE") is None


def test_system_mode_sets_ssl_cert_file_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("MATRIX_SSL_TRUST", "system")
    fake_ca = tmp_path / "ca.pem"
    fake_ca.write_text("dummy pem")
    # Force resolver to return our temp file regardless of platform
    monkeypatch.setattr(
        "matrix_sdk.ssl_compat.resolve_system_ca_file",
        lambda: str(fake_ca),
        raising=True,
    )

    configure_ssl_trust()
    assert os.environ.get("SSL_CERT_FILE") == str(fake_ca)


def test_auto_prefers_truststore_when_available(monkeypatch: pytest.MonkeyPatch):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("MATRIX_SSL_TRUST", "auto")

    # Inject a fake 'truststore' module that marks when it's used
    fake = types.ModuleType("truststore")

    def _inject():
        os.environ["_TRUSTSTORE_INJECTED"] = "1"

    fake.inject_into_ssl = _inject  # type: ignore[attr-defined]
    # Patch sys.modules with the fake module
    monkeypatch.setitem(sys.modules, "truststore", fake)

    configure_ssl_trust()

    # If truststore is available, we prefer it and should not set SSL_CERT_FILE
    assert os.environ.get("_TRUSTSTORE_INJECTED") == "1"
    assert os.environ.get("SSL_CERT_FILE") is None


def test_auto_falls_back_to_system_when_truststore_broken_or_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    _clear_ca_env(monkeypatch)
    monkeypatch.setenv("MATRIX_SSL_TRUST", "auto")

    # Present a 'truststore' module that lacks inject_into_ssl(),
    # so import succeeds but injection fails → fallback to system CA file.
    broken = types.ModuleType("truststore")
    monkeypatch.setitem(sys.modules, "truststore", broken)

    fake_ca = tmp_path / "ca.pem"
    fake_ca.write_text("dummy pem")
    monkeypatch.setattr(
        "matrix_sdk.ssl_compat.resolve_system_ca_file",
        lambda: str(fake_ca),
        raising=True,
    )

    configure_ssl_trust()
    assert os.environ.get("SSL_CERT_FILE") == str(fake_ca)
