@echo off
setlocal
pushd "%~dp0" || exit /b 1
where py >nul 2>nul
if not errorlevel 1 (
    set "BOOTSTRAP=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Install Python 3.11 or later from python.org and run this file again.
        pause
        exit /b 1
    )
    set "BOOTSTRAP=python"
)
if not exist ".venv\Scripts\python.exe" (
    %BOOTSTRAP% -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "from PIL import ImageTk" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" src\app.py
if errorlevel 1 goto failed
popd
exit /b 0

:failed
echo Failed to start Photo Recovery. See the error above.
pause
popd
exit /b 1
