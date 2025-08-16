#!/usr/bin/env bash
# examples/sdk_e2e_install_run_demo.sh
# -----------------------------------------------------------------------------
# End-to-end smoke test using ONLY the Python SDK (no CLI):
#   1) Install an entity to ~/.matrix/runners/<alias>/<version>
#   2) Persist alias -> target
#   3) Start the server (writes logs + lock)
#   4) Show status (ps), tail logs, doctor, then stop
# -----------------------------------------------------------------------------
set -Eeuo pipefail
export MATRIX_SDK_DEBUG=1
# --- locate repo root (one dir up from examples/) ----------------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- load .env.local and .env if present (do not override existing env) -----
load_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  # shellcheck disable=SC2162
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *"="* ]] && continue
    local k="${line%%=*}"
    local v="${line#*=}"
    k="$(echo "$k" | xargs)"
    # strip single/double quotes around the value
    v="$(echo "$v" | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
    # only set if not already exported
    [[ -z "${!k+x}" ]] && export "$k=$v" || true
  done < "$f"
}

load_env_file "$ROOT/.env.local"
load_env_file "$ROOT/.env"

# --- activate local venv if present ------------------------------------------
if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "$ROOT/.venv/bin/activate"
elif [[ -f "$ROOT/venv/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "$ROOT/venv/bin/activate"
fi

# --- make the local package importable without install -----------------------
export PYTHONPATH="${PYTHONPATH:-}:$ROOT"

# --- config (env overrides + args) -------------------------------------------
export MATRIX_HUB_BASE="${MATRIX_HUB_BASE:-${MATRIX_HUB_URL:-http://127.0.0.1:7300}}"
export MATRIX_HUB_TOKEN="${MATRIX_HUB_TOKEN:-${MATRIX_TOKEN:-}}"

# CLI-style args override env; fall back to sane defaults
ID="${1:-${ID:-mcp_server:hello-sse-server@0.1.0}}"
ALIAS="${2:-${ALIAS:-hello-world-mcp-sse}}"
LOG_TAIL="${LOG_TAIL:-40}"

# Export for the Python block
export ID ALIAS LOG_TAIL
# Namespaced copies (used by Python as first choice)
export MATRIX_ID="$ID"
export MATRIX_ALIAS="$ALIAS"
export MATRIX_LOG_TAIL="$LOG_TAIL"

echo "── Matrix SDK E2E (install → run → ps → logs → doctor → stop) ─────────────"
echo " HUB_BASE : ${MATRIX_HUB_BASE}"
echo " ID       : ${ID}"
echo " ALIAS    : ${ALIAS}"
echo " LOG_TAIL : ${LOG_TAIL}"
echo "──────────────────────────────────────────────────────────────────────────"
echo
# --- NEW: Clear previous logs for a clean run ---
echo "➜ Clearing old log file to ensure a clean run..."
LOG_FILE="${MATRIX_HOME:-$HOME/.matrix}/logs/${ALIAS}.log"
rm -f "$LOG_FILE"
# --- END NEW ---

# --- Python driver (SDK only) ------------------------------------------------
python - <<'PY'
import json, os, sys, time
from pathlib import Path

# SDK imports (re-exported in matrix_sdk.__init__)
from matrix_sdk import MatrixClient, MatrixError
from matrix_sdk.alias import AliasStore
from matrix_sdk.installer import LocalInstaller
from matrix_sdk.runtime import (
    start as rt_start,
    status as rt_status,
    stop as rt_stop,
    doctor as rt_doctor,
    log_path as rt_log_path,
)

# Robust env reads (namespaced first, then legacy keys, with defaults)
HUB_BASE  = os.getenv("MATRIX_HUB_BASE", os.getenv("MATRIX_HUB_URL", "http://127.0.0.1:7300"))
HUB_TOKEN = os.getenv("MATRIX_HUB_TOKEN") or os.getenv("MATRIX_TOKEN") or None
ID        = os.getenv("MATRIX_ID", os.getenv("ID", "mcp_server:hello-sse-server@0.1.0"))
ALIAS     = os.getenv("MATRIX_ALIAS", os.getenv("ALIAS", "hello-world-mcp-sse"))
LOG_TAIL  = int(os.getenv("MATRIX_LOG_TAIL", os.getenv("LOG_TAIL", "40")))

def echo(msg): print(msg, flush=True)

def install_and_alias():
    echo("➜ Installing via SDK (LocalInstaller.build)…")
    client = MatrixClient(base_url=HUB_BASE, token=HUB_TOKEN)
    installer = LocalInstaller(client)
    try:
        res = installer.build(ID, alias=ALIAS, timeout=900)
    except MatrixError as e:
        echo(f"✖ install failed: HTTP {getattr(e, 'status_code', None)} {e}")
        sys.exit(2)
    except Exception as e:
        echo(f"✖ install failed: {e}")
        sys.exit(2)

    target = res.target
    echo(f"✔ installed plan materialized → {target}")
    # Save alias
    try:
        AliasStore().set(ALIAS, id=ID, target=target)
        echo(f"✔ alias saved: {ALIAS} → {target}")
    except Exception as e:
        echo(f"! warn: failed to save alias: {e}")
    print(json.dumps({"target": target}, ensure_ascii=False))
    return target

def start_server(target):
    echo("➜ Starting server (runtime.start)…")
    try:
        li = rt_start(target, alias=ALIAS, port=None)  # returns LockInfo
    except Exception as e:
        echo(f"✖ start failed: {e}")
        sys.exit(3)
    echo(f"✔ pid={li.pid} port={li.port} alias={li.alias}")
    return li

def show_ps():
    echo("➜ Status (runtime.status)…")
    try:
        rows = rt_status()
    except Exception as e:
        echo(f"! warn: status error: {e}")
        return
    if not rows:
        echo("(no running entries)")
        return
    for r in rows:
        echo(f" • {r.alias:24s} pid={r.pid:<6d} port={r.port:<5d} target={r.target}")

def show_logs():
    echo(f"➜ Last {LOG_TAIL} log line(s)…")
    lp = Path(rt_log_path(ALIAS))
    if not lp.exists():
        echo("(no logs yet)")
        return
    try:
        lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-LOG_TAIL:] if len(lines) > LOG_TAIL else lines
        for ln in tail:
            print(ln)
    except Exception as e:
        echo(f"! warn: logs read error: {e}")

def check_doctor():
    echo("➜ Doctor (runtime.doctor)…")
    try:
        r = rt_doctor(ALIAS)
        echo(f" status: {r.get('status')}")
    except Exception as e:
        echo(f"! warn: doctor error: {e}")

def stop_server():
    echo("➜ Stopping server (runtime.stop)…")
    try:
        ok = rt_stop(ALIAS)
        echo("✔ stopped" if ok else "(!) nothing to stop")
    except Exception as e:
        echo(f"! warn: stop error: {e}")

def main():
    target = install_and_alias()
    _ = start_server(target)
    time.sleep(1.5)  # allow server to print something / open port
    show_ps()
    show_logs()
    check_doctor()
    stop_server()
    echo("Done.")

if __name__ == "__main__":
    main()
PY
