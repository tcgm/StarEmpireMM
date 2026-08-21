[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string[]]$EmbeddedLoaderPackages = @()
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$managerRoot = (Resolve-Path (Join-Path $repository "..")).Path
$stamp = Get-Date -Format "yyyyMMdd.HHmmss"
$versionSource = Get-Content -LiteralPath (Join-Path $repository "installer\version.py") -Raw
$versionMatch = [regex]::Match($versionSource, 'MANAGER_VERSION\s*=\s*"([0-9.]+)"')
if (-not $versionMatch.Success) {
    throw "Could not read the Manager version from installer\version.py"
}
$managerVersion = $versionMatch.Groups[1].Value
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $managerRoot ("builds\Manager." + $stamp + ".V" + $managerVersion)
}
$output = [System.IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $output) {
    throw "Refusing to reuse Manager build directory: $output"
}

$dist = Join-Path $output "dist"
$work = Join-Path $output "work"
New-Item -ItemType Directory -Path $dist, $work -Force | Out-Null

$spec = Join-Path $repository "installer\StarEmpireUiModManager.spec"
$embedded = @()
$embeddedNames = @{}
foreach ($source in $EmbeddedLoaderPackages) {
    $resolved = (Resolve-Path -LiteralPath $source).Path
    if ([System.IO.Path]::GetExtension($resolved) -ine ".seloader") {
        throw "Embedded compatibility input must be a .seloader file: $resolved"
    }
    $name = [System.IO.Path]::GetFileName($resolved).ToLowerInvariant()
    if ($embeddedNames.ContainsKey($name)) {
        throw "Duplicate embedded compatibility filename: $name"
    }
    $embeddedNames[$name] = $true
    $embedded += $resolved
}
$environmentName = "STAR_EMPIRE_MANAGER_EMBEDDED_LOADERS"
$previousEnvironment = [Environment]::GetEnvironmentVariable($environmentName, "Process")
try {
    [Environment]::SetEnvironmentVariable(
        $environmentName,
        ($embedded -join [System.IO.Path]::PathSeparator),
        "Process")
    & python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller Manager build failed with exit code $LASTEXITCODE"
    }
}
finally {
    [Environment]::SetEnvironmentVariable(
        $environmentName, $previousEnvironment, "Process")
}

$manager = Join-Path $dist "StarEmpireModManager.exe"
if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) {
    throw "Manager build did not produce $manager"
}
$smoke = Start-Process -FilePath $manager -ArgumentList "--self-test" -Wait -PassThru -WindowStyle Hidden
if ($smoke.ExitCode -ne 0) {
    throw "Frozen Manager self-test failed with exit code $($smoke.ExitCode)"
}
$header = [System.IO.File]::ReadAllBytes($manager)[0..1]
if ($header[0] -ne 0x4D -or $header[1] -ne 0x5A) {
    throw "Manager output is not a Windows PE executable"
}
$hash = (Get-FileHash -LiteralPath $manager -Algorithm SHA256).Hash
$sourceRevision = $null
$sourceDirty = $null
try {
    $revisionOutput = & git -C $repository rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0) {
        $sourceRevision = ($revisionOutput | Select-Object -First 1).Trim()
        $statusOutput = & git -C $repository status --porcelain 2>$null
        if ($LASTEXITCODE -eq 0) {
            $sourceDirty = -not [string]::IsNullOrWhiteSpace(
                ($statusOutput -join [Environment]::NewLine))
        }
    }
}
catch {
    $sourceRevision = $null
    $sourceDirty = $null
}
$embeddedReleaseEntries = @(
    foreach ($loader in $embedded) {
        [ordered]@{
            file = [System.IO.Path]::GetFileName($loader)
            sha256 = (Get-FileHash -LiteralPath $loader -Algorithm SHA256).Hash
            bytes = (Get-Item -LiteralPath $loader).Length
        }
    }
)
$releaseLedger = [ordered]@{
    schema = 1
    product = "Star Empire Mod Manager"
    manager_version = $managerVersion
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    source_revision = $sourceRevision
    source_dirty = $sourceDirty
    executable = [ordered]@{
        file = [System.IO.Path]::GetFileName($manager)
        sha256 = $hash
        bytes = (Get-Item -LiteralPath $manager).Length
    }
    embedded_loaders = $embeddedReleaseEntries
}
$releaseLedgerPath = Join-Path $dist "RELEASE.json"
$releaseLedger | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $releaseLedgerPath -Encoding utf8
& python -m tools.audit_manager_artifact --manager $manager
if ($LASTEXITCODE -ne 0) {
    throw "Manager artifact audit failed with exit code $LASTEXITCODE"
}
[pscustomobject]@{
    Manager = $manager
    Sha256 = $hash
    Bytes = (Get-Item -LiteralPath $manager).Length
    BuildRoot = $output
    ReleaseLedger = $releaseLedgerPath
    EmbeddedCompatibilityPackages = $embedded.Count
} | Format-List
