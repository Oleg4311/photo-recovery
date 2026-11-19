$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSetup = Join-Path $taskRoot 'release\PhotoRecovery-0.7-Setup.exe'
$taskLog = Join-Path $PSScriptRoot 'install-test.log'
$taskInstall = Start-Process -FilePath $taskSetup -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=russian', '/MERGETASKS=desktopicon', ('/LOG="' + $taskLog + '"')) -WindowStyle Hidden -Wait -PassThru
if ($taskInstall.ExitCode -ne 0) { throw "Installation failed: $($taskInstall.ExitCode)" }
$taskApp = Join-Path $env:LOCALAPPDATA 'Programs\Photo Recovery\PhotoRecovery.exe'
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $taskApp).Hash -ne (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $taskRoot 'dist\PhotoRecovery\PhotoRecovery.exe')).Hash) { throw 'Installed executable differs from the tested build' }
$taskDesktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Photo Recovery.lnk'
$taskMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Photo Recovery.lnk'
$taskShell = New-Object -ComObject WScript.Shell
foreach ($taskLink in @($taskDesktop, $taskMenu)) {
    if (-not (Test-Path -LiteralPath $taskLink)) { throw "Shortcut missing: $taskLink" }
    if ($taskShell.CreateShortcut($taskLink).TargetPath -ne $taskApp) { throw 'Shortcut target is incorrect' }
}
$taskAppProcess = Start-Process -FilePath $taskDesktop -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 3
    $taskAppProcess.Refresh()
    if ($taskAppProcess.HasExited -or $taskAppProcess.MainWindowTitle -notmatch 'Photo Recovery 0.7') { throw 'Application did not start from its desktop shortcut' }
    if (-not $taskAppProcess.CloseMainWindow()) { throw 'Application did not close normally' }
    if (-not $taskAppProcess.WaitForExit(10000)) { throw 'Application close timed out' }
} finally {
    $taskAppProcess.Refresh()
    if (-not $taskAppProcess.HasExited) { Stop-Process -Id $taskAppProcess.Id }
}
if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $taskApp) 'unins000.exe'))) { throw 'Uninstaller missing' }
@{installed_executable=$taskApp; desktop_shortcut=$taskDesktop; start_menu_shortcut=$taskMenu; shortcut_launch='passed'; normal_close='passed'; uninstall_present=$true} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'install-test.json')
'Installation and shortcut launch: OK'
