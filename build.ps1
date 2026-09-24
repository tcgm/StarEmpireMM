[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string[]]$EmbeddedLoaderPackages = @()
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$pyVersion = & py -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "Building with: py -> Python $pyVersion ($((& py -c 'import sys; print(sys.executable)')))"

foreach ($package in @("pyinstaller", "tkinterdnd2", "cryptography")) {
    cmd /c "py -m pip show $package >nul 2>nul"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "$package not found; installing it..."
        py -m pip install $package
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
