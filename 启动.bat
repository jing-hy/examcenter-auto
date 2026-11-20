@echo off
rem ---------------------------------------------------------------------------
rem This launcher is intentionally ASCII-only: cmd.exe may read .bat files with
rem the active OEM code page, and Chinese text here would garble on some Win7.
rem All Chinese messages are printed by Python instead.
rem ---------------------------------------------------------------------------
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run_autoexam.py %*
    goto :done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run_autoexam.py %*
    goto :done
)

echo.
echo   [ERROR] Python not found.
echo.
echo   1) Install Python 3.8+ (Win7 supports up to 3.8.10), or
echo   2) Run tools\make_portable.ps1 once, then use launcher-portable.bat
echo.
pause

:done
set "PYTHONIOENCODING="
set "PYTHONUTF8="
endlocal
