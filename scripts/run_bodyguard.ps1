param(
    [ValidateSet("init", "scan", "watch", "report")]
    [string]$Command = "watch",
    [int]$Latest = 5
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py -3"
    }
    throw "Python was not found in PATH."
}

$py = Get-PythonCommand

if ($Command -eq "report") {
    Invoke-Expression "$py `".\bug_bodyguard.py`" report --latest $Latest"
}
else {
    Invoke-Expression "$py `".\bug_bodyguard.py`" $Command"
}
