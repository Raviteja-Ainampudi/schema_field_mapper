# Create or refresh the project venv on Windows.
# A Linux/WSL .venv cannot be reused here — recreate when switching OS.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$VenvDir = if ($env:VENV_DIR) { $env:VENV_DIR } else { ".venv" }

function Find-Python312 {
    $candidates = @("py -3.12", "py -3", "python3.12", "python")
    foreach ($candidate in $candidates) {
        $parts = $candidate -split " "
        $exe = $parts[0]
        $args = @()
        if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $version = & $exe @args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if (-not $version) { continue }
        $minor = [int]($version.Split(".")[1])
        if ($minor -ge 12) {
            return @{ Exe = $exe; Args = $args }
        }
    }
    return $null
}

$python = Find-Python312
if (-not $python) {
    Write-Error @"
Python 3.12+ not found.

Install from https://www.python.org/downloads/ (check "Add python.exe to PATH"),
or use the Microsoft Store Python 3.12 package, then re-run:

  .\scripts\setup_venv.ps1
"@
}

$pyLabel = & $python.Exe @($python.Args) --version
Write-Host "Using $pyLabel ($($python.Exe) $($python.Args -join ' '))"

if (Test-Path $VenvDir) {
    $cfg = Join-Path $VenvDir "pyvenv.cfg"
    $linuxBin = Join-Path $VenvDir "bin/python"
    $winPy = Join-Path $VenvDir "Scripts/python.exe"
    if ((Test-Path $linuxBin) -and -not (Test-Path $winPy)) {
        Write-Host "Removing Linux/WSL .venv (not usable on Windows)..."
        Remove-Item -Recurse -Force $VenvDir
    } elseif (Test-Path $winPy) {
        Write-Host "Refreshing existing Windows venv..."
        Remove-Item -Recurse -Force $VenvDir
    } else {
        Write-Host "Removing incomplete .venv..."
        Remove-Item -Recurse -Force $VenvDir
    }
}

& $python.Exe @($python.Args) -m venv $VenvDir
& (Join-Path $VenvDir "Scripts/python.exe") -m pip install --upgrade pip
& (Join-Path $VenvDir "Scripts/pip.exe") install -r requirements-dev.txt

Write-Host ""
Write-Host "Done. Activate with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
