# 本机打包要上传到阿里云的文件列表（在 PowerShell 中于项目根目录执行）
# 用法：.\scripts\aliyun\pack_upload.ps1
# 然后：scp stock-analysis-upload.zip root@你的ECSIP:/root/

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$Zip = Join-Path $Root "stock-analysis-upload.zip"
if (Test-Path $Zip) { Remove-Item $Zip }

$items = @(
    ".github",
    "run_pipeline.py",
    "single_stock_scoring.py",
    "visualize_result.py",
    "daily_push_serverchan.py",
    "serverchan_push.py",
    "requirements.txt",
    ".env.example",
    "DEPLOY_ALIYUN.md",
    "docs",
    "deep_value_funnel",
    "strategies",
    "scripts"
)

Compress-Archive -Path $items -DestinationPath $Zip -Force
Write-Host "已生成: $Zip"
Write-Host ""
Write-Host "上传到 ECS 后执行:"
Write-Host "  unzip stock-analysis-upload.zip -d ~/stock-analysis"
Write-Host "  cd ~/stock-analysis && bash scripts/aliyun/install.sh"
