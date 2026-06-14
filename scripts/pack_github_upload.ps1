# Pack files for GitHub web upload (excludes .venv, .env, local outputs)
# Usage: .\scripts\pack_github_upload.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Out = Join-Path $Root "github-upload"
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Path $Out | Out-Null

$copyItems = @(
    ".github",
    "deep_value_funnel",
    "docs",
    "scripts",
    "strategies",
    ".env.example",
    ".gitignore",
    "daily_push_serverchan.py",
    "DEPLOY_ALIYUN.md",
    "requirements.txt",
    "run_pipeline.py",
    "serverchan_push.py",
    "single_stock_scoring.py",
    "visualize_result.py"
)

foreach ($item in $copyItems) {
    $src = Join-Path $Root $item
    if (-not (Test-Path $src)) {
        Write-Warning "Skip missing: $item"
        continue
    }
    $dest = Join-Path $Out $item
    Copy-Item -Path $src -Destination $dest -Recurse -Force
}

Get-ChildItem -Path $Out -Recurse -Filter "*.code-workspace" | Remove-Item -Force

Write-Host ""
Write-Host "Ready: $Out"
Write-Host "Drag all contents into GitHub Upload files page."
