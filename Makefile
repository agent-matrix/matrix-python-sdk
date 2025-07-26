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
MKDOCS      := mkdocs
CACHE_DIR   := ~/.cache/matrix-sdk

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Primary targets:"
	@echo "  install       Install runtime + dev dependencies"
	@echo "  lint          Run ruff + flake8"
	@echo "  fmt           Run black"
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
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

# ---------------------------------------------------------------------------
# Linting & Formatting
# ---------------------------------------------------------------------------
lint:
	@echo "Running linter (ruff + flake8)…"
	ruff check $(SRC_DIR) $(TEST_DIR)
	flake8 $(SRC_DIR) $(TEST_DIR)

fmt:
	@echo "Formatting code with black…"
	black $(SRC_DIR) .github workflows mkdocs.yml

typecheck:
	@echo "Running mypy…"
	mypy $(SRC_DIR)

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------
test:
	@echo "Running pytest…"
	pytest -q --disable-warnings --maxfail=1

# ---------------------------------------------------------------------------
# Build & Publish
# ---------------------------------------------------------------------------
build:
	@echo "Building source & wheel…"
	$(PYTHON) -m build --sdist --wheel

publish: build
	@echo "Publishing to PyPI…"
	twine upload $(BUILD_DIR)/*

# ---------------------------------------------------------------------------
# Documentation (MkDocs)
# ---------------------------------------------------------------------------
docs-serve:
	@echo "Launching MkDocs dev server…"
	$(MKDOCS) serve

docs-build:
	@echo "Building MkDocs static site…"
	$(MKDOCS) build

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
