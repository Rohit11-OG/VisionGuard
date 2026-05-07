# VisionGuard — one-line installer for Windows (PowerShell)
# Usage: irm https://raw.githubusercontent.com/Rohit11-OG/VisionGuard/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
$REPO = "git+https://github.com/Rohit11-OG/VisionGuard.git"

Write-Host "[visionguard] Installing from GitHub..."

$py = if (Get-Command python -ErrorAction SilentlyContinue) { "python" }
      elseif (Get-Command py -ErrorAction SilentlyContinue) { "py -3" }
      else { throw "Python 3.9+ not found in PATH. Install it from python.org and rerun." }

Write-Host "[visionguard] Using: $py"

try {
    Invoke-Expression "$py -m pip install --upgrade `"visionguard[full] @ $REPO`""
} catch {
    Write-Host "[visionguard] Retrying without extras..."
    Invoke-Expression "$py -m pip install `"$REPO`""
}

Write-Host ""
Write-Host "[visionguard] Install complete."
Write-Host ""
Write-Host "  Usage (run from your CV project directory):"
Write-Host "    visionguard bootstrap   # one-time setup"
Write-Host "    visionguard scan        # scan for bugs now"
Write-Host "    visionguard watch       # auto-scan on every file save"
Write-Host "    visionguard report      # list recent reports"
