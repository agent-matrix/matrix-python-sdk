Param(
  # Optional: override with -PackageDir or set $env:PACKAGE_DIR
  [string]$PackageDir = $(if ($env:PACKAGE_DIR) { $env:PACKAGE_DIR } else { (Get-Location).Path })
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Step  ($m) { Write-Host "▶ $m" -ForegroundColor Blue }
function Info  ($m) { Write-Host "ℹ $m" }
function Warn  ($m) { Write-Host "⚠ $m" -ForegroundColor Yellow }
function Die   ($m) { Write-Host "✖ $m" -ForegroundColor Red; exit 1 }

# Resolve python launcher (prefer PYTHON_EXE env, else python, else py)
$Python = $env:PYTHON_EXE
if (-not $Python) {
  if (Get-Command python -ErrorAction SilentlyContinue) { $Python = "python" }
  elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py" }
  else { Die "Could not find Python on PATH. Install Python 3.11/3.12 and try again." }
}

Set-Location $PackageDir
if (-not (Test-Path "pyproject.toml")) {
  Die "pyproject.toml not found in $PackageDir"
}

Step "Installing/Updating build backend (pip, build, wheel)"
& $Python -m pip install --upgrade pip build wheel | Out-Null

Step "Cleaning old dist/ build/ *.egg-info and wheelhouse/"
Remove-Item -Recurse -Force dist, build, *.egg-info, wheelhouse -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force wheelhouse | Out-Null

Step "Building sdist and wheel"
& $Python -m build

Step "Collecting artifacts into wheelhouse/"
Get-ChildItem dist\*.whl     -ErrorAction SilentlyContinue | Copy-Item -Destination wheelhouse\ -Force
Get-ChildItem dist\*.tar.gz  -ErrorAction SilentlyContinue | Copy-Item -Destination wheelhouse\ -Force

Step "Done — produced:"
Get-ChildItem wheelhouse | ForEach-Object { "  - $($_.Name)" }

@'
Next steps (install from wheelhouse):

  # Create and activate a virtualenv (recommended)
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1

  # Install without touching PyPI (universal wheel for pure-Python)
  python -m pip install --no-index --find-links=wheelhouse matrix-python-sdk

  # Or install a specific built file
  python -m pip install (Get-ChildItem wheelhouse\matrix_python_sdk-*-py3-none-any.whl | Select-Object -First 1).FullName
'@ | Write-Host
