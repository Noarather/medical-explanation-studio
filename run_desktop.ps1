$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Set-Location $PSScriptRoot

$created = $false
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "无法创建 Python 环境" }
    $created = $true
}

$python = ".\.venv\Scripts\python.exe"

if (-not $created) {
    & $python -c "import yaml, fitz, numpy, psutil, openpyxl, docx, keyring, openai, dashscope, PySide6, docling, rapidocr, onnxruntime, instructor, httpx" 2>$null
    $needsInstall = $LASTEXITCODE -ne 0
} else {
    $needsInstall = $true
}

if ($needsInstall) {
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "解析依赖安装失败，请检查 Python 版本与网络后重新启动" }
}

& $python -c "from PySide6.QtCore import qVersion" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在修复 Qt 运行环境..."
    & $python -m pip install --disable-pip-version-check --force-reinstall "PySide6==6.9.2"
    if ($LASTEXITCODE -ne 0) { throw "Qt 运行环境修复失败" }
}

& $python desktop_app.py
