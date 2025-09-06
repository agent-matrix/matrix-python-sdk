# tests/test_find_potential_servers.py
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from pathlib import Path

import pytest

from matrix_sdk.installer.find_potential_servers import (
    ServerFinder,
)
from matrix_sdk.installer.find_potential_servers import (
    main as finder_main,
)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_finder_python_server_py(tmp_path: Path):
    """
    Finds a Python server when server.py exists (with common web/MCP hint words).
    """
    _write(tmp_path / "server.py", "from fastapi import FastAPI\napp = FastAPI()\n")

    finder = ServerFinder(root=tmp_path, top_n=5, exts=set(), debug=False)
    results = finder.run()

    assert results, "Expected at least one candidate"
    # Top choice should be server.py
    assert results[0].rel_path.as_posix() == "server.py"
    assert results[0].score > 0


def test_finder_respects_runner_json_hint(tmp_path: Path):
    """
    runner.json with 'entry' should strongly bias the candidate list.
    """
    _write(tmp_path / "app" / "server.py", "print('hi')")
    _write(
        tmp_path / "runner.json",
        json.dumps({"type": "python", "entry": "app/server.py"}),
    )

    finder = ServerFinder(root=tmp_path, top_n=5, exts=set(), debug=False)
    results = finder.run()

    assert results, "Expected at least one candidate"
    assert results[0].rel_path.as_posix() == "app/server.py"
    # Very strong score due to runner.json hint (100)
    assert results[0].score >= 100


def test_finder_package_json_script(tmp_path: Path):
    """
    package.json's scripts.start=...<file>.js should produce a candidate.
    """
    _write(
        tmp_path / "package.json",
        json.dumps({"name": "demo", "scripts": {"start": "node server.js"}}),
    )
    _write(tmp_path / "server.js", "console.log('ok');")

    finder = ServerFinder(root=tmp_path, top_n=5, exts=set(), debug=False)
    results = finder.run()

    assert results, "Expected at least one candidate from package.json / server.js"
    assert any(c.rel_path.as_posix().endswith("server.js") for c in results)


def test_finder_skips_node_modules(tmp_path: Path):
    """
    Ensure files in node_modules are ignored by the directory pruner.
    """
    _write(tmp_path / "server.js", "console.log('top');")
    _write(tmp_path / "node_modules" / "server.js", "console.log('skip me');")

    finder = ServerFinder(root=tmp_path, top_n=10, exts=set(), debug=False)
    results = finder.run()

    # Should include top-level server.js
    assert any(c.rel_path.as_posix() == "server.js" for c in results)
    # And no candidate should come from node_modules
    assert all("node_modules/" not in c.rel_path.as_posix() for c in results)


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """
    The CLI --json output should be valid JSON with relative paths.
    """
    _write(tmp_path / "server.py", "print('ok')")

    # Call CLI main() with arguments (no subprocess needed)
    rc = finder_main([str(tmp_path), "--top", "2", "--json"])
    assert rc == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) <= 2
    assert any(item["path"] == "server.py" for item in payload)
