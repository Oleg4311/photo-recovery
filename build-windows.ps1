$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv failed' }
}
& $taskPython -m pip install -r requirements.txt Pillow==13.0.0 pyinstaller==6.22.3
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& $taskPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
& $taskPython -m PyInstaller --noconfirm --clean --onedir --windowed --name PhotoRecovery --paths src src\app.py
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
& $taskPython -m PyInstaller --noconfirm --clean --onedir --console --name PhotoRecoveryCLI --paths src src\cli.py
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed' }
