# Installation

## From PyPI

```bash
pip install matrix-python-sdk
```

> Requires Python 3.10+.

## From source

```bash
git clone https://github.com/agent-matrix/matrix-python-sdk.git
cd matrix-python-sdk
pip install .
```

## Upgrade

```bash
pip install --upgrade matrix-python-sdk
```

## Optional extras

* `httpx` (installed): HTTP client used by the SDK.
* `pydantic` (installed): typed models for responses.
* `pyyaml` (optional): enables `fetch_manifest()` to parse YAML manifests.