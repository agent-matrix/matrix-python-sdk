# matrix_sdk/tests/test_installer.py
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from matrix_sdk.installer import LocalInstaller, BuildReport


class _DummyClient:
    """Only used to satisfy LocalInstaller(client) signature."""
    def __init__(self): ...
    def install(self, *a, **k):  # not used in these tests
        return {}


@pytest.fixture
def installer():
    return LocalInstaller(client=_DummyClient())


def test_materialize_fetches_runner_from_plan_url(tmp_path, monkeypatch, installer):
    """
    LocalInstaller.materialize should fetch plan.runner_url and write runner.json.
    """
    # Mock urlopen to return a valid runner.json
    runner_obj = {"type": "python", "entry": "app/server.py"}

    def _ok_urlopen(url, timeout=15):
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return json.dumps(runner_obj).encode("utf-8")
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _ok_urlopen)

    outcome = {"plan": {"runner_url": "https://example.test/runner.json"}}
    report = installer.materialize(outcome, tmp_path)

    assert isinstance(report, BuildReport)
    rpath = Path(tmp_path) / "runner.json"
    assert rpath.is_file(), "runner.json should be written from runner_url"
    data = json.loads(rpath.read_text())
    assert data["type"] == "python" and data["entry"] == "app/server.py"


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
