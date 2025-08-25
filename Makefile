# Makefile for matrix-python-sdk
# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
SYS_PYTHON := python3
VENV_DIR   := .venv

PYTHON     := $(VENV_DIR)/bin/python
PIP        := $(PYTHON) -m pip

BUILD_DIR  := dist
SRC_DIR    := matrix_sdk
TEST_DIR   := tests
DOCS_DIR   := docs
CACHE_DIR  := ~/.cache/matrix-sdk

# Sentinels
VENV_CREATED   := $(VENV_DIR)/.created
VENV_SENTINEL  := $(VENV_DIR)/.install_sentinel

# Incremental install knobs
# -U upgrades only when needed; strategy keeps upgrades minimal
INSTALL_OPTS ?= -U --upgrade-strategy only-if-needed
BASIC_TOOLS  ?= pip setuptools wheel

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
	@echo "  install      Create venv if needed, then incrementally update deps (fast)"
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

# 1) Ensure venv exists and core tools are up to date (one-time + occasional)
$(VENV_CREATED):
	@echo "Creating virtual environment in $(VENV_DIR) if missing…"
	test -f $(PYTHON) || $(SYS_PYTHON) -m venv $(VENV_DIR)
	@echo "Upgrading basic tools ($(BASIC_TOOLS))…"
	$(PIP) install -U $(BASIC_TOOLS)
	@touch $@

# 2) Heavy dependency sync ONLY when pyproject.toml changes (kept for safety)
$(VENV_SENTINEL): pyproject.toml | $(VENV_CREATED)
	@echo "Detected pyproject.toml change → syncing dev dependencies (one-time)…"
	$(PIP) install $(INSTALL_OPTS) -e ".[dev]"
	@touch $@

# 3) User-facing: fast, incremental install/update for tight dev loops
install: $(VENV_CREATED)
	@echo "Incremental install: updating project & deps if needed (fast)…"
	$(PIP) install $(INSTALL_OPTS) -e ".[dev]"
	@# Touch the sentinel so lint/test targets won’t re-trigger a heavy sync right after.
	@touch $(VENV_SENTINEL)
	@echo "Environment is up to date."

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
