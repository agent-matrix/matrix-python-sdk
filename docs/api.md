# API Reference

> SDK version: **0.1.1**
> Scope: Hub catalog client + local installer + lightweight runtime

This SDK provides three pillars:

1. **MatrixClient** — search the Hub, read entities, request install plans.
2. **LocalInstaller** — materialize a plan locally (files, artifacts, env, `runner.json`).
3. **runtime** — start/stop/list/tail/doctor local MCP servers (no daemon).

---

## MatrixClient

**Location**: `matrix_sdk.client.MatrixClient`
**Errors**: raises `matrix_sdk.client.MatrixError(status, detail)` on non-2xx.

```python
class MatrixClient:
    def __init__(self, base_url: str, token: str | None = None, *,
                 timeout: float = 15.0, **kwargs) -> None: ...
    # Catalog
    def search(self, q: str, *, type: str | None = "any", limit: int = 10,
               capabilities: str | None = None, frameworks: str | None = None,
               providers: str | None = None, mode: str | None = None,
               include_pending: bool = False, with_snippets: bool = False) -> dict: ...
    def entity(self, id: str) -> dict: ...
    def install(self, id: str, *, target: str, **kwargs) -> dict: ...
    # Remotes
    def list_remotes(self) -> list | dict: ...
    def add_remote(self, url: str, *, name: str | None = None) -> dict: ...
    def delete_remote(self, name: str) -> dict: ...
    def trigger_ingest(self, name: str) -> dict: ...
```

**Notes**

* `entity(id)` URL-encodes `id` internally.
* `install(id, target)` calls Hub `/catalog/install` and returns the Hub payload (plan/results/etc).
* Treat `type="any"` (or `None`) as “no filter”.

---

## Deep links

**Location**: `matrix_sdk.deep_link`
**Errors**: `InvalidMatrixUri` for bad/unsupported URIs.

```python
from matrix_sdk.deep_link import parse, handle_install, InvalidMatrixUri

dl = parse("matrix://install?id=tool%3Ahello%400.1.0&alias=hello")
# -> DeepLink(action='install', id='tool:hello@0.1.0', alias='hello')

res = handle_install(url, client, target="/abs/install/dir")
# -> HandleResult(id=..., target=..., response=<hub install payload>)
```

Rules:

* Only `matrix://install` is supported.
* `id` is **required**; `alias` matches `^[a-z0-9][a-z0-9._-]{0,63}$`.

---

## LocalInstaller

**Location**: `matrix_sdk.installer.LocalInstaller`
**Data classes**: `BuildReport`, `EnvReport`, `BuildResult`
**Other errors**: `matrix_sdk.manifest.ManifestResolutionError`, `ArchiveFetchError`, `GitFetchError`.

```python
from matrix_sdk.installer import LocalInstaller

installer = LocalInstaller(client)

result = installer.build(
    "mcp_server:hello-sse-server@0.1.0",
    alias="hello-sse",
    # target defaults via policy if omitted
)

print(result.target)         # install directory
print(result.env.python_prepared, result.env.node_prepared)
print(result.build.files_written, result.build.artifacts_fetched)
print(result.runner)         # contents of runner.json (dict)
```

### Lifecycle

1. **plan(id, target)** → ask Hub for an install plan.
2. **materialize(outcome, target)** → write declared files, fetch artifacts, ensure/validate `runner.json`.
3. **prepare\_env(target, runner)** → create `.venv` (Python) and/or run `npm|yarn|pnpm install` (Node).
4. **build(id, …)** → runs all three steps and returns `BuildResult`.

**Runner inference** (when missing):

* If `server.py` → `{"type":"python","entry":"server.py","python":{"venv":".venv"}}`
* If `server.js`/`package.json` → Node with sensible default entry.

---

## runtime

**Location**: `matrix_sdk.runtime`
**State/Logs**: `~/.matrix/state/<alias>/runner.lock.json`, `~/.matrix/logs/<alias>.log`
**Model**: `LockInfo(pid:int, port:int|None, alias:str, target:str, started_at:float, runner_path:str)`

```python
from matrix_sdk import runtime

lock = runtime.start("/path/to/install", alias="hello-sse")   # reads runner.json
# uses venv python for python runners; finds a free port if needed

runtime.status()            # -> list[LockInfo]
runtime.doctor("hello-sse") # -> {"status": "ok"|"fail", "reason": "..."}
runtime.stop("hello-sse")   # -> bool
for line in runtime.tail_logs("hello-sse", n=40): print(line, end="")
runtime.log_path("hello-sse")  # -> str
```

**Requirements**

* `runner.json` must define at least: `{"type": "python"|"node", "entry": "…"}`
* Python runners: venv Python must exist (created during `prepare_env`).

---

## Artifact fetchers

### HTTP archives

**Location**: `matrix_sdk.archivefetch`
**API**:

```python
from matrix_sdk.archivefetch import fetch_http_artifact, ArchiveFetchError

fetch_http_artifact(
    url: str,
    target: pathlib.Path | str,
    dest: str | None = None,        # optional: also save raw file at target/dest
    sha256: str | None = None,      # optional integrity
    unpack: bool = False,           # auto-detects .zip/.tar(.gz) too
    timeout: int | float = 60,
    logger: logging.Logger | None = None,
)
```

* Safe ZIP/TAR extraction (no path traversal).
* Optional flatten of GitHub-style single-folder archives.

### Git repos

**Location**: `matrix_sdk.gitfetch`
**API**:

```python
from matrix_sdk.gitfetch import fetch_git_artifact, GitFetchError

fetch_git_artifact(
    spec: Mapping[str, object],     # {repo, ref, depth?, subdir?, strip_vcs?, recurse_submodules?, lfs?, verify_sha?}
    target: pathlib.Path,
    git_bin: str = "git",
    allow_hosts: Iterable[str] | None = None,  # defaults via env/standard hosts
    timeout: int = 180,
    logger: logging.Logger | None = None,
)
```

* HTTPS by default; host allow-list enforced (deny-by-default if none given).
* Shallow clone, optional sparse subdir, optional LFS, optional commit verification.

---

## Schemas (Pydantic)

**Location**: `matrix_sdk.schemas`
Models mirror Hub responses; unknown fields are allowed.

* `SearchItem`, `SearchResponse`
* `EntityDetail` (extra fields allowed; includes manifest-ish + computed fields)
* `InstallStepResult`, `InstallOutcome`
* `MatrixAPIError` (generic error wrapper)

---

## Exceptions

* `matrix_sdk.client.MatrixError(status, detail)` — Hub non-2xx.
* `matrix_sdk.deep_link.InvalidMatrixUri` — deep-link parse/validation errors.
* `matrix_sdk.manifest.ManifestResolutionError` — manifest/artifact integrity/host violations.
* `matrix_sdk.archivefetch.ArchiveFetchError` — download/unpack errors.
* `matrix_sdk.gitfetch.GitFetchError` — git materialization errors.

---

## Environment variables

* `MATRIX_SDK_DEBUG=1` — verbose logs for installer/runtime/archivefetch.
* `MATRIX_HOME` — base dir for `~/.matrix` (state/logs).
* `MATRIX_GIT_ALLOWED_HOSTS` — CSV allow-list for git fetch (defaults to common hosts if not provided via API).
* `MATRIX_GIT_ALLOW_INSECURE=1` — allow `http://` git (discouraged).
* `MATRIX_SDK_DEBUG_GIT=1` — extra git logs.

---

## End-to-end example

```python
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller
from matrix_sdk import runtime

# 1. Initialize the client and installer
client = MatrixClient(base_url="http://127.0.0.1:7300")
installer = LocalInstaller(client)

# 2. Build the project locally
result = installer.build("mcp_server:hello-sse-server@0.1.0", alias="my-server")
print(f"✅ Project installed to: {result.target}")

# 3. Start the server using the runtime module
server = runtime.start(result.target, alias="my-server")
print(f"🚀 Server started with PID {server.pid} on port {server.port}")

# 4. Check status and stop the server
print(f"ℹ️ Current status: {runtime.status()}")
runtime.stop("my-server")
print("🛑 Server stopped.")
```

![](assets/2025-08-16-12-47-54.png)