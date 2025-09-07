# Reference (v0.1.9)

A quick map of the primary modules and entry points.

## Core

- `matrix_sdk.client.MatrixClient` — Hub API client  
  Methods: `search()`, `entity()`, `install()`, `list_remotes()`, `add_remote()`, `delete_remote()`, `trigger_ingest()`

- `matrix_sdk.installer.LocalInstaller` — local install orchestration  
  Returns: `BuildReport`, `EnvReport`, `BuildResult`

- `matrix_sdk.runtime` — run local MCP servers  
  Functions: `start()`, `stop()`, `status()`, `doctor()`, `tail_logs()`, `log_path()`

## Installer helpers

- Runner schema & discovery (re-exported):
  - `_is_valid_runner_schema(runner, logger)`
  - `_coerce_runner_to_legacy_process(obj)`
  - `_ensure_sse_url(url)`
  - `_url_from_manifest(manifest)`
  - `_extract_mcp_sse_url(node)`

## Artifact fetchers

- `matrix_sdk.archivefetch` — `fetch_http_artifact`, `ArchiveFetchError`
- `matrix_sdk.gitfetch` — `fetch_git_artifact`, `GitFetchError`

## Models

- `matrix_sdk.schemas` — Pydantic models for Hub responses  
  (`SearchItem`, `SearchResponse`, `EntityDetail`, `InstallOutcome`, etc.)

## Exceptions

- `matrix_sdk.client.MatrixError`
- `matrix_sdk.manifest.ManifestResolutionError`
- `matrix_sdk.archivefetch.ArchiveFetchError`
- `matrix_sdk.gitfetch.GitFetchError`

## Env toggles (common)

- `MATRIX_SDK_DEBUG=1`
- `MATRIX_SDK_HTTP_TIMEOUT=15`
- `MATRIX_SDK_RUNNER_SEARCH_DEPTH=2`
- `MATRIX_SDK_ALLOW_MANIFEST_FETCH=1`
- `MATRIX_SDK_ENABLE_CONNECTOR=1`
````

