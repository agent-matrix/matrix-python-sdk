# matrix_sdk/bulk/discovery.py
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict

import tomli

from .schemas import ServerManifest
from .utils import with_temp_extract


def discover_manifest(source: Dict[str, Any]) -> ServerManifest:
    """
    Discover a ServerManifest from given source descriptor.
    Supports 'zip' and 'git' kinds.

    source keys:
      - kind: 'zip' or 'git'
      - path: local file path for zip
      - url: git repo URL for git
      - ref: branch or tag (optional)
    """
    kind = source.get("kind")
    if kind == "zip":
        zip_path = source.get("path")
        if not zip_path or not os.path.isfile(zip_path):
            raise ValueError(f"Invalid zip path: {zip_path}")
        # NOTE: Ensure `with_temp_extract` is defined in `matrix_sdk/bulk/utils.py`
        # It should be a context manager that extracts a zip file to a temporary directory.
        with with_temp_extract(zip_path) as tmpdir:
            return _load_pyproject_manifest(tmpdir)
    elif kind == "git":
        repo_url = source.get("url")
        if not repo_url:
            raise ValueError("'url' is required for git source")

        ref = source.get("ref", "main")
        tmpdir = tempfile.mkdtemp(prefix="mcp_git_")
        try:
            # Construct the command with guaranteed string values
            command = [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                ref,
                repo_url,
                tmpdir,
            ]
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return _load_pyproject_manifest(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        raise ValueError(f"Unsupported source kind: {kind}")


def _load_pyproject_manifest(path: str) -> ServerManifest:
    """
    Parse pyproject.toml under path/tool.mcp_server block to build ServerManifest.
    """
    toml_path = os.path.join(path, "pyproject.toml")
    if not os.path.isfile(toml_path):
        raise FileNotFoundError(f"pyproject.toml not found in {path}")
    with open(toml_path, "rb") as f:
        data = tomli.load(f)
    meta = data.get("tool", {}).get("mcp_server", None)
    if not meta:
        raise ValueError("[tool.mcp_server] section missing in pyproject.toml")
    return ServerManifest(**meta)
