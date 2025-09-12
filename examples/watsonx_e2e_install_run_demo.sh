#!/usr/bin/env bash
# examples/watsonx_e2e_install_run_demo.sh
# -----------------------------------------------------------------------------
# End-to-end smoke test using ONLY the Python SDK (no CLI):
#   1) Install an entity to ~/.matrix/runners/<alias>/<version>
#   2) Persist alias -> target
#   3) Start the server (writes logs + lock)
#   4) Show status (ps), tail logs, doctor, then stop
#
# Flags:
#   --id <UID>          default: mcp_server:watsonx-agent@0.1.0
#   --alias <name>      default: watsonx-chat
#   --hub-base <url>    default: $MATRIX_HUB_BASE or https://api.matrixhub.io
#   --token <jwt>       default: $MATRIX_HUB_TOKEN
#   --port <n>          optional fixed port for runtime.start
#   --log-tail <n>      default: 60
#   --clean-logs        remove previous log file for the alias before start
#   --no-stop           do not stop the server at the end (leave running)
# -----------------------------------------------------------------------------
set -Eeuo pipefail

# Defaults (can be overridden by env or flags)
ID_DEFAULT="mcp_server:watsonx-agent@0.1.0"
ALIAS_DEFAULT="watsonx-chat"
HUB_BASE_DEFAULT="${MATRIX_HUB_BASE:-${MATRIX_HUB_URL:-https://api.matrixhub.io}}"
TOKEN_DEFAULT="${MATRIX_HUB_TOKEN:-${MATRIX_TOKEN:-}}"
LOG_TAIL_DEFAULT=60
PORT_DEFAULT=""
CLEAN_LOGS=0
STOP_AT_END=1

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
    # strip surrounding single/double quotes and spaces safely
    v="$(echo "$v" | sed -e 's/^["'\'' ]*//' -e 's/["'\'' ]*$//')"
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

# --- make the local package importable; PREPEND repo root --------------------
# (Previously appended, which allowed an older site-packages wheel to win.)
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# --- harden the manifest fallback behavior -----------------------------------
# Ensure fallback is enabled and domains used by GitHub are allowed.
export MATRIX_SDK_ALLOW_MANIFEST_FETCH="${MATRIX_SDK_ALLOW_MANIFEST_FETCH:-1}"
export MATRIX_SDK_MANIFEST_DOMAINS="${MATRIX_SDK_MANIFEST_DOMAINS:-raw.githubusercontent.com,github.com,codeload.github.com}"

# --- parse flags --------------------------------------------------------------
ID="$ID_DEFAULT"
ALIAS="$ALIAS_DEFAULT"
HUB_BASE="$HUB_BASE_DEFAULT"
TOKEN="$TOKEN_DEFAULT"
LOG_TAIL="$LOG_TAIL_DEFAULT"
PORT="$PORT_DEFAULT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id)         ID="${2:?}"; shift 2;;
    --alias)      ALIAS="${2:?}"; shift 2;;
    --hub-base)   HUB_BASE="${2:?}"; shift 2;;
    --token)      TOKEN="${2:-}"; shift 2;;
    --log-tail)   LOG_TAIL="${2:?}"; shift 2;;
    --port)       PORT="${2:?}"; shift 2;;
    --clean-logs) CLEAN_LOGS=1; shift;;
    --no-stop)    STOP_AT_END=0; shift;;
    -h|--help)
      sed -n '1,120p' "$0"
      exit 0;;
    *)
      echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

export MATRIX_SDK_DEBUG="${MATRIX_SDK_DEBUG:-1}"

# Export for the Python block
export MATRIX_HUB_BASE="$HUB_BASE"
export MATRIX_HUB_TOKEN="$TOKEN"
export MATRIX_ID="$ID"
export MATRIX_ALIAS="$ALIAS"
export MATRIX_LOG_TAIL="$LOG_TAIL"
export MATRIX_FIXED_PORT="$PORT"

echo "── Matrix SDK E2E (install → run → ps → logs → doctor → stop) ─────────────"
echo " HUB_BASE : ${MATRIX_HUB_BASE}"
echo " ID       : ${MATRIX_ID}"
echo " ALIAS    : ${MATRIX_ALIAS}"
echo " LOG_TAIL : ${MATRIX_LOG_TAIL}"
[[ -n "${MATRIX_FIXED_PORT}" ]] && echo " PORT     : ${MATRIX_FIXED_PORT}" || true
echo "──────────────────────────────────────────────────────────────────────────"
echo

# --- optionally clear previous logs ------------------------------------------
if [[ "$CLEAN_LOGS" -eq 1 ]]; then
  LOG_FILE="${MATRIX_HOME:-$HOME/.matrix}/logs/${ALIAS}.log"
  echo "➜ Clearing old log file: ${LOG_FILE}"
  rm -f "$LOG_FILE" || true
fi

# --- ensure we stop on exit if requested -------------------------------------
_cleanup() {
  if [[ "$STOP_AT_END" -eq 1 ]]; then
    # call stop in the same python env for robustness
    python - "$ALIAS" <<'PY'
import sys
from matrix_sdk.runtime import stop as rt_stop
alias = sys.argv[1]
try:
    ok = rt_stop(alias)
    print(f"✓ auto-stop: {alias} {'stopped' if ok else '(not running)'}")
except Exception as e:
    print(f"! auto-stop warn: {e}")
PY
  fi
}
trap _cleanup EXIT

# --- Python driver (SDK only) ------------------------------------------------
python - <<'PY'
import json, os, sys, time, shutil
from pathlib import Path

# Sanity: show which SDK we're using (helps catch path precedence issues)
try:
    import matrix_sdk as _ms
    print(f"[debug] using matrix_sdk from: {_ms.__file__}", flush=True)
except Exception:
    pass

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

HUB_BASE   = os.getenv("MATRIX_HUB_BASE", "https://api.matrixhub.io")
HUB_TOKEN  = os.getenv("MATRIX_HUB_TOKEN") or None
ID         = os.getenv("MATRIX_ID", "mcp_server:watsonx-agent@0.1.0")
ALIAS      = os.getenv("MATRIX_ALIAS", "watsonx-chat")
LOG_TAIL   = int(os.getenv("MATRIX_LOG_TAIL", "60"))
FIXED_PORT = os.getenv("MATRIX_FIXED_PORT") or None
if FIXED_PORT:
    try:
        FIXED_PORT = int(FIXED_PORT)
    except Exception:
        FIXED_PORT = None

def echo(msg): print(msg, flush=True)

def install_and_alias():
    echo("➜ Installing via SDK (LocalInstaller.build)…")
    client = MatrixClient(base_url=HUB_BASE, token=HUB_TOKEN)
    installer = LocalInstaller(client)
    try:
        # Increased timeout in case git fetch takes longer on CI or slow links
        res = installer.build(ID, alias=ALIAS, timeout=1200)
    except MatrixError as e:
        echo(f"✖ install failed: HTTP {getattr(e, 'status_code', None)} {e}")
        sys.exit(2)
    except Exception as e:
        echo(f"✖ install failed: {e}")
        sys.exit(2)

    target = res.target
    echo(f"✔ installed plan materialized → {target}")

    # --- ADDED LOGIC: Copy .env file into the target directory ---
    source_env_file = ".env"
    if os.path.exists(source_env_file):
        try:
            shutil.copy2(source_env_file, target)
            echo(f"➜ Copied local '{source_env_file}' to runner environment.")
        except Exception as e:
            echo(f"! warn: failed to copy '{source_env_file}': {e}")
    # -------------------------------------------------------------

    # Save/overwrite alias mapping to the new target
    try:
        AliasStore().set(ALIAS, id=ID, target=target)
        echo(f"✔ alias saved: {ALIAS} → {target}")
    except Exception as e:
        echo(f"! warn: failed to save alias: {e}")
    return target

def start_server(target):
    echo("➜ Starting server (runtime.start)…")
    try:
        li = rt_start(target, alias=ALIAS, port=FIXED_PORT)  # returns LockInfo
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
        # Robust for connectors: pid may be 0, port may be None
        pid_str  = "-" if r.pid is None else str(r.pid)
        port_str = "-" if r.port is None else str(r.port)
        alias_str = r.alias or "-"
        target_str = r.target or "-"
        echo(f" • {alias_str:24s} pid={pid_str:<6s} port={port_str:<5s} target={target_str}")

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
        for k in ("pid","port","entry","type","url","health"):
            if k in r:
                echo(f"  {k}: {r[k]}")
    except Exception as e:
        echo(f"! warn: doctor error: {e}")

def main():
    target = install_and_alias()
    _ = start_server(target)
    time.sleep(1.5)  # allow server to print something / open port
    show_ps()
    show_logs()
    check_doctor()
    echo("Done.")

if __name__ == "__main__":
    main()
PY
