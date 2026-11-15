@echo off
rem ============================================================
rem  Cataclysm Shortcut launcher (CDDA / CCB save snapshot tool).
rem  IMPORTANT: keep this file pure ASCII with CRLF line endings.
rem  cmd.exe parses batch files using the OEM/ANSI codepage (936
rem  on Chinese Windows) and does not accept LF-only line endings,
rem  so non-ASCII text or LF endings break the script.
rem ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Cataclysm Shortcut - starting in background
echo ------------------------------------------------------------
echo   F4           Open / close the panel
echo   F5           Take a snapshot (saves in-game first)
echo   Ctrl+F5      Snapshot the save files already on disk
echo   F6           Restore the latest snapshot (auto restart)
echo   Ctrl+Alt+Q   Quit the tool
echo ------------------------------------------------------------
echo   Tray icon    Left click = panel, right click = menu
echo   Snapshots : .cdda_snapshots\^<world^>\#001 ...
echo   Settings  : .cdda_snapshots\config.json
echo   Log       : .cdda_snapshots\cdda_snapshot.log
echo ============================================================
echo.

if not exist "%~dp0cdda_snapshot.pyw" goto noscript

where pythonw >nul 2>nul
if errorlevel 1 goto nopython

start "" pythonw.exe "%~dp0cdda_snapshot.pyw" %*
exit /b 0

:noscript
echo [ERROR] cdda_snapshot.pyw was not found next to this file.
echo         Keep all tool files together in this folder.
echo.
pause
exit /b 1

:nopython
echo [ERROR] pythonw.exe was not found.
echo.
echo This tool needs Python 3.9 or newer.
echo Download: https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH".
echo.
pause
exit /b 1
