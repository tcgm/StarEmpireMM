[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string[]]$EmbeddedLoaderPackages = @()
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

foreach ($package in @("pyinstaller", "tkinterdnd2")) {
    cmd /c "python -m pip show $package >nul 2>nul"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$package not found; installing it..."
        python -m pip install $package
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install $package"
        }
    }
}

$scriptArgs = @{}
if (-not [string]::IsNullOrWhiteSpace($OutputRoot)) {
    $scriptArgs["OutputRoot"] = $OutputRoot
}
if ($EmbeddedLoaderPackages.Count -gt 0) {
    $scriptArgs["EmbeddedLoaderPackages"] = $EmbeddedLoaderPackages
}

& (Join-Path $root "tools\build_manager.ps1") @scriptArgs
exit $LASTEXITCODE
