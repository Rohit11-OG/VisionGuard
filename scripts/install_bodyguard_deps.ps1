param(
    [switch]$IncludeBasedPyright = $true
)

$ErrorActionPreference = "Stop"

function Write-Section([string]$Message) {
    Write-Host ""
    Write-Host "== $Message =="
}

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py -3"
    }
    throw "Python was not found in PATH. Install Python 3.9+ and rerun."
}

function Invoke-Checked([string]$Cmd, [string]$Label) {
    Write-Host "[run] $Label"
    Invoke-Expression $Cmd
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Command-Exists([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$py = Get-PythonCommand

Write-Section "Bodyguard dependency installer"
Write-Host "Python command: $py"

Write-Section "Upgrade pip tooling"
Invoke-Checked "$py -m pip install --upgrade pip setuptools wheel" "Upgrade pip/setuptools/wheel"

Write-Section "Install Python optional dependencies"
$pyPackages = @(
    "watchfiles",
    "watchdog",
    "libcst",
    "ruff",
    "semgrep",
    "opentelemetry-api",
    "opentelemetry-sdk"
)
Invoke-Checked "$py -m pip install $($pyPackages -join ' ')" "Install watch/fix/scan/telemetry Python packages"

if ($IncludeBasedPyright) {
    Write-Section "Install BasedPyright"
    if (Command-Exists "basedpyright") {
        Write-Host "[ok] basedpyright already available."
    }
    else {
        $installed = $false
        if (Command-Exists "npm") {
            try {
                Invoke-Checked "npm install -g basedpyright" "Install basedpyright via npm"
                $installed = $true
            }
            catch {
                Write-Host "[warn] npm install for basedpyright failed."
            }
        }
        if (-not $installed) {
            try {
                Invoke-Checked "$py -m pip install basedpyright" "Install basedpyright via pip"
                $installed = $true
            }
            catch {
                Write-Host "[warn] pip install for basedpyright failed."
            }
        }
        if (-not $installed) {
            Write-Host "[warn] basedpyright could not be installed automatically. Optional check will be skipped."
        }
    }
}

Write-Section "Verify key commands"
$verify = @(
    @{ Name = "ruff"; Cmd = "ruff --version" },
    @{ Name = "semgrep"; Cmd = "semgrep --version" },
    @{ Name = "watchfiles"; Cmd = "python -c `"import watchfiles`"" },
    @{ Name = "libcst"; Cmd = "python -c `"import libcst`"" }
)

foreach ($item in $verify) {
    try {
        Write-Host "[check] $($item.Name)"
        Invoke-Expression $item.Cmd | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[ok] $($item.Name) available."
        }
        else {
            Write-Host "[warn] $($item.Name) check returned non-zero exit code."
        }
    }
    catch {
        Write-Host "[warn] $($item.Name) check failed."
    }
}

if (Command-Exists "basedpyright") {
    Write-Host "[ok] basedpyright available."
}
else {
    Write-Host "[warn] basedpyright not found (optional)."
}

Write-Section "Done"
Write-Host "Run: powershell -ExecutionPolicy Bypass -File scripts/install_bodyguard_deps.ps1"
Write-Host "Tip: run in a virtual environment to avoid dependency conflicts with other Python projects."
