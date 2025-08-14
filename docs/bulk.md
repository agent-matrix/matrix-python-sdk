# Bulk Registration

The SDK includes a tiny, async **Bulk Registrar** that discovers MCP servers in a repo (or ZIP) and registers them into an **MCP Gateway Admin API** (e.g., `http://localhost:4444`). It’s optional and independent of the Hub.

!!! note
    The registrar **does not** modify your local project. It discovers manifests and calls your **Gateway Admin API** to create/update server entries.

---

## What it does (in one picture)

1. **Discovery (matrix-first)**  
   - If a `matrix/` folder exists, read:
     - `matrix/index.json` (list of servers **or** list of manifest paths), and/or
     - `matrix/*.manifest.json`
   - Else, fall back to `pyproject.toml` → `[tool.mcp_server]`.

2. **Normalize → Manifest**  
   - Convert discovery into a canonical `ServerManifest` (Pydantic v1/v2 compatible).

3. **Register (upsert)**  
   - POST to your Gateway’s Admin endpoint:
     - Try **JSON** first.
     - If the gateway rejects JSON with `422/400/415` (e.g. “Missing required field: 'name'”), auto-fallback to **form URL-encoded** with a sanitized `name`.

4. **Resilience**  
   - Idempotency key (SHA256 of manifest payload).
   - Concurrency + exponential backoff.
   - Optional **capability probe** (best-effort GET `${endpoint}/capabilities`).

---

## Quickstart (Python)

```python
import asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

sources = [
    # Prefer a local ZIP (no network). Else use git or an already-cloned directory.
    {"kind": "git", "url": "https://github.com/IBM/docling-mcp.git", "ref": "main", "probe": True}
]

registrar = BulkRegistrar(
    gateway_url="http://localhost:4444",
    token="YOUR_ADMIN_TOKEN",
    concurrency=10,   # optional, default 50 in the class but you can override here
    probe=True,       # optional, defaults to True
)

results = asyncio.run(registrar.register_servers(sources))
print(results)
```

Typical successful result item from the gateway:

```json
{"message": "Server created successfully!", "success": true}
```

---

## CLI usage (module)

You can invoke the registrar via the module entrypoint:

```bash
python -m matrix_sdk.bulk.cli \
  --git https://github.com/ruslanmv/hello-mcp --ref main \
  --gateway-url http://localhost:4444 \
  --token "$ADMIN_TOKEN"
```

**Alternate sources**

```bash
# Use a local ZIP (preferred — avoids git/network):
python -m matrix_sdk.bulk.cli \
  --zip /path/to/repo.zip \
  --gateway-url http://localhost:4444 \
  --token "$ADMIN_TOKEN"

# Use an already-cloned directory:
python -m matrix_sdk.bulk.cli \
  --dir /path/to/repo \
  --gateway-url http://localhost:4444 \
  --token "$ADMIN_TOKEN"
```

**Optional flags**

* `--concurrency 20` – number of parallel registrations
* `--no-probe` – disable capability probing
* `--env-file .env.local` – load env first (KEY=VALUE lines)

---

## Source descriptor schema

A **source** describes where to discover manifests.

| Field   | Type | Required        | Example                                           |
| ------- | ---- | --------------- | ------------------------------------------------- |
| `kind`  | enum | yes             | `"zip"` \| `"git"` \| `"dir"`                     |
| `path`  | str  | for `zip`/`dir` | `/abs/path/repo.zip` or `/abs/path/repo`          |
| `url`   | str  | for `git`       | `https://github.com/IBM/docling-mcp.git`          |
| `ref`   | str  | optional (git)  | `main` / tag / SHA                                |
| `probe` | bool | optional        | `true` (default) – try `${endpoint}/capabilities` |

You can pass **multiple** sources; the registrar handles each (with overall concurrency control).

---

## How discovery works

1. **Matrix folder (preferred)**

   * `matrix/index.json` can be:

     * An **array of strings** (each a path to `*.manifest.json` under `matrix/`), or
     * An **array of objects** with `"type": "mcp_server"` and server fields.
   * Any `matrix/*.manifest.json` is also read.

2. **Fallback: `pyproject.toml`**

   * `[tool.mcp_server]` block is mapped to a `ServerManifest`.
   * Endpoint is normalized to:

     ```toml
     [tool.mcp_server.endpoint]
     transport = "sse"  # or "http" | "ws" | "stdio"
     url = "http://127.0.0.1:8000"
     schema = "mcp/v1"
     auth = "none"
     ```

---

## Registration behavior

* **JSON first**, then **auto-fallback to form** if the gateway says your JSON is invalid for the legacy “create” route:

  * Common signal: `HTTP 422` with `"Missing required field: 'name'"` or `"Invalid name"`.
* **Name sanitization** (for the form fallback):

  * Allowed: letters, digits, underscore (`_`), dot (`.`), hyphen (`-`), spaces.
  * Disallowed chars are removed, whitespace is collapsed, and result is trimmed to 255 chars.
  * If empty after cleaning, a stable fallback like `server-<hash>` is used.

**Idempotency**

* For each manifest, the registrar computes a SHA256 idempotency key from the JSON payload (sorted keys).
* You can safely re-run without creating duplicates (server-side must support idempotency).

**Concurrency & backoff**

* Overall concurrency is bound by a semaphore (configurable).
* Each upsert uses exponential backoff with jitter (defaults: `max_retries=5`, `base_delay=1.0s`).

**Capability probe (optional)**

* If enabled, for HTTP/SSE endpoints the registrar **GETs** `${endpoint.url}/capabilities` and merges the returned list into `capabilities`.

---

## Environment variables

You can control the CLI/script by env vars (all optional):

| Variable                   | Meaning                                                           | Default                 |
| -------------------------- | ----------------------------------------------------------------- | ----------------------- |
| `GATEWAY_URL`              | Admin API base URL                                                | `http://localhost:4444` |
| `ADMIN_TOKEN`              | Bearer token (alternates: `GATEWAY_TOKEN`, `GATEWAY_ADMIN_TOKEN`) | *(none)*                |
| `ZIP_PATH`                 | Path to local ZIP                                                 | *(none)*                |
| `DIR_PATH`                 | Path to local directory                                           | *(none)*                |
| `GIT_URL`                  | Git repo URL                                                      | *(none)*                |
| `GIT_REF`                  | Git ref                                                           | `main`                  |
| `CONCURRENCY`              | Parallel registrations                                            | `10` (CLI)              |
| `PROBE`                    | `true`/`false` capability probing                                 | `true`                  |
| `ENV_FILE` / `DOTENV_FILE` | Path to `.env` to load first                                      | `.env.local`            |

---

## Return shape

`BulkRegistrar.register_servers(sources)` returns a **flat list** of results (one per discovered manifest). Each item is either:

```json
{ "message": "Server created successfully!", "success": true }
```

or an error wrapper, e.g.:

```json
{ "error": "Gateway upsert failed: HTTP 422, request_id=…, body={...}" }
```

---

## Troubleshooting

**HTTP 401/403 (Unauthorized/Forbidden)**
→ Set `ADMIN_TOKEN` (or `GATEWAY_TOKEN` / `GATEWAY_ADMIN_TOKEN`).

```bash
export ADMIN_TOKEN="..."
```

**HTTP 422 “Missing required field: 'name'” or “Invalid name”**
→ Your gateway expects **form fields** and a sanitized `name`. The client will fallback to form automatically and clean the name. If you still see the error, your original `name`/`id` may be empty; discovery will synthesize a safe fallback.

**`AnyUrl is not JSON serializable`**
→ You’re likely bypassing the provided models. The registrar already serializes models into JSON-safe dicts; don’t send raw Pydantic types to `httpx`.

**`RuntimeWarning: 'matrix_sdk.bulk.cli' in sys.modules…`**
→ Ensure `matrix_sdk/bulk/__init__.py` doesn’t import `cli`. Keep it minimal:

```python
# matrix_sdk/bulk/__init__.py
__all__ = []
```

**Git clone failures**
→ Prefer a local ZIP (`ZIP_PATH`) to avoid any network/git issues.

---

## Security notes

* Only run against Gateways you control. The registrar sends **Bearer tokens** to the URL you provide.
* If you store tokens in `.env` files, protect those files (e.g., don’t commit them).
* The registrar does **not** execute arbitrary code in the repo; it reads manifests and TOML/JSON files.

---

## Minimal API reference

### `class BulkRegistrar`

```python
BulkRegistrar(
    gateway_url: str,
    token: str,
    concurrency: int = 50,
    probe: bool = True,
    backoff_config: dict | None = None
)
```

* `register_servers(sources: Iterable[dict]) -> list[Any]`
  Discovers manifests for each source and upserts them concurrently, returning a flat list of results/errors.

### Source kinds

* `{"kind": "zip", "path": "/path/repo.zip", "probe": true}`
* `{"kind": "dir", "path": "/path/repo", "probe": true}`
* `{"kind": "git", "url": "https://...", "ref": "main", "probe": true}`

---

## Worked examples

**Register a local ZIP**

```bash
export ADMIN_TOKEN=...
python -m matrix_sdk.bulk.cli \
  --zip ./hello-mcp-main.zip \
  --gateway-url http://localhost:4444
```

**Register docling-mcp (Git)**

```bash
export ADMIN_TOKEN=...
python -m matrix_sdk.bulk.cli \
  --git https://github.com/IBM/docling-mcp.git --ref main \
  --gateway-url http://localhost:4444
```

**Script example**

```bash
# examples/bulk_register_example.sh
bash examples/bulk_register_example.sh
```

---

## Compatibility

* **Python**: 3.10+
* **Pydantic**: v1 *or* v2 (the models handle both)
* **httpx**: 0.27+

If your Gateway’s Admin API only supports form POSTs, the registrar will **automatically switch** after a JSON attempt fails with a recognizable error.




See [Usage](usage.md) for more scenarios.