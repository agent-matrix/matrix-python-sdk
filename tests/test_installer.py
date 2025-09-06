# SPDX-License-Identifier: MIT
# tests/test_installer.py
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

# Public facade stays the same after the refactor
from matrix_sdk.installer import (
    BuildReport,
    LocalInstaller,
    _ensure_sse_url,
    _is_valid_runner_schema,
)
from matrix_sdk.manifest import ManifestResolutionError


class _DummyClient:
    """Only used to satisfy LocalInstaller(client) signature in tests."""

    def install(self, *a, **k):
        # Not exercised in these tests (we pass outcome directly)
        return {}


@pytest.fixture
def installer():
    return LocalInstaller(client=_DummyClient())


def test_materialize_fetches_runner_from_plan_url(
    tmp_path, monkeypatch, installer, installer_env
):
    """
    LocalInstaller.materialize should fetch plan.runner_url and write runner.json.
    """
    # Mock urlopen used inside runner_discovery via _http_get_text(Request,...)
    runner_obj = {"type": "python", "entry": "app/server.py"}
    seen = {}

    def _ok_urlopen(url, timeout=15):
        # url may be a Request; store final URL for verification
        final_url = getattr(url, "full_url", url)
        seen["url"] = final_url

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(runner_obj).encode("utf-8")

            @property
            def headers(self):
                return {"Content-Type": "application/json; charset=utf-8"}

        return _Resp()

    # Patch where it's used (module-level import in runner_discovery)
    monkeypatch.setattr(
        "matrix_sdk.installer.runner_discovery.urllib.request.urlopen",
        _ok_urlopen,
        raising=True,
    )

    # Relative runner_url resolved against plan.provenance.source_url
    outcome = {
        "plan": {
            "runner_url": "runner.json",
            "provenance": {"source_url": "https://example.test/base/"},
        }
    }
    report = installer.materialize(outcome, tmp_path)

    assert isinstance(report, BuildReport)
    rpath = Path(tmp_path) / "runner.json"
    assert rpath.is_file(), "runner.json should be written from runner_url"
    data = json.loads(rpath.read_text())
    assert data["type"] == "python" and data["entry"] == "app/server.py"
    assert seen["url"].startswith(
        "https://example.test/base/"
    ), "relative runner_url should be resolved against provenance"


def test_materialize_accepts_embedded_runner_b64(tmp_path, installer):
    """
    LocalInstaller.materialize should accept an embedded base64 runner (plan.runner_b64).
    """
    obj = {"type": "node", "entry": "index.js"}
    b64 = base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
    outcome = {"plan": {"runner_b64": b64}}

    report = installer.materialize(outcome, tmp_path)

    assert isinstance(report, BuildReport)
    rpath = Path(tmp_path) / "runner.json"
    assert rpath.is_file(), "runner.json should be written from runner_b64"
    data = json.loads(rpath.read_text())
    assert data["type"] == "node" and data["entry"] == "index.js"


def test_materialize_infers_runner_from_server_py(tmp_path, installer):
    """
    If no explicit runner is provided, discovery should infer a python runner
    when server.py exists at the target.
    """
    # Create a common entrypoint
    (Path(tmp_path) / "server.py").write_text("# demo", encoding="utf-8")

    outcome = {"plan": {}}  # no runner hints
    report = installer.materialize(outcome, tmp_path)

    assert isinstance(report, BuildReport)
    rpath = Path(tmp_path) / "runner.json"
    assert rpath.is_file(), "runner.json should be synthesized by inference"
    data = json.loads(rpath.read_text())
    assert data["type"] == "python" and data["entry"] == "server.py"


def test_materialize_synthesizes_connector_from_manifest(tmp_path, installer):
    """
    If a manifest with server URL exists (v2-ish), discovery should synthesize a connector.
    """
    # Embedded manifest shape accepted by runner_schema._url_from_manifest
    outcome = {
        "plan": {
            "manifest": {
                "mcp_registration": {"server": {"url": "https://svc.example/api"}}
            }
        }
    }
    report = installer.materialize(outcome, tmp_path)

    assert isinstance(report, BuildReport)
    rpath = Path(tmp_path) / "runner.json"
    assert rpath.is_file(), "runner.json should be synthesized from manifest"
    data = json.loads(rpath.read_text())
    assert data["type"] == "connector"
    # URL normalized to /sse tail by _ensure_sse_url
    assert data["url"].endswith("/sse")


def test_materialize_raises_manifest_resolution_on_artifact_error(
    tmp_path, monkeypatch, installer
):
    """
    files.materialize_artifacts now escalates fetch errors as ManifestResolutionError.
    """
    # Import the module to patch the right symbol location
    import matrix_sdk.installer.files as files_mod

    def _raise_http_artifact(**kwargs):
        raise files_mod.ArchiveFetchError("boom")

    monkeypatch.setattr(
        files_mod, "fetch_http_artifact", _raise_http_artifact, raising=True
    )

    outcome = {"plan": {"artifacts": [{"url": "https://example.test/a.zip"}]}}

    with pytest.raises(ManifestResolutionError):
        installer.materialize(outcome, tmp_path)


# -------------------- New tests for the refactor/regressions --------------------


def test_shallow_search_finds_nested_runner(tmp_path, installer, write_json):
    """
    Shallow search should find a runner.json within depth and normalize to root/runner.json.
    """
    write_json("nested/deeper/runner.json", {"type": "python", "entry": "srv.py"})
    outcome = {"plan": {}}  # no direct hint

    report = installer.materialize(outcome, tmp_path)
    assert isinstance(report, BuildReport)

    out = Path(tmp_path) / "runner.json"
    assert out.is_file(), "normalized runner.json should be written at project root"
    data = json.loads(out.read_text())
    assert data["type"] == "python" and data["entry"] == "srv.py"


def test_secure_join_blocks_traversal(tmp_path):
    """
    _secure_join must block paths that escape the target root.
    """
    from matrix_sdk.installer.files import _secure_join

    root = tmp_path
    assert _secure_join(root, "../evil.txt") is None
    assert _secure_join(root, "..\\evil.txt") is None

    # Normal path OK
    p = _secure_join(root, "ok/inner.txt")
    assert p is not None
    assert str(p).startswith(str(root))


def test_ensure_sse_url_preserves_query_and_fragment():
    """
    _ensure_sse_url should preserve querystring and fragment while normalizing path.
    """
    url = "https://api.example/foo/bar?x=1#frag"
    out = _ensure_sse_url(url)
    assert out.startswith("https://api.example/")
    assert out.endswith("/sse?x=1#frag") or out.endswith(
        "/sse#frag?x=1"
    )  # fragment after query is standard
    # basic invariant: includes both '?x=1' and '#frag'
    assert "?x=1" in out and "#frag" in out


def test_is_valid_runner_schema_accepts_modern_process():
    """
    Modern process runner (process.command=[]) should pass schema validation.
    """
    modern = {"process": {"command": ["python", "server.py"]}}
    assert _is_valid_runner_schema(modern, __import__("logging").getLogger("test"))


def test_relative_runner_url_resolves_against_provenance(
    tmp_path, monkeypatch, installer, installer_env
):
    """
    runner_discovery should resolve relative runner_url using provenance base URL.
    """
    runner_obj = {"type": "node", "entry": "index.js"}
    seen = {}

    def _ok_urlopen(url, timeout=15):
        final_url = getattr(url, "full_url", url)
        seen["url"] = final_url

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(runner_obj).encode("utf-8")

            @property
            def headers(self):
                return {"Content-Type": "application/json; charset=utf-8"}

        return _Resp()

    monkeypatch.setattr(
        "matrix_sdk.installer.runner_discovery.urllib.request.urlopen",
        _ok_urlopen,
        raising=True,
    )

    outcome = {
        "plan": {
            "runner_url": "runner.json",
            "provenance": {"source_url": "https://hub.example/pkg/v1/"},
        }
    }
    installer.materialize(outcome, tmp_path)

    assert "hub.example/pkg/v1/" in seen["url"]


def test_shallow_search_log_format_no_stray_percent(tmp_path, caplog_debug):
    """
    Ensure the fixed log line doesn't leak a raw '%s' placeholder.
    """
    from matrix_sdk.installer.runner_discovery import _find_runner_file_shallow

    # Run with no file present to trigger the final "finished" log
    _find_runner_file_shallow(tmp_path, "runner.json", 1)

    # check that no formatted message contains a literal '%s'
    assert not any(
        "%s" in rec.getMessage() for rec in caplog_debug.records
    ), "Found an unformatted '%s' in runner_discovery logs"
