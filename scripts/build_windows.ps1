param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $SkipInstall) {
    python -m pip install --upgrade pip
    python -m pip install -r requirements-build.txt
}

$Commit = "local"
$Branch = "local"

try {
    $GitCommit = git rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $GitCommit) {
        $Commit = $GitCommit.Trim()
    }
} catch {
}

try {
    $GitBranch = git rev-parse --abbrev-ref HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $GitBranch) {
        $Branch = $GitBranch.Trim()
    }
} catch {
}

$BuildTime = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

@"
APP_COMMIT = "$Commit"
APP_BRANCH = "$Branch"
APP_BUILD_TIME = "$BuildTime"
"@ | Set-Content -Path "build_info.py" -Encoding UTF8

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name FootLive `
    --icon "foot-live.ico" `
    --add-data "foot-live.png;." `
    "foot_scores.py"

Write-Host "Built dist\FootLive.exe"
