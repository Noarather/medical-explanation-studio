param(
    [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
    [string]$InstallDirectory = "",
    [string]$DatabasePath = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ReleaseDirectory = (Resolve-Path -LiteralPath $ReleaseDirectory).Path
$taskBuild = Get-Content -LiteralPath "$ReleaseDirectory\BUILD.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$taskPython = Join-Path $PSScriptRoot '.venv-build\Scripts\python.exe'
$taskTool = Join-Path $PSScriptRoot 'tools\release_artifacts.py'
$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskReportPath = Join-Path $ReleaseDirectory "DEPLOYMENT-$taskStamp.json"
if (Test-Path -LiteralPath $taskReportPath) { throw 'Deployment report already exists' }
$taskRecord = [ordered]@{status='checking';version=$taskBuild.version;buildId=$taskBuild.buildId;startedAt=(Get-Date -Format o)}
function Save-Deployment { $taskRecord | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $taskReportPath -Encoding UTF8 }
function Check-Exit([string]$Step) { if ($LASTEXITCODE -ne 0) { throw "$Step failed: $LASTEXITCODE" } }
try {
    # Reject redirected sandbox accounts: an installation must target the real host user.
    if ($env:USERNAME -match '^CodexSandbox') { throw 'Run deployment in the actual host user context, not the sandbox account' }
    $taskKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B4EBDA72-14A0-4A67-9A3B-14A09F7F5925}_is1'
    $taskReg = Get-ItemProperty -LiteralPath $taskKey -ErrorAction SilentlyContinue
    $taskShell = New-Object -ComObject WScript.Shell
    $taskShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) '医学题库智能解析.lnk'
    $taskLinkTarget = if (Test-Path -LiteralPath $taskShortcut) { $taskShell.CreateShortcut($taskShortcut).TargetPath } else { '' }
    $taskRegistered = if ($taskReg) { $taskReg.InstallLocation.TrimEnd('\') } else { '' }
    if ($taskRegistered -and $taskLinkTarget -and (Split-Path -Parent $taskLinkTarget) -ne $taskRegistered) {
        throw 'Registry and desktop shortcut disagree; resolve installation target before upgrading'
    }
    if (-not $InstallDirectory) {
        if ($taskRegistered) { $InstallDirectory = $taskRegistered }
        elseif ($taskLinkTarget) { $InstallDirectory = Split-Path -Parent $taskLinkTarget }
        else { $InstallDirectory = Join-Path $env:LOCALAPPDATA 'Programs\MedExplainStudio' }
    }
    $InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
    if ($taskRegistered -and $InstallDirectory -ne $taskRegistered) { throw 'Refusing to install a second copy instead of upgrading the registered application' }
    if ((Split-Path -Leaf $InstallDirectory) -ne 'MedExplainStudio') { throw 'Unexpected installation directory; verify the target first' }
    $taskExe = Join-Path $InstallDirectory 'MedExplainStudio.exe'
    $taskProcesses = @(Get-Process -Name MedExplainStudio -ErrorAction SilentlyContinue)
    if ($taskProcesses.Count) { throw 'MedExplainStudio is running. Save edits and exit normally before installation; no processes were killed.' }
    $taskRecord.installDirectory = $InstallDirectory
    $taskRecord.previousVersion = if (Test-Path -LiteralPath $taskExe) { (Get-Item -LiteralPath $taskExe).VersionInfo.ProductVersion } else { 'not_installed' }
    $taskRecord.previousExeSha256 = if (Test-Path -LiteralPath $taskExe) { (Get-FileHash -LiteralPath $taskExe -Algorithm SHA256).Hash } else { '' }
    if (-not $DatabasePath) {
        $DatabasePath = if ($env:MEDEXPLAIN_DATABASE_PATH) { $env:MEDEXPLAIN_DATABASE_PATH } else { Join-Path $env:LOCALAPPDATA 'MedExplainStudio\med_explain.db' }
    }
    $DatabasePath = [IO.Path]::GetFullPath($DatabasePath)
    if ($DatabasePath.StartsWith($ReleaseDirectory, [StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to back up a packaging test database instead of business data' }
    $taskRecord.databasePath = $DatabasePath
    if (Test-Path -LiteralPath $DatabasePath) {
        $taskBackup = Join-Path (Split-Path -Parent $DatabasePath) "backups\before-$($taskBuild.buildId)-$taskStamp.db"
        $taskBackupJson = & $taskPython -X utf8 $taskTool backup --source $DatabasePath --destination $taskBackup
        Check-Exit 'Consistent database backup'
        $taskRecord.backup = ($taskBackupJson | ConvertFrom-Json)
    } elseif (Test-Path -LiteralPath $taskExe) {
        throw 'Existing installation but business database not found; verify DatabasePath before upgrading'
    } else { $taskRecord.backup = @{status='not_needed_new_install'} }
    $taskInstaller = ''
    foreach ($taskArtifact in $taskBuild.artifacts) {
        $taskPath = Join-Path "$ReleaseDirectory\installer" $taskArtifact.name
        if ((Get-FileHash -LiteralPath $taskPath -Algorithm SHA256).Hash -ne $taskArtifact.sha256) { throw 'Installer artifact hash mismatch' }
        if ($taskArtifact.name.EndsWith('.exe')) { $taskInstaller = $taskPath; $taskRecord.installerSha256 = $taskArtifact.sha256 }
    }
    if (-not $taskInstaller -or (Get-Item -LiteralPath $taskInstaller).Length -gt 16MB) { throw 'Invalid split installer launcher' }
    if (@($taskBuild.artifacts | Where-Object { $_.name.EndsWith('.bin') }).Count -eq 0) { throw 'Missing installer BIN' }
    # Validate portable contents and require successful packaging probes before changing the installation.
    & $taskPython -X utf8 $taskTool verify-app --release $ReleaseDirectory --source "$ReleaseDirectory\portable\MedExplainStudio"
    Check-Exit 'Portable manifest verification'
    foreach ($taskProbeName in @('parser.json','ui.json','ui-scroll.json')) {
        if (-not (Get-Content -LiteralPath "$ReleaseDirectory\checks\$taskProbeName" -Raw -Encoding UTF8 | ConvertFrom-Json).ok) { throw 'Packaging probe did not pass' }
    }
    $taskRecord.status = 'installing'
    $taskInstallLog = Join-Path $ReleaseDirectory "logs\install-$taskStamp.log"
    $taskRecord.installLog = $taskInstallLog
    Save-Deployment
    $taskArguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS',
        ('/DIR="' + $InstallDirectory + '"'), ('/LOG="' + $taskInstallLog + '"'))
    $taskInstall = Start-Process -FilePath $taskInstaller -ArgumentList $taskArguments -PassThru -Wait -WindowStyle Hidden
    $taskRecord.installExitCode = $taskInstall.ExitCode
    if ($taskInstall.ExitCode -ne 0) { throw 'Installer failed; see installation log. No automatic data rollback was performed.' }
    & $taskPython -X utf8 $taskTool verify-app --release $ReleaseDirectory --source $InstallDirectory
    Check-Exit 'Installed application manifest verification'
    $taskRecord.fileManifestVerified = $true
    if ((Get-Item -LiteralPath $taskExe).VersionInfo.ProductVersion -ne $taskBuild.version) { throw 'Installed version differs from build' }
    $taskChecks = Join-Path $ReleaseDirectory "checks\installed-$taskStamp"
    New-Item -ItemType Directory -Path $taskChecks | Out-Null
    $taskOldData = $env:LOCALAPPDATA
    $taskOldDb = $env:MEDEXPLAIN_DATABASE_PATH
    $taskOldQt = $env:QT_QPA_PLATFORM
    try {
        $env:LOCALAPPDATA = $taskChecks
        $env:MEDEXPLAIN_DATABASE_PATH = Join-Path $taskChecks 'isolated.db'
        $env:QT_QPA_PLATFORM = 'offscreen'
        foreach ($taskProbe in @(@('--bundle-check','--parser-smoke-report',('"' + "$taskChecks\parser.json" + '"')), @('--ui-smoke-report',('"' + "$taskChecks\ui.json" + '"')), @('--ui-scroll-smoke-report',('"' + "$taskChecks\ui-scroll.json" + '"')))) {
            $taskRun = Start-Process -FilePath $taskExe -ArgumentList $taskProbe -Wait -PassThru -WindowStyle Hidden
            if ($taskRun.ExitCode -ne 0) { throw 'Installed app probe failed; retain backup and inspect reports' }
        }
    } finally { $env:LOCALAPPDATA=$taskOldData; $env:MEDEXPLAIN_DATABASE_PATH=$taskOldDb; $env:QT_QPA_PLATFORM=$taskOldQt }
    $taskParser = Get-Content -LiteralPath "$taskChecks\parser.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskUi = Get-Content -LiteralPath "$taskChecks\ui.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskScroll = Get-Content -LiteralPath "$taskChecks\ui-scroll.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $taskParser.unicode_path -or -not $taskUi.indexRefresh.completedReloaded -or -not $taskScroll.indexRefresh.completedReloaded) { throw 'Installed Unicode PDF or index status refresh check missing' }
    if (-not $taskParser.ok -or -not $taskUi.ok -or $taskUi.build.buildId -ne $taskBuild.buildId -or -not $taskScroll.ok -or $taskScroll.build.buildId -ne $taskBuild.buildId -or -not $taskScroll.textbookScroll.nativeWheel -or -not $taskScroll.textbookScroll.crossPageEdits) { throw 'Installed check/build identity mismatch' }
    $taskRecord.parserReport = "$taskChecks\parser.json"
    $taskRecord.uiReport = "$taskChecks\ui.json"
    $taskRecord.scrollReport = "$taskChecks\ui-scroll.json"
    $taskRecord.shortcutTarget = if (Test-Path -LiteralPath $taskShortcut) { $taskShell.CreateShortcut($taskShortcut).TargetPath } else { '' }
    if ($taskRecord.shortcutTarget -and $taskRecord.shortcutTarget -ne $taskExe) { throw 'Desktop shortcut still points at a different application' }
    $taskRecord.status = 'installed_and_verified'
    $taskRecord.completedAt = Get-Date -Format o
    Save-Deployment
    Write-Host "Installed and verified: $taskExe ($($taskBuild.buildId))"
    Write-Host "Deployment record: $taskReportPath"
} catch {
    $taskRecord.status = 'failed'
    $taskRecord.error = $_.Exception.Message
    Save-Deployment
    throw
}
