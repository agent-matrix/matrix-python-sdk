#!/usr/bin/env bash
#
# scripts/build_wheels.sh — build local wheel(s) for matrix-python-sdk for testing (no PyPI publish)
#
# Usage:
#   ./scripts/build_wheels.sh              # builds in current project dir
#   PACKAGE_DIR=./path/to/sdk ./scripts/build_wheels.sh
#
# Outputs wheels and sdist under ./dist and copies them to ./wheelhouse
# You can then install on another machine via:
#   pip install --no-index --find-links=wheelhouse matrix-python-sdk==<version>
# or directly:
#   pip install wheelhouse/matrix_python_sdk-<ver>-py3-none-any.whl

set -Eeuo pipefail

PROJECT_DIR="${PACKAGE_DIR:-$(pwd)}"
cd "${PROJECT_DIR}"

step() { printf "\033[1;34m▶ %s\033[0m\n" "$*"; }
info() { printf "ℹ %s\n" "$*"; }
warn() { printf "\033[33m⚠ %s\033[0m\n" "$*"; }
die()  { printf "\033[31m✖ %s\033[0m\n" "$*"; exit 1; }

# Sanity checks
[[ -f pyproject.toml ]] || die "pyproject.toml not found in ${PROJECT_DIR}"

# Ensure build tooling is present (user or venv env)
step "Installing/Updating build backend (pip, build, wheel)"
python -m pip install --upgrade pip build wheel >/dev/null

# Clean prior artifacts
step "Cleaning old dist/ and wheelhouse/"
rm -rf dist build *.egg-info wheelhouse
mkdir -p wheelhouse

# Build sdist + wheel
step "Building sdist and wheel"
python -m build

# Copy to wheelhouse
step "Collecting artifacts into wheelhouse/"
cp -v dist/*.whl wheelhouse/
cp -v dist/*.tar.gz wheelhouse/ || true

# Print result and how-to
step "Done"
ls -1 wheelhouse | sed 's/^/  - /'

cat <<'EOF'

Next steps (install from wheelhouse):

  # Create and activate a virtualenv (recommended)
  python -m venv .venv
  source .venv/bin/activate    # Windows: .venv\Scripts\activate

  # Install without touching PyPI
  python -m pip install --no-index --find-links=wheelhouse matrix-python-sdk

  # Or install a specific built file
  python -m pip install wheelhouse/matrix_python_sdk-*-py3-none-any.whl

EOF
