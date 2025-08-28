# ====================================================================================
#
#   This is the construct program for the Matrix Python SDK.
#   Know thyself.
#
#   TRANSMISSION >> Access available programs with 'make help'
#
# ====================================================================================

# System & Environment
BRIGHT_GREEN  := $(shell tput -T screen setaf 10)
DIM_GREEN     := $(shell tput -T screen setaf 2)
RESET         := $(shell tput -T screen sgr0)

# Configurable Constants
SYS_PYTHON := python3
VENV_DIR   := .venv

PYTHON     := $(VENV_DIR)/bin/python
PIP        := $(PYTHON) -m pip

BUILD_DIR  := dist
SRC_DIR    := matrix_sdk
TEST_DIR   := tests
DOCS_DIR   := docs
CACHE_DIR  := ~/.cache/matrix-sdk

# Sentinels for the construct
VENV_CREATED   := $(VENV_DIR)/.created
VENV_SENTINEL  := $(VENV_DIR)/.install_sentinel

# Incremental program injection
INSTALL_OPTS ?= -U --upgrade-strategy only-if-needed
BASIC_TOOLS  ?= pip setuptools wheel

# Conditionally include directories that exist for scanning
PY_TARGETS := $(SRC_DIR)
ifneq ("$(wildcard $(TEST_DIR))","")
    PY_TARGETS += $(TEST_DIR)
endif

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Main Directory
# ---------------------------------------------------------------------------
help:
	@echo
	@echo "$(BRIGHT_GREEN)M A T R I X   P Y T H O N   S D K ::: C O N T R O L   P R O G R A M$(RESET)"
	@echo
	@printf "$(BRIGHT_GREEN)  %-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "PROGRAM" "DESCRIPTION"
	@printf "$(BRIGHT_GREEN)  %-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "--------------------" "--------------------------------------------------------"
	@echo
	@echo "$(BRIGHT_GREEN)Core Operations$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "install" "💉 Inject/update programs into the construct (fast)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "lint" "🕶️  Scan for Agents (ruff check)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "fmt" "🥄 Bend the code (auto-format with black & ruff)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "typecheck" "💊 Verify reality constructs (mypy)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "test" "🥋 Enter the Dojo (run simulations with pytest)"
	@echo
	@echo "$(BRIGHT_GREEN)Build & Broadcast$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "build" "🏗️  Construct residual self-image (sdist & wheel)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "wheels" "🧱 Build local constructs for offline testing"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "publish" "📡 Broadcast to Zion's mainframe (PyPI)"
	@echo
	@echo "$(BRIGHT_GREEN)Architect's Archives$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "docs-serve" "📜 Access records live (serve docs at localhost:8000)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "docs-build" "📑 Compile the Architect's records (build static site)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "docs-clean" "🧹 Purge compiled records"
	@echo
	@echo "$(BRIGHT_GREEN)System Maintenance$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "clean" "🔌 Unplug from the Matrix (remove all generated files)"
	@printf "  $(BRIGHT_GREEN)%-20s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "help" "🐇 Follow the white rabbit (show this message)"
	@echo

# ---------------------------------------------------------------------------
# Environment Construction
# ---------------------------------------------------------------------------

# 1) Ensure venv exists and core tools are up to date
$(VENV_CREATED):
	@echo "$(DIM_GREEN)-> Initializing virtual construct in $(VENV_DIR)...$(RESET)"
	@test -f $(PYTHON) || $(SYS_PYTHON) -m venv $(VENV_DIR)
	@echo "$(DIM_GREEN)-> Upgrading core tools ($(BASIC_TOOLS))...$(RESET)"
	@$(PIP) install -U $(BASIC_TOOLS) > /dev/null
	@touch $@

# 2) Heavy dependency sync ONLY when pyproject.toml changes
$(VENV_SENTINEL): pyproject.toml | $(VENV_CREATED)
	@echo "$(DIM_GREEN)-> Construct reality has changed. Syncing all programs...$(RESET)"
	@$(PIP) install $(INSTALL_OPTS) -e ".[dev]"
	@touch $@

# 3) User-facing: fast, incremental install/update
install: $(VENV_CREATED)
	@echo "$(DIM_GREEN)-> Injecting program updates (incremental)...$(RESET)"
	@$(PIP) install $(INSTALL_OPTS) -e ".[dev]"
	@# Touch the sentinel so other targets don't re-trigger a heavy sync
	@touch $(VENV_SENTINEL)
	@echo "$(BRIGHT_GREEN)Construct is up to date.$(RESET)"

# ---------------------------------------------------------------------------
# Quality Control Unit
# ---------------------------------------------------------------------------
lint: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Scanning for Agents (ruff)...$(RESET)"
	@$(PYTHON) -m ruff check $(PY_TARGETS)

fmt: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Bending the code with black...$(RESET)"
	@$(PYTHON) -m black $(PY_TARGETS) $(wildcard .github)
	@echo "$(DIM_GREEN)-> Re-aligning constructs with ruff...$(RESET)"
	@$(PYTHON) -m ruff check --fix $(PY_TARGETS)

typecheck: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Verifying reality constructs (mypy)...$(RESET)"
	@$(PYTHON) -m mypy $(SRC_DIR)

# ---------------------------------------------------------------------------
# Simulation & Training
# ---------------------------------------------------------------------------
test: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Entering the Dojo... initiating simulations...$(RESET)"
	@$(PYTHON) -m pytest -q --disable-warnings --maxfail=1

# ---------------------------------------------------------------------------
# Build & Broadcast
# ---------------------------------------------------------------------------
build: clean $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Compiling residual self-image (sdist & wheel)...$(RESET)"
	@$(PYTHON) -m build --sdist --wheel

publish: build
	@echo "$(DIM_GREEN)-> Broadcasting to Zion's mainframe (PyPI)...$(RESET)"
	@$(PYTHON) -m twine upload $(BUILD_DIR)/*

# Local wheelhouse builder (no-PyPI testing)
.PHONY: wheels build-wheels
wheels build-wheels: $(VENV_CREATED)
	@echo "$(DIM_GREEN)-> Building local constructs into wheelhouse/ via scripts/build_wheels.sh$(RESET)"
	@chmod +x scripts/build_wheels.sh
	@env PATH="$(VENV_DIR)/bin:$$PATH" ./scripts/build_wheels.sh

# ---------------------------------------------------------------------------
# Architect's Archives (MkDocs)
# ---------------------------------------------------------------------------
docs-serve: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Accessing Architect's records at http://127.0.0.1:8000...$(RESET)"
	@$(PYTHON) -m mkdocs serve

docs-build: $(VENV_SENTINEL)
	@echo "$(DIM_GREEN)-> Compiling Architect's records...$(RESET)"
	@$(PYTHON) -m mkdocs build

docs-clean:
	@echo "$(DIM_GREEN)-> Purging compiled records...$(RESET)"
	@rm -rf site/

# ---------------------------------------------------------------------------
# System Purge
# ---------------------------------------------------------------------------
clean:
	@echo "$(DIM_GREEN)-> Unplugging from the Matrix... purging all constructs...$(RESET)"
	@rm -rf $(VENV_DIR)
	@rm -rf $(BUILD_DIR) *.egg-info
	@rm -rf site/
	@rm -rf $(CACHE_DIR)
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name "__pycache__" -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Phony targets
# ---------------------------------------------------------------------------
.PHONY: help install lint fmt typecheck test build publish docs-serve docs-build docs-clean clean