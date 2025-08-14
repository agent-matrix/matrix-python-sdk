#!/usr/bin/env bash
# examples/bulk_register_example.sh
#
# Wrapper to run the bulk registration (matrix-first discovery).
# Prefers a local ZIP, supports a local DIR, falls back to Git.
#
# Env you can set (or via .env file):
#   GATEWAY_URL="http://localhost:4444"
#   ADMIN_TOKEN="..."    # or GATEWAY_TOKEN / GATEWAY_ADMIN_TOKEN
#   ZIP_PATH="/abs/path/to/repo.zip"
#   DIR_PATH="/abs/path/to/checked-out/repo"
#   GIT_URL="https://github.com/ruslanmv/hello-mcp"
#   GIT_REF="main"
#   CONCURRENCY="20"
#   PROBE="true"         # "false" to disable capability probing
#   DOTENV_FILE="path/to/.env.local"   # override default path
#
# Usage:
#   bash examples/bulk_register_example.sh

set -Eeuo pipefail

# ---- error trap for friendlier failures --------------------------------------
trap 'ec=$?; echo "❌ Failed at line $BASH_LINENO: exit $ec" >&2; exit $ec' ERR

# ---- locate repo root and dotenv file ----------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOTENV_FILE="${DOTENV_FILE:-${REPO_ROOT}/.env.local}"

# ---- load .env.local if present (export all) ---------------------------------
if [[ -f "${DOTENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "${DOTENV_FILE}"
  set +a
  echo "Loaded env: ${DOTENV_FILE}"
else
  echo "No .env file found at ${DOTENV_FILE} (skipping)."
fi

# ---- normalize token names ---------------------------------------------------
if [[ -z "${ADMIN_TOKEN:-}" ]]; then
  if [[ -n "${GATEWAY_ADMIN_TOKEN:-}" ]]; then
    export ADMIN_TOKEN="${GATEWAY_ADMIN_TOKEN}"
  elif [[ -n "${GATEWAY_TOKEN:-}" ]]; then
    export ADMIN_TOKEN="${GATEWAY_TOKEN}"
  fi
fi

# ---- small helpers -----------------------------------------------------------
mask_token () {
  local t="${1:-}"
  if [[ -z "$t" ]]; then
    echo "(unset)"
  else
    local len=${#t}
    if (( len > 14 )); then
      echo "${t:0:6}…${t: -4} (len=${len})"
    else
      echo "(set, len=${len})"
    fi
  fi
}

is_false () {
  case "${1:-}" in
    0|false|False|FALSE|no|No|NO|off|Off|OFF) return 0;;
    *) return 1;;
  esac
}

# ---- config summary ----------------------------------------------------------
echo "Gateway URL : ${GATEWAY_URL:-http://localhost:4444}"
echo "Admin token : $(mask_token "${ADMIN_TOKEN:-}")"
echo "ZIP_PATH    : ${ZIP_PATH:-(unset)}"
echo "DIR_PATH    : ${DIR_PATH:-(unset)}"
echo "GIT_URL     : ${GIT_URL:-https://github.com/ruslanmv/hello-mcp}"
echo "GIT_REF     : ${GIT_REF:-main}"
echo "CONCURRENCY : ${CONCURRENCY:-20}"
echo "PROBE       : ${PROBE:-true}"
echo

# ---- build CLI args for Python module ----------------------------------------
PY="${PYTHON:-python}"
ARGS=( "--gateway-url" "${GATEWAY_URL:-http://localhost:4444}" )

# Token (optional but recommended)
if [[ -n "${ADMIN_TOKEN:-}" ]]; then
  ARGS+=( "--token" "${ADMIN_TOKEN}" )
fi

# Concurrency
if [[ -n "${CONCURRENCY:-}" ]]; then
  ARGS+=( "--concurrency" "${CONCURRENCY}" )
fi

# Probe flag
if is_false "${PROBE:-}"; then
  ARGS+=( "--no-probe" )
fi

# Pass env file path to Python (nice for logging/replicability)
if [[ -f "${DOTENV_FILE}" ]]; then
  ARGS+=( "--env-file" "${DOTENV_FILE}" )
fi

# Source selection: prefer ZIP, then DIR, else Git
if [[ -n "${ZIP_PATH:-}" ]]; then
  if [[ ! -f "${ZIP_PATH}" ]]; then
    echo "❌ ZIP_PATH is set but file not found: ${ZIP_PATH}" >&2
    exit 2
  fi
  ARGS+=( "--zip" "${ZIP_PATH}" )
  echo "Source      : ZIP (${ZIP_PATH})"
elif [[ -n "${DIR_PATH:-}" ]]; then
  if [[ ! -d "${DIR_PATH}" ]]; then
    echo "❌ DIR_PATH is set but directory not found: ${DIR_PATH}" >&2
    exit 2
  fi
  ARGS+=( "--dir" "${DIR_PATH}" )
  echo "Source      : DIR (${DIR_PATH})"
else
  # Git fallback
  command -v git >/dev/null 2>&1 || { echo "❌ git is required for --git fallback" >&2; exit 2; }
  ARGS+=( "--git" "${GIT_URL:-https://github.com/ruslanmv/hello-mcp}" "--ref" "${GIT_REF:-main}" )
  echo "Source      : GIT (${GIT_URL:-https://github.com/ruslanmv/hello-mcp}@${GIT_REF:-main})"
  echo "Hint        : To avoid git/network issues, set ZIP_PATH to a local archive."
fi

echo

# ---- run the Python CLI ------------------------------------------------------
# Uses: matrix_sdk.bulk.cli (matrix-first discovery)
set -x
"${PY}" -m matrix_sdk.bulk.cli "${ARGS[@]}"
set +x
