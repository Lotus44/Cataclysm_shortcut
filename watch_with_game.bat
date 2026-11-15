@echo off
rem ============================================================
rem  Start Cataclysm Shortcut in "always-on watch" mode.
rem
rem  Why this exists: CDDA is often added to Steam as a NON-STEAM
rem  shortcut, and by default Steam can only launch
rem  cataclysm-tiles.exe itself -- it cannot pass arguments, so it
rem  cannot also start this tool. This script keeps the tool
rem  resident in the background instead: it registers its hotkeys,
rem  sits in the tray, and watches for the game process. Launch the
rem  game from Steam, the desktop, or a command line -- the tool is
rem  already there.
rem
rem  NOTE: if you instead point the Steam shortcut at
rem    pythonw.exe  +  Startup Options: "cdda_snapshot.pyw" --steam-launch
rem  (see README, Steam section), autostarting this script is fine
rem  too: Steam's launch drops a steam_takeover.flag, and this
rem  resident copy sees it on its next watch tick (within 2.5 s),
rem  exits quietly and hands the single-instance mutex over -- so the
rem  Steam-launched copy takes over and playtime keeps counting.
rem  Only if the resident copy is busy with a snapshot/restore does
rem  it take up to 12 s (then Steam's launch gives up; retry).
rem
rem  Put this file (or a shortcut to it) in your Startup folder:
rem    Win+R  ->  shell:startup  ->  drop a shortcut here
rem
rem  IMPORTANT: keep this file pure ASCII with CRLF line endings.
rem  cmd.exe parses batch files in the ANSI codepage (936 on Chinese
rem  Windows) and rejects LF-only endings, which silently eats the
rem  first character of each line.
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist "%~dp0cdda_snapshot.pyw" (
    echo [ERROR] cdda_snapshot.pyw not found next to this file.
    pause
    exit /b 1
)

where pythonw >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pythonw.exe not found. Install Python 3.9+ first.
    pause
    exit /b 1
)

rem --autostart turns on the process watcher.
rem pythonw has no console, so this is silent.
start "" pythonw.exe "%~dp0cdda_snapshot.pyw" --autostart

exit /b 0
