@echo off
REM ---------------------------------------------------------------------------
REM  Auto-parse FPSci results into CSV.
REM
REM  Wired to run when you START any scenario, via "commandsOnSessionStart" in
REM  experimentconfig.Any, and it parses everything logged up to that moment --
REM  so finishing a run and picking the next one refreshes the CSVs.
REM
REM  play.bat also calls this once FPSci exits, which is what captures your final
REM  run of a sitting (nothing starts after it, so the session-start hook cannot).
REM  Double-clicking this by hand any time is still safe.
REM
REM  Reads : FPSci-bin\results\*.db          (every subject's database)
REM  Writes: FPSci-bin\analysis\trials.csv, shots.csv, targets.csv, sessions.csv
REM          and summary.json  -- rewritten in full on every run
REM  Log   : FPSci-bin\analysis\parse_log.txt  -- check here if a CSV looks stale
REM
REM  IMPORTANT: the databases are COPIED to a temp folder and the copies are
REM  parsed. FPSci opens the same .db again on the next session, and sqlite only
REM  allows one writer -- reading the live file makes FPSci's inserts fail with
REM  "database is locked" and silently drop logged rows. Never point the parser
REM  at results\ directly while the game is open.
REM
REM  Drop the --no-raw flag below to also dump every database table verbatim
REM  into analysis\raw\ (adds ~40 MB and ~6 s per run).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM Absolute path first so this works no matter what PATH the game inherited.
set "PY=C:\Users\mokal\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

set "SNAP=%TEMP%\fpsci_db_snapshot"

if not exist "analysis" mkdir "analysis"

REM A parse that gets killed mid-run (closing the play.bat window, Ctrl+C, or the
REM machine going down) leaves its lock behind, and a stranded lock would silently
REM block every future parse. Clear any lock older than 10 minutes first: a real
REM parse takes seconds, so anything that old is definitely dead.
powershell -NoProfile -Command "$l='analysis\parse.lock'; if (Test-Path $l) { if (((Get-Date) - (Get-Item $l).LastWriteTime).TotalMinutes -gt 10) { Remove-Item $l -Force } }" >nul 2>&1

REM Skip if a parse really is still running, so two runs cannot interleave their
REM writes into the same CSVs.
if exist "analysis\parse.lock" (
    echo [%DATE% %TIME%] skipped: a parse was already running >> "analysis\parse_log.txt"
    exit /b 0
)
> "analysis\parse.lock" echo running

echo. >> "analysis\parse_log.txt"
echo ==== [%DATE% %TIME%] parse start ==== >> "analysis\parse_log.txt"

if exist "%SNAP%" rd /s /q "%SNAP%"
mkdir "%SNAP%"
copy /y "results\*.db" "%SNAP%\" >> "analysis\parse_log.txt" 2>&1

"%PY%" "..\FPSci\scripts\analysis\fpsci_parse.py" "%SNAP%" -o "analysis" --no-raw >> "analysis\parse_log.txt" 2>&1
echo ==== exit code %ERRORLEVEL% ==== >> "analysis\parse_log.txt"

rd /s /q "%SNAP%"

del "analysis\parse.lock"
endlocal
