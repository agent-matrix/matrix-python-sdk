# Configuration

You can configure the Matrix Python SDK by passing parameters directly when you initialize the clients. The recommended way to manage these configuration values is by using environment variables.

-----

## Environment Variables (Recommended Practice)

While the SDK does not automatically load environment variables, it is a best practice to store your credentials and URLs in them. You can place these in a `.env` file in your project's root directory.

**Example `.env` file:**

```env
# .env
MATRIX_HUB_URL="http://localhost:7300"
MATRIX_GATEWAY_URL="http://localhost:4444"
MATRIX_TOKEN="YOUR_API_TOKEN"
MATRIX_ADMIN_TOKEN="YOUR_ADMIN_TOKEN"
```

You would then load these variables in your Python code (e.g., using `python-dotenv` and `os.getenv`) and pass them to the client.

-----

## Direct Instantiation

You must pass the configuration values directly to the constructor of the client you are using. This is the only method the SDK supports for configuration.

### `MatrixClient`

Used for interacting with the main Hub API (search, install, etc.).

```python
import os
from matrix_sdk.client import MatrixClient

# Load configuration from environment or other source
hub_url = os.getenv("MATRIX_HUB_URL", "http://localhost:7300")
api_token = os.getenv("MATRIX_TOKEN")

# Pass the values directly to the client
client = MatrixClient(
    base_url=hub_url,
    token=api_token
)

# Now you can use the client
# client.search(q="my query")
```

### `BulkRegistrar`

Used for bulk registration, which requires the gateway URL and an admin token.

```python
import os
import asyncio
from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

# Load configuration from environment or other source
gateway_url = os.getenv("MATRIX_GATEWAY_URL", "http://localhost:4444")
admin_token = os.getenv("MATRIX_ADMIN_TOKEN")

# Pass the values directly to the registrar
registrar = BulkRegistrar(
    gateway_url=gateway_url,
    token=admin_token
)

# Now you can use the registrar
# asyncio.run(registrar.register_servers(sources))
```