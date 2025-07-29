# Makefile for matrix-python-sdk
# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------
PYTHON      := python3
PIP         := $(PYTHON) -m pip
BUILD_DIR   := dist
SRC_DIR     := matrix_sdk
TEST_DIR    := tests
DOCS_DIR    := docs
CACHE_DIR   := ~/.cache/matrix-sdk

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
	@echo "  install       Install runtime + dev dependencies"
	@echo "  lint          Run ruff to check for issues"
	@echo "  fmt           Auto-format code with black and ruff"
	@echo "  typecheck     Run mypy"
	@echo "  test          Run pytest"
	@echo "  build         Build sdist & wheel"
	@echo "  publish       Upload to PyPI via twine"
	@echo ""
	@echo "Docs targets:"
	@echo "  docs-serve    Serve MkDocs site at http://127.0.0.1:8000"
	@echo "  docs-build    Build MkDocs static site into site/"
	@echo "  docs-clean    Remove built site/ directory"
	@echo ""
	@echo "Cleanup:"
	@echo "  clean         Remove build + cache artifacts"
	@echo "  help          Show this message"

# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------
install:
	@echo "Installing dependencies…"
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

# ---------------------------------------------------------------------------
# Linting & Formatting
# ---------------------------------------------------------------------------
lint:
	@echo "Running linter (ruff)…"
	$(PYTHON) -m ruff check $(PY_TARGETS)

fmt:
	@echo "Formatting code with black…"
	$(PYTHON) -m black $(PY_TARGETS) $(wildcard .github)
	@echo "Fixing imports and other issues with ruff…"
	$(PYTHON) -m ruff check --fix $(PY_TARGETS)

typecheck:
	@echo "Running mypy…"
	$(PYTHON) -m mypy $(SRC_DIR)

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test:
	@echo "Running pytest…"
	$(PYTHON) -m pytest -q --disable-warnings --maxfail=1

# ---------------------------------------------------------------------------
# Build & Publish
# ---------------------------------------------------------------------------
build: clean
	@echo "Building source & wheel…"
	$(PYTHON) -m build --sdist --wheel

publish: build
	@echo "Publishing to PyPI…"
	$(PYTHON) -m twine upload $(BUILD_DIR)/*

# ---------------------------------------------------------------------------
# Documentation (MkDocs)
# ---------------------------------------------------------------------------
docs-serve:
	@echo "Launching MkDocs dev server…"
	$(PYTHON) -m mkdocs serve

docs-build:
	@echo "Building MkDocs static site…"
	$(PYTHON) -m mkdocs build

docs-clean:
	@echo "Cleaning MkDocs site/ directory…"
	rm -rf site/

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
clean:
	@echo "Removing build artifacts and cache…"
	rm -rf $(BUILD_DIR) *.egg-info
	rm -rf site/
	rm -rf $(CACHE_DIR)
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Phony targets
# ---------------------------------------------------------------------------
.PHONY: help install lint fmt typecheck test build publish docs-serve docs-build docs-clean clean
