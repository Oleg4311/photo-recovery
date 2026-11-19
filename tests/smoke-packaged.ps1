$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskOutput = Join-Path $taskRoot 'benchmark\v05-packaged'
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
$taskCatalogueText = & '.\dist\PhotoRecoveryCLI\PhotoRecoveryCLI.exe' '.\benchmark\v05-gui\selected.img' --list-deleted
if ($LASTEXITCODE -ne 0) { throw 'Packaged catalogue failed' }
$taskCatalogue = ($taskCatalogueText -join "`n") | ConvertFrom-Json
$taskTarget = $taskCatalogue.entries | Where-Object name -eq 'main.jpg' | Select-Object -First 1
if (-not $taskTarget) { throw 'Selected file missing from packaged catalogue' }
& '.\dist\PhotoRecoveryCLI\PhotoRecoveryCLI.exe' '.\benchmark\v05-gui\selected.img' $taskOutput --select $taskTarget.id --deep --all-types --search-mib 1
if ($LASTEXITCODE -ne 0) { throw 'Packaged CLI failed' }
$taskExpected = (Get-Content -Raw -LiteralPath '.\benchmark\v05-gui\result.json' | ConvertFrom-Json).selected_jpeg_exact_sha256
$taskFound = Get-ChildItem -LiteralPath $taskOutput -Recurse -Filter '*.jpg' | Where-Object {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash -eq $taskExpected
}
if (-not $taskFound) { throw 'Packaged CLI did not recover the exact JPEG' }
$taskGui = Start-Process -FilePath '.\dist\PhotoRecovery\PhotoRecovery.exe' -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 3
    $taskGui.Refresh()
    if ($taskGui.HasExited) { throw 'Packaged GUI exited during startup' }
    if ($taskGui.MainWindowTitle -notmatch 'Photo Recovery 0.7') { throw 'Packaged GUI did not create its window' }
    if (-not $taskGui.CloseMainWindow()) { throw 'Packaged GUI did not accept normal close' }
    if (-not $taskGui.WaitForExit(10000)) { throw 'Packaged GUI did not exit' }
} finally {
    $taskGui.Refresh()
    if (-not $taskGui.HasExited) { Stop-Process -Id $taskGui.Id }
}
'Packaged catalogue, selected deep CLI recovery and GUI startup/close: OK' | Set-Content -Encoding UTF8 -LiteralPath '.\benchmark\v05-packaged\result.txt'
