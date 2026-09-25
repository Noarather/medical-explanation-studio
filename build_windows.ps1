param([switch]$ReuseDependencies, [switch]$SkipLocalInstall)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot

$Version = "2.2.0"
$BuildVenv = Join-Path $PSScriptRoot ".venv-build"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BuildId = "$Version-$Stamp"
$ReleaseDir = Join-Path $PSScriptRoot "release\MedExplainStudio-$BuildId"
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
New-Item -ItemType Directory -Path "$ReleaseDir\checks","$ReleaseDir\logs","$ReleaseDir\installer" | Out-Null
$env:PYTHONUTF8 = "1"
$OriginalDatabaseOverride = $env:MEDEXPLAIN_DATABASE_PATH
$env:QT_QPA_PLATFORM = "offscreen"
$env:MEDEXPLAIN_DATABASE_PATH = "$ReleaseDir\checks\isolated.db"
$env:MEDEXPLAIN_BUILD_INFO = "$ReleaseDir\_build-info.json"
$Revision = & git rev-parse --short HEAD
@{version=$Version;buildId=$BuildId;builtAt=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K');revision=$Revision} |
    ConvertTo-Json | Set-Content -LiteralPath $env:MEDEXPLAIN_BUILD_INFO -Encoding UTF8

function Assert-ExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}

function Invoke-Python([string[]]$Arguments, [string]$Step) {
    & $BuildPython @Arguments
    Assert-ExitCode $Step
}

Write-Host "[1/9] Create isolated build environment"
if (-not (Test-Path $BuildPython)) {
    $PyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if (-not $PyLauncher) { throw "Python Launcher (py.exe) with Python 3.13 is required" }
    & $PyLauncher.Source -3.13 -m venv $BuildVenv
    Assert-ExitCode "Create .venv-build"
}
$BuildRuntime = & $BuildPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($BuildRuntime.Trim() -ne "3.13") { throw "Python 3.13 is required for release builds; got $BuildRuntime" }

Write-Host "[2/9] Install pinned dependencies"
if (-not $ReuseDependencies) {
    Invoke-Python @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt") "Install requirements"
}
Invoke-Python @("-m", "pip", "check") "Dependency consistency"
Invoke-Python @("-m", "pip", "freeze", "--all") "Capture dependencies" | Set-Content -LiteralPath "$ReleaseDir\DEPENDENCIES.txt"
$QtVersion = & $BuildPython -c "from PySide6.QtCore import qVersion; print(qVersion())"
Assert-ExitCode "Read Qt version"
if ($QtVersion.Trim() -ne "6.9.2") { throw "Expected PySide6/Qt 6.9.2, got $QtVersion" }

Write-Host "[3/9] Validate config and generate icon"
$secretPattern = '(sk-[A-Za-z0-9]{12,}|api[_-]?key\s*:\s*[A-Za-z0-9]{12,})'
if (Select-String -Path "config.yaml" -Pattern $secretPattern -CaseSensitive:$false -Quiet) {
    throw "config.yaml may contain a real credential"
}
$env:QT_QPA_PLATFORM = "offscreen"
if (-not (Test-Path "assets\app_icon.ico")) { Invoke-Python @("tools\generate_icon.py") "Generate Windows icon" }
if (-not (Test-Path "assets\app_icon.ico")) { throw "Missing assets\app_icon.ico" }

Write-Host "== 前端构建 =="
Push-Location frontend
if (-not $ReuseDependencies) {
    npm ci
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm ci failed" }
}
npm test -- --reporter=json "--outputFile=$ReleaseDir\checks\frontend.json"
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "frontend tests failed" }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "frontend build failed" }
Pop-Location
if (-not (Test-Path "frontend/dist/index.html")) { throw "frontend/dist/index.html missing" }

Write-Host "[4/9] Run complete test suite"
Invoke-Python @("-m", "pytest", "-q", "-o", "cache_dir=$ReleaseDir/checks/cache", "--basetemp=$ReleaseDir/checks/pytest", "--junitxml=$ReleaseDir/checks/python.xml") "Complete tests"
Invoke-Python @("tools\release_artifacts.py", "snapshot", "--release", $ReleaseDir) "Snapshot actual source"

Write-Host "[5/9] Build PyInstaller one-directory bundle"
Invoke-Python @("-m", "PyInstaller", "med_explain.spec", "--noconfirm", "--distpath", "$ReleaseDir\portable", "--workpath", "$ReleaseDir\build") "PyInstaller build"
$DistRoot = Join-Path $ReleaseDir "portable\MedExplainStudio"
$ExePath = Join-Path $DistRoot "MedExplainStudio.exe"
if (-not (Test-Path $ExePath)) { throw "Missing executable: $ExePath" }

Write-Host "[6/9] Audit bundled runtime dependencies"
$allFiles = Get-ChildItem -LiteralPath $DistRoot -Recurse -File
$allDirs = Get-ChildItem -LiteralPath $DistRoot -Recurse -Directory
$checks = @{
    "Qt platforms" = [bool]($allDirs | Where-Object { $_.Name -eq "platforms" })
    "Qt styles" = [bool]($allDirs | Where-Object { $_.Name -eq "styles" })
    "PyMuPDF" = [bool]($allFiles | Where-Object { $_.Name -match '(_fitz|mupdf|pymupdf)' })
    "jieba dictionary" = [bool]($allFiles | Where-Object { $_.Name -eq "dict.txt" -and $_.FullName -match 'jieba' })
    "python-docx data" = [bool]($allDirs | Where-Object { $_.Name -eq "docx" })
}
if ($allDirs | Where-Object { $_.Name -in 'docling','torch','rapidocr','onnxruntime','transformers' }) { throw "Local AI runtime unexpectedly bundled" }
if ($allFiles | Where-Object { $_.Extension -in '.onnx','.safetensors','.pt' }) { throw "Local AI weights unexpectedly bundled" }
foreach ($entry in $checks.GetEnumerator()) {
    if (-not $entry.Value) { throw "Bundle audit failed: $($entry.Key)" }
    Write-Host "  OK  $($entry.Key)"
}
$webengineChecks = @(
    "$DistRoot\_internal\PySide6\Qt6WebEngineCore.dll",
    "$DistRoot\_internal\PySide6\QtWebEngineProcess.exe",
    "$DistRoot\_internal\frontend\dist\index.html"
)
foreach ($item in $webengineChecks) {
    if (-not (Test-Path $item)) { throw "bundle audit failed: $item missing" }
    Write-Host "  OK  $item"
}
$env:QT_QPA_PLATFORM = "offscreen"
$ParserProbeReport = Join-Path $ReleaseDir "checks\parser.json"
New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($ParserProbeReport)) -Force | Out-Null
$ParserProbeArguments = @("--bundle-check", "--parser-smoke-report", ('"' + $ParserProbeReport + '"'))
$probe = Start-Process -FilePath $ExePath -ArgumentList $ParserProbeArguments -Wait -PassThru -WindowStyle Hidden
if ($probe.ExitCode -ne 0) { throw "Bundled Python import probe failed with exit code $($probe.ExitCode)" }
$ParserProbeResult = Get-Content -LiteralPath $ParserProbeReport -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $ParserProbeResult.ok -or -not $ParserProbeResult.utf8_mode -or -not $ParserProbeResult.unicode_path) { throw "Bundled PDF/OCR, Unicode path or UTF-8 probe failed" }
Write-Host "  OK  bundled imports, synthetic PDF extraction, OCR and UTF-8"
$UiReport = Join-Path $ReleaseDir "checks\ui.json"
$UiProbe = Start-Process -FilePath $ExePath -ArgumentList @('--ui-smoke-report', ('"' + $UiReport + '"')) -Wait -PassThru -WindowStyle Hidden
if ($UiProbe.ExitCode -ne 0) { throw "Packaged UI probe failed" }
$ScrollReport = Join-Path $ReleaseDir "checks\ui-scroll.json"
$ScrollProbe = Start-Process -FilePath $ExePath -ArgumentList @('--ui-scroll-smoke-report', ('"' + $ScrollReport + '"')) -Wait -PassThru -WindowStyle Hidden
if ($ScrollProbe.ExitCode -ne 0) { throw "Packaged native scroll probe failed" }

Write-Host "[7/9] Optional code signing"
$Signed = $false
if ($env:SIGN_CERT_PATH -and (Test-Path $env:SIGN_CERT_PATH)) {
    $signtool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if (-not $signtool) { throw "SIGN_CERT_PATH is set but signtool.exe is unavailable" }
    & $signtool.Source sign /fd SHA256 /f $env:SIGN_CERT_PATH /p $env:SIGN_CERT_PASSWORD /tr "http://timestamp.digicert.com" /td SHA256 $ExePath
    Assert-ExitCode "Sign executable"
    $Signed = $true
} else {
    Write-Host "  No SIGN_CERT_PATH supplied; signing skipped"
}

# User requirement: keep the lightweight split installer; see docs/PACKAGING.md.
$InstallerConfig = Get-Content -LiteralPath "installer.iss" -Raw
if ($InstallerConfig -notmatch '(?im)^\s*DiskSpanning\s*=\s*yes\s*(?:;.*)?$') {
    throw "Packaging policy requires DiskSpanning=yes (small EXE + external BIN)"
}
Write-Host "[8/9] Build Inno Setup installer"
$iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
$isccPath = if ($iscc) { $iscc.Source } else { "" }
if (-not $isccPath) {
    $knownIscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($knownIscc) { $isccPath = $knownIscc }
}
if (-not $isccPath) { throw "Inno Setup 6 ISCC.exe was not found" }
# Inno Setup cannot read some long model-cache/license paths. Compile via a short junction.
$InstallerSourceDir = (Resolve-Path -LiteralPath $DistRoot).Path
$InstallerSourceAlias = Join-Path ([System.IO.Path]::GetTempPath()) ("mep-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$LongestInstallerPath = ($allFiles | ForEach-Object {
    $InstallerSourceAlias.Length + $_.FullName.Substring($InstallerSourceDir.Length).Length
} | Measure-Object -Maximum).Maximum
if ($LongestInstallerPath -ge 250) { throw "Installer source paths are too long even through the temporary junction" }
$InstallerWorkDir = Join-Path $PSScriptRoot "work"
New-Item -ItemType Directory -Path $InstallerWorkDir -Force | Out-Null
$PreparedInstallerScript = Join-Path $InstallerWorkDir "installer-$Stamp.iss"
$PreparedInstallerConfig = $InstallerConfig.Replace('Source: "dist\MedExplainStudio\*"', ('Source: "' + $InstallerSourceAlias + '\*"'))
$PreparedInstallerConfig = $PreparedInstallerConfig.Replace('SetupIconFile=assets\app_icon.ico', ('SetupIconFile=' + (Join-Path $PSScriptRoot 'assets\app_icon.ico')))
if (-not $PreparedInstallerConfig.Contains('Source: "' + $InstallerSourceAlias + '\*"')) { throw "Could not prepare short installer source path" }
$PreparedInstallerConfig = $PreparedInstallerConfig.Replace('OutputBaseFilename=MedExplainStudio-Setup', "OutputBaseFilename=MedExplainStudio-Setup-$BuildId")
$PreparedInstallerConfig | Set-Content -LiteralPath $PreparedInstallerScript -Encoding UTF8
New-Item -ItemType Junction -Path $InstallerSourceAlias -Value $InstallerSourceDir | Out-Null
try {
    & $isccPath ("/O" + (Join-Path $ReleaseDir "installer")) $PreparedInstallerScript
    Assert-ExitCode "Inno Setup build"
} finally {
    # Remove only the junction owned by this build. Never recursively delete its target.
    $InstallerJunction = Get-Item -LiteralPath $InstallerSourceAlias -Force -ErrorAction SilentlyContinue
    if ($InstallerJunction) {
        if ($InstallerJunction.LinkType -ne 'Junction' -or [System.IO.Path]::GetFullPath(@($InstallerJunction.Target)[0]) -ne $InstallerSourceDir) {
            throw "Unexpected installer junction target; automatic cleanup stopped"
        }
        [System.IO.Directory]::Delete($InstallerSourceAlias)
    }
}
$Installer = Join-Path $ReleaseDir "installer\MedExplainStudio-Setup-$BuildId.exe"
$InstallerBin = Join-Path $ReleaseDir "installer\MedExplainStudio-Setup-$BuildId-1.bin"
if (-not (Test-Path $Installer)) { throw "Missing installer: $Installer" }
if (-not (Test-Path $InstallerBin)) { throw "Missing installer data slice: $InstallerBin" }
if ((Get-Item -LiteralPath $Installer).Length -gt 16MB) {
    throw "Installer launcher exceeds 16 MiB; verify split packaging before release"
}
if ($Signed) {
    $signtool = Get-Command "signtool.exe"
    & $signtool.Source sign /fd SHA256 /f $env:SIGN_CERT_PATH /p $env:SIGN_CERT_PASSWORD /tr "http://timestamp.digicert.com" /td SHA256 $Installer
    Assert-ExitCode "Sign installer"
}

Write-Host "[9/9] Generate verified release metadata"
$RecordArgs = @("tools\release_artifacts.py", "record", "--release", $ReleaseDir)
if ($Signed) { $RecordArgs += "--signed" }
Invoke-Python $RecordArgs "Record verified release"
"版本 $Version，构建 $BuildId。请解压并保持 EXE 与全部 BIN 同目录。安装前正常退出旧版；原位升级保留业务数据。" |
    Set-Content -LiteralPath "$ReleaseDir\installer\安装说明.txt" -Encoding UTF8
Compress-Archive -Path "$ReleaseDir\installer\*" -DestinationPath "$ReleaseDir\MedExplainStudio-$BuildId.zip"
Get-FileHash -LiteralPath "$ReleaseDir\MedExplainStudio-$BuildId.zip" -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash)  MedExplainStudio-$BuildId.zip" } | Set-Content -LiteralPath "$ReleaseDir\ZIP-SHA256.txt" -Encoding ASCII
Write-Host "Verified release: $ReleaseDir"
if (-not $SkipLocalInstall) {
    $env:MEDEXPLAIN_DATABASE_PATH = $OriginalDatabaseOverride
    & "$PSScriptRoot\deploy_local.ps1" -ReleaseDirectory $ReleaseDir
} else { Write-Host "Local installation pending; run deploy_local.ps1 before declaring delivery complete." }
