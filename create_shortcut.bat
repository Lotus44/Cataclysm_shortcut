@echo off
rem Creates Cataclysm_shortcut.lnk next to this script.
rem Keep this file pure ASCII with CRLF endings.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python "%~dp0make_shortcut.py" "%~dp0"
if errorlevel 1 goto failed

echo.
echo Done. Cataclysm_shortcut.lnk was created in this folder.
echo Double-click that .lnk to start the tool with no console window.
echo.
pause
exit /b 0

:nopython
echo.
echo [ERROR] python.exe was not found. Install Python 3.9+ first,
echo         or just double-click start_cataclysm_shortcut.bat.
echo.
pause
exit /b 1

:failed
echo.
echo [ERROR] Failed to create the shortcut.
echo         Just double-click start_cataclysm_shortcut.bat instead.
echo.
pause
exit /b 1
