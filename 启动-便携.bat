@echo off
rem Portable launcher: uses the embedded Python runtime created by
rem tools\make_portable.ps1 (no system Python required, works on Win7).
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist "runtime\python.exe" (
    echo.
    echo   [ERROR] runtime\python.exe not found.
    echo   Run this once in PowerShell from the project folder:
    echo       powershell -ExecutionPolicy Bypass -File tools\make_portable.ps1
    echo.
    pause
    goto :done
)

"runtime\python.exe" run_autoexam.py %*

:done
set "PYTHONIOENCODING="
set "PYTHONUTF8="
endlocal
