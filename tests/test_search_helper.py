# SPDX-License-Identifier: MIT
# tests/test_search_helper.py
from __future__ import annotations

# Import the helper module directly from the generated file path
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest


def load_module(path: str, name: str) -> types.ModuleType:
    """
    Load a module from a file path under a given qualified name and
    ensure it is registered in sys.modules before exec_module.

    This avoids dataclasses + future annotations issues where dataclasses
    attempts to resolve string annotations by looking up the class's module
    in sys.modules during class creation.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # IMPORTANT: insert into sys.modules so dataclasses can resolve cls.__module__
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[assignment]
    return mod


class FakeMatrixError(Exception):
    def __init__(self, status_code: int, detail: str = "bad"):
        super().__init__(detail)
        self.status_code = status_code


class FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, **params) -> Dict[str, Any]:
        self.calls.append(params)
        # Scenario logic is injected by monkeypatching attributes on self
        behavior = getattr(self, "_behavior", "ok")
        if behavior == "transient_fail":
            count = getattr(self, "_fail_count", 0)
            if count > 0:
                self._fail_count -= 1
                raise FakeMatrixError(500, "boom")
            return {"items": [{"id": "ok"}], "total": 1}
        if behavior == "empty_then_keyword":
            if params.get("mode") == "keyword":
                return {"items": [{"id": "k"}], "total": 1}
            return {"items": [], "total": 0}
        # default: single item
        return {"items": [{"id": "x"}], "total": 1}


def test_normalizes_filters_and_type(tmp_path: Path):
    helper = load_module(
        str(Path(__file__).parent.parent / "matrix_sdk" / "search.py"),
        "matrix_sdk.search",
    )
    client = FakeClient()
    res = helper.search(
        client,
        "hello",
        type="any",
        capabilities=["rag", "sql", ""],
        frameworks="langchain, litellm",
        providers={"self", "acme"},  # set order is not guaranteed, but CSV is fine
        limit=999,  # will clamp to 100
    )
    assert res["total"] == 1
    params = client.calls[-1]
    assert "type" not in params  # "any" omitted
    assert params["limit"] == 100  # clamped
    assert params["capabilities"] == "rag,sql"
    # frameworks preserves commas/spaces -> module strips spaces only around entries
    assert params["frameworks"].replace(" ", "") in (
        "langchain,litellm",
        "litellm,langchain",
    )
    prov_set = set(params["providers"].split(","))
    assert prov_set == {"self", "acme"}


def test_fallback_chain_semantic_to_keyword(tmp_path: Path):
    helper = load_module(
        str(Path(__file__).parent.parent / "matrix_sdk" / "search.py"),
        "matrix_sdk.search",
    )
    client = FakeClient()
    client._behavior = (
        "empty_then_keyword"  # first call empty (semantic), then keyword non-empty
    )
    res = helper.search(client, "hello", type=None, mode="semantic")
    # should try semantic then keyword
    assert res["total"] == 1
    modes = [c.get("mode") for c in client.calls]
    assert modes[0] == "semantic"
    assert "keyword" in modes[1:]


def test_retry_on_transient_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    helper = load_module(
        str(Path(__file__).parent.parent / "matrix_sdk" / "search.py"),
        "matrix_sdk.search",
    )
    client = FakeClient()
    client._behavior = "transient_fail"
    client._fail_count = 1  # fail once, then succeed

    # speed up sleep by patching time.sleep
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)
    res = helper.search(
        client,
        "hello",
        mode="keyword",
        options=helper.SearchOptions(max_attempts=3, backoff_base=0.01),
    )
    assert res["total"] == 1
    assert len(client.calls) >= 2  # retried at least once
