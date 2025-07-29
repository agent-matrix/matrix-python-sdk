# API Reference

## `MatrixClient`

```python
class MatrixClient:
    def __init__(self, base_url: str, token: Optional[str] = None,
                 timeout: float = 20.0, cache: Optional[Cache] = None,
                 user_agent: Optional[str] = None) -> None

    def search(...): ...
    def get_entity(...): ...
    def install(...): ...
    def list_remotes(...): ...
    def add_remote(...): ...
    def trigger_ingest(...): ...
```

## `BulkRegistrar`

```python
class BulkRegistrar:
    def __init__(self, gateway_url: str, token: str, concurrency: int = 50)
    async def register_servers(sources: Iterable[Dict[str, Any]]) -> list
```

## Types

* `SearchItem`
* `SearchResponse`
* `EntityDetail`
* `ServerManifest`
* `InstallOutcome`
* `EndpointDescriptor`
* `MatrixAPIError`

## `Cache`

```python
class Cache:
    def __init__(self, cache_dir: Path|str, ttl: int) -> None
    def get(self, key: str, *, allow_expired: bool = False) -> CachedResponse|None
    def set(self, key: str, response: Any, *, etag: Optional[str] = None) -> None
```
