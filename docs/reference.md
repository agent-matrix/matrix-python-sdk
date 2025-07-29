# API Reference

This document provides a high-level overview of the key classes and data models in the Matrix Python SDK.

---
## `MatrixClient`

The primary client for interacting with the Matrix Hub API.

**Location**: `matrix_sdk.client.MatrixClient`

```python
class MatrixClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        *,
        timeout: float = 20.0,
        cache: Optional[Cache] = None,
        user_agent: Optional[str] = None,
    ) -> None

    def search(self, *, q: str, type: Optional[str] = None, **filters: Any) -> Union[SearchResponse, Dict[str, Any]]: ...

    def get_entity(self, id: str) -> Union[EntityDetail, Dict[str, Any]]: ...

    def install(self, id: str, target: str, version: Optional[str] = None) -> Union[InstallOutcome, Dict[str, Any]]: ...

    def list_remotes(self) -> Dict[str, Any]: ...

    def add_remote(self, url: str, *, name: Optional[str] = None, trust_policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...

    def trigger_ingest(self, name: str) -> Dict[str, Any]: ...
```

-----

## `BulkRegistrar`

An asynchronous client for registering multiple MCP servers concurrently.

**Location**: `matrix_sdk.bulk.bulk_registrar.BulkRegistrar`

```python
class BulkRegistrar:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        concurrency: int = 50,
        probe: bool = True,
        backoff_config: Optional[dict[str, Union[int, float]]] = None,
    ) -> None

    async def register_servers(self, sources: Iterable[Dict[str, Any]]) -> List[Any]: ...
```

-----

## Data Models

The SDK uses Pydantic models for all API responses. These are the primary schemas you will work with.

### Main Schemas

**Location**: `matrix_sdk.schemas`

  * `SearchItem`
  * `SearchResponse`
  * `EntityDetail`
  * `InstallStepResult`
  * `InstallOutcome`
  * `MatrixAPIError`

### Bulk Registration Schemas

**Location**: `matrix_sdk.bulk.schemas`

  * `ServerManifest`
  * `EndpointDescriptor`

-----

## `Cache`

An optional file-based cache for API responses.

**Location**: `matrix_sdk.cache.Cache`

```python
class Cache:
    def __init__(self, cache_dir: Union[Path, str] = ..., ttl: int = ...) -> None

    def get(self, key: str, *, allow_expired: bool = False) -> Optional[CachedResponse]: ...

    def set(self, key: str, response: Any, *, etag: Optional[str] = None) -> None: ...
```