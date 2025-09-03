#!/usr/bin/env bash
#
# A simple example demonstrating the full local lifecycle of a component:
# 1. Install a component using the SDK.
# 2. Start it as a background process.
# 3. Check its health and view its logs.
# 4. Stop the process.
#
set -e # Exit immediately if a command fails

# --- Environment Setup ---
# Find the project's root directory to make the SDK importable
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${PYTHONPATH:-}:${ROOT_DIR}"

# Set the Matrix Hub URL (can be overridden by an environment variable)
export MATRIX_HUB_BASE="${MATRIX_HUB_BASE:-https://api.matrixhub.io}"

# --- ADDED FOR DEBUGGING ---
# Enable verbose logging from the Matrix SDK installer
export MATRIX_SDK_DEBUG="1"

# --- Python Driver Script ---
# The following Python code is executed directly by the shell script.
python - <<'END_PYTHON'
import time
import os
from matrix_sdk.client import MatrixClient
from matrix_sdk.installer import LocalInstaller
from matrix_sdk import runtime

# --- Configuration ---
COMPONENT_ID = "mcp_server:hello-sse-server@0.1.0"
ALIAS = "hello-sse-example"

print(f"--- Starting Matrix SDK Simple Example ---")
print(f"HUB URL: {os.getenv('MATRIX_HUB_BASE')}")
print(f"COMPONENT: {COMPONENT_ID}")
print("-" * 40)

try:
    # 1. Initialize the client and installer
    client = MatrixClient(base_url=os.getenv("MATRIX_HUB_BASE"))
    installer = LocalInstaller(client)

    # 2. Install the component locally
    print(f"➜ Installing '{ALIAS}'...")
    result = installer.build(COMPONENT_ID, alias=ALIAS)
    print(f"✅ Project installed to: {result.target}\n")

    # 3. Start the server
    print(f"➜ Starting '{ALIAS}' as a background process...")
    lock = runtime.start(result.target, alias=ALIAS)
    print(f"🚀 Server started with PID: {lock.pid} on Port: {lock.port}")
    
    # Give the server a moment to initialize before checking it
    time.sleep(2)
    print("-" * 40)

    # 4. Health & Logs
    print(f"➜ Performing health check on '{ALIAS}'...")
    health_status = runtime.doctor(ALIAS)
    print(f"🩺 Health status: {health_status.get('status')}\n")

    print(f"➜ Tailing last 10 log lines for '{ALIAS}':")
    for line in runtime.tail_logs(ALIAS, n=10):
        print(line, end="")
    print("-" * 40)

except Exception as e:
    print(f"\n❌ An error occurred: {e}")
finally:
    # 5. Stop the server
    print(f"\n➜ Stopping '{ALIAS}'...")
    if runtime.stop(ALIAS):
        print("🛑 Server stopped successfully.")
    else:
        print("⚠️ Server was not running or already stopped.")

print("\n--- Example Finished ---")

END_PYTHON
