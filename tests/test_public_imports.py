# tests/test_public_imports.py
"""
Packaging / public-import regression guard.

A previous release shipped `matrix_sdk/installer` as a *flat* module
(`installer.py`) whose body did `from .installer.core import ...`, so
`matrix_sdk.installer` was not a package and downstream code (matrix-cli)
crashed with:

    ModuleNotFoundError: No module named 'matrix_sdk.installer.core';
    'matrix_sdk.installer' is not a package

These tests assert that the public import surface matrix-cli depends on is
importable. Run as normal pytest (source tree) AND, crucially, against the
*built wheel* in CI (see .github/workflows/build-wheels.yml) so a packaging
regression that drops the `installer` subpackage fails the build instead of
shipping.
"""

from __future__ import annotations

import importlib

import pytest

# Modules that must always be importable from an installed matrix-python-sdk.
REQUIRED_MODULES = [
    "matrix_sdk",
    "matrix_sdk.client",
    "matrix_sdk.alias",
    "matrix_sdk.ids",
    "matrix_sdk.installer",  # must be a PACKAGE, not a flat module
    "matrix_sdk.installer.core",  # the file that went missing in the bad build
]


@pytest.mark.parametrize("module", REQUIRED_MODULES)
def test_module_importable(module: str) -> None:
    importlib.import_module(module)


def test_installer_is_a_package() -> None:
    """`matrix_sdk.installer` must be a package (have submodules), never a
    flat module — otherwise `matrix_sdk.installer.core` cannot resolve."""
    pkg = importlib.import_module("matrix_sdk.installer")
    assert hasattr(pkg, "__path__"), (
        "matrix_sdk.installer is a flat module, not a package — the "
        "installer/ subpackage was dropped from the distribution."
    )


def test_matrix_cli_install_import_surface() -> None:
    """The exact symbols matrix_cli/commands/install.py imports at runtime."""
    import matrix_sdk.installer as installer_pkg
    import matrix_sdk.installer.core as installer_core

    # `from matrix_sdk.installer import LocalInstaller` must resolve to the same
    # object defined in core (the package facade re-exports it).
    assert installer_pkg.LocalInstaller is installer_core.LocalInstaller

    # The Build*/Env* dataclasses must be importable from core.
    for name in ("BuildReport", "BuildResult", "EnvReport", "LocalInstaller"):
        assert isinstance(getattr(installer_core, name), type)
