$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskTools = Join-Path $taskRoot 'build-tools'
$taskCompilerDir = Join-Path $taskTools 'inno-6.7.4'
$taskCompiler = Join-Path $taskCompilerDir 'ISCC.exe'
New-Item -ItemType Directory -Force -Path $taskTools | Out-Null
if (-not (Test-Path -LiteralPath $taskCompiler)) {
    $taskDownload = Join-Path $taskTools 'innosetup-6.7.4.exe'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe' -OutFile $taskDownload
    $taskSignature = Get-AuthenticodeSignature -LiteralPath $taskDownload
    if ($taskSignature.Status -ne 'Valid') { throw "Compiler signature check failed: $($taskSignature.Status)" }
    $taskProcess = Start-Process -FilePath $taskDownload -ArgumentList @('/PORTABLE=1', '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', ('/DIR="' + $taskCompilerDir + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($taskProcess.ExitCode -ne 0) { throw "Compiler extraction failed: $($taskProcess.ExitCode)" }
}
& $taskCompiler /Q (Join-Path $PSScriptRoot 'PhotoRecovery.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer build failed' }
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $taskRoot 'release\PhotoRecovery-0.7-Setup.exe')
