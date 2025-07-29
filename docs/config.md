# Configuration

You can specify your default Matrix Hub and Gateway in Python code or via config files.

## Python Setup

```python
import os
os.environ["MATRIX_HUB_URL"] = "http://your-matrix-hub:7300"
os.environ["MATRIX_GATEWAY_URL"] = "http://your-gateway:4444"
os.environ["MATRIX_TOKEN"] = "YOUR_TOKEN"
```

You can also pass the URLs and tokens directly to `MatrixClient` or `BulkRegistrar`.

## Config File Example

`matrix_sdk_config.toml`:

```toml
hub_url = "http://localhost:7300"
gateway_url = "http://localhost:4444"
token = "YOUR_TOKEN"
```

To load in Python:

```python
import tomli
cfg = tomli.loads(open("matrix_sdk_config.toml", "rb").read())
client = MatrixClient(cfg["hub_url"], token=cfg["token"])
```
