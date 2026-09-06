# gxlib-cnki 一键安装（Windows PowerShell）
# 用法：powershell -ExecutionPolicy Bypass -File install.ps1
#      国内网络加速：powershell -ExecutionPolicy Bypass -File install.ps1 -Mirror
param(
  [switch]$Mirror
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/5] 检查 Python (需 3.10-3.12)..."
python --version
if ($LASTEXITCODE -ne 0) { Write-Host "未找到 python，请先安装 Python 3.10-3.12"; exit 1 }

Write-Host "[2/5] 创建虚拟环境..."
if (-not (Test-Path ".venv")) { python -m venv .venv }
.\.venv\Scripts\Activate.ps1

Write-Host "[3/5] 安装依赖..."
if ($Mirror) {
  pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
} else {
  pip install -r requirements.txt
}

Write-Host "[4/5] 安装 Playwright 浏览器内核..."
if ($Mirror) { $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright/" }
python -m playwright install chromium

Write-Host "[5/5] 生成 config.json..."
if (-not (Test-Path "config.json")) { Copy-Item "config.example.json" "config.json" }

Write-Host ""
Write-Host "=== 安装完成 ==="
Write-Host "  1) 编辑 config.json 填入图书馆读者证号/密码"
Write-Host "  2) .\.venv\Scripts\Activate.ps1"
Write-Host "  3) python cnki_fulltext.py browser-trust"