$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskTest = Join-Path $taskRoot 'tests\native_device_smoke.py'
$taskResult = Join-Path $taskRoot 'benchmark\v05-native\result.json'
$taskAdmin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($taskAdmin) {
    & $taskPython $taskTest
    if ($LASTEXITCODE -ne 0) { throw 'Native test failed' }
} else {
    $taskArgs = @('"' + $taskTest + '"')
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($taskProcess.ExitCode -ne 0) { throw "Native test failed: $($taskProcess.ExitCode). See benchmark\v05-native\error.txt" }
}
Get-Content -LiteralPath $taskResult
