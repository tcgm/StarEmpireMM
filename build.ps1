[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string[]]$EmbeddedLoaderPackages = @()
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

$scriptArgs = @{}
if (-not [string]::IsNullOrWhiteSpace($OutputRoot)) {
    $scriptArgs["OutputRoot"] = $OutputRoot
}
if ($EmbeddedLoaderPackages.Count -gt 0) {
    $scriptArgs["EmbeddedLoaderPackages"] = $EmbeddedLoaderPackages
}

& (Join-Path $root "tools\build_manager.ps1") @scriptArgs
exit $LASTEXITCODE
