# Install the `t4` command. Windows PowerShell.
#
#   .\install.ps1
#   .\install.ps1 -Prefix "$HOME\bin"
param([string]$Prefix = "$HOME\.local\bin")

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

$py = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        & $candidate -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
    }
}
if (-not $py) { throw "Python 3.8+ is required but was not found. Install it from python.org or the Microsoft Store." }

Write-Host "Using $(& $py --version)"
& $py -m venv "$Here\.venv"
$VenvPy = "$Here\.venv\Scripts\python.exe"

# Editable installs from pyproject.toml need pip >= 21.3 (PEP 660). Try to
# upgrade, but carry on without a network -- a plain install works regardless.
& $VenvPy -m pip install --quiet --upgrade pip setuptools 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "note: could not upgrade pip (offline?); continuing" }

& $VenvPy -m pip install --quiet -e "$Here" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "note: editable install unavailable; installing a normal copy."
    Write-Host "      re-run .\install.ps1 after pulling changes."
    & $VenvPy -m pip install --quiet "$Here"
}

New-Item -ItemType Directory -Force -Path $Prefix | Out-Null

# A shim rather than a symlink: symlinks on Windows need elevation or
# developer mode, a .cmd shim always works.
$shim = Join-Path $Prefix "t4.cmd"
Set-Content -Path $shim -Encoding ASCII -Value @"
@echo off
"$Here\.venv\Scripts\t4.exe" %*
"@

Write-Host ""
Write-Host "Installed: $shim"
$onPath = ($env:Path -split ';') -contains $Prefix
if (-not $onPath) {
    Write-Host ""
    Write-Host "$Prefix is not on your PATH. Add it for your user with:"
    Write-Host "  [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$Prefix`", 'User')"
    Write-Host "Then open a new terminal."
}
Write-Host ""
Write-Host "Then, in your project directory:"
Write-Host "  t4 init"
