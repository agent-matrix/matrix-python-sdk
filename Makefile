# Makefile for matrix-python-sdk
# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
# Use the system's python3 to bootstrap the virtual environment
SYS_PYTHON	:= python3
VENV_DIR	:= .venv

# All subsequent commands will use the python interpreter from the venv
PYTHON		:= $(VENV_DIR)/bin/python
PIP		:= $(PYTHON) -m pip

BUILD_DIR	:= dist
SRC_DIR		:= matrix_sdk
TEST_DIR	:= tests
DOCS_DIR	:= docs
CACHE_DIR	:= ~/.cache/matrix-sdk

# A sentinel file to check if the venv is set up and dependencies are installed.
# If pyproject.toml changes, this file becomes outdated, triggering a re-install.
VENV_SENTINEL := $(VENV_DIR)/.install_sentinel

# Conditionally include directories that exist for linting/formatting
PY_TARGETS := $(SRC_DIR)
ifneq ("$(wildcard $(TEST_DIR))","")
	PY_TARGETS += $(TEST_DIR)
endif

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Primary targets:"
	@echo "  install      Setup virtual environment and install all dependencies"
	@echo "  lint         Run ruff to check for issues"
	@echo "  fmt          Auto-format code with black and ruff"
	@echo "  typecheck    Run mypy"
	@echo "  test         Run pytest"
	@echo "  build        Build sdist & wheel"
	@echo "  publish      Upload to PyPI via twine"
	@echo ""
	@echo "Docs targets:"
	@echo "  docs-serve   Serve MkDocs site at http://127.0.0.1:8000"
	@echo "  docs-build   Build MkDocs static site into site/"
	@echo "  docs-clean   Remove built site/ directory"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean        Remove build artifacts, cache, and the virtual environment"
	@echo "  help         Show this message"

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------
# This is the core target for setting up the environment. It's triggered
# automatically by any other target that depends on it.
$(VENV_SENTINEL): pyproject.toml
	@echo "Setting up virtual environment in $(VENV_DIR)..."
	test -f $(PYTHON) || $(SYS_PYTHON) -m venv $(VENV_DIR)
	@echo "Installing dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	@touch $@

# The user-facing 'install' target is now just an alias for ensuring the
# environment is ready.
install: $(VENV_SENTINEL)
	@echo "Virtual environment is up to date."

# ---------------------------------------------------------------------------
# Linting & Formatting
# ---------------------------------------------------------------------------
lint: $(VENV_SENTINEL)
	@echo "Running linter (ruff)…"
	$(PYTHON) -m ruff check $(PY_TARGETS)

fmt: $(VENV_SENTINEL)
	@echo "Formatting code with black…"
	$(PYTHON) -m black $(PY_TARGETS) $(wildcard .github)
	@echo "Fixing imports and other issues with ruff…"
	$(PYTHON) -m ruff check --fix $(PY_TARGETS)

typecheck: $(VENV_SENTINEL)
	@echo "Running mypy…"
	$(PYTHON) -m mypy $(SRC_DIR)

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test: $(VENV_SENTINEL)
	@echo "Running pytest…"
	$(PYTHON) -m pytest -q --disable-warnings --maxfail=1

# ---------------------------------------------------------------------------
# Build & Publish
# ---------------------------------------------------------------------------
build: clean $(VENV_SENTINEL)
	@echo "Building source & wheel…"
	$(PYTHON) -m build --sdist --wheel

publish: build
	@echo "Publishing to PyPI…"
	$(PYTHON) -m twine upload $(BUILD_DIR)/*

# ---------------------------------------------------------------------------
# Documentation (MkDocs)
# ---------------------------------------------------------------------------
docs-serve: $(VENV_SENTINEL)
	@echo "Launching MkDocs dev server…"
	$(PYTHON) -m mkdocs serve

docs-build: $(VENV_SENTINEL)
	@echo "Building MkDocs static site…"
	$(PYTHON) -m mkdocs build

docs-clean:
	@echo "Cleaning MkDocs site/ directory…"
	rm -rf site/

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
clean:
	@echo "Removing build artifacts, cache, and virtual environment…"
	rm -rf $(VENV_DIR)
	rm -rf $(BUILD_DIR) *.egg-info
	rm -rf site/
	rm -rf $(CACHE_DIR)
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Phony targets
# ---------------------------------------------------------------------------
.PHONY: help install lint fmt typecheck test build publish docs-serve docs-build docs-clean clean