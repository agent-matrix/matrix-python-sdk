# matrix_sdk/bulk/discovery.py
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List

try:
    import tomllib  # Python 3.11+
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .models import EndpointDescriptor, ServerManifest
from .utils import with_temp_extract


def discover_manifests_from_source(source: Dict[str, Any]) -> List[ServerManifest]:
    """Discover 0..N ServerManifest objects from a source descriptor.

    Source keys:
      - kind: 'zip' | 'git' | 'dir'
      - path: local file path for zip, or directory path for 'dir'
      - url: git repo URL for git
      - ref: branch or tag (optional)
    """
    kind = source.get("kind")

    if kind == "zip":
        zip_path = source.get("path")
        if not zip_path or not os.path.isfile(zip_path):
            raise ValueError(f"Invalid zip path: {zip_path}")
        with with_temp_extract(zip_path) as tmpdir:
            return _discover_in_dir(tmpdir)

    if kind == "dir":
        dir_path = source.get("path")
        if not dir_path or not os.path.isdir(dir_path):
            raise ValueError(f"Invalid directory: {dir_path}")
        return _discover_in_dir(dir_path)

    if kind == "git":
        repo_url = source.get("url")
        if not repo_url:
            raise ValueError("'url' is required for git source")
        ref = source.get("ref", "main")
        tmpdir = tempfile.mkdtemp(prefix="mcp_git_")
        try:
            cmd = ["git", "clone", "--depth", "1", "--branch", ref, repo_url, tmpdir]
            subprocess.run(
                cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            return _discover_in_dir(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    raise ValueError(f"Unsupported source kind: {kind}")


def _discover_in_dir(root: str) -> List[ServerManifest]:
    """
    Matrix-first: if `matrix/` exists, parse manifests there; else fall back to pyproject.toml.
    """
    # 1) Matrix folder path
    matrix_dir = os.path.join(root, "matrix")
    if os.path.isdir(matrix_dir):
        manifests = _load_matrix_manifests(matrix_dir)
        if manifests:
            return manifests

    # 2) Fallback: pyproject.toml and tool.mcp_server
    pyproject = os.path.join(root, "pyproject.toml")
    if os.path.isfile(pyproject):
        return [_load_pyproject_manifest(pyproject)]

    return []


def _parse_manifest_index(index_path: str, matrix_dir: str) -> List[ServerManifest]:
    """Parses an index.json file to find and load server manifests."""
    results: List[ServerManifest] = []
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        # Accept arrays or objects with a "servers" key
        entries = (
            index_data.get("servers") if isinstance(index_data, dict) else index_data
        )
        if not isinstance(entries, list):
            return []

        for item in entries:
            mf = None
            # Case 1: Item is a relative file path to another manifest
            if isinstance(item, str) and item.endswith(".json"):
                path = (
                    os.path.join(matrix_dir, item) if not os.path.isabs(item) else item
                )
                if os.path.isfile(path):
                    mf = _load_single_matrix_manifest(path)
            # Case 2: Item is an inline manifest dictionary
            elif isinstance(item, dict) and item.get("type") in {
                "mcp_server",
                "server",
            }:
                mf = _map_matrix_dict_to_manifest(item)

            if mf:
                results.append(mf)
    except Exception:
        # If parsing fails for any reason, return an empty list for this source.
        return []
    return results


def _load_matrix_manifests(matrix_dir: str) -> List[ServerManifest]:
    """Loads all manifests from a directory, prioritizing index.json."""
    results: List[ServerManifest] = []

    # 1. Prefer index.json if present by using our new helper function.
    index_path = os.path.join(matrix_dir, "index.json")
    if os.path.isfile(index_path):
        results.extend(_parse_manifest_index(index_path, matrix_dir))

    # 2. Glob *.manifest.json as a fallback or for additional manifests.
    for path in glob.glob(os.path.join(matrix_dir, "*.manifest.json")):
        mf = _load_single_matrix_manifest(path)
        if mf and mf not in results:  # Avoid duplicates if also in index.json
            results.append(mf)

    return results


def _load_single_matrix_manifest(path: str) -> ServerManifest | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _map_matrix_dict_to_manifest(data)
    except Exception:
        return None


def _map_matrix_dict_to_manifest(d: Dict[str, Any]) -> ServerManifest | None:
    try:
        et = (d.get("type") or d.get("entity_type") or "mcp_server").lower()
        if et not in {"mcp_server", "server"}:
            return None
        # Map endpoint structure
        ep = d.get("endpoint") or {}
        endpoint = EndpointDescriptor(
            transport=str(ep.get("transport", "sse")),
            url=str(ep.get("url")),
            schema=str(ep.get("schema", "mcp/v1")),
            auth=(ep.get("auth") or "none"),
        )
        man = ServerManifest(
            entity_type="mcp_server",
            id=str(d.get("id") or d.get("name") or "component")
            .strip()
            .lower()
            .replace(" ", "-"),
            name=str(d.get("name") or d.get("id") or "component"),
            version=d.get("version"),
            summary=d.get("summary") or d.get("description"),
            description=d.get("description"),
            providers=list(d.get("providers") or []),
            frameworks=list(d.get("frameworks") or []),
            capabilities=list(d.get("capabilities") or []),
            endpoint=endpoint,
            labels=d.get("labels") or {},
            quality_score=d.get("quality_score") or 0.0,
            source_url=d.get("source_url"),
            license=d.get("license"),
        )
        # Compute uid if missing (handled by model validator)
        _ = man.uid
        return man
    except Exception:
        return None


def _load_pyproject_manifest(pyproject_path: str) -> ServerManifest:
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    meta = data.get("tool", {}).get("mcp_server", None)
    if not meta:
        raise ValueError("[tool.mcp_server] section missing in pyproject.toml")

    # Normalize endpoint
    ep = meta.get("endpoint") or {}
    endpoint = EndpointDescriptor(
        transport=str(ep.get("transport", "sse")),
        url=str(ep.get("url")),
        schema=str(ep.get("schema", "mcp/v1")),
        auth=(ep.get("auth") or "none"),
    )

    man = ServerManifest(
        entity_type="mcp_server",
        id=str(meta.get("id") or meta.get("name") or "component")
        .strip()
        .lower()
        .replace(" ", "-"),
        name=str(meta.get("name") or meta.get("id") or "component"),
        version=meta.get("version"),
        summary=meta.get("summary") or meta.get("description"),
        description=meta.get("description"),
        providers=list(meta.get("providers") or []),
        frameworks=list(meta.get("frameworks") or []),
        capabilities=list(meta.get("capabilities") or []),
        endpoint=endpoint,
        labels=meta.get("labels") or {},
        quality_score=meta.get("quality_score") or 0.0,
        source_url=meta.get("source_url"),
        license=meta.get("license"),
    )
    _ = man.uid
    return man
