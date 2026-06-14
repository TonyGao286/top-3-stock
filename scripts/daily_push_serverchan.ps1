# Windows 计划任务用：每日推送关键指标到微信（Server酱）
# 在「任务计划程序」中新建任务，触发器选「每天」，操作选「启动程序」：
#   程序：powershell.exe
#   参数：-NoProfile -ExecutionPolicy Bypass -File "c:\单只股票分析\scripts\daily_push_serverchan.ps1"

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

& $Python daily_push_serverchan.py
exit $LASTEXITCODE
